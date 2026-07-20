"""
preprocess.py の Tier判定・欠損偏りガード系関数の回帰テスト。
resolve_rate_columns / estimate_bias_params / judge_tier(未知機種) /
mark_invalid / mark_rng_anomaly / invalid_rate / check_missing_bias。

相関を使う関数は乱数を使わず、決定的な線形関数(完全相関=解析的にr=±1.0)・
対称二次関数(解析的にr=0)で合成データを作る(値は手計算/検算済み)。
"""
import numpy as np
import pandas as pd
import pytest

import preprocess as pp


# ── resolve_rate_columns ─────────────────────────────────────────

def _make_rate_df(machine_name: str, rb_col=None) -> pd.DataFrame:
    """
    40件・差枚率=rate(線形-0.5〜0.5)に対し、BB確率=2*rate+5(完全相関r=1.0)。
    RB確率は未指定なら rate**2(対称二次関数、rateとの線形相関は解析的にr=0)。
    ART確率は10件のみ非NULL(サンプル数不足 n<30 分岐を踏ませる)。
    """
    n = 40
    rate = np.linspace(-0.5, 0.5, n)
    turns = np.full(n, 1000.0)
    diff = rate * turns
    bb = rate * 2 + 5
    rb = rb_col if rb_col is not None else (rate ** 2 + 0.001)
    art = np.concatenate([np.linspace(0, 1, 10), np.full(n - 10, np.nan)])
    return pd.DataFrame({
        '機種名': [machine_name] * n,
        '回転数': turns,
        '差枚': diff,
        'BB確率': bb,
        'RB確率': rb,
        'ART確率': art,
    })


def test_resolve_rate_columns_strong_corr_selected_weak_corr_excluded():
    df = _make_rate_df('テスト機種R')
    result = pp.resolve_rate_columns(df, 'テスト機種R')
    # BB確率はrateと完全相関(|r|=1.0>=0.5) → 採用。RB確率は対称二次関数で相関≈0 → None。
    assert result['BB'] == 'BB確率'
    assert result['RB'] is None


def test_resolve_rate_columns_art_below_min_samples_not_selected():
    # ART確率は非NULLが10件のみ(<_RATE_CORR_MIN_SAMPLES=30) → RB確率がNoneでもART確率は選ばれない
    df = _make_rate_df('テスト機種R2')
    result = pp.resolve_rate_columns(df, 'テスト機種R2')
    assert result['RB'] is None


def test_resolve_rate_columns_no_duplicate_column_assignment():
    # BB確率・RB確率とも完全相関(ほぼr=1.0、浮動小数点誤差で厳密タイではない)で
    # いずれかが先取りされても、もう一方の列が重複割当てされず別列になることを確認
    # (どちらが'BB'/'RB'になるかはソート時の浮動小数点誤差に依存するため断定しない)
    df = _make_rate_df('テスト機種R3', rb_col=np.linspace(-0.5, 0.5, 40) * 3 + 1)
    result = pp.resolve_rate_columns(df, 'テスト機種R3')
    assert result['BB'] is not None
    assert result['RB'] is not None
    assert result['BB'] != result['RB']


def test_resolve_rate_columns_empty_group_returns_none_none():
    df = pd.DataFrame({'機種名': [], '回転数': [], '差枚': []})
    result = pp.resolve_rate_columns(df, '存在しない機種')
    assert result == {'BB': None, 'RB': None}


# ── judge_tier(未知機種) ─────────────────────────────────────────

def test_judge_tier_unknown_machine_uses_resolve_rate_columns():
    # 実在しない機種名 → specsに無いので resolve_rate_columns の結果で決まる
    df = _make_rate_df('_pytest未知機種_zzz999')
    result = pp.judge_tier(df, '_pytest未知機種_zzz999')
    assert result == {'BB': 'B', 'RB': 'C', 'ART': 'C'}


# ── estimate_bias_params ─────────────────────────────────────────

def test_estimate_bias_params_below_30_samples_returns_zero():
    df = pd.DataFrame({
        '機種名': ['機種E'] * 10,
        '回転数': np.full(10, 1000.0),
        '差枚': np.full(10, 50.0),
    })
    result = pp.estimate_bias_params(df, '機種E')
    assert result == {'direction': 0, 'strength': 0.0}


def test_estimate_bias_params_positive_correlation():
    # 差枚率=turns*0.0001(turnsと正の完全相関) → direction=+1, strength≈1.0
    turns = 1000.0 + np.arange(40) * 10.0
    rate = turns * 0.0001
    df = pd.DataFrame({'機種名': ['機種E2'] * 40, '回転数': turns, '差枚': rate * turns})
    result = pp.estimate_bias_params(df, '機種E2')
    assert result['direction'] == 1
    assert result['strength'] == pytest.approx(1.0, abs=1e-9)


def test_estimate_bias_params_negative_correlation():
    turns = 1000.0 + np.arange(40) * 10.0
    rate = -turns * 0.0001
    df = pd.DataFrame({'機種名': ['機種E3'] * 40, '回転数': turns, '差枚': rate * turns})
    result = pp.estimate_bias_params(df, '機種E3')
    assert result['direction'] == -1
    assert result['strength'] == pytest.approx(1.0, abs=1e-9)


