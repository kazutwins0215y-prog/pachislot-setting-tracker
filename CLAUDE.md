# プロジェクト概要

パチスロホールのデータを収集・分析し、高設定が入る台を予測して、iPhoneから直接Webで確認できるシステム。
詳細は [要件定義.md](要件定義.md) を参照。

## フェーズとフォルダの対応

| 要件定義 | フォルダ | 状態 | 概要 |
|---|---|---|---|
| 1. データ収集 | [`fase1/`](fase1/) | 実装済み | ana-slo.comから日次収集しTursoへ書き込み（`メイン.py`/`scraper.py`/`db.py`/`stores.json`）。詳細→[`fase1/データ収集_skill.md`](fase1/データ収集_skill.md) |
| 2. 設定推測・パターン分析 | [`fase2/`](fase2/) | 実装済み | 下表参照 |
| 3. 配信・公開 | [`fase3/`](fase3/) | Stage A/B完了・デプロイ済み(2026-07-07) | 分析用Turso(`pachislot-analysis`)へ差分upsert（`upload_analysis.py`）し、Streamlit Community Cloudで公開。詳細→[`fase3/配信公開_skill.md`](fase3/配信公開_skill.md) |
| 4. 日次自動実行 | [`fase4/`](fase4/) | 実装済み・タスクスケジューラ登録済み | `run_daily.py`が朝6:30ポーリング＋10:30追い実行で「fase1収集→機種スペック再取得(5日おき)→評価→分析→アップロード」を直列実行。詳細→[`fase4/日次自動実行_skill.md`](fase4/日次自動実行_skill.md) |

> ※ fase1はTurso**埋め込みレプリカ方式**（書き込みはTursoへ、fase2はローカルレプリカ`ホールデータ/turso_replica.db`を読む）。取得トランスポートは**requests方式**（`fase1/scraper.py`の`create_session`/`fetch_page`。2026-07-16〜17にブラウザ自動化(SeleniumBase UC Mode)への切替を試みたが、2026-07-22の実測でブロック真因が「IP/Norton」ではなく「ブラウザ自動化ツール自体の指紋」だったと判明（同一PC・同一IPでrequestsは200取得成功、ブラウザ自動化は403空ボディ）したためrequests方式へ復帰・断念）。GitHub Actionsでのクラウド実行はana-slo.com(Cloudflare)がActionsのデータセンターIPを空ボディ403でブロックするため不可（IP/ASレベルのブロックのため取得方式に関わらず不可）。**fase4のタスクスケジューラがPC上（住宅IP）で毎日実行する運用**。SSL検証は`truststore`必須（NortonのHTTPSスキャン対策）。リポジトリ: https://github.com/kazutwins0215y-prog/pachislot-setting-tracker （**公開**。2026-07-17にコミット履歴のメールをnoreplyへ書き換え・シークレット非含有を確認のうえpublic化。クラウド実行はしないが公開設定は将来のために維持）。経緯・詳細→[`fase1/データ収集_skill.md`](fase1/データ収集_skill.md)

### fase2 ファイル構成

分析パイプラインの技術詳細はハブ[`fase2/データ分析_skill.md`](fase2/データ分析_skill.md)→`データ分析_詳細_*.md`を参照。
各ファイルの実装経緯・変更履歴の原文は[`fase2/データ分析_詳細_実装履歴.md`](fase2/データ分析_詳細_実装履歴.md)に保存済み。

