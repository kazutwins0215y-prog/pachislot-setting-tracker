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


def _run_zentaikei_judgment(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> int:
    """
    [2026-07-09設計合意] 機種×日の全台系/高配分の日次判定(patterns.score_zentaikei_judgment)を
    machine_judgment_logへ記録する(今後の実装予定.md 1.8節 Phase1)。

    prediction_log系の翌日予測(直近1日のみ記録)と異なり、収集済み全履歴分をまとめて
    渡す(全台系イベントは月数回と稀なため記録を早く始める価値がある。当日完結の
    記述統計でリークの心配がないため過去分の再計算も問題ない。重複は
    write_machine_judgment_log側のPRIMARY KEYで行単位に吸収される)。
    πはStage3のβ₀と同じprior_high_ratio(load_channel_weights経由)を使う。
    上限キャリブレーション補正後のhigh_prob(scored)を入力にするため、
    compute_uplimitの後に呼ぶ(機能Aの表示値と揃えるため)。
    """
    prior = pp.load_channel_weights()['prior_high_ratio']
    judgment_df = pt.score_zentaikei_judgment(scored, prior)
    return sc.write_machine_judgment_log(analysis_db, judgment_df)


def _run_zentaikei_judgment_fisher(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> int:
    """
    [今後の実装予定.md 1.8.1節・2026-07-14実装] 機種×日の全台系/高配分判定の
    Fisher直接確率検定版(patterns.score_zentaikei_judgment_fisher)を
    machine_judgment_fisher_logへ並走記録する。n=2〜4の少台数機種で現行z検定より
    検出力が高いことを実データ(東中野等4店舗)で確認済み。0節の検証ゲートに従い
    並走記録専用(合成スコア・表示には使わない。既存のmachine_judgment_logは無変更)。
    """
    judgment_df = pt.score_zentaikei_judgment_fisher(scored)
    return sc.write_machine_judgment_fisher_log(analysis_db, judgment_df)


def _run_group_calendar_conditions(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> 'pd.DataFrame':
    """
    [今後の実装予定.md 1.8節「末尾版」フェーズ2] 台番号末尾グループのカレンダー構造検定
    (patterns.build_group_calendar_conditions)をgroup_calendar_conditionsへ保存する。

    machine_judgment_log(append-only)と異なり、teppan_conditionsと同じ「店舗単位で
    全削除→再挿入」方式(収集済み全履歴を毎回再検定するため、過去分の履歴を残す必要が
    ない)。上限キャリブレーション補正後のhigh_probを使うため、compute_uplimitの後
    (write_stage3_scoresと同じ位置)に呼ぶ。

    Returns: build_group_calendar_conditionsの生の結果DataFrame(空ならempty)。
    フェーズ3(_run_tail_group_predictions)が同じ結果を再利用するため、
    保存だけでなく呼び出し元へ返す。
    """
    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return pd.DataFrame()
    max_date = all_dates[-1]

    group_series = pt.tail_digit_group(scored['台番号'])
    result = pt.build_group_calendar_conditions(scored, hole_name, group_series)
    sc.write_group_calendar_conditions(
        analysis_db, hole_name, result, str(max_date), group_types='台番号末尾',
    )
    return result


def _run_machine_group_conditions(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> dict:
    """
    [今後の実装予定.md 1.8節「機種単位の癖分析」] グループ=機種で末尾版と同じ検出器
    (patterns.build_group_calendar_conditions)を看板機種検定(include_constant=True)込みで
    実行し、恒常窓(全期間)・直近窓(RECENT_TEST_WINDOW_DAYS日)の2本をgroup_calendar_conditions
    へ保存する(グループ種別'機種'/'機種_直近'。2026-07-10ユーザー合意の案A=2窓検定並走)。

    一致ルールは機種には意味がないため含めない(include_match_rules=False)。
    machine_judgment_log(append-only)と異なり、末尾版と同じ「グループ種別単位で
    全削除→再挿入」方式。上限キャリブレーション補正後のhigh_probを使うため、
    _run_group_calendar_conditionsと同じ位置(compute_uplimitの後)で呼ぶ。

    Returns: {'機種': DataFrame, '機種_直近': DataFrame}(フェーズ3の予測記録が再利用する)
    """
    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return {'機種': pd.DataFrame(), '機種_直近': pd.DataFrame()}
    max_date = all_dates[-1]

    group_series_full = pt.machine_group(scored)
    result_full = pt.build_group_calendar_conditions(
        scored, hole_name, group_series_full, group_type='機種',
        include_match_rules=False, include_constant=True,
    )
    sc.write_group_calendar_conditions(
        analysis_db, hole_name, result_full, str(max_date), group_types='機種',
    )

    recent_start = pd.to_datetime(max_date) - pd.Timedelta(days=int(pt.RECENT_TEST_WINDOW_DAYS - 1))
    recent_mask = pd.to_datetime(scored['日付']) >= recent_start
    scored_recent = scored.loc[recent_mask]
    group_series_recent = pt.machine_group(scored_recent)
    result_recent = pt.build_group_calendar_conditions(
        scored_recent, hole_name, group_series_recent, group_type='機種_直近',
        include_match_rules=False, include_constant=True,
    )
    sc.write_group_calendar_conditions(
        analysis_db, hole_name, result_recent, str(max_date), group_types='機種_直近',
    )

    return {'機種': result_full, '機種_直近': result_recent}


def _run_store_day_conditions(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> 'pd.DataFrame':
    """
    [今後の実装予定.md 1.9節「店舗×曜日(店全体レベル)の癖軸」] 店舗全体レベルの
    カレンダー構造検定(patterns.store_day_calendar_test)をgroup_calendar_conditionsへ
    保存する(グループ種別='店舗日')。末尾版・機種版と同じ「グループ種別単位で
    全削除→再挿入」方式(店舗単位で全履歴を毎回再検定するため過去分の履歴を残す必要がない)。

    Returns: store_day_calendar_testの生の結果DataFrame(空ならempty)。
    _run_store_day_predictionsが同じ結果を再利用するため、保存だけでなく呼び出し元へ返す。
    """
    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return pd.DataFrame()
    max_date = all_dates[-1]

    result = pt.store_day_calendar_test(scored, hole_name)
    sc.write_group_calendar_conditions(
        analysis_db, hole_name, result, str(max_date), group_types='店舗日',
    )
    return result


def _run_store_day_predictions(
    scored: 'pd.DataFrame',
    hole_name: str,
    result: 'pd.DataFrame',
    analysis_db: str,
) -> int:
    """
    [今後の実装予定.md 1.9節「店舗×曜日の癖軸」] S_店舗日の翌観測日予測をprediction_logへ
    追記する(並走記録のみ。合成スコア(狙い目度)へは混ぜない。S_末尾等と同じ運用)。

    店全体レベルの予測のため台ごとの区別はなく、翌日が有意条件に該当する場合は
    使用データ最終日に判定可能だった全台へ同じ値を書く(evaluate_predictions.pyが
    実測差枚と台単位で突合するため、末尾版/機種版と同じ粒度で保存する)。
    """
    if result.empty:
        return 0
    sig = result[result['BH有意']]
    if sig.empty:
        return 0

    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return 0
    max_date = all_dates[-1]
    next_date = pd.to_datetime(max_date) + pd.DateOffset(days=1)

    pred = pt.predict_store_day_next_day(next_date, sig)
    if pred is None:
        return 0

    hole_last_day = scored[(scored['ホール名'] == hole_name) & (scored['日付'] == max_date)]
    if 'is_invalid' in hole_last_day.columns:
        hole_last_day = hole_last_day[~hole_last_day['is_invalid'].fillna(True)]
    units = hole_last_day[['機種名', '台番号']].dropna().drop_duplicates()

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for _, u in units.iterrows():
        machine, unit = u['機種名'], int(u['台番号'])
        rows.append({
            '実行日時': now,
            '使用データ最終日': str(max_date),
            '対象日': next_date.strftime('%Y-%m-%d'),
            'ホール名': hole_name,
            '機種名': machine,
            '台番号': unit,
            '予測種別': 'S_店舗日',
            '長期スコア': pred['値'],
            '短期スコア': None,
            'ブレンド値': pred['値'],
            '使用alpha': 0.0,
            '詳細': {'該当条件': pred['該当条件']},
        })

    sc.write_prediction_log(analysis_db, rows)
    return len(rows)


def _refresh_machine_bias_list(analysis_db: str) -> list[str]:
    """
    [今後の実装予定.md 1.8.5節「機種バイアス除外・案A」] 全店舗横断で
    group_calendar_conditions(グループ種別='機種', 日付条件='恒常')を集計し直し、
    machine_bias_flagsを最新化してバイアス機種名リストを返す。

    store_profile.pyの1店舗分の処理(run_for_hole)から毎回呼ぶため、複数店舗を
    一括実行する場合はループが進むほど直近の検定結果を反映する(単一パス構成のまま、
    最終店舗以外は前回実行分のデータが混じる最大1日分の遅延を許容する設計)。
    集計自体は小さなSQL集約のみで軽量なため、店舗ごとに再計算しても実害はない。
    """
    conditions = sc.read_machine_constant_conditions(analysis_db)
    total_stores = sc.count_profiled_stores(analysis_db)
    bias_df = pt.identify_machine_bias(conditions, total_stores)
    sc.write_machine_bias_flags(analysis_db, bias_df)
    if bias_df.empty:
        return []
    return bias_df.loc[bias_df['バイアス判定'], '機種名'].tolist()


def _run_machine_group_predictions(
    scored: 'pd.DataFrame',
    hole_name: str,
    group_results: dict,
    analysis_db: str,
    bias_machines: list[str] | None = None,
) -> int:
    """
    [今後の実装予定.md 1.8節「機種単位の癖分析」] S_機種/S_機種_直近の翌観測日予測を
    prediction_logへ追記する(並走記録のみ。合成スコア(狙い目度)へは混ぜない。
    prediction_accuracyで的中実績を確認してから合流を判断する、S_末尾と同じ運用)。

    group_results(_run_machine_group_conditionsが返す、この店舗の恒常窓/直近窓それぞれの
    全仮説)のうちBH有意=Trueの行だけを使い、使用データ最終日に在籍していた全台について
    機種→台展開でpatterns.predict_machine_group_next_dayを計算する(台単位で書くのは
    evaluate_predictions.pyが実測差枚と台単位で突合するため、末尾版と同じ設計)。

    S_末尾と同様、長期/短期のブレンドは行わない(使用alpha=0.0固定)。

    [今後の実装予定.md 1.8.5節「機種バイアス除外・案A」] bias_machinesが渡された場合、
    通常のS_機種/S_機種_直近に加えて「恒常」条件のうちバイアス機種の行だけを除外した
    変種をS_機種_除外/S_機種_直近_除外として同じ期間に並走記録する(除外前後を
    prediction_accuracyで同一期間対比較するため、既存のS_機種は変更せず残す)。
    """
    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return 0
    max_date = all_dates[-1]
    next_date = pd.to_datetime(max_date) + pd.DateOffset(days=1)

    hole_last_day = scored[(scored['ホール名'] == hole_name) & (scored['日付'] == max_date)]
    if 'is_invalid' in hole_last_day.columns:
        hole_last_day = hole_last_day[~hole_last_day['is_invalid'].fillna(True)]
    units = hole_last_day[['機種名', '台番号']].dropna().drop_duplicates()

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for group_type, pred_type in (('機種', 'S_機種'), ('機種_直近', 'S_機種_直近')):
        group_result = group_results.get(group_type, pd.DataFrame())
        if group_result.empty:
            continue
        sig = group_result[group_result['BH有意']]
        if sig.empty:
            continue

        variants = [(pred_type, sig)]
        if bias_machines:
            is_biased_constant = (sig['日付条件'] == '恒常') & (sig['グループ'].isin(bias_machines))
            if is_biased_constant.any():
                variants.append((f'{pred_type}_除外', sig[~is_biased_constant]))

        for variant_pred_type, variant_sig in variants:
            if variant_sig.empty:
                continue
            for _, u in units.iterrows():
                machine, unit = u['機種名'], int(u['台番号'])
                pred = pt.predict_machine_group_next_day(machine, next_date, variant_sig)
                if pred is None:
                    continue
                rows.append({
                    '実行日時': now,
                    '使用データ最終日': str(max_date),
                    '対象日': next_date.strftime('%Y-%m-%d'),
                    'ホール名': hole_name,
                    '機種名': machine,
                    '台番号': unit,
                    '予測種別': variant_pred_type,
                    '長期スコア': pred['値'],
                    '短期スコア': None,
                    'ブレンド値': pred['値'],
                    '使用alpha': 0.0,
                    '詳細': {'該当条件': pred['該当条件']},
                })

    sc.write_prediction_log(analysis_db, rows)
    return len(rows)


def _run_machine_bias_calibrated_predictions(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> int:
    """
    [今後の実装予定.md 1.8.5節「機種バイアス除外・案B」] multi_store.pyが全店舗横断で
    学習した機種ベースラインδ(machine_bias_delta.json)でlog_oddsを較正した
    high_probを使い、機種恒常検定→S_機種_較正の翌観測日予測を並走記録する
    (合成スコア・表示には一切使わない実験軸。案Aと同一期間でprediction_accuracyの
    spearman/リフトを比較するのが目的)。

    δ未学習(ファイル未作成、または対象機種なし)の場合は較正が無意味(全機種delta=0で
    通常のS_機種とほぼ同じ結果になる)なため記録自体をスキップする。
    """
    delta_map = pp.load_machine_bias_delta()
    if not delta_map:
        return 0

    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return 0
    max_date = all_dates[-1]

    calibrated = scored.copy()
    offset = calibrated['機種名'].map(delta_map).fillna(0.0).to_numpy(dtype=float)
    calibrated_log_odds = calibrated['log_odds'].to_numpy(dtype=float) - offset
    calibrated['high_prob'] = pp.sigmoid(calibrated_log_odds)

    group_series = pt.machine_group(calibrated)
    result = pt.build_group_calendar_conditions(
        calibrated, hole_name, group_series, group_type='機種_較正',
        include_match_rules=False, include_constant=True,
    )
    sc.write_group_calendar_conditions(
        analysis_db, hole_name, result, str(max_date), group_types='機種_較正',
    )
    if result.empty:
        return 0
    sig = result[result['BH有意']]
    if sig.empty:
        return 0

    next_date = pd.to_datetime(max_date) + pd.DateOffset(days=1)
    hole_last_day = calibrated[(calibrated['ホール名'] == hole_name) & (calibrated['日付'] == max_date)]
    if 'is_invalid' in hole_last_day.columns:
        hole_last_day = hole_last_day[~hole_last_day['is_invalid'].fillna(True)]
    units = hole_last_day[['機種名', '台番号']].dropna().drop_duplicates()

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for _, u in units.iterrows():
        machine, unit = u['機種名'], int(u['台番号'])
        pred = pt.predict_machine_group_next_day(machine, next_date, sig)
        if pred is None:
            continue
        rows.append({
            '実行日時': now,
            '使用データ最終日': str(max_date),
            '対象日': next_date.strftime('%Y-%m-%d'),
            'ホール名': hole_name,
            '機種名': machine,
            '台番号': unit,
            '予測種別': 'S_機種_較正',
            '長期スコア': pred['値'],
            '短期スコア': None,
            'ブレンド値': pred['値'],
            '使用alpha': 0.0,
            '詳細': {'該当条件': pred['該当条件']},
        })

    sc.write_prediction_log(analysis_db, rows)
    return len(rows)


def _run_introduction_conditions(
    scored: 'pd.DataFrame',
    hole_name: str,
    analysis_db: str,
) -> tuple['pd.DataFrame', 'pd.DataFrame']:
    """
    [今後の実装予定.md 1.8.3節「導入後カーブ」] 機種レベルイベント(新台/増台/減台/
    再導入/純移動)を検出し、導入後カーブの検定結果をgroup_calendar_conditionsへ
    保存する(グループ種別'導入後')。teppan_conditions/末尾版/機種版と同じ
    「店舗単位で全削除→再挿入」方式。上限キャリブレーション補正後のhigh_probを
    使うため、compute_uplimitの後(write_stage3_scoresと同じ位置)で呼ぶ。

    Returns: (検定結果DataFrame, イベント検出結果DataFrame)
    フェーズ3(_run_introduction_predictions)が両方を再利用するため呼び出し元へ返す。
    """
    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return pd.DataFrame(), pd.DataFrame()
    max_date = all_dates[-1]

    events = pt.detect_introduction_events(scored, hole_name)
    sc.write_introduction_events(analysis_db, hole_name, events)
    result = pt.introduction_curve_test(scored, hole_name, events)
    sc.write_group_calendar_conditions(
        analysis_db, hole_name, result, str(max_date), group_types='導入後',
    )
    return result, events


def _run_introduction_predictions(
    scored: 'pd.DataFrame',
    hole_name: str,
    condition_result: 'pd.DataFrame',
    events: 'pd.DataFrame',
    analysis_db: str,
) -> int:
    """
    [今後の実装予定.md 1.8.3節「導入後カーブ」] S_導入後の翌観測日予測を
    prediction_logへ追記する(並走記録のみ。合成スコアへは混ぜない。
    prediction_accuracyで的中実績を確認してから合流を判断する、
    S_末尾/S_機種と同じ運用)。

    機種ごとに最新のイベント(判別不能を除く5カテゴリのうち日付が最も新しいもの。
    introduction_curve_testのウィンドウ打ち切りと同じ「次のイベントで前のイベントの
    影響を打ち切る」考え方に合わせ、最新イベントのみを予測に使う)を特定し、
    翌観測日までの経過日数(暦日)が14日未満ならS_導入後を予測する。
    純移動はイベント時に実際に移動した台のみ、それ以外4カテゴリは使用データ最終日
    時点でその機種に在籍する全台へ同じ予測値を書く(introduction_curve_testの
    検定対象が台単位/機種単位のどちらかに合わせた粒度)。
    """
    if condition_result.empty or events.empty:
        return 0
    sig = condition_result[condition_result['BH有意']]
    if sig.empty:
        return 0

    non_censored = events[events['カテゴリ'] != '判別不能']
    if non_censored.empty:
        return 0

    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return 0
    max_date = all_dates[-1]
    next_date = pd.to_datetime(max_date) + pd.DateOffset(days=1)

    latest_events = non_censored.loc[non_censored.groupby('機種名')['日付'].idxmax()]

    hole_last_day = scored[(scored['ホール名'] == hole_name) & (scored['日付'] == max_date)]
    if 'is_invalid' in hole_last_day.columns:
        hole_last_day = hole_last_day[~hole_last_day['is_invalid'].fillna(True)]
    units_by_machine = (
        hole_last_day[['機種名', '台番号']].dropna().drop_duplicates()
        .groupby('機種名')['台番号'].apply(lambda s: sorted(int(u) for u in s))
    )

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for _, ev in latest_events.iterrows():
        machine = ev['機種名']
        category = ev['カテゴリ']
        elapsed = (next_date - pd.Timestamp(ev['日付'])).days
        pred = pt.predict_introduction_next_day(category, elapsed, sig)
        if pred is None:
            continue

        if category == '純移動':
            target_units = ev['移動台番号リスト']
            if not isinstance(target_units, (list, np.ndarray)) or len(target_units) == 0:
                continue
            target_units = [int(u) for u in target_units]
        else:
            target_units = units_by_machine.get(machine, [])

        for unit in target_units:
            rows.append({
                '実行日時': now,
                '使用データ最終日': str(max_date),
                '対象日': next_date.strftime('%Y-%m-%d'),
                'ホール名': hole_name,
                '機種名': machine,
                '台番号': unit,
                '予測種別': 'S_導入後',
                '長期スコア': pred['値'],
                '短期スコア': None,
                'ブレンド値': pred['値'],
                '使用alpha': 0.0,
                '詳細': {'該当条件': pred['該当条件']},
            })

    sc.write_prediction_log(analysis_db, rows)
    return len(rows)


def _run_tail_group_predictions(
    scored: 'pd.DataFrame',
    hole_name: str,
    group_result: 'pd.DataFrame',
    analysis_db: str,
) -> int:
    """
    [今後の実装予定.md 1.8節「末尾版」フェーズ3] S_末尾の翌観測日予測をprediction_logへ
    追記する(並走記録のみ。合成スコア(狙い目度)へは混ぜない。prediction_accuracyで
    的中実績を確認してから合流を判断する、遷移予測と同じ運用)。

    group_result(_run_group_calendar_conditionsが返す、この店舗の全仮説)のうち
    BH有意=Trueの行だけを使い、使用データ最終日に判定可能だった全台について
    patterns.predict_tail_group_next_dayで翌観測日の予測値(該当する有意条件の
    max効果量。重複統合込み)を計算する。

    S_鉄板台等と異なり長期/短期のブレンドは行わない(2026-07-10確定設計「検出窓は
    全期間で開始」。短期窓での再検定は今回スコープ外のため使用alpha=0.0固定、
    長期スコア=ブレンド値として記録する)。
    """
    if group_result.empty:
        return 0
    sig = group_result[group_result['BH有意']]
    if sig.empty:
        return 0

    all_dates = sorted(scored['日付'].dropna().unique())
    if not all_dates:
        return 0
    max_date = all_dates[-1]
    next_date = pd.to_datetime(max_date) + pd.DateOffset(days=1)

    hole_last_day = scored[(scored['ホール名'] == hole_name) & (scored['日付'] == max_date)]
    if 'is_invalid' in hole_last_day.columns:
        hole_last_day = hole_last_day[~hole_last_day['is_invalid'].fillna(True)]
    units = hole_last_day[['機種名', '台番号']].dropna().drop_duplicates()

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for _, u in units.iterrows():
        machine, unit = u['機種名'], int(u['台番号'])
        pred = pt.predict_tail_group_next_day(unit, next_date, sig)
        if pred is None:
            continue
        rows.append({
            '実行日時': now,
            '使用データ最終日': str(max_date),
            '対象日': next_date.strftime('%Y-%m-%d'),
            'ホール名': hole_name,
            '機種名': machine,
            '台番号': unit,
            '予測種別': 'S_末尾',
            '長期スコア': pred['値'],
            '短期スコア': None,
            'ブレンド値': pred['値'],
            '使用alpha': 0.0,
            '詳細': {'該当条件': pred['該当条件']},
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

    # [2026-07-09設計合意] 機種×日の全台系/高配分判定をmachine_judgment_logへ記録
    # (上限キャリブレーション補正後のhigh_probを使うため、write_stage3_scoresと同じ位置)
    n_judgment = _run_zentaikei_judgment(scored, hole_name, analysis_db)
    # [1.8.1節・2026-07-14実装] 同判定のFisher直接確率検定版(少台数機種向け)を並走記録
    n_judgment_fisher = _run_zentaikei_judgment_fisher(scored, hole_name, analysis_db)

    # [今後の実装予定.md 1.8節「末尾版」フェーズ2] 台番号末尾グループのカレンダー構造検定を保存
    group_calendar_result = _run_group_calendar_conditions(scored, hole_name, analysis_db)
    n_group_calendar = int(group_calendar_result['BH有意'].sum()) if not group_calendar_result.empty else 0
    # [今後の実装予定.md 1.8節「末尾版」フェーズ3] S_末尾の翌観測日予測をprediction_logへ追記
    n_tail_pred = _run_tail_group_predictions(scored, hole_name, group_calendar_result, analysis_db)

    # [今後の実装予定.md 1.9節「店舗×曜日の癖軸」] 店舗全体レベルのカレンダー構造検定を保存し、
    # S_店舗日の翌観測日予測をprediction_logへ追記(並走記録のみ。合成スコアには混ぜない)
    store_day_result = _run_store_day_conditions(scored, hole_name, analysis_db)
    n_store_day_sig = int(store_day_result['BH有意'].sum()) if not store_day_result.empty else 0
    n_store_day_pred = _run_store_day_predictions(scored, hole_name, store_day_result, analysis_db)

    # [今後の実装予定.md 1.8節「機種単位の癖分析」] 看板機種+機種カレンダー癖を
    # 恒常窓/直近90日窓の2窓で検定・保存し、S_機種/S_機種_直近の翌観測日予測を追記
    machine_group_results = _run_machine_group_conditions(scored, hole_name, analysis_db)
    n_machine_calendar = sum(
        int(r['BH有意'].sum()) for r in machine_group_results.values() if not r.empty
    )
    # [今後の実装予定.md 1.8.5節「機種バイアス除外」] 案A: 全店舗横断の恒常バイアス機種を
    # 再集計し、除外版(S_機種_除外等)を並走記録。案B: δ較正版(S_機種_較正)を並走記録
    bias_machines = _refresh_machine_bias_list(analysis_db)
    n_machine_pred = _run_machine_group_predictions(
        scored, hole_name, machine_group_results, analysis_db, bias_machines=bias_machines,
    )
    n_machine_calibrated_pred = _run_machine_bias_calibrated_predictions(
        scored, hole_name, analysis_db
    )

    # [今後の実装予定.md 1.8.3節「導入後カーブ」] 機種レベルイベント(新台/増台/減台/
    # 再導入/純移動)を検出し、導入後カーブを検定・保存。S_導入後の翌観測日予測を追記
    introduction_result, introduction_events = _run_introduction_conditions(
        scored, hole_name, analysis_db
    )
    n_introduction_sig = (
        int(introduction_result['BH有意'].sum()) if not introduction_result.empty else 0
    )
    n_introduction_pred = _run_introduction_predictions(
        scored, hole_name, introduction_result, introduction_events, analysis_db
    )

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
        f'据え置き予測{n_sueki}台、機種判定ログ新規{n_judgment}件(Fisher版{n_judgment_fisher}件)、'
        f'末尾版有意{n_group_calendar}件・予測{n_tail_pred}台、'
        f'店舗日有意{n_store_day_sig}件・予測{n_store_day_pred}台、'
        f'機種版有意{n_machine_calendar}件・予測{n_machine_pred}台'
        f'(バイアス機種{len(bias_machines)}件・較正予測{n_machine_calibrated_pred}台)、'
        f'導入後有意{n_introduction_sig}件・予測{n_introduction_pred}台)。'
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
