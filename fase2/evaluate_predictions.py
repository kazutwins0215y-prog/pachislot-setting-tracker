"""
evaluate_predictions.py — prediction_log と実測差枚を突き合わせ、prediction_accuracy を更新する

機能B再設計 Stage7-2。prediction_log(patterns.predict_next_day_with_blend由来、
run_store_profile.pyが実行のたびに追記)の予測値と、レプリカ(turso_replica.db)の
実測差枚をJOINし、(ホール名, 予測種別)ごとにSpearman順位相関・Precision@N・
リフトを集計してprediction_accuracyへUPSERTする。

実行方法: python evaluate_predictions.py

[リーク禁止に関する注意] 本スクリプトは評価専用であり、実測値はここでのみ参照する。
patterns.py の予測計算(predict_next_day)には実測差枚を一切渡さない。
"""
import sys

import numpy as np
import pandas as pd
from scipy import stats

import data_source as ds

MIN_SAMPLES = 30  # Spearman相関を計算する最低サンプル数。未満は「検証中」扱い(暫定値、実データで調整)
TOP_N = 3         # Precision@N・リフトの上位N台(暫定値、実データで調整)

_CREATE_PREDICTION_ACCURACY_SQL = '''
    CREATE TABLE IF NOT EXISTS prediction_accuracy (
        ホール名       TEXT NOT NULL,
        予測種別       TEXT NOT NULL,
        サンプル数     INTEGER,
        spearman相関   REAL,
        precision_at_n REAL,
        リフト         REAL,
        集計期間開始   TEXT,
        集計期間終了   TEXT,
        更新日時       TEXT,
        PRIMARY KEY (ホール名, 予測種別)
    )
'''


def _load_predictions(analysis_db: str) -> pd.DataFrame:
    """prediction_logのうち、対象日が今日以前(実測データが存在しうる日)の行を読む。"""
    con = ds.connect_analysis(analysis_db)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if 'prediction_log' not in tables:
            return pd.DataFrame()
        today_str = pd.Timestamp.now().strftime('%Y-%m-%d')
        df = pd.read_sql_query(
            'SELECT * FROM prediction_log WHERE 対象日 <= ?', con, params=(today_str,)
        )
    finally:
        con.close()
    return df


def _load_actuals(replica_db: str, holes: list[str], min_date: str, max_date: str) -> pd.DataFrame:
    """レプリカのslot_dataから実測差枚を(日付, ホール名, 機種名, 台番号)単位で読む。"""
    con = ds.connect_replica(replica_db)
    try:
        placeholders = ','.join('?' for _ in holes)
        query = f'''
            SELECT 日付, ホール名, 機種名, 台番号, 差枚
            FROM slot_data
            WHERE ホール名 IN ({placeholders}) AND 日付 BETWEEN ? AND ?
        '''
        df = pd.read_sql_query(query, con, params=[*holes, min_date, max_date])
    finally:
        con.close()
    return df


def _precision_and_lift(day_grp: pd.DataFrame, top_n: int) -> tuple[float | None, float | None]:
    """
    1日分(1店舗×1対象日)の予測上位N台 vs 実測上位N台の重複率とリフトを返す。

    [2026-07-14 応急処置] リフトは比率(上位N台平均÷店舗平均)だと店舗平均差枚が0近傍の日に
    ±数百倍へ発散し平均が壊れるため(実測: -442〜+1369)、差枚差ベース
    (上位N台平均差枚 − 店舗平均差枚。単位: 枚)に変更。正なら予測上位台が店平均より優位。
    """
    if len(day_grp) < 2:
        return None, None
    n_top = min(top_n, len(day_grp))
    pred_top_idx = day_grp.nlargest(n_top, 'ブレンド値').index
    actual_top_idx = day_grp.nlargest(n_top, '差枚').index
    precision = len(set(pred_top_idx) & set(actual_top_idx)) / n_top

    store_avg = day_grp['差枚'].mean()
    pred_top_avg = day_grp.loc[pred_top_idx, '差枚'].mean()
    lift = None if pd.isna(store_avg) or pd.isna(pred_top_avg) else float(pred_top_avg - store_avg)
    return precision, lift


def evaluate(analysis_db: str | None = None, replica_db: str | None = None) -> None:
    analysis_db = analysis_db or str(ds.ANALYSIS_DB_PATH)
    replica_db = replica_db or str(ds.REPLICA_DB_PATH)

    preds = _load_predictions(analysis_db)
    if preds.empty:
        print('prediction_logに評価対象データがありません。')
        return

    holes = sorted(preds['ホール名'].unique())
    actuals = _load_actuals(replica_db, holes, preds['対象日'].min(), preds['対象日'].max())
    if actuals.empty:
        print('レプリカに対応する実測データ(slot_data)が見つかりません。')
        return

    merged = preds.merge(
        actuals,
        left_on=['対象日', 'ホール名', '機種名', '台番号'],
        right_on=['日付', 'ホール名', '機種名', '台番号'],
        how='inner',
    ).dropna(subset=['差枚', 'ブレンド値'])
    if merged.empty:
        print('prediction_logと実測データの突合結果が0件でした(まだ翌日データ未収集の可能性)。')
        return

    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    con = ds.connect_analysis(analysis_db)
    try:
        con.execute(_CREATE_PREDICTION_ACCURACY_SQL)
        for (hole, pred_type), grp in merged.groupby(['ホール名', '予測種別']):
            n = len(grp)
            if n >= MIN_SAMPLES:
                rho, _ = stats.spearmanr(grp['ブレンド値'], grp['差枚'])
                spearman = None if np.isnan(rho) else float(rho)
            else:
                spearman = None  # サンプル不足 = 検証中

            precisions, lifts = [], []
            for _, day_grp in grp.groupby('対象日'):
                precision, lift = _precision_and_lift(day_grp, TOP_N)
                if precision is not None:
                    precisions.append(precision)
                if lift is not None:
                    lifts.append(lift)

            precision_at_n = float(np.mean(precisions)) if precisions else None
            lift_avg = float(np.mean(lifts)) if lifts else None

            con.execute(
                '''
                INSERT INTO prediction_accuracy
                    (ホール名, 予測種別, サンプル数, spearman相関, precision_at_n, リフト,
                     集計期間開始, 集計期間終了, 更新日時)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ホール名, 予測種別) DO UPDATE SET
                    サンプル数=excluded.サンプル数,
                    spearman相関=excluded.spearman相関,
                    precision_at_n=excluded.precision_at_n,
                    リフト=excluded.リフト,
                    集計期間開始=excluded.集計期間開始,
                    集計期間終了=excluded.集計期間終了,
                    更新日時=excluded.更新日時
                ''',
                (
                    hole, pred_type, n, spearman, precision_at_n, lift_avg,
                    str(grp['対象日'].min()), str(grp['対象日'].max()), now,
                ),
            )
        con.commit()
    finally:
        con.close()

    print(f'prediction_accuracyを更新しました({merged["ホール名"].nunique()}店舗、{len(merged)}件突合)。')


def main() -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    evaluate()


if __name__ == '__main__':
    main()
