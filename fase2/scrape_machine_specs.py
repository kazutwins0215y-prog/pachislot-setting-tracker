"""
chonborista.com から機種別の設定差確率表(BB/RB/AT確率・機械割)と
「機種概要」テーブルの機種タイプ(仕様欄。例: ノーマル/AT機/AT機(スマスロ))を収集し、
ステージングファイル fase2/raw_specs_scraped.json に保存する。

機種タイプ(machine_type)は assign_tier.py がTier A確定(ノーマルタイプ判定)に使用する。
BIG/REG列名の確率表だけでは判定できない(AT/スマスロ機でもBIG/REG列名の場合がある。
実例: 沖ドキ!シリーズ・ドッチ・ディスクアップULTRAREMIX)ため、サイトの仕様欄を併用する。

machine_setting_specs.json への反映(tier判定込み)は別ステップで行う。

再取得ルール(2026-07-20導入): レプリカDB初出(first_seen)から90日以内の機種のみ再取得対象。
90日超過でその時点のデータのまま`frozen=True`にして以後は再取得しない(未取得のまま凍結した
場合はstatusを`gave_up`にする)。既存機種は`migrate_specs_freeze.py`で移行済みの前提。
5日おきの実行間隔は本スクリプト側では管理せず、呼び出し元(fase4/run_daily.py)が判定する。

実行方法: python fase2/scrape_machine_specs.py
"""
import difflib
import json
import logging
import os
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
import truststore
from bs4 import BeautifulSoup

truststore.inject_into_ssl()  # certifiではなくWindows証明書ストアを使う(Norton等のHTTPSスキャンによる証明書差し替え対策)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / 'raw_specs_scraped.json'

BASE_URL = 'https://chonborista.com'
MAX_RETRIES = 3
RETRY_BASE_WAIT = 30  # seconds
REQUEST_DELAY = 1.5  # seconds between requests(礼儀)

SKIP_MACHINES = {'1台設置機種'}  # 個別機種として特定不能なプレースホルダー

NAME_PREFIXES = ['A‐SLOT+ ', 'A-SLOT+ ', 'A‐SLOT ', 'A-SLOT ', 'スマスロ ', 'パチスロ']

FREEZE_AFTER_DAYS = 90  # レプリカ初出からこの日数を超えたら以後再取得しない
MAX_MACHINES_PER_RUN = 30  # 1回の実行でスクレイプする機種数の上限(保険。通常は新台のみで数件のはず)
_UNRESOLVED_STATUSES = {'not_found', 'ambiguous', 'error'}


class AccessForbiddenError(Exception):
    pass


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
        'Referer': 'https://chonborista.com/',
    })
    return session


def _http_error_action(status: int, attempt: int) -> str:
    """HTTPステータスコードから取るべき行動を返す('abort'|'retry'|'raise')。
    403はリトライ不要で即abort(サーキットブレーカー)。429はリトライ上限到達でも
    ブロックの疑いが強いためabort。503/504は一時的エラーとしてリトライ上限到達後raise。"""
    if status == 403:
        return 'abort'
    if status in (429, 503, 504):
        if attempt < MAX_RETRIES - 1:
            return 'retry'
        return 'abort' if status == 429 else 'raise'
    return 'raise'


def fetch(session: requests.Session, url: str, params: dict | None = None) -> requests.Response:
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            action = _http_error_action(status, attempt)
            if action == 'retry':
                wait = RETRY_BASE_WAIT * (2 ** attempt)
                logger.warning(f'HTTP {status}。{wait}秒待機後リトライ ({attempt + 1}/{MAX_RETRIES}): {url}')
                time.sleep(wait)
            elif action == 'abort':
                raise AccessForbiddenError(f'HTTP {status}によりブロックの疑いがあるため中断します: {url}') from e
            else:
                raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BASE_WAIT * (2 ** attempt)
                logger.warning(f'接続エラー。{wait}秒待機後リトライ ({attempt + 1}/{MAX_RETRIES}): {e}')
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f'{url} のリクエストに {MAX_RETRIES} 回失敗しました')


def normalize(name: str) -> str:
    name = name.strip()
    for prefix in NAME_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.replace(' ', '').replace('　', '')


def search_candidates(session: requests.Session, name: str) -> list[tuple[str, str]]:
    """検索結果からスロット記事の (URL, タイトル) 一覧を返す。"""
    query = search_query(name)
    response = fetch(session, BASE_URL + '/', params={'s': query})
    soup = BeautifulSoup(response.text, 'html.parser')
    results = []
    for art in soup.select('article.sidelong__article'):
        a = art.select_one('a.sidelong__link')
        if not a:
            continue
        href = a.get('href', '')
        if '/slot/' not in href:
            continue
        h2 = a.select_one('h2')
        title = h2.get_text(strip=True) if h2 else ''
        results.append((href, title))
    return results


