"""
ui_theme.py — モバイルファーストUI(黒基調ダークテーマ・ZZZ風/ライムアクセント)の共通スタイル・Plotly設定

iPhone Safari前提のUI改修(2026-07)で新設、同月に黒基調ダークテーマへ改修。表示層のみを集約し、
機能(データ・計算ロジック)には一切関与しない。CSSはStreamlitのdata-testid依存のため、将来の
バージョンアップで崩れた場合の修正箇所をこのファイル1つに閉じる狙い。

依存: app.py が起動時に inject_css() を呼ぶ。各チャートは
      apply_mobile_layout(fig, ...) → st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)
      の順で呼び出す(全チャート共通規約)。
"""
import pandas as pd
import streamlit as st

# ── カラーパレット(ダーク基調・ZZZ風ライムアクセント) ──────────────────
ACCENT = '#D6FE3E'
PAGE_BG = '#0D0E11'
CARD_BG = '#1A1C21'
CARD_BORDER = '#2A2D34'
TEXT = '#F2F3F5'
TEXT_SUB = '#A8ADB8'
GRID = 'rgba(255,255,255,0.08)'
ZERO_LINE = 'rgba(255,255,255,0.55)'

# カテゴリカル8色(dataviz検証スクリプトでダークサーフェス#1A1C21上の全チェックPASS済み)
CATEGORICAL = [
    '#3987e5', '#199e70', '#c98500', '#008300',
    '#9085e9', '#e66767', '#d55181', '#d95926',
]
# 発散スケール(±スコア)。プラス=青・マイナス=赤・中間=白(2026-07 UIリニューアルで反転。
# ユーザー指定でRdBu風の白中間点を採用。スコア0付近のセルが多いため白発光気味になる点は承知の上での選択)。
POS_COLOR = '#2878E0'  # プラス(狙い目)
NEG_COLOR = '#F5334F'  # マイナス(弱い)
DIVERGING = [[0, NEG_COLOR], [0.5, '#FFFFFF'], [1, POS_COLOR]]
# 逐次スケール(信頼度)。Bluesの低値が背景へ沈み込む向きに置換
SEQ_BLUE = [[0, '#0D366B'], [1, '#86B6EF']]
# カレンダー暖色。YlOrRdの淡黄発光を避けdark→鮮やかなオレンジへ(2026-07再改修で彩度を強化)
SEQ_WARM = [[0, '#3A1400'], [0.55, '#A83C00'], [1, '#DB6A0A']]

# 全チャート共通のPlotly設定。モードバー非表示・スクロールズーム無効(ページスクロールを阻害しない)。
PLOTLY_CONFIG = {'displayModeBar': False, 'scrollZoom': False, 'doubleClick': 'reset'}


def inject_css() -> None:
    """黒基調(ZZZ風・ライムアクセント)+角丸カード+ピル型ボタン+1カラムのCSSを注入する。"""
    st.markdown(
        f"""
        <style>
        #MainMenu, footer, header[data-testid="stHeader"] {{ display: none; }}

        .block-container {{
            max-width: 680px;
            padding: 1rem 1rem 4rem;
        }}

        [data-testid="stVerticalBlockBorderWrapper"] {{
            border-radius: 16px;
            border: 1px solid {CARD_BORDER};
            background: {CARD_BG};
            box-shadow: 0 2px 10px rgba(0,0,0,.35);
        }}

        .stButton > button {{
            border-radius: 9999px;
            background-color: {CARD_BG};
            color: {TEXT};
            border: 1px solid {CARD_BORDER};
        }}
        .stButton > button[kind="primary"] {{
            background-color: {ACCENT};
            border-color: {ACCENT};
            color: #111111;
        }}

        [data-testid="stExpander"],
        div[data-baseweb="select"] > div,
        div[data-baseweb="input"],
        div[data-baseweb="base-input"] {{
            border-radius: 12px;
        }}

        html, body, [class*="css"] {{
            font-size: 16px;
        }}
        input, textarea, select {{
            font-size: 16px !important;
        }}

        [data-testid="stCaptionContainer"], .stCaption, small {{
            color: {TEXT_SUB} !important;
        }}

        h1, h2, h3 {{
            color: {TEXT};
            font-weight: 700;
        }}
        h1 {{
            font-size: 1.35rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        h2 {{ font-size: 1.15rem; }}
        h3 {{ font-size: 1.0rem; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def apply_mobile_layout(fig, height: int | None = None, show_colorbar: bool = False) -> None:
    """モバイル幅向けにPlotly figのレイアウトを共通調整する(全チャート共通規約)。"""
    fig.update_layout(
        title='',
        margin=dict(l=8, r=8, t=8, b=8),
        legend=dict(orientation='h', y=-0.18, font=dict(size=11, color=TEXT)),
        font=dict(size=12, color=TEXT),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        dragmode=False,
        colorway=CATEGORICAL,
        hoverlabel=dict(bgcolor='#24262C', font=dict(color=TEXT, size=12)),
    )
    fig.update_xaxes(tickfont=dict(size=11, color='#C6CBD1'), gridcolor=GRID, zerolinecolor=ZERO_LINE, automargin=True)
    fig.update_yaxes(tickfont=dict(size=11, color='#C6CBD1'), gridcolor=GRID, zerolinecolor=ZERO_LINE, automargin=True)
    if height is not None:
        fig.update_layout(height=height)
    if not show_colorbar:
        fig.update_coloraxes(showscale=False)
        fig.update_traces(showscale=False, selector=dict(type='heatmap'))


def wrap_label(s: str, width: int = 14) -> str:
    """長いラベルを切り捨てずwidth文字ごとに改行(<br>)して折り返す。"""
    s = str(s)
    if len(s) <= width:
        return s
    return '<br>'.join(s[i:i + width] for i in range(0, len(s), width))


def style_signed(df, cols: list[str]):
    """
    指定列の数値を DIVERGING と同じ配色(プラス=青/マイナス=赤)でfont-color着色した
    Styler を返す(st.dataframe(styler) で表示)。サイト全体の色統一(2026-07リニューアル)を
    表形式の表示にも適用するための共通ヘルパー。
    """
    def _color(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ''
        color = POS_COLOR if float(v) >= 0 else NEG_COLOR
        return f'color: {color}'

    styler = df.style
    style_fn = styler.map if hasattr(styler, 'map') else styler.applymap
    return style_fn(_color, subset=cols)
