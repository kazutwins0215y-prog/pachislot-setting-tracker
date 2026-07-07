"""
app_b.py — 振り返り分析ダッシュボード + 狙い目メモ

【機能B-詳細: 店舗特徴(個別店舗詳細)】
  用途: 蓄積データからのパターン検出結果・店舗プロファイルをじっくり見る
  内容: 各サブスコアの値と内訳 / 信頼度 / γ_store / 検知期間履歴 / カレンダーヒートマップ
    render_store_detail(profiles, 店名) … app.pyの店舗トップページ「店舗特徴」に表示
    (2026-07 UIリニューアルで店舗横断比較render_overviewは削除。
     店舗横断のおすすめ表示はapp_top.render_recommend_stores()に統合済み)

【機能B-簡潔: 狙い目メモ】
  用途: 次回「家を出る前」に確認する要約
  内容: 狙い目店舗のランキング(上位3〜5) / 各店舗の熱い台(上位2〜3台) /
        根拠の一言要約 / 信頼度(データが薄い店は別枠)

両方とも: 詳細は持たず「内訳と信頼度を必ず併記」する方針

依存: score.py (合成スコア・store_profile)

実行方法:
  Streamlit画面は app.py から呼ばれる(単独起動は廃止)
  テキストメモ: python app_b.py
"""
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


