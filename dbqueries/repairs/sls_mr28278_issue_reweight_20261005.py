"""sls branch 87: MR 28278's two lines are fully issued by quantity (350/350 and 40/40), but the
issues were weighed at the MR's average kg/unit (~29.75) instead of each line's own
(45847: 10,484/350 = 29.95; 45848: 1,116/40 = 27.9), so 45848 shows -74 kg and 45847 +74 kg.
Re-weighs each line's issues at the line's kg/unit (last issue takes the rounding) so every
line's issued weight equals its received weight. Owner OK 2026-10-05.

Dry-run by default. Run from vowerp3be:
  PYTHONPATH=. uv run python dbqueries/repairs/sls_mr28278_issue_reweight_20261005.py [--apply]
Rollback:
  UPDATE jute_issue SET weight = 4105 WHERE jute_issue_id = 88605
  UPDATE jute_issue SET weight = 6305 WHERE jute_issue_id = 88607
  UPDATE jute_issue SET weight = 863  WHERE jute_issue_id = 88603
  UPDATE jute_issue SET weight = 327  WHERE jute_issue_id = 88612
"""
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.config.db import get_tenant_engine

LINES = (45847, 45848)


def main(apply: bool) -> None:
    with Session(get_tenant_engine("sls")) as db:
        for li_id in LINES:
            li = db.execute(text("SELECT actual_qty, actual_weight FROM jute_mr_li WHERE jute_mr_li_id = :id"),
                            {"id": li_id}).fetchone()
            issues = db.execute(text("""SELECT jute_issue_id, quantity, weight FROM jute_issue
                                        WHERE jute_mr_li_id = :id AND status_id <> 4 ORDER BY issue_date, jute_issue_id"""),
                                {"id": li_id}).fetchall()
            assert sum(float(i.quantity) for i in issues) == float(li.actual_qty), f"line {li_id}: qty not fully issued"
            per_unit = float(li.actual_weight) / float(li.actual_qty)
            remaining = round(float(li.actual_weight))
            for n, i in enumerate(issues):
                new_wt = remaining if n == len(issues) - 1 else round(float(i.quantity) * per_unit)
                remaining -= new_wt
                db.execute(text("UPDATE jute_issue SET weight = :w WHERE jute_issue_id = :id"), {"w": new_wt, "id": i.jute_issue_id})
                print(f"line {li_id} issue {i.jute_issue_id}: qty {i.quantity}, weight {i.weight} -> {new_wt}")
        bal = db.execute(text("SELECT jute_mr_li_id, bal_weight FROM vw_jute_stock_outstanding WHERE jute_mr_li_id IN (45847, 45848)")).fetchall()
        print("balances after:", [(r.jute_mr_li_id, float(r.bal_weight)) for r in bal])
        assert all(abs(float(r.bal_weight)) < 0.5 for r in bal)
        if apply:
            db.commit()
            print("APPLIED")
        else:
            db.rollback()
            print("DRY-RUN (rolled back). Add --apply to write.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