# ── mark_invalid / mark_rng_anomaly ──────────────────────────────

_ANOMALY_SPECS = {'キングハナハナ-30': {'settings': {'6': {'BB': 0.0043}}}}
_ANOMALY_TIER = {'キングハナハナ-30': {'BB': 'A', 'RB': 'C', 'ART': 'C'}}


def _make_invalid_df() -> pd.DataFrame:
    return pd.DataFrame({
        '機種名': ['他機種', '他機種', '他機種', 'キングハナハナ-30', 'キングハナハナ-30'],
        '回転数': [10, 100000, 0, 8922, 8922],
        '合成確率': [0.0, 0.001, 0.0, 0.0, 0.0],
        'BB確率': [0.0001, 0.0, 0.0, 0.0043, 0.0043],
        'RB確率': [0.0, 0.0, 0.0, 0.0, 0.0],
        'BB': [0, 0, 0, 666, 38],   # 666=異常な過剰カウント, 38=期待値相当(正常)
        'RB': [0, 0, 0, 0, 0],
    })


def test_mark_invalid_expected_count_below_threshold():
    df = _make_invalid_df()
    out = pp.mark_invalid(df)
    # row0: expected_bb=10*0.0001=0.001<5 → invalid
    assert bool(out.iloc[0]['is_invalid']) is True
    # row1: expected_gosei=100000*0.001=100>=5 → valid
    assert bool(out.iloc[1]['is_invalid']) is False
    # row2: 回転数=0 → invalid
    assert bool(out.iloc[2]['is_invalid']) is True


def test_mark_rng_anomaly_detects_excess_count_but_not_normal_count():
    df = _make_invalid_df()
    anomaly = pp.mark_rng_anomaly(df, _ANOMALY_TIER, _ANOMALY_SPECS)
    assert bool(anomaly.iloc[3]) is True   # BB=666, expected≈38.36 → Poisson片側検定で異常
    assert bool(anomaly.iloc[4]) is False  # BB=38 ≈ expected → 異常ではない
    assert bool(anomaly.iloc[0]) is False  # tier対象外の機種は常にFalse


def test_mark_invalid_ORs_count_check_with_rng_anomaly():
    # row3(キングハナハナ-30, BB=666): 期待発生数38.36>=5なので単体では有効だが、
    # RNG異常検定でTrueとなりOR結合でis_invalid=Trueになることを確認
    df = _make_invalid_df()
    out = pp.mark_invalid(df, _ANOMALY_TIER, _ANOMALY_SPECS)
    assert bool(out.iloc[3]['is_invalid']) is True
    assert bool(out.iloc[4]['is_invalid']) is False  # BB=38は異常でも回転数不足でもない


# ── invalid_rate ─────────────────────────────────────────────────

def test_invalid_rate_analytic_value():
    df = pd.DataFrame({'is_invalid': [True, False, True, False]})
    mask = pd.Series([True, True, False, False])
    # masked=先頭2行(True,False) → sum=1, total=2 → rate=0.5
    assert pp.invalid_rate(df, mask) == pytest.approx(0.5)


def test_invalid_rate_empty_mask_returns_zero():
    df = pd.DataFrame({'is_invalid': [True, False]})
    mask = pd.Series([False, False])
    assert pp.invalid_rate(df, mask) == 0.0


def test_invalid_rate_missing_column_returns_zero():
    df = pd.DataFrame({'other': [1, 2]})
    mask = pd.Series([True, True])
    assert pp.invalid_rate(df, mask) == 0.0


# ── check_missing_bias ───────────────────────────────────────────

def test_check_missing_bias_detected_but_not_skip():
    # 候補群10件(invalid3件=0.3) vs 対照群10件(invalid1件=0.1) → diff=0.2
    # threshold既定0.12: 0.2>0.12→bias_detected True、0.2<0.24(threshold*2)→skip_test False
    is_invalid = [True, True, True] + [False] * 7 + [True] + [False] * 9
    candidate_mask = pd.Series([True] * 10 + [False] * 10)
    df = pd.DataFrame({'is_invalid': is_invalid})
    result = pp.check_missing_bias(df, candidate_mask)
    assert result['bias_detected'] is True
    assert result['skip_test'] is False
    assert result['candidate_rate'] == pytest.approx(0.3)
    assert result['control_rate'] == pytest.approx(0.1)


def test_check_missing_bias_no_difference_not_detected():
    is_invalid = [True] * 2 + [False] * 8 + [True] * 2 + [False] * 8
    candidate_mask = pd.Series([True] * 10 + [False] * 10)
    df = pd.DataFrame({'is_invalid': is_invalid})
    result = pp.check_missing_bias(df, candidate_mask)
    assert result['bias_detected'] is False
    assert result['skip_test'] is False


def test_check_missing_bias_large_difference_triggers_skip():
    # diff=0.8-0.0=0.8 > threshold*2(0.24) → skip_test True
    is_invalid = [True] * 8 + [False] * 2 + [False] * 10
    candidate_mask = pd.Series([True] * 10 + [False] * 10)
    df = pd.DataFrame({'is_invalid': is_invalid})
    result = pp.check_missing_bias(df, candidate_mask)
    assert result['bias_detected'] is True
    assert result['skip_test'] is True
