"""
STOCKFIX-20260831 — clear negative and dust SR balances on sls, branches 87 + 29.

Balance grain and formula are copied verbatim from get_sr_exception_report_query()
(src/inventory/reportQueries.py): one row per proc_inward_dtl (pid.active = 1,
proc_inward.sr_status = 3), balance = approved_qty
  - SUM(issue_li.issue_qty)      via issue_hdr active=1/NULL, status_id IN (1,3,5,20)
  - SUM(vw_drcr_return_qty.return_qty)

Three phases, all scoped to proc_inward.branch_id IN (87, 29):

  PHASE 1 NEGATIVES  balance < -0.0005 — walk the lot's consuming issue_li rows
                     NEWEST FIRST (issue_date DESC, issue_li_id DESC) and cut qty
                     line by line until the shortfall is gone. A line may reach 0;
                     the row is kept. Never goes below 0 — if the arithmetic would
                     need that, the whole plan hard-fails.
  PHASE 2 LEAKS      issue_li rows (qty > 0, live header) pointing at a
                     soft-deleted proc_inward_dtl (active = 0). Those consume
                     stock that the report grain cannot see. qty := 0.
  PHASE 3 DUST       balance != 0 but ABS(balance) < 0.0005 (float residue from
                     the vowsls import). ONE issue_li per lot: the newest line
                     with qty >= |balance|, new_qty = old_qty + balance. Works for
                     both signs. Lots already touched by phase 1 are skipped.

Received/approved qty is never touched — only issue_li.issue_qty.

Schema notes (sls, verified by DESCRIBE):
  * issue_li has NO rate/amount/value column (issue_qty, req_quantity only), so
    there is nothing to re-derive alongside qty. Issue valuation reports multiply
    issue_qty by proc_inward_dtl.accepted_rate at read time, so they self-correct.
  * issue_hdr carries NO aggregate qty/amount column, so no header roll-up goes
    stale. Nothing outside issue_li is written — same as the earlier repair
    scripts (tkt_srn_repoint_20260824.py, tkt125_srn_repoint_20260827.py).
  * issue_qty and approved_qty are both DOUBLE, so exact cancellation to 0 is
    representable; --apply still re-measures and micro-corrects (max 3 rounds).

Usage (from repo root, venv active):
    python dbqueries/repairs/stock_negative_dust_fix_20260831.py --check
    python dbqueries/repairs/stock_negative_dust_fix_20260831.py --apply

--check is SELECT-only: it prints the summary + before/after table and writes
    stock_negative_dust_fix_20260831_plan.csv
    stock_negative_dust_fix_20260831_rollback.sql
--apply runs the same plan in ONE transaction, then post-verifies every touched
lot and asserts no lot in branch 87/29 became newly negative.
Rollback = run the generated .sql. updated_by is set to 1 (sls repair marker).
"""
import csv
import datetime
import sys
from pathlib import Path

import pymysql

ENV = Path(__file__).resolve().parents[2] / "env" / "database.env"
DB = "sls"
BRANCHES = (87, 29)
TAG = "STOCKFIX-20260831"

DUST = 0.0005          # |balance| below this and non-zero = dust (ROUND(bal,3) = 0)
EPS = 1e-12            # "close enough to zero" for float bookkeeping
OK = 1e-9              # post-apply success threshold on a touched lot

PLAN_CSV = Path(__file__).with_name("stock_negative_dust_fix_20260831_plan.csv")
ROLLBACK_SQL = Path(__file__).with_name("stock_negative_dust_fix_20260831_rollback.sql")

# Expected counts from the read-only assessment (2026-08-31). Drift = warn, not fail.
EXPECT_NEG = {87: 24, 29: 9}
EXPECT_NEG_DELTA = {87: -514.70, 29: -38.516}
EXPECT_DUST = {87: 148, 29: 1037}
EXPECT_LEAKS = {159802, 159839, 159906, 160151, 160179, 160923}


