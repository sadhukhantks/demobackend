"""
Transfer app-posted daily_attendance from vowsls to sls for branch 87,
dates 2026-08-01 .. 2026-08-04 ONLY (entry moved to sls from 2026-08-05).

Requested 2026-08-25: "transfer the attendance done through app and posted to vowsls
to sls for branch_id 87, 1st-4th August 2026 only".

SCOPE
  vowsls.daily_attendance WHERE company_id = 1 AND attendance_date BETWEEN
  '2026-08-01' AND '2026-08-04'  (1,828 rows / 781 employees at probe time).
  vowsls co 2 (EJM -> sls branch 29) is OUT of scope by user instruction.

MAPPINGS (verified 2026-08-25 against live post-Aug-5 sls br87 rows)
  eb_id   : dbqueries/migrations/hrms_eb_id_co1_branch87_map.csv (4,581) + 5 manual
            additions resolved by emp_code (see EXTRA_EB below; 3 via
            _map_hrms_eb_njm_br87_mirror_2026, 2 later br87 hires 61801/61802).
            NOT _map_pay_eb - that maps to the branch-4 employee clones.
  dept    : sub_dept_mst_sls_co1_branch87_map.csv (109 rows, ids DIFFER old vs new)
  desig   : designation_mst_sls_co1_branch87_map.csv (344 rows, ids DIFFER)
  branch  : constant 87. created_by/updated_by/device_id copied as-is (precedent:
            all prior daily_attendance passes).

TRACEABILITY / IDEMPOTENCY
  Each inserted sls row gets client_uuid = 'vowsls-att-<old daily_atten_id>'.
  The UNIQUE index on client_uuid makes a re-run physically unable to double-insert.

DEDUP
  Source rows whose (mapped eb_id, attendance_date) already exist in sls.daily_attendance
  are skipped (kind=skipped_existing; 1 pair at probe time: eb 58910 on 2026-08-04).
  Anything else INSERT IGNORE swallows is reconciled into kind=skipped_dup.

AUDIT (user requirement: complete before/after log in the transfer DB)
  vowsls_to_sls_transfer.att_transfer_log_br87_aug2026 - one row per:
    pre_existing      : sls br87 row already in the window before this run (full row_json)
    transferred       : source row copied (old/new ids for atten/eb/dept/desig + full
                        source row_json)
    skipped_existing / skipped_unmapped / skipped_dup : source row not copied + reason

ROLLBACK (committed run only):
  DELETE s FROM sls.daily_attendance s
    JOIN vowsls_to_sls_transfer.att_transfer_log_br87_aug2026 l
      ON l.new_daily_atten_id = s.daily_atten_id AND l.kind = 'transferred';
  -- (equivalently: DELETE FROM sls.daily_attendance WHERE client_uuid LIKE 'vowsls-att-%')
  -- then optionally: DROP TABLE vowsls_to_sls_transfer.att_transfer_log_br87_aug2026;

Usage:
  python run_transfer_vowsls_att_br87_aug1_4_to_sls.py            # dry run (rolls back)
  python run_transfer_vowsls_att_br87_aug1_4_to_sls.py --commit   # apply
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
LOG_TABLE = "vowsls_to_sls_transfer.att_transfer_log_br87_aug2026"
UUID_PREFIX = "vowsls-att-"

# emp_code-resolved br87 ebs absent from the CSV (probe 2026-08-25):
#   54508 '568231' -> 61801, 54510 '568222' -> 61802,
#   54519 '665088' -> 59109, 54523 'C910272' -> 59113, 54524 '115157' -> 59114
EXTRA_EB = {54508: 61801, 54510: 61802, 54519: 59109, 54523: 59113, 54524: 59114}

with (HERE / "hrms_eb_id_co1_branch87_map.csv").open() as f:
    EB = {int(r["vowsls_eb_id"]): int(r["sls_eb_id"]) for r in csv.DictReader(f)}
EB.update(EXTRA_EB)
with (HERE / "sub_dept_mst_sls_co1_branch87_map.csv").open() as f:
    DEPT = {int(r["vowsls_dept_id"]): int(r["sls_sub_dept_id"]) for r in csv.DictReader(f)}
with (HERE / "designation_mst_sls_co1_branch87_map.csv").open() as f:
    DESIG = {int(r["vowsls_designation_id"]): int(r["sls_designation_id"]) for r in csv.DictReader(f)}

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       charset="utf8mb4", autocommit=False)
cur = conn.cursor()
# app writes to sls.daily_attendance concurrently - avoid REPEATABLE READ's
# shared locks on the rows our INSERT..SELECTs read
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
      attendance_date DATE,
      old_daily_atten_id BIGINT,
      new_daily_atten_id BIGINT,
      old_eb_id BIGINT, new_eb_id BIGINT,
      old_dept_id BIGINT, new_dept_id BIGINT,
      old_desig_id BIGINT, new_desig_id BIGINT,
      spell VARCHAR(255), working_hours DECIMAL(10,2),
      attendance_mark VARCHAR(255), attendance_source VARCHAR(255), attendance_type VARCHAR(255),
      note VARCHAR(255),
      row_json JSON,
      logged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      KEY idx_kind (kind), KEY idx_new_id (new_daily_atten_id)
    )""")

