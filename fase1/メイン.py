from datetime import datetime as dt, timedelta
import json
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(__file__))

from scraper import build_url, get_info, create_session, AccessForbiddenError
from db import get_connection, setup_db, get_processed_dates, write_db, write_missing, write_null_record

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

TARGET_CYCLE  = 40   # 1リクエストあたりの目標サイクル時間（秒）
MIN_SLEEP     = 10   # 最低待機時間（秒）
BATCH_SIZE    = 20   # この件数ごとに長めの休憩を挟む
BATCH_BREAK   = 60 * 5  # バッチ休憩時間（秒）

INITIAL_BACKFILL_DAYS = 90   # 新規店舗追加時: 初回のみ何日分さかのぼって取得するか

STORES_FILE = os.path.join(os.path.dirname(__file__), 'stores.json')


def load_stores() -> list[str]:
    with open(STORES_FILE, encoding='utf-8') as f:
        config = json.load(f)
    return config['stores']


def process_store(con, hole_name: str):
    processed = get_processed_dates(con, hole_name)
    today = dt.now()

    if processed:
        # 前回取得済みの最終日の翌日から当日まで（実行間隔が空いてもギャップを残さない）
        last_date = max(dt.strptime(d, '%Y-%m-%d') for d in processed)
        start_date = last_date + timedelta(days=1)
    else:
        # 新規店舗: 初回のみ指定日数さかのぼる
        start_date = today - timedelta(days=INITIAL_BACKFILL_DAYS)

    day_list = []
    d = start_date
    while d.date() <= today.date():
        day_list.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    remaining = [day for day in day_list if day not in processed]

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
            except AccessForbiddenError as e:
                logger.error(f'{hole_name}: アクセスが拒否されたため処理を中止します: {e}')
                return
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
        for hole_name in stores:
            process_store(con, hole_name)
    finally:
        con.close()


if __name__ == '__main__':
    main()
