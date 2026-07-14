"""
preprocess.py — DB読み込みから個台スコア(high_prob)まで (Stage 0〜4-1)

Stage 0  (load_slot_data / normalize):
    SQLiteからslot_dataを読み込み、数値変換・ユニーク化
Stage 1a (judge_tier / resolve_rate_columns / estimate_bias_params / calibrate_all):
    機種別BB/RB/ART列のTier A/B/C判定 + AT中除外バイアス補正
    データ提供元(サイト)は店舗によってBB確率/RB確率/ART確率の使い方が異なる場合がある
    (例: 他店でBB確率に入るデータが、ある店舗ではART確率に入っている)ため、
    Tier Bチャンネルがどの列を参照すべきかを店舗の実データ(差枚率との相関)で
    都度検証する(resolve_rate_columns)。
Stage 2  (logLR_rng / logLR_sashimai / logLR_kaiten / compute_all_logLR):
    3チャンネルlogLR計算。差枚チャンネルは machine_setting_specs.json の payout が
    あれば理論値をそのまま優先使用し、無ければ従来通り経験的分位点にフォールバック
Stage 3  (sigmoid / compute_log_odds / load_weights):
    LogOdds重み付き合算 → high_prob(0〜1)
Stage 4  (mark_invalid / mark_rng_anomaly):
    回転数不足台日、およびTier A機種のRNGチャンネルで設定6理論値でも統計的に
    説明不能な観測値(データ提供元の表記ミス等) → is_invalid フラグ
Stage 4-1 (check_missing_bias / invalid_rate):
    欠損偏りガード(深さ型検定の前段チェックに使用)
"""
import sqlite3
import json
import logging
import pandas as pd
import numpy as np
from pathlib import Path
from scipy import stats

SPECS_PATH = Path(__file__).parent / 'machine_setting_specs.json'
WEIGHTS_PATH = Path(__file__).parent / 'weights.json'
BIN_CURVES_PATH = Path(__file__).parent / 'kaiten_bin_curves.json'
CHANNEL_WEIGHTS_PATH = Path(__file__).parent / 'stage3_channel_weights.json'
MACHINE_BIAS_DELTA_PATH = Path(__file__).parent / 'machine_bias_delta.json'

INVALID_THRESHOLD = 5           # 期待発生回数の下限
MISSING_BIAS_THRESHOLD = 0.12   # 判定不能率差の初期閾値(12pt)
BET_PER_GAME = 3                # パチスロ標準ベット枚数(機械割⇔差枚/Gの換算に使用)
KAITEN_ZSCORE_MIN_DAYS = 5      # 台内zスコアに必要な最低履歴日数

# w3=0.0: 回転数チャンネルは既定で無効。
# 旧実装のStage1b/5は「logLR_rngから作ったカーブをlogLR_rngで検証する」循環学習で、
# RNG証拠の二重計上になっていたため停止した(2026-07)。multi_store.pyの
# LOSO交差検証(validate_kaiten_channel)に合格した場合のみ、学習済みw3>0が
# stage3_channel_weights.jsonに書き込まれて有効化される。
_DEFAULT_CHANNEL_WEIGHTS = {'w1': 1.0, 'w2': 0.5, 'w3': 0.0}
_DEFAULT_ORTH_PARAMS = {'orth_a': 0.0, 'orth_b': 0.0}

# 事前確率π: 「証拠ゼロの台日が高設定である確率」(店舗の高設定投入率の事前推定)。
# Stage3のLogOddsに切片 β₀ = ln(π/(1-π)) として入る。旧実装はβ₀なし(=暗黙にπ=0.5)で、
# 全証拠がゼロの行がhigh_prob=0.5となり、店舗集計E[高設定台数]/Nが0.5近辺の狭帯域に
# 集中する原因だった(上限キャリブレーションの過剰発動の根本原因、2026-07判明)。
# 0.15は業界感覚(高設定投入は多くて1〜2割)に基づく暫定値。stage3_channel_weights.jsonの
# prior_high_ratioで上書き可能。将来は店舗別に学習する(上限モデルStep2/Kalman参照)。
DEFAULT_PRIOR_HIGH_RATIO = 0.15

logger = logging.getLogger(__name__)

_specs_cache: dict | None = None


def _load_specs() -> dict:
    global _specs_cache
    if _specs_cache is None:
        _specs_cache = json.loads(SPECS_PATH.read_text(encoding='utf-8'))
    return _specs_cache


def load_bin_curves() -> dict:
    """
    multi_store.py(Stage1b/5)が学習した機種別デシルカーブを読み込む。
    未学習(ファイル未作成)の場合は空dict(= 全機種フォールバック0.0)を返す。
    """
    if BIN_CURVES_PATH.exists():
        return json.loads(BIN_CURVES_PATH.read_text(encoding='utf-8'))
    return {}


