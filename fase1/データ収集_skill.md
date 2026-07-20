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
| ⑤ Webドライバー取得 | SeleniumBase (UC Mode) でページ取得。Cloudflare 回避・リトライ3回・指数バックオフ対応。**取得直後に`is_block_page`でブロックの疑いを判定**（2026-07-20追加。層1） | `scraper.fetch_page` |
| ⑥ HTML解析 | section単位でカラム数自動検出・台データ抽出 | `scraper.get_info` |
| ⑦ データ保存 | 正常データ → `slot_data`、欠損 → `missing_data`（Turso DBへ） | `db.write_db` / `write_missing` / `write_null_record` |
| ⑧ レート制御 | 通常10〜40秒待機、20件ごとに5分休憩、**1回の実行で最大`MAX_REQUESTS_PER_RUN`(100)リクエストまで**（2026-07-20追加。同日50→100へ変更） | `メイン.py` |
| ⑨ レプリカ同期 | 実行終了時に`sync()`でローカルレプリカ（`ホールデータ/turso_replica.db`）を最新化。fase2はこれを読む | `db.sync_replica` |

- **403（`AccessForbiddenError`）発生時は全店舗の処理を即中止する**。CloudflareのブロックはIP単位のため、残り店舗への試行は無駄なリクエストで被ブロック実績を積むだけになる（2026-07変更。以前は該当店舗のみスキップ）
- **Tursoストリーム失効（`stream not found`）時は再接続して同じ日を再試行する**（2026-07-14追加。詳細は「メイン.py」節参照）
- **ブロック検知は2層構成**（2026-07-20追加。「ブロック検知2層構成」節参照）: 層1=`fetch_page`内で`is_block_page`が骨格欠如/本文空を検知したら即`AccessForbiddenError`。層2=`メイン.py`のサーキットブレーカーが「連続3店舗が全対象日『ページにデータなし』」を検知したら同じく`AccessForbiddenError`扱いで中止
- **1回の実行あたりリクエスト上限100件**（`MAX_REQUESTS_PER_RUN`）。到達すると正常終了(exit 0)し、残りは翌回の実行が続きから自動再開する（「総リクエスト上限」節参照）

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
- **取得直後に`is_block_page(html)`を呼び、ブロックの疑いがあれば`AccessForbiddenError`を送出**（2026-07-20追加。リトライしない）

#### `is_block_page(html)`（2026-07-20追加。ブロック検知 層1）

純関数。2026-07-17に発生した「空ページ403の偽成功」（`fetch_page`が空ボディ403を検知できず、
ブロック中の応答を正常な『ページにデータなし』として全店舗×複数日にわたり誤記録した事故。
詳細は「ブロック検知2層構成」節）の再発防止策。

```python
def is_block_page(html: str) -> bool:
    has_skeleton = 'ana-slo' in html.lower() and soup.find('title') is not None
    body_nearly_empty = len(soup.get_text(strip=True)) < BLOCK_PAGE_BODY_TEXT_MIN_LEN  # 200文字
    return (not has_skeleton) or body_nearly_empty
```

- 判定はA・B併用のOR条件: A=サイト骨格の目印(`ana-slo`文字列・`<title>`タグ)の欠如、B=本文テキストがほぼ空(200文字未満)
- 正常な「データなし」日もWordPressのサイト骨格（ヘッダー/フッターのナビ文言等）は必ず持つため、骨格が欠けている時点でブロックの疑いが強いと判断できる
- テスト: `tests/test_scraper_block_detection.py`（自作HTMLフィクスチャ。正常データページ/正常データなしページ/空HTML/骨格欠如/本文空/骨格はあるが本文が長いCloudflare代替ページ、の6ケース）

### `build_url(slug, date)`
- スラッグを `quote(safe='')` でエンコードしてURLを生成
- `.lower()` は**使わない**（hexが小文字になり実サイトのURL形式と不一致になるため）

