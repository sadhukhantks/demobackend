"""
Give branch 87 (FACTORY, co 106) its own copies of the pay schemes branch 4 (FACTORY, co 1) runs,
and re-point every branch-87 payroll artefact at them.

WHY THE WHOLE CHAIN MOVES TOGETHER
----------------------------------
`pay_scheme_master.branch_id` is NULL on all 145 existing rows, so a scheme is tied to a branch only
indirectly — through the employees assigned to it and the pay periods that reference it. The Pay
Scheme page filters by `co_id`, so selecting co 106 currently shows 3 schemes while branch-87
employees are actually assigned co-1 (NJM) schemes. This script creates the co-106 / branch-87
copies and moves branch 87 onto them.

The backend couples PAYSCHEME_ID across four tables, so re-pointing only some would break payroll:

  * src/hrms/query.py:1119  `pes.PAYSCHEME_ID = eps.PAY_SCHEME_ID`
        -> pay_employee_structure must agree with pay_employee_payscheme, or an employee loses
           their salary structure and every component falls back to the scheme default.
  * src/hrms/query.py:895   `DELETE FROM pay_employee_payroll WHERE PAYPERIOD_ID = :p AND PAYSCHEME_ID = :s`
        -> payroll is keyed by (period, scheme) as a pair; a mismatch makes regeneration miss the
           old rows and silently double the payroll.
  * src/hrms/query.py:622   payroll joined to pay_employee_payperiod on period AND scheme.

Hence all six phases are one unit. Running only `scheme` leaves the data consistent (nothing points
at the new schemes yet); stopping anywhere between `map` and `payroll` does NOT.

SCOPE (measured)
----------------
  source schemes ....... 17   (co_id = 1, referenced by a branch-4 period or a branch-4 assignment)
  pay_scheme_master .... 17 new rows, co_id 106 / branch_id 87, payscheme_id = source + 2000
  pay_scheme_details . 1304 new rows, ID = source + 500000
  re-pointed to the new schemes, for employees whose official row is branch 87:
      pay_employee_payscheme (current) ....     244
      pay_period (branch 87) .............    1,135
      pay_employee_structure .............  271,471
      pay_employee_payroll ...............  172,868

Branch-4 employees are untouched: no eb_id belongs to both branches (verified), so the two
populations are disjoint. The 1,810 clones from `run_sync_co1_employees_branch4_branch87.py` carry
no payroll and are unaffected.

NOT DONE: 2,678 active branch-87 employees have no current pay scheme at all. There is no source to
derive one from — they need assigning in the UI (or a rule from the user).

ROLLBACK:
  UPDATE sls.pay_employee_payroll   r JOIN vowsls_to_sls_transfer._map_pay_scheme_b87 m ON m.new_payscheme_id = r.PAYSCHEME_ID   SET r.PAYSCHEME_ID  = m.source_payscheme_id;
  UPDATE sls.pay_employee_structure s JOIN vowsls_to_sls_transfer._map_pay_scheme_b87 m ON m.new_payscheme_id = s.PAYSCHEME_ID   SET s.PAYSCHEME_ID  = m.source_payscheme_id;
  UPDATE sls.pay_period             p JOIN vowsls_to_sls_transfer._map_pay_scheme_b87 m ON m.new_payscheme_id = p.PAYSCHEME_ID   SET p.PAYSCHEME_ID  = m.source_payscheme_id;
  UPDATE sls.pay_employee_payscheme e JOIN vowsls_to_sls_transfer._map_pay_scheme_b87 m ON m.new_payscheme_id = e.PAY_SCHEME_ID  SET e.PAY_SCHEME_ID = m.source_payscheme_id;
  DELETE FROM sls.pay_scheme_details WHERE ID > 500000;
  DELETE FROM sls.pay_scheme_master  WHERE payscheme_id > 2000;
  DROP TABLE vowsls_to_sls_transfer._map_pay_scheme_b87;

Usage:
  python run_mirror_pay_schemes_branch4_to_branch87.py            # dry run (counts only)
  python run_mirror_pay_schemes_branch4_to_branch87.py --commit
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv

SRC_BRANCH, SRC_CO = 4, 1
DST_BRANCH, DST_CO = 87, 106
SCHEME_OFF = 2_000        # sls MAX(payscheme_id) = 1,174
DETAIL_OFF = 500_000      # sls MAX(pay_scheme_details.ID) = 110,765
CHUNK = 500_000
MAP = "vowsls_to_sls_transfer._map_pay_scheme_b87"

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
STATS: dict[str, object] = {}

# schemes that branch 4 actually runs
SRC_SCHEMES = f"""
    SELECT m.payscheme_id FROM sls.pay_scheme_master m
     WHERE m.co_id = {SRC_CO} AND (
           m.payscheme_id IN (SELECT DISTINCT PAYSCHEME_ID FROM sls.pay_period
                               WHERE branch_id = {SRC_BRANCH})
        OR m.payscheme_id IN (SELECT DISTINCT e.PAY_SCHEME_ID FROM sls.pay_employee_payscheme e
                                JOIN sls.hrms_ed_official_details o
                                  ON o.eb_id = e.EMPLOYEEID AND o.branch_id = {SRC_BRANCH}
                               WHERE e.STATUS = 1))
