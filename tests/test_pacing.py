import メイン as main_module

classify_pacing = main_module.classify_pacing
BATCH_SIZE = main_module.BATCH_SIZE
LONG_BREAK_AT = main_module.LONG_BREAK_AT


def test_zero_is_normal():
    # 累計0件(1件も取っていない)は待機種別なし
    assert classify_pacing(0) == 'normal'


def test_below_batch_is_normal():
    assert classify_pacing(1) == 'normal'
    assert classify_pacing(BATCH_SIZE - 1) == 'normal'


def test_batch_multiple_is_batch():
    assert classify_pacing(BATCH_SIZE) == 'batch'       # 20
    assert classify_pacing(BATCH_SIZE * 2) == 'batch'   # 40
    assert classify_pacing(BATCH_SIZE * 4) == 'batch'   # 80


def test_long_multiple_takes_precedence_over_batch():
    # 100は20の倍数でもあるが、長め休憩(long)が優先される
    assert LONG_BREAK_AT % BATCH_SIZE == 0
    assert classify_pacing(LONG_BREAK_AT) == 'long'        # 100
    assert classify_pacing(LONG_BREAK_AT * 2) == 'long'    # 200


def test_between_long_multiples_batch_still_fires():
    # 120は100の倍数ではないが20の倍数なのでbatch
    assert classify_pacing(LONG_BREAK_AT + BATCH_SIZE) == 'batch'  # 120
    assert classify_pacing(LONG_BREAK_AT + BATCH_SIZE * 3) == 'batch'  # 160


def test_non_multiples_are_normal():
    for n in (99, 101, 119, 199, 21, 39):
        assert classify_pacing(n) == 'normal'


def test_daily_scale_counts_never_break():
    # 日次実行相当(全店合計でも十数件)の累計はすべてnormal(=従来通り休憩なし)
    for n in range(1, BATCH_SIZE):
        assert classify_pacing(n) == 'normal'
