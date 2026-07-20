"""
patterns_common.py — パターン検出層の共有基盤(2026-07-19にpatterns.pyから分割)

共有定数・周期探索(ACF/PDM/Lomb-Scargle)・カレンダー49候補・BH補正・効果量・
カレンダー検定(calendar_test)・αブレンド原始関数(blend/blend_scalar/FIXED_ALPHA)。
他のpatterns_*モジュールはここからimportする(逆方向のimportは循環になるため禁止)。
利用側は従来どおり `import patterns as pt`(facade)を使うこと。
"""
import pandas as pd
import numpy as np
from scipy import stats
from scipy.optimize import minimize_scalar
from scipy.signal import lombscargle

SHORT_WINDOW = 14               # S_新台増台・S_移動台の短期トレンドウィンドウ幅N
FDR_ALPHA = 0.05                # FDR補正の有意水準
EFFECT_SIZE_THRESHOLD = 0.3     # rank-biserial相関等の効果量下限(calendar_test用)
GINI_THRESHOLD = 0.1            # S_ローテ Gini係数下限(台数が多いと値が小さくなるため低め)
ACF_MAX_LAG = 60                # ACFスクリーニングの最大lag(日)
INVALID_RATE_THRESHOLD = 0.5    # Lomb-Scargle切り替え判定不能率閾値
SHORT_WINDOW_DEFAULT = 30       # αブレンド短期版のウィンドウ幅M

BLENDABLE_SCORES = ['S_全台系', 'S_鉄板台', 'S_ローテ', 'S_据え置き']

GROUP_CALENDAR_MIN_DAYS = 5  # 候補日・対照日ともにこの日数未満の組み合わせは検定対象外


def acf_screen(series: pd.Series, max_lag: int = ACF_MAX_LAG) -> list[int]:
    """
    pairwise-complete ACF で有意なlagを返す。
    欠損ペアは除外して計算する。
    """
    x = series.values.astype(float)
    n = len(x)
    significant: list[int] = []
    for lag in range(1, min(max_lag + 1, n)):
        x1 = x[:-lag]
        x2 = x[lag:]
        valid = ~(np.isnan(x1) | np.isnan(x2))
        n_v = int(valid.sum())
        if n_v < 10:
            continue
        v1, v2 = x1[valid], x2[valid]
        if np.std(v1) == 0 or np.std(v2) == 0:
            continue
        try:
            r, _ = stats.pearsonr(v1, v2)
        except Exception:
            continue
        if abs(r) > 2.0 / np.sqrt(n_v):
            significant.append(lag)
    return significant


def pdm_confirm(series: pd.Series, candidate_lags: list[int]) -> dict:
    """
    PDM(Phase Dispersion Minimization)で候補lagを確認する。
    波形を仮定しない手法のため、矩形的なパターンにも対応。
    theta = 平均ビン内分散 / 全体分散。theta < 0.7 を周期確認とみなす。
    """
    if not candidate_lags:
        return {}
    x = series.values.astype(float)
    times = np.arange(len(x))
    valid = ~np.isnan(x)
    t_v = times[valid]
    v_v = x[valid]
    if len(v_v) < 10:
        return {}
    total_var = float(np.var(v_v, ddof=1))
    if total_var == 0.0:
        return {lag: {'theta': 1.0, 'confirmed': False} for lag in candidate_lags}
    n_bins = 5
    results: dict = {}
    for lag in candidate_lags:
        phases = (t_v % lag) / lag
        bin_idx = (phases * n_bins).astype(int) % n_bins
        bin_vars = [
            float(np.var(v_v[bin_idx == b], ddof=1))
            for b in range(n_bins)
            if (bin_idx == b).sum() > 1
        ]
        theta = float(np.mean(bin_vars) / total_var) if bin_vars else 1.0
        results[lag] = {'theta': theta, 'confirmed': theta < 0.7}
    return results


def lomb_scargle_screen(series: pd.Series, timestamps: pd.Series) -> list[float]:
    """
    判定不能率が INVALID_RATE_THRESHOLD 超の台に使用する。
    等間隔サンプリング前提が崩れているため通常ACFの代替。
    有意パワー(>0.4)を持つ周期(日数)のリストを返す。
    """
    valid = ~(series.isna() | timestamps.isna())
    t = timestamps[valid].astype(float).values
    y = series[valid].astype(float).values
    if len(t) < 10:
        return []
    y = y - y.mean()
    if float(np.std(y)) == 0.0:
        return []
    periods = np.arange(2, ACF_MAX_LAG + 1, dtype=float)
    freqs = 2.0 * np.pi / periods
    try:
        pgram = lombscargle(t, y, freqs, normalize=True)
    except Exception:
        return []
    return [float(periods[i]) for i in range(len(periods)) if pgram[i] > 0.4]


_WEEKDAY_NAMES = ['月', '火', '水', '木', '金', '土', '日']


