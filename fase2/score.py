"""
score.py — スコア統合・稼働推定・店舗プロファイル管理

S_稼働低さ (score_kadou_hikusha):
    店舗全体の回転数集計から混雑度の低さを自動推定(手入力不要)
    ※ 個台チャンネル③(Stage2の役割②)とは目的が異なる

合成スコア (synthesize):
    狙い目度 = Σ(wᵢ×Sᵢ) ÷ Σ(wᵢ)
    NaN(データ不足)のサブスコアは「0点」ではなく「除外」して再正規化

店舗プロファイル (update_store_profile):
    振り返り分析ごとに store_profile テーブルを再計算・更新
    γ_store は multi_store.py の Stage5 で学習されたら保存される

Stage3スコア保存 (write_stage3_scores):
    台×日ごとのlog_odds/high_prob/is_invalidを分析DBへ保存
    機能A(可視化の初期表示高速化)・機能B(熱い台)が読む

鉄板台検出条件保存 (write_teppan_conditions):
    S_鉄板台がどの条件(カレンダー候補/周期)で有意だったかを分析DBへ保存
    「明日は該当日か」の判断・機能B再設計のトップページ表示に使う

日次スナップショット記録 (write_score_snapshot):
    wᵢ(サブスコア重み)学習用の教師データを店舗×使用データ最終日の粒度でappend-only蓄積
    (記録側のみ先行実装。学習側は蓄積後の別タスク)

機種判定ログ記録 (write_machine_judgment_log):
    patterns.score_zentaikei_judgmentの出力(機種×日×ホールの全台系/高配分判定)を
    append-only蓄積(全履歴バックフィル対応。今後の実装予定.md 1.8節 Phase1)

末尾版レイヤー2検定結果保存 (write_group_calendar_conditions):
    patterns.build_group_calendar_conditionsの出力(台番号末尾グループ×日付条件の
    Mann-Whitney U検定+一致ルール2本)を分析DBへ保存(teppan_conditionsと同じ
    店舗単位で全削除→再挿入。今後の実装予定.md 1.8節「末尾版」フェーズ2)

依存: patterns.py (各サブスコア列), preprocess.py (回転数列)
"""
import sqlite3
import json
import numpy as np
import pandas as pd
from pathlib import Path

WEIGHTS_PATH = Path(__file__).parent / 'weights.json'
UPLIMIT_CONFIG_PATH = Path(__file__).parent / 'uplimit_config.json'

SUB_SCORES = [
    'S_全台系', 'S_鉄板台', 'S_ローテ', 'S_新台増台',
    'S_移動台', 'S_据え置き', 'S_稼働低さ',
]

# store_profile テーブルのパターンキー → サブスコア列名
_PATTERN_MAP = {
    's_all':     'S_全台系',
    's_teppan':  'S_鉄板台',
    's_rote':    'S_ローテ',
    's_shintai': 'S_新台増台',
    's_idoudai': 'S_移動台',
    's_sueki':   'S_据え置き',
    's_kadou':   'S_稼働低さ',
}


def score_kadou_hikusha(df: pd.DataFrame, hole_name: str) -> pd.Series:
    """
    S_稼働低さ: 店舗全体の回転数集計から混雑度の低さを 0〜1 で返す。
    基準値 = 過去の同条件(同曜日等)の平均。

    全台合計回転数が基準値より低い日 → スコア高 (稼働が少なく参加しやすい)
    全台合計回転数が基準値以上の日   → スコア 0
    """
    scores = pd.Series(np.nan, index=df.index)
    mask = df['ホール名'] == hole_name
    sub = df[mask]

    if sub.empty or '回転数' not in sub.columns:
        return scores

    # 日次合計回転数（全台の合計 = 店舗稼働の代理指標）
    daily_total = (
        sub.dropna(subset=['回転数'])
        .groupby('日付')['回転数']
        .sum()
    )
    if daily_total.empty:
        return scores

    daily_df = daily_total.reset_index()
    daily_df.columns = ['日付', '合計回転数']
    daily_df['曜日'] = pd.to_datetime(daily_df['日付'], errors='coerce').dt.dayofweek

    # 曜日ごとの基準値（同曜日全期間の平均）
    dow_baseline = daily_df.groupby('曜日')['合計回転数'].mean()
    daily_df['基準値'] = daily_df['曜日'].map(dow_baseline)
    # 曜日データが取れない場合は全体平均にフォールバック
    daily_df['基準値'] = daily_df['基準値'].fillna(daily_df['合計回転数'].mean())

    # S_稼働低さ = clip(1 - 今日の合計 / 基準値, 0, 1)
    daily_df['S_稼働低さ'] = np.where(
        daily_df['基準値'] > 0,
        (1.0 - daily_df['合計回転数'] / daily_df['基準値']).clip(0.0, 1.0),
        np.nan,
    )

    score_map = dict(zip(daily_df['日付'], daily_df['S_稼働低さ']))
    scores.loc[mask] = sub['日付'].map(score_map).values

    return scores


_DEFAULT_UPLIMIT_CONFIG = {
    '分位点': 0.9,
    '安全マージン': 0.05,
    '絶対上限': 0.5,
    '業界一般値フォールバック': 0.4,
    '短期ウィンドウ日数': 30,
    '最低必要日数': 30,
    '特異日除外リスト': [],
}


