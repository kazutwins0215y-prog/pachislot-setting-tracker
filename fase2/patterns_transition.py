"""
patterns_transition.py — 遷移モデル(据え置き/上げ/下げ)による全台翌日予測・予測ブレンド
(2026-07-19にpatterns.pyから分割。利用側は `import patterns as pt` のfacade経由を推奨)
"""
import pandas as pd
import numpy as np

from patterns_common import FIXED_ALPHA

# ── [Stage7-3] 遷移モデル(据え置き/上げ/下げ)による全台翌日予測 ──────────────

TRANSITION_MIN_PAIRS = 50  # 遷移確率の推定に必要な最低の連続日ペア数(暫定値)

# [2026-07 タスク4] 前日差枚条件付き層別の暫定パラメータ(実データで調整前提)。
STRAT_QUANTILE = 0.8              # 前日差枚が店舗内でこの分位点以上なら上位層
STRAT_PERMUTATION_ITERS = 1000    # 層間差の並べ替え検定の反復回数
STRAT_SIGNIFICANCE_ALPHA = 0.05   # 並べ替え検定の有意水準


def _build_transition_pairs(df: pd.DataFrame, hole_name: str) -> pd.DataFrame:
    """
    店舗単位で暦日差1日・両日ともhigh_prob判定可能な連続ペアを構築する共通処理。
    estimate_transition_matrix(無条件版)・estimate_transition_matrix_stratified
    (前日差枚条件付き版)の両方から使う(2026-07 タスク4で関数抽出、二重実装を避ける)。

    暦日差が1日でないペア(休業・欠測・取得漏れ)はk日遷移が混ざるため除外する。

    Returns: DataFrame(列: 機種名, 台番号, p_prev, p_curr, 日付_prev, 差枚_prev)
        差枚_prev: 前日(t-1)の実測差枚(差枚列が無い場合は全てNaN。層別の可否判定に使う)。
        ペアが1件もなければ空DataFrame。
    """
    empty = pd.DataFrame(columns=['機種名', '台番号', 'p_prev', 'p_curr', '日付_prev', '差枚_prev'])

    d = df[df['ホール名'] == hole_name]
    if 'is_invalid' in d.columns:
        d = d[~d['is_invalid'].fillna(True)]
    d = d.dropna(subset=['high_prob'])
    if d.empty:
        return empty

    d = d.sort_values(['機種名', '台番号', '日付'])
    g = d.groupby(['機種名', '台番号'], sort=False)
    prev_p = g['high_prob'].shift(1)
    prev_date = g['日付'].shift(1)
    day_gap = (pd.to_datetime(d['日付']) - pd.to_datetime(prev_date)).dt.days
    mask = (day_gap == 1) & prev_p.notna()
    if not bool(mask.any()):
        return empty

    prev_diff = g['差枚'].shift(1) if '差枚' in d.columns else pd.Series(np.nan, index=d.index)

    return pd.DataFrame({
        '機種名': d.loc[mask, '機種名'].to_numpy(),
        '台番号': d.loc[mask, '台番号'].to_numpy(),
        'p_prev': prev_p[mask].to_numpy(),
        'p_curr': d.loc[mask, 'high_prob'].to_numpy(),
        '日付_prev': prev_date[mask].to_numpy(),
        '差枚_prev': prev_diff[mask].to_numpy(),
    })


