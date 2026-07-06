"""
raw_specs_scraped.json(chonborista.comから取得した生データ)を正規化し、
実測ホールデータとの相関に基づいてTier判定を行った上で
machine_setting_specs.json を再構築する。

Tier判定方針(データ分析_skill.md Stage1aに準拠):
    - ボーナスのみ機種(BIG/REG方式、AT/CZ要素なし) → Tier A(理論値をそのまま使用)
      根拠: 既存のマイジャグラーVもTier A(相関計算なしで確定)が前例。
            AT/CZを挟まない機種は当選契機と理論値のズレを生む要因がない。
      判定は「BIG/REG列のみの確率表(is_pure_bonus)」と「chonborista.comの『仕様』欄が
      ノーマルタイプ/Aタイプと明記(is_confirmed_normal_type)」の両方を満たす場合のみ確定させる。
      列名だけでは不十分(2026-07判明): AT/ART/スマスロ機でもBIG/REG列名の確率表を
      公開している場合があり(沖ドキ!シリーズ・ドッチ・ディスクアップULTRAREMIX等)、
      列名だけで無条件Tier Aにすると理論値と実測値の意味がズレて重大な外れ値を生む。
    - AT/CZ機など上記に該当しない機種 → judge_tier()と同じロジックで
      BB確率/RB確率と実測差枚率の相関を計算し |r|>0.5 なら Tier B、それ以外は Tier C。

機械割(出玉率)は判定に使わないが、要望によりTierによらず全設定に反映する。
数値が存在しない項目は null で埋め、後続の数値処理で扱いやすくする。

実行方法: python fase2/assign_tier.py
"""
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

import data_source as ds

BASE_DIR = Path(__file__).resolve().parent
RAW_PATH = BASE_DIR / 'raw_specs_scraped.json'
SPECS_PATH = BASE_DIR / 'machine_setting_specs.json'

# ── 列名正規化ルール ──────────────────────────────────────────
BB_EXACT = ['BIG', 'BB確率', 'BB']
RB_EXACT = ['REG', 'RB確率', 'RB']
SUMMARY_HINT = ('合算', '合成')  # 合算・合成を含む列は他列の合計である可能性がある
PURE_BONUS_PAIRS = [('BIG', 'REG'), ('BB確率', 'RB確率')]


def collect_columns(settings_raw: dict) -> list[str]:
    """設定1〜6に登場する列名をテーブル出現順に統合する。"""
    seen: list[str] = []
    for key in sorted(settings_raw.keys(), key=lambda x: int(x)):
        for col in settings_raw[key].keys():
            if col not in seen:
                seen.append(col)
    return seen


def _pick_payout_column(col_names: list[str]) -> str | None:
    """『出玉率』(技術介入型は『技術介入』)を含む列を選ぶ。複数ある場合は
    市場予測・部分成功など平均的な実測値を優先し、完全攻略(技術上限)は避ける。"""
    payout_cols = [c for c in col_names if '出玉率' in c or '技術介入' in c]
    if not payout_cols:
        return None
    for c in payout_cols:
        if '市場予測' in c or '平均' in c or '60%' in c:
            return c
    for c in payout_cols:
        if '完全攻略' not in c and '完全手順' not in c and '全て失敗' not in c:
            return c
    return payout_cols[0]


def _cell_value(row: dict, col: str) -> float | None:
    cell = row.get(col)
    if not cell:
        return None
    if 'prob' in cell:
        return cell['prob']
    if 'ratio' in cell:
        return cell['ratio']
    return None


def _find_best_summing_pair(candidates: list[str], target: float, row: dict) -> tuple[str, str] | None:
    """candidatesの中から、値の合計がtargetに最も近い(1%未満の誤差)組を返す。"""
    best = None
    best_err = None
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a, b = candidates[i], candidates[j]
            va, vb = _cell_value(row, a), _cell_value(row, b)
            if va is None or vb is None:
                continue
            err = abs((va + vb) - target)
            if target and err / target > 0.01:
                continue
            if best_err is None or err < best_err:
                best_err, best = err, (a, b)
    return best


