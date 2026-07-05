---
name: データ収集-skill
description: データ収集フェーズ全体のスキルまとめ（scraper.py / db.py / メイン.py）— ana-slo.comスクレイピング・SQLite保存・アクセス制御
metadata:
  type: project
---

# データ収集スキル — 総合リファレンス

> 要件定義「1. データ収集」・データ収集_構成図.md・各スキルファイルを統合したドキュメント（2026-06-28）

---

## システム概要

`ana-slo.com` からパチスロホールの台データをスクレイピングし、SQLiteに保存する。

```
メイン.py（エントリーポイント）
  ├── scraper.py（HTTPリクエスト・HTML解析）
  └── db.py（SQLite保存・スキーマ管理）
        └── ホールデータ/{ホール名}.db
```

---

## 処理フロー

| フェーズ | 処理 | 担当 |
|---|---|---|
| ① 入力受付 | 日付・URL検証、slug抽出 | `メイン.py` + `scraper.extract_slug` |
| ② DB初期化 | テーブル作成・スキーマmigration | `db.setup_db` |
| ③ 重複スキップ | 取得済み日付をDBから取得して除外 | `db.get_processed_dates` |
| ④ URL構築 | `https://ana-slo.com/{日付}-{slug}-data/` | `scraper.build_url` |
| ⑤ HTTP取得 | リトライ3回・指数バックオフ・SSL対応 | `scraper.fetch_page` |
| ⑥ HTML解析 | section単位でカラム数自動検出・台データ抽出 | `scraper.get_info` |
| ⑦ データ保存 | 正常データ → `slot_data`、欠損 → `missing_data` | `db.write_db` / `write_missing` / `write_null_record` |
| ⑧ レート制御 | 通常10〜40秒待機、20件ごとに5分休憩 | `メイン.py` |

---

## scraper.py

### 対象URL
`https://ana-slo.com/{date}-{slug}-data/`

### `extract_slug(store_url)`
- URLパスの末尾セグメントから `-データ一覧` を除去してスラッグを返す
- `unquote` でパーセントエンコードを解除してから処理

### `create_session()`
- Cloudflare回避用のChrome偽装ヘッダーを設定したセッションを返す
- `Accept-Encoding` は `gzip, deflate` **のみ**。`br`（brotli）は含めない
  - `requests` はbrotli未対応のため、`br` を含めるとレスポンスが文字化けする
- `create_session()` は `scraper.py` からインポートして使う。`メイン.py` 内でヘッダーを直接設定しない

### `build_url(slug, date)`
- スラッグを `quote(safe='')` でエンコードしてURLを生成
- `.lower()` は**使わない**（hexが小文字になり実サイトのURL形式と不一致になるため）

### `fetch_page(session, url)`
- MAX_RETRIES=3、指数バックオフ（RETRY_BASE_WAIT=60秒）でリトライ
- 429/503/504 のみリトライ対象
- **403 は `AccessForbiddenError` を送出**（リトライしない）
- SSLError時は `verify=False` にフォールバック（urllib3警告を抑制）

> **【保留中の懸念点】SSLError フォールバックが 1 リクエスト限り**
> 現在の実装では `session.get(url, verify=False)` はその1回のリクエストにしか効かない。
> SSLエラーが出るサイトでは毎リクエストごとにSSLErrorが発生し、リクエスト数が実質2倍になる。
> また、フォールバック先でConnectionErrorが起きた場合はリトライなしで例外が上位に伝播する。
>
> **修正方法（未適用）:**
> ```python
> except requests.exceptions.SSLError:
>     urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
>     session.verify = False  # セッション全体に効かせる
>     response = session.get(url, timeout=30)
>     response.raise_for_status()
>     return response
> ```
>
> **現状の判断:** 対象サイト（ana-slo.com）はHTTPS正常のため、このコードパスは実質的に死にコード。
> 別サイトへの転用時に備えるなら修正する。現状は修正保留。

### `get_info(session, url, hole_date)`
- `id=re.compile('^section')` で機種セクションを列挙
- `1台設置` を含む機種名は `tab01_variety` タブとして処理（is_variety フラグ）
- **カラム数検出**: `datas[j]` に '/' があり `datas[j+1]` にない位置 `j+1` を列数 `n` とする
- **行数推定**: `len(datas) // n - 1`（ヘッダ1行分を引く）
- データ抽出は `count % n == 0` でrow境界を検出し、日付・機種名をプリペンド
- `'平均'` セルで抽出を打ち切る
- `is_variety=True` の場合、機種名はtd内に含まれるため `prepend=1`（日付のみ）、それ以外は `prepend=2`（日付＋機種名）
- 戻り値: `(data_list, data_column_list, data_row_list, missing_machines)` の4要素タプル
  - `sections` が空 → `[(None, 'ページにデータなし')]`
  - カラム数特定不可 → `[(slot_name, 'カラム数特定不可')]`

