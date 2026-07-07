---
name: launch-app
description: Use when the user asks to start, launch, run, or open 機能A / 機能B / the Streamlit app ("機能Aを起動", "機能Bを開いて", "アプリを起動して", "ダッシュボードを見たい" など). Covers the command to launch the integrated Streamlit entry point (home page + per-store pages).
---

# 機能A/B起動 skill

fase2の可視化ツール（機能A: 店内比較、機能B: 振り返りダッシュボード）をローカルでStreamlit起動する手順。

前提: [`CLAUDE.md`](../../../CLAUDE.md)のfase2構成、[`fase2/データ分析_skill.md`](../../../fase2/データ分析_skill.md)を参照。

## 起動コマンド

```bash
streamlit run fase2/app.py
```

起動直後はホームページ（店舗検索＋当日・翌日ランキング＋機能B店舗横断比較）が表示され、
店舗検索の店舗ボタンから店舗トップページ（機能A店内比較＋機能B個別店舗詳細）へ遷移できる
（[`fase2/app.py`](../../../fase2/app.py)）。ブラウザが自動で開かない場合はターミナルに表示される
`http://localhost:8501` を開く。停止は `Ctrl+C`。

※ 2026-07のUI再構成で `app_a.py`/`app_b.py`/`app_top.py` のstreamlit単独起動は廃止
（エントリポイントは`app.py`のみ）。機能B-簡潔のテキストメモは `py -3.12 fase2/app_b.py` で出力可能。

## 起動前に確認すること

- 表示データは`ホールデータ/analysis.db`（`stage3_scores`/`store_profile`）を読む。
  データ収集後に分析DBが更新されていない場合は古い結果が表示されるため、
  最新化したい場合は先に`fase2/run_store_profile.py`を実行する
  （[`add-new-store`](../add-new-store/SKILL.md) skill参照）。
- `streamlit`未インストールの場合は`pip install streamlit`が必要（fase1の`requirements.txt`には含まれない）。
