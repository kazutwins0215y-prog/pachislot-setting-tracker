"""バラエティ最終行欠損の偵察・復元スクリプト（2026-07-14のscraper.pyバグ修正に伴う一回性ツール）。

背景: 旧scraper.pyの行数計算 len(datas)//n-1 は「表末尾に平均行がある」前提で、
平均行を持たないバラエティ(1台設置機種)表では最終行(台番号最大の1台)を毎日取り捨てていた。
エラーも欠損記録も出ないため、全店舗×全期間で1台分が静かに欠けている。
詳細: fase1/データ収集_skill.md の「バラエティ最終行の取り捨てバグ」節を参照。

使い方（Git Bash・リポジトリルートで実行。.envにTurso認証が必要）:

  偵察: 各店舗の最新収集日ページを再取得し、DBとの差分(=現在欠けている台)を報告する
    py -3.12 fase1/recover_variety_gaps.py --recon

  復元: 指定店舗×日付範囲を再取得し、欠けている行だけINSERT OR IGNOREで追記する
    py -3.12 fase1/recover_variety_gaps.py --hole bigディッパー東中野店 --start 2025-12-22 --end 2026-05-10

- 冪等: 既存行はUNIQUE(日付,ホール名,機種名,台番号)で無視されるため、中断後の再実行は安全
- 403検知時は即中止する。時間を置いて同じコマンドを再実行すれば続きから埋まる
- アクセス間隔はメイン.pyと同じ(40秒サイクル・20件ごとに5分休憩)
"""
from datetime import datetime as dt, timedelta
import argparse
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(__file__))

from scraper import build_url, get_info, create_driver, fetch_page, AccessForbiddenError
from db import get_connection, sync_replica, _parse_row
from メイン import load_stores, TARGET_CYCLE, MIN_SLEEP, BATCH_SIZE, BATCH_BREAK

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

EXIT_CODE_FORBIDDEN = 43


def build_rows(data_list, cols, row_counts, hole_name):
    """write_dbと同じ行構築（書き込みはしない）。"""
    start = 0
    rows = []
    for col_count, row_count in zip(cols, row_counts):
        for _ in range(row_count):
            rows.append(_parse_row(data_list[start:start + col_count], hole_name))
            start += col_count
    return rows


def fetch_rows(driver, hole_name: str, day: str):
    url = build_url(hole_name, day)
    html = fetch_page(driver, url)
    data_list, cols, row_counts, missing = get_info(html, url, day)
    return build_rows(data_list, cols, row_counts, hole_name), missing


def db_keys(con, hole_name: str, day: str) -> set:
    cur = con.cursor()
    cur.execute(
        'SELECT 機種名, 台番号 FROM slot_data WHERE ホール名=? AND 日付=?',
        (hole_name, day),
    )
    return {(k, d) for k, d in cur.fetchall()}


def recon(con):
    """各店舗の最新収集日ページを再取得し、DBに無い行を報告する。書き込みはしない。"""
    stores = load_stores()
    driver = create_driver()
    report = []
    try:
        for i, hole in enumerate(stores):
            t_start = time.monotonic()
            cur = con.cursor()
            cur.execute('SELECT MAX(日付) FROM slot_data WHERE ホール名=?', (hole,))
            latest = cur.fetchone()[0]
            if not latest:
                report.append((hole, None, 'DBにデータなし'))
                continue
            try:
                rows, missing = fetch_rows(driver, hole, latest)
            except AccessForbiddenError as e:
                logger.error(f'403検知のため偵察を中止します: {e}')
                break
            page_keys = {(r[2], r[3]) for r in rows}
            lacking = sorted(page_keys - db_keys(con, hole, latest), key=lambda x: (x[1] or 0))
            report.append((hole, latest, lacking))
            logger.info(f'{hole} ({latest}): ページ{len(page_keys)}台 / DBに無い={lacking}')
            if i < len(stores) - 1:
                time.sleep(max(MIN_SLEEP, TARGET_CYCLE - (time.monotonic() - t_start)))
    finally:
        driver.quit()

    logger.info('===== 偵察結果まとめ =====')
    for hole, latest, lacking in report:
        logger.info(f'  {hole} ({latest}): {lacking if lacking else "欠損なし"}')
    logger.info('欠損があった店舗は --hole/--start/--end で復元を実行してください')


def recover(con, hole: str, start: str, end: str):
    """指定範囲の全日を再取得し、DBに無い行をINSERT OR IGNOREで追記する。"""
    days = []
    d = dt.strptime(start, '%Y-%m-%d')
    end_d = dt.strptime(end, '%Y-%m-%d')
    while d <= end_d:
        days.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)

    logger.info(f'{hole}: {len(days)}日分を復元します ({start}〜{end})')
    driver = create_driver()
    total = 0
    try:
        for i, day in enumerate(days):
            t_start = time.monotonic()
            try:
                rows, missing = fetch_rows(driver, hole, day)
                if not rows:
                    logger.warning(f'{day}: データなし(missing={missing})')
                else:
                    before = len(db_keys(con, hole, day))
                    cur = con.cursor()
                    cur.executemany('''
                        INSERT OR IGNORE INTO slot_data
                            (日付, ホール名, 機種名, 台番号, 回転数, 差枚, BB, RB, ART, BB確率, RB確率, ART確率, 合成確率)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', rows)
                    con.commit()
                    added = len(db_keys(con, hole, day)) - before
                    total += added
                    logger.info(f'{day}: ページ{len(rows)}行 → 追加{added}行')
            except AccessForbiddenError as e:
                logger.error(f'403検知のため中止します(ここまでの追加={total}行)。時間を置いて同じコマンドで再開できます: {e}')
                sync_replica(con)
                sys.exit(EXIT_CODE_FORBIDDEN)
            except Exception as e:
                logger.error(f'{day}: 失敗: {e}')
                try:
                    con.rollback()
                except Exception:
                    pass

            if i < len(days) - 1:
                if (i + 1) % BATCH_SIZE == 0:
                    logger.info(f'{i + 1}件完了。{BATCH_BREAK}秒のバッチ休憩に入ります')
                    driver.quit()
                    time.sleep(BATCH_BREAK)
                    driver = create_driver()
                else:
                    time.sleep(max(MIN_SLEEP, TARGET_CYCLE - (time.monotonic() - t_start)))
    finally:
        driver.quit()
    logger.info(f'復元完了: 追加合計{total}行')


def main():
    parser = argparse.ArgumentParser(description='バラエティ最終行欠損の偵察・復元')
    parser.add_argument('--recon', action='store_true', help='全店舗の最新日を再取得し欠損を報告(書き込みなし)')
    parser.add_argument('--hole', help='復元対象の店舗名(stores.jsonのスラッグ)')
    parser.add_argument('--start', help='復元開始日 YYYY-MM-DD')
    parser.add_argument('--end', help='復元終了日 YYYY-MM-DD')
    args = parser.parse_args()

    if not args.recon and not (args.hole and args.start and args.end):
        parser.error('--recon か、--hole/--start/--end の3点セットを指定してください')

    con = get_connection()
    try:
        if args.recon:
            recon(con)
        else:
            recover(con, args.hole, args.start, args.end)
        sync_replica(con)
    finally:
        con.close()


if __name__ == '__main__':
    main()