### ログ設定ルール
- `logging.basicConfig` はエントリーポイント（`メイン.py`）でのみ設定
- `scraper.py` / `db.py` は `logging.getLogger(__name__)` のみ使用

---

## db.py

### DBファイルの場所
`ホールデータ/{ホール名スラッグ}.db`（店舗ごとにファイル分離）

### テーブル構造: `slot_data`

```sql
CREATE TABLE slot_data (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    日付     TEXT NOT NULL,
    ホール名 TEXT NOT NULL,
    機種名   TEXT NOT NULL,
    台番号   INTEGER,
    回転数   INTEGER,
    差枚     INTEGER,
    BB       INTEGER,
    RB       INTEGER,
    ART      INTEGER,       -- ART非搭載機種はNULL
    BB確率   REAL,          -- '1/xxx' → 1÷xxx の実数。分母0はNULL
    RB確率   REAL,
    ART確率  REAL,          -- ART非搭載機種はNULL
    合成確率 REAL,
    UNIQUE(日付, ホール名, 機種名, 台番号)
)
```

### テーブル構造: `missing_data`

```sql
CREATE TABLE missing_data (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    日付     TEXT NOT NULL,
    ホール名 TEXT NOT NULL,
    機種名   TEXT,          -- ホール全体の欠損時はNULL
    理由     TEXT,          -- 'ページにデータなし' / 'カラム数特定不可'
    記録日時 TEXT DEFAULT (datetime('now', 'localtime'))
)
```

### 主要関数

| 関数 | 役割 |
|---|---|
| `setup_db(db_path)` | 起動時に1回だけ呼ぶ。旧スキーマ検出時は自動migration |
| `_migrate_to_new_schema(con, cur)` | `slot_data` → `slot_data_old` にリネーム後、新テーブル作成・データ移行。旧データは `slot_data_old` に残す（手動確認・復元用） |
| `get_processed_dates(db_path, hole_name)` | 取得済み日付セットを返す。`main()` でループ前に呼び、取得済み日付をスキップしてHTTPリクエストを節約 |
| `_parse_row(row, hole_name)` | `'/'` を含むセルを確率列、含まないセルを数値列として自動判別 |
| `write_db(...)` | `data_column_list` / `data_row_list` を使って `data_list` をスライスし行に分割。`executemany` + `INSERT OR IGNORE` で一括インサート |
| `write_missing(...)` | 欠損記録を `missing_data` テーブルに追加。`machine_name=None` はホール全体の欠損（ページにデータなし） |
| `write_null_record(...)` | 機種名判明・データ取得失敗時、数値列NULLのプレースホルダーを `slot_data` に挿入。同日・同機種のNULLレコードが既にあれば挿入しない（重複防止チェック付き） |

### `_parse_row` の確率列判別ロジック

```python
num_cols  = [c for c in data_cols if not (c and '/' in str(c))]
prob_cols = [c for c in data_cols if c and '/' in str(c)]

gosei = prob_cols[0]   # 合成確率（先頭）
probs = prob_cols[1:]  # [BB確率, RB確率, (ART確率)]
```

### 型変換ヘルパー

```python
def _to_int(s) -> int | None:
    # '5,667' → 5667、'+410' → 410、'-1,331' → -1331
    return int(str(s).replace(',', ''))

def _to_prob(s) -> float | None:
    # '1/298.3' → 0.003352...、'1/0.0' → None
    denom = float(s.split('/')[1])
    return 1.0 / denom if denom != 0 else None
```

### データフロー

```
scraper.get_info()
  → (data_list, data_column_list, data_row_list, missing_machines)
  → write_db() → _parse_row() × N行 → INSERT OR IGNORE INTO slot_data
  → write_missing() / write_null_record() × 欠損機種数
```

### 注意点
- `con.close()` は `try/finally` で確実に実施
- スキーマ変更時は `_CURRENT_SCHEMA_COLS` セットと `_CREATE_TABLE_SQL` を両方更新する
- ART/ART確率は現在全行NULL（ART非搭載機種のみのデータのため正常）。分析フェーズでNULL除外が必要

---

## メイン.py

### アクセス間隔（動的sleep）

固定ランダムではなく**経過時間ベース**でsleepを調整する。

