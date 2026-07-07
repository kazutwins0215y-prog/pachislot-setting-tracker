"""
ui_theme.py — モバイルファーストUI(HoYoLAB風カードUI)の共通スタイル・Plotly設定

iPhone Safari前提のUI改修(2026-07)で新設。表示層のみを集約し、機能(データ・計算ロジック)には
一切関与しない。CSSはStreamlitのdata-testid依存のため、将来のバージョンアップで崩れた場合の
修正箇所をこのファイル1つに閉じる狙い。

依存: app.py が起動時に inject_css() を呼ぶ。各チャートは
      apply_mobile_layout(fig, ...) → st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)
      の順で呼び出す(全チャート共通規約)。
"""
import streamlit as st

ACCENT = '#4C72B0'

# 全チャート共通のPlotly設定。モードバー非表示・スクロールズーム無効(ページスクロールを阻害しない)。
PLOTLY_CONFIG = {'displayModeBar': False, 'scrollZoom': False, 'doubleClick': 'reset'}


def inject_css() -> None:
    """HoYoLAB風(薄グレー地+白の角丸カード+ピル型ボタン+1カラム)のCSSを注入する。"""
    st.markdown(
        """
        <style>
        #MainMenu, footer, header[data-testid="stHeader"] { display: none; }

        .block-container {
            max-width: 680px;
            padding: 1rem 1rem 4rem;
        }

        [data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 16px;
            border: 1px solid #ECEEF2;
            background: #fff;
            box-shadow: 0 2px 8px rgba(0,0,0,.05);
        }

        .stButton > button {
            border-radius: 9999px;
        }
        .stButton > button[kind="primary"] {
            background-color: #4C72B0;
            border-color: #4C72B0;
        }

        [data-testid="stExpander"],
        div[data-baseweb="select"] > div,
        div[data-baseweb="input"],
        div[data-baseweb="base-input"] {
            border-radius: 12px;
        }

        html, body, [class*="css"] {
            font-size: 16px;
        }
        input, textarea, select {
            font-size: 16px !important;
        }

        h1 { font-size: 1.35rem; }
        h2 { font-size: 1.15rem; }
        h3 { font-size: 1.0rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def apply_mobile_layout(fig, height: int | None = None, show_colorbar: bool = False) -> None:
    """モバイル幅向けにPlotly figのレイアウトを共通調整する(全チャート共通規約)。"""
    fig.update_layout(
        title='',
        margin=dict(l=8, r=8, t=8, b=8),
        legend=dict(orientation='h', y=-0.18, font=dict(size=11)),
        font=dict(size=12),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        dragmode=False,
    )
    fig.update_xaxes(tickfont=dict(size=11))
    fig.update_yaxes(tickfont=dict(size=11))
    if height is not None:
        fig.update_layout(height=height)
    if not show_colorbar:
        fig.update_coloraxes(showscale=False)
        fig.update_traces(showscale=False, selector=dict(type='heatmap'))


def short_label(s: str, max_len: int = 14) -> str:
    """長いラベルを末尾…省略で切り詰める。"""
    s = str(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + '…'
