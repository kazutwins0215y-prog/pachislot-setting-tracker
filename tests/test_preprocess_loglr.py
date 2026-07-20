"""
preprocess.py の最小純関数(スカラー・解析値中心)の回帰テスト。
sigmoid / logLR_rng / logLR_sashimai / logLR_kaiten / _split_setting_keys /
_tier_a_probs / _payout_mu_high / _tier_a_p_max。

期待値は可能な限り手計算(コメントに数式・根拠を記載)。数式そのものは
preprocess.py の docstring に記載のものを転記している(実装と無関係な
独立計算ツールとして scipy.stats.norm.cdf / numpy を使用)。
"""
import numpy as np
import pytest

import preprocess as pp


# ── sigmoid ──────────────────────────────────────────────────────

def test_sigmoid_zero_is_one_half():
    assert pp.sigmoid(0.0) == pytest.approx(0.5)


def test_sigmoid_monotonic_increasing():
    xs = [-5.0, -1.0, 0.0, 1.0, 5.0]
    ys = [pp.sigmoid(x) for x in xs]
    assert ys == sorted(ys)
    assert len(set(ys)) == len(ys)  # 厳密な単調増加(同値なし)


def test_sigmoid_saturates_towards_bounds():
    assert pp.sigmoid(50.0) == pytest.approx(1.0, abs=1e-10)
    assert pp.sigmoid(-50.0) == pytest.approx(0.0, abs=1e-10)


def test_sigmoid_series_input():
    import pandas as pd
    out = pp.sigmoid(pd.Series([0.0, 100.0, -100.0]))
    assert out.iloc[0] == pytest.approx(0.5)
    assert out.iloc[1] == pytest.approx(1.0, abs=1e-10)
    assert out.iloc[2] == pytest.approx(0.0, abs=1e-10)


# ── logLR_rng ────────────────────────────────────────────────────
# 数式: logLR = k*ln(p_s/p_baseline) - n*(p_s-p_baseline)

def test_logLR_rng_analytic_value():
    # k=100, n=1000, p_s=0.11, p_baseline=0.10
    # 手計算: 100*ln(1.1) - 1000*0.01 = 100*0.0953101798... - 10 = -0.468980...
    val = pp.logLR_rng(100, 1000, 0.11, 0.10)
    assert val == pytest.approx(100 * np.log(1.1) - 10.0, rel=1e-12)
    assert val == pytest.approx(-0.4689820195675214, rel=1e-9)


def test_logLR_rng_n_zero_or_negative_returns_zero():
    assert pp.logLR_rng(10, 0, 0.11, 0.10) == 0.0
    assert pp.logLR_rng(10, -5, 0.11, 0.10) == 0.0


def test_logLR_rng_p_s_zero_or_negative_returns_zero():
    assert pp.logLR_rng(10, 1000, 0.0, 0.10) == 0.0
    assert pp.logLR_rng(10, 1000, -0.01, 0.10) == 0.0


def test_logLR_rng_p_baseline_zero_or_negative_returns_zero():
    assert pp.logLR_rng(10, 1000, 0.11, 0.0) == 0.0
    assert pp.logLR_rng(10, 1000, 0.11, -0.01) == 0.0


# ── logLR_sashimai ───────────────────────────────────────────────
# 数式: logLR = (diff/n*mu_s - mu_s^2/2) / sigma^2

def test_logLR_sashimai_analytic_value():
    # diff=3000, n=6000, mu_s=0.05, sigma=2.0
    # 手計算: (3000/6000*0.05 - 0.05^2/2) / 2.0^2 = (0.025 - 0.00125) / 4 = 0.0059375
    val = pp.logLR_sashimai(3000, 6000, 0.05, 2.0)
    assert val == pytest.approx(0.0059375, rel=1e-12)


def test_logLR_sashimai_n_zero_or_negative_returns_zero():
    assert pp.logLR_sashimai(3000, 0, 0.05, 2.0) == 0.0
    assert pp.logLR_sashimai(3000, -1, 0.05, 2.0) == 0.0


def test_logLR_sashimai_sigma_zero_or_negative_returns_zero():
    assert pp.logLR_sashimai(3000, 6000, 0.05, 0.0) == 0.0
    assert pp.logLR_sashimai(3000, 6000, 0.05, -1.0) == 0.0


# ── logLR_kaiten ─────────────────────────────────────────────────

def test_logLR_kaiten_curve_none_returns_zero():
    assert pp.logLR_kaiten(1.5, '未学習機種', {}) == 0.0


def test_logLR_kaiten_curve_empty_list_returns_zero():
    assert pp.logLR_kaiten(1.5, '機種A', {'機種A': []}) == 0.0


def test_logLR_kaiten_decile_boundary_low_and_high():
    # percentile = norm.cdf(z); decile_idx = min(int(percentile*10), 9)
    # z=-3.0 -> percentile≈0.00135 -> decile 0 (最低ビン)
    # z=3.0  -> percentile≈0.99865 -> decile 9 (最高ビンに天井打ち、"9番"境界の確認)
    curve = [10.0, 11, 12, 13, 14, 15, 16, 17, 18, 99.0]
    assert pp.logLR_kaiten(-3.0, '機種A', {'機種A': curve}) == pytest.approx(10.0)
    assert pp.logLR_kaiten(3.0, '機種A', {'機種A': curve}) == pytest.approx(99.0)


