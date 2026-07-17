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

`ana-slo.com` からパチスロホールの台データをスクレイピングし、クラウドDB「Turso」（libSQL/SQLite互換）に保存する。実行は**ローカルPC（住宅IP）上のfase4タスクスケジューラ**で行う。

> **クラウド実行は不可（2026-07-17確定）**: 2026-07-16に**SeleniumBase (UC Mode)** を導入し、
> Cloudflareを回避してGitHub Actionsでのクラウド実行を再開しようとしたが、2026-07-17の診断で
> **ana-slo.comがGitHub ActionsのIP（データセンター/AS）をトップページごと空ボディ403でブロック**
> していることが確定した。チャレンジ画面すら出ないIP/ASレベルのブロックのため、UC Mode・
> curl_cffi等のフィンガープリント偽装では突破できない（住宅IPが必須）。よってクラウド化は断念し、
> PC実行を継続する。詳細は[`fase4/日次自動実行_skill.md`](../fase4/日次自動実行_skill.md)参照。
> SeleniumBase自体はPC実行のブラウザ手段として残置している（PCの住宅IPでは動作する）。

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

実行環境:
- **ローカルPC**（唯一の実運用経路）: `py -3.12 メイン.py`（Python 3.12必須。3.14は`libsql`ビルド不可）。fase4タスクスケジューラが毎日自動実行
- **GitHub Actions**: `.github/workflows/scrape.yml`は残置しているが、Actions IPが403ブロックされるため実行しても取得できない（将来住宅プロキシ等を検討する場合の検証用）
- **手動偵察**: `py -3.12 fase1/recover_variety_gaps.py --recon`（バラエティ欠損確認。PC上で実行する）

収集を伴わずレプリカだけ最新化したい場合は `py -3.12 fase1/sync_replica.py`。
リポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker
（**公開**。2026-07-17にコミット履歴のメールアドレスをnoreplyへ書き換え、トークン・DBファイル
非含有を履歴含めて確認のうえpublic化。当初はGitHub Actionsでのクラウド実行を目的に公開したが、
Actions IPが403ブロックされクラウド実行は断念。公開設定は将来のために維持している）

---

## 処理フロー

| フェーズ | 処理 | 担当 |
|---|---|---|
| ① 店舗一覧読み込み | `stores.json`から対象ホール（スラッグ）一覧を取得 | `メイン.py.load_stores` |
| ② DB初期化 | テーブル作成（`IF NOT EXISTS`、Turso上に無ければ作成） | `db.setup_db` |
| ③ 日付範囲の自動算出 | 店舗ごとに取得済み最終日を調べ、翌日〜`COLLECT_UNTIL_DAYS_AGO`(1日前=前日)を対象に。加えて直近`RETRY_LOOKBACK_DAYS`(14日)内の未処理日（取得失敗によるギャップ）も再試行対象に含める（新規店舗は`INITIAL_BACKFILL_DAYS`＝90日分バックフィル） | `メイン.py.compute_remaining_days` + `db.get_processed_dates` |
| ④ URL構築 | `https://ana-slo.com/{日付}-{slug}-data/` | `scraper.build_url` |
| ⑤ Webドライバー取得 | SeleniumBase (UC Mode) でページ取得。Cloudflare 回避・リトライ3回・指数バックオフ対応 | `scraper.fetch_page` |
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

### Cloudflare 対策と SeleniumBase (UC Mode)（2026-07-16追加）

2026-07-16に **requests から SeleniumBase へ移行**。理由：
- GitHub Actions のAWS DCプール IP が Cloudflare に IP 単位でブロックされていた
- SeleniumBase の UC (undetected-chromedriver) モードは Cloudflare の検知を回避
- ローカル PC でも GitHub Actions でも同じコードで動作

#### `create_driver()`
- **SeleniumBaseDriver ラッパークラス**を返す（context manager ベース）
- `uc=True` で UC モード（Cloudflare 回避）、`headless=True` でヘッドレス実行
- `start()` / `quit()` で自動的にブラウザの起動・終了を管理

#### SSL証明書検証と truststore（2026-07-14追加、2026-07-16継続）

`scraper.py` 冒頭で `truststore.inject_into_ssl()` を実行。
- Windows PC の Norton Antivirus がHTTPSスキャン用に証明書を差し替えているため、truststore で OS 証明書ストア を使用
- GitHub Actions (Linux) でも OS のストアを参照するため、環境依存性なし
- requirements.txt に `truststore` を追加

#### `fetch_page(driver, url)`
- SeleniumBase ドライバーでページ取得（HTML テキストを返す）
- MAX_RETRIES=3、指数バックオフ（RETRY_BASE_WAIT=60秒）でリトライ
- **403 は `AccessForbiddenError` を送出**（リトライしない）

### `build_url(slug, date)`
- スラッグを `quote(safe='')` でエンコードしてURLを生成
- `.lower()` は**使わない**（hexが小文字になり実サイトのURL形式と不一致になるため）

