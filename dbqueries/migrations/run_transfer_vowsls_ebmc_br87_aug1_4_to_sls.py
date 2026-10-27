"""
Follow-up to run_transfer_vowsls_att_br87_aug1_4_to_sls.py (applied 2026-08-25):
transfer the machine-number rows (daily_ebmc_attendance) for the same scope -
vowsls co-1 -> sls branch 87, attendance dates 2026-08-01 .. 2026-08-04.

Requested 2026-08-26: "the machine nos were not migrated, also migrate this;
check if the ids match each other".

ID-MATCH FINDING (probed 2026-08-26)
  vowsls mechine_master ids do NOT equal the machine ids sls branch 87 uses.
  The same ids DO exist in sls.machine_mst with identical names, but those rows are
  other branches' copies - sls keeps one machine row per branch, and live br87
  entries (Aug-5 onward) use a separate id set (e.g. FRAME -12: vowsls 716 -> sls 2974).
  Correct bridge = dbqueries/migrations/machine_mst_sls_co1_branch87_map.csv (643 rows),
  the same map the weaving migration used (SPEC_weaving_migration_sls.md line 55).
  Verified: covers all 192 machines in the window, every target name-matches and sits
  in a branch-87 dept, and all 150 machine ids used by live br87 rows are map targets.

SCOPE
  vowsls.daily_ebmc_attendance WHERE company_id = 1 AND attendace_date BETWEEN
  '2026-08-01' AND '2026-08-04'  (1,151 rows / 352 employees / 192 machines at probe).
  All 1,151 parents (daily_atten_id) are in att_transfer_log_br87_aug2026 as
  kind='transferred'; parent supplies the new daily_atten_id AND new eb_id.

MAPPINGS
  daily_atten_id + eb_id : parent join on att_transfer_log_br87_aug2026 (kind=transferred)
  mc_id                  : machine_mst_sls_co1_branch87_map.csv
  designation_id         : designation_mst_sls_co1_branch87_map.csv (13 ids in window, all covered)
  branch_id              : constant 87. spell_id / or_mc_id left NULL (legacy rows carry
                           spell as text, same as the attendance pass). Rest copied as-is.

AUDIT
  vowsls_to_sls_transfer.ebmc_transfer_log_br87_aug2026 - kinds:
    pre_existing / transferred / skipped_no_parent / skipped_existing / skipped_dup.
  New-row identity recovered by joining back on
  (daily_atten_id, mc_id, spell, created_date_time) - probed unique in source.

ROLLBACK (committed run only):
  DELETE s FROM sls.daily_ebmc_attendance s
    JOIN vowsls_to_sls_transfer.ebmc_transfer_log_br87_aug2026 l
      ON l.new_dtl_rec_id = s.dtl_rec_id AND l.kind = 'transferred';

Usage:
  python run_transfer_vowsls_ebmc_br87_aug1_4_to_sls.py            # dry run (rolls back)
  python run_transfer_vowsls_ebmc_br87_aug1_4_to_sls.py --commit   # apply
"""
import csv
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

D_FROM, D_TO = "2026-08-01", "2026-08-04"
BRANCH = 87
LOG_TABLE = "vowsls_to_sls_transfer.ebmc_transfer_log_br87_aug2026"
ATT_LOG = "vowsls_to_sls_transfer.att_transfer_log_br87_aug2026"

with (HERE / "machine_mst_sls_co1_branch87_map.csv").open() as f:
    MC = {int(r["vowsls_mechine_id"]): int(r["sls_machine_id"]) for r in csv.DictReader(f)}
with (HERE / "designation_mst_sls_co1_branch87_map.csv").open() as f:
    DESIG = {int(r["vowsls_designation_id"]): int(r["sls_designation_id"]) for r in csv.DictReader(f)}

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       charset="utf8mb4", autocommit=False)
cur = conn.cursor()
cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")

def log(msg):
    print(msg, flush=True)

