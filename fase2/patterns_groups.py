"""
patterns_groups.py — 末尾版/機種版/店舗日のカレンダー癖検定・機種バイアス判定・翌日予測
(2026-07-19にpatterns.pyから分割。利用側は `import patterns as pt` のfacade経由を推奨)
"""
import pandas as pd
import numpy as np
from scipy import stats

from patterns_common import (
    EFFECT_SIZE_THRESHOLD,
    GROUP_CALENDAR_MIN_DAYS,
    _wilcoxon_rank_biserial,
    benjamini_hochberg,
    calendar_candidates,
)

# ── 末尾版: グループ定義(今後の実装予定.md 1.8節「次回分(末尾版)」) ─────────

def tail_digit_group(units: pd.Series) -> pd.Series:
    """
    台番号Series → グループ名Series('グループ末尾_0'〜'9' または 'グループゾロ目')。
    ゾロ目 = 全桁が同一の台番号(11,22,…,99,111,222,…)。ゾロ目該当台が存在しない
    店舗ではこのグループが自然に出現せず、呼び出し側(group_calendar_conditions系)で
    自動スキップされる(グループ一覧はunique()から作るため)。

    グループ定義は関数として切り出してあり、「グループ=機種」等への差し替え
    (今後の実装予定.md 1.8節「機種単位の癖分析」)は本関数と同じ返り値の形
    (行→グループ名のSeries)を作る別関数を用意すれば、末尾版レイヤー2の検出ロジックは
    そのまま再利用できる設計。ユニークな台番号ごとに1回だけ判定してmapする
    (全行を都度str変換しない)。
    """
    unique_units = units.dropna().astype(int).unique()

    def _label(u: int) -> str:
        s = str(u)
        if len(s) >= 2 and len(set(s)) == 1:
            return 'グループゾロ目'
        return f'グループ末尾_{u % 10}'

    mapping = {u: _label(u) for u in unique_units}
    return units.astype('Int64').map(mapping)


# ── 機種単位の癖分析(今後の実装予定.md 1.8節「機種単位の癖分析」) ──────────

MACHINE_GROUP_MIN_UNITS = 2   # 日次有効台数の中央値がこれ未満の機種は検定対象外
RECENT_TEST_WINDOW_DAYS = 90  # 看板機種/機種カレンダーの「直近窓」検定用の日数(暫定値)。
                               # αブレンド投影用のSHORT_WINDOW_DEFAULT(=30)とは別物
MACHINE_BIAS_MIN_STORE_RATIO = 0.5  # 機種バイアス判定の閾値(過半数。今後の実装予定.md 1.8.5節「案A」)
                               # (こちらは検定窓、あちらは予測投影窓)


def group_size_medians(df: pd.DataFrame) -> pd.Series:
    """
    機種ごとの日次有効台数の中央値(machine_groupのゲート判定・
    group_calendar_conditions.台数中央値列の保存に使う)。is_invalid列があれば除外する。
    """
    sub = df
    if 'is_invalid' in df.columns:
        sub = df.loc[~df['is_invalid'].fillna(True)]
    daily_counts = (
        sub.dropna(subset=['機種名', '台番号', '日付'])
        .groupby(['機種名', '日付'])['台番号'].nunique()
    )
    return daily_counts.groupby('機種名').median()


def machine_group(df: pd.DataFrame, min_units: int = MACHINE_GROUP_MIN_UNITS) -> pd.Series:
    """
    行 → 機種名 のグループSeries(tail_digit_groupと同じ「行→グループ名」の形式にすることで、
    group_calendar_test/build_group_calendar_conditionsを検出器を変えずに再利用する)。
    グループ名は機種名そのまま(末尾版のような接頭辞は付けない)。

    日次有効台数の中央値がmin_units未満の機種はNaN(検定対象外。2〜3台構成のローテは
    機種集計の恩恵がなく1.2節の台粒度ローテに任せる、という2026-07-10ユーザー合意。
    「検出は緩く保存・使用側でゲート」の思想でn≥2は検定側の最小ゲート、表示/予測側の
    追加ゲート(暫定n≥3等)は台数中央値列を見て別途判断する)。
    """
    medians = group_size_medians(df)
    valid_machines = set(medians[medians >= min_units].index)

    machine = df['機種名']
    return machine.where(machine.isin(valid_machines))


