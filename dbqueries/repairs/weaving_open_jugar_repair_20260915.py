"""
Repair jute_prod_weaving_daily.open_jugar under the 2026-09-15 chain ruling
(TKT-000150 / TKT-000156): the jugar chain follows EFFECTIVE START TIME, not spell
codes. eff_start = tran_date minus one day when spell_mst.is_overnight = 1, plus
spell_mst.starting_time. Tiebreak = legacy spell-code rank (A1 B1 A2 B2 C, else 99),
then weaving_daily_id. Partition = (co_id, machine_id, weaving_quality_id).
Pure twin of weaving_query._open_jugar_probe_sql (same order, same COALESCE-0 rule).

Scope (CLI): one company, rows with tran_date >= --from. The chain is walked over
rows from (--from minus --lookback days) so predecessors before --from are seen;
only rows >= --from are written.

Skipped on purpose (reported in the plan CSV, never written):
  * rows with NO predecessor inside the lookback window (would resolve to 0 blindly).
Gap cap: a predecessor older than --maxgap days (default 45 = WEAVING_CHAIN_MAX_GAP_DAYS)
counts as none, so the row opens at 0 - same as the app since 2026-09-16.

Writes (only with --apply):
  1. UPDATE open_jugar on rows whose stored value differs (batched CASE update, no
     updated_by stamp - derived data, same as the app writers).
  2. reprocess_needed = 1 on jute_prod_weaving_process_lock headers of every unit
     (co, tran_date, spell) that had a row changed, so the next Sync re-freezes
     jute_prod_weaving_log with the corrected opening / jugar / production.
Rollback: <out>_rollback.sql (UPDATE ... SET open_jugar = <old> per row).

Usage:
  python dbqueries/repairs/weaving_open_jugar_repair_20260915.py --db sls --co 2 --from 2026-08-01            # dry run
  python dbqueries/repairs/weaving_open_jugar_repair_20260915.py --db sls --co 2 --from 2026-08-01 --apply
"""
import argparse, collections, csv, datetime as dt, sys
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
from src.juteProduction.services.weaving_rules import chain_effective_start  # noqa: E402
from src.juteProduction.constants import WEAVING_CHAIN_MAX_GAP_DAYS  # noqa: E402

RANK = {"A1": 1, "B1": 2, "A2": 3, "B2": 4, "C": 5}

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--co", type=int, required=True)
ap.add_argument("--from", dest="from_date", required=True)
ap.add_argument("--lookback", type=int, default=400, help="days before --from loaded for predecessors")
ap.add_argument("--maxgap", type=int, default=WEAVING_CHAIN_MAX_GAP_DAYS, help="chain gap cap in DAYS between tran_dates (DATEDIFF, same as the SQL): older predecessor -> opening 0")
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()
FROM = dt.date.fromisoformat(args.from_date)
LOAD_FROM = FROM - dt.timedelta(days=args.lookback)

env = {}
for line in open(REPO / "env" / "database.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k] = v
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database=args.db, autocommit=False)
cur = conn.cursor()
cur.execute("SELECT spell_id, spell_code, starting_time, is_overnight FROM spell_mst")
spells = {sid: (code, st, ov) for sid, code, st, ov in cur.fetchall()}
cur.execute("""SELECT weaving_daily_id, machine_id, weaving_quality_id, tran_date, spell_id, close_jugar, open_jugar
               FROM jute_prod_weaving_daily
               WHERE co_id = %s AND active = 1 AND tran_date >= %s""", (args.co, LOAD_FROM))
raw = cur.fetchall()
print(f"[{args.db}] co {args.co}: {len(raw)} active rows loaded from {LOAD_FROM} (writes only >= {FROM})")

f = lambda x: float(x or 0)
# tuple layout: (eff, rk, id, machine, q, date, spell_id, code, cj, oj)
parts = collections.defaultdict(list)
for rid, m, q, d, sid, cj, oj in raw:
    code, st, ov = spells.get(sid, (None, None, None))
    parts[(m, q)].append((chain_effective_start(d, st, ov), RANK.get(code, 99), rid, m, q, d, sid, code, f(cj), f(oj)))
del raw

