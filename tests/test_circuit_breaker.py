import メイン as main_module

classify_day_result = main_module.classify_day_result
all_days_no_data = main_module.all_days_no_data
update_circuit_breaker = main_module.update_circuit_breaker


def test_classify_day_result_no_data():
    assert classify_day_result([], [(None, 'ページにデータなし')]) == 'no_data'


def test_classify_day_result_has_data():
    assert classify_day_result(['dummy'], []) == 'other'


def test_classify_day_result_column_count_unresolved_is_other():
    assert classify_day_result([], [('機種A', 'カラム数特定不可')]) == 'other'


def test_all_days_no_data_true_when_every_day_is_no_data():
    assert all_days_no_data(['no_data', 'no_data', 'no_data']) is True


def test_all_days_no_data_false_when_any_day_has_data():
    assert all_days_no_data(['no_data', 'other']) is False


def test_all_days_no_data_neutral_when_no_days_processed():
    assert all_days_no_data([]) is None


def test_update_circuit_breaker_neutral_does_not_change_count():
    consecutive, tripped = update_circuit_breaker(2, None, threshold=3)
    assert (consecutive, tripped) == (2, False)


def test_update_circuit_breaker_increments_on_no_data():
    consecutive, tripped = update_circuit_breaker(1, True, threshold=3)
    assert (consecutive, tripped) == (2, False)


def test_update_circuit_breaker_trips_at_threshold():
    consecutive, tripped = update_circuit_breaker(2, True, threshold=3)
    assert (consecutive, tripped) == (3, True)


def test_update_circuit_breaker_resets_on_data_found():
    consecutive, tripped = update_circuit_breaker(2, False, threshold=3)
    assert (consecutive, tripped) == (0, False)