def group_constant_test(
    df: pd.DataFrame,
    hole_name: str,
    group_series: pd.Series,
    min_days: int = GROUP_CALENDAR_MIN_DAYS,
) -> pd.DataFrame:
    """
    看板機種検定(今後の実装予定.md 1.8節「機種単位の癖分析」)。「そのグループの投入率が、
    同日の他グループ平均より恒常的に高いか」を対応ペアのWilcoxon符号順位検定(片側greater)+
    rank-biserial相関で判定する。日付条件='恒常'固定の1行として返し、
    build_group_calendar_conditionsがgroup_calendar_test/match_rule_testと同じ
    仮説群に混ぜてBH補正する。

    店舗全体の平均投入率が定数オフセットとして乗る問題(フェーズ1で判明したStouffer破綻と
    同型)を、group_calendar_testの「候補日vs対照日」ではなく「自グループ vs 同日の
    他グループ平均」の対応ペア化で回避する(match_rule_testと同じ思想)。

    グループ内有効台数が少ないグループはgroup_series側で既に除外されている前提
    (machine_group参照)。対応ペア数がmin_days未満のグループは検定対象外(p_raw/効果量NaN)。
    """
    empty = pd.DataFrame(columns=['グループ', '日付条件', '該当日数', '対照日数', 'p_raw', '効果量'])

    mask = df['ホール名'] == hole_name
    if 'is_invalid' in df.columns:
        mask &= ~df['is_invalid'].fillna(True)
    sub = df.loc[mask].dropna(subset=['high_prob', '日付']).copy()
    if sub.empty:
        return empty

    sub['_グループ'] = group_series.reindex(sub.index)
    sub = sub.dropna(subset=['_グループ'])
    if sub.empty:
        return empty

    daily = sub.groupby(['_グループ', '日付'])['high_prob'].agg(n='count', sum_hp='sum').reset_index()
    daily['投入率'] = daily['sum_hp'] / daily['n']

    groups = sorted(daily['_グループ'].unique())
    records = []
    for g in groups:
        g_rate = daily.loc[daily['_グループ'] == g].set_index('日付')['投入率']
        other = daily.loc[daily['_グループ'] != g]
        if other.empty:
            records.append({
                'グループ': g, '日付条件': '恒常', '該当日数': 0, '対照日数': np.nan,
                'p_raw': np.nan, '効果量': np.nan,
            })
            continue
        other_mean = other.groupby('日付')['投入率'].mean()

        common_dates = g_rate.index.intersection(other_mean.index)
        diffs = (g_rate.loc[common_dates] - other_mean.loc[common_dates]).to_numpy(dtype=float)
        k = len(diffs)

        if k < min_days:
            records.append({
                'グループ': g, '日付条件': '恒常', '該当日数': k, '対照日数': np.nan,
                'p_raw': np.nan, '効果量': np.nan,
            })
            continue

        if np.all(diffs == 0.0):
            records.append({
                'グループ': g, '日付条件': '恒常', '該当日数': k, '対照日数': np.nan,
                'p_raw': 1.0, '効果量': 0.0,
            })
            continue

        _, p = stats.wilcoxon(diffs, alternative='greater')
        rbc = _wilcoxon_rank_biserial(diffs)
        records.append({
            'グループ': g, '日付条件': '恒常', '該当日数': k, '対照日数': np.nan,
            'p_raw': float(p), '効果量': rbc,
        })

    return pd.DataFrame(records)


