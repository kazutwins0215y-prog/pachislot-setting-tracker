import pytest

import メイン as main_module

parse_args = main_module.parse_args
resolve_max_requests = main_module.resolve_max_requests
MAX_REQUESTS_PER_RUN = main_module.MAX_REQUESTS_PER_RUN
UNLIMITED_REQUESTS = main_module.UNLIMITED_REQUESTS


def test_max_requests_defaults_to_none_when_unspecified():
    args = parse_args([])
    assert args.max_requests is None


def test_max_requests_parses_explicit_value():
    args = parse_args(['--max-requests', '3'])
    assert args.max_requests == 3


def test_max_requests_parses_zero():
    args = parse_args(['--max-requests', '0'])
    assert args.max_requests == 0


def test_resolve_max_requests_unspecified_uses_default_100():
    assert resolve_max_requests(None) == MAX_REQUESTS_PER_RUN


def test_resolve_max_requests_zero_means_unlimited():
    assert resolve_max_requests(0) == UNLIMITED_REQUESTS


def test_resolve_max_requests_unlimited_is_effectively_uncapped():
    """0=無制限は「予算切れで店舗をスキップする」判定(requests_remaining<=0)に
    絶対に該当しないくらい大きい値であること(スライス用途にも使うためintである必要がある)。"""
    assert isinstance(UNLIMITED_REQUESTS, int)
    assert UNLIMITED_REQUESTS > 10**6


def test_resolve_max_requests_positive_value_passthrough():
    assert resolve_max_requests(3) == 3


def test_resolve_max_requests_negative_raises_value_error():
    with pytest.raises(ValueError):
        resolve_max_requests(-1)
