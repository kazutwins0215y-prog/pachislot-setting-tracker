"""
score.py のDB書き込み関数の回帰テスト(一時sqliteファイル、tmp_path使用)。
write_group_calendar_conditions(グループ種別ごとに削除・再現テスト) /
write_prediction_log(重複追記ガード=append-only) /
write_stage3_scores(全削除→再挿入・サブスコア列マイグレーション) /
write_machine_judgment_log(INSERT OR IGNORE冪等・戻り値差分) /
update_store_profile(ALTER TABLE列追加の冪等)。
"""
import sqlite3

import pandas as pd
import pytest

import score as sc


# ── write_group_calendar_conditions: グループ種別ごとに削除の再現テスト ──

def test_write_group_calendar_conditions_does_not_delete_other_group_types(tmp_path):
    # 過去バグの回帰: 末尾版('台番号末尾')書き込み後に機種版('機種')を書き込むと
    # 末尾版の既存行が消えていた(group_types未指定=全削除だった旧実装)
    db_path = str(tmp_path / 'analysis.db')
    tail_df = pd.DataFrame([{
        'グループ種別': '台番号末尾', 'グループ': 'グループ末尾_1', '日付条件': '曜日_金',
        '該当日数': 10, 'p_raw': 0.01, '効果量': 0.5, 'BH有意': True,
    }])
    sc.write_group_calendar_conditions(db_path, 'テスト店', tail_df, '2026-07-01', group_types='台番号末尾')

    machine_df = pd.DataFrame([{
        'グループ種別': '機種', 'グループ': 'A', '日付条件': '恒常',
        '該当日数': 20, 'p_raw': 0.02, '効果量': 0.4, 'BH有意': True,
    }])
    sc.write_group_calendar_conditions(db_path, 'テスト店', machine_df, '2026-07-01', group_types='機種')

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            'SELECT グループ種別, グループ FROM group_calendar_conditions WHERE ホール名 = ? ORDER BY グループ種別',
            ('テスト店',),
        ).fetchall()
    finally:
        con.close()
    assert set(rows) == {('台番号末尾', 'グループ末尾_1'), ('機種', 'A')}


def test_write_group_calendar_conditions_replaces_same_group_type(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    df1 = pd.DataFrame([{
        'グループ種別': '台番号末尾', 'グループ': 'グループ末尾_1', '日付条件': '曜日_金',
        '該当日数': 10, 'p_raw': 0.01, '効果量': 0.5, 'BH有意': True,
    }])
    sc.write_group_calendar_conditions(db_path, 'テスト店', df1, '2026-07-01', group_types='台番号末尾')

    df2 = pd.DataFrame([{
        'グループ種別': '台番号末尾', 'グループ': 'グループ末尾_2', '日付条件': '曜日_土',
        '該当日数': 8, 'p_raw': 0.03, '効果量': 0.4, 'BH有意': True,
    }])
    sc.write_group_calendar_conditions(db_path, 'テスト店', df2, '2026-07-02', group_types='台番号末尾')

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            'SELECT グループ FROM group_calendar_conditions WHERE ホール名 = ?', ('テスト店',),
        ).fetchall()
    finally:
        con.close()
    assert rows == [('グループ末尾_2',)]  # 旧グループ末尾_1は削除され、末尾_2だけが残る


# ── write_prediction_log: 重複追記ガード ──────────────────────────

def _make_prediction_rows(last_date, n=2, hole='テスト店', pred_type='鉄板台'):
    return [
        {
            '実行日時': '2026-07-01 10:00:00', '使用データ最終日': last_date,
            '対象日': '2026-07-02', 'ホール名': hole, '機種名': 'A', '台番号': i,
            '予測種別': pred_type, '長期スコア': 0.3, '短期スコア': 0.4,
            'ブレンド値': 0.35, '使用alpha': 0.3, '詳細': {'note': 'test'},
        }
        for i in range(1, n + 1)
    ]


