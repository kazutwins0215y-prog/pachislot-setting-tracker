import run_daily as rd


def test_all_yesterday_present_ignores_catchup_only_stores(monkeypatch):
    """catchup_only店の昨日分が無くても、通常店が揃っていればTrueを返す"""
    import datetime as real_datetime

    class _FixedDt(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 7, 20, 7, 0, 0)

    monkeypatch.setattr(rd, 'dt', _FixedDt)

    yesterday = '2026-07-19'
    pairs = {('通常店A', yesterday), ('通常店B', yesterday)}  # 三ノ輪uno相当は無い
    stores = ['通常店A', '通常店B', '三ノ輪uno']
    catchup_only = ['三ノ輪uno']

    assert rd.all_yesterday_present(pairs, stores, catchup_only) is True


def test_all_yesterday_present_without_catchup_only_arg_is_unchanged(monkeypatch):
    """catchup_only_stores省略時は従来通り全店が判定対象になる(非破壊)"""
    import datetime as real_datetime

    class _FixedDt(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 7, 20, 7, 0, 0)

    monkeypatch.setattr(rd, 'dt', _FixedDt)

    yesterday = '2026-07-19'
    pairs = {('通常店A', yesterday), ('通常店B', yesterday)}  # 三ノ輪uno相当は無い
    stores = ['通常店A', '通常店B', '三ノ輪uno']

    assert rd.all_yesterday_present(pairs, stores) is False


def test_main_py_mode_maps_morning_to_morning_and_catchup_to_all():
    """run_daily側のmode(morning/catchup)をメイン.py側の--mode値(morning/all)へ変換する"""
    assert rd._main_py_mode('morning') == 'morning'
    assert rd._main_py_mode('catchup') == 'all'
