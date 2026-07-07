"""
app_top.py — 機能B再設計 Phase 4: トップページ(当日・翌日ランキング)

【トップページ】
  用途: 家を出る前に「今どの店が狙い目か」「明日どの台が狙い目か」を一目で確認する
  内容:
    1. 店舗ランキング(当日・符号付き合成スコア。プラス=狙い目、マイナス=避けるべき店)
    2. 当日の熱い台(店舗別、stage3_scoresのhigh_prob上位)
    3. 翌日予測ランキング(S_鉄板台。prediction_logの最新行+prediction_accuracyの的中率)

的中率・信頼度が低くても翌日予測候補を非表示にはしない(機能B再設計1節の決定通り。
最終判断は人間が行う前提)。的中率はPhase3のprediction_accuracyを読むだけで、
ここでは計算しない(集計ロジックの二重実装を避けるため evaluate_predictions.py に一任)。

依存: app_b.py(店舗ランキング・熱い台の既存実装を再利用), patterns.py(calendar_candidates),
      score.py 経由で作成される prediction_log / prediction_accuracy テーブル

実行方法: app.py のホームページから render() で呼ばれる(単独起動は廃止)
"""
import json
import sqlite3

import numpy as np
import pandas as pd

import app_b as ab
import data_source as ds
import patterns as pt
from evaluate_predictions import MIN_SAMPLES

_TOP_N_PREDICTIONS = 15  # 翌日予測ランキングのグラフ表示件数(暫定値)


# ── データ読み込み ────────────────────────────────────────────────


