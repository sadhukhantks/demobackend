"""
Backfill spell_id on the vowsls->sls br87 transferred rows (2026-08-01..04):
daily_attendance (transferred 2026-08-25) and daily_ebmc_attendance (2026-08-26).

Requested 2026-08-26: "the spell id was also not posted - check other missing things too".

WHY IT WAS NULL
  vowsls carries only the spell TEXT ('A1'/'B1'/'C'); spell_id is an sls-only column.
  Live br87 entries post spell_id only (spell text NULL).

MAPPING (verified against sls.spell_mst + shift_mst 2026-08-26)
  shift_mst 10/11/12 = branch 87 (A/B/C) -> spell_mst rows:
    'A1' -> 102 (shift 10)   'B1' -> 105 (shift 11)   'C' -> 108 (shift 12)
  These are exactly the three spell_ids all live br87 rows use (Aug-5 onward).
  Window rows carry no other spell values. Spell TEXT is kept as-is (source snapshot).

OTHER-MISSING SWEEP RESULT (2026-08-26, informational)
  - entry_time/exit_time: NULL on ALL 1,828 vowsls source rows - nothing to import.
  - geo_location / entry_source_time / sync_received_time / face columns: app-native,
    no vowsls source (sync/entry-source are NULL even on live rows).
  - Only table referencing daily_atten_id besides these two = a vowsls view.
  - vowsls daily_mc_attendance has no sls counterpart table - nothing to import into.

SCOPE GUARD
  Only rows listed in the transfer audit tables (kind='transferred') AND spell_id IS NULL.

ROLLBACK (committed run only):
  UPDATE sls.daily_attendance a
    JOIN vowsls_to_sls_transfer.att_transfer_log_br87_aug2026 l
      ON l.new_daily_atten_id = a.daily_atten_id AND l.kind = 'transferred'
     SET a.spell_id = NULL;
  UPDATE sls.daily_ebmc_attendance a
    JOIN vowsls_to_sls_transfer.ebmc_transfer_log_br87_aug2026 l
      ON l.new_dtl_rec_id = a.dtl_rec_id AND l.kind = 'transferred'
     SET a.spell_id = NULL;

Usage:
  python run_backfill_spell_id_br87_aug1_4.py            # dry run (rolls back)
  python run_backfill_spell_id_br87_aug1_4.py --commit   # apply
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
HERE = Path(__file__).resolve().parent

ENV = {}
for line in (HERE.parents[1] / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

SPELL_CASE = "CASE a.spell WHEN 'A1' THEN 102 WHEN 'B1' THEN 105 WHEN 'C' THEN 108 END"
ATT_LOG = "vowsls_to_sls_transfer.att_transfer_log_br87_aug2026"
EBMC_LOG = "vowsls_to_sls_transfer.ebmc_transfer_log_br87_aug2026"

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       charset="utf8mb4", autocommit=False)
cur = conn.cursor()
cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")

def log(msg):
    print(msg, flush=True)

def one(sql):
    cur.execute(sql)
    return cur.fetchone()[0]

# guard: the three ids must still be the br87 spells (code + shift branch)
cur.execute("""SELECT sp.spell_id, sp.spell_code, sh.branch_id
                 FROM sls.spell_mst sp JOIN sls.shift_mst sh ON sh.shift_id = sp.shift_id
                WHERE sp.spell_id IN (102, 105, 108)""")
rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
expect = {102: ("A1", 87), 105: ("B1", 87), 108: ("C", 87)}
if rows != expect:
    sys.exit(f"ABORT: spell_mst/shift_mst no longer match expectation: {rows}")

# unexpected spell values would fall through the CASE to NULL - refuse instead
bad = one(f"""SELECT COUNT(*) FROM sls.daily_attendance a
              JOIN {ATT_LOG} l ON l.new_daily_atten_id = a.daily_atten_id
               AND l.kind = 'transferred'
              WHERE a.spell_id IS NULL AND a.spell NOT IN ('A1','B1','C')""")
bad += one(f"""SELECT COUNT(*) FROM sls.daily_ebmc_attendance a
              JOIN {EBMC_LOG} l ON l.new_dtl_rec_id = a.dtl_rec_id
               AND l.kind = 'transferred'
              WHERE a.spell_id IS NULL AND a.spell NOT IN ('A1','B1','C')""")
if bad:
    sys.exit(f"ABORT: {bad} transferred rows carry a spell value outside A1/B1/C.")

cur.execute(f"""UPDATE sls.daily_attendance a
                 JOIN {ATT_LOG} l ON l.new_daily_atten_id = a.daily_atten_id
                  AND l.kind = 'transferred'
                  SET a.spell_id = {SPELL_CASE}
                WHERE a.spell_id IS NULL""")
att_updated = cur.rowcount
log(f"daily_attendance spell_id backfilled: {att_updated}")

cur.execute(f"""UPDATE sls.daily_ebmc_attendance a
                 JOIN {EBMC_LOG} l ON l.new_dtl_rec_id = a.dtl_rec_id
                  AND l.kind = 'transferred'
                  SET a.spell_id = {SPELL_CASE}
                WHERE a.spell_id IS NULL""")
ebmc_updated = cur.rowcount
log(f"daily_ebmc_attendance spell_id backfilled: {ebmc_updated}")

log("\n--- verification ---")
cur.execute(f"""SELECT a.spell, a.spell_id, COUNT(*) FROM sls.daily_attendance a
                 JOIN {ATT_LOG} l ON l.new_daily_atten_id = a.daily_atten_id
                  AND l.kind = 'transferred'
                GROUP BY 1,2""")
att_dist = cur.fetchall()
for r in att_dist:
    log(f"  attendance spell/spell_id: {r}")
cur.execute(f"""SELECT a.spell, a.spell_id, COUNT(*) FROM sls.daily_ebmc_attendance a
                 JOIN {EBMC_LOG} l ON l.new_dtl_rec_id = a.dtl_rec_id
                  AND l.kind = 'transferred'
                GROUP BY 1,2""")
ebmc_dist = cur.fetchall()
for r in ebmc_dist:
    log(f"  ebmc spell/spell_id: {r}")

null_left = one(f"""SELECT COUNT(*) FROM sls.daily_attendance a
                 JOIN {ATT_LOG} l ON l.new_daily_atten_id = a.daily_atten_id
                  AND l.kind = 'transferred' WHERE a.spell_id IS NULL""") + \
            one(f"""SELECT COUNT(*) FROM sls.daily_ebmc_attendance a
                 JOIN {EBMC_LOG} l ON l.new_dtl_rec_id = a.dtl_rec_id
                  AND l.kind = 'transferred' WHERE a.spell_id IS NULL""")
log(f"  transferred rows still NULL spell_id: {null_left}")

mismatch = one(f"""SELECT COUNT(*) FROM sls.daily_attendance a
                 JOIN {ATT_LOG} l ON l.new_daily_atten_id = a.daily_atten_id
                  AND l.kind = 'transferred'
                 JOIN sls.spell_mst sp ON sp.spell_id = a.spell_id
                WHERE sp.spell_code <> a.spell""") + \
           one(f"""SELECT COUNT(*) FROM sls.daily_ebmc_attendance a
                 JOIN {EBMC_LOG} l ON l.new_dtl_rec_id = a.dtl_rec_id
                  AND l.kind = 'transferred'
                 JOIN sls.spell_mst sp ON sp.spell_id = a.spell_id
                WHERE sp.spell_code <> a.spell""")
log(f"  rows where spell_id's code disagrees with spell text: {mismatch}")

ok = null_left == 0 and mismatch == 0

if COMMIT and ok:
    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log
                   (step, status, started_at, finished_at, details)
                   VALUES ('vowsls_att_br87_spell_backfill', 'committed', NOW(), NOW(), %s)""",
                (json.dumps({"mapping": {"A1": 102, "B1": 105, "C": 108},
                             "daily_attendance_updated": att_updated,
                             "daily_ebmc_attendance_updated": ebmc_updated,
                             "att_dist": [list(map(str, r)) for r in att_dist],
                             "ebmc_dist": [list(map(str, r)) for r in ebmc_dist],
                             "row_sets": "kind='transferred' in att/ebmc_transfer_log_br87_aug2026"}),))
    conn.commit()
    log("\nCOMMITTED.")
elif COMMIT:
    conn.rollback()
    log("\nVERIFICATION FAILED - rolled back despite --commit.")
else:
    conn.rollback()
    log("\nDRY RUN - rolled back. Re-run with --commit to apply.")

cur.close()
conn.close()
