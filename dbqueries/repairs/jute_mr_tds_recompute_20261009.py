"""
JUTETDS-20261009 — store the app's own 194Q TDS on every approved FY 26-27 jute MR and
refresh its money columns, so the MR, its bill pass and the Tally purchase export agree.

Owner ruling 2026-10-09: the Jute Purchase Tally export showed approved FY 26-27 MRs with
(a) tds_amount NULL (approved before TDS was stored: 294 on sls), and (b) tds_amount 0 where
the current 194Q rule (derive_mr_tds, owner ruling 2026-10-03) charges TDS (~135 on sls),
plus a few whose stored net_total still had paise from before whole-rupee rounding.
"Recompute them" and "fix these as well".

Per MR, in the ERP's 194Q order (jute_mr_date, branch_mr_no, jute_mr_id) so each MR sees
the refreshed totals of the party's earlier MRs — exactly what approve / bill pass do:
    tds   = derive_mr_tds(total_amount from calculate_mr_amounts)
    money = recompute_mr_money(mr, tds)   -> jute_mr_li.total_price, jute_mr.total_amount,
                                             claim_amount, tds_amount, roundoff, net_total
MRs whose stored values already match are not written (audit triggers stay quiet).

Scope: status 3, jute_mr_date >= 2026-04-01, live lines all carrying accepted_weight (kg era).
No accounting postings exist for these MRs (sls has no acc_* tables; dev3 has no JUTE postings).

Usage (cwd = vowerp3be):
    .venv/Scripts/python dbqueries/repairs/jute_mr_tds_recompute_20261009.py dev3           # dry run
    .venv/Scripts/python dbqueries/repairs/jute_mr_tds_recompute_20261009.py sls --apply   # execute
Every run writes a plan CSV; --apply also writes a rollback SQL file (row backups as
UPDATEs) next to this script BEFORE committing. Files are timestamped, never overwritten.
"""
import csv
import datetime
import sys
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.juteProcurement.mr import calculate_mr_amounts, derive_mr_tds, recompute_mr_money  # noqa: E402
from src.juteProcurement.totals import compute_jute_totals  # noqa: E402

TAG = "JUTETDS-20261009"
FY_START = "2026-04-01"
TENANT = sys.argv[1] if len(sys.argv) > 1 else ""
APPLY = "--apply" in sys.argv
if TENANT not in ("dev3", "sls"):
    sys.exit("usage: jute_mr_tds_recompute_20261009.py dev3|sls [--apply]")

MONEY_COLS = ("total_amount", "claim_amount", "tds_amount", "roundoff", "net_total")

SCOPE_SQL = text("""
    SELECT jm.jute_mr_id, bm.co_id, jm.party_id, jm.jute_mr_date, jm.branch_mr_no, jm.mr_weight,
           jm.total_amount, jm.claim_amount, jm.tds_amount, jm.roundoff, jm.net_total
    FROM jute_mr jm
    JOIN branch_mst bm ON bm.branch_id = jm.branch_id
    WHERE jm.status_id = 3
      AND jm.jute_mr_date >= :fy_start
      AND EXISTS (SELECT 1 FROM jute_mr_li li WHERE li.jute_mr_id = jm.jute_mr_id
                  AND (li.active = 1 OR li.active IS NULL))
      AND NOT EXISTS (SELECT 1 FROM jute_mr_li li WHERE li.jute_mr_id = jm.jute_mr_id
                      AND (li.active = 1 OR li.active IS NULL) AND li.accepted_weight IS NULL)
    ORDER BY jm.jute_mr_date, COALESCE(jm.branch_mr_no, 0), jm.jute_mr_id
""")
LINES_SQL = text("""
    SELECT jute_mr_li_id, total_price,
           ROUND((COALESCE(accepted_weight, actual_weight, 0) / 100) * COALESCE(rate, 0), 2) AS new_price
    FROM jute_mr_li WHERE jute_mr_id = :mr_id AND (active = 1 OR active IS NULL)
""")


def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def differs(old, new) -> bool:
    return old is None or abs(float(old) - float(new)) > 0.004


