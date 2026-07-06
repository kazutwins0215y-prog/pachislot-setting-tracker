"""
app_b.py — 振り返り分析ダッシュボード + 狙い目メモ

【機能B-詳細: 振り返り分析ダッシュボード】
  用途: 蓄積データからのパターン検出結果・店舗プロファイルをじっくり見る
  内容: 各サブスコアの値と内訳 / 信頼度 / γ_store / 複数店舗の横並び比較

【機能B-簡潔: 狙い目メモ】
  用途: 次回「家を出る前」に確認する要約
  内容: 狙い目店舗のランキング(上位3〜5) / 各店舗の熱い台(上位2〜3台) /
        根拠の一言要約 / 信頼度(データが薄い店は別枠)

両方とも: 詳細は持たず「内訳と信頼度を必ず併記」する方針

依存: score.py (合成スコア・store_profile)

実行方法:
  streamlit run app_b.py -- --mode detail
  python app_b.py --mode simple
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import data_source as ds

_WEIGHTS_PATH = Path(__file__).parent / 'weights.json'

# store_profile.パターン列の値 → 表示名
_PATTERN_LABELS: dict[str, str] = {
    's_all':     'S_全台系',
    's_teppan':  'S_鉄板台',
    's_rote':    'S_ローテ',
    's_shintai': 'S_新台増台',
    's_idoudai': 'S_移動台',
    's_sueki':   'S_据え置き',
    's_kadou':   'S_稼働低さ',
}

_DEFAULT_WEIGHTS: dict[str, float] = {
    'S_全台系':   1.5,
    'S_鉄板台':   1.0,
    'S_ローテ':   1.0,
    'S_新台増台': 1.0,
    'S_移動台':   1.0,
    'S_据え置き': 1.0,
    'S_稼働低さ': 1.5,
}

# 信頼度が低い店舗として別枠扱いする閾値
_LOW_RELIABILITY_THRESHOLD = 0.4
# 上位店舗数（簡潔メモ）
_TOP_STORES = 5
# 各店舗で表示する熱い台数（簡潔メモ）
_HOT_MACHINES_N = 3


# ── ユーティリティ ────────────────────────────────────────────────


def load_weights() -> dict[str, float]:
    if _WEIGHTS_PATH.exists():
        with open(_WEIGHTS_PATH, encoding='utf-8') as f:
            return json.load(f)
    return _DEFAULT_WEIGHTS


def _load_profile_from_db(db_path: str) -> pd.DataFrame:
    """単一DBから store_profile を読み込む。テーブルがなければ空DFを返す。"""
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'store_profile' not in tables:
                return pd.DataFrame()
            df = pd.read_sql_query('SELECT * FROM store_profile', con)
            df['db_path'] = db_path
            return df
        except Exception:
            return pd.DataFrame()
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()


def load_all_profiles() -> pd.DataFrame:
    """分析DB(analysis.db)の store_profile を全店舗分読み込んで返す。"""
    if ds.ANALYSIS_DB_PATH.exists():
        profiles = _load_profile_from_db(str(ds.ANALYSIS_DB_PATH))
        if not profiles.empty:
            return profiles
    return pd.DataFrame(
        columns=['ホール名', 'パターン', 'スコア', '信頼度',
                 'gamma_store', '更新日時', 'db_path']
    )


def synthesize_scores(profiles: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    """
    store_profile DataFrameから店舗ごとの合成スコア・平均信頼度・最高サブスコアを返す。
    スコアが NaN のパターンは除外して再正規化する（score.py の synthesize と同方針）。
    有効重み = weights.get(label,1.0) × 信頼度 とし、信頼度が低いサブスコアほど
    合成への寄与を減衰させる(score.py の synthesize と同じ方針。片方だけ直すと
    機能A/B・機能B内の一言メモとダッシュボードで数値が食い違うため両方に反映)。

    Returns:
        ホール名 / 合成スコア / 平均信頼度 / 最高サブスコア / db_path を持つ DataFrame
        合成スコア降順にソート済み
    """
    if profiles.empty:
        return pd.DataFrame()

    rows = []
    for hole_name, grp in profiles.groupby('ホール名'):
        numerator = 0.0
        denominator = 0.0
        best_label: str | None = None
        best_val = -np.inf
        reliabilities: list[float] = []

        for _, row in grp.iterrows():
            label = _PATTERN_LABELS.get(str(row['パターン']), str(row['パターン']))
            score = row['スコア']
            rel = row['信頼度']

            if pd.isna(score):
                continue

            rel_val = float(rel) if not pd.isna(rel) else 1.0
            w = float(weights.get(label, 1.0)) * rel_val
            numerator += w * float(score)
            denominator += w
            reliabilities.append(rel_val if not pd.isna(rel) else 0.0)

            if float(score) > best_val:
                best_val = float(score)
                best_label = label

        synth = numerator / denominator if denominator > 0 else np.nan
        avg_rel = float(np.mean(reliabilities)) if reliabilities else 0.0
        db_path = grp['db_path'].iloc[0] if 'db_path' in grp.columns else ''

        rows.append({
            'ホール名': hole_name,
            '合成スコア': synth,
            '平均信頼度': avg_rel,
            '最高サブスコア': best_label,
            'db_path': db_path,
        })

    return (
        pd.DataFrame(rows)
        .sort_values('合成スコア', ascending=False, na_position='last')
        .reset_index(drop=True)
    )


def _pivot_profiles(profiles: pd.DataFrame) -> pd.DataFrame:
    """
    store_profile を横持ちに変換:
      ホール名 × (パターン別スコア・信頼度・γ_store・更新日時) の表形式
    """
    if profiles.empty:
        return pd.DataFrame()

    rows = []
    for hole_name, grp in profiles.groupby('ホール名'):
        row: dict = {'ホール名': hole_name}
        for _, r in grp.iterrows():
            label = _PATTERN_LABELS.get(str(r['パターン']), str(r['パターン']))
            row[f'{label}_スコア'] = r['スコア']
            row[f'{label}_信頼度'] = r['信頼度']

        if 'gamma_store' in grp.columns:
            gv = grp['gamma_store'].dropna()
            row['γ_store'] = float(gv.iloc[0]) if not gv.empty else None

        if '更新日時' in grp.columns:
            row['更新日時'] = grp['更新日時'].max()

        rows.append(row)

    return pd.DataFrame(rows)


def load_hot_machines(db_path: str, hole_name: str, n: int = _HOT_MACHINES_N) -> list[dict]:
    """
    stage3_scores の最新日において高設定確率が高い台を上位 n 件返す。
    テーブルがなければ空リストを返す。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'stage3_scores' not in tables:
                return []

            df = pd.read_sql_query(
                """
                SELECT 日付, 機種名, 台番号, high_prob
                FROM stage3_scores
                WHERE ホール名 = ?
                  AND (is_invalid IS NULL OR is_invalid != 1)
                ORDER BY 日付 DESC
                """,
                con,
                params=(hole_name,),
            )
        finally:
            con.close()

        if df.empty:
            return []

        latest_date = df['日付'].max()
        latest = df[df['日付'] == latest_date].dropna(subset=['high_prob'])
        if latest.empty:
            return []

        return (
            latest
            .sort_values('high_prob', ascending=False)
            .head(n)[['機種名', '台番号', 'high_prob']]
            .to_dict('records')
        )
    except Exception:
        return []