def load_uplimit_config(path: str | Path | None = None) -> dict:
    """
    店舗×日 高設定台数上限キャリブレーション(候補C・Step1)の設定
    (uplimit_config.json)を読み込む。weights.jsonと同様、コード変更なしで
    調整できるよう外出し。ファイルが無い/一部キーが無い場合はデフォルト値で補完する。
    """
    path = Path(path) if path else UPLIMIT_CONFIG_PATH
    if not path.exists():
        return dict(_DEFAULT_UPLIMIT_CONFIG)
    loaded = json.loads(path.read_text(encoding='utf-8'))
    return {**_DEFAULT_UPLIMIT_CONFIG, **loaded}


def compute_daily_uplimit_ratio(df: pd.DataFrame, hole_name: str) -> pd.Series:
    """
    店舗×日でΣhigh_prob/稼働台数(is_invalid除外後)を集計して返す(日付→比率)。
    機種・島単位ではなく店舗全体の集計である点に注意
    (1機種に投入が集中しても店舗全体の投入率が上限内なら発動しない、という
    前提を満たすため。詳細はデータ分析_skill.md「店舗×日 高設定台数上限キャリブレーション」参照)。
    """
    mask = df['ホール名'] == hole_name
    sub = df[mask]
    if 'is_invalid' in sub.columns:
        sub = sub[~sub['is_invalid'].fillna(True)]
    sub = sub.dropna(subset=['high_prob'])
    if sub.empty:
        return pd.Series(dtype=float)
    daily = sub.groupby('日付')['high_prob'].agg(['sum', 'count'])
    return daily['sum'] / daily['count']


def _solve_uplimit_offset(
    log_odds_values: np.ndarray,
    target_ratio: float,
    max_offset: float = 20.0,
    n_iter: int = 60,
) -> float:
    """
    mean(sigmoid(log_odds - offset)) == target_ratio となる offset(>=0)を二分探索で求める。
    sigmoidはoffsetに対して単調減少するため二分探索が使える。
    既に target_ratio 以下なら 0.0(補正不要)を返す。
    """
    from preprocess import sigmoid

    def mean_prob(offset: float) -> float:
        return float(np.mean(sigmoid(log_odds_values - offset)))

    if mean_prob(0.0) <= target_ratio:
        return 0.0

    lo, hi = 0.0, max_offset
    for _ in range(n_iter):
        mid = (lo + hi) / 2.0
        if mean_prob(mid) > target_ratio:
            lo = mid
        else:
            hi = mid
    return hi


def compute_uplimit(df_scored: pd.DataFrame, hole_name: str, config: dict | None = None) -> dict:
    """
    店舗×日 高設定台数上限キャリブレーション(候補C・Step1。詳細はデータ分析_skill.md参照)。

    Stage3は台ごとに独立にsigmoid(log_odds)を出力するため、店舗内で合計すると
    理論上ありえない水準までE[高設定台数]/Nが積み上がることがある。これを検出し、
    店舗×日単位で確率を下方に補正(shrinkage、ハードキャップではない連続的な縮小)する。

    上限の推定は既存の長期/短期αブレンド(候補C)を流用: 全履歴の分位点(長期)と
    直近M日の分位点(短期)をpatterns.FIXED_ALPHAでブレンドし、安全マージンを足して
    絶対上限(0.5)でクリップする。データ不足(新規店等)は業界一般値フォールバックを使う。
    α自体の学習(実測差枚に対する予測力の検証)は前提条件のStage7評価ハーネス
    (prediction_log/evaluate_predictions.py)を店舗×日集計向けに転用してから行う方針で、
    今回はFIXED_ALPHAの固定値で暫定実装する(今後の実装予定.md 2.2節参照)。

    特異日(周年・グランドオープン等)は config の特異日除外リストに日付文字列を
    追加することで、分位点の統計にもキャップ適用対象にも含めない(案A、暫定は空リスト)。

    [副作用] 超過日についてdf_scoredの該当行('log_odds'・'high_prob')をin-placeで
    書き換える(全台一律offsetをlog_oddsから引く)。呼び出し側はこの関数の後に
    write_stage3_scoresを呼ぶこと(補正後の値を保存するため)。

    Returns:
        {'上限キャリブレーション値': float, '上限信頼度': float,
         '発動日数': int, '対象日数': int}
    """
    config = config or load_uplimit_config()
    exclude_dates = set(config.get('特異日除外リスト', []))

    ratio_all = compute_daily_uplimit_ratio(df_scored, hole_name)
    stats_ratio = ratio_all.drop(
        index=[d for d in ratio_all.index if d in exclude_dates], errors='ignore'
    )
    n_days = len(stats_ratio)
    min_days = int(config['最低必要日数'])

    if n_days < min_days:
        # データ不足(新規店等) → 業界一般値を暫定使用。offsetは適用しない
        return {
            '上限キャリブレーション値': float(config['業界一般値フォールバック']),
            '上限信頼度': 0.0,
            '発動日数': 0,
            '対象日数': n_days,
        }

    quantile = float(config['分位点'])
    long_q = float(stats_ratio.quantile(quantile))

    short_window = int(config['短期ウィンドウ日数'])
    sorted_dates = sorted(stats_ratio.index)
    if len(sorted_dates) >= short_window:
        short_ratio = stats_ratio.loc[sorted_dates[-short_window:]]
        short_q = float(short_ratio.quantile(quantile))
    else:
        short_q = None

    import patterns as pt
    blended_q = pt.blend_scalar(long_q, short_q, pt.FIXED_ALPHA)

    margin = float(config['安全マージン'])
    abs_cap = float(config['絶対上限'])
    uplimit = min(abs_cap, blended_q + margin)

    day_factor = min(1.0, n_days / 30.0)
    sample_factor = min(1.0, n_days / 50.0)
    reliability = float(np.clip(day_factor * 0.7 + sample_factor * 0.3, 0.0, 1.0))

    from preprocess import sigmoid

    n_exceed = 0
    hole_mask = df_scored['ホール名'] == hole_name
    for date, ratio_val in ratio_all.items():
        if date in exclude_dates or ratio_val <= uplimit:
            continue
        day_mask = hole_mask & (df_scored['日付'] == date)
        if 'is_invalid' in df_scored.columns:
            day_mask &= ~df_scored['is_invalid'].fillna(True)
        day_mask &= df_scored['log_odds'].notna()
        idx = df_scored.index[day_mask]
        if len(idx) == 0:
            continue

        log_odds_vals = df_scored.loc[idx, 'log_odds'].values.astype(float)
        offset = _solve_uplimit_offset(log_odds_vals, uplimit)
        if offset <= 0.0:
            continue

        new_log_odds = log_odds_vals - offset
        df_scored.loc[idx, 'log_odds'] = new_log_odds
        df_scored.loc[idx, 'high_prob'] = sigmoid(new_log_odds)
        n_exceed += 1

    return {
        '上限キャリブレーション値': float(uplimit),
        '上限信頼度': reliability,
        '発動日数': n_exceed,
        '対象日数': n_days,
    }