"""
# branch-87 employees. No eb_id spans both branches, so a plain DISTINCT is unambiguous.
B87_EB = f"SELECT DISTINCT eb_id FROM sls.hrms_ed_official_details WHERE branch_id = {DST_BRANCH}"


def log(msg):
    print(msg, flush=True)


def scalar(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()[0]


def step(label, sql, count_sql):
    n = scalar(count_sql)
    if not COMMIT:
        log(f"  [dry] {label}: {n:,}")
        STATS[label] = n
        return
    cur.execute(sql)
    conn.commit()
    log(f"  {label}: {cur.rowcount:,} (expected {n:,})")
    STATS[label] = cur.rowcount


def build_map():
    log("-- scheme map --")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {MAP} (
            source_payscheme_id bigint      NOT NULL PRIMARY KEY,
            new_payscheme_id    bigint      NOT NULL,
            payscheme_code      varchar(255) NULL,
            KEY idx_b87_new (new_payscheme_id)
        ) ENGINE=InnoDB""")
    conn.commit()
    if scalar(f"SELECT COUNT(*) FROM {MAP}"):
        log("  already built — reusing")
    else:
        cur.execute(f"""
            INSERT INTO {MAP} (source_payscheme_id, new_payscheme_id, payscheme_code)
            SELECT m.payscheme_id, m.payscheme_id + {SCHEME_OFF}, m.payscheme_code
              FROM sls.pay_scheme_master m
             WHERE m.payscheme_id IN ({SRC_SCHEMES})""")
        conn.commit()
        log(f"  mapped {cur.rowcount} schemes")
    log(f"  schemes in map: {scalar(f'SELECT COUNT(*) FROM {MAP}')}")


