---
name: データ収集-skill
description: データ収集フェーズ全体のスキルまとめ（scraper.py / db.py / メイン.py）— ana-slo.comスクレイピング・SQLite保存・アクセス制御
metadata:
  type: project
---

# データ収集スキル — 総合リファレンス

> 要件定義「1. データ収集」・データ収集_構成図.md・各スキルファイルを統合したドキュメント（2026-06-28、2026-07 Stage A移行反映）

---

## システム概要

`ana-slo.com` からパチスロホールの台データをスクレイピングし、クラウドDB「Turso」（libSQL/SQLite互換）に保存する。GitHub Actionsで毎日自動実行され、PC上での手動実行も引き続き可能。

```
メイン.py（エントリーポイント）
  ├── stores.json（対象店舗一覧）
  ├── scraper.py（HTTPリクエスト・HTML解析）
  └── db.py（Turso保存・スキーマ管理）
        └── Turso DB（libsql://xxxx.turso.io、Primary Location: Tokyo）
              全店舗が同一DB内で`ホール名`列により区別される（共有DB方式）
```

実行環境: GitHub Actions（`.github/workflows/scrape.yml`、毎日21:00 JST・`workflow_dispatch`で手動実行も可）またはローカルPC（`py -3.12 メイン.py`）。
リポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （非公開）

---

## 処理フロー

| フェーズ | 処理 | 担当 |
|---|---|---|
| ① 店舗一覧読み込み | `stores.json`から対象ホール（スラッグ）一覧を取得 | `メイン.py.load_stores` |
| ② DB初期化 | テーブル作成（`IF NOT EXISTS`、Turso上に無ければ作成） | `db.setup_db` |
| ③ 日付範囲の自動算出 | 店舗ごとに取得済み最終日を調べ、翌日〜当日を対象に（新規店舗は`INITIAL_BACKFILL_DAYS`＝90日分バックフィル） | `メイン.py.process_store` + `db.get_processed_dates` |
| ④ URL構築 | `https://ana-slo.com/{日付}-{slug}-data/` | `scraper.build_url` |
| ⑤ HTTP取得 | リトライ3回・指数バックオフ・SSL対応 | `scraper.fetch_page` |
| ⑥ HTML解析 | section単位でカラム数自動検出・台データ抽出 | `scraper.get_info` |
| ⑦ データ保存 | 正常データ → `slot_data`、欠損 → `missing_data`（Turso DBへ） | `db.write_db` / `write_missing` / `write_null_record` |
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

### 接続先
Turso（libSQL/SQLite互換）の共有DB1つに全店舗のデータが入る（`ホール名`列で区別、店舗ごとのファイル分離は廃止）。

```python
def get_connection():
    return libsql.connect(
        database=os.environ['TURSO_DATABASE_URL'],
        auth_token=os.environ['TURSO_AUTH_TOKEN'],
    )
```

`メイン.py`で1回だけ接続を作り、全店舗のループで使い回す（`con`を各関数に引数で渡す設計。旧`db_path`引数は廃止）。

### テーブル構造: `slot_data`

```sql
CREATE TABLE IF NOT EXISTS slot_data (
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
CREATE TABLE IF NOT EXISTS missing_data (
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
| `get_connection()` | Turso DBへの接続を返す。`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`環境変数が必須 |
| `setup_db(con)` | 起動時に1回だけ呼ぶ。`CREATE TABLE IF NOT EXISTS`のみ（Tursoは新規DBのため旧スキーマmigrationロジックは不要・削除済み） |
| `get_processed_dates(con, hole_name)` | 指定ホールの取得済み日付セットを返す。`メイン.py`で店舗ごとの日付範囲自動算出に使う |
| `_parse_row(row, hole_name)` | `'/'` を含むセルを確率列、含まないセルを数値列として自動判別 |
| `write_db(con, ...)` | `data_column_list` / `data_row_list` を使って `data_list` をスライスし行に分割。`executemany` + `INSERT OR IGNORE` で一括インサート |
| `write_missing(con, ...)` | 欠損記録を `missing_data` テーブルに追加。`machine_name=None` はホール全体の欠損（ページにデータなし） |
| `write_null_record(con, ...)` | 機種名判明・データ取得失敗時、数値列NULLのプレースホルダーを `slot_data` に挿入。同日・同機種のNULLレコードが既にあれば挿入しない（重複防止チェック付き） |

### 移行時の注意点（Turso Upload DB）
- TursoのUpload DB機能は`journal_mode=WAL`のSQLiteファイルしか受け付けない（`Protocol error: upload works only for DBs with journal_mode=WAL`）。アップロード前に`PRAGMA journal_mode=WAL;`を実行し、`PRAGMA wal_checkpoint(TRUNCATE);`で`-wal`/`-shm`ファイルを本体に統合してからアップロードする
- ローカルPythonが3.14の場合、`libsql`パッケージのプリビルドwheelが無くソースビルド（Rust/maturin）に失敗することがある。Python 3.12を使うと解決する（`py -3.12 -m pip install -r requirements.txt`）

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
- `con.close()` は `try/finally` で確実に実施（`メイン.py`の`main()`内）
- スキーマ変更時は `_CREATE_TABLE_SQL` を更新する
- ART/ART確率は現在全行NULL（ART非搭載機種のみのデータのため正常）。分析フェーズでNULL除外が必要

---

## メイン.py

### 店舗ループとstores.json

`input()`による対話入力は廃止。`stores.json`（`{"stores": ["スラッグ1", "スラッグ2", ...]}`）から対象ホール一覧を読み込み、1つのTurso接続(`con`)を使い回して店舗ごとに処理する。

```python
def main():
    stores = load_stores()
    con = get_connection()
    try:
        setup_db(con)
        for hole_name in stores:
            process_store(con, hole_name)
    finally:
        con.close()
