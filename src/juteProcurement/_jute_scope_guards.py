"""
Ownership guards for the two jute-MR fields the QC and MR pages now write.

Both `jute_mr.party_branch_id` and `jute_mr_li.warehouse_id` are bare integer
FKs with no DB-level constraint, so an id belonging to another party or another
branch stores happily and only surfaces much later as a NULL address/GST at
bill pass, or as stock attributed to a godown of a different site.

Shared by materialInspection.complete_inspection and mr.update_mr so both
writers enforce the same rule — a guard in only one of them leaves the other
open.
"""

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session


def validate_party_branch(db: Session, party_branch_id: Optional[int], party_id) -> None:
    """400 unless the branch is an active branch OF that party."""
    if party_branch_id is None:
        return
    if party_id in (None, "", 0):
        raise HTTPException(
            status_code=400,
            detail="Cannot set a party branch without a party",
        )
    ok = db.execute(
        text("""
            SELECT 1 FROM party_branch_mst
            WHERE party_mst_branch_id = :party_branch_id
              AND party_id = :party_id
              AND (active = 1 OR active IS NULL)
        """),
        {"party_branch_id": party_branch_id, "party_id": party_id},
    ).scalar()
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Selected party branch does not belong to the selected party",
        )


def validate_warehouse(db: Session, warehouse_id: Optional[int], branch_id: Optional[int]) -> None:
    """400 unless the godown belongs to the record's own branch."""
    if warehouse_id is None:
        return
    if branch_id is None:
        raise HTTPException(
            status_code=400,
            detail="Cannot set a godown without a branch on the record",
        )
    ok = db.execute(
        text("""
            SELECT 1 FROM warehouse_mst
            WHERE warehouse_id = :warehouse_id AND branch_id = :branch_id
        """),
        {"warehouse_id": warehouse_id, "branch_id": branch_id},
    ).scalar()
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Selected godown does not belong to this branch",
        )


# ---------------------------------------------------------------------------
# Issued lines must stay intact (owner ruling 2026-10-05)
#
# An MR / QC save may not cut a line's quantity below what is already issued from
# it, nor change the item of a line that has live issues: the issues would keep the
# old line / item and the stock goes negative on one line and positive on another
# (sls MRs 28586 / 28592, 2026-10-05). Weight-only over-issue stays a WARNING
# (owner ruling 2026-09-03: fixed afterwards with 'Sync issued weights').
#
# Usage: snapshot before the line writes, check after them (same transaction).
# Only lines this save actually changed are judged, so an older mismatch never
# blocks an unrelated save.
# ---------------------------------------------------------------------------

_ISSUED_LINES_SQL = text("""
    SELECT li.jute_mr_li_id, li.actual_item_id, COALESCE(li.actual_qty, 0) AS actual_qty,
           COALESCE(SUM(ji.quantity), 0) AS issued_qty,
           GROUP_CONCAT(ji.jute_issue_id ORDER BY ji.jute_issue_id) AS issue_ids
    FROM jute_mr_li li
    JOIN jute_issue ji ON ji.jute_mr_li_id = li.jute_mr_li_id AND ji.status_id <> 4
    WHERE li.jute_mr_id = :mr_id
    GROUP BY li.jute_mr_li_id, li.actual_item_id, li.actual_qty
""")


def snapshot_issued_lines(db: Session, mr_id: int) -> dict:
    """{jute_mr_li_id: row} for the MR's lines that have live issues."""
    return {r.jute_mr_li_id: r for r in db.execute(_ISSUED_LINES_SQL, {"mr_id": int(mr_id)}).fetchall()}


def assert_issued_lines_intact(db: Session, mr_id: int, before: dict) -> None:
    """409 when this save cut an issued line below its issued quantity or changed its item."""
    if not before:
        return
    problems = []
    for li_id, now in snapshot_issued_lines(db, mr_id).items():
        was = before.get(li_id)
        if was is None:
            continue
        issued = float(now.issued_qty or 0)
        if now.actual_item_id != was.actual_item_id:
            problems.append(f"line {li_id}: the item cannot change while issues {now.issue_ids} are drawn from it")
        elif float(now.actual_qty) != float(was.actual_qty) and float(now.actual_qty) < issued - 0.001:
            problems.append(
                f"line {li_id}: quantity {float(now.actual_qty):g} is below the {issued:g} already issued "
                f"(issues {now.issue_ids})"
            )
    if problems:
        raise HTTPException(
            status_code=409,
            detail="; ".join(problems) + ". Move or reject those issues first, then change the MR line.",
        )