def run():
    log("\n-- 1/2. create the branch-87 schemes --")
    step("pay_scheme_master", f"""
        INSERT INTO sls.pay_scheme_master
               (payscheme_id, co_id, effective_from, payscheme_code, payscheme_name,
                record_status, updated_by, wage_type, branch_id)
        SELECT k.new_payscheme_id, {DST_CO}, m.effective_from, m.payscheme_code, m.payscheme_name,
               m.record_status, 0, m.wage_type, {DST_BRANCH}
          FROM sls.pay_scheme_master m
          JOIN {MAP} k ON k.source_payscheme_id = m.payscheme_id
          LEFT JOIN sls.pay_scheme_master t ON t.payscheme_id = k.new_payscheme_id
         WHERE t.payscheme_id IS NULL""",
        f"""SELECT COUNT(*) FROM {MAP} k
              LEFT JOIN sls.pay_scheme_master t ON t.payscheme_id = k.new_payscheme_id
             WHERE t.payscheme_id IS NULL""")

    step("pay_scheme_details", f"""
        INSERT INTO sls.pay_scheme_details
               (ID, COMPONENT_ID, FORMULA, PAY_SCHEME_ID, TYPE, STATUS, DEFAULT_VALUE, updated_by)
        SELECT d.ID + {DETAIL_OFF}, d.COMPONENT_ID, d.FORMULA, k.new_payscheme_id,
               d.TYPE, d.STATUS, d.DEFAULT_VALUE, 0
          FROM sls.pay_scheme_details d
          JOIN {MAP} k ON k.source_payscheme_id = d.PAY_SCHEME_ID
          LEFT JOIN sls.pay_scheme_details t ON t.ID = d.ID + {DETAIL_OFF}
         WHERE t.ID IS NULL""",
        f"""SELECT COUNT(*) FROM sls.pay_scheme_details d
              JOIN {MAP} k ON k.source_payscheme_id = d.PAY_SCHEME_ID
              LEFT JOIN sls.pay_scheme_details t ON t.ID = d.ID + {DETAIL_OFF}
             WHERE t.ID IS NULL""")

    log("\n-- 3/4. re-point branch-87 assignments and periods --")
    step("pay_employee_payscheme", f"""
        UPDATE sls.pay_employee_payscheme e
          JOIN {MAP} k ON k.source_payscheme_id = e.PAY_SCHEME_ID
          JOIN ({B87_EB}) b ON b.eb_id = e.EMPLOYEEID
           SET e.PAY_SCHEME_ID = k.new_payscheme_id
         WHERE e.STATUS = 1""",
        f"""SELECT COUNT(*) FROM sls.pay_employee_payscheme e
              JOIN {MAP} k ON k.source_payscheme_id = e.PAY_SCHEME_ID
              JOIN ({B87_EB}) b ON b.eb_id = e.EMPLOYEEID
             WHERE e.STATUS = 1""")

    step("pay_period", f"""
        UPDATE sls.pay_period p
          JOIN {MAP} k ON k.source_payscheme_id = p.PAYSCHEME_ID
           SET p.PAYSCHEME_ID = k.new_payscheme_id, p.COMPANY_ID = {DST_CO}
         WHERE p.branch_id = {DST_BRANCH}""",
        f"""SELECT COUNT(*) FROM sls.pay_period p
              JOIN {MAP} k ON k.source_payscheme_id = p.PAYSCHEME_ID
             WHERE p.branch_id = {DST_BRANCH}""")

    log("\n-- 5. re-point branch-87 salary structure --")
    step("pay_employee_structure", f"""
        UPDATE sls.pay_employee_structure s
          JOIN {MAP} k ON k.source_payscheme_id = s.PAYSCHEME_ID
          JOIN ({B87_EB}) b ON b.eb_id = s.EMPLOYEEID
           SET s.PAYSCHEME_ID = k.new_payscheme_id""",
        f"""SELECT COUNT(*) FROM sls.pay_employee_structure s
              JOIN {MAP} k ON k.source_payscheme_id = s.PAYSCHEME_ID
              JOIN ({B87_EB}) b ON b.eb_id = s.EMPLOYEEID""")

    log("\n-- 6. re-point branch-87 payroll (chunked) --")
    todo = scalar(f"""SELECT COUNT(*) FROM sls.pay_employee_payroll r
                        JOIN {MAP} k ON k.source_payscheme_id = r.PAYSCHEME_ID
                        JOIN ({B87_EB}) b ON b.eb_id = r.EMPLOYEEID""")
    STATS["pay_employee_payroll"] = todo
    if not COMMIT:
        log(f"  [dry] pay_employee_payroll: {todo:,}")
    else:
        max_id = scalar("SELECT MAX(ID) FROM sls.pay_employee_payroll")
        total, lo = 0, 0
        while lo <= max_id:
            hi = lo + CHUNK
            cur.execute(f"""
                UPDATE sls.pay_employee_payroll r
                  JOIN {MAP} k ON k.source_payscheme_id = r.PAYSCHEME_ID
                  JOIN ({B87_EB}) b ON b.eb_id = r.EMPLOYEEID
                   SET r.PAYSCHEME_ID = k.new_payscheme_id
                 WHERE r.ID >= {lo} AND r.ID < {hi}""")
            total += cur.rowcount
            conn.commit()
            lo = hi
        log(f"  pay_employee_payroll: {total:,} (expected {todo:,})")
        STATS["pay_employee_payroll"] = total