def _fit_transition_from_pairs(p_prev: np.ndarray, p_curr: np.ndarray) -> dict | None:
    """
    p_prev/p_curr配列(ソフトカウント)からp_stay/p_up/piを推定する共通処理。
    estimate_transition_matrix・estimate_transition_matrix_stratified(層ごと)で共用。
    ペア数がTRANSITION_MIN_PAIRS未満、または分母が0の場合はNone(=予測不可)。
    """
    n_pairs = len(p_prev)
    if n_pairs < TRANSITION_MIN_PAIRS:
        return None

    denom_hi = float(p_prev.sum())
    denom_lo = float((1.0 - p_prev).sum())
    if denom_hi <= 0 or denom_lo <= 0:
        return None

    eps = 1e-6  # 0/1に張り付くと予測が定数化するため内側にクリップ
    p_stay = float(np.clip((p_prev * p_curr).sum() / denom_hi, eps, 1.0 - eps))
    p_up = float(np.clip(((1.0 - p_prev) * p_curr).sum() / denom_lo, eps, 1.0 - eps))
    # [2026-07 タスク3追記(c)] ベース率pi = ペア集合のp_prev平均(ソフトカウントと同じ
    # 集合で定義)。store_profileの店舗の癖(据え/上げ/下げ)保存で使う(データ分析_skill.md参照)。
    pi = float(np.clip(p_prev.mean(), eps, 1.0 - eps))
    return {'p_stay': p_stay, 'p_up': p_up, 'n_pairs': n_pairs, 'pi': pi}


def estimate_transition_matrix(df: pd.DataFrame, hole_name: str) -> dict | None:
    """
    店舗単位で設定の日次遷移確率を推定する(v1: 無条件版)。

    ホールの設定運用は「据え置き」だけでなく「上げ」「下げ」を含むため、
    翌日予測の事前分布は単純な減衰priorではなく2状態(高/低)マルコフ遷移として持つ:
      p_stay = P(高_t | 高_{t-1})  … 据え置き率(1 - p_stay が下げ率)
      p_up   = P(高_t | 低_{t-1})  … 上げ率

    真の設定ラベルは観測できないため、連続した暦日ペア(同一ホール×機種×台番号、
    両日とも判定可能)の事後確率high_probをソフトカウントとして使う:
      p_stay = Σ p_{t-1}·p_t / Σ p_{t-1}
      p_up   = Σ (1-p_{t-1})·p_t / Σ (1-p_{t-1})
    事後確率のノイズにより真の遷移より平滑化(持続性の過小評価)側に偏る既知のバイアスが
    あるが、v1の推定量として許容する(条件付き拡張はestimate_transition_matrix_stratified、
    バイアス補正は今後の実装予定.md参照)。

    ペア構築は_build_transition_pairsに委譲(2026-07 タスク4)。
    ペア数がTRANSITION_MIN_PAIRS未満の場合はNone(=予測不可。虚構の値を作らない)。
    """
    pairs = _build_transition_pairs(df, hole_name)
    if pairs.empty:
        return None
    return _fit_transition_from_pairs(
        pairs['p_prev'].to_numpy(dtype=float), pairs['p_curr'].to_numpy(dtype=float)
    )


def stratify_threshold_by_date(
    df: pd.DataFrame, hole_name: str, quantile: float = STRAT_QUANTILE,
) -> pd.Series:
    """
    店舗×日ごとの実測差枚の分位点(閾値)を返す(index=日付、is_invalid行は除外)。
    estimate_transition_matrix_stratified(層分けの基準)と
    run_store_profile._run_transition_predictions(当日の層判定)の両方から使う
    共通ヘルパー(2026-07 タスク4)。差枚列が無い店舗は空Seriesを返す。
    """
    sub = df[df['ホール名'] == hole_name]
    if 'is_invalid' in sub.columns:
        sub = sub[~sub['is_invalid'].fillna(True)]
    if '差枚' not in sub.columns:
        return pd.Series(dtype=float)
    sub = sub.dropna(subset=['差枚'])
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.groupby('日付')['差枚'].quantile(quantile)


