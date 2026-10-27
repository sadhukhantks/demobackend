"""
TKT-000105 / TKT-000107(part 1) / TKT-000108 — re-point issue_li.inward_dtl_id (sls).

Root cause: backdated issue-entry burst (user 22, 24.07-18.08.26) through a picker that
(a) did not net supplier returns (fixed 20260823_drcr_return_reduces_stock.sql) and
(b) has no inward_date <= issue_date guard, so issues were linked to returned or
future-received lots. This script moves 7 issue lines to the correct lots.

NOT touched (blocked): TKT-000106 (net -40, needs reporter), TKT-000082 (superadmin
ruling pending), TKT-000107 line 160151 (soft-deleted lot, no headroom).

Usage (from repo root, venv active):
    python dbqueries/repairs/tkt_srn_repoint_20260824.py --check    # SELECT-only precheck
    python dbqueries/repairs/tkt_srn_repoint_20260824.py --apply    # precheck + UPDATE + postcheck

--apply writes rollback SQL next to this file: tkt_srn_repoint_20260824_rollback.sql
Rollback = run that file. updated_by is set to 1 (sls repair marker convention).
"""
import sys
from pathlib import Path

import pymysql

ENV = Path(__file__).resolve().parents[2] / "env" / "database.env"
DB = "sls"

# (issue_li_id, expected_current_inward_dtl_id, new_inward_dtl_id, expected_issue_qty, ticket)
ROWS = [
    (161085, 18764, 42145, 20, "TKT-000108"),
    (159808, 42098, 23622, 1, "TKT-000105"),
    (159809, 42153, 23789, 1, "TKT-000105"),
    (160684, 23789, 42098, 1, "TKT-000105"),
    (160863, 23622, 42153, 1, "TKT-000105"),
    (159807, 41931, 23625, 1, "TKT-000107"),
    (161191, 23625, 41931, 1, "TKT-000107"),
]
LOTS = sorted({r[1] for r in ROWS} | {r[2] for r in ROWS})


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

    rollback = Path(__file__).with_name("tkt_srn_repoint_20260824_rollback.sql")
    lines = [f"-- rollback for tkt_srn_repoint_20260824 on {DB}"]
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