def load_channel_weights() -> dict:
    """
    multi_store.py(Stage5)が学習したStage3チャンネル重み(w1/w2/w3)と
    直交化パラメータ(orth_a/orth_b)、事前確率(prior_high_ratio)を読み込む。
    未学習の場合は既定値(w1=1.0, w2=0.5, w3=0.0=回転数チャンネル無効,
    prior_high_ratio=DEFAULT_PRIOR_HIGH_RATIO)を返す。
    w3はmulti_store.validate_kaiten_channel(LOSO交差検証)に合格した場合のみ正になる。
    """
    defaults = {
        **_DEFAULT_CHANNEL_WEIGHTS,
        **_DEFAULT_ORTH_PARAMS,
        'prior_high_ratio': DEFAULT_PRIOR_HIGH_RATIO,
    }
    if CHANNEL_WEIGHTS_PATH.exists():
        loaded = json.loads(CHANNEL_WEIGHTS_PATH.read_text(encoding='utf-8'))
        return {**defaults, **loaded}
    return defaults


def load_machine_bias_delta() -> dict[str, float]:
    """
    [今後の実装予定.md 1.8.5節「機種バイアス除外・案B」] multi_store.pyが全店舗横断で
    学習した機種ごとのlog_odds系統オフセットδ(機種名→delta)を読み込む。
    未学習(ファイル未作成)の場合は空dict(= 較正なし・全機種delta=0扱い)を返す。
    並走評価専用(S_機種_較正)でのみ使用し、本流のlog_odds/high_probには使わない。
    """
    if MACHINE_BIAS_DELTA_PATH.exists():
        raw = json.loads(MACHINE_BIAS_DELTA_PATH.read_text(encoding='utf-8'))
        return {k: float(v['delta']) for k, v in raw.items()}
    return {}


# ── Stage 0: 正規化 ──────────────────────────────────────────────

