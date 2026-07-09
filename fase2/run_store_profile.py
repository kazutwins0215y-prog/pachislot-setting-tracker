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

import numpy as np
import pandas as pd

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


def _run_teppan_predictions(
    scored: 'pd.DataFrame',
    hole_name: str,
    teppan_details: list[dict],
    analysis_db: str,
) -> None:
    """
    [Stage7-1] S_鉄板台の翌観測日予測をprediction_logへ追記する。

    teppan_details(patterns.score_teppandaiが検出した台のみ)から台ごとに
    カレンダー条件・確認済み周期をまとめ、長期/短期両方のhpでpredict_next_day_with_blend
    を呼んでブレンド値を記録する。検出されていない台(teppan_detailsに現れない台)は
    「予測不可」として対象外(Stage4-1と同じ、判定不能はNaN=記録しない方針)。
    """
    if not teppan_details:
        return

    cal_conditions: dict[tuple, list[dict]] = {}
    lags: dict[tuple, list[int]] = {}
    for d in teppan_details:
        key = (d['機種名'], int(d['台番号']))
        if d['経路'] == 'カレンダー':
            cal_conditions.setdefault(key, []).append({'条件': d['条件'], '効果量': d['効果量']})
        elif d.get('周期日数') is not None:
            lags.setdefault(key, [])
            lag = int(d['周期日数'])
            if lag not in lags[key]:
                lags[key].append(lag)

    detected_units = sorted(set(cal_conditions) | set(lags))
    if not detected_units:
        return

    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return
    max_date = all_dates[-1]
    # pd.Timedelta(days=1)はpandas2.2+numpy2.5環境でgeneric unit非推奨警告が出るためDateOffsetを使う
    next_date = pd.to_datetime(max_date) + pd.DateOffset(days=1)

    if len(all_dates) >= pt.SHORT_WINDOW_DEFAULT:
        cutoff = all_dates[-pt.SHORT_WINDOW_DEFAULT]
        short_df = scored[scored['日付'] >= cutoff]
    else:
        short_df = scored.iloc[0:0]

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for machine, unit in detected_units:
        hp_long = pt.build_observed_history(scored, hole_name, machine, unit)
        hp_short = pt.build_observed_history(short_df, hole_name, machine, unit)
        unit_lags = lags.get((machine, unit), [])
        unit_cal = cal_conditions.get((machine, unit), [])

        result = pt.predict_next_day_with_blend(hp_long, hp_short, unit_lags, unit_cal, next_date)
        if result['ブレンド値'] is None:
            continue

        rows.append({
            '実行日時': now,
            '使用データ最終日': str(max_date),
            '対象日': next_date.strftime('%Y-%m-%d'),
            'ホール名': hole_name,
            '機種名': machine,
            '台番号': unit,
            '予測種別': 'S_鉄板台',
            '長期スコア': result['長期スコア'],
            '短期スコア': result['短期スコア'],
            'ブレンド値': result['ブレンド値'],
            '使用alpha': result['使用alpha'],
            '詳細': {'周期日数': unit_lags, 'カレンダー条件': unit_cal},
        })

    sc.write_prediction_log(analysis_db, rows)


