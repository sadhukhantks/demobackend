"""
Give every branch-4 emp_code a branch-87 eligibility row in
`sls.tbl_holiday_att_inc_eligibility`.

WHY
---
Branch 4 (FACTORY co 1, NJM) and branch 87 (FACTORY co 106) hold the same
people: all 4,602 active branch-4 emp_codes resolve to exactly one branch-87
eb_id (verified 1:1, no ambiguity). After the EMPMILL12 load only 425 of those
branch-87 employees had an eligibility row, so the page was near-empty for
branch 87.

WHAT IT WRITES
--------------
One row per branch-4 emp_code whose branch-87 twin has no active row yet
(4,602 - 425 = 4,177). Values are carried from the branch-4 employee's own
active row where one exists — in practice that is a no-op, because all 255
employees who have branch-4 values already have an identical branch-87 row.
So the new rows land on the table defaults:

    holiday_eligibility = 'N', att_incn_eligibility = 'N', rates = 0.00

'N' is deliberate: nobody starts drawing holiday pay or an incentive from a
seeded row. Flip them from the portal page (or a bulk UPDATE) once the real
eligibility is known. status = 3, matching what the page writes.

2,071 of the 4,602 twins are inactive employees in branch 87 (personal.active
= 0). They are included — the instruction was all branch-4 emp_codes — and the
list page does not filter on personal.active, so they will appear.

ROLLBACK:
  DELETE FROM sls.tbl_holiday_att_inc_eligibility WHERE hl_att_inc_id > <max_id_before, printed>;

Usage:
  python run_seed_branch87_hol_inc_eligibility_from_branch4.py            # dry run (counts only)
  python run_seed_branch87_hol_inc_eligibility_from_branch4.py --commit
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


# branch-4 emp_code -> its single branch-87 eb, plus the branch-4 eligibility
# values if the employee has a row (MAX over a group that holds at most one).
TWINS = """
    SELECT o4.emp_code,
           MIN(o87.eb_id)              AS b87_eb,
           MAX(s.holiday_eligibility)  AS hol,
           MAX(s.att_incn_eligibility) AS inc,
           MAX(s.fn_att_inc_rate)      AS fn,
           MAX(s.mn_att_inc_rate)      AS mn,
           MAX(s.fy_year)              AS fy
      FROM sls.hrms_ed_official_details o4
      JOIN sls.hrms_ed_official_details o87
             ON o87.branch_id = 87 AND o87.emp_code = o4.emp_code
      LEFT JOIN sls.tbl_holiday_att_inc_eligibility s
             ON s.eb_id = o4.eb_id AND s.is_active = 1
     WHERE o4.branch_id = 4 AND o4.active = 1
       AND o4.emp_code IS NOT NULL AND o4.emp_code <> ''
     GROUP BY o4.emp_code
"""

NEW_ROWS = f"""
    FROM ({TWINS}) tw
   WHERE NOT EXISTS (SELECT 1 FROM sls.tbl_holiday_att_inc_eligibility t
                      WHERE t.eb_id = tw.b87_eb AND t.is_active = 1)
"""

max_before = scalar("SELECT COALESCE(MAX(hl_att_inc_id), 0) FROM sls.tbl_holiday_att_inc_eligibility")
twins = scalar(f"SELECT COUNT(*) FROM ({TWINS}) x")
ambiguous = scalar("""
    SELECT COUNT(*) FROM (
        SELECT o4.emp_code
          FROM sls.hrms_ed_official_details o4
          JOIN sls.hrms_ed_official_details o87
                 ON o87.branch_id = 87 AND o87.emp_code = o4.emp_code
         WHERE o4.branch_id = 4 AND o4.active = 1 AND o4.emp_code IS NOT NULL
         GROUP BY o4.emp_code
        HAVING COUNT(DISTINCT o87.eb_id) > 1) x""")
to_insert = scalar(f"SELECT COUNT(*) {NEW_ROWS}")
carried = scalar(f"SELECT COUNT(*) {NEW_ROWS} AND tw.hol IS NOT NULL")

log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}")
log(f"  branch-4 emp_codes with a branch-87 twin : {twins:,}")
log(f"  codes hitting >1 branch-87 eb            : {ambiguous}")
log(f"  branch-87 twins still without a row      : {to_insert:,}")
log(f"    of which carry branch-4 values         : {carried:,}  (rest seeded N/N/0.00/0.00)")
log(f"  ROLLBACK KEY -> DELETE FROM sls.tbl_holiday_att_inc_eligibility WHERE hl_att_inc_id > {max_before};")

if ambiguous:
    log("  ABORT: an emp_code resolves to more than one branch-87 employee.")
    sys.exit(1)

if not COMMIT:
    log("  dry run — nothing written. Re-run with --commit.")
    sys.exit(0)

cur.execute("INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at) "
            "VALUES ('hol_inc_eligibility_branch4_to_87', 'running', NOW())")
log_id = cur.lastrowid

cur.execute(f"""
    INSERT INTO sls.tbl_holiday_att_inc_eligibility
           (eb_id, holiday_eligibility, att_incn_eligibility,
            fn_att_inc_rate, mn_att_inc_rate, is_active, updated_by, status, fy_year)
    SELECT tw.b87_eb, COALESCE(tw.hol, 'N'), COALESCE(tw.inc, 'N'),
           COALESCE(tw.fn, 0), COALESCE(tw.mn, 0), 1, NULL, 3, tw.fy
    {NEW_ROWS}""")
inserted = cur.rowcount

stats = {"twins": twins, "inserted": inserted, "carried_values": carried,
         "max_id_before": max_before}
cur.execute("UPDATE vowsls_to_sls_transfer.migration_log SET status='committed', finished_at=NOW(), details=%s "
            "WHERE id=%s", (json.dumps(stats), log_id))
conn.commit()

log(f"  inserted                                 : {inserted:,}")
log("  committed.")