### `get_info(html, url, hole_date)`
- 第1引数が `session`（requests） から `html` (文字列) に変更（2026-07-16のSeleniumBase移行時。requests時代のSSLフォールバック経路は廃止済み）
- BeautifulSoup で HTML 解析（変わらず）
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

`input()`による対話入力は廃止。`stores.json`（`{"stores": ["スラッグ1", "スラッグ2", ...]}`）から対象ホール一覧を読み込み、1つのTurso接続(`con`)を使い回して店舗ごとに処理する。`stores`の各文字列は**DB上のホール名**であると同時に**日次データURLのスラッグ**も兼ねる（`build_url(hole_name, day)`）。

```python
EXIT_CODE_FORBIDDEN = 43  # 403検知時の専用終了コード(fase4/run_daily.pyが判別に使う)

def main():
    stores = load_stores()
    con = get_connection()
    forbidden = False
    budget_exhausted = False
    consecutive_no_data = 0
    requests_remaining = MAX_REQUESTS_PER_RUN
    try:
        setup_db(con)
        try:
            for hole_name in stores:
                con, store_all_no_data, requests_used = process_store(con, hole_name, requests_remaining)
                requests_remaining -= requests_used

                consecutive_no_data, tripped = update_circuit_breaker(consecutive_no_data, store_all_no_data)
                if tripped:
                    raise AccessForbiddenError('連続3店舗で全対象日が「ページにデータなし」のためブロックの疑いがあり中止します')

                if requests_remaining <= 0:
                    budget_exhausted = True
                    break
        except AccessForbiddenError as e:
            logger.error(f'アクセス拒否(403)またはブロック疑いのため全店舗の処理を中止します: {e}')
            forbidden = True

        if budget_exhausted and not forbidden:
            _log_remaining_backlog(con, stores)  # 残り取得対象日数をログに出すだけ(書き込みなし)

        sync_replica(con)  # fase2が読むローカルレプリカを最新化
    finally:
        con.close()

    if forbidden:
        sys.exit(EXIT_CODE_FORBIDDEN)
```

- 2026-07(fase4導入)に403検知時の終了コードを追加。従来は403捕捉後も`sync_replica`を経て**exit 0(正常終了)**していたため呼び出し元が403を判別できなかった。`sync_replica`・`con.close()`は従来どおり実行した上で最後に`sys.exit(43)`する。通常終了・その他の例外の挙動は変えていない。呼び出し元`fase4/run_daily.py`はこのexit 43でその日の評価・分析をスキップする（詳細は[`fase4/日次自動実行_skill.md`](../fase4/日次自動実行_skill.md)参照）
- 2026-07-20に**サーキットブレーカー(層2)**と**総リクエスト上限**を追加（詳細は各節参照）。`process_store`の戻り値が`con`単体から`(con, store_all_no_data, requests_used)`のタプルに変更（ストリーム失効時に新しい`con`が返る点は従来どおり）
- **`catchup_only_stores`と`--mode`（2026-07-20追加・リクエスト削減案B簡易版）**: `stores.json`に`"catchup_only_stores": ["三ノ輪uno", "新橋uno"]`のように、夕方更新が常態でfase4のmorningポーリングを長引かせる店舗を登録できる（`stores`の部分集合であることを`validate_catchup_only_stores`が起動時に検証し、違反時は`ValueError`で即停止）。`メイン.py`に`--mode morning|all`引数を追加（省略時`all`＝従来どおり全店対象で手動実行に非破壊）。`--mode morning`のときだけ`stores_for_mode(all_stores, catchup_only_stores, mode)`（純関数）が`catchup_only_stores`をスキップした店舗リストを返す。呼び出し元`fase4/run_daily.py`は独自の`--mode`（morning/catchup。全く別物）からこのメイン.py側`--mode`へ`_main_py_mode()`で変換して渡す（morning→morning, catchup→all）。詳細・運用注意は[`fase4/日次自動実行_skill.md`](../fase4/日次自動実行_skill.md)参照

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
- **負キャッシュ（`given_up`引数、2026-07-20追加・リクエスト削減案A）**: `compute_remaining_days(processed, today, given_up=set())`の第3引数。`db.get_no_data_giveup_dates(con, hole_name, giveup_days=NO_DATA_GIVEUP_DAYS)`が`missing_data`テーブルから`理由='ページにデータなし'`の記録を`GROUP BY 日付 HAVING COUNT(DISTINCT date(記録日時)) >= NO_DATA_GIVEUP_DAYS`（デフォルト3）で集計し、異なる暦日にまたがって`NO_DATA_GIVEUP_DAYS`回以上「データなし」が観測された対象日の集合を返す。この集合は取得対象からリタイア（除外）する。**同一日に複数回実行しても同じ暦日には1カウントしかしない**（`date(記録日時)`でDISTINCT）ため、「1日粘れば取れるかもしれない一時的な欠損」を誤ってリタイアさせない。`process_store`・`_log_remaining_backlog`の両方でこの集合を`compute_remaining_days`へ渡す。デフォルト値は空setのため引数省略時は従来と完全一致する（非破壊）。

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

