"""
app_top.py — 機能B再設計 Phase4 + 2026-07 UIリニューアル: トップページ

【トップページ】
  用途: 家を出る前に「今どの店が狙い目か」「明日どの台が狙い目か」を一目で確認する
  内容:
    1. render_recommend_stores(): MM/DD(曜)のおすすめ店舗
       (「設定予測の的中が期待できる店舗」ランキング(案cハイブリッド、2026-07-13決定事項)の
        上位3+下位3、表形式・色分け。稼働の低さはランキング外の添え列)
    2. render_hot_predictions(): MM/DD(曜)の熱い台予測
       (店舗ごと/全店舗横断、個別台・機種・ローテ・新台・増台・移動台・据えの7タブ。
        各タブのスコアは符号付きパーセンタイル(2026-07-13決定事項「案b」)に統一表示)

的中率・信頼度が低くても候補を非表示にはしない(機能B再設計1節の決定通り。
最終判断は人間が行う前提)。的中率はPhase3のprediction_accuracyを読むだけで、
ここでは計算しない(集計ロジックの二重実装を避けるため evaluate_predictions.py に一任)。

依存: app_b.py(合成スコア計算の既存実装を再利用), data_source.py(analysis.db接続),
      score.py 経由で作成される stage3_scores / store_profile / prediction_log / prediction_accuracy

実行方法: app.py のホームページから render_recommend_stores() / render_hot_predictions() で
          呼ばれる(単独起動は廃止)
"""
import json
import sqlite3

import numpy as np
import pandas as pd

import app_b as ab
import data_source as ds
import patterns as pt
from evaluate_predictions import MIN_SAMPLES

_TOP_N_STORES = 3        # おすすめ店舗ランキングの上位/下位件数
_TOP_N_PER_STORE = 3     # 店舗ごとの予測タブの表示件数
_TOP_N_CROSS_STORE = 5   # 全店舗横断タブの表示件数
_WEEKDAY_LABELS = ['月', '火', '水', '木', '金', '土', '日']

# 7タブの暫定閾値(実データ運用しながら調整する前提。fase2/今後の実装予定.md参照)
_RELIABILITY_GATE = 0.4     # ローテ/新台増台/移動台/据え置きタブの店舗単位信頼度ゲート
_BREADTH_SCORE_GATE = 0.3   # 新台増台/移動台タブの台単位スコアしきい値
# [2026-07 タスク3] S_据え置きが「全期間1定数(0〜1)」から「当日断面の符号付き値([-1,1]、
# 該当日は+r̄_t≧patterns.SUEKI_DAILY_THRESHOLD(暫定0.2))」に変わったため、0.5では
# ほぼ該当日を拾えなくなる。該当日は必ずSUEKI_DAILY_THRESHOLD以上になる仕様なので、
# 実質「該当日のみ」を意味する0.0超に暫定変更(実データで調整前提)。
_SUEKI_SCORE_GATE = 0.0     # 据え置きタブの台単位スコアしきい値

# 予測鮮度の猶予日数(暫定)。run_store_profile.pyの実行間隔が空いた店舗(fase4異常停止時など)の
# 古い予測が最新予測と混在しないよう、個別台/機種タブ(prediction_log由来)にのみ適用する。
_PREDICTION_STALE_GRACE_DAYS = 2


# ── データ読み込み ────────────────────────────────────────────────