def search_query(name: str) -> str:
    """記号を空白に置換した検索用クエリを作る(WordPress検索は記号混じりだと0件になりやすいため)。"""
    cleaned = ''.join(c if c.isalnum() else ' ' for c in name)
    return re.sub(r'\s+', ' ', cleaned).strip()


def base_name(name: str) -> str:
    """副題(『戦国乙女4 戦乱に閃く炯眼の軍師』の後半など)を除いた先頭部分を返す。"""
    for sep in (' ', '　'):
        if sep in name:
            return name.split(sep, 1)[0]
    return name


def rank_candidates(name: str, candidates: list[tuple[str, str]]) -> list[tuple[str, str, float]]:
    norm_target = normalize(name)
    norm_base = normalize(base_name(name))
    scored = []
    for href, title in candidates:
        norm_title = normalize(title)
        score = difflib.SequenceMatcher(None, norm_target, norm_title).ratio()
        if norm_title.startswith(norm_target):
            score += 0.5  # 「機種名｜〜」「機種名 スロット」等、正式ページは前方一致しやすい
        elif norm_target in norm_title:
            score += 0.2
        elif norm_base and norm_title.startswith(norm_base):
            score += 0.35  # 副題省略パターン(サイト側タイトルが機種名の先頭部分のみ)
        elif norm_base and norm_base in norm_title:
            score += 0.15
        scored.append((href, title, score))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored


def parse_overview_table(soup: BeautifulSoup) -> dict[str, str]:
    """
    『機種概要』見出し配下のテーブルを th→td の辞書として返す(仕様/機種タイプ判定に使用)。
    テンプレートによっては先頭行に『機種名』が無く『メーカー』から始まるページがあるため
    (例: ファンキージャグラー2)、旧実装の『先頭th=機種名』判定ではなく見出しテキストで
    テーブルを特定する(parse_spec_tablesの『確率』見出し検索と同じ方式)。
    """
    for h3 in soup.find_all('h3'):
        span = h3.find('span')
        heading = span.get_text(strip=True) if span else h3.get_text(strip=True)
        if '機種概要' not in heading:
            continue
        container = h3.parent
        table = container.find('table') if container else None
        if table is None:
            continue
        result: dict[str, str] = {}
        for tr in table.select('tr'):
            th_cell = tr.find('th')
            td_cell = tr.find('td')
            if th_cell and td_cell:
                result[th_cell.get_text(strip=True)] = td_cell.get_text(strip=True)
        return result
    return {}


def parse_overview_name(soup: BeautifulSoup) -> str | None:
    return parse_overview_table(soup).get('機種名')


def parse_machine_type(soup: BeautifulSoup) -> str | None:
    """
    『仕様』行(例: 'ノーマル'/'ノーマルタイプ'/'AT機'/'AT機(スマスロ)')を返す。
    is_pure_bonus(BIG/REG列のみ)だけでは機種タイプを判定できないため
    (AT機でもBIG/REG列名の確率表を持つ場合がある。例: 沖ドキ!シリーズ・ドッチ・ディスクアップULTRAREMIX)、
    assign_tier.py はこの値も併用してTier A確定を判断する。
    """
    return parse_overview_table(soup).get('仕様')


def _frac_to_prob(text: str) -> float | None:
    m = re.match(r'1/([\d.]+)', text)
    if m:
        return round(1 / float(m.group(1)), 6)
    return None


def _pct_to_ratio(text: str) -> float | None:
    m = re.match(r'([\d.]+)%', text)
    if m:
        return round(float(m.group(1)) / 100, 4)
    return None