if one(f"SELECT COUNT(*) FROM {LOG_TABLE} WHERE kind = 'transferred'"):
    sys.exit(f"ABORT: {LOG_TABLE} already holds transferred rows - this ran before. "
             "Roll back or drop the log table first.")
if one("SELECT COUNT(*) FROM sls.daily_attendance WHERE client_uuid LIKE %s", (UUID_PREFIX + "%",)):
    sys.exit("ABORT: sls.daily_attendance already holds rows with the vowsls-att- "
             "client_uuid marker - this ran before.")

# ------------------------------------------------------------ temp map tables
cur.execute("""CREATE TEMPORARY TABLE vowsls_to_sls_transfer._tmp_eb_map
               (old_id BIGINT PRIMARY KEY, new_id BIGINT NOT NULL)""")
cur.executemany("INSERT INTO vowsls_to_sls_transfer._tmp_eb_map VALUES (%s,%s)",
                list(EB.items()))
cur.execute("""CREATE TEMPORARY TABLE vowsls_to_sls_transfer._tmp_dept_map
               (old_id BIGINT PRIMARY KEY, new_id BIGINT NOT NULL)""")
cur.executemany("INSERT INTO vowsls_to_sls_transfer._tmp_dept_map VALUES (%s,%s)",
                list(DEPT.items()))
cur.execute("""CREATE TEMPORARY TABLE vowsls_to_sls_transfer._tmp_desig_map
               (old_id BIGINT PRIMARY KEY, new_id BIGINT NOT NULL)""")
cur.executemany("INSERT INTO vowsls_to_sls_transfer._tmp_desig_map VALUES (%s,%s)",
                list(DESIG.items()))
log(f"maps loaded: eb {len(EB)}, dept {len(DEPT)}, desig {len(DESIG)}")

SRC_JSON = """JSON_OBJECT(
      'daily_atten_id', a.daily_atten_id, 'attendance_date', a.attendance_date,
      'attendance_mark', a.attendance_mark, 'attendance_source', a.attendance_source,
      'attendance_type', a.attendance_type, 'company_id', a.company_id,
      'created_by', a.created_by, 'created_date_time', a.created_date_time,
      'device_id', a.device_id, 'eb_code', a.eb_code, 'eb_id', a.eb_id, 'eb_no', a.eb_no,
      'entry_time', a.entry_time, 'exit_time', a.exit_time, 'idle_hours', a.idle_hours,
      'is_active', a.is_active, 'remarks', a.remarks, 'spell', a.spell,
      'spell_hours', a.spell_hours, 'status_id', a.status_id,
      'update_date_time', a.update_date_time, 'updated_by', a.updated_by,
      'worked_department_id', a.worked_department_id,
      'worked_designation_id', a.worked_designation_id, 'working_hours', a.working_hours)"""

