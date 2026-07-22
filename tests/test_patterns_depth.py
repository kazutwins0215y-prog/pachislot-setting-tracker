"""
patterns_depth.py の回帰テスト。
sueki_daily_r / score_sueki_daily(符号規約) / score_rotation(Gini) /
score_teppandai(性質検証) / build_observed_history / predict_next_day。
"""
import numpy as np
import pandas as pd
import pytest

import patterns_depth as pd_depth


# ── sueki_daily_r ─────────────────────────────────────────────────

def test_sueki_daily_r_perfect_linear_trend_gives_r_near_one():
    # 完全な線形増加列はlag-1ペアが常に相関1.0(数学的事実)。
    # EWM平滑化も定数1.0列の加重平均=1.0のまま(spanに依存しない)
    hp = pd.Series([0.02 * i for i in range(1, 21)])
    r = pd_depth.sueki_daily_r(hp)
    assert len(r) == 20
    assert np.isnan(r[0])  # 先頭は常にNaN(ペア無し)
    assert r[-1] == pytest.approx(1.0, abs=1e-9)


def test_sueki_daily_r_short_series_returns_all_nan():
    r = pd_depth.sueki_daily_r(pd.Series([0.5]))
    assert len(r) == 1
    assert np.isnan(r[0])


# ── score_sueki_daily ────────────────────────────────────────────

def _make_unit_history_df(hp_values):
    n = len(hp_values)
    dates = pd.date_range('2026-01-01', periods=n, freq='D').strftime('%Y-%m-%d')
    return pd.DataFrame({
        '日付': dates, 'ホール名': 'テスト店', '機種名': 'A', '台番号': 1,
        'high_prob': hp_values,
    })


def test_score_sueki_daily_positive_branch_passthrough_r_bar():
    # r_bar>=閾値(0.2)の日は正スコア=r_barそのまま(完全な線形増加でr≈1.0)
    df = _make_unit_history_df([0.02 * i for i in range(1, 21)])
    scores = pd_depth.score_sueki_daily(df)
    assert scores.iloc[-1] == pytest.approx(1.0, abs=1e-9)


def test_score_sueki_daily_negative_branch_scaled_by_negative_scale():
    # 完全な交互列(0.1,0.9,0.1,0.9,...)はlag-1相関が厳密に-1.0(線形の負の変換)。
    # r_bar=-1.0 < 0.2 → signed = -NEGATIVE_SCALE * min(1, (0.2-(-1))/0.2) = -0.5*1 = -0.5
    hp_values = [0.1 if i % 2 == 0 else 0.9 for i in range(20)]
    df = _make_unit_history_df(hp_values)
    scores = pd_depth.score_sueki_daily(df)
    assert scores.iloc[-1] == pytest.approx(-pd_depth.NEGATIVE_SCALE, abs=1e-9)


def test_score_sueki_daily_insufficient_history_is_nan():
    df = _make_unit_history_df([0.5, 0.6])
    scores = pd_depth.score_sueki_daily(df)
    assert scores.isna().all()


# ── score_rotation (Gini) ────────────────────────────────────────

def test_score_rotation_analytic_gini_when_significant():
    # 3台: unit1=0.9(10行)・unit2=0.1(10行)・unit3=0.1(10行)。値が完全一定のため
    # 並べ替え検定は現実の配分(=全"高"値が1台に集中)を上回るケースがほぼ存在せず有意。
    # Gini(sorted=[0.1,0.1,0.9], n=3, total=1.1):
    #   (2*(1*0.1+2*0.1+3*0.9))/(3*1.1) - 4/3 = (2*3.0)/3.3 - 1.333333 = 0.484848...
    dates = pd.date_range('2026-01-01', periods=10, freq='D').strftime('%Y-%m-%d')
    df = pd.concat([
        pd.DataFrame({'日付': dates, 'ホール名': 'テスト店', '機種名': 'A', '台番号': 1, 'high_prob': 0.9}),
        pd.DataFrame({'日付': dates, 'ホール名': 'テスト店', '機種名': 'A', '台番号': 2, 'high_prob': 0.1}),
        pd.DataFrame({'日付': dates, 'ホール名': 'テスト店', '機種名': 'A', '台番号': 3, 'high_prob': 0.1}),
    ], ignore_index=True)

    scores = pd_depth.score_rotation(df, 'A')
    assert scores.notna().all()
    assert scores.iloc[0] == pytest.approx(0.48484848484848486)


def test_score_rotation_fewer_than_three_units_is_nan():
    dates = pd.date_range('2026-01-01', periods=10, freq='D').strftime('%Y-%m-%d')
    df = pd.concat([
        pd.DataFrame({'日付': dates, 'ホール名': 'テスト店', '機種名': 'A', '台番号': 1, 'high_prob': 0.9}),
        pd.DataFrame({'日付': dates, 'ホール名': 'テスト店', '機種名': 'A', '台番号': 2, 'high_prob': 0.1}),
    ], ignore_index=True)
    scores = pd_depth.score_rotation(df, 'A')
    assert scores.isna().all()