def _rel_bar(value: float, width: int = 8) -> str:
    """信頼度を ████░░░░ 形式の文字列で返す（テキストメモ用）。"""
    filled = round(value * width)
    return '█' * filled + '░' * (width - filled)


# ── Phase 5: 検知期間履歴・カレンダーヒートマップ用ユーティリティ ──────────

# 検知期間判定のしきい値(暫定値。「スコア > 0」を検出中とみなす。実データで調整)
_PERIOD_SCORE_THRESHOLD = 0.0
_WEEKDAY_LABELS = ['月', '火', '水', '木', '金', '土', '日']


def _load_pattern_history(db_path: str) -> pd.DataFrame:
    """pattern_history テーブルを全店舗分読み込む。テーブルが無ければ空DFを返す。"""
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'pattern_history' not in tables:
                return pd.DataFrame()
            return pd.read_sql_query('SELECT * FROM pattern_history', con)
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()


def _detect_pattern_periods(
    hist_hole: pd.DataFrame, threshold: float = _PERIOD_SCORE_THRESHOLD
) -> pd.DataFrame:
    """
    pattern_history(単一店舗分)から「スコア > threshold」が連続している区間を
    検出期間として近似抽出する(統計検定の再実行はしない軽量な後処理)。
    区間は実行日時の最小〜最大でまとめる。NaN・しきい値以下は区間を打ち切る。
    """
    empty = pd.DataFrame(columns=['パターン', '開始', '終了', '実行回数', '平均スコア', '平均信頼度'])
    if hist_hole.empty:
        return empty

    rows = []
    for pattern, grp in hist_hole.sort_values('実行日時').groupby('パターン'):
        above = (grp['スコア'] > threshold).fillna(False).to_numpy()
        if not above.any():
            continue
        run_id = (above != np.concatenate(([False], above[:-1]))).cumsum()
        grp = grp.assign(_above=above, _run=run_id)
        for _, seg in grp[grp['_above']].groupby('_run'):
            rows.append({
                'パターン': _PATTERN_LABELS.get(pattern, pattern),
                '開始': seg['実行日時'].min(),
                '終了': seg['実行日時'].max(),
                '実行回数': len(seg),
                '平均スコア': float(seg['スコア'].mean()),
                '平均信頼度': (
                    float(seg['信頼度'].mean()) if seg['信頼度'].notna().any() else np.nan
                ),
            })

    return pd.DataFrame(rows) if rows else empty


