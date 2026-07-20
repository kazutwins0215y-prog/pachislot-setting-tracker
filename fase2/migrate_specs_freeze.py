"""
raw_specs_scraped.json の移行スクリプト(一度だけ手動実行)。

既存エントリの初出日(first_seen)はレプリカDBの収集開始日(2026年5月頃)に密集しており、
そのままscrape_machine_specs.pyの「初出から90日以内は5日おきに再取得」ルールを適用すると
既存の約150機種が一斉に「新台扱い」として再取得されてしまう。これを避けるため、
移行時点で全エントリへfrozenフラグを一括付与する。

方針:
- status=ok/needs_review(データ取得済み) → frozen=True(以後は再取得しない。first_seenは設定しない)
- status=not_found/ambiguous/error(未取得) → frozen=False, first_seen=移行日
  (取得できていない機種には移行日を起点とした新しい90日の猶予を与え、再取得対象として残す)

実行方法: python fase2/migrate_specs_freeze.py
"""
import json
import os
import time
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RAW_PATH = BASE_DIR / 'raw_specs_scraped.json'

_UNRESOLVED_STATUSES = {'not_found', 'ambiguous', 'error'}


def migrate_entry(entry: dict, today_str: str) -> dict:
    """1機種分のエントリへfrozen/first_seenを付与した新しいdictを返す(元のdictは変更しない)。"""
    entry = dict(entry)
    if entry.get('status') in _UNRESOLVED_STATUSES:
        entry['frozen'] = False
        entry['first_seen'] = today_str
    else:
        entry['frozen'] = True
    return entry


def atomic_write_json(path: Path, data: dict) -> None:
    """OneDrive同期エージェントによる一瞬のファイルロック(PermissionError)に備えてリトライする。"""
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    last_error = None
    for attempt in range(5):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(0.5 * (attempt + 1))
    raise last_error


def main() -> None:
    raw = json.loads(RAW_PATH.read_text(encoding='utf-8'))
    today_str = date.today().isoformat()

    frozen_count = 0
    active_count = 0
    for name, entry in raw.items():
        if 'frozen' in entry:
            print(f'スキップ(既に移行済み): {name}')
            continue
        migrated = migrate_entry(entry, today_str)
        raw[name] = migrated
        if migrated['frozen']:
            frozen_count += 1
        else:
            active_count += 1

    atomic_write_json(RAW_PATH, raw)
    print(f'移行完了: frozen={frozen_count}件 / 再取得対象={active_count}件')


if __name__ == '__main__':
    main()