def test_score_rotation_insufficient_valid_rows_is_nan():
    # 店舗全体で有効行10件未満 → 検定対象外
    df = pd.DataFrame({
        '日付': pd.date_range('2026-01-01', periods=6, freq='D').strftime('%Y-%m-%d'),
        'ホール名': 'テスト店', '機種名': 'A', '台番号': [1, 1, 1, 2, 2, 2],
        'high_prob': [0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
    })
    scores = pd_depth.score_rotation(df, 'A')
    assert scores.isna().all()


# ── score_teppandai (性質検証) ─────────────────────────────────────

def _make_teppan_df(hp_values, start='2026-01-05'):
    n = len(hp_values)
    dates = pd.date_range(start, periods=n, freq='D')
    return pd.DataFrame({
        '日付': dates.strftime('%Y-%m-%d'), 'ホール名': 'テスト店', '機種名': 'A', '台番号': 1,
        'high_prob': hp_values,
    })


def test_score_teppandai_history_under_14_days_is_all_nan():
    df = _make_teppan_df([0.5] * 13)
    scores = pd_depth.score_teppandai(df, 'A')
    assert scores.isna().all()


def test_score_teppandai_no_signal_constant_series_is_nan():
    # 変動が無い(std=0)系列はACF/カレンダーどちらの経路も有意な検出をしない
    df = _make_teppan_df([0.5] * 30)
    scores = pd_depth.score_teppandai(df, 'A')
    assert scores.isna().all()


def test_score_teppandai_unknown_machine_returns_nan_with_original_index():
    df = _make_teppan_df([0.5] * 20)
    scores = pd_depth.score_teppandai(df, '存在しない機種')
    assert len(scores) == len(df)
    assert scores.isna().all()


def test_score_teppandai_detects_weekly_pattern_positive_on_hot_day():
    # 2026-01-05は月曜。金曜(dayofweek==4)だけhigh_prob=0.9、他は0.1の8週間データ。
    # カレンダー経路(曜日_金)は確実に有意検出されるため金曜は必ず正スコア。
    # 周期経路(lag=7)は5ビン量子化と7日周期が割り切れないため一部の非金曜日も
    # 正ビンに入り得る(位相量子化の仕様上の性質)ので、非金曜側は「平均では金曜より
    # 低い」という弱い性質のみ検証する。
    dates = pd.date_range('2026-01-05', periods=56, freq='D')
    hp = [0.9 if d.dayofweek == 4 else 0.1 for d in dates]
    df = pd.DataFrame({
        '日付': dates.strftime('%Y-%m-%d'), 'ホール名': 'テスト店', '機種名': 'A', '台番号': 1,
        'high_prob': hp,
    })
    scores = pd_depth.score_teppandai(df, 'A')

    is_friday = dates.dayofweek == 4
    assert scores[is_friday].notna().all()
    assert (scores[is_friday] > 0).all()
    assert scores[~is_friday].notna().all()
    assert scores[~is_friday].mean() < scores[is_friday].mean()


# ── build_observed_history ───────────────────────────────────────

def test_build_observed_history_sorts_and_nans_invalid_rows():
    df = pd.DataFrame({
        '日付': ['2026-01-03', '2026-01-01', '2026-01-02'],
        'ホール名': ['テスト店'] * 3,
        '機種名': ['A'] * 3,
        '台番号': [1, 1, 1],
        'high_prob': [0.3, 0.1, 0.2],
        'is_invalid': [False, False, True],
    })
    hp = pd_depth.build_observed_history(df, 'テスト店', 'A', 1)
    assert hp.tolist()[0] == pytest.approx(0.1)   # 2026-01-01が先頭(日付昇順)
    assert np.isnan(hp.tolist()[1])                # 2026-01-02はis_invalid=True→NaN
    assert hp.tolist()[2] == pytest.approx(0.3)    # 2026-01-03
    assert list(hp.index) == [0, 1, 2]              # 観測順に0始まりindex化


# ── predict_next_day ─────────────────────────────────────────────

def test_predict_next_day_returns_none_when_no_signal_in_any_path():
    hp = pd.Series([0.5] * 20)  # 変動なし→周期経路も検出なし、カレンダー条件も空
    result = pd_depth.predict_next_day(hp, [7], [], '2026-01-01')
    assert result is None


def test_predict_next_day_calendar_matched_day_returns_effect():
    cal_conditions = [{'条件': '末尾_1', '効果量': 0.6}]
    # 2026-01-11: 日の下1桁=1 → 末尾_1に該当
    result = pd_depth.predict_next_day(pd.Series([0.5] * 20), [], cal_conditions, '2026-01-11')
    assert result == pytest.approx(0.6)


def test_predict_next_day_calendar_unmatched_day_returns_negative_scaled_mean():
    cal_conditions = [{'条件': '末尾_1', '効果量': 0.6}]
    # 2026-01-12: 日の下1桁=2 → 末尾_1に非該当
    result = pd_depth.predict_next_day(pd.Series([0.5] * 20), [], cal_conditions, '2026-01-12')
    assert result == pytest.approx(-pd_depth.NEGATIVE_SCALE * 0.6)
