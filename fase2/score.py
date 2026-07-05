"""
score.py — スコア統合・稼働推定・店舗プロファイル管理

S_稼働低さ (score_kadou_hikusha):
    店舗全体の回転数集計から混雑度の低さを自動推定(手入力不要)
    ※ 個台チャンネル③(Stage2の役割②)とは目的が異なる

合成スコア (synthesize):
    狙い目度 = Σ(wᵢ×Sᵢ) ÷ Σ(wᵢ)
    NaN(データ不足)のサブスコアは「0点」ではなく「除外」して再正規化

店舗プロファイル (update_store_profile):
    振り返り分析ごとに store_profile テーブルを再計算・更新
    γ_store は multi_store.py の Stage5 で学習されたら保存される

依存: patterns.py (各サブスコア列), preprocess.py (回転数列)
"""
import sqlite3
import json
import numpy as np
import pandas as pd
from pathlib import Path

WEIGHTS_PATH = Path(__file__).parent / 'weights.json'

SUB_SCORES = [
    'S_全台系', 'S_鉄板台', 'S_ローテ', 'S_新台増台',
    'S_移動台', 'S_据え置き', 'S_稼働低さ',
]

# store_profile テーブルのパターンキー → サブスコア列名
_PATTERN_MAP = {
    's_all':     'S_全台系',
    's_teppan':  'S_鉄板台',
    's_rote':    'S_ローテ',
    's_shintai': 'S_新台増台',
    's_idoudai': 'S_移動台',
    's_sueki':   'S_据え置き',
    's_kadou':   'S_稼働低さ',
}


def score_kadou_hikusha(df: pd.DataFrame, hole_name: str) -> pd.Series:
    """
    S_稼働低さ: 店舗全体の回転数集計から混雑度の低さを 0〜1 で返す。
    基準値 = 過去の同条件(同曜日等)の平均。

    全台合計回転数が基準値より低い日 → スコア高 (稼働が少なく参加しやすい)
    全台合計回転数が基準値以上の日   → スコア 0
    """
    scores = pd.Series(np.nan, index=df.index)
    mask = df['ホール名'] == hole_name
    sub = df[mask]

    if sub.empty or '回転数' not in sub.columns:
        return scores

    # 日次合計回転数（全台の合計 = 店舗稼働の代理指標）
    daily_total = (
        sub.dropna(subset=['回転数'])
        .groupby('日付')['回転数']
        .sum()
    )
    if daily_total.empty:
        return scores

    daily_df = daily_total.reset_index()
    daily_df.columns = ['日付', '合計回転数']
    daily_df['曜日'] = pd.to_datetime(daily_df['日付'], errors='coerce').dt.dayofweek

    # 曜日ごとの基準値（同曜日全期間の平均）
    dow_baseline = daily_df.groupby('曜日')['合計回転数'].mean()
    daily_df['基準値'] = daily_df['曜日'].map(dow_baseline)
    # 曜日データが取れない場合は全体平均にフォールバック
    daily_df['基準値'] = daily_df['基準値'].fillna(daily_df['合計回転数'].mean())

    # S_稼働低さ = clip(1 - 今日の合計 / 基準値, 0, 1)
    daily_df['S_稼働低さ'] = np.where(
        daily_df['基準値'] > 0,
        (1.0 - daily_df['合計回転数'] / daily_df['基準値']).clip(0.0, 1.0),
        np.nan,
    )

    score_map = dict(zip(daily_df['日付'], daily_df['S_稼働低さ']))
    scores.loc[mask] = sub['日付'].map(score_map).values

    return scores


def compute_reliability(df: pd.DataFrame, score_col: str) -> float:
    """
    指定サブスコアの信頼度を計算する(履歴日数・サンプル数ベース)。
    is_biased フラグが立っている場合は値を下げる。

    - 30日以上のデータで day_factor = 1.0
    - 50サンプル以上で sample_factor = 1.0
    - 最終スコア = day_factor×0.7 + sample_factor×0.3
    """
    if score_col not in df.columns:
        return 0.0

    non_nan_idx = df[score_col].dropna().index
    n_samples = len(non_nan_idx)
    if n_samples == 0:
        return 0.0

    n_days = (
        df.loc[non_nan_idx, '日付'].nunique()
        if '日付' in df.columns
        else n_samples
    )

    day_factor = min(1.0, n_days / 30.0)
    sample_factor = min(1.0, n_samples / 50.0)
    reliability = day_factor * 0.7 + sample_factor * 0.3

    # 欠損偏りフラグによるペナルティ
    if 'is_biased' in df.columns:
        biased_rate = float(df['is_biased'].fillna(False).mean())
        reliability *= max(0.0, 1.0 - biased_rate)

    return float(np.clip(reliability, 0.0, 1.0))


def synthesize(df: pd.DataFrame, weights: dict) -> pd.DataFrame:
    """
    Σ(wᵢ×Sᵢ) ÷ Σ(wᵢ) を計算。
    Sᵢ が NaN の行は分子・分母とも除外して再正規化する。

    追加列:
      狙い目度       — 加重平均スコア (0〜1 または NaN)
      有効サブスコア数 — 各行で計算に使われたサブスコアの個数
    """
    out = df.copy()

    available = [s for s in SUB_SCORES if s in df.columns]

    numerator = pd.Series(0.0, index=df.index)
    denominator = pd.Series(0.0, index=df.index)
    valid_count = pd.Series(0, index=df.index)

    for score_col in available:
        w = float(weights.get(score_col, 1.0))
        valid_mask = df[score_col].notna()
        numerator[valid_mask] += w * df.loc[valid_mask, score_col]
        denominator[valid_mask] += w
        valid_count[valid_mask] += 1

    out['狙い目度'] = np.where(denominator > 0, numerator / denominator, np.nan)
    out['有効サブスコア数'] = valid_count

    return out


def update_store_profile(
    db_path: str,
    hole_name: str,
    df_scored: pd.DataFrame,
    gamma_store: float | None = None,
) -> None:
    """
    store_profile テーブルを最新サブスコア・信頼度で上書き更新する。
    gamma_store は multi_store.py で学習された値(未学習時はNone)。

    テーブル構造:
      ホール名 TEXT, パターン TEXT, スコア REAL,
      信頼度 REAL, gamma_store REAL, 更新日時 TEXT
      PRIMARY KEY (ホール名, パターン)
    """
    now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []

    for pattern, score_col in _PATTERN_MAP.items():
        if score_col not in df_scored.columns:
            continue
        score_mean = df_scored[score_col].dropna().mean()
        score_val = None if pd.isna(score_mean) else float(score_mean)
        reliability = compute_reliability(df_scored, score_col)
        rows.append((hole_name, pattern, score_val, reliability, gamma_store, now))

    if not rows:
        return

    con = sqlite3.connect(db_path)
    try:
        con.execute('''
            CREATE TABLE IF NOT EXISTS store_profile (
                ホール名    TEXT,
                パターン   TEXT,
                スコア     REAL,
                信頼度     REAL,
                gamma_store REAL,
                更新日時   TEXT,
                PRIMARY KEY (ホール名, パターン)
            )
        ''')
        con.executemany(
            '''
            INSERT OR REPLACE INTO store_profile
                (ホール名, パターン, スコア, 信頼度, gamma_store, 更新日時)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            rows,
        )
        con.commit()
    finally:
        con.close()
