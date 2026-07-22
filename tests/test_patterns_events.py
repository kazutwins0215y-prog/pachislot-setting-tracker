"""
patterns_events.py の回帰テスト。
detect_events / detect_all_events / is_new_series / detect_introduction_events。
"""
import pandas as pd
import pytest

import patterns_events as pe


# ── detect_events ─────────────────────────────────────────────────

def _make_unit_sets_df(hole, machine, date_unit_sets):
    """date_unit_sets: [(日付, [台番号,...]), ...] から台×日の行DataFrameを作る。"""
    rows = []
    for date_, units in date_unit_sets:
        for u in units:
            rows.append({'日付': date_, 'ホール名': hole, '機種名': machine, '台番号': u})
    return pd.DataFrame(rows)


def test_detect_events_classifies_move_increase_decrease():
    # day1:{1,2,3} → day2:{1,2,4}(3が消え4が現れる=移動) →
    # day3:{1,2,4,5,6}(純増台) → day4:{1,2}(3台とも撤去)
    df = _make_unit_sets_df('テスト店', 'A', [
        ('2026-01-01', [1, 2, 3]),
        ('2026-01-02', [1, 2, 4]),
        ('2026-01-03', [1, 2, 4, 5, 6]),
        ('2026-01-04', [1, 2]),
    ])
    result = pe.detect_events(df, 'テスト店', 'A').set_index('日付')

    day2 = result.loc['2026-01-02']
    assert day2['移動件数'] == 1
    assert day2['移動台番号'] == [4]
    assert day2['撤去台番号'] == []
    assert day2['増台台番号'] == []

    day3 = result.loc['2026-01-03']
    assert day3['移動件数'] == 0
    assert day3['増台台番号'] == [5, 6]
    assert day3['撤去台番号'] == []

    day4 = result.loc['2026-01-04']
    assert day4['移動件数'] == 0
    assert day4['撤去台番号'] == [4, 5, 6]


def test_detect_events_no_change_produces_no_row():
    df = _make_unit_sets_df('テスト店', 'A', [
        ('2026-01-01', [1, 2]),
        ('2026-01-02', [1, 2]),
    ])
    result = pe.detect_events(df, 'テスト店', 'A')
    assert result.empty


def test_detect_all_events_concatenates_all_machines():
    df = pd.concat([
        _make_unit_sets_df('テスト店', 'A', [('2026-01-01', [1]), ('2026-01-02', [2])]),
        _make_unit_sets_df('テスト店', 'B', [('2026-01-01', [9]), ('2026-01-02', [8])]),
    ], ignore_index=True)
    result = pe.detect_all_events(df)
    assert set(result['機種名']) == {'A', 'B'}
    assert len(result) == 2  # 各機種1件ずつ(day2のみ変化あり)


def test_detect_all_events_empty_input_returns_empty_with_columns():
    df = pd.DataFrame(columns=['日付', 'ホール名', '機種名', '台番号'])
    result = pe.detect_all_events(df)
    assert result.empty
    assert list(result.columns) == [
        '日付', 'ホール名', '機種名', '移動件数', '移動台番号', '撤去台番号', '増台台番号',
    ]


# ── is_new_series ─────────────────────────────────────────────────

def test_is_new_series_true_for_newly_appeared_unit():
    df = _make_unit_sets_df('テスト店', 'A', [
        ('2026-01-01', [1, 2, 3]),
        ('2026-01-02', [1, 2, 4]),
    ])
    assert pe.is_new_series(df, 'テスト店', 'A', 4, '2026-01-02') is True


def test_is_new_series_false_for_unit_present_previous_day():
    df = _make_unit_sets_df('テスト店', 'A', [
        ('2026-01-01', [1, 2, 3]),
        ('2026-01-02', [1, 2, 4]),
    ])
    assert pe.is_new_series(df, 'テスト店', 'A', 1, '2026-01-02') is False


def test_is_new_series_false_for_first_date():
    df = _make_unit_sets_df('テスト店', 'A', [('2026-01-01', [1, 2, 3])])
    assert pe.is_new_series(df, 'テスト店', 'A', 1, '2026-01-01') is False


def test_is_new_series_false_for_unknown_date():
    df = _make_unit_sets_df('テスト店', 'A', [('2026-01-01', [1, 2, 3])])
    assert pe.is_new_series(df, 'テスト店', 'A', 1, '2026-99-99') is False


# ── detect_introduction_events ────────────────────────────────────

