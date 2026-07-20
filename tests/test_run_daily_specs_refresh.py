from datetime import date

import run_daily as rd


def test_should_refresh_when_never_run_before():
    assert rd.should_refresh_specs(None, date(2026, 7, 20), interval_days=5) is True


def test_should_not_refresh_before_interval_elapsed():
    assert rd.should_refresh_specs('2026-07-18', date(2026, 7, 20), interval_days=5) is False


def test_should_refresh_exactly_at_interval_boundary():
    assert rd.should_refresh_specs('2026-07-15', date(2026, 7, 20), interval_days=5) is True


def test_should_refresh_when_interval_well_exceeded():
    assert rd.should_refresh_specs('2026-07-01', date(2026, 7, 20), interval_days=5) is True
