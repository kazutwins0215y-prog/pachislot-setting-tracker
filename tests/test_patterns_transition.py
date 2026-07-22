"""
patterns_transition.py の回帰テスト。
_fit_transition_from_pairs(p_stay/p_up/piの解析値) /
estimate_transition_matrix(ペア数不足でNone)。
"""
import numpy as np
import pandas as pd
import pytest

import patterns_transition as pt


# ── _fit_transition_from_pairs ───────────────────────────────────

def test_fit_transition_from_pairs_analytic_value():
    # p_prev=0.8定数・p_curr=0.9定数、n=60ペア(閾値50以上)
    # p_stay = Σp_prev*p_curr/Σp_prev = 0.8*0.9*60/(0.8*60) = 0.9
    # p_up   = Σ(1-p_prev)*p_curr/Σ(1-p_prev) = 0.2*0.9*60/(0.2*60) = 0.9
    # pi     = mean(p_prev) = 0.8
    p_prev = np.full(60, 0.8)
    p_curr = np.full(60, 0.9)
    result = pt._fit_transition_from_pairs(p_prev, p_curr)
    assert result['p_stay'] == pytest.approx(0.9)
    assert result['p_up'] == pytest.approx(0.9)
    assert result['pi'] == pytest.approx(0.8)
    assert result['n_pairs'] == 60


def test_fit_transition_from_pairs_below_min_pairs_returns_none():
    p_prev = np.full(49, 0.8)  # TRANSITION_MIN_PAIRS(50)未満
    p_curr = np.full(49, 0.9)
    assert pt._fit_transition_from_pairs(p_prev, p_curr) is None


def test_fit_transition_from_pairs_all_low_denom_hi_zero_returns_none():
    # p_prev全て0 → Σp_prev=0(分母ゼロ) → 予測不可
    p_prev = np.zeros(60)
    p_curr = np.full(60, 0.5)
    assert pt._fit_transition_from_pairs(p_prev, p_curr) is None


def test_fit_transition_from_pairs_all_high_denom_lo_zero_returns_none():
    # p_prev全て1 → Σ(1-p_prev)=0(分母ゼロ) → 予測不可
    p_prev = np.ones(60)
    p_curr = np.full(60, 0.5)
    assert pt._fit_transition_from_pairs(p_prev, p_curr) is None


# ── estimate_transition_matrix ───────────────────────────────────

def _make_transition_df(n_days, hp_value, hole='テスト店', machine='A', unit=1):
    dates = pd.date_range('2026-01-01', periods=n_days, freq='D').strftime('%Y-%m-%d')
    return pd.DataFrame({
        '日付': dates, 'ホール名': hole, '機種名': machine, '台番号': unit,
        'high_prob': hp_value,
    })


def test_estimate_transition_matrix_constant_series_analytic_value():
    # 55日連続・high_prob定数0.85 → 54ペア(閾値50以上)。定数系列は
    # p_stay=p_up=pi=0.85になる(メモリレスな一定確率系ゆえの数学的帰結)
    df = _make_transition_df(55, 0.85)
    result = pt.estimate_transition_matrix(df, 'テスト店')
    assert result is not None
    assert result['p_stay'] == pytest.approx(0.85)
    assert result['p_up'] == pytest.approx(0.85)
    assert result['pi'] == pytest.approx(0.85)
    assert result['n_pairs'] == 54


def test_estimate_transition_matrix_insufficient_days_returns_none():
    # 30日=29ペア < TRANSITION_MIN_PAIRS(50)
    df = _make_transition_df(30, 0.85)
    result = pt.estimate_transition_matrix(df, 'テスト店')
    assert result is None


def test_estimate_transition_matrix_no_matching_hole_returns_none():
    df = _make_transition_df(55, 0.85, hole='別の店')
    result = pt.estimate_transition_matrix(df, 'テスト店')
    assert result is None


def test_estimate_transition_matrix_non_consecutive_dates_excluded():
    # 隔日データ(暦日差2)は連続日ペアとして数えられずNone
    dates = pd.date_range('2026-01-01', periods=110, freq='2D').strftime('%Y-%m-%d')
    df = pd.DataFrame({
        '日付': dates, 'ホール名': 'テスト店', '機種名': 'A', '台番号': 1,
        'high_prob': 0.85,
    })
    result = pt.estimate_transition_matrix(df, 'テスト店')
    assert result is None