def group_calendar_test(
    df: pd.DataFrame,
    hole_name: str,
    group_series: pd.Series,
    min_days: int = GROUP_CALENDAR_MIN_DAYS,
) -> pd.DataFrame:
    """
    グループ×日付条件のMann-Whitney U相対検定(今後の実装予定.md 1.8節「末尾版」レイヤー2の
    「固定グループ×固定日付条件」パート)。BH補正は掛けない生のp値・効果量を返す
    (一致ルール2本(match_rule_test)と合わせて1つの仮説群としてBH補正するため、
    それは呼び出し側のbuild_group_calendar_conditionsが行う)。

    [2026-07-10フェーズ1検証で確定] 当初案のStouffer統合z(z=(Σp−nπ)/√(nπ(1−π))を
    Σz÷√kで統合)は、店舗全体の平均high_probがπより系統的に高い(sigmoid飽和の右裾。
    詳細はデータ分析_skill.md参照)ため全仮説が有意化して閾値として破綻することが
    実データ(マルハン新宿東宝・エスパス歌舞伎町)で判明した。既存calendar_test(鉄板台の
    カレンダー検定)と同じ「候補日 vs 対照日」のMann-Whitney U片側検定(greater)+
    rank-biserial効果量に変更することで、店舗オフセットが自動的に相殺される(A案)。

    group_series: dfと同じindexを持つ、行→グループ名のSeries
    (例: tail_digit_group(df['台番号']))。グループ定義を外部注入にすることで
    「グループ=機種」等への差し替え(今後の実装予定.md 1.8節「機種単位の癖分析」)でも
    本関数をそのまま再利用できる。

    グループ×日で投入率(Σhigh_prob÷n、is_invalid除外)を集計し、
    calendar_candidates(49候補)の日付条件ごとに候補日/対照日へ二分してMann-Whitney U
    片側検定(greater)+rank-biserial相関(effect_size)を計算する。
    候補日・対照日のいずれかがmin_days未満の組み合わせは検定対象外(p_raw/効果量はNaN。
    品質ガード兼・仮説数の自動削減)。

    Returns:
        DataFrame(グループ, 日付条件, 該当日数, 対照日数, p_raw, 効果量)
        (使用データ最終日・ホール名は呼び出し側で付与する)
    """
    empty = pd.DataFrame(columns=['グループ', '日付条件', '該当日数', '対照日数', 'p_raw', '効果量'])

    mask = df['ホール名'] == hole_name
    if 'is_invalid' in df.columns:
        mask &= ~df['is_invalid'].fillna(True)
    sub = df.loc[mask].dropna(subset=['high_prob', '日付']).copy()
    if sub.empty:
        return empty

    sub['_グループ'] = group_series.reindex(sub.index)
    sub = sub.dropna(subset=['_グループ'])
    if sub.empty:
        return empty

    daily = sub.groupby(['_グループ', '日付'])['high_prob'].agg(n='count', sum_hp='sum').reset_index()
    daily['投入率'] = daily['sum_hp'] / daily['n']

    all_dates = sorted(sub['日付'].dropna().unique())
    dt_idx = pd.to_datetime(all_dates, errors='coerce')
    conditions = calendar_candidates(dt_idx)
    date_pos = {d: i for i, d in enumerate(all_dates)}

    groups = sorted(daily['_グループ'].unique())
    records = []
    for g in groups:
        g_rate = daily.loc[daily['_グループ'] == g].set_index('日付')['投入率']
        for cname, mask_arr in conditions.items():
            cand_dates = [d for d in g_rate.index if mask_arr[date_pos[d]]]
            ctrl_dates = [d for d in g_rate.index if not mask_arr[date_pos[d]]]
            k_cand, k_ctrl = len(cand_dates), len(ctrl_dates)

            if k_cand < min_days or k_ctrl < min_days:
                records.append({
                    'グループ': g, '日付条件': cname, '該当日数': k_cand, '対照日数': k_ctrl,
                    'p_raw': np.nan, '効果量': np.nan,
                })
                continue

            x = g_rate.loc[cand_dates].to_numpy(dtype=float)
            y = g_rate.loc[ctrl_dates].to_numpy(dtype=float)
            _, p = stats.mannwhitneyu(x, y, alternative='greater')
            u2, _ = stats.mannwhitneyu(x, y, alternative='two-sided')
            rbc = float(2.0 * u2 / (len(x) * len(y)) - 1.0)
            records.append({
                'グループ': g, '日付条件': cname, '該当日数': k_cand, '対照日数': k_ctrl,
                'p_raw': float(p), '効果量': rbc,
            })

    return pd.DataFrame(records)


