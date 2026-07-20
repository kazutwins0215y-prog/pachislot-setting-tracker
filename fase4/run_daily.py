"""
run_daily.py — fase4: 日次自動実行オーケストレータ

fase1収集→fase2評価(evaluate_predictions.py)→fase2分析・予測(run_store_profile.py)を
直列実行する。設計の背景は日次自動実行_設計.md、実装仕様は実装指示書.mdを参照。

標準ライブラリのみで書かれている(どのPythonでも動く)。ただしfase1/メイン.pyの
サブプロセス起動にはpy -3.12を使う(libsqlのビルド問題のためfase1はPython3.12必須)。

実行方法:
    py -3.12 run_daily.py --mode morning   # 6:30起動想定。ポーリングして昨日分を待つ
    py -3.12 run_daily.py --mode catchup   # 10:30起動想定。収集1回→評価→分析

注意: この--mode(morning/catchup)は、fase1/メイン.py側に新設した--mode(morning/all)とは
別物(名前が同じで紛らわしい)。run_daily側は「ポーリングするか単発か」、メイン.py側は
「catchup_only_stores(stores.json、夕方更新が常態の店)をスキップするか」を表す。
run_daily→メイン.pyの呼び出しは_main_py_mode()で変換する(morning→morning, catchup→all)。
"""
import argparse
import csv
import ctypes
import json
import logging
import msvcrt
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime as dt, timedelta
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
LOCK_FILE_PATH = Path(__file__).resolve().parent / 'run_daily.lock'

SPECS_REFRESH_INTERVAL_DAYS = 5  # 機種スペック再取得の実行間隔(days)。5日未満なら今回はスキップ
SPECS_REFRESH_STATE_PATH = Path(__file__).resolve().parent / 'specs_refresh_state.json'

PY_FASE1_CMD = ['py', '-3.12']  # fase1呼び出し用Pythonランチャ(libsqlのビルド制約)
PY_FASE2_CMD = ['py', '-3.12']  # fase2呼び出し用(2026-07-19にpythonから統一。タスクスケジューラ環境のPATH次第で別バージョンを掴む環境差異を排除)

COLLECTION_LOG_HEADER = ['対象日', 'ホール名', '検知日時', 'ポーリング回数', 'mode']

# SetThreadExecutionStateのフラグ(Win32 API)。バッテリー駆動時にモダンスタンバイが
# 「Austerity Battery Drain Budget Exceeded」等の理由で強制的にスリープ/休止へ移行し、
# 実行中のrun_daily.pyが強制終了(exit 0xC000013A)される事例が確認されたための対策。
# 詳細はfase4/日次自動実行_skill.md参照。
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def _prevent_sleep(logger: logging.Logger) -> None:
    """OSに「スリープしないでほしい」という実行状態をリクエストする。
    Windows以外・API呼び出し失敗時は無視して処理を継続する(ベストエフォート)。"""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        )
    except (AttributeError, OSError) as e:
        logger.warning(f'スリープ防止リクエストに失敗しました(処理は継続します): {e}')


def _allow_sleep(logger: logging.Logger) -> None:
    """スリープ防止リクエストを解除し、通常の電源管理に戻す。"""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except (AttributeError, OSError) as e:
        logger.warning(f'スリープ防止リクエストの解除に失敗しました: {e}')


def acquire_lock():
    """run_daily.lockを排他ロックする。他プロセスが実行中で取得できなければNoneを返す。
    プロセスが異常終了してもOSがロックを自動解放するため残留事故は起きない。"""
    lock_file = open(LOCK_FILE_PATH, 'a+b')
    try:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def release_lock(lock_file) -> None:
    try:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    lock_file.close()


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


def load_stores_config() -> dict:
    with open(STORES_JSON_PATH, encoding='utf-8') as f:
        return json.load(f)


def load_stores() -> list[str]:
    """全店舗リストを返す(監視・ログ表示用。catchup_only店も含む全店)。"""
    return load_stores_config()['stores']


def load_catchup_only_stores() -> list[str]:
    """夕方更新が常態でmorningポーリングの早期終了を妨げる店舗のリストを返す。
    収集再開後にcollection_log.csvの実測でメンバーを見直す前提(stores.jsonの1行編集で変更可)。"""
    return load_stores_config().get('catchup_only_stores', [])


def _main_py_mode(rd_mode: str) -> str:
    """run_daily側のmode('morning'/'catchup')をfase1/メイン.py側の--mode値('morning'/'all')へ変換する。
    名前が同じ'morning'でも指すもの(fase1側は店舗フィルタ、run_daily側はポーリング挙動)が
    異なる別物である点に注意。run_daily catchupはcatchup_only店も含めて確実に拾うためallを渡す。"""
    return 'morning' if rd_mode == 'morning' else 'all'


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


