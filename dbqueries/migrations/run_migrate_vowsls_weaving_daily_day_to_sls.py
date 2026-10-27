"""
Migrate ONE day of legacy weaving production for NJM (legacy company 1) from
`vowsls.cuts_jugar_buff_1` into `sls.jute_prod_weaving_daily` as co 106 / branch 87.

Same unpivot + mapping rules as the 2026-07-05 bulk run (SPEC_weaving_migration_sls.md §2):
one legacy loom-day row has 5 spell column groups (_a1/_a2/_b1/_b2/_c); each becomes one
target row iff its quality code resolves AND any of cuts/close/production/jugar is non-NULL.

  spell    -> branch-87 spell_mst  A1=102 A2=103 B1=105 B2=106 C=108
  loom_id  -> machine_mst_sls_co1_branch87_map.csv
  quality  -> jute_prod_weaving_quality_sls_map.csv (legacy company 1 rows)
  ticket   -> sls.hrms_ed_official_details.emp_code (branch 87, MAX eb_id) ; blank/unknown -> NULL
  cuts=COALESCE(cuts,0)  close_jugar=close  less_production=less_production  active=COALESCE(is_active,1)
  updated_by = 1 (marks migration rows; app writers stamp real user ids)

open_jugar is a STORED chain column (weaving_query._open_jugar_probe_sql). After the insert
every active row of the day is re-resolved, then each row's single chain successor
(get_weaving_chain_successor_query, ported verbatim) is re-resolved too — exactly what the app
writers do per saved row.

Idempotent: INSERT IGNORE on uq_weaving_daily_unit_machine (co_id, tran_date, spell_id,
machine_id, active) — rerunning only fills gaps.

ROLLBACK (per day):
  DELETE FROM sls.jute_prod_weaving_daily WHERE co_id=106 AND tran_date='<date>' AND updated_by=1;
  -- then re-run this script's chain repair for the next day, or backfill_weaving_open_jugar.sql
  DELETE FROM vowsls_to_sls_transfer.migration_log WHERE step='vowsls_weaving_daily_<date>';

Usage:
  python run_migrate_vowsls_weaving_daily_day_to_sls.py --date 2026-08-02            # dry run
  python run_migrate_vowsls_weaving_daily_day_to_sls.py --date 2026-08-02 --commit   # apply
"""
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import pymysql

SRC_CO, DST_CO, DST_BRANCH, UPDATED_BY = 1, 106, 87, 1
SPELLS = {"a1": 102, "a2": 103, "b1": 105, "b2": 106, "c": 108}
RANK = "CASE {a}.spell_code WHEN 'A1' THEN 1 WHEN 'B1' THEN 2 WHEN 'A2' THEN 3 WHEN 'B2' THEN 4 WHEN 'C' THEN 5 ELSE 99 END"
# 2026-09-15: chain order = effective start time (overnight spell dated D started on D-1);
# RANK is a tiebreak only. NOTE: the chain-repair SQL below still uses the OLD order and
# must be re-ported from weaving_query._open_jugar_probe_sql before the next day import.
CHAIN_ORDER_STALE = True

COMMIT = "--commit" in sys.argv
DATE = dt.date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def unpivot(r: dict, qmap: dict, mmap: dict, ebmap: dict) -> tuple[list[tuple], dict]:
    """Legacy wide row -> list of target tuples + skip counters."""
    out, skip = [], {"no_quality": 0, "unmapped_quality": 0, "unmapped_loom": 0}
    if r["loom_id"] not in mmap:
        skip["unmapped_loom"] += 1
        return out, skip
    for sp, spell_id in SPELLS.items():
        if all(r[f"{k}_{sp}"] is None for k in ("cuts", "close", "production", "jugar")):
            continue
        qc = (r[f"quality_code_{sp}"] or "").strip()
        if not qc:
            skip["no_quality"] += 1
            continue
        if qc not in qmap:
            skip["unmapped_quality"] += 1
            continue
        ticket = (r[f"ticket_no_{sp}"] or "").strip()
        out.append((DST_CO, DST_BRANCH, r["loom_date"], spell_id, mmap[r["loom_id"]], qmap[qc],
                    ebmap.get(ticket), r[f"cuts_{sp}"] or 0, r[f"less_production_{sp.upper()}"] or 0,
                    r[f"close_{sp}"], 1 if r["is_active"] is None else r["is_active"], UPDATED_BY))
    return out, skip