```

### 日付範囲の自動算出（無人実行対応）

店舗ごとに`get_processed_dates`で取得済み日付を調べ、以下のロジックで対象日付を決める。

```python
end_date = today - timedelta(days=COLLECT_UNTIL_DAYS_AGO)  # デフォルト2日前まで

if processed:
    # 前回取得済みの最終日の翌日から収集対象の最終日まで（実行間隔が空いてもギャップを残さない）
    last_date = max(dt.strptime(d, '%Y-%m-%d') for d in processed)
    start_date = last_date + timedelta(days=1)
else:
    # 新規店舗: 初回のみ指定日数さかのぼる
    start_date = today - timedelta(days=INITIAL_BACKFILL_DAYS)  # デフォルト90日
```

- `COLLECT_UNTIL_DAYS_AGO`（デフォルト2日前）: サイト側が当日・前日分をまだ更新していない可能性があるため、直近2日分は収集対象から外す。次回実行時に自動的にキャッチアップされる
- 日次実行が想定通り毎日走っていれば「前々日1日分」だけになり数分で終わる
- 実行が数日〜数週間空いても、最終取得日からのギャップを自動的に埋める（固定の「直近N日」方式だと長期間の抜けが永久に埋まらないため、この方式を採用）
- 新規店舗（`stores.json`に追加した直後で取得済みデータが0件）は`INITIAL_BACKFILL_DAYS`分だけ初回バックフィルする（この場合も収集終了日は`end_date`まで）

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

### 欠損処理

```python
data_list, data_column_list, data_row_list, missing_machines = get_info(session, url, day)
if data_list:
    write_db(con, data_list, data_column_list, data_row_list, hole_name, day)
for machine_name, reason in missing_machines:
    write_missing(con, hole_name, day, machine_name, reason)
    if machine_name:                          # 機種名が特定できた場合のみ
        write_null_record(con, hole_name, day, machine_name)
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

## Stage A移行（実装済み・2026-07）

要件定義「3. 配信・公開」（旧LINE通知）の議論を受け、fase1をクラウド化した。変更点は以下の通り。

| 項目 | 移行前 | 移行後 |
|---|---|---|
| DB | ローカルSQLite（店舗ごとに`ホールデータ/{ホール名}.db`） | クラウドDB「Turso」（libSQL/SQLite互換、Tokyo）に1つの共有DBとして統合。`db.py`をsqlite3標準ライブラリからTursoクライアント(`libsql`)へ書き換え |
| 店舗URL入力 | `メイン.py`で`input()`により対話入力 | リポジトリ内`stores.json`に店舗一覧（スラッグ）を記載し読み込む方式 |
| 日付範囲入力 | `input()`で開始日・終了日を対話入力 | 店舗ごとに`get_processed_dates`の最終日+1〜当日を自動算出（新規店舗は90日分バックフィル） |
| 実行環境 | ローカルPCで手動実行のみ | GitHub Actions（`.github/workflows/scrape.yml`）を試みたが、**ana-slo.com（Cloudflare）がGitHub Actionsのデータセンター系IPを最初のリクエストから403ブロックすることが判明**（2026-07-05実行テストで5店舗全て初回リクエストで403）。PCの住宅用IPでは約120リクエストまで通過していたのと対照的。そのため`schedule`（自動実行）は停止し、当面PC上での手動実行（`py -3.12 メイン.py`）に戻した。`workflow_dispatch`（手動トリガー）のみ残置（将来住宅用プロキシ等を検討する場合の検証用） |
| 既存データ | ローカルSQLite5ファイル（計291,979件） | `merge_stores_for_turso.py`で1ファイルに統合→journal_mode=WAL変換→Turso Upload DBで移行済み |

- Turso無料枠はストレージ5GB。店舗数・蓄積データ増加により将来逼迫する可能性があるため、運用しながら使用量を監視し、必要に応じて有料プラン（Developer: $4.99〜/月、9GBストレージ）への移行を検討する
- GitHubリポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （非公開）。`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`はリポジトリのActions Secretsに登録済み（現在は`workflow_dispatch`の手動検証用途のみで使用）
- ローカル実行にはPython 3.12を使用（3.14では`libsql`のビルドに失敗するため。詳細は「db.py」節参照）
- 詳細な移行方針は[`要件定義.md`](../要件定義.md)「3. 配信・公開」参照

---

## 関連ファイル

- 要件定義 → `要件定義.md`
- 構成図 → `fase1/データ収集_構成図.md`
- 依存パッケージ → `fase1/requirements.txt`（requests / beautifulsoup4 / libsql）
- 対象店舗一覧 → `fase1/stores.json`
- GitHub Actions定義 → `.github/workflows/scrape.yml`
- 移行用ワンショットスクリプト → `fase1/merge_stores_for_turso.py`（既存ローカルSQLiteをTurso Upload DB用に統合。恒久パイプラインには含まれない）
