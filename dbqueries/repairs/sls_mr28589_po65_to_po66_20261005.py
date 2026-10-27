"""sls branch 29: MR 28589 (gate entry 81) was booked against PO 65 (jute_po_id 13406)
instead of PO 66 (jute_po_id 13407). Owner request 2026-10-05.

Moves the MR header (po_id, mukam_id) and its one line (jute_po_li_id 31448 -> 31450,
allowable_moisture copied from the new PO line, as the gate entry does) to PO 66, then
recomputes PO settlement for both POs. The MR is Open, has no issues and no transfer chain.

Dry-run by default (rolls back). Write with:  PYTHONPATH=. uv run python dbqueries/repairs/sls_mr28589_po65_to_po66_20261005.py --apply
Run from the vowerp3be folder.

Rollback (if ever needed):
  UPDATE jute_mr SET po_id = 13406, mukam_id = 5 WHERE jute_mr_id = 28589
  UPDATE jute_mr_li SET jute_po_li_id = 31448, allowable_moisture = 20 WHERE jute_mr_li_id = 46288
  then recompute settlement for 13406 and 13407.
"""
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.config.db import get_tenant_engine
from src.juteProcurement import po_settlement as ps

MR_ID, MR_LI_ID = 28589, 46288
OLD_PO, NEW_PO = 13406, 13407
OLD_PO_LI, NEW_PO_LI = 31448, 31450
TRIGGER = "REPAIR:sls_mr28589_po65_to_po66_20261005"


def main(apply: bool) -> None:
    with Session(get_tenant_engine("sls")) as db:
        mr = db.execute(text("SELECT po_id, status_id, branch_id FROM jute_mr WHERE jute_mr_id = :id"),
                        {"id": MR_ID}).fetchone()
        assert mr and mr.po_id == OLD_PO and mr.branch_id == 29, f"unexpected MR state {mr}"
        new_li = db.execute(text("SELECT allowable_moisture FROM jute_po_li WHERE jute_po_li_id = :id AND jute_po_id = :po"),
                            {"id": NEW_PO_LI, "po": NEW_PO}).fetchone()
        new_mukam = db.execute(text("SELECT jute_mukam_id FROM jute_po WHERE jute_po_id = :po"), {"po": NEW_PO}).scalar()

        n1 = db.execute(text("""UPDATE jute_mr SET po_id = :po, mukam_id = :mukam WHERE jute_mr_id = :id AND po_id = :old"""),
                        {"po": NEW_PO, "mukam": new_mukam, "id": MR_ID, "old": OLD_PO}).rowcount
        n2 = db.execute(text("""UPDATE jute_mr_li SET jute_po_li_id = :li, allowable_moisture = :am
                                WHERE jute_mr_li_id = :id AND jute_mr_id = :mr AND jute_po_li_id = :old"""),
                        {"li": NEW_PO_LI, "am": new_li.allowable_moisture, "id": MR_LI_ID, "mr": MR_ID, "old": OLD_PO_LI}).rowcount
        assert (n1, n2) == (1, 1), f"rowcounts {n1}, {n2}"
        print(f"MR {MR_ID}: po {OLD_PO} -> {NEW_PO}, mukam -> {new_mukam}; line {MR_LI_ID}: po_li {OLD_PO_LI} -> {NEW_PO_LI}, "
              f"allowable_moisture -> {new_li.allowable_moisture}")

        for po in (OLD_PO, NEW_PO):
            col = []
            d = ps.recompute_po_settlement(db, po, user_id=1, source="AUTO", trigger_ref=TRIGGER, collector=col)
            print(f"PO {po}: {d} | {col[0].detail}")

        if apply:
            db.commit()
            print("APPLIED")
        else:
            db.rollback()
            print("DRY-RUN (rolled back). Re-run with --apply to write.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
