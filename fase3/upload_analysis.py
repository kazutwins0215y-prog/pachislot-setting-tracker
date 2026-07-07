"""
upload_analysis.py — 分析DB(ホールデータ/analysis.db)を分析用Tursoへ差分upsertする

fase4/run_daily.py の最後(run_evaluate_and_profile直後)に呼ばれ、ローカルの
analysis.db(run_store_profile.pyの出力=PC側の正)を分析専用Turso(生データDBとは
物理分離)へ転送する。これによりStreamlit Community Cloud側(streamlit_app.py)が
最新の分析成果物を読める状態になる。

設計: fase3/配信公開_設計.md「データ連携設計」節
仕様: fase3/実装指示書.md タスク1

CLI:
    py -3.12 upload_analysis.py           # 差分upsert(日次用)
    py -3.12 upload_analysis.py --full    # 全再構築(アルゴリズム変更で過去日を
                                           # 再計算した場合に手動実行)

実行環境の注意: libsqlのビルド制約によりPython 3.12必須。
"""
import argparse
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import analysis_turso as at

BASE = Path(__file__).resolve().parent.parent
LOCAL_ANALYSIS_DB_PATH = BASE / 'ホールデータ' / 'analysis.db'
REPLICA_PATH = BASE / 'ホールデータ' / 'turso_analysis_replica.db'

REUPSERT_MARGIN_DAYS = 7           # stage3_scoresの直近再upsertマージン(日)
SQL_CHUNK_SIZE = 1000               # 1回のINSERT文にまとめる行数(SQLiteの変数上限約32766を
                                    # 最大列数13でも十分下回るサイズ。1回のexecute()+commit()
                                    # がネットワーク往復の単位になる)
PROGRESS_PRINT_EVERY_ROWS = 20000   # 進捗表示の間隔(行)
DIFF_MODE_WARNING_ROWS = 100_000    # 差分モードでこれを超えたらウォーターマーク不整合を疑いWARNING

# 分析用Tursoが持つテーブルの明示リスト(--fullのDROP対象・生データには構造的に触れない)
TABLES = [
    'stage3_scores', 'prediction_log', 'store_profile',
    'teppan_conditions', 'pattern_history', 'prediction_accuracy',
]
FULL_REPLACE_TABLES = ['store_profile', 'teppan_conditions', 'pattern_history', 'prediction_accuracy']


def _connect_local_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f'分析DBが見つかりません: {db_path}')
    return sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=ro', uri=True)


def _local_table_sql(local_con: sqlite3.Connection, table: str) -> str:
    row = local_con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        raise ValueError(f'ローカルanalysis.dbに{table}テーブルがありません')
    return row[0]


def _remote_scalar(remote_con, sql: str, params: tuple = ()) -> object:
    cur = remote_con.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def _remote_table_names(remote_con) -> set[str]:
    cur = remote_con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in cur.fetchall()}


def _ensure_remote_table(remote_con, local_con: sqlite3.Connection, table: str) -> None:
    if table not in _remote_table_names(remote_con):
        remote_con.execute(_local_table_sql(local_con, table))
        remote_con.commit()


def _table_columns(local_con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in local_con.execute(f'PRAGMA table_info({table})').fetchall()]


def _bulk_insert(remote_con, insert_prefix: str, cols: list[str], rows: list[tuple], label: str = '') -> None:
    """rowsをSQL_CHUNK_SIZE行ごとの複数行VALUES INSERT文にまとめてexecute+commitする。

    cur.executemany()は1行ごとに個別のネットワーク往復が発生し、埋め込みレプリカ経由では
    致命的に遅い(実測: 約0.12秒/行。80万行では十数時間かかる計算になり実際にハングした)。
    複数行分のVALUES句を1回のexecute()にまとめることで往復回数を1/SQL_CHUNK_SIZEに削減する。
    """
    if not rows:
        return
    cur = remote_con.cursor()
    col_list = ', '.join(cols)
    row_placeholder = '(' + ', '.join('?' for _ in cols) + ')'
    printed_at = 0
    for i in range(0, len(rows), SQL_CHUNK_SIZE):
        chunk = rows[i:i + SQL_CHUNK_SIZE]
        values_sql = ', '.join(row_placeholder for _ in chunk)
        params = [v for row in chunk for v in row]
        cur.execute(f'{insert_prefix} ({col_list}) VALUES {values_sql}', params)
        remote_con.commit()
        done = i + len(chunk)
        if label and (done - printed_at >= PROGRESS_PRINT_EVERY_ROWS or done == len(rows)):
            print(f'  [{label}] {done}/{len(rows)}行', flush=True)
            printed_at = done


