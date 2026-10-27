"""
KGFIX2-20260905 — close over-issued MR lines and reprice issues at the production
rate on sls, branches 29 + 87 (companion to jute_issue_kg_round_20260905.py).

Owner rulings 2026-09-05:
  * jute_mr_li carries TWO rates: `rate` = approved rate -> accounting (bill pass,
    total_price, transfer invoices); `actual_rate` = production rate -> jute stock
    / batch-cost reports, i.e. jute_issue.issue_value = weight x actual_rate / 100.
  * "If over issued, the last issue is reduced to make it 0": a line whose issues
    exceed its received bales/kg has its LAST issue (issue_date, id) cut back by the
    excess so the line balance is exactly zero on both ledgers.

PHASE A  CLOSE     eligible lines (jute_mr.status NOT IN (4,6,21), li.active = 1,
                   li.status not 4/6) in branches 29/87 with SUM(qty) > actual_qty
                   or SUM(weight) > actual_weight (status <> 4 issues, all dates):
                   last issue: quantity -= excess bales, weight -= excess kg.
                   Hard-fails if that would take the row below zero.
PHASE B  REPRICE   in-scope issues (branch 29/87, issue_date >= 2026-03-31,
                   status <> 4, updated_by NOT LIKE 'STOCKADJ%', on an MR line) whose
                   line has a non-NULL actual_rate and whose issue_value differs from
                   ROUND(weight x actual_rate / 100, 2) by more than 0.01.
                   Lines with NULL actual_rate are left alone (reports already fall
                   back to `rate` when issue_value is 0).

updated_by := 'KGFIX2-20260905' on every touched row (rollback handle).

Usage (repo root, venv active):
    python dbqueries/repairs/jute_issue_close_reprice_20260905.py --check
    python dbqueries/repairs/jute_issue_close_reprice_20260905.py --apply
--check is SELECT-only and writes the plan CSV + rollback SQL beside this file.
--apply runs the plan in ONE transaction and post-verifies: no closed line keeps a
non-zero balance, no repriced row deviates from weight x actual_rate / 100.
"""
import csv
import datetime
import sys
from pathlib import Path

import pymysql

ENV = Path(__file__).resolve().parents[2] / "env" / "database.env"
DB = "sls"
BRANCHES = (29, 87)
CUTOFF = datetime.date(2026, 3, 31)
TAG = "KGFIX2-20260905"
TOL = 0.001
VALUE_TOL = 0.01

PLAN_CSV = Path(__file__).with_name("jute_issue_close_reprice_20260905_plan.csv")
ROLLBACK_SQL = Path(__file__).with_name("jute_issue_close_reprice_20260905_rollback.sql")

# From the read-only assessment (2026-09-05). Drift = warn, not fail.
EXPECT_CLOSE_LINES = {45030, 45012}


# --------------------------------------------------------------------------
# pure planning (unit-tested in src/test/test_jute_issue_close_reprice_repair.py)
# --------------------------------------------------------------------------
def close_line(aq, aw, issues):
    """`issues` = every non-rejected issue on the line ordered (issue_date, id).

    Returns (jute_issue_id, new_qty, new_weight) for the last issue, or None when
    the line is not over-issued. Raises ValueError if the cut would go below 0.
    """
    aq, aw = float(aq or 0), float(aw or 0)
    if not issues:
        return None
    sum_q = sum(float(i["quantity"] or 0) for i in issues)
    sum_w = sum(float(i["weight"] or 0) for i in issues)
    over_q = max(0.0, sum_q - aq)
    over_w = max(0.0, sum_w - aw)
    if over_q <= TOL and over_w <= TOL:
        return None
    last = issues[-1]
    new_q = round(float(last["quantity"] or 0) - over_q, 3)
    new_w = round(float(last["weight"] or 0) - over_w, 3)
    if new_q < -TOL or new_w < -TOL:
        raise ValueError(
            f"line over-issue exceeds its last issue {last['jute_issue_id']}: "
            f"qty {last['quantity']}-{over_q}, weight {last['weight']}-{over_w}"
        )
    return last["jute_issue_id"], max(0.0, new_q), max(0.0, new_w)


def reprice(weight, value, actual_rate):
    """New issue_value at the production rate, or None when nothing changes."""
    if actual_rate is None:
        return None
    new_v = round(float(weight or 0) * float(actual_rate) / 100.0, 2)
    return new_v if abs(new_v - float(value or 0)) > VALUE_TOL else None


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