def _load_latest_predictions(analysis_db: str) -> tuple[pd.DataFrame, dict]:
    """
    prediction_log から (ホール名, 機種名, 台番号) ごとの最新行(実行日時が最大)を返す。
    prediction_logはappend-onlyのため、同じ台について複数回分の予測が積み上がる。
    表示するのは直近の run_store_profile.py 実行で計算された最新予測のみ。

    fase4の実行間隔が空いた店舗(異常停止時など)の古い予測が最新予測と混在しないよう、
    グローバル最新対象日(ロード済み行の対象日最大値。_load_target_dateと同じ定義)から
    _PREDICTION_STALE_GRACE_DAYS日より古い行は除外する(タスク1)。
    戻り値の2つ目は空表示判定に使う付随情報(global_latest_date/excluded_holes/all_holes)。
    """
    empty_info = {'global_latest_date': None, 'excluded_holes': set(), 'all_holes': set()}
    try:
        con = sqlite3.connect(analysis_db)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'prediction_log' not in tables:
                return pd.DataFrame(), empty_info
            df = pd.read_sql_query(
                "SELECT * FROM prediction_log WHERE 予測種別 = 'S_鉄板台'", con
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame(), empty_info

    if df.empty:
        return df, empty_info

    df['対象日_dt'] = pd.to_datetime(df['対象日'])
    global_latest_dt = df['対象日_dt'].max()

    latest_idx = df.groupby(['ホール名', '機種名', '台番号'])['実行日時'].idxmax()
    latest = df.loc[latest_idx].reset_index(drop=True)
    latest['経過日数'] = (global_latest_dt - latest['対象日_dt']).dt.days

    all_holes = set(latest['ホール名'])
    fresh = latest[latest['経過日数'] <= _PREDICTION_STALE_GRACE_DAYS].reset_index(drop=True)
    fresh = fresh.drop(columns=['対象日_dt'])
    excluded_holes = all_holes - set(fresh['ホール名'])

    info = {
        'global_latest_date': global_latest_dt.strftime('%Y-%m-%d'),
        'excluded_holes': excluded_holes,
        'all_holes': all_holes,
    }
    return fresh, info


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


# ── 対象日ラベル ──────────────────────────────────────────────────


def _load_target_date(analysis_db: str) -> str | None:
    """prediction_log(S_鉄板台)の最新の対象日(YYYY-MM-DD)を返す。無ければNone。"""
    try:
        con = sqlite3.connect(analysis_db)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'prediction_log' not in tables:
                return None
            row = pd.read_sql_query(
                "SELECT MAX(対象日) AS d FROM prediction_log WHERE 予測種別 = 'S_鉄板台'", con
            )
        finally:
            con.close()
    except Exception:
        return None
    if row.empty or pd.isna(row['d'].iloc[0]):
        return None
    return str(row['d'].iloc[0])


def _date_label(date_str: str | None) -> str:
    """MM/DD(曜)形式のラベルを返す。対象日が無ければ今日の日付を使う。"""
    ts = pd.Timestamp(date_str) if date_str else pd.Timestamp.now()
    return f'{ts.strftime("%m/%d")}({_WEEKDAY_LABELS[ts.dayofweek]})'


# ── Streamlit エントリポイント ────────────────────────────────────


def _load_kadou_lookup(profiles: pd.DataFrame) -> dict[str, float]:
    """store_profileからS_稼働低さ(パターン's_kadou')をホール名→スコアの辞書で返す(添え表示用)。"""
    if profiles.empty:
        return {}
    kadou = profiles[profiles['パターン'] == 's_kadou'].dropna(subset=['スコア'])
    return {r['ホール名']: float(r['スコア']) for _, r in kadou.iterrows()}


def render_recommend_stores() -> None:
    """
    「MM/DD(曜)のおすすめ店舗」本体。app.pyのホームページから呼ばれる。

    [今後の実装予定.md 4節「機能B理想形」項目1、2026-07-13決定事項] 「設定予測の的中が
    期待できる店舗」ランキングに変更(旧: 当日記述型を含む7軸の単純加重平均だった合成スコア)。
    案cハイブリッド(app_b.compute_store_recommend_score)を上位3+下位3の計6店舗で表示し、
    S_稼働低さはランキングから外して添え列として別掲する。
    """
    import streamlit as st

    import ui_theme as ui

    if not ds.ANALYSIS_DB_PATH.exists():
        st.error(
            f'分析DBが見つかりません: {ds.ANALYSIS_DB_PATH}\n\n'
            'fase2/run_store_profile.py を先に実行してください。'
        )
        return

    analysis_db = str(ds.ANALYSIS_DB_PATH)

    with st.spinner('データ読み込み中...'):
        profiles = ab.load_all_profiles()
        target_date = _load_target_date(analysis_db)
        kadou_lookup = _load_kadou_lookup(profiles)
        holes = sorted(set(profiles['ホール名'])) if not profiles.empty else []
        rows = [
            {'ホール名': hole, **ab.compute_store_recommend_score(analysis_db, hole),
             '稼働低さ': kadou_lookup.get(hole)}
            for hole in holes
        ]
        rank_df = pd.DataFrame(rows)

    st.header(f'{_date_label(target_date)}のおすすめ店舗')
    st.caption(
        '「設定予測の的中が期待できる店舗」のランキングです(稼働の低さは参考情報として'
        '別掲するのみで、ランキングには使いません)。実績データが十分な軸はprediction_accuracyの'
        '相関、未蓄積の軸は有意な検出条件の数×効果量を暫定指標とします'
        '(今後の実装予定.md 4節「機能B理想形」項目1、案cハイブリッド)。'
    )

    if rank_df.empty:
        st.warning(
            'store_profile データがありません。'
            'fase2/run_store_profile.py を先に実行してください。'
        )
        return

    ranked = rank_df.dropna(subset=['おすすめ度']).sort_values('おすすめ度', ascending=False)
    if ranked.empty:
        st.info('おすすめ度を計算できる店舗がありません。')
        return

    combined = pd.concat(
        [ranked.head(_TOP_N_STORES), ranked.tail(_TOP_N_STORES)]
    ).drop_duplicates(subset=['ホール名'])

    disp = combined[['ホール名', 'おすすめ度', '実績軸数', '有効軸数', '稼働低さ']].reset_index(drop=True)
    styled = ui.style_signed(disp, ['おすすめ度']).format({
        'おすすめ度': '{:+.2f}',
        '稼働低さ': lambda v: f'{v:.2f}' if pd.notna(v) else '-',
    })
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ── 熱い台予測: データ読み込み ────────────────────────────────────


def _load_latest_snapshot(analysis_db: str) -> pd.DataFrame:
    """
    stage3_scoresから店舗ごとの最新日(is_invalid除外)のスナップショットを1クエリで返す。
    幅型/深さ型サブスコア列(score.py Stage B拡張分)を含む。テーブルが無ければ空DF。
    """
    try:
        con = sqlite3.connect(analysis_db)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'stage3_scores' not in tables:
                return pd.DataFrame()
            return pd.read_sql_query(
                '''
                SELECT s.* FROM stage3_scores s
                JOIN (
                    SELECT ホール名, MAX(日付) AS max_date
                    FROM stage3_scores
                    WHERE is_invalid IS NULL OR is_invalid != 1
                    GROUP BY ホール名
                ) m ON s.ホール名 = m.ホール名 AND s.日付 = m.max_date
                WHERE s.is_invalid IS NULL OR s.is_invalid != 1
                ''',
                con,
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()


def _load_store_reliabilities(analysis_db: str) -> dict[tuple[str, str], float]:
    """store_profileから(ホール名, パターンキー)→信頼度の辞書を返す。"""
    try:
        con = sqlite3.connect(analysis_db)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'store_profile' not in tables:
                return {}
            df = pd.read_sql_query('SELECT ホール名, パターン, 信頼度 FROM store_profile', con)
        finally:
            con.close()
    except Exception:
        return {}
    return {(r['ホール名'], r['パターン']): r['信頼度'] for _, r in df.iterrows()}


def _split_pos_neg(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """符号付きパーセンタイルの参照分布用に、NaN除去のうえ正/負に分割する。"""
    finite = values[~np.isnan(values)]
    return finite[finite > 0], finite[finite < 0]


def _load_blend_value_reference(analysis_db: str) -> tuple[np.ndarray, np.ndarray]:
    """
    [今後の実装予定.md 4節「機能B理想形」項目2] 個別台タブの符号付きパーセンタイル参照分布
    (prediction_logの全履歴ブレンド値、予測種別='S_鉄板台')。
    """
    try:
        con = sqlite3.connect(analysis_db)
        try:
            df = pd.read_sql_query(
                "SELECT ブレンド値 AS v FROM prediction_log "
                "WHERE 予測種別 = 'S_鉄板台' AND ブレンド値 IS NOT NULL", con,
            )
        finally:
            con.close()
    except Exception:
        return np.array([]), np.array([])
    return _split_pos_neg(df['v'].to_numpy(dtype=float))


def _load_machine_avg_blend_reference(analysis_db: str) -> tuple[np.ndarray, np.ndarray]:
    """機種タブの符号付きパーセンタイル参照分布(機種×対象日ごとの平均ブレンド値の全履歴)。"""
    try:
        con = sqlite3.connect(analysis_db)
        try:
            df = pd.read_sql_query(
                "SELECT AVG(ブレンド値) AS v FROM prediction_log "
                "WHERE 予測種別 = 'S_鉄板台' AND ブレンド値 IS NOT NULL "
                "GROUP BY ホール名, 機種名, 対象日", con,
            )
        finally:
            con.close()
    except Exception:
        return np.array([]), np.array([])
    return _split_pos_neg(df['v'].to_numpy(dtype=float))


def _load_stage3_column_reference(analysis_db: str, column: str) -> tuple[np.ndarray, np.ndarray]:
    """ローテ/新台増台/移動台/据えタブの符号付きパーセンタイル参照分布(stage3_scores列の全履歴)。"""
    try:
        con = sqlite3.connect(analysis_db)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'stage3_scores' not in tables:
                return np.array([]), np.array([])
            cols = [row[1] for row in con.execute('PRAGMA table_info(stage3_scores)').fetchall()]
            if column not in cols:
                return np.array([]), np.array([])
            df = pd.read_sql_query(
                f'SELECT "{column}" AS v FROM stage3_scores WHERE "{column}" IS NOT NULL', con,
            )
        finally:
            con.close()
    except Exception:
        return np.array([]), np.array([])
    return _split_pos_neg(df['v'].to_numpy(dtype=float))


def signed_percentile(value: float, positive_ref: np.ndarray, negative_ref: np.ndarray) -> float:
    """
    [今後の実装予定.md 4節「機能B理想形」項目2、2026-07-13決定事項「案b・0固定アンカー」]
    符号は生スコアのまま、大きさは同符号の履歴分布内でのパーセンタイル順位(0〜1)に変換する。
    0(または参照分布が空)は「信号なし」として0を返す。素の順位パーセンタイルと異なり、
    符号を保持するため「逆効果」と「プラスだが相対的に弱い」を区別できる。
    """
    if value is None or pd.isna(value) or value == 0:
        return 0.0
    if value > 0:
        if positive_ref is None or len(positive_ref) == 0:
            return 0.0
        return float((positive_ref <= value).mean())
    if negative_ref is None or len(negative_ref) == 0:
        return 0.0
    return -float((negative_ref >= value).mean())


def _apply_signed_percentile(
    df: pd.DataFrame, raw_col: str, pos_ref: np.ndarray, neg_ref: np.ndarray, new_col: str = '予測スコア',
) -> pd.DataFrame:
    """生スコア列(raw_col)を符号付きパーセンタイルに変換し、7タブ共通の表示列名(new_col)へ差し替える。"""
    out = df.copy()
    out[new_col] = out[raw_col].apply(lambda v: signed_percentile(v, pos_ref, neg_ref))
    if new_col != raw_col:
        out = out.drop(columns=[raw_col])
    return out


def _fmt_signed_pct(v: float) -> str:
    """符号付きパーセンタイルの表示用フォーマット(0は符号なし、それ以外は+/-付きの%)。"""
    if pd.isna(v):
        return ''
    if v == 0:
        return '0%'
    return f'{v:+.0%}'


def _load_latest_introduction_categories(analysis_db: str) -> dict[tuple[str, str], str]:
    """
    [今後の実装予定.md 1.8.3節「導入後カーブ」実装ステップ4] introduction_eventsから
    (ホール名, 機種名)ごとの最新イベント(日付が最大、'判別不能'は除く)のカテゴリを返す。
    新台/増台タブの振り分けに使う。
    """
    try:
        con = sqlite3.connect(analysis_db)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'introduction_events' not in tables:
                return {}
            df = pd.read_sql_query(
                "SELECT ホール名, 機種名, 日付, カテゴリ FROM introduction_events "
                "WHERE カテゴリ != '判別不能'",
                con,
            )
        finally:
            con.close()
    except Exception:
        return {}
    if df.empty:
        return {}
    latest_idx = df.groupby(['ホール名', '機種名'])['日付'].idxmax()
    latest = df.loc[latest_idx]
    return {(r['ホール名'], r['機種名']): r['カテゴリ'] for _, r in latest.iterrows()}


# ── 熱い台予測: 7タブの中身 ───────────────────────────────────────

_TAB_NAMES = ['個別台', '機種', 'ローテ', '新台', '増台', '移動台', '据え']


def _tab_individual(
    pred_df: pd.DataFrame, acc_lookup: dict, hole: str | None, top_n: int
) -> pd.DataFrame | None:
    """個別台タブ: ブレンド値上位N台+的中率(prediction_accuracy実データ)。"""
    if 'ホール名' not in pred_df.columns:
        return None
    df = pred_df if hole is None else pred_df[pred_df['ホール名'] == hole]
    df = df.dropna(subset=['ブレンド値']).sort_values('ブレンド値', ascending=False).head(top_n)
    if df.empty:
        return None
    df = df.copy()
    df['台'] = df['機種名'] + ' ' + df['台番号'].astype(int).astype(str) + '番台'
    df['鮮度'] = df['経過日数'].apply(lambda d: '' if pd.isna(d) or int(d) == 0 else f'{int(d)}日前')
    df['的中率'] = df.apply(
        lambda r: _accuracy_text(acc_lookup.get((r['ホール名'], r['予測種別']))), axis=1
    )
    # 詳細はタスク2の根拠文生成に使う(表には出さず_render_prediction_tabsで剥がして表示する)
    cols = (['ホール名'] if hole is None else []) + ['台', 'ブレンド値', '対象日', '鮮度', '的中率', '詳細']
    return df[cols].reset_index(drop=True)


def _tab_machine(
    pred_df: pd.DataFrame, acc_lookup: dict, hole: str | None, top_n: int
) -> pd.DataFrame | None:
    """機種タブ: 台のブレンド値を機種単位で平均(暫定)し上位N機種+的中率(店舗全体の参考値)。"""
    if 'ホール名' not in pred_df.columns:
        return None
    df = pred_df if hole is None else pred_df[pred_df['ホール名'] == hole]
    df = df.dropna(subset=['ブレンド値'])
    if df.empty:
        return None
    grp = df.groupby(['ホール名', '機種名'], as_index=False).agg(
        平均ブレンド値=('ブレンド値', 'mean'), 台数=('台番号', 'count'),
        経過日数=('経過日数', 'max'),  # 機種内で最も古い台の経過日数を代表値にする(グループ全体の鮮度注記用)
    )
    grp = grp.sort_values('平均ブレンド値', ascending=False).head(top_n)
    if grp.empty:
        return None
    grp = grp.copy()
    grp['鮮度'] = grp['経過日数'].apply(lambda d: '' if pd.isna(d) or int(d) == 0 else f'{int(d)}日前')
    grp['的中率'] = grp['ホール名'].map(
        lambda h: _accuracy_text(acc_lookup.get((h, 'S_鉄板台')))
    )
    cols = (['ホール名'] if hole is None else []) + ['機種名', '平均ブレンド値', '台数', '鮮度', '的中率']
    return grp[cols].reset_index(drop=True)


def _tab_rotation(
    snapshot: pd.DataFrame, reliabilities: dict, hole: str | None, top_n: int
) -> pd.DataFrame | None:
    """
    ローテタブ: S_ローテが非NULL(検出条件はscore_rotation側で担保済み)かつ
    店舗の's_rote'信頼度が閾値以上の機種を上位N件。
    """
    if 'S_ローテ' not in snapshot.columns:
        return None
    df = snapshot if hole is None else snapshot[snapshot['ホール名'] == hole]
    df = df.dropna(subset=['S_ローテ']).copy()
    if df.empty:
        return None
    df['信頼度'] = df['ホール名'].map(lambda h: reliabilities.get((h, 's_rote')))
    df = df[df['信頼度'].fillna(0) >= _RELIABILITY_GATE]
    if df.empty:
        return None
    df = df.drop_duplicates(subset=['ホール名', '機種名']).sort_values('S_ローテ', ascending=False).head(top_n)
    df['的中率'] = '検証中'
    cols = (['ホール名'] if hole is None else []) + ['機種名', 'S_ローテ', '的中率']
    return df[cols].reset_index(drop=True)


def _tab_breadth_unit(
    snapshot: pd.DataFrame, reliabilities: dict, pattern_key: str,
    hole: str | None, top_n: int,
    category_lookup: dict[tuple[str, str], str] | None = None,
    required_category: str | None = None,
) -> pd.DataFrame | None:
    """
    新台/増台/移動台タブ共通: S_新台増台またはS_移動台が閾値超かつ店舗信頼度が閾値以上の台を上位N件。

    [今後の実装予定.md 1.8.3節「導入後カーブ」実装ステップ4] 新台/増台タブはpattern_key=
    's_shintai'で同じS_新台増台データを使うが、required_category('新台'/'増台')で
    introduction_events(patterns.detect_introduction_events)の機種の最新イベント
    カテゴリに応じて対象機種を絞り込む(旧detect_events由来のS_新台増台には復帰ノイズが
    混入していたが、detect_introduction_eventsが除外済みのカテゴリなのでここで
    自然にふるい落とされる。カテゴリ情報が無い機種はどちらのタブにも出ない)。
    移動台タブ(required_category=None)は絞り込みなし(現状維持、本節のスコープ外)。
    """
    score_col, rel_key = {
        's_shintai': ('S_新台増台', 's_shintai'),
        's_idoudai': ('S_移動台', 's_idoudai'),
    }[pattern_key]

    if score_col not in snapshot.columns:
        return None
    df = snapshot if hole is None else snapshot[snapshot['ホール名'] == hole]
    df = df.dropna(subset=[score_col]).copy()
    df = df[df[score_col] > _BREADTH_SCORE_GATE]
    if df.empty:
        return None
    df['信頼度'] = df['ホール名'].map(lambda h: reliabilities.get((h, rel_key)))
    df = df[df['信頼度'].fillna(0) >= _RELIABILITY_GATE]
    if df.empty:
        return None
    if required_category is not None:
        category_lookup = category_lookup or {}
        df = df[
            df.apply(
                lambda r: category_lookup.get((r['ホール名'], r['機種名'])) == required_category,
                axis=1,
            )
        ]
        if df.empty:
            return None
    df = df.sort_values(score_col, ascending=False).head(top_n)
    df = df.copy()
    df['台'] = df['機種名'] + ' ' + df['台番号'].astype(int).astype(str) + '番台'
    df['的中率'] = '検証中'
    cols = (['ホール名'] if hole is None else []) + ['台', score_col, '的中率']
    return df[cols].reset_index(drop=True)


def _tab_sueki(
    snapshot: pd.DataFrame, reliabilities: dict, hole: str | None, top_n: int
) -> pd.DataFrame | None:
    """
    据えタブ: S_据え置きが閾値超かつ店舗の's_sueki'信頼度が閾値以上の台を上位N件。

    [2026-07 タスク3] S_据え置きはpatterns.score_sueki_daily(日次判定)に差し替え済み。
    直近K日窓の平滑化lag-1自己相関r̄_tがSUEKI_DAILY_THRESHOLD以上の日のみ正値(該当日)を
    持つため、閾値超フィルタ(_SUEKI_SCORE_GATE)は実質「最新日が据え置き該当日の台」を
    抽出する形になる(スコア閾値のみで判定。データ分析_skill.md参照)。
    """
    if 'S_据え置き' not in snapshot.columns:
        return None
    df = snapshot if hole is None else snapshot[snapshot['ホール名'] == hole]
    df = df.dropna(subset=['S_据え置き']).copy()
    df = df[df['S_据え置き'] > _SUEKI_SCORE_GATE]
    if df.empty:
        return None
    df['信頼度'] = df['ホール名'].map(lambda h: reliabilities.get((h, 's_sueki')))
    df = df[df['信頼度'].fillna(0) >= _RELIABILITY_GATE]
    if df.empty:
        return None
    df = df.sort_values('S_据え置き', ascending=False).head(top_n)
    df = df.copy()
    df['台'] = df['機種名'] + ' ' + df['台番号'].astype(int).astype(str) + '番台'
    df = df.rename(columns={'S_据え置き': '予測スコア'})
    cols = (['ホール名'] if hole is None else []) + ['台', '予測スコア']
    return df[cols].reset_index(drop=True)


def _all_stale(hole: str | None, stale_info: dict | None) -> bool:
    """
    個別台/機種タブが空になった原因が「予測が古い(猶予落ち)」かどうかを判定する(タスク1の3点目)。
    店舗指定時はその店舗の予測が全て猶予落ちしたか、全店舗横断時は該当店舗が1つも
    残らなかった(全店舗が猶予落ちした)かで判定する。
    """
    if not stale_info or not stale_info.get('global_latest_date'):
        return False
    excluded = stale_info.get('excluded_holes') or set()
    if hole is not None:
        return hole in excluded
    all_holes = stale_info.get('all_holes') or set()
    return bool(all_holes) and excluded == all_holes


def _render_prediction_tabs(
    hole: str | None,
    pred_df: pd.DataFrame,
    acc_lookup: dict,
    snapshot: pd.DataFrame,
    reliabilities: dict,
    top_n: int,
    stale_info: dict | None = None,
    target_date: str | None = None,
    introduction_categories: dict[tuple[str, str], str] | None = None,
    score_refs: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
) -> None:
    """7タブ(個別台/機種/ローテ/新台/増台/移動台/据え)を描画する。該当なしは st.info。"""
    import streamlit as st

    import ui_theme as ui

    builders = {
        '個別台': (lambda: _tab_individual(pred_df, acc_lookup, hole, top_n), 'ブレンド値'),
        '機種': (lambda: _tab_machine(pred_df, acc_lookup, hole, top_n), '平均ブレンド値'),
        'ローテ': (lambda: _tab_rotation(snapshot, reliabilities, hole, top_n), 'S_ローテ'),
        '新台': (
            lambda: _tab_breadth_unit(
                snapshot, reliabilities, 's_shintai', hole, top_n,
                category_lookup=introduction_categories, required_category='新台',
            ),
            'S_新台増台',
        ),
        '増台': (
            lambda: _tab_breadth_unit(
                snapshot, reliabilities, 's_shintai', hole, top_n,
                category_lookup=introduction_categories, required_category='増台',
            ),
            'S_新台増台',
        ),
        '移動台': (lambda: _tab_breadth_unit(snapshot, reliabilities, 's_idoudai', hole, top_n), 'S_移動台'),
        '据え': (lambda: _tab_sueki(snapshot, reliabilities, hole, top_n), '予測スコア'),
    }

    tabs = st.tabs(_TAB_NAMES)
    for tab, name in zip(tabs, _TAB_NAMES):
        with tab:
            build_fn, color_col = builders[name]
            result = build_fn()
            if result is None or result.empty:
                if name in ('個別台', '機種') and _all_stale(hole, stale_info):
                    st.info(f"予測が古いため非表示(最終対象日: {stale_info['global_latest_date']})")
                else:
                    st.info('該当なし')
                continue
            # 詳細列はタスク2の根拠文生成専用(個別台タブのみ)。表には出さない
            display_df = result.drop(columns=['詳細']) if '詳細' in result.columns else result
            # [今後の実装予定.md 4節 項目2] 生スコアは軸ごとにスケールが異なるため、
            # 符号付きパーセンタイル(全店舗・全期間の実績分布内での相対位置)に統一して表示する
            pos_ref, neg_ref = (score_refs or {}).get(name, (np.array([]), np.array([])))
            display_df = _apply_signed_percentile(display_df, color_col, pos_ref, neg_ref)
            styled = ui.style_signed(display_df, ['予測スコア']).format({'予測スコア': _fmt_signed_pct})
            if '鮮度' in display_df.columns:
                styled = ui.style_stale_rows(styled, display_df['鮮度'] != '')
            st.dataframe(styled, use_container_width=True, hide_index=True)

            if name == '個別台' and '詳細' in result.columns:
                for i, row in result.reset_index(drop=True).iterrows():
                    row_date = row.get('対象日') or target_date
                    reason = _reason_text(row.get('詳細'), row_date)
                    st.caption(f"{i + 1}. {row['台']} — {reason}")


# ── Streamlit エントリポイント: 熱い台予測 ──────────────────────────


def render_hot_predictions() -> None:
    """
    「MM/DD(曜)の熱い台予測」本体。app.pyのホームページから呼ばれる。
    6-1 店舗ごとの予測(検索サジェスト→7タブ)・6-2 全店舗横断の予測(同7タブ)の2段構成。
    """
    import streamlit as st

    if not ds.ANALYSIS_DB_PATH.exists():
        st.error(
            f'分析DBが見つかりません: {ds.ANALYSIS_DB_PATH}\n\n'
            'fase2/run_store_profile.py を先に実行してください。'
        )
        return

    analysis_db = str(ds.ANALYSIS_DB_PATH)

    with st.spinner('データ読み込み中...'):
        target_date = _load_target_date(analysis_db)
        pred_df, stale_info = _load_latest_predictions(analysis_db)
        acc_df = _load_prediction_accuracy(analysis_db)
        snapshot = _load_latest_snapshot(analysis_db)
        reliabilities = _load_store_reliabilities(analysis_db)
        introduction_categories = _load_latest_introduction_categories(analysis_db)
        # [今後の実装予定.md 4節「機能B理想形」項目2] 符号付きパーセンタイルの参照分布を
        # 軸ごとに1回だけ読み込む(店舗別・全店舗横断の両方でこの同じ分布を使うことで、
        # タブ間だけでなく店舗間でもスコアのスケールが揃う)
        score_refs = {
            '個別台': _load_blend_value_reference(analysis_db),
            '機種': _load_machine_avg_blend_reference(analysis_db),
            'ローテ': _load_stage3_column_reference(analysis_db, 'S_ローテ'),
            '新台': _load_stage3_column_reference(analysis_db, 'S_新台増台'),
            '増台': _load_stage3_column_reference(analysis_db, 'S_新台増台'),
            '移動台': _load_stage3_column_reference(analysis_db, 'S_移動台'),
            '据え': _load_stage3_column_reference(analysis_db, 'S_据え置き'),
        }

    st.header(f'{_date_label(target_date)}の熱い台予測')
    st.caption(
        'スコアは符号付きパーセンタイルで統一表示しています'
        '(符号は生スコアのまま、大きさは全店舗・全期間の同符号履歴内での相対位置。'
        '0=シグナルなし)。'
    )

    acc_lookup: dict[tuple, pd.Series] = {}
    if not acc_df.empty:
        for _, r in acc_df.iterrows():
            acc_lookup[(r['ホール名'], r['予測種別'])] = r

    # 猶予落ちで個別台/機種タブから除外された店舗も選択肢には残す(選択時に専用メッセージを出すため)
    pred_holes = stale_info.get('all_holes') or set()
    snap_holes = set(snapshot['ホール名']) if 'ホール名' in snapshot.columns else set()
    holes = sorted(pred_holes | snap_holes)

    # ── 6-1 店舗ごとの予測 ──
    st.subheader('店舗ごとの予測')
    if not holes:
        st.info('データがありません。')
    else:
        sel_hole = st.selectbox(
            '店舗を選択', holes, index=None,
            placeholder='店舗名を選択(入力で絞り込み)',
            key='hot_pred_hole',
        )
        if sel_hole:
            _render_prediction_tabs(
                sel_hole, pred_df, acc_lookup, snapshot, reliabilities, _TOP_N_PER_STORE,
                stale_info=stale_info, target_date=target_date,
                introduction_categories=introduction_categories, score_refs=score_refs,
            )
        else:
            st.caption('店舗を選択すると7タブで予測を表示します。')

    st.divider()

    # ── 6-2 全店舗横断の予測 ──
    st.subheader('全店舗横断の予測')
    _render_prediction_tabs(
        None, pred_df, acc_lookup, snapshot, reliabilities, _TOP_N_CROSS_STORE,
        stale_info=stale_info, target_date=target_date,
        introduction_categories=introduction_categories, score_refs=score_refs,
    )
