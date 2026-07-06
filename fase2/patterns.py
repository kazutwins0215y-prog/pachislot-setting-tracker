"""
patterns.py — イベント検出・パターンスコア・αブレンド

移動台検出 (detect_events / detect_all_events / is_new_series):
    K=0前日比較で移動/撤去/増台イベントを検出
幅型パターン (score_zentaiki / score_shintai / score_idoudai):
    S_全台系 / S_新台増台 / S_移動台 — 1日分データで検出可能
深さ型パターン (score_teppandai / score_rotation / score_sueki):
    S_鉄板台(ACF→PDM→Lomb-Scargle / カレンダー検定) / S_ローテ / S_据え置き
αブレンド (blend / walk_forward_alpha / learn_all_alphas):
    長期/短期サブスコアのウォークフォワードα学習
    対象: S_全台系・S_鉄板台・S_ローテ・S_据え置き

依存: preprocess.py (check_missing_bias を深さ型検定内で呼ぶ)
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


# ── 移動台検出 ────────────────────────────────────────────────────

def detect_events(df: pd.DataFrame, hole_name: str, machine_name: str) -> pd.DataFrame:
    """
    指定ホール×機種の前日比較で移動/撤去/増台イベントを検出する。
    N = min(消失台数, 新規台数) を移動件数とし、残りを撤去/増台に分類。
    同型入替(台番号変更なし)は検知不可のため対象外。

    Returns:
        DataFrame: 日付, ホール名, 機種名, 移動件数, 移動台番号, 撤去台番号, 増台台番号
        - 移動台番号: 移動後の新台番号リスト (S_移動台の対象)
        - 撤去台番号: 実際に撤去された台番号リスト
        - 増台台番号: 純粋な増台台番号リスト (S_新台増台の対象)
    """
    mask = (df['ホール名'] == hole_name) & (df['機種名'] == machine_name)
    sub = df.loc[mask, ['日付', '台番号']].dropna().drop_duplicates()

    dates = sorted(sub['日付'].unique())
    records = []

    for i in range(1, len(dates)):
        d_prev, d_curr = dates[i - 1], dates[i]

        prev_set = set(sub.loc[sub['日付'] == d_prev, '台番号'].astype(int))
        curr_set = set(sub.loc[sub['日付'] == d_curr, '台番号'].astype(int))

        disappeared = sorted(prev_set - curr_set)
        appeared = sorted(curr_set - prev_set)

        if not disappeared and not appeared:
            continue

        n_moved = min(len(disappeared), len(appeared))

        records.append({
            '日付': d_curr,
            'ホール名': hole_name,
            '機種名': machine_name,
            '移動件数': n_moved,
            '移動台番号': appeared[:n_moved],
            '撤去台番号': disappeared[n_moved:],
            '増台台番号': appeared[n_moved:],
        })

    return pd.DataFrame(
        records,
        columns=['日付', 'ホール名', '機種名', '移動件数', '移動台番号', '撤去台番号', '増台台番号'],
    )


def detect_all_events(df: pd.DataFrame) -> pd.DataFrame:
    """全ホール×全機種に対してイベント検出を実行してまとめて返す。"""
    parts = []
    for (hole, machine), _ in df.groupby(['ホール名', '機種名'], sort=False):
        events = detect_events(df, hole, machine)
        if not events.empty:
            parts.append(events)

    if not parts:
        return pd.DataFrame(
            columns=['日付', 'ホール名', '機種名', '移動件数', '移動台番号', '撤去台番号', '増台台番号']
        )

    return pd.concat(parts, ignore_index=True)


def is_new_series(
    df: pd.DataFrame,
    hole_name: str,
    machine_name: str,
    台番号: int,
    date: str,
) -> bool:
    """
    指定の台が「移動後の新シリーズ」かどうかを返す。
    台の同一性キーは (機種名, 台番号) — これが変わったら履歴リセット。
    """
    mask = (df['ホール名'] == hole_name) & (df['機種名'] == machine_name)
    sub = df.loc[mask, ['日付', '台番号']].dropna().drop_duplicates()

    dates = sorted(sub['日付'].unique())

    if date not in dates:
        return False

    idx = dates.index(date)
    if idx == 0:
        return False

    d_prev = dates[idx - 1]
    prev_units = set(sub.loc[sub['日付'] == d_prev, '台番号'].astype(int))
    return int(台番号) not in prev_units


# ── 幅型パターン ──────────────────────────────────────────────────

def score_zentaiki(df: pd.DataFrame, group_cols: list[str]) -> pd.Series:
    """
    S_全台系: 当日・指定グループ(機種/列/島)内の横断スコア集計。
    Stage3スコアの「高さ × 揺らぎの少なさ」を 0〜1 で返す。
    1日分のデータのみでも検出可能。
    """
    if 'is_invalid' in df.columns:
        valid = df[~df['is_invalid'].fillna(True)]
    else:
        valid = df

    avail_cols = [c for c in group_cols if c in df.columns]
    if not avail_cols:
        avail_cols = ['機種名'] if '機種名' in df.columns else []
    group_key = ['日付', 'ホール名'] + avail_cols

    stats = (
        valid.groupby(group_key)['high_prob']
        .agg(_mean='mean', _std='std', _count='count')
        .reset_index()
    )
    stats['_std'] = stats['_std'].fillna(0.0)
    # 高さ × 揺らぎの少なさ: std=0 → uniformity=1, std=0.5(最大) → uniformity=0
    stats['_score'] = (
        stats['_mean'] * (1.0 - (stats['_std'] / 0.5)).clip(0.0, 1.0)
    ).clip(0.0, 1.0)
    stats.loc[stats['_count'] < 2, '_score'] = np.nan

    score_map = {
        tuple(row[group_key]): row['_score']
        for _, row in stats.iterrows()
    }
    keys = df[group_key].apply(tuple, axis=1)
    return keys.map(score_map)


def _compute_event_scores(
    df: pd.DataFrame,
    events_df: pd.DataFrame,
    unit_col: str,
    window: int,
) -> pd.Series:
    """S_新台増台・S_移動台 共通の計算ロジック。"""
    scores = pd.Series(np.nan, index=df.index)

    if events_df.empty or unit_col not in events_df.columns:
        return scores

    if 'is_invalid' in df.columns:
        valid_mask = ~df['is_invalid'].fillna(True)
    else:
        valid_mask = pd.Series(True, index=df.index)

    # 基準値: 店舗×機種の全履歴平均 high_prob (is_invalid除外)
    baseline_map = (
        df[valid_mask]
        .groupby(['ホール名', '機種名'])['high_prob']
        .mean()
        .to_dict()
    )

    for _, event in events_df.iterrows():
        hole = event['ホール名']
        machine = event['機種名']
        start_date = event['日付']
        units = event.get(unit_col, [])

        if not isinstance(units, (list, np.ndarray)) or len(units) == 0:
            continue

        baseline = baseline_map.get((hole, machine), 0.5)
        if pd.isna(baseline):
            baseline = 0.5

        for unit in units:
            unit_int = int(unit)
            unit_mask = (
                (df['ホール名'] == hole)
                & (df['機種名'] == machine)
                & (df['台番号'] == unit_int)
                & (df['日付'] >= start_date)
                & valid_mask
            )
            unit_rows = df[unit_mask].sort_values('日付')

            probs = unit_rows['high_prob'].values
            for j, idx in enumerate(unit_rows.index):
                w_start = max(0, j - window + 1)
                trend = float(np.mean(probs[w_start:j + 1]))
                raw = max(0.0, trend - baseline)
                # 正規化: 差が 0.5 で score=1.0 に到達
                scores[idx] = float(min(1.0, raw / 0.5))

    return scores


def score_shintai(
    df: pd.DataFrame,
    events_df: pd.DataFrame,
    window: int = SHORT_WINDOW,
) -> pd.Series:
    """
    S_新台増台: 増台後の直近移動平均 - 基準値 → max(0, ...) → 0〜1。
    配分が落ち着くと差が縮み自動フェードアウト。
    """
    return _compute_event_scores(df, events_df, '増台台番号', window)


def score_idoudai(
    df: pd.DataFrame,
    events_df: pd.DataFrame,
    window: int = SHORT_WINDOW,
) -> pd.Series:
    """
    S_移動台: 移動後の直近移動平均 - 同機種店舗全体平均 → max(0, ...) → 0〜1。
    S_新台増台とは独立したサブスコア(重みを別々に調整できる)。
    """
    return _compute_event_scores(df, events_df, '移動台番号', window)


def compute_breadth_scores(
    df: pd.DataFrame,
    events_df: pd.DataFrame,
    group_cols: list[str] | None = None,
) -> pd.DataFrame:
    """全幅型サブスコアを計算して列追加した DataFrame を返す。"""
    if group_cols is None:
        group_cols = ['機種名']

    out = df.copy()
    out['S_全台系'] = score_zentaiki(out, group_cols)
    out['S_新台増台'] = score_shintai(out, events_df)
    out['S_移動台'] = score_idoudai(out, events_df)
    return out


# ── 深さ型パターン ────────────────────────────────────────────────

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
    既知カレンダー17候補(曜日7 + 日付末尾10 + ゾロ目1)の日付マスクを返す。
    calendar_test(検定)と score_teppandai(該当日スコア付与)で共用する。
    """
    candidates: dict[str, np.ndarray] = {}
    for i, name in enumerate(_WEEKDAY_NAMES):
        candidates[f'曜日_{name}'] = (dt.dayofweek == i)
    for d in range(10):
        candidates[f'末尾_{d}'] = (dt.day % 10 == d)
    candidates['ゾロ目'] = np.isin(dt.day, [11, 22])
    return candidates


