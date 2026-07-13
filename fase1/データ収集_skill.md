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

`ana-slo.com` からパチスロホールの台データをスクレイピングし、クラウドDB「Turso」（libSQL/SQLite互換）に保存する。ana-slo.com（Cloudflare）がGitHub Actionsのデータセンター系IPを即403ブロックするため、GitHub Actionsでの自動実行（`schedule`）は停止中。代わりに2026-07に[`fase4/`](../fase4/)（タスクスケジューラ+`run_daily.py`）を実装し、自宅PC（住宅用IP）上で`py -3.12 メイン.py`を毎日自動実行する運用に移行済み（詳細は[`fase4/日次自動実行_skill.md`](../fase4/日次自動実行_skill.md)参照）。単体での手動実行も引き続き可能。

```
メイン.py（エントリーポイント）
  ├── stores.json（対象店舗一覧）
  ├── scraper.py（HTTPリクエスト・HTML解析）
  └── db.py（Turso保存・スキーマ管理）
        └── Turso DB（libsql://xxxx.turso.io、Primary Location: Tokyo）
        │     全店舗が同一DB内で`ホール名`列により区別される（共有DB方式）
        │     書き込みは埋め込みレプリカ経由でリモートプライマリへ委譲される
        └── ホールデータ/turso_replica.db（Turso埋め込みレプリカ、2026-07追加）
              SQLite互換のローカルファイル。実行終了時にsync()で最新化され、
              fase2（分析・可視化）はTursoへ直接接続せずこのファイルを読む
```

実行環境: ローカルPC（`py -3.12 メイン.py`。Python 3.14では`libsql`がビルド不可のため3.12必須）。GitHub Actions（`.github/workflows/scrape.yml`）は`workflow_dispatch`（手動トリガー）のみ残置。
収集を伴わずレプリカだけ最新化したい場合は `py -3.12 fase1/sync_replica.py`。
リポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （非公開）

---

## 処理フロー

| フェーズ | 処理 | 担当 |
|---|---|---|
| ① 店舗一覧読み込み | `stores.json`から対象ホール（スラッグ）一覧を取得 | `メイン.py.load_stores` |
| ② DB初期化 | テーブル作成（`IF NOT EXISTS`、Turso上に無ければ作成） | `db.setup_db` |
| ③ 日付範囲の自動算出 | 店舗ごとに取得済み最終日を調べ、翌日〜`COLLECT_UNTIL_DAYS_AGO`(1日前=前日)を対象に。加えて直近`RETRY_LOOKBACK_DAYS`(14日)内の未処理日（取得失敗によるギャップ）も再試行対象に含める（新規店舗は`INITIAL_BACKFILL_DAYS`＝90日分バックフィル） | `メイン.py.compute_remaining_days` + `db.get_processed_dates` |
| ④ URL構築 | `https://ana-slo.com/{日付}-{slug}-data/` | `scraper.build_url` |
| ⑤ HTTP取得 | リトライ3回・指数バックオフ・SSL対応 | `scraper.fetch_page` |
| ⑥ HTML解析 | section単位でカラム数自動検出・台データ抽出 | `scraper.get_info` |
| ⑦ データ保存 | 正常データ → `slot_data`、欠損 → `missing_data`（Turso DBへ） | `db.write_db` / `write_missing` / `write_null_record` |
| ⑧ レート制御 | 通常10〜40秒待機、20件ごとに5分休憩 | `メイン.py` |
| ⑨ レプリカ同期 | 実行終了時に`sync()`でローカルレプリカ（`ホールデータ/turso_replica.db`）を最新化。fase2はこれを読む | `db.sync_replica` |

- **403（`AccessForbiddenError`）発生時は全店舗の処理を即中止する**。CloudflareのブロックはIP単位のため、残り店舗への試行は無駄なリクエストで被ブロック実績を積むだけになる（2026-07変更。以前は該当店舗のみスキップ）
- **Tursoストリーム失効（`stream not found`）時は再接続して同じ日を再試行する**（2026-07-14追加。詳細は「メイン.py」節参照）

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

### SSL証明書検証とtruststore（2026-07-14追加）

