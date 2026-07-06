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

import data_source as ds
import preprocess as pp

WEIGHTS_PATH = Path(__file__).parent / 'weights.json'

MIN_SAMPLES_PER_MACHINE = 200   # bin_curve学習(機種×全店舗プール)に必要な最小行数
MIN_SAMPLES_PER_STORE = 200     # γ_store学習(店舗単位)に必要な最小行数
MIN_ROWS_TIER_CHECK = 30        # Stage6-1で1店舗分として扱う最小行数
MIN_STORES_MACRO_CHECK = 3      # Stage6-2の相関算出に必要な最小店舗数


# ── データ読み込み(全店舗プール) ──────────────────────────────────

def load_all_stores_scored(replica_db: str | Path | None = None) -> pd.DataFrame:
    """
    レプリカDB内の全店舗を読み込み、Stage0〜2(logLR_rng/logLR_sashimai)まで
    計算して結合する。Tier判定・列解決は従来通り店舗単位で行う。

    bin_curvesはあえて渡さず(空dict)、この時点ではlogLR_kaitenは0.0のまま
    にする。kaiten_zscoreは店舗を跨いだ台の取り違えを避けるため、結合後に
    (ホール名, 機種名, 台番号)単位でまとめて一括計算する。
    """
    replica_db = str(replica_db or ds.REPLICA_DB_PATH)
    frames = []
    for hole_name in ds.list_holes(replica_db):
        df = pp.load_slot_data(replica_db, hole_name)
        if df.empty:
            continue
        df = pp.normalize(df)
        machine_tier, bias_params, column_map = pp.calibrate_all(df)
        specs = pp._load_specs()
        scored = pp.compute_all_logLR(df, machine_tier, bias_params, specs, column_map, bin_curves={})
        scored = pp.compute_log_odds(scored)
        scored = pp.mark_invalid(scored, machine_tier, specs)
        scored = scored[~scored['is_invalid']].copy()
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


def _save_channel_weights(weights: dict) -> None:
    """
    Stage3チャンネル重み・直交化パラメータを保存する。キー名は
    preprocess.load_channel_weights(読み手)が期待する w1/w2/w3/orth_a/orth_b に合わせる。
    ※ 旧実装はb0〜b3キーで保存しており、読み手にマージされても無視される
      (=学習結果が使われない)バグがあった。
    Stage5の学習対象外のキー(prior_high_ratio=事前確率π。手動設定値)は
    既存ファイルの値を保全してから上書き保存する(学習実行のたびに消えるのを防ぐ)。
    """
    allowed = {'w1', 'w2', 'w3', 'orth_a', 'orth_b'}
    assert set(weights.keys()) <= allowed, f'不正なキー: {weights.keys()}'
    preserved_keys = {'prior_high_ratio'}
    merged = dict(weights)
    if pp.CHANNEL_WEIGHTS_PATH.exists():
        existing = json.loads(pp.CHANNEL_WEIGHTS_PATH.read_text(encoding='utf-8'))
        for key in preserved_keys & set(existing.keys()):
            merged[key] = existing[key]
    pp.CHANNEL_WEIGHTS_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding='utf-8'
    )


# ── Stage 5-1: LOSO交差検証つき回転数チャンネル学習(γ_store学習) ────

MIN_PAIRS_PER_STORE = 100          # 検証(翌観測日ペア)に必要な店舗あたり最小ペア数
KAITEN_GATE_POSITIVE_RATIO = 0.7   # 合格条件: 相関が正の店舗の割合
KAITEN_GATE_ALPHA = 0.05           # 合格条件: プール相関のp値


def _kaiten_valid_mask(df: pd.DataFrame, bin_curves: dict) -> pd.Series:
    """回転数チャンネルがシグナルを持つ行(zscoreあり・学習済み機種)のマスク。"""
    return df['kaiten_zscore'].notna() & df['機種名'].isin(bin_curves.keys())


def _fit_orth_params(df: pd.DataFrame, bin_curves: dict) -> tuple[float, float] | None:
    """
    直交化パラメータ(orth_a, orth_b)を学習する: y3(カーブ値)をy1(logLR_rng)に
    単回帰し、y3のうちy1で説明できる成分 a + b·y1 を求める。
    残差 y3 − (a + b·y1) が「回転数だけが持つ独立成分」となる。
    """
    y3 = pp.compute_logLR_kaiten_column(df, bin_curves)
    mask = _kaiten_valid_mask(df, bin_curves)
    if int(mask.sum()) < MIN_PAIRS_PER_STORE:
        return None
    y1 = df.loc[mask, 'logLR_rng'].to_numpy(dtype=float)
    if np.std(y1) == 0:
        return None
    b, a = np.polyfit(y1, y3[mask].to_numpy(dtype=float), 1)
    return float(a), float(b)


