import pytest

import db


def test_to_int_valid_with_comma():
    assert db._to_int('12,345') == 12345


def test_to_int_none_input():
    assert db._to_int(None) is None


def test_to_int_invalid_string():
    assert db._to_int('---') is None


def test_to_prob_valid():
    assert db._to_prob('1/298.3') == pytest.approx(1 / 298.3)


def test_to_prob_zero_denominator_is_none():
    assert db._to_prob('1/0') is None


def test_to_prob_malformed_no_slash():
    assert db._to_prob('298.3') is None


def test_to_prob_none_input():
    assert db._to_prob(None) is None


def test_parse_row_full_columns():
    # 数値6列(台番号,回転数,差枚,BB,RB,ART) + 確率4列(合成,BB,RB,ART)
    row = (
        '2026-07-18', 'からくりサーカス2',
        '45', '5,820', '-120', '3', '2', '1',
        '1/298.3', '1/512.0', '1/2048.0', '1/199.9',
    )
    parsed = db._parse_row(row, 'テスト店')

    assert parsed[0] == '2026-07-18'
    assert parsed[1] == 'テスト店'
    assert parsed[2] == 'からくりサーカス2'
    assert parsed[3:9] == (45, 5820, -120, 3, 2, 1)
    assert parsed[9] == pytest.approx(1 / 512.0)   # BB確率
    assert parsed[10] == pytest.approx(1 / 2048.0)  # RB確率
    assert parsed[11] == pytest.approx(1 / 199.9)   # ART確率
    assert parsed[12] == pytest.approx(1 / 298.3)   # 合成確率


def test_parse_row_missing_bb_rb_art_probabilities():
    """合成確率しか無い(BB/RB/ART確率が欠落した)台のケース"""
    row = ('2026-07-18', 'テスト機種', '10', '3000', '50', '1', '0', '0', '1/300.0')
    parsed = db._parse_row(row, 'テスト店')

    assert parsed[3:9] == (10, 3000, 50, 1, 0, 0)
    assert parsed[9] is None   # BB確率
    assert parsed[10] is None  # RB確率
    assert parsed[11] is None  # ART確率
    assert parsed[12] == pytest.approx(1 / 300.0)  # 合成確率


def test_parse_row_missing_machine_name_and_data():
    """機種名すら取得できていない異常系(データ列が無い)"""
    row = ('2026-07-18',)
    parsed = db._parse_row(row, 'テスト店')

    assert parsed[0] == '2026-07-18'
    assert parsed[1] == 'テスト店'
    assert parsed[2] is None
    assert parsed[3:9] == (None, None, None, None, None, None)
    assert parsed[9:] == (None, None, None, None)
