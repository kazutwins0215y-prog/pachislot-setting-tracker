"""
run_daily.py — fase4: 日次自動実行オーケストレータ

fase1収集→fase2評価(evaluate_predictions.py)→fase2分析・予測(run_store_profile.py)を
直列実行する。設計の背景は日次自動実行_設計.md、実装仕様は実装指示書.mdを参照。

標準ライブラリのみで書かれている(どのPythonでも動く)。ただしfase1/メイン.pyの
サブプロセス起動にはpy -3.12を使う(libsqlのビルド問題のためfase1はPython3.12必須)。

実行方法:
    py -3.12 run_daily.py --mode morning   # 6:30起動想定。ポーリングして昨日分を待つ
    py -3.12 run_daily.py --mode catchup   # 10:30起動想定。収集1回→評価→分析
"""
import argparse
import csv
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import datetime as dt, timedelta
from pathlib import Path

POLL_START = "06:30"        # これより前に起動されたら待機
POLL_DEADLINE = "08:15"     # ポーリング打ち切り時刻
POLL_INTERVAL_MIN = 15      # ポーリング間隔(分)
EXIT_CODE_FORBIDDEN = 43    # fase1/メイン.pyの403終了コード

BASE = Path(__file__).resolve().parent.parent
FASE1_DIR = BASE / 'fase1'
FASE2_DIR = BASE / 'fase2'
FASE3_DIR = BASE / 'fase3'
REPLICA_DB_PATH = BASE / 'ホールデータ' / 'turso_replica.db'
COLLECTION_LOG_CSV = BASE / 'ホールデータ' / 'collection_log.csv'
STORES_JSON_PATH = FASE1_DIR / 'stores.json'
LOG_DIR = Path(__file__).resolve().parent / 'logs'

PY_FASE1_CMD = ['py', '-3.12']  # fase1呼び出し用Pythonランチャ(libsqlのビルド制約)
PY_FASE2_CMD = ['python']       # fase2呼び出し用(既存運用どおり通常のpython。py -3.12ではない)

COLLECTION_LOG_HEADER = ['対象日', 'ホール名', '検知日時', 'ポーリング回数', 'mode']