def test_write_prediction_log_duplicate_batch_is_skipped(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    rows = _make_prediction_rows('2026-07-01', n=2)
    sc.write_prediction_log(db_path, rows)
    sc.write_prediction_log(db_path, rows)  # 同一(ホール名,予測種別,使用データ最終日)の再投入

    con = sqlite3.connect(db_path)
    try:
        count = con.execute('SELECT COUNT(*) FROM prediction_log').fetchone()[0]
    finally:
        con.close()
    assert count == 2  # 重複追記されず1バッチ分のみ


def test_write_prediction_log_new_last_date_is_appended(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    sc.write_prediction_log(db_path, _make_prediction_rows('2026-07-01', n=2))
    sc.write_prediction_log(db_path, _make_prediction_rows('2026-07-02', n=2))  # 別の使用データ最終日

    con = sqlite3.connect(db_path)
    try:
        count = con.execute('SELECT COUNT(*) FROM prediction_log').fetchone()[0]
    finally:
        con.close()
    assert count == 4  # append-onlyで両バッチとも残る


# ── write_stage3_scores: 全削除→再挿入・サブスコア列マイグレーション ──

def _make_stage3_df(units, extra_cols=None):
    n = len(units)
    df = pd.DataFrame({
        '日付': ['2026-07-01'] * n, '機種名': ['A'] * n, '台番号': units,
        'log_odds': [0.1] * n, 'high_prob': [0.5] * n, 'is_invalid': [False] * n,
    })
    if extra_cols:
        for col, vals in extra_cols.items():
            df[col] = vals
    return df


def test_write_stage3_scores_replaces_previous_rows_for_hole(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    sc.write_stage3_scores(db_path, 'テスト店', _make_stage3_df([1, 2, 3]))
    sc.write_stage3_scores(db_path, 'テスト店', _make_stage3_df([1, 2]))  # 台数が減った再計算

    con = sqlite3.connect(db_path)
    try:
        count = con.execute(
            'SELECT COUNT(*) FROM stage3_scores WHERE ホール名 = ?', ('テスト店',)
        ).fetchone()[0]
    finally:
        con.close()
    assert count == 2  # 3件→2件に全置換(3+2=5にはならない)


def test_write_stage3_scores_migrates_pattern_columns(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    df = _make_stage3_df([1], extra_cols={'S_全台系': [0.7]})
    sc.write_stage3_scores(db_path, 'テスト店', df)

    con = sqlite3.connect(db_path)
    try:
        cols = [row[1] for row in con.execute('PRAGMA table_info(stage3_scores)').fetchall()]
        value = con.execute(
            'SELECT "S_全台系" FROM stage3_scores WHERE ホール名 = ? AND 台番号 = ?', ('テスト店', 1)
        ).fetchone()[0]
    finally:
        con.close()
    assert 'S_全台系' in cols
    assert value == pytest.approx(0.7)


def test_write_stage3_scores_missing_required_column_raises():
    df = pd.DataFrame({'日付': ['2026-07-01'], '機種名': ['A']})  # 台番号等が無い
    with pytest.raises(ValueError):
        sc.write_stage3_scores('unused.db', 'テスト店', df)


# ── write_machine_judgment_log: INSERT OR IGNORE冪等・戻り値差分 ──

def _make_judgment_df(dates, hole='テスト店', machine='A'):
    return pd.DataFrame({
        'ホール名': [hole] * len(dates), '日付': dates, '機種名': [machine] * len(dates),
        '台数': [10] * len(dates), '期待高設定台数': [9.0] * len(dates),
        'zスコア': [6.6] * len(dates), 'p値': [0.0001] * len(dates),
        '投入率': [0.9] * len(dates), 'S_全台系': [0.9] * len(dates),
        '判定ラベル': ['全台系'] * len(dates),
    })


def test_write_machine_judgment_log_first_insert_returns_row_count(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    inserted = sc.write_machine_judgment_log(db_path, _make_judgment_df(['2026-07-01', '2026-07-02']))
    assert inserted == 2


def test_write_machine_judgment_log_duplicate_key_is_ignored_and_returns_zero(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    sc.write_machine_judgment_log(db_path, _make_judgment_df(['2026-07-01']))
    inserted_again = sc.write_machine_judgment_log(db_path, _make_judgment_df(['2026-07-01']))
    assert inserted_again == 0

    con = sqlite3.connect(db_path)
    try:
        count = con.execute('SELECT COUNT(*) FROM machine_judgment_log').fetchone()[0]
    finally:
        con.close()
    assert count == 1


def test_write_machine_judgment_log_new_date_returns_only_new_count(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    sc.write_machine_judgment_log(db_path, _make_judgment_df(['2026-07-01']))
    inserted = sc.write_machine_judgment_log(db_path, _make_judgment_df(['2026-07-01', '2026-07-02']))
    assert inserted == 1  # 既存の07-01はIGNORE、新規の07-02のみカウント


# ── update_store_profile: ALTER TABLE列追加の冪等 ─────────────────

def _make_profile_df(s_zentaikei=0.5):
    return pd.DataFrame({'S_全台系': [s_zentaikei] * 5, '日付': [f'2026-07-0{i}' for i in range(1, 6)]})


def test_update_store_profile_creates_schema_with_migrated_columns(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    sc.update_store_profile(db_path, 'テスト店', _make_profile_df())

    con = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in con.execute('PRAGMA table_info(store_profile)').fetchall()}
    finally:
        con.close()
    for expected_col in ['上限キャリブレーション値', '上限信頼度', '遷移_ベース率', '遷移_p_stay', '遷移_p_up', '遷移_ペア数']:
        assert expected_col in cols


def test_update_store_profile_upsert_replaces_value_without_duplicating_rows(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    sc.update_store_profile(db_path, 'テスト店', _make_profile_df(s_zentaikei=0.5))
    sc.update_store_profile(db_path, 'テスト店', _make_profile_df(s_zentaikei=0.8))  # 再実行で上書き

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            'SELECT スコア FROM store_profile WHERE ホール名 = ? AND パターン = ?',
            ('テスト店', 's_all'),
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1  # PRIMARY KEY (ホール名,パターン)で1行のまま
    assert rows[0][0] == pytest.approx(0.8)  # 最新実行の値に上書きされている


def test_update_store_profile_called_twice_does_not_raise_on_alter_table(tmp_path):
    db_path = str(tmp_path / 'analysis.db')
    sc.update_store_profile(db_path, 'テスト店', _make_profile_df())
    sc.update_store_profile(db_path, 'テスト店', _make_profile_df())  # ALTER TABLE二重実行にならないこと
