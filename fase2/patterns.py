"""
patterns.py — パターン検出層の窓口(facade)

2026-07-19に単一ファイル(2,642行)を役割別モジュールへ分割した。本ファイルは
従来どおり `import patterns as pt` で全名前を提供する互換窓口のみを担い、
実体は以下の6モジュールにある。**新しい関数・定数は該当する実体モジュールへ
追加し、本ファイルにfrom-importを足すこと**(ここにコードを書かない)。

- patterns_common.py     : 共有定数・周期探索(ACF/PDM/Lomb-Scargle)・カレンダー49候補・
                           BH補正・calendar_test・αブレンド原始関数(blend/FIXED_ALPHA)
- patterns_events.py     : 台移動/撤去/増台の検出(detect_events)・導入イベント判別・導入後カーブ検定
- patterns_breadth.py    : 幅型スコア(S_全台系/S_新台増台/S_移動台)・全台系/高配分の日次判定(z版/Fisher版)
- patterns_groups.py     : 末尾版/機種版/店舗日のカレンダー癖検定・機種バイアス判定・翌日予測
- patterns_depth.py      : 深さ型スコア(S_鉄板台/S_ローテ/S_据え置き)・鉄板台翌日予測・短期版計算
- patterns_transition.py : 遷移モデル(据え置き/上げ/下げ)・翌日予測ブレンド

各検出器の設計・変更履歴はfase2/データ分析_詳細_patterns.md参照。
"""
from patterns_common import *
from patterns_events import *
from patterns_breadth import *
from patterns_groups import *
from patterns_depth import *
from patterns_transition import *

# 私有ヘルパー(接頭辞_)はimport *では再輸出されないため、旧patterns.py名前空間との
# 完全互換のために明示的に再輸出する(外部の既知の利用は公開名のみだが保険)
from patterns_common import _WEEKDAY_NAMES, _wilcoxon_rank_biserial
from patterns_events import _all_confirmed_absent, _introduction_elapsed_bin
from patterns_breadth import _compute_event_scores
from patterns_depth import (
    _combine_signed, _phase_bin_effects, _phase_day_scores, _project_phase_score,
)
from patterns_transition import (
    _build_transition_pairs, _fit_transition_from_pairs, _stratified_permutation_test,
)
