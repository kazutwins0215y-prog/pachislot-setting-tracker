"""
app.py — ホームページ(店舗検索+ランキング)と店舗トップページを切り替える統合エントリポイント

構成:
  ホームページ(主ページ・起動直後に表示):
    0. 鮮度警告バナー(app_top.render_freshness_banner。データが古い場合のみ表示)
    1. 店舗検索: st.selectboxで店舗を選択し店舗トップページへ遷移
    2. MM/DD(曜)のおすすめ店舗(app_top.render_recommend_stores)
    3. MM/DD(曜)の熱い台予測(app_top.render_hot_predictions)
  店舗トップページ(店舗ごと・「ホームに戻る」ボタン+店舗切替selectboxで復帰/移動):
    1. 機能A: 店内比較ダッシュボード(app_a.render)
    2. 店舗特徴(app_b.render_store_detail、機能Aの「店舗分析」ビュー選択時のみ表示)

ページ切替は st.session_state['selected_hole'] で管理する(サイドバーは使わない)。

実行方法:
    streamlit run app.py
"""
import streamlit as st

import app_a
import app_b
import app_top
import data_source as ds
import ui_theme

st.set_page_config(page_title='判別ツール', layout='centered')
ui_theme.inject_css()

_HOLE_KEY = 'selected_hole'


def _list_all_holes(profiles) -> list[str]:
    """レプリカDB(slot_data)とstore_profileの店舗名を統合して返す。"""
    holes: set[str] = set()
    try:
        holes.update(ds.list_holes())
    except FileNotFoundError:
        pass
    if not profiles.empty:
        holes.update(profiles['ホール名'].dropna().astype(str).tolist())
    return sorted(holes)


def _go_home() -> None:
    st.session_state[_HOLE_KEY] = None


def _render_home(profiles) -> None:
    st.title('判別ツール')

    # ── 鮮度警告バナー(データが古い場合のみ表示) ──
    app_top.render_freshness_banner()

    # ── 店舗検索 ──
    with st.container(border=True):
        st.header('店舗検索')
        holes = _list_all_holes(profiles)
        if not holes:
            st.error(
                '店舗データがありません。fase1のデータ収集と '
                'fase2/run_store_profile.py を先に実行してください。'
            )
        else:
            sel = st.selectbox(
                '店舗名で検索', holes, index=None,
                placeholder='店舗名を選択(入力で絞り込み)',
                key='hole_search',
            )
            if sel:
                st.session_state[_HOLE_KEY] = sel
                st.rerun()

    # ── MM/DD(曜)のおすすめ店舗 ──
    with st.container(border=True):
        app_top.render_recommend_stores()

    # ── MM/DD(曜)の熱い台予測 ──
    with st.container(border=True):
        app_top.render_hot_predictions()


def _render_store_page(profiles, hole: str) -> None:
    col_back, col_search = st.columns([1, 2])
    with col_back:
        st.button('← ホームに戻る', on_click=_go_home, key='back_home', type='primary')
    with col_search:
        holes = _list_all_holes(profiles)
        sel = st.selectbox(
            '店舗を切替', holes, index=None,
            placeholder='店舗名を選択(入力で絞り込み)',
            key='store_page_search', label_visibility='collapsed',
        )
        if sel and sel != hole:
            st.session_state[_HOLE_KEY] = sel
            st.rerun()

    st.title(hole)

    with st.container(border=True):
        st.header('店内比較(機能A)')
        app_a.render(hole)

    # 店舗特徴(機能B)は機能Aの「店舗分析」ビュー選択時のみ表示
    if st.session_state.get('a_view', '店舗分析') == '店舗分析':
        with st.container(border=True):
            st.header('店舗特徴')
            app_b.render_store_detail(profiles, hole)


_profiles = app_b.load_all_profiles()
_hole = st.session_state.get(_HOLE_KEY)
if _hole:
    _render_store_page(_profiles, _hole)
else:
    _render_home(_profiles)