`scraper.py`冒頭で`truststore.inject_into_ssl()`を実行し、Pythonの証明書検証を
certifi（requests同梱の証明書リスト）ではなく**OSの証明書ストア**（Windowsなら証明書マネージャ）で行う。

- **経緯**: ユーザーPCのNorton Antivirus（Web/Mail Shield）がHTTPSスキャンのため全サイトの証明書を
  Norton発行のものに差し替えており、certifiベースの検証が常に`SSLCertVerificationError`で失敗
  →毎リクエストが`verify=False`（検証無効）フォールバックで動いていた。NortonのルートCAは
  Windows証明書ストアには登録済みのため、truststoreの導入で正常に検証が通るようになった
- truststoreはPyPA公式（pipが内部使用）。Windowsストアが無い環境（GitHub Actions等のLinux）でも
  OSのストアを参照するだけで挙動は変わらない。Python 3.10+が必要（本プロジェクトは3.12固定なので問題なし）

### `fetch_page(session, url)`
- MAX_RETRIES=3、指数バックオフ（RETRY_BASE_WAIT=60秒）でリトライ
- 429/503/504 のみリトライ対象
- **403 は `AccessForbiddenError` を送出**（リトライしない）
- SSLError時は `verify=False` にフォールバック（urllib3警告を抑制）。truststore導入後は通常発動しない

> **【2026-07-14修正済み】SSLフォールバック経路が403判定を素通りしていたバグ**
> 旧実装はSSLErrorのexceptブロック内で`raise_for_status()`を呼んでいたため、そこで発生した
> `HTTPError`は同じtryの`except HTTPError`節（403→`AccessForbiddenError`変換・429リトライ）に
> 捕捉されず生のまま上位へ漏れていた。Norton環境では毎リクエストがこのフォールバック経路を
> 通っていたため、**403が出ても「全店舗即中止」が働かず、有楽町unoバックフィル時に403のまま
> 全日を空回りする事故が発生**（2026-07-14）。現在はtryを二重にし、`raise_for_status()`を
> 通常経路・フォールバック経路共通の位置で実行する構造に修正済み。
>
> ```python
> try:
>     try:
>         response = session.get(url, timeout=30)
>     except requests.exceptions.SSLError:
>         urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
>         response = session.get(url, verify=False, timeout=30)
>     response.raise_for_status()  # ← どちらの経路でもここを通る
>     return response
> except requests.exceptions.HTTPError as e:
>     ...  # 403→AccessForbiddenError / 429等リトライ
> ```

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
| `get_connection()` | Turso DBへの**埋め込みレプリカ接続**を返す（`ホールデータ/turso_replica.db`＋`sync_url`。2026-07変更）。接続時に`sync()`でリモート最新状態をローカルへ反映（初回はフルダウンロード）。読み取りはローカル・書き込みはリモートプライマリへ委譲。`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`環境変数が必須（2026-07にリポジトリルートの`.env`ファイルからの読み込みに対応。`db.py`冒頭で`python-dotenv`の`load_dotenv()`を実行。`.env`は`.gitignore`済みでGit管理対象外） |
| `sync_replica(con)` | リモート最新状態をローカルレプリカへ反映。失敗しても警告のみ（書き込みはリモートに到達済みで、次回実行時に回復するため） |
| `setup_db(con)` | 起動時に1回だけ呼ぶ。`CREATE TABLE IF NOT EXISTS`＋`CREATE INDEX IF NOT EXISTS idx_slot_hole_date (ホール名, 日付)`。UNIQUE制約のインデックスは先頭列が日付のため`WHERE ホール名=?`に効かず、Tursoの読み取り行数課金では全表スキャン回避が必須（2026-07追加） |
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
EXIT_CODE_FORBIDDEN = 43  # 403検知時の専用終了コード(fase4/run_daily.pyが判別に使う)

def main():
    stores = load_stores()
    con = get_connection()
    forbidden = False
    try:
        setup_db(con)
        try:
            for hole_name in stores:
                con = process_store(con, hole_name)  # ストリーム失効で再接続した場合、新しいconが返る
        except AccessForbiddenError as e:
            logger.error(f'アクセス拒否(403)のため全店舗の処理を中止します: {e}')
            forbidden = True
        sync_replica(con)  # fase2が読むローカルレプリカを最新化
    finally:
        con.close()

    if forbidden:
        sys.exit(EXIT_CODE_FORBIDDEN)
