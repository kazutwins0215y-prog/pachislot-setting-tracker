"""
2026-07-17に発生した「空ページ403の偽成功」により、実際はアクセス遮断だったにもかかわらず
missing_data に理由='ページにデータなし'として誤記録された約100行の理由を修正するワンショットスクリプト。

背景: is_block_page導入(2026-07-20)前のfetch_pageは空ボディ403を検知できず、
ブロック中の応答を正常な『ページにデータなし』として全店舗×複数日にわたり誤記録した
（詳細はメモリ[[project_pc_403_block_incident]]、fase1/データ収集_skill.md参照）。
missing_dataはどのコードからも読まれておらず実害は無いため急ぎではないが、
記録の正確性のために理由を訂正する。

実行タイミング: ブロック解除・収集再開後、fase4の日次書き込みが正常に通ることを
確認してから実行する（ブロック中はTurso書き込み枠を無駄に消費しないため。2026-07-20方針）。

使い方（Git Bash・リポジトリルートで実行。.envにTurso認証が必要）:

  確認のみ(既定・書き込みなし): 対象件数と2026-07-14〜17に記録されたmissing_data全件を表示する
    py -3.12 fase1/fix_block_misrecorded_missing_data.py

  適用: 対象行の理由を更新する
    py -3.12 fase1/fix_block_misrecorded_missing_data.py --apply
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from db import get_connection, sync_replica

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

TARGET_REASON = 'ページにデータなし'
TARGET_DATE_LIKE = '2026-07-17%'  # 偽成功が発生した実行日(記録日時ベース)
NEW_REASON = 'ブロック誤記録(実際はアクセス遮断)'

REVIEW_RANGE_START = '2026-07-14'  # 巻き添え確認用の目視レビュー範囲(こちらはUPDATE対象外)
REVIEW_RANGE_END_EXCLUSIVE = '2026-07-18'


def show_target_count(con) -> int:
    cur = con.cursor()
    cur.execute(
        'SELECT COUNT(*) FROM missing_data WHERE 理由 = ? AND 記録日時 LIKE ?',
        (TARGET_REASON, TARGET_DATE_LIKE),
    )
    count = cur.fetchone()[0]
    logger.info(f'UPDATE対象(記録日時 LIKE {TARGET_DATE_LIKE!r}・理由={TARGET_REASON!r}): {count}件')
    return count


def show_review_rows(con) -> None:
    """7/14〜16の明示403(正常検知)期間も含めて巻き添え記録が無いか目視確認する。UPDATE対象はTARGET_DATE_LIKEのみ。"""
    cur = con.cursor()
    cur.execute(
        'SELECT 日付, ホール名, 機種名, 理由, 記録日時 FROM missing_data '
        'WHERE 記録日時 >= ? AND 記録日時 < ? ORDER BY 記録日時',
        (REVIEW_RANGE_START, REVIEW_RANGE_END_EXCLUSIVE),
    )
    rows = cur.fetchall()
    logger.info(f'参考: {REVIEW_RANGE_START}〜{REVIEW_RANGE_END_EXCLUSIVE}に記録されたmissing_data全件({len(rows)}件、UPDATE対象外含む):')
    for row in rows:
        logger.info(f'  {row}')


def apply_update(con) -> int:
    cur = con.cursor()
    cur.execute(
        'UPDATE missing_data SET 理由 = ? WHERE 理由 = ? AND 記録日時 LIKE ?',
        (NEW_REASON, TARGET_REASON, TARGET_DATE_LIKE),
    )
    con.commit()
    return cur.rowcount


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='指定しない場合は件数確認のみ(既定はdry-run)')
    args = parser.parse_args()

    con = get_connection()
    try:
        show_target_count(con)
        show_review_rows(con)

        if not args.apply:
            logger.info('dry-run(既定動作)のため更新は行っていません。--apply を付けて再実行してください。')
            return

        updated = apply_update(con)
        logger.info(f'{updated}件を理由={NEW_REASON!r}へ更新しました')
        sync_replica(con)
    finally:
        con.close()


if __name__ == '__main__':
    main()
