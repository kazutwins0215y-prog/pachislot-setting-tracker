"""
app_b.py — 振り返り分析ダッシュボード + 狙い目メモ

【機能B-詳細: 店舗特徴(個別店舗詳細)】
  用途: 蓄積データからのパターン検出結果・店舗プロファイルをじっくり見る
  内容: γ_store / 癖の有効性マトリクス(2026-07-13追加) / 検知期間履歴 / カレンダーヒートマップ
    (サブスコア内訳の縦棒グラフは2026-07-13に削除。癖の有効性マトリクスが実データ
     (prediction_accuracy等)に基づくより正確な代替のため)
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


def _group_consecutive_date_runs(dates: list) -> list[tuple[int, int]]:
    """日付(datetime、昇順ソート済み)のリストから、1日刻みで連続している
    区間の(開始インデックス, 終了インデックス+1)を返す。"""
    if not dates:
        return []
    runs = []
    start = 0
    for i in range(1, len(dates) + 1):
        if i == len(dates) or (dates[i] - dates[i - 1]).days > 1:
            runs.append((start, i))
            start = i
    return runs


def _load_stage3_pattern_periods(
    db_path: str, hole_name: str, pattern_col: str, threshold: float = _PERIOD_SCORE_THRESHOLD,
) -> pd.DataFrame:
    """
    [今後の実装予定.md 4節 項目4] stage3_scores(台×日粒度)から、指定サブスコア列が
    thresholdを上回っている(機種名,台番号)ごとの連続区間を検出する。
    pattern_history(店舗全体平均のみ)と異なり機種名・台番号つきで検知期間を表示できる。
    S_鉄板台・S_移動台・S_据え置きに使う(pattern_colはstage3_scoresの列名と一致させること)。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'stage3_scores' not in tables:
                return pd.DataFrame()
            cols = [row[1] for row in con.execute('PRAGMA table_info(stage3_scores)').fetchall()]
            if pattern_col not in cols:
                return pd.DataFrame()
            df = pd.read_sql_query(
                f'''
                SELECT 日付, 機種名, 台番号, "{pattern_col}" AS score
                FROM stage3_scores
                WHERE ホール名 = ?
                  AND (is_invalid IS NULL OR is_invalid != 1)
                  AND "{pattern_col}" IS NOT NULL
                ''',
                con, params=(hole_name,),
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df[df['score'] > threshold].copy()
    if df.empty:
        return pd.DataFrame()
    df['日付_dt'] = pd.to_datetime(df['日付'], errors='coerce')
    df = df.dropna(subset=['日付_dt'])

    rows = []
    for (machine, unit), grp in df.groupby(['機種名', '台番号']):
        grp = grp.sort_values('日付_dt')
        dates = grp['日付_dt'].tolist()
        scores = grp['score'].tolist()
        for s, e in _group_consecutive_date_runs(dates):
            rows.append({
                '機種名': machine, '台番号': int(unit),
                '開始': dates[s], '終了': dates[e - 1],
                '平均スコア': float(np.mean(scores[s:e])),
            })

    return pd.DataFrame(rows)


def _load_zentaikei_periods(db_path: str, hole_name: str) -> pd.DataFrame:
    """
    [今後の実装予定.md 4節 項目4] machine_judgment_log(機種×日×ホール粒度、
    全台系/高配分/普段どおりの判定+台数)から、「普段どおり」以外が連続している
    機種名ごとの区間を検出する。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'machine_judgment_log' not in tables:
                return pd.DataFrame()
            df = pd.read_sql_query(
                '''
                SELECT 日付, 機種名, 台数, 期待高設定台数, 判定ラベル
                FROM machine_judgment_log
                WHERE ホール名 = ? AND 判定ラベル != '普段どおり'
                ''',
                con, params=(hole_name,),
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()
    df['日付_dt'] = pd.to_datetime(df['日付'], errors='coerce')
    df = df.dropna(subset=['日付_dt'])

    rows = []
    for machine, grp in df.groupby('機種名'):
        grp = grp.sort_values('日付_dt')
        dates = grp['日付_dt'].tolist()
        labels = grp['判定ラベル'].tolist()
        counts = grp['台数'].tolist()
        expects = grp['期待高設定台数'].tolist()
        for s, e in _group_consecutive_date_runs(dates):
            rows.append({
                '機種名': machine,
                '開始': dates[s], '終了': dates[e - 1],
                '判定ラベル': '/'.join(sorted(set(labels[s:e]))),
                '台数': int(round(np.mean(counts[s:e]))),
                '期待高設定台数_平均': float(np.mean(expects[s:e])),
            })

    return pd.DataFrame(rows)


def _load_introduction_events(
    db_path: str, hole_name: str, categories: tuple[str, ...],
) -> pd.DataFrame:
    """
    [今後の実装予定.md 4節 項目4] introduction_events(機種レベルイベント登記簿)から
    指定カテゴリ(新台/増台等)の該当行を新しい順に返す。1日1イベントの単発ログのため
    連続区間へのグルーピングはしない(S_全台系・stage3系と異なりイベント自体が離散)。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'introduction_events' not in tables:
                return pd.DataFrame()
            placeholders = ', '.join('?' * len(categories))
            df = pd.read_sql_query(
                f'''
                SELECT 日付, 機種名, カテゴリ, 台数変化, 台番号リスト
                FROM introduction_events
                WHERE ホール名 = ? AND カテゴリ IN ({placeholders})
                ORDER BY 日付 DESC
                ''',
                con, params=(hole_name, *categories),
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()

    return df


def _format_unit_list(json_str: str | None, max_show: int = 15) -> str:
    """台番号リスト(JSON文字列)を「12・13・14番」形式のテキストに整形する。多すぎる場合は省略。"""
    if not json_str:
        return ''
    units = json.loads(json_str)
    if len(units) > max_show:
        shown = '・'.join(f'{u}番' for u in units[:max_show])
        return f'{shown} 他{len(units) - max_show}台'
    return '・'.join(f'{u}番' for u in units)


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


def _month_grid(
    score_by_day: dict[str, float],
    year: int,
    month: int,
    event_days: set[str] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    year年month月のカレンダー形式(週×曜日)グリッドを作る。
    score_by_day: 'YYYY-MM-DD' → スコアの辞書。月内でデータが無い日はNaN。
    event_days: [今後の実装予定.md 1.9節「店舗×曜日の癖軸」] 有意な店舗日条件に該当する
    日付集合('YYYY-MM-DD')。該当日は日表示に★を付ける(過去・未来どちらの日付も対象)。
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
            date_str = f'{year:04d}-{month:02d}-{day:02d}'
            marker = '★' if event_days and date_str in event_days else ''
            text[wi, di] = f'{day}{marker}'
            if date_str in score_by_day:
                z[wi, di] = score_by_day[date_str]
    return z, text


# ── Phase 5(今後の実装予定.md 4節 項目5): カレンダーヒートマップ ──────────


def _load_daily_actual_diff(hole_name: str) -> pd.Series:
    """レプリカ(turso_replica.db)のslot_dataから日次平均差枚(店舗全体平均)を返す(日付→平均差枚)。"""
    if not ds.REPLICA_DB_PATH.exists():
        return pd.Series(dtype=float)
    try:
        con = ds.connect_replica()
        try:
            df = pd.read_sql_query(
                'SELECT 日付, 差枚 FROM slot_data WHERE ホール名 = ? AND 差枚 IS NOT NULL',
                con, params=(hole_name,),
            )
        finally:
            con.close()
    except Exception:
        return pd.Series(dtype=float)
    if df.empty:
        return pd.Series(dtype=float)
    return df.groupby('日付')['差枚'].mean()


def _store_relative_scores(daily: pd.Series) -> dict[str, float]:
    """
    [今後の実装予定.md 4節「機能B理想形」項目5] 日次系列を、店舗自身の中央値を基準にした
    符号付きパーセンタイル(-1〜1)へ変換する(app_top.signed_percentileと同じ考え方だが、
    0固定ではなくその店の中央値を基準にする。差枚・設定配分は店舗間でスケールが大きく
    異なるため、絶対値ではなく「この店にとって強い/弱い日か」を表現する)。
    """
    if daily.empty:
        return {}
    median = float(daily.median())
    above = (daily[daily > median] - median).to_numpy()
    below = (median - daily[daily < median]).to_numpy()  # 正の「下振れ幅」
    out: dict[str, float] = {}
    for date, value in daily.items():
        if pd.isna(value) or value == median:
            out[date] = 0.0
        elif value > median:
            out[date] = float((above <= (value - median)).mean()) if len(above) else 0.0
        else:
            out[date] = -float((below <= (median - value)).mean()) if len(below) else 0.0
    return out


def _load_machine_bias_list(db_path: str) -> list[str]:
    """
    [今後の実装予定.md 1.8.5節「機種バイアス除外・案A」] run_store_profile.pyが書き込む
    machine_bias_flags(バイアス判定=1)の機種名リストを返す。おすすめ店舗スコア・
    有効性マトリクス・カレンダー投影から、バイアス機種の「恒常」条件だけを除外するために
    使う(日付条件付きの条件は店固有性が高いため除外対象にしない)。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'machine_bias_flags' not in tables:
                return []
            rows = con.execute(
                'SELECT 機種名 FROM machine_bias_flags WHERE バイアス判定 = 1'
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()
    except Exception:
        return []


def _load_future_calendar_conditions(db_path: str, hole_name: str) -> pd.DataFrame:
    """
    [今後の実装予定.md 4節「機能B理想形」項目5] 未来投影に使えるカレンダー型の有意条件を
    1つのDataFrame(日付条件, 効果量)にまとめて返す。曜日・毎月X日等はgroup_calendar_conditions
    (台番号末尾/機種/機種_直近、BH有意のみ)、周期パターンのうちカレンダー経路のもの(曜日等)は
    teppan_conditionsから集める。一致ルール(台番号依存で店舗単位に集約できない)・
    据え置き/遷移/導入後(未来日で該当が確定しない)は対象外(決定事項の制約)。

    [今後の実装予定.md 1.8.5節「機種バイアス除外・案A」] バイアス機種(machine_bias_flags)の
    「恒常」行は除外する(店の癖ではなく機種側の推定バイアスのため)。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            frames: list[pd.DataFrame] = []
            if 'group_calendar_conditions' in tables:
                bias_machines = _load_machine_bias_list(db_path)
                exclude_clause, params = '', [hole_name]
                if bias_machines:
                    placeholders = ','.join('?' * len(bias_machines))
                    exclude_clause = (
                        f" AND NOT (日付条件 = '恒常' AND グループ IN ({placeholders}))"
                    )
                    params = [hole_name, *bias_machines]
                df = pd.read_sql_query(
                    "SELECT グループ, 日付条件, 効果量 FROM group_calendar_conditions "
                    "WHERE ホール名 = ? AND BH有意 = 1 "
                    "AND グループ種別 IN ('台番号末尾', '機種', '機種_直近', '店舗日')"
                    f"{exclude_clause}",
                    con, params=params,
                )
                frames.append(df[df['グループ'] != '一致ルール'][['日付条件', '効果量']])
            if 'teppan_conditions' in tables:
                df2 = pd.read_sql_query(
                    "SELECT 条件 AS 日付条件, 効果量 FROM teppan_conditions "
                    "WHERE ホール名 = ? AND 経路 = 'カレンダー'",
                    con, params=(hole_name,),
                )
                frames.append(df2)
        finally:
            con.close()
    except Exception:
        return pd.DataFrame(columns=['日付条件', '効果量'])
    if not frames:
        return pd.DataFrame(columns=['日付条件', '効果量'])
    return pd.concat(frames, ignore_index=True).dropna(subset=['効果量'])


def _load_store_day_conditions(db_path: str, hole_name: str) -> pd.DataFrame:
    """
    [今後の実装予定.md 1.9節「店舗×曜日の癖軸」] group_calendar_conditionsから
    グループ種別='店舗日'のBH有意な行(日付条件, 効果量)を返す。カレンダーヒートマップの
    イベント日マーカー(★)表示専用(_load_future_calendar_conditionsは色の投影に使うため
    別途取得する)。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'group_calendar_conditions' not in tables:
                return pd.DataFrame(columns=['日付条件', '効果量'])
            return pd.read_sql_query(
                "SELECT 日付条件, 効果量 FROM group_calendar_conditions "
                "WHERE ホール名 = ? AND グループ種別 = '店舗日' AND BH有意 = 1",
                con, params=(hole_name,),
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame(columns=['日付条件', '効果量'])


def _mark_event_days(conditions: pd.DataFrame, dates: list) -> set[str]:
    """
    [今後の実装予定.md 1.9節「店舗×曜日の癖軸」] 有意な店舗日条件(store_day_calendar_test)に
    該当する日付の集合('YYYY-MM-DD')を返す(カレンダーヒートマップの★マーカー表示用。
    実績日・未来日どちらの日付リストにも使える)。
    """
    if conditions.empty or not dates:
        return set()
    import patterns as pt

    dt_index = pd.DatetimeIndex(dates)
    candidates = pt.calendar_candidates(dt_index)
    sig_names = set(conditions['日付条件'])

    marked: set[str] = set()
    for cname in sig_names:
        mask = candidates.get(cname)
        if mask is None:
            continue
        for i, dt in enumerate(dt_index):
            if bool(mask[i]):
                marked.add(dt.strftime('%Y-%m-%d'))
    return marked


def _project_future_calendar_scores(conditions: pd.DataFrame, dates: list) -> dict[str, float]:
    """
    [今後の実装予定.md 4節「機能B理想形」項目5] カレンダー型の有意条件を将来日付へ照合し、
    該当した条件の効果量平均を日付→スコアの辞書で返す(該当条件が無い日は辞書に含めない=空欄)。
    「恒常」行(機種版の看板機種検定)は日付によらず常に該当する。
    """
    if conditions.empty or not dates:
        return {}
    import patterns as pt

    dt_index = pd.DatetimeIndex(dates)
    candidates = pt.calendar_candidates(dt_index)
    constant_effects = conditions.loc[conditions['日付条件'] == '恒常', '効果量'].tolist()

    out: dict[str, float] = {}
    for i, dt in enumerate(dt_index):
        matched = list(constant_effects)
        for cname, mask in candidates.items():
            if not bool(mask[i]):
                continue
            matched.extend(conditions.loc[conditions['日付条件'] == cname, '効果量'].tolist())
        if matched:
            out[dt.strftime('%Y-%m-%d')] = float(np.mean(matched))
    return out


# ── Phase 3(今後の実装予定.md 4節 項目3): 癖の有効性マトリクス ──────────

_HABIT_MIN_SAMPLES = 30      # evaluate_predictions.MIN_SAMPLESと合わせる(実績ベース判定の最低サンプル数)
_HABIT_EFFECTIVE_THRESHOLD = 0.1   # spearman相関がこれを超えたら「有効」(2026-07-13決定事項の店舗依存閾値と整合)

# 軸定義: 表示名 → prediction_accuracyの予測種別・group_calendar_conditionsのグループ種別・
# teppan_conditions利用有無(2026-07-13決定事項「機能B理想形」項目3のデータ源マッピング)
_HABIT_AXES: list[dict] = [
    {'軸': 'S_鉄板台',   'pred_types': ['S_鉄板台'],                'group_types': None,                 'use_teppan': True},
    {'軸': '遷移予測',   'pred_types': ['遷移予測', '遷移予測_前日差枚'], 'group_types': None,                 'use_teppan': False},
    {'軸': 'S_据え置き', 'pred_types': ['S_据え置き'],              'group_types': None,                 'use_teppan': False},
    {'軸': 'S_末尾',     'pred_types': ['S_末尾'],                  'group_types': ['台番号末尾'],         'use_teppan': False},
    {'軸': 'S_機種',     'pred_types': ['S_機種', 'S_機種_直近'],    'group_types': ['機種', '機種_直近'],  'use_teppan': False},
    {'軸': 'S_導入後',   'pred_types': ['S_導入後'],                'group_types': ['導入後'],            'use_teppan': False},
]
# 翌日予測を出さない当日記述型パターン(prediction_accuracyに乗らない。2026-07-13決定事項「評価不能」)
_HABIT_DESCRIPTIVE_ONLY = ['S_全台系', 'S_新台増台', 'S_移動台', 'S_ローテ', 'S_稼働低さ']


def _load_prediction_accuracy(db_path: str, hole_name: str) -> pd.DataFrame:
    """prediction_accuracy(evaluate_predictions.py集計済み)をホール単位で読み込む。"""
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'prediction_accuracy' not in tables:
                return pd.DataFrame()
            return pd.read_sql_query(
                'SELECT * FROM prediction_accuracy WHERE ホール名 = ?', con, params=(hole_name,),
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()


def _load_condition_significance(db_path: str, hole_name: str) -> pd.DataFrame:
    """
    group_calendar_conditionsをグループ種別単位で集計(検定数・BH有意数・有意条件内の最大効果量)する。

    [今後の実装予定.md 1.8.5節「機種バイアス除外・案A」] バイアス機種(machine_bias_flags)の
    「恒常」行は集計から除外する(店の癖ではなく機種側の推定バイアスのため。
    おすすめ店舗スコア・有効性マトリクスの両方がこの集計を再利用する)。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'group_calendar_conditions' not in tables:
                return pd.DataFrame()
            bias_machines = _load_machine_bias_list(db_path)
            exclude_clause, params = '', [hole_name]
            if bias_machines:
                placeholders = ','.join('?' * len(bias_machines))
                exclude_clause = (
                    " AND NOT (グループ種別 IN ('機種', '機種_直近', '機種_較正') "
                    f"AND 日付条件 = '恒常' AND グループ IN ({placeholders}))"
                )
                params = [hole_name, *bias_machines]
            return pd.read_sql_query(
                f'''
                SELECT グループ種別, COUNT(*) AS 検定数,
                       SUM(BH有意) AS 有意数,
                       MAX(CASE WHEN BH有意 = 1 THEN 効果量 END) AS 最大効果量
                FROM group_calendar_conditions
                WHERE ホール名 = ?{exclude_clause}
                GROUP BY グループ種別
                ''',
                con, params=params,
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()


def _load_teppan_condition_count(db_path: str, hole_name: str) -> tuple[int, float | None]:
    """
    teppan_conditions(検出済み条件のみを保存。有意性は検出時点で確定済みのためBH有意列は無い)の
    件数と最大効果量を返す。
    """
    try:
        con = sqlite3.connect(db_path)
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'teppan_conditions' not in tables:
                return 0, None
            row = con.execute(
                'SELECT COUNT(*), MAX(効果量) FROM teppan_conditions WHERE ホール名 = ?', (hole_name,),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return 0, None
    if row is None or row[0] is None:
        return 0, None
    return int(row[0]), (float(row[1]) if row[1] is not None else None)


def build_habit_matrix(db_path: str, hole_name: str) -> pd.DataFrame:
    """
    [今後の実装予定.md 4節「機能B理想形」項目3] 「ある癖(軸)がこの店舗で翌日予測に有効か」を
    軸ごとに判定し、ステータス(有効/弱い/検証中/データ不足/無効/対象外)と指標値を
    まとめたDataFrameを返す(2026-07-13決定事項「案cハイブリッド」に準拠)。

    判定順序:
    1. prediction_accuracyにサンプル数≥_HABIT_MIN_SAMPLESの実績があれば実績ベース
       (spearman相関の符号・大きさで有効/弱い/無効を判定。1軸に複数予測種別が
       ある場合はサンプル数で加重平均する)
    2. 実績が無い/不足の場合はgroup_calendar_conditions・teppan_conditionsの
       BH有意な検出条件の有無を暫定指標として「検証中」/「データ不足」に振り分ける
    3. 翌日予測を出さない当日記述型パターンは「対象外」固定

    Returns:
        DataFrame: 軸, ステータス, 指標, 値, 件数
    """
    acc = _load_prediction_accuracy(db_path, hole_name)
    cond = _load_condition_significance(db_path, hole_name)
    teppan_n, teppan_effect = _load_teppan_condition_count(db_path, hole_name)

    rows = []
    for axis in _HABIT_AXES:
        label = axis['軸']
        acc_sub = acc[acc['予測種別'].isin(axis['pred_types'])] if not acc.empty else pd.DataFrame()
        if not acc_sub.empty:
            acc_sub = acc_sub[acc_sub['サンプル数'].fillna(0) >= _HABIT_MIN_SAMPLES]

        if not acc_sub.empty and acc_sub['spearman相関'].notna().any():
            valid = acc_sub.dropna(subset=['spearman相関'])
            weight_sum = valid['サンプル数'].sum()
            spearman = (
                float((valid['spearman相関'] * valid['サンプル数']).sum() / weight_sum)
                if weight_sum > 0 else np.nan
            )
            n_total = int(valid['サンプル数'].sum())
            if pd.isna(spearman):
                status = 'データ不足'
            elif spearman > _HABIT_EFFECTIVE_THRESHOLD:
                status = '有効'
            elif spearman > 0:
                status = '弱い'
            else:
                status = '無効'
            rows.append({'軸': label, 'ステータス': status, '指標': 'spearman相関', '値': spearman, '件数': n_total})
            continue

        # 実績不足 → 有意条件ベースへフォールバック
        if axis['use_teppan']:
            sig_n, effect = teppan_n, teppan_effect
        elif axis['group_types'] and not cond.empty:
            sub = cond[cond['グループ種別'].isin(axis['group_types'])]
            sig_n = int(sub['有意数'].fillna(0).sum())
            effect = float(sub['最大効果量'].max()) if sub['最大効果量'].notna().any() else None
        else:
            sig_n, effect = 0, None

        if sig_n > 0:
            rows.append({'軸': label, 'ステータス': '検証中', '指標': '有意条件の最大効果量', '値': effect, '件数': sig_n})
        else:
            rows.append({'軸': label, 'ステータス': 'データ不足', '指標': '-', '値': np.nan, '件数': 0})

    for label in _HABIT_DESCRIPTIVE_ONLY:
        rows.append({'軸': label, 'ステータス': '対象外(当日記述型)', '指標': '-', '値': np.nan, '件数': None})

    return pd.DataFrame(rows)


_HABIT_CONDITION_COUNT_CAP = 10  # 検証中軸の「有意条件数」の飽和上限(暫定値、実データで調整)


def compute_store_recommend_score(db_path: str, hole_name: str) -> dict:
    """
    [今後の実装予定.md 4節「機能B理想形」項目1、2026-07-13決定事項「案cハイブリッド」]
    店舗の「設定予測の的中が期待できる度合い」をスコア化する。build_habit_matrixの
    軸別ステータスを再利用し、軸ごとの寄与を次のルールで合算する:
      - 実績ベース(有効/弱い/無効): spearman相関をそのまま加算(符号付き、-1〜1相当)
      - 検証中(有意条件ベース): min(有意条件数, _HABIT_CONDITION_COUNT_CAP) × 最大効果量を加算
        (teppan_conditionsの件数は機種×台番号×条件の組合せ数であり、
        group_calendar_conditionsのFDR補正済み件数と桁が異なるため飽和させて揃える。
        実データ検証で無キャップ版が三ノ輪unoのS_鉄板台(件数48)だけでスコアが
        50超に張り付く事故を確認済み)
      - データ不足・対象外(当日記述型): 寄与0(スキップ)
    S_稼働低さは決定事項により本スコアに含めない(呼び出し側でstore_profileから
    別途「添えスコア」として取得すること)。

    Returns: {'おすすめ度': float, '有効軸数': int, '実績軸数': int}
    """
    habit_df = build_habit_matrix(db_path, hole_name)
    contrib = 0.0
    n_axes = 0
    n_accuracy_based = 0
    for _, row in habit_df.iterrows():
        status = row['ステータス']
        if status in ('対象外(当日記述型)', 'データ不足'):
            continue
        n_axes += 1
        if status in ('有効', '弱い', '無効'):
            contrib += float(row['値'])
            n_accuracy_based += 1
        elif status == '検証中':
            effect, count = row['値'], row['件数']
            if pd.notna(effect) and pd.notna(count):
                contrib += float(effect) * min(float(count), _HABIT_CONDITION_COUNT_CAP)

    return {'おすすめ度': contrib, '有効軸数': n_axes, '実績軸数': n_accuracy_based}


def _style_habit_status(df: pd.DataFrame, status_col: str, color_map: dict[str, str], default_color: str):
    """ステータス列を色分けするStyler(style_signedと同じhasattr分岐でpandasバージョン差異に対応)。"""
    def _color(v):
        return f'color: {color_map.get(v, default_color)}'

    styler = df.style
    style_fn = styler.map if hasattr(styler, 'map') else styler.applymap
    return style_fn(_color, subset=[status_col])


# ── 公開関数 ──────────────────────────────────────────────────────


def load_store_profiles(db_path: str) -> pd.DataFrame:
    """store_profile テーブルを全店舗分読み込む。"""
    return _load_profile_from_db(db_path)


def render_store_detail(profiles: pd.DataFrame, sel_hole: str) -> None:
    """
    機能B-個別店舗詳細: γ_store・癖の有効性マトリクス・検知期間履歴・カレンダーヒートマップ。
    app.pyの店舗トップページから店舗固定で呼ばれる。
    """
    import streamlit as st
    import plotly.graph_objects as go

    import ui_theme as ui

    hole_grp = profiles[profiles['ホール名'] == sel_hole].copy()
    if hole_grp.empty:
        st.info(
            f'{sel_hole} の store_profile データがありません。'
            'fase2/run_store_profile.py を先に実行してください。'
        )
        return

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

    # ── 4. 癖の有効性マトリクス ──
    st.subheader(f'{sel_hole} — 癖の有効性')
    st.caption(
        '各軸(癖)がこの店舗で翌日予測に有効かを表示します。'
        'prediction_accuracy(実測との答え合わせ)が十分に蓄積した軸は実績ベース、'
        '未蓄積の軸は有意条件の検出有無を暫定指標とします'
        '(今後の実装予定.md 4節「機能B理想形」項目3、案cハイブリッド)。'
    )

    habit_df = build_habit_matrix(str(ds.ANALYSIS_DB_PATH), sel_hole)
    status_colors = {
        '有効': ui.POS_COLOR, '弱い': ui.ACCENT, '検証中': ui.TEXT_SUB,
        'データ不足': ui.TEXT_SUB, '無効': ui.NEG_COLOR, '対象外(当日記述型)': ui.TEXT_SUB,
    }
    disp_habit = habit_df.copy()
    disp_habit['値'] = disp_habit['値'].map(lambda x: f'{x:.3f}' if pd.notna(x) else '-')
    disp_habit['件数'] = disp_habit['件数'].map(lambda x: '-' if x is None or pd.isna(x) else str(int(x)))
    habit_styler = _style_habit_status(
        disp_habit[['軸', 'ステータス', '指標', '値', '件数']],
        'ステータス', status_colors, ui.TEXT,
    )
    st.dataframe(habit_styler, use_container_width=True, hide_index=True)

    st.divider()

    # ── 5. 検知期間履歴 ──
    st.subheader(f'{sel_hole} — 検知期間履歴')
    st.caption(
        '各サブスコアが検出された機種名・台番号つきの期間を表示します'
        '(全台系はmachine_judgment_log、新台/増台はintroduction_events、'
        '鉄板台/移動台/据え置きはstage3_scoresを使用。稼働低さは店舗全体の指標のため機種名なし)。'
    )

    hist_all = _load_pattern_history(str(ds.ANALYSIS_DB_PATH))
    hist_hole = (
        hist_all[hist_all['ホール名'] == sel_hole].copy()
        if not hist_all.empty else pd.DataFrame()
    )

    db_path_str = str(ds.ANALYSIS_DB_PATH)
    tab_names = list(_PATTERN_LABELS.values())
    period_tabs = st.tabs(tab_names)

    for tab, pattern_name in zip(period_tabs, tab_names):
        with tab:
            if pattern_name == 'S_全台系':
                periods = _load_zentaikei_periods(db_path_str, sel_hole)
                if periods.empty:
                    st.info('検出期間履歴がまだありません。')
                else:
                    disp = periods.sort_values('終了', ascending=False).copy()
                    disp['開始'] = disp['開始'].dt.strftime('%m/%d')
                    disp['終了'] = disp['終了'].dt.strftime('%m/%d')
                    disp['期待高設定台数_平均'] = disp['期待高設定台数_平均'].map(lambda x: f'{x:.1f}')
                    st.dataframe(
                        disp[['開始', '終了', '機種名', '判定ラベル', '台数', '期待高設定台数_平均']],
                        use_container_width=True, hide_index=True,
                    )

            elif pattern_name == 'S_ローテ':
                st.info(
                    '検出ロジックを再設計中のため未実装です'
                    '(今後の実装予定.md 1.2節「S_ローテの非該当日判定・翌日予測拡張」参照。'
                    '正解発表データでの実証を経てから着手予定)。'
                )

            elif pattern_name == 'S_新台増台':
                events = _load_introduction_events(db_path_str, sel_hole, ('新台', '増台'))
                if events.empty:
                    st.info('検出期間履歴がまだありません。')
                else:
                    disp = events.copy()
                    disp['台番号'] = disp['台番号リスト'].map(_format_unit_list)
                    disp['日付'] = pd.to_datetime(disp['日付']).dt.strftime('%m/%d')
                    st.dataframe(
                        disp[['日付', '機種名', 'カテゴリ', '台数変化', '台番号']],
                        use_container_width=True, hide_index=True,
                    )

            elif pattern_name == 'S_稼働低さ':
                kadou_hist = hist_hole[hist_hole['パターン'] == 's_kadou'] if not hist_hole.empty else pd.DataFrame()
                periods = _detect_pattern_periods(kadou_hist)
                if periods.empty:
                    st.info('検出期間履歴がまだありません。')
                else:
                    disp = periods.sort_values('終了', ascending=False).copy()
                    disp['開始'] = pd.to_datetime(disp['開始']).dt.strftime('%m/%d')
                    disp['終了'] = pd.to_datetime(disp['終了']).dt.strftime('%m/%d')
                    disp['平均スコア'] = disp['平均スコア'].map(lambda x: f'{x:.3f}')
                    st.dataframe(
                        disp[['開始', '終了', '平均スコア']],
                        use_container_width=True, hide_index=True,
                    )

            else:
                # S_鉄板台 / S_移動台 / S_据え置き は stage3_scores の列名と表示名が一致
                periods = _load_stage3_pattern_periods(db_path_str, sel_hole, pattern_name)
                if periods.empty:
                    st.info('検出期間履歴がまだありません。')
                else:
                    disp = periods.sort_values('終了', ascending=False).copy()
                    disp['開始'] = disp['開始'].dt.strftime('%m/%d')
                    disp['終了'] = disp['終了'].dt.strftime('%m/%d')
                    disp['平均スコア'] = disp['平均スコア'].map(lambda x: f'{x:.3f}')
                    st.dataframe(
                        disp[['開始', '終了', '機種名', '台番号', '平均スコア']],
                        use_container_width=True, hide_index=True,
                    )

    st.divider()

    # ── 6. カレンダーヒートマップ ──
    st.subheader(f'{sel_hole} — カレンダーヒートマップ')
    st.caption(
        '店舗内相対評価(-1〜1、この店舗自身の中央値を基準にした符号付きパーセンタイル)で'
        '差枚・設定配分を表示します。データ取得済み日は実績、それ以降の日はカレンダー型の'
        '検出パターン(曜日・毎月X日・機種の看板パターン等)からの予測です'
        '(今後の実装予定.md 4節「機能B理想形」項目5)。'
        '★は「店舗×曜日の癖軸」(1.9節、検証中・並走記録のみ)で有意と判定された日を示します。'
    )

    daily_avg = _load_daily_stage3_avg(str(ds.ANALYSIS_DB_PATH), sel_hole)     # 設定配分(実績)
    daily_diff = _load_daily_actual_diff(sel_hole)                            # 差枚(実績)
    rel_avg = _store_relative_scores(daily_avg)
    rel_diff = _store_relative_scores(daily_diff)
    last_actual_date = max(daily_avg.index) if not daily_avg.empty else None

    this_month = pd.Timestamp.now().strftime('%Y-%m')
    available_months = sorted(
        {d[:7] for d in rel_avg} | {d[:7] for d in rel_diff} | {this_month},
        reverse=True,
    )

    metric_options = ['設定配分', '差枚']
    c1, c2 = st.columns(2)
    with c1:
        sel_month = st.selectbox('表示月', available_months, index=0, key='calendar_month')
    with c2:
        sel_metric = st.selectbox('表示項目', metric_options, index=0, key='calendar_metric')

    year, month = int(sel_month[:4]), int(sel_month[5:7])
    rel_map = rel_avg if sel_metric == '設定配分' else rel_diff

    import calendar as _calmod
    n_days = _calmod.monthrange(year, month)[1]
    month_dates = [pd.Timestamp(year, month, d) for d in range(1, n_days + 1)]
    future_dates = [
        d for d in month_dates
        if last_actual_date is None or d.strftime('%Y-%m-%d') > last_actual_date
    ]
    conditions = _load_future_calendar_conditions(str(ds.ANALYSIS_DB_PATH), sel_hole)
    projected = _project_future_calendar_scores(conditions, future_dates)

    combined_map = {**rel_map, **projected}

    store_day_conditions = _load_store_day_conditions(str(ds.ANALYSIS_DB_PATH), sel_hole)
    event_days = _mark_event_days(store_day_conditions, month_dates)

    if not combined_map:
        st.info('カレンダー表示用のデータがありません。')
    else:
        z, text = _month_grid(combined_map, year, month, event_days=event_days)
        # DIVERGINGは中間点が白のため、白文字(ui.TEXT)だと0付近のセルで読めなくなる。
        # 濃色文字はcard_bg〜赤/青の全域で3:1以上のコントラストを確保できるためこちらを使う。
        fig_cal = go.Figure(data=go.Heatmap(
            z=z, x=_WEEKDAY_LABELS, y=[f'第{i + 1}週' for i in range(z.shape[0])],
            text=text, texttemplate='%{text}', textfont=dict(size=11, color=ui.CARD_BG),
            colorscale=ui.DIVERGING, zmid=0.0, zmin=-1.0, zmax=1.0,
            hoverongaps=False,
        ))
        ui.apply_mobile_layout(fig_cal, height=280)
        fig_cal.update_yaxes(autorange='reversed')
        st.plotly_chart(fig_cal, use_container_width=True, config=ui.PLOTLY_CONFIG)
        if any(d.strftime('%Y-%m-%d') in projected for d in future_dates):
            st.caption(f'{last_actual_date}以前は実績、それ以降はカレンダー型パターンからの予測です。')


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