# --------------------------------------------------------------------------
# pure planning helpers (unit-tested in src/test/test_stock_negative_dust_fix.py)
# --------------------------------------------------------------------------
def allocate_cuts(shortfall, lines):
    """Consume `shortfall` from `lines` = [(id, qty)] given NEWEST FIRST.

    Returns [(id, new_qty, delta)] in the order consumed; new_qty is never < 0
    and a line may land exactly on 0. Raises ValueError if the lines cannot
    cover the shortfall (that is the "hard-fail the plan" case).
    """
    if shortfall <= 0:
        raise ValueError(f"shortfall must be positive, got {shortfall}")
    cuts = []
    remaining = shortfall
    for li_id, qty in lines:
        if remaining <= EPS:
            break
        if qty <= 0:
            continue
        cut = qty if qty < remaining else remaining
        new_qty = qty - cut
        if new_qty < 0:
            raise ValueError(f"issue_li {li_id}: new qty {new_qty} < 0")
        cuts.append((li_id, new_qty, -cut))
        remaining -= cut
    if remaining > OK:
        raise ValueError(
            f"shortfall {shortfall} exceeds consumable issue qty by {remaining}"
        )
    return cuts


def dust_cut(balance, lines):
    """Single-line dust correction. `lines` = [(id, qty)] NEWEST FIRST.

    Picks the newest line with qty >= |balance| and returns
    (id, new_qty = qty + balance, delta = balance) — a positive balance bumps the
    issue up, a negative one trims it. None if no line is big enough.
    """
    need = abs(balance)
    for li_id, qty in lines:
        if qty >= need:
            new_qty = qty + balance
            if new_qty < 0:
                raise ValueError(f"issue_li {li_id}: new qty {new_qty} < 0")
            return (li_id, new_qty, balance)
    return None


# --------------------------------------------------------------------------
# db
# --------------------------------------------------------------------------
def creds():
    kv = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            kv[k] = v
    return dict(
        host=kv["DATABASE_HOST"], port=int(kv["DATABASE_PORT"]),
        user=kv["DATABASE_USER"], password=kv["DATABASE_PASSWORD"],
        database=DB, cursorclass=pymysql.cursors.DictCursor,
    )


BAL = ("COALESCE(pid.approved_qty, 0) - COALESCE(iss.qty, 0) - COALESCE(ret.qty, 0)")

# joins/filters lifted from get_sr_exception_report_query()
BALANCE_SQL = """
SELECT
    pid.inward_dtl_id,
    pi.branch_id,
    pi.sr_no,
    vip.full_item_code AS item_code,
    im.item_name,
    COALESCE(pid.approved_qty, 0) AS received_qty,
    COALESCE(iss.qty, 0)          AS issued_qty,
    COALESCE(ret.qty, 0)          AS return_qty,
    {bal} AS balance_qty
FROM proc_inward_dtl AS pid
INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
INNER JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
LEFT JOIN item_mst AS im ON im.item_id = pid.item_id
LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = pid.item_id
LEFT JOIN (
    SELECT il.inward_dtl_id, SUM(il.issue_qty) AS qty
    FROM issue_li AS il
    INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
        AND ih.status_id IN (1, 3, 5, 20)
        AND il.inward_dtl_id IS NOT NULL
    GROUP BY il.inward_dtl_id
) AS iss ON iss.inward_dtl_id = pid.inward_dtl_id
LEFT JOIN (
    SELECT r.inward_dtl_id, SUM(r.return_qty) AS qty
    FROM vw_drcr_return_qty AS r
    GROUP BY r.inward_dtl_id
) AS ret ON ret.inward_dtl_id = pid.inward_dtl_id
WHERE pid.active = 1
    AND pi.sr_status = 3
    AND pi.branch_id IN ({branches})
    AND ({cond})
"""


def fetch_balances(cur, cond):
    cur.execute(BALANCE_SQL.format(
        bal=BAL, branches=",".join(map(str, BRANCHES)), cond=cond.format(bal=BAL)))
    rows = cur.fetchall()
    for r in rows:
        r["balance_qty"] = float(r["balance_qty"])
    return rows