def calendar_candidates(dt: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """
    既知カレンダー49候補(曜日7 + 日付末尾10 + ゾロ目1 + 毎月X日31)の日付マスクを返す。
    calendar_test(鉄板台検定)と末尾版レイヤー2(group_calendar_conditions)で共用する。

    [2026-07-10 今後の実装予定.md 1.8節「末尾版」] 毎月X日31候補を追加(旧18候補から拡張。
    18候補時代の設計メモに残っていた「17候補」表記は数え間違い)。これにより鉄板台の
    カレンダー検定結果も従来と変わり得る(仮説数増によるBH検出力の微減はユーザー許容済み)。
    毎月X日は月1回しか該当しないため、月末寄りの日(29〜31日)ほど該当日数が少なく
    検定不能(候補/対照いずれかが最低日数未満)になりやすい点に注意。
    """
    candidates: dict[str, np.ndarray] = {}
    for i, name in enumerate(_WEEKDAY_NAMES):
        candidates[f'曜日_{name}'] = (dt.dayofweek == i)
    for d in range(10):
        candidates[f'末尾_{d}'] = (dt.day % 10 == d)
    candidates['ゾロ目'] = np.isin(dt.day, [11, 22])
    for d in range(1, 32):
        candidates[f'毎月_{d}日'] = (dt.day == d)
    return candidates


def _wilcoxon_rank_biserial(diffs: np.ndarray) -> float:
    """
    対応ありWilcoxon符号順位検定のrank-biserial相関(-1〜1)。
    r = (正の差のランク和 − 負の差のランク和) / (両者の合計)。match_rule_testで使う
    (calendar_test/group_calendar_testのrank-biserial相関と同じ「符号付き効果量」の
    物差しに揃えるため、scipy.stats.wilcoxonの内部統計量には依存せず自前で計算する)。
    差が0のペアはランク付けから除外する(scipyのzero_method='wilcox'相当)。
    """
    nonzero = diffs[diffs != 0]
    if len(nonzero) == 0:
        return 0.0
    ranks = stats.rankdata(np.abs(nonzero))
    w_pos = float(ranks[nonzero > 0].sum())
    w_neg = float(ranks[nonzero < 0].sum())
    total = w_pos + w_neg
    return (w_pos - w_neg) / total if total > 0 else 0.0


def calendar_test(
    series: pd.Series,
    dates: pd.Series,
    check_missing_bias_fn,
) -> dict:
    """
    既知カレンダー49候補(曜日7 + 日付末尾10 + ゾロ目1 + 毎月X日31。calendar_candidates参照)の検定。
    一方向検定(並べ替え or Mann-Whitney U) + FDR補正 + 効果量ゲート。
    check_missing_bias_fn: preprocess.check_missing_bias を渡す。

    Returns:
        {候補名: {'p_raw': float, 'effect_size': float, 'significant': bool}}
        ※ p_raw はBH補正前の生p値(補正は significant フラグにのみ反映)。
          旧キー名 p_adj は「補正済み」と誤解を招くため2026-07に改名。
    """
    dt = pd.to_datetime(dates.values, errors='coerce')
    mini_df = pd.DataFrame({'is_invalid': series.isna().values}, index=series.index)

    candidates = calendar_candidates(dt)

    names_list = list(candidates.keys())
    p_values: list[float] = []
    effect_sizes: list[float] = []
    bias_skips: list[bool] = []

    for name in names_list:
        mask_arr = candidates[name]
        mask_s = pd.Series(mask_arr, index=series.index)
        bias = check_missing_bias_fn(mini_df, mask_s)
        if bias['skip_test']:
            p_values.append(1.0)
            effect_sizes.append(0.0)
            bias_skips.append(True)
            continue
        bias_skips.append(False)
        valid_s = series.dropna()
        valid_mask = mask_s.reindex(valid_s.index).fillna(False)
        grp_c = valid_s[valid_mask]
        grp_ctrl = valid_s[~valid_mask]
        if len(grp_c) < 5 or len(grp_ctrl) < 5:
            p_values.append(1.0)
            effect_sizes.append(0.0)
            continue
        _, p = stats.mannwhitneyu(grp_c, grp_ctrl, alternative='greater')
        n1, n2 = len(grp_c), len(grp_ctrl)
        u_stat, _ = stats.mannwhitneyu(grp_c, grp_ctrl, alternative='two-sided')
        # u_stat は grp_c 側のU統計量なので、grp_c > grp_ctrl (=p値の検定方向)ほど
        # rbc が正になるようにする(符号を反転すると有意なパターンが常にeffect_sizeゲートで弾かれる)
        rbc = float(2.0 * u_stat / (n1 * n2) - 1.0)
        p_values.append(float(p))
        effect_sizes.append(rbc)

    significant_flags = benjamini_hochberg(p_values)
    results: dict = {}
    for i, name in enumerate(names_list):
        results[name] = {
            'p_raw': p_values[i],
            'effect_size': effect_sizes[i],
            'significant': (bool(significant_flags[i])
                            and effect_sizes[i] >= EFFECT_SIZE_THRESHOLD
                            and not bias_skips[i]),
        }
    return results


def benjamini_hochberg(p_values: list[float], alpha: float = FDR_ALPHA) -> list[bool]:
    """Benjamini-Hochberg FDR補正を適用し、各p値が有意かどうかを返す。"""
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: p_values[i])
    last_reject = -1
    for rank_minus1, orig_i in enumerate(order):
        if p_values[orig_i] <= (rank_minus1 + 1) / n * alpha:
            last_reject = rank_minus1
    reject = [False] * n
    for rank_minus1 in range(last_reject + 1):
        reject[order[rank_minus1]] = True
    return reject