def all_yesterday_present(
    pairs: set[tuple], stores: list[str], catchup_only_stores: list[str] = (),
) -> bool:
    """通常店の昨日分がすべて揃ったかを判定する。catchup_only_stores(夕方更新が常態の店)は
    判定対象から除外し、通常店だけが揃った時点でポーリングを早期終了できるようにする
    (削減幅最大の箇所)。デフォルトは空タプルで従来と完全一致(非破壊)。"""
    yesterday = (dt.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    target_stores = [s for s in stores if s not in catchup_only_stores]
    return all((store, yesterday) in pairs for store in target_stores)


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
        PY_FASE1_CMD + ['メイン.py', '--mode', _main_py_mode(mode)],
        FASE1_DIR, logger, f'fase1収集(第{poll_count}回)', stats,
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
    catchup_only_stores = load_catchup_only_stores()
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
        if all_yesterday_present(pairs, stores, catchup_only_stores):
            logger.info('通常店の昨日分データが揃いました(catchup_only店は対象外)。ポーリングを終了します')
            break

        now = dt.now()
        next_time = now + timedelta(minutes=POLL_INTERVAL_MIN)
        if now >= poll_deadline_dt or next_time >= poll_deadline_dt:
            logger.warning('POLL_DEADLINEに到達したためポーリングを打ち切ります')
            break
        time.sleep((next_time - now).total_seconds())

    return forbidden, poll_count


def should_refresh_specs(last_run: str | None, today: date, interval_days: int) -> bool:
    """前回実行日からinterval_days以上経過していれば機種スペック再取得を実行すべきかを返す。
    未実行(last_run=None)なら常にTrue。"""
    if last_run is None:
        return True
    elapsed_days = (today - date.fromisoformat(last_run)).days
    return elapsed_days >= interval_days


def read_specs_refresh_state() -> str | None:
    if not SPECS_REFRESH_STATE_PATH.exists():
        return None
    state = json.loads(SPECS_REFRESH_STATE_PATH.read_text(encoding='utf-8'))
    return state.get('last_run')


def write_specs_refresh_state(today_str: str) -> None:
    """OneDrive同期エージェントによる一瞬のファイルロック(PermissionError)に備えてリトライする。"""
    tmp_path = SPECS_REFRESH_STATE_PATH.with_suffix('.json.tmp')
    tmp_path.write_text(json.dumps({'last_run': today_str}, ensure_ascii=False), encoding='utf-8')
    last_error = None
    for attempt in range(5):
        try:
            os.replace(tmp_path, SPECS_REFRESH_STATE_PATH)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(0.5 * (attempt + 1))
    raise last_error


def maybe_refresh_machine_specs(logger: logging.Logger, stats: RunStats) -> None:
    """
    機種スペック(理論値)の再取得を5日おきに実行する(fase2/scrape_machine_specs.py→assign_tier.py)。
    最終実行日はSPECS_REFRESH_STATE_PATHへ永続化してrun_daily側で間隔を判定する
    (機種ごとの90日凍結ルール自体はscrape_machine_specs.py側が管理)。
    失敗しても後続(evaluate/run_store_profile)を止めない(理論値未取得でもpreprocess.judge_tierの
    実測値フォールバックで分析は継続できるため)。
    """
    today_str = date.today().isoformat()
    last_run = read_specs_refresh_state()
    if not should_refresh_specs(last_run, date.today(), SPECS_REFRESH_INTERVAL_DAYS):
        logger.info(f'機種スペック再取得は間隔未経過のためスキップします(前回実行日={last_run})')
        return

    scrape_exit = run_subprocess(
        PY_FASE2_CMD + ['scrape_machine_specs.py'], FASE2_DIR, logger, 'scrape_machine_specs', stats,
    )
    if scrape_exit != 0:
        logger.error(f'scrape_machine_specs.pyが異常終了しました(exit={scrape_exit})。理論値未取得のまま続行します')

    tier_exit = run_subprocess(
        PY_FASE2_CMD + ['assign_tier.py'], FASE2_DIR, logger, 'assign_tier', stats,
    )
    if tier_exit != 0:
        logger.error(f'assign_tier.pyが異常終了しました(exit={tier_exit})')

    try:
        write_specs_refresh_state(today_str)
    except Exception as e:
        logger.error(f'specs_refresh_state.jsonの更新に失敗しました(次回実行時に再試行します): {e}')


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

    lock_file = acquire_lock()
    if lock_file is None:
        logger.info(f'別のrun_dailyが実行中のためスキップします (mode={args.mode})')
        return

    try:
        stats = RunStats()
        t_start = time.monotonic()
        logger.info(f'===== run_daily 開始 (mode={args.mode}) =====')

        _prevent_sleep(logger)
        try:
            forbidden, poll_count = poll_and_collect(args.mode, logger, stats)

            if forbidden:
                logger.error('403ブロックのため評価・分析をスキップして終了します')
                log_summary(args.mode, logger, poll_count, forbidden, t_start, stats)
                sys.exit(1)

            maybe_refresh_machine_specs(logger, stats)
            run_evaluate_and_profile(logger, stats)
            log_summary(args.mode, logger, poll_count, forbidden, t_start, stats)
            logger.info('===== run_daily 正常終了 =====')
        finally:
            _allow_sleep(logger)
    finally:
        release_lock(lock_file)


if __name__ == '__main__':
    main()