def _load_daily_stage3_avg(db_path: str, hole_name: str) -> pd.Series:
    """stage3_scoresからhigh_probの日次平均(is_invalid除外)を返す(日付→平均値)。"""
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'stage3_scores' not in tables:
                return pd.Series(dtype=float)
            df = pd.read_sql_query(
                """
                SELECT 日付, high_prob
                FROM stage3_scores
                WHERE ホール名 = ?
                  AND (is_invalid IS NULL OR is_invalid != 1)
                  AND high_prob IS NOT NULL
                """,
                con, params=(hole_name,),
            )
        finally:
            con.close()
    except Exception:
        return pd.Series(dtype=float)

    if df.empty:
        return pd.Series(dtype=float)
    return df.groupby('日付')['high_prob'].mean()


def _month_grid(score_by_day: dict[str, float], year: int, month: int) -> tuple[np.ndarray, np.ndarray]:
    """
    year年month月のカレンダー形式(週×曜日)グリッドを作る。
    score_by_day: 'YYYY-MM-DD' → スコアの辞書。月内でデータが無い日はNaN。
    Returns: (z: 週数×7 のスコア行列, text: 同形状の「日」表示用文字列行列)
    """
    import calendar

    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    z = np.full((len(weeks), 7), np.nan)
    text = np.full((len(weeks), 7), '', dtype=object)
    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            if day == 0:
                continue
            text[wi, di] = str(day)
            date_str = f'{year:04d}-{month:02d}-{day:02d}'
            if date_str in score_by_day:
                z[wi, di] = score_by_day[date_str]
    return z, text


# ── 公開関数 ──────────────────────────────────────────────────────


def load_store_profiles(db_path: str) -> pd.DataFrame:
    """store_profile テーブルを全店舗分読み込む。"""
    return _load_profile_from_db(db_path)


