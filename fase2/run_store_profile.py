"""
run_store_profile.py — 1店舗分のパイプラインを通しで実行し store_profile を更新する

preprocess.py(Stage0〜4) → patterns.py(幅型/深さ型/αブレンド) → score.py(S_稼働低さ・
合成・store_profile書き込み) を順に実行する。機能A/Bは既存の store_profile を読むだけ
なので、新しく取り込んだ店舗(store_profileテーブル未作成)や、データ更新後の再計算には
このスクリプトを実行する必要がある。

fase4(日次自動実行)が実装されるまでの間、このスクリプトを手動実行することで
機能B(振り返りダッシュボード/狙い目メモ)に店舗を反映できる。

実行方法:
    python run_store_profile.py                    # ホールデータ/ 配下の全DBを更新
    python run_store_profile.py --db yasuda7.db     # 特定DBのみ更新
"""
import argparse
import json
import sys
from pathlib import Path

import preprocess as pp
import patterns as pt
import score as sc

_DB_ROOT = Path(__file__).parent.parent / 'ホールデータ'


def _hole_name_for_db(db_path: Path) -> str | None:
    import sqlite3
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute('SELECT DISTINCT ホール名 FROM slot_data').fetchall()
    except Exception:
        return None
    finally:
        con.close()
    return rows[0][0] if len(rows) == 1 else None


def _existing_gamma_store(db_path: Path, hole_name: str) -> float | None:
    import sqlite3
    con = sqlite3.connect(str(db_path))
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if 'store_profile' not in tables:
            return None
        row = con.execute(
            'SELECT gamma_store FROM store_profile WHERE ホール名 = ? LIMIT 1', (hole_name,)
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        con.close()


def run_for_db(db_path: str, hole_name: str | None = None) -> None:
    """指定DBのslot_dataから store_profile を再計算して書き込む。"""
    db_path_p = Path(db_path)
    if hole_name is None:
        hole_name = _hole_name_for_db(db_path_p)
    if hole_name is None:
        print(f'  [スキップ] {db_path_p.name}: ホール名を特定できません。')
        return

    df = pp.load_slot_data(str(db_path_p), hole_name)
    if df.empty:
        print(f'  [スキップ] {db_path_p.name}: slot_dataが空です。')
        return
    df = pp.normalize(df)

    machine_tier, bias_params, column_map = pp.calibrate_all(df)
    specs = pp._load_specs()
    scored = pp.compute_all_logLR(df, machine_tier, bias_params, specs, column_map)
    scored = pp.compute_log_odds(scored)
    scored = pp.mark_invalid(scored, machine_tier, specs)

    events_df = pt.detect_all_events(scored)
    scored = pt.compute_breadth_scores(scored, events_df)
    scored = pt.compute_depth_scores(scored)

    alphas = pt.learn_all_alphas(scored, hole_name)
    for score_col in pt.BLENDABLE_SCORES:
        short = pt.compute_short_term_score(scored, score_col)
        scored[score_col] = pt.blend(scored[score_col], short, alphas[score_col])

    scored['S_稼働低さ'] = sc.score_kadou_hikusha(scored, hole_name)

    weights = pp.load_weights(str(pp.WEIGHTS_PATH)) if pp.WEIGHTS_PATH.exists() else {}
    synthesized = sc.synthesize(scored, weights)

    gamma_store = _existing_gamma_store(db_path_p, hole_name)
    sc.update_store_profile(str(db_path_p), hole_name, synthesized, gamma_store=gamma_store)
    print(f'  [完了] {hole_name}: store_profile を更新しました({len(synthesized):,}行)。')


def main() -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description='store_profile を再計算して更新する')
    parser.add_argument('--db', default=None, help='ホールデータ/ 配下のDBファイル名(省略時は全件)')
    args = parser.parse_args()

    if args.db:
        db_files = [_DB_ROOT / args.db]
    else:
        db_files = sorted(_DB_ROOT.glob('*.db')) if _DB_ROOT.exists() else []

    if not db_files:
        print('対象DBが見つかりません。')
        return

    print(f'{len(db_files)}件のDBを処理します。')
    for db_path in db_files:
        if not db_path.exists():
            print(f'  [スキップ] {db_path.name}: ファイルが存在しません。')
            continue
        run_for_db(str(db_path))


if __name__ == '__main__':
    main()
