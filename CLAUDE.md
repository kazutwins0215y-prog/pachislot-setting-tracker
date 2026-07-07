# プロジェクト概要

パチスロホールのデータを収集・分析し、高設定が入る台を予測して、iPhoneから直接Webで確認できるシステム。
詳細は [要件定義.md](要件定義.md) を参照。

## フェーズとフォルダの対応

| 要件定義 | フォルダ | 状態 | 主要ファイル |
|---|---|---|---|
| 1. データ収集 | [`fase1/`](fase1/) | 実装済み（Turso対応・埋め込みレプリカ連携済み。GitHub Actions自動実行は403ブロックのため停止中） | `メイン.py` / `scraper.py` / `db.py` / `sync_replica.py` / `stores.json` |
| 2. 設定推測・パターン分析 | [`fase2/`](fase2/) | 実装済み | 下表参照 |
| 3. 配信・公開（iPhone Web閲覧） | `fase3/` | Stage A一部実装（DB移行済み・自動実行は停止中）・Stage B未着手 | Stage A: TursoDB移行は完了。GitHub Actionsでの自動実行はCloudflareにデータセンターIPをブロックされるため停止し、当面PC手動実行に戻した。Stage B: 機能A/Bもクラウドホスティングし iPhoneから直接アクセス（未着手） |
| 4. 日次自動実行 | [`fase4/`](fase4/) | 実装済み（タスクスケジューラ登録は運用者が任意のタイミングで実施） | `run_daily.py`（朝6:30ポーリング実行＋10:30追い実行。fase1収集→`evaluate_predictions.py`→`run_store_profile.py`を直列実行し、`ホールデータ/collection_log.csv`にサイト更新検知時刻を記録） |

> ※ fase1は2026-07にTurso(libSQL)対応・非対話化済み。`db.py`はsqlite3→Turso/libsqlクライアントに書き換え（**埋め込みレプリカ方式**: 書き込みはTursoへ委譲しつつ`ホールデータ/turso_replica.db`をローカルに維持し、fase2はこのレプリカを読む）、`メイン.py`は`input()`を廃止し`stores.json`+自動日付算出（前回取得済み最終日の翌日〜2日前。直近14日の取得失敗日は自動再試行。403発生時は全店舗中止）に変更。**GitHub Actionsでの自動実行(`schedule`)は、ana-slo.com(Cloudflare)がデータセンター系IPを即403ブロックすることが判明したため停止中**（`workflow_dispatch`の手動トリガーのみ残す）。当面はPC上で`py -3.12 メイン.py`を手動実行し、Tursoへ書き込む運用。リポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （非公開）。詳細は[`fase1/データ収集_skill.md`](fase1/データ収集_skill.md)参照

### fase2 ファイル構成

| ファイル | 状態 | 内容 |
|---|---|---|
| [`fase2/preprocess.py`](fase2/preprocess.py) | 実装済み | Stage0〜4-1: DB読み込み→正規化→Tier判定→logLR→統合スコア→判定保留（NumPyベクトル化済み） |
| [`fase2/patterns.py`](fase2/patterns.py) | 実装済み | 移動台検出・幅型パターン(S_全台系/新台/移動台)・深さ型パターン(S_鉄板台/ローテ/据え置き)・αブレンド。S_鉄板台は該当日のみ効果量ベースでスコア付与(検出条件は`teppan_conditions`へ保存)、αは固定0.3(旧ウォークフォワード学習は当日リークのため停止、Stage7で再設計予定) |
| [`fase2/score.py`](fase2/score.py) | 実装済み | S_稼働低さ・合成スコアΣ(wᵢ×Sᵢ)÷Σ(wᵢ)・店舗プロファイル管理。`write_prediction_log`は(ホール名, 予測種別, 使用データ最終日)単位の重複追記ガード付き(fase4のcatchup/リトライで同一予測が二重記録されるのを防ぐ) |
| [`fase2/app_a.py`](fase2/app_a.py) | 実装済み | 機能A: Streamlit 店内比較・可視化ツール（店舗分析/台番号/機種名の3ビュー）。`render()`で本体を公開し`app.py`から呼び出し可能 |
| [`fase2/app_b.py`](fase2/app_b.py) | 実装済み(再設計Phase1〜5実装済み・Phase6は将来対応) | 機能B: 振り返りダッシュボード + 狙い目メモ。`render_detail()`で詳細ダッシュボード本体を公開し`app.py`から呼び出し可能。個別店舗詳細に検知期間履歴・当月カレンダーヒートマップ(統合スコア日次平均+パターン別内訳)を追加済み。再設計の詳細は[`fase2/データ分析_skill.md`](fase2/データ分析_skill.md)、Phase6(将来項目)は[`fase2/今後の実装予定.md`](fase2/今後の実装予定.md)参照 |
| [`fase2/app_top.py`](fase2/app_top.py) | 実装済み(機能B再設計Phase4) | トップページ: 当日の店舗ランキング(符号付き合成スコア)・当日の熱い台・翌日予測ランキング(S_鉄板台、`prediction_log`の最新行+`prediction_accuracy`の的中率)。`render()`で本体を公開し`app.py`から呼び出し可能 |
| [`fase2/app.py`](fase2/app.py) | 実装済み | トップページ・機能A・機能Bを1つのWebページに統合するエントリポイント（サイドバーの選択メニューで切替）。`streamlit run app.py`で起動 |
| [`fase2/evaluate_predictions.py`](fase2/evaluate_predictions.py) | 実装済み(機能B再設計Stage7-2) | `prediction_log`とレプリカの実測差枚を突き合わせ、Spearman相関・Precision@N・リフトを`prediction_accuracy`へ集計。`python evaluate_predictions.py`で実行(翌日以降の実データがレプリカに入ってから意味を持つ) |
| [`fase2/multi_store.py`](fase2/multi_store.py) | 実装済み(2026-07再設計) | Stage1b+Stage5+Stage6: 機種別デシルカーブ(bin_curves)学習と**LOSO交差検証ゲート**(旧実装は循環学習だったため、直交化残差が翌観測日のRNG証拠を予測できた場合のみw3>0・γ_storeを保存。不合格時はw3=0=回転数チャンネル無効)・検証(Tier再現性/マクロ整合性)。`python multi_store.py`で全店舗一括実行 |
| [`fase2/data_source.py`](fase2/data_source.py) | 実装済み | fase2共通のデータ読み込み層。入力=Tursoレプリカ(`ホールデータ/turso_replica.db`、読み取り専用)・分析成果物の保存先=分析DB(`ホールデータ/analysis.db`)のパスと接続を集約 |
| [`fase2/run_store_profile.py`](fase2/run_store_profile.py) | 実装済み | 1店舗分のpreprocess→patterns→scoreパイプラインを通しで実行し、分析DBの`stage3_scores`(Stage3出力)と`store_profile`を更新するバッチ(`--hole <店舗名>`で特定店舗のみ)。`fase4/run_daily.py`が全店舗一括(引数なし)で毎日自動実行するほか、新規店舗取込時の即時反映等では引き続き手動実行も可能。**注意**: `--hole`指定なしの全店舗一括実行を怠ると特定店舗だけ更新が滞り機能Bの予測日付が他店舗より遅れる事例が発生済み(詳細は[`fase2/データ分析_skill.md`](fase2/データ分析_skill.md)「既知の運用リスク」参照) |
| [`fase2/scrape_machine_specs.py`](fase2/scrape_machine_specs.py) | 実装済み | chonborista.comから機種別設定差確率表を取得し`raw_specs_scraped.json`に保存 |
| [`fase2/assign_tier.py`](fase2/assign_tier.py) | 実装済み | `raw_specs_scraped.json`を正規化・Tier判定して`machine_setting_specs.json`を再構築 |