def compute_reliability(df: pd.DataFrame, score_col: str) -> float:
    """
    指定サブスコアの信頼度を計算する(履歴日数・サンプル数ベース)。
    is_biased フラグが立っている場合は値を下げる。

    - 30日以上のデータで day_factor = 1.0
    - 50サンプル以上で sample_factor = 1.0
    - 最終スコア = day_factor×0.7 + sample_factor×0.3
    """
    if score_col not in df.columns:
        return 0.0

    non_nan_idx = df[score_col].dropna().index
    n_samples = len(non_nan_idx)
    if n_samples == 0:
        return 0.0

    n_days = (
        df.loc[non_nan_idx, '日付'].nunique()
        if '日付' in df.columns
        else n_samples
    )

    day_factor = min(1.0, n_days / 30.0)
    sample_factor = min(1.0, n_samples / 50.0)
    reliability = day_factor * 0.7 + sample_factor * 0.3

    # 欠損偏りフラグによるペナルティ
    if 'is_biased' in df.columns:
        biased_rate = float(df['is_biased'].fillna(False).mean())
        reliability *= max(0.0, 1.0 - biased_rate)

    return float(np.clip(reliability, 0.0, 1.0))


def synthesize(
    df: pd.DataFrame,
    weights: dict,
    reliabilities: dict[str, float] | None = None,
) -> pd.DataFrame:
    """
    Σ(wᵢ×Sᵢ) ÷ Σ(wᵢ) を計算。
    Sᵢ が NaN の行は分子・分母とも除外して再正規化する。

    reliabilities: サブスコア列名→信頼度(店舗×パターンで1つの値。
    compute_reliability(df, score_col)を列ごとに事前計算して渡す)。
    渡された列は有効重み = weights.get(col,1.0) × reliabilities.get(col,1.0) となり、
    信頼度が低いサブスコアほど合成への寄与が減衰する。省略時は従来通り重みのみ。

    追加列:
      狙い目度       — 加重平均スコア ([-1,1] または NaN。符号付きサブスコア混在のため)
      有効サブスコア数 — 各行で計算に使われたサブスコアの個数
    """
    out = df.copy()
    reliabilities = reliabilities or {}

    available = [s for s in SUB_SCORES if s in df.columns]

    numerator = pd.Series(0.0, index=df.index)
    denominator = pd.Series(0.0, index=df.index)
    valid_count = pd.Series(0, index=df.index)

    for score_col in available:
        w = float(weights.get(score_col, 1.0)) * float(reliabilities.get(score_col, 1.0))
        valid_mask = df[score_col].notna()
        numerator[valid_mask] += w * df.loc[valid_mask, score_col]
        denominator[valid_mask] += w
        valid_count[valid_mask] += 1

    out['狙い目度'] = np.where(denominator > 0, numerator / denominator, np.nan)
    out['有効サブスコア数'] = valid_count

    return out


_CREATE_STAGE3_SCORES_SQL = '''
    CREATE TABLE IF NOT EXISTS stage3_scores (
        日付       TEXT NOT NULL,
        ホール名   TEXT NOT NULL,
        機種名     TEXT NOT NULL,
        台番号     INTEGER NOT NULL,
        log_odds   REAL,
        high_prob  REAL,
        is_invalid INTEGER,
        PRIMARY KEY (日付, ホール名, 機種名, 台番号)
    )
'''


