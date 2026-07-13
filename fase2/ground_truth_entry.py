"""
ground_truth_entry.py — 正解発表(ground truth)のローカル専用入力フォーム

店舗の設定発表(LINEオプチャ/X/Webサイト等)を手入力で ホールデータ/ground_truth.db へ
蓄積する。app.py(クラウドデプロイ対象)には統合しない、ローカル専用ツール。

- プルダウンは「選択した日付時点のレプリカ(turso_replica.db)」から機種名・台番号を絞り込む
  (過去入力では現在の機種構成と一致しないため)
- レプリカ未収集日は近傍日(±3日)の構成を候補表示 + 自由入力欄にフォールバック
- 訂正は「直近に入力した1件の取消」のみ(このセッションで挿入したidに限定)。
  それ以外は append-only(DELETE/UPDATE禁止)
- 設計の一次情報: fase2/今後の実装予定.md 3節「正解発表(ground truth)受け取り基盤」

実行方法:
    streamlit run fase2/ground_truth_entry.py
"""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import data_source as ds

_DATA_DIR = Path(__file__).resolve().parent.parent / 'ホールデータ'
GT_DB_PATH = _DATA_DIR / 'ground_truth.db'
STORES_JSON_PATH = Path(__file__).resolve().parent.parent / 'fase1' / 'stores.json'

ANNOUNCE_TYPES = ['全456', '全6', '半数456', '台数指定', 'ローテ対象', '台指定', '通常日']
UNIT_LIST_TYPES = {'ローテ対象', '台指定'}
COUNT_TYPES = {'半数456', '台数指定'}
SETTING_PRESETS = ['456', '56', '6']
TIMING_OPTIONS = ['事後', '当日', '事前告知']
NEARBY_WINDOW_DAYS = 3

_CREATE_TABLE_SQL = '''
    CREATE TABLE IF NOT EXISTS ground_truth (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        日付       TEXT NOT NULL,
        ホール名   TEXT NOT NULL,
        機種名     TEXT,
        発表タイプ TEXT NOT NULL,
        数値       REAL,
        台番号リスト TEXT,
        設定値     TEXT,
        発表タイミング TEXT,
        媒体メモ   TEXT,
        入力日時   TEXT NOT NULL
    )
'''


def connect_gt() -> sqlite3.Connection:
    """ground_truth.dbへの接続を返す(無ければファイル・テーブルを作成)。"""
    GT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(GT_DB_PATH))
    con.execute(_CREATE_TABLE_SQL)
    con.commit()
    return con


def load_stores() -> list[str]:
    with open(STORES_JSON_PATH, encoding='utf-8') as f:
        data = json.load(f)
    return sorted(data.get('stores', []))


def fetch_machines(con_replica: sqlite3.Connection, date_str: str, hole: str) -> list[str]:
    rows = con_replica.execute(
        'SELECT DISTINCT 機種名 FROM slot_data WHERE 日付 = ? AND ホール名 = ? ORDER BY 機種名',
        (date_str, hole),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def fetch_units(con_replica: sqlite3.Connection, date_str: str, hole: str, machine: str) -> list[int]:
    rows = con_replica.execute(
        'SELECT DISTINCT 台番号 FROM slot_data WHERE 日付 = ? AND ホール名 = ? AND 機種名 = ? '
        'ORDER BY 台番号',
        (date_str, hole, machine),
    ).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def find_nearby_date(con_replica: sqlite3.Connection, date_str: str, hole: str) -> str | None:
    """指定日にレプリカ未収集の場合、±NEARBY_WINDOW_DAYS日以内で最も近い収集済み日を返す。"""
    base = date.fromisoformat(date_str)
    for offset in range(1, NEARBY_WINDOW_DAYS + 1):
        for cand in (base - timedelta(days=offset), base + timedelta(days=offset)):
            cand_str = cand.isoformat()
            if fetch_machines(con_replica, cand_str, hole):
                return cand_str
    return None


def insert_row(con_gt: sqlite3.Connection, row: dict) -> int:
    cur = con_gt.execute(
        'INSERT INTO ground_truth '
        '(日付, ホール名, 機種名, 発表タイプ, 数値, 台番号リスト, 設定値, 発表タイミング, 媒体メモ, 入力日時) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            row['日付'], row['ホール名'], row.get('機種名'), row['発表タイプ'],
            row.get('数値'), row.get('台番号リスト'), row.get('設定値'),
            row.get('発表タイミング'), row.get('媒体メモ'),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        ),
    )
    con_gt.commit()
    return cur.lastrowid


def delete_row(con_gt: sqlite3.Connection, row_id: int) -> None:
    con_gt.execute('DELETE FROM ground_truth WHERE id = ?', (row_id,))
    con_gt.commit()


def load_entries(con_gt: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        'SELECT * FROM ground_truth ORDER BY 日付 DESC, id DESC', con_gt,
    )


def cross_check(con_replica: sqlite3.Connection | None, row: pd.Series) -> str:
    """機種名/台番号リストがレプリカの当該日付×ホールに実在するか突合する。"""
    if con_replica is None:
        return '(レプリカ未接続)'
    machine = row['機種名']
    if not machine:
        return '(店舗全体のため対象外)'
    machines = fetch_machines(con_replica, row['日付'], row['ホール名'])
    if machine not in machines:
        return '不一致(機種名)'
    unit_list_raw = row['台番号リスト']
    if unit_list_raw:
        try:
            units = json.loads(unit_list_raw)
        except (TypeError, ValueError):
            units = []
        known_units = set(fetch_units(con_replica, row['日付'], row['ホール名'], machine))
        missing = [u for u in units if u not in known_units]
        if missing:
            return f'不一致(台番号: {missing})'
    return '一致'