```

- 2026-07(fase4導入)に403検知時の終了コードを追加。従来は403捕捉後も`sync_replica`を経て**exit 0(正常終了)**していたため呼び出し元が403を判別できなかった。`sync_replica`・`con.close()`は従来どおり実行した上で最後に`sys.exit(43)`する。通常終了・その他の例外の挙動は変えていない。呼び出し元`fase4/run_daily.py`はこのexit 43でその日の評価・分析をスキップする（詳細は[`fase4/日次自動実行_skill.md`](../fase4/日次自動実行_skill.md)参照）

### 日付範囲の自動算出（無人実行対応）

店舗ごとに`get_processed_dates`で取得済み日付を調べ、以下のロジックで対象日付を決める。

ロジックは`compute_remaining_days(processed, today)`（純関数、2026-07切り出し）に集約。

```python
end_date = today - timedelta(days=COLLECT_UNTIL_DAYS_AGO)  # デフォルト1日前(前日)まで

if processed:
    last_date = max(dt.strptime(d, '%Y-%m-%d') for d in processed)
    retry_start = end_date - timedelta(days=RETRY_LOOKBACK_DAYS)  # デフォルト14日前
    # 通常は最終日の翌日から。ただし直近RETRY_LOOKBACK_DAYS内は未処理日(ギャップ)を再試行
    start_date = min(last_date + timedelta(days=1), retry_start)
else:
    # 新規店舗: 初回のみ指定日数さかのぼる
    start_date = today - timedelta(days=INITIAL_BACKFILL_DAYS)  # デフォルト90日
