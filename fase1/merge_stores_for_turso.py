"""
Turso移行用: ホールデータ/配下の店舗ごとのSQLiteファイルを1つのファイルに統合する。
一度きりの移行作業用スクリプト（恒久的なパイプラインには組み込まない）。

実行後、生成された ホールデータ/merged_for_turso.db を
Turso ダッシュボードの「Upload Database」でアップロードする。
"""
import os
import sqlite3

# db.py はTursoクライアント(libsql)に依存するため、ここでは重複させずSQL定義のみ直接記載する
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

SOURCE_DIR = os.path.join(os.path.dirname(__file__), '..', 'ホールデータ')
OUTPUT_PATH = os.path.join(SOURCE_DIR, 'merged_for_turso.db')


def main():
    if os.path.exists(OUTPUT_PATH):
        os.remove(OUTPUT_PATH)

    dest = sqlite3.connect(OUTPUT_PATH)
    dest_cur = dest.cursor()
    dest_cur.execute(_CREATE_TABLE_SQL)
    dest_cur.execute(_CREATE_MISSING_TABLE_SQL)
    dest.commit()

    total_slot = 0
    total_missing = 0

    for name in sorted(os.listdir(SOURCE_DIR)):
        if not name.endswith('.db') or name == 'merged_for_turso.db':
            continue
        src_path = os.path.join(SOURCE_DIR, name)
        src = sqlite3.connect(src_path)
        src_cur = src.cursor()

        src_cur.execute('''
            SELECT 日付, ホール名, 機種名, 台番号, 回転数, 差枚, BB, RB, ART,
                   BB確率, RB確率, ART確率, 合成確率
            FROM slot_data
        ''')
        rows = src_cur.fetchall()
        dest_cur.executemany('''
            INSERT OR IGNORE INTO slot_data
                (日付, ホール名, 機種名, 台番号, 回転数, 差枚, BB, RB, ART, BB確率, RB確率, ART確率, 合成確率)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', rows)
        total_slot += len(rows)

        src_cur.execute('SELECT 日付, ホール名, 機種名, 理由, 記録日時 FROM missing_data')
        missing_rows = src_cur.fetchall()
        dest_cur.executemany('''
            INSERT INTO missing_data (日付, ホール名, 機種名, 理由, 記録日時)
            VALUES (?, ?, ?, ?, ?)
        ''', missing_rows)
        total_missing += len(missing_rows)

        src.close()
        print(f'{name}: slot_data {len(rows)}件, missing_data {len(missing_rows)}件を統合')

    dest.commit()
    dest.close()
    print(f'完了: 合計 slot_data {total_slot}件, missing_data {total_missing}件 → {OUTPUT_PATH}')


if __name__ == '__main__':
    main()
