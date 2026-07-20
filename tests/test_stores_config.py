import pytest

import メイン as main_module

validate_catchup_only_stores = main_module.validate_catchup_only_stores
stores_for_mode = main_module.stores_for_mode


def test_validate_catchup_only_stores_subset_ok():
    """catchup_only_storesがstoresの部分集合ならエラーにならない"""
    validate_catchup_only_stores(['A店', 'B店', 'C店'], ['A店', 'C店'])


def test_validate_catchup_only_stores_empty_ok():
    """catchup_only_storesが空でもOK"""
    validate_catchup_only_stores(['A店', 'B店'], [])


def test_validate_catchup_only_stores_not_subset_raises():
    """storesに存在しない店舗がcatchup_only_storesに混ざっていればValueError"""
    with pytest.raises(ValueError):
        validate_catchup_only_stores(['A店', 'B店'], ['A店', '存在しない店'])


def test_stores_for_mode_morning_excludes_catchup_only():
    """morningモードではcatchup_only店が収集対象から除外される"""
    all_stores = ['A店', 'B店', 'C店']
    catchup_only = ['B店']
    result = stores_for_mode(all_stores, catchup_only, 'morning')
    assert result == ['A店', 'C店']


def test_stores_for_mode_all_includes_everything():
    """allモードでは全店が対象になる"""
    all_stores = ['A店', 'B店', 'C店']
    catchup_only = ['B店']
    result = stores_for_mode(all_stores, catchup_only, 'all')
    assert result == all_stores


def test_stores_for_mode_morning_with_no_catchup_only_is_unchanged():
    """catchup_only_storesが空ならmorningでも全店が対象(非破壊)"""
    all_stores = ['A店', 'B店', 'C店']
    result = stores_for_mode(all_stores, [], 'morning')
    assert result == all_stores
