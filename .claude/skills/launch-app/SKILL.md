---
name: launch-app
description: Use when the user asks to start, launch, run, or open 機能A / 機能B / the Streamlit app / the ground truth entry form ("機能Aを起動", "機能Bを開いて", "アプリを起動して", "ダッシュボードを見たい", "正解発表フォームを開いて", "ground_truth入力" など). Covers the command to launch the integrated Streamlit entry point (home page + per-store pages) and the separate local-only ground truth entry app.
---

# 機能A/B起動 skill

fase2の可視化ツール（機能A: 店内比較、店舗特徴(機能B)）をローカルでStreamlit起動する手順。

前提: [`CLAUDE.md`](../../../CLAUDE.md)のfase2構成、[`fase2/データ分析_skill.md`](../../../fase2/データ分析_skill.md)を参照。

## 起動コマンド

```bash
streamlit run fase2/app.py
```

起動直後はホームページ（店舗検索＋MM/DD(曜)のおすすめ店舗＋MM/DD(曜)の熱い台予測(7タブ)）が表示され、
店舗検索のselectboxから店舗トップページ（機能A店内比較＋店舗特徴。店舗特徴は機能Aで「店舗分析」
ビュー選択時のみ表示）へ遷移できる
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

## 正解発表(ground truth)の入力フォーム

店舗の設定発表を手入力する場合は別アプリ（`app.py`には統合されないローカル専用ツール）。
**`app.py`(判別ツール)と同時起動するとポート8501が競合するため、必ず別ポート(8502)を明示指定する**:

```bash
streamlit run fase2/ground_truth_entry.py --server.port 8502
```

日付→ホール→機種名→台番号の順でレプリカ`slot_data`から候補を絞り込みながら
`ホールデータ/ground_truth.db`へ記録する。詳細は
[`fase2/今後の実装予定.md`](../../../fase2/今後の実装予定.md)3節参照。

## 2アプリ同時起動時のポート運用

`app.py`(判別ツール)と`ground_truth_entry.py`(正解発表フォーム)を両方起動する場合、
**`app.py`は既定ポート8501、`ground_truth_entry.py`は`--server.port 8502`固定**とし、
`ground_truth_entry.py`側は起動コマンドに常にこのオプションを付ける
（省略するとStreamlitが空いている方のポートを自動選択し、どちらが8501になるか
実行順に依存して毎回変わってしまうため）。