def one(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()[0]

cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
      log_id INT AUTO_INCREMENT PRIMARY KEY,
      kind VARCHAR(20) NOT NULL,
      attendace_date DATE,
      old_dtl_rec_id BIGINT,
      new_dtl_rec_id BIGINT,
      old_daily_atten_id BIGINT, new_daily_atten_id BIGINT,
      old_eb_id BIGINT, new_eb_id BIGINT,
      old_mc_id BIGINT, new_mc_id BIGINT,
      old_desig_id BIGINT, new_desig_id BIGINT,
      spell VARCHAR(255), mc_stoppage_hours DECIMAL(10,2),
      note VARCHAR(255),
      row_json JSON,
      logged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      KEY idx_kind (kind), KEY idx_new_id (new_dtl_rec_id)
    )""")

if one(f"SELECT COUNT(*) FROM {LOG_TABLE} WHERE kind = 'transferred'"):
    sys.exit(f"ABORT: {LOG_TABLE} already holds transferred rows - this ran before. "
             "Roll back or drop the log table first.")

# ------------------------------------------------------------ temp map tables
cur.execute("""CREATE TEMPORARY TABLE vowsls_to_sls_transfer._tmp_mc_map
               (old_id BIGINT PRIMARY KEY, new_id BIGINT NOT NULL)""")
cur.executemany("INSERT INTO vowsls_to_sls_transfer._tmp_mc_map VALUES (%s,%s)",
                list(MC.items()))
cur.execute("""CREATE TEMPORARY TABLE vowsls_to_sls_transfer._tmp_desig_map
               (old_id BIGINT PRIMARY KEY, new_id BIGINT NOT NULL)""")
cur.executemany("INSERT INTO vowsls_to_sls_transfer._tmp_desig_map VALUES (%s,%s)",
                list(DESIG.items()))
log(f"maps loaded: mc {len(MC)}, desig {len(DESIG)}")

SRC_JSON = """JSON_OBJECT(
      'dtl_rec_id', e.dtl_rec_id, 'attendace_date', e.attendace_date,
      'company_id', e.company_id, 'created_by', e.created_by,
      'daily_atten_id', e.daily_atten_id, 'eb_code', e.eb_code, 'eb_id', e.eb_id,
      'eb_no', e.eb_no, 'is_active', e.is_active, 'mc_id', e.mc_id, 'spell', e.spell,
      'updated_date_time', e.updated_date_time, 'updated_by', e.updated_by,
      'designation_id', e.designation_id, 'created_date_time', e.created_date_time,
      'or_mc_id', e.or_mc_id, 'mc_stoppage_hours', e.mc_stoppage_hours)"""

SRC_WHERE = "e.company_id = 1 AND e.attendace_date BETWEEN %s AND %s"

src_total = one(f"SELECT COUNT(*) FROM vowsls.daily_ebmc_attendance e WHERE {SRC_WHERE}",
                (D_FROM, D_TO))
log(f"source vowsls co-1 ebmc rows in window: {src_total}")

# ---------------------------------------------------------------- pre-image
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendace_date, new_dtl_rec_id, new_daily_atten_id, new_eb_id, new_mc_id,
       new_desig_id, spell, mc_stoppage_hours, note, row_json)
    SELECT 'pre_existing', s.attendace_date, s.dtl_rec_id, s.daily_atten_id, s.eb_id,
           s.mc_id, s.designation_id, s.spell, s.mc_stoppage_hours,
           'sls br87 ebmc row present before transfer',
           JSON_OBJECT('dtl_rec_id', s.dtl_rec_id, 'attendace_date', s.attendace_date,
                       'branch_id', s.branch_id, 'daily_atten_id', s.daily_atten_id,
                       'eb_id', s.eb_id, 'eb_no', s.eb_no, 'mc_id', s.mc_id,
                       'spell', s.spell, 'spell_id', s.spell_id,
                       'designation_id', s.designation_id, 'is_active', s.is_active,
                       'created_by', s.created_by, 'created_date_time', s.created_date_time,
                       'mc_stoppage_hours', s.mc_stoppage_hours)
      FROM sls.daily_ebmc_attendance s
     WHERE s.branch_id = %s AND s.attendace_date BETWEEN %s AND %s""",
    (BRANCH, D_FROM, D_TO))
log(f"pre-existing sls br87 ebmc rows logged: {cur.rowcount}")