def map_columns(col_names: list[str], settings_raw: dict | None = None) -> dict:
    remaining = list(col_names)
    result = {}

    payout_col = _pick_payout_column(remaining)
    if payout_col:
        result['payout'] = payout_col
    # 出玉率系の列は(選ばれなかった副次列も含めて)BB/RB候補から除外する
    remaining = [c for c in remaining if '出玉率' not in c and '技術介入' not in c]

    for c in BB_EXACT:
        if c in remaining:
            result['BB'] = c
            remaining.remove(c)
            break

    for c in RB_EXACT:
        if c in remaining:
            result['RB'] = c
            remaining.remove(c)
            break

    total_like = [c for c in remaining if any(h in c for h in SUMMARY_HINT)]
    still_needed = [t for t in ('BB', 'RB') if t not in result]

    if total_like and settings_raw and len(remaining) >= len(still_needed) + 1:
        first_row = settings_raw[sorted(settings_raw.keys(), key=lambda x: int(x))[0]]
        best_pick, best_err = None, None
        for total_col in total_like:
            target = _cell_value(first_row, total_col)
            if target is None:
                continue
            candidates = [c for c in remaining if c != total_col]  # 他の合算列も候補に含める(例: 'BB合算')
            if len(still_needed) == 2:
                pair = _find_best_summing_pair(candidates, target, first_row)
                if pair:
                    err = abs(sum(_cell_value(first_row, c) for c in pair) - target)
                    if best_err is None or err < best_err:
                        best_err, best_pick = err, pair
            elif len(still_needed) == 1:
                other = 'RB' if still_needed[0] == 'BB' else 'BB'
                other_val = _cell_value(first_row, result.get(other)) if result.get(other) else None
                if other_val is None:
                    continue
                residual = target - other_val
                for c in candidates:
                    v = _cell_value(first_row, c)
                    if v is None:
                        continue
                    err = abs(v - residual)
                    if residual and err / residual > 0.01:
                        continue
                    if best_err is None or err < best_err:
                        best_err, best_pick = err, (c,)
        if best_pick:
            if len(best_pick) == 2:
                ordered = sorted(best_pick, key=lambda c: col_names.index(c))
                result['BB'], result['RB'] = ordered[0], ordered[1]
            else:
                result[still_needed[0]] = best_pick[0]
            return result

    fallback = [c for c in remaining if c not in total_like] or list(remaining)

    # 内訳の組合せが特定できない場合は出現順で機械的に割り当てる
    fallback.sort(key=lambda c: col_names.index(c))
    if 'BB' not in result and fallback:
        result['BB'] = fallback.pop(0)
    if 'RB' not in result and fallback:
        result['RB'] = fallback.pop(0)

    return result


def is_pure_bonus(col_names: list[str]) -> bool:
    return any(a in col_names and b in col_names for a, b in PURE_BONUS_PAIRS)


# 『仕様』欄がこれらの接頭辞で始まる場合のみノーマルタイプ(Aタイプ)と確定させる。
# is_pure_bonus(BIG/REG列のみの確率表)だけでは機種タイプを判定できない。
# AT/ART/スマスロ機でもBIG/REG列名の確率表を公開している場合があるため
# (実例: 沖ドキ!シリーズ・ドッチ・ディスクアップULTRAREMIXは全てAT機なのにBIG/REG列表記で、
#  理論値をそのまま使うTier Aに誤判定されていた。ホールデータの実測値が理論値の
#  数倍〜十数倍に達する異常となって顕在化した)。
_NORMAL_TYPE_PREFIXES = ('ノーマル', 'Aタイプ')


def is_confirmed_normal_type(machine_type: str | None) -> bool:
    """chonborista.comの『仕様』欄がノーマルタイプ/Aタイプと明記しているかを判定する。"""
    if not machine_type:
        return False
    return machine_type.startswith(_NORMAL_TYPE_PREFIXES)


def _parse_range_pct(raw_text: str) -> float | None:
    """『97.6〜102.0%』のような範囲表記の中間値を返す(技術介入等で単一値が無い機種向け)。"""
    nums = re.findall(r'[\d.]+', raw_text)
    if not nums:
        return None
    vals = [float(n) for n in nums]
    return round((sum(vals) / len(vals)) / 100, 4)


def build_normalized_settings(settings_raw: dict, col_map: dict) -> dict:
    """{"1": {"BB":.., "RB":.., "payout":..}, ...} 形式に正規化する。値が無ければnull。"""
    out = {}
    for key in sorted(settings_raw.keys(), key=lambda x: int(x)):
        row = settings_raw[key]
        entry = {}
        for target in ('BB', 'RB', 'payout'):
            src_col = col_map.get(target)
            cell = row.get(src_col) if src_col else None
            if cell is None:
                entry[target] = None
            elif 'prob' in cell:
                entry[target] = cell['prob']
            elif 'ratio' in cell:
                entry[target] = cell['ratio']
            elif target == 'payout' and cell.get('raw'):
                entry[target] = _parse_range_pct(cell['raw'])
            else:
                entry[target] = None
        out[key] = entry
    return out