MATCH_RULE_DIGIT2 = '下2桁一致'
MATCH_RULE_TAIL = '末尾一致'


def match_rule_test(
    df: pd.DataFrame,
    hole_name: str,
    rule: str,
    min_days: int = GROUP_CALENDAR_MIN_DAYS,
) -> dict:
    """
    一致ルール検定(今後の実装予定.md 1.8節「末尾版」レイヤー2の一致ルール2本)。BH補正は
    掛けない生のp値・効果量を返す(group_calendar_testと合わせてbuild_group_calendar_conditions
    が1つの仮説群としてBH補正する)。

    固定グループ×固定日付条件では表現できない動的な対応関係(日によって「一致する台」が
    変わる)を、日ごとの「一致する台 vs 一致しない台」の投入率差を対応ペアとして蓄積し、
    Wilcoxon符号順位検定(片側greater)で検定する(1ホール×1ルール=1仮説)。

    rule=MATCH_RULE_DIGIT2('下2桁一致'): 日付の日(1〜31) == 台番号下2桁(unit%100)の台が
        一致グループ(例: 12日に末尾12番台が高配分)
    rule=MATCH_RULE_TAIL('末尾一致'): 日付の日の末尾(day%10) == 台番号末尾(unit%10)の台が
        一致グループ(全末尾統合版。毎日該当日がある)

    一致グループ・非一致グループのどちらかが0台の日はその日をペアから除外する。
    有効なペア数がmin_days未満の場合は検定不可(p_raw/効果量はNaN)。

    Returns:
        {'該当日数', 'p_raw', '効果量'}
    """
    mask = df['ホール名'] == hole_name
    if 'is_invalid' in df.columns:
        mask &= ~df['is_invalid'].fillna(True)
    sub = df.loc[mask].dropna(subset=['high_prob', '日付', '台番号']).copy()
    empty = {'該当日数': 0, 'p_raw': np.nan, '効果量': np.nan}
    if sub.empty:
        return empty

    units = sub['台番号'].astype(int)
    dt = pd.to_datetime(sub['日付'], errors='coerce')
    if rule == MATCH_RULE_DIGIT2:
        is_match = (units % 100) == dt.dt.day
    elif rule == MATCH_RULE_TAIL:
        is_match = (units % 10) == (dt.dt.day % 10)
    else:
        raise ValueError(f'不明なrule: {rule}')
    sub = sub.assign(_一致=is_match.to_numpy())

    diffs = []
    for _, day_grp in sub.groupby('日付'):
        matched = day_grp.loc[day_grp['_一致'], 'high_prob']
        unmatched = day_grp.loc[~day_grp['_一致'], 'high_prob']
        if matched.empty or unmatched.empty:
            continue
        diffs.append(float(matched.mean()) - float(unmatched.mean()))

    k = len(diffs)
    if k < min_days:
        return {'該当日数': k, 'p_raw': np.nan, '効果量': np.nan}

    diffs_arr = np.array(diffs, dtype=float)
    if np.all(diffs_arr == 0.0):
        return {'該当日数': k, 'p_raw': 1.0, '効果量': 0.0}
    _, p = stats.wilcoxon(diffs_arr, alternative='greater')
    rbc = _wilcoxon_rank_biserial(diffs_arr)
    return {'該当日数': k, 'p_raw': float(p), '効果量': rbc}


