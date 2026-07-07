"""
bootstrap.py — Streamlit Community Cloud起動時のデータ実体化ロジック

生データDB・分析DBをそれぞれのTursoから埋め込みレプリカとしてsyncし、
fase2/data_source.py が読む固定パス(REPLICA_DB_PATH / ANALYSIS_DB_PATH)へ
実体化する。これによりfase2(app_top/app_a/app_b)は無改修のまま動く
(設計は fase3/配信公開_設計.md「fase2を無改修に保つ仕組み」節参照)。

呼び出し前提: streamlit_app.py が sys.path に fase1/・fase2/・fase3/ を
追加してから本モジュールをimportすること(db / data_source / analysis_turso の
import解決に必要)。

制約: fase2/app.py がモジュールレベルでst.set_page_config()を呼ぶため、
run()はst系のUI要素を一切出さずに完了させること(streamlit_app.py側で
@st.cache_resource(show_spinner=False)に包んで1回だけ呼ぶ)。
"""
import os

import db as fase1_db          # fase1/db.py: 生データDBの埋め込みレプリカ接続
import analysis_turso as at    # fase3/analysis_turso.py: 分析DBの埋め込みレプリカ接続
import data_source as ds       # fase2/data_source.py: REPLICA_DB_PATH/ANALYSIS_DB_PATHの固定パス

_SECRET_KEYS = [
    'TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN',
    'TURSO_ANALYSIS_DATABASE_URL', 'TURSO_ANALYSIS_AUTH_TOKEN',
]


def _load_secrets_into_env() -> None:
    """st.secretsに認証情報があれば環境変数へ転写する。ローカル実行時は
    .streamlit/secrets.tomlが無くst.secretsアクセス自体が例外になるため、
    その場合は.envから読まれた既存の環境変数をそのまま使う。"""
    try:
        import streamlit as st
        for key in _SECRET_KEYS:
            if key not in os.environ and key in st.secrets:
                os.environ[key] = st.secrets[key]
    except Exception:
        pass


def run() -> None:
    """生データDB・分析DBをそれぞれ埋め込みレプリカとしてsyncし、fase2固定パスへ実体化する。"""
    _load_secrets_into_env()

    # ①生データDB → REPLICA_DB_PATH (fase1/db.pyのREPLICA_PATHと同一パス)
    con1 = fase1_db.get_connection()
    con1.close()

    # ②分析DB → ANALYSIS_DB_PATHへ直接sync(レプリカ=SQLite互換ファイルなのでfase2から
    # 見て通常のanalysis.dbと区別がつかず、抽出・コピー処理は不要)
    con2 = at.get_connection(ds.ANALYSIS_DB_PATH)
    con2.close()