### `get_info(html, url, hole_date)`
- 第1引数が `session`（requests） から `html` (文字列) に変更（2026-07-16）
- BeautifulSoup で HTML 解析（変わらず）
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
- **行数**: 実際に取り込んだセル数から `count // n` で算出（2026-07-14修正。旧実装の`len(datas) // n - 1`は平均行を持たないバラエティ表で最終行を取り捨てるバグがあった→「バラエティ最終行の取り捨てバグ」節参照）
- データ抽出は `count % n == 0` でrow境界を検出し、日付・機種名をプリペンド
- `'平均'` セルで抽出を打ち切る。末尾に不完全な行が残った場合は警告を出して除外する
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

#### スラッグとDBホール名の分離（`slug_overrides`、2026-07-17追加）

`stores`の各文字列は**DB上のホール名**であると同時に、既定では**日次データURLのスラッグ**も兼ねる（`build_url(hole_name, day)`）。ただし**ana-slo.comは店舗の日次URLスラッグを予告なく変えることがある**（2026-07に有楽町unoが`有楽町uno`→`uno-yurakucho`へ変更され、旧URLが404化して7/12以降が全て「ページにデータなし」になった実例）。

これに対応するため`stores.json`に任意で`slug_overrides`（`{"ホール名": "新スラッグ"}`）を持てるようにした。`メイン.py.slug_for(hole_name)`が「override指定があればそのスラッグ、無ければホール名そのまま」を返し、`build_url`呼び出し（`メイン.py`・`recover_variety_gaps.py`の2箇所）はこれを経由する。

- **DB上のホール名は変えない**（`write_db`/`get_processed_dates`等は従来どおりホール名を使う）ため、過去データ・fase2/fase3・分析DBは一切影響を受けず、店舗の同一性が保たれる
- URL変更に気づく手口: ある店だけ`missing_data`に「ページにデータなし」が連続し、サイトの店舗トップ（例 `ana-slo.com/hole/uno-yurakucho/`）には最新データがあるのに日次URLが404。新スラッグは店舗トップのURL末尾が手がかり
- 例: `"slug_overrides": {"有楽町uno": "uno-yurakucho"}`

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

## バラエティ最終行の取り捨てバグと復元（2026-07-14発覚・修正）

**サマリー**: 旧`get_info`の行数計算`len(datas)//n - 1`は「表末尾に平均行がある」前提だったが、バラエティ(1台設置機種)表には平均行が無いため、**バラエティ最終行(台番号最大の1台)を毎日・全店舗で無言で取り捨てていた**。エラーも`missing_data`記録も出ないため長期間気づけなかった。scraper.pyは実取り込み行数`count // n`を使う方式に修正済み。過去分の欠損は`recover_variety_gaps.py`で復元する。

### 発覚の経緯と欠損の構造

- 東中野で「不二子BT(台番号220)が2026-05-11〜07-11の62日間DBに無い」ことから発覚
- 5/10以前は台番号の並びが違い、最終行=虚構推理(219)が犠牲になっていた（＝**バグは収集開始時から常時発生**。5/11の店側の台番号振り直しで犠牲の台が交代しただけ）
- 通常の機種セクションは平均行（n セル）があるため`-1`が正しく機能し影響なし。**欠けるのは常にバラエティ表の一番下の1台のみ**
- 表の並びが一時的に変わった日はその日だけ別の台が欠ける（他店で検出された単日欠損6件もこのパターンの可能性が高い）

### 復元ツール: `recover_variety_gaps.py`

```bash
# 偵察: 全店舗の最新収集日ページを再取得し、DBに無い行を報告（書き込みなし・店舗数ぶんのリクエスト）
py -3.12 fase1/recover_variety_gaps.py --recon

# 復元: 店舗×日付範囲を再取得し、欠けている行だけをINSERT OR IGNOREで追記
py -3.12 fase1/recover_variety_gaps.py --hole bigディッパー東中野店 --start 2025-12-22 --end 2026-05-10

# 復元(範囲自動): --start/--end省略時はDBのその店舗のMIN(日付)〜MAX(日付)が対象
py -3.12 fase1/recover_variety_gaps.py --hole yasuda7
```

**実行はPC上で行う**（Actions IPは403のためクラウド実行不可。上記コマンドをGit Bashで実行）。

- 冪等（既存行はUNIQUE制約で無視）。403で中断しても同じコマンドの再実行で続きから再開できる
- アクセス間隔はメイン.pyと同じ（40秒サイクル・20件ごと5分休憩）。約90日分で1〜2時間、
  約195日分で約3時間
- 復元後は`fase2/run_store_profile.py`（全店舗）で分析成果物の再生成が必要

### 復元状況（2026-07-17時点）

