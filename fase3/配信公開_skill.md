# fase3: 配信・公開 運用skill

Streamlit Community Cloudへのデプロイ手順・Secrets設定・アルゴリズム変更時の
`--full`再構築手順・無料枠の監視ポイント・トラブルシューティングをまとめる。
設計は[`配信公開_設計.md`](配信公開_設計.md)、実装タスクは[`実装指示書.md`](実装指示書.md)参照。

## 分析用Turso DBの新規作成(初回のみ・実施済み)

### Turso CLIのインストール(Windows)

公式手順はWSL経由(`docs.turso.tech/cli/installation`)。当PCはWSL(Ubuntu)が
導入済みだったため以下で完了した:

```powershell
wsl
```
```bash
curl -sSfL https://get.tur.so/install.sh | bash
turso auth login --headless   # WSL内はブラウザが開けないため--headless必須。
                               # 表示されたURLをWindows側のブラウザで開いてログイン
```

**注意**: インストーラが`~/.bashrc`末尾にPATH追加行を書くが、`bash -lc`のような
非対話シェルでは`.bashrc`が読まれないため`turso`コマンドが見つからないことがある。
非対話実行(スクリプト経由等)ではフルパス`~/.turso/turso`を直接呼ぶか、
対話シェルで`source ~/.bashrc`してから使う。

### DB作成

既存の生データDBが所属する**グループ**(`turso group list`で確認できる。ロケーションは
グループ単位で決まる)に新規DBを追加するのが簡単(物理的なDB自体は別物なので
分離の目的は満たされる):

```bash
turso group list                              # 既存グループ名とロケーションを確認(例: analytics, aws-ap-northeast-1)
turso db create pachislot-analysis --group analytics
turso db show pachislot-analysis --url        # → TURSO_ANALYSIS_DATABASE_URL
turso db tokens create pachislot-analysis     # → TURSO_ANALYSIS_AUTH_TOKEN
```

発行した2つの値を`.env`に追記する(**Git管理外のまま**。既存の`.gitignore`で除外済み)。
**トークンはチャット等に貼らず、`>> .env`へのリダイレクトで直接書き込むこと**
(値を画面に表示しない)。

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

## Streamlit Community Cloudへのデプロイ(2026-07-07実施・デプロイ済み)

1. https://share.streamlit.io にGitHubアカウントでログインする。初回は
   「Streamlit」というGitHub Appの認可(Authorize)を求められる
2. 「Create app」→ リポジトリ入力欄の**「Paste GitHub URL」**を使うのが簡単。
   ただしこの欄は**リポジトリURLではなく対象.pyファイルへの直リンク(blob URL)**
   を要求する仕様のため、以下の形式で貼る(Repository/Branch/Main file pathの
   3項目が自動で埋まる):
   ```
   https://github.com/kazutwins0215y-prog/pachislot-setting-tracker/blob/master/fase3/streamlit_app.py
   ```
3. **非公開リポジトリへのアクセス権限**: 上記URLを貼ってもリポジトリが認識されない
   場合、Repository欄付近の「Missing repos? / Manage access」からGitHub側の
   「Install & Authorize Streamlit」画面に飛び、対象リポジトリ(または
   All repositories)へのアクセスを許可する
4. **Advanced settings → Python version**: `3.12`を明示的に指定する。
   **未指定だとPython 3.14がデフォルトで選ばれる**ことを実機で確認済み
   (libsqlのLinux向けwheelは3.12前提のため、3.14のままだと想定外の挙動の
   リスクがある)
   **テーマ(2026-07モバイルUI改修で追加・同月に黒基調ダークテーマへ更新)**: リポジトリルートの
   `.streamlit/config.toml`がStreamlit Community Cloud側のテーマ(`base="dark"`・
   `primaryColor="#D6FE3E"`等)に効く(Cloud実行時はcwdがリポジトリルートのため)。
   ローカル実行用の`fase2/.streamlit/config.toml`と内容を同一に保つこと(相互コピー同期。
   詳細は[`fase2/データ分析_skill.md`](../fase2/データ分析_skill.md)「モバイルファーストUI」参照)。
   デプロイ時に別途設定する項目はない(リポジトリに含まれるファイルがそのまま反映される。
   push後はCloud側の再起動を待って実機で黒基調反映を確認すること)