SRC_SCOPE = """FROM vowsls.daily_attendance a
               WHERE a.company_id = 1 AND a.attendance_date BETWEEN %s AND %s"""

src_total = one(f"SELECT COUNT(*) {SRC_SCOPE}", (D_FROM, D_TO))
log(f"source vowsls co-1 rows in window: {src_total}")

# ---------------------------------------------------------------- pre-image
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendance_date, old_daily_atten_id, new_daily_atten_id, old_eb_id, new_eb_id,
       old_dept_id, new_dept_id, old_desig_id, new_desig_id, spell, working_hours,
       attendance_mark, attendance_source, attendance_type, note, row_json)
    SELECT 'pre_existing', s.attendance_date, NULL, s.daily_atten_id, NULL, s.eb_id,
           NULL, s.worked_department_id, NULL, s.worked_designation_id, s.spell,
           s.working_hours, s.attendance_mark, s.attendance_source, s.attendance_type,
           'sls br87 row present before transfer',
           JSON_OBJECT('daily_atten_id', s.daily_atten_id, 'attendance_date', s.attendance_date,
                       'branch_id', s.branch_id, 'eb_id', s.eb_id, 'eb_no', s.eb_no,
                       'spell', s.spell, 'spell_id', s.spell_id,
                       'attendance_mark', s.attendance_mark,
                       'attendance_source', s.attendance_source,
                       'attendance_type', s.attendance_type,
                       'worked_department_id', s.worked_department_id,
                       'worked_designation_id', s.worked_designation_id,
                       'working_hours', s.working_hours, 'created_by', s.created_by,
                       'created_date_time', s.created_date_time,
                       'entry_time', s.entry_time, 'exit_time', s.exit_time,
                       'is_active', s.is_active, 'remarks', s.remarks,
                       'status_id', s.status_id, 'client_uuid', s.client_uuid)
      FROM sls.daily_attendance s
     WHERE s.branch_id = %s AND s.attendance_date BETWEEN %s AND %s""",
    (BRANCH, D_FROM, D_TO))
log(f"pre-existing sls br87 rows logged: {cur.rowcount}")
pre_count = cur.rowcount

# ------------------------------------------------------ skipped: unmapped eb
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendance_date, old_daily_atten_id, old_eb_id, old_dept_id, old_desig_id,
       spell, working_hours, attendance_mark, attendance_source, attendance_type,
       note, row_json)
    SELECT 'skipped_unmapped', a.attendance_date, a.daily_atten_id, a.eb_id,
           a.worked_department_id, a.worked_designation_id, a.spell, a.working_hours,
           a.attendance_mark, a.attendance_source, a.attendance_type,
           'no br87 eb mapping', {SRC_JSON}
      {SRC_SCOPE}
       AND a.eb_id NOT IN (SELECT old_id FROM vowsls_to_sls_transfer._tmp_eb_map)""",
    (D_FROM, D_TO))
log(f"skipped_unmapped logged: {cur.rowcount}")