def validate_kaiten_channel(
    all_stores_df: pd.DataFrame,
    min_samples: int = MIN_SAMPLES_PER_MACHINE,
) -> dict:
    """
    回転数チャンネルのLeave-One-Store-Out(LOSO)交差検証。

    旧実装は「y1(logLR_rng)を教師に学習したカーブをy1で検証する」循環構造で、
    回帰の傾きが構成上ほぼ1になり検証になっていなかった。本関数は:
    1. 店舗sを除いた残り店舗でbin_curvesと直交化パラメータを学習
    2. sに適用してout-of-sampleのy3を得て、y1で直交化(残差y3⊥)
    3. **同一台内で y3⊥(当日) が 翌観測日の y1 を予測するか**を相関で検証
       (同日のy1との相関は「出ている台に客が座り続ける」逆因果で汚染されるため、
       設定の据え置き傾向を介して翌日に伝わる成分を教師にする)

    合格条件: プール相関 > 0 かつ p < KAITEN_GATE_ALPHA かつ
              相関が正の店舗が KAITEN_GATE_POSITIVE_RATIO 以上 かつ 2店舗以上で評価可能

    Returns:
        {'passed': bool, 'pooled': {'r','p','slope','n_pairs'} | None,
         'per_store': {店舗名: {'r','p','slope','n_pairs'}}, 'reason': str}
    """
    holes = sorted(all_stores_df['ホール名'].dropna().unique().tolist())
    per_store: dict[str, dict] = {}
    pooled_x: list[np.ndarray] = []
    pooled_y: list[np.ndarray] = []

    for hole_name in holes:
        train = all_stores_df[all_stores_df['ホール名'] != hole_name]
        test = all_stores_df[all_stores_df['ホール名'] == hole_name]
        curves = learn_bin_curves(train, min_samples)
        if not curves:
            continue
        orth = _fit_orth_params(train, curves)
        if orth is None:
            continue
        orth_a, orth_b = orth

        y3o = pp.compute_logLR_kaiten_column(test, curves, orth_a=orth_a, orth_b=orth_b)
        mask = _kaiten_valid_mask(test, curves)

        te = test[['ホール名', '機種名', '台番号', '日付', 'logLR_rng']].copy()
        te['_y3o'] = y3o.where(mask)
        te = te.sort_values(['機種名', '台番号', '日付'])
        te['_y1_next'] = te.groupby(['機種名', '台番号'])['logLR_rng'].shift(-1)

        pair = te.dropna(subset=['_y3o', '_y1_next'])
        if len(pair) < MIN_PAIRS_PER_STORE:
            continue
        x = pair['_y3o'].to_numpy(dtype=float)
        y = pair['_y1_next'].to_numpy(dtype=float)
        if np.std(x) == 0:
            continue
        r, p = stats.pearsonr(x, y)
        slope, _ = np.polyfit(x, y, 1)
        per_store[hole_name] = {
            'r': float(r), 'p': float(p), 'slope': float(slope), 'n_pairs': len(pair),
        }
        pooled_x.append(x)
        pooled_y.append(y)

    if len(per_store) < 2:
        return {'passed': False, 'pooled': None, 'per_store': per_store,
                'reason': f'評価可能な店舗が{len(per_store)}店舗(2店舗以上必要)'}

    px = np.concatenate(pooled_x)
    py = np.concatenate(pooled_y)
    pooled_r, pooled_p = stats.pearsonr(px, py)
    pooled_slope, _ = np.polyfit(px, py, 1)
    positive_ratio = sum(1 for v in per_store.values() if v['r'] > 0) / len(per_store)

    passed = (pooled_r > 0
              and pooled_p < KAITEN_GATE_ALPHA
              and positive_ratio >= KAITEN_GATE_POSITIVE_RATIO)
    reason = ('合格' if passed else
              f'不合格(pooled_r={pooled_r:.4f}, p={pooled_p:.3g}, 正の店舗率={positive_ratio:.0%})')

    return {
        'passed': bool(passed),
        'pooled': {'r': float(pooled_r), 'p': float(pooled_p),
                   'slope': float(pooled_slope), 'n_pairs': len(px)},
        'per_store': per_store,
        'reason': reason,
    }