def build_group_calendar_conditions(
    df: pd.DataFrame,
    hole_name: str,
    group_series: pd.Series,
    group_type: str = '台番号末尾',
    min_days: int = GROUP_CALENDAR_MIN_DAYS,
    include_match_rules: bool = True,
    include_constant: bool = False,
) -> pd.DataFrame:
    """
    group_calendar_test(固定グループ×固定日付条件)・match_rule_test(一致ルール2本)・
    group_constant_test(看板/恒常検定)を合わせて1つの仮説群としてBH補正し、
    group_calendar_conditionsテーブル保存用の最終結果を返す(今後の実装予定.md 1.8節
    「末尾版」レイヤー2、および「機種単位の癖分析」の統合エントリポイント)。

    「毎月6日⊂日付末尾6⊂一致ルール」のように条件は入れ子になるが、重複統合
    (同一グループへの該当条件のうちmax効果量を採用)はここでは行わない。設計上
    「保存は全条件を残し、予測時にmax(効果量)を採用」と決まっているため
    (フェーズ3=S_末尾並走記録の実装時に行う)。

    group_type: 保存先テーブルの「グループ種別」列の値(既定'台番号末尾'。
    「グループ=機種」等への拡張時は呼び出し側でこの値を差し替える)。
    一致ルールの行はグループ列='一致ルール'固定(動的グループのため個別グループ名を
    持たない)。

    include_match_rules: 一致ルール2本を含めるか(末尾版=True、機種版は意味を持たないため
    呼び出し側でFalseにする)。
    include_constant: group_constant_test(看板機種検定)を含めるか(末尾版=False既定、
    機種版=Trueで呼び出し側が指定する)。

    Returns:
        DataFrame(グループ種別, グループ, 日付条件, 該当日数, 対照日数, p_raw, 効果量, BH有意)
        (ホール名・使用データ最終日は呼び出し側で付与する)
    """
    frames = []

    grid = group_calendar_test(df, hole_name, group_series, min_days=min_days)
    grid.insert(0, 'グループ種別', group_type)
    frames.append(grid)

    if include_match_rules:
        match_records = []
        for rule in (MATCH_RULE_DIGIT2, MATCH_RULE_TAIL):
            r = match_rule_test(df, hole_name, rule, min_days=min_days)
            match_records.append({
                'グループ種別': group_type, 'グループ': '一致ルール', '日付条件': rule,
                '該当日数': r['該当日数'], '対照日数': np.nan,
                'p_raw': r['p_raw'], '効果量': r['効果量'],
            })
        frames.append(pd.DataFrame(match_records))

    if include_constant:
        constant_df = group_constant_test(df, hole_name, group_series, min_days=min_days)
        constant_df.insert(0, 'グループ種別', group_type)
        frames.append(constant_df)

    result = pd.concat(frames, ignore_index=True)
    testable = result['p_raw'].notna()
    flags = benjamini_hochberg(result.loc[testable, 'p_raw'].tolist())
    result['BH有意'] = False
    result.loc[testable, 'BH有意'] = [
        bool(f) and eff >= EFFECT_SIZE_THRESHOLD
        for f, eff in zip(flags, result.loc[testable, '効果量'])
    ]
    return result


def predict_tail_group_next_day(
    unit: int,
    next_date,
    significant_conditions: pd.DataFrame,
) -> dict | None:
    """
    [今後の実装予定.md 1.8節「末尾版」フェーズ3] S_末尾の翌観測日予測。

    台番号unitが属する末尾グループ(固定グループ×固定日付条件)、および一致ルール
    (下2桁一致/末尾一致、unit依存の動的判定)の両方について、next_dateが該当する
    有意条件を集め、**重複統合(同一グループへの該当条件のうちmax効果量を採用、
    加算しない=二重計上回避)** を適用する(2026-07-10確定設計)。

    significant_conditions: build_group_calendar_conditionsの出力のうちBH有意=Trueの
    行(この店舗・この使用データ最終日分。呼び出し側でフィルタして渡す)。

    Returns:
        {'値': float(該当した有意条件のmax効果量), '該当条件': [{'グループ','日付条件','効果量'}, ...]}
        該当する有意条件が1つもない場合はNone(予測不可)。
    """
    if significant_conditions.empty:
        return None

    s = str(int(unit))
    if len(s) >= 2 and len(set(s)) == 1:
        unit_group = 'グループゾロ目'
    else:
        unit_group = f'グループ末尾_{int(unit) % 10}'

    dt = pd.Timestamp(next_date)
    candidates = calendar_candidates(pd.DatetimeIndex([dt]))

    matched: list[dict] = []

    grp_rows = significant_conditions[significant_conditions['グループ'] == unit_group]
    for _, row in grp_rows.iterrows():
        cname = row['日付条件']
        mask_arr = candidates.get(cname)
        if mask_arr is not None and bool(mask_arr[0]):
            matched.append({'グループ': unit_group, '日付条件': cname, '効果量': float(row['効果量'])})

    match_rows = significant_conditions[significant_conditions['グループ'] == '一致ルール']
    for _, row in match_rows.iterrows():
        rule = row['日付条件']
        is_hit = (
            (rule == MATCH_RULE_DIGIT2 and (int(unit) % 100) == dt.day)
            or (rule == MATCH_RULE_TAIL and (int(unit) % 10) == (dt.day % 10))
        )
        if is_hit:
            matched.append({'グループ': '一致ルール', '日付条件': rule, '効果量': float(row['効果量'])})

    if not matched:
        return None
    best = max(matched, key=lambda m: m['効果量'])
    return {'値': best['効果量'], '該当条件': matched}


