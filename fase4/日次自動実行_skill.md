# fase4: 日次自動実行 運用skill

fase1収集→fase2評価(`evaluate_predictions.py`)→fase2分析・予測(`run_store_profile.py`)を
毎朝無人で直列実行する`fase4/run_daily.py`の運用手順。設計・仕様は
[`日次自動実行_設計.md`](日次自動実行_設計.md)・[`実装指示書.md`](実装指示書.md)参照。

## 前提

- 実行PCは**自宅の住宅用IP**であること(GitHub Actions等データセンターIPはCloudflareに403ブロックされる)
- 夜間は**シャットダウンせずスリープ**にする(完全シャットダウンからのタスクスケジューラ自動起動は不可)
- 電源オプションで「スリープ解除タイマーの許可」を有効化しておくこと
  (コントロールパネル→電源オプション→プラン設定の変更→詳細な電源設定の変更→
  スリープ→ウェイクタイマーの許可→有効)

## タスクスケジューラ登録コマンド

PowerShellを**管理者権限**で開いて実行する(`schtasks /create`はスリープ解除オプションを
指定できないため`Register-ScheduledTask`を使う)。パスは実際のリポジトリ配置に合わせてある。

### 朝タスク(6:30起動・ポーリングあり)

```powershell
$repo = 'C:\Users\user\OneDrive\デスクトップ\データ'
$action = New-ScheduledTaskAction -Execute 'py' `
    -Argument "-3.12 `"$repo\fase4\run_daily.py`" --mode morning" `
    -WorkingDirectory "$repo\fase4"
$trigger = New-ScheduledTaskTrigger -Daily -At 6:30AM
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'PachislotDaily_Morning' `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description 'fase4: 朝のポーリング収集+評価+分析(6:30起動、8:15打ち切り)'
```

### 追いタスク(10:30起動・単発)

```powershell
$repo = 'C:\Users\user\OneDrive\デスクトップ\データ'
$action = New-ScheduledTaskAction -Execute 'py' `
    -Argument "-3.12 `"$repo\fase4\run_daily.py`" --mode catchup" `
    -WorkingDirectory "$repo\fase4"
$trigger = New-ScheduledTaskTrigger -Daily -At 10:30AM
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'PachislotDaily_Catchup' `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description 'fase4: 追い収集(単発)+評価+分析(10:30起動)'
```

### 解除コマンド

```powershell
Unregister-ScheduledTask -TaskName 'PachislotDaily_Morning' -Confirm:$false
Unregister-ScheduledTask -TaskName 'PachislotDaily_Catchup' -Confirm:$false
```

### 登録状態の確認

```powershell
Get-ScheduledTask -TaskName 'PachislotDaily_*' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'PachislotDaily_Morning'   # LastRunTime/LastTaskResult確認
Get-ScheduledTaskInfo -TaskName 'PachislotDaily_Catchup'
```

## 手動で回したい場合

```powershell
cd "C:\Users\user\OneDrive\デスクトップ\データ\fase4"
py -3.12 run_daily.py --mode catchup   # 収集1回→評価→分析。日中いつでも安全に手動実行できる
py -3.12 run_daily.py --mode morning   # ポーリングあり。起動時刻がPOLL_DEADLINE(8:15)を過ぎていれば収集1回だけ実行される
```

`--mode catchup`は`prediction_log`の重複追記ガード([`fase2/score.py`](../fase2/score.py)の
`write_prediction_log`)により、データが進んでいなければ予測は追記されず安全に何度でも
再実行できる。

## 日々の確認ポイント

- **ログ**: `fase4/logs/run_daily_YYYYMMDD.log`(コンソール出力と同内容)。末尾の
  「実行サマリ」ブロックで以下を確認:
  - `mode` / `ポーリング回数` / `403検知` / `所要時間`
  - 店舗ごとの「最終データ日」「昨日分あり(○/×)」
- **collection_log.csv**: `ホールデータ/collection_log.csv`。列は
  `対象日, ホール名, 検知日時, ポーリング回数, mode`。
  「検知日時」はサイト更新時刻の上界(ポーリング間隔=15分の分解能)。
  **このCSVはappend-onlyで消さない・上書きしない**(運用初期の主目的である
  「サイト更新が実際に何時か」を知るための観測データのため)。

## トラブルシューティング

| 症状 | 対応 |
|---|---|
| ログに「403ブロックを検知しました(exit 43)」、run_daily自体もexit 1 | その日はana-slo.comにブロックされている。**その日の追加実行はしない**(連打すると解除が遅れる可能性)。翌日以降のタスク実行で自然に回復するのを待つ。頻発する場合はIP・アクセス頻度を見直す |
| PCがオフ/スリープ解除失敗でタスクが起動しなかった | `-StartWhenAvailable`により、PCが起きたタイミングで自動的に追いつき実行される。電源オプションの「ウェイクタイマーの許可」が有効か確認 |
| 朝タスクが8:15までに全店舗の昨日分を検知できなかった | 正常。ポーリングはPOLL_DEADLINEで打ち切られ評価・分析は実行される。取れなかった店舗は追いタスク(10:30)で回収される想定。機能Bの「使用データ最終日」表示で古いことが分かる |
| `run_store_profile.py`は走るが予測追記が0件 | 正常(重複ガード)。データが進んでいない店舗は前回と同じ`(ホール名,予測種別,使用データ最終日)`のため自動スキップされる |
| `evaluate_predictions.py`が失敗する | ERRORログを残しつつ`run_store_profile.py`は実行される(答え合わせの失敗で予測追記を止めない設計)。翌日以降のデータが揃ってから再実行されれば解消することが多い |

## 運用初期の観測タスク(1ヶ月経過後に実施)

`collection_log.csv`が1ヶ月分程度たまったら、`検知日時`の時刻分布を確認する:

```powershell
# 簡易確認例: 検知日時の「時」だけ集計
Import-Csv "ホールデータ\collection_log.csv" |
    ForEach-Object { ([datetime]$_.検知日時).Hour } |
    Group-Object | Sort-Object Name
```

実際のサイト更新時刻の分布を見て、`fase4/run_daily.py`冒頭の
`POLL_START` / `POLL_DEADLINE` / `POLL_INTERVAL_MIN`を実態に合わせて調整する
(例: 実際は7:00までにほぼ更新されているなら窓を6:45〜7:30に短縮、間隔を30分に緩和 等)。
定数を変更したらタスクスケジューラの再登録は不要(スクリプト内の値を読むだけのため)。
