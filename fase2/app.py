"""
app.py — ホームページ(店舗検索+ランキング)と店舗トップページを切り替える統合エントリポイント

構成:
  ホームページ(主ページ・起動直後に表示):
    1. 店舗検索: 店舗名の部分一致で絞り込み、店舗ボタンで店舗トップページへ遷移
    2. 当日・翌日ランキング(app_top.render)
    3. 機能B 店舗横断比較(app_b.render_overview: ランキング・サブスコア比較・ヒートマップ)
  店舗トップページ(店舗ごと・「ホームに戻る」ボタンで復帰):
    1. 機能A: 店内比較ダッシュボード(app_a.render)
    2. 機能B: 個別店舗詳細(app_b.render_store_detail)

ページ切替は st.session_state['selected_hole'] で管理する(サイドバーは使わない)。

実行方法:
    streamlit run app.py
"""
import streamlit as st

import app_a
import app_b
import app_top
import data_source as ds

st.set_page_config(page_title='パチスロ設定判別ツール', layout='wide')

_HOLE_KEY = 'selected_hole'
_SEARCH_COLS = 3  # 店舗ボタンの列数


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
    st.title('パチスロ設定判別ツール')

    # ── 店舗検索 ──
    st.header('店舗検索')
    holes = _list_all_holes(profiles)
    if not holes:
        st.error(
            '店舗データがありません。fase1のデータ収集と '
            'fase2/run_store_profile.py を先に実行してください。'
        )
    else:
        query = st.text_input(
            '店舗名で検索', value='',
            placeholder='店舗名の一部を入力(空欄で全店舗を表示)',
            key='hole_search',
        )
        matched = [h for h in holes if query.strip() in h] if query.strip() else holes
        if not matched:
            st.info('該当する店舗がありません。')
        cols = st.columns(_SEARCH_COLS)
        for i, hole in enumerate(matched):
            with cols[i % _SEARCH_COLS]:
                if st.button(hole, key=f'goto_{hole}', use_container_width=True):
                    st.session_state[_HOLE_KEY] = hole
                    st.rerun()

    st.divider()

    # ── 当日・翌日ランキング ──
    app_top.render()

    st.divider()

    # ── 機能B: 店舗横断比較 ──
    st.header('店舗横断比較')
    app_b.render_overview(profiles)


def _render_store_page(profiles, hole: str) -> None:
    st.button('← ホームに戻る', on_click=_go_home, key='back_home')
    st.title(hole)

    st.header('店内比較(機能A)')
    app_a.render(hole)

    st.divider()

    st.header('振り返りダッシュボード(機能B)')
    app_b.render_store_detail(profiles, hole)


_profiles = app_b.load_all_profiles()
_hole = st.session_state.get(_HOLE_KEY)
if _hole:
    _render_store_page(_profiles, _hole)
else:
    _render_home(_profiles)
