"""
KGROUND-20260905 — round jute issue weights to WHOLE KG on sls, branches 29 + 87,
issue_date >= 2026-03-31.

Why: jute procurement stock is kept in whole kg (MR line actual_weight is integer),
but the issue path stored 2dp weights (qty x unit-weight), so per-row weights and the
open balance on partially-issued lines carried fractions of a kg. Code fix ships in
the same change (src/juteProcurement/totals.py::round_kg); this repairs what is
already stored.

Scope (per row): jute_issue.branch_id IN (29, 87), issue_date >= 2026-03-31,
status_id <> 4, jute_mr_li_id NOT NULL, updated_by NOT LIKE 'STOCKADJ%' — the
31-Mar zeroing rows (STOCKADJ-20260331 / STOCKADJ2-20260329) closed legacy
fractional-weight lines EXACTLY and are never touched.

Rule (per MR line that has at least one in-scope row):
  * line actual_weight not a whole number  -> skip the whole line (legacy vowsls
    import; its issues close it exactly, rounding would re-open it).
  * every in-scope row: weight := round_kg(weight)  (half-up, int).
  * if the line is FULLY issued by quantity (|sum qty - actual_qty| <= 0.001) AND
    was already balanced by weight (|actual_weight - sum weight| < 0.01), the LAST
    in-scope row (issue_date, id) absorbs the rounding residue so
    sum(weight) == actual_weight again, exactly.
    A line that is over-issued by bales (br87 lines 45030 / 45012) is NOT
    re-balanced: that gap is physical, not rounding, and must stay visible.
  * issue_value scales with the row's own implied rate:
    new_value = round(new_weight * value / weight, 2); zero/absent values stay.
  * quantity is never touched. updated_by := 'KGROUND-20260905' (rollback handle).

Usage (repo root, venv active):
    python dbqueries/repairs/jute_issue_kg_round_20260905.py --check
    python dbqueries/repairs/jute_issue_kg_round_20260905.py --apply

--check is SELECT-only: prints the summary, writes
    jute_issue_kg_round_20260905_plan.csv
    jute_issue_kg_round_20260905_rollback.sql
--apply runs the same plan in ONE transaction, then re-reads every touched line and
asserts (a) every in-scope row is a whole kg (or the exact closing balance) and
(b) no line that was balanced before is unbalanced after.
Rollback = run the generated .sql.
"""
import csv
import datetime
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pymysql

ENV = Path(__file__).resolve().parents[2] / "env" / "database.env"
DB = "sls"
BRANCHES = (29, 87)
CUTOFF = datetime.date(2026, 3, 31)
TAG = "KGROUND-20260905"
QTY_TOL = 0.001
BAL_TOL = 0.01

PLAN_CSV = Path(__file__).with_name("jute_issue_kg_round_20260905_plan.csv")
ROLLBACK_SQL = Path(__file__).with_name("jute_issue_kg_round_20260905_rollback.sql")

# From the read-only assessment (2026-09-05). Drift = warn, not fail.
EXPECT_ROWS = {29: 273, 87: 11}


def round_kg(value) -> int:
    """Same rule as src/juteProcurement/totals.py::round_kg (kept inline so the
    script runs without the app on the path)."""
    if value is None:
        return 0
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def is_whole(x, tol=1e-6):
    return abs(float(x) - round(float(x))) < tol


# --------------------------------------------------------------------------
# pure planning (unit-tested in src/test/test_jute_issue_kg_round_repair.py)
# --------------------------------------------------------------------------
def in_scope(issue):
    return (
        issue["issue_date"] >= CUTOFF
        and not str(issue.get("updated_by") or "").startswith("STOCKADJ")
    )