def _stratified_permutation_test(
    p_prev: np.ndarray,
    p_curr: np.ndarray,
    is_top: np.ndarray,
    n_iter: int = STRAT_PERMUTATION_ITERS,
    seed: int = 42,
) -> float:
    """
    層ラベル(is_top)をシャッフルした帰無分布と観測差を比較する並べ替え検定
    (2026-07 タスク4)。ソフトカウント由来のp_stay/p_upは通常の比率検定(z検定等)が
    前提とする独立二項分布に従わないため、ラベルシャッフルによるノンパラ検定を採用
    (score_rotationの並べ替え検定と同じ考え方)。

    統計量 = max(|Δp_stay|, |Δp_up|)。p_stay・p_upのどちらか一方でも層間に有意差が
    あれば層別に意味があるとみなす保守的な基準(暫定。実データで見直す)。
    層の人数(n_top)は固定してシャッフルする(観測値と同じ周辺分布での並べ替え検定)。

    計算コストが問題になる場合はブートストラップ等への変更を検討する(暫定実装)。
    """
    def _stay_up(mask: np.ndarray) -> tuple[float, float]:
        denom_hi = p_prev[mask].sum()
        denom_lo = (1.0 - p_prev[mask]).sum()
        stay = (p_prev[mask] * p_curr[mask]).sum() / denom_hi if denom_hi > 0 else np.nan
        up = ((1.0 - p_prev[mask]) * p_curr[mask]).sum() / denom_lo if denom_lo > 0 else np.nan
        return stay, up

    stay_top, up_top = _stay_up(is_top)
    stay_bot, up_bot = _stay_up(~is_top)
    obs_stat = max(abs(stay_top - stay_bot), abs(up_top - up_bot))

    n = len(is_top)
    n_top = int(is_top.sum())
    rng = np.random.default_rng(seed)
    count_ge = 0
    for _ in range(n_iter):
        perm_mask = np.zeros(n, dtype=bool)
        perm_mask[rng.choice(n, size=n_top, replace=False)] = True
        s_stay_t, s_up_t = _stay_up(perm_mask)
        s_stay_b, s_up_b = _stay_up(~perm_mask)
        stat = max(abs(s_stay_t - s_stay_b), abs(s_up_t - s_up_b))
        if stat >= obs_stat:  # NaN同士の比較はFalseになるため自動的にスキップされる
            count_ge += 1
    return count_ge / n_iter


def estimate_transition_matrix_stratified(df: pd.DataFrame, hole_name: str) -> dict | None:
    """
    [2026-07 タスク4] 前日(t-1)の実測差枚による条件付き遷移行列(層別版)。

    「出た台は翌日出ない」逆信号(エスパス日拓新宿歌舞伎町店で-0.021、p=2.6e-14確認済み)を
    層別遷移行列で拾うための拡張。ペア構築はestimate_transition_matrixと共通
    (_build_transition_pairs)。

    層の定義: ペア(t-1→t)の前日(t-1)の実測差枚が、その店舗×その日(t-1)の
    上位STRAT_QUANTILE(暫定0.8)分位点以上か否かの2層(閾値は店舗内の日次分位点のため
    店舗規模に自動適応する。ロジスティック変調は不採用と決定済み)。

    層ごとにTRANSITION_MIN_PAIRS未満ならNone(層別不可)。層間のp_stay/p_up差を
    _stratified_permutation_testで検定し、'有意'キーに有意性フラグ(有意水準
    STRAT_SIGNIFICANCE_ALPHA)を入れて返す。呼び出し側(run_store_profile.
    _run_transition_predictions)は'有意'がTrueの店舗のみ条件付き版を並走記録する。

    Returns:
        {'上位層': {p_stay,p_up,n_pairs,pi}, '下位層': {...},
         '分位閾値': float, '検定p値': float, '有意': bool}
        または None(差枚列が無い/層別不可)。
    """
    pairs = _build_transition_pairs(df, hole_name)
    if pairs.empty or bool(pairs['差枚_prev'].isna().all()):
        return None  # 差枚列が無い、または全欠損 → 層別不可

    thresholds = stratify_threshold_by_date(df, hole_name)
    if thresholds.empty:
        return None

    pairs = pairs.copy()
    pairs['閾値'] = pairs['日付_prev'].map(thresholds)
    pairs = pairs.dropna(subset=['差枚_prev', '閾値'])
    if pairs.empty:
        return None

    p_prev = pairs['p_prev'].to_numpy(dtype=float)
    p_curr = pairs['p_curr'].to_numpy(dtype=float)
    is_top = pairs['差枚_prev'].to_numpy(dtype=float) >= pairs['閾値'].to_numpy(dtype=float)

    mat_top = _fit_transition_from_pairs(p_prev[is_top], p_curr[is_top])
    mat_bottom = _fit_transition_from_pairs(p_prev[~is_top], p_curr[~is_top])
    if mat_top is None or mat_bottom is None:
        return None  # どちらかの層がTRANSITION_MIN_PAIRS未満 → 層別不可

    p_value = _stratified_permutation_test(p_prev, p_curr, is_top)

    return {
        '上位層': mat_top,
        '下位層': mat_bottom,
        '分位閾値': STRAT_QUANTILE,
        '検定p値': p_value,
        '有意': bool(p_value < STRAT_SIGNIFICANCE_ALPHA),
    }


