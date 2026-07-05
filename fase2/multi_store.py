"""
multi_store.py — 複数店舗データでの精緻化 (Stage 1b + Stage 5 + Stage 6)

前提: 複数店舗のデータが揃ってから実行する。単店データの段階では不要。

Stage 1b: 回転数チャンネルの行動的成分(店舗別学習)
    繁盛度・客層効果による判別力の強さは店舗ごとに異なる。
    単店では学習不可 → 複数店舗データから階層モデルで学習。
    実装上は、機種別デシルカーブ(bin_curves、台内zスコア→logLR)を全店舗
    データをプールして学習する(learn_bin_curves)ことでStage2チャンネル③を
    有効化する。preprocess.compute_all_logLRはStage1a同様「1回学習・全店舗再利用」
    の方針でkaiten_bin_curves.jsonを自動読み込みする。

Stage 5-1: 階層モデルによる重みの学習
    LogOdds_i = β₀ + β₁y₁ᵢ + β₂y₂ᵢ + (β₃ + γ_store(i)) * y₃ᵢ
    教師ラベルなし → RNGベーススコア y1 を疑似正解として半教師あり回帰。
    学習された γ_store = 「その店の客層・イベント文化の強さ」の代理特徴量。

Stage 5-2: スケーラビリティ
    Stage1a(Tier判定)は機種×データ提供元で1回校正 → 全店舗再利用。
    新規店舗追加で個別学習が必要なのは γ_store のみ。

Stage 6: 検証戦略
    6-1: 店舗間Tier再現性チェック(同一機種BB列の意味・回転数符号の一致)
    6-2: マクロ整合性チェック(長期機械割と事後スコア集計値の整合)
    6-3: 店舗群分割クロスバリデーション(学習店舗で重みを決め検証店舗で確認)

依存: preprocess.py (Stage0〜4出力) + score.py (store_profile)

実行方法:
    python multi_store.py
"""
import sqlite3
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

import preprocess as pp

WEIGHTS_PATH = Path(__file__).parent / 'weights.json'
_DB_ROOT = Path(__file__).parent.parent / 'ホールデータ'

MIN_SAMPLES_PER_MACHINE = 200   # bin_curve学習(機種×全店舗プール)に必要な最小行数
MIN_SAMPLES_PER_STORE = 200     # γ_store学習(店舗単位)に必要な最小行数
MIN_ROWS_TIER_CHECK = 30        # Stage6-1で1店舗分として扱う最小行数
MIN_STORES_MACRO_CHECK = 3      # Stage6-2の相関算出に必要な最小店舗数


# ── データ読み込み(全店舗プール) ──────────────────────────────────

def _hole_name_for_db(db_path: Path) -> str | None:
    """DB内のslot_dataから店舗名(ホール名)を1つ特定する。複数ある場合はNone。"""
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute('SELECT DISTINCT ホール名 FROM slot_data').fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return rows[0][0] if len(rows) == 1 else None


def find_db_files() -> list[Path]:
    if not _DB_ROOT.exists():
        return []
    return sorted(_DB_ROOT.glob('*.db'))


def load_all_stores_scored(db_root: Path | None = None) -> pd.DataFrame:
    """
    全店舗DBを読み込み、Stage0〜2(logLR_rng/logLR_sashimai)まで計算して結合する。

    bin_curvesはあえて渡さず(空dict)、この時点ではlogLR_kaitenは0.0のまま
    にする。kaiten_zscoreは店舗を跨いだ台の取り違えを避けるため、結合後に
    (ホール名, 機種名, 台番号)単位でまとめて一括計算する。
    """
    frames = []
    for db_path in find_db_files() if db_root is None else sorted(Path(db_root).glob('*.db')):
        hole_name = _hole_name_for_db(db_path)
        if hole_name is None:
            continue
        df = pp.load_slot_data(str(db_path), hole_name)
        if df.empty:
            continue
        df = pp.normalize(df)
        machine_tier, bias_params, column_map = pp.calibrate_all(df)
        specs = pp._load_specs()
        scored = pp.compute_all_logLR(df, machine_tier, bias_params, specs, column_map, bin_curves={})
        scored = pp.compute_log_odds(scored)
        scored = pp.mark_invalid(scored, machine_tier, specs)
        scored = scored[~scored['is_invalid']].copy()
        scored['db_path'] = str(db_path)
        frames.append(scored)

    if not frames:
        return pd.DataFrame()

    all_df = pd.concat(frames, ignore_index=True)
    all_df['kaiten_zscore'] = pp.compute_kaiten_zscore(all_df)
    return all_df


