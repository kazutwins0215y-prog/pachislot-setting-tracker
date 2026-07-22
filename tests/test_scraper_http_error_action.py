import scraper


def test_403_aborts_immediately_even_on_first_attempt():
    assert scraper._http_error_action(403, attempt=0) == 'abort'


def test_429_retries_before_retry_limit():
    assert scraper._http_error_action(429, attempt=0) == 'retry'


def test_429_aborts_after_retry_limit_reached():
    assert scraper._http_error_action(429, attempt=scraper.MAX_RETRIES - 1) == 'abort'


def test_503_retries_before_retry_limit():
    assert scraper._http_error_action(503, attempt=0) == 'retry'


def test_503_raises_as_normal_error_after_retry_limit():
    assert scraper._http_error_action(503, attempt=scraper.MAX_RETRIES - 1) == 'raise'


def test_504_retries_before_retry_limit():
    assert scraper._http_error_action(504, attempt=0) == 'retry'


def test_504_raises_as_normal_error_after_retry_limit():
    assert scraper._http_error_action(504, attempt=scraper.MAX_RETRIES - 1) == 'raise'


def test_other_status_raises_immediately():
    assert scraper._http_error_action(500, attempt=0) == 'raise'
