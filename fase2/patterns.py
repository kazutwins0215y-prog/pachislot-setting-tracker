"""
patterns.py — イベント検出・パターンスコア・αブレンド

移動台検出 (detect_events / detect_all_events / is_new_series):
    K=0前日比較で移動/撤去/増台イベントを検出
幅型パターン (score_zentaiki / score_shintai / score_idoudai):
    S_全台系 / S_新台増台 / S_移動台 — 1日分データで検出可能
機種強さ軸・全台系/高配分の日次判定 (score_zentaikei_judgment):
    機種×日×ホールでz・投入率・S_全台系から3値ラベル(全台系/高配分/普段どおり)を判定
機種単位の癖分析 (machine_group / group_constant_test / predict_machine_group_next_day):
    グループ=機種で末尾版の検出器(group_calendar_test/match_rule_test)を再利用し、
    看板機種(恒常検定)+機種カレンダー癖を恒常窓/直近90日窓の2窓で検定
深さ型パターン (score_teppandai / score_rotation / score_sueki_daily):
    S_鉄板台(ACF→PDM→Lomb-Scargle / カレンダー検定) / S_ローテ / S_据え置き(日次判定)
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

        # 基準値が計算不能(=この店舗×機種に有効行がない)なら検出不可として除外する。
        # 旧実装は0.5を代入していたが、Stage3のβ₀導入(事前確率π=0.15)後は0.5が
        # 「中立」を意味しなくなったため、虚構の基準値は作らない方針に統一(2026-07)
        baseline = baseline_map.get((hole, machine))
        if baseline is None or pd.isna(baseline):
            continue

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
                raw = trend - baseline
                # 正規化: 差が±0.5でscore=±1.0に到達。基準以下に沈む(弱い)場合は負値
                scores[idx] = float(np.clip(raw / 0.5, -1.0, 1.0))

    return scores


def score_shintai(
    df: pd.DataFrame,
    events_df: pd.DataFrame,
    window: int = SHORT_WINDOW,
) -> pd.Series:
    """
    S_新台増台: 増台後の直近移動平均 - 基準値 → clip(-1, 1)。
    基準以上に強ければ正、基準以下に沈んでいれば負(弱い)。
    配分が落ち着くと差が縮み自動フェードアウト。
    """
    return _compute_event_scores(df, events_df, '増台台番号', window)


def score_idoudai(
    df: pd.DataFrame,
    events_df: pd.DataFrame,
    window: int = SHORT_WINDOW,
) -> pd.Series:
    """
    S_移動台: 移動後の直近移動平均 - 同機種店舗全体平均 → clip(-1, 1)。
    基準以上に強ければ正、基準以下に沈んでいれば負(弱い)。
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


# ── 機種強さ軸・全台系/高配分の日次判定 [2026-07-09設計合意] ──────────

JUDGMENT_Z_THRESHOLD = 2.0             # zスコアの有意性閾値(統計理論値)
JUDGMENT_ZENTAIKEI_S_THRESHOLD = 0.5   # S_全台系の全台系判定閾値(仮置き。Phase2で調整)

JUDGMENT_LABEL_ZENTAIKEI = '全台系'
JUDGMENT_LABEL_KOUHAIBUN = '高配分'
JUDGMENT_LABEL_NORMAL = '普段どおり'


