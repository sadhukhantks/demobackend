"""
Set the jute rates of sls MRs 28374 and 28371 and of their PO 13390 (owner request, 2026-10-02).

    EJM (co 2, branch 29 FACTORY), supplier Loknath Jute Pvt Ltd, PO 51 (jute_po_id 13390).
    Both MRs are Pending (13) with NO rate on any line and 0 amounts, and neither has
    been transferred to Jagrati yet. The juteTransfer app takes a Jagrati hop's rate
    from the root MR line, so these rates also become the rates of the Jagrati MR when
    the transfer is made.

Owner rulings 2026-10-02: rate by the quality the line was ORDERED as (the PO line it
belongs to): TD-5 = 12,400, TD-6 = 12,100 per quintal. MR 28371's 230 kg on 60 bales is
correct. The PO lines get the same rates.

    MR line   MR     ordered (PO line)   received   kg      rate     total_price
    46103     28374  TD-5 (31414)        D TD-6     5,065   12,400   6,28,060.00
    46104     28374  TD-6 (31415)        D TD-7     5,115   12,100   6,18,915.00
    46101     28371  TD-6 (31415)        D TD-6       230   12,100     27,830.00

    PO line 31414  D TD-5  100 qtl  12,500 -> 12,400  value 12,50,000 -> 12,40,000
    PO line 31415  D TD-6  100 qtl  12,300 -> 12,100  value 12,30,000 -> 12,10,000
    PO 13390 value 24,80,000 -> 24,50,000

MR money is recomputed with the app's own recompute_mr_money (line total_price, header
total / claim / round-off / net; the stored TDS of a Pending MR is kept, as the app does).
A line's rate and actual_rate are written together, as the MR save does.

Usage (cwd = vowerp3be):
    uv run python dbqueries/repairs/sls_mr28374_28371_po13390_rates_20261002.py           # dry run
    uv run python dbqueries/repairs/sls_mr28374_28371_po13390_rates_20261002.py --apply   # execute
A rollback SQL file (the exact previous values) is written next to this script before
anything is changed.
"""
import datetime
import os
import sys

sys.path.insert(0, os.getcwd())
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.config.db import get_tenant_engine  # noqa: E402
from src.juteProcurement.mr import recompute_mr_money  # noqa: E402

TENANT = "sls"
UPDATED_BY = 24  # owner's sls login, who gave the ruling
APPLY = "--apply" in sys.argv
PO_ID = 13390

# jute_mr_li_id -> (jute_mr_id, jute_po_li_id, actual_item_id, accepted kg, new rate)
LINES = {
    46103: (28374, 31414, 57, 5065, 12400.0),
    46104: (28374, 31415, 59, 5115, 12100.0),
    46101: (28371, 31415, 57, 230, 12100.0),
}
MR_IDS = (28371, 28374)
# jute_po_li_id -> (item_id, old rate, old value, new rate)
PO_LINES = {
    31414: (56, 12500.0, 1250000.0, 12400.0),
    31415: (57, 12300.0, 1230000.0, 12100.0),
}

MR_COLS = "jute_mr_id, status_id, branch_id, po_id, mr_weight, total_amount, claim_amount, tds_amount, roundoff, net_total, bill_pass_no, updated_by, updated_date_time"
LI_COLS = "jute_mr_li_id, jute_mr_id, jute_po_li_id, actual_item_id, accepted_weight, actual_weight, rate, actual_rate, claim_rate, total_price, active, updated_date_time"
PO_COLS = "jute_po_id, status_id, jute_po_value, updated_by, updated_date_time"
POLI_COLS = "jute_po_li_id, jute_po_id, item_id, rate, value, active, updated_date_time"


def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def rows(db, sql, **p):
    return [dict(r._mapping) for r in db.execute(text(sql), p).fetchall()]


def snapshot(db):
    return {
        "mr": {r["jute_mr_id"]: r for r in rows(db, f"SELECT {MR_COLS} FROM jute_mr WHERE jute_mr_id IN :ids", ids=MR_IDS)},
        "li": {r["jute_mr_li_id"]: r for r in rows(db, f"SELECT {LI_COLS} FROM jute_mr_li WHERE jute_mr_id IN :ids", ids=MR_IDS)},
        "po": rows(db, f"SELECT {PO_COLS} FROM jute_po WHERE jute_po_id = :p", p=PO_ID)[0],
        "poli": {r["jute_po_li_id"]: r for r in rows(db, f"SELECT {POLI_COLS} FROM jute_po_li WHERE jute_po_id = :p", p=PO_ID)},
    }


