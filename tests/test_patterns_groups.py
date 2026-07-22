"""
patterns_groups.py の回帰テスト。
group_size_medians / machine_group / group_constant_test / group_calendar_test。
"""
import numpy as np
import pandas as pd
import pytest

import patterns_groups as pg


# ── group_size_medians / machine_group ───────────────────────────

def _make_group_size_df():
    dates = pd.date_range('2026-01-01', periods=5, freq='D').strftime('%Y-%m-%d')
    rows = []
    # 機種A: 毎日2台 → 日次台数中央値=2.0
    for d in dates:
        for unit in [1, 2]:
            rows.append({'日付': d, '機種名': 'A', '台番号': unit, 'high_prob': 0.5})
    # 機種B: [1,1,3,3,3]台 → sorted[1,1,3,3,3]の中央値=3.0
    counts = [1, 1, 3, 3, 3]
    for d, cnt in zip(dates, counts):
        for unit in range(100, 100 + cnt):
            rows.append({'日付': d, '機種名': 'B', '台番号': unit, 'high_prob': 0.5})
    return pd.DataFrame(rows)


def test_group_size_medians_analytic_value():
    df = _make_group_size_df()
    medians = pg.group_size_medians(df)
    assert medians['A'] == pytest.approx(2.0)
    assert medians['B'] == pytest.approx(3.0)


def test_group_size_medians_excludes_invalid_rows():
    df = _make_group_size_df()
    df['is_invalid'] = False
    # 機種Aの1日目、台番号2を無効化 → その日の有効台数は1に減る
    mask = (df['機種名'] == 'A') & (df['台番号'] == 2) & (df['日付'] == df['日付'].iloc[0])
    df.loc[mask, 'is_invalid'] = True
    medians = pg.group_size_medians(df)
    assert medians['A'] == pytest.approx(2.0)  # 残り4日は2台のままなので中央値は変わらない


def test_machine_group_below_min_units_is_nan():
    # 機種C: 毎日1台のみ(中央値1.0 < MACHINE_GROUP_MIN_UNITS=2) → 対象外
    dates = pd.date_range('2026-01-01', periods=5, freq='D').strftime('%Y-%m-%d')
    df = pd.DataFrame({'日付': dates, '機種名': 'C', '台番号': 1, 'high_prob': 0.5})
    result = pg.machine_group(df)
    assert result.isna().all()


def test_machine_group_at_or_above_min_units_keeps_name():
    df = _make_group_size_df()
    result = pg.machine_group(df)
    assert (result[df['機種名'] == 'A'] == 'A').all()
    assert (result[df['機種名'] == 'B'] == 'B').all()


# ── group_constant_test ──────────────────────────────────────────

def _make_constant_test_df(rate_g1, rate_g2, n_days=6):
    dates = pd.date_range('2026-01-01', periods=n_days, freq='D').strftime('%Y-%m-%d')
    rows = []
    for d in dates:
        rows.append({'日付': d, 'ホール名': 'テスト店', 'high_prob': rate_g1, '_grp': 'G1'})
        rows.append({'日付': d, 'ホール名': 'テスト店', 'high_prob': rate_g2, '_grp': 'G2'})
    df = pd.DataFrame(rows)
    group_series = df.pop('_grp')
    return df, group_series


def test_group_constant_test_detects_constant_positive_gap():
    # G1が全日でG2より高い(0.6 vs 0.3)定数差 → 全diffs=0.3>0でrbc=1.0・有意
    df, group_series = _make_constant_test_df(0.6, 0.3)
    result = pg.group_constant_test(df, 'テスト店', group_series)
    row_g1 = result[result['グループ'] == 'G1'].iloc[0]
    assert row_g1['該当日数'] == 6
    assert row_g1['効果量'] == pytest.approx(1.0)
    assert row_g1['p_raw'] < 0.05


def test_group_constant_test_zero_diff_gives_p_one_effect_zero():
    df, group_series = _make_constant_test_df(0.5, 0.5)
    result = pg.group_constant_test(df, 'テスト店', group_series)
    row_g1 = result[result['グループ'] == 'G1'].iloc[0]
    assert row_g1['p_raw'] == pytest.approx(1.0)
    assert row_g1['効果量'] == pytest.approx(0.0)


def test_group_constant_test_below_min_days_is_nan():
    # GROUP_CALENDAR_MIN_DAYS(5)未満の対応ペア数
    df, group_series = _make_constant_test_df(0.6, 0.3, n_days=4)
    result = pg.group_constant_test(df, 'テスト店', group_series)
    row_g1 = result[result['グループ'] == 'G1'].iloc[0]
    assert row_g1['該当日数'] == 4
    assert pd.isna(row_g1['p_raw'])
    assert pd.isna(row_g1['効果量'])


# ── group_calendar_test ───────────────────────────────────────────

def test_group_calendar_test_detects_matching_weekday_with_full_separation():
    # 60日、金曜だけ高いG1単一グループ → calendar_testと同じ数式でrbc=1.0
    dates = pd.date_range('2026-01-05', periods=60, freq='D')
    hp = [0.9 if d.dayofweek == 4 else 0.3 for d in dates]
    df = pd.DataFrame({
        '日付': dates.strftime('%Y-%m-%d'), 'ホール名': 'テスト店', 'high_prob': hp,
    })
    group_series = pd.Series('G1', index=df.index)

    result = pg.group_calendar_test(df, 'テスト店', group_series)
    row = result[(result['グループ'] == 'G1') & (result['日付条件'] == '曜日_金')].iloc[0]
    assert row['効果量'] == pytest.approx(1.0)
    assert row['p_raw'] < 0.001


def test_group_calendar_test_below_min_days_candidates_are_nan():
    # 候補日数がGROUP_CALENDAR_MIN_DAYS(5)未満の候補条件(例: 毎月31日)はNaN
    dates = pd.date_range('2026-01-01', periods=10, freq='D')
    df = pd.DataFrame({
        '日付': dates.strftime('%Y-%m-%d'), 'ホール名': 'テスト店', 'high_prob': 0.5,
    })
    group_series = pd.Series('G1', index=df.index)
    result = pg.group_calendar_test(df, 'テスト店', group_series)
    row = result[(result['グループ'] == 'G1') & (result['日付条件'] == '毎月_31日')].iloc[0]
    assert pd.isna(row['p_raw'])
    assert pd.isna(row['効果量'])


def test_group_calendar_test_no_matching_hole_returns_empty():
    dates = pd.date_range('2026-01-01', periods=10, freq='D')
    df = pd.DataFrame({
        '日付': dates.strftime('%Y-%m-%d'), 'ホール名': '別の店', 'high_prob': 0.5,
    })
    group_series = pd.Series('G1', index=df.index)
    result = pg.group_calendar_test(df, 'テスト店', group_series)
    assert result.empty