# --------------------------------------------- skipped: parent not transferred
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendace_date, old_dtl_rec_id, old_daily_atten_id, old_eb_id, old_mc_id,
       old_desig_id, spell, mc_stoppage_hours, note, row_json)
    SELECT 'skipped_no_parent', e.attendace_date, e.dtl_rec_id, e.daily_atten_id,
           e.eb_id, e.mc_id, e.designation_id, e.spell, e.mc_stoppage_hours,
           'parent attendance row was not transferred', {SRC_JSON}
      FROM vowsls.daily_ebmc_attendance e
     WHERE {SRC_WHERE}
       AND NOT EXISTS (SELECT 1 FROM {ATT_LOG} l
                        WHERE l.old_daily_atten_id = e.daily_atten_id
                          AND l.kind = 'transferred')""",
    (D_FROM, D_TO))
log(f"skipped_no_parent logged: {cur.rowcount}")

# -------------------------------------------- guard: unmapped machine / desig
bad = one(f"""SELECT COUNT(*) FROM vowsls.daily_ebmc_attendance e
     WHERE {SRC_WHERE}
       AND ((e.mc_id IS NOT NULL AND e.mc_id NOT IN
               (SELECT old_id FROM vowsls_to_sls_transfer._tmp_mc_map))
         OR (e.designation_id IS NOT NULL AND e.designation_id NOT IN
               (SELECT old_id FROM vowsls_to_sls_transfer._tmp_desig_map)))""",
    (D_FROM, D_TO))
if bad:
    conn.rollback()
    sys.exit(f"ABORT: {bad} source rows have machine/designation ids missing from the "
             "br87 map CSVs - maps were probed 100%, investigate before transferring.")

# ------------------------------- skipped: equivalent row already in sls (any)
DUP_EXISTS = """EXISTS (SELECT 1 FROM sls.daily_ebmc_attendance s
                         WHERE s.daily_atten_id = l.new_daily_atten_id
                           AND s.mc_id = mm.new_id AND (s.spell <=> e.spell))"""
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendace_date, old_dtl_rec_id, old_daily_atten_id, new_daily_atten_id,
       old_eb_id, new_eb_id, old_mc_id, new_mc_id, old_desig_id, spell,
       mc_stoppage_hours, note, row_json)
    SELECT 'skipped_existing', e.attendace_date, e.dtl_rec_id, e.daily_atten_id,
           l.new_daily_atten_id, e.eb_id, l.new_eb_id, e.mc_id, mm.new_id,
           e.designation_id, e.spell, e.mc_stoppage_hours,
           'sls already has this parent+machine+spell row', {SRC_JSON}
      FROM vowsls.daily_ebmc_attendance e
      JOIN {ATT_LOG} l ON l.old_daily_atten_id = e.daily_atten_id AND l.kind = 'transferred'
      LEFT JOIN vowsls_to_sls_transfer._tmp_mc_map mm ON mm.old_id = e.mc_id
     WHERE {SRC_WHERE}
       AND {DUP_EXISTS}""",
    (D_FROM, D_TO))
log(f"skipped_existing logged: {cur.rowcount}")

# ------------------------------------------------------------------- transfer
cur.execute(f"""
    INSERT IGNORE INTO sls.daily_ebmc_attendance
           (attendace_date, branch_id, created_by, daily_atten_id, eb_code, eb_id, eb_no,
            is_active, mc_id, spell, spell_id, updated_date_time, updated_by,
            designation_id, created_date_time, or_mc_id, mc_stoppage_hours)
    SELECT e.attendace_date, %s, e.created_by, l.new_daily_atten_id, e.eb_code,
           l.new_eb_id, e.eb_no, e.is_active, mm.new_id, e.spell, NULL,
           e.updated_date_time, e.updated_by, dg.new_id, e.created_date_time,
           e.or_mc_id, e.mc_stoppage_hours
      FROM vowsls.daily_ebmc_attendance e
      JOIN {ATT_LOG} l ON l.old_daily_atten_id = e.daily_atten_id AND l.kind = 'transferred'
      LEFT JOIN vowsls_to_sls_transfer._tmp_mc_map mm ON mm.old_id = e.mc_id
      LEFT JOIN vowsls_to_sls_transfer._tmp_desig_map dg ON dg.old_id = e.designation_id
     WHERE {SRC_WHERE}
       AND NOT {DUP_EXISTS}""",
    (BRANCH, D_FROM, D_TO))
inserted = cur.rowcount
log(f"rows inserted into sls.daily_ebmc_attendance: {inserted}")

