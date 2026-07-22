"""
preprocess.py の DataFrame純関数の回帰テスト。
normalize / compute_kaiten_zscore / compute_logLR_kaiten_column /
compute_all_logLR / compute_log_odds。

compute_all_logLR / compute_log_odds は bin_curves={} ・重みを常に明示指定し、
machine_setting_specs.json / kaiten_bin_curves.json / stage3_channel_weights.json
の読み込みを一切発生させない(_specs_cacheを汚さない)。
"""
import numpy as np
import pandas as pd
import pytest

import preprocess as pp


# ── normalize ────────────────────────────────────────────────────

def test_normalize_drops_rows_with_null_unit_or_turns():
    df = pd.DataFrame({
        '日付': ['2026-01-01', '2026-01-01', '2026-01-01'],
        'ホール名': ['店A', '店A', '店A'],
        '機種名': ['機種X', '機種X', '機種X'],
        '台番号': [1, np.nan, 3],
        '回転数': [1000, 2000, np.nan],
    })
    out = pp.normalize(df)
    assert len(out) == 1
    assert out.iloc[0]['台番号'] == 1


def test_normalize_removes_duplicate_keys_keep_first():
    df = pd.DataFrame({
        '日付': ['2026-01-01', '2026-01-01'],
        'ホール名': ['店A', '店A'],
        '機種名': ['機種X', '機種X'],
        '台番号': [1, 1],
        '回転数': [1000, 9999],
    })
    out = pp.normalize(df)
    assert len(out) == 1
    assert out.iloc[0]['回転数'] == 1000  # keep='first'


# ── compute_kaiten_zscore ────────────────────────────────────────

def test_compute_kaiten_zscore_below_min_days_is_nan():
    # min_days=5 に対し3件のみの台は履歴不足 → 全行NaN
    df = pd.DataFrame({
        'ホール名': ['店A'] * 3,
        '機種名': ['機種X'] * 3,
        '台番号': [1, 1, 1],
        '回転数': [900, 1000, 1100],
    })
    z = pp.compute_kaiten_zscore(df, min_days=5)
    assert z.isna().all()


def test_compute_kaiten_zscore_std_zero_is_nan():
    # 履歴6件あるが全日同値(std=0) → NaN
    df = pd.DataFrame({
        'ホール名': ['店A'] * 6,
        '機種名': ['機種X'] * 6,
        '台番号': [1] * 6,
        '回転数': [1000] * 6,
    })
    z = pp.compute_kaiten_zscore(df, min_days=5)
    assert z.isna().all()


def test_compute_kaiten_zscore_analytic_values():
    # 6件・分散あり → 標準化値を手計算(mean=1025, std(ddof=1)≈93.5414)
    df = pd.DataFrame({
        'ホール名': ['店A'] * 6,
        '機種名': ['機種X'] * 6,
        '台番号': [1] * 6,
        '回転数': [900, 950, 1000, 1050, 1100, 1150],
    })
    z = pp.compute_kaiten_zscore(df, min_days=5)
    expected = [-1.3363062095621219, -0.8017837257372732, -0.2672612419124244,
                0.2672612419124244, 0.8017837257372732, 1.3363062095621219]
    np.testing.assert_allclose(z.to_numpy(), expected, rtol=1e-9)


# ── compute_logLR_kaiten_column ──────────────────────────────────

_CURVE = [10.0, 11, 12, 13, 14, 15, 16, 17, 18, 99.0]


def test_compute_logLR_kaiten_column_empty_bin_curves_is_all_zero():
    df = pd.DataFrame({'機種名': ['機種A'] * 3, 'kaiten_zscore': [-3.0, 0.0, 3.0]})
    out = pp.compute_logLR_kaiten_column(df, bin_curves={})
    assert (out == 0.0).all()


def test_compute_logLR_kaiten_column_maps_zscore_to_curve_deciles():
    # z=-3.0→decile0(=10.0), z=0.0→decile5(=15.0), z=3.0→decile9(=99.0、天井打ち)
    df = pd.DataFrame({'機種名': ['機種A'] * 3, 'kaiten_zscore': [-3.0, 0.0, 3.0]})
    out = pp.compute_logLR_kaiten_column(df, bin_curves={'機種A': _CURVE})
    np.testing.assert_allclose(out.to_numpy(), [10.0, 15.0, 99.0])


def test_compute_logLR_kaiten_column_nan_zscore_yields_zero():
    # 履歴不足でzscoreがNaNの行はシグナルなし(0.0のまま)
    df = pd.DataFrame({'機種名': ['機種A'] * 2, 'kaiten_zscore': [0.0, np.nan]})
    out = pp.compute_logLR_kaiten_column(df, bin_curves={'機種A': _CURVE})
    assert out.iloc[0] == pytest.approx(15.0)
    assert out.iloc[1] == pytest.approx(0.0)


def test_compute_logLR_kaiten_column_orthogonalization():
    # 直交化: curve - (orth_a + orth_b * logLR_rng)
    # 3行ともcurve値=[10,15,99], logLR_rng=0.3 固定, orth_a=0.1, orth_b=0.2
    # 手計算: 10-(0.1+0.06)=9.84, 15-0.16=14.84, 99-0.16=98.84
    df = pd.DataFrame({
        '機種名': ['機種A'] * 3,
        'kaiten_zscore': [-3.0, 0.0, 3.0],
        'logLR_rng': [0.3, 0.3, 0.3],
    })
    out = pp.compute_logLR_kaiten_column(df, bin_curves={'機種A': _CURVE}, orth_a=0.1, orth_b=0.2)
    np.testing.assert_allclose(out.to_numpy(), [9.84, 14.84, 98.84], rtol=1e-9)


