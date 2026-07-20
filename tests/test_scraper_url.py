from urllib.parse import quote

import scraper


def test_build_url_ascii_slug():
    url = scraper.build_url('yuraku-hall', '2026-07-19')
    assert url == 'https://ana-slo.com/2026-07-19-yuraku-hall-data/'


def test_build_url_japanese_slug_is_percent_encoded():
    slug = '有楽町uno'
    url = scraper.build_url(slug, '2026-07-19')
    assert url == f'https://ana-slo.com/2026-07-19-{quote(slug, safe="")}-data/'
    assert '有楽町' not in url  # 生の日本語がそのまま入らないこと


def test_build_url_slug_with_space_is_encoded():
    url = scraper.build_url('a b', '2026-07-19')
    assert '%20' in url
    assert ' ' not in url


def test_extract_slug_removes_data_list_suffix():
    encoded_path = quote('有楽町uno-データ一覧', safe='')
    url = f'https://ana-slo.com/{encoded_path}/'
    assert scraper.extract_slug(url) == '有楽町uno'


def test_extract_slug_without_trailing_slash():
    encoded_path = quote('some-slug-データ一覧', safe='')
    url = f'https://ana-slo.com/{encoded_path}'
    assert scraper.extract_slug(url) == 'some-slug'


def test_extract_slug_without_data_list_suffix_is_noop():
    url = 'https://ana-slo.com/plain-slug/'
    assert scraper.extract_slug(url) == 'plain-slug'