def fetch_issue_lines(cur, lot_ids):
    """{inward_dtl_id: [line, ...]} newest first — same header filter as the report."""
    if not lot_ids:
        return {}
    cur.execute(
        """
        SELECT il.issue_li_id, il.inward_dtl_id, il.issue_qty, il.updated_by,
               ih.issue_pass_no, ih.issue_pass_print_no, ih.issue_date
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND il.inward_dtl_id IN ({})
        ORDER BY il.inward_dtl_id, ih.issue_date DESC, il.issue_li_id DESC
        """.format(",".join(map(str, sorted(lot_ids))))
    )
    out = {}
    for r in cur.fetchall():
        r["issue_qty"] = float(r["issue_qty"] or 0)
        out.setdefault(r["inward_dtl_id"], []).append(r)
    return out


def fetch_leaks(cur):
    cur.execute(
        """
        SELECT il.issue_li_id, il.inward_dtl_id, il.issue_qty, il.updated_by,
               ih.issue_pass_no, ih.issue_pass_print_no, ih.issue_date,
               pi.branch_id, pi.sr_no, vip.full_item_code AS item_code, im.item_name
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        INNER JOIN proc_inward_dtl AS pid ON pid.inward_dtl_id = il.inward_dtl_id
        INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
        LEFT JOIN item_mst AS im ON im.item_id = pid.item_id
        LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = pid.item_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND il.issue_qty > 0
            AND pid.active = 0
            AND pi.branch_id IN ({})
        ORDER BY il.issue_li_id
        """.format(",".join(map(str, BRANCHES)))
    )
    rows = cur.fetchall()
    for r in rows:
        r["issue_qty"] = float(r["issue_qty"])
    return rows