def load_slot_data(db_path: str, hole_name: str) -> pd.DataFrame:
    """slot_data テーブルを読み込み、数値型に変換したDataFrameを返す。"""
    con = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT * FROM slot_data WHERE ホール名 = ?",
            con,
            params=(hole_name,),
        )
    finally:
        con.close()

    for col in ['台番号', '回転数', '差枚', 'BB', 'RB', 'ART']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    for col in ['BB確率', 'RB確率', 'ART確率', '合成確率']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    ART列の確認・ユニーク化を行い、正規化済みDataFrameを返す。

    - ART列は多くの店舗で全行NULLだが、店舗によっては他店でBB/BB確率に入るデータが
      ART/ART確率列に入っている場合がある(データ提供元のページ仕様が店舗ごとに異なるため)。
      これ自体は異常ではなく、Stage1aのresolve_rate_columnsで店舗ごとに検証・吸収する。
    - (日付, ホール名, 機種名, 台番号) の重複を除去
    """
    if 'ART' in df.columns and df['ART'].notna().any():
        logger.info(
            'ART列に非NULL値が存在します。この店舗はBB/RB相当のデータをART列に'
            '格納している可能性があります(resolve_rate_columnsで自動検証されます)。'
        )

    # 台番号・回転数がNULLの行はプレースホルダーとして除外
    df = df.dropna(subset=['台番号', '回転数'])

    key_cols = ['日付', 'ホール名', '機種名', '台番号']
    df = df.drop_duplicates(subset=key_cols, keep='first').reset_index(drop=True)
    return df


# ── Stage 1a: Tier判定・バイアス補正 ──────────────────────────────

_RATE_COLUMNS = ('BB確率', 'RB確率', 'ART確率')
_RATE_CORR_MIN_SAMPLES = 30
_RATE_CORR_THRESHOLD = 0.5


def _rate_col_correlations(grp_with_rate: pd.DataFrame) -> list[tuple[str, float, int]]:
    """
    機種1件分のデータ(差枚率列を含む)について、候補確率列(BB確率/RB確率/ART確率)
    ごとの(列名, 差枚率との相関係数, サンプル数)を返す。サンプル不足時は r=0.0。
    """
    out = []
    for col in _RATE_COLUMNS:
        if col not in grp_with_rate.columns:
            continue
        sub = grp_with_rate.dropna(subset=[col, '差枚率'])
        n = len(sub)
        if n < _RATE_CORR_MIN_SAMPLES:
            out.append((col, 0.0, n))
            continue
        r, _ = stats.pearsonr(sub[col], sub['差枚率'])
        out.append((col, float(r), n))
    return out


def resolve_rate_columns(df: pd.DataFrame, machine_name: str) -> dict[str, str | None]:
    """
    'BB'/'RB'それぞれのTier Bチャンネルが実際にどの確率列(BB確率/RB確率/ART確率)を
    参照すべきかを、この店舗の実データ(差枚率との相関)で検証して決定する。

    データ提供元(サイト)は店舗ごとにBB確率/RB確率/ART確率の使い方が異なる場合がある
    (例: 他店でBB確率に入るデータが、ある店舗ではART確率に入っている)。
    machine_setting_specs.json のラベル(BB確率など)を機械的に信じるのではなく、
    相関が最も強い列を優先的に採用する。同一列を'BB'・'RB'で重複採用はしない。

    Returns:
        {'BB': 列名 or None, 'RB': 列名 or None}
        (None は、この店舗ではどの列も差枚率との相関が閾値に達しなかったことを示す)
    """
    grp = df[df['機種名'] == machine_name].dropna(subset=['回転数', '差枚']).copy()
    grp = grp[grp['回転数'] > 0]
    result: dict[str, str | None] = {'BB': None, 'RB': None}
    if grp.empty:
        return result
    grp['差枚率'] = grp['差枚'] / grp['回転数']

    scored = sorted(
        (c for c in _rate_col_correlations(grp) if c[2] >= _RATE_CORR_MIN_SAMPLES and abs(c[1]) >= _RATE_CORR_THRESHOLD),
        key=lambda c: abs(c[1]),
        reverse=True,
    )

    used: set[str] = set()
    for slot in ('BB', 'RB'):
        for col, _r, _n in scored:
            if col not in used:
                result[slot] = col
                used.add(col)
                break
    return result


def judge_tier(df: pd.DataFrame, machine_name: str) -> dict:
    """
    指定機種のBB/RB/ART列それぞれのTierを判定する。

    Tier A: 観測確率が machine_setting_specs.json の理論値と一致する列
    Tier B: 理論値未対応だが差枚率との相関 |r| > 0.5 の列
    Tier C: それ以外 → 切り捨て

    Returns:
        {'BB': 'A'|'B'|'C', 'RB': 'A'|'B'|'C', 'ART': 'C'}
    """
    specs = _load_specs()

    # specsに既知Tierが定義されていれば優先使用
    # (どの物理列を参照するかはresolve_rate_columnsが店舗の実データで別途検証する)
    if machine_name in specs and 'tier' in specs[machine_name]:
        return {**specs[machine_name]['tier'], 'ART': 'C'}

    # 未知機種: 差枚率との相関でTierを推定(BB確率/RB確率/ART確率のいずれが
    # 実質的なチャンネル列かをこの店舗の実データで検証して決める)
    resolved = resolve_rate_columns(df, machine_name)
    result: dict[str, str] = {ch: ('B' if resolved[ch] else 'C') for ch in ('BB', 'RB')}
    result['ART'] = 'C'
    return result


def estimate_bias_params(df: pd.DataFrame, machine_name: str) -> dict:
    """
    回転数チャンネルのAT中除外バイアス補正パラメータを推定する。
    AT機は当りが多い台ほど記録上の回転数が実態より少なくなる。
    回転数↔差枚率の相関から補正方向・強さを算出。

    Returns:
        {'direction': 1|-1, 'strength': float}
    """
    grp = df[df['機種名'] == machine_name].dropna(subset=['回転数', '差枚'])
    grp = grp[grp['回転数'] > 0]

    if len(grp) < 30:
        return {'direction': 0, 'strength': 0.0}

    r, _ = stats.pearsonr(grp['回転数'], grp['差枚'] / grp['回転数'])
    return {
        'direction': int(np.sign(r)) if abs(r) > 0.1 else 0,
        'strength': float(abs(r)),
    }


def calibrate_all(df: pd.DataFrame) -> tuple[dict, dict, dict]:
    """
    全機種に対してTier判定・バイアス補正・Tier B列の解決を実行する。

    column_map は、specs由来のTier判定機種も含めて、この店舗の実データで
    'BB'/'RB'チャンネルがどの物理列(BB確率/RB確率/ART確率)を参照すべきかを検証した結果。
    店舗によってBB確率/RB確率/ART確率の使い方が異なる場合があるため
    (例: 他店でBB確率に入るデータが、ある店舗ではART確率に入っている)、
    specsのラベルを機械的に信じずに毎回この店舗のデータで検証し直す。

    Returns:
        (machine_tier, bias_params, column_map)
    """
    machine_tier: dict = {}
    bias_params: dict = {}
    column_map: dict = {}
    for machine in df['機種名'].dropna().unique():
        machine_tier[machine] = judge_tier(df, machine)
        bias_params[machine] = estimate_bias_params(df, machine)
        column_map[machine] = resolve_rate_columns(df, machine)
    return machine_tier, bias_params, column_map


# ── Stage 2: 3チャンネルlogLR ────────────────────────────────────

def logLR_rng(k: int, n: int, p_s: float, p_baseline: float) -> float:
    """
    チャンネル①: RNG確率のlogLR (ポアソン近似)。
    k=当り回数, n=回転数, p_s=設定sの理論確率, p_baseline=低設定基準確率
    Tier A → 理論値を specs から取得 / Tier B → 同機種内パーセンタイル相対スコア

    logLR = k * ln(p_s / p_baseline) - n * (p_s - p_baseline)
    """
    if n <= 0 or p_s <= 0 or p_baseline <= 0:
        return 0.0
    return float(k * np.log(p_s / p_baseline) - n * (p_s - p_baseline))


def logLR_sashimai(diff: int, n: int, mu_s: float, sigma: float) -> float:
    """
    チャンネル②: 差枚(出玉)のlogLR (正規近似)。
    diff=差枚, n=回転数, mu_s=設定sの期待差枚/G, sigma=機種ボラティリティ

    ベースライン mu_baseline=0 を前提とした per-game 対数尤度比:
    logLR = (diff/n * mu_s - mu_s^2 / 2) / sigma^2

    [NOTE] O(n)の総尤度比ではなく per-game スケールに正規化。
    logLR_rng が n 依存で±10程度に収まるのに対し、O(n)のままでは
    差枚チャンネルが数千倍大きくなり sigmoid が飽和するため。
    Stage5の重み学習でスケール差を吸収する設計は変わらない。
    """
    if n <= 0 or sigma <= 0:
        return 0.0
    return float((diff / n * mu_s - mu_s ** 2 / 2) / sigma ** 2)


def logLR_kaiten(kaiten_zscore: float, machine_name: str, bin_curves: dict) -> float:
    """
    チャンネル③: 回転数行動のlogLR (経験的非線形logLR曲線)。
    台内zスコアをデシル分けし、実測曲線からlogLRを返す。
    低デシルは平坦・高デシルで急上昇という非対称性を想定。
    bin_curves が未学習の場合は 0.0 を返す。
    """
    curve = bin_curves.get(machine_name)
    if curve is None or len(curve) == 0:
        return 0.0
    percentile = float(stats.norm.cdf(kaiten_zscore))
    decile_idx = min(int(percentile * 10), 9)
    return float(curve[decile_idx])


def compute_kaiten_zscore(df: pd.DataFrame, min_days: int = KAITEN_ZSCORE_MIN_DAYS) -> pd.Series:
    """
    チャンネル③の入力: 台内(同一 ホール名×機種名×台番号)の回転数を
    その台自身の履歴で標準化したzスコア。「いつもよりよく回っている」を表す。

    履歴日数が min_days 未満、または標準偏差が0(全日同値)の台は NaN
    (信号なし。logLR_kaiten側でNaNは寄与0として扱われる)。
    """
    g = df.groupby(['ホール名', '機種名', '台番号'])['回転数']
    counts = g.transform('count')
    mu = g.transform('mean')
    sigma = g.transform('std')
    z = (df['回転数'] - mu) / sigma
    return z.where((counts >= min_days) & (sigma > 0))


def compute_logLR_kaiten_column(
    df: pd.DataFrame,
    bin_curves: dict,
    orth_a: float = 0.0,
    orth_b: float = 0.0,
) -> pd.Series:
    """
    チャンネル③: kaiten_zscore を機種別デシルカーブ(bin_curves、multi_store.pyの
    Stage1b/5で複数店舗データから学習)に通してlogLR_kaitenを求める。

    bin_curvesに機種が無い(未学習)場合、またはkaiten_zscoreがNaN(履歴不足)の
    場合は0.0(寄与なし)。df に 'kaiten_zscore' 列が無ければ自動計算する。

    orth_a/orth_b: 直交化パラメータ(multi_store.pyで学習)。0以外を渡すと、
    カーブ値から同一行のlogLR_rngで説明できる成分を除いた残差
    (curve − (orth_a + orth_b × logLR_rng)) を返す。カーブがlogLR_rngを教師に
    学習されている以上、素のカーブ値をStage3で加算するとRNG証拠の二重計上に
    なるため、有効化時は必ず直交化して「回転数だけが持つ独立成分」に絞る。
    直交化はカーブ値を持つ行(=シグナルのある行)にのみ適用する
    (シグナルの無い行に−(a+b·y1)を注入しないため)。
    """
    out = pd.Series(0.0, index=df.index)
    if not bin_curves:
        return out

    zscore = df['kaiten_zscore'] if 'kaiten_zscore' in df.columns else compute_kaiten_zscore(df)
    percentile = pd.Series(stats.norm.cdf(zscore.fillna(0.0)), index=df.index)
    decile = np.minimum((percentile * 10).astype(int), 9)

    use_orth = (orth_a != 0.0 or orth_b != 0.0) and 'logLR_rng' in df.columns

    for machine_name, grp_idx in df.groupby('機種名', sort=False).groups.items():
        curve = bin_curves.get(machine_name)
        if not curve:
            continue
        valid = zscore.loc[grp_idx].notna()
        target_idx = valid[valid].index
        if target_idx.empty:
            continue
        decile_vals = decile.loc[target_idx].to_numpy(dtype=int)
        vals = np.asarray(curve, dtype=float)[decile_vals]
        if use_orth:
            y1 = df.loc[target_idx, 'logLR_rng'].fillna(0.0).to_numpy(dtype=float)
            vals = vals - (orth_a + orth_b * y1)
        out.loc[target_idx] = vals

    return out


def _split_setting_keys(settings: dict) -> tuple[list, list]:
    """
    スペック表の設定キーを高設定側/低設定側に分割する。設定は機種によらず1〜6の
    共通尺度(2なし・3なし等の欠番あり)のため、設定4以上=高設定・設定3以下=低設定の
    固定境界で分ける。旧実装の「上位半分/下位半分」折半は欠番パターンによって境界が
    ズレていた(沖ドキ系(1,2,3,5,6)は設定3が高設定側に混入して基準が甘く、
    ピンクパンサーSP(1,4,5,6)は設定4が低設定側に混入して基準が過剰に厳しくなる。
    2026-07-14修正、対象6機種)。片側が空になる変則ラダーのみ従来の折半へ
    フォールバックする(現データには存在しない)。
    """
    keys = sorted(settings.keys(), key=lambda x: int(x))
    n = len(keys)
    if n < 2:
        return [], []
    high_keys = [k for k in keys if int(k) >= 4]
    low_keys = [k for k in keys if int(k) <= 3]
    if not high_keys or not low_keys:
        high_keys, low_keys = keys[n // 2:], keys[: n // 2]
    return high_keys, low_keys


def _tier_a_probs(machine_specs: dict, channel: str) -> tuple[float, float] | tuple[None, None]:
    """Tier A機種の高設定平均確率(p_s)・低設定平均確率(p_baseline)を返す。"""
    settings = machine_specs.get('settings', {})
    if not settings:
        return None, None
    high_keys, low_keys = _split_setting_keys(settings)
    if not high_keys:
        return None, None
    vals_high = [settings[k][channel] for k in high_keys if channel in settings[k]]
    vals_low = [settings[k][channel] for k in low_keys if channel in settings[k]]
    if not vals_high or not vals_low:
        return None, None
    p_s = float(np.mean(vals_high))
    p_bl = float(np.mean(vals_low))
    return (p_s, p_bl) if p_s > 0 and p_bl > 0 else (None, None)


def _payout_mu_high(machine_specs: dict) -> float | None:
    """
    機種別理論値差表の payout(機械割)から、高設定側代表値の理論期待差枚/Gを返す。
    _tier_a_probs と同じ設定4以上/設定3以下の分割(_split_setting_keys)を使い、
    高設定側の平均payoutを代表値として扱う。データが無ければ None。
    """
    settings = machine_specs.get('settings', {})
    if not settings:
        return None
    high_keys, _ = _split_setting_keys(settings)
    if not high_keys:
        return None
    vals_high = [settings[k]['payout'] for k in high_keys if settings[k].get('payout') is not None]
    if not vals_high:
        return None
    payout_high = float(np.mean(vals_high))
    return (payout_high - 1.0) * BET_PER_GAME


def compute_all_logLR(
    df: pd.DataFrame,
    machine_tier: dict,
    bias_params: dict,
    specs: dict,
    column_map: dict | None = None,
    bin_curves: dict | None = None,
) -> pd.DataFrame:
    """
    全行に対して3チャンネルのlogLRを計算し、列として追加して返す。
    追加列: logLR_rng, logLR_sashimai, logLR_kaiten
    行ループを排除し NumPy ベクトル演算で処理する。

    specsにpayout(機械割理論値)がある機種は、経験的分位点の代わりに
    理論値をそのままmu_s(差枚チャンネルの期待値)として優先使用する。
    設定確率はRNGで規定される既知の値であり、店舗単位で一律に割り引く根拠が
    無いため補正はかけない(理由: 店舗全体の実測平均が理論値より低いのは
    低設定運用が多いことの反映であり、個々の台の設定別期待値のズレではない)。

    column_map: calibrate_all(resolve_rate_columns)が店舗の実データで検証した
    'BB'/'RB'チャンネルの実列名({'BB': 列名 or None, 'RB': 列名 or None})。
    Tier B判定の場合、specsのラベル(BB確率など)を決め打ちせずこちらを優先使用する。
    未指定時は従来通りBB確率/RB確率列を直接参照する(後方互換)。

    bin_curves: multi_store.py(Stage1b/5)が複数店舗データから学習した機種別
    デシルカーブ。省略時はkaiten_bin_curves.jsonから自動読み込みする。
    未学習(空dict)の場合はlogLR_kaiten=0.0(単店段階のフォールバック、従来動作)。
    """
    df = df.copy()
    rng_out = pd.Series(0.0, index=df.index)
    sashimai_out = pd.Series(0.0, index=df.index)

    for machine_name, grp in df.groupby('機種名', sort=False):
        tier = machine_tier.get(machine_name, {'BB': 'C', 'RB': 'C', 'ART': 'C'})
        machine_specs = specs.get(machine_name, {})
        resolved_cols = (column_map or {}).get(machine_name, {})
        idx = grp.index

        n_arr = grp['回転数'].fillna(0.0).to_numpy(dtype=float)
        valid = n_arr > 0

        # ── 差枚チャンネルのパラメータ推定 ──
        valid_rows = grp[grp['回転数'].fillna(0) > 0].dropna(subset=['差枚', '回転数'])
        if len(valid_rows) >= 30:
            diff_rate = valid_rows['差枚'] / valid_rows['回転数']
            _q1, _q3 = diff_rate.quantile(0.25), diff_rate.quantile(0.75)
            _iqr = _q3 - _q1
            _clip_lo = float(_q1 - 3 * _iqr)
            _clip_hi = float(_q3 + 3 * _iqr)
            sigma_diff = float(diff_rate.clip(_clip_lo, _clip_hi).std()) or 1.0
            mu_s_diff = float(diff_rate.quantile(0.75))
            if mu_s_diff <= 0.0:
                mu_s_diff = 0.0
        else:
            sigma_diff, mu_s_diff = 1.0, 0.0
            _clip_lo, _clip_hi = -np.inf, np.inf

        # 理論機械割(payout)がspecsにあれば理論値を優先使用。
        # サンプル数に依存せず使え、機種を跨いで再利用できる。
        mu_s_theoretical = _payout_mu_high(machine_specs)
        if mu_s_theoretical is not None and mu_s_theoretical > 0.0:
            mu_s_diff = mu_s_theoretical

        # ── RNGチャンネル: ベクトル化 ──
        bb_tier = tier.get('BB', 'C')
        rb_tier = tier.get('RB', 'C')
        rng = np.zeros(len(grp))

        if bb_tier == 'A':
            p_s, p_bl = _tier_a_probs(machine_specs, 'BB')
            if p_s and p_bl:
                k = grp['BB'].fillna(0.0).to_numpy(dtype=float)
                rng += k * np.log(p_s / p_bl) - n_arr * (p_s - p_bl)
        elif bb_tier == 'B':
            bb_col = resolved_cols.get('BB', 'BB確率') if resolved_cols else 'BB確率'
            if bb_col is not None and bb_col in grp.columns:
                bb_rates = grp[bb_col]
                valid_bb = bb_rates.notna()
                if valid_bb.any():
                    # rank(method='max', pct=True) は ECDF(x) = P(X<=x) と等価
                    # NaN行は分布から除外して算出し、寄与は0にする(NaNを0埋めしてランクに
                    # 混ぜると、欠損行が「最低値タイ」として不当に高いパーセンタイルを得てしまうため)
                    pctile = bb_rates[valid_bb].rank(method='max', pct=True).clip(0.01, 0.99)
                    contrib = np.log(pctile / (1.0 - pctile))
                    rng += contrib.reindex(grp.index, fill_value=0.0).to_numpy()

        if rb_tier == 'A':
            p_s, p_bl = _tier_a_probs(machine_specs, 'RB')
            if p_s and p_bl:
                k = grp['RB'].fillna(0.0).to_numpy(dtype=float)
                rng += k * np.log(p_s / p_bl) - n_arr * (p_s - p_bl)
        elif rb_tier == 'B':
            rb_col = resolved_cols.get('RB', 'RB確率') if resolved_cols else 'RB確率'
            if rb_col is not None and rb_col in grp.columns:
                rb_rates = grp[rb_col]
                valid_rb = rb_rates.notna()
                if valid_rb.any():
                    pctile = rb_rates[valid_rb].rank(method='max', pct=True).clip(0.01, 0.99)
                    contrib = np.log(pctile / (1.0 - pctile))
                    rng += contrib.reindex(grp.index, fill_value=0.0).to_numpy()

        rng[~valid] = 0.0
        rng_out.loc[idx] = rng

        # ── 差枚チャンネル: ベクトル化 ──
        if mu_s_diff > 0 and sigma_diff > 0:
            diff_arr = grp['差枚'].fillna(0.0).to_numpy(dtype=float)
            with np.errstate(divide='ignore', invalid='ignore'):
                dr = np.where(valid, np.clip(diff_arr / np.where(valid, n_arr, 1.0), _clip_lo, _clip_hi), 0.0)
            sash = (dr * mu_s_diff - mu_s_diff ** 2 / 2) / sigma_diff ** 2
            sash[~valid] = 0.0
            sashimai_out.loc[idx] = np.nan_to_num(sash)

    df['logLR_rng'] = rng_out
    df['logLR_sashimai'] = sashimai_out

    if bin_curves is None:
        bin_curves = load_bin_curves()
    if bin_curves:
        df['kaiten_zscore'] = compute_kaiten_zscore(df)
        # 直交化パラメータ(multi_store.pyがLOSO検証時に学習)を適用し、
        # RNG証拠と独立な成分だけをチャンネル③に残す(二重計上の防止)
        params = load_channel_weights()
        df['logLR_kaiten'] = compute_logLR_kaiten_column(
            df, bin_curves,
            orth_a=float(params.get('orth_a', 0.0)),
            orth_b=float(params.get('orth_b', 0.0)),
        )
    else:
        # multi_store.py(Stage1b/5)で複数店舗データから学習するまでは0.0
        df['logLR_kaiten'] = 0.0
    return df


# ── Stage 3: LogOdds統合スコア ────────────────────────────────────

def sigmoid(x: float | pd.Series) -> float | pd.Series:
    """logOdds → 高設定確率(0〜1)"""
    return 1.0 / (1.0 + np.exp(-x))


def compute_log_odds(
    df: pd.DataFrame,
    w1: float | None = None,
    w2: float | None = None,
    w3: float | None = None,
    prior_high_ratio: float | None = None,
) -> pd.DataFrame:
    """
    3チャンネルのlogLRを重み付き合算し、log_odds と high_prob 列を追加して返す。
    w1: RNGチャンネル / w2: 差枚チャンネル / w3: 回転数行動チャンネル(Stage5で学習)
    prior_high_ratio: 事前確率π(高設定投入率の事前推定)。切片 β₀=ln(π/(1-π)) として
    LogOddsに加算する。証拠ゼロの行の high_prob が0.5ではなくπに落ちるようになり、
    店舗集計 E[高設定台数]/N が意味を持つ(2026-07導入。DEFAULT_PRIOR_HIGH_RATIO参照)。

    各引数を省略した場合は stage3_channel_weights.json (multi_store.py の
    Stage5が学習) から自動読み込みする。未学習時の既定値は w1=1.0, w2=0.5,
    **w3=0.0(回転数チャンネル無効)**。w3はmulti_store.pyのLOSO交差検証に
    合格した場合のみ正の学習値が保存される(循環学習・二重計上対策、2026-07)。

    [NOTE] w2=0.5 は暫定値。per-game 正規化後もチャンネル間の識別力に差が残るため
    控えめに設定。Stage5の重み学習後に改めて調整すること。
    """
    defaults = load_channel_weights()
    if w1 is None:
        w1 = defaults['w1']
    if w2 is None:
        w2 = defaults['w2']
    if w3 is None:
        w3 = defaults['w3']
    if prior_high_ratio is None:
        prior_high_ratio = defaults['prior_high_ratio']
    if not (0.0 < prior_high_ratio < 1.0):
        raise ValueError(f'prior_high_ratioは(0,1)の範囲が必要: {prior_high_ratio}')
    beta0 = float(np.log(prior_high_ratio / (1.0 - prior_high_ratio)))

    df = df.copy()
    df['log_odds'] = (
        beta0
        + w1 * df['logLR_rng'].fillna(0.0)
        + w2 * df['logLR_sashimai'].fillna(0.0)
        + w3 * df['logLR_kaiten'].fillna(0.0)
    )
    df['high_prob'] = sigmoid(df['log_odds'])
    return df


def load_weights(weights_path: str) -> dict:
    """weights.json から重み wᵢ を読み込む。"""
    with open(weights_path, encoding='utf-8') as f:
        return json.load(f)


# ── Stage 4: 判定保留 ─────────────────────────────────────────────

RNG_ANOMALY_ALPHA = 1e-6  # Tier A RNGチャンネルの異常値検定(片側Poisson)の有意水準


def _tier_a_p_max(machine_specs: dict, channel: str) -> float | None:
    """Tier A機種の設定6(理論上の最良ケース)の理論確率を返す(値が無ければNone)。"""
    settings = machine_specs.get('settings', {})
    vals = [s[channel] for s in settings.values() if s.get(channel) is not None]
    return max(vals) if vals else None


def mark_rng_anomaly(
    df: pd.DataFrame,
    machine_tier: dict,
    specs: dict,
    alpha: float = RNG_ANOMALY_ALPHA,
) -> pd.Series:
    """
    Tier A機種のBB/RB観測回数が、設定6(理論上の最良ケース)を仮定してもなお
    Poisson片側検定で説明不能なほど過剰な行を検出する。

    データ提供元(ana-slo.com)側の表記ミスにより、実際にはあり得ない確率が
    記録されている行がまれに存在する(2026-07判明。例: マルハン新宿東宝ビル店
    キングハナハナ-30 774番、回転数8922でBB=666=観測確率0.075。理論値は設定6でも0.0043で
    17倍もの乖離があり、通常の統計的ばらつきでは説明できない)。
    このような行はlogLR_rngを極端に押し上げ、sigmoid飽和(high_prob=1.0固定)を招き
    S_全台系などの下流サブスコアを歪めるため、Stage4の判定不能と同じ「虚構の値を
    代入せず単純除外する」方針で除外する(理論値へのクリップ等の代入は行わない)。

    alpha=1e-6 の根拠: 全店舗のTier A行×チャンネル検定数(約10万件)に対する
    Bonferroni補正後の有意水準(≈5×10⁻⁷)に近い、かつ実データで境界を確認した結果
    「除外される最後の1件」と「除外されない最初の1件」のp値に約4桁の開きがあり、
    この水準なら閾値をわずかに動かしても結果が変わらないことを確認済み。
    """
    anomaly = pd.Series(False, index=df.index)
    for machine_name, grp in df.groupby('機種名', sort=False):
        tier = machine_tier.get(machine_name, {})
        machine_specs = specs.get(machine_name, {})
        n_arr = grp['回転数'].fillna(0.0).to_numpy(dtype=float)

        for ch, kcol in (('BB', 'BB'), ('RB', 'RB')):
            if tier.get(ch) != 'A' or kcol not in grp.columns:
                continue
            p_max = _tier_a_p_max(machine_specs, ch)
            if not p_max:
                continue
            k_arr = grp[kcol].fillna(0.0).to_numpy(dtype=float)
            expected = n_arr * p_max
            valid = expected > 0
            if not valid.any():
                continue
            pvals = np.ones(len(grp))
            pvals[valid] = stats.poisson.sf(k_arr[valid] - 1.0, expected[valid])
            anomaly.loc[grp.index] |= (pvals < alpha)

    return anomaly


def mark_invalid(
    df: pd.DataFrame,
    machine_tier: dict | None = None,
    specs: dict | None = None,
) -> pd.DataFrame:
    """
    回転数が少なすぎる台日、およびTier A RNGチャンネルの統計的異常値に
    is_invalid=True を付与して返す。
    - 期待発生回数がINVALID_THRESHOLD未満の行(回転数不足)
    - machine_tier/specsを渡した場合: mark_rng_anomaly で検出したデータ異常行

    machine_tier/specs省略時は従来通り回転数不足チェックのみ行う(後方互換)。
    """
    df = df.copy()
    n = df['回転数'].fillna(0.0)

    # 期待発生回数: 合成確率 → BB確率 → RB確率 の優先順で最大値を採用
    gosei = df['合成確率'].fillna(0.0)
    bb_prob = df['BB確率'].fillna(0.0)
    rb_prob = df['RB確率'].fillna(0.0)

    expected_gosei = np.where(gosei > 0, n * gosei, 0.0)
    expected_bb = n * bb_prob
    expected_rb = n * rb_prob
    expected_max = np.maximum(expected_gosei, np.maximum(expected_bb, expected_rb))

    df['is_invalid'] = (expected_max < INVALID_THRESHOLD) | (n == 0)

    if machine_tier is not None and specs is not None:
        df['is_invalid'] = df['is_invalid'] | mark_rng_anomaly(df, machine_tier, specs)

    return df


# ── Stage 4-1: 欠損偏りガード ─────────────────────────────────────

def invalid_rate(df: pd.DataFrame, mask: pd.Series) -> float:
    """
    指定した条件(mask)の日付群における判定不能率を計算する。
    mask: 候補日群を示すboolean Series
    """
    masked = df[mask]
    total = len(masked)
    if total == 0:
        return 0.0
    if 'is_invalid' not in masked.columns:
        return 0.0
    return int(masked['is_invalid'].sum()) / total


def check_missing_bias(
    df: pd.DataFrame,
    candidate_mask: pd.Series,
    threshold: float = MISSING_BIAS_THRESHOLD,
) -> dict:
    """
    Stage 4-1の欠損偏りガード。
    候補日群と非候補日群の判定不能率の差を確認する。
    深さ型パターン検定(depth.py)の前段チェックで必ず呼び出す。

    Returns:
        {
          'bias_detected': bool,    # 閾値超過でフラグ
          'skip_test': bool,        # 大幅超過で検定スキップ指示
          'candidate_rate': float,  # 候補日群の判定不能率
          'control_rate': float,    # 非候補日群の判定不能率
        }
    """
    c_rate = invalid_rate(df, candidate_mask)
    ctrl_rate = invalid_rate(df, ~candidate_mask)
    diff = abs(c_rate - ctrl_rate)
    return {
        'bias_detected': diff > threshold,
        'skip_test': diff > threshold * 2,
        'candidate_rate': c_rate,
        'control_rate': ctrl_rate,
    }
