"""
patterns_depth.py — 深さ型スコア(S_鉄板台/S_ローテ/S_据え置き)・鉄板台翌日予測・短期版計算
(2026-07-19にpatterns.pyから分割。利用側は `import patterns as pt` のfacade経由を推奨)
"""
import pandas as pd
import numpy as np

from patterns_common import (
    FDR_ALPHA,
    FIXED_ALPHA,
    GINI_THRESHOLD,
    INVALID_RATE_THRESHOLD,
    SHORT_WINDOW_DEFAULT,
    acf_screen,
    calendar_candidates,
    calendar_test,
    lomb_scargle_screen,
    pdm_confirm,
)
from patterns_breadth import score_zentaiki

TEPPAN_PHASE_BINS = 5  # pdm_confirmと同じ位相ビン数

# [2026-07 機能B再設計] 鉄板台の「非該当日」に与える負スコアの縮小率。暫定値であり、
# 実データ運用後に的中率(prediction_accuracy)を見ながら調整する前提(Phase6)。
NEGATIVE_SCALE = 0.5


def _phase_bin_effects(hp: pd.Series, lag: int, n_bins: int = TEPPAN_PHASE_BINS) -> dict[int, float]:
    """
    確認済み周期lagについて、位相ビンごとの平均high_probが全体平均を上回るビン(該当ビン)の
    効果量((ビン平均−全体平均)/0.5、0〜1)を返す。該当ビンが1つもなければ空dict
    (=この周期では検出なし)。_phase_day_scores(過去向け)とpredict_next_day(翌日投影)の
    両方から共有される検出ロジック本体(二重実装を避けるため分離)。

    ※ 位相は「観測順インデックス」基準(既存のACF/PDMと同じ近似)。
      営業日が飛ぶと暦日とはズレる点に注意。
    """
    x = hp.values.astype(float)
    n = len(x)
    valid = ~np.isnan(x)
    if int(valid.sum()) < 10:
        return {}
    overall = float(np.nanmean(x))
    t = np.arange(n)
    phases = (t % lag) / lag
    bins = (phases * n_bins).astype(int) % n_bins

    positive_bins: dict[int, float] = {}
    for b in range(n_bins):
        m = (bins == b) & valid
        if int(m.sum()) < 2:
            continue
        diff = float(np.mean(x[m])) - overall
        if diff > 0:
            positive_bins[b] = min(1.0, diff / 0.5)
    return positive_bins


def _phase_day_scores(hp: pd.Series, lag: int, n_bins: int = TEPPAN_PHASE_BINS) -> np.ndarray:
    """
    確認済み周期lagについて、位相ビンごとの平均high_probが全体平均を上回るビン(該当ビン)に
    属する日へ (ビン平均 − 全体平均) / 0.5 のスコア(0〜1)を付与する。
    この周期で検出(該当ビンが1つ以上)がある場合、非該当ビンの日には
    -NEGATIVE_SCALE × 該当ビン効果量の平均 を付与する(弱さの表現)。
    検出自体がない(該当ビンが1つもない)場合は全日0.0のまま。
    0.5の正規化はscore_zentaiki等と同じ規約。
    """
    n = len(hp)
    out = np.zeros(n)
    positive_bins = _phase_bin_effects(hp, lag, n_bins)
    if not positive_bins:
        return out  # この周期では検出なし

    t = np.arange(n)
    phases = (t % lag) / lag
    bins = (phases * n_bins).astype(int) % n_bins
    hot_mask = np.isin(bins, list(positive_bins.keys()))
    for b, effect in positive_bins.items():
        out[bins == b] = effect
    mean_effect = float(np.mean(list(positive_bins.values())))
    out[~hot_mask] = -NEGATIVE_SCALE * mean_effect
    return out


def _project_phase_score(hp: pd.Series, lag: int, n_bins: int = TEPPAN_PHASE_BINS) -> float:
    """
    次の観測点(観測順インデックス = len(hp)、まだ観測していない日)の周期経路予測値を返す。
    該当ビンなら正の効果量、非該当ビンは-NEGATIVE_SCALE×平均効果量、
    この周期自体の検出がなければ0.0(情報なし)。predict_next_dayから呼ばれる。
    """
    positive_bins = _phase_bin_effects(hp, lag, n_bins)
    if not positive_bins:
        return 0.0
    next_t = len(hp)
    phase = (next_t % lag) / lag
    bin_idx = int(phase * n_bins) % n_bins
    if bin_idx in positive_bins:
        return positive_bins[bin_idx]
    return -NEGATIVE_SCALE * float(np.mean(list(positive_bins.values())))


