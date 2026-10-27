"""
MRLINEWT-20260912 — push jute_mr.actual_weight down to jute_mr_li lines that QC
saved BEFORE gate-out weighed the lorry (so every line sits at 0 kg while the
header carries the net). sls, branch 29. Code fix ships in the same change
(juteGateEntry OUT action now calls totals.redistribute_mr_line_weights); this
repairs what is already stored.

Scope (per MR): branch_id = 29, status_id IN (1, 13) (Open / Pending — an
Approved MR has been through MR-page save and owns its money), header
actual_weight > 0, at least one active line with actual_qty > 0, and EVERY
active line at actual_weight 0/NULL (never touch a partially-weighted MR).
Rule: totals.plan_mr_line_weights — proportional to qty, whole kg,
largest-remainder, moisture deduction as on MR save.

Usage (repo root, venv active):
    python dbqueries/repairs/jute_mr_line_wt_redistribute_20260912.py --check
    python dbqueries/repairs/jute_mr_line_wt_redistribute_20260912.py --apply

--check is SELECT-only: prints the plan, writes *_plan.csv and *_rollback.sql.
--apply runs the plan in ONE transaction and re-asserts sum(line) == header.
Rollback = run the generated .sql (restores the 0 weights).
"""
import csv
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.juteProcurement.totals import plan_mr_line_weights  # noqa: E402

ENV = ROOT / "env" / "database.env"
DB = "sls"
BRANCH = 29
STATUSES = (1, 13)
PLAN_CSV = Path(__file__).with_name("jute_mr_line_wt_redistribute_20260912_plan.csv")
ROLLBACK_SQL = Path(__file__).with_name("jute_mr_line_wt_redistribute_20260912_rollback.sql")


def connect():
    env = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]),
                           user=env["DATABASE_USER"], password=env["DATABASE_PASSWORD"],
                           database=DB, cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def load_plan(cur):
    cur.execute("""
        SELECT jm.jute_mr_id, jm.branch_id, jm.status_id, jm.actual_weight AS header_kg,
               SUM(CASE WHEN li.actual_qty > 0 THEN 1 ELSE 0 END) AS qty_lines,
               SUM(COALESCE(li.actual_weight, 0)) AS line_kg
        FROM jute_mr jm JOIN jute_mr_li li ON li.jute_mr_id = jm.jute_mr_id AND li.active = 1
        WHERE jm.branch_id = %s AND jm.status_id IN %s AND jm.actual_weight > 0
        GROUP BY jm.jute_mr_id, jm.branch_id, jm.status_id, jm.actual_weight
        HAVING qty_lines > 0 AND line_kg = 0
        ORDER BY jm.jute_mr_id
    """, (BRANCH, STATUSES))
    mrs = cur.fetchall()
    plan = []
    for mr in mrs:
        cur.execute("""
            SELECT jute_mr_li_id, actual_qty, allowable_moisture, actual_moisture,
                   COALESCE(actual_weight, 0) AS old_weight, COALESCE(shortage_kgs, 0) AS old_shortage,
                   COALESCE(accepted_weight, 0) AS old_accepted
            FROM jute_mr_li WHERE jute_mr_id = %s AND active = 1 ORDER BY jute_mr_li_id
        """, (mr["jute_mr_id"],))
        lines = cur.fetchall()
        for old, new in zip(lines, plan_mr_line_weights(mr["header_kg"], lines)):
            plan.append({"jute_mr_id": mr["jute_mr_id"], "status_id": mr["status_id"],
                         "header_kg": mr["header_kg"], **old, **{f"new_{k}": v for k, v in new.items() if k != "jute_mr_li_id"}})
    return plan


def write_files(plan):
    with PLAN_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(plan[0].keys()) if plan else ["empty"])
        w.writeheader()
        w.writerows(plan)
    with ROLLBACK_SQL.open("w") as f:
        f.write(f"-- MRLINEWT-20260912 rollback ({DB}, branch {BRANCH}) — restores pre-repair line weights\n")
        for p in plan:
            f.write(f"UPDATE jute_mr_li SET actual_weight = {p['old_weight']}, shortage_kgs = {p['old_shortage']}, "
                    f"accepted_weight = {p['old_accepted']} WHERE jute_mr_li_id = {p['jute_mr_li_id']};\n")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    conn = connect()
    try:
        cur = conn.cursor()
        plan = load_plan(cur)
        write_files(plan)
        mrs = sorted({p["jute_mr_id"] for p in plan})
        print(f"{DB} br{BRANCH}: {len(mrs)} MRs / {len(plan)} lines -> {PLAN_CSV.name}, {ROLLBACK_SQL.name}")
        for p in plan:
            print(f"  MR {p['jute_mr_id']} st{p['status_id']} hdr {p['header_kg']:>8.0f} | li {p['jute_mr_li_id']} "
                  f"qty {p['actual_qty']:>6} -> {p['new_actual_weight']:>6} kg, accepted {p['new_accepted_weight']}")
        if mode != "--apply":
            conn.rollback()
            return
        for p in plan:
            cur.execute("UPDATE jute_mr_li SET actual_weight=%s, shortage_kgs=%s, accepted_weight=%s WHERE jute_mr_li_id=%s",
                        (p["new_actual_weight"], p["new_shortage_kgs"], p["new_accepted_weight"], p["jute_mr_li_id"]))
        for mr_id in mrs:
            cur.execute("""SELECT jm.actual_weight AS h, SUM(li.actual_weight) AS s FROM jute_mr jm
                           JOIN jute_mr_li li ON li.jute_mr_id = jm.jute_mr_id AND li.active = 1
                           WHERE jm.jute_mr_id = %s GROUP BY jm.actual_weight""", (mr_id,))
            r = cur.fetchone()
            assert abs(float(r["h"]) - float(r["s"])) < 0.5, f"MR {mr_id}: header {r['h']} != lines {r['s']}"
        conn.commit()
        print(f"APPLIED: {len(plan)} lines, {len(mrs)} MRs verified sum(line) == header")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