# ------------------------------------------- skipped: employee+date already in sls
PAIR_EXISTS = """EXISTS (SELECT 1 FROM sls.daily_attendance s
                          WHERE s.eb_id = m.new_id AND s.attendance_date = a.attendance_date)"""
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendance_date, old_daily_atten_id, old_eb_id, new_eb_id, old_dept_id,
       old_desig_id, spell, working_hours, attendance_mark, attendance_source,
       attendance_type, note, row_json)
    SELECT 'skipped_existing', a.attendance_date, a.daily_atten_id, a.eb_id, m.new_id,
           a.worked_department_id, a.worked_designation_id, a.spell, a.working_hours,
           a.attendance_mark, a.attendance_source, a.attendance_type,
           'sls already has a row for this employee+date', {SRC_JSON}
      FROM vowsls.daily_attendance a
      JOIN vowsls_to_sls_transfer._tmp_eb_map m ON m.old_id = a.eb_id
     WHERE a.company_id = 1 AND a.attendance_date BETWEEN %s AND %s
       AND {PAIR_EXISTS}""",
    (D_FROM, D_TO))
log(f"skipped_existing logged: {cur.rowcount}")

# ---------------------------------------------------- guard: unmapped dept/desig
bad = one(f"""SELECT COUNT(*)
      {SRC_SCOPE}
       AND a.eb_id IN (SELECT old_id FROM vowsls_to_sls_transfer._tmp_eb_map)
       AND ((a.worked_department_id IS NOT NULL AND a.worked_department_id NOT IN
               (SELECT old_id FROM vowsls_to_sls_transfer._tmp_dept_map))
         OR (a.worked_designation_id IS NOT NULL AND a.worked_designation_id NOT IN
               (SELECT old_id FROM vowsls_to_sls_transfer._tmp_desig_map)))""",
    (D_FROM, D_TO))
if bad:
    conn.rollback()
    sys.exit(f"ABORT: {bad} source rows have dept/desig ids missing from the br87 map "
             "CSVs - maps were probed 100%, investigate before transferring.")

# ------------------------------------------------------------------- transfer
cur.execute(f"""
    INSERT IGNORE INTO sls.daily_attendance
           (attendance_date, attendance_mark, attendance_source, attendance_type, branch_id,
            created_by, created_date_time, device_id, eb_code, eb_id, eb_no, entry_time,
            exit_time, idle_hours, is_active, remarks, spell, spell_hours, status_id,
            update_date_time, updated_by, worked_department_id, worked_designation_id,
            working_hours, client_uuid)
    SELECT a.attendance_date, a.attendance_mark, a.attendance_source, a.attendance_type,
           %s, a.created_by, a.created_date_time, a.device_id, a.eb_code, m.new_id,
           a.eb_no, a.entry_time, a.exit_time, a.idle_hours, a.is_active, a.remarks,
           a.spell, a.spell_hours, a.status_id, a.update_date_time, a.updated_by,
           dm.new_id, gm.new_id, a.working_hours, CONCAT(%s, a.daily_atten_id)
      FROM vowsls.daily_attendance a
      JOIN vowsls_to_sls_transfer._tmp_eb_map m ON m.old_id = a.eb_id
      LEFT JOIN vowsls_to_sls_transfer._tmp_dept_map dm ON dm.old_id = a.worked_department_id
      LEFT JOIN vowsls_to_sls_transfer._tmp_desig_map gm ON gm.old_id = a.worked_designation_id
     WHERE a.company_id = 1 AND a.attendance_date BETWEEN %s AND %s
       AND NOT {PAIR_EXISTS}""",
    (BRANCH, UUID_PREFIX, D_FROM, D_TO))
inserted = cur.rowcount
log(f"rows inserted into sls.daily_attendance: {inserted}")

# ------------------------------------------------------- audit: transferred
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendance_date, old_daily_atten_id, new_daily_atten_id, old_eb_id, new_eb_id,
       old_dept_id, new_dept_id, old_desig_id, new_desig_id, spell, working_hours,
       attendance_mark, attendance_source, attendance_type, note, row_json)
    SELECT 'transferred', a.attendance_date, a.daily_atten_id, s.daily_atten_id,
           a.eb_id, s.eb_id, a.worked_department_id, s.worked_department_id,
           a.worked_designation_id, s.worked_designation_id, a.spell, a.working_hours,
           a.attendance_mark, a.attendance_source, a.attendance_type, NULL, {SRC_JSON}
      FROM vowsls.daily_attendance a
      JOIN sls.daily_attendance s ON s.client_uuid = CONCAT(%s, a.daily_atten_id)
     WHERE a.company_id = 1 AND a.attendance_date BETWEEN %s AND %s""",
    (UUID_PREFIX, D_FROM, D_TO))
