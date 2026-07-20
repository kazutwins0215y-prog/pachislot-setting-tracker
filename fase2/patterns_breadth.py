"""
patterns_breadth.py — 幅型スコア(S_全台系/S_新台増台/S_移動台)と
機種強さ軸・全台系/高配分の日次判定(z版/Fisher版)
(2026-07-19にpatterns.pyから分割。利用側は `import patterns as pt` のfacade経由を推奨)
"""
import pandas as pd
import numpy as np
from scipy import stats

from patterns_common import SHORT_WINDOW, benjamini_hochberg

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


# ── 機種強さ軸・少台数機種向けFisher版 [1.8.1節・2026-07-14実装] ──────────
# 2026-07-14の実データ確認(東中野/東宝/高円寺/プレサス4店舗)により、θ=0.5は
# 店舗背景hot率(平均18〜20%)とほぼ整合し、n=2の「全台hot」検出はFisher版が
# 現行z検定の2〜5倍を捕捉(例: 東中野n=2は40件中z検定6件→Fisher32件)。
# 一方n=1は現行z・Fisher双方とも有意0件(単台の有意差検定は原理的に不可能なことを
# 実データでも確認)のため対象外とし、判定は常に'普段どおり'とする。
# 並走記録専用(0節の検証ゲート未通過。合成スコア・表示には使わない)。

JUDGMENT_FISHER_THETA = 0.5    # hot判定閾値(店舗背景hot率と整合、実データ確認済み)
JUDGMENT_FISHER_MIN_N = 2      # n=1は単日有意性検定が不可能なため対象外(実データ確認済み)


def score_zentaikei_judgment_fisher(
    df: pd.DataFrame,
    theta: float = JUDGMENT_FISHER_THETA,
    min_n: int = JUDGMENT_FISHER_MIN_N,
) -> pd.DataFrame:
    """
    機種×日×ホールの全台系/高配分判定のFisher直接確率検定版(1.8.1節、並走記録専用)。

    現行のscore_zentaikei_judgment(z検定、固定prior=0.15基準)はn=2〜4の少台数機種で
    検出力不足(high_probが0.6程度で飽和しz≥2に届きにくい)。本関数は各台をhigh_prob>=theta
    で二値化(hot/not)し、機種内のhot数kを「同日・同ホールの店内他台(自機種を除く)のhot率」
    を対照群としたFisher直接確率検定(片側、超過方向)で評価する。対照群を同日店内実測にする
    ことで、店舗が強い日に全機種のzが底上げされる店舗オフセット問題を自動的に吸収する
    (1.8.2節のC案型の日次差分と同じ思想)。

    n<min_n(既定2、すなわちn=1)は検定を行わず常に'普段どおり'とする(1台の有意差検定は
    背景率と単一ベルヌーイ試行の比較になり原理的に有意になり得ないことを理論・実データ
    両面で確認済み。1台機種を含む店の全台系識別は今後の実装予定.md 1.8.1節の
    「識別モード」将来項目で別途扱う)。**n<min_nの機種はFDR補正の対象母集団(その日の
    検定家族)からも除外する**(2026-07-14実装時に発覚: 常にp=1.0のダミーとして家族に
    含めるとBenjamini-Hochbergのしきい値i/m×αのmだけが不当に増え、実在する候補の検出力を
    削ってしまう。東中野の実データで検証したところ全期間で有意1件のみに落ち込む過剰補正を
    確認したため、テストしない仮説は最初から家族に入れない設計に修正した)。

    判定ラベル: FDR有意 かつ k==n(全台hot) → '全台系'、FDR有意 かつ k<n → '高配分'、
    それ以外 → '普段どおり'(score_zentaikei_judgmentと同じ3値スキーマ)。

    dfはis_invalid列を持つことを想定(preprocess.mark_invalid適用後)。

    Returns:
        DataFrame: ホール名, 日付, 機種名, 台数, hot台数, p値, 投入率, 判定ラベル
        (機種×日×ホール粒度、1行1組)
    """
    if 'is_invalid' in df.columns:
        valid = df[~df['is_invalid'].fillna(True)]
    else:
        valid = df

    cols = ['ホール名', '日付', '機種名', '台数', 'hot台数', 'p値', '投入率', '判定ラベル']
    valid = valid.dropna(subset=['high_prob'])
    if valid.empty:
        return pd.DataFrame(columns=cols)

    valid = valid.copy()
    valid['_hot'] = valid['high_prob'] >= theta

    group_key = ['ホール名', '日付', '機種名']
    agg_df = (
        valid.groupby(group_key)
        .agg(台数=('high_prob', 'count'), hot台数=('_hot', 'sum'), 期待高設定台数=('high_prob', 'sum'))
        .reset_index()
    )
    if agg_df.empty:
        return pd.DataFrame(columns=cols)

    agg_df['投入率'] = agg_df['期待高設定台数'] / agg_df['台数']
    agg_df['判定ラベル'] = JUDGMENT_LABEL_NORMAL
    agg_df['p値'] = np.nan

    daily_totals = (
        valid.groupby(['ホール名', '日付'])
        .agg(店舗台数=('high_prob', 'count'), 店舗hot数=('_hot', 'sum'))
    )

    for (hole, date), sub in agg_df.groupby(['ホール名', '日付']):
        try:
            n_total, k_total = daily_totals.loc[(hole, date)]
        except KeyError:
            continue

        # n<min_nの機種は検定自体を行わない(=FDR家族に入れない)。
        # ダミーp値で家族に含めると母数mだけ増えBH閾値i/m×αが不当に厳しくなるため
        testable_idx = [
            row_idx for row_idx in sub.index
            if int(agg_df.at[row_idx, '台数']) >= min_n
        ]
        if not testable_idx:
            continue

        p_values = []
        for row_idx in testable_idx:
            n = int(agg_df.at[row_idx, '台数'])
            k = int(agg_df.at[row_idx, 'hot台数'])
            n_bg = int(n_total) - n
            k_bg = int(k_total) - k
            if n_bg <= 0 or k_bg < 0:
                p_values.append(1.0)
                continue
            table = [[k, n - k], [k_bg, n_bg - k_bg]]
            try:
                _, p = stats.fisher_exact(table, alternative='greater')
            except Exception:
                p = 1.0
            p_values.append(p)

        for row_idx, p in zip(testable_idx, p_values):
            agg_df.at[row_idx, 'p値'] = p

        fdr_flags = benjamini_hochberg(p_values)
        for row_idx, is_fdr_sig in zip(testable_idx, fdr_flags):
            if not is_fdr_sig:
                continue
            n = int(agg_df.at[row_idx, '台数'])
            k = int(agg_df.at[row_idx, 'hot台数'])
            agg_df.at[row_idx, '判定ラベル'] = (
                JUDGMENT_LABEL_ZENTAIKEI if k == n else JUDGMENT_LABEL_KOUHAIBUN
            )

    return agg_df[cols]