| ファイル | 役割 | 詳細 |
|---|---|---|
| [`preprocess.py`](fase2/preprocess.py) | Stage0〜4-1: 正規化→Tier判定→logLR→統合スコア(high_prob)→判定保留 | [詳細_preprocess](fase2/データ分析_詳細_preprocess.md) |
| [`patterns.py`](fase2/patterns.py) | パターン検出層の窓口(facade)。実体は`patterns_common/events/breadth/groups/depth/transition.py`の6モジュール(2026-07-19分割・`import patterns as pt`は不変): 幅型(全台系/新台増台/移動台)・深さ型(鉄板台/ローテ/据え置き)・機種判定・末尾版・機種版・導入後カーブ・遷移モデル・αブレンド | [詳細_patterns](fase2/データ分析_詳細_patterns.md) |
| [`score.py`](fase2/score.py) | サブスコア統合Σ(wᵢ×Sᵢ)÷Σ(wᵢ)・S_稼働低さ・店舗プロファイル・各種ログ/検定結果の保存（予測ログはappend-only+重複ガード） | [詳細_score](fase2/データ分析_詳細_score.md) / [詳細_データモデル](fase2/データ分析_詳細_データモデル.md) |
| [`app.py`](fase2/app.py) | 統合エントリポイント（`streamlit run app.py`）。ホーム⇔店舗トップをsession_stateで切替、iPhone向けカードUI | [詳細_出力3系統](fase2/データ分析_詳細_出力3系統.md) |
| [`app_top.py`](fase2/app_top.py) | トップページ: おすすめ店舗ランキング＋熱い台予測7タブ（鮮度フィルタ・新台/増台タブ分離）＋ホーム最上部の鮮度警告バナー（収集全体停止/店舗別分析遅延を検知） | 同上 |
| [`app_a.py`](fase2/app_a.py) | 機能A: 店内比較（店舗分析/台番号/機種名の3ビュー）。`render(hole_name)`をapp.pyから呼ぶ | 同上 |
| [`app_b.py`](fase2/app_b.py) | 機能B: 店舗特徴（癖の有効性マトリクス・検知期間履歴・カレンダーヒートマップ）＋おすすめ店舗スコア`compute_store_recommend_score` | 同上 |
| [`ui_theme.py`](fase2/ui_theme.py) | 表示層の共通スタイル（黒基調ダークテーマ・Plotly調整・プラス=青/マイナス=赤）。機能ロジックには関与しない | 同上 |
| [`run_store_profile.py`](fase2/run_store_profile.py) | preprocess→patterns→scoreの通し実行バッチ（`--hole`で1店舗/引数なしで全店舗。fase4が毎日全店舗実行）。**注意: 全店舗一括を怠ると店舗間で予測日付がズレる事例あり** | [詳細_実装履歴](fase2/データ分析_詳細_実装履歴.md) |
| [`evaluate_predictions.py`](fase2/evaluate_predictions.py) | `prediction_log`と実測差枚を突合し`prediction_accuracy`を更新（Spearman・Precision@N・リフト=差枚差ベース） | [詳細_データモデル](fase2/データ分析_詳細_データモデル.md) |
| [`multi_store.py`](fase2/multi_store.py) | Stage1b/5/6: bin_curves学習＋LOSO交差検証ゲート（不合格時はw3=0）・検証 | [詳細_preprocess](fase2/データ分析_詳細_preprocess.md) |
| [`data_source.py`](fase2/data_source.py) | レプリカ(読み取り専用)・分析DBのパスと接続の共通層 | — |
| [`scrape_machine_specs.py`](fase2/scrape_machine_specs.py) / [`assign_tier.py`](fase2/assign_tier.py) | 機種スペック表の取得→正規化・Tier判定（`machine_setting_specs.json`再構築）。**2026-07-20にfase4日次実行へ自動組込**（初出90日以内のみ再取得・以後`frozen`固定） | [詳細_preprocess](fase2/データ分析_詳細_preprocess.md) |
| [`migrate_specs_freeze.py`](fase2/migrate_specs_freeze.py) | 上記自動化の導入時に一度だけ実行する移行スクリプト（既存`raw_specs_scraped.json`へ`frozen`一括付与） | 同上 |
| [`ground_truth_entry.py`](fase2/ground_truth_entry.py) | 正解発表のローカル専用入力フォーム（`ホールデータ/ground_truth.db`へappend-only。app.pyには統合しない） | [今後の実装予定](fase2/今後の実装予定.md)3節 |

## 開発ルール