def issue_no(row):
    return (row.get("issue_pass_print_no") or "").strip() or str(row.get("issue_pass_no") or "")


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def build_plan(cur, warn):
    """Returns (plan_rows, errors). One plan row per issue_li change."""
    plan, errors = [], []

    negatives = fetch_balances(cur, "{bal} < -%s" % DUST)
    dusts = fetch_balances(cur, "{bal} <> 0 AND ABS({bal}) < %s" % DUST)
    leaks = fetch_leaks(cur)

    # ---- sanity vs the 2026-08-31 assessment -----------------------------
    def by_branch(rows):
        d = {}
        for r in rows:
            d[r["branch_id"]] = d.get(r["branch_id"], 0) + 1
        return d

    got_neg, got_dust = by_branch(negatives), by_branch(dusts)
    if got_neg != EXPECT_NEG:
        warn(f"negative-line counts drifted: live {got_neg} vs expected {EXPECT_NEG}")
    if got_dust != EXPECT_DUST:
        warn(f"dust-line counts drifted: live {got_dust} vs expected {EXPECT_DUST}")
    for br, exp in EXPECT_NEG_DELTA.items():
        live = sum(r["balance_qty"] for r in negatives if r["branch_id"] == br)
        if abs(live - exp) > 0.01:
            warn(f"branch {br} negative total {live:.4f} vs expected {exp:.4f}")
    got_leaks = {r["issue_li_id"] for r in leaks}
    if got_leaks != EXPECT_LEAKS:
        warn(f"leak set drifted: extra {sorted(got_leaks - EXPECT_LEAKS)}, "
             f"missing {sorted(EXPECT_LEAKS - got_leaks)} — planning ALL live rows")

    lines = fetch_issue_lines(cur, {r["inward_dtl_id"] for r in negatives + dusts})

    def rec(phase, lot, li, old_qty, new_qty):
        return {
            "phase": phase,
            "branch_id": lot["branch_id"],
            "inward_dtl_id": lot["inward_dtl_id"],
            "sr_no": lot.get("sr_no"),
            "item": " ".join(x for x in (lot.get("item_code"), lot.get("item_name")) if x),
            "issue_li_id": li["issue_li_id"],
            "issue_no": issue_no(li),
            "issue_date": li.get("issue_date"),
            "old_qty": old_qty,
            "new_qty": new_qty,
            "delta": new_qty - old_qty,
            "old_updated_by": li.get("updated_by"),   # rollback only, not in the CSV
        }

    # ---- PHASE 1: negatives ---------------------------------------------
    for lot in sorted(negatives, key=lambda r: r["balance_qty"]):
        avail = lines.get(lot["inward_dtl_id"], [])
        try:
            cuts = allocate_cuts(-lot["balance_qty"], [(l["issue_li_id"], l["issue_qty"]) for l in avail])
        except ValueError as e:
            errors.append(f"lot {lot['inward_dtl_id']} (SR {lot['sr_no']}, bal "
                          f"{lot['balance_qty']:.6f}): {e}")
            continue
        by_id = {l["issue_li_id"]: l for l in avail}
        for li_id, new_qty, _ in cuts:
            li = by_id[li_id]
            plan.append(rec(1, lot, li, li["issue_qty"], new_qty))

    touched_lots = {p["inward_dtl_id"] for p in plan}

    # ---- PHASE 2: leak rows on soft-deleted lots -------------------------
    for li in leaks:
        plan.append(rec(2, li, li, li["issue_qty"], 0.0))

    # ---- PHASE 3: dust ---------------------------------------------------
    for lot in dusts:
        if lot["inward_dtl_id"] in touched_lots:      # already fixed by phase 1
            continue
        avail = lines.get(lot["inward_dtl_id"], [])
        try:
            cut = dust_cut(lot["balance_qty"], [(l["issue_li_id"], l["issue_qty"]) for l in avail])
        except ValueError as e:
            errors.append(f"lot {lot['inward_dtl_id']} (dust {lot['balance_qty']:.3e}): {e}")
            continue
        if cut is None:
            errors.append(f"lot {lot['inward_dtl_id']} (SR {lot['sr_no']}, dust "
                          f"{lot['balance_qty']:.3e}): no issue line with qty >= "
                          f"{abs(lot['balance_qty']):.3e}")
            continue
        li_id, new_qty, _ = cut
        li = next(l for l in avail if l["issue_li_id"] == li_id)
        plan.append(rec(3, lot, li, li["issue_qty"], new_qty))

    # ---- dedupe: one final qty per issue_li ------------------------------
    seen, deduped = {}, []
    for p in plan:
        prev = seen.get(p["issue_li_id"])
        if prev is None:
            seen[p["issue_li_id"]] = p
            deduped.append(p)
        elif abs(prev["new_qty"] - p["new_qty"]) <= EPS:
            warn(f"issue_li {p['issue_li_id']} planned twice with the same qty — dropped dup")
        else:
            errors.append(f"issue_li {p['issue_li_id']} planned twice with conflicting "
                          f"qty {prev['new_qty']} vs {p['new_qty']}")
    return deduped, errors


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------
FIELDS = ["phase", "branch_id", "inward_dtl_id", "sr_no", "item", "issue_li_id",
          "issue_no", "issue_date", "old_qty", "new_qty", "delta"]


def write_plan_csv(plan):
    with PLAN_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for p in plan:
            w.writerow({k: p[k] for k in FIELDS})


def write_rollback(plan):
    out = [
        f"-- rollback for {TAG} (stock_negative_dust_fix_20260831.py) on {DB}",
        f"-- generated {datetime.date.today()} — {len(plan)} issue_li rows",
        "-- restores issue_li.issue_qty to its pre-repair value; issue_li has no",
        "-- rate/amount column and issue_hdr no aggregates, so qty is the whole story.",
        "START TRANSACTION;",
    ]
    for p in plan:
        ub = p["old_updated_by"]
        out.append(
            f"UPDATE issue_li SET issue_qty = {p['old_qty']!r}, "
            f"updated_by = {ub if ub is not None else 'NULL'} "
            f"WHERE issue_li_id = {p['issue_li_id']};"
            f"  -- phase {p['phase']}, lot {p['inward_dtl_id']}, was set to {p['new_qty']!r}"
        )
    out += ["COMMIT;", ""]
    ROLLBACK_SQL.write_text("\n".join(out), encoding="utf-8")