# ── ホールデータ読み込み(Tier B/C判定用) ──────────────────────

_RATE_COLUMNS = ('BB確率', 'RB確率', 'ART確率')
_RATE_CORR_MIN_SAMPLES = 30
_RATE_CORR_THRESHOLD = 0.5


def load_all_hall_data() -> pd.DataFrame:
    """レプリカDBから全店舗の実測データ(Tier B/C判定用)を読み込む。"""
    con = ds.connect_replica()
    try:
        df = pd.read_sql_query(
            "SELECT 機種名, 回転数, 差枚, BB確率, RB確率, ART確率 FROM slot_data", con
        )
    finally:
        con.close()
    for col in ['回転数', '差枚', 'BB確率', 'RB確率', 'ART確率']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


def judge_ab_tier(df: pd.DataFrame, machine_name: str) -> dict:
    """
    judge_tier()(preprocess.py)と同一ロジックで機種名についてBB/RB相関Tierを算出する。

    店舗によってはBB確率に入るはずのデータがART確率列に入っている場合があるため
    (データ提供元のページ仕様が店舗ごとに異なる。preprocess.resolve_rate_columns参照)、
    BB確率/RB確率に限定せずART確率も候補に含めて最も相関の強い列を採用する。
    ここでは全店舗のデータを結合して判定するため、ART確率にデータを置く店舗が
    一部でも十分なサンプルがあれば拾える(全店舗がその店舗仕様の場合は判定に反映される)。
    """
    grp = df[df['機種名'] == machine_name].copy()
    grp = grp.dropna(subset=['回転数', '差枚'])
    grp = grp[grp['回転数'] > 0]
    if grp.empty:
        return {'BB': 'C', 'RB': 'C'}
    grp['差枚率'] = grp['差枚'] / grp['回転数']

    scored = []
    for col in _RATE_COLUMNS:
        sub = grp.dropna(subset=[col, '差枚率'])
        n = len(sub)
        if n < _RATE_CORR_MIN_SAMPLES:
            continue
        r, _ = stats.pearsonr(sub[col], sub['差枚率'])
        if abs(r) >= _RATE_CORR_THRESHOLD:
            scored.append((abs(r), col))
    scored.sort(reverse=True)

    result = {'BB': 'C', 'RB': 'C'}
    used: set[str] = set()
    for slot in ('BB', 'RB'):
        for _r, col in scored:
            if col not in used:
                result[slot] = 'B'
                used.add(col)
                break
    return result


def channel_has_data(settings_norm: dict, channel: str) -> bool:
    return sum(1 for v in settings_norm.values() if v.get(channel) is not None) >= 2


def main():
    raw = json.loads(RAW_PATH.read_text(encoding='utf-8'))
    hall_df = load_all_hall_data()

    specs: dict = {
        '_comment': (
            '機種別理論値差表。chonborista.comより取得(取得日: raw_specs_scraped.json参照)。'
            'Tierはノーマルタイプ(ボーナスのみ・chonboristaの「仕様」欄がノーマル/Aタイプと明記)→A、'
            'それ以外(AT/ART/スマスロ機含む)はホールデータとの相関(|r|>0.5)でB/Cを自動判定。'
            'payoutは機械割(出玉率)。値が存在しない項目はnull。'
        )
    }

    tier_counts = {'A': 0, 'B': 0, 'C': 0}

    for machine_name, entry in raw.items():
        if entry.get('status') != 'ok':
            continue
        settings_raw = entry.get('settings') or {}
        if not settings_raw:
            continue

        col_names = collect_columns(settings_raw)
        col_map = map_columns(col_names, settings_raw)
        settings_norm = build_normalized_settings(settings_raw, col_map)
        pure_bonus = is_pure_bonus(col_names) and is_confirmed_normal_type(entry.get('machine_type'))

        if pure_bonus:
            tier = {
                'BB': 'A' if channel_has_data(settings_norm, 'BB') else 'C',
                'RB': 'A' if channel_has_data(settings_norm, 'RB') else 'C',
            }
        else:
            tier = judge_ab_tier(hall_df, machine_name)

        tier_counts[tier.get('BB', 'C')] = tier_counts.get(tier.get('BB', 'C'), 0) + 1

        specs[machine_name] = {
            'tier': tier,
            'source_columns': col_map,  # 監査用: どの生列をBB/RB/payoutに割り当てたか
            'settings': settings_norm,
        }

    SPECS_PATH.write_text(json.dumps(specs, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'書き込み完了: {len(specs) - 1}機種')
    print('BB tier分布:', tier_counts)


if __name__ == '__main__':
    main()