e = dotenv_values(ROOT / "env" / "database.env")
engine = create_engine(
    f"mysql+pymysql://{e['DATABASE_USER']}:{quote_plus(e['DATABASE_PASSWORD'])}"
    f"@{e['DATABASE_HOST']}:{e['DATABASE_PORT']}/{TENANT}"
)
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
base = Path(__file__).with_suffix("")
plan_path = Path(f"{base}_{TENANT}_{stamp}_plan.csv")
rollback_path = Path(f"{base}_{TENANT}_{stamp}_rollback.sql")

plan, rollback = [], []
with Session(engine) as db:
    mrs = [dict(r._mapping) for r in db.execute(SCOPE_SQL, {"fy_start": FY_START})]
    for mr in mrs:
        mr_id = mr["jute_mr_id"]
        amounts = calculate_mr_amounts(db, mr_id)
        tds = derive_mr_tds(db, party_id=mr["party_id"], mr_date=mr["jute_mr_date"], mr_id=mr_id,
                            branch_mr_no=mr["branch_mr_no"], total_amount=amounts["total_amount"])["tds_amount"]
        total, claim, tds = round(amounts["total_amount"], 2), round(amounts["claim_amount"], 2), round(tds, 2)
        roundoff, net = compute_jute_totals(total, claim, tds)
        new = dict(zip(MONEY_COLS, (total, claim, tds, roundoff, net)))
        lines = [dict(r._mapping) for r in db.execute(LINES_SQL, {"mr_id": mr_id})]
        stale_lines = [ln for ln in lines if differs(ln["total_price"], ln["new_price"])]
        if not stale_lines and not any(differs(mr[c], new[c]) for c in MONEY_COLS):
            continue

        rollback.append(
            "UPDATE jute_mr SET " + ", ".join(f"{c} = {lit(mr[c])}" for c in (*MONEY_COLS, "mr_weight"))
            + f" WHERE jute_mr_id = {mr_id};"
        )
        rollback.extend(
            f"UPDATE jute_mr_li SET total_price = {lit(None if ln['total_price'] is None else float(ln['total_price']))}"
            f" WHERE jute_mr_li_id = {ln['jute_mr_li_id']};"
            for ln in lines
        )
        money = recompute_mr_money(db, mr_id, tds_amount=tds)
        assert all(not differs(money[c], new[c]) for c in MONEY_COLS), (mr_id, money, new)
        plan.append({
            "co_id": mr["co_id"], "jute_mr_id": mr_id, "jute_mr_date": mr["jute_mr_date"],
            "party_id": mr["party_id"],
            **{f"old_{c}": mr[c] for c in MONEY_COLS}, **{f"new_{c}": new[c] for c in MONEY_COLS},
            "stale_lines": len(stale_lines),
        })

    with open(plan_path, "w", newline="", encoding="utf-8") as f:
        if plan:
            w = csv.DictWriter(f, fieldnames=list(plan[0]))
            w.writeheader()
            w.writerows(plan)

    def count(pred):
        return sum(1 for p in plan if pred(p))

    print(f"{TAG} {TENANT}: {len(mrs)} MRs in scope, {len(plan)} to update")
    print(f"  TDS NULL -> stored:        {count(lambda p: p['old_tds_amount'] is None)}")
    print(f"  TDS 0 -> due:              {count(lambda p: p['old_tds_amount'] == 0 and p['new_tds_amount'] > 0)}")
    print(f"  TDS changed otherwise:     {count(lambda p: p['old_tds_amount'] not in (None, 0) and differs(p['old_tds_amount'], p['new_tds_amount']))}")
    print(f"  net_total changed:         {count(lambda p: differs(p['old_net_total'], p['new_net_total']))}")
    print(f"  line total_price refreshed:{sum(p['stale_lines'] for p in plan)}")
    print(f"  TDS added  (Rs): {sum(p['new_tds_amount'] - float(p['old_tds_amount'] or 0) for p in plan):,.2f}")
    print(f"  plan: {plan_path}")

    if APPLY and plan:
        rollback_path.write_text(
            f"-- {TAG} rollback for {TENANT} ({len(plan)} MRs), written {stamp} before commit\n"
            + "\n".join(rollback) + "\n",
            encoding="utf-8",
        )
        db.commit()
        print(f"  APPLIED. rollback: {rollback_path}")
    else:
        db.rollback()
        print("  dry run - rolled back" if not APPLY else "  nothing to apply")
