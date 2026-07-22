"""
patterns_common.py の周期探索・カレンダー基盤の回帰テスト。
acf_screen / pdm_confirm / calendar_candidates / _wilcoxon_rank_biserial / calendar_test。
"""
import numpy as np
import pandas as pd
import pytest

import patterns_common as pc
import preprocess as pp


# ── acf_screen ────────────────────────────────────────────────────

def test_acf_screen_detects_perfect_period_5():
    # [0,10,20,30,40]の繰り返し(周期5)を8回 → lag=5(と倍数)は完全相関でr=1.0
    x = pd.Series([0, 10, 20, 30, 40] * 8, dtype=float)
    result = pc.acf_screen(x)
    assert 5 in result


def test_acf_screen_constant_series_returns_empty():
    x = pd.Series([0.5] * 40)
    assert pc.acf_screen(x) == []


def test_acf_screen_too_short_series_returns_empty():
    # 各lagで有効ペア数<10になり全lagスキップ
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    assert pc.acf_screen(x) == []


# ── pdm_confirm ───────────────────────────────────────────────────

def test_pdm_confirm_perfect_period_5_theta_zero():
    # lag=5・n_bins=5(内部固定)が一致するため bin=t%5 が厳密に決まり、
    # 各ビン内は完全に同一値(分散0) → theta=0.0(解析的に0)
    x = pd.Series([0, 10, 20, 30, 40] * 8, dtype=float)
    result = pc.pdm_confirm(x, [5])
    assert result[5]['theta'] == pytest.approx(0.0)
    assert result[5]['confirmed'] is True


def test_pdm_confirm_empty_candidate_lags_returns_empty_dict():
    x = pd.Series([1.0] * 20)
    assert pc.pdm_confirm(x, []) == {}


def test_pdm_confirm_constant_series_zero_variance_not_confirmed():
    x = pd.Series([5.0] * 20)
    result = pc.pdm_confirm(x, [4])
    assert result[4]['theta'] == pytest.approx(1.0)
    assert result[4]['confirmed'] is False


# ── calendar_candidates ───────────────────────────────────────────

def test_calendar_candidates_returns_49_candidates():
    dt = pd.DatetimeIndex(['2026-07-11'])
    result = pc.calendar_candidates(dt)
    assert len(result) == 7 + 10 + 1 + 31 == 49


def test_calendar_candidates_day_11_matches_tail1_and_zorome():
    dt = pd.DatetimeIndex(['2026-07-11'])
    result = pc.calendar_candidates(dt)
    assert bool(result['末尾_1'][0])
    assert bool(result['ゾロ目'][0])
    assert bool(result['毎月_11日'][0])
    assert not bool(result['末尾_2'][0])


def test_calendar_candidates_day_15_not_zorome():
    dt = pd.DatetimeIndex(['2026-07-15'])
    result = pc.calendar_candidates(dt)
    assert not bool(result['ゾロ目'][0])


def test_calendar_candidates_weekday_matches_actual_dayofweek():
    dt = pd.DatetimeIndex(['2026-07-13'])
    expected_name = pc._WEEKDAY_NAMES[dt.dayofweek[0]]
    result = pc.calendar_candidates(dt)
    assert bool(result[f'曜日_{expected_name}'][0])


# ── _wilcoxon_rank_biserial ───────────────────────────────────────

def test_wilcoxon_rank_biserial_analytic_value():
    # diffs=[3,-1,2] → |diffs|=[3,1,2]のrank=[3,1,2] → 正側{3,2}のrank和=5、負側{-1}のrank和=1
    # r=(5-1)/(5+1)=4/6
    diffs = np.array([3.0, -1.0, 2.0])
    assert pc._wilcoxon_rank_biserial(diffs) == pytest.approx(4.0 / 6.0)


def test_wilcoxon_rank_biserial_all_positive_is_one():
    diffs = np.array([1.0, 2.0, 3.0])
    assert pc._wilcoxon_rank_biserial(diffs) == pytest.approx(1.0)


def test_wilcoxon_rank_biserial_all_zero_diffs_returns_zero():
    diffs = np.array([0.0, 0.0])
    assert pc._wilcoxon_rank_biserial(diffs) == pytest.approx(0.0)


def test_wilcoxon_rank_biserial_zero_diffs_excluded_from_ranking():
    # 0は除外。残る{1,-1}は同順位(タイ)でrank双方1.5 → r=0.0
    diffs = np.array([0.0, 1.0, -1.0])
    assert pc._wilcoxon_rank_biserial(diffs) == pytest.approx(0.0)


# ── calendar_test ─────────────────────────────────────────────────

def test_calendar_test_detects_matching_weekday_with_full_separation():
    # 60日、金曜(dayofweek==4)だけhp=0.9、他は0.3 → 完全分離のためrbc=1.0・有意
    dates = pd.date_range('2026-01-05', periods=60, freq='D')
    hp = pd.Series([0.9 if d.dayofweek == 4 else 0.3 for d in dates])
    date_series = pd.Series(dates.strftime('%Y-%m-%d'))

    result = pc.calendar_test(hp, date_series, pp.check_missing_bias)

    assert len(result) == 49
    assert result['曜日_金']['significant'] is True
    assert result['曜日_金']['effect_size'] == pytest.approx(1.0)
    assert result['曜日_金']['p_raw'] < 0.001