def test_detect_introduction_events_first_day_is_hantei_funou():
    # 機種Aの初出日が店舗観測開始日と一致 → '判別不能'(左打ち切り)
    df = _make_unit_sets_df('テスト店', 'A', [('2026-01-01', [1, 2])])
    result = pe.detect_introduction_events(df, 'テスト店')
    row = result[result['日付'] == '2026-01-01'].iloc[0]
    assert row['カテゴリ'] == '判別不能'


def test_detect_introduction_events_new_machine_after_store_start_is_shintai():
    # 機種Bは店舗観測開始日(2026-01-01)より後の初出 → '新台'
    df = pd.concat([
        _make_unit_sets_df('テスト店', 'A', [
            ('2026-01-01', [1]), ('2026-01-02', [1]), ('2026-01-03', [1]),
        ]),
        _make_unit_sets_df('テスト店', 'B', [('2026-01-03', [10, 11])]),
    ], ignore_index=True)
    result = pe.detect_introduction_events(df, 'テスト店')
    row = result[(result['機種名'] == 'B') & (result['日付'] == '2026-01-03')].iloc[0]
    assert row['カテゴリ'] == '新台'
    assert row['台番号リスト'] == [10, 11]


def test_detect_introduction_events_increase_decrease_and_pure_move():
    # 機種C: day0{1,2}(判別不能,無視) → day1{1,2,3}(増台) →
    # day2{1,3}(2が消える。以降absence_threshold(7)日間2が戻らないため減台確定) →
    # day3{1,9}(3が消え9が現れる=純移動) → day4〜9{1,9}維持(day2減台確認に必要な未来枠。
    # window_end=curr_idx(2)+7=9 < len(store_dates)を満たすため店舗観測日は10日必要)
    dates = pd.date_range('2026-01-01', periods=10, freq='D').strftime('%Y-%m-%d').tolist()
    unit_sets = [
        (dates[0], [1, 2]),
        (dates[1], [1, 2, 3]),
        (dates[2], [1, 3]),
        (dates[3], [1, 9]),
        (dates[4], [1, 9]),
        (dates[5], [1, 9]),
        (dates[6], [1, 9]),
        (dates[7], [1, 9]),
        (dates[8], [1, 9]),
        (dates[9], [1, 9]),
    ]
    df = _make_unit_sets_df('テスト店', 'C', unit_sets)
    result = pe.detect_introduction_events(df, 'テスト店').set_index('日付')

    row1 = result.loc[dates[1]]
    assert row1['カテゴリ'] == '増台'
    assert row1['台数変化'] == 1

    row2 = result.loc[dates[2]]
    assert row2['カテゴリ'] == '減台'
    assert row2['台数変化'] == -1

    row3 = result.loc[dates[3]]
    assert row3['カテゴリ'] == '純移動'
    assert bool(row3['移動フラグ']) is True
    assert row3['移動台番号リスト'] == [9]


def test_detect_introduction_events_reintroduction_after_long_absence():
    # 埋め草機種Fillerが全期間在籍し店舗観測日を連続させる。
    # 機種Dはday0(判別不能,無視)〜day3まで在籍、day4〜11(8日間)不在、day12に再出現
    # → 不在日数(店舗観測日ベース) = 12 - 3 - 1 = 8 >= INTRODUCTION_ABSENCE_THRESHOLD(7) → 再導入
    dates = pd.date_range('2026-01-01', periods=20, freq='D').strftime('%Y-%m-%d').tolist()
    filler = _make_unit_sets_df('テスト店', 'Filler', [(d, [900]) for d in dates])
    d_dates = [(dates[i], [1]) for i in range(4)] + [(dates[12], [2])]
    machine_d = _make_unit_sets_df('テスト店', 'D', d_dates)
    df = pd.concat([filler, machine_d], ignore_index=True)

    result = pe.detect_introduction_events(df, 'テスト店')
    row = result[(result['機種名'] == 'D') & (result['日付'] == dates[12])].iloc[0]
    assert row['カテゴリ'] == '再導入'
    assert row['不在日数'] == 8


def test_detect_introduction_events_pending_decrease_not_confirmed_without_future_days():
    # 減台候補の直後に十分な未来日(absence_threshold=7日分)が無い場合は確定させない
    dates = ['2026-01-01', '2026-01-02', '2026-01-03']
    df = _make_unit_sets_df('テスト店', 'E', [
        (dates[0], [1, 2]),
        (dates[1], [1, 2, 3]),
        (dates[2], [1, 3]),  # 2が消えるが未来日が2日分しかなく確定不可
    ])
    result = pe.detect_introduction_events(df, 'テスト店')
    assert result[result['日付'] == dates[2]].empty
