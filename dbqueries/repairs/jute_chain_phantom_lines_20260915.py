"""
CHAINPHANTOM-20260915 -- vertical transfer chain (juteTransfer app) cloned
soft-deleted root lines (jute_mr_li.active = 0, ERP QC edit) into the child
MRs it created for the next company. The clones are zero-kg / zero-amount
(the ERP zeroes a line when it deactivates it) so money and stock are
untouched, but the chain's finalize / unfinalize match root lines to child
lines POSITIONALLY. juteTransfer now reads active lines only (fix 2026-09-15),
so an already-built chain with phantoms would mis-pair lines on its next
finalize / unfinalize. Deactivating the clones restores 1:1 pairing.

Plan = child line at position k where the root line at position k (both in
jute_mr_li_id order, same count) is active = 0. Guards per child line:
accepted_weight = 0, total_price = 0, no jute_issue / jute_lot_src /
sales_invoice_dtl reference. A child failing any guard is reported and
skipped; nothing else changes.

Usage (repo root, venv active):
    python dbqueries/repairs/jute_chain_phantom_lines_20260915.py --check
    python dbqueries/repairs/jute_chain_phantom_lines_20260915.py --apply
--check is SELECT-only and writes the plan CSV + rollback SQL beside this file.
--apply runs the plan in ONE transaction and post-verifies every row landed.
"""
import csv
import sys
from pathlib import Path

import pymysql

ENV = Path(__file__).resolve().parents[2] / "env" / "database.env"
DB = "sls"
TAG = "CHAINPHANTOM-20260915"
PLAN_CSV = Path(__file__).with_name("jute_chain_phantom_lines_20260915_plan.csv")
ROLLBACK_SQL = Path(__file__).with_name("jute_chain_phantom_lines_20260915_rollback.sql")

CHILDREN_SQL = """
SELECT c.jute_mr_id AS child, c.src_jute_mr_id AS root, c.branch_id, c.branch_mr_no
FROM jute_mr c
WHERE c.transfer_mode = 0 AND c.src_jute_mr_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM jute_mr_li r WHERE r.jute_mr_id = c.src_jute_mr_id AND r.active = 0)
ORDER BY c.jute_mr_id"""
LINES_SQL = """
SELECT li.jute_mr_li_id, COALESCE(li.active, 1) AS active, li.accepted_weight, li.total_price,
       (SELECT COUNT(*) FROM jute_issue ji WHERE ji.jute_mr_li_id = li.jute_mr_li_id) AS issues,
       (SELECT COUNT(*) FROM jute_lot_src ls WHERE ls.new_jute_mr_li_id = li.jute_mr_li_id
                                                OR ls.src_jute_mr_li_id = li.jute_mr_li_id) AS lots,
       (SELECT COUNT(*) FROM sales_invoice_dtl d WHERE d.jute_mr_li_id = li.jute_mr_li_id) AS inv_lines
FROM jute_mr_li li WHERE li.jute_mr_id = %s ORDER BY li.jute_mr_li_id"""


def plan_child(child, root, root_lines, child_lines):
    """Pure: (plan_rows, skip_reason). Positional pairing, every guard must hold."""
    if len(root_lines) != len(child_lines):
        return [], f"line count root={len(root_lines)} child={len(child_lines)}"
    rows = []
    for r, c in zip(root_lines, child_lines):
        if r["active"] != 0:
            continue
        if c["active"] == 0:
            continue  # already done (re-run safe)
        if float(c["accepted_weight"] or 0) or float(c["total_price"] or 0):
            return [], f"child line {c['jute_mr_li_id']} carries weight/amount"
        if c["issues"] or c["lots"] or c["inv_lines"]:
            return [], f"child line {c['jute_mr_li_id']} is referenced (issue/lot/invoice)"
        rows.append({"child": child, "root": root, "root_li_id": r["jute_mr_li_id"],
                     "child_li_id": c["jute_mr_li_id"]})
    return rows, None


def connect():
    env = dict(l.strip().split("=", 1) for l in ENV.read_text().splitlines()
               if "=" in l and not l.startswith("#"))
    return pymysql.connect(host=env["DATABASE_HOST"], user=env["DATABASE_USER"],
                           password=env["DATABASE_PASSWORD"], database=DB,
                           port=int(env.get("DATABASE_PORT", 3306)),
                           cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def build_plan(cur):
    cur.execute(CHILDREN_SQL)
    plan, skipped = [], []
    for ch in cur.fetchall():
        cur.execute(LINES_SQL, (ch["root"],)); root_lines = cur.fetchall()
        cur.execute(LINES_SQL, (ch["child"],)); child_lines = cur.fetchall()
        rows, why = plan_child(ch["child"], ch["root"], root_lines, child_lines)
        if why:
            skipped.append((ch["child"], why))
        plan.extend(rows)
    return plan, skipped


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("--check", "--apply"):
        sys.exit(__doc__)
    apply = sys.argv[1] == "--apply"
    conn = connect(); cur = conn.cursor()
    plan, skipped = build_plan(cur)
    children = sorted({p["child"] for p in plan})
    print(f"{DB}: {len(plan)} phantom child lines across {len(children)} child MRs")
    for ch, why in skipped:
        print(f"  SKIP child {ch}: {why}")
    if not plan:
        return
    with PLAN_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(plan[0].keys())); w.writeheader(); w.writerows(plan)
    ids = ",".join(str(p["child_li_id"]) for p in plan)
    ROLLBACK_SQL.write_text(
        f"-- rollback {TAG}: re-activate the {len(plan)} cloned lines\n"
        f"UPDATE jute_mr_li SET active = 1 WHERE jute_mr_li_id IN ({ids});\n")
    print(f"plan -> {PLAN_CSV.name}, rollback -> {ROLLBACK_SQL.name}")
    if not apply:
        return
    try:
        cur.execute(f"UPDATE jute_mr_li SET active = 0, updated_date_time = NOW() "
                    f"WHERE jute_mr_li_id IN ({ids}) AND COALESCE(active, 1) = 1")
        touched = cur.rowcount
        cur.execute(f"SELECT COUNT(*) AS n FROM jute_mr_li WHERE jute_mr_li_id IN ({ids}) AND active = 0")
        landed = cur.fetchone()["n"]
        if landed != len(plan):
            raise RuntimeError(f"post-verify: {landed} of {len(plan)} inactive")
        conn.commit()
        print(f"APPLIED {TAG}: {touched} rows deactivated, {landed} verified")
    except Exception:
        conn.rollback(); raise


if __name__ == "__main__":
    main()