def _full_replace_table(remote_con, local_con: sqlite3.Connection, table: str) -> int:
    cols = _table_columns(local_con, table)
    col_list = ', '.join(cols)
    rows = local_con.execute(f'SELECT {col_list} FROM {table}').fetchall()

    remote_con.execute(f'DELETE FROM {table}')
    remote_con.commit()
    _bulk_insert(remote_con, f'INSERT INTO {table}', cols, rows, label=table)
    return len(rows)


def _upload_stage3_scores(remote_con, local_con: sqlite3.Connection, full: bool) -> int:
    cols = _table_columns(local_con, 'stage3_scores')
    col_list = ', '.join(cols)

    if full:
        rows = local_con.execute(f'SELECT {col_list} FROM stage3_scores').fetchall()
    else:
        max_date = _remote_scalar(remote_con, 'SELECT MAX(日付) FROM stage3_scores')
        if max_date is None:
            rows = local_con.execute(f'SELECT {col_list} FROM stage3_scores').fetchall()
        else:
            threshold = (date.fromisoformat(max_date) - timedelta(days=REUPSERT_MARGIN_DAYS)).isoformat()
            rows = local_con.execute(
                f'SELECT {col_list} FROM stage3_scores WHERE 日付 > ?', (threshold,)
            ).fetchall()

    _bulk_insert(remote_con, 'INSERT OR REPLACE INTO stage3_scores', cols, rows, label='stage3_scores')
    return len(rows)


def _upload_prediction_log(remote_con, local_con: sqlite3.Connection, full: bool) -> int:
    """予測IDウォーターマーク方式(append-only)。evaluate_predictions.pyはprediction_accuracy
    のみ更新しprediction_logをUPDATEしないため、単純なID差分で足りる。"""
    cols = _table_columns(local_con, 'prediction_log')
    col_list = ', '.join(cols)

    if full:
        rows = local_con.execute(f'SELECT {col_list} FROM prediction_log').fetchall()
    else:
        max_id = _remote_scalar(remote_con, 'SELECT MAX(予測ID) FROM prediction_log')
        if max_id is None:
            rows = local_con.execute(f'SELECT {col_list} FROM prediction_log').fetchall()
        else:
            rows = local_con.execute(
                f'SELECT {col_list} FROM prediction_log WHERE 予測ID > ?', (max_id,)
            ).fetchall()

    _bulk_insert(remote_con, 'INSERT INTO prediction_log', cols, rows, label='prediction_log')
    return len(rows)


def upload(full: bool = False) -> None:
    t_start = time.monotonic()
    local_con = _connect_local_readonly(LOCAL_ANALYSIS_DB_PATH)
    remote_con = at.get_connection(REPLICA_PATH)

    try:
        if full:
            for table in TABLES:
                remote_con.execute(f'DROP TABLE IF EXISTS {table}')
            remote_con.commit()
        for table in TABLES:
            _ensure_remote_table(remote_con, local_con, table)

        summary: list[tuple[str, int, float]] = []

        t0 = time.monotonic()
        n = _upload_stage3_scores(remote_con, local_con, full)
        elapsed = time.monotonic() - t0
        summary.append(('stage3_scores', n, elapsed))
        print(f'stage3_scores 完了: {n}行 ({elapsed:.1f}秒)', flush=True)

        t0 = time.monotonic()
        n = _upload_prediction_log(remote_con, local_con, full)
        elapsed = time.monotonic() - t0
        summary.append(('prediction_log', n, elapsed))
        print(f'prediction_log 完了: {n}行 ({elapsed:.1f}秒)', flush=True)

        for table in FULL_REPLACE_TABLES:
            t0 = time.monotonic()
            n = _full_replace_table(remote_con, local_con, table)
            elapsed = time.monotonic() - t0
            summary.append((table, n, elapsed))
            print(f'{table} 完了: {n}行 ({elapsed:.1f}秒)', flush=True)

        at.sync_replica(remote_con)
    finally:
        local_con.close()

    mode_label = '--full(全再構築)' if full else '差分upsert'
    total = sum(n for _, n, _ in summary)
    print(f'===== upload_analysis.py 完了 (mode={mode_label}) =====')
    for table, n, elapsed in summary:
        print(f'  {table}: {n}行 ({elapsed:.1f}秒)')
    print(f'  合計: {total}行 (総所要時間 {time.monotonic() - t_start:.1f}秒)')

    if not full and total > DIFF_MODE_WARNING_ROWS:
        print(
            f'WARNING: 差分モードでの転送行数が{total}行と異常に多いです。'
            'ウォーターマーク不整合の可能性があります(処理は継続済み)。',
            file=sys.stderr,
        )


def main() -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='分析DB(analysis.db)を分析用Tursoへ差分upsertする')
    parser.add_argument('--full', action='store_true', help='6テーブルを全再構築する(DROP→全件INSERT)')
    args = parser.parse_args()
    upload(full=args.full)


if __name__ == '__main__':
    main()
