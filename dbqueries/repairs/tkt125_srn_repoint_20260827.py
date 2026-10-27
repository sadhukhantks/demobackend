"""
TKT-000125 — re-point issue_li.inward_dtl_id off a fully-returned lot (sls, co 106, br 87).

Item 141-0196N (item_id 333704, COP SPINDLE 11/16" X 41"). Lot 15500
(SRN LC/FACTORY/24-25/000122): received 20 on 2024-05-17, ALL 20 returned to
supplier via DN 62 on 2024-05-31 — yet 40 PCS were issued from it in June 2026
through the then return-blind picker, leaving the lot at -40. The item ledger
(signed sum) therefore shows 40 while the issue picker (positive lots only)
shows 80 — the gap the reporter saw as "ledger 55 vs issue posting 85".

Fix: move the four June-2026 issue lines (5+5+20+10 = 40) to lot 41915
(SRN LC/FACTORY/26-27/000116, received 2026-06-08, 45 approved / 5 issued /
40 headroom) — the lot the goods physically came from; every issue date
postdates its receipt. Post-repair: lot 15500 nets 0, lot 41915 nets 0,
picker total = ledger total = 40. Valuation note: the 40 units re-value from
195/PC (dead 2024 lot) to 290/PC (actual 2026 lot) in issue-value reports.

Same repair class as tkt_srn_repoint_20260824.py (TKT-105/107/108, applied).

Usage (from repo root, venv active):
    python dbqueries/repairs/tkt125_srn_repoint_20260827.py --check    # SELECT-only precheck
    python dbqueries/repairs/tkt125_srn_repoint_20260827.py --apply    # precheck + UPDATE + postcheck

--apply writes rollback SQL next to this file: tkt125_srn_repoint_20260827_rollback.sql
Rollback = run that file. updated_by is set to 1 (sls repair marker convention).
"""
import sys
from pathlib import Path

import pymysql

ENV = Path(__file__).resolve().parents[2] / "env" / "database.env"
DB = "sls"

# (issue_li_id, expected_current_inward_dtl_id, new_inward_dtl_id, expected_issue_qty, ticket)
ROWS = [
    (159931, 15500, 41915, 5, "TKT-000125"),
    (159984, 15500, 41915, 5, "TKT-000125"),
    (159992, 15500, 41915, 20, "TKT-000125"),
    (159993, 15500, 41915, 10, "TKT-000125"),
]
LOTS = sorted({r[1] for r in ROWS} | {r[2] for r in ROWS})
TARGET_LOT = 41915


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


def balances(cur):
    cur.execute(
        "SELECT inward_dtl_id, balance_qty FROM vw_approved_inward_qty "
        "WHERE inward_dtl_id IN ({})".format(",".join(map(str, LOTS)))
    )
    return {r["inward_dtl_id"]: float(r["balance_qty"]) for r in cur.fetchall()}


def precheck(cur):
    errs = []
    ids = ",".join(str(r[0]) for r in ROWS)
    cur.execute(
        f"SELECT issue_li_id, inward_dtl_id, issue_qty, item_id, updated_by "
        f"FROM issue_li WHERE issue_li_id IN ({ids})"
    )
    li = {r["issue_li_id"]: r for r in cur.fetchall()}
    cur.execute(
        "SELECT inward_dtl_id, item_id, active FROM proc_inward_dtl "
        "WHERE inward_dtl_id IN ({})".format(",".join(map(str, LOTS)))
    )
    lots = {r["inward_dtl_id"]: r for r in cur.fetchall()}

    for li_id, cur_lot, new_lot, qty, tkt in ROWS:
        row = li.get(li_id)
        if row is None:
            errs.append(f"{tkt}: issue_li {li_id} not found")
            continue
        if row["inward_dtl_id"] != cur_lot:
            errs.append(f"{tkt}: issue_li {li_id} inward_dtl_id={row['inward_dtl_id']}, expected {cur_lot} — state drifted, ABORT")
        if float(row["issue_qty"]) != qty:
            errs.append(f"{tkt}: issue_li {li_id} issue_qty={row['issue_qty']}, expected {qty}")
        tgt = lots.get(new_lot)
        if tgt is None:
            errs.append(f"{tkt}: target lot {new_lot} not found")
        else:
            if tgt["active"] != 1:
                errs.append(f"{tkt}: target lot {new_lot} active={tgt['active']}")
            if tgt["item_id"] != row["item_id"]:
                errs.append(f"{tkt}: item mismatch — issue_li {li_id} item {row['item_id']} vs lot {new_lot} item {tgt['item_id']}")

    # headroom guard: everything moving in must fit into the target lot's balance
    moving = sum(r[3] for r in ROWS)
    tgt_bal = balances(cur).get(TARGET_LOT, 0.0)
    if tgt_bal < moving:
        errs.append(f"target lot {TARGET_LOT} balance {tgt_bal} < {moving} moving in — headroom gone, ABORT")
    return li, errs


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    conn = pymysql.connect(**creds(), autocommit=False)
    cur = conn.cursor()

    li, errs = precheck(cur)
    before = balances(cur)
    print(f"Precheck on {DB}: {len(ROWS)} rows, {len(errs)} problems")
    for e in errs:
        print("  !!", e)
    print("Lot balances before:", {k: before.get(k) for k in LOTS})
    if errs:
        sys.exit("ABORT: precheck failed, nothing changed.")

    rollback = Path(__file__).with_name("tkt125_srn_repoint_20260827_rollback.sql")
    lines = [f"-- rollback for tkt125_srn_repoint_20260827 on {DB}"]
    for li_id, cur_lot, new_lot, _, tkt in ROWS:
        ub = li[li_id]["updated_by"]
        lines.append(
            f"UPDATE issue_li SET inward_dtl_id = {cur_lot}, updated_by = {ub if ub is not None else 'NULL'} "
            f"WHERE issue_li_id = {li_id} AND inward_dtl_id = {new_lot};  -- {tkt}"
        )
    rollback.write_text("\n".join(lines) + "\n")
    print(f"Rollback written: {rollback}")

    if mode != "--apply":
        print("Check-only mode, no changes. Run with --apply to execute.")
        return

    for li_id, cur_lot, new_lot, _, tkt in ROWS:
        n = cur.execute(
            "UPDATE issue_li SET inward_dtl_id = %s, updated_by = 1 "
            "WHERE issue_li_id = %s AND inward_dtl_id = %s",
            (new_lot, li_id, cur_lot),
        )
        if n != 1:
            conn.rollback()
            sys.exit(f"ABORT (rolled back): issue_li {li_id} matched {n} rows")
        print(f"  {tkt}: issue_li {li_id} {cur_lot} -> {new_lot}")
    conn.commit()

    after = balances(cur)
    print("Lot balances after:", {k: after.get(k) for k in LOTS})
    neg = {k: v for k, v in after.items() if v < 0}
    print("APPLIED OK." if not neg else f"APPLIED but negative balances remain: {neg}")


if __name__ == "__main__":
    main()
