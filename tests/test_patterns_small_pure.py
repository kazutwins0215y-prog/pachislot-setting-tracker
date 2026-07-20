"""
複数の patterns_*.py モジュールにまたがる小さな純関数の回帰テスト。
_combine_signed(depth) / predict_transition_next_day(transition) /
tail_digit_group(groups) / _introduction_elapsed_bin(events)。
"""
import numpy as np
import pandas as pd
import pytest

import patterns_depth as pd_
import patterns_transition as pt_
import patterns_groups as pg
import patterns_events as pe


# ── _combine_signed (noisy-or型統合) ─────────────────────────────

def test_combine_signed_both_zero_is_zero():
    out = pd_._combine_signed(np.array([0.0]), np.array([0.0]))
    assert out[0] == pytest.approx(0.0)


def test_combine_signed_both_positive_noisy_or():
    # a=0.5, b=0.5 → 1-(1-0.5)(1-0.5) = 1-0.25 = 0.75
    out = pd_._combine_signed(np.array([0.5]), np.array([0.5]))
    assert out[0] == pytest.approx(0.75)


def test_combine_signed_both_negative_noisy_or_mirrored():
    # a=-0.5, b=-0.5 → -(1-(1-0.5)(1-0.5)) = -0.75
    out = pd_._combine_signed(np.array([-0.5]), np.array([-0.5]))
    assert out[0] == pytest.approx(-0.75)


def test_combine_signed_only_one_side_nonzero_passes_through():
    out_a = pd_._combine_signed(np.array([0.3]), np.array([0.0]))
    out_b = pd_._combine_signed(np.array([0.0]), np.array([-0.4]))
    assert out_a[0] == pytest.approx(0.3)
    assert out_b[0] == pytest.approx(-0.4)


def test_combine_signed_mixed_sign_is_simple_average():
    # a=0.4, b=-0.2 → 単純平均 = 0.1
    out = pd_._combine_signed(np.array([0.4]), np.array([-0.2]))
    assert out[0] == pytest.approx(0.1)


def test_combine_signed_vectorized_over_array():
    a = np.array([0.5, -0.5, 0.3, 0.0, 0.4])
    b = np.array([0.5, -0.5, 0.0, 0.0, -0.2])
    out = pd_._combine_signed(a, b)
    expected = np.array([0.75, -0.75, 0.3, 0.0, 0.1])
    np.testing.assert_allclose(out, expected)


# ── predict_transition_next_day ─────────────────────────────────

def test_predict_transition_next_day_analytic_value():
    # P(高_翌日) = p_today*p_stay + (1-p_today)*p_up
    matrix = {'p_stay': 0.8, 'p_up': 0.2}
    result = pt_.predict_transition_next_day(0.6, matrix)
    assert result == pytest.approx(0.6 * 0.8 + 0.4 * 0.2)


def test_predict_transition_next_day_p_today_one_returns_p_stay():
    matrix = {'p_stay': 0.75, 'p_up': 0.1}
    assert pt_.predict_transition_next_day(1.0, matrix) == pytest.approx(0.75)


def test_predict_transition_next_day_p_today_zero_returns_p_up():
    matrix = {'p_stay': 0.75, 'p_up': 0.1}
    assert pt_.predict_transition_next_day(0.0, matrix) == pytest.approx(0.1)


# ── tail_digit_group ─────────────────────────────────────────────

def test_tail_digit_group_last_digit_grouping():
    units = pd.Series([10, 20, 100, 111, 11, 22, 5])
    out = pg.tail_digit_group(units)
    assert out.iloc[0] == 'グループ末尾_0'   # 10
    assert out.iloc[1] == 'グループ末尾_0'   # 20
    assert out.iloc[2] == 'グループ末尾_0'   # 100 (全桁同一ではない)
    assert out.iloc[3] == 'グループゾロ目'   # 111 (全桁同一)
    assert out.iloc[4] == 'グループゾロ目'   # 11
    assert out.iloc[5] == 'グループゾロ目'   # 22
    assert out.iloc[6] == 'グループ末尾_5'   # 5 (1桁なのでゾロ目対象外)


def test_tail_digit_group_nan_propagates_as_na():
    units = pd.Series([10, np.nan, 22])
    out = pg.tail_digit_group(units)
    assert pd.isna(out.iloc[1])
    assert out.iloc[0] == 'グループ末尾_0'
    assert out.iloc[2] == 'グループゾロ目'


# ── _introduction_elapsed_bin ────────────────────────────────────

def test_introduction_elapsed_bin_boundaries():
    assert pe._introduction_elapsed_bin(-1) == '初日'
    assert pe._introduction_elapsed_bin(0) == '初日'
    assert pe._introduction_elapsed_bin(1) == '2〜3日'
    assert pe._introduction_elapsed_bin(2) == '2〜3日'
    assert pe._introduction_elapsed_bin(3) == '4〜7日'
    assert pe._introduction_elapsed_bin(6) == '4〜7日'
    assert pe._introduction_elapsed_bin(7) == '8〜14日'
    assert pe._introduction_elapsed_bin(13) == '8〜14日'
    assert pe._introduction_elapsed_bin(14) == '15日以降'
    assert pe._introduction_elapsed_bin(100) == '15日以降'