def render_store_detail(profiles: pd.DataFrame, sel_hole: str) -> None:
    """
    機能B-個別店舗詳細: サブスコア内訳・γ_store・検知期間履歴・カレンダーヒートマップ。
    app.pyの店舗トップページから店舗固定で呼ばれる。
    """
    import streamlit as st
    import plotly.express as px
    import plotly.graph_objects as go

    import ui_theme as ui

    hole_grp = profiles[profiles['ホール名'] == sel_hole].copy()
    if hole_grp.empty:
        st.info(
            f'{sel_hole} の store_profile データがありません。'
            'fase2/run_store_profile.py を先に実行してください。'
        )
        return

    hole_grp['表示名'] = hole_grp['パターン'].map(_PATTERN_LABELS)

    st.subheader(f'{sel_hole} — サブスコア詳細')

    valid = hole_grp.dropna(subset=['スコア']).copy()
    valid['表示名'] = valid['パターン'].map(_PATTERN_LABELS)
    if not valid.empty:
        valid['表示名_折返し'] = valid['表示名'].map(lambda s: ui.wrap_label(s, 6))
        fig_bar = px.bar(
            valid,
            x='表示名_折返し', y='スコア',
            color='信頼度', color_continuous_scale=ui.SEQ_BLUE,
            range_y=[-1, 1],
            labels={'表示名_折返し': ''},
        )
        ui.apply_mobile_layout(fig_bar, height=320)
        fig_bar.update_xaxes(tickangle=0)
        st.plotly_chart(fig_bar, use_container_width=True, config=ui.PLOTLY_CONFIG)
    else:
        st.info('サブスコアデータがありません。')

    # γ_store
    if 'gamma_store' in hole_grp.columns:
        gv = hole_grp['gamma_store'].dropna()
        if not gv.empty:
            st.metric('γ_store', f'{float(gv.iloc[0]):.4f}')
        else:
            st.caption('γ_store: 未学習（Stage5・複数店舗データが必要）')

    if '更新日時' in hole_grp.columns:
        st.caption(f'最終更新: {hole_grp["更新日時"].max()}')

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
        pattern_tabs = [p for p in _PATTERN_LABELS.values() if p in periods['パターン'].unique()]
        tabs = st.tabs(pattern_tabs)
        for tab, pattern_name in zip(tabs, pattern_tabs):
            with tab:
                disp_periods = (
                    periods[periods['パターン'] == pattern_name]
                    .sort_values('終了', ascending=False)
                    .copy()
                )
                disp_periods['開始'] = pd.to_datetime(disp_periods['開始']).dt.strftime('%m/%d')
                disp_periods['終了'] = pd.to_datetime(disp_periods['終了']).dt.strftime('%m/%d')
                disp_periods['平均スコア'] = disp_periods['平均スコア'].map(lambda x: f'{x:.3f}')
                st.dataframe(
                    disp_periods[['開始', '終了', '平均スコア']],
                    use_container_width=True, hide_index=True,
                )

    st.divider()

    # ── 6. 当月カレンダーヒートマップ ──
    st.subheader(f'{sel_hole} — カレンダーヒートマップ（店舗内の絶対評価）')
    st.caption('他店との比較ではなく、この店舗の中で相対的に強い日/弱い日を示します。')

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
        available_patterns: list[str] = []
        pattern_daily = pd.Series(dtype=float)
        if not hist_hole_dated.empty:
            pattern_daily = hist_hole_dated.groupby(['パターン', '日付'])['スコア'].mean()
            available_patterns = [
                p for p in _PATTERN_LABELS.keys() if p in hist_hole_dated['パターン'].unique()
            ]
        metric_options = ['統合スコア日次平均'] + [_PATTERN_LABELS.get(p, p) for p in available_patterns]

        c1, c2 = st.columns(2)
        with c1:
            sel_month = st.selectbox('表示月', available_months, index=0, key='calendar_month')
        with c2:
            sel_metric = st.selectbox('表示項目', metric_options, index=0, key='calendar_metric')

        year, month = int(sel_month[:4]), int(sel_month[5:7])

        if sel_metric == '統合スコア日次平均':
            if daily_avg.empty:
                st.caption('stage3_scoresデータがありません。')
            else:
                z, text = _month_grid(daily_avg.to_dict(), year, month)
                fig_cal = go.Figure(data=go.Heatmap(
                    z=z, x=_WEEKDAY_LABELS, y=[f'第{i + 1}週' for i in range(z.shape[0])],
                    text=text, texttemplate='%{text}', textfont=dict(size=11, color=ui.TEXT),
                    colorscale=ui.SEQ_WARM, zmin=0.0, zmax=1.0,
                    hoverongaps=False,
                ))
                ui.apply_mobile_layout(fig_cal, height=280)
                fig_cal.update_yaxes(autorange='reversed')
                st.plotly_chart(fig_cal, use_container_width=True, config=ui.PLOTLY_CONFIG)
        else:
            pattern_key = next(
                (p for p in available_patterns if _PATTERN_LABELS.get(p, p) == sel_metric), None
            )
            if pattern_key is None:
                st.caption('pattern_historyデータがありません(パターン別の内訳は表示できません)。')
            else:
                score_map = pattern_daily.loc[pattern_key].to_dict()
                z2, text2 = _month_grid(score_map, year, month)
                # DIVERGINGは中間点が白のため、白文字(ui.TEXT)だと0付近のセルで読めなくなる。
                # 濃色文字はcard_bg〜赤/青の全域で3:1以上のコントラストを確保できるためこちらを使う。
                fig_pat = go.Figure(data=go.Heatmap(
                    z=z2, x=_WEEKDAY_LABELS, y=[f'第{i + 1}週' for i in range(z2.shape[0])],
                    text=text2, texttemplate='%{text}', textfont=dict(size=11, color=ui.CARD_BG),
                    colorscale=ui.DIVERGING, zmid=0.0, zmin=-1.0, zmax=1.0,
                    hoverongaps=False,
                ))
                ui.apply_mobile_layout(fig_pat, height=280)
                fig_pat.update_yaxes(autorange='reversed')
                st.plotly_chart(fig_pat, use_container_width=True, config=ui.PLOTLY_CONFIG)


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


# ── メイン ───────────────────────────────────────────────────────


def main() -> None:
    """機能B-簡潔: 狙い目メモをテキスト出力する(`python app_b.py`)。"""
    # Windows のコンソール(cp932)は █/░ 等の記号をエンコードできず crash するため utf-8 に固定する。
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    profiles = load_all_profiles()
    print(memo_simple(profiles))


if __name__ == '__main__':
    main()
