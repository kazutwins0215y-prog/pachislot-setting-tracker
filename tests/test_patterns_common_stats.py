"""
patterns_common.py の統計ユーティリティの回帰テスト。
benjamini_hochberg / blend / blend_scalar / learn_all_alphas。
"""
import numpy as np
import pandas as pd
import pytest

import patterns_common as pc


# ── benjamini_hochberg ───────────────────────────────────────────

def test_benjamini_hochberg_all_rejected():
    # 全p値が極小(0.001) → 全棄却
    result = pc.benjamini_hochberg([0.001] * 5, alpha=0.05)
    assert result == [True] * 5


def test_benjamini_hochberg_all_non_rejected():
    # 全p値が大きい(0.9) → 全非棄却
    result = pc.benjamini_hochberg([0.9] * 5, alpha=0.05)
    assert result == [False] * 5


def test_benjamini_hochberg_empty_list():
    assert pc.benjamini_hochberg([]) == []


def test_benjamini_hochberg_boundary_case():
    # p=[0.01, 0.02, 0.031, 0.04, 0.05], alpha=0.05, n=5
    # 閾値 i/n*alpha (i=1..5) = [0.01, 0.02, 0.03, 0.04, 0.05]
    # 個別判定: [True, True, False, True, True](3番目のみ超過)
    # BH手続きは「条件を満たす最大順位」まで全て棄却するため、
    # 4番目(0.04<=0.04)が満たされる以上、3番目(0.031>0.03)も含め全て棄却される。
    p_values = [0.01, 0.02, 0.031, 0.04, 0.05]
    result = pc.benjamini_hochberg(p_values, alpha=0.05)
    assert result == [True, True, True, True, True]


def test_benjamini_hochberg_only_smallest_rejected():
    # 最小値のみ閾値を満たし、残りは満たさない場合はその順位までのみ棄却
    # 閾値: i/5*0.05 = [0.01,0.02,0.03,0.04,0.05]
    p_values = [0.005, 0.5, 0.6, 0.7, 0.8]
    result = pc.benjamini_hochberg(p_values, alpha=0.05)
    assert result == [True, False, False, False, False]


# ── blend ────────────────────────────────────────────────────────

def test_blend_both_present_uses_weighted_average():
    long_s = pd.Series([1.0, 2.0, 3.0])
    short_s = pd.Series([0.0, 4.0, 6.0])
    alpha = 0.3
    out = pc.blend(long_s, short_s, alpha)
    # 手計算: alpha*short + (1-alpha)*long
    expected = alpha * short_s + (1.0 - alpha) * long_s
    pd.testing.assert_series_equal(out, expected, check_names=False)


def test_blend_short_nan_falls_back_to_long():
    long_s = pd.Series([1.0, 2.0, 3.0])
    short_s = pd.Series([np.nan, 4.0, np.nan])
    out = pc.blend(long_s, short_s, alpha=0.5)
    assert out.iloc[0] == pytest.approx(1.0)  # short NaN → long のまま
    assert out.iloc[1] == pytest.approx(0.5 * 4.0 + 0.5 * 2.0)
    assert out.iloc[2] == pytest.approx(3.0)


def test_blend_long_nan_propagates_nan():
    long_s = pd.Series([np.nan, 2.0])
    short_s = pd.Series([1.0, 4.0])
    out = pc.blend(long_s, short_s, alpha=0.5)
    assert np.isnan(out.iloc[0])
    assert out.iloc[1] == pytest.approx(3.0)


# ── blend_scalar ─────────────────────────────────────────────────

def test_blend_scalar_short_none_returns_long():
    assert pc.blend_scalar(0.3, None, alpha=0.3) == pytest.approx(0.3)


def test_blend_scalar_short_nan_returns_long():
    assert pc.blend_scalar(0.3, float('nan'), alpha=0.3) == pytest.approx(0.3)


def test_blend_scalar_analytic_value():
    # long=0.3, short=0.4, alpha=0.3 → 0.3*0.4 + 0.7*0.3 = 0.12+0.21 = 0.33
    assert pc.blend_scalar(0.3, 0.4, alpha=0.3) == pytest.approx(0.33)


# ── learn_all_alphas ─────────────────────────────────────────────

def test_learn_all_alphas_returns_fixed_alpha_for_all_scores():
    result = pc.learn_all_alphas(pd.DataFrame(), 'テスト店')
    assert result == {score: pc.FIXED_ALPHA for score in pc.BLENDABLE_SCORES}


def test_learn_all_alphas_custom_score_list():
    result = pc.learn_all_alphas(pd.DataFrame(), 'テスト店', scores=['S_全台系'])
    assert result == {'S_全台系': pc.FIXED_ALPHA}
