"""
app.py — 機能A・機能Bを1つのWebページに統合するエントリポイント

サイドバーの選択メニューで「トップページ」「機能A: 店内比較」
「機能B-詳細: 振り返りダッシュボード」を切り替える。切り替え時は選択中の機能の
画面・サイドバーのみが表示され、もう一方の読み込み・描画処理は実行されない。

実行方法:
    streamlit run app.py

機能単体で従来通り起動したい場合は app_top.py / app_a.py / app_b.py を
そのまま使用可能(このファイルはそれらを変更しない薄いラッパー)。
"""
import streamlit as st

import app_a
import app_b
import app_top

st.set_page_config(page_title='パチスロ設定判別ツール', layout='wide')

st.sidebar.title('機能選択')
mode = st.sidebar.radio(
    '表示する機能',
    ['トップページ: 当日・翌日ランキング', '機能A: 店内比較', '機能B-詳細: 振り返りダッシュボード'],
    label_visibility='collapsed',
)
st.sidebar.divider()

if mode == 'トップページ: 当日・翌日ランキング':
    app_top.render()
elif mode == '機能A: 店内比較':
    app_a.render()
else:
    app_b.render_detail()
