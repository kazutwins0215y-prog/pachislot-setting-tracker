import truststore
truststore.inject_into_ssl()  # certifiではなくWindows証明書ストアを使う(Norton等のHTTPSスキャンによる証明書差し替え対策)

from seleniumbase import SB
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


class SeleniumBaseDriver:
    """SeleniumBase context manager wrapper"""
    def __init__(self):
        self.driver = None
        self.context = None

    def start(self):
        self.context = SB(
            uc=True,  # undetected-chromedriver モード（Cloudflare回避）
            headless=True,
        )
        self.driver = self.context.__enter__()
        return self

    def quit(self):
        if self.context:
            self.context.__exit__(None, None, None)

    def get(self, url, timeout=None):
        return self.driver.get(url)

    def get_page_source(self):
        return self.driver.get_page_source()


def create_driver() -> SeleniumBaseDriver:
    """SeleniumBase ドライバーラッパーを作成（自動で start() を呼ぶ）"""
    driver = SeleniumBaseDriver()
    driver.start()
    return driver


def build_url(slug: str, date: str) -> str:
    encoded = quote(slug, safe='')
    return f"https://ana-slo.com/{date}-{encoded}-data/"


BLOCK_PAGE_BODY_TEXT_MIN_LEN = 200  # 正常ページ(データなし日含む)はナビ/フッター文言だけでも十分超える長さ


def is_block_page(html: str) -> bool:
    """
    ブロック(Cloudflare等による空ページ/代替ページ)の疑いがあるHTMLかどうかを判定する純関数。

    2026-07-17に発生した「空ページ403の偽成功」(fetch_pageが空ボディ403を検知できず、
    ブロック中の応答を正常な『ページにデータなし』として誤記録し続けた)の再発防止策として
    2026-07-20に導入。判定はA・B併用のOR条件:
      A: サイト骨格の目印(本文に'ana-slo'文字列を含む、かつ<title>タグが存在する)の欠如
      B: 本文テキストがほぼ空
    正常な「データなし」日もWordPressのサイト骨格(ヘッダー/フッター等)は必ず持つため、
    骨格が欠けている時点でブロックの疑いが強い。
    """
    if not html:
        return True
    soup = BeautifulSoup(html, 'html.parser')
    has_skeleton = 'ana-slo' in html.lower() and soup.find('title') is not None
    body_text = soup.get_text(strip=True)
    body_nearly_empty = len(body_text) < BLOCK_PAGE_BODY_TEXT_MIN_LEN
    return (not has_skeleton) or body_nearly_empty


def fetch_page(driver, url: str) -> str:
    """SeleniumBase でページを取得（HTMLテキストを返す）"""
    for attempt in range(MAX_RETRIES):
        try:
            driver.get(url, timeout=30)
            # ステータスチェック（if possible）
            html = driver.get_page_source()
            if '403 Forbidden' in html or 'Error 403' in html:
                raise AccessForbiddenError(f'アクセス拒否 (403): {url}')
            if is_block_page(html):
                raise AccessForbiddenError(f'ブロックの疑いがあるページを検知しました(骨格欠如/本文ほぼ空): {url}')
            return html
        except AccessForbiddenError:
            raise
        except Exception as e:
            error_msg = str(e).lower()
            if '403' in error_msg or 'forbidden' in error_msg:
                raise AccessForbiddenError(f'アクセス拒否 (403): {url}') from e
            if ('timeout' in error_msg or 'connection' in error_msg) and attempt < MAX_RETRIES - 1:
                wait = RETRY_BASE_WAIT * (2 ** attempt)
                logger.warning(f'接続エラー。{wait}秒待機後リトライ ({attempt + 1}/{MAX_RETRIES}): {e}')
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f'{url} のリクエストに {MAX_RETRIES} 回失敗しました')


def get_info(html: str, url: str, hole_date: str):
    soup = BeautifulSoup(html, 'html.parser')

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

        # 行数は実際に取り込んだ行数(平均行の手前まで)を数える。
        # 旧実装の len(datas)//n - 1 は「表末尾に平均行がある」前提の計算で、
        # 平均行を持たないバラエティ(1台設置)表では最終行を毎回取り捨てていた
        # (2026-07-14発覚。全店舗×全期間でバラエティ最終行1台が欠損)
        if count % n != 0:
            partial = count % n
            del data_list[-(partial + prepend):]
            logger.warning(f'{slot_name}: 末尾の不完全な行({partial}セル)を除外しました')
        data_row_list.append(count // n)

    logger.info('データ取得完了')
    return data_list, data_column_list, data_row_list, missing_machines