def plan_line(aq, aw, issues):
    """`issues` = every non-rejected issue on the line, ordered (issue_date, id).

    Returns [(jute_issue_id, new_weight, new_value)] for rows that change.
    """
    aq, aw = float(aq or 0), float(aw or 0)
    if not is_whole(aw):
        return []                                     # legacy fractional line: leave
    scoped = [i for i in issues if in_scope(i)]
    if not scoped:
        return []

    new_w = {i["jute_issue_id"]: float(i["weight"] or 0) for i in issues}
    for i in scoped:
        new_w[i["jute_issue_id"]] = float(round_kg(i["weight"]))

    sum_q = sum(float(i["quantity"] or 0) for i in issues)
    sum_w_before = sum(float(i["weight"] or 0) for i in issues)
    fully_issued = aq > 0 and abs(sum_q - aq) <= QTY_TOL
    balanced_before = abs(aw - sum_w_before) < BAL_TOL
    if fully_issued and balanced_before:
        last = scoped[-1]["jute_issue_id"]
        new_w[last] = round(new_w[last] + aw - sum(new_w.values()), 3)

    changes = []
    for i in scoped:
        w0, v0 = float(i["weight"] or 0), float(i["issue_value"] or 0)
        w1 = new_w[i["jute_issue_id"]]
        v1 = round(w1 * v0 / w0, 2) if w0 > 0 and v0 > 0 else v0
        if abs(w1 - w0) > 1e-9 or abs(v1 - v0) > 1e-9:
            changes.append((i["jute_issue_id"], w1, v1))
    return changes


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


LINES_SQL = """
SELECT jml.jute_mr_li_id, jm.branch_id, jm.branch_mr_no, jm.jute_mr_date,
       jml.actual_qty, jml.actual_weight
FROM jute_mr_li jml
JOIN jute_mr jm ON jm.jute_mr_id = jml.jute_mr_id
WHERE jm.branch_id IN %s
  AND jml.jute_mr_li_id IN (
      SELECT DISTINCT jute_mr_li_id FROM jute_issue
      WHERE branch_id IN %s AND issue_date >= %s AND status_id <> 4
        AND jute_mr_li_id IS NOT NULL
        AND COALESCE(updated_by, '') NOT LIKE 'STOCKADJ%%')
ORDER BY jm.branch_id, jml.jute_mr_li_id
"""

# weight is a FLOAT: the text protocol rounds it to 6 significant digits, which hides
# float32 noise such as 1971.9999 (reads back as 1972). CAST exposes the stored value.
ISSUES_SQL = """
SELECT jute_issue_id, jute_mr_li_id, branch_id, issue_date, status_id,
       quantity, CAST(weight AS DECIMAL(18,6)) AS weight, issue_value, updated_by
FROM jute_issue
WHERE jute_mr_li_id IN %s AND status_id <> 4
ORDER BY jute_mr_li_id, issue_date, jute_issue_id
"""


def load(cur):
    cur.execute(LINES_SQL, (BRANCHES, BRANCHES, CUTOFF))
    lines = cur.fetchall()
    if not lines:
        return [], {}
    cur.execute(ISSUES_SQL, (tuple(l["jute_mr_li_id"] for l in lines),))
    by_line = {}
    for r in cur.fetchall():
        by_line.setdefault(r["jute_mr_li_id"], []).append(r)
    return lines, by_line


def build_plan(lines, by_line):
    plan, skipped = [], []
    for ln in lines:
        issues = by_line.get(ln["jute_mr_li_id"], [])
        if not is_whole(ln["actual_weight"] or 0):
            skipped.append(ln)
            continue
        idx = {i["jute_issue_id"]: i for i in issues}
        for jid, w1, v1 in plan_line(ln["actual_qty"], ln["actual_weight"], issues):
            i = idx[jid]
            plan.append({
                "jute_issue_id": jid, "branch_id": i["branch_id"],
                "jute_mr_li_id": ln["jute_mr_li_id"], "branch_mr_no": ln["branch_mr_no"],
                "issue_date": i["issue_date"], "status_id": i["status_id"],
                "quantity": float(i["quantity"] or 0),
                "weight_before": float(i["weight"] or 0), "weight_after": w1,
                "value_before": float(i["issue_value"] or 0), "value_after": v1,
                "updated_by_before": i["updated_by"],
            })
    return plan, skipped