5. **Secrets**に以下4つを登録(TOMLフォーマット):
   ```toml
   TURSO_DATABASE_URL = "libsql://holedata-kurage.aws-ap-northeast-1.turso.io"
   TURSO_AUTH_TOKEN = "...(生データDBのトークン)..."
   TURSO_ANALYSIS_DATABASE_URL = "libsql://pachislot-analysis-....turso.io"
   TURSO_ANALYSIS_AUTH_TOKEN = "...(分析DBのトークン)..."
   ```
   **Secrets未設定のままデプロイすると`KeyError: 'TURSO_DATABASE_URL'`で起動失敗する**
   (下記トラブルシューティング参照)。Deploy実行前に設定するのが望ましいが、
   後から設定してもSecrets保存時に自動でreboot(再起動)がかかるため事後設定でも問題ない
6. アプリを**Private**に設定し、Settings → Sharingで自分のGmailをviewerとして招待
7. デプロイ後の確認:
   - コールドスタートが完走するか(初回は生データ+分析で約200MBのsyncが走り
     数十秒〜1分程度かかる)
   - メモリ1GB以内に収まっているか(Streamlit Cloudのアプリ管理画面で確認可)
   - iPhoneのSafariでGoogleログイン→ホームページ⇔店舗トップページの遷移や
     機能A/Bのウィジェット操作が2回目以降も効くか(`importlib.reload`対応が
     効いているかの実地確認)

### うまくいかない場合の代替(実装前にユーザーへ相談すること)

- **libsqlがインストール不可**: bootstrapのsyncをlibsql-client(HTTP経由)でのDB取得に差し替える
- **メモリ不足**: Hugging Face Spaces(無料CPU、16GBストレージ)へ切り替える

## 日常的な利用(デプロイ後)

- iPhoneのSafariでアプリURL(`https://xxxxx.streamlit.app`)を開くだけでよい。
  ホーム画面に追加しておくと起動が早い
- 自分でStreamlitやPCを起動する操作は不要(アプリはクラウド上で独立して稼働)
- しばらくアクセスが無いとスリープする。「Zzz... This app has gone to sleep」画面が
  出たら「Yes, get this app back up!」をクリックし数十秒〜1分待つ
- 表示されるデータの鮮度は**PC側の`fase4/run_daily.py`が毎日実行されていること**に
  依存する(タスクスケジューラ登録状況は[`fase4/日次自動実行_skill.md`](../fase4/日次自動実行_skill.md)参照)

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
| `KeyError: 'TURSO_DATABASE_URL'`(bootstrap.pyのfase1_db.get_connection()内で発生) | Secretsが未設定/未保存のまま起動している。Manage app → Settings → Secretsで4つのキーを設定して保存する(保存時に自動reboot) |
| トレースバックのパスに`python3.14`が見える | Advanced settings → Python versionが未指定または反映されておらず3.14がデフォルトになっている。明示的に3.12を選択して保存する |
| 「Paste GitHub URL」に貼ってもリポジトリが認識されない | その欄はリポジトリURLではなく対象.pyファイルへのblob URL(`.../blob/master/fase3/streamlit_app.py`)を要求する仕様。非公開リポジトリの場合はGitHub側でStreamlitアプリへのアクセス許可(Manage access)も必要 |
| ホーム⇔店舗ページの遷移やapp_a/app_bのウィジェット操作が効かない | `fase3/streamlit_app.py`は`app`モジュールを`importlib.reload`で毎リラン再実行する実装になっている(`import app`だけだと2回目以降のリランでfase2/app.pyのトップレベルコードが再実行されずUIが固まる)。このreload処理が入っているか確認する |
