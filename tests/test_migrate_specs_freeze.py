import migrate_specs_freeze as m


def test_migrate_ok_entry_is_frozen_without_first_seen():
    entry = m.migrate_entry({'status': 'ok', 'settings': {}}, '2026-07-20')
    assert entry['frozen'] is True
    assert 'first_seen' not in entry


def test_migrate_needs_review_entry_is_frozen():
    entry = m.migrate_entry({'status': 'needs_review'}, '2026-07-20')
    assert entry['frozen'] is True


def test_migrate_not_found_entry_stays_active_with_fresh_first_seen():
    entry = m.migrate_entry({'status': 'not_found', 'candidates': []}, '2026-07-20')
    assert entry['frozen'] is False
    assert entry['first_seen'] == '2026-07-20'


def test_migrate_ambiguous_entry_stays_active():
    entry = m.migrate_entry({'status': 'ambiguous'}, '2026-07-20')
    assert entry['frozen'] is False


def test_migrate_error_entry_stays_active():
    entry = m.migrate_entry({'status': 'error', 'error': 'boom'}, '2026-07-20')
    assert entry['frozen'] is False


def test_migrate_does_not_mutate_original_dict():
    original = {'status': 'ok'}
    m.migrate_entry(original, '2026-07-20')
    assert 'frozen' not in original