def summarise(lines, by_line, plan, skipped):
    print(f"Lines with in-scope issues: {len(lines)}  (skipped legacy fractional-weight lines: {len(skipped)})")
    for ln in skipped:
        print(f"   skip line {ln['jute_mr_li_id']} br{ln['branch_id']} MR#{ln['branch_mr_no']} "
              f"aw={ln['actual_weight']}")
    scoped_rows = {b: 0 for b in BRANCHES}
    for ln in lines:
        for i in by_line.get(ln["jute_mr_li_id"], []):
            if in_scope(i):
                scoped_rows[i["branch_id"]] += 1
    for b in BRANCHES:
        n = sum(1 for p in plan if p["branch_id"] == b)
        dw = sum(p["weight_after"] - p["weight_before"] for p in plan if p["branch_id"] == b)
        dv = sum(p["value_after"] - p["value_before"] for p in plan if p["branch_id"] == b)
        drift = "" if scoped_rows[b] == EXPECT_ROWS.get(b) else f"  !! expected {EXPECT_ROWS.get(b)} in-scope rows"
        print(f"branch {b}: in-scope rows {scoped_rows[b]}{drift}; rows changing {n}; "
              f"net weight delta {dw:+.3f} kg; net value delta {dv:+.2f}")
    big = [p for p in plan if abs(p["weight_after"] - p["weight_before"]) >= 1]
    if big:
        print(f"\n{len(big)} row(s) move by >= 1 kg (residue absorbed on a closing row):")
        for p in big:
            print(f"   issue {p['jute_issue_id']} line {p['jute_mr_li_id']} MR#{p['branch_mr_no']} "
                  f"{p['weight_before']} -> {p['weight_after']}")


def write_artifacts(plan):
    with PLAN_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(plan[0].keys()) if plan else ["jute_issue_id"])
        w.writeheader()
        w.writerows(plan)
    with ROLLBACK_SQL.open("w", encoding="utf-8") as fh:
        fh.write(f"-- Rollback for {TAG} ({len(plan)} rows). Run against sls.\n")
        fh.write("START TRANSACTION;\n")
        for p in plan:
            ub = "NULL" if p["updated_by_before"] is None else "'" + str(p["updated_by_before"]).replace("'", "''") + "'"
            fh.write(f"UPDATE jute_issue SET weight = {p['weight_before']!r}, issue_value = {p['value_before']!r}, "
                     f"updated_by = {ub} WHERE jute_issue_id = {p['jute_issue_id']} AND updated_by = '{TAG}';\n")
        fh.write("COMMIT;\n")
    print(f"\nwrote {PLAN_CSV.name}, {ROLLBACK_SQL.name}")


def verify(cur, lines, by_line_before, plan):
    touched = {p["jute_mr_li_id"] for p in plan}
    if not touched:
        return []
    cur.execute(ISSUES_SQL, (tuple(touched),))
    after = {}
    for r in cur.fetchall():
        after.setdefault(r["jute_mr_li_id"], []).append(r)
    errors = []
    for ln in lines:
        li = ln["jute_mr_li_id"]
        if li not in touched:
            continue
        aw = float(ln["actual_weight"] or 0)
        before = by_line_before[li]
        rows = after[li]
        sum_before = sum(float(i["weight"] or 0) for i in before)
        sum_after = sum(float(i["weight"] or 0) for i in rows)
        if abs(aw - sum_before) < BAL_TOL and abs(aw - sum_after) > QTY_TOL:
            errors.append(f"line {li}: was balanced ({sum_before:.3f}/{aw}), now {sum_after:.3f}")
        scoped = [i for i in rows if in_scope(i)]
        closing = scoped[-1]["jute_issue_id"] if scoped else None
        for i in scoped:
            w = float(i["weight"] or 0)
            if not is_whole(w) and i["jute_issue_id"] != closing:
                errors.append(f"issue {i['jute_issue_id']} line {li}: weight {w} not whole kg")
    return errors


def apply(conn, cur, plan):
    sql = ("UPDATE jute_issue SET weight = %s, issue_value = %s, updated_by = %s "
           "WHERE jute_issue_id = %s")
    n = 0
    for p in plan:
        n += cur.execute(sql, (p["weight_after"], p["value_after"], TAG, p["jute_issue_id"]))
    print(f"updated {n} jute_issue rows")
    return n


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if mode not in ("--check", "--apply"):
        sys.exit("usage: jute_issue_kg_round_20260905.py [--check|--apply]")
    conn = connect()
    cur = conn.cursor()
    try:
        lines, by_line = load(cur)
        plan, skipped = build_plan(lines, by_line)
        summarise(lines, by_line, plan, skipped)
        write_artifacts(plan)
        if mode != "--apply":
            print("\nCheck-only mode, no changes. Run with --apply to execute.")
            return
        n = apply(conn, cur, plan)
        if n != len(plan):
            conn.rollback()
            sys.exit(f"!! updated {n} rows but planned {len(plan)} — rolled back")
        errors = verify(cur, lines, by_line, plan)
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