def predict_transition_next_day(p_today: float, matrix: dict) -> float:
    """
    当日の事後確率と遷移行列から翌日の高設定事前確率を返す。
    P(高_翌日) = p_today·P(高→高) + (1-p_today)·P(低→高)
    """
    return p_today * matrix['p_stay'] + (1.0 - p_today) * matrix['p_up']


def predict_transition_with_blend(
    p_today: float,
    matrix_long: dict | None,
    matrix_short: dict | None,
    alpha: float = None,
) -> dict:
    """
    長期版(全履歴で推定した遷移行列)・短期版(直近M日窓)の両予測を
    predict_next_day_with_blendと同じ規約でブレンドする(短期不可=alpha実質0)。

    Returns: {'長期スコア', '短期スコア', 'ブレンド値', '使用alpha'}(すべて計算不可ならNone)
    """
    if alpha is None:
        alpha = FIXED_ALPHA

    long_pred = predict_transition_next_day(p_today, matrix_long) if matrix_long else None
    short_pred = predict_transition_next_day(p_today, matrix_short) if matrix_short else None

    if long_pred is None and short_pred is None:
        return {'長期スコア': None, '短期スコア': None, 'ブレンド値': None, '使用alpha': None}
    if long_pred is None:
        return {'長期スコア': None, '短期スコア': short_pred, 'ブレンド値': short_pred, '使用alpha': 1.0}
    if short_pred is None:
        return {'長期スコア': long_pred, '短期スコア': None, 'ブレンド値': long_pred, '使用alpha': 0.0}

    blended = alpha * short_pred + (1.0 - alpha) * long_pred
    return {'長期スコア': long_pred, '短期スコア': short_pred, 'ブレンド値': blended, '使用alpha': alpha}


def predict_sueki_with_blend(
    r_long: float | None,
    r_short: float | None,
    deviation: float,
    alpha: float = None,
) -> dict:
    """
    [2026-07 タスク3] S_据え置きの翌日投影 = r̄_t ×(当日high_probの台基準からの偏差)を
    長期版(全履歴のr̄_t)・短期版(直近SHORT_WINDOW_DEFAULT日窓のr̄_t)でブレンドする。
    r_long/r_shortはsueki_daily_rの最終日値(NaNなら計算不可としてNoneで渡す)。
    predict_next_day_with_blend/predict_transition_with_blendと同じブレンド規約
    (短期不可=alpha実質0)。deviationは長期/短期で共通(同一日の値のため)。
    """
    if alpha is None:
        alpha = FIXED_ALPHA

    long_pred = r_long * deviation if r_long is not None else None
    short_pred = r_short * deviation if r_short is not None else None

    if long_pred is None and short_pred is None:
        return {'長期スコア': None, '短期スコア': None, 'ブレンド値': None, '使用alpha': None}
    if long_pred is None:
        return {'長期スコア': None, '短期スコア': short_pred, 'ブレンド値': short_pred, '使用alpha': 1.0}
    if short_pred is None:
        return {'長期スコア': long_pred, '短期スコア': None, 'ブレンド値': long_pred, '使用alpha': 0.0}

    blended = alpha * short_pred + (1.0 - alpha) * long_pred
    return {'長期スコア': long_pred, '短期スコア': short_pred, 'ブレンド値': blended, '使用alpha': alpha}

