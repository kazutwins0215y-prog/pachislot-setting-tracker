import sqlite3

import db


def _make_con():
    """missing_dataテーブルだけを持つインメモリsqlite3接続(libsql不要)。
    db.get_no_data_giveup_datesがlibsql固有APIを使わず標準SQLのみで書かれていることの確認も兼ねる。"""
    con = sqlite3.connect(':memory:')
    con.execute('''
        CREATE TABLE missing_data (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            日付     TEXT NOT NULL,
            ホール名 TEXT NOT NULL,
            機種名   TEXT,
            理由     TEXT,
            記録日時 TEXT
        )
    ''')
    return con


def _insert(con, hole_name, date_, reason, recorded_at):
    con.execute(
        'INSERT INTO missing_data (日付, ホール名, 機種名, 理由, 記録日時) VALUES (?, ?, ?, ?, ?)',
        (date_, hole_name, None, reason, recorded_at),
    )


def test_multiple_records_same_calendar_day_count_as_one_day():
    """同一暦日に複数回記録があっても1暦日としてしか数えない→リタイアしない(3暦日未満)"""
    con = _make_con()
    hole = 'テスト店'
    for hhmmss in ('09:00:00', '09:15:00', '10:30:00'):
        _insert(con, hole, '2026-07-10', 'ページにデータなし', f'2026-07-10 {hhmmss}')

    result = db.get_no_data_giveup_dates(con, hole, giveup_days=3)
    assert '2026-07-10' not in result


def test_three_distinct_calendar_days_triggers_giveup():
    """3つの異なる暦日にわたって記録があればリタイア対象に入る"""
    con = _make_con()
    hole = 'テスト店'
    for calendar_day in ('2026-07-10', '2026-07-11', '2026-07-12'):
        _insert(con, hole, '2026-07-10', 'ページにデータなし', f'{calendar_day} 09:00:00')

    result = db.get_no_data_giveup_dates(con, hole, giveup_days=3)
    assert '2026-07-10' in result


def test_two_distinct_calendar_days_does_not_trigger_giveup():
    """2暦日ではまだリタイアしない(閾値=3未満)"""
    con = _make_con()
    hole = 'テスト店'
    for calendar_day in ('2026-07-10', '2026-07-11'):
        _insert(con, hole, '2026-07-10', 'ページにデータなし', f'{calendar_day} 09:00:00')

    result = db.get_no_data_giveup_dates(con, hole, giveup_days=3)
    assert '2026-07-10' not in result


def test_other_reason_is_not_counted():
    """理由が'ページにデータなし'以外の欠損記録は数えない"""
    con = _make_con()
    hole = 'テスト店'
    for calendar_day in ('2026-07-10', '2026-07-11', '2026-07-12'):
        _insert(con, hole, '2026-07-10', 'カラム数特定不可', f'{calendar_day} 09:00:00')

    result = db.get_no_data_giveup_dates(con, hole, giveup_days=3)
    assert '2026-07-10' not in result