def guards(db, s):
    assert set(s["mr"]) == set(MR_IDS), s["mr"]
    for mr_id, r in s["mr"].items():
        assert r["status_id"] == 13 and r["branch_id"] == 29 and r["po_id"] == PO_ID, r
        assert not r["bill_pass_no"], f"MR {mr_id} has a bill pass"
    children = rows(db, "SELECT jute_mr_id FROM jute_mr WHERE src_jute_mr_id IN :ids", ids=MR_IDS)
    assert not children, f"transfer children exist: {children}"
    issues = rows(db, "SELECT COUNT(*) AS n FROM jute_issue WHERE jute_mr_li_id IN :li AND status_id <> 4", li=tuple(LINES))
    assert issues[0]["n"] == 0, "issues already drawn from these lines"
    active = {k: v for k, v in s["li"].items() if v["active"] in (1, None)}
    assert set(active) == set(LINES), f"active lines {sorted(active)} != {sorted(LINES)}"
    for li_id, (mr_id, po_li, item, kg, _new) in LINES.items():
        r = s["li"][li_id]
        assert (r["jute_mr_id"], r["jute_po_li_id"], r["actual_item_id"]) == (mr_id, po_li, item), r
        assert int(round(r["accepted_weight"] or r["actual_weight"] or 0)) == kg, r
        assert r["rate"] is None, f"line {li_id} already has a rate ({r['rate']}) -- re-run refused"
    assert s["po"]["status_id"] == 3, s["po"]
    assert set(s["poli"]) == set(PO_LINES), s["poli"]
    for po_li, (item, old_rate, old_value, _new) in PO_LINES.items():
        r = s["poli"][po_li]
        assert (r["item_id"], float(r["rate"]), float(r["value"])) == (item, old_rate, old_value), r


def rollback_sql(s, stamp):
    out = [f"-- rollback for sls_mr28374_28371_po13390_rates on {TENANT}, generated {stamp}", "START TRANSACTION;"]
    for li_id in LINES:
        r = s["li"][li_id]
        out.append(f"UPDATE jute_mr_li SET rate = {lit(r['rate'])}, actual_rate = {lit(r['actual_rate'])}, total_price = {lit(float(r['total_price']))}, "
                   f"updated_date_time = {lit(r['updated_date_time'])} WHERE jute_mr_li_id = {li_id};")
    for mr_id, r in s["mr"].items():
        out.append(f"UPDATE jute_mr SET total_amount = {lit(r['total_amount'])}, claim_amount = {lit(r['claim_amount'])}, tds_amount = {lit(r['tds_amount'])}, "
                   f"roundoff = {lit(r['roundoff'])}, net_total = {lit(r['net_total'])}, mr_weight = {lit(r['mr_weight'])}, "
                   f"updated_by = {lit(r['updated_by'])}, updated_date_time = {lit(r['updated_date_time'])} WHERE jute_mr_id = {mr_id};")
    for po_li in PO_LINES:
        r = s["poli"][po_li]
        out.append(f"UPDATE jute_po_li SET rate = {lit(float(r['rate']))}, value = {lit(float(r['value']))}, "
                   f"updated_date_time = {lit(r['updated_date_time'])} WHERE jute_po_li_id = {po_li};")
    p = s["po"]
    out.append(f"UPDATE jute_po SET jute_po_value = {lit(float(p['jute_po_value']))}, updated_by = {lit(p['updated_by'])}, "
               f"updated_date_time = {lit(p['updated_date_time'])} WHERE jute_po_id = {PO_ID};")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def show(title, s):
    print(f"--- {title}")
    for li_id in LINES:
        r = s["li"][li_id]
        print(f"  line {li_id} MR {r['jute_mr_id']} item {r['actual_item_id']} {r['accepted_weight']:>6} kg  rate {r['rate']}  actual_rate {r['actual_rate']}  total_price {r['total_price']}")
    for mr_id, r in sorted(s["mr"].items()):
        print(f"  MR {mr_id}: total {r['total_amount']}  claim {r['claim_amount']}  tds {r['tds_amount']}  roundoff {r['roundoff']}  net {r['net_total']}  mr_weight {r['mr_weight']}")
    for po_li, r in sorted(s["poli"].items()):
        print(f"  PO line {po_li} item {r['item_id']}: rate {r['rate']}  value {r['value']}")
    print(f"  PO {PO_ID}: value {s['po']['jute_po_value']}")


