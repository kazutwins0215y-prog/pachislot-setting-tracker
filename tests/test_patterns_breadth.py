"""
patterns_breadth.py の回帰テスト。
score_zentaiki(uniformity式・count<2でNaN) /
score_zentaikei_judgment(+_fisher)(z検定・Fisher検定による3値判定)。
"""
import numpy as np
import pandas as pd
import pytest

import patterns_breadth as pb


# ── score_zentaiki ──────────────────────────────────────────────

def test_score_zentaiki_uniformity_formula_analytic_value():
    # mean=0.5, std(ddof=1)=sqrt(0.02)=0.14142135...
    # uniformity = 1 - std/0.5 = 0.71715728...
    # score = mean * uniformity = 0.35857864...
    df = pd.DataFrame({
        '日付': ['2026-07-01', '2026-07-01'],
        'ホール名': ['テスト店', 'テスト店'],
        '機種名': ['A', 'A'],
        'high_prob': [0.4, 0.6],
    })
    out = pb.score_zentaiki(df, ['機種名'])
    assert out.iloc[0] == pytest.approx(0.3585786437626905)
    assert out.iloc[1] == pytest.approx(0.3585786437626905)


def test_score_zentaiki_identical_values_full_uniformity():
    # std=0 → uniformity=1 → score=mean
    df = pd.DataFrame({
        '日付': ['2026-07-01', '2026-07-01'],
        'ホール名': ['テスト店', 'テスト店'],
        '機種名': ['A', 'A'],
        'high_prob': [0.6, 0.6],
    })
    out = pb.score_zentaiki(df, ['機種名'])
    assert out.iloc[0] == pytest.approx(0.6)


def test_score_zentaiki_single_unit_group_is_nan():
    # count<2の組(機種Bは1行のみ)はNaN
    df = pd.DataFrame({
        '日付': ['2026-07-01', '2026-07-01'],
        'ホール名': ['テスト店', 'テスト店'],
        '機種名': ['A', 'B'],
        'high_prob': [0.6, 0.5],
    })
    out = pb.score_zentaiki(df, ['機種名'])
    assert np.isnan(out.iloc[1])


def test_score_zentaiki_excludes_invalid_rows():
    # is_invalid=Trueの行は統計計算(mean/std)から除外される。
    # グループキーは日付・ホール名・機種名のみ(有効性は含まない)なので、
    # 無効行も同じグループのスコアにマップされるが、値自体は有効2行(0.6,0.6)のみで決まる
    # (無効行のhigh_prob=0.9を混ぜていればmean=0.7・std>0になり0.6にはならない)。
    df = pd.DataFrame({
        '日付': ['2026-07-01'] * 3,
        'ホール名': ['テスト店'] * 3,
        '機種名': ['A'] * 3,
        'high_prob': [0.6, 0.6, 0.9],
        'is_invalid': [False, False, True],
    })
    out = pb.score_zentaiki(df, ['機種名'])
    assert out.iloc[0] == pytest.approx(0.6)
    assert out.iloc[2] == pytest.approx(0.6)


# ── score_zentaikei_judgment ─────────────────────────────────────

def _make_judgment_rows(hole, date, machine, high_prob_values, s_zentaikei):
    return pd.DataFrame({
        '日付': [date] * len(high_prob_values),
        'ホール名': [hole] * len(high_prob_values),
        '機種名': [machine] * len(high_prob_values),
        'high_prob': high_prob_values,
        'S_全台系': [s_zentaikei] * len(high_prob_values),
    })


def test_score_zentaikei_judgment_labels_and_nan_fallback():
    hole, date, prior = 'テスト店', '2026-07-01', 0.15
    # A: 全台hp=0.9・S_全台系=0.9(高) → z高・FDR有意・S>=0.5 → 全台系
    # B: 半数hp=0.9/半数hp=0.1・S_全台系=0.2(低) → z有意だがS<0.5 → 高配分
    # D: 全台hp=0.6・S_全台系=NaN(算出不能) → z有意だがS判定不能 → 高配分側に倒す
    # C: 全台hp=0.1 → z<閾値 → 普段どおり
    df = pd.concat([
        _make_judgment_rows(hole, date, 'A', [0.9] * 10, 0.9),
        _make_judgment_rows(hole, date, 'B', [0.9] * 5 + [0.1] * 5, 0.2),
        _make_judgment_rows(hole, date, 'D', [0.6] * 10, np.nan),
        _make_judgment_rows(hole, date, 'C', [0.1] * 10, 0.1),
    ], ignore_index=True)

    result = pb.score_zentaikei_judgment(df, prior=prior)
    labels = result.set_index('機種名')['判定ラベル']

    assert labels['A'] == pb.JUDGMENT_LABEL_ZENTAIKEI
    assert labels['B'] == pb.JUDGMENT_LABEL_KOUHAIBUN
    assert labels['D'] == pb.JUDGMENT_LABEL_KOUHAIBUN
    assert labels['C'] == pb.JUDGMENT_LABEL_NORMAL

    row_a = result.set_index('機種名').loc['A']
    assert row_a['台数'] == 10
    assert row_a['期待高設定台数'] == pytest.approx(9.0)
    assert row_a['投入率'] == pytest.approx(0.9)


def test_score_zentaikei_judgment_empty_df_returns_empty_with_columns():
    df = pd.DataFrame({
        '日付': [], 'ホール名': [], '機種名': [], 'high_prob': [], 'S_全台系': [],
    })
    result = pb.score_zentaikei_judgment(df, prior=0.15)
    assert result.empty
    assert list(result.columns) == [
        'ホール名', '日付', '機種名', '台数', '期待高設定台数',
        'zスコア', 'p値', '投入率', 'S_全台系', '判定ラベル',
    ]


# ── score_zentaikei_judgment_fisher ──────────────────────────────

def test_score_zentaikei_judgment_fisher_labels():
    hole, date = 'テスト店', '2026-07-01'
    # E: n=1(min_n未満) → 検定対象外・常に普段どおり
    # F: n=2・k=2(全台hot)、背景(G)はほぼ非hot → Fisher検定で有意 → k==n → 全台系
    # G: n=20・全台非hot(背景として機能)
    df = pd.concat([
        _make_judgment_rows(hole, date, 'E', [0.9], None).drop(columns=['S_全台系']),
        _make_judgment_rows(hole, date, 'F', [0.9, 0.9], None).drop(columns=['S_全台系']),
        _make_judgment_rows(hole, date, 'G', [0.1] * 20, None).drop(columns=['S_全台系']),
    ], ignore_index=True)

    result = pb.score_zentaikei_judgment_fisher(df)
    by_machine = result.set_index('機種名')

    assert by_machine.loc['E', '判定ラベル'] == pb.JUDGMENT_LABEL_NORMAL
    assert pd.isna(by_machine.loc['E', 'p値'])  # min_n未満はFDR家族から除外・検定自体を行わない

    assert by_machine.loc['F', '判定ラベル'] == pb.JUDGMENT_LABEL_ZENTAIKEI
    assert by_machine.loc['G', '判定ラベル'] == pb.JUDGMENT_LABEL_NORMAL