def fit_hierarchical_model(
    all_stores_df: pd.DataFrame,
    min_samples_per_store: int = MIN_SAMPLES_PER_STORE,
) -> tuple[dict, dict, dict]:
    """
    回転数チャンネル(Stage1b/5)の学習+合格ゲート。

    [2026-07 再設計] 旧実装は「y1から作ったy3をy1に回帰」する循環学習で、
    β3≈1が構成上自明・γ_storeはノイズ主体だったため、以下に置き換えた:
    1. validate_kaiten_channel(LOSO交差検証・翌観測日y1予測)を実行
    2. 合格時のみ: w3 = プール回帰傾き(0〜1にクリップ)、
       γ_store = 店舗別傾き − プール傾き(out-of-sample評価に基づく)
       不合格時: w3 = 0.0(チャンネル無効)、γ_store = 全店舗0.0
    3. 本番適用用のbin_curves・直交化パラメータは全店舗プールで学習して保存
       (新規店舗にも適用するため。検証はLOSO、適用はプールという分担)

    Returns:
        (gamma_store_dict, weights, validation)
        weights: {'w1','w2','w3','orth_a','orth_b'}
    """
    all_holes = sorted(all_stores_df['ホール名'].dropna().unique().tolist())

    validation = validate_kaiten_channel(all_stores_df)

    # 本番適用用: 全店舗プールで学習
    bin_curves = learn_bin_curves(all_stores_df)
    orth = _fit_orth_params(all_stores_df, bin_curves) if bin_curves else None
    orth_a, orth_b = orth if orth is not None else (0.0, 0.0)

    gamma_store_dict = {h: 0.0 for h in all_holes}
    if validation['passed']:
        w3 = float(np.clip(validation['pooled']['slope'], 0.0, 1.0))
        for hole_name, v in validation['per_store'].items():
            if v['n_pairs'] >= min_samples_per_store:
                gamma_store_dict[hole_name] = float(v['slope'] - validation['pooled']['slope'])
    else:
        w3 = 0.0

    weights = {'w1': 1.0, 'w2': 0.5, 'w3': w3,
               'orth_a': float(orth_a), 'orth_b': float(orth_b)}

    _save_bin_curves(bin_curves)
    _save_channel_weights(weights)

    return gamma_store_dict, weights, validation


def update_gamma_store(gamma_store_dict: dict, analysis_db: str | Path | None = None) -> None:
    """
    分析DBの store_profile テーブルの gamma_store フィールドを更新する。
    (ホール名, パターン)行が既に存在する店舗のみ更新対象になる
    (store_profile自体の作成はscore.update_store_profileの役割)。
    """
    analysis_db = str(analysis_db or ds.ANALYSIS_DB_PATH)
    con = sqlite3.connect(analysis_db)
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

    try:
        holes = ds.list_holes()
    except FileNotFoundError as e:
        print(e)
        return
    if len(holes) < 2:
        print(f'複数店舗データが揃っていません(現在{len(holes)}店舗)。'
              'multi_store.pyはレプリカDBに2店舗以上のデータがある場合のみ実行してください。')
        return

    print(f'{len(holes)}店舗のデータを読み込み中...')
    all_df = load_all_stores_scored()
    if all_df.empty:
        print('有効なデータがありませんでした。')
        return
    print(f'読み込み完了: {len(all_df):,}行 / {all_df["ホール名"].nunique()}店舗')

    gamma_store_dict, weights, validation = fit_hierarchical_model(all_df)
    print('\n[Stage5] 回転数チャンネル LOSO交差検証')
    print(f'  判定: {validation["reason"]}')
    if validation['pooled']:
        pl = validation['pooled']
        print(f'  プール: r={pl["r"]:+.4f}, p={pl["p"]:.3g}, slope={pl["slope"]:+.4f}, n={pl["n_pairs"]:,}')
    for hole_name, v in validation['per_store'].items():
        print(f'  - {hole_name}: r={v["r"]:+.4f}, p={v["p"]:.3g}, n_pairs={v["n_pairs"]:,}')
    print('\n[Stage5] 学習結果')
    print(f'  チャンネル重み = {weights}')
    if not validation['passed']:
        print('  → 検証不合格のため w3=0.0(回転数チャンネル無効)で保存しました。')
    for hole_name, gamma in sorted(gamma_store_dict.items(), key=lambda kv: -kv[1]):
        print(f'  γ_store[{hole_name}] = {gamma:+.4f}')

    update_gamma_store(gamma_store_dict)
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