def calendar_test(
    series: pd.Series,
    dates: pd.Series,
    check_missing_bias_fn,
) -> dict:
    """
    既知カレンダー17候補(曜日7 + 日付末尾10 + ゾロ目1)の検定。
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


TEPPAN_PHASE_BINS = 5  # pdm_confirmと同じ位相ビン数


def _phase_day_scores(hp: pd.Series, lag: int, n_bins: int = TEPPAN_PHASE_BINS) -> np.ndarray:
    """
    確認済み周期lagについて、位相ビンごとの平均high_probが全体平均を上回るビンに
    属する日へ (ビン平均 − 全体平均) / 0.5 のスコア(0〜1)を付与して返す。
    該当しない日は0.0。0.5の正規化はscore_zentaiki等と同じ規約。

    ※ 位相は「観測順インデックス」基準(既存のACF/PDMと同じ近似)。
      営業日が飛ぶと暦日とはズレる点に注意。
    """
    x = hp.values.astype(float)
    out = np.zeros(len(x))
    valid = ~np.isnan(x)
    if int(valid.sum()) < 10:
        return out
    overall = float(np.nanmean(x))
    t = np.arange(len(x))
    phases = (t % lag) / lag
    bins = (phases * n_bins).astype(int) % n_bins
    for b in range(n_bins):
        m = (bins == b) & valid
        if int(m.sum()) < 2:
            continue
        diff = float(np.mean(x[m])) - overall
        if diff > 0:
            # 該当ビンに属する日全体(欠測日含む=そのビンの日は熱いという予測)
            out[bins == b] = min(1.0, diff / 0.5)
    return out


def score_teppandai(
    df: pd.DataFrame,
    machine_name: str,
    unit_col: str = '台番号',
    details_out: list | None = None,
) -> pd.Series:
    """
    S_鉄板台: 2経路統合の鉄板台スコア(0〜1)。**該当日のみ**にスコアを付与する。

    [2026-07 仕様変更] 旧実装は検出台の全日に定数(1.0/0.6)を付与しており、
    「特定条件の日に入る」という鉄板台の性質と逆に、非該当日の狙い目度を
    押し上げて該当日のコントラストを消していた。現仕様:
    - カレンダー経路: 有意候補(例: 末尾7)に合致する日のみ、効果量(rank-biserial)をスコアに
    - 周期経路(ACF→PDM / Lomb-Scargle): 確認済み周期の高位相ビンに属する日のみ、
      (ビン平均−全体平均)/0.5 をスコアに
    - 両経路は noisy-or (1-(1-a)(1-b)) で統合(両経路一致日ほど高スコア)
    - 非該当日・未検出台は NaN(synthesizeで除外・再正規化。
      「非該当日は低設定寄り」の負スコア化は機能B再設計の符号付き拡張時に検討)

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

        # ── 周期経路: 確認済み周期ごとに該当日スコア(複数周期はmax) ──
        main_scores = np.zeros(n_total)
        if invalid_rate_val > INVALID_RATE_THRESHOLD:
            ts = pd.Series(np.arange(n_total, dtype=float))
            for period in lomb_scargle_screen(hp, ts):
                lag = max(2, int(round(period)))
                day_scores = _phase_day_scores(hp, lag)
                if day_scores.max() > 0:
                    main_scores = np.maximum(main_scores, day_scores)
                    if details_out is not None:
                        details_out.append({
                            'ホール名': hole, '機種名': machine_name, '台番号': int(unit),
                            '経路': '周期(Lomb-Scargle)', '条件': f'周期{lag}日(観測順)',
                            '効果量': round(float(day_scores.max()), 3),
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
                        main_scores = np.maximum(main_scores, day_scores)
                        if details_out is not None:
                            details_out.append({
                                'ホール名': hole, '機種名': machine_name, '台番号': int(unit),
                                '経路': '周期(ACF+PDM)', '条件': f'周期{lag}日(観測順)',
                                '効果量': round(float(1.0 - res['theta']), 3),
                            })

        # ── カレンダー経路: 有意候補に合致する日のみ効果量をスコアに ──
        cal_scores = np.zeros(n_total)
        date_series = pd.Series(grp_sorted['日付'].values)
        cal_results = calendar_test(hp, date_series, check_missing_bias)
        significant_names = [n for n, v in cal_results.items() if v['significant']]
        if significant_names:
            dt_idx = pd.to_datetime(date_series.values, errors='coerce')
            candidates = calendar_candidates(dt_idx)
            for name in significant_names:
                effect = float(np.clip(cal_results[name]['effect_size'], 0.0, 1.0))
                day_mask = np.asarray(candidates[name], dtype=bool)
                cal_scores = np.maximum(cal_scores, np.where(day_mask, effect, 0.0))
                if details_out is not None:
                    details_out.append({
                        'ホール名': hole, '機種名': machine_name, '台番号': int(unit),
                        '経路': 'カレンダー', '条件': name,
                        '効果量': round(effect, 3),
                    })

        # ── 2経路統合(noisy-or): 両経路が同じ日を指すほど高スコア ──
        combined = 1.0 - (1.0 - main_scores) * (1.0 - cal_scores)
        combined = np.where(combined > 0, combined, np.nan)
        if np.isnan(combined).all():
            continue  # 検出不可 → NaN のまま

        scores.loc[grp_sorted.index] = combined

    return scores


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


def score_sueki(df: pd.DataFrame) -> pd.Series:
    """
    S_据え置き: Stage3スコアのlag-1自己相関(0〜1)。
    正の自己相関 = 前日と同傾向の高設定が続いている(据え置き)。
    履歴10日未満の台は NaN(検出不可扱い)。
    """
    scores = pd.Series(np.nan, index=df.index)
    for (hole, machine, unit), grp in df.groupby(['ホール名', '機種名', '台番号'], sort=False):
        grp_sorted = grp.sort_values('日付')
        hp = grp_sorted['high_prob'].copy()
        if 'is_invalid' in grp_sorted.columns:
            hp[grp_sorted['is_invalid'].fillna(True).values] = np.nan
        x = hp.values.astype(float)
        x1, x2 = x[:-1], x[1:]
        valid = ~(np.isnan(x1) | np.isnan(x2))
        if int(valid.sum()) < 10:
            continue
        v1, v2 = x1[valid], x2[valid]
        if np.std(v1) == 0 or np.std(v2) == 0:
            continue
        try:
            r, _ = stats.pearsonr(v1, v2)
        except Exception:
            continue
        scores.loc[grp_sorted.index] = float(max(0.0, r))
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
    out['S_据え置き'] = score_sueki(out)
    return out


# ── αブレンド ─────────────────────────────────────────────────────

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
        short_scores = score_sueki(short_df)
    else:
        return result

    result.loc[short_scores.index] = short_scores.values
    return result


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