# ── compute_all_logLR ────────────────────────────────────────────
# 沖ドキ系ラダー{1,2,3,5,6}相当のspecs(_tier_a_probs/_payout_mu_high検証と同一値)
_SETTINGS_5KEY = {
    '1': {'BB': 0.01, 'payout': 0.98},
    '2': {'BB': 0.011, 'payout': 0.99},
    '3': {'BB': 0.012, 'payout': 1.00},
    '5': {'BB': 0.02, 'payout': 1.05},
    '6': {'BB': 0.025, 'payout': 1.10},
}


def test_compute_all_logLR_tier_a_with_payout_override():
    # Tier A: p_s=0.0225, p_bl=0.011(_tier_a_probs参照)
    # row1: k=12,n=1000 → rng=12*ln(2.045...)-1000*0.0115
    # row2: k=20,n=2000 → rng=20*ln(2.045...)-2000*0.0115
    # payout理論値 mu_high=0.225 が経験分位点(<30件でデフォ0.0)を上書き:
    # row1: diff=100→dr=0.1, sash=(0.1*0.225-0.225^2/2)/1^2=-0.0028125
    # row2: diff=-50,n=2000→dr=-0.025, sash=(-0.025*0.225-0.0253125)=-0.0309375
    specs = {'テスト機種A': {'settings': _SETTINGS_5KEY}}
    machine_tier = {'テスト機種A': {'BB': 'A', 'RB': 'C', 'ART': 'C'}}
    df = pd.DataFrame({
        '機種名': ['テスト機種A', 'テスト機種A'],
        '回転数': [1000, 2000],
        'BB': [12, 20],
        'RB': [0, 0],
        '差枚': [100, -50],
    })
    out = pp.compute_all_logLR(df, machine_tier, specs, column_map=None, bin_curves={})

    np.testing.assert_allclose(
        out['logLR_rng'].to_numpy(),
        [-2.9125595630559538, -8.687599271759922],
        rtol=1e-9,
    )
    np.testing.assert_allclose(
        out['logLR_sashimai'].to_numpy(),
        [-0.002812499999999999, -0.030937500000000003],
        rtol=1e-9,
    )
    # bin_curves={}を明示指定したため回転数チャンネルは0.0固定(json読み込み無し)
    assert (out['logLR_kaiten'] == 0.0).all()


def test_compute_all_logLR_tier_b_uses_rank_percentile_and_column_map():
    # Tier B: BB確率のECDF(rank pct)によるlogit変換。3件[0.01,0.05,0.09]は
    # 順位1,2,3/3 → pct=[1/3, 2/3, 1.0(0.99にクリップ)]
    # contrib = ln(pct/(1-pct)) = [-0.6931..., 0.6931..., 4.5951...]
    machine_tier = {'テスト機種B': {'BB': 'B', 'RB': 'C', 'ART': 'C'}}
    column_map = {'テスト機種B': {'BB': 'BB確率', 'RB': None}}
    df = pd.DataFrame({
        '機種名': ['テスト機種B'] * 3,
        '回転数': [1000, 1000, 1000],
        'BB確率': [0.01, 0.05, 0.09],
        '差枚': [0, 0, 0],
    })
    out = pp.compute_all_logLR(df, machine_tier, specs={}, column_map=column_map, bin_curves={})

    np.testing.assert_allclose(
        out['logLR_rng'].to_numpy(),
        [-0.6931471805599454, 0.6931471805599452, 4.595119850134589],
        rtol=1e-9,
    )
    # specsが空でpayout理論値が無く、<30件でmu_s_diff既定0.0のため差枚チャンネルは0
    assert (out['logLR_sashimai'] == 0.0).all()


# ── compute_log_odds ─────────────────────────────────────────────

def test_compute_log_odds_analytic_value_with_explicit_weights():
    # beta0 = ln(0.15/0.85) = -1.7346010553881064
    # log_odds = beta0 + w1*rng + w2*sash + w3*0(kaitenはNaN→fillna 0)
    df = pd.DataFrame({
        'logLR_rng': [-2.9125595630559538, -8.687599271759922],
        'logLR_sashimai': [-0.002812499999999999, -0.030937500000000003],
        'logLR_kaiten': [np.nan, np.nan],
    })
    out = pp.compute_log_odds(df, w1=1.0, w2=0.5, w3=0.0, prior_high_ratio=0.15)
    np.testing.assert_allclose(
        out['log_odds'].to_numpy(),
        [-4.6485668684440595, -10.437669077148028],
        rtol=1e-9,
    )
    np.testing.assert_allclose(
        out['high_prob'].to_numpy(),
        [0.009484497736382557, 2.930658321242783e-05],
        rtol=1e-6,
    )


@pytest.mark.parametrize('bad_prior', [0.0, 1.0, -0.1, 1.5])
def test_compute_log_odds_prior_out_of_range_raises(bad_prior):
    df = pd.DataFrame({'logLR_rng': [0.0], 'logLR_sashimai': [0.0], 'logLR_kaiten': [0.0]})
    with pytest.raises(ValueError):
        pp.compute_log_odds(df, w1=1.0, w2=0.5, w3=0.0, prior_high_ratio=bad_prior)
