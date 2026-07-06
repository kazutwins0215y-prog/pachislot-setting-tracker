"""
run_store_profile.py — 1店舗分のパイプラインを通しで実行し分析DBを更新する

preprocess.py(Stage0〜4) → patterns.py(幅型/深さ型/αブレンド) → score.py(S_稼働低さ・
合成・store_profile/stage3_scores書き込み) を順に実行する。

データの流れ:
    ホールデータ/turso_replica.db (fase1が維持するTursoレプリカ・読み取り専用)
        → 本スクリプトで再計算
        → ホールデータ/analysis.db (stage3_scores / store_profile)

機能A/Bは analysis.db を読むだけなので、データ更新後(fase1収集後)や
新規店舗取込時にはこのスクリプトを実行する必要がある。
fase4(日次自動実行)が実装されるまでの間の手動運用補助スクリプト。

実行方法:
    python run_store_profile.py                       # レプリカ内の全店舗を更新
    python run_store_profile.py --hole yasuda7        # 特定店舗のみ更新
"""
import argparse
import sys

import data_source as ds
import preprocess as pp
import patterns as pt
import score as sc


def _existing_gamma_store(analysis_db: str, hole_name: str) -> float | None:
    import sqlite3
    con = sqlite3.connect(analysis_db)
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


def run_for_hole(hole_name: str, replica_db: str | None = None, analysis_db: str | None = None) -> None:
    """指定店舗のslot_dataから stage3_scores / store_profile を再計算して書き込む。"""
    replica_db = replica_db or str(ds.REPLICA_DB_PATH)
    analysis_db = analysis_db or str(ds.ANALYSIS_DB_PATH)

    df = pp.load_slot_data(replica_db, hole_name)
    if df.empty:
        print(f'  [スキップ] {hole_name}: slot_dataが空です。')
        return
    df = pp.normalize(df)

    machine_tier, bias_params, column_map = pp.calibrate_all(df)
    specs = pp._load_specs()
    scored = pp.compute_all_logLR(df, machine_tier, bias_params, specs, column_map)
    scored = pp.compute_log_odds(scored)
    scored = pp.mark_invalid(scored, machine_tier, specs)

    # Stage3出力を保存(機能Aの初期表示高速化・機能Bの「熱い台」が読む)
    sc.write_stage3_scores(analysis_db, hole_name, scored)

    events_df = pt.detect_all_events(scored)
    scored = pt.compute_breadth_scores(scored, events_df)
    teppan_details: list[dict] = []
    scored = pt.compute_depth_scores(scored, teppan_details=teppan_details)
    # S_鉄板台の検出条件(どのカレンダー候補/周期で有意か)を保存。
    # 「明日は該当日か」の判断材料として機能Bが参照する
    sc.write_teppan_conditions(analysis_db, hole_name, teppan_details)

    alphas = pt.learn_all_alphas(scored, hole_name)
    for score_col in pt.BLENDABLE_SCORES:
        short = pt.compute_short_term_score(scored, score_col)
        scored[score_col] = pt.blend(scored[score_col], short, alphas[score_col])

    scored['S_稼働低さ'] = sc.score_kadou_hikusha(scored, hole_name)

    weights = pp.load_weights(str(pp.WEIGHTS_PATH)) if pp.WEIGHTS_PATH.exists() else {}
    synthesized = sc.synthesize(scored, weights)

    gamma_store = _existing_gamma_store(analysis_db, hole_name)
    sc.update_store_profile(analysis_db, hole_name, synthesized, gamma_store=gamma_store)
    print(f'  [完了] {hole_name}: stage3_scores / store_profile を更新しました({len(synthesized):,}行)。')


def main() -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description='stage3_scores / store_profile を再計算して更新する')
    parser.add_argument('--hole', default=None, help='店舗名(省略時はレプリカ内の全店舗)')
    args = parser.parse_args()

    try:
        holes = ds.list_holes()
    except FileNotFoundError as e:
        print(e)
        return

    if args.hole:
        if args.hole not in holes:
            print(f'店舗 {args.hole!r} がレプリカDBに見つかりません。存在する店舗: {holes}')
            return
        holes = [args.hole]

    print(f'{len(holes)}店舗を処理します。')
    for hole_name in holes:
        run_for_hole(hole_name)


if __name__ == '__main__':
    main()