def predict_machine_group_next_day(
    machine_name: str,
    next_date,
    significant_conditions: pd.DataFrame,
) -> dict | None:
    """
    [今後の実装予定.md 1.8節「機種単位の癖分析」] S_機種/S_機種_直近の翌観測日予測。
    predict_tail_group_next_dayの機種版(一致ルールに相当する動的グループがないため
    その分岐は持たない)。

    machine_nameが該当する有意条件('恒常'固定行は常に該当、カレンダー条件はnext_dateが
    該当する場合のみ)を集め、**重複統合(同一グループへの該当条件のうちmax効果量を採用、
    加算しない=二重計上回避)** を適用する(末尾版と同じ2026-07-10確定設計)。

    significant_conditions: build_group_calendar_conditions(include_constant=True)の
    出力のうちBH有意=Trueの行(この店舗・この検定窓・この使用データ最終日分。
    呼び出し側でフィルタして渡す)。

    Returns:
        {'値': float(該当した有意条件のmax効果量), '該当条件': [{'グループ','日付条件','効果量'}, ...]}
        該当する有意条件が1つもない場合はNone(予測不可)。
    """
    if significant_conditions.empty:
        return None

    grp_rows = significant_conditions[significant_conditions['グループ'] == machine_name]
    if grp_rows.empty:
        return None

    dt = pd.Timestamp(next_date)
    candidates = calendar_candidates(pd.DatetimeIndex([dt]))

    matched: list[dict] = []
    for _, row in grp_rows.iterrows():
        cname = row['日付条件']
        if cname == '恒常':
            matched.append({'グループ': machine_name, '日付条件': cname, '効果量': float(row['効果量'])})
            continue
        mask_arr = candidates.get(cname)
        if mask_arr is not None and bool(mask_arr[0]):
            matched.append({'グループ': machine_name, '日付条件': cname, '効果量': float(row['効果量'])})

    if not matched:
        return None
    best = max(matched, key=lambda m: m['効果量'])
    return {'値': best['効果量'], '該当条件': matched}


def identify_machine_bias(
    constant_conditions: pd.DataFrame,
    total_store_count: int,
    min_store_ratio: float = MACHINE_BIAS_MIN_STORE_RATIO,
) -> pd.DataFrame:
    """
    [今後の実装予定.md 1.8.5節「機種バイアス(全店恒常条件)の除外・案A」]
    全店舗横断で「機種×恒常」条件がBH有意になった店舗の比率を機種ごとに集計し、
    過半数の店舗で有意だった機種を「機種側のhigh_prob推定バイアス」と判定する。

    鉄拳6が11店中8店・いざ番長が7店で「恒常」有意になっており、ユーザーの実地実感も
    ないことから、店の癖ではなく機種側の系統バイアス(スペック表由来の判定の甘さ等)が
    店舗横断で写っていると判断(2026-07-14軸総点検で確定)。日付条件付き(末尾8・毎月X日等)
    は店固有性が高いため対象外(呼び出し側がグループ種別='機種'・日付条件='恒常'の行のみを
    渡すこと)。

    constant_conditions: 全店舗のgroup_calendar_conditions(グループ種別='機種',
    日付条件='恒常')を集約したDataFrame(列: ホール名, グループ(機種名), BH有意)。
    呼び出し側(score.py)がDBから読み込んで渡す。

    total_store_count: 分母に使う全店舗数(プロファイル済みの全店舗数。「8/11店」のように
    その機種が設置されていない店も非バイアス側として数える設計のため、機種の在籍店舗数
    ではなく全店舗数を使う)。

    Returns:
        DataFrame(機種名, 対象店舗数, 有意店舗数, 有意店舗比率, バイアス判定)
        対象店舗数はconstant_conditionsにその機種の恒常行が存在した店舗数(参考値、
        バイアス判定の分母には使わない)。有意店舗比率降順でソートする。
    """
    columns = ['機種名', '対象店舗数', '有意店舗数', '有意店舗比率', 'バイアス判定']
    if constant_conditions.empty or total_store_count <= 0:
        return pd.DataFrame(columns=columns)

    grouped = constant_conditions.groupby('グループ').agg(
        対象店舗数=('ホール名', 'nunique'),
        有意店舗数=('BH有意', lambda s: int(s.astype(bool).sum())),
    ).reset_index().rename(columns={'グループ': '機種名'})

    grouped['有意店舗比率'] = grouped['有意店舗数'] / total_store_count
    grouped['バイアス判定'] = grouped['有意店舗比率'] > min_store_ratio
    return grouped.sort_values('有意店舗比率', ascending=False).reset_index(drop=True)[columns]


