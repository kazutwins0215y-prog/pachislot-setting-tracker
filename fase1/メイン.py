from datetime import datetime as dt, timedelta
import json
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(__file__))

from scraper import build_url, get_info, create_session, AccessForbiddenError
from db import (
    get_connection, setup_db, get_processed_dates,
    write_db, write_missing, write_null_record, sync_replica,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

TARGET_CYCLE  = 40   # 1リクエストあたりの目標サイクル時間（秒）
MIN_SLEEP     = 10   # 最低待機時間（秒）
BATCH_SIZE    = 20   # この件数ごとに長めの休憩を挟む
BATCH_BREAK   = 60 * 5  # バッチ休憩時間（秒）

INITIAL_BACKFILL_DAYS = int(os.environ.get('INITIAL_BACKFILL_DAYS', 90))  # 新規店舗追加時: 初回のみ何日分さかのぼって取得するか（環境変数で一時的に上書き可能）
COLLECT_UNTIL_DAYS_AGO = 2   # 何日前までを収集対象にするか（サイト側の当日・前日データ未更新に備える）
RETRY_LOOKBACK_DAYS = 14     # 取得失敗等で空いた未処理日(ギャップ)を何日前まで再試行するか

STORES_FILE = os.path.join(os.path.dirname(__file__), 'stores.json')


def load_stores() -> list[str]:
    with open(STORES_FILE, encoding='utf-8') as f:
        config = json.load(f)
    return config['stores']


def compute_remaining_days(processed: set, today: dt) -> list[str]:
    """
    取得対象の日付リスト(YYYY-MM-DD、昇順)を返す。

    - 通常: 前回取得済み最終日の翌日〜収集対象最終日
    - ギャップ再試行: 途中の日が取得失敗すると「最終日の翌日から」だけでは
      永久にスキップされるため、直近RETRY_LOOKBACK_DAYS内は未処理日を含めて
      走査対象に含める(取得済みの日はprocessedで除外されるので再取得はしない)
    - 新規店舗: INITIAL_BACKFILL_DAYS分さかのぼる
    """
    end_date = today - timedelta(days=COLLECT_UNTIL_DAYS_AGO)

    if processed:
        last_date = max(dt.strptime(d, '%Y-%m-%d') for d in processed)
        retry_start = end_date - timedelta(days=RETRY_LOOKBACK_DAYS)
        start_date = min(last_date + timedelta(days=1), retry_start)
    else:
        start_date = today - timedelta(days=INITIAL_BACKFILL_DAYS)

    day_list = []
    d = start_date
    while d.date() <= end_date.date():
        day_list.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    return [day for day in day_list if day not in processed]


def process_store(con, hole_name: str):
    processed = get_processed_dates(con, hole_name)
    remaining = compute_remaining_days(processed, dt.now())

    if not remaining:
        logger.info(f'{hole_name}: 対象期間のデータはすべてDB済みです')
        return

    logger.info(f'{hole_name}: {len(remaining)} 日分を取得します')

    session = create_session()
    try:
        for i, day in enumerate(remaining):
            url = build_url(hole_name, day)
            t_start = time.monotonic()
            try:
                data_list, data_column_list, data_row_list, missing_machines = get_info(session, url, day)
                if data_list:
                    write_db(con, data_list, data_column_list, data_row_list, hole_name, day)
                for machine_name, reason in missing_machines:
                    write_missing(con, hole_name, day, machine_name, reason)
                    logger.warning(f'{day} 欠損記録: 機種={machine_name!r} 理由={reason}')
                    if machine_name:
                        write_null_record(con, hole_name, day, machine_name)
            except AccessForbiddenError:
                # 403はIP単位のブロックのため、この店舗だけでなく全店舗の処理を中止する
                # (残り店舗への無駄なリクエストで被ブロック実績を積まない)
                raise
            except Exception as e:
                logger.error(f'{hole_name}: {day} の処理に失敗: {e}')
            if i < len(remaining) - 1:
                if (i + 1) % BATCH_SIZE == 0:
                    logger.info(f'{i + 1}件完了。{BATCH_BREAK}秒のバッチ休憩に入ります')
                    session.close()
                    time.sleep(BATCH_BREAK)
                    session = create_session()
                    logger.info('セッションを再生成しました')
                else:
                    elapsed = time.monotonic() - t_start
                    sleep_time = max(MIN_SLEEP, TARGET_CYCLE - elapsed)
                    logger.debug(f'経過 {elapsed:.1f}秒 → {sleep_time:.1f}秒待機')
                    time.sleep(sleep_time)
    finally:
        session.close()


def main():
    stores = load_stores()
    con = get_connection()
    try:
        setup_db(con)
        try:
            for hole_name in stores:
                process_store(con, hole_name)
        except AccessForbiddenError as e:
            logger.error(f'アクセス拒否(403)のため全店舗の処理を中止します: {e}')
        # 書き込みはリモートへ委譲済みのため、最後にローカルレプリカへ反映して
        # fase2(分析・可視化)が最新データを読めるようにする
        sync_replica(con)
    finally:
        con.close()


if __name__ == '__main__':
    main()