`AccessForbiddenError`は`process_store`から再送出され、`main()`側で捕捉して**全店舗の処理を中止**する（2026-07変更。以前は該当店舗のみスキップして次の店舗へ進んでいたが、CloudflareのブロックはIP単位のため残り店舗への試行は無駄なリクエストになるだけだった）。中止後もレプリカ同期と接続クローズは実行される。**サーキットブレーカー(層2)が作動した場合も同じ`AccessForbiddenError`として同じ経路(exit 43)で中止する**（2026-07-20追加。下記節参照）。

### ブロック検知2層構成（2026-07-20導入）

2026-07-17の「空ページ403の偽成功」事故（`fetch_page`が空ボディ403を検知できず、ブロック中の応答を
『ページにデータなし』として全店舗×複数日にわたり誤記録した。詳細はメモリ`project_pc_403_block_incident`）
の再発防止策。テストは実装前に先行作成（`tests/test_scraper_block_detection.py`・`tests/test_circuit_breaker.py`）。

- **層1（即時検知）**: `scraper.is_block_page(html)`（前述の「scraper.py」節参照）を`fetch_page`が
  取得直後に呼び、骨格欠如または本文ほぼ空を検知したら即`AccessForbiddenError`
- **層2（サーキットブレーカー・層1の見逃し対策）**: `process_store`が店舗ごとに「対象日全てが
  `classify_day_result`で`'no_data'`（＝セクション自体が見つからず『ページにデータなし』のみ記録）
  だったか」を`(bool | None)`で返す（対象日0件の店舗は`None`＝中立）。`main()`が`update_circuit_breaker`で
  連続カウントを更新し、**連続`CIRCUIT_BREAKER_THRESHOLD`(3)店舗**が該当すると`AccessForbiddenError`を
  送出して中止する。データが1日でも取得できた店舗、または対象日0件の店舗はカウントをリセット/変更しない

```python
def classify_day_result(data_list, missing_machines) -> str:
    # 'no_data' = セクションが見つからず『ページにデータなし』のみが記録された日
    # 'other'   = データを取得できた日、またはそれ以外の欠損・処理エラーの日
    ...

def all_days_no_data(day_statuses: list[str]) -> bool | None:
    # 店舗の全処理日が'no_data'だったか。処理日0件ならNone(中立)
    ...

def update_circuit_breaker(consecutive: int, store_all_no_data: bool | None, threshold=3) -> tuple[int, bool]:
    # store_all_no_data=None は中立(カウント変更なし)。戻り値は(更新後consecutive, 作動したか)
    ...
```

いずれも純関数のため`tests/test_circuit_breaker.py`でSeleniumBase/DBに触れずに単体テストできる。

### 総リクエスト上限（`MAX_REQUESTS_PER_RUN`、2026-07-20導入）

1回の実行（`メイン.py`起動）で送信するリクエスト数（≒処理日数）の上限。デフォルト100（2026-07-20導入時は50、同日ユーザー判断で100へ引き上げ。「連続アクセス30回で403」「20件+5分休憩で120件まで通過」の既存実績の範囲内）。

