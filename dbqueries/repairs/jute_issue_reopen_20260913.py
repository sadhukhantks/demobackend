"""
ISSUEREOPEN-20260913 -- TKT-000158: roll the APPROVED jute issues of Limelight
(sls co 106 "Limelight Commercial Pvt. Ltd.", branch 87 FACTORY) dated
2026-04-01 / 2026-04-02 back from Approved (3) so the floor can correct them.

Why a repair and not an endpoint: the jute-issue lifecycle in
src/juteProcurement/issue.py is 21 Draft -> 1 Open -> 3 Approved / 4 Rejected
with NO reopen path, and update/delete only accept Draft (21). Un-approving is
an owner decision per document, so it is done here, tagged and reversible.

Target status (owner picks):
    --to open   (default)  status 1  -> the lines can be REJECTED (4) from the
                           page, which gives the stock back; re-enter correct lines.
    --to draft             status 21 -> the lines can be EDITED / deleted in place.

Stock impact of this script itself: NONE. vw_jute_stock_outstanding nets every
issue with status_id <> 4, so 3 -> 1 / 21 leaves balances unchanged; only the
user's later reject / edit moves stock. --check prints the current balance of
each MR line the rows draw on (lines 45030 / 45012 are the negative ones).

updated_by := 'ISSUEREOPEN-20260913' on every touched row (rollback handle).

Usage (repo root, venv active):
    python dbqueries/repairs/jute_issue_reopen_20260913.py --check [--to open|draft]
    python dbqueries/repairs/jute_issue_reopen_20260913.py --apply [--to open|draft]
--check is SELECT-only and writes the plan CSV + rollback SQL beside this file.
--apply runs the plan in ONE transaction and post-verifies every row landed.
"""
import csv
import datetime
import sys
from pathlib import Path

import pymysql

ENV = Path(__file__).resolve().parents[2] / "env" / "database.env"
DB = "sls"
CO_ID = 106
BRANCH_ID = 87
DATES = (datetime.date(2026, 4, 1), datetime.date(2026, 4, 2))
FROM_STATUS = 3
TARGETS = {"open": 1, "draft": 21}
TAG = "ISSUEREOPEN-20260913"

PLAN_CSV = Path(__file__).with_name("jute_issue_reopen_20260913_plan.csv")
ROLLBACK_SQL = Path(__file__).with_name("jute_issue_reopen_20260913_rollback.sql")

# From the read-only assessment (2026-09-13). Drift = warn, not fail.
EXPECT_IDS = {86351, 86352, 86353, 86354, 86355, 86356, 86357, 86358, 86359, 86360}


# --------------------------------------------------------------------------
# pure planning (unit-tested in src/test/test_jute_issue_reopen_repair.py)
# --------------------------------------------------------------------------
def plan_rows(rows, target_status):
    """One plan row per approved issue; anything not at FROM_STATUS is skipped."""
    return [{
        "jute_issue_id": r["jute_issue_id"], "issue_date": r["issue_date"],
        "jute_mr_li_id": r["jute_mr_li_id"], "branch_mr_no": r.get("branch_mr_no"),
        "party": r.get("party"), "quantity": r.get("quantity"), "weight": r.get("weight"),
        "status_before": r["status_id"], "status_after": target_status,
        "updated_by_before": r.get("updated_by"), "update_date_time_before": r.get("update_date_time"),
    } for r in rows if r["status_id"] == FROM_STATUS]


def rollback_sql(plan):
    def lit(v):
        return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"
    out = [f"-- Rollback for {TAG} ({len(plan)} rows). Run against {DB}.", "START TRANSACTION;"]
    for p in plan:
        out.append(
            f"UPDATE jute_issue SET status_id = {p['status_before']}, updated_by = {lit(p['updated_by_before'])}, "
            f"update_date_time = {lit(p['update_date_time_before'])} "
            f"WHERE jute_issue_id = {p['jute_issue_id']} AND updated_by = '{TAG}';")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# db
# --------------------------------------------------------------------------
def _env():
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def connect():
    e = _env()
    return pymysql.connect(
        host=e["DATABASE_HOST"], port=int(e.get("DATABASE_PORT", 3306)),
        user=e["DATABASE_USER"], password=e["DATABASE_PASSWORD"], database=DB,
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )


