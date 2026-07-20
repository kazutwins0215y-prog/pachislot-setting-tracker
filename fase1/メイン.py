from datetime import datetime as dt, timedelta
import argparse
import json
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(__file__))

from scraper import build_url, get_info, create_driver, fetch_page, AccessForbiddenError
from db import (
    get_connection, setup_db, get_processed_dates, get_no_data_giveup_dates,
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
COLLECT_UNTIL_DAYS_AGO = 1   # 何日前までを収集対象にするか（サイトは前日分を23:00〜翌10:00頃にページ一括更新するため中間状態の取り込みリスクなし。未更新日はRETRY_LOOKBACK_DAYSのギャップ再試行が翌日以降拾う）
RETRY_LOOKBACK_DAYS = 14     # 取得失敗等で空いた未処理日(ギャップ)を何日前まで再試行するか

MAX_REQUESTS_PER_RUN = 100     # 1回の実行(メイン.py起動)で送信する最大リクエスト数。到達すると正常終了し、残りは翌回の実行が続きから自動的に再開する(2026-07-20導入・同日100へ変更。BATCH_SIZE=20+BATCH_BREAK=5分の対策で120件まで通過を確認した実績の範囲内)
CIRCUIT_BREAKER_THRESHOLD = 3  # 連続でこの店舗数が「全対象日ともページにデータなし」ならブロックの疑いとして即中止する(2026-07-20導入。空ページ403の見逃し対策・層2)
NO_DATA_GIVEUP_DAYS = 3  # 「ページにデータなし」の欠損記録が何暦日以上に渡って観測されたらその対象日を打ち切る(取得しない)か(2026-07-20導入。リクエスト削減の負キャッシュ・案A)

STORES_FILE = os.path.join(os.path.dirname(__file__), 'stores.json')


def load_stores_config() -> dict:
    with open(STORES_FILE, encoding='utf-8') as f:
        return json.load(f)


def validate_catchup_only_stores(stores: list[str], catchup_only_stores: list[str]) -> None:
    """catchup_only_storesがstoresの部分集合であることを検証する(純関数)。
    設定ミス(存在しない店舗名の指定)を早期に検出するため、違反時はValueErrorで即停止する。"""
    invalid = [s for s in catchup_only_stores if s not in stores]
    if invalid:
        raise ValueError(
            f'catchup_only_storesにstoresへ存在しない店舗があります: {invalid}'
        )


def stores_for_mode(all_stores: list[str], catchup_only_stores: list[str], mode: str) -> list[str]:
    """--modeに応じた収集対象店舗リストを返す(純関数)。

    morning: catchup_only_stores(夕方更新が常態の店)を除外し、通常店だけを対象にする
             (run_daily側のmorningポーリングを早期終了させ、リクエスト数を削減するため)。
    all/catchup: 全店対象(従来動作。catchup_onlyで残った店も含めて拾う)。
    """
    if mode == 'morning':
        return [s for s in all_stores if s not in catchup_only_stores]
    return list(all_stores)


def load_stores() -> list[str]:
    config = load_stores_config()
    validate_catchup_only_stores(config['stores'], config.get('catchup_only_stores', []))
    return config['stores']


def compute_remaining_days(processed: set, today: dt, given_up: set = frozenset()) -> list[str]:
    """
    取得対象の日付リスト(YYYY-MM-DD、昇順)を返す。

    - 通常: 前回取得済み最終日の翌日〜収集対象最終日
    - ギャップ再試行: 途中の日が取得失敗すると「最終日の翌日から」だけでは
      永久にスキップされるため、直近RETRY_LOOKBACK_DAYS内は未処理日を含めて
      走査対象に含める(取得済みの日はprocessedで除外されるので再取得はしない)
    - 新規店舗: INITIAL_BACKFILL_DAYS分さかのぼる
    - given_up: 「ページにデータなし」がNO_DATA_GIVEUP_DAYS暦日以上続いた対象日
      (負キャッシュ)。永続的にデータが無いと判断し、以後はリクエストしない。
      省略時は空set扱いで従来と完全一致する(非破壊)。
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
    return [day for day in day_list if day not in processed and day not in given_up]


def classify_day_result(data_list: list, missing_machines: list) -> str:
    """
    1日分の取得結果を層2サーキットブレーカー用に分類する(純関数)。

    'no_data' = セクション自体が見つからず『ページにデータなし』のみが記録された日
                （正常な収集エラーでも起こりうるが、ブロック中はこれが全店舗×全日で連発する）
    'other'   = データを取得できた日、またはそれ以外の欠損(カラム数特定不可等)・処理エラーの日
    """
    if not data_list and missing_machines == [(None, 'ページにデータなし')]:
        return 'no_data'
    return 'other'


def all_days_no_data(day_statuses: list[str]) -> bool | None:
    """店舗の全処理日が『ページにデータなし』だったか。処理日が0件ならNone(中立)を返す。"""
    if not day_statuses:
        return None
    return all(status == 'no_data' for status in day_statuses)


def update_circuit_breaker(
    consecutive: int, store_all_no_data: bool | None, threshold: int = CIRCUIT_BREAKER_THRESHOLD,
) -> tuple[int, bool]:
    """
    層2サーキットブレーカーの連続カウントを1店舗分の結果で更新する(純関数)。
    store_all_no_data=None(対象日0件の店舗)は中立としてカウントを変更しない。
    戻り値: (更新後のconsecutive, ブレーカーが作動したか)
    """
    if store_all_no_data is None:
        return consecutive, False
    if store_all_no_data:
        consecutive += 1
        return consecutive, consecutive >= threshold
    return 0, False


def process_store(con, hole_name: str, requests_remaining: int):
    """
    戻り値: (更新後のcon, この店舗の全処理日が『ページにデータなし』だったか(中立=None), 消費したリクエスト数)
    """
    processed = get_processed_dates(con, hole_name)
    given_up = get_no_data_giveup_dates(con, hole_name, NO_DATA_GIVEUP_DAYS)
    remaining = compute_remaining_days(processed, dt.now(), given_up)

    if not remaining:
        logger.info(f'{hole_name}: 対象期間のデータはすべてDB済みです')
        return con, None, 0

    if requests_remaining <= 0:
        logger.info(f'{hole_name}: リクエスト上限に到達しているため今回はスキップします({len(remaining)}日分は次回実行時に持ち越し)')
        return con, None, 0

    target_days = remaining[:requests_remaining]
    if len(target_days) < len(remaining):
        logger.info(f'{hole_name}: リクエスト上限のため{len(target_days)}/{len(remaining)}日分のみ取得します(残りは次回実行時に持ち越し)')

    logger.info(f'{hole_name}: {len(target_days)} 日分を取得します')

    driver = create_driver()
    day_statuses = []
    try:
        for i, day in enumerate(target_days):
            t_start = time.monotonic()
            try:
                day_statuses.append(_fetch_and_write(con, driver, hole_name, day))
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
                        day_statuses.append(_fetch_and_write(con, driver, hole_name, day))
                    except Exception as e2:
                        logger.error(f'{hole_name}: {day} の再試行にも失敗: {e2}')
                        day_statuses.append('other')
                        try:
                            con.rollback()
                        except Exception as rollback_err:
                            logger.warning(f'ロールバックに失敗しました(次の書き込みで自動回復する場合があります): {rollback_err}')
                else:
                    logger.error(f'{hole_name}: {day} の処理に失敗: {e}')
                    day_statuses.append('other')
                    # 書き込み失敗でトランザクションが開きっぱなしのまま残ると、次の日の
                    # 書き込みが「connection has reached an invalid state, started with Txn」で
                    # 巻き添え失敗するため、ここで後始末してから次の日へ進む
                    try:
                        con.rollback()
                    except Exception as rollback_err:
                        logger.warning(f'ロールバックに失敗しました(次の書き込みで自動回復する場合があります): {rollback_err}')
            if i < len(target_days) - 1:
                if (i + 1) % BATCH_SIZE == 0:
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

    return con, all_days_no_data(day_statuses), len(target_days)


def _is_stream_error(e: Exception) -> bool:
    """埋め込みレプリカ接続のHranaストリームがサーバー側で失効した場合のエラー。
    数時間の長時間接続で発生し、同じconnectionでは以降ずっと同じ失敗を繰り返すため再接続が必要。"""
    return 'stream not found' in str(e)


def _fetch_and_write(con, driver, hole_name: str, day: str) -> str:
    """戻り値: この日の分類('no_data'/'other'。classify_day_result参照。層2サーキットブレーカー用)"""
    url = build_url(hole_name, day)
    html = fetch_page(driver, url)
    data_list, data_column_list, data_row_list, missing_machines = get_info(html, url, day)
    if data_list:
        write_db(con, data_list, data_column_list, data_row_list, hole_name, day)
    for machine_name, reason in missing_machines:
        write_missing(con, hole_name, day, machine_name, reason)
        logger.warning(f'{day} 欠損記録: 機種={machine_name!r} 理由={reason}')
        if machine_name:
            write_null_record(con, hole_name, day, machine_name)
    return classify_day_result(data_list, missing_machines)


EXIT_CODE_FORBIDDEN = 43  # 403検知時の専用終了コード(fase4/run_daily.pyが判別に使う)


def _log_remaining_backlog(con, stores: list[str]) -> None:
    """リクエスト上限到達で実行を打ち切った際、全店舗の残り取得対象日数をログに出す。"""
    total = 0
    for hole_name in stores:
        processed = get_processed_dates(con, hole_name)
        given_up = get_no_data_giveup_dates(con, hole_name, NO_DATA_GIVEUP_DAYS)
        total += len(compute_remaining_days(processed, dt.now(), given_up))
    logger.info(f'全店舗の残り取得対象: 約{total}日分(次回実行時に自動的に続きから再開されます)')


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='fase1: ana-slo.comからのデータ収集')
    parser.add_argument(
        '--mode', choices=['morning', 'all'], default='all',
        help=(
            'morning: stores.jsonのcatchup_only_stores(夕方更新が常態の店)をスキップして収集する。'
            'all(省略時・従来動作): 全店対象。'
            'fase4/run_daily.pyの--mode(morning/catchup)とは別物で、catchup時はallを渡す。'
        ),
    )
    return parser.parse_args(argv)


def main():
    args = parse_args()
    config = load_stores_config()
    catchup_only_stores = config.get('catchup_only_stores', [])
    validate_catchup_only_stores(config['stores'], catchup_only_stores)
    stores = stores_for_mode(config['stores'], catchup_only_stores, args.mode)
    con = get_connection()
    forbidden = False
    budget_exhausted = False
    consecutive_no_data = 0
    requests_remaining = MAX_REQUESTS_PER_RUN
    try:
        setup_db(con)
        try:
            for hole_name in stores:
                con, store_all_no_data, requests_used = process_store(con, hole_name, requests_remaining)
                requests_remaining -= requests_used

                consecutive_no_data, tripped = update_circuit_breaker(consecutive_no_data, store_all_no_data)
                if tripped:
                    raise AccessForbiddenError(
                        f'{consecutive_no_data}店舗連続で全対象日が「ページにデータなし」のためブロックの疑いがあり中止します'
                    )

                if requests_remaining <= 0:
                    budget_exhausted = True
                    logger.info(f'リクエスト上限({MAX_REQUESTS_PER_RUN})に到達したため、今回の実行はここで終了します')
                    break
        except AccessForbiddenError as e:
            logger.error(f'アクセス拒否(403)またはブロック疑いのため全店舗の処理を中止します: {e}')
            forbidden = True

        if budget_exhausted and not forbidden:
            _log_remaining_backlog(con, stores)

        # 書き込みはリモートへ委譲済みのため、最後にローカルレプリカへ反映して
        # fase2(分析・可視化)が最新データを読めるようにする
        sync_replica(con)
    finally:
        con.close()

    if forbidden:
        sys.exit(EXIT_CODE_FORBIDDEN)


if __name__ == '__main__':
    main()
