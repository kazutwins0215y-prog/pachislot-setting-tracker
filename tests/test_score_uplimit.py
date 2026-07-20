"""
score.py の稼働率・上限キャリブレーション関連純関数の回帰テスト。
score_kadou_hikusha / compute_daily_uplimit_ratio / _solve_uplimit_offset /
compute_uplimit(config明示・in-place副作用・データ不足フォールバック)。
"""
import numpy as np
import pandas as pd
import pytest

import score as sc


def _logit(p: float) -> float:
    return float(np.log(p / (1.0 - p)))


# ── score_kadou_hikusha ──────────────────────────────────────────

def test_score_kadou_hikusha_analytic_value_per_weekday_baseline():
    # 月曜(2026-06-01=月,2026-06-08=月): 合計[100,80] → 基準値(平均)=90
    #   2026-06-01: 1-100/90=-0.111→clip 0.0 / 2026-06-08: 1-80/90=0.11111111111111116
    # 火曜(2026-06-02=火,2026-06-09=火): 合計[200,150] → 基準値=175
    #   2026-06-02: 1-200/175=-0.1428→clip 0.0 / 2026-06-09: 1-150/175=0.1428571428571429
    df = pd.DataFrame({
        'ホール名': ['店A', '店A', '店A', '店A', '他店'],
        '日付': ['2026-06-01', '2026-06-08', '2026-06-02', '2026-06-09', '2026-06-01'],
        '回転数': [100, 80, 200, 150, 99999],
    })
    out = sc.score_kadou_hikusha(df, '店A')
    assert out.iloc[0] == pytest.approx(0.0)
    assert out.iloc[1] == pytest.approx(0.11111111111111116)
    assert out.iloc[2] == pytest.approx(0.0)
    assert out.iloc[3] == pytest.approx(0.1428571428571429)
    assert np.isnan(out.iloc[4])  # 他店の行はマスク対象外でNaNのまま


def test_score_kadou_hikusha_hole_not_present_returns_all_nan():
    df = pd.DataFrame({'ホール名': ['店B'], '日付': ['2026-06-01'], '回転数': [100]})
    out = sc.score_kadou_hikusha(df, '店A')
    assert out.isna().all()


def test_score_kadou_hikusha_missing_turns_column_returns_all_nan():
    df = pd.DataFrame({'ホール名': ['店A'], '日付': ['2026-06-01']})
    out = sc.score_kadou_hikusha(df, '店A')
    assert out.isna().all()


# ── compute_daily_uplimit_ratio ───────────────────────────────────

def test_compute_daily_uplimit_ratio_excludes_invalid_and_averages():
    # DayA: high_prob=[0.2,0.4,0.6] 全件有効 → 平均0.4
    # DayB: high_prob=[0.9,0.1,0.5] 中央(0.1)がis_invalid=True → 除外 → (0.9+0.5)/2=0.7
    df = pd.DataFrame({
        'ホール名': ['店U'] * 6 + ['他店'],
        '日付': ['A', 'A', 'A', 'B', 'B', 'B', 'A'],
        'high_prob': [0.2, 0.4, 0.6, 0.9, 0.1, 0.5, 0.99],
        'is_invalid': [False, False, False, False, True, False, False],
    })
    out = sc.compute_daily_uplimit_ratio(df, '店U')
    assert out['A'] == pytest.approx(0.4)
    assert out['B'] == pytest.approx(0.7)
    assert 'A' in out.index and len(out) == 2  # 他店の行が混入していない


def test_compute_daily_uplimit_ratio_drops_nan_high_prob():
    df = pd.DataFrame({
        'ホール名': ['店U'] * 3,
        '日付': ['A', 'A', 'A'],
        'high_prob': [0.2, np.nan, 0.6],
        'is_invalid': [False, False, False],
    })
    out = sc.compute_daily_uplimit_ratio(df, '店U')
    assert out['A'] == pytest.approx(0.4)  # (0.2+0.6)/2、NaN行はdropna除外でcountに入らない


def test_compute_daily_uplimit_ratio_empty_returns_empty_series():
    df = pd.DataFrame({'ホール名': ['他店'], '日付': ['A'], 'high_prob': [0.5]})
    out = sc.compute_daily_uplimit_ratio(df, '店U')
    assert out.empty


# ── _solve_uplimit_offset ─────────────────────────────────────────

def test_solve_uplimit_offset_already_below_target_returns_zero():
    vals = np.zeros(5)  # mean(sigmoid(0))=0.5
    offset = sc._solve_uplimit_offset(vals, target_ratio=0.5)
    assert offset == 0.0


def test_solve_uplimit_offset_uniform_values_analytic():
    # 全員同一値(log_odds=0) → mean(sigmoid(-offset))=sigmoid(-offset)=target
    # target=0.3 → offset = -logit(0.3) = ln(7/3) ≈ 0.8472978603872036
    vals = np.zeros(5)
    offset = sc._solve_uplimit_offset(vals, target_ratio=0.3)
    assert offset == pytest.approx(0.8472978603872036, abs=1e-6)