def walk_forward_alpha(
    long_scores: pd.Series,
    short_scores: pd.Series,
    target: pd.Series,
    min_train_size: int = 60,
) -> float:
    """
    ウォークフォワード検証でαを学習する。
    target: 差枚 or Stage3スコアの時系列。サンプル不足時は 0.0 を返す。
    """
    df_wf = pd.concat([long_scores, short_scores, target], axis=1)
    df_wf.columns = ['long', 'short', 'target']
    df_wf = df_wf.sort_index().dropna(subset=['long', 'target'])

    n = len(df_wf)
    if n < min_train_size + 1:
        return 0.0

    long_v = df_wf['long'].values.astype(float)
    short_v = df_wf['short'].values.astype(float)
    tgt_v = df_wf['target'].values.astype(float)

    def eval_alpha(alpha: float) -> float:
        total_se = 0.0
        count = 0
        for t in range(min_train_size, n):
            s, l = short_v[t], long_v[t]
            blended = (alpha * s + (1.0 - alpha) * l) if not np.isnan(s) else l
            total_se += (blended - tgt_v[t]) ** 2
            count += 1
        return total_se / count if count > 0 else np.inf

    res = minimize_scalar(eval_alpha, bounds=(0.0, 1.0), method='bounded')
    return float(np.clip(res.x, 0.0, 1.0))


def blend(
    long_score: pd.Series,
    short_score: pd.Series,
    alpha: float,
) -> pd.Series:
    """
    ブレンド済みサブスコア = α×short + (1-α)×long。
    short_score が NaN(サンプル不足)の行はα=0として長期版を使用。
    """
    result = long_score.copy().astype(float)
    has_both = short_score.notna() & long_score.notna()
    result[has_both] = (
        alpha * short_score[has_both] + (1.0 - alpha) * long_score[has_both]
    )
    return result


def blend_scalar(long_value: float, short_value: float | None, alpha: float) -> float:
    """
    blend()と同じ数式(α×short+(1-α)×long)を単一のスカラー値に適用する版。

    blend()は台×日の行単位Series専用のため、店舗×日 高設定上限キャリブレーション
    (候補C。score.compute_uplimit)のように「長期分位点1本・短期分位点1本」という
    集計値どうしをブレンドする用途にはそのまま使えない。同じ数式を再利用するための
    スカラー版として新設(候補C・Step1。詳細はデータ分析_skill.md参照)。

    short_valueがNone(短期側サンプル不足)の場合はlong_valueをそのまま返す
    (blend()の「short=NaNの行はα=0扱い」という規約と同じ)。
    """
    if short_value is None or (isinstance(short_value, float) and np.isnan(short_value)):
        return float(long_value)
    return float(alpha * short_value + (1.0 - alpha) * long_value)


FIXED_ALPHA = 0.3  # 暫定固定値(短期3:長期7)。ウォークフォワードα学習は停止中(下記docstring参照)


def learn_all_alphas(
    df: pd.DataFrame,
    hole_name: str,
    scores: list[str] = BLENDABLE_SCORES,
) -> dict:
    """
    [2026-07 仕様変更] 固定α(FIXED_ALPHA)を返す。

    旧実装のウォークフォワード学習は停止した。理由:
    - ターゲット(当日の店舗平均high_prob)に対し、特徴量のS_全台系等は
      まさにその当日のhigh_probから計算されており、同日情報のリークで
      実質自己回帰になっていた(αが「未来の予測に効く比率」を表さない)
    - compute_short_term_scoreは末尾M日窓を1回計算して貼るだけで、
      各時点tにおける短期版になっておらず、α推定の実効サンプルが末尾のみだった
    真のウォークフォワード(特徴=t時点までで計算、ターゲット=t+1の実測差枚)は
    Stage7(予測精度の自己検証ループ)として機能B再設計とあわせて再実装する。
    walk_forward_alpha関数はその際の再利用に備えて残置(現在未使用)。

    Returns: {スコア名: FIXED_ALPHA}
    """
    return {score_col: FIXED_ALPHA for score_col in scores}