def _selftest() -> None:
    base = {f"{k}_{s}": None for s in SPELLS for k in ("cuts", "close", "production", "jugar", "quality_code", "ticket_no")}
    base.update({f"less_production_{s.upper()}": None for s in SPELLS})
    row = dict(base, loom_id=5, loom_date=dt.date(2026, 8, 2), is_active=None,
               cuts_a1=3, close_a1=7, quality_code_a1="Q1", ticket_no_a1="T1",
               cuts_b1=1, quality_code_b1="",            # data, no quality -> skipped
               jugar_c=2, quality_code_c="ZZ")           # unmapped quality -> skipped
    rows, skip = unpivot(row, {"Q1": 44}, {5: 555}, {"T1": 99})
    assert rows == [(106, 87, dt.date(2026, 8, 2), 102, 555, 44, 99, 3, 0, 7, 1, 1)], rows
    assert skip == {"no_quality": 1, "unmapped_quality": 1, "unmapped_loom": 0}, skip
    assert unpivot(dict(row, loom_id=6), {"Q1": 44}, {5: 555}, {})[0] == []


_selftest()

ENV = {}
for line in (REPO / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v
conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"], password=ENV["DATABASE_PASSWORD"],
                       port=int(ENV["DATABASE_PORT"]), autocommit=False, connect_timeout=30, read_timeout=1800)
cur = conn.cursor()

qmap = {r["quality_code"].strip(): int(r["sls_weaving_quality_id"])
        for r in csv.DictReader(open(HERE / "jute_prod_weaving_quality_sls_map.csv"))
        if int(r["vowsls_company_id"]) == SRC_CO}
mmap = {int(r["vowsls_mechine_id"]): int(r["sls_machine_id"])
        for r in csv.DictReader(open(HERE / "machine_mst_sls_co1_branch87_map.csv"))}

cur.execute("SELECT * FROM vowsls.cuts_jugar_buff_1 WHERE company_id=%s AND loom_date=%s", (SRC_CO, DATE))
cols = [d[0] for d in cur.description]
src = [dict(zip(cols, r)) for r in cur.fetchall()]

tickets = {(r[f"ticket_no_{sp}"] or "").strip() for r in src for sp in SPELLS} - {""}
ebmap = {}
if tickets:
    cur.execute(f"SELECT emp_code, MAX(eb_id) FROM sls.hrms_ed_official_details WHERE branch_id=%s "
                f"AND emp_code IN ({','.join(['%s'] * len(tickets))}) GROUP BY emp_code", [DST_BRANCH, *tickets])
    ebmap = dict(cur.fetchall())

rows, skip = [], {"no_quality": 0, "unmapped_quality": 0, "unmapped_loom": 0}
for r in src:
    o, s = unpivot(r, qmap, mmap, ebmap)
    rows += o
    for k in skip:
        skip[k] += s[k]

cur.execute("SELECT COUNT(*) FROM sls.jute_prod_weaving_daily WHERE co_id=%s AND tran_date=%s", (DST_CO, DATE))
pre_existing = cur.fetchone()[0]
STATS = {"date": str(DATE), "source_rows": len(src), "emit_rows": len(rows), "with_eb": sum(1 for r in rows if r[6]),
         "tickets": len(tickets), "tickets_resolved": len(ebmap), "skipped_cells": skip,
         "target_rows_before": pre_existing}
print(json.dumps(STATS, indent=1, default=str))
if not src:
    print(f"\nNo legacy rows for company {SRC_CO} on {DATE} — nothing to migrate.")
    sys.exit(0)
if not COMMIT:
    print("\nDry run — nothing written. Re-run with --commit to apply.")
    sys.exit(0)