2026-07-16のPC偵察（`--recon`）で**東中野以外の全10店舗に現在もバラエティ1台の欠損がある**ことを
確認済み（yasuda7=ゾンビランドサガ(322)、エスパス=頭文字D 2nd(1410)、
新高円寺=ヱヴァ約束の扉(552)、マルハン=ゾンビランドサガ(698)、グランパ中野=スマスロサンダーV(311)、
楽園池袋=いざ番長(5115)、プレサス=戦国コレクション6(367)、新橋uno=ヱヴァ約束の扉(582)、
三ノ輪uno=ダークハイビ(620)、有楽町uno=クランキークレスト(530)）。これは各店の「最新収集日1日分」の
欠損確認であり、過去全期間分（各店の収集開始〜現在）の復元には`--hole`で店舗ごとに範囲指定して実行する。

| 対象 | 期間 | 状態 |
|---|---|---|
| 東中野 不二子BT(220) | 2026-05-11〜07-11 | 復元済み（2026-07-14、62日分+他店単日4ページ） |
| 東中野 虚構推理(219)ほか | 収集開始2025-12-19〜2026-05-10 | **未復元**（`--hole bigディッパー東中野店 --start 2025-12-19 --end 2026-05-10`） |
| 他10店舗 | 全期間（各約192〜198日） | **未復元**（`--hole 店舗名`で範囲自動。1店舗ずつPC実行） |
| bigディッパー戸越銀座店 | — | DBにデータなし（新規店舗・バックフィル未実施のため復元対象外） |

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
- GitHubリポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （公開。2026-07-17public化）。`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`はリポジトリのActions Secretsに登録済み（`scrape.yml`/`recon.yml`/`recover.yml`の3ワークフローが使用。3つとも`concurrency: ana-slo-scraping`で同時実行1本に制限）。ローカルPC手動実行時はリポジトリルートの`.env`ファイル（`.gitignore`済み・`python-dotenv`で読み込み）に同じ2つの値を設定する
- ローカル実行にはPython 3.12を使用（3.14では`libsql`のビルドに失敗するため。詳細は「db.py」節参照）
- 詳細な移行方針は[`要件定義.md`](../要件定義.md)「3. 配信・公開」参照

---

## 関連ファイル

- 要件定義 → `要件定義.md`
- 構成図 → `fase1/データ収集_構成図.md`
- 依存パッケージ → `fase1/requirements.txt`（seleniumbase / beautifulsoup4 / libsql / python-dotenv / truststore。2026-07-17にrequests→seleniumbaseへ差し替え）
- 対象店舗一覧 → `fase1/stores.json`
- GitHub Actions定義 → `.github/workflows/scrape.yml`（日次収集・手動のみ）/ `recon.yml`（バラエティ欠損偵察）/ `recover.yml`（バラエティ欠損復元）
- 移行用ワンショットスクリプト → `fase1/merge_stores_for_turso.py`（既存ローカルSQLiteをTurso Upload DB用に統合。恒久パイプラインには含まれない）
- 欠損復元スクリプト → `fase1/recover_variety_gaps.py`（バラエティ最終行バグの偵察・復元。恒久パイプラインには含まれない）

---

## 旧CLAUDE.md記載の実装詳細（2026-07-14移設・原文のまま保存）

> CLAUDE.mdの省エネ化(2026-07-14)で移設。本文と重複する記述を含むが、
> 情報消失防止のため原文で保存する。矛盾がある場合は本文(各節)側が正。

**フェーズ表の注記(fase1)**:

> ※ fase1は2026-07にTurso(libSQL)対応・非対話化済み。`db.py`はsqlite3→Turso/libsqlクライアントに書き換え（**埋め込みレプリカ方式**: 書き込みはTursoへ委譲しつつ`ホールデータ/turso_replica.db`をローカルに維持し、fase2はこのレプリカを読む）、`メイン.py`は`input()`を廃止し`stores.json`+自動日付算出（前回取得済み最終日の翌日〜前日。2026-07にfase4導入とあわせて2日前から短縮。直近14日の取得失敗日は自動再試行。403発生時は全店舗中止）に変更。**GitHub Actionsでの自動実行(`schedule`)は、ana-slo.com(Cloudflare)がデータセンター系IPを即403ブロックすることが判明したため停止中**（`workflow_dispatch`の手動トリガーのみ残す）。現在はPC上でfase4のタスクスケジューラが`py -3.12 メイン.py`を毎日自動実行し、Tursoへ書き込む運用（手動実行も引き続き可能）。**2026-07-14追加**: ①Tursoストリーム失効(`stream not found`。長時間接続で発生し同じconnectionでは回復しない)を検知したら再接続して同日を再試行する自動回復を`メイン.py`に実装、②`scraper.py`に`truststore`を導入しSSL検証をWindows証明書ストアで実施(Norton AntivirusのHTTPSスキャンによる証明書差し替えでcertifi検証が常に失敗し`verify=False`で動いていた問題の根本対応)、③SSLフォールバック経路で403→`AccessForbiddenError`変換が素通りするバグを修正。リポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （非公開）。詳細は[`fase1/データ収集_skill.md`](データ収集_skill.md)参照