def verify():
    log("\n-- verification --")
    for label, sql in [
        (f"schemes now visible for co {DST_CO}",
         f"SELECT COUNT(*) FROM sls.pay_scheme_master WHERE co_id = {DST_CO}"),
        (f"...of which stamped branch {DST_BRANCH}",
         f"""SELECT COUNT(*) FROM sls.pay_scheme_master
              WHERE co_id = {DST_CO} AND branch_id = {DST_BRANCH}"""),
        ("new schemes with no detail rows (should be 0)",
         f"""SELECT COUNT(*) FROM {MAP} k
               LEFT JOIN sls.pay_scheme_details d ON d.PAY_SCHEME_ID = k.new_payscheme_id
              WHERE d.ID IS NULL"""),
        ("branch-87 assignments still on a co-1 scheme (should be 0)",
         f"""SELECT COUNT(*) FROM sls.pay_employee_payscheme e
               JOIN {MAP} k ON k.source_payscheme_id = e.PAY_SCHEME_ID
               JOIN ({B87_EB}) b ON b.eb_id = e.EMPLOYEEID
              WHERE e.STATUS = 1"""),
        ("branch-87 structure rows disagreeing with the employee's scheme (should be 0)",
         f"""SELECT COUNT(*) FROM sls.pay_employee_structure s
               JOIN sls.pay_employee_payscheme e
                 ON e.EMPLOYEEID = s.EMPLOYEEID AND e.STATUS = 1
               JOIN ({B87_EB}) b ON b.eb_id = s.EMPLOYEEID
              WHERE s.STATUS = 1 AND s.PAYSCHEME_ID <> e.PAY_SCHEME_ID
                AND s.PAYSCHEME_ID IN (SELECT source_payscheme_id FROM {MAP})"""),
        ("branch-87 payroll disagreeing with its period's scheme (should be 0)",
         f"""SELECT COUNT(*) FROM sls.pay_employee_payroll r
               JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID AND p.branch_id = {DST_BRANCH}
              WHERE r.PAYSCHEME_ID <> p.PAYSCHEME_ID"""),
        ("branch-4 assignments accidentally moved (should be 0)",
         f"""SELECT COUNT(*) FROM sls.pay_employee_payscheme e
               JOIN {MAP} k ON k.new_payscheme_id = e.PAY_SCHEME_ID
               JOIN (SELECT DISTINCT eb_id FROM sls.hrms_ed_official_details
                      WHERE branch_id = {SRC_BRANCH}) b ON b.eb_id = e.EMPLOYEEID"""),
        ("active branch-87 employees still with no scheme (informational)",
         f"""SELECT COUNT(DISTINCT o.eb_id) FROM sls.hrms_ed_official_details o
              WHERE o.branch_id = {DST_BRANCH} AND o.active = 1
                AND NOT EXISTS (SELECT 1 FROM sls.pay_employee_payscheme e
                                 WHERE e.EMPLOYEEID = o.eb_id AND e.STATUS = 1)"""),
    ]:
        log(f"  {label}: {scalar(sql):,}")


log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}\n")
build_map()
run()
if COMMIT:
    verify()
    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
                   VALUES ('pay_scheme_mirror_branch87', 'committed', NOW(), NOW(), %s)""",
                (json.dumps(STATS, default=str),))
    conn.commit()
    log("\nlogged to vowsls_to_sls_transfer.migration_log step 'pay_scheme_mirror_branch87'")
else:
    log("\nDry run complete - nothing written (except the scheme map).")
conn.close()