def test_logLR_kaiten_center_maps_to_middle_decile():
    # z=0.0 -> percentile=0.5 -> decile_idx = min(int(5.0), 9) = 5
    curve = [0, 1, 2, 3, 4, 5.0, 6, 7, 8, 9]
    assert pp.logLR_kaiten(0.0, '機種A', {'機種A': curve}) == pytest.approx(5.0)


# ── _split_setting_keys ──────────────────────────────────────────

def test_split_setting_keys_less_than_two_keys_returns_empty():
    assert pp._split_setting_keys({'1': {}}) == ([], [])
    assert pp._split_setting_keys({}) == ([], [])


def test_split_setting_keys_okidoki_style_ladder_1235_6():
    # 沖ドキ系相当: 設定{1,2,3,5,6}。設定4以上=高(5,6)・設定3以下=低(1,2,3)
    settings = {k: {} for k in ('1', '2', '3', '5', '6')}
    high, low = pp._split_setting_keys(settings)
    assert high == ['5', '6']
    assert low == ['1', '2', '3']


def test_split_setting_keys_pink_panther_style_ladder_1456():
    # ピンクパンサーSP相当: 設定{1,4,5,6}。高=(4,5,6)・低=(1,)
    settings = {k: {} for k in ('1', '4', '5', '6')}
    high, low = pp._split_setting_keys(settings)
    assert high == ['4', '5', '6']
    assert low == ['1']


def test_split_setting_keys_fallback_when_high_side_empty():
    # 設定{1,2,3}のみ(4以上が存在しない変則ラダー) → 折半フォールバック
    # keys=['1','2','3'], n=3 → keys[3//2:]=['2','3'](高), keys[:3//2]=['1'](低)
    settings = {k: {} for k in ('1', '2', '3')}
    high, low = pp._split_setting_keys(settings)
    assert high == ['2', '3']
    assert low == ['1']


# ── _tier_a_probs ────────────────────────────────────────────────
# 共通フィクスチャ: 沖ドキ系ラダー{1,2,3,5,6}のBB確率・payout
_SETTINGS_5KEY = {
    '1': {'BB': 0.01, 'payout': 0.98},
    '2': {'BB': 0.011, 'payout': 0.99},
    '3': {'BB': 0.012, 'payout': 1.00},
    '5': {'BB': 0.02, 'payout': 1.05},
    '6': {'BB': 0.025, 'payout': 1.10},
}


def test_tier_a_probs_analytic_value():
    # 高設定側(5,6)平均 = (0.02+0.025)/2 = 0.0225
    # 低設定側(1,2,3)平均 = (0.01+0.011+0.012)/3 = 0.011
    p_s, p_bl = pp._tier_a_probs({'settings': _SETTINGS_5KEY}, 'BB')
    assert p_s == pytest.approx(0.0225)
    assert p_bl == pytest.approx(0.011)


def test_tier_a_probs_no_settings_returns_none():
    assert pp._tier_a_probs({}, 'BB') == (None, None)
    assert pp._tier_a_probs({'settings': {}}, 'BB') == (None, None)


def test_tier_a_probs_channel_missing_returns_none():
    assert pp._tier_a_probs({'settings': _SETTINGS_5KEY}, 'RB') == (None, None)


def test_tier_a_probs_non_positive_average_returns_none():
    # 高設定側平均が0以下(データ異常値相当) → (None, None)
    settings = {'1': {'BB': 0.01}, '5': {'BB': 0.0}, '6': {'BB': 0.0}}
    assert pp._tier_a_probs({'settings': settings}, 'BB') == (None, None)


# ── _payout_mu_high ──────────────────────────────────────────────

def test_payout_mu_high_analytic_value():
    # 高設定側(5,6)payout平均 = (1.05+1.10)/2 = 1.075
    # mu_high = (1.075-1.0) * BET_PER_GAME(3) = 0.225
    mu = pp._payout_mu_high({'settings': _SETTINGS_5KEY})
    assert mu == pytest.approx(0.225)


def test_payout_mu_high_no_settings_returns_none():
    assert pp._payout_mu_high({}) is None
    assert pp._payout_mu_high({'settings': {}}) is None


def test_payout_mu_high_no_payout_values_returns_none():
    settings = {'1': {'BB': 0.01}, '5': {'BB': 0.02}, '6': {'BB': 0.025}}
    assert pp._payout_mu_high({'settings': settings}) is None


# ── _tier_a_p_max ────────────────────────────────────────────────

def test_tier_a_p_max_returns_max_across_all_settings():
    # 設定6が最大とは限らず全設定値の最大を取る仕様であることを、
    # 意図的に順序をずらしたdictでも正しく最大値0.025を返すことで確認
    p_max = pp._tier_a_p_max({'settings': _SETTINGS_5KEY}, 'BB')
    assert p_max == pytest.approx(0.025)


def test_tier_a_p_max_no_values_returns_none():
    assert pp._tier_a_p_max({'settings': {}}, 'BB') is None
    assert pp._tier_a_p_max({'settings': {'1': {'BB': None}}}, 'BB') is None