def _connect_replica_or_none() -> sqlite3.Connection | None:
    try:
        return ds.connect_replica()
    except FileNotFoundError:
        return None


def main() -> None:
    st.set_page_config(page_title='正解発表入力', layout='centered')
    st.title('正解発表 入力フォーム')
    st.caption('ローカル専用ツール。ホールデータ/ground_truth.db へ追記します(app.pyには統合されません)。')

    con_gt = connect_gt()
    con_replica = _connect_replica_or_none()
    if con_replica is None:
        st.warning(
            f'{ds.MISSING_REPLICA_MSG}\n'
            'プルダウン絞り込みが使えないため、機種名・台番号は自由入力になります。'
        )

    stores = load_stores()

    st.subheader('新規入力')
    sel_date = st.date_input('日付', value=date.today(), format='YYYY-MM-DD')
    date_str = sel_date.isoformat()
    sel_hole = st.selectbox('ホール名', stores, index=None, placeholder='店舗を選択')

    machines: list[str] = []
    effective_date_str = date_str
    fallback_note = None
    if sel_hole and con_replica is not None:
        machines = fetch_machines(con_replica, date_str, sel_hole)
        if not machines:
            nearby = find_nearby_date(con_replica, date_str, sel_hole)
            if nearby:
                machines = fetch_machines(con_replica, nearby, sel_hole)
                effective_date_str = nearby
                fallback_note = (
                    f'{date_str} はレプリカ未収集のため、近傍日 {nearby} の機種構成を候補表示しています。'
                    '実際と異なる場合は自由入力欄を使ってください。'
                )
            else:
                fallback_note = (
                    f'{date_str} 前後{NEARBY_WINDOW_DAYS}日にレプリカデータがありません。自由入力欄を使ってください。'
                )
    if fallback_note:
        st.info(fallback_note)

    use_free_machine = st.checkbox('機種名を自由入力する(店舗全体の発表の場合はチェックせず未選択のままでOK)')
    if use_free_machine:
        sel_machine = st.text_input('機種名(自由入力)') or None
    else:
        sel_machine = st.selectbox(
            '機種名(未選択=店舗全体)', machines, index=None,
            placeholder='機種を選択(空欄可)',
        )

    announce_type = st.radio('発表タイプ', ANNOUNCE_TYPES, horizontal=True)

    numeric_value = None
    if announce_type in COUNT_TYPES:
        numeric_value = st.number_input('数値(台数)', min_value=0, step=1, value=0)

    unit_list_json = None
    if announce_type in UNIT_LIST_TYPES:
        if sel_machine and con_replica is not None:
            candidate_units = fetch_units(con_replica, effective_date_str, sel_hole, sel_machine)
        else:
            candidate_units = []
        sel_units = st.multiselect('台番号(該当日に入った台)', candidate_units)
        free_units_text = st.text_input('台番号 自由入力(カンマ区切り、候補にない台番号がある場合)')
        free_units = []
        if free_units_text:
            for tok in free_units_text.split(','):
                tok = tok.strip()
                if tok.isdigit():
                    free_units.append(int(tok))
        all_units = sorted(set(sel_units) | set(free_units))
        if all_units:
            unit_list_json = json.dumps(all_units, ensure_ascii=False)

    setting_choice = st.radio('設定値', SETTING_PRESETS + ['その他(自由入力)'], index=0, horizontal=True)
    if setting_choice == 'その他(自由入力)':
        setting_value = st.text_input('設定値(自由入力)') or None
    else:
        setting_value = setting_choice

    timing = st.selectbox('発表タイミング', TIMING_OPTIONS, index=0)
    media_memo = st.text_input('媒体メモ(任意。LINEオプチャ/X画像/Webサイト等)')

    if st.button('この内容で登録', type='primary'):
        if not sel_hole:
            st.error('ホール名を選択してください。')
        else:
            row = {
                '日付': date_str,
                'ホール名': sel_hole,
                '機種名': sel_machine,
                '発表タイプ': announce_type,
                '数値': float(numeric_value) if numeric_value else None,
                '台番号リスト': unit_list_json,
                '設定値': setting_value,
                '発表タイミング': timing,
                '媒体メモ': media_memo or None,
            }
            new_id = insert_row(con_gt, row)
            st.session_state['last_inserted_id'] = new_id
            st.success(f'登録しました(id={new_id})。')
            st.rerun()

    last_id = st.session_state.get('last_inserted_id')
    if last_id is not None:
        if st.button(f'直前の入力(id={last_id})を取り消す'):
            delete_row(con_gt, last_id)
            st.session_state['last_inserted_id'] = None
            st.success('取り消しました。')
            st.rerun()

    st.divider()
    st.subheader('入力済み一覧')
    entries = load_entries(con_gt)
    if entries.empty:
        st.info('まだ入力がありません。')
    else:
        col1, col2 = st.columns(2)
        with col1:
            filter_hole = st.selectbox('ホール名で絞り込み', ['(すべて)'] + stores, index=0)
        with col2:
            filter_date = st.date_input('日付で絞り込み', value=None, format='YYYY-MM-DD')

        view = entries
        if filter_hole != '(すべて)':
            view = view[view['ホール名'] == filter_hole]
        if filter_date:
            view = view[view['日付'] == filter_date.isoformat()]

        if st.checkbox('突合チェックを表示(レプリカとの一致確認、やや低速)'):
            view = view.copy()
            view['突合'] = view.apply(lambda r: cross_check(con_replica, r), axis=1)

        st.dataframe(view, use_container_width=True, hide_index=True)

    con_gt.close()
    if con_replica is not None:
        con_replica.close()


if __name__ == '__main__':
    main()