with Session(get_tenant_engine(TENANT)) as db:
    before = snapshot(db)
    guards(db, before)
    show("BEFORE", before)
    print("--- PLAN")
    for li_id, (mr_id, _po_li, _item, kg, new) in LINES.items():
        print(f"  line {li_id} (MR {mr_id}): rate NULL -> {new:.0f}, total_price -> {round(kg / 100 * new, 2):,.2f}")
    for mr_id in MR_IDS:
        tot = sum(round(kg / 100 * new, 2) for (m, _p, _i, kg, new) in LINES.values() if m == mr_id)
        print(f"  MR {mr_id}: total_amount -> {tot:,.2f}")
    new_po_total = 0.0
    for po_li, (_item, old_rate, old_value, new) in PO_LINES.items():
        qtl = old_value / old_rate
        new_value = round(qtl * new, 2)
        new_po_total += new_value
        print(f"  PO line {po_li}: {qtl:g} qtl  rate {old_rate:.0f} -> {new:.0f}, value {old_value:,.2f} -> {new_value:,.2f}")
    print(f"  PO {PO_ID}: value {float(before['po']['jute_po_value']):,.2f} -> {new_po_total:,.2f}")
    db.rollback()

    if not APPLY:
        print("\nDRY RUN - nothing written. Re-run with --apply to execute.")
        sys.exit(0)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    rb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"sls_mr28374_28371_po13390_rates_20261002_{stamp}_rollback.sql")
    with open(rb_path, "w") as fh:
        fh.write(rollback_sql(before, stamp))
    print(f"\nrollback written: {rb_path}")

    try:
        guards(db, snapshot(db))  # re-check inside the write transaction
        for li_id, (mr_id, _po_li, _item, _kg, new) in LINES.items():
            n = db.execute(text("UPDATE jute_mr_li SET rate = :r, actual_rate = :r, updated_date_time = NOW() "
                                "WHERE jute_mr_li_id = :li AND jute_mr_id = :mr AND rate IS NULL"),
                           {"r": new, "li": li_id, "mr": mr_id}).rowcount
            assert n == 1, f"line {li_id}: {n} rows"
        for mr_id in MR_IDS:
            recompute_mr_money(db, mr_id)  # tds_amount None = keep the stored TDS (Pending MR)
            n = db.execute(text("UPDATE jute_mr SET updated_by = :u, updated_date_time = NOW() WHERE jute_mr_id = :mr"),
                           {"u": UPDATED_BY, "mr": mr_id}).rowcount
            assert n == 1
        for po_li, (_item, old_rate, old_value, new) in PO_LINES.items():
            new_value = round(old_value / old_rate * new, 2)
            n = db.execute(text("UPDATE jute_po_li SET rate = :r, value = :v, updated_date_time = NOW() "
                                "WHERE jute_po_li_id = :li AND jute_po_id = :po AND rate = :old"),
                           {"r": new, "v": new_value, "li": po_li, "po": PO_ID, "old": old_rate}).rowcount
            assert n == 1, f"PO line {po_li}: {n} rows"
        n = db.execute(text("UPDATE jute_po SET jute_po_value = (SELECT ROUND(SUM(value), 2) FROM jute_po_li WHERE jute_po_id = :po AND active = 1), "
                            "updated_by = :u, updated_date_time = NOW() WHERE jute_po_id = :po"),
                       {"po": PO_ID, "u": UPDATED_BY}).rowcount
        assert n == 1
        db.commit()
    except Exception:
        db.rollback()
        print("FAILED - rolled back, nothing changed")
        raise

    show("AFTER", snapshot(db))
    print("\nAPPLIED.")