def parse_spec_tables(soup: BeautifulSoup) -> dict:
    """『◯◯確率・機械割』見出し配下の表を設定値ごとに統合して返す。"""
    settings: dict[str, dict] = {}
    for h3 in soup.find_all('h3'):
        span = h3.find('span')
        heading = span.get_text(strip=True) if span else h3.get_text(strip=True)
        if '確率' not in heading:
            continue

        container = h3.parent
        tables = container.find_all('table', recursive=False) if container else []
        for table in tables:
            rows = table.select('tbody tr')
            if not rows:
                continue
            header_cells = [th.get_text(strip=True) for th in rows[0].find_all('th')]
            if not header_cells or header_cells[0] != '設定':
                continue
            col_names = header_cells[1:]
            n_cols = len(col_names)
            # rowspan(設定間で同一値が続く列)を考慮した列位置追跡
            pending: dict[int, list] = {}  # col_idx -> [残り行数, テキスト]
            for row in rows[1:]:
                tds = row.find_all('td')
                if not tds:
                    continue
                m = re.match(r'設定(\d)', tds[0].get_text(strip=True))
                if not m:
                    continue
                setting_no = m.group(1)

                values: list[str | None] = [None] * n_cols
                td_iter = iter(tds[1:])
                for col_idx in range(n_cols):
                    if col_idx in pending and pending[col_idx][0] > 0:
                        values[col_idx] = pending[col_idx][1]
                        pending[col_idx][0] -= 1
                        if pending[col_idx][0] == 0:
                            del pending[col_idx]
                        continue
                    td = next(td_iter, None)
                    if td is None:
                        break
                    text = td.get_text(strip=True)
                    values[col_idx] = text
                    rowspan = int(td.get('rowspan', 1) or 1)
                    if rowspan > 1:
                        pending[col_idx] = [rowspan - 1, text]

                entry = settings.setdefault(setting_no, {})
                for col_name, val in zip(col_names, values):
                    if val is None:
                        continue
                    prob = _frac_to_prob(val)
                    if prob is not None:
                        entry[col_name] = {'raw': val, 'prob': prob}
                        continue
                    pct = _pct_to_ratio(val)
                    if pct is not None:
                        entry[col_name] = {'raw': val, 'ratio': pct}
                    else:
                        entry[col_name] = {'raw': val}
        break  # 最初に見つかった『確率・機械割』見出しのみ対象
    return settings


MAX_CANDIDATE_ATTEMPTS = 3


def scrape_one(session: requests.Session, machine_name: str) -> dict:
    candidates = search_candidates(session, machine_name)
    time.sleep(REQUEST_DELAY)
    if not candidates:
        return {'status': 'not_found', 'candidates': []}

    ranked = rank_candidates(machine_name, candidates)
    if ranked[0][2] < 0.3:
        return {
            'status': 'ambiguous',
            'candidates': [{'url': u, 'title': t} for u, t, _ in ranked[:5]],
        }

    norm_target = normalize(machine_name)
    norm_base = normalize(base_name(machine_name))
    last_attempt = None
    for href, title, score in ranked[:MAX_CANDIDATE_ATTEMPTS]:
        detail = fetch(session, href)
        time.sleep(REQUEST_DELAY)
        soup = BeautifulSoup(detail.text, 'html.parser')
        site_name = parse_overview_name(soup)
        machine_type = parse_machine_type(soup)
        settings = parse_spec_tables(soup)

        norm_site = normalize(site_name or '')
        match_ratio = difflib.SequenceMatcher(None, norm_target, norm_site).ratio()
        name_ok = (
            match_ratio >= 0.5
            or norm_target in norm_site
            or (norm_base and norm_site.startswith(norm_base))
            or (site_name is None and score >= 0.8)  # 旧テンプレート等で機種名欄が無いページ
        )
        last_attempt = {
            'status': 'ok' if (name_ok and settings) else 'needs_review',
            'url': href,
            'search_title': title,
            'machine_type': machine_type,
            'site_machine_name': site_name,
            'match_score': round(match_ratio, 3),
            'settings': settings,
        }
        if name_ok and settings:
            return last_attempt

    return last_attempt


def load_first_seen_map() -> dict[str, str]:
    """レプリカDBの機種ごとの初出日(MIN(日付))を返す。specs取得対象の機種一覧はこの辞書のkeysとする。"""
    import data_source as ds

    con = ds.connect_replica()
    try:
        cur = con.cursor()
        cur.execute("SELECT 機種名, MIN(日付) FROM slot_data GROUP BY 機種名")
        rows = cur.fetchall()
    finally:
        con.close()
    return {name: first_seen for name, first_seen in rows if name and name not in SKIP_MACHINES}


def select_targets(
    machines: list[str], results: dict, first_seen_map: dict[str, str], today: date,
) -> tuple[list[str], dict]:
    """
    凍結期限(FREEZE_AFTER_DAYS)を過ぎた機種をfrozen=Trueへ更新し、
    今回スクレイプすべき機種名リストを返す。resultsは変更せず更新後の新しいdictを返す。

    このシステムで一度もスクレイプを試みたことがない機種(entryにlast_attemptが未記録)は、
    レプリカ上の真の初出日がどれだけ過去でも今回は必ず1回スクレイプ対象にする(凍結判定を課さない)。
    判定を「first_seenキーの有無」にすると、select_targetsが呼ばれた時点で(実際にはまだ
    scrape_one()を一度も呼んでいなくても)first_seenが記録されてしまい、1回上限に引っかかって
    今回処理されなかった機種や中断で処理前に終わった機種が次回実行で「もう初回ではない」と
    誤判定され、一度も取得を試みないままgave_upで凍結される再発事故が2026-07-20の実データ確認で
    判明した。scrape_one()実行後にのみ設定されるlast_attemptを判定基準にすることで、
    「実際に1回は試みたか」を正しく表す。
    """
    updated = dict(results)
    targets = []
    for name in machines:
        entry = dict(updated.get(name, {}))
        if entry.get('frozen'):
            continue
        first_seen = first_seen_map.get(name)
        if first_seen is None:
            continue  # レプリカに存在しない機種名(安全側でスキップ)
        entry.setdefault('first_seen', first_seen)
        never_attempted = 'last_attempt' not in entry
        if not never_attempted:
            elapsed_days = (today - date.fromisoformat(entry['first_seen'])).days
            if elapsed_days > FREEZE_AFTER_DAYS:
                entry['frozen'] = True
                entry['frozen_at'] = today.isoformat()
                if entry.get('status') in _UNRESOLVED_STATUSES or 'status' not in entry:
                    entry['status'] = 'gave_up'
                updated[name] = entry
                continue
        updated[name] = entry
        targets.append(name)
    return targets, updated