changes, capped_gap, skipped_nopred, units = [], [], [], set()
for part in parts.values():
    part.sort()
    prev = None
    for r in part:
        eff, rk, rid, m, q, d, sid, code, cj, oj = r
        new = prev[8] if prev else 0.0
        if q is None:
            new = 0.0  # NULL quality never chains (matches app writers + window + view)
        if d >= FROM:
            if q is None:
                if oj != new:
                    changes.append((r, new, prev or r)); units.add((d, sid))
            elif prev is None:
                if oj != new:
                    skipped_nopred.append((r, new))
            elif (d - prev[5]).days > args.maxgap:
                # gap cap (2026-09-16): predecessor too old -> opening 0, same as the app.
                # Measured on tran_date like the SQL DATEDIFF, NOT on start datetimes
                # (a 45d 8h span is DATEDIFF 46 -> capped).
                new = 0.0
                if oj != new:
                    changes.append((r, new, prev))
                    units.add((d, sid))
                    capped_gap.append((r, new, prev))
            elif oj != new:
                changes.append((r, new, prev))
                units.add((d, sid))
        prev = r

by_spell = collections.Counter(r[7] for r, _, _ in changes)
by_date = collections.Counter(r[5] for r, _, _ in changes)
print(f"rows to change: {len(changes)}  by spell {dict(by_spell)}")
print("by date:", ", ".join(f"{d} {n}" for d, n in sorted(by_date.items())))
print(f"of which capped to 0 (predecessor > {args.maxgap} days old): {len(capped_gap)}")
print(f"skipped (no predecessor inside lookback, stored != 0): {len(skipped_nopred)}")
print(f"units to flag stale: {len(units)}")

stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
base = HERE / f"weaving_open_jugar_repair_{args.db}_co{args.co}_{stamp}"
with open(f"{base}_plan.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["weaving_daily_id", "machine_id", "quality", "tran_date", "spell", "close_jugar",
                "old_open_jugar", "new_open_jugar", "from_spell", "from_date", "from_close", "status"])
    capped_ids = {r[2] for r, _, _ in capped_gap}
    for r, new, prev in changes:
        status = f"CHANGE gap-capped {(r[5] - prev[5]).days}d" if r[2] in capped_ids else "CHANGE"
        w.writerow([r[2], r[3], r[4], r[5], r[7], r[8], r[9], new, prev[7], prev[5], prev[8], status])
    for r, new in skipped_nopred:
        w.writerow([r[2], r[3], r[4], r[5], r[7], r[8], r[9], new, "", "", "", "SKIP no predecessor in lookback"])
with open(f"{base}_rollback.sql", "w") as fh:
    fh.write(f"-- rollback for weaving_open_jugar_repair {args.db} co {args.co} from {FROM} ({stamp})\n")
    for r, new, prev in changes:
        fh.write(f"UPDATE jute_prod_weaving_daily SET open_jugar = {r[9]} WHERE weaving_daily_id = {r[2]};\n")
    for d, sid in sorted(units):
        fh.write(f"-- header ({d}, spell {sid}) reprocess_needed was set to 1 by the repair\n")
print("plan:", f"{base}_plan.csv")
print("rollback:", f"{base}_rollback.sql")

if not args.apply:
    print("DRY RUN - nothing written. Re-run with --apply.")
    conn.close(); sys.exit(0)

B = 500
for i in range(0, len(changes), B):
    chunk = changes[i:i + B]
    whens = " ".join(f"WHEN {r[2]} THEN {new}" for r, new, _ in chunk)
    ids = ",".join(str(r[2]) for r, _, _ in chunk)
    cur.execute(f"UPDATE jute_prod_weaving_daily SET open_jugar = CASE weaving_daily_id {whens} END "
                f"WHERE weaving_daily_id IN ({ids})")
flagged = 0
for d, sid in sorted(units):
    cur.execute("UPDATE jute_prod_weaving_process_lock SET reprocess_needed = 1 "
                "WHERE co_id = %s AND tran_date = %s AND spell_id = %s AND active = 1", (args.co, d, sid))
    flagged += cur.rowcount
conn.commit()
print(f"APPLIED: {len(changes)} rows updated, {flagged} headers flagged stale.")
conn.close()