def summarise(plan, negatives_before, warnings):
    print(f"\n=== {TAG} plan summary ({DB}, branches {BRANCHES}) ===")
    for ph in (1, 2, 3):
        rows = [p for p in plan if p["phase"] == ph]
        if not rows:
            print(f"phase {ph}: no rows")
            continue
        lots = {p["inward_dtl_id"] for p in rows}
        print(f"phase {ph}: {len(rows)} issue_li rows across {len(lots)} lots, "
              f"delta qty {sum(p['delta'] for p in rows):+.6f}")
        for br in BRANCHES:
            b = [p for p in rows if p["branch_id"] == br]
            if b:
                print(f"          br {br}: {len(b)} rows, {len({p['inward_dtl_id'] for p in b})} "
                      f"lots, delta {sum(p['delta'] for p in b):+.6f}")
    print(f"TOTAL: {len(plan)} issue_li rows, "
          f"{len({p['inward_dtl_id'] for p in plan})} lots, "
          f"delta qty {sum(p['delta'] for p in plan):+.6f}")
    print(f"negative lots in br {BRANCHES} before repair: {len(negatives_before)}")
    if warnings:
        print(f"\n!! {len(warnings)} WARNING(S):")
        for w in warnings:
            print("  !!", w)
    else:
        print("no warnings — live findings match the 2026-08-31 assessment")


def print_table(plan, limit=40):
    print(f"\nper-line before -> after (first {limit} of {len(plan)}):")
    print(f"{'ph':>2} {'br':>3} {'lot':>8} {'issue_li':>9} {'old_qty':>16} "
          f"{'new_qty':>16} {'delta':>16}  sr_no")
    for p in plan[:limit]:
        print(f"{p['phase']:>2} {p['branch_id']:>3} {p['inward_dtl_id']:>8} "
              f"{p['issue_li_id']:>9} {p['old_qty']:>16.6f} {p['new_qty']:>16.6f} "
              f"{p['delta']:>+16.6f}  {p['sr_no']}")
    if len(plan) > limit:
        print(f"... {len(plan) - limit} more rows in {PLAN_CSV.name}")


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------
def apply_updates(cur, changes):
    """changes = [(issue_li_id, new_qty)] — returns rows matched."""
    n = 0
    for li_id, new_qty in changes:
        n += cur.execute(
            "UPDATE issue_li SET issue_qty = %s, updated_by = 1 WHERE issue_li_id = %s",
            (new_qty, li_id),
        )
    return n