def apply_budget_cap(targets: list[str], cap: int) -> tuple[list[str], int]:
    """1回の実行での処理件数上限を適用し、(処理対象, 残り件数)を返す。"""
    if len(targets) <= cap:
        return targets, 0
    return targets[:cap], len(targets) - cap


ATOMIC_WRITE_RETRIES = 5
ATOMIC_WRITE_RETRY_WAIT = 0.5  # seconds


def atomic_write_json(path: Path, data: dict) -> None:
    """
    一時ファイル→os.replaceでJSONを書き込む(preprocessが日次で読むため破損防止)。
    このプロジェクトはOneDrive同期フォルダ内で動作しており、同期エージェントが
    書き込み直後のファイルを一瞬ロックしos.replaceがPermissionError(WinError 5)で
    失敗する事例を実データ確認(2026-07-20)で確認したため、短時間リトライする。
    """
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    last_error = None
    for attempt in range(ATOMIC_WRITE_RETRIES):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(ATOMIC_WRITE_RETRY_WAIT * (attempt + 1))
    raise last_error


def main():
    first_seen_map = load_first_seen_map()
    machines = sorted(first_seen_map.keys())
    logger.info(f'レプリカ全体の機種数: {len(machines)}')

    results: dict = {}
    if OUTPUT_PATH.exists():
        results = json.loads(OUTPUT_PATH.read_text(encoding='utf-8'))
        logger.info(f'既存の結果を読み込み: {len(results)}件')

    today = date.today()
    today_str = today.isoformat()
    targets, results = select_targets(machines, results, first_seen_map, today)
    targets, remaining = apply_budget_cap(targets, MAX_MACHINES_PER_RUN)
    if remaining:
        logger.warning(f'処理上限({MAX_MACHINES_PER_RUN})に到達。残り{remaining}件は次回実行へ持ち越します')
    logger.info(f'今回のスクレイプ対象: {len(targets)}件(90日凍結ルール適用後)')

    aborted = False
    session = create_session()
    for i, name in enumerate(targets, 1):
        logger.info(f'[{i}/{len(targets)}] {name}')
        try:
            entry = scrape_one(session, name)
            entry['first_seen'] = first_seen_map[name]
            entry['frozen'] = False
            entry['last_attempt'] = today_str
            results[name] = entry
        except AccessForbiddenError as e:
            logger.error(f'ブロックを検知したため残り{len(targets) - i}機種をスキップして中断します: {e}')
            aborted = True
            atomic_write_json(OUTPUT_PATH, results)
            break
        except Exception as e:
            logger.error(f'{name}: 取得失敗 {e}')
            entry = dict(results.get(name, {}))
            entry['status'] = 'error'
            entry['error'] = str(e)
            entry['first_seen'] = first_seen_map[name]
            entry['frozen'] = False
            entry['last_attempt'] = today_str
            results[name] = entry

        atomic_write_json(OUTPUT_PATH, results)

    ok = sum(1 for v in results.values() if v.get('status') == 'ok')
    review = sum(1 for v in results.values() if v.get('status') == 'needs_review')
    ambiguous = sum(1 for v in results.values() if v.get('status') == 'ambiguous')
    not_found = sum(1 for v in results.values() if v.get('status') == 'not_found')
    error = sum(1 for v in results.values() if v.get('status') == 'error')
    gave_up = sum(1 for v in results.values() if v.get('status') == 'gave_up')
    frozen = sum(1 for v in results.values() if v.get('frozen') is True)
    logger.info(
        f'完了: ok={ok} needs_review={review} ambiguous={ambiguous} not_found={not_found} '
        f'error={error} gave_up={gave_up} frozen(累計)={frozen}'
    )

    if aborted:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
