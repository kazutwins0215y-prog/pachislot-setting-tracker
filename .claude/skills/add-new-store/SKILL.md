---
name: add-new-store
description: Use when the user asks to add a new pachislot store/hall to data collection ("新店舗を追加", "新しいホールを追加", "店舗データを追加したい" など), given an ana-slo.com store URL and a desired collection start date. Covers slug extraction, stores.json registration, historical backfill via INITIAL_BACKFILL_DAYS, and the fase2 post-processing needed before the new store shows up in 機能A/B.
---

# 新店舗データ追加 skill

fase1（データ収集）に新しいホールを登録し、指定した開始日まで過去データをバックフィルし、
fase2（分析）側の成果物にも反映させるまでの一連の手順。

前提: [`CLAUDE.md`](../../../CLAUDE.md) のフェーズ構成、[`fase1/データ収集_skill.md`](../../../fase1/データ収集_skill.md) を参照。

## 手順

### 1. store URLからスラッグを抽出する

ana-slo.comの店舗一覧ページURL（例: `https://ana-slo.com/ホールデータ/東京都/{店舗名}-データ一覧/`）から、
`fase1/scraper.py`の`extract_slug`と同じロジックでスラッグを求める。

- URLパスを`unquote`でデコード
- 末尾セグメントを取得
- `-データ一覧`サフィックスを除去

例: `.../グランパ中野-データ一覧/` → スラッグ `グランパ中野`

### 2. `fase1/stores.json`にスラッグを追加する

`stores`配列の末尾に追加する（既存店舗の順序・内容は変更しない）。

### 3. バックフィル期間を決める

`fase1/メイン.py`の新規店舗ロジックは`INITIAL_BACKFILL_DAYS`（デフォルト90日）だけ本日からさかのぼる。
90日で足りない開始日を指定された場合は、**環境変数`INITIAL_BACKFILL_DAYS`で一時的に上書きする**
（2026-07に`int(os.environ.get('INITIAL_BACKFILL_DAYS', 90))`へ変更済み。既存店舗は取得済み日付があるため
このロジックの影響を受けず、新規店舗のみバックフィル日数が変わる）。

必要日数の計算式:

```
必要日数 = (今日の日付 - 収集開始希望日).days + 数日分の余裕（実行日ズレ対策として+3〜5日）
```

例: 今日が2026-07-06で開始日が2026-01-01なら186日 → 余裕を見て190日を指定する。

### 4. 実行コマンド

前提: リポジトリルートの`.env`ファイルに`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`が設定済みであること
（`db.py`が`python-dotenv`で自動読み込みする。`.env`が無い/値が空だと`KeyError: 'TURSO_DATABASE_URL'`で失敗する）。

Git Bash:

```bash
INITIAL_BACKFILL_DAYS=<必要日数> py -3.12 fase1/メイン.py
```

PowerShell（`VAR=value command`構文が無いため`$env:`で設定してから実行する）:

```powershell
$env:INITIAL_BACKFILL_DAYS=190; py -3.12 fase1/メイン.py
```

- 環境変数はこの実行の1回限り。`メイン.py`のデフォルト値(90)は変更されない
- 全店舗（既存店舗含む）が同時に処理されるが、既存店舗は通常の差分取得のみ行われる
- **1回の実行で送信するリクエストは最大`MAX_REQUESTS_PER_RUN`(100)件まで**（2026-07-20導入。当日50→100へ変更）。
  上限に達すると正常終了(exit 0)し、残りの日数は翌回の実行（次のタスクスケジューラ起動、または同じコマンドの
  再実行）で自動的に続きから再開する。90日分のバックフィルなら概算1〜2回の実行で収まる計算（新規店舗は
  `stores.json`の末尾に置く運用のため、上限は新規店舗より前の既存店舗の取得を優先的に消費する点に注意）
- 実行時間の目安: 1日あたり約40秒 + 20件ごとに5分休憩（リクエスト上限内で完了する分の所要時間）
- 403（`AccessForbiddenError`）が出ると全店舗の処理が中断される。Cloudflareのブロックが解除されるまで
  時間を置いてから同じコマンドを再実行する（未取得日は`get_processed_dates`により自動的に再試行対象になる）。
  連続3店舗が全対象日「ページにデータなし」だった場合もブロック疑いとして同様に中断される（層2サーキットブレーカー）
- 手動で同日中に何度も連続実行して早くバックフィルを終わらせるのは非推奨（集中リクエストでブロックを誘発しうる）

### 5. fase2側の反映（データ収集後に必須）

fase2は`ホールデータ/turso_replica.db`（fase1が実行終了時にsyncするレプリカ）を読むだけで、
分析成果物（`stage3_scores`/`store_profile`）は自動更新されない。新規店舗を機能A/Bに反映させるには
手動でバッチを実行する。

```bash
cd fase2
python run_store_profile.py --hole <店舗名（stores.jsonのスラッグと同じ）>
```

- 機種別デシルカーブ(bin_curves)やチャンネル重みも再学習したい場合は、全店舗分の交差検証を伴うため
  追加で`python multi_store.py`を実行する（対象は新店舗だけでなく全店舗）

## 完了確認

- `fase1/stores.json`に新スラッグが追加されている
- `ホールデータ/analysis.db`の`store_profile`テーブルに新店舗の行が存在する（`run_store_profile.py`実行後）
- 統合アプリ（`streamlit run fase2/app.py`）ホームページの店舗検索に新店舗が表示され、店舗トップページ(機能A/機能B)が開ける
