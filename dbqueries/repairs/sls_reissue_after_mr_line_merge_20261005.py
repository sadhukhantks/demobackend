"""sls branch 29, 2026-10-05: MRs 28586 and 28592 were edited (item-59 line emptied, its bales
folded into the item-57 line) AFTER jute issues had been drawn from the item-59 line. The
emptied line went negative in vw_jute_stock_outstanding by exactly what the item-57 line
shows as left over. Moves those issues to the item-57 line (item_id 59 -> 57); weights unchanged.

  MR 28586: issues 89018, 89019  line 46278 -> 46277
  MR 28592: issues 89025, 89026  line 46300 -> 46299

Dry-run by default. Run from vowerp3be:
  PYTHONPATH=. uv run python dbqueries/repairs/sls_reissue_after_mr_line_merge_20261005.py [--apply]
Rollback:
  UPDATE jute_issue SET jute_mr_li_id = 46278, item_id = 59 WHERE jute_issue_id IN (89018, 89019)
  UPDATE jute_issue SET jute_mr_li_id = 46300, item_id = 59 WHERE jute_issue_id IN (89025, 89026)
"""
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.config.db import get_tenant_engine

MOVES = [  # (issue ids, from line, to line, from item, to item)
    ((89018, 89019), 46278, 46277, 59, 57),
    ((89025, 89026), 46300, 46299, 59, 57),
]
LINES = (46277, 46278, 46299, 46300)


def balances(db):
    rows = db.execute(text(f"SELECT jute_mr_li_id, bal_qty, bal_weight FROM vw_jute_stock_outstanding "
                           f"WHERE jute_mr_li_id IN ({', '.join(map(str, LINES))}) ORDER BY jute_mr_li_id")).fetchall()
    return [(r.jute_mr_li_id, float(r.bal_qty or 0), float(r.bal_weight or 0)) for r in rows]


def main(apply: bool) -> None:
    with Session(get_tenant_engine("sls")) as db:
        print("before:", balances(db))
        for ids, src, dst, it_from, it_to in MOVES:
            n = db.execute(text(f"""UPDATE jute_issue SET jute_mr_li_id = :dst, item_id = :it_to
                                    WHERE jute_issue_id IN ({', '.join(map(str, ids))})
                                      AND jute_mr_li_id = :src AND item_id = :it_from"""),
                           {"dst": dst, "src": src, "it_from": it_from, "it_to": it_to}).rowcount
            assert n == len(ids), f"expected {len(ids)} rows for {ids}, got {n}"
        after = balances(db)
        print("after: ", after)
        assert all(q > -0.5 and w > -0.5 for _, q, w in after), "still negative"
        if apply:
            db.commit()
            print("APPLIED")
        else:
            db.rollback()
            print("DRY-RUN (rolled back). Add --apply to write.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