# ── Stage 1b: 機種別デシルカーブ(全店舗プール学習) ─────────────────

def learn_bin_curves(
    all_stores_df: pd.DataFrame,
    min_samples: int = MIN_SAMPLES_PER_MACHINE,
) -> dict[str, list[float]]:
    """
    機種ごとに、台内zスコアのデシル→logLR_rng(疑似正解y1)基準の実測曲線を、
    全店舗データをプールして学習する。単店データでは標本数が不足するため、
    複数店舗のデータをプールできることがStage1bの前提(このためmulti_store.py
    に置いている)。

    curve[d] = そのデシルに属する行の平均logLR_rng − 機種全体の平均logLR_rng
    (baseline控除。低デシルは0付近・高デシルで正に振れる非対称カーブを想定)

    標本数がmin_samples未満の機種はcurvesに含めない
    (= logLR_kaiten側でcurve無し→0.0フォールバック)。
    """
    curves: dict[str, list[float]] = {}
    df = all_stores_df.dropna(subset=['kaiten_zscore', 'logLR_rng'])
    if df.empty:
        return curves

    percentile = pd.Series(stats.norm.cdf(df['kaiten_zscore']), index=df.index)
    decile = np.minimum((percentile * 10).astype(int), 9)

    for machine_name, grp_idx in df.groupby('機種名', sort=False).groups.items():
        if len(grp_idx) < min_samples:
            continue
        y1 = df.loc[grp_idx, 'logLR_rng']
        d = decile.loc[grp_idx]
        baseline = float(y1.mean())
        curve = []
        for decile_i in range(10):
            bucket = y1[d == decile_i]
            curve.append(float(bucket.mean() - baseline) if len(bucket) > 0 else 0.0)
        curves[machine_name] = curve

    return curves