# [2026-07 UIリニューアル] トップページ「熱い台予測」7タブが、再計算なしで最新スナップショットを
# SELECTするだけで済むよう、幅型/深さ型サブスコアも台×日粒度でstage3_scoresへ保存する。
_STAGE3_PATTERN_COLS = ['S_全台系', 'S_鉄板台', 'S_ローテ', 'S_新台増台', 'S_移動台', 'S_据え置き']


def _ensure_stage3_scores_schema(con: sqlite3.Connection) -> None:
    """
    [2026-07 UIリニューアル] サブスコア6列を追加するマイグレーション。
    既存DBはCREATE TABLE IF NOT EXISTSでは列が増えないため、teppan_conditions等と同様
    PRAGMA table_infoで存在確認してからALTER TABLEする。
    """
    con.execute(_CREATE_STAGE3_SCORES_SQL)
    cols = [row[1] for row in con.execute('PRAGMA table_info(stage3_scores)').fetchall()]
    for col in _STAGE3_PATTERN_COLS:
        if col not in cols:
            con.execute(f'ALTER TABLE stage3_scores ADD COLUMN "{col}" REAL')


def write_stage3_scores(db_path: str, hole_name: str, df_scored: pd.DataFrame) -> None:
    """
    Stage3出力(log_odds / high_prob / is_invalid)を台×日単位で分析DBへ保存する。
    店舗単位で全削除→再挿入(再計算のたびに全量を最新化する)。
    機能A(app_a: 初期表示の高速化)・機能B(app_b: 熱い台)がこのテーブルを読む。

    df_scoredに_STAGE3_PATTERN_COLS(幅型/深さ型サブスコア)が含まれていれば併せて保存する
    (トップページ「熱い台予測」7タブが最新日のスナップショットを再計算なしで参照するため)。
    含まれない列はNULLのまま保存する(必須列チェックの対象外)。
    """
    required = ['日付', '機種名', '台番号', 'log_odds', 'high_prob', 'is_invalid']
    missing = [c for c in required if c not in df_scored.columns]
    if missing:
        raise ValueError(f'stage3_scores保存に必要な列がありません: {missing}')

    pattern_cols = [c for c in _STAGE3_PATTERN_COLS if c in df_scored.columns]

    sub = df_scored.dropna(subset=['日付', '機種名', '台番号'])
    rows = [
        (
            str(r.日付), hole_name, str(r.機種名), int(r.台番号),
            None if pd.isna(r.log_odds) else float(r.log_odds),
            None if pd.isna(r.high_prob) else float(r.high_prob),
            int(bool(r.is_invalid)),
            *[
                None if pd.isna(getattr(r, col)) else float(getattr(r, col))
                for col in pattern_cols
            ],
        )
        for r in sub.itertuples()
    ]

    all_cols = ['日付', 'ホール名', '機種名', '台番号', 'log_odds', 'high_prob', 'is_invalid', *pattern_cols]
    col_list = ', '.join(f'"{c}"' for c in all_cols)
    placeholders = ', '.join('?' * len(all_cols))

    con = sqlite3.connect(db_path)
    try:
        _ensure_stage3_scores_schema(con)
        con.execute('DELETE FROM stage3_scores WHERE ホール名 = ?', (hole_name,))
        con.executemany(
            f'INSERT OR REPLACE INTO stage3_scores ({col_list}) VALUES ({placeholders})',
            rows,
        )
        con.commit()
    finally:
        con.close()


_CREATE_TEPPAN_CONDITIONS_SQL = '''
    CREATE TABLE IF NOT EXISTS teppan_conditions (
        ホール名 TEXT NOT NULL,
        機種名   TEXT NOT NULL,
        台番号   INTEGER NOT NULL,
        経路     TEXT NOT NULL,
        条件     TEXT NOT NULL,
        効果量   REAL,
        周期日数 INTEGER,
        更新日時 TEXT
    )
'''


def _ensure_teppan_conditions_schema(con: sqlite3.Connection) -> None:
    """
    [Stage7-0] 周期日数カラム(周期経路の位相アンカー。翌日投影predict_next_dayで使う)を
    追加するマイグレーション。既存DBはCREATE TABLE IF NOT EXISTSでは列が増えないため、
    PRAGMA table_infoで存在確認してからALTER TABLEする。
    """
    con.execute(_CREATE_TEPPAN_CONDITIONS_SQL)
    cols = [row[1] for row in con.execute('PRAGMA table_info(teppan_conditions)').fetchall()]
    if '周期日数' not in cols:
        con.execute('ALTER TABLE teppan_conditions ADD COLUMN 周期日数 INTEGER')