def dashboard_detail(profiles: pd.DataFrame) -> None:
    """
    機能B-詳細: Streamlit で全サブスコア・内訳・信頼度・γ_storeを表示する。
    """
    import streamlit as st
    import plotly.express as px
    import plotly.graph_objects as go

    if profiles.empty:
        st.warning(
            'store_profile データがありません。'
            'fase2/run_store_profile.py を先に実行してください。'
        )
        return

    weights = load_weights()
    synth_df = synthesize_scores(profiles, weights)
    pivot_df = _pivot_profiles(profiles)

    st.caption(
        'サブスコアは符号付き([-1, 1]): プラス=そのパターンが強く出ている、'
        'マイナス=弱い(該当日が少ない/非該当日が多い)ことを示します。'
    )

    # ── サイドバー設定 ──
    st.sidebar.subheader('表示設定')
    min_rel = st.sidebar.slider('最低信頼度フィルタ', 0.0, 1.0, 0.0, 0.05)
    show_gamma = st.sidebar.checkbox('γ_store を表示', value=True)

    # ── 1. 店舗ランキング ──
    st.subheader('店舗ランキング（合成スコア）')

    if not synth_df.empty:
        disp = synth_df[synth_df['平均信頼度'] >= min_rel].copy()
        disp['合成スコア'] = disp['合成スコア'].map(
            lambda x: f'{x:.3f}' if pd.notna(x) else 'N/A'
        )
        disp['平均信頼度'] = disp['平均信頼度'].map(lambda x: f'{x:.0%}')
        st.dataframe(
            disp[['ホール名', '合成スコア', '平均信頼度', '最高サブスコア']],
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    # ── 2. サブスコア棒グラフ比較 ──
    st.subheader('サブスコア比較（店舗横断）')

    score_cols = [
        col for col in pivot_df.columns if col.endswith('_スコア')
    ] if not pivot_df.empty else []

    if score_cols and not pivot_df.empty:
        pattern_names = [c.replace('_スコア', '') for c in score_cols]
        sel_pattern = st.selectbox('サブスコア選択', pattern_names, key='detail_pattern')
        sel_col = f'{sel_pattern}_スコア'
        rel_col = f'{sel_pattern}_信頼度'

        bar_df = pivot_df[['ホール名', sel_col]].dropna(subset=[sel_col]).copy()
        bar_df = bar_df.rename(columns={sel_col: 'スコア'})

        if rel_col in pivot_df.columns:
            bar_df = bar_df.merge(
                pivot_df[['ホール名', rel_col]].rename(columns={rel_col: '信頼度'}),
                on='ホール名', how='left',
            )
            # 最低信頼度でフィルタ
            bar_df = bar_df[bar_df['信頼度'].fillna(0) >= min_rel]

        bar_df = bar_df.sort_values('スコア', ascending=True)

        if not bar_df.empty:
            color_col = '信頼度' if '信頼度' in bar_df.columns else None
            fig = px.bar(
                bar_df, x='スコア', y='ホール名', orientation='h',
                color=color_col,
                color_continuous_scale='Blues',
                range_x=[-1, 1],
                title=f'{sel_pattern} — 店舗別スコア比較',
            )
            fig.update_layout(height=max(280, len(bar_df) * 42 + 80))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info('表示できるデータがありません（信頼度フィルタを緩めてください）。')
    else:
        st.info('サブスコアデータがありません。')

    st.divider()

    # ── 3. 全サブスコアヒートマップ ──
    st.subheader('全サブスコアヒートマップ')

    if score_cols and not pivot_df.empty:
        heat_data = pivot_df.set_index('ホール名')[score_cols].copy()
        heat_data.columns = [c.replace('_スコア', '') for c in heat_data.columns]

        if not heat_data.dropna(how='all').empty:
            fig_heat = px.imshow(
                heat_data,
                color_continuous_scale='RdBu',
                labels=dict(x='サブスコア', y='ホール名', color='スコア'),
                title='店舗 × サブスコア ヒートマップ',
                zmin=-1, zmax=1,
                aspect='auto',
            )
            fig_heat.update_layout(height=max(280, len(heat_data) * 38 + 100))
            st.plotly_chart(fig_heat, use_container_width=True)
        else:
            st.info('ヒートマップ用データがありません。')
    else:
        st.info('サブスコアデータがありません。')

    st.divider()

    # ── 4. 個別店舗詳細 ──
    st.subheader('個別店舗詳細')

    hole_names = profiles['ホール名'].unique().tolist()
    sel_hole = st.selectbox('ホール名', sorted(hole_names), key='detail_hole')

    hole_grp = profiles[profiles['ホール名'] == sel_hole].copy()
    hole_grp['表示名'] = hole_grp['パターン'].map(_PATTERN_LABELS)

    col1, col2 = st.columns([1, 1])

    with col1:
        st.markdown(f'**{sel_hole} — サブスコア内訳**')

        tbl = hole_grp[['表示名', 'スコア', '信頼度']].copy()
        tbl['スコア'] = tbl['スコア'].map(lambda x: f'{x:.3f}' if pd.notna(x) else '─')
        tbl['信頼度'] = tbl['信頼度'].map(lambda x: f'{x:.0%}' if pd.notna(x) else '─')
        st.dataframe(tbl, use_container_width=True, hide_index=True)

        # γ_store
        if 'gamma_store' in hole_grp.columns and show_gamma:
            gv = hole_grp['gamma_store'].dropna()
            if not gv.empty:
                st.metric('γ_store', f'{float(gv.iloc[0]):.4f}')
            else:
                st.caption('γ_store: 未学習（Stage5・複数店舗データが必要）')

        if '更新日時' in hole_grp.columns:
            st.caption(f'最終更新: {hole_grp["更新日時"].max()}')

    with col2:
        valid = hole_grp.dropna(subset=['スコア']).copy()
        valid['表示名'] = valid['パターン'].map(_PATTERN_LABELS)
        if not valid.empty:
            fig_bar = px.bar(
                valid,
                x='スコア', y='表示名', orientation='h',
                color='信頼度', color_continuous_scale='Blues',
                range_x=[-1, 1],
                title=f'{sel_hole} サブスコア',
            )
            fig_bar.update_layout(
                height=max(280, len(valid) * 45 + 80),
                margin=dict(t=40, b=20),
            )
            st.plotly_chart(fig_bar, use_container_width=True)

    # 低信頼度の警告
    low_rel = hole_grp[hole_grp['信頼度'].fillna(0) < _LOW_RELIABILITY_THRESHOLD]
    if not low_rel.empty:
        low_names = low_rel['パターン'].map(_PATTERN_LABELS).dropna().tolist()
        st.warning(
            f'⚠ 信頼度が低いサブスコア: {", ".join(low_names)}'
            ' — データを蓄積してから再確認してください。'
        )

    st.divider()

    # ── 5. 検知期間履歴 ──
    st.subheader(f'{sel_hole} — 検知期間履歴')
    st.caption(
        'pattern_history(run_store_profile.py実行ごとの記録)から'
        '「スコアが0を上回っている」連続区間を検出期間として近似表示します'
        '(統計検定の再実行はしません)。'
    )

    hist_all = _load_pattern_history(str(ds.ANALYSIS_DB_PATH))
    hist_hole = (
        hist_all[hist_all['ホール名'] == sel_hole].copy()
        if not hist_all.empty else pd.DataFrame()
    )

    periods = _detect_pattern_periods(hist_hole)
    if periods.empty:
        st.info(
            '検出期間履歴がまだありません。run_store_profile.pyを複数回実行すると蓄積されます'
            '(fase4の日次自動実行が実装されるまでは手動実行の頻度に応じた粒度になります)。'
        )
    else:
        disp_periods = periods.sort_values('終了', ascending=False).copy()
        disp_periods['平均スコア'] = disp_periods['平均スコア'].map(lambda x: f'{x:.3f}')
        disp_periods['平均信頼度'] = disp_periods['平均信頼度'].map(
            lambda x: f'{x:.0%}' if pd.notna(x) else '─'
        )
        st.dataframe(disp_periods, use_container_width=True, hide_index=True)

    st.divider()

    # ── 6. 当月カレンダーヒートマップ(2層) ──
    st.subheader(f'{sel_hole} — カレンダーヒートマップ（店舗内の絶対評価）')
    st.caption(
        '他店との比較ではなく、この店舗の中で相対的に強い日/弱い日を示します。'
        '1枚目は日次平均high_prob(stage3_scores)、2枚目以降はパターン別(pattern_history)の内訳です。'
    )

    daily_avg = _load_daily_stage3_avg(str(ds.ANALYSIS_DB_PATH), sel_hole)

    hist_hole_dated = pd.DataFrame()
    if not hist_hole.empty:
        hist_hole_dated = hist_hole.copy()
        hist_hole_dated['日付'] = pd.to_datetime(hist_hole_dated['実行日時']).dt.strftime('%Y-%m-%d')

    available_months = sorted(
        {d[:7] for d in daily_avg.index}
        | ({d for d in hist_hole_dated['日付'].str[:7]} if not hist_hole_dated.empty else set()),
        reverse=True,
    )

    if not available_months:
        st.info('カレンダー表示用のデータがありません。')
    else:
        sel_month = st.selectbox('表示月', available_months, index=0, key='calendar_month')
        year, month = int(sel_month[:4]), int(sel_month[5:7])

        st.markdown('**統合スコア日次平均（high_prob平均・0〜1）**')
        if daily_avg.empty:
            st.caption('stage3_scoresデータがありません。')
        else:
            z, text = _month_grid(daily_avg.to_dict(), year, month)
            fig_cal = go.Figure(data=go.Heatmap(
                z=z, x=_WEEKDAY_LABELS, y=[f'第{i + 1}週' for i in range(z.shape[0])],
                text=text, texttemplate='%{text}',
                colorscale='YlOrRd', zmin=0.0, zmax=1.0,
                hoverongaps=False,
            ))
            fig_cal.update_layout(title=f'{sel_month} 統合スコア日次平均', height=280)
            st.plotly_chart(fig_cal, use_container_width=True)

        if hist_hole_dated.empty:
            st.caption('pattern_historyデータがありません(パターン別の内訳は表示できません)。')
        else:
            pattern_daily = hist_hole_dated.groupby(['パターン', '日付'])['スコア'].mean()
            available_patterns = [
                p for p in _PATTERN_LABELS.keys() if p in hist_hole_dated['パターン'].unique()
            ]
            tabs = st.tabs([_PATTERN_LABELS.get(p, p) for p in available_patterns])
            for tab, pattern in zip(tabs, available_patterns):
                with tab:
                    score_map = pattern_daily.loc[pattern].to_dict()
                    z2, text2 = _month_grid(score_map, year, month)
                    fig_pat = go.Figure(data=go.Heatmap(
                        z=z2, x=_WEEKDAY_LABELS, y=[f'第{i + 1}週' for i in range(z2.shape[0])],
                        text=text2, texttemplate='%{text}',
                        colorscale='RdBu', zmid=0.0, zmin=-1.0, zmax=1.0,
                        hoverongaps=False,
                    ))
                    fig_pat.update_layout(
                        title=f'{sel_month} {_PATTERN_LABELS.get(pattern, pattern)}', height=280
                    )
                    st.plotly_chart(fig_pat, use_container_width=True)


def memo_simple(profiles: pd.DataFrame) -> str:
    """
    機能B-簡潔: 狙い目メモをテキスト形式で返す。
    上位店舗のランキング・熱い台・一言根拠・信頼度を含む。
    """
    if profiles.empty:
        return (
            '【狙い目メモ】\n'
            'データがありません。fase2/run_store_profile.py を先に実行してください。'
        )

    weights = load_weights()
    synth_df = synthesize_scores(profiles, weights)

    if synth_df.empty:
        return '【狙い目メモ】\n合成スコアを計算できませんでした。'

    reliable = synth_df[
        synth_df['平均信頼度'] >= _LOW_RELIABILITY_THRESHOLD
    ].head(_TOP_STORES)

    thin = synth_df[
        synth_df['平均信頼度'] < _LOW_RELIABILITY_THRESHOLD
    ].head(3)

    lines: list[str] = []
    lines.append('=' * 52)
    lines.append('  【狙い目メモ】')
    lines.append(f'  更新: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append('=' * 52)

    # ── 信頼度が十分な店舗ランキング ──
    if reliable.empty:
        lines.append('')
        lines.append(f'⚠ 信頼度 {_LOW_RELIABILITY_THRESHOLD:.0%} 以上の店舗がありません。')
        lines.append('  データを蓄積してください。')
    else:
        lines.append(f'\n■ 狙い目ランキング（上位{len(reliable)}店）')
        lines.append('-' * 52)

        for rank, row in enumerate(synth_df[synth_df['平均信頼度'] >= _LOW_RELIABILITY_THRESHOLD].head(_TOP_STORES).itertuples(), 1):
            synth_val = f'{row.合成スコア:.3f}' if pd.notna(row.合成スコア) else 'N/A'
            rel_val = float(row.平均信頼度)
            reason = str(row.最高サブスコア) if row.最高サブスコア else '根拠不明'
            db_path_val = str(row.db_path) if hasattr(row, 'db_path') and row.db_path else ''

            lines.append(f'\n{rank}位 【{row.ホール名}】')
            lines.append(f'   狙い目度: {synth_val}  信頼度: {rel_val:.0%} {_rel_bar(rel_val)}')
            lines.append(f'   根拠: {reason}が高い')

            # 熱い台
            if db_path_val:
                hot = load_hot_machines(db_path_val, str(row.ホール名), _HOT_MACHINES_N)
                if hot:
                    lines.append('   熱い台:')
                    for m in hot:
                        prob = float(m['high_prob'])
                        machine = str(m['機種名'])
                        unit_no = int(m['台番号'])
                        lines.append(
                            f'     └ {machine} {unit_no}番台'
                            f'  (高設定確率: {prob:.1%})'
                        )
                else:
                    lines.append('   熱い台: Stage3スコアなし（run_store_profile.py を先に実行）')
            else:
                lines.append('   熱い台: DB参照不可')

    # ── データが薄い店舗（別枠） ──
    if not thin.empty:
        lines.append('')
        lines.append('-' * 52)
        lines.append(f'■ 参考: データが薄い店舗（信頼度 {_LOW_RELIABILITY_THRESHOLD:.0%} 未満）')
        for row in thin.itertuples():
            synth_val = f'{row.合成スコア:.3f}' if pd.notna(row.合成スコア) else 'N/A'
            rel_val = float(row.平均信頼度)
            lines.append(
                f'  ・{row.ホール名}'
                f'  狙い目度: {synth_val}  信頼度: {rel_val:.0%} {_rel_bar(rel_val)}'
            )

    lines.append('\n' + '=' * 52)
    return '\n'.join(lines)


# ── Streamlit エントリポイント ────────────────────────────────────


def render_detail() -> None:
    """機能B-詳細の画面本体。単独実行(_run_streamlit_dashboard)・統合アプリ(app.py)の両方から呼べる。"""
    import streamlit as st

    st.title('機能B-詳細: 振り返りダッシュボード')

    if not ds.ANALYSIS_DB_PATH.exists():
        st.error(
            f'分析DBが見つかりません: {ds.ANALYSIS_DB_PATH}\n\n'
            'fase2/run_store_profile.py を先に実行してください。'
        )
        return

    profiles = load_all_profiles()

    with st.spinner('データ読み込み中...'):
        dashboard_detail(profiles)


def _run_streamlit_dashboard() -> None:
    """Streamlit ダッシュボード本体。`streamlit run app_b.py -- --mode detail` で呼ばれる。"""
    import streamlit as st

    st.set_page_config(page_title='機能B: 振り返りダッシュボード', layout='wide')
    render_detail()


# ── メイン ───────────────────────────────────────────────────────


def main() -> None:
    # Windows のコンソール(cp932)は █/░ 等の記号をエンコードできず crash するため utf-8 に固定する。
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(
        description='機能B: 振り返りダッシュボード + 狙い目メモ'
    )
    # choices を使わず文字列として受け取る。
    # Streamlit が内部引数を付加するケースや前方一致("simpl" 等)に対応するため
    # parse_known_args + 前方一致正規化で処理する。
    parser.add_argument('--mode', default='simple',
                        help='detail: Streamlit ダッシュボード / simple: テキストメモ（既定）')
    args, _ = parser.parse_known_args()

    raw = args.mode.strip().lower()
    mode = 'detail' if raw.startswith('d') else 'simple'

    if mode == 'detail':
        _run_streamlit_dashboard()
    else:
        profiles = load_all_profiles()
        print(memo_simple(profiles))


if __name__ == '__main__':
    main()