def _run_transition_predictions(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> tuple[int, int, dict | None]:
    """
    [Stage7-3] 遷移モデル(据え置き/上げ/下げ)による全台翌日予測をprediction_logへ追記する。

    S_鉄板台の翌日予測(検出済みの台のみ)と異なり、使用データ最終日に判定可能だった
    全台が対象(検出条件の有無に依存しない全台カバレッジの翌日予測)。
    予測値 = 当日high_prob×P(高→高) + (1-当日high_prob)×P(低→高)。
    遷移行列は長期(全履歴)・短期(直近SHORT_WINDOW_DEFAULT日)の両方で推定しαブレンドする。
    リーク禁止: 使用するのは使用データ最終日以前のhigh_probのみ(実測差枚は渡さない)。
    入力は上限キャリブレーション補正後のhigh_prob(呼び出し位置で保証)。

    [2026-07 タスク4] 無条件版(予測種別='遷移予測')に加え、前日(t-1)の実測差枚が
    店舗内上位2割かどうかで層別した条件付き版(予測種別='遷移予測_前日差枚')を
    並走記録する。層間差が有意な店舗のみ(pt.estimate_transition_matrix_stratifiedの
    '有意'フラグ)。無条件版はDELETE/UPDATEなしで従来どおり全店舗記録する
    (採否はevaluate_predictions.pyの(ホール名,予測種別)別集計で対比較する運用)。
    条件付き版に使う「当日の実測差枚」は使用データ最終日の確定値のみ
    (翌日の値は一切使わないためリークではない)。

    Returns: (無条件版の記録件数, 条件付き版の記録件数, 長期版遷移行列matrix_long。
    ペア数不足でNoneの場合あり)。matrix_longは呼び出し元がupdate_store_profileへ渡し、
    店舗の癖(据え/上げ/下げ)をstore_profileに保存する(2026-07 タスク3追記(c))。
    """
    matrix_long = pt.estimate_transition_matrix(scored, hole_name)

    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates or matrix_long is None:
        return 0, 0, matrix_long  # ペア数不足(新規店舗など)は予測不可として記録しない
    max_date = all_dates[-1]
    next_date = pd.to_datetime(max_date) + pd.DateOffset(days=1)

    if len(all_dates) >= pt.SHORT_WINDOW_DEFAULT:
        cutoff = all_dates[-pt.SHORT_WINDOW_DEFAULT]
        short_df = scored[scored['日付'] >= cutoff]
        matrix_short = pt.estimate_transition_matrix(short_df, hole_name)
    else:
        short_df = scored.iloc[0:0]
        matrix_short = None

    last_day = scored[(scored['日付'] == max_date) & (scored['ホール名'] == hole_name)]
    if 'is_invalid' in last_day.columns:
        last_day = last_day[~last_day['is_invalid'].fillna(True)]
    last_day = last_day.dropna(subset=['high_prob'])

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    detail_common = {'長期遷移': matrix_long, '短期遷移': matrix_short}
    rows = []
    for _, r in last_day.iterrows():
        p_today = float(r['high_prob'])
        result = pt.predict_transition_with_blend(p_today, matrix_long, matrix_short)
        if result['ブレンド値'] is None:
            continue
        rows.append({
            '実行日時': now,
            '使用データ最終日': str(max_date),
            '対象日': next_date.strftime('%Y-%m-%d'),
            'ホール名': hole_name,
            '機種名': r['機種名'],
            '台番号': int(r['台番号']),
            '予測種別': '遷移予測',
            '長期スコア': result['長期スコア'],
            '短期スコア': result['短期スコア'],
            'ブレンド値': result['ブレンド値'],
            '使用alpha': result['使用alpha'],
            '詳細': {**detail_common, '当日high_prob': p_today},
        })
    sc.write_prediction_log(analysis_db, rows)

    # [2026-07 タスク4] 前日差枚条件付き版(並走ログ)。層間差が有意な店舗のみ追加記録。
    strat_rows = []
    strat_long = pt.estimate_transition_matrix_stratified(scored, hole_name)
    if strat_long is not None and strat_long['有意']:
        strat_short = (
            pt.estimate_transition_matrix_stratified(short_df, hole_name)
            if not short_df.empty else None
        )
        threshold_today = pt.stratify_threshold_by_date(scored, hole_name).get(max_date)
        if threshold_today is not None:
            for _, r in last_day.iterrows():
                diff_today = r.get('差枚')
                if diff_today is None or pd.isna(diff_today):
                    continue  # 当日の実測差枚が無い台は条件判定不可のため対象外
                p_today = float(r['high_prob'])
                is_top_today = float(diff_today) >= threshold_today
                layer_key = '上位層' if is_top_today else '下位層'
                m_long = strat_long[layer_key]
                m_short = strat_short[layer_key] if strat_short is not None else None

                result = pt.predict_transition_with_blend(p_today, m_long, m_short)
                if result['ブレンド値'] is None:
                    continue
                strat_rows.append({
                    '実行日時': now,
                    '使用データ最終日': str(max_date),
                    '対象日': next_date.strftime('%Y-%m-%d'),
                    'ホール名': hole_name,
                    '機種名': r['機種名'],
                    '台番号': int(r['台番号']),
                    '予測種別': '遷移予測_前日差枚',
                    '長期スコア': result['長期スコア'],
                    '短期スコア': result['短期スコア'],
                    'ブレンド値': result['ブレンド値'],
                    '使用alpha': result['使用alpha'],
                    '詳細': {
                        '層': layer_key,
                        '分位閾値': strat_long['分位閾値'],
                        '検定p値': strat_long['検定p値'],
                        '当日high_prob': p_today,
                        '当日差枚': float(diff_today),
                        '長期遷移': {'上位層': strat_long['上位層'], '下位層': strat_long['下位層']},
                        '短期遷移': strat_short,
                    },
                })
    sc.write_prediction_log(analysis_db, strat_rows)

    return len(rows), len(strat_rows), matrix_long


def _run_sueki_predictions(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> int:
    """
    [2026-07 タスク3] S_据え置き(日次判定)の翌観測日予測をprediction_logへ追記する。

    対象: 使用データ最終日にr̄_t(長期版)が計算できた(NaNでない)全台。
    翌日投影 = r̄_t ×(当日high_probの台基準からの偏差)。台基準 = その台の
    SUEKI_WINDOW日窓内の平均high_prob。長期版(全履歴)・短期版(直近
    SHORT_WINDOW_DEFAULT日窓)のr̄_tをFIXED_ALPHAでブレンドする(短期不可=α実質0)。
    リーク禁止: 使用するのは使用データ最終日以前のhigh_probのみ(実測差枚は渡さない)。
    """
    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return 0
    max_date = all_dates[-1]
    next_date = pd.to_datetime(max_date) + pd.DateOffset(days=1)

    if len(all_dates) >= pt.SHORT_WINDOW_DEFAULT:
        cutoff = all_dates[-pt.SHORT_WINDOW_DEFAULT]
        short_df = scored[scored['日付'] >= cutoff]
    else:
        short_df = scored.iloc[0:0]

    hole_last_day = scored[(scored['ホール名'] == hole_name) & (scored['日付'] == max_date)]
    units = hole_last_day[['機種名', '台番号']].dropna().drop_duplicates()

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for _, u in units.iterrows():
        machine, unit = u['機種名'], int(u['台番号'])

        hp_long = pt.build_observed_history(scored, hole_name, machine, unit)
        r_arr_long = pt.sueki_daily_r(hp_long)
        r_long = float(r_arr_long[-1]) if len(r_arr_long) and not np.isnan(r_arr_long[-1]) else None
        if r_long is None:
            continue  # 長期版が計算不可(履歴不足)の台は予測対象外

        hp_short = pt.build_observed_history(short_df, hole_name, machine, unit)
        r_arr_short = pt.sueki_daily_r(hp_short) if not hp_short.empty else np.array([])
        r_short = (
            float(r_arr_short[-1])
            if len(r_arr_short) and not np.isnan(r_arr_short[-1]) else None
        )

        window_vals = hp_long.values[-pt.SUEKI_WINDOW:]
        baseline = float(np.nanmean(window_vals)) if not np.all(np.isnan(window_vals)) else None
        today_hp = float(hp_long.values[-1])
        if baseline is None:
            continue
        deviation = today_hp - baseline

        result = pt.predict_sueki_with_blend(r_long, r_short, deviation)
        if result['ブレンド値'] is None:
            continue

        rows.append({
            '実行日時': now,
            '使用データ最終日': str(max_date),
            '対象日': next_date.strftime('%Y-%m-%d'),
            'ホール名': hole_name,
            '機種名': machine,
            '台番号': unit,
            '予測種別': 'S_据え置き',
            '長期スコア': result['長期スコア'],
            '短期スコア': result['短期スコア'],
            'ブレンド値': result['ブレンド値'],
            '使用alpha': result['使用alpha'],
            '詳細': {
                'r値': r_long,
                '切断フラグ': bool(r_long < pt.SUEKI_DAILY_THRESHOLD),
                '当日偏差': deviation,
                '窓幅': pt.SUEKI_WINDOW,
            },
        })

    sc.write_prediction_log(analysis_db, rows)
    return len(rows)


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

    events_df = pt.detect_all_events(scored)
    scored = pt.compute_breadth_scores(scored, events_df)
    teppan_details: list[dict] = []
    scored = pt.compute_depth_scores(scored, teppan_details=teppan_details)
    # S_鉄板台の検出条件(どのカレンダー候補/周期で有意か)を保存。
    # 「明日は該当日か」の判断材料として機能Bが参照する
    sc.write_teppan_conditions(analysis_db, hole_name, teppan_details)
    # [Stage7-1] 検出済みの台について翌観測日のS_鉄板台スコアを予測しprediction_logへ追記
    # (使用データ最終日=scored内の実際の最大日付。リーク検証用に必ず実データから取得する)
    _run_teppan_predictions(scored, hole_name, teppan_details, analysis_db)
    # [2026-07 タスク3] S_据え置き(日次判定)の翌観測日予測をprediction_logへ追記
    n_sueki = _run_sueki_predictions(scored, hole_name, analysis_db)

    alphas = pt.learn_all_alphas(scored, hole_name)
    for score_col in pt.BLENDABLE_SCORES:
        short = pt.compute_short_term_score(scored, score_col)
        scored[score_col] = pt.blend(scored[score_col], short, alphas[score_col])

    scored['S_稼働低さ'] = sc.score_kadou_hikusha(scored, hole_name)

    # [店舗高設定上限モデル Step1] 店舗×日 E[high_prob]/N が店舗の実質上限を超える日を
    # 検出し、超過分だけ全台のlog_odds/high_probをin-placeで下方補正する(ハードキャップ
    # ではなく連続的なshrinkage)。patterns.pyのサブスコア(検出用信号)はこの直前の
    # 計算で確定済みなので影響を受けず、Stage3出力(high_prob)側にのみ効く。
    uplimit_result = sc.compute_uplimit(scored, hole_name)

    # Stage3出力を保存(機能Aの初期表示高速化・機能Bの「熱い台」が読む)。
    # 上限キャリブレーション補正後の値を保存するため、補正が終わったここで書き込む
    sc.write_stage3_scores(analysis_db, hole_name, scored)

    # [Stage7-3] 遷移モデルによる全台翌日予測。上限キャリブレーション補正後の
    # high_probを入力にするため、compute_uplimitの後に呼ぶ
    n_transition, n_transition_strat, matrix_long = _run_transition_predictions(
        scored, hole_name, analysis_db
    )

    weights = pp.load_weights(str(pp.WEIGHTS_PATH)) if pp.WEIGHTS_PATH.exists() else {}
    reliabilities = {
        col: sc.compute_reliability(scored, col)
        for col in sc.SUB_SCORES
        if col in scored.columns
    }
    synthesized = sc.synthesize(scored, weights, reliabilities=reliabilities)

    # [2026-07 タスク5] wᵢ学習用の日次スナップショットを記録(学習側は蓄積後の別タスク)。
    # 使用データ最終日の断面のみの店舗平均を残す(全期間平均ではない教師データ用の粒度)
    snapshot_dates = sorted(synthesized['日付'].dropna().unique())
    if snapshot_dates:
        snapshot_last_date = snapshot_dates[-1]
        last_day_rows = synthesized[synthesized['日付'] == snapshot_last_date]
        sub_score_means = {
            col: (None if pd.isna(last_day_rows[col].mean()) else float(last_day_rows[col].mean()))
            for col in sc.SUB_SCORES if col in last_day_rows.columns
        }
        target_mean_raw = last_day_rows['狙い目度'].mean()
        target_mean = None if pd.isna(target_mean_raw) else float(target_mean_raw)
        effective_weights = {
            col: float(weights.get(col, 1.0)) * float(reliabilities.get(col, 1.0))
            for col in sc.SUB_SCORES if col in scored.columns
        }
        sc.write_score_snapshot(
            analysis_db, hole_name, str(snapshot_last_date),
            sub_score_means, target_mean, effective_weights,
        )

    gamma_store = _existing_gamma_store(analysis_db, hole_name)
    sc.update_store_profile(
        analysis_db, hole_name, synthesized, gamma_store=gamma_store,
        uplimit_value=uplimit_result['上限キャリブレーション値'],
        uplimit_reliability=uplimit_result['上限信頼度'],
        transition_matrix_long=matrix_long,
    )
    # store_profileは最新1行のみ上書きのため、検出期間の履歴はpattern_historyに追記して残す
    sc.write_pattern_history(analysis_db, hole_name, synthesized)
    print(
        f'  [完了] {hole_name}: stage3_scores / store_profile を更新しました'
        f'({len(synthesized):,}行、上限キャリブレーション発動'
        f'{uplimit_result["発動日数"]}/{uplimit_result["対象日数"]}日、'
        f'遷移予測{n_transition}台(前日差枚条件付き{n_transition_strat}台)、'
        f'据え置き予測{n_sueki}台)。'
    )


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