# ── 店舗×曜日(店全体レベル)の癖軸(今後の実装予定.md 1.9節、2026-07-14設計確定) ──────

STORE_DAY_MIN_UNITS = 5  # 店舗×日集計に使う最低有効台数(未満の日は集計から除外。欠測・臨時休業対策)


def store_day_calendar_test(
    df: pd.DataFrame,
    hole_name: str,
    min_days: int = GROUP_CALENDAR_MIN_DAYS,
    min_units: int = STORE_DAY_MIN_UNITS,
) -> pd.DataFrame:
    """
    [今後の実装予定.md 1.9節「店舗×曜日(店全体レベル)の癖軸」] 店舗全体を1グループとして
    扱い、日付条件ごとに「候補日 vs 対照日」のMann-Whitney U片側検定(greater)+
    rank-biserial効果量を行う(末尾版/機種版のgroup_calendar_testと同じ枠組みだが、
    グループが店全体1つのため複数グループ間の比較にはならない)。

    既存軸(末尾版/機種版/機種強さ軸)はすべてhigh_prob(モデルの事後確率)を検定対象に
    しているが、この軸は「特定機種に固めない・中間設定中心の広浅い還元」を検出する目的のため、
    **実測差枚そのもの(勝ち台率=差枚>0の台の割合)を検定する**(2026-07-14ユーザー合意)。
    high_probベースの投入率だと機種側の推定を経由するため、広浅型の還元(モデルが強い/弱いと
    判定しない中間設定の底上げ)を原理的に検出できない。勝ち台率は店内の機種構成(ボラティリティが
    機種ごとに大きく異なる)に左右されにくく0〜1に正規化されるため、台あたり差枚(平均)より
    店舗横断比較に向くと判断し主指標に採用した。台あたり差枚は検定対象にはせず、
    「参考差枚差」(候補日平均−対照日平均、単位:枚)として同じ行に記述統計のみ保存する
    (実地照合用。プレサス土曜+240枚のような数字をそのまま確認できるようにするため)。

    有効台数(is_invalid除外)がmin_units未満の日は集計から除外する(欠測・臨時休業等の
    ノイズ日を候補日/対照日どちらの集団にも混ぜない)。

    BH補正は他軸(末尾版/機種版)とは混ぜず、この関数の49候補だけで独立に行う
    (店舗全体1グループのみのため、build_group_calendar_conditionsの一致ルール・
    看板機種検定は該当しない=専用のエントリポイントとする)。

    Returns:
        DataFrame(グループ種別='店舗日', グループ='店全体', 日付条件, 該当日数, 対照日数,
                  p_raw, 効果量, 参考差枚差, BH有意)
    """
    columns = [
        'グループ種別', 'グループ', '日付条件', '該当日数', '対照日数',
        'p_raw', '効果量', '参考差枚差', 'BH有意',
    ]
    empty = pd.DataFrame(columns=columns)

    mask = df['ホール名'] == hole_name
    if 'is_invalid' in df.columns:
        mask &= ~df['is_invalid'].fillna(True)
    sub = df.loc[mask].dropna(subset=['差枚', '日付']).copy()
    if sub.empty:
        return empty

    grp = sub.groupby('日付')['差枚']
    daily = pd.DataFrame({
        'n': grp.size(),
        '勝ち数': grp.apply(lambda s: int((s > 0).sum())),
        '平均差枚': grp.mean(),
    })
    daily = daily[daily['n'] >= min_units]
    if daily.empty:
        return empty
    daily['勝ち台率'] = daily['勝ち数'] / daily['n']

    all_dates = sorted(daily.index)
    dt_idx = pd.to_datetime(all_dates, errors='coerce')
    conditions = calendar_candidates(dt_idx)
    date_pos = {d: i for i, d in enumerate(all_dates)}

    records = []
    for cname, mask_arr in conditions.items():
        cand_dates = [d for d in all_dates if mask_arr[date_pos[d]]]
        ctrl_dates = [d for d in all_dates if not mask_arr[date_pos[d]]]
        k_cand, k_ctrl = len(cand_dates), len(ctrl_dates)

        if k_cand < min_days or k_ctrl < min_days:
            records.append({
                'グループ種別': '店舗日', 'グループ': '店全体', '日付条件': cname,
                '該当日数': k_cand, '対照日数': k_ctrl,
                'p_raw': np.nan, '効果量': np.nan, '参考差枚差': np.nan,
            })
            continue

        x = daily.loc[cand_dates, '勝ち台率'].to_numpy(dtype=float)
        y = daily.loc[ctrl_dates, '勝ち台率'].to_numpy(dtype=float)
        _, p = stats.mannwhitneyu(x, y, alternative='greater')
        u2, _ = stats.mannwhitneyu(x, y, alternative='two-sided')
        rbc = float(2.0 * u2 / (len(x) * len(y)) - 1.0)
        diff_ref = float(
            daily.loc[cand_dates, '平均差枚'].mean() - daily.loc[ctrl_dates, '平均差枚'].mean()
        )
        records.append({
            'グループ種別': '店舗日', 'グループ': '店全体', '日付条件': cname,
            '該当日数': k_cand, '対照日数': k_ctrl,
            'p_raw': float(p), '効果量': rbc, '参考差枚差': diff_ref,
        })

    result = pd.DataFrame(records)
    testable = result['p_raw'].notna()
    flags = benjamini_hochberg(result.loc[testable, 'p_raw'].tolist())
    result['BH有意'] = False
    result.loc[testable, 'BH有意'] = [
        bool(f) and eff >= EFFECT_SIZE_THRESHOLD
        for f, eff in zip(flags, result.loc[testable, '効果量'])
    ]
    return result[columns]