def score_zentaikei_judgment(
    df: pd.DataFrame,
    prior: float,
    z_threshold: float = JUDGMENT_Z_THRESHOLD,
    s_threshold: float = JUDGMENT_ZENTAIKEI_S_THRESHOLD,
) -> pd.DataFrame:
    """
    機種×日×ホールでzスコア・投入率・S_全台系を算出し、3値の判定ラベル
    (全台系/高配分/普段どおり)を付与する(今後の実装予定.md 1.8節)。

    z = (Σhigh_prob - n×prior) / √(n×prior×(1-prior))。期待より上振れしているかの
    片側検定として扱い、同日×同ホール内の全機種でbenjamini_hochbergによるFDR補正を
    重ねる(1日に全機種同時判定する分の誤検出増を抑えるため)。z_threshold以上かつ
    FDR有意の場合のみ「異常」とし、S_全台系(揃いの確認)で全台系/高配分に振り分ける。
    S_全台系が算出不能(機種内有効台数<2、score_zentaiki参照)の場合は揃い判定不能として
    高配分側に倒す(2026-07-09ユーザー合意: 3値のシンプルさを優先)。

    dfはcompute_breadth_scores適用後(S_全台系列を持つ)を想定。
    投入率は生の比率(Σhigh_prob÷n、0〜1)で返す。表示側(機能A)の×6は呼び出し元の責務
    (3節の正解発表ラベルの投入率=k/nと直接比較できる粒度で保存するため)。

    Returns:
        DataFrame: ホール名, 日付, 機種名, 台数, 期待高設定台数, zスコア, p値, 投入率,
        S_全台系, 判定ラベル (機種×日×ホール粒度、1行1組)
    """
    if 'is_invalid' in df.columns:
        valid = df[~df['is_invalid'].fillna(True)]
    else:
        valid = df

    cols = [
        'ホール名', '日付', '機種名', '台数', '期待高設定台数',
        'zスコア', 'p値', '投入率', 'S_全台系', '判定ラベル',
    ]
    group_key = ['ホール名', '日付', '機種名']
    agg_df = (
        valid.dropna(subset=['high_prob'])
        .groupby(group_key)
        .agg(台数=('high_prob', 'count'), 期待高設定台数=('high_prob', 'sum'), S_全台系=('S_全台系', 'first'))
        .reset_index()
    )
    if agg_df.empty:
        return pd.DataFrame(columns=cols)

    n = agg_df['台数'].astype(float)
    sigma = np.sqrt(n * prior * (1.0 - prior))
    agg_df['zスコア'] = np.where(sigma > 0, (agg_df['期待高設定台数'] - n * prior) / sigma, np.nan)
    agg_df['投入率'] = agg_df['期待高設定台数'] / n
    # 片側検定(上振れのみ): z=NaNの行(sigma=0、n=0で通常発生しない)はp値もNaN
    agg_df['p値'] = agg_df['zスコア'].apply(lambda z: float(stats.norm.sf(z)) if pd.notna(z) else np.nan)
    agg_df['判定ラベル'] = JUDGMENT_LABEL_NORMAL

    for _, sub in agg_df.groupby(['ホール名', '日付']):
        p_values = sub['p値'].fillna(1.0).tolist()
        fdr_flags = benjamini_hochberg(p_values)
        for row_idx, is_fdr_sig in zip(sub.index, fdr_flags):
            z = agg_df.at[row_idx, 'zスコア']
            if pd.isna(z) or z < z_threshold or not is_fdr_sig:
                continue
            s = agg_df.at[row_idx, 'S_全台系']
            agg_df.at[row_idx, '判定ラベル'] = (
                JUDGMENT_LABEL_ZENTAIKEI if pd.notna(s) and s >= s_threshold else JUDGMENT_LABEL_KOUHAIBUN
            )

    return agg_df[cols]


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


GROUP_CALENDAR_MIN_DAYS = 5  # 候補日・対照日ともにこの日数未満の組み合わせは検定対象外


# ── 機種単位の癖分析(今後の実装予定.md 1.8節「機種単位の癖分析」) ──────────

MACHINE_GROUP_MIN_UNITS = 2   # 日次有効台数の中央値がこれ未満の機種は検定対象外
RECENT_TEST_WINDOW_DAYS = 90  # 看板機種/機種カレンダーの「直近窓」検定用の日数(暫定値)。
                               # αブレンド投影用のSHORT_WINDOW_DEFAULT(=30)とは別物
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
        # 負側はr̄_t=0で-1に飽和する暫定式。店舗が日次ほぼ無記憶(r̄≈0)の実態では大半の日が
        # -1近傍になり店舗平均s_suekiが大きく負に沈むが、prediction_accuracyの成績を見てから
        # 調整する方針で現状維持と決定(2026-07-08。調整候補: NEGATIVE_SCALE乗算での緩和)
        signed = np.where(
            np.isnan(r_bar),
            np.nan,
            np.where(
                r_bar >= SUEKI_DAILY_THRESHOLD,
                r_bar,
                -np.minimum(1.0, (SUEKI_DAILY_THRESHOLD - r_bar) / SUEKI_DAILY_THRESHOLD),
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
        short_scores = score_sueki_daily(short_df)
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