```python
TARGET_CYCLE = 40  # 目標サイクル時間（秒）
MIN_SLEEP    = 10  # 最低待機時間（秒）

t_start = time.monotonic()
# ... fetch & write ...
elapsed = time.monotonic() - t_start
sleep_time = max(MIN_SLEEP, TARGET_CYCLE - elapsed)
time.sleep(sleep_time)
```

- ページが重く時間がかかった → sleep を短縮
- ページが軽く速く終わった → sleep を長く取る
- elapsed が TARGET_CYCLE を超えても MIN_SLEEP は保証

### 403アクセス拒否時の中断

```python
except AccessForbiddenError as e:
    logger.error(f'アクセスが拒否されたため処理を中止します: {e}')
    return
```

残りの日付も処理しない（一時的なIP制限が原因のため無駄なリクエストを避ける）。

### DB済み日付のスキップ

- `get_processed_dates()` で取得済み日付セットを取得
- ループ前に `remaining` リストを作成してスキップ

### 欠損処理

```python
data_list, data_column_list, data_row_list, missing_machines = get_info(session, url, day)
if data_list:
    write_db(...)
for machine_name, reason in missing_machines:
    write_missing(db_path, hole_name, day, machine_name, reason)
    if machine_name:                          # 機種名が特定できた場合のみ
        write_null_record(db_path, hole_name, day, machine_name)
```

- `machine_name=None`（ページにデータなし）は `missing_data` のみ記録
- `machine_name` あり（カラム数特定不可）は `missing_data` 記録 + `slot_data` にNULLレコード挿入

### バッチ休憩（Cloudflare対策）

```python
BATCH_SIZE  = 20      # この件数ごとに長めの休憩
BATCH_BREAK = 60 * 5  # バッチ休憩時間（秒）
```

- 連続アクセス約30回でCloudflareに403を食らった実績あり
- BATCH_SIZE=20 + BATCH_BREAK=5分で120件まで通過を確認
- 120件で再び403が発生 → BATCH_SIZE を下げるか BATCH_BREAK を延ばすことで調整

### セッション再生成（バッチ休憩後）

バッチ休憩後は必ずセッションを閉じて新規生成する。

```python
session = create_session()
try:
    for i, day in enumerate(remaining):
        ...
        if (i + 1) % BATCH_SIZE == 0:
            session.close()
            time.sleep(BATCH_BREAK)
            session = create_session()
finally:
    session.close()
```

- `keep-alive` 接続のまま5分放置するとサーバー側が切断 → 再開1発目が必ず `ConnectionResetError(10054)` になるため、休憩前に `session.close()` してから休憩後に `create_session()` で回避する
- `with create_session() as session:` ではループ中に再生成できないため `try/finally` に変更

---

## 検討中の変更（未実装・Stage A移行計画）

2026-07に要件定義「3. 配信・公開」（旧LINE通知）の議論の中で、fase1側の以下の変更を決定（実装は未着手）。

| 項目 | 現状 | 変更後 |
|---|---|---|
| DB | ローカルSQLite（`ホールデータ/{ホール名}.db`） | クラウドDB「Turso」（libSQL/SQLite互換）。`db.py`をsqlite3標準ライブラリからTursoクライアントへ書き換え |
| 店舗URL入力 | `メイン.py`で`input()`により対話入力 | リポジトリ内`stores.json`に店舗一覧を記載し読み込む方式（店舗数・データ量は今後も増加予定のため、Git管理で追跡） |
| 日付範囲入力 | `input()`で開始日・終了日を対話入力 | `get_processed_dates`を使い「前回取得済み日の翌日〜当日」を自動算出 |
| 実行環境 | ローカルPCで手動実行 | GitHub Actionsの定期実行（`schedule: cron`）を追加。Selenium等のブラウザ操作が不要な素の`requests`実装のため無料枠内で運用可能 |
| PC上の手動実行 | — | 廃止しない。同じコード・同じTurso DBに対して引き続き手動実行可能（クラウド化は「DBの置き場所」と「定期実行の主体」を追加するだけ） |

- Turso無料枠はストレージ5GB。店舗数・蓄積データ増加により将来逼迫する可能性があるため、運用開始後は使用量を監視し、必要に応じて有料プラン（Developer: $4.99〜/月、9GBストレージ）への移行を検討する
- 詳細な移行方針は[`要件定義.md`](../要件定義.md)「3. 配信・公開」参照

---

## 関連ファイル

- 要件定義 → `要件定義.md`
- 構成図 → `fase1/データ収集_構成図.md`
