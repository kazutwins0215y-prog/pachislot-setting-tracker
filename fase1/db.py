import os
import logging

import libsql

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = '''
    CREATE TABLE IF NOT EXISTS slot_data (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        日付     TEXT NOT NULL,
        ホール名 TEXT NOT NULL,
        機種名   TEXT NOT NULL,
        台番号   INTEGER,
        回転数   INTEGER,
        差枚     INTEGER,
        BB       INTEGER,
        RB       INTEGER,
        ART      INTEGER,
        BB確率   REAL,
        RB確率   REAL,
        ART確率  REAL,
        合成確率 REAL,
        UNIQUE(日付, ホール名, 機種名, 台番号)
    )
'''

_CREATE_MISSING_TABLE_SQL = '''
    CREATE TABLE IF NOT EXISTS missing_data (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        日付     TEXT NOT NULL,
        ホール名 TEXT NOT NULL,
        機種名   TEXT,
        理由     TEXT,
        記録日時 TEXT DEFAULT (datetime('now', 'localtime'))
    )
'''


def _to_int(s) -> int | None:
    if s is None:
        return None
    try:
        return int(str(s).replace(',', ''))
    except (ValueError, TypeError):
        return None


def _to_prob(s) -> float | None:
    """'1/298.3' → 0.003353... に変換。分母0またはパース失敗はNULL。"""
    if s is None:
        return None
    try:
        parts = str(s).split('/')
        if len(parts) != 2:
            return None
        denom = float(parts[1])
        return 1.0 / denom if denom != 0 else None
    except (ValueError, TypeError):
        return None


def get_connection():
    """Turso(libSQL)への接続を返す。TURSO_DATABASE_URL / TURSO_AUTH_TOKEN 環境変数が必須。"""
    return libsql.connect(
        database=os.environ['TURSO_DATABASE_URL'],
        auth_token=os.environ['TURSO_AUTH_TOKEN'],
    )


def setup_db(con):
    cur = con.cursor()
    cur.execute(_CREATE_TABLE_SQL)
    cur.execute(_CREATE_MISSING_TABLE_SQL)
    con.commit()


def get_processed_dates(con, hole_name: str) -> set:
    cur = con.cursor()
    cur.execute('SELECT DISTINCT 日付 FROM slot_data WHERE ホール名 = ?', (hole_name,))
    return {row[0] for row in cur.fetchall()}


def _parse_row(row, hole_name: str):
    data_cols = row[2:] if len(row) >= 3 else []
    num_cols  = [c for c in data_cols if not (c and '/' in str(c))]
    prob_cols = [c for c in data_cols if c and '/' in str(c)]

    gosei = prob_cols[0] if prob_cols else None
    probs = prob_cols[1:]

    return (
        row[0]                                          if len(row) >= 1 else None,  # 日付
        hole_name,                                                                    # ホール名
        row[1]                                          if len(row) >= 2 else None,  # 機種名
        _to_int(num_cols[0])  if len(num_cols) > 0 else None,                        # 台番号
        _to_int(num_cols[1])  if len(num_cols) > 1 else None,                        # 回転数
        _to_int(num_cols[2])  if len(num_cols) > 2 else None,                        # 差枚
        _to_int(num_cols[3])  if len(num_cols) > 3 else None,                        # BB
        _to_int(num_cols[4])  if len(num_cols) > 4 else None,                        # RB
        _to_int(num_cols[5])  if len(num_cols) > 5 else None,                        # ART
        _to_prob(probs[0])    if len(probs)   > 0 else None,                         # BB確率
        _to_prob(probs[1])    if len(probs)   > 1 else None,                         # RB確率
        _to_prob(probs[2])    if len(probs)   > 2 else None,                         # ART確率
        _to_prob(gosei),                                                              # 合成確率
    )


def write_db(con, data_list, data_column_list, data_row_list, hole_name: str, hole_date: str):
    cur = con.cursor()
    start = 0
    rows = []
    for col_count, row_count in zip(data_column_list, data_row_list):
        for _ in range(row_count):
            end = start + col_count
            rows.append(_parse_row(data_list[start:end], hole_name))
            start = end

    cur.executemany('''
        INSERT OR IGNORE INTO slot_data
            (日付, ホール名, 機種名, 台番号, 回転数, 差枚, BB, RB, ART, BB確率, RB確率, ART確率, 合成確率)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', rows)
    con.commit()
    logger.info(f'{hole_name} に {len(rows)} 件挿入しました ({hole_date})')


def write_missing(con, hole_name: str, hole_date: str, machine_name: str | None, reason: str):
    """欠損記録を missing_data テーブルに保存する。"""
    cur = con.cursor()
    cur.execute(
        'INSERT INTO missing_data (日付, ホール名, 機種名, 理由) VALUES (?, ?, ?, ?)',
        (hole_date, hole_name, machine_name, reason),
    )
    con.commit()


def write_null_record(con, hole_name: str, hole_date: str, machine_name: str):
    """機種は特定できたがデータ取得失敗した場合、数値列NULLのプレースホルダーをslot_dataに挿入する。"""
    cur = con.cursor()
    cur.execute(
        'SELECT 1 FROM slot_data WHERE 日付=? AND ホール名=? AND 機種名=? AND 台番号 IS NULL AND 回転数 IS NULL',
        (hole_date, hole_name, machine_name),
    )
    if cur.fetchone():
        return
    cur.execute(
        'INSERT INTO slot_data (日付, ホール名, 機種名) VALUES (?, ?, ?)',
        (hole_date, hole_name, machine_name),
    )
    con.commit()