def predict_store_day_next_day(
    next_date,
    significant_conditions: pd.DataFrame,
) -> dict | None:
    """
    [今後の実装予定.md 1.9節「店舗×曜日の癖軸」] S_店舗日の翌観測日予測。店舗全体で
    1つのグループしかないため、末尾版/機種版と異なりグループ照合は不要で、next_dateが
    該当する有意カレンダー条件を集めてmax(効果量)を採る(重複統合、加算しない=二重計上回避)。

    significant_conditions: store_day_calendar_testの出力のうちBH有意=Trueの行
    (この店舗・この使用データ最終日分。呼び出し側でフィルタして渡す)。

    Returns:
        {'値': float(該当条件のmax効果量), '該当条件': [{'日付条件','効果量'}, ...]}
        該当する有意条件が1つもない場合はNone(予測不可)。
    """
    if significant_conditions.empty:
        return None

    dt = pd.Timestamp(next_date)
    candidates = calendar_candidates(pd.DatetimeIndex([dt]))

    matched: list[dict] = []
    for _, row in significant_conditions.iterrows():
        cname = row['日付条件']
        mask_arr = candidates.get(cname)
        if mask_arr is not None and bool(mask_arr[0]):
            matched.append({'日付条件': cname, '効果量': float(row['効果量'])})

    if not matched:
        return None
    best = max(matched, key=lambda m: m['効果量'])
    return {'値': best['効果量'], '該当条件': matched}