def apply(conn, cur, plan):
    ids = ",".join(str(p["issue_li_id"]) for p in plan)
    cur.execute(f"SELECT issue_li_id, issue_qty FROM issue_li WHERE issue_li_id IN ({ids})")
    live = {r["issue_li_id"]: float(r["issue_qty"] or 0) for r in cur.fetchall()}
    drift = [p for p in plan
             if p["issue_li_id"] not in live or abs(live[p["issue_li_id"]] - p["old_qty"]) > EPS]
    if drift:
        for p in drift[:10]:
            print(f"  !! issue_li {p['issue_li_id']} qty {live.get(p['issue_li_id'])} "
                  f"!= planned old {p['old_qty']}")
        sys.exit(f"ABORT: {len(drift)} rows drifted since the plan was built, nothing changed.")

    n = apply_updates(cur, [(p["issue_li_id"], p["new_qty"]) for p in plan])
    print(f"phase 1-3: {n} issue_li rows updated (planned {len(plan)})")
    if n != len(plan):
        conn.rollback()
        sys.exit("ABORT (rolled back): update count mismatch.")

    # ---- float micro-correction: re-measure, nudge the same issue_li -----
    lots = {p["inward_dtl_id"]: p for p in plan if p["phase"] != 2}
    last_li = {}
    for p in plan:                       # last planned line per lot carries the fix
        if p["phase"] != 2:
            last_li[p["inward_dtl_id"]] = p["issue_li_id"]
    for round_no in (1, 2, 3):
        rows = fetch_balances(cur, "pid.inward_dtl_id IN ({})".format(
            ",".join(map(str, sorted(lots)))))
        off = [r for r in rows if abs(r["balance_qty"]) > EPS]
        if not off:
            print(f"micro-correction: all {len(rows)} touched lots at |balance| <= {EPS}")
            break
        print(f"micro-correction round {round_no}: {len(off)} lots off by > {EPS}")
        cur.execute("SELECT issue_li_id, issue_qty FROM issue_li WHERE issue_li_id IN ({})".format(
            ",".join(str(last_li[r["inward_dtl_id"]]) for r in off)))
        qty = {r["issue_li_id"]: float(r["issue_qty"] or 0) for r in cur.fetchall()}
        fixes = []
        for r in off:
            li_id = last_li[r["inward_dtl_id"]]
            new_qty = qty[li_id] + r["balance_qty"]
            if new_qty < 0:
                conn.rollback()
                sys.exit(f"ABORT (rolled back): micro-correction would drive issue_li "
                         f"{li_id} to {new_qty}")
            fixes.append((li_id, new_qty))
        apply_updates(cur, fixes)
    else:
        print("!! micro-correction did not converge in 3 rounds — see post-check")

    conn.commit()
    print("COMMITTED.")


def postcheck(cur, plan, negatives_before):
    lots = sorted({p["inward_dtl_id"] for p in plan if p["phase"] != 2})
    rows = fetch_balances(cur, "pid.inward_dtl_id IN ({})".format(",".join(map(str, lots))))
    bad = [r for r in rows if abs(r["balance_qty"]) > OK]
    print(f"\npostcheck: {len(rows)} touched lots, {len(bad)} still off by > {OK}")
    for r in bad[:20]:
        print(f"  !! lot {r['inward_dtl_id']} (SR {r['sr_no']}) balance {r['balance_qty']:.3e}")
    after_neg = fetch_balances(cur, "{bal} < -%s" % OK)
    new_neg = {r["inward_dtl_id"] for r in after_neg} - {r["inward_dtl_id"] for r in negatives_before}
    print(f"negative lots in br {BRANCHES} after: {len(after_neg)} "
          f"(was {len(negatives_before)}); newly negative: {len(new_neg)}")
    for lot in sorted(new_neg)[:20]:
        r = next(x for x in after_neg if x["inward_dtl_id"] == lot)
        print(f"  !! NEW NEGATIVE lot {lot} (SR {r['sr_no']}) balance {r['balance_qty']:.6f}")
    print("REPAIR OK." if not bad and not new_neg else
          "REPAIR INCOMPLETE — review above; rollback SQL is next to this script.")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if mode not in ("--check", "--apply"):
        sys.exit("usage: stock_negative_dust_fix_20260831.py [--check|--apply]")

    warnings = []
    conn = pymysql.connect(**creds(), autocommit=False)
    cur = conn.cursor()

    plan, errors = build_plan(cur, warnings.append)
    negatives_before = fetch_balances(cur, "{bal} < -%s" % OK)

    summarise(plan, negatives_before, warnings)
    if errors:
        print(f"\n!! {len(errors)} PLAN ERROR(S) — plan is NOT safe to apply:")
        for e in errors:
            print("  XX", e)

    write_plan_csv(plan)
    write_rollback(plan)
    print(f"\nplan CSV : {PLAN_CSV}")
    print(f"rollback : {ROLLBACK_SQL}")
    print_table(plan)

    if errors:
        sys.exit("\nABORT: fix the plan errors above; nothing was changed.")
    if mode != "--apply":
        print("\nCheck-only mode, no changes. Run with --apply to execute.")
        return

    apply(conn, cur, plan)
    postcheck(cur, plan, negatives_before)


if __name__ == "__main__":
    main()