- `process_store(con, hole_name, requests_remaining)`が店舗ごとの対象日リストを
  `remaining[:requests_remaining]`で先頭から切り詰めて取得し、実際に消費した件数`requests_used`を返す
- `main()`が全店舗をまたいで`requests_remaining`を減算していき、0以下になった時点でその実行を打ち切る
  （**exit 0の正常終了**。403/ブロック疑いとは異なりexit 43にはしない）
- 打ち切り時は`_log_remaining_backlog`で全店舗の残り取得対象日数をログに出す（書き込みは発生しない）
- 未処理分は`compute_remaining_days`のギャップ再試行ロジックにより**翌回の実行が自動的に続きから再開する**
  （バックフィルは昇順のため取りこぼしなく繋がる）。戸越銀座店の90日バックフィルもこの仕組みで数日に自動分割される
  （末尾店舗のため他店の当日分消費後に残った枠を使う。上限100なら概算1〜2日で完了）
- **新規店舗は`stores.json`の末尾に追加する運用**（既存店舗の取得が上限で後回しにならないようにするため。
  `.claude/skills/add-new-store/SKILL.md`にも明記）
- 手動で同日中に連続実行して早く揃えようとするのは非推奨（2026-07-14型の集中リクエストになりブロックを誘発しうる）

### 欠損処理

```python
data_list, data_column_list, data_row_list, missing_machines = get_info(html, url, day)
if data_list:
    write_db(con, data_list, data_column_list, data_row_list, hole_name, day)
for machine_name, reason in missing_machines:
    write_missing(con, hole_name, day, machine_name, reason)
    if machine_name:                          # 機種名が特定できた場合のみ
        write_null_record(con, hole_name, day, machine_name)
return classify_day_result(data_list, missing_machines)  # 2026-07-20追加。層2サーキットブレーカー用
```

- `machine_name=None`（ページにデータなし）は `missing_data` のみ記録
- `machine_name` あり（カラム数特定不可）は `missing_data` 記録 + `slot_data` にNULLレコード挿入
- 2026-07-17の偽成功で誤記録された約100行（理由='ページにデータなし'・記録日時2026-07-17）は
  `fase1/fix_block_misrecorded_missing_data.py`で理由を訂正できる（**実行はブロック解除・日次収集の
  正常再開を確認してから**。missing_dataはどのコードからも読まれないため急ぎではない）

### バッチ休憩（Cloudflare対策）

```python
BATCH_SIZE  = 20      # この件数ごとに長めの休憩
BATCH_BREAK = 60 * 5  # バッチ休憩時間（秒）
```

- 連続アクセス約30回でCloudflareに403を食らった実績あり
- BATCH_SIZE=20 + BATCH_BREAK=5分で120件まで通過を確認
- 120件で再び403が発生する場合はBATCH_SIZEを下げるかBATCH_BREAKを延ばすことで調整する
- **`LONG_BREAK_AT`(100件時点で1回だけ20分休憩する試験的対策)は2026-07-20に削除**。
  同日導入した`MAX_REQUESTS_PER_RUN=100`（「総リクエスト上限」節）により`process_store`の
  対象日リストが常に100件以下に切り詰められるため、「100件処理した直後にまだ続きがある」
  という`LONG_BREAK_AT`の発火条件が構造的に成立しなくなった（到達不能コードのため削除）

### ドライバー再生成（バッチ休憩後）