def _load_latest_predictions(analysis_db: str) -> pd.DataFrame:
    """
    prediction_log から (ホール名, 機種名, 台番号) ごとの最新行(実行日時が最大)を返す。
    prediction_logはappend-onlyのため、同じ台について複数回分の予測が積み上がる。
    表示するのは直近の run_store_profile.py 実行で計算された最新予測のみ。
    """
    try:
        con = sqlite3.connect(analysis_db)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'prediction_log' not in tables:
                return pd.DataFrame()
            df = pd.read_sql_query(
                "SELECT * FROM prediction_log WHERE 予測種別 = 'S_鉄板台'", con
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    latest_idx = df.groupby(['ホール名', '機種名', '台番号'])['実行日時'].idxmax()
    return df.loc[latest_idx].reset_index(drop=True)


def _load_prediction_accuracy(analysis_db: str) -> pd.DataFrame:
    """prediction_accuracy(評価専用スクリプトevaluate_predictions.pyの出力)を読む。"""
    try:
        con = sqlite3.connect(analysis_db)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'prediction_accuracy' not in tables:
                return pd.DataFrame()
            return pd.read_sql_query('SELECT * FROM prediction_accuracy', con)
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()


# ── 根拠テキスト ──────────────────────────────────────────────────


def _reason_text(detail_raw: str | None, target_date: str) -> str:
    """
    prediction_logの詳細列(JSON: {周期日数, カレンダー条件})から根拠テキストを組み立てる。
    カレンダー条件は対象日と照合(patterns.calendar_candidatesで一致判定。
    predict_next_dayが内部で行うのと同じ照合をここでも1回行うだけで、
    ノイジーOR統合等のモデル本体は再実装しない)。
    """
    if not detail_raw:
        return '根拠情報なし'

    try:
        detail = json.loads(detail_raw) if isinstance(detail_raw, str) else detail_raw
    except (TypeError, json.JSONDecodeError):
        return '根拠情報なし'

    parts: list[str] = []

    cal_list = detail.get('カレンダー条件') or []
    if cal_list:
        dt_idx = pd.DatetimeIndex([pd.Timestamp(target_date)])
        candidates = pt.calendar_candidates(dt_idx)
        matched = [c for c in cal_list if bool(candidates.get(c['条件'], np.array([False]))[0])]
        if matched:
            best = max(matched, key=lambda c: float(c['効果量']))
            parts.append(f"カレンダー: {best['条件']}に該当(効果量+{float(best['効果量']):.2f})")
        else:
            mean_effect = float(np.mean([float(c['効果量']) for c in cal_list]))
            parts.append(
                f"カレンダー: 既知条件({len(cal_list)}件)に非該当"
                f"(推定{-pt.NEGATIVE_SCALE * mean_effect:+.2f})"
            )

    lags = detail.get('周期日数') or []
    if lags:
        lag_str = '・'.join(f'{lag}日' for lag in lags)
        parts.append(f'周期: {lag_str}周期を検出済み')

    return ' / '.join(parts) if parts else '根拠情報なし'


def _accuracy_text(acc_row: pd.Series | None) -> str:
    """
    prediction_accuracyの1行から的中率の表示文字列を組み立てる。
    サンプル数がMIN_SAMPLES未満、または行自体が無い場合は「検証中」
    (機能B再設計1節: 的中率・信頼度が低くても非表示にはしない方針の一部として、
    値が無いこと自体を隠さず明示する)。
    """
    if acc_row is None or pd.isna(acc_row.get('サンプル数')) or int(acc_row['サンプル数']) < MIN_SAMPLES:
        n = 0 if acc_row is None or pd.isna(acc_row.get('サンプル数')) else int(acc_row['サンプル数'])
        return f'検証中(サンプル{n}件)'

    spearman = acc_row.get('spearman相関')
    precision = acc_row.get('precision_at_n')
    lift = acc_row.get('リフト')
    bits = [f"サンプル{int(acc_row['サンプル数'])}件"]
    if pd.notna(spearman):
        bits.append(f'相関{float(spearman):+.2f}')
    if pd.notna(precision):
        bits.append(f'Precision@N {float(precision):.0%}')
    if pd.notna(lift):
        bits.append(f'リフト{float(lift):.2f}倍')
    return ' / '.join(bits)


# ── Streamlit エントリポイント ────────────────────────────────────


def render() -> None:
    """当日・翌日ランキング本体。app.pyのホームページから呼ばれる。"""
    import streamlit as st
    import plotly.express as px

    import ui_theme as ui

    st.header('当日・翌日ランキング')

    if not ds.ANALYSIS_DB_PATH.exists():
        st.error(
            f'分析DBが見つかりません: {ds.ANALYSIS_DB_PATH}\n\n'
            'fase2/run_store_profile.py を先に実行してください。'
        )
        return

    analysis_db = str(ds.ANALYSIS_DB_PATH)

    with st.spinner('データ読み込み中...'):
        profiles = ab.load_all_profiles()
        weights = ab.load_weights()
        synth_df = ab.synthesize_scores(profiles, weights)
        pred_df = _load_latest_predictions(analysis_db)
        acc_df = _load_prediction_accuracy(analysis_db)

    # ── 1. 店舗ランキング(当日) ──
    st.subheader('店舗ランキング')

    if synth_df.empty:
        st.warning(
            'store_profile データがありません。'
            'fase2/run_store_profile.py を先に実行してください。'
        )
    else:
        ranked = synth_df.sort_values('合成スコア', ascending=True)
        fig = px.bar(
            ranked, x='合成スコア', y='ホール名', orientation='h',
            color='合成スコア', color_continuous_scale='RdBu',
            range_x=[-1, 1], range_color=[-1, 1],
        )
        ui.apply_mobile_layout(fig, height=max(280, len(ranked) * 32 + 80))
        st.plotly_chart(fig, use_container_width=True, config=ui.PLOTLY_CONFIG)

    st.divider()

    # ── 2. 当日の熱い台(店舗別) ──
    st.subheader('当日の熱い台(店舗別)')

    if synth_df.empty:
        st.info('店舗データがありません。')
    else:
        for row in synth_df.itertuples():
            db_path_val = str(row.db_path) if getattr(row, 'db_path', None) else ''
            with st.expander(str(row.ホール名)):
                if not db_path_val:
                    st.caption('DB参照不可')
                    continue
                hot = ab.load_hot_machines(db_path_val, str(row.ホール名))
                if not hot:
                    st.caption('Stage3スコアなし(run_store_profile.py を先に実行)')
                    continue
                hot_df = pd.DataFrame(hot)
                hot_df['high_prob'] = hot_df['high_prob'].map(lambda x: f'{float(x):.1%}')
                st.dataframe(hot_df, use_container_width=True, hide_index=True)

    st.divider()

    # ── 3. 翌日予測ランキング(S_鉄板台) ──
    st.subheader('翌日予測')

    if pred_df.empty:
        st.info(
            'prediction_logに翌日予測データがありません。'
            'run_store_profile.py を実行すると蓄積されます。'
        )
        return

    acc_lookup: dict[tuple, pd.Series] = {}
    if not acc_df.empty:
        for _, r in acc_df.iterrows():
            acc_lookup[(r['ホール名'], r['予測種別'])] = r

    pred_df = pred_df.copy()
    pred_df['根拠'] = pred_df.apply(
        lambda r: _reason_text(r.get('詳細'), r['対象日']), axis=1
    )
    pred_df['的中率'] = pred_df.apply(
        lambda r: _accuracy_text(acc_lookup.get((r['ホール名'], r['予測種別']))), axis=1
    )
    pred_df['台'] = pred_df['機種名'] + ' ' + pred_df['台番号'].astype(str) + '番台'
    pred_df['ラベル'] = pred_df['ホール名'] + ' / ' + pred_df['台']

    ranked_pred = pred_df.dropna(subset=['ブレンド値']).sort_values('ブレンド値', ascending=True)
    top_pred = pd.concat([ranked_pred.head(_TOP_N_PREDICTIONS), ranked_pred.tail(_TOP_N_PREDICTIONS)]).drop_duplicates(subset=['ラベル'])

    if not top_pred.empty:
        top_pred = top_pred.copy()
        top_pred['短縮ラベル'] = top_pred.apply(
            lambda r: f"{ui.short_label(r['ホール名'], 8)} {ui.short_label(r['機種名'], 10)} {int(r['台番号'])}番",
            axis=1,
        )
        fig_pred = px.bar(
            top_pred.sort_values('ブレンド値'), x='ブレンド値', y='短縮ラベル', orientation='h',
            color='ブレンド値', color_continuous_scale='RdBu',
            range_x=[-1, 1], range_color=[-1, 1],
            custom_data=['ホール名', '台'],
        )
        fig_pred.update_traces(
            hovertemplate='%{customdata[0]} / %{customdata[1]}<br>ブレンド値: %{x:.3f}<extra></extra>'
        )
        ui.apply_mobile_layout(fig_pred, height=max(280, len(top_pred) * 32 + 80))
        st.plotly_chart(fig_pred, use_container_width=True, config=ui.PLOTLY_CONFIG)

    disp_pred = pred_df.sort_values('ブレンド値', ascending=False).copy()
    disp_pred['ブレンド値'] = disp_pred['ブレンド値'].map(lambda x: f'{x:.3f}' if pd.notna(x) else 'N/A')
    st.dataframe(
        disp_pred[['ホール名', '台', 'ブレンド値', '対象日']],
        use_container_width=True, hide_index=True,
    )
    with st.expander('根拠・的中率の詳細'):
        st.dataframe(
            disp_pred[['ホール名', '台', '対象日', '使用データ最終日', '根拠', '的中率']],
            use_container_width=True, hide_index=True,
        )
