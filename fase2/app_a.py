"""
app_a.py — 店内比較・可視化ツール (Streamlit)

用途: 来店前/来店中に1店舗内の台を手動で見比べる

表示ビュー:
  [店舗単位] 月ごとの合計差枚数の平均、任意月の日次差枚数推移
  [台番号]   台番号別の日次トレンド折れ線(機種名サジェスト・台番号末尾絞り込み付き)
  [機種名]   機種名別の日次トレンド折れ線(台番号末尾での色分けにも対応)

絞り込み(データ範囲・日付絞り込み・機種名/台番号選択)は各ビュー本文内で行う
(旧サイドバーフィルタは廃止)。

注意: 運用しながら必要な図を追加していく想定(確定版ではない)

依存: preprocess.py (load_slot_data / high_prob), score.py (合成スコア・内訳)

実行方法:
  app.py の店舗トップページから render(hole_name) で呼ばれる(単独起動は廃止)
"""
import math
import sqlite3

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

import data_source as ds
import ui_theme as ui

_WEEKDAY_JP = ['月', '火', '水', '木', '金', '土', '日']


# ── DB ユーティリティ ─────────────────────────────────────────────────

def _load_stage3_scores(hole_name: str) -> pd.DataFrame:
    """分析DB(analysis.db)からstage3_scoresを読む。未作成なら空DFを返す。"""
    if not ds.ANALYSIS_DB_PATH.exists():
        return pd.DataFrame()
    try:
        con = sqlite3.connect(str(ds.ANALYSIS_DB_PATH))
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'", con
            )['name'].tolist()
            if 'stage3_scores' not in tables:
                return pd.DataFrame()
            return pd.read_sql_query(
                'SELECT 日付, 機種名, 台番号, log_odds, high_prob, is_invalid '
                'FROM stage3_scores WHERE ホール名 = ?',
                con,
                params=(hole_name,),
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _load_data_cached(hole_name: str) -> tuple[pd.DataFrame, str, str]:
    """データ読み込み + スコア計算をキャッシュ（UIコールなし）。"""
    import time
    t0 = time.perf_counter()

    con = ds.connect_replica()
    try:
        df = pd.read_sql_query(
            'SELECT * FROM slot_data WHERE ホール名 = ?',
            con,
            params=(hole_name,),
        )
    finally:
        con.close()

    scores = _load_stage3_scores(hole_name)
    if not scores.empty:
        df = df.merge(scores, on=['日付', '機種名', '台番号'], how='left')

    for col in ['台番号', '回転数', '差枚', 'BB', 'RB', 'ART']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    for col in ['BB確率', 'RB確率', 'ART確率', '合成確率', 'log_odds', 'high_prob']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    warn_msg = ''
    if 'high_prob' not in df.columns or df['high_prob'].isna().all():
        try:
            import preprocess as pp
            machine_tier, bias_params, column_map = pp.calibrate_all(df)
            specs = pp._load_specs()
            scored = pp.compute_all_logLR(df, machine_tier, bias_params, specs, column_map)
            scored = pp.compute_log_odds(scored)
            scored = pp.mark_invalid(scored, machine_tier, specs)
            df['log_odds'] = scored['log_odds'].values
            df['high_prob'] = scored['high_prob'].values
            df['is_invalid'] = scored['is_invalid'].values
        except Exception as e:
            warn_msg = f'統合スコア計算エラー: {e}'

    if 'high_prob' in df.columns:
        df['統合スコア'] = (df['high_prob'] * 6).round(2)

    df['日付'] = pd.to_datetime(df['日付'], errors='coerce')
    df['月'] = df['日付'].dt.to_period('M').astype(str)

    elapsed = time.perf_counter() - t0
    info_msg = f'初回読み込み {elapsed:.1f}s ({len(df):,}行) — 2回目以降はキャッシュ'
    return df, warn_msg, info_msg


def load_data(hole_name: str) -> pd.DataFrame:
    """slot_data(レプリカ) と stage3_scores(分析DB) を結合して読み込む。
    stage3_scores がない場合は preprocess でオンザフライ計算。"""
    df, warn_msg, info_msg = _load_data_cached(hole_name)
    if warn_msg:
        st.warning(warn_msg)
    st.caption(info_msg)
    return df


# ── ビュー: 店舗単位 ─────────────────────────────────────────────────

def view_store_summary(df: pd.DataFrame) -> None:
    """店舗分析の集計ビュー（月間累計差枚・累計推移）を描画する。"""
    st.subheader('店舗分析ビュー')
    if df.empty or '差枚' not in df.columns:
        st.info('差枚データがありません。')
        return

    daily = (
        df.dropna(subset=['日付', '差枚'])
        .groupby('日付', as_index=False)['差枚']
        .sum()
        .rename(columns={'差枚': '合計差枚'})
    )
    daily['月'] = daily['日付'].dt.strftime('%Y/%m')
    monthly = (
        daily.groupby('月', as_index=False)['合計差枚']
        .sum()
        .rename(columns={'合計差枚': '月間累計差枚'})
    )

    st.markdown('**月ごとの累計差枚**')
    fig = px.bar(monthly, x='月', y='月間累計差枚', color_discrete_sequence=[ui.ACCENT])
    _apply_jp_yaxis(fig, monthly['月間累計差枚'].tolist())
    ui.apply_mobile_layout(fig, height=320)
    st.plotly_chart(fig, use_container_width=True, config=ui.PLOTLY_CONFIG)

    months = sorted(daily['月'].unique().tolist())
    sel_month = st.selectbox('月を選択', ['全期間'] + months, key='sum_month')
    plot = daily if sel_month == '全期間' else daily[daily['月'] == sel_month]
    plot = plot.sort_values('日付').copy()
    plot['累計差枚'] = plot['合計差枚'].cumsum()
    st.markdown(f'**累計差枚推移 — {sel_month}**')
    fig2 = px.line(plot, x='日付', y='累計差枚', markers=True, color_discrete_sequence=[ui.ACCENT])
    cum = plot['累計差枚'].dropna()
    y_min2 = float(cum.min()) if not cum.empty else 0.0
    y_max2 = float(cum.max()) if not cum.empty else 0.0
    if y_min2 < 0:
        fig2.add_hrect(y0=y_min2 * 1.1, y1=0, fillcolor='rgba(220,80,80,0.16)', layer='below', line_width=0)
    if y_max2 > 0:
        fig2.add_hrect(y0=0, y1=y_max2 * 1.1, fillcolor='rgba(60,180,100,0.16)', layer='below', line_width=0)
    fig2.add_hline(y=0, line_color=ui.ZERO_LINE, line_width=2.5)
    fig2.update_xaxes(tickformat='%Y/%m/%d')
    _apply_jp_yaxis(fig2, cum.tolist())
    ui.apply_mobile_layout(fig2, height=320)
    st.plotly_chart(fig2, use_container_width=True, config=ui.PLOTLY_CONFIG)


# ── ビュー: 台ごとの比較 ─────────────────────────────────────────────

# ── ビュー: 台番号比較 ───────────────────────────────────────────────

# ── グラフ・表示ユーティリティ ──────────────────────────────────────

_PROB_COLS = ['BB確率', 'RB確率', 'ART確率', '合成確率']


def _fmt_prob_cols(df: pd.DataFrame) -> pd.DataFrame:
    """確率列を分数表記（1/N）に変換したコピーを返す。"""
    df = df.copy()
    for col in _PROB_COLS:
        if col in df.columns:
            df[col] = df[col].apply(
                lambda p: f'1/{round(1/p)}' if pd.notna(p) and p > 0 else ''
            )
    return df


def _fmt_jp(n: float) -> str:
    """数値を日本語単位（万）で表記する。1万以上は万単位、未満はカンマ区切り整数。"""
    if abs(n) >= 10000:
        v = n / 10000
        return f'{v:.1f}万'.replace('.0万', '万')
    return f'{n:,.0f}'


def _apply_jp_yaxis(fig, vals) -> None:
    """Y軸のtickを日本語単位（万）でフォーマットする。"""
    clean = [float(v) for v in vals if pd.notna(v)]
    if not clean:
        return
    y_min, y_max = min(clean), max(clean)
    span = y_max - y_min
    if span == 0:
        fig.update_yaxes(tickvals=[y_min], ticktext=[_fmt_jp(y_min)])
        return
    raw_step = span / 5
    mag = 10 ** math.floor(math.log10(abs(raw_step))) if raw_step else 1
    step = mag * min([1, 2, 5, 10], key=lambda s: abs(s * mag - raw_step))
    lo = math.floor(y_min / step) * step
    hi = math.ceil(y_max / step) * step
    ticks = []
    v = lo
    while v <= hi + step * 1e-9:
        ticks.append(round(v))
        v += step
    fig.update_yaxes(tickvals=ticks, ticktext=[_fmt_jp(t) for t in ticks])


def _apply_xaxis_date_fmt(fig, agg: pd.DataFrame, period: str = '') -> None:
    """横軸の日付フォーマットを設定する。1週間の場合は曜日を付与する。"""
    if period == '1週間' and '日付' in agg.columns:
        dates = sorted(pd.to_datetime(agg['日付'].dropna().unique()))
        if dates:
            step = max(1, math.ceil(len(dates) / 7))
            shown = dates[::step]
            fig.update_xaxes(
                tickvals=shown,
                ticktext=[
                    f"{d.strftime('%m/%d')}({_WEEKDAY_JP[d.dayofweek]})"
                    for d in shown
                ],
            )
            return
    fig.update_xaxes(tickformat='%Y/%m/%d')


def _apply_chart_style(fig, agg: pd.DataFrame, y_col: str, period: str = '') -> None:
    """0ライン・プラス/マイナス背景色・日付フォーマットを適用する。"""
    y_vals = agg[y_col].dropna()
    y_min = float(y_vals.min()) if not y_vals.empty else 0.0
    y_max = float(y_vals.max()) if not y_vals.empty else 0.0

    if y_min < 0:
        fig.add_hrect(
            y0=y_min * 1.1, y1=0,
            fillcolor='rgba(220,80,80,0.12)', layer='below', line_width=0,
        )
    if y_max > 0:
        fig.add_hrect(
            y0=0, y1=y_max * 1.1,
            fillcolor='rgba(60,180,100,0.12)', layer='below', line_width=0,
        )
    fig.add_hline(y=0, line_color='rgba(255,255,255,0.4)', line_width=1.5)
    _apply_xaxis_date_fmt(fig, agg, period)
    _apply_jp_yaxis(fig, agg[y_col].tolist())


def _period_filter(df: pd.DataFrame, period: str) -> pd.DataFrame:
    d_max = df['日付'].max()
    if period == '1週間':
        d_min = d_max - pd.Timedelta(days=6)
    elif period == '1か月':
        d_min = d_max - pd.DateOffset(months=1)
    elif period == '3か月':
        d_min = d_max - pd.DateOffset(months=3)
    else:
        d_min = df['日付'].min()
    return df[(df['日付'] >= d_min) & (df['日付'] <= d_max)].copy()


def _num_cols(df: pd.DataFrame) -> list[str]:
    return [
        c for c in ['統合スコア', '差枚', '回転数', 'BB確率', 'RB確率', '合成確率', 'high_prob', 'log_odds']
        if c in df.columns and df[c].notna().any()
    ]


def _apply_score_yaxis(fig) -> None:
    """統合スコア(0〜6)用の固定Y軸を設定する。"""
    fig.update_yaxes(
        range=[0, 6],
        tickvals=[0, 1, 2, 3, 4, 5, 6],
        ticktext=['0', '1', '2', '3', '4', '5', '6'],
    )


def _date_filter_ui(df: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    """日付末尾・曜日・単日フィルタUIをインライン表示し絞り込み済みDataFrameを返す。"""
    with st.expander('日付絞り込み', expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            suffix_opts = ['(指定なし)', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'ぞろ目']
            suffix = st.selectbox('日付末尾', suffix_opts, key=f'{key_prefix}_date_suffix')
        with c2:
            wday = st.selectbox('曜日', ['(指定なし)'] + _WEEKDAY_JP, key=f'{key_prefix}_date_wday')

        st.divider()
        avail_dates = sorted(df['日付'].dt.date.dropna().unique().tolist())
        specific_dates = st.multiselect(
            '単日指定',
            options=avail_dates,
            default=[],
            key=f'{key_prefix}_date_specific',
            format_func=lambda d: d.strftime('%Y/%m/%d'),
        )

    has_suffix = suffix != '(指定なし)'
    has_wday = wday != '(指定なし)'
    has_specific = bool(specific_dates)

    if not (has_suffix or has_wday or has_specific):
        return df

    group1 = pd.Series(True, index=df.index)
    if has_suffix:
        if suffix == 'ぞろ目':
            day_vals = df['日付'].dt.day
            group1 &= day_vals.apply(
                lambda d: pd.notna(d) and int(d) >= 10 and len(set(str(int(d)))) == 1
            )
        else:
            group1 &= df['日付'].dt.day % 10 == int(suffix)
    if has_wday:
        group1 &= df['日付'].dt.dayofweek == _WEEKDAY_JP.index(wday)

    if has_specific:
        group2 = df['日付'].dt.date.isin(specific_dates)
        combined = (group1 | group2) if (has_suffix or has_wday) else group2
    else:
        combined = group1

    return df[combined]


def view_by_slot(df: pd.DataFrame) -> None:
    """台番号ごとの時系列比較（横軸: 日付固定）。"""
    st.subheader('台番号比較')
    if df.empty or '日付' not in df.columns:
        st.info('データがありません。')
        return

    nums = [c for c in ['統合スコア', '差枚'] if c in df.columns and df[c].notna().any()]
    if not nums:
        st.info('統合スコア・差枚データがありません。')
        return

    c1, c2 = st.columns(2)
    with c1:
        period = st.selectbox('データ範囲', ['1週間', '1か月', '3か月', '全期間'], index=0, key='slot_period')
    with c2:
        y_axis = st.selectbox('Y軸（指標）', nums, index=0, key='slot_y')

    plot_df = _period_filter(df, period)
    plot_df = _date_filter_ui(plot_df, 'slot')

    if '台番号' not in plot_df.columns:
        st.info('台番号データがありません。')
        return

    # 機種名で台番号をサジェスト
    if '機種名' in plot_df.columns:
        all_machines = sorted(plot_df['機種名'].dropna().unique().tolist())
        sel_machines = st.multiselect(
            '機種名で絞り込む（未選択で全台番号を表示）', all_machines, default=[], key='slot_machines',
        )
        if sel_machines:
            suggested = plot_df[plot_df['機種名'].isin(sel_machines)]['台番号'].dropna().astype(int).unique()
            nos_pool = sorted(suggested.tolist())
        else:
            nos_pool = sorted(plot_df['台番号'].dropna().astype(int).unique().tolist())
    else:
        nos_pool = sorted(plot_df['台番号'].dropna().astype(int).unique().tolist())

    _d_sfx = st.session_state.get('slot_date_suffix', '(指定なし)')
    if st.session_state.get('_slot_sfx_sync') != _d_sfx:
        st.session_state['slot_no_suffix'] = [_d_sfx] if _d_sfx != '(指定なし)' else []
        st.session_state['_slot_sfx_sync'] = _d_sfx
    sel_slot_suffixes = st.multiselect('台番号末尾', ['0','1','2','3','4','5','6','7','8','9','ぞろ目'], default=[], key='slot_no_suffix')
    if sel_slot_suffixes:
        _s_ints = {int(s) for s in sel_slot_suffixes if s != 'ぞろ目'}
        _s_zorrome = 'ぞろ目' in sel_slot_suffixes
        nos_pool = [
            n for n in nos_pool
            if n % 10 in _s_ints
            or (_s_zorrome and len(str(n)) > 1 and len(set(str(n))) == 1)
        ]

    plot_df['台番号_str'] = plot_df['台番号'].dropna().astype(int).astype(str)
    nos = [str(n) for n in nos_pool]
    sel_nos = st.multiselect('台番号を選択', nos, default=[], key='slot_nos')
    if not sel_nos:
        st.info('台番号を選択してください。')
        return
    plot_df = plot_df[plot_df['台番号_str'].isin(sel_nos)].copy()

    # 台番号×機種名の連続期間でセグメント化
    if '機種名' in plot_df.columns:
        plot_df = plot_df.sort_values(['台番号', '日付']).copy()
        plot_df['_seg_id'] = (
            plot_df.groupby('台番号')['機種名']
            .transform(lambda s: (s != s.shift()).cumsum())
        )
        seg_meta = (
            plot_df.groupby(['台番号', '_seg_id'])
            .agg(_機種名=('機種名', 'first'), _開始=('日付', 'min'), _終了=('日付', 'max'))
            .reset_index()
        )
        multi_nos = set(
            seg_meta[seg_meta.groupby('台番号')['_seg_id'].transform('count') > 1]['台番号']
        )
        def _seg_label(row):
            base = f"{row['_機種名']} {int(row['台番号'])}番台"
            if row['台番号'] in multi_nos:
                s = row['_開始'].strftime('%m/%d')
                e = row['_終了'].strftime('%m/%d')
                return f"{base}（{s}〜{e}）"
            return base
        seg_meta['_seg_label'] = seg_meta.apply(_seg_label, axis=1)
        plot_df = plot_df.merge(
            seg_meta[['台番号', '_seg_id', '_seg_label']], on=['台番号', '_seg_id'], how='left',
        )
        color_col = '_seg_label'
    else:
        color_col = '台番号_str'

    plot_df = plot_df.dropna(subset=['日付', y_axis, color_col])
    if plot_df.empty:
        st.info('有効なデータがありません。')
        return

    agg = plot_df.groupby(['日付', color_col])[y_axis].mean().reset_index()
    fig = px.line(
        agg, x='日付', y=y_axis, color=color_col, markers=True,
        labels={'_seg_label': '台', '台番号_str': '台番号'},
    )
    fig.update_traces(marker=dict(size=8))
    fig.update_layout(xaxis_title='日付')
    if y_axis == '統合スコア':
        _apply_score_yaxis(fig)
        _apply_xaxis_date_fmt(fig, agg, period)
    else:
        _apply_chart_style(fig, agg, y_axis, period)
    ui.apply_mobile_layout(fig, height=380)

    event = st.plotly_chart(
        fig, use_container_width=True,
        on_select='rerun', selection_mode='points',
        key='slot_chart', config=ui.PLOTLY_CONFIG,
    )

    # クリックした点の素データを表示
    pts = event.selection.points if event else []
    if pts:
        sel_rows = []
        for pt in pts:
            cn = pt.get('curve_number', 0)
            trace_name = fig.data[cn].name if cn < len(fig.data) else ''
            date_str = str(pt.get('x', ''))[:10]
            mask = (
                (plot_df['日付'].dt.strftime('%Y-%m-%d') == date_str)
                & (plot_df[color_col] == trace_name)
            )
            sel_rows.append(plot_df[mask])

        if sel_rows:
            sel_df = pd.concat(sel_rows).drop_duplicates()
            if not sel_df.empty:
                st.markdown('---')
                st.markdown('**選択した点の素データ**')
                show_cols = [c for c in [
                    '日付', '台番号', '機種名', '差枚', '回転数', '合成確率', 'high_prob',
                ] if c in sel_df.columns]
                disp_sel = _fmt_prob_cols(sel_df[show_cols]).sort_values('日付').reset_index(drop=True)
                if 'high_prob' in disp_sel.columns:
                    disp_sel = disp_sel.rename(columns={'high_prob': '高設定確率'})
                    disp_sel['高設定確率'] = disp_sel['高設定確率'].apply(
                        lambda p: f'{float(p):.0%}' if pd.notna(p) else ''
                    )
                st.dataframe(disp_sel, use_container_width=True)


# ── ビュー: 機種名比較 ───────────────────────────────────────────────

def view_by_machine(df: pd.DataFrame) -> None:
    """機種名ごとの時系列比較（横軸: 日付固定）。"""
    st.subheader('機種名比較')
    if df.empty or '日付' not in df.columns:
        st.info('データがありません。')
        return

    nums = [c for c in ['統合スコア', '差枚'] if c in df.columns and df[c].notna().any()]
    if not nums:
        st.info('統合スコア・差枚データがありません。')
        return

    if '機種名' not in df.columns:
        st.info('機種名データがありません。')
        return

    c1, c2 = st.columns(2)
    with c1:
        period = st.selectbox('データ範囲', ['1週間', '1か月', '3か月', '全期間'], index=0, key='mach_period')
    with c2:
        y_axis = st.selectbox('Y軸（指標）', nums, index=0, key='mach_y')

    plot_df = _period_filter(df, period)
    plot_df = _date_filter_ui(plot_df, 'mach')

    _d_sfx_m = st.session_state.get('mach_date_suffix', '(指定なし)')
    if st.session_state.get('_mach_sfx_sync') != _d_sfx_m:
        st.session_state['mach_slot_suffix'] = [_d_sfx_m] if _d_sfx_m != '(指定なし)' else []
        st.session_state['_mach_sfx_sync'] = _d_sfx_m
    mach_slot_suffixes = st.multiselect('台番号末尾', ['0','1','2','3','4','5','6','7','8','9','ぞろ目'], default=[], key='mach_slot_suffix')

    if mach_slot_suffixes and '台番号' in plot_df.columns:
        # 台番号末尾選択時: 機種名選択なし・末尾ごとに色分けして描画
        _m_ints = {int(s) for s in mach_slot_suffixes if s != 'ぞろ目'}
        _m_zorrome = 'ぞろ目' in mach_slot_suffixes

        def _slot_label(x):
            if pd.isna(x):
                return None
            n = int(x)
            if n % 10 in _m_ints:
                return f'末尾{n % 10}'
            if _m_zorrome and len(str(n)) > 1 and len(set(str(n))) == 1:
                return 'ぞろ目'
            return None

        no_col = pd.to_numeric(plot_df['台番号'], errors='coerce')
        plot_df = plot_df[no_col.notna()].copy()
        plot_df['_suffix'] = pd.to_numeric(plot_df['台番号'], errors='coerce').apply(_slot_label)
        plot_df = plot_df[plot_df['_suffix'].notna()]
        plot_df = plot_df.dropna(subset=['日付', y_axis])
        if plot_df.empty:
            st.info('有効なデータがありません。')
            return
        agg = plot_df.groupby(['日付', '_suffix'])[y_axis].mean().reset_index()
        fig = px.line(
            agg, x='日付', y=y_axis, color='_suffix', markers=True,
            labels={'_suffix': '台番号末尾'},
        )
        fig.update_traces(marker=dict(size=8))
        fig.update_layout(xaxis_title='日付')
        if y_axis == '統合スコア':
            _apply_score_yaxis(fig)
            _apply_xaxis_date_fmt(fig, agg, period)
        else:
            _apply_chart_style(fig, agg, y_axis, period)
        ui.apply_mobile_layout(fig, height=380)
        st.plotly_chart(fig, use_container_width=True, config=ui.PLOTLY_CONFIG)
        return

    # 台番号末尾未選択: 機種名ごとに描画
    machines = sorted(plot_df['機種名'].dropna().unique().tolist())
    sel = st.multiselect('機種名を選択', machines, default=[], key='mach_machines')
    if not sel:
        st.info('機種名を選択してください。')
        return
    plot_df = plot_df[plot_df['機種名'].isin(sel)]

    plot_df = plot_df.dropna(subset=['日付', y_axis, '機種名'])
    if plot_df.empty:
        st.info('有効なデータがありません。')
        return

    agg = plot_df.groupby(['日付', '機種名'])[y_axis].mean().reset_index()
    fig = px.line(
        agg, x='日付', y=y_axis, color='機種名', markers=True,
    )
    fig.update_traces(marker=dict(size=8))
    fig.update_layout(xaxis_title='日付')
    if y_axis == '統合スコア':
        _apply_score_yaxis(fig)
        _apply_xaxis_date_fmt(fig, agg, period)
    else:
        _apply_chart_style(fig, agg, y_axis, period)
    ui.apply_mobile_layout(fig, height=380)
    st.plotly_chart(fig, use_container_width=True, config=ui.PLOTLY_CONFIG)


# ── メイン ───────────────────────────────────────────────────────────

def render(hole_name: str) -> None:
    """機能Aの画面本体。app.pyの店舗トップページから店舗固定で呼ばれる。"""
    with st.spinner('データ読み込み中... (初回のみ時間がかかります)'):
        df = load_data(hole_name)

    if df.empty:
        st.warning('このDBにデータがありません。')
        return

    if df['日付'].notna().any():
        st.caption(f'{df["日付"].min().date()} 〜 {df["日付"].max().date()}')

    view = st.segmented_control(
        'ビュー', ['店舗分析', '台番号', '機種名'], default='店舗分析',
        label_visibility='collapsed', key='a_view',
    )
    if view is None:
        view = '店舗分析'
    st.divider()

    if view == '店舗分析':
        view_store_summary(df)
    elif view == '台番号':
        view_by_slot(df)
    else:
        view_by_machine(df)

    if st.button('キャッシュをクリア（新データ反映）', key='a_clear_cache'):
        _load_data_cached.clear()