SCOPE_SQL = """
SELECT ji.jute_issue_id, ji.issue_date, ji.status_id, ji.jute_mr_li_id, ji.quantity,
       CAST(ji.weight AS DECIMAL(18,3)) AS weight, ji.updated_by, ji.update_date_time,
       jm.branch_mr_no, p.supp_name AS party
FROM jute_issue ji
JOIN branch_mst bm ON bm.branch_id = ji.branch_id
LEFT JOIN jute_mr_li jml ON jml.jute_mr_li_id = ji.jute_mr_li_id
LEFT JOIN jute_mr jm ON jm.jute_mr_id = jml.jute_mr_id
LEFT JOIN party_mst p ON p.party_id = jm.party_id
WHERE bm.co_id = %s AND ji.branch_id = %s AND ji.issue_date IN %s AND ji.status_id = %s
ORDER BY ji.issue_date, ji.jute_issue_id
"""

BALANCE_SQL = """
SELECT jute_mr_li_id, branch_mr_no, actual_qty, actual_weight, bal_qty, bal_weight
FROM vw_jute_stock_outstanding WHERE jute_mr_li_id IN %s ORDER BY jute_mr_li_id
"""


def summarise(cur, plan):
    target = plan[0]["status_after"] if plan else "-"
    print(f"{len(plan)} approved issue(s) on {DB} co{CO_ID} br{BRANCH_ID} {DATES[0]}..{DATES[-1]} -> status {target}")
    for p in plan:
        print(f"   issue {p['jute_issue_id']} {p['issue_date']} line {p['jute_mr_li_id']} MR#{p['branch_mr_no']} "
              f"{p['party']}: {p['quantity']} bales / {p['weight']} kg")
    got = {p["jute_issue_id"] for p in plan}
    if got != EXPECT_IDS:
        print(f"   !! expected ids {sorted(EXPECT_IDS)}, got {sorted(got)}")
    lines = tuple(sorted({p["jute_mr_li_id"] for p in plan if p["jute_mr_li_id"]}))
    if lines:
        cur.execute(BALANCE_SQL, (lines,))
        print("MR line balances today (unchanged by this script; negative = over-issued):")
        for r in cur.fetchall():
            neg = float(r["bal_weight"] or 0) < 0 or float(r["bal_qty"] or 0) < 0
            print(f"   line {r['jute_mr_li_id']} MR#{r['branch_mr_no']} bal {r['bal_qty']} bales / "
                  f"{r['bal_weight']} kg{'  <-- NEGATIVE' if neg else ''}")


def write_artifacts(plan):
    cols = ["jute_issue_id", "issue_date", "jute_mr_li_id", "branch_mr_no", "party", "quantity", "weight",
            "status_before", "status_after", "updated_by_before", "update_date_time_before"]
    with PLAN_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(plan)
    ROLLBACK_SQL.write_text(rollback_sql(plan), encoding="utf-8")
    print(f"\nwrote {PLAN_CSV.name}, {ROLLBACK_SQL.name}")


def apply(cur, plan):
    sql = ("UPDATE jute_issue SET status_id = %s, updated_by = %s, update_date_time = NOW() "
           "WHERE jute_issue_id = %s AND status_id = %s")
    n = 0
    for p in plan:
        n += cur.execute(sql, (p["status_after"], TAG, p["jute_issue_id"], p["status_before"]))
    print(f"updated {n} jute_issue rows")
    return n


def verify(cur, plan):
    cur.execute("SELECT jute_issue_id, status_id, updated_by FROM jute_issue WHERE jute_issue_id IN %s",
                (tuple(p["jute_issue_id"] for p in plan),))
    want = {p["jute_issue_id"]: p["status_after"] for p in plan}
    return [f"issue {r['jute_issue_id']} status {r['status_id']} != {want[r['jute_issue_id']]}"
            for r in cur.fetchall() if r["status_id"] != want[r["jute_issue_id"]] or r["updated_by"] != TAG]


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "--check"
    to = args[args.index("--to") + 1] if "--to" in args else "open"
    if mode not in ("--check", "--apply") or to not in TARGETS:
        sys.exit("usage: jute_issue_reopen_20260913.py [--check|--apply] [--to open|draft]")
    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute(SCOPE_SQL, (CO_ID, BRANCH_ID, DATES, FROM_STATUS))
        plan = plan_rows(cur.fetchall(), TARGETS[to])
        summarise(cur, plan)
        write_artifacts(plan)
        if mode != "--apply":
            print("\nCheck-only mode, no changes. Run with --apply to execute.")
            return
        if not plan:
            sys.exit("nothing to apply")
        n = apply(cur, plan)
        if n != len(plan):
            conn.rollback()
            sys.exit(f"!! updated {n} rows but planned {len(plan)} -- rolled back")
        errors = verify(cur, plan)
        if errors:
            conn.rollback()
            print("\n".join(errors))
            sys.exit("!! post-verify failed -- rolled back, nothing written")
        conn.commit()
        print(f"\nCOMMITTED {n} rows, tag {TAG}. Rollback: {ROLLBACK_SQL.name}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
