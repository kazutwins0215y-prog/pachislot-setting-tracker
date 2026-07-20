from datetime import date

import scrape_machine_specs as sms


# ── _http_error_action ──────────────────────────────────────────

def test_403_aborts_immediately_even_on_first_attempt():
    assert sms._http_error_action(403, attempt=0) == 'abort'


def test_429_retries_before_retry_limit():
    assert sms._http_error_action(429, attempt=0) == 'retry'


def test_429_aborts_after_retry_limit_reached():
    assert sms._http_error_action(429, attempt=sms.MAX_RETRIES - 1) == 'abort'


def test_503_retries_before_retry_limit():
    assert sms._http_error_action(503, attempt=0) == 'retry'


def test_503_raises_as_normal_error_after_retry_limit():
    assert sms._http_error_action(503, attempt=sms.MAX_RETRIES - 1) == 'raise'


def test_other_status_raises_immediately():
    assert sms._http_error_action(500, attempt=0) == 'raise'


# ── select_targets ──────────────────────────────────────────────

def test_new_machine_within_freeze_window_is_a_target():
    today = date(2026, 7, 20)
    targets, updated = sms.select_targets(
        ['新台A'], {}, {'新台A': '2026-07-01'}, today,
    )
    assert targets == ['新台A']
    assert not updated['新台A'].get('frozen')


def test_already_frozen_machine_is_skipped():
    today = date(2026, 7, 20)
    results = {'旧台A': {'status': 'ok', 'frozen': True}}
    targets, updated = sms.select_targets(
        ['旧台A'], results, {'旧台A': '2026-05-01'}, today,
    )
    assert targets == []
    assert updated['旧台A']['frozen'] is True


def test_never_attempted_machine_is_scraped_even_if_replica_first_seen_is_old():
    """一度もscrape_one()を試みたことがない機種(last_attempt未記録)は、レプリカ上の
    真の初出日が90日超過していても今回は必ず1回スクレイプ対象にする
    (取得を一度も試みず凍結する事故の防止)。"""
    today = date(2026, 7, 20)
    targets, updated = sms.select_targets(
        ['放置台'], {}, {'放置台': '2026-01-01'}, today,
    )
    assert targets == ['放置台']
    assert not updated['放置台'].get('frozen')
    assert updated['放置台']['first_seen'] == '2026-01-01'


def test_regression_first_seen_recorded_but_never_actually_scraped_still_a_target():
    """
    回帰テスト(2026-07-20発覚): 1回上限や中断で、select_targets呼び出しにより
    first_seenだけが記録されscrape_one()は一度も呼ばれなかった機種が、次回実行時に
    「もう初回ではない」と誤判定されgave_upで凍結されてしまうバグがあった
    (first_seenキーの有無で「初回」を判定していたのが原因)。last_attempt基準に
    修正後は、first_seenがあってもlast_attemptが無ければ引き続きスクレイプ対象になること。
    """
    today = date(2026, 7, 20)
    results = {'放置台': {'first_seen': '2026-01-01'}}  # first_seenのみ記録・last_attemptは無し
    targets, updated = sms.select_targets(
        ['放置台'], results, {'放置台': '2026-01-01'}, today,
    )
    assert targets == ['放置台']
    assert not updated['放置台'].get('frozen')


def test_already_attempted_machine_past_freeze_window_gets_frozen_and_excluded():
    today = date(2026, 7, 20)
    results = {
        '放置台': {'status': 'not_found', 'first_seen': '2026-01-01', 'last_attempt': '2026-01-01'},
    }
    targets, updated = sms.select_targets(
        ['放置台'], results, {'放置台': '2026-01-01'}, today,
    )
    assert targets == []
    assert updated['放置台']['frozen'] is True
    assert updated['放置台']['status'] == 'gave_up'


def test_already_attempted_machine_past_freeze_window_with_existing_ok_data_keeps_status():
    today = date(2026, 7, 20)
    results = {
        '既存台': {
            'status': 'ok', 'settings': {'1': {}}, 'first_seen': '2026-01-01', 'last_attempt': '2026-01-01',
        },
    }
    targets, updated = sms.select_targets(
        ['既存台'], results, {'既存台': '2026-01-01'}, today,
    )
    assert targets == []
    assert updated['既存台']['frozen'] is True
    assert updated['既存台']['status'] == 'ok'


def test_already_attempted_machine_within_freeze_window_stays_a_target():
    today = date(2026, 7, 20)
    results = {
        '現役台': {'status': 'not_found', 'first_seen': '2026-07-01', 'last_attempt': '2026-07-15'},
    }
    targets, updated = sms.select_targets(
        ['現役台'], results, {'現役台': '2026-07-01'}, today,
    )
    assert targets == ['現役台']


def test_machine_missing_from_first_seen_map_is_skipped():
    today = date(2026, 7, 20)
    targets, updated = sms.select_targets(['不明台'], {}, {}, today)
    assert targets == []


def test_original_results_dict_is_not_mutated():
    today = date(2026, 7, 20)
    results = {
        '放置台': {'status': 'not_found', 'first_seen': '2026-01-01', 'last_attempt': '2026-01-01'},
    }
    sms.select_targets(['放置台'], results, {'放置台': '2026-01-01'}, today)
    assert results['放置台'].get('frozen') is None


# ── apply_budget_cap ─────────────────────────────────────────────

def test_apply_budget_cap_under_limit_returns_all():
    targets, remaining = sms.apply_budget_cap(['a', 'b'], cap=5)
    assert targets == ['a', 'b']
    assert remaining == 0


def test_apply_budget_cap_over_limit_truncates():
    targets, remaining = sms.apply_budget_cap(['a', 'b', 'c'], cap=2)
    assert targets == ['a', 'b']
    assert remaining == 1


# ── atomic_write_json ────────────────────────────────────────────

def test_atomic_write_json_retries_through_transient_permission_error(tmp_path, monkeypatch):
    """OneDrive同期エージェント等による一瞬のファイルロック(PermissionError)を
    リトライで乗り越えられること(2026-07-20の実データ確認で実際に発生した事象)。"""
    monkeypatch.setattr(sms.time, 'sleep', lambda _seconds: None)
    target = tmp_path / 'raw_specs_scraped.json'
    target.write_text('{}', encoding='utf-8')

    real_replace = sms.os.replace
    calls = {'n': 0}

    def flaky_replace(src, dst):
        calls['n'] += 1
        if calls['n'] < 3:
            raise PermissionError('simulated OneDrive lock')
        return real_replace(src, dst)

    monkeypatch.setattr(sms.os, 'replace', flaky_replace)
    sms.atomic_write_json(target, {'ok': True})
    assert calls['n'] == 3
    assert target.read_text(encoding='utf-8') == '{\n  "ok": true\n}'


def test_atomic_write_json_raises_after_exhausting_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(sms.time, 'sleep', lambda _seconds: None)
    target = tmp_path / 'raw_specs_scraped.json'
    target.write_text('{}', encoding='utf-8')

    def always_fails(src, dst):
        raise PermissionError('simulated persistent lock')

    monkeypatch.setattr(sms.os, 'replace', always_fails)
    try:
        sms.atomic_write_json(target, {'ok': True})
        assert False, 'PermissionErrorが伝播しなかった'
    except PermissionError:
        pass