log(f"transferred logged: {cur.rowcount}")

# ------------------------------- audit: anything INSERT IGNORE silently dropped
cur.execute(f"""
    INSERT INTO {LOG_TABLE}
      (kind, attendance_date, old_daily_atten_id, old_eb_id, new_eb_id, old_dept_id,
       old_desig_id, spell, working_hours, attendance_mark, attendance_source,
       attendance_type, note, row_json)
    SELECT 'skipped_dup', a.attendance_date, a.daily_atten_id, a.eb_id, m.new_id,
           a.worked_department_id, a.worked_designation_id, a.spell, a.working_hours,
           a.attendance_mark, a.attendance_source, a.attendance_type,
           'INSERT IGNORE dropped it (unique-key duplicate)', {SRC_JSON}
      FROM vowsls.daily_attendance a
      JOIN vowsls_to_sls_transfer._tmp_eb_map m ON m.old_id = a.eb_id
     WHERE a.company_id = 1 AND a.attendance_date BETWEEN %s AND %s
       AND NOT EXISTS (SELECT 1 FROM {LOG_TABLE} l
                        WHERE l.old_daily_atten_id = a.daily_atten_id
                          AND l.kind IN ('transferred', 'skipped_existing'))""",
    (D_FROM, D_TO))
log(f"skipped_dup logged: {cur.rowcount}")

# ------------------------------------------------------------- verification
log("\n--- verification ---")
cur.execute(f"""SELECT attendance_date, COUNT(*) FROM sls.daily_attendance
                WHERE branch_id = %s AND attendance_date BETWEEN %s AND %s
                GROUP BY 1 ORDER BY 1""", (BRANCH, D_FROM, D_TO))
for d, c in cur.fetchall():
    log(f"  sls br87 {d}: {c} rows")

orphans = one(f"""SELECT COUNT(*) FROM sls.daily_attendance a
                JOIN {LOG_TABLE} l ON l.new_daily_atten_id = a.daily_atten_id
                 AND l.kind = 'transferred'
                LEFT JOIN sls.sub_dept_mst s ON s.sub_dept_id = a.worked_department_id
                LEFT JOIN sls.designation_mst d ON d.designation_id = a.worked_designation_id
                WHERE (a.worked_department_id IS NOT NULL AND s.sub_dept_id IS NULL)
                   OR (a.worked_designation_id IS NOT NULL AND d.designation_id IS NULL)""")
log(f"  dept/desig orphans among transferred rows: {orphans}")

eb_orphans = one(f"""SELECT COUNT(*) FROM sls.daily_attendance a
                JOIN {LOG_TABLE} l ON l.new_daily_atten_id = a.daily_atten_id
                 AND l.kind = 'transferred'
                LEFT JOIN sls.hrms_ed_personal_details p ON p.eb_id = a.eb_id
                WHERE p.eb_id IS NULL OR p.branch_id <> %s""", (BRANCH,))
log(f"  transferred rows whose eb is not a br87 employee: {eb_orphans}")

cur.execute(f"SELECT kind, COUNT(*) FROM {LOG_TABLE} GROUP BY kind")
counts = dict(cur.fetchall())
for k, c in sorted(counts.items()):
    log(f"  audit {k}: {c}")

accounted = sum(c for k, c in counts.items() if k != "pre_existing")
ok = orphans == 0 and eb_orphans == 0 and accounted == src_total
log(f"  source rows {src_total} vs audit-accounted {accounted} -> {'OK' if accounted == src_total else 'MISMATCH'}")

if COMMIT and ok:
    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log
                   (step, status, started_at, finished_at, details)
                   VALUES ('vowsls_att_br87_aug1_4', 'committed', NOW(), NOW(), %s)""",
                (json.dumps({"counts": counts, "source_rows": src_total,
                             "inserted": inserted, "window": [D_FROM, D_TO],
                             "branch": BRANCH, "uuid_prefix": UUID_PREFIX,
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