## 参照ルール

- 「要件定義1」「データ収集」「fase1」→ [`fase1/`](fase1/) を参照
  - 技術詳細: [`fase1/データ収集_skill.md`](fase1/データ収集_skill.md)
  - 構成図: [`fase1/データ収集_構成図.md`](fase1/データ収集_構成図.md)
- 「要件定義2」「分析」「fase2」→ [`fase2/`](fase2/) を参照
  - 技術詳細: [`fase2/データ分析_skill.md`](fase2/データ分析_skill.md)
  - 構成図: [`fase2/データ分析_構成図.md`](fase2/データ分析_構成図.md)
- 「要件定義3」「配信」「公開」「fase3」→ `fase3/` を参照（未実装。機能A/Bのコード自体はfase2に残し、fase3はそれをiPhoneへ届ける公開・デプロイ層を担う）
- 「要件定義4」「自動実行」「fase4」→ [`fase4/`](fase4/) を参照
  - 技術詳細: [`fase4/日次自動実行_設計.md`](fase4/日次自動実行_設計.md) / 実装指示書: [`fase4/実装指示書.md`](fase4/実装指示書.md)
  - 運用手順(タスクスケジューラ登録・トラブルシューティング): [`fase4/日次自動実行_skill.md`](fase4/日次自動実行_skill.md)

## データ保存先

- クラウドDB: Turso（libSQL/SQLite互換、Primary Location: Tokyo/nrt）。`fase1/db.py`が`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`環境変数で**埋め込みレプリカ接続**（GitHub Secretsに登録済み）
- Tursoレプリカ: `ホールデータ/turso_replica.db`（fase1が実行終了時のsyncで維持するSQLite互換ファイル。**fase2はTursoへ直接接続せずこれを読み取り専用参照する**。収集せず同期だけ行う場合は`py -3.12 fase1/sync_replica.py`）
- 分析DB: `ホールデータ/analysis.db`（fase2の成果物`stage3_scores`/`store_profile`。ローカル専用・`fase2/run_store_profile.py`で再生成可能）
- 旧ローカルSQLite: `ホールデータ/{ホール名スラッグ}.db`（Turso移行前の生データのアーカイブ。現在はどのコードからも参照されない。`.gitignore`でGit管理対象外。Turso移行時に`fase1/merge_stores_for_turso.py`で1ファイルに統合しUpload DB機能で移行済み）
- 対象サイト: `ana-slo.com`

## Claude Codeスキル

- 「新店舗を追加」「新しいホールを追加」→ [`.claude/skills/add-new-store/SKILL.md`](.claude/skills/add-new-store/SKILL.md)（stores.jsonへの登録・バックフィル日数指定・fase2側の反映手順）
- 「機能A/Bを起動」「アプリを起動」→ [`.claude/skills/launch-app/SKILL.md`](.claude/skills/launch-app/SKILL.md)（Streamlitアプリの起動コマンド）