バッチ休憩の前に`driver.quit()`でブラウザを閉じ、休憩後に`create_driver()`で
新規生成する（requests時代の「セッション再生成」に相当。ブラウザを開いたまま長時間
放置しない・休憩ごとに新しいブラウザ指紋で再開する）。処理全体は`try/finally`で囲み、
最後に必ず`driver.quit()`する。

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
- GitHubリポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （公開。2026-07-17public化）。`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`はリポジトリのActions Secretsに登録済み（`scrape.yml`/`recon.yml`の2ワークフローが使用。どちらも`concurrency: ana-slo-scraping`で同時実行1本に制限。`recover.yml`はクラウド実行断念に伴い削除済み）。ローカルPC手動実行時はリポジトリルートの`.env`ファイル（`.gitignore`済み・`python-dotenv`で読み込み）に同じ2つの値を設定する
- ローカル実行にはPython 3.12を使用（3.14では`libsql`のビルドに失敗するため。詳細は「db.py」節参照）
- 詳細な移行方針は[`要件定義.md`](../要件定義.md)「3. 配信・公開」参照

---

## 関連ファイル

- 要件定義 → `要件定義.md`
- 構成図 → `fase1/データ収集_構成図.md`
- 依存パッケージ → `fase1/requirements.txt`（seleniumbase / beautifulsoup4 / libsql / python-dotenv / truststore。2026-07-17にrequests→seleniumbaseへ差し替え）
- 対象店舗一覧 → `fase1/stores.json`
- GitHub Actions定義 → `.github/workflows/scrape.yml`（日次収集・手動のみ）/ `recon.yml`（バラエティ欠損偵察）。旧`recover.yml`（バラエティ欠損復元）はクラウド実行断念に伴い削除済み（復元はPC上で`recover_variety_gaps.py`を直接実行する）
- 移行用ワンショットスクリプト → `fase1/merge_stores_for_turso.py`（既存ローカルSQLiteをTurso Upload DB用に統合。恒久パイプラインには含まれない）
- 欠損復元スクリプト → `fase1/recover_variety_gaps.py`（バラエティ最終行バグの偵察・復元。恒久パイプラインには含まれない）
- 誤記録修正スクリプト → `fase1/fix_block_misrecorded_missing_data.py`（2026-07-17の空ページ403偽成功でmissing_dataに誤記録された理由をUPDATEするワンショットツール。既定はdry-run、`--apply`で実行。恒久パイプラインには含まれない。実行タイミングは「欠損処理」節参照）

---

## 旧CLAUDE.md記載の実装詳細（2026-07-14移設・原文のまま保存）

> CLAUDE.mdの省エネ化(2026-07-14)で移設。本文と重複する記述を含むが、
> 情報消失防止のため原文で保存する。矛盾がある場合は本文(各節)側が正。

**フェーズ表の注記(fase1)**:

> ※ fase1は2026-07にTurso(libSQL)対応・非対話化済み。`db.py`はsqlite3→Turso/libsqlクライアントに書き換え（**埋め込みレプリカ方式**: 書き込みはTursoへ委譲しつつ`ホールデータ/turso_replica.db`をローカルに維持し、fase2はこのレプリカを読む）、`メイン.py`は`input()`を廃止し`stores.json`+自動日付算出（前回取得済み最終日の翌日〜前日。2026-07にfase4導入とあわせて2日前から短縮。直近14日の取得失敗日は自動再試行。403発生時は全店舗中止）に変更。**GitHub Actionsでの自動実行(`schedule`)は、ana-slo.com(Cloudflare)がデータセンター系IPを即403ブロックすることが判明したため停止中**（`workflow_dispatch`の手動トリガーのみ残す）。現在はPC上でfase4のタスクスケジューラが`py -3.12 メイン.py`を毎日自動実行し、Tursoへ書き込む運用（手動実行も引き続き可能）。**2026-07-14追加**: ①Tursoストリーム失効(`stream not found`。長時間接続で発生し同じconnectionでは回復しない)を検知したら再接続して同日を再試行する自動回復を`メイン.py`に実装、②`scraper.py`に`truststore`を導入しSSL検証をWindows証明書ストアで実施(Norton AntivirusのHTTPSスキャンによる証明書差し替えでcertifi検証が常に失敗し`verify=False`で動いていた問題の根本対応)、③SSLフォールバック経路で403→`AccessForbiddenError`変換が素通りするバグを修正。リポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （非公開）。詳細は[`fase1/データ収集_skill.md`](データ収集_skill.md)参照