def _save_bin_curves(bin_curves: dict) -> None:
    pp.BIN_CURVES_PATH.write_text(
        json.dumps(bin_curves, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def _save_channel_weights(beta_dict: dict) -> None:
    pp.CHANNEL_WEIGHTS_PATH.write_text(
        json.dumps(beta_dict, ensure_ascii=False, indent=2), encoding='utf-8'
    )


# ── Stage 5-1: 階層モデル(γ_store学習) ─────────────────────────────

def fit_hierarchical_model(
    all_stores_df: pd.DataFrame,
    min_samples_per_store: int = MIN_SAMPLES_PER_STORE,
) -> tuple[dict, dict]:
    """
    全店舗データで階層モデルを fitting し、γ_store と更新済み重み β を返す。

    LogOdds_i = β0 + β1*y1i + β2*y2i + (β3 + γ_store(i)) * y3i
    のうちβ1(RNG)・β2(差枚)はStage3の既存暫定値(w1=1.0, w2=0.5)を据え置き、
    本関数は β3(回転数チャンネルの基準重み)とγ_store(店舗ごとの偏差)を学習する。

    教師ラベルが無いため、Stage2で既に計算済みのRNGベースlogLR(y1、
    machine_setting_specs.jsonの理論確率に基づく既知の証拠)を疑似正解として、
    店舗ごとに y3(デシルカーブ適用後の回転数チャンネル)の y1 に対する回帰係数
    (単回帰の傾き)を求める。この傾きが大きい店舗ほど「回転数の伸びが実際に
    高設定を強く示唆する店」= 客層・イベント文化の強さの代理特徴量となる。

    標本数がmin_samples_per_store未満の店舗はgamma_store=0.0(補正なし、
    全店舗共通のβ3にフォールバック)とする。

    Returns:
        (gamma_store_dict, beta_dict)
        gamma_store_dict: {hole_name: gamma_store_value}
        beta_dict: {'b0': float, 'b1': float, 'b2': float, 'b3': float}
    """
    bin_curves = learn_bin_curves(all_stores_df)

    df = all_stores_df.copy()
    if bin_curves and '機種名' in df.columns:
        df['logLR_kaiten_est'] = pp.compute_logLR_kaiten_column(df, bin_curves)
    else:
        df['logLR_kaiten_est'] = np.nan

    learned_machines = set(bin_curves.keys())
    all_holes = sorted(df['ホール名'].dropna().unique().tolist()) if 'ホール名' in df.columns else []

    fit_df = df[
        df['機種名'].isin(learned_machines)
        & df['kaiten_zscore'].notna()
        & df['logLR_rng'].notna()
    ] if learned_machines else df.iloc[0:0]

    store_slopes: dict[str, float] = {}
    store_n: dict[str, int] = {}
    for hole_name, grp in fit_df.groupby('ホール名'):
        if len(grp) < min_samples_per_store:
            continue
        x = grp['logLR_kaiten_est'].to_numpy(dtype=float)
        y = grp['logLR_rng'].to_numpy(dtype=float)
        if np.std(x) == 0:
            continue
        slope, _intercept = np.polyfit(x, y, 1)
        store_slopes[hole_name] = float(slope)
        store_n[hole_name] = len(grp)

    if store_slopes:
        total_n = sum(store_n.values())
        b3_global = sum(store_slopes[h] * store_n[h] for h in store_slopes) / total_n
    else:
        # どの店舗も標本不足 → 回転数チャンネルは補正なしの既定値(w3=1.0相当)に留める
        b3_global = 1.0

    gamma_store_dict = {h: store_slopes[h] - b3_global for h in store_slopes}
    for hole_name in all_holes:
        gamma_store_dict.setdefault(hole_name, 0.0)

    beta_dict = {'b0': 0.0, 'b1': 1.0, 'b2': 0.5, 'b3': b3_global}

    _save_bin_curves(bin_curves)
    _save_channel_weights(beta_dict)

    return gamma_store_dict, beta_dict


def update_gamma_store(db_path: str, gamma_store_dict: dict) -> None:
    """
    store_profile テーブルの gamma_store フィールドを更新する。
    (ホール名, パターン)行が既に存在する店舗のみ更新対象になる
    (store_profile自体の作成はscore.update_store_profileの役割)。
    """
    con = sqlite3.connect(db_path)
    try:
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table'", con
        )['name'].tolist()
        if 'store_profile' not in tables:
            return
        for hole_name, gamma in gamma_store_dict.items():
            con.execute(
                'UPDATE store_profile SET gamma_store = ? WHERE ホール名 = ?',
                (gamma, hole_name),
            )
        con.commit()
    finally:
        con.close()


# ── Stage 6: 検証戦略 ────────────────────────────────────────────────

def check_tier_reproducibility(
    all_stores_df: pd.DataFrame,
    machine_name: str,
) -> dict:
    """
    Stage 6-1: 同一機種が複数店舗にある場合、
    BB列の意味・回転数チャンネルの符号が店舗間で一致するかを確認する。

    - Tier一致: 各店舗のデータからjudge_tierを再実行し、BB/RB判定が一致するか
    - 符号一致: kaiten_zscore(回転数の伸び)とlogLR_rng(RNG based疑似正解)の
      店舗内相関の符号が、全店舗で一致するか(=回転数が伸びるほど高設定を
      示唆するという関係の向きが店舗によらず普遍的か)
    """
    sub = all_stores_df[all_stores_df['機種名'] == machine_name]
    result: dict = {'machine_name': machine_name, 'n_stores': 0, 'tier_consistent': None,
                    'corr_sign_consistent': None, 'tier_by_store': {}, 'corr_by_store': {}}

    tier_by_store: dict[str, dict] = {}
    corr_by_store: dict[str, float] = {}

    for hole_name, grp in sub.groupby('ホール名'):
        if len(grp) < MIN_ROWS_TIER_CHECK:
            continue
        tier_by_store[hole_name] = pp.judge_tier(grp, machine_name)

        pair = grp.dropna(subset=['kaiten_zscore', 'logLR_rng'])
        if len(pair) >= MIN_ROWS_TIER_CHECK and pair['kaiten_zscore'].std() > 0 and pair['logLR_rng'].std() > 0:
            r, _p = stats.pearsonr(pair['kaiten_zscore'], pair['logLR_rng'])
            corr_by_store[hole_name] = float(r)

    result['n_stores'] = len(tier_by_store)
    result['tier_by_store'] = tier_by_store
    result['corr_by_store'] = corr_by_store

    if len(tier_by_store) >= 2:
        distinct_tiers = {json.dumps(t, sort_keys=True) for t in tier_by_store.values()}
        result['tier_consistent'] = len(distinct_tiers) == 1

    signs = {np.sign(r) for r in corr_by_store.values() if r != 0}
    if len(corr_by_store) >= 2:
        result['corr_sign_consistent'] = len(signs) <= 1

    return result


def check_macro_consistency(
    all_stores_df: pd.DataFrame,
    scored_df: pd.DataFrame,
) -> dict:
    """
    Stage 6-2: 全日・全機種の差枚合計÷回転数合計から長期実測機械割(の代理指標)
    を算出し、事後スコア集計値(店舗平均high_prob)と整合するかを確認する。

    「実測差枚率が高い店ほど、事後的に高設定判定が出やすい」という向きの
    整合性をスピアマン相関で検証する(店舗単位の粗い集計のため、値の絶対的な
    一致ではなく順位の整合性を確認する設計)。
    """
    actual = (
        all_stores_df.dropna(subset=['差枚', '回転数'])
        .groupby('ホール名')
        .apply(lambda g: g['差枚'].sum() / g['回転数'].sum() if g['回転数'].sum() > 0 else np.nan)
    )
    predicted = scored_df.dropna(subset=['high_prob']).groupby('ホール名')['high_prob'].mean()

    joined = pd.DataFrame({'実測差枚率': actual, '事後high_prob平均': predicted}).dropna()

    if len(joined) < MIN_STORES_MACRO_CHECK:
        return {
            'n_stores': len(joined),
            'consistent': None,
            'reason': f'店舗数不足({MIN_STORES_MACRO_CHECK}店舗以上必要)',
        }

    corr = joined['実測差枚率'].corr(joined['事後high_prob平均'], method='spearman')
    return {
        'n_stores': len(joined),
        'spearman_corr': float(corr) if pd.notna(corr) else None,
        'consistent': bool(corr > 0) if pd.notna(corr) else None,
        'detail': joined.to_dict('index'),
    }


# ── メイン ───────────────────────────────────────────────────────

def run() -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    db_files = find_db_files()
    if len(db_files) < 2:
        print(f'複数店舗データが揃っていません(現在{len(db_files)}店舗)。'
              'multi_store.pyは2店舗以上のDBが ホールデータ/ にある場合のみ実行してください。')
        return

    print(f'{len(db_files)}店舗のデータを読み込み中...')
    all_df = load_all_stores_scored()
    if all_df.empty:
        print('有効なデータがありませんでした。')
        return
    print(f'読み込み完了: {len(all_df):,}行 / {all_df["ホール名"].nunique()}店舗')

    gamma_store_dict, beta_dict = fit_hierarchical_model(all_df)
    print('\n[Stage5] 階層モデル学習結果')
    print(f'  β = {beta_dict}')
    for hole_name, gamma in sorted(gamma_store_dict.items(), key=lambda kv: -kv[1]):
        print(f'  γ_store[{hole_name}] = {gamma:+.4f}')

    for db_path in db_files:
        hole_name = _hole_name_for_db(db_path)
        if hole_name is None or hole_name not in gamma_store_dict:
            continue
        update_gamma_store(str(db_path), {hole_name: gamma_store_dict[hole_name]})
    print('\nstore_profile.gamma_store を更新しました(既存行がある店舗のみ)。')

    print('\n[Stage6] 検証')
    shared_machines = (
        all_df.groupby('機種名')['ホール名'].nunique()
        .pipe(lambda s: s[s >= 2])
        .index.tolist()
    )
    if shared_machines:
        print(f'  複数店舗共通機種: {len(shared_machines)}機種')
        for machine_name in shared_machines[:5]:
            r = check_tier_reproducibility(all_df, machine_name)
            print(f'  - {machine_name}: n_stores={r["n_stores"]}, '
                  f'tier_consistent={r["tier_consistent"]}, '
                  f'corr_sign_consistent={r["corr_sign_consistent"]}')
    else:
        print('  複数店舗共通機種が見つかりませんでした(6-1スキップ)。')

    if 'high_prob' in all_df.columns:
        macro = check_macro_consistency(all_df, all_df)
        print(f'  マクロ整合性: {macro}')


if __name__ == '__main__':
    run()