- **新アルゴリズム（軸・検出器・モデル）の投入時は有効性検証ゲート必須**（2026-07-14ユーザー指示で恒久化）: ①`prediction_log`への並走記録から始める（合成・表示に入れない）→②`evaluate_predictions.py`で実測採点→③ユーザーの実地知識・ground truthと照合→④合格後にのみ合成参加・表示へ昇格。手順の詳細は[`fase2/今後の実装予定.md`](fase2/今後の実装予定.md)0節参照
- **ドキュメント肥大防止**（2026-07-14ユーザー指示で恒久化）: CLAUDE.mdは「役割1〜2文＋参照先」の地図に保ち、実装詳細・変更履歴は各skill/詳細ファイルへ書く。skillはハブ＋詳細ファイル方式（`データ分析_skill.md`の構成）を踏襲し、新しい節は冒頭にサマリーを置く。ドキュメントの情報を移動するときは「先に転記→検証→後で削除」の順を厳守（情報消失防止）

## 参照ルール

- 「要件定義1」「データ収集」「fase1」→ [`fase1/`](fase1/) を参照
  - 技術詳細: [`fase1/データ収集_skill.md`](fase1/データ収集_skill.md)
  - 構成図: [`fase1/データ収集_構成図.md`](fase1/データ収集_構成図.md)
- 「要件定義2」「分析」「fase2」→ [`fase2/`](fase2/) を参照
  - 技術詳細: ハブ[`fase2/データ分析_skill.md`](fase2/データ分析_skill.md)（目次と横断注意点。各節の一次情報は`fase2/データ分析_詳細_*.md`の5ファイル: preprocess / patterns / score / 出力3系統 / データモデル）
  - 構成図: [`fase2/データ分析_構成図.md`](fase2/データ分析_構成図.md)
  - 将来項目・優先キュー: [`fase2/今後の実装予定.md`](fase2/今後の実装予定.md)
- 「要件定義3」「配信」「公開」「fase3」→ [`fase3/`](fase3/) を参照（機能A/Bのコード自体はfase2に残し、fase3はそれをiPhoneへ届ける公開・デプロイ層を担う）
  - 技術詳細: [`fase3/配信公開_設計.md`](fase3/配信公開_設計.md)
  - 運用手順(デプロイ・Secrets・`--full`実行・トラブルシューティング): [`fase3/配信公開_skill.md`](fase3/配信公開_skill.md)
- 「要件定義4」「自動実行」「fase4」→ [`fase4/`](fase4/) を参照
  - 技術詳細: [`fase4/日次自動実行_設計.md`](fase4/日次自動実行_設計.md)
  - 運用手順(タスクスケジューラ登録・トラブルシューティング): [`fase4/日次自動実行_skill.md`](fase4/日次自動実行_skill.md)

## データ保存先

- クラウドDB: Turso（libSQL/SQLite互換、Primary Location: Tokyo/nrt）。`fase1/db.py`が`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`環境変数で**埋め込みレプリカ接続**（GitHub Secretsに登録済み）
- Tursoレプリカ: `ホールデータ/turso_replica.db`（fase1が実行終了時のsyncで維持するSQLite互換ファイル。**fase2はTursoへ直接接続せずこれを読み取り専用参照する**。収集せず同期だけ行う場合は`py -3.12 fase1/sync_replica.py`）
- 分析DB: `ホールデータ/analysis.db`（fase2の成果物`stage3_scores`/`store_profile`。ローカル専用・`fase2/run_store_profile.py`で再生成可能）
- クラウドDB(分析用): Turso、DB名`pachislot-analysis`（生データDBと物理分離・同じorganization・同じグループ`analytics`/`aws-ap-northeast-1`に作成済み）。`fase3/analysis_turso.py`が`TURSO_ANALYSIS_DATABASE_URL`/`TURSO_ANALYSIS_AUTH_TOKEN`環境変数で**埋め込みレプリカ接続**し、`fase3/upload_analysis.py`が`analysis.db`の6テーブルを差分upsertする
- 分析用Tursoレプリカ: `ホールデータ/turso_analysis_replica.db`（`upload_analysis.py`が維持する埋め込みレプリカ。ウォーターマーク読み取り専用・消えても次回syncで再生成される）
- 旧ローカルSQLite: `ホールデータ/{ホール名スラッグ}.db`（Turso移行前の生データのアーカイブ。現在はどのコードからも参照されない。`.gitignore`でGit管理対象外。Turso移行時に`fase1/merge_stores_for_turso.py`で1ファイルに統合しUpload DB機能で移行済み）
- 正解発表DB: `ホールデータ/ground_truth.db`（店舗の設定発表を`fase2/ground_truth_entry.py`で手入力する唯一の原本。再生成不可能なためanalysis.dbとは分離。バックアップはOneDrive自動同期）
- 対象サイト: `ana-slo.com`