def test_solve_uplimit_offset_property_mean_matches_target():
    # ばらつきのある値でも、解いたoffsetを引いた後の平均sigmoidがtargetに一致する(性質検証)
    vals = np.array([-1.0, 0.0, 1.0, 2.0])
    target = 0.4
    offset = sc._solve_uplimit_offset(vals, target_ratio=target)
    mean_after = float(np.mean(1.0 / (1.0 + np.exp(-(vals - offset)))))
    assert mean_after == pytest.approx(target, abs=1e-6)


# ── compute_uplimit ────────────────────────────────────────────────

_UPLIMIT_CONFIG = {
    '分位点': 0.5,
    '安全マージン': 0.0,
    '絶対上限': 0.9,
    '業界一般値フォールバック': 0.4,
    '短期ウィンドウ日数': 3,
    '最低必要日数': 5,
    '特異日除外リスト': [],
}


def _make_uplimit_df(n_days: int) -> pd.DataFrame:
    """
    n_days日分、各日2台(同一high_prob)。ratio=[0.1,0.2,0.3,0.4,0.9]の先頭n_days件を使用。
    log_odds は該当ratioのlogitを設定し、mean(sigmoid(log_odds))が厳密にratioと一致するようにする。
    """
    ratios = [0.1, 0.2, 0.3, 0.4, 0.9][:n_days]
    dates = [f'2026-02-{i + 1:02d}' for i in range(n_days)]
    rows = []
    for date_, ratio in zip(dates, ratios):
        lo = _logit(ratio)
        for unit in (1, 2):
            rows.append({
                '日付': date_, 'ホール名': '店U', '機種名': '機種X', '台番号': unit,
                'log_odds': lo, 'high_prob': 1.0 / (1.0 + np.exp(-lo)), 'is_invalid': False,
            })
    return pd.DataFrame(rows)


def test_compute_uplimit_analytic_value_and_in_place_mutation():
    # 5日分[0.1,0.2,0.3,0.4,0.9]、分位点0.5(中央値)
    # 全履歴の中央値(long_q)=0.3、短期3日窓[0.3,0.4,0.9]の中央値(short_q)=0.4
    # blended = FIXED_ALPHA(0.3)*0.4 + 0.7*0.3 = 0.33、絶対上限0.9でクリップされず0.33
    # day_factor=5/30, sample_factor=5/50 → reliability=5/30*0.7+5/50*0.3=0.14666...
    df = _make_uplimit_df(5)
    df_before = df.copy()

    result = sc.compute_uplimit(df, '店U', config=_UPLIMIT_CONFIG)

    assert result['上限キャリブレーション値'] == pytest.approx(0.33, abs=1e-9)
    assert result['上限信頼度'] == pytest.approx(5 / 30 * 0.7 + 5 / 50 * 0.3, abs=1e-9)
    assert result['対象日数'] == 5
    assert result['発動日数'] == 2  # ratio 0.4, 0.9 の2日がuplimit(0.33)超過

    # 超過しなかった日(ratio<=0.33)はin-placeで書き換えられない
    for date_ in ('2026-02-01', '2026-02-02', '2026-02-03'):
        mask = df['日付'] == date_
        before_mask = df_before['日付'] == date_
        np.testing.assert_allclose(
            df.loc[mask, 'log_odds'].to_numpy(),
            df_before.loc[before_mask, 'log_odds'].to_numpy(),
        )

    # 超過した日(ratio 0.4, 0.9)はoffset分だけlog_oddsが下方修正され、
    # 修正後の平均high_probがuplimit(0.33)に一致する
    for date_ in ('2026-02-04', '2026-02-05'):
        mask = df['日付'] == date_
        before_mask = df_before['日付'] == date_
        assert not np.allclose(
            df.loc[mask, 'log_odds'].to_numpy(),
            df_before.loc[before_mask, 'log_odds'].to_numpy(),
        )
        assert df.loc[mask, 'high_prob'].mean() == pytest.approx(0.33, abs=1e-6)


def test_compute_uplimit_insufficient_data_falls_back_without_mutation():
    # 最低必要日数5に対し3日分しかない → 業界一般値フォールバック・発動日数0・df変更なし
    df = _make_uplimit_df(3)
    df_before = df.copy()

    result = sc.compute_uplimit(df, '店U', config=_UPLIMIT_CONFIG)

    assert result['上限キャリブレーション値'] == pytest.approx(0.4)  # 業界一般値フォールバック
    assert result['上限信頼度'] == 0.0
    assert result['発動日数'] == 0
    assert result['対象日数'] == 3

    pd.testing.assert_series_equal(df['log_odds'], df_before['log_odds'])
    pd.testing.assert_series_equal(df['high_prob'], df_before['high_prob'])
