"""
streamlit_app.py — Streamlit Community Cloudのデプロイエントリポイント

bootstrap(生データDB・分析DBの埋め込みレプリカsync)を完了させてから
fase2/app.py(ホームページ⇔店舗トップページの2ページ構成)をimportして起動する。
デプロイ設定ではこのファイルをメインファイルに指定する。

設計: fase3/配信公開_設計.md「クラウド側設計」節
仕様: fase3/実装指示書.md タスク3

制約: fase2/app.pyがモジュールレベルでst.set_page_config()を呼ぶため、
bootstrapはst系のUI要素を一切出さずに完了させる(@st.cache_resourceで包み、
show_spinner=Falseで進捗表示も抑止する)。データ更新は1日1回(朝)なので、
キャッシュTTLは60分とする。

[重要] `import app` は最初のリランでのみ実行され、以降はPythonのモジュール
キャッシュにより再実行されない。`streamlit run app.py` を直接叩く既存運用では
Streamlitが毎リラン時にメインスクリプトを丸ごと再実行するため気付かないが、
このラッパー経由だとホーム⇔店舗ページの遷移やapp_a/app_bのウィジェット操作が
2回目以降効かなくなる。そのため`app`モジュールだけは`importlib.reload`で
毎リラン強制的に再実行する(app_a/app_b/app_topは関数定義のみでモジュール
トップレベルにst呼び出しを持たないため、reload対象はappだけで足りる)。
"""
import importlib
import sys
from pathlib import Path

import streamlit as st

_BASE = Path(__file__).resolve().parent.parent
for _dir in (_BASE / 'fase1', _BASE / 'fase2', _BASE / 'fase3'):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import bootstrap  # noqa: E402


@st.cache_resource(ttl=3600, show_spinner=False)
def _bootstrap_once() -> None:
    bootstrap.run()


_bootstrap_once()

# fase2/app.py。st.set_page_config()を含む本体をここで起動する(毎リラン再実行)
if 'app' in sys.modules:
    importlib.reload(sys.modules['app'])
else:
    import app  # noqa: E402
