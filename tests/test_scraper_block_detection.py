import scraper


def _page(title: str, site_marker: bool, body: str) -> str:
    # 実ページはヘッダー/フッターに店舗一覧・地域別メニュー・関連記事リンク等の
    # ボイラープレートが多く、data0件の日でも本文がほぼ空になることはない
    nav = (
        'ana-slo.com メインメニュー ホーム 店舗一覧 東京都 神奈川県 埼玉県 千葉県 茨城県 栃木県 '
        '群馬県 設定判別 データ分析 6号機 5号機 台データ 差枚データ 勝率ランキング このサイトについて '
        'お問い合わせ 姉妹サイト一覧 運営会社 広告掲載について'
    )
    footer = (
        '© ana-slo.com 当サイトの情報は目安であり実際の設定を保証するものではありません 掲載データは '
        '各ホールへの取材・独自調査に基づきますが正確性を保証するものではありませんのでご了承ください '
        '関連リンク: 店舗一覧 データ一覧 プライバシーポリシー 利用規約 サイトマップ 運営会社情報 '
        '広告・掲載のお問い合わせはこちら'
    )
    return f'''<!DOCTYPE html><html><head><title>{title}</title></head>
<body><header>{nav if site_marker else ''}</header><main>{body}</main>
<footer>{footer if site_marker else ''}</footer></body></html>'''


def test_normal_data_page_is_not_blocked():
    body = '<div id="section0">機種名A</div><table><tbody><tr><td>1/1</td><td>1</td></tr></tbody></table>'
    html = _page('テスト店 - ana-slo.com', site_marker=True, body=body)
    assert scraper.is_block_page(html) is False


def test_normal_no_data_page_with_full_skeleton_is_not_blocked():
    """データ0件の日でもWordPress骨格(title/サイト文言)自体は残るため誤検知しないこと"""
    body = '<p>本日はデータがございません。しばらくお待ちください。関連記事や他店舗のデータもご覧いただけます。</p>' * 5
    html = _page('テスト店 - ana-slo.com', site_marker=True, body=body)
    assert scraper.is_block_page(html) is False


def test_empty_html_is_blocked():
    assert scraper.is_block_page('') is True


def test_missing_title_and_site_marker_is_blocked():
    html = '<html><body><p>Access denied</p></body></html>'
    assert scraper.is_block_page(html) is True


def test_nearly_empty_body_with_skeleton_is_blocked():
    html = '<html><head><title>ana-slo.com</title></head><body></body></html>'
    assert scraper.is_block_page(html) is True


def test_interstitial_with_long_body_but_no_site_marker_is_blocked():
    """本文の文字数だけでは判定しない(A: 骨格の目印欠如 も独立して効くこと)"""
    html = _page('Just a moment...', site_marker=False, body='x' * 1000)
    assert scraper.is_block_page(html) is True
