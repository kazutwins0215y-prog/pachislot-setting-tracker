"""
data_source.py — fase2共通のデータ読み込み層

fase1が維持するTurso埋め込みレプリカ(ホールデータ/turso_replica.db)を
読み取り専用で参照し、分析成果物(stage3_scores / store_profile)は
ローカル専用の分析DB(ホールデータ/analysis.db)に保存する。

- レプリカは fase1/メイン.py 実行時に自動更新される。収集を伴わず最新化する
  場合は `py -3.12 fase1/sync_replica.py` を実行する。
- fase2からTursoへ直接接続はしない(読み取り行数課金の回避と、
  libsql/Python3.12依存をfase1に閉じ込めるため)。
- 旧構成の店舗別DB(ホールデータ/{店舗名}.db)はTurso移行前のアーカイブであり、
  fase2はもう参照しない。
"""
import sqlite3
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / 'ホールデータ'

# fase1/db.py の REPLICA_PATH と同じファイル(fase2はlibsqlに依存しないため独立に定義)
REPLICA_DB_PATH = _DATA_DIR / 'turso_replica.db'
# 分析成果物(stage3_scores / store_profile)の保存先。ローカル専用・再計算で再生成可能
ANALYSIS_DB_PATH = _DATA_DIR / 'analysis.db'

MISSING_REPLICA_MSG = (
    f'レプリカDBが見つかりません: {REPLICA_DB_PATH}\n'
    'fase1のデータ収集(py -3.12 fase1/メイン.py)を実行するか、'
    '収集せずに同期だけ行う場合は py -3.12 fase1/sync_replica.py を実行してください。'
)


def connect_replica(db_path: str | Path | None = None) -> sqlite3.Connection:
    """レプリカDBへの読み取り専用接続を返す。ファイルが無ければFileNotFoundError。"""
    path = Path(db_path) if db_path is not None else REPLICA_DB_PATH
    if not path.exists():
        raise FileNotFoundError(MISSING_REPLICA_MSG)
    # mode=ro: レプリカはfase1(libsql)が管理するファイルのため誤書き込みを防ぐ
    return sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)


def connect_analysis(db_path: str | Path | None = None) -> sqlite3.Connection:
    """分析DB(stage3_scores / store_profile)への接続を返す。無ければ作成される。"""
    path = Path(db_path) if db_path is not None else ANALYSIS_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(path))


def list_holes(db_path: str | Path | None = None) -> list[str]:
    """レプリカDBに存在する店舗名(ホール名)の一覧をソートして返す。"""
    con = connect_replica(db_path)
    try:
        rows = con.execute('SELECT DISTINCT ホール名 FROM slot_data').fetchall()
    finally:
        con.close()
    return sorted(r[0] for r in rows if r[0])


def latest_replica_date(db_path: str | Path | None = None) -> str | None:
    """レプリカDB(生データ)の最新日付(YYYY-MM-DD)を返す。ファイル無し/データ無しはNone。"""
    try:
        con = connect_replica(db_path)
    except FileNotFoundError:
        return None
    try:
        row = con.execute('SELECT MAX(日付) FROM slot_data').fetchone()
    finally:
        con.close()
    return row[0] if row else None