class RunStats:
    """実行サマリ用に各ステップの所要時間・終了コードとcollection_log追記件数を集計する。"""

    def __init__(self) -> None:
        self.steps: list[tuple[str, int, float]] = []  # (label, exit_code, elapsed_sec)
        self.collection_log_appended = 0

    def add_step(self, label: str, exit_code: int, elapsed: float) -> None:
        self.steps.append((label, exit_code, elapsed))

    def add_collection_log_rows(self, n: int) -> None:
        self.collection_log_appended += n


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f'run_daily_{dt.now().strftime("%Y%m%d")}.log'

    logger = logging.getLogger('run_daily')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    file_handler = logging.FileHandler(log_path, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def load_stores() -> list[str]:
    import json
    with open(STORES_JSON_PATH, encoding='utf-8') as f:
        config = json.load(f)
    return config['stores']


def read_replica_state() -> set[tuple]:
    """レプリカの(ホール名, 日付)の組を読み取り専用で取得する。ファイル未作成ならcollectionの初回として空集合を返す。"""
    if not REPLICA_DB_PATH.exists():
        return set()
    con = sqlite3.connect(f'{REPLICA_DB_PATH.resolve().as_uri()}?mode=ro', uri=True)
    try:
        rows = con.execute('SELECT DISTINCT ホール名, 日付 FROM slot_data').fetchall()
    finally:
        con.close()
    return {(r[0], r[1]) for r in rows}


def append_collection_log(diff: list[tuple], detected_at: str, poll_count: int, mode: str) -> None:
    """新規に確認できた(ホール名, 日付)をcollection_log.csvへappendする。ファイルが無ければヘッダーから作成する。"""
    COLLECTION_LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not COLLECTION_LOG_CSV.exists()
    with open(COLLECTION_LOG_CSV, 'a', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(COLLECTION_LOG_HEADER)
        for hole_name, target_date in diff:
            writer.writerow([target_date, hole_name, detected_at, poll_count, mode])


def all_yesterday_present(pairs: set[tuple], stores: list[str]) -> bool:
    yesterday = (dt.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    return all((store, yesterday) in pairs for store in stores)


def run_subprocess(cmd: list[str], cwd: Path, logger: logging.Logger, label: str, stats: RunStats) -> int:
    logger.info(f'{label} を実行します: {" ".join(cmd)} (cwd={cwd})')
    t_start = time.monotonic()
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    elapsed = time.monotonic() - t_start
    for line in result.stdout.splitlines():
        logger.info(f'  [{label}] {line}')
    for line in result.stderr.splitlines():
        logger.info(f'  [{label}:stderr] {line}')
    logger.info(f'{label} 終了 (exit={result.returncode}, {elapsed:.1f}秒)')
    stats.add_step(label, result.returncode, elapsed)
    return result.returncode


def collect_once(
    pairs: set[tuple], poll_count: int, mode: str, logger: logging.Logger, stats: RunStats,
) -> tuple[set[tuple], bool, int]:
    """fase1/メイン.pyを1回実行し、新規に現れた(ホール名,日付)をcollection_log.csvへ記録する。"""
    exit_code = run_subprocess(
        PY_FASE1_CMD + ['メイン.py'], FASE1_DIR, logger, f'fase1収集(第{poll_count}回)', stats,
    )

    forbidden = exit_code == EXIT_CODE_FORBIDDEN
    if forbidden:
        logger.error('403ブロックを検知しました(exit 43)。本日の処理を中止します')
    elif exit_code != 0:
        logger.warning(f'fase1/メイン.pyが異常終了しました(exit={exit_code})。一時的失敗として扱い処理を継続します')

    new_pairs = read_replica_state()
    diff = sorted(new_pairs - pairs)
    if diff:
        detected_at = dt.now().strftime('%Y-%m-%dT%H:%M:%S')
        append_collection_log(diff, detected_at, poll_count, mode)
        stats.add_collection_log_rows(len(diff))
        logger.info(f'新規データを検知しcollection_log.csvへ追記しました({len(diff)}件): {diff}')
    else:
        logger.info('新規データはありませんでした')

    return new_pairs, forbidden, exit_code


def _today_at(hhmm: str) -> dt:
    now = dt.now()
    hour, minute = (int(x) for x in hhmm.split(':'))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def poll_and_collect(mode: str, logger: logging.Logger, stats: RunStats) -> tuple[bool, int]:
    """
    収集フェーズ(ポーリングまたは単発)を実行し、(forbidden, poll_count)を返す。
    morning: POLL_START〜POLL_DEADLINEの間、全店舗の昨日分が揃うかPOLL_DEADLINEまでポーリングする。
    catchup: ポーリングせず収集1回のみ。
    """
    stores = load_stores()
    pairs = read_replica_state()
    poll_count = 0
    forbidden = False

    if mode == 'catchup':
        poll_count += 1
        pairs, forbidden, _ = collect_once(pairs, poll_count, mode, logger, stats)
        return forbidden, poll_count

    # mode == 'morning'
    poll_start_dt = _today_at(POLL_START)
    poll_deadline_dt = _today_at(POLL_DEADLINE)

    now = dt.now()
    if now < poll_start_dt:
        wait_sec = (poll_start_dt - now).total_seconds()
        logger.info(f'POLL_START({POLL_START})前のため{wait_sec:.0f}秒待機します')
        time.sleep(wait_sec)

    now = dt.now()
    if now >= poll_deadline_dt:
        logger.warning('起動時刻がPOLL_DEADLINEを過ぎているため収集を1回だけ実行します')
        poll_count += 1
        pairs, forbidden, _ = collect_once(pairs, poll_count, mode, logger, stats)
        return forbidden, poll_count

    while True:
        poll_count += 1
        pairs, forbidden, _ = collect_once(pairs, poll_count, mode, logger, stats)
        if forbidden:
            break
        if all_yesterday_present(pairs, stores):
            logger.info('全店舗の昨日分データが揃いました。ポーリングを終了します')
            break

        now = dt.now()
        next_time = now + timedelta(minutes=POLL_INTERVAL_MIN)
        if now >= poll_deadline_dt or next_time >= poll_deadline_dt:
            logger.warning('POLL_DEADLINEに到達したためポーリングを打ち切ります')
            break
        time.sleep((next_time - now).total_seconds())

    return forbidden, poll_count


def run_evaluate_and_profile(logger: logging.Logger, stats: RunStats) -> None:
    eval_exit = run_subprocess(
        PY_FASE2_CMD + ['evaluate_predictions.py'], FASE2_DIR, logger, 'evaluate_predictions', stats,
    )
    if eval_exit != 0:
        logger.error(f'evaluate_predictions.pyが異常終了しました(exit={eval_exit})。予測追記(run_store_profile)は継続します')

    profile_exit = run_subprocess(
        PY_FASE2_CMD + ['run_store_profile.py'], FASE2_DIR, logger, 'run_store_profile', stats,
    )
    if profile_exit != 0:
        logger.error(f'run_store_profile.pyが異常終了しました(exit={profile_exit})')

    # 分析用Tursoへの差分upsert(fase3)。失敗しても異常終了させない
    # (翌日の差分実行がウォーターマーク差分で自動的に追いつくため。設計書「自己修復性」参照)
    upload_exit = run_subprocess(
        PY_FASE1_CMD + ['upload_analysis.py'], FASE3_DIR, logger, 'upload_analysis', stats,
    )
    if upload_exit != 0:
        logger.error(f'upload_analysis.pyが異常終了しました(exit={upload_exit})。翌日の差分実行で自動的に追いつきます')


def log_summary(
    mode: str, logger: logging.Logger, poll_count: int, forbidden: bool, t_start: float, stats: RunStats,
) -> None:
    elapsed = time.monotonic() - t_start
    stores = load_stores()
    pairs = read_replica_state()
    yesterday = (dt.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    logger.info('===== 実行サマリ =====')
    logger.info(
        f'mode={mode} ポーリング回数={poll_count} 403検知={forbidden} '
        f'collection_log追記件数={stats.collection_log_appended} 総所要時間={elapsed:.1f}秒'
    )
    for label, exit_code, step_elapsed in stats.steps:
        logger.info(f'  [ステップ] {label}: exit={exit_code} 所要時間={step_elapsed:.1f}秒')
    for store in stores:
        dates = sorted(d for h, d in pairs if h == store)
        last_date = dates[-1] if dates else 'なし'
        has_yesterday = '○' if (store, yesterday) in pairs else '×'
        logger.info(f'  {store}: 最終データ日={last_date} 昨日({yesterday})分={has_yesterday}')
    logger.info('======================')


def main() -> None:
    parser = argparse.ArgumentParser(description='fase4: 日次自動実行オーケストレータ')
    parser.add_argument('--mode', choices=['morning', 'catchup'], required=True)
    args = parser.parse_args()

    logger = setup_logging()
    stats = RunStats()
    t_start = time.monotonic()
    logger.info(f'===== run_daily 開始 (mode={args.mode}) =====')

    forbidden, poll_count = poll_and_collect(args.mode, logger, stats)

    if forbidden:
        logger.error('403ブロックのため評価・分析をスキップして終了します')
        log_summary(args.mode, logger, poll_count, forbidden, t_start, stats)
        sys.exit(1)

    run_evaluate_and_profile(logger, stats)
    log_summary(args.mode, logger, poll_count, forbidden, t_start, stats)
    logger.info('===== run_daily 正常終了 =====')


if __name__ == '__main__':
    main()
