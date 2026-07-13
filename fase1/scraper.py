import truststore
truststore.inject_into_ssl()  # certifiではなくWindows証明書ストアを使う(Norton等のHTTPSスキャンによる証明書差し替え対策)

import requests
import urllib3
from bs4 import BeautifulSoup
import time
import re
import logging
from urllib.parse import quote, unquote, urlparse

logger = logging.getLogger(__name__)

class AccessForbiddenError(Exception):
    pass


MAX_RETRIES = 3
RETRY_BASE_WAIT = 60  # seconds


def extract_slug(store_url: str) -> str:
    path = unquote(urlparse(store_url).path)
    last = [s for s in path.split('/') if s][-1]
    return last.removesuffix('-データ一覧')


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': 'https://ana-slo.com/',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'Connection': 'keep-alive',
    })
    return session


def build_url(slug: str, date: str) -> str:
    encoded = quote(slug, safe='')
    return f"https://ana-slo.com/{date}-{encoded}-data/"


def fetch_page(session: requests.Session, url: str) -> requests.Response:
    for attempt in range(MAX_RETRIES):
        try:
            try:
                response = session.get(url, timeout=30)
            except requests.exceptions.SSLError:
                # SSL検証失敗時のフォールバック。raise_for_statusは下の共通経路で
                # 実行する(ここで呼ぶと403→AccessForbiddenError変換を素通りするため)
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                response = session.get(url, verify=False, timeout=30)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 403:
                raise AccessForbiddenError(f'アクセス拒否 (403): {url}') from e
            if status in (429, 503, 504) and attempt < MAX_RETRIES - 1:
                wait = RETRY_BASE_WAIT * (2 ** attempt)
                logger.warning(f'HTTP {status}。{wait}秒待機後リトライ ({attempt + 1}/{MAX_RETRIES})')
                time.sleep(wait)
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


def get_info(session: requests.Session, url: str, hole_date: str):
    response = fetch_page(session, url)
    soup = BeautifulSoup(response.text, 'html.parser')

    data_list = []
    data_column_list = []
    data_row_list = []
    missing_machines = []  # [(機種名 or None, 理由), ...]

    sections = soup.find_all(id=re.compile('^section'))

    if not sections:
        logger.warning('機種名が取得できませんでした')
        missing_machines.append((None, 'ページにデータなし'))
        return data_list, data_column_list, data_row_list, missing_machines

    for i, section in enumerate(sections):
        slot_name = section.get_text(strip=True)
        if not slot_name:
            continue

        is_variety = '1台設置' in slot_name
        tab_id = 'tab01_variety' if is_variety else f'tab01_{i}'

        selector = f'#{tab_id} > div > table > tbody > tr > td'
        datas = soup.select(selector)

        n = 0
        for j in range(len(datas) - 1):
            curr = datas[j].get_text(strip=True)
            nxt = datas[j + 1].get_text(strip=True)
            if curr and '/' in curr and '/' not in nxt:
                n = j + 1
                prepend = 1 if is_variety else 2
                data_column_list.append(n + prepend)
                data_row_list.append(len(datas) // n - 1)
                break

        if n == 0:
            logger.warning(f'{slot_name}: カラム数を特定できませんでした')
            missing_machines.append((slot_name, 'カラム数特定不可'))
            continue

        count = 0
        for data in datas:
            text = data.get_text(strip=True)
            if not text:
                continue
            if text == '平均':
                break
            if count % n == 0:
                data_list.append(hole_date)
                if not is_variety:
                    data_list.append(slot_name)
            data_list.append(text)
            count += 1

    logger.info('データ取得完了')
    return data_list, data_column_list, data_row_list, missing_machines