def write_teppan_conditions(db_path: str, hole_name: str, details: list[dict]) -> None:
    """
    S_鉄板台の検出条件(patterns.score_teppandaiのdetails_out)を分析DBへ保存する。
    店舗単位で全削除→再挿入。detailsが空でも古い行の削除は行う
    (再計算で検出されなくなった条件を残さないため)。

    周期経路の行は周期日数(lag、観測順)を保存する(カレンダー経路の行はNULL)。
    位相の起点日時刻自体は保存しない。翌日投影(predict_next_day)は、その時点で
    計算済みの観測順history(hp)を使い次の観測点のインデックスから直接位相を
    算出できるため、日次の位相を事前計算して保存する必要がない。
    """
    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = [
        (hole_name, d['機種名'], int(d['台番号']), d['経路'], d['条件'],
         None if d.get('効果量') is None else float(d['効果量']),
         d.get('周期日数'), now)
        for d in details
        if d.get('ホール名') == hole_name
    ]

    con = sqlite3.connect(db_path)
    try:
        _ensure_teppan_conditions_schema(con)
        con.execute('DELETE FROM teppan_conditions WHERE ホール名 = ?', (hole_name,))
        if rows:
            con.executemany(
                '''
                INSERT INTO teppan_conditions
                    (ホール名, 機種名, 台番号, 経路, 条件, 効果量, 周期日数, 更新日時)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                rows,
            )
        con.commit()
    finally:
        con.close()


_CREATE_GROUP_CALENDAR_CONDITIONS_SQL = '''
    CREATE TABLE IF NOT EXISTS group_calendar_conditions (
        ホール名         TEXT NOT NULL,
        グループ種別     TEXT NOT NULL,
        グループ         TEXT NOT NULL,
        日付条件         TEXT NOT NULL,
        該当日数         INTEGER,
        p値              REAL,
        効果量           REAL,
        BH有意           INTEGER,
        台数中央値       REAL,
        使用データ最終日 TEXT NOT NULL,
        更新日時         TEXT
    )
'''


def _ensure_group_calendar_conditions_schema(con: sqlite3.Connection) -> None:
    """
    [2026-07 機種単位の癖分析] 台数中央値カラム(機種版のみ使用。使用側ゲート(暫定n≥3等)の
    判断材料。末尾版の行はNULLのまま)を追加するマイグレーション。既存DBはCREATE TABLE
    IF NOT EXISTSでは列が増えないため、teppan_conditions等と同様PRAGMA table_infoで
    存在確認してからALTER TABLEする。
    """
    cols = [row[1] for row in con.execute('PRAGMA table_info(group_calendar_conditions)').fetchall()]
    if '台数中央値' not in cols:
        con.execute('ALTER TABLE group_calendar_conditions ADD COLUMN 台数中央値 REAL')


def write_group_calendar_conditions(
    db_path: str, hole_name: str, result_df: pd.DataFrame, last_date: str,
    group_types: str | list[str] | None = None,
) -> None:
    """
    [今後の実装予定.md 1.8節「末尾版」「機種単位の癖分析」] patterns.build_group_calendar_conditions
    の出力を分析DBへ保存する。teppan_conditionsと同じ「グループ種別単位で全削除→再挿入」方式
    (全期間の再検定のたびに最新化する。machine_judgment_logと異なりappend-onlyの
    履歴蓄積ではない)。

    有意でない組み合わせも含め全条件を保存する(2026-07-10確定設計「保存は緩く・
    使用側でゲート」。予測時の重複統合(同一グループでmax効果量を採用)は呼び出し側の
    predict_*_next_dayで行う)。

    group_types: 削除対象のグループ種別(省略時はresult_dfのグループ種別列から推定)。
    result_dfが空になり得る呼び出し(例: 直近窓検定でデータ不足のため0行)では、削除対象が
    推定できず何も削除されない事故を防ぐため呼び出し側で明示的に渡すこと(2026-07修正:
    以前は「ホール名のみ」で全グループ種別を無条件に削除していたため、例えば末尾版
    ('台番号末尾')の書き込み後に機種版('機種')を書き込むと末尾版の既存行が消えていた)。
    台数中央値列は末尾版など該当しない行はNULLのまま保存する(result_dfに列がなくても可)。
    """
    if group_types is None:
        group_types = result_df['グループ種別'].unique().tolist() if not result_df.empty else []
    elif isinstance(group_types, str):
        group_types = [group_types]

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    has_size_col = '台数中央値' in result_df.columns
    rows = [
        (
            hole_name, r['グループ種別'], r['グループ'], r['日付条件'],
            None if pd.isna(r['該当日数']) else int(r['該当日数']),
            None if pd.isna(r['p_raw']) else float(r['p_raw']),
            None if pd.isna(r['効果量']) else float(r['効果量']),
            int(bool(r['BH有意'])),
            None if not has_size_col or pd.isna(r['台数中央値']) else float(r['台数中央値']),
            last_date, now,
        )
        for _, r in result_df.iterrows()
    ]

    con = sqlite3.connect(db_path)
    try:
        con.execute(_CREATE_GROUP_CALENDAR_CONDITIONS_SQL)
        _ensure_group_calendar_conditions_schema(con)
        for gt in group_types:
            con.execute(
                'DELETE FROM group_calendar_conditions WHERE ホール名 = ? AND グループ種別 = ?',
                (hole_name, gt),
            )
        if rows:
            con.executemany(
                '''
                INSERT INTO group_calendar_conditions
                    (ホール名, グループ種別, グループ, 日付条件, 該当日数, p値, 効果量, BH有意, 台数中央値, 使用データ最終日, 更新日時)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                rows,
            )
        con.commit()
    finally:
        con.close()


_CREATE_PATTERN_HISTORY_SQL = '''
    CREATE TABLE IF NOT EXISTS pattern_history (
        ホール名 TEXT NOT NULL,
        パターン TEXT NOT NULL,
        スコア   REAL,
        信頼度   REAL,
        実行日時 TEXT NOT NULL
    )
'''