# ------------------------------------------------------- audit: transferred
# join-back key (daily_atten_id, mc_id, spell, created_date_time) probed unique in source
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendace_date, old_dtl_rec_id, new_dtl_rec_id, old_daily_atten_id,
       new_daily_atten_id, old_eb_id, new_eb_id, old_mc_id, new_mc_id, old_desig_id,
       new_desig_id, spell, mc_stoppage_hours, note, row_json)
    SELECT 'transferred', e.attendace_date, e.dtl_rec_id, s.dtl_rec_id,
           e.daily_atten_id, s.daily_atten_id, e.eb_id, s.eb_id, e.mc_id, s.mc_id,
           e.designation_id, s.designation_id, e.spell, e.mc_stoppage_hours, NULL,
           {SRC_JSON}
      FROM vowsls.daily_ebmc_attendance e
      JOIN {ATT_LOG} l ON l.old_daily_atten_id = e.daily_atten_id AND l.kind = 'transferred'
      LEFT JOIN vowsls_to_sls_transfer._tmp_mc_map mm ON mm.old_id = e.mc_id
      JOIN sls.daily_ebmc_attendance s
        ON s.daily_atten_id = l.new_daily_atten_id AND (s.mc_id <=> mm.new_id)
       AND (s.spell <=> e.spell) AND s.created_date_time = e.created_date_time
       AND s.branch_id = %s
     WHERE {SRC_WHERE}
       AND NOT EXISTS (SELECT 1 FROM {LOG_TABLE} x
                        WHERE x.old_dtl_rec_id = e.dtl_rec_id
                          AND x.kind = 'skipped_existing')""",
    (BRANCH, D_FROM, D_TO))
log(f"transferred logged: {cur.rowcount}")

# ------------------------------- audit: anything INSERT IGNORE silently dropped
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendace_date, old_dtl_rec_id, old_daily_atten_id, old_eb_id, old_mc_id,
       old_desig_id, spell, mc_stoppage_hours, note, row_json)
    SELECT 'skipped_dup', e.attendace_date, e.dtl_rec_id, e.daily_atten_id, e.eb_id,
           e.mc_id, e.designation_id, e.spell, e.mc_stoppage_hours,
           'INSERT IGNORE dropped it (unique-key duplicate)', {SRC_JSON}
      FROM vowsls.daily_ebmc_attendance e
     WHERE {SRC_WHERE}
       AND NOT EXISTS (SELECT 1 FROM {LOG_TABLE} x
                        WHERE x.old_dtl_rec_id = e.dtl_rec_id
                          AND x.kind IN ('transferred','skipped_existing','skipped_no_parent'))""",
    (D_FROM, D_TO))
log(f"skipped_dup logged: {cur.rowcount}")

# ------------------------------------------------------------- verification
log("\n--- verification ---")
cur.execute(f"""SELECT attendace_date, COUNT(*) FROM sls.daily_ebmc_attendance
                WHERE branch_id = %s AND attendace_date BETWEEN %s AND %s
                GROUP BY 1 ORDER BY 1""", (BRANCH, D_FROM, D_TO))
for d, cnt in cur.fetchall():
    log(f"  sls br87 ebmc {d}: {cnt} rows")

mc_orphans = one(f"""SELECT COUNT(*) FROM sls.daily_ebmc_attendance a
                JOIN {LOG_TABLE} l ON l.new_dtl_rec_id = a.dtl_rec_id AND l.kind = 'transferred'
                LEFT JOIN sls.machine_mst m ON m.machine_id = a.mc_id
                LEFT JOIN sls.dept_mst d ON d.dept_id = m.dept_id
                WHERE a.mc_id IS NOT NULL AND (m.machine_id IS NULL OR d.branch_id <> %s)""",
                 (BRANCH,))
log(f"  transferred rows whose machine is not a br87 machine: {mc_orphans}")

parent_mismatch = one(f"""SELECT COUNT(*) FROM sls.daily_ebmc_attendance a
                JOIN {LOG_TABLE} l ON l.new_dtl_rec_id = a.dtl_rec_id AND l.kind = 'transferred'
                JOIN sls.daily_attendance p ON p.daily_atten_id = a.daily_atten_id
                WHERE p.eb_id <> a.eb_id OR p.attendance_date <> a.attendace_date
                   OR p.branch_id <> %s""", (BRANCH,))
log(f"  transferred rows disagreeing with their sls parent attendance row: {parent_mismatch}")

cur.execute(f"SELECT kind, COUNT(*) FROM {LOG_TABLE} GROUP BY kind")
counts = dict(cur.fetchall())
for k, cnt in sorted(counts.items()):
    log(f"  audit {k}: {cnt}")

accounted = sum(cnt for k, cnt in counts.items() if k != "pre_existing")
ok = mc_orphans == 0 and parent_mismatch == 0 and accounted == src_total \
     and counts.get("transferred", 0) == inserted
log(f"  source rows {src_total} vs audit-accounted {accounted}; inserted {inserted} vs "
    f"transferred-logged {counts.get('transferred', 0)} -> {'OK' if ok else 'MISMATCH'}")

if COMMIT and ok:
    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log
                   (step, status, started_at, finished_at, details)
                   VALUES ('vowsls_ebmc_br87_aug1_4', 'committed', NOW(), NOW(), %s)""",
                (json.dumps({"counts": counts, "source_rows": src_total,
                             "inserted": inserted, "window": [D_FROM, D_TO],
                             "branch": BRANCH,
                             "log_table": LOG_TABLE.split(".")[1]}),))
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
