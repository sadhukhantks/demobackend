"""Move a jute MR that was booked against the wrong PO to the right one (same branch, same party).

What it changes (one transaction):
  * jute_mr.po_id and mukam_id (the new PO's mukam)
  * each line's jute_po_li_id: the new PO line with the same item, else the line in the same
    position; allowable_moisture is re-copied from the new PO line (as the gate entry does);
    rate / actual_rate are re-copied ONLY when they still equal the old PO line's rate
    (an MR-side rate override is left alone)
  * when the MR already has money totals (Pending / Approved), recompute_mr_money (TDS kept)
  * PO settlement recomputed for the old and the new PO

Refuses: Approved (3) MRs with bill pass complete, a different branch / party, live issues are
fine (issues key on the MR line, not the PO), a juteTransfer chain (children or hop) is refused.

Dry-run by default. Run from the vowerp3be folder:
  PYTHONPATH=. uv run python dbqueries/repairs/move_mr_to_po.py <tenant> <jute_mr_id> <new_jute_po_id> [--apply]
Prints the before-state so the change can be reversed with the same script (old PO id).
"""
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.config.db import get_tenant_engine
from src.juteProcurement import po_settlement as ps
from src.juteProcurement.mr import recompute_mr_money


def move(db: Session, mr_id: int, new_po_id: int) -> list[str]:
    out = []
    mr = db.execute(text("""SELECT jute_mr_id, branch_id, po_id, mukam_id, status_id, party_id,
                                   COALESCE(bill_pass_complete, 0) AS bpc, total_amount
                            FROM jute_mr WHERE jute_mr_id = :id"""), {"id": mr_id}).mappings().fetchone()
    po = db.execute(text("SELECT jute_po_id, branch_id, party_id, jute_mukam_id FROM jute_po WHERE jute_po_id = :id"),
                    {"id": new_po_id}).mappings().fetchone()
    assert mr and po, "MR or PO not found"
    assert mr["po_id"] != new_po_id, "MR is already on that PO"
    assert mr["branch_id"] == po["branch_id"], "different branch"
    assert str(mr["party_id"] or "") == str(po["party_id"] or ""), "different party"
    assert mr["status_id"] in (1, 13, 20, 3), f"status {mr['status_id']} not movable"
    assert not mr["bpc"], "bill pass already complete"
    try:
        chain = db.execute(text("""SELECT (src_jute_mr_id IS NOT NULL) AS hop,
                                          (SELECT COUNT(*) FROM jute_mr c WHERE c.src_jute_mr_id = :id AND c.jute_mr_id <> :id) AS kids
                                   FROM jute_mr WHERE jute_mr_id = :id"""), {"id": mr_id}).mappings().fetchone()
        assert not chain["hop"] and not chain["kids"], "MR is part of a juteTransfer chain"
    except AssertionError:
        raise
    except Exception:
        db.rollback()  # tenant without the transfer schema

    old_po_id = mr["po_id"]
    out.append(f"BEFORE MR {mr_id}: po {old_po_id}, mukam {mr['mukam_id']}, status {mr['status_id']}, total {mr['total_amount']}")
    db.execute(text("UPDATE jute_mr SET po_id = :po, mukam_id = :mukam WHERE jute_mr_id = :id"),
               {"po": new_po_id, "mukam": po["jute_mukam_id"], "id": mr_id})

    new_lines = db.execute(text("""SELECT jute_po_li_id, item_id, rate, allowable_moisture FROM jute_po_li
                                   WHERE jute_po_id = :po AND (active = 1 OR active IS NULL) ORDER BY jute_po_li_id"""),
                           {"po": new_po_id}).mappings().all()
    old_lines = {r["jute_po_li_id"]: r for r in db.execute(text(
        "SELECT jute_po_li_id, rate FROM jute_po_li WHERE jute_po_id = :po ORDER BY jute_po_li_id"), {"po": old_po_id}).mappings()}
    old_order = list(old_lines)
    mr_lines = db.execute(text("""SELECT jute_mr_li_id, jute_po_li_id, actual_item_id, challan_item_id, rate, actual_rate, allowable_moisture
                                  FROM jute_mr_li WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)
                                  ORDER BY jute_mr_li_id"""), {"id": mr_id}).mappings().all()
    for li in mr_lines:
        item = li["actual_item_id"] or li["challan_item_id"]
        target = next((n for n in new_lines if n["item_id"] == item), None)
        if target is None:
            pos = old_order.index(li["jute_po_li_id"]) if li["jute_po_li_id"] in old_order else 0
            target = new_lines[min(pos, len(new_lines) - 1)]
        old_rate = (old_lines.get(li["jute_po_li_id"]) or {}).get("rate")
        params = {"id": li["jute_mr_li_id"], "li": target["jute_po_li_id"], "am": target["allowable_moisture"]}
        sets = ["jute_po_li_id = :li", "allowable_moisture = :am"]
        if old_rate is not None and li["rate"] == old_rate:
            sets.append("rate = :rate")
            params["rate"] = target["rate"]
        if old_rate is not None and li["actual_rate"] == old_rate:
            sets.append("actual_rate = :rate")
            params["rate"] = target["rate"]
        db.execute(text(f"UPDATE jute_mr_li SET {', '.join(sets)} WHERE jute_mr_li_id = :id"), params)
        out.append(f"  line {li['jute_mr_li_id']} (item {item}): po_li {li['jute_po_li_id']} -> {target['jute_po_li_id']}, "
                   f"moisture {li['allowable_moisture']} -> {target['allowable_moisture']}, "
                   f"rate {li['rate']} -> {params.get('rate', li['rate'])}, actual_rate {li['actual_rate']} -> "
                   f"{params['rate'] if 'actual_rate = :rate' in sets else li['actual_rate']}")

    if mr["total_amount"] is not None:
        out.append(f"  money recomputed: {recompute_mr_money(db, mr_id)}")
    trigger = f"REPAIR:move_mr_to_po MR {mr_id} {old_po_id}->{new_po_id}"
    for p in (old_po_id, new_po_id):
        col = []
        d = ps.recompute_po_settlement(db, p, user_id=1, source="AUTO", trigger_ref=trigger, collector=col)
        out.append(f"  PO {p} settlement: {d} | {col[0].detail}")
    return out


if __name__ == "__main__":
    tenant, mr_id, new_po = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    with Session(get_tenant_engine(tenant)) as db:
        print("\n".join(move(db, mr_id, new_po)))
        if "--apply" in sys.argv:
            db.commit()
            print("APPLIED")
        else:
            db.rollback()
            print("DRY-RUN (rolled back). Add --apply to write.")