def write_pattern_history(db_path: str, hole_name: str, df_scored: pd.DataFrame) -> None:
    """
    各サブスコアの実行時点のスコア・信頼度を pattern_history へ追記する。
    store_profile と異なり最新1行への上書きではなく、いつからそのパターンが
    検出されていたかを後から追える履歴として残すため、DELETEは行わない
    (append-only)。
    """
    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []

    for pattern, score_col in _PATTERN_MAP.items():
        if score_col not in df_scored.columns:
            continue
        score_mean = df_scored[score_col].dropna().mean()
        score_val = None if pd.isna(score_mean) else float(score_mean)
        reliability = compute_reliability(df_scored, score_col)
        rows.append((hole_name, pattern, score_val, reliability, now))

    if not rows:
        return

    con = sqlite3.connect(db_path)
    try:
        con.execute(_CREATE_PATTERN_HISTORY_SQL)
        con.executemany(
            '''
            INSERT INTO pattern_history
                (ホール名, パターン, スコア, 信頼度, 実行日時)
            VALUES (?, ?, ?, ?, ?)
            ''',
            rows,
        )
        con.commit()
    finally:
        con.close()


_CREATE_PREDICTION_LOG_SQL = '''
    CREATE TABLE IF NOT EXISTS prediction_log (
        予測ID           INTEGER PRIMARY KEY AUTOINCREMENT,
        実行日時         TEXT NOT NULL,
        使用データ最終日 TEXT NOT NULL,
        対象日           TEXT NOT NULL,
        ホール名         TEXT NOT NULL,
        機種名           TEXT NOT NULL,
        台番号           INTEGER NOT NULL,
        予測種別         TEXT NOT NULL,
        長期スコア       REAL,
        短期スコア       REAL,
        ブレンド値       REAL,
        使用alpha        REAL,
        詳細             TEXT
    )
'''


def write_prediction_log(db_path: str, rows: list[dict]) -> None:
    """
    S_鉄板台の翌日予測結果(patterns.predict_next_day_with_blend)をprediction_logへ
    追記する(append-only、DELETEしない。pattern_historyと同じ「過去の予測を凍結して
    残す」方針だが、目的は逆向き=未来の答え合わせ用)。

    rows各要素キー: 実行日時, 使用データ最終日, 対象日, ホール名, 機種名, 台番号,
    予測種別, 長期スコア, 短期スコア, ブレンド値, 使用alpha, 詳細(dictまたはJSON文字列)。
    ブレンド値がNone(予測不可)の行は呼び出し側で事前に除外しておくこと。

    [fase4随伴改修] 重複追記ガード: (ホール名, 予測種別, 使用データ最終日)の組が
    既存であれば該当rowsを追記前にスキップする(粒度は台単位ではなくバッチ単位。
    1回のrun_store_profile実行=1店舗×1予測種別×1データ最終日のバッチのため十分)。
    データが進んでいない店舗でrun_store_profileを再実行しても、fase4のcatchup/
    リトライでprediction_logが二重記録されないようにするための単一の防波堤。
    DELETE/UPDATEは行わずINSERT対象を絞るだけなのでappend-only方針は維持される。
    """
    if not rows:
        return

    con = sqlite3.connect(db_path)
    try:
        con.execute(_CREATE_PREDICTION_LOG_SQL)

        existing_keys: set[tuple] = set()
        batch_keys = {(r['ホール名'], r['予測種別'], r['使用データ最終日']) for r in rows}
        for hole_name, pred_type, last_date in batch_keys:
            found = con.execute(
                '''
                SELECT 1 FROM prediction_log
                WHERE ホール名 = ? AND 予測種別 = ? AND 使用データ最終日 = ?
                LIMIT 1
                ''',
                (hole_name, pred_type, last_date),
            ).fetchone()
            if found:
                existing_keys.add((hole_name, pred_type, last_date))

        if existing_keys:
            skipped = [r for r in rows if (r['ホール名'], r['予測種別'], r['使用データ最終日']) in existing_keys]
            rows = [r for r in rows if (r['ホール名'], r['予測種別'], r['使用データ最終日']) not in existing_keys]
            print(
                f'  [write_prediction_log] 既存の予測と重複するため{len(skipped)}件をスキップ: '
                f'{sorted(existing_keys)}'
            )

        if not rows:
            return

        con.executemany(
            '''
            INSERT INTO prediction_log
                (実行日時, 使用データ最終日, 対象日, ホール名, 機種名, 台番号,
                 予測種別, 長期スコア, 短期スコア, ブレンド値, 使用alpha, 詳細)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            [
                (
                    r['実行日時'], r['使用データ最終日'], r['対象日'], r['ホール名'],
                    r['機種名'], int(r['台番号']), r['予測種別'],
                    r.get('長期スコア'), r.get('短期スコア'), r.get('ブレンド値'),
                    r.get('使用alpha'),
                    r['詳細'] if isinstance(r.get('詳細'), str)
                    else json.dumps(r.get('詳細'), ensure_ascii=False),
                )
                for r in rows
            ],
        )
        con.commit()
    finally:
        con.close()


_CREATE_MACHINE_JUDGMENT_LOG_SQL = '''
    CREATE TABLE IF NOT EXISTS machine_judgment_log (
        実行日時       TEXT NOT NULL,
        ホール名       TEXT NOT NULL,
        日付           TEXT NOT NULL,
        機種名         TEXT NOT NULL,
        台数           INTEGER,
        期待高設定台数 REAL,
        zスコア        REAL,
        p値            REAL,
        投入率         REAL,
        S_全台系       REAL,
        判定ラベル     TEXT,
        PRIMARY KEY (ホール名, 日付, 機種名)
    )
'''


def write_machine_judgment_log(db_path: str, judgment_df: pd.DataFrame) -> int:
    """
    [2026-07-09設計合意] patterns.score_zentaikei_judgmentの出力(機種×日×ホール粒度)を
    machine_judgment_logへappend-only記録する(今後の実装予定.md 1.8節 Phase1)。

    prediction_log/score_snapshotと異なり、呼び出し側(run_store_profile.py)は
    毎回「収集済み全履歴」分をまとめて渡す想定(全台系イベントは月数回と稀なため
    記録を早く始める価値がある。この判定はリークの心配がない当日完結の計算)。
    そのため重複ガードは事前の存在チェックではなく、PRIMARY KEY (ホール名, 日付, 機種名)
    に対する INSERT OR IGNORE で行単位に行う(初回=全履歴バックフィル、以降=新規日のみ
    実質追記となり、fase4の日次再実行でも二重記録されない)。

    Returns: 新規に挿入された行数(参考値。INSERT OR IGNOREのため厳密なcursor.rowcountは
    SQLite側の仕様上取れないため、事前の件数差分で算出する)。
    """
    if judgment_df.empty:
        return 0

    con = sqlite3.connect(db_path)
    try:
        con.execute(_CREATE_MACHINE_JUDGMENT_LOG_SQL)
        before = con.execute('SELECT COUNT(*) FROM machine_judgment_log').fetchone()[0]

        now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
        rows = [
            (
                now, r['ホール名'], str(r['日付']), r['機種名'],
                int(r['台数']) if pd.notna(r['台数']) else None,
                None if pd.isna(r['期待高設定台数']) else float(r['期待高設定台数']),
                None if pd.isna(r['zスコア']) else float(r['zスコア']),
                None if pd.isna(r['p値']) else float(r['p値']),
                None if pd.isna(r['投入率']) else float(r['投入率']),
                None if pd.isna(r['S_全台系']) else float(r['S_全台系']),
                r['判定ラベル'],
            )
            for _, r in judgment_df.iterrows()
        ]
        con.executemany(
            '''
            INSERT OR IGNORE INTO machine_judgment_log
                (実行日時, ホール名, 日付, 機種名, 台数, 期待高設定台数, zスコア, p値, 投入率, S_全台系, 判定ラベル)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            rows,
        )
        con.commit()

        after = con.execute('SELECT COUNT(*) FROM machine_judgment_log').fetchone()[0]
        return after - before
    finally:
        con.close()


