"""
patterns_events.py — 台移動/撤去/増台の検出・導入イベント判別・導入後カーブ検定
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
)

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


# ── 導入後イベント判別(今後の実装予定.md 1.8.3節「導入後カーブ」2026-07-13設計確定) ──

INTRODUCTION_ABSENCE_THRESHOLD = 7  # 再導入判定・減台の遅延確定・増台側の復帰ガードで
                                     # 共通利用する定数(店舗観測日ベース)

INTRODUCTION_CATEGORIES = ['新台', '増台', '減台', '再導入', '純移動']


def _all_confirmed_absent(
    by_date: pd.Series,
    origin_idx: int,
    store_dates: list,
    units: list[int],
    absence_threshold: int,
) -> bool:
    """
    unitsの全台が、store_dates上でorigin_idx直後からabsence_threshold日分の間、
    一度も現れないかを確認する(減台の遅延確定用)。未来日数が足りない場合(直近の
    減台候補)は確定不可としてFalseを返す(次回の全履歴再計算時に改めて判定される)。
    """
    window_end = origin_idx + absence_threshold
    if window_end >= len(store_dates):
        return False
    unit_set = set(units)
    for future_idx in range(origin_idx + 1, window_end + 1):
        future_units = by_date.get(store_dates[future_idx], set())
        if unit_set & future_units:
            return False
    return True


def detect_introduction_events(
    df: pd.DataFrame,
    hole_name: str,
    absence_threshold: int = INTRODUCTION_ABSENCE_THRESHOLD,
) -> pd.DataFrame:
    """
    指定ホールの機種レベルイベント(新台/増台/減台/再導入/純移動)を判別する
    (今後の実装予定.md 1.8.3節。既存detect_events(移動台検出、K=0前日比較)は
    変更せず本関数を新設する)。

    「店舗観測日」(このホールの全機種横断のユニーク日付)を基準タイムラインとし、
    機種ごとに台番号集合が非空だった日だけを辿って前回在籍日との差分を見る
    (欠測日は自然にスキップされる)。

    判定ロジック:
    - 初出日: 店舗収集開始日(店舗観測日の先頭)と同じなら'判別不能'(左打ち切り、
      カーブ学習除外)、それ以外は'新台'
    - 前回在籍日との店舗観測日ギャップ >= absence_threshold: '再導入'
      (不在日数を記録、baselineはリセット)
    - 通常比較(ギャップ < absence_threshold): disappeared/appearedをdetect_eventsと
      同じmin()ペアリングで移動判定。appeared側は「直近absence_threshold店舗観測日
      以内に在籍していた台」を復帰として除外(増台側の欠測ノイズガード)。
      除外後の純増減が0かつペアありなら'純移動'、純増なら'増台'(移動フラグ=ペアあり)、
      純減なら'減台'候補として以後absence_threshold店舗観測日以内に消えた台が
      1台も戻らないことを全数確認できた場合のみ確定記録(1台でも復帰したら今回は
      イベントなし扱い)。日次実行では毎回全履歴を再計算するため、直近の減台候補は
      未来日が足りず自然に保留され、翌日以降の再実行で確定する(pending状態を
      別途持つ必要がない)
    - 機種が0台になったまま二度と戻らない「全撤去」は、対応する present day が
      存在しないためイベント行を生成しない(カーブ学習に使う「後」の系列が
      そもそも存在しないため対象外。再導入判定は不在日数の起点として
      直前在籍日を引き続き使うため影響なし)

    Returns:
        DataFrame: 日付, ホール名, 機種名, カテゴリ, 台数変化, 移動フラグ,
                   台番号リスト, 移動台番号リスト, 不在日数
        - カテゴリ: '新台'/'増台'/'減台'/'再導入'/'純移動'/'判別不能'
        - 台数変化: len(当日台数) - len(前回在籍日台数) (復帰ガード適用前の実数)
        - 台番号リスト: 当日のその機種の在籍台番号(全台)
        - 移動台番号リスト: 移動フラグ=True の行のみ、実際に移動した(=新規に現れた側の)
          台番号(detect_eventsの移動台番号と同じ考え方。純移動カーブ検定が使う)
        - 不在日数: '再導入'行のみ店舗観測日ベースの不在日数、他はNaN
    """
    columns = [
        '日付', 'ホール名', '機種名', 'カテゴリ', '台数変化', '移動フラグ',
        '台番号リスト', '移動台番号リスト', '不在日数',
    ]

    hole_mask = df['ホール名'] == hole_name
    hole_df = df.loc[hole_mask, ['日付', '機種名', '台番号']].dropna().drop_duplicates()
    if hole_df.empty:
        return pd.DataFrame(columns=columns)

    store_dates = sorted(hole_df['日付'].unique())
    store_start = store_dates[0]
    date_idx = {d: i for i, d in enumerate(store_dates)}

    records = []

    for machine_name, g in hole_df.groupby('機種名', sort=False):
        by_date = g.groupby('日付')['台番号'].apply(lambda s: set(s.astype(int)))
        present_dates = sorted(by_date.index, key=lambda d: date_idx[d])

        prev_units: set[int] = set()
        prev_present_idx: int | None = None
        unit_last_seen_idx: dict[int, int] = {}

        for d in present_dates:
            curr_units = by_date[d]
            curr_idx = date_idx[d]

            if prev_present_idx is None:
                category = '判別不能' if d == store_start else '新台'
                records.append({
                    '日付': d, 'ホール名': hole_name, '機種名': machine_name,
                    'カテゴリ': category, '台数変化': len(curr_units), '移動フラグ': False,
                    '台番号リスト': sorted(curr_units), '移動台番号リスト': [], '不在日数': np.nan,
                })
            else:
                gap = curr_idx - prev_present_idx - 1
                count_delta = len(curr_units) - len(prev_units)

                if gap >= absence_threshold:
                    records.append({
                        '日付': d, 'ホール名': hole_name, '機種名': machine_name,
                        'カテゴリ': '再導入', '台数変化': count_delta, '移動フラグ': False,
                        '台番号リスト': sorted(curr_units), '移動台番号リスト': [], '不在日数': gap,
                    })
                    prev_units = set()  # 再導入後は連続比較の起点をリセット
                else:
                    disappeared = prev_units - curr_units
                    appeared_raw = curr_units - prev_units
                    revived = {
                        u for u in appeared_raw
                        if u in unit_last_seen_idx
                        and (curr_idx - unit_last_seen_idx[u]) < absence_threshold
                    }
                    net_appeared = appeared_raw - revived
                    n_moved = min(len(disappeared), len(net_appeared))
                    net_change = len(net_appeared) - len(disappeared)
                    moved_flag = n_moved > 0
                    moved_units = sorted(net_appeared)[:n_moved]

                    if net_change == 0:
                        if moved_flag:
                            records.append({
                                '日付': d, 'ホール名': hole_name, '機種名': machine_name,
                                'カテゴリ': '純移動', '台数変化': count_delta, '移動フラグ': True,
                                '台番号リスト': sorted(curr_units), '移動台番号リスト': moved_units,
                                '不在日数': np.nan,
                            })
                        # net_change==0かつmoved_flag=False: 実質変化なし(復帰のみ含む) → イベントなし
                    elif net_change > 0:
                        records.append({
                            '日付': d, 'ホール名': hole_name, '機種名': machine_name,
                            'カテゴリ': '増台', '台数変化': count_delta, '移動フラグ': moved_flag,
                            '台番号リスト': sorted(curr_units), '移動台番号リスト': moved_units,
                            '不在日数': np.nan,
                        })
                    else:
                        excess_removed = sorted(disappeared)[n_moved:]
                        if excess_removed and _all_confirmed_absent(
                            by_date, curr_idx, store_dates, excess_removed, absence_threshold,
                        ):
                            records.append({
                                '日付': d, 'ホール名': hole_name, '機種名': machine_name,
                                'カテゴリ': '減台', '台数変化': count_delta, '移動フラグ': moved_flag,
                                '台番号リスト': sorted(curr_units), '移動台番号リスト': moved_units,
                                '不在日数': np.nan,
                            })
                        # 未確定(復帰あり、または未来日不足)ならイベントなし

            for u in curr_units:
                unit_last_seen_idx[u] = curr_idx
            prev_units = curr_units
            prev_present_idx = curr_idx

    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(records, columns=columns)


# ── 導入後カーブ(今後の実装予定.md 1.8.3節「導入後カーブ」2026-07-13設計確定) ──────

INTRODUCTION_BIN_ORDER = ['初日', '2〜3日', '4〜7日', '8〜14日', '15日以降']
# 15日以降は看板機種交絡の兆候検知用に検定・保存はするが、翌日予測には使わない
INTRODUCTION_PREDICTABLE_BINS = frozenset(INTRODUCTION_BIN_ORDER[:-1])


def _introduction_elapsed_bin(elapsed_days: int) -> str:
    """経過日数(暦日、イベント当日=0)を導入後カーブのビン名へ変換する。"""
    if elapsed_days <= 0:
        return '初日'
    if elapsed_days <= 2:
        return '2〜3日'
    if elapsed_days <= 6:
        return '4〜7日'
    if elapsed_days <= 13:
        return '8〜14日'
    return '15日以降'


def introduction_curve_test(
    df: pd.DataFrame,
    hole_name: str,
    events_df: pd.DataFrame,
    min_days: int = GROUP_CALENDAR_MIN_DAYS,
) -> pd.DataFrame:
    """
    導入後カーブの検定(今後の実装予定.md 1.8.3節)。カテゴリ5種(新台/増台/減台/
    再導入/純移動)×経過日数ビン5種(初日/2〜3日/4〜7日/8〜14日/15日以降)=
    最大25仮説を店舗単位でBH補正する(group_constant_testの「行を経過日数ビンで
    絞った」版、両側検定にする点のみ異なる)。

    events_df(detect_introduction_eventsの出力。'判別不能'は対象外なので自動で
    除外する)の各イベントについて、その日から「同じ機種の次のイベントの前日」
    または(次イベントが無ければ)データ末尾までを追跡ウィンドウとする
    (次イベントの影響を古いイベントのカーブに混ぜないため)。

    ウィンドウ内の日ごとに対応ペアの差分を蓄積する:
    - 純移動: 移動台(移動台番号リスト)のhigh_prob − 同日店舗全体平均(台単位、自身除く)
    - それ以外4カテゴリ: 機種の日次投入率(Σhigh_prob/n) − 同日の他機種平均投入率
      (group_constant_testと同じ「自グループ vs 同日他グループ平均」の対応ペア化)
    店舗内の同カテゴリ全イベントの差分を1つのサンプル集団にプールする
    (「店舗単位で学習」2026-07-13確定設計)。

    カテゴリ×ビンごとにWilcoxon符号順位検定(**両側**)+符号付きrank-biserial
    効果量を計算し、店舗内25仮説をBH補正する(対照はA案=素の対店内差で開始、
    15日以降ビンも検定・保存して看板機種交絡の兆候検知に使う)。両側にするのは
    「この店は新台に入れない」という負のカーブも回避情報として検出するため
    (BH有意ゲートは|効果量|>=EFFECT_SIZE_THRESHOLDで正負どちらも拾う)。

    Returns:
        DataFrame(グループ種別='導入後', グループ=カテゴリ名, 日付条件=ビン名,
                   該当日数=ペア数, 対照日数=NaN, p_raw, 効果量, BH有意)
        (ホール名・使用データ最終日は呼び出し側で付与する)
    """
    columns = ['グループ種別', 'グループ', '日付条件', '該当日数', '対照日数', 'p_raw', '効果量', 'BH有意']
    empty = pd.DataFrame(columns=columns)

    if events_df.empty:
        return empty
    events = events_df[events_df['カテゴリ'].isin(INTRODUCTION_CATEGORIES)].copy()
    if events.empty:
        return empty

    mask = df['ホール名'] == hole_name
    if 'is_invalid' in df.columns:
        mask &= ~df['is_invalid'].fillna(True)
    sub = df.loc[mask].dropna(subset=['high_prob', '日付']).copy()
    if sub.empty:
        return empty

    # 機種×日の投入率(4カテゴリ用。他機種平均は「日合計から自分を引く」O(1)方式)
    daily = sub.groupby(['機種名', '日付'])['high_prob'].agg(n='count', sum_hp='sum').reset_index()
    daily['投入率'] = daily['sum_hp'] / daily['n']
    rate_lookup = {(r['機種名'], r['日付']): r['投入率'] for _, r in daily.iterrows()}
    day_rate_agg = daily.groupby('日付')['投入率'].agg(sum_rate='sum', cnt='count')
    day_rate_sum = day_rate_agg['sum_rate'].to_dict()
    day_rate_cnt = day_rate_agg['cnt'].to_dict()

    # 台×日のhigh_prob、店舗全体(台単位)の同日平均(純移動用)
    unit_hp_lookup = sub.groupby(['機種名', '台番号', '日付'])['high_prob'].mean().to_dict()
    day_unit_agg = sub.groupby('日付')['high_prob'].agg(sum_hp='sum', cnt='count')
    day_unit_sum = day_unit_agg['sum_hp'].to_dict()
    day_unit_cnt = day_unit_agg['cnt'].to_dict()

    # 機種ごとの次イベント日(追跡ウィンドウの打ち切り境界)
    events = events.sort_values(['機種名', '日付'])
    events['_次イベント日'] = events.groupby('機種名')['日付'].shift(-1)

    all_dates = sorted(sub['日付'].dropna().unique())

    diffs_by_key: dict[tuple[str, str], list[float]] = {}

    for _, ev in events.iterrows():
        category = ev['カテゴリ']
        machine = ev['機種名']
        event_date = ev['日付']
        window_end = ev['_次イベント日']
        window_dates = [
            d for d in all_dates
            if d >= event_date and (pd.isna(window_end) or d < window_end)
        ]
        if not window_dates:
            continue
        event_ts = pd.Timestamp(event_date)

        if category == '純移動':
            moved_units = ev['移動台番号リスト']
            if not isinstance(moved_units, (list, np.ndarray)) or len(moved_units) == 0:
                continue
            for unit in moved_units:
                unit_i = int(unit)
                for d in window_dates:
                    own = unit_hp_lookup.get((machine, unit_i, d))
                    cnt = day_unit_cnt.get(d, 0)
                    if own is None or pd.isna(own) or cnt <= 1:
                        continue
                    other_mean = (day_unit_sum[d] - own) / (cnt - 1)
                    diff = own - other_mean
                    bin_name = _introduction_elapsed_bin((pd.Timestamp(d) - event_ts).days)
                    diffs_by_key.setdefault((category, bin_name), []).append(diff)
        else:
            for d in window_dates:
                own = rate_lookup.get((machine, d))
                cnt = day_rate_cnt.get(d, 0)
                if own is None or pd.isna(own) or cnt <= 1:
                    continue
                other_mean = (day_rate_sum[d] - own) / (cnt - 1)
                diff = own - other_mean
                bin_name = _introduction_elapsed_bin((pd.Timestamp(d) - event_ts).days)
                diffs_by_key.setdefault((category, bin_name), []).append(diff)

    records = []
    for category in INTRODUCTION_CATEGORIES:
        for bin_name in INTRODUCTION_BIN_ORDER:
            diffs = diffs_by_key.get((category, bin_name), [])
            k = len(diffs)
            if k < min_days:
                records.append({
                    'グループ種別': '導入後', 'グループ': category, '日付条件': bin_name,
                    '該当日数': k, '対照日数': np.nan, 'p_raw': np.nan, '効果量': np.nan,
                })
                continue
            diffs_arr = np.array(diffs, dtype=float)
            if np.all(diffs_arr == 0.0):
                records.append({
                    'グループ種別': '導入後', 'グループ': category, '日付条件': bin_name,
                    '該当日数': k, '対照日数': np.nan, 'p_raw': 1.0, '効果量': 0.0,
                })
                continue
            _, p = stats.wilcoxon(diffs_arr, alternative='two-sided')
            rbc = _wilcoxon_rank_biserial(diffs_arr)
            records.append({
                'グループ種別': '導入後', 'グループ': category, '日付条件': bin_name,
                '該当日数': k, '対照日数': np.nan, 'p_raw': float(p), '効果量': rbc,
            })

    result = pd.DataFrame(records, columns=columns[:-1])
    testable = result['p_raw'].notna()
    flags = benjamini_hochberg(result.loc[testable, 'p_raw'].tolist())
    result['BH有意'] = False
    result.loc[testable, 'BH有意'] = [
        bool(f) and abs(eff) >= EFFECT_SIZE_THRESHOLD
        for f, eff in zip(flags, result.loc[testable, '効果量'])
    ]
    return result


def predict_introduction_next_day(
    category: str,
    elapsed_days: int,
    significant_conditions: pd.DataFrame,
) -> dict | None:
    """
    [今後の実装予定.md 1.8.3節「導入後カーブ」] S_導入後の翌観測日予測。
    末尾版/機種版と異なりカテゴリは店舗単位で1本しかないため、重複統合
    (max効果量採用)ロジックは不要(該当する(カテゴリ,ビン)は最大1行)。
    15日以降ビン(elapsed_days>=14)は看板機種交絡チェック用の保存のみで
    予測対象外のため常にNoneを返す。

    significant_conditions: introduction_curve_testの出力のうちBH有意=Trueの行
    (この店舗・この使用データ最終日分。呼び出し側でフィルタして渡す)。

    Returns:
        {'値': float(効果量), '該当条件': {'カテゴリ','経過ビン','効果量'}}
        該当する有意条件が無ければNone(予測不可)。
    """
    if significant_conditions.empty or elapsed_days < 0:
        return None
    bin_name = _introduction_elapsed_bin(elapsed_days)
    if bin_name not in INTRODUCTION_PREDICTABLE_BINS:
        return None

    row = significant_conditions[
        (significant_conditions['グループ'] == category)
        & (significant_conditions['日付条件'] == bin_name)
    ]
    if row.empty:
        return None
    eff = float(row.iloc[0]['効果量'])
    return {'値': eff, '該当条件': {'カテゴリ': category, '経過ビン': bin_name, '効果量': eff}}

