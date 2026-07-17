from datetime import datetime as dt, timedelta
import json
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(__file__))

from scraper import build_url, get_info, create_driver, fetch_page, AccessForbiddenError
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

LONG_BREAK_AT      = 100     # 試験的: この件数を処理し終えた直後に1回だけ長め休憩を挟む(効果があれば恒久化予定)
LONG_BREAK_SECONDS = 60 * 20  # 長め休憩の時間（秒）

INITIAL_BACKFILL_DAYS = int(os.environ.get('INITIAL_BACKFILL_DAYS', 90))  # 新規店舗追加時: 初回のみ何日分さかのぼって取得するか（環境変数で一時的に上書き可能）
COLLECT_UNTIL_DAYS_AGO = 1   # 何日前までを収集対象にするか（サイトは前日分を23:00〜翌10:00頃にページ一括更新するため中間状態の取り込みリスクなし。未更新日はRETRY_LOOKBACK_DAYSのギャップ再試行が翌日以降拾う）
RETRY_LOOKBACK_DAYS = 14     # 取得失敗等で空いた未処理日(ギャップ)を何日前まで再試行するか

STORES_FILE = os.path.join(os.path.dirname(__file__), 'stores.json')


def load_stores() -> list[str]:
    with open(STORES_FILE, encoding='utf-8') as f:
        config = json.load(f)
    return config['stores']


def load_slug_overrides() -> dict:
    """DB上のホール名 → ana-slo日次データURL用スラッグの対応(変更があった店舗のみ)。

    ana-slo.comは店舗の日次データページURLのスラッグを予告なく変えることがある
    (例: 2026-07に有楽町unoが「有楽町uno」→「uno-yurakucho」へ変更され旧URLが404化した)。
    DB上のホール名はそのままに、URL生成に使うスラッグだけ差し替えるためのマップ。
    stores.jsonに`slug_overrides`が無ければ空辞書を返す。"""
    with open(STORES_FILE, encoding='utf-8') as f:
        config = json.load(f)
    return config.get('slug_overrides', {})


_SLUG_OVERRIDES = load_slug_overrides()


def slug_for(hole_name: str) -> str:
    """ホール名(DB保存名)を ana-slo日次データURL用のスラッグへ変換する。
    override指定が無い店舗はホール名をそのままスラッグとして使う(従来どおり)。"""
    return _SLUG_OVERRIDES.get(hole_name, hole_name)


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
        return con

    logger.info(f'{hole_name}: {len(remaining)} 日分を取得します')

    driver = create_driver()
    try:
        for i, day in enumerate(remaining):
            t_start = time.monotonic()
            try:
                _fetch_and_write(con, driver, hole_name, day)
            except AccessForbiddenError:
                # 403はIP単位のブロックのため、この店舗だけでなく全店舗の処理を中止する
                # (残り店舗への無駄なリクエストで被ブロック実績を積まない)
                raise
            except Exception as e:
                if _is_stream_error(e):
                    # ストリーム失効は同じconnectionでは以降ずっと再現するため、
                    # 再接続してこの日を再試行する(次の日へ進んでも直らない)
                    logger.warning(f'{hole_name}: {day} でDBストリームが失効しました。再接続して再試行します: {e}')
                    con.close()
                    con = get_connection()
                    try:
                        _fetch_and_write(con, driver, hole_name, day)
                    except Exception as e2:
                        logger.error(f'{hole_name}: {day} の再試行にも失敗: {e2}')
                        try:
                            con.rollback()
                        except Exception as rollback_err:
                            logger.warning(f'ロールバックに失敗しました(次の書き込みで自動回復する場合があります): {rollback_err}')
                else:
                    logger.error(f'{hole_name}: {day} の処理に失敗: {e}')
                    # 書き込み失敗でトランザクションが開きっぱなしのまま残ると、次の日の
                    # 書き込みが「connection has reached an invalid state, started with Txn」で
                    # 巻き添え失敗するため、ここで後始末してから次の日へ進む
                    try:
                        con.rollback()
                    except Exception as rollback_err:
                        logger.warning(f'ロールバックに失敗しました(次の書き込みで自動回復する場合があります): {rollback_err}')
            if i < len(remaining) - 1:
                if (i + 1) == LONG_BREAK_AT:
                    logger.info(f'{i + 1}件完了。試験的に{LONG_BREAK_SECONDS}秒の長め休憩に入ります')
                    driver.quit()
                    time.sleep(LONG_BREAK_SECONDS)
                    driver = create_driver()
                    logger.info('ドライバーを再生成しました')
                elif (i + 1) % BATCH_SIZE == 0:
                    logger.info(f'{i + 1}件完了。{BATCH_BREAK}秒のバッチ休憩に入ります')
                    driver.quit()
                    time.sleep(BATCH_BREAK)
                    driver = create_driver()
                    logger.info('ドライバーを再生成しました')
                else:
                    elapsed = time.monotonic() - t_start
                    sleep_time = max(MIN_SLEEP, TARGET_CYCLE - elapsed)
                    logger.debug(f'経過 {elapsed:.1f}秒 → {sleep_time:.1f}秒待機')
                    time.sleep(sleep_time)
    finally:
        driver.quit()

    return con


def _is_stream_error(e: Exception) -> bool:
    """埋め込みレプリカ接続のHranaストリームがサーバー側で失効した場合のエラー。
    数時間の長時間接続で発生し、同じconnectionでは以降ずっと同じ失敗を繰り返すため再接続が必要。"""
    return 'stream not found' in str(e)


def _fetch_and_write(con, driver, hole_name: str, day: str):
    url = build_url(slug_for(hole_name), day)
    html = fetch_page(driver, url)
    data_list, data_column_list, data_row_list, missing_machines = get_info(html, url, day)
    if data_list:
        write_db(con, data_list, data_column_list, data_row_list, hole_name, day)
    for machine_name, reason in missing_machines:
        write_missing(con, hole_name, day, machine_name, reason)
        logger.warning(f'{day} 欠損記録: 機種={machine_name!r} 理由={reason}')
        if machine_name:
            write_null_record(con, hole_name, day, machine_name)


EXIT_CODE_FORBIDDEN = 43  # 403検知時の専用終了コード(fase4/run_daily.pyが判別に使う)


def main():
    stores = load_stores()
    con = get_connection()
    forbidden = False
    try:
        setup_db(con)
        try:
            for hole_name in stores:
                con = process_store(con, hole_name)
        except AccessForbiddenError as e:
            logger.error(f'アクセス拒否(403)のため全店舗の処理を中止します: {e}')
            forbidden = True
        # 書き込みはリモートへ委譲済みのため、最後にローカルレプリカへ反映して
        # fase2(分析・可視化)が最新データを読めるようにする
        sync_replica(con)
    finally:
        con.close()

    if forbidden:
        sys.exit(EXIT_CODE_FORBIDDEN)


if __name__ == '__main__':
    main()