OVER_LINES_SQL = """
SELECT jml.jute_mr_li_id, jm.branch_id, jm.branch_mr_no, jml.actual_qty, jml.actual_weight, jml.actual_rate
FROM jute_mr_li jml
JOIN jute_mr jm ON jm.jute_mr_id = jml.jute_mr_id
JOIN jute_issue ji ON ji.jute_mr_li_id = jml.jute_mr_li_id AND ji.status_id <> 4
WHERE jm.branch_id IN %s AND jml.active = 1 AND jm.status_id NOT IN (4, 6, 21)
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'))
GROUP BY 1, 2, 3, 4, 5, 6
HAVING SUM(ji.quantity) > jml.actual_qty + 0.001
    OR CAST(SUM(ji.weight) AS DECIMAL(18,6)) > jml.actual_weight + 0.001
ORDER BY jm.branch_id, jml.jute_mr_li_id
"""

LINE_ISSUES_SQL = """
SELECT jute_issue_id, jute_mr_li_id, branch_id, issue_date, status_id,
       quantity, CAST(weight AS DECIMAL(18,6)) AS weight, issue_value, updated_by
FROM jute_issue WHERE jute_mr_li_id IN %s AND status_id <> 4
ORDER BY jute_mr_li_id, issue_date, jute_issue_id
"""

SCOPE_SQL = """
SELECT ji.jute_issue_id, ji.jute_mr_li_id, ji.branch_id, ji.issue_date, ji.status_id,
       ji.quantity, CAST(ji.weight AS DECIMAL(18,6)) AS weight, ji.issue_value, ji.updated_by,
       jml.actual_rate, jm.branch_mr_no
FROM jute_issue ji
JOIN jute_mr_li jml ON jml.jute_mr_li_id = ji.jute_mr_li_id
JOIN jute_mr jm ON jm.jute_mr_id = jml.jute_mr_id
WHERE ji.branch_id IN %s AND ji.issue_date >= %s AND ji.status_id <> 4
  AND COALESCE(ji.updated_by, '') NOT LIKE 'STOCKADJ%%'
ORDER BY ji.jute_mr_li_id, ji.issue_date, ji.jute_issue_id
"""


def build_plan(cur):
    """One plan row per touched issue: {id, q0, q1, w0, w1, v0, v1, why, ...}."""
    plan = {}

    def touch(i, why):
        p = plan.setdefault(i["jute_issue_id"], {
            "jute_issue_id": i["jute_issue_id"], "branch_id": i["branch_id"],
            "jute_mr_li_id": i["jute_mr_li_id"], "branch_mr_no": i.get("branch_mr_no"),
            "issue_date": i["issue_date"], "status_id": i["status_id"],
            "qty_before": float(i["quantity"] or 0), "qty_after": float(i["quantity"] or 0),
            "weight_before": float(i["weight"] or 0), "weight_after": float(i["weight"] or 0),
            "value_before": float(i["issue_value"] or 0), "value_after": float(i["issue_value"] or 0),
            "updated_by_before": i["updated_by"], "why": "",
        })
        p["why"] = (p["why"] + "+" + why).strip("+")
        return p

    # PHASE A
    cur.execute(OVER_LINES_SQL, (BRANCHES,))
    over = cur.fetchall()
    closed_lines = []
    if over:
        cur.execute(LINE_ISSUES_SQL, (tuple(l["jute_mr_li_id"] for l in over),))
        by_line = {}
        for r in cur.fetchall():
            by_line.setdefault(r["jute_mr_li_id"], []).append(r)
        for ln in over:
            issues = by_line.get(ln["jute_mr_li_id"], [])
            cut = close_line(ln["actual_qty"], ln["actual_weight"], issues)
            if not cut:
                continue
            jid, new_q, new_w = cut
            last = issues[-1]
            last["branch_mr_no"] = ln["branch_mr_no"]
            p = touch(last, "close")
            p["qty_after"], p["weight_after"] = new_q, new_w
            closed_lines.append((ln, p))

    # PHASE B (uses weight_after from phase A when the same row was cut)
    cur.execute(SCOPE_SQL, (BRANCHES, CUTOFF))
    scope = cur.fetchall()
    for i in scope:
        w = plan[i["jute_issue_id"]]["weight_after"] if i["jute_issue_id"] in plan else i["weight"]
        new_v = reprice(w, i["issue_value"], i["actual_rate"])
        if new_v is None:
            continue
        p = touch(i, "reprice")
        p["value_after"] = new_v
        p["actual_rate"] = float(i["actual_rate"])
    return list(plan.values()), closed_lines, len(scope)


def summarise(plan, closed_lines, n_scope):
    print(f"PHASE A close: {len(closed_lines)} over-issued line(s)")
    for ln, p in closed_lines:
        print(f"   line {ln['jute_mr_li_id']} br{ln['branch_id']} MR#{ln['branch_mr_no']} "
              f"aq={ln['actual_qty']} aw={ln['actual_weight']} -> issue {p['jute_issue_id']} "
              f"qty {p['qty_before']} -> {p['qty_after']}, weight {p['weight_before']} -> {p['weight_after']}")
    got = {ln["jute_mr_li_id"] for ln, _ in closed_lines}
    if got != EXPECT_CLOSE_LINES:
        print(f"   !! expected lines {sorted(EXPECT_CLOSE_LINES)}, got {sorted(got)}")
    rep = [p for p in plan if "reprice" in p["why"]]
    print(f"PHASE B reprice: {len(rep)} of {n_scope} in-scope rows")
    for b in BRANCHES:
        rows = [p for p in rep if p["branch_id"] == b]
        dv = sum(p["value_after"] - p["value_before"] for p in rows)
        zero = sum(1 for p in rows if p["value_before"] == 0)
        print(f"   branch {b}: {len(rows)} rows ({zero} were zero-valued); value delta {dv:+.2f}")


