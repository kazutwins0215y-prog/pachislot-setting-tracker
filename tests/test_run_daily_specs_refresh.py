import logging
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


def test_write_specs_refresh_state_failure_does_not_propagate(monkeypatch):
    """write_specs_refresh_stateが例外(例: OneDriveの一瞬のPermissionError)を投げても、
    maybe_refresh_machine_specsはそれを飲み込んで正常returnし、後続(evaluate/run_store_profile)を
    止めないこと(バグ修正の再現テスト)。"""

    def boom(today_str):
        raise PermissionError('simulated OneDrive file lock')

    monkeypatch.setattr(rd, 'read_specs_refresh_state', lambda: None)  # should_refresh_specsをTrueにする
    monkeypatch.setattr(rd, 'write_specs_refresh_state', boom)
    monkeypatch.setattr(rd, 'run_subprocess', lambda *a, **k: 0)  # サブプロセスを実際に起動しない

    logger = logging.getLogger('test_run_daily_specs_refresh')
    stats = rd.RunStats()

    rd.maybe_refresh_machine_specs(logger, stats)  # 例外が伝播せず正常returnすること