```

- `COLLECT_UNTIL_DAYS_AGO`（デフォルト1日前、2026-07にfase4導入とあわせて2日前→1日前へ短縮）: サイトは前日分を23:00〜翌10:00頃にページ一括更新するため中間状態を取り込むリスクがなく、当日分だけ収集対象から外せば足りる。未更新だった日は`RETRY_LOOKBACK_DAYS`のギャップ再試行が翌日以降拾う。この短縮により翌日予測（機能B）の対象日が「昨日」から「今日」になった
- 日次実行が想定通り毎日走っていれば「前日1日分」だけになり数分で終わる
- 実行が数日〜数週間空いても、最終取得日からのギャップを自動的に埋める（固定の「直近N日」方式だと長期間の抜けが永久に埋まらないため、この方式を採用）
- **`RETRY_LOOKBACK_DAYS`（デフォルト14日、2026-07追加）**: 途中の日が取得失敗した場合、旧実装（最終日の翌日から）ではその日が永久にスキップされた。直近14日は未処理日を走査対象に含めることで自動再試行する。取得済みの日は`processed`で除外されるため再取得はしない。サイト側にページ自体が無い日（店休日等）も14日間は再試行されるが、1日1リクエストの追加で許容範囲。14日より古い失敗日は再試行されない（手動でDBを確認して対応）
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

### Tursoストリーム失効時の自動再接続（2026-07-14追加）

Turso埋め込みレプリカ接続を数時間使い続けると、サーバー側がHranaストリームを打ち切り、
以降の書き込みが `Hrana: api error: status=404 Not Found, body={"error":"stream not found: ...}` で
失敗するようになる。**同じconnectionでは二度と回復しない**ため、旧実装（その日をスキップして続行）では
以降の全日が同じエラーで空回りしていた（有楽町unoの200日バックフィルで163日目・約2.5〜3時間経過時点で
発生し、37日分が欠損した実績あり）。

対策として`process_store`内でこのエラーを`_is_stream_error`（メッセージに`stream not found`を含むか）で
検知し、`con.close()`→`get_connection()`で再接続してから**同じ日を即座に再試行**する。
再接続後の`con`は`process_store`の戻り値として`main()`へ返し、次店舗のループへ引き継ぐ
（このため`process_store`は必ず`con`を返す設計になった）。
取得・書き込み処理は`_fetch_and_write(con, session, hole_name, day)`に切り出し、初回と再試行で共用する。

### 403アクセス拒否時の中断

`AccessForbiddenError`は`process_store`から再送出され、`main()`側で捕捉して**全店舗の処理を中止**する（2026-07変更。以前は該当店舗のみスキップして次の店舗へ進んでいたが、CloudflareのブロックはIP単位のため残り店舗への試行は無駄なリクエストになるだけだった）。中止後もレプリカ同期と接続クローズは実行される。

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
| fase2との連携 | 店舗別SQLiteファイルをfase2が直接読む | **Turso埋め込みレプリカ**（`ホールデータ/turso_replica.db`、2026-07追加）。Turso移行後「fase1はTursoに書くがfase2はローカルの旧DBしか読まない」断絶が生じていたため、`get_connection()`をリモート直接続から埋め込みレプリカ接続に変更。fase2はこのレプリカファイルをsqlite3で読み取り専用参照する（旧店舗別DBはアーカイブ扱い） |

- Turso無料枠はストレージ5GB。店舗数・蓄積データ増加により将来逼迫する可能性があるため、運用しながら使用量を監視し、必要に応じて有料プラン（Developer: $4.99〜/月、9GBストレージ）への移行を検討する
- GitHubリポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （非公開）。`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`はリポジトリのActions Secretsに登録済み（現在は`workflow_dispatch`の手動検証用途のみで使用）。ローカルPC手動実行時はリポジトリルートの`.env`ファイル（`.gitignore`済み・`python-dotenv`で読み込み）に同じ2つの値を設定する
- ローカル実行にはPython 3.12を使用（3.14では`libsql`のビルドに失敗するため。詳細は「db.py」節参照）
- 詳細な移行方針は[`要件定義.md`](../要件定義.md)「3. 配信・公開」参照

---

## 関連ファイル

- 要件定義 → `要件定義.md`
- 構成図 → `fase1/データ収集_構成図.md`
- 依存パッケージ → `fase1/requirements.txt`（requests / beautifulsoup4 / libsql / python-dotenv / truststore）
- 対象店舗一覧 → `fase1/stores.json`
- GitHub Actions定義 → `.github/workflows/scrape.yml`
- 移行用ワンショットスクリプト → `fase1/merge_stores_for_turso.py`（既存ローカルSQLiteをTurso Upload DB用に統合。恒久パイプラインには含まれない）

---

## 旧CLAUDE.md記載の実装詳細（2026-07-14移設・原文のまま保存）

> CLAUDE.mdの省エネ化(2026-07-14)で移設。本文と重複する記述を含むが、
> 情報消失防止のため原文で保存する。矛盾がある場合は本文(各節)側が正。

**フェーズ表の注記(fase1)**:

> ※ fase1は2026-07にTurso(libSQL)対応・非対話化済み。`db.py`はsqlite3→Turso/libsqlクライアントに書き換え（**埋め込みレプリカ方式**: 書き込みはTursoへ委譲しつつ`ホールデータ/turso_replica.db`をローカルに維持し、fase2はこのレプリカを読む）、`メイン.py`は`input()`を廃止し`stores.json`+自動日付算出（前回取得済み最終日の翌日〜前日。2026-07にfase4導入とあわせて2日前から短縮。直近14日の取得失敗日は自動再試行。403発生時は全店舗中止）に変更。**GitHub Actionsでの自動実行(`schedule`)は、ana-slo.com(Cloudflare)がデータセンター系IPを即403ブロックすることが判明したため停止中**（`workflow_dispatch`の手動トリガーのみ残す）。現在はPC上でfase4のタスクスケジューラが`py -3.12 メイン.py`を毎日自動実行し、Tursoへ書き込む運用（手動実行も引き続き可能）。**2026-07-14追加**: ①Tursoストリーム失効(`stream not found`。長時間接続で発生し同じconnectionでは回復しない)を検知したら再接続して同日を再試行する自動回復を`メイン.py`に実装、②`scraper.py`に`truststore`を導入しSSL検証をWindows証明書ストアで実施(Norton AntivirusのHTTPSスキャンによる証明書差し替えでcertifi検証が常に失敗し`verify=False`で動いていた問題の根本対応)、③SSLフォールバック経路で403→`AccessForbiddenError`変換が素通りするバグを修正。リポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （非公開）。詳細は[`fase1/データ収集_skill.md`](データ収集_skill.md)参照
