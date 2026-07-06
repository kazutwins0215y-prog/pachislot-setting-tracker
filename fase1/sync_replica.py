"""
sync_replica.py — Tursoの最新データをローカルレプリカへ同期する

ホールデータ/turso_replica.db (fase2の分析・可視化が読むSQLite互換ファイル) を
Tursoの最新状態に更新する。メイン.py(データ収集)は実行の最後に自動で同期する
ため、通常このスクリプトの実行は不要。以下の場合に使う:

- 初回セットアップ(レプリカファイルがまだ存在しない)
- 収集を伴わずに分析用データだけを最新化したい場合
  (例: GitHub Actions等の別環境で収集された後にPCで分析する場合)

実行方法(TURSO_DATABASE_URL / TURSO_AUTH_TOKEN 環境変数が必須):
    py -3.12 fase1/sync_replica.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from db import get_connection, setup_db, REPLICA_PATH

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def main():
    logger.info(f'Tursoからローカルレプリカへ同期します: {REPLICA_PATH}')
    con = get_connection()  # 接続時にsync()が実行される
    try:
        setup_db(con)
        cur = con.cursor()
        cur.execute('SELECT COUNT(*), COUNT(DISTINCT ホール名) FROM slot_data')
        n_rows, n_holes = cur.fetchone()
        logger.info(f'同期完了: slot_data {n_rows:,}行 / {n_holes}店舗')
    finally:
        con.close()


if __name__ == '__main__':
    main()
