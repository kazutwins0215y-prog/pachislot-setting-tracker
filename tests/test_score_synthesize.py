"""
score.py の合成スコア関連純関数の回帰テスト。
synthesize(狙い目度=Σ(w×S)/Σw、NaN列除外・再正規化) / compute_reliability。
"""
import numpy as np
import pandas as pd
import pytest

import score as sc


# ── synthesize ───────────────────────────────────────────────────

def test_synthesize_weighted_average_analytic_value():
    # weights={'S_全台系':2.0,'S_鉄板台':1.0} 全行データあり
    # row: S_全台系=0.8, S_鉄板台=0.4 → (2*0.8+1*0.4)/(2+1) = 2.0/3.0
    df = pd.DataFrame({'S_全台系': [0.8], 'S_鉄板台': [0.4]})
    out = sc.synthesize(df, weights={'S_全台系': 2.0, 'S_鉄板台': 1.0})
    assert out['狙い目度'].iloc[0] == pytest.approx(2.0 / 3.0)
    assert out['有効サブスコア数'].iloc[0] == 2


def test_synthesize_nan_column_excluded_and_renormalized():
    # row0: 両方あり → (1*0.8+1*0.4)/(1+1)=0.6, 有効数2
    # row1: S_全台系=NaN → 分子分母から除外し S_鉄板台=0.6 のみで再正規化 → 0.6, 有効数1
    # row2: 両方NaN → 分母0 → NaN, 有効数0
    df = pd.DataFrame({
        'S_全台系': [0.8, np.nan, np.nan],
        'S_鉄板台': [0.4, 0.6, np.nan],
    })
    out = sc.synthesize(df, weights={'S_全台系': 1.0, 'S_鉄板台': 1.0})
    assert out['狙い目度'].iloc[0] == pytest.approx(0.6)
    assert out['狙い目度'].iloc[1] == pytest.approx(0.6)
    assert np.isnan(out['狙い目度'].iloc[2])
    assert out['有効サブスコア数'].tolist() == [2, 1, 0]


def test_synthesize_reliabilities_decay_effective_weight():
    # weights={'S_全台系':2.0,'S_鉄板台':1.0}, reliabilities={'S_全台系':0.5,'S_鉄板台':1.0}
    # 有効重み: S_全台系=2*0.5=1.0, S_鉄板台=1*1.0=1.0
    # row: S_全台系=0.8, S_鉄板台=0.4 → (1.0*0.8+1.0*0.4)/(1.0+1.0)=0.6
    df = pd.DataFrame({'S_全台系': [0.8], 'S_鉄板台': [0.4]})
    out = sc.synthesize(
        df,
        weights={'S_全台系': 2.0, 'S_鉄板台': 1.0},
        reliabilities={'S_全台系': 0.5, 'S_鉄板台': 1.0},
    )
    assert out['狙い目度'].iloc[0] == pytest.approx(0.6)


def test_synthesize_missing_weight_defaults_to_one():
    # weights辞書に無い列は既定重み1.0
    df = pd.DataFrame({'S_全台系': [0.5], 'S_鉄板台': [0.5]})
    out = sc.synthesize(df, weights={'S_全台系': 3.0})  # S_鉄板台は既定1.0
    # (3*0.5+1*0.5)/(3+1) = 2.0/4.0 = 0.5
    assert out['狙い目度'].iloc[0] == pytest.approx(0.5)


def test_synthesize_ignores_columns_not_in_sub_scores():
    # SUB_SCORESに無い列は合成に使われない
    df = pd.DataFrame({'S_全台系': [1.0], '無関係列': [999.0]})
    out = sc.synthesize(df, weights={'S_全台系': 1.0})
    assert out['狙い目度'].iloc[0] == pytest.approx(1.0)


# ── compute_reliability ──────────────────────────────────────────

def _make_reliability_df(with_bias: bool) -> pd.DataFrame:
    # 15個の日付(うち5個は2回登場)=20行、S列は全行有効値
    dates = [f'2026-01-{d:02d}' for d in range(1, 16)]
    all_dates = dates[:5] * 2 + dates[5:]  # 10 + 10 = 20行、ユニーク日付15
    df = pd.DataFrame({'日付': all_dates, 'S_全台系': [0.5] * len(all_dates)})
    if with_bias:
        # 20行中4行(20%)がis_biased=True
        df['is_biased'] = [True] * 4 + [False] * 16
    return df


def test_compute_reliability_day_and_sample_factor():
    # day_factor=15/30=0.5, sample_factor=20/50=0.4 → 0.5*0.7+0.4*0.3=0.47
    df = _make_reliability_df(with_bias=False)
    result = sc.compute_reliability(df, 'S_全台系')
    assert result == pytest.approx(0.47)


def test_compute_reliability_is_biased_penalty():
    # 上と同じ日数/サンプル数だが20%がis_biased=True → 0.47*(1-0.2)=0.376
    df = _make_reliability_df(with_bias=True)
    result = sc.compute_reliability(df, 'S_全台系')
    assert result == pytest.approx(0.376)


def test_compute_reliability_missing_column_returns_zero():
    df = pd.DataFrame({'日付': ['2026-01-01'], '他列': [1.0]})
    assert sc.compute_reliability(df, 'S_全台系') == 0.0


def test_compute_reliability_all_nan_returns_zero():
    df = pd.DataFrame({'日付': ['2026-01-01', '2026-01-02'], 'S_全台系': [np.nan, np.nan]})
    assert sc.compute_reliability(df, 'S_全台系') == 0.0


def test_compute_reliability_clipped_to_one_when_saturated():
    # 100件・全てユニーク日付 → day_factor=min(1,100/30)=1.0, sample_factor=min(1,100/50)=1.0
    # → reliability=1.0*0.7+1.0*0.3=1.0(上限)
    df = pd.DataFrame({'日付': [f'd{i}' for i in range(100)], 'S_全台系': [0.5] * 100})
    result = sc.compute_reliability(df, 'S_全台系')
    assert result == pytest.approx(1.0)
