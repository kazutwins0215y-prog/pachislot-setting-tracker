# fase3: 配信・公開 運用skill

Streamlit Community Cloudへのデプロイ手順・Secrets設定・アルゴリズム変更時の
`--full`再構築手順・無料枠の監視ポイント・トラブルシューティングをまとめる。
設計は[`配信公開_設計.md`](配信公開_設計.md)、実装タスクは[`実装指示書.md`](実装指示書.md)参照。

## 分析用Turso DBの新規作成(初回のみ)

```
turso db create pachislot-analysis --location nrt   # 既存DBと同じTokyo/nrtに作成
turso db show pachislot-analysis --url               # → TURSO_ANALYSIS_DATABASE_URL
turso db tokens create pachislot-analysis             # → TURSO_ANALYSIS_AUTH_TOKEN
```

発行した2つの値を`.env`に追記する(**Git管理外のまま**。既存の`.gitignore`で除外済み):

```
TURSO_ANALYSIS_DATABASE_URL=libsql://pachislot-analysis-....turso.io
TURSO_ANALYSIS_AUTH_TOKEN=...
```

確認: `turso db list`で`pachislot-analysis`が見えること。

## 日次運用(手動介入不要)

`fase4/run_daily.py`が収集→評価→分析の最後に`fase3/upload_analysis.py`(差分モード)を
自動実行する。失敗してもrun_daily自体は継続し、翌日の差分実行が自動的に追いつく
(ウォーターマーク方式のため。詳細は[`配信公開_設計.md`](配信公開_設計.md)参照)。

手動で1回だけ差分アップロードしたい場合:

```powershell
cd "C:\Users\user\OneDrive\デスクトップ\データ\fase3"
py -3.12 upload_analysis.py
```

## アルゴリズム変更時の`--full`実行手順

Stage3以降のロジックを変更し過去日を再計算した場合、分析用Turso側にも
過去日分の再計算結果を反映する必要がある。手順:

1. `fase2/run_store_profile.py`(全店舗、引数なし)を実行しローカル`analysis.db`を最新化
2. `py -3.12 fase3/upload_analysis.py --full`を実行(6テーブルをDROP→全件INSERT。
   約80万行/回・数分かかる)
3. 完了後のprint出力でテーブルごとの転送行数を確認

**注意**: `--full`は書き込み枠を消費するため、検証目的でも1〜2回に留める
(通常運用は差分モードのみで十分)。生データDB(`slot_data`/`missing_data`)には
接続先が異なるため構造的に触れられない。

## Streamlit Community Cloudへのデプロイ

1. https://share.streamlit.io にGitHubアカウントでログインし、
   `pachislot-setting-tracker`リポジトリを接続
2. **Main file path**: `fase3/streamlit_app.py`
3. **Advanced settings → Python version**: `3.12`を指定
   (libsqlのLinux向けwheelが3.12で解決できることを確認済み。ローカルWindowsの
   3.14ビルド問題はクラウドでは発生しない)
4. **Secrets**に以下4つを登録(TOMLフォーマット):
   ```toml
   TURSO_DATABASE_URL = "libsql://holedata-kurage.aws-ap-northeast-1.turso.io"
   TURSO_AUTH_TOKEN = "...(生データDBのトークン)..."
   TURSO_ANALYSIS_DATABASE_URL = "libsql://pachislot-analysis-....turso.io"
   TURSO_ANALYSIS_AUTH_TOKEN = "...(分析DBのトークン)..."
   ```
5. アプリを**Private**に設定し、Settings → Sharingで自分のGmailをviewerとして招待
6. デプロイ後の確認:
   - コールドスタートが完走するか(初回は生データ+分析で約200MBのsyncが走り
     数十秒〜1分程度かかる)
   - メモリ1GB以内に収まっているか(Streamlit Cloudのアプリ管理画面で確認可)
   - iPhoneのSafariでGoogleログイン→トップページ・機能A・機能B-詳細の3画面が
     表示・操作できるか

### うまくいかない場合の代替(実装前にユーザーへ相談すること)

- **libsqlがインストール不可**: bootstrapのsyncをlibsql-client(HTTP経由)でのDB取得に差し替える
- **メモリ不足**: Hugging Face Spaces(無料CPU、16GBストレージ)へ切り替える

## 無料枠の監視ポイント

| サービス | 確認場所 | 目安 |
|---|---|---|
| Turso書き込み行数 | Tursoダッシュボード → Usage(organization全体の集計) | 通常運用で約76万行/月(無料枠1,000万の約8%)。`--full`実行時は+約80万行/回 |
| Tursoストレージ | 同上 | 生データDB+分析DB(+約80MB)の合計。5GB超過でDeveloperプラン($4.99/月〜)検討 |
| Streamlit Cloudメモリ | アプリ管理画面のリソース使用状況 | 無料枠は約1GB。超過が続く場合はHugging Face Spaces等へ移行検討 |

店舗数が増えるとstage3_scoresの行数がほぼ比例して増える(新店舗追加時は
[`.claude/skills/add-new-store/SKILL.md`](../.claude/skills/add-new-store/SKILL.md)の
作業と合わせて上記の書き込み行数見積りを再確認する)。

## トラブルシューティング

| 症状 | 対応 |
|---|---|
| コールドスタートが数十秒〜1分かかる | 無料枠の仕様(無操作時スリープからの復帰)。個人利用・1日数回の閲覧では許容範囲。頻繁に使う時間帯があるなら気にしなくてよい |
| `upload_analysis.py`が失敗する(ネットワーク等) | `run_daily.py`はERRORログのみで継続する。翌日の差分実行がウォーターマーク差分で自動的に追いつくため、当日中の手動リトライは必須ではない。急ぎの場合は`py -3.12 fase3/upload_analysis.py`を手動再実行 |
| クラウド側の表示が古い | サイドバー等に手動更新ボタンは無いため、`st.cache_resource(ttl=3600)`のTTL(60分)経過を待つか、Streamlit Cloud管理画面から「Reboot app」でキャッシュをクリアする |
| viewer招待したのにログインできない | Streamlit CloudのSettings → SharingでGmailアドレスのスペルを確認。招待メールのリンクから初回アクセスが必要な場合がある |
| デプロイ後に`ModuleNotFoundError` | リポジトリルートの`requirements.txt`に該当パッケージが無い可能性。fase2/fase3の実際のimportと突き合わせて追記する |
| ホーム⇔店舗ページの遷移やapp_a/app_bのウィジェット操作が効かない | `fase3/streamlit_app.py`は`app`モジュールを`importlib.reload`で毎リラン再実行する実装になっている(`import app`だけだと2回目以降のリランでfase2/app.pyのトップレベルコードが再実行されずUIが固まる)。このreload処理が入っているか確認する |