cur.executemany("""INSERT IGNORE INTO sls.jute_prod_weaving_daily
    (co_id, branch_id, tran_date, spell_id, machine_id, weaving_quality_id, eb_id, cuts, less_production,
     close_jugar, active, updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
STATS["inserted"] = cur.rowcount

# --- open_jugar chain repair: the day's rows, then each one's single successor (app writer semantics)
PROBE = f"""COALESCE(
  (SELECT COALESCE(p.close_jugar,0) FROM sls.jute_prod_weaving_daily p LEFT JOIN sls.spell_mst ps ON ps.spell_id=p.spell_id
    WHERE p.active=1 AND p.co_id=wd.co_id AND p.machine_id=wd.machine_id
      AND p.tran_date=wd.tran_date AND ({RANK.format(a='ps')}, p.weaving_daily_id) < ({RANK.format(a='sp')}, wd.weaving_daily_id)
    ORDER BY {RANK.format(a='ps')} DESC, p.weaving_daily_id DESC LIMIT 1),
  (SELECT COALESCE(p.close_jugar,0) FROM sls.jute_prod_weaving_daily p LEFT JOIN sls.spell_mst ps ON ps.spell_id=p.spell_id
    WHERE p.active=1 AND p.co_id=wd.co_id AND p.machine_id=wd.machine_id
      AND p.tran_date<wd.tran_date ORDER BY p.tran_date DESC, {RANK.format(a='ps')} DESC, p.weaving_daily_id DESC LIMIT 1),
  0)"""
RESOLVE = f"SELECT {PROBE} FROM sls.jute_prod_weaving_daily wd LEFT JOIN sls.spell_mst sp ON sp.spell_id=wd.spell_id WHERE wd.weaving_daily_id=%s"
SUCCESSOR = f"""SELECT COALESCE(
  (SELECT s.weaving_daily_id FROM sls.jute_prod_weaving_daily s LEFT JOIN sls.spell_mst ps ON ps.spell_id=s.spell_id
    WHERE s.active=1 AND s.co_id=%s AND s.machine_id=%s ANDs.tran_date=%s
      AND ({RANK.format(a='ps')}, s.weaving_daily_id) > (COALESCE((SELECT {RANK.format(a='sp2')} FROM sls.spell_mst sp2 WHERE sp2.spell_id=%s),99), %s)
    ORDER BY {RANK.format(a='ps')} ASC, s.weaving_daily_id ASC LIMIT 1),
  (SELECT s.weaving_daily_id FROM sls.jute_prod_weaving_daily s LEFT JOIN sls.spell_mst ps ON ps.spell_id=s.spell_id
    WHERE s.active=1 AND s.co_id=%s AND s.machine_id=%s ANDs.tran_date>%s
    ORDER BY s.tran_date ASC, {RANK.format(a='ps')} ASC, s.weaving_daily_id ASC LIMIT 1))"""


def resync(row_id: int) -> None:
    cur.execute(RESOLVE, (row_id,))
    cur.execute("UPDATE sls.jute_prod_weaving_daily SET open_jugar=%s WHERE weaving_daily_id=%s", (cur.fetchone()[0], row_id))


cur.execute("SELECT weaving_daily_id, machine_id, spell_id FROM sls.jute_prod_weaving_daily "
            "WHERE co_id=%s AND tran_date=%s AND active=1", (DST_CO, DATE))
day_rows = cur.fetchall()
successors = set()
for rid, mid, spid in day_rows:
    resync(rid)
    cur.execute(SUCCESSOR, (DST_CO, mid, DATE, spid, rid, DST_CO, mid, DATE))
    succ = cur.fetchone()[0]
    if succ:
        successors.add(succ)
for sid in successors:
    resync(sid)
STATS["open_jugar_resynced"] = len(day_rows)
STATS["successors_resynced"] = len(successors)

cur.execute("INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details) "
            "VALUES (%s,'committed',NOW(),NOW(),%s)", (f"vowsls_weaving_daily_{DATE}", json.dumps(STATS, default=str)))
conn.commit()
print(json.dumps(STATS, indent=1, default=str))
print(f"\ncommitted; logged step vowsls_weaving_daily_{DATE}")
