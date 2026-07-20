from datetime import datetime as dt, timedelta

import メイン as main_module

compute_remaining_days = main_module.compute_remaining_days


def _date_range(start: dt, end: dt) -> list[str]:
    days = []
    d = start
    while d.date() <= end.date():
        days.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    return days


def test_new_store_backfills_initial_backfill_days():
    """processedが空(新規店舗)の場合はINITIAL_BACKFILL_DAYS分さかのぼる"""
    today = dt(2026, 7, 19)
    result = compute_remaining_days(set(), today)

    backfill_days = main_module.INITIAL_BACKFILL_DAYS
    expected_start = today - timedelta(days=backfill_days)
    expected_end = today - timedelta(days=main_module.COLLECT_UNTIL_DAYS_AGO)

    assert result == _date_range(expected_start, expected_end)


def test_normal_operation_only_returns_the_new_day():
    """直近RETRY_LOOKBACK_DAYS以内が全て取得済みなら、翌日分1件だけが残る"""
    today = dt(2026, 7, 19)
    last_processed = today - timedelta(days=2)
    processed = set(_date_range(today - timedelta(days=100), last_processed))

    result = compute_remaining_days(processed, today)

    expected_new_day = (today - timedelta(days=main_module.COLLECT_UNTIL_DAYS_AGO)).strftime('%Y-%m-%d')
    assert result == [expected_new_day]


def test_gap_within_retry_lookback_is_retried():
    """直近RETRY_LOOKBACK_DAYS以内に取得漏れの日があれば、最終日翌日分と合わせて再試行対象に入る"""
    today = dt(2026, 7, 19)
    last_processed = today - timedelta(days=2)
    gap_day = today - timedelta(days=5)

    all_days = set(_date_range(today - timedelta(days=100), last_processed))
    processed = all_days - {gap_day.strftime('%Y-%m-%d')}

    result = compute_remaining_days(processed, today)

    expected_new_day = (today - timedelta(days=main_module.COLLECT_UNTIL_DAYS_AGO)).strftime('%Y-%m-%d')
    expected = sorted([gap_day.strftime('%Y-%m-%d'), expected_new_day])
    assert result == expected


def test_stale_store_catches_up_fully_beyond_retry_lookback():
    """最終取得日がRETRY_LOOKBACK_DAYSより前(長期未取得)なら、最終日翌日から丸ごと取得対象になる"""
    today = dt(2026, 7, 19)
    last_processed = today - timedelta(days=40)
    processed = {last_processed.strftime('%Y-%m-%d')}

    result = compute_remaining_days(processed, today)

    expected_start = last_processed + timedelta(days=1)
    expected_end = today - timedelta(days=main_module.COLLECT_UNTIL_DAYS_AGO)
    assert result == _date_range(expected_start, expected_end)


def test_collect_until_days_ago_excludes_today():
    """収集対象は前日までで、当日分は対象に含めない"""
    today = dt(2026, 7, 19)
    processed = {(today - timedelta(days=1)).strftime('%Y-%m-%d')}

    result = compute_remaining_days(processed, today)

    assert today.strftime('%Y-%m-%d') not in result
