---
name: launch-app
description: Use when the user asks to start, launch, run, or open 機能A / 機能B / the Streamlit app ("機能Aを起動", "機能Bを開いて", "アプリを起動して", "ダッシュボードを見たい" など). Covers the command to launch the integrated Streamlit entry point and how to launch 機能A/機能B individually.
---

# 機能A/B起動 skill

fase2の可視化ツール（機能A: 店内比較、機能B: 振り返りダッシュボード）をローカルでStreamlit起動する手順。

前提: [`CLAUDE.md`](../../../CLAUDE.md)のfase2構成、[`fase2/データ分析_skill.md`](../../../fase2/データ分析_skill.md)を参照。

## 起動コマンド

### 機能A・機能B統合（通常はこちらを使う）

```bash
streamlit run fase2/app.py
```

サイドバーのラジオボタンで「機能A: 店内比較」⇔「機能B-詳細: 振り返りダッシュボード」を切り替えられる
（[`fase2/app.py`](../../../fase2/app.py)）。ブラウザが自動で開かない場合はターミナルに表示される
`http://localhost:8501` を開く。停止は `Ctrl+C`。

### 機能単体で起動したい場合

```bash
streamlit run fase2/app_a.py   # 機能A: 店内比較・可視化（店舗分析/台番号/機種名の3ビュー）
streamlit run fase2/app_b.py   # 機能B: 振り返りダッシュボード + 狙い目メモ
```

## 起動前に確認すること

- 表示データは`ホールデータ/analysis.db`（`stage3_scores`/`store_profile`）を読む。
  データ収集後に分析DBが更新されていない場合は古い結果が表示されるため、
  最新化したい場合は先に`fase2/run_store_profile.py`を実行する
  （[`add-new-store`](../add-new-store/SKILL.md) skill参照）。
- `streamlit`未インストールの場合は`pip install streamlit`が必要（fase1の`requirements.txt`には含まれない）。