_CREATE_SCORE_SNAPSHOT_SQL = f'''
    CREATE TABLE IF NOT EXISTS score_snapshot (
        実行日時         TEXT NOT NULL,
        使用データ最終日 TEXT NOT NULL,
        ホール名         TEXT NOT NULL,
        {', '.join(f'"{c}" REAL' for c in SUB_SCORES)},
        狙い目度_店舗平均 REAL,
        有効重みJSON      TEXT,
        PRIMARY KEY (ホール名, 使用データ最終日)
    )
'''


def write_score_snapshot(
    db_path: str,
    hole_name: str,
    last_date: str,
    sub_score_means: dict[str, float | None],
    target_mean: float | None,
    effective_weights: dict[str, float],
) -> None:
    """
    [2026-07 タスク5] wᵢ(サブスコア重み)学習用の日次スナップショットをappend-only蓄積する。

    粒度は(ホール名, 使用データ最終日)で1行。値は「使用データ最終日の日次断面」の
    店舗平均(全期間平均ではない。教師=翌日の店舗実測差枚率に対応する説明変数にする
    ため、run_for_holeがsynthesize直後・最終日の行のみで計算して渡す)。
    合成前のサブスコア別の値と実効重み(weights×reliabilities)を両方残す
    (狙い目度だけでは重みを変えた再現学習ができないため)。

    学習側(Spearman直接最大化・店舗別ゲート等)は蓄積後の別タスクで、本関数は記録のみ。
    タスク3で S_据え置き が符号付き日次値に変わった後の値であることに注意
    (店舗の癖としての解釈はstore_profileの遷移列を参照)。

    [重複ガード] (ホール名, 使用データ最終日)が既存であればスキップする
    (write_prediction_logと同型。fase4のcatchup/リトライで二重記録されないための防波堤)。
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute(_CREATE_SCORE_SNAPSHOT_SQL)
        found = con.execute(
            'SELECT 1 FROM score_snapshot WHERE ホール名 = ? AND 使用データ最終日 = ? LIMIT 1',
            (hole_name, last_date),
        ).fetchone()
        if found:
            print(
                f'  [write_score_snapshot] 既存のスナップショットと重複するためスキップ: '
                f'{hole_name} / {last_date}'
            )
            return

        now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
        cols = ['実行日時', '使用データ最終日', 'ホール名', *SUB_SCORES, '狙い目度_店舗平均', '有効重みJSON']
        col_list = ', '.join(f'"{c}"' for c in cols)
        placeholders = ', '.join('?' * len(cols))
        values = (
            now, last_date, hole_name,
            *[sub_score_means.get(c) for c in SUB_SCORES],
            target_mean,
            json.dumps(effective_weights, ensure_ascii=False),
        )
        con.execute(f'INSERT INTO score_snapshot ({col_list}) VALUES ({placeholders})', values)
        con.commit()
    finally:
        con.close()


def _ensure_store_profile_schema(con: sqlite3.Connection) -> None:
    """
    [店舗高設定上限モデル Step1] 上限キャリブレーション値・上限信頼度カラムを追加する
    マイグレーション(既存DBはCREATE TABLE IF NOT EXISTSでは列が増えないため、
    teppan_conditionsと同様PRAGMA table_infoで存在確認してからALTER TABLEする)。

    この2カラムは店舗単位のスカラー値であり、gamma_storeと同じ扱いで
    (ホール名, パターン)の全行に同じ値を複製して持たせる
    (store_profileの既存の縦持ち構造を変えないため)。
    """
    con.execute('''
        CREATE TABLE IF NOT EXISTS store_profile (
            ホール名    TEXT,
            パターン   TEXT,
            スコア     REAL,
            信頼度     REAL,
            gamma_store REAL,
            更新日時   TEXT,
            PRIMARY KEY (ホール名, パターン)
        )
    ''')
    cols = [row[1] for row in con.execute('PRAGMA table_info(store_profile)').fetchall()]
    if '上限キャリブレーション値' not in cols:
        con.execute('ALTER TABLE store_profile ADD COLUMN 上限キャリブレーション値 REAL')
    if '上限信頼度' not in cols:
        con.execute('ALTER TABLE store_profile ADD COLUMN 上限信頼度 REAL')
    # [2026-07 タスク3追記(c)] 店舗の癖(据え/上げ/下げ)を遷移行列(長期版)から保存する4列。
    # gamma_store等と同じ「店舗スカラーを全行に複製」方式(縦持ち構造は変えない)。
    for col in ['遷移_ベース率', '遷移_p_stay', '遷移_p_up']:
        if col not in cols:
            con.execute(f'ALTER TABLE store_profile ADD COLUMN "{col}" REAL')
    if '遷移_ペア数' not in cols:
        con.execute('ALTER TABLE store_profile ADD COLUMN 遷移_ペア数 INTEGER')


def update_store_profile(
    db_path: str,
    hole_name: str,
    df_scored: pd.DataFrame,
    gamma_store: float | None = None,
    uplimit_value: float | None = None,
    uplimit_reliability: float | None = None,
    transition_matrix_long: dict | None = None,
) -> None:
    """
    store_profile テーブルを最新サブスコア・信頼度で上書き更新する。
    gamma_store は multi_store.py で学習された値(未学習時はNone)。
    uplimit_value/uplimit_reliability は compute_uplimit() の出力
    (店舗高設定上限モデル Step1。未計算時はNone)。
    transition_matrix_long は patterns.estimate_transition_matrix()の戻り値(長期版、
    キー: pi, p_stay, p_up, n_pairs)。ペア数不足でNoneの店舗は遷移4列ともNULLになる
    (2026-07 タスク3追記(c)。店舗の癖(据え/上げ/下げ)を記録。表示UIは別タスク)。

    テーブル構造:
      ホール名 TEXT, パターン TEXT, スコア REAL,
      信頼度 REAL, gamma_store REAL,
      上限キャリブレーション値 REAL, 上限信頼度 REAL,
      遷移_ベース率 REAL, 遷移_p_stay REAL, 遷移_p_up REAL, 遷移_ペア数 INTEGER,
      更新日時 TEXT
      PRIMARY KEY (ホール名, パターン)
    """
    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    m = transition_matrix_long or {}
    trans_pi = m.get('pi')
    trans_p_stay = m.get('p_stay')
    trans_p_up = m.get('p_up')
    trans_n_pairs = m.get('n_pairs')
    rows = []

    for pattern, score_col in _PATTERN_MAP.items():
        if score_col not in df_scored.columns:
            continue
        score_mean = df_scored[score_col].dropna().mean()
        score_val = None if pd.isna(score_mean) else float(score_mean)
        reliability = compute_reliability(df_scored, score_col)
        rows.append((
            hole_name, pattern, score_val, reliability, gamma_store,
            uplimit_value, uplimit_reliability,
            trans_pi, trans_p_stay, trans_p_up, trans_n_pairs,
            now,
        ))

    if not rows:
        return

    con = sqlite3.connect(db_path)
    try:
        _ensure_store_profile_schema(con)
        con.executemany(
            '''
            INSERT OR REPLACE INTO store_profile
                (ホール名, パターン, スコア, 信頼度, gamma_store,
                 上限キャリブレーション値, 上限信頼度,
                 遷移_ベース率, 遷移_p_stay, 遷移_p_up, 遷移_ペア数, 更新日時)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            rows,
        )
        con.commit()
    finally:
        con.close()