## テスト

`tests/`に単体テストを設置（自作フィクスチャ）。`py -3.12 -m pytest tests/`で手動実行に加え、`.github/workflows/tests.yml`でpush/PRごとにGitHub Actions(windows-latest)が自動実行する。フェーズ1(fase1純ロジック: `compute_remaining_days`/`build_url`/`extract_slug`/`_parse_row`/`is_block_page`/サーキットブレーカー関連等・37件PASS)に加え、2026-07-20の機種スペック自動取得(`migrate_specs_freeze.py`/`scrape_machine_specs.py`の凍結判定/`run_daily.py`の5日間隔判定)29件、リクエスト削減(案A負キャッシュ・案B簡易版catchup_only_stores)とspecs_refresh_state書き込み失敗の再現テスト16件で計82件PASSだったところに、2026-07-21にfase2中核(`preprocess.py`のlogLR3チャンネル・Tier判定・欠損偏りガード、`patterns_common.py`ほかpatterns_*モジュールの統計ユーティリティ、`score.py`の合成スコア・稼働率・上限キャリブレーション等の純関数)の回帰テスト107件(パートB前半・B-0〜B-3)を追加し計189件PASSだったところに、さらに同日中にパートB後半(B-4/B-5)としてpatterns検出器の数値ロジック(幅型のS_全台系・機種強さ軸z/Fisher判定、深さ型のS_据え置き・S_ローテ・S_鉄板台、遷移モデル、周期探索ACF/PDM、末尾版/機種版カレンダー検定、移動/導入イベント検出)と、score.pyのDB書き込み関数(一時sqlite。`write_group_calendar_conditions`のグループ種別別削除回帰・`write_prediction_log`の重複追記ガード回帰を含む)の回帰テスト82件を追加し計268件PASSだったところに、2026-07-22のfase1取得トランスポート復帰(SeleniumBase→requests)で`--max-requests`CLI仕様(`resolve_max_requests`純関数・未指定=既定100/0=無制限)とfase1版`_http_error_action`(403即abort・429/503/504のリトライ/abort/raise分岐)の仕様テスト16件を追加し計284件PASS。CI依存に`numpy`/`pandas`/`scipy`を追加（`streamlit`/`plotly`は追加せず、`app*.py`はテストで一切importしない方針を維持）。バグ修正時・新機能追加時は再現テスト・仕様テスト先行が運用ルール。

## 開発ハーネス（3エージェント）

中規模以上のタスク（新機能・アルゴリズム追加・リファクタ）は、[`planner`](.claude/agents/planner.md)(Opus・計画書作成)→ユーザー承認→[`generator`](.claude/agents/generator.md)(Sonnet・実装)→[`evaluator`](.claude/agents/evaluator.md)(Sonnet・厳格採点、不合格ならgeneratorへ差し戻し最大2回)の順で進める。計画書＝スプリント契約は`.claude/plans/YYYY-MM-DD_タスク名.md`（Git管理外）。1行修正・質問・小タスクはハーネスを使わず直接対応する。

## Claude Codeスキル

- 「新店舗を追加」「新しいホールを追加」→ [`.claude/skills/add-new-store/SKILL.md`](.claude/skills/add-new-store/SKILL.md)（stores.jsonへの登録・バックフィル日数指定・fase2側の反映手順）
- 「機能A/Bを起動」「アプリを起動」→ [`.claude/skills/launch-app/SKILL.md`](.claude/skills/launch-app/SKILL.md)（Streamlitアプリの起動コマンド）
