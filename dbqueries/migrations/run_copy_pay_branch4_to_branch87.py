"""
Follow-ups to `run_migrate_vowsls_payscheme_to_sls.py` (2026-08-15), both user-approved:

PHASE `bu` — fix BUSINESSUNIT_ID on the migrated payroll rows.
  Legacy `tbl_pay_employee_payroll.BUSINESSUNIT_ID` holds the **company** id (1, 2, 3, 46, 106…).
  sls-native rows hold the **branch** id, and `get_pay_register_salary` (src/hrms/query.py)
  filters `ep.BUSINESSUNIT_ID = :branch_id`. The migration passed the column through unchanged,
  so 0 of the 7.3M rows under branch-4 periods matched their employee's branch and the Pay
  Register branch filter returned nothing for migrated history.
  Fix: set BUSINESSUNIT_ID from the row's own pay_period.branch_id — exactly how sls-native rows
  behave (native period 18 is branch 4 → its payroll rows carry 4).
  Rows whose period has branch_id IS NULL (646 such periods, ~307k payroll rows, legacy
  branch_id = 0) are LEFT ALONE rather than nulled — nulling would destroy the legacy company id
  and gain nothing. They stay unfilterable by branch; nothing knows their branch.

PHASE `copy` — replicate branch 4's pay-scheme setup onto branch 87.
  branch 4  = FACTORY, co 1   (2,802 employees)
  branch 87 = FACTORY, co 106 (4,597 employees, but only 1 pay period)
  Both branches are live and hold their own payroll; this is a COPY, not a relocation.
    * sls.pay_period                    — 1,135 branch-4 periods -> branch 87 / co 106,
                                          new ID = source ID + PERIOD_COPY_OFFSET.
                                          STATUS is copied verbatim (nothing in the backend gates
                                          on it) — note the copies carry a processed-looking
                                          status with no payroll rows behind them.
    * sls.tbl_payslip_print_component   — 7 rows (payscheme 15 / co 1 / branch 4) -> co 106 /
                                          branch 87. pay_id is AUTO_INCREMENT, not offset.
  Both inserts are guarded against re-running (NOT EXISTS on the natural key), so the phase is
  idempotent.

KNOWN BACKEND QUIRK (pre-existing, not introduced here): `find_pay_period_by_dates` matches on
(branch_id, from_date, to_date) only — it ignores PAYSCHEME_ID and takes `ORDER BY id DESC LIMIT 1`.
Branch 4 already has many same-month periods across its 16 schemes, so this can already resolve to
the wrong scheme's period; copying 1,135 periods onto branch 87 gives that quirk more room to bite.

ROLLBACK:
  DELETE FROM sls.pay_period WHERE ID > 500000;
  DELETE FROM sls.tbl_payslip_print_component WHERE company_id = 106 AND branch_id = 87;
  -- the BUSINESSUNIT_ID backfill is NOT reversible from sls alone; to restore the legacy value:
  --   UPDATE sls.pay_employee_payroll r
  --     JOIN vowsls.tbl_pay_employee_payroll l ON l.ID = r.ID - 100000000
  --      SET r.BUSINESSUNIT_ID = l.BUSINESSUNIT_ID
  --    WHERE r.ID > 100000000;

Usage:
  python run_copy_pay_branch4_to_branch87.py                 # dry run (counts only)
  python run_copy_pay_branch4_to_branch87.py --commit
  python run_copy_pay_branch4_to_branch87.py --commit --only copy
  python run_copy_pay_branch4_to_branch87.py --commit --only bu
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
PHASES = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else "all"

SRC_BRANCH, SRC_CO = 4, 1
DST_BRANCH, DST_CO = 87, 106
PERIOD_COPY_OFFSET = 500_000
MIGRATED_PAYROLL_MIN_ID = 100_000_000   # PK offset used by the payroll migration
CHUNK = 500_000

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


def log(msg):
    print(msg, flush=True)


def scalar(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()[0]


def want(phase):
    return PHASES in ("all", phase)


# ════════════════════════════════════════════════ phase bu
def fix_businessunit():
    log("\n-- BUSINESSUNIT_ID backfill (migrated payroll rows) --")
    todo = scalar(f"""
        SELECT COUNT(*) FROM sls.pay_employee_payroll r
          JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID
         WHERE r.ID > {MIGRATED_PAYROLL_MIN_ID}
           AND p.branch_id IS NOT NULL
           AND (r.BUSINESSUNIT_ID <> p.branch_id OR r.BUSINESSUNIT_ID IS NULL)""")
    skipped = scalar(f"""
        SELECT COUNT(*) FROM sls.pay_employee_payroll r
          JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID
         WHERE r.ID > {MIGRATED_PAYROLL_MIN_ID} AND p.branch_id IS NULL""")
    log(f"  rows to correct: {todo:,}   left alone (period has no branch): {skipped:,}")
    STATS["bu_rows_to_correct"] = todo
    STATS["bu_rows_left_alone"] = skipped
    if not COMMIT:
        log("  [dry] no update issued")
        return

    max_id = scalar("SELECT MAX(ID) FROM sls.pay_employee_payroll")
    total, lo = 0, MIGRATED_PAYROLL_MIN_ID
    while lo <= max_id:
        hi = lo + CHUNK
        cur.execute(f"""
            UPDATE sls.pay_employee_payroll r
              JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID
               SET r.BUSINESSUNIT_ID = p.branch_id
             WHERE r.ID > {MIGRATED_PAYROLL_MIN_ID}
               AND p.branch_id IS NOT NULL
               AND r.BUSINESSUNIT_ID <> p.branch_id
               AND r.ID >= {lo} AND r.ID < {hi}""")
        total += cur.rowcount
        conn.commit()
        lo = hi
        log(f"    updated {total:>10,}  (through id {min(hi, max_id):,})")
    log(f"  BUSINESSUNIT_ID corrected on {total:,} rows")
    STATS["bu_rows_updated"] = total


# ════════════════════════════════════════════════ phase copy
def copy_branch():
    log(f"\n-- copy pay setup: branch {SRC_BRANCH} (co {SRC_CO}) -> branch {DST_BRANCH} (co {DST_CO}) --")

    # 1. pay_period. Guard is a derived table: MySQL forbids sub-selecting the INSERT target.
    period_sql = f"""
        INSERT INTO sls.pay_period
               (ID, FROM_DATE, TO_DATE, PAYSCHEME_ID, STATUS, branch_id, COMPANY_ID, updated_by)
        SELECT p.ID + {PERIOD_COPY_OFFSET}, p.FROM_DATE, p.TO_DATE, p.PAYSCHEME_ID, p.STATUS,
               {DST_BRANCH}, {DST_CO}, 0
          FROM sls.pay_period p
          LEFT JOIN (SELECT PAYSCHEME_ID, FROM_DATE, TO_DATE FROM sls.pay_period
                      WHERE branch_id = {DST_BRANCH}) t
                 ON t.PAYSCHEME_ID <=> p.PAYSCHEME_ID
                AND t.FROM_DATE <=> p.FROM_DATE
                AND t.TO_DATE <=> p.TO_DATE
         WHERE p.branch_id = {SRC_BRANCH} AND t.FROM_DATE IS NULL"""
    eligible = scalar(f"""
        SELECT COUNT(*) FROM sls.pay_period p
          LEFT JOIN (SELECT PAYSCHEME_ID, FROM_DATE, TO_DATE FROM sls.pay_period
                      WHERE branch_id = {DST_BRANCH}) t
                 ON t.PAYSCHEME_ID <=> p.PAYSCHEME_ID
                AND t.FROM_DATE <=> p.FROM_DATE
                AND t.TO_DATE <=> p.TO_DATE
         WHERE p.branch_id = {SRC_BRANCH} AND t.FROM_DATE IS NULL""")
    if COMMIT:
        cur.execute(period_sql)
        conn.commit()
        log(f"  pay_period: inserted {cur.rowcount} (eligible {eligible})")
        STATS["pay_period_copied"] = cur.rowcount
    else:
        log(f"  [dry] pay_period: {eligible} eligible rows")
        STATS["pay_period_copied"] = eligible

    # 2. tbl_payslip_print_component — pay_id is AUTO_INCREMENT, so no offset
    pc_sql = f"""
        INSERT INTO sls.tbl_payslip_print_component
               (component_id, desc_print, payslip_order, payscheme_id, company_id, branch_id,
                fixed_var_cols, is_active, total_print, payslip_print, updated_by)
        SELECT s.component_id, s.desc_print, s.payslip_order, s.payscheme_id,
               {DST_CO}, {DST_BRANCH}, s.fixed_var_cols, s.is_active, s.total_print,
               s.payslip_print, 0
          FROM sls.tbl_payslip_print_component s
          LEFT JOIN (SELECT payscheme_id, component_id FROM sls.tbl_payslip_print_component
                      WHERE company_id = {DST_CO} AND branch_id = {DST_BRANCH}) t
                 ON t.payscheme_id = s.payscheme_id AND t.component_id <=> s.component_id
         WHERE s.company_id = {SRC_CO} AND s.branch_id = {SRC_BRANCH}
           AND t.payscheme_id IS NULL"""
    eligible = scalar(f"""
        SELECT COUNT(*) FROM sls.tbl_payslip_print_component s
          LEFT JOIN (SELECT payscheme_id, component_id FROM sls.tbl_payslip_print_component
                      WHERE company_id = {DST_CO} AND branch_id = {DST_BRANCH}) t
                 ON t.payscheme_id = s.payscheme_id AND t.component_id <=> s.component_id
         WHERE s.company_id = {SRC_CO} AND s.branch_id = {SRC_BRANCH}
           AND t.payscheme_id IS NULL""")
    if COMMIT:
        cur.execute(pc_sql)
        conn.commit()
        log(f"  tbl_payslip_print_component: inserted {cur.rowcount} (eligible {eligible})")
        STATS["print_component_copied"] = cur.rowcount
    else:
        log(f"  [dry] tbl_payslip_print_component: {eligible} eligible rows")
        STATS["print_component_copied"] = eligible


# ════════════════════════════════════════════════ verification
def verify():
    log("\n-- verification --")
    for label, sql in [
        (f"periods on branch {DST_BRANCH}",
         f"SELECT COUNT(*) FROM sls.pay_period WHERE branch_id = {DST_BRANCH}"),
        (f"periods on branch {SRC_BRANCH} (unchanged)",
         f"SELECT COUNT(*) FROM sls.pay_period WHERE branch_id = {SRC_BRANCH}"),
        ("copied periods carrying any payroll (should be 0)",
         f"""SELECT COUNT(*) FROM sls.pay_employee_payroll
              WHERE PAYPERIOD_ID > {PERIOD_COPY_OFFSET}"""),
        (f"print components on co {DST_CO} / branch {DST_BRANCH}",
         f"""SELECT COUNT(*) FROM sls.tbl_payslip_print_component
              WHERE company_id = {DST_CO} AND branch_id = {DST_BRANCH}"""),
        ("migrated payroll rows still mismatching their period branch",
         f"""SELECT COUNT(*) FROM sls.pay_employee_payroll r
               JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID
              WHERE r.ID > {MIGRATED_PAYROLL_MIN_ID} AND p.branch_id IS NOT NULL
                AND r.BUSINESSUNIT_ID <> p.branch_id"""),
        (f"branch-{SRC_BRANCH} payroll now visible to the Pay Register branch filter",
         f"""SELECT COUNT(*) FROM sls.pay_employee_payroll r
               JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID AND p.branch_id = {SRC_BRANCH}
              WHERE r.BUSINESSUNIT_ID = {SRC_BRANCH}"""),
    ]:
        log(f"  {label}: {scalar(sql):,}")


log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}   phases: {PHASES}")
if want("bu"):
    fix_businessunit()
if want("copy"):
    copy_branch()
if COMMIT:
    verify()
    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
                   VALUES ('pay_branch4_to_87_and_bu_fix', 'committed', NOW(), NOW(), %s)""",
                (json.dumps({"phases": PHASES, "stats": STATS}, default=str),))
    conn.commit()
    log("\nlogged to vowsls_to_sls_transfer.migration_log step 'pay_branch4_to_87_and_bu_fix'")
else:
    log("\nDry run complete - nothing written.")
conn.close()