def _combine_signed(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    符号付き2経路(周期・カレンダー)のnoisy-or型統合。0は「その経路からの情報なし」を表す。
    - 両方0: 0(情報なし)
    - 片方のみ0: 非ゼロの側をそのまま採用
    - 両方正: noisy-or 1-(1-a)(1-b)
    - 両方負: 絶対値をnoisy-orして符号を負に戻す -(1-(1-|a|)(1-|b|))
    - 符号が異なる: 単純平均(暫定簡易ルール。実データで調整)
    """
    out = np.zeros_like(a, dtype=float)
    both_pos = (a > 0) & (b > 0)
    both_neg = (a < 0) & (b < 0)
    only_a = (a != 0) & (b == 0)
    only_b = (a == 0) & (b != 0)
    mixed = (a != 0) & (b != 0) & ~both_pos & ~both_neg

    out[both_pos] = 1.0 - (1.0 - a[both_pos]) * (1.0 - b[both_pos])
    out[both_neg] = -(1.0 - (1.0 - np.abs(a[both_neg])) * (1.0 - np.abs(b[both_neg])))
    out[only_a] = a[only_a]
    out[only_b] = b[only_b]
    out[mixed] = (a[mixed] + b[mixed]) / 2.0
    return out


def score_teppandai(
    df: pd.DataFrame,
    machine_name: str,
    unit_col: str = '台番号',
    details_out: list | None = None,
) -> pd.Series:
    """
    S_鉄板台: 2経路統合の鉄板台スコア(-1〜1)。**検出済みの台**のみにスコアを付与する。

    [2026-07 仕様変更] 旧実装は検出台の全日に定数(1.0/0.6)を付与しており、
    「特定条件の日に入る」という鉄板台の性質と逆に、非該当日の狙い目度を
    押し上げて該当日のコントラストを消していた。現仕様:
    - カレンダー経路: 有意候補(例: 末尾7)に合致する日は効果量(rank-biserial、正)、
      検出済みの台の非該当日は -NEGATIVE_SCALE×平均効果量(負、弱さの表現)
    - 周期経路(ACF→PDM / Lomb-Scargle): 確認済み周期の高位相ビンに属する日は
      (ビン平均−全体平均)/0.5(正)、同じ周期の非該当ビンの日は
      -NEGATIVE_SCALE×平均効果量(負)
    - 両経路は符号付きnoisy-or(_combine_signed)で統合
      (両経路が同じ日に正の効果を示すほど高スコア、負の効果を示すほど低スコア、
      符号が割れる日は単純平均)
    - 検出自体がない(どちらの経路も一度も有意でない)台は NaN(synthesizeで除外・再正規化。
      「検出不可」と「弱い」を混同しない — Stage4-1と同じ方針)

    details_out: listを渡すと検出条件(経路/条件/効果量)を台単位で追記する
    (どの条件で有意だったかを機能B等で表示するためのメタデータ)。
    履歴14日未満の台は NaN(検出不可扱い)。
    """
    from preprocess import check_missing_bias  # 循環インポート回避のため局所import

    scores = pd.Series(np.nan, index=df.index)
    mask_machine = df['機種名'] == machine_name
    sub = df[mask_machine]
    if sub.empty:
        return scores

    for (hole, unit), grp in sub.groupby(['ホール名', unit_col], sort=False):
        grp_sorted = grp.sort_values('日付')
        hp = grp_sorted['high_prob'].copy()
        if 'is_invalid' in grp_sorted.columns:
            hp[grp_sorted['is_invalid'].fillna(True).values] = np.nan
        hp = hp.reset_index(drop=True)

        n_total = len(hp)
        if n_total < 14:
            continue
        n_invalid = int(hp.isna().sum())
        invalid_rate_val = n_invalid / n_total

        # ── 周期経路: 確認済み周期ごとに該当日スコア(複数周期は符号付きnoisy-orで統合。
        #    0=情報なしとして扱うためnp.maximumではなく_combine_signedを使う) ──
        main_scores = np.zeros(n_total)
        if invalid_rate_val > INVALID_RATE_THRESHOLD:
            ts = pd.Series(np.arange(n_total, dtype=float))
            for period in lomb_scargle_screen(hp, ts):
                lag = max(2, int(round(period)))
                day_scores = _phase_day_scores(hp, lag)
                if day_scores.max() > 0:
                    main_scores = _combine_signed(main_scores, day_scores)
                    if details_out is not None:
                        details_out.append({
                            'ホール名': hole, '機種名': machine_name, '台番号': int(unit),
                            '経路': '周期(Lomb-Scargle)', '条件': f'周期{lag}日(観測順)',
                            '効果量': round(float(day_scores.max()), 3),
                            '周期日数': lag,
                        })
        else:
            sig_lags = acf_screen(hp)
            if sig_lags:
                pdm_result = pdm_confirm(hp, sig_lags)
                for lag, res in pdm_result.items():
                    if not res['confirmed']:
                        continue
                    day_scores = _phase_day_scores(hp, lag)
                    if day_scores.max() > 0:
                        main_scores = _combine_signed(main_scores, day_scores)
                        if details_out is not None:
                            details_out.append({
                                'ホール名': hole, '機種名': machine_name, '台番号': int(unit),
                                '経路': '周期(ACF+PDM)', '条件': f'周期{lag}日(観測順)',
                                '効果量': round(float(1.0 - res['theta']), 3),
                                '周期日数': lag,
                            })

        # ── カレンダー経路: 有意候補に合致する日は効果量(正)、
        #    検出済みの台の非該当日は-NEGATIVE_SCALE×平均効果量(負) ──
        cal_scores = np.zeros(n_total)
        date_series = pd.Series(grp_sorted['日付'].values)
        cal_results = calendar_test(hp, date_series, check_missing_bias)
        significant_names = [n for n, v in cal_results.items() if v['significant']]
        if significant_names:
            dt_idx = pd.to_datetime(date_series.values, errors='coerce')
            candidates = calendar_candidates(dt_idx)
            matched_mask = np.zeros(n_total, dtype=bool)
            effects: list[float] = []
            for name in significant_names:
                effect = float(np.clip(cal_results[name]['effect_size'], 0.0, 1.0))
                effects.append(effect)
                day_mask = np.asarray(candidates[name], dtype=bool)
                cal_scores = np.where(day_mask, np.maximum(cal_scores, effect), cal_scores)
                matched_mask |= day_mask
                if details_out is not None:
                    details_out.append({
                        'ホール名': hole, '機種名': machine_name, '台番号': int(unit),
                        '経路': 'カレンダー', '条件': name,
                        '効果量': round(effect, 3),
                    })
            mean_effect = float(np.mean(effects))
            cal_scores = np.where(matched_mask, cal_scores, -NEGATIVE_SCALE * mean_effect)

        # ── 2経路統合: 符号付きnoisy-or(_combine_signed) ──
        combined = _combine_signed(main_scores, cal_scores)
        combined = np.where(combined != 0.0, combined, np.nan)
        if np.isnan(combined).all():
            continue  # 検出不可 → NaN のまま

        scores.loc[grp_sorted.index] = combined

    return scores


def build_observed_history(
    df: pd.DataFrame,
    hole_name: str,
    machine_name: str,
    unit: int,
    unit_col: str = '台番号',
) -> pd.Series:
    """
    score_teppandaiと同じ切り出し(日付昇順・is_invalidはNaN化・観測順に0始まりindex化)で
    指定台のhigh_prob履歴を返す。predict_next_day系の翌日投影で、検出時と同じ位相基準を
    再現するために使う(二重実装を避けるため共通化)。
    """
    mask = (
        (df['ホール名'] == hole_name)
        & (df['機種名'] == machine_name)
        & (df[unit_col] == unit)
    )
    grp_sorted = df[mask].sort_values('日付')
    hp = grp_sorted['high_prob'].copy()
    if 'is_invalid' in grp_sorted.columns:
        hp[grp_sorted['is_invalid'].fillna(True).values] = np.nan
    return hp.reset_index(drop=True)


def predict_next_day(
    hp: pd.Series,
    lags: list[int],
    cal_conditions: list[dict],
    next_date,
) -> float | None:
    """
    S_鉄板台の「次の観測日」(next_date、暦日)のスコアを、検出済み条件のみから予測する。
    [リーク禁止] hpはこの予測計算に使うデータ最終日までの観測順history
    (is_invalidはNaN化済み)のみを渡すこと。実測値(翌日の差枚等)は一切使わない。

    lags: teppan_conditionsの周期経路で確認済みの周期(観測順lag)のリスト。
        複数ある場合は各lagの投影値を_combine_signedで順に統合する
        (score_teppandai本体が複数周期をnoisy-orで統合するのと同じ扱い)。
    cal_conditions: teppan_conditionsのカレンダー経路の行(条件名・効果量)のリスト。
        next_dateの曜日・日付末尾と照合し、一致すれば効果量(正)、
        一致しなければ-NEGATIVE_SCALE×平均効果量(負)を採用する。

    周期・カレンダーともに情報がない(条件が空、または該当ビン/候補が未検出)場合はNoneを返す。
    """
    lag_pred = 0.0
    for lag in lags:
        lag_pred = _combine_signed(
            np.array([lag_pred]), np.array([_project_phase_score(hp, lag)])
        )[0]

    cal_pred = 0.0
    if cal_conditions:
        dt = pd.Timestamp(next_date)
        candidates = calendar_candidates(pd.DatetimeIndex([dt]))
        matched_effects = [
            float(c['効果量']) for c in cal_conditions
            if bool(candidates.get(c['条件'], np.array([False]))[0])
        ]
        if matched_effects:
            cal_pred = max(matched_effects)
        else:
            mean_effect = float(np.mean([float(c['効果量']) for c in cal_conditions]))
            cal_pred = -NEGATIVE_SCALE * mean_effect

    if lag_pred == 0.0 and cal_pred == 0.0:
        return None
    return float(_combine_signed(np.array([lag_pred]), np.array([cal_pred]))[0])


def predict_next_day_with_blend(
    hp_long: pd.Series,
    hp_short: pd.Series,
    lags: list[int],
    cal_conditions: list[dict],
    next_date,
    alpha: float = None,
) -> dict:
    """
    長期版(全履歴hp_long)・短期版(直近M日窓hp_short、compute_short_term_scoreと同じ
    切り出し)の両方でpredict_next_dayを計算し、FIXED_ALPHAでブレンドする
    (blend()と同じ「short版がNaN=alpha実質0で長期版を使用」の規約に合わせる)。

    長期/短期の生予測値をそのままprediction_logに残しておくことで、将来
    walk_forward_alphaによるα再学習にこのログをそのまま再利用できる(今後の実装予定.md 1.1節)。

    Returns: {'長期スコア', '短期スコア', 'ブレンド値', '使用alpha'}(すべて計算不可ならNone)
    """
    if alpha is None:
        alpha = FIXED_ALPHA

    long_pred = predict_next_day(hp_long, lags, cal_conditions, next_date)
    short_pred = predict_next_day(hp_short, lags, cal_conditions, next_date)

    if long_pred is None and short_pred is None:
        return {'長期スコア': None, '短期スコア': None, 'ブレンド値': None, '使用alpha': None}
    if long_pred is None:
        return {'長期スコア': None, '短期スコア': short_pred, 'ブレンド値': short_pred, '使用alpha': 1.0}
    if short_pred is None:
        return {'長期スコア': long_pred, '短期スコア': None, 'ブレンド値': long_pred, '使用alpha': 0.0}

    blended = alpha * short_pred + (1.0 - alpha) * long_pred
    return {'長期スコア': long_pred, '短期スコア': short_pred, 'ブレンド値': blended, '使用alpha': alpha}


def score_rotation(
    df: pd.DataFrame,
    machine_name: str,
    group_col: str = '台番号',
) -> pd.Series:
    """
    S_ローテ: 窓内集中度(ジニ係数) + 並べ替え検定 + FDR補正 + 効果量ゲート。
    判定: 分散が有意 かつ ジニ係数が中程度(0.15〜0.7) → ローテーション検出。
    スコアはジニ係数(0〜1)をそのままホール×機種の全行に適用。
    """
    from preprocess import check_missing_bias  # 循環インポート回避のため局所import

    scores = pd.Series(np.nan, index=df.index)
    mask_machine = df['機種名'] == machine_name
    sub = df[mask_machine]
    if sub.empty:
        return scores

    for hole, hole_grp in sub.groupby('ホール名', sort=False):
        if 'is_invalid' in hole_grp.columns:
            valid = hole_grp[~hole_grp['is_invalid'].fillna(True)]
        else:
            valid = hole_grp
        if len(valid) < 10:
            continue

        unit_means = valid.groupby(group_col)['high_prob'].mean()
        n_units = len(unit_means)
        if n_units < 3:
            continue

        vals = unit_means.values
        sorted_vals = np.sort(vals)
        n = len(sorted_vals)
        total = sorted_vals.sum()
        if total > 0:
            # 標準Gini: G = (2*sum((i+1)*x_i))/(n*sum(x)) - (n+1)/n  (i=0..n-1, 昇順)
            gini = float(
                (2.0 * np.dot(np.arange(1, n + 1), sorted_vals)) / (n * total) - (n + 1) / n
            )
        else:
            gini = 0.0
        gini = float(np.clip(gini, 0.0, 1.0))

        # 欠損偏りガード(全行候補として渡す)
        mini_df = (valid[['is_invalid']].copy() if 'is_invalid' in valid.columns
                   else pd.DataFrame({'is_invalid': pd.Series(False, index=valid.index)}))
        bias = check_missing_bias(mini_df, pd.Series(True, index=valid.index))
        if bias['skip_test']:
            continue

        # 並べ替え検定: ユニット間 high_prob 平均の分散が偶然より大きいか
        obs_var = float(np.var(vals, ddof=1))
        flat = valid['high_prob'].dropna().values
        if len(flat) < n_units:
            continue
        unit_sizes = valid.groupby(group_col).size().values
        rng = np.random.default_rng(42)
        count_ge = sum(
            float(np.var(
                [float(np.mean(perm[s:s + sz]))
                 for s, sz in zip(np.cumsum(np.concatenate([[0], unit_sizes[:-1]])), unit_sizes)],
                ddof=1,
            )) >= obs_var
            for perm in (rng.permutation(flat) for _ in range(500))
        )
        p_val = count_ge / 500

        # ローテ: 有意 かつ 中程度集中(GINI_THRESHOLD≤gini<0.7 = 1台独占でも均等でもない)
        if (p_val < FDR_ALPHA
                and gini >= GINI_THRESHOLD
                and gini < 0.7):
            scores.loc[hole_grp.index] = gini

    return scores


# [2026-07 タスク3] 据え置き日次判定の暫定パラメータ(実データで調整前提)。
SUEKI_WINDOW = 14           # 日次r_tを計算する直近K日窓
SUEKI_EWMA_SPAN = 7         # r_t平滑化のEWMA span
SUEKI_MIN_PAIRS = 8         # 14日窓(最大13ペア)内の最低有効ペア数。10だと窓の約8割充足が
                             # 必要でNaNが増えすぎるため緩和(実測: 足切り8→NaN率20.5%/足切り10→30.7%)
SUEKI_DAILY_THRESHOLD = 0.2  # 平滑後r̄_tがこの値以上の日を「据え置き該当日」とみなす


def sueki_daily_r(hp: pd.Series) -> np.ndarray:
    """
    S_据え置き(日次版)の生の平滑化lag-1自己相関r̄_tを日ごとに計算する。
    台ごとの直近SUEKI_WINDOW日窓でlag-1自己相関を計算し(窓内の有効ペアが
    SUEKI_MIN_PAIRS未満の日はNaN)、EWMA(span=SUEKI_EWMA_SPAN)で平滑化する。

    符号変換(score_sueki_dailyの閾値判定)前の生の値。_run_sueki_predictions
    (run_store_profile.py)の翌日投影の乗数としても共用する(両者で二重実装しないため)。
    """
    x = hp.reset_index(drop=True).astype(float)
    n = len(x)
    if n < 2:
        return np.full(n, np.nan)

    x1 = x.iloc[:-1].reset_index(drop=True)
    x2 = x.iloc[1:].reset_index(drop=True)
    # 窓幅(K-1ペア)のrolling.corrはpairwise-complete(NaNペアはmin_periods判定から除外)
    pair_r = x1.rolling(window=SUEKI_WINDOW - 1, min_periods=SUEKI_MIN_PAIRS).corr(x2)

    r_raw = np.full(n, np.nan)
    r_raw[1:] = pair_r.values  # ペアpは日p+1に対応

    r_smoothed = pd.Series(r_raw).ewm(span=SUEKI_EWMA_SPAN, min_periods=1).mean()
    return r_smoothed.values


def score_sueki_daily(df: pd.DataFrame) -> pd.Series:
    """
    S_据え置き(日次版): 平滑化lag-1自己相関r̄_tがSUEKI_DAILY_THRESHOLD以上の日を
    該当日(正スコア=+r̄_t)、それ未満の日を切断(負スコア)とする、S_鉄板台と同じ
    符号規約([-1,1]・負=非該当日)のスコア。履歴不足でr̄_tが計算できない日はNaN。

    [2026-07 タスク3] 旧score_sueki(台単位で全期間1定数)からの差し替え。店舗の
    「据え置き癖」指標としての役割はestimate_transition_matrixのpi/p_stay由来の
    値に譲る(データ分析_skill.md参照)。
    """
    scores = pd.Series(np.nan, index=df.index)
    for (hole, machine, unit), grp in df.groupby(['ホール名', '機種名', '台番号'], sort=False):
        grp_sorted = grp.sort_values('日付')
        hp = grp_sorted['high_prob'].copy()
        if 'is_invalid' in grp_sorted.columns:
            hp[grp_sorted['is_invalid'].fillna(True).values] = np.nan

        r_bar = sueki_daily_r(hp)
        # [2026-07-14 応急処置] prediction_accuracy実測(全10店でspearman≈0・負飽和が
        # 全行の約6割)を受け、負側にNEGATIVE_SCALEを乗じて緩和(2026-07-08合意済みの調整候補
        # を実施)。r̄_t=0の無記憶状態での飽和値は-1→-0.5になる
        signed = np.where(
            np.isnan(r_bar),
            np.nan,
            np.where(
                r_bar >= SUEKI_DAILY_THRESHOLD,
                r_bar,
                -NEGATIVE_SCALE * np.minimum(1.0, (SUEKI_DAILY_THRESHOLD - r_bar) / SUEKI_DAILY_THRESHOLD),
            ),
        )
        scores.loc[grp_sorted.index] = signed
    return scores


def compute_depth_scores(df: pd.DataFrame, teppan_details: list | None = None) -> pd.DataFrame:
    """
    全深さ型サブスコアを計算して列追加した DataFrame を返す。
    teppan_details: listを渡すとS_鉄板台の検出条件(経路/条件/効果量)を追記する。
    """
    out = df.copy()
    teppan = pd.Series(np.nan, index=out.index)
    rotation = pd.Series(np.nan, index=out.index)
    for machine in out['機種名'].dropna().unique():
        mask = out['機種名'] == machine
        teppan[mask] = score_teppandai(out, machine, details_out=teppan_details).reindex(out.index[mask])
        rotation[mask] = score_rotation(out, machine).reindex(out.index[mask])
    out['S_鉄板台'] = teppan
    out['S_ローテ'] = rotation
    out['S_据え置き'] = score_sueki_daily(out)
    return out


def compute_short_term_score(
    df: pd.DataFrame,
    score_col: str,
    window: int = SHORT_WINDOW_DEFAULT,
) -> pd.Series:
    """
    指定サブスコアの短期版(直近M日のウィンドウ)を計算する。
    サンプル不足時はNaN → blend() 内でα=0フォールバック。
    """
    result = pd.Series(np.nan, index=df.index)
    all_dates = sorted(df['日付'].unique())
    if len(all_dates) < window:
        return result

    cutoff = all_dates[-window]
    short_df = df[df['日付'] >= cutoff]

    if score_col == 'S_全台系':
        short_scores = score_zentaiki(short_df, ['機種名'])
    elif score_col == 'S_鉄板台':
        teppan = pd.Series(np.nan, index=short_df.index)
        for machine in short_df['機種名'].dropna().unique():
            s = score_teppandai(short_df, machine)
            teppan.update(s)
        short_scores = teppan
    elif score_col == 'S_ローテ':
        rotation = pd.Series(np.nan, index=short_df.index)
        for machine in short_df['機種名'].dropna().unique():
            s = score_rotation(short_df, machine)
            rotation.update(s)
        short_scores = rotation
    elif score_col == 'S_据え置き':
        short_scores = score_sueki_daily(short_df)
    else:
        return result

    result.loc[short_scores.index] = short_scores.values
    return result

