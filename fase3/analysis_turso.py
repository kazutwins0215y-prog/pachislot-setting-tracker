"""
analysis_turso.py — 分析用Turso(libSQL)への埋め込みレプリカ接続ヘルパー

fase1/db.py の get_connection() と同じ方式で、生データDBとは物理分離された
分析専用Tursoデータベースへ接続する(設計は fase3/配信公開_設計.md 参照)。

- PC側(upload_analysis.py): レプリカファイルは `ホールデータ/turso_analysis_replica.db`
- クラウド側(bootstrap.py): レプリカファイルを `fase2/data_source.py` の
  `ANALYSIS_DB_PATH` に直接置く(レプリカはSQLite互換ファイルなのでfase2から見て
  通常のanalysis.dbと区別がつかない)

レプリカファイルのパスを呼び出し側の引数で受け取ることで、この2用途を1つの
ヘルパーで共用する。
"""
import os
import logging
from pathlib import Path

import libsql
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent.parent / '.env')


def get_connection(replica_path: str | Path):
    """
    分析用Turso(libSQL)への埋め込みレプリカ接続を返す。
    TURSO_ANALYSIS_DATABASE_URL / TURSO_ANALYSIS_AUTH_TOKEN 環境変数が必須。

    接続時にsync()を実行し、リモートの最新状態をreplica_pathへ反映する。
    以降の読み取りはローカル、書き込みはリモートプライマリへ委譲される。
    """
    replica_path = Path(replica_path)
    replica_path.parent.mkdir(parents=True, exist_ok=True)
    con = libsql.connect(
        str(replica_path),
        sync_url=os.environ['TURSO_ANALYSIS_DATABASE_URL'],
        auth_token=os.environ['TURSO_ANALYSIS_AUTH_TOKEN'],
    )
    con.sync()
    return con


def sync_replica(con) -> bool:
    """リモートの最新状態をローカルレプリカへ反映する。失敗してもFalseを返すのみ
    (次回接続時のsyncで回復するため)。"""
    try:
        con.sync()
        return True
    except Exception as e:
        logger.warning(f'分析用レプリカの同期に失敗しました(次回実行時に再同期されます): {e}')
        return False