def write_artifacts(plan):
    cols = ["jute_issue_id", "branch_id", "jute_mr_li_id", "branch_mr_no", "issue_date", "status_id",
            "qty_before", "qty_after", "weight_before", "weight_after", "value_before", "value_after",
            "actual_rate", "updated_by_before", "why"]
    with PLAN_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(plan)
    with ROLLBACK_SQL.open("w", encoding="utf-8") as fh:
        fh.write(f"-- Rollback for {TAG} ({len(plan)} rows). Run against sls.\n")
        fh.write("START TRANSACTION;\n")
        for p in plan:
            ub = "NULL" if p["updated_by_before"] is None else "'" + str(p["updated_by_before"]).replace("'", "''") + "'"
            fh.write(f"UPDATE jute_issue SET quantity = {p['qty_before']!r}, weight = {p['weight_before']!r}, "
                     f"issue_value = {p['value_before']!r}, updated_by = {ub} "
                     f"WHERE jute_issue_id = {p['jute_issue_id']} AND updated_by = '{TAG}';\n")
        fh.write("COMMIT;\n")
    print(f"\nwrote {PLAN_CSV.name}, {ROLLBACK_SQL.name}")


def apply(cur, plan):
    sql = ("UPDATE jute_issue SET quantity = %s, weight = %s, issue_value = %s, updated_by = %s "
           "WHERE jute_issue_id = %s")
    n = 0
    for p in plan:
        n += cur.execute(sql, (p["qty_after"], p["weight_after"], p["value_after"], TAG, p["jute_issue_id"]))
    print(f"updated {n} jute_issue rows")
    return n


def verify(cur, plan, closed_lines):
    errors = []
    if closed_lines:
        ids = tuple(ln["jute_mr_li_id"] for ln, _ in closed_lines)
        cur.execute("""
            SELECT jml.jute_mr_li_id, jml.actual_qty aq, jml.actual_weight aw,
                   SUM(ji.quantity) q, CAST(SUM(ji.weight) AS DECIMAL(18,6)) w
            FROM jute_mr_li jml JOIN jute_issue ji ON ji.jute_mr_li_id = jml.jute_mr_li_id AND ji.status_id <> 4
            WHERE jml.jute_mr_li_id IN %s GROUP BY 1, 2, 3""", (ids,))
        for r in cur.fetchall():
            if abs(float(r["q"]) - float(r["aq"])) > TOL or abs(float(r["w"]) - float(r["aw"])) > TOL:
                errors.append(f"line {r['jute_mr_li_id']} not closed: qty {r['q']}/{r['aq']} wt {r['w']}/{r['aw']}")
    rep = [p for p in plan if "reprice" in p["why"]]
    if rep:
        cur.execute("""
            SELECT ji.jute_issue_id, ji.issue_value, CAST(ji.weight AS DECIMAL(18,6)) w, jml.actual_rate ar
            FROM jute_issue ji JOIN jute_mr_li jml ON jml.jute_mr_li_id = ji.jute_mr_li_id
            WHERE ji.jute_issue_id IN %s""", (tuple(p["jute_issue_id"] for p in rep),))
        for r in cur.fetchall():
            want = round(float(r["w"]) * float(r["ar"]) / 100.0, 2)
            if abs(float(r["issue_value"]) - want) > VALUE_TOL:
                errors.append(f"issue {r['jute_issue_id']} value {r['issue_value']} != {want}")
    return errors


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if mode not in ("--check", "--apply"):
        sys.exit("usage: jute_issue_close_reprice_20260905.py [--check|--apply]")
    conn = connect()
    cur = conn.cursor()
    try:
        plan, closed_lines, n_scope = build_plan(cur)
        summarise(plan, closed_lines, n_scope)
        write_artifacts(plan)
        if mode != "--apply":
            print("\nCheck-only mode, no changes. Run with --apply to execute.")
            return
        n = apply(cur, plan)
        if n != len(plan):
            conn.rollback()
            sys.exit(f"!! updated {n} rows but planned {len(plan)} — rolled back")
        errors = verify(cur, plan, closed_lines)
        if errors:
            conn.rollback()
            print("\n".join(errors))
            sys.exit("!! post-verify failed — rolled back, nothing written")
        conn.commit()
        print(f"\nCOMMITTED {n} rows, tag {TAG}. Rollback: {ROLLBACK_SQL.name}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
