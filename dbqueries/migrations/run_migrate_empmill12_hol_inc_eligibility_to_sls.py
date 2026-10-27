"""
Migrate holiday / attendance-incentive eligibility from legacy
`EMPMILL12.tbl_holiday_att_inc_eligibility` into `sls.tbl_holiday_att_inc_eligibility`.

SCOPE
-----
Only `is_active = 1` rows (2,906 of 40,692). The other 37,786 are superseded
versions of the same employees — the legacy screen, the payroll queries and the
new portal page all read `is_active = 1` only, so history adds nothing.

EB MAPPING
----------
Legacy eb_ids are vowsls eb_ids (EMPMILL12 holds the transactional tables, the
employee master lives in vowsls). They are translated through `vowsls_to_sls_transfer._map_pay_eb`,
the authoritative legacy->sls employee map — 0 of the 2,651 employees are
unmapped and all 2,651 exist in `sls.hrms_ed_personal_details`
(branch 29/co 2 = 1,971, branch 87/co 106 = 425, branch 4/co 1 = 255).

DEDUPE
------
The map is many-to-one (people who exist twice in vowsls exist once in sls), and
255 legacy employees also carry two active rows of their own (a status=1 fy NULL
row plus a status=3 fy 2024 one). Both cases collapse to the newest row —
`MAX(hl_att_inc_id)` per mapped eb. Checked before writing: 0 of those groups
disagree on the four eligibility values, so nothing is lost by picking one.

The legacy `hl_att_inc_id` is preserved as the PK (max 81,175, target table was
empty), which is what makes this traceable and exactly reversible. `updated_by`
is dropped — legacy user ids mean nothing in sls.

Re-running is a no-op: INSERT IGNORE on the PK plus a NOT EXISTS guard on any
active row the employee already has.

ROLLBACK:
  DELETE t FROM sls.tbl_holiday_att_inc_eligibility t
    JOIN EMPMILL12.tbl_holiday_att_inc_eligibility e ON e.hl_att_inc_id = t.hl_att_inc_id;

Usage:
  python run_migrate_empmill12_hol_inc_eligibility_to_sls.py            # dry run (counts only)
  python run_migrate_empmill12_hol_inc_eligibility_to_sls.py --commit
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv

REPO = Path(__file__).resolve().parents[2]
ENV = {}
for line in (REPO / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       autocommit=False, connect_timeout=30, read_timeout=3600, write_timeout=3600)
cur = conn.cursor()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(msg, flush=True)


def scalar(sql):
    cur.execute(sql)
    return cur.fetchone()[0]


# One row per mapped sls employee — the newest active legacy row.
KEEP = """
    SELECT m.new_eb_id AS nb, MAX(e.hl_att_inc_id) AS keep_id
      FROM EMPMILL12.tbl_holiday_att_inc_eligibility e
      JOIN vowsls_to_sls_transfer._map_pay_eb m ON m.legacy_eb_id = e.eb_id
     WHERE e.is_active = 1
     GROUP BY m.new_eb_id
"""

source_rows = scalar("""
    SELECT COUNT(*) FROM EMPMILL12.tbl_holiday_att_inc_eligibility WHERE is_active = 1""")
unmapped = scalar("""
    SELECT COUNT(DISTINCT e.eb_id)
      FROM EMPMILL12.tbl_holiday_att_inc_eligibility e
      LEFT JOIN vowsls_to_sls_transfer._map_pay_eb m ON m.legacy_eb_id = e.eb_id
     WHERE e.is_active = 1 AND m.new_eb_id IS NULL""")
conflicts = scalar(f"""
    SELECT COUNT(*) FROM (
        SELECT m.new_eb_id
          FROM EMPMILL12.tbl_holiday_att_inc_eligibility e
          JOIN vowsls_to_sls_transfer._map_pay_eb m ON m.legacy_eb_id = e.eb_id
         WHERE e.is_active = 1
         GROUP BY m.new_eb_id
        HAVING COUNT(DISTINCT CONCAT(e.holiday_eligibility, e.att_incn_eligibility,
                                     e.fn_att_inc_rate, e.mn_att_inc_rate)) > 1) x""")
target_before = scalar("SELECT COUNT(*) FROM sls.tbl_holiday_att_inc_eligibility")
to_insert = scalar(f"""
    SELECT COUNT(*) FROM ({KEEP}) k
      JOIN sls.hrms_ed_personal_details p ON p.eb_id = k.nb
     WHERE NOT EXISTS (SELECT 1 FROM sls.tbl_holiday_att_inc_eligibility t
                        WHERE t.eb_id = k.nb AND t.is_active = 1)""")

log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}")
log(f"  legacy active rows        : {source_rows:,}")
log(f"  unmapped legacy employees : {unmapped}")
log(f"  value-conflicting groups  : {conflicts}")
log(f"  sls rows before           : {target_before:,}")
log(f"  rows to insert            : {to_insert:,}")

if unmapped or conflicts:
    log("  ABORT: mapping is no longer clean — re-check before loading.")
    sys.exit(1)

INSERT = f"""
    INSERT IGNORE INTO sls.tbl_holiday_att_inc_eligibility
           (hl_att_inc_id, eb_id, holiday_eligibility, att_incn_eligibility,
            fn_att_inc_rate, mn_att_inc_rate, is_active, updated_by, status, fy_year)
    SELECT e.hl_att_inc_id, k.nb, e.holiday_eligibility, e.att_incn_eligibility,
           e.fn_att_inc_rate, e.mn_att_inc_rate, 1, NULL, e.status, e.fy_year
      FROM ({KEEP}) k
      JOIN EMPMILL12.tbl_holiday_att_inc_eligibility e ON e.hl_att_inc_id = k.keep_id
      JOIN sls.hrms_ed_personal_details p ON p.eb_id = k.nb
     WHERE NOT EXISTS (SELECT 1 FROM sls.tbl_holiday_att_inc_eligibility t
                        WHERE t.eb_id = k.nb AND t.is_active = 1)"""

if not COMMIT:
    log("  dry run — nothing written. Re-run with --commit.")
    sys.exit(0)

cur.execute("INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at) "
            "VALUES ('empmill12_hol_inc_eligibility', 'running', NOW())")
log_id = cur.lastrowid

cur.execute(INSERT)
inserted = cur.rowcount

stats = {"legacy_active_rows": source_rows, "inserted": inserted,
         "target_rows_before": target_before}
by_branch = {}
cur.execute("""
    SELECT p.branch_id, COUNT(*) FROM sls.tbl_holiday_att_inc_eligibility t
      JOIN sls.hrms_ed_personal_details p ON p.eb_id = t.eb_id
     WHERE t.is_active = 1 GROUP BY p.branch_id""")
for branch_id, cnt in cur.fetchall():
    by_branch[str(branch_id)] = cnt
stats["by_branch"] = by_branch

cur.execute("UPDATE vowsls_to_sls_transfer.migration_log SET status='committed', finished_at=NOW(), details=%s "
            "WHERE id=%s", (json.dumps(stats), log_id))
conn.commit()

log(f"  inserted                  : {inserted:,}")
log(f"  by branch                 : {by_branch}")
log("  committed.")
