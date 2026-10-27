"""
Repair the 646 `sls.pay_period` rows that have no branch, then re-derive BUSINESSUNIT_ID.

Root cause: legacy `tbl_pay_period.branch_id` was 0 on these rows, and the payroll migration
mapped 0 -> NULL. A branchless period is invisible to `find_pay_period_by_dates` (it filters
`WHERE branch_id = :branch_id`), and it left 307,542 payroll rows with no branch to derive
BUSINESSUNIT_ID from — so those rows kept the legacy COMPANY id and stayed unfilterable in the
Pay Register. Fixing the periods fixes both symptoms at once.

DERIVATION, highest priority first (recorded per row in `vowsls_to_sls_transfer._map_pay_period_branch.basis`):

  employee_branch       the modal branch of the period's payroll employees, taken from their
                        ACTIVE hrms_ed_official_details row, accepted only when that branch
                        holds >= MIN_SHARE of the period's payroll rows.
                        (Active-only is deliberate: falling back to inactive official rows drags
                        in employees' historical branches and drops unambiguous periods 451 -> 369.)
  company_single_branch the period's COMPANY_ID owns exactly one branch in branch_mst, so there
                        is nothing to choose between. Covers periods with no payroll at all.
  ambiguous             neither applies -> branch_id stays NULL. Guessing a branch here would
                        silently file payroll under the wrong factory; leaving it NULL keeps the
                        row honest and its legacy company id intact.

A derived branch is only accepted when it belongs to the period's own COMPANY_ID — otherwise the
row is downgraded to `ambiguous` and counted separately.

Phase 2 re-runs the BUSINESSUNIT_ID backfill from `run_copy_pay_branch4_to_branch87.py` so the
payroll rows under the newly-branched periods pick up their branch.

ROLLBACK:
  UPDATE sls.pay_period p JOIN vowsls_to_sls_transfer._map_pay_period_branch m ON m.period_id = p.ID
     SET p.branch_id = NULL WHERE m.basis <> 'ambiguous';
  -- BUSINESSUNIT_ID: see run_copy_pay_branch4_to_branch87.py header.
  DROP TABLE vowsls_to_sls_transfer._map_pay_period_branch;

Usage:
  python run_fix_pay_period_missing_branch.py            # dry run — prints the derivation buckets
  python run_fix_pay_period_missing_branch.py --commit
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
MIN_SHARE = 0.90
MIGRATED_PAYROLL_MIN_ID = 100_000_000
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


# ─────────────────────────────────────────────── 1. derive
def derive():
    log("-- deriving a branch for the branchless periods --")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer._map_pay_period_branch (
            period_id      int         NOT NULL PRIMARY KEY,
            company_id     int         NULL,
            derived_branch int         NULL,
            derived_company int        NULL,
            basis          varchar(28) NOT NULL,
            share          decimal(5,4) NULL,
            payroll_rows   int         NOT NULL DEFAULT 0
        ) ENGINE=InnoDB""")
    conn.commit()

    if scalar("SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_pay_period_branch"):
        log("  already derived — reusing (drop the table to redo)")
    else:
        # Modal active-official branch per branchless period.
        # `share` is measured against the EVIDENCE base (payroll rows whose employee has an active
        # official row), not the period's total rows — an employee with no active official row
        # says nothing about the branch, so counting them in the denominator would veto periods
        # where every employee that IS resolvable agrees.
        cur.execute(f"""
            INSERT INTO vowsls_to_sls_transfer._map_pay_period_branch
                   (period_id, company_id, derived_branch, derived_company, basis, share, payroll_rows)
            WITH ev AS (
                SELECT r.PAYPERIOD_ID pid, o.branch_id, COUNT(*) n
                  FROM sls.pay_employee_payroll r
                  JOIN sls.hrms_ed_official_details o ON o.eb_id = r.EMPLOYEEID AND o.active = 1
                  JOIN sls.pay_period pp ON pp.ID = r.PAYPERIOD_ID AND pp.branch_id IS NULL
                 GROUP BY r.PAYPERIOD_ID, o.branch_id
            ), ranked AS (
                SELECT pid, branch_id, n,
                       SUM(n) OVER (PARTITION BY pid) AS ev_total,
                       ROW_NUMBER() OVER (PARTITION BY pid ORDER BY n DESC) AS rk
                  FROM ev
            ), best AS (
                SELECT pid, branch_id, n, ev_total FROM ranked WHERE rk = 1
            )
            SELECT p.ID, p.COMPANY_ID,
                   CASE
                     WHEN best.branch_id IS NOT NULL
                      AND best.n / best.ev_total >= {MIN_SHARE} THEN best.branch_id
                     WHEN solo.branch_id IS NOT NULL           THEN solo.branch_id
                     ELSE NULL
                   END,
                   CASE
                     WHEN best.branch_id IS NOT NULL
                      AND best.n / best.ev_total >= {MIN_SHARE} THEN COALESCE(bm.co_id, p.COMPANY_ID)
                     ELSE p.COMPANY_ID
                   END,
                   CASE
                     WHEN best.branch_id IS NOT NULL
                      AND best.n / best.ev_total >= {MIN_SHARE}
                      AND bm.co_id <=> p.COMPANY_ID            THEN 'employee_branch'
                     WHEN best.branch_id IS NOT NULL
                      AND best.n / best.ev_total >= {MIN_SHARE} THEN 'employee_branch_co_fixed'
                     WHEN solo.branch_id IS NOT NULL           THEN 'company_single_branch'
                     WHEN best.branch_id IS NOT NULL           THEN 'ambiguous_mixed'
                     ELSE 'ambiguous_no_evidence'
                   END,
                   best.n / best.ev_total,
                   COALESCE(tot.total, 0)
              FROM sls.pay_period p
              LEFT JOIN (SELECT PAYPERIOD_ID, COUNT(*) total
                           FROM sls.pay_employee_payroll GROUP BY PAYPERIOD_ID) tot
                     ON tot.PAYPERIOD_ID = p.ID
              LEFT JOIN best ON best.pid = p.ID
              LEFT JOIN sls.branch_mst bm ON bm.branch_id = best.branch_id
              LEFT JOIN (SELECT co_id, MIN(branch_id) branch_id FROM sls.branch_mst
                          GROUP BY co_id HAVING COUNT(*) = 1) solo
                     ON solo.co_id = p.COMPANY_ID
             WHERE p.branch_id IS NULL""")
        conn.commit()
        log(f"  derived {cur.rowcount} rows")

    cur.execute("""SELECT basis, COUNT(*) periods, SUM(payroll_rows) rows_
                     FROM vowsls_to_sls_transfer._map_pay_period_branch GROUP BY basis ORDER BY periods DESC""")
    buckets = cur.fetchall()
    for basis, periods, rows in buckets:
        log(f"    {basis:<24} {periods:>4} periods   {int(rows or 0):>8,} payroll rows")
    STATS["derivation"] = {b: [p, int(r or 0)] for b, p, r in buckets}


# ─────────────────────────────────────────────── 2. apply
def apply_branches():
    log("\n-- writing branch_id onto the periods --")
    n = scalar("""SELECT COUNT(*) FROM sls.pay_period p
                    JOIN vowsls_to_sls_transfer._map_pay_period_branch m ON m.period_id = p.ID
                   WHERE p.branch_id IS NULL AND m.derived_branch IS NOT NULL""")
    if not COMMIT:
        log(f"  [dry] would set branch_id on {n} periods")
        STATS["periods_branched"] = n
        return
    # COMPANY_ID moves with the branch: on the co1->co106 remap periods the legacy company is the
    # stale field, and the employees are ground truth for where the payroll was actually paid.
    cur.execute("""UPDATE sls.pay_period p
                     JOIN vowsls_to_sls_transfer._map_pay_period_branch m ON m.period_id = p.ID
                      SET p.branch_id = m.derived_branch, p.COMPANY_ID = m.derived_company
                    WHERE p.branch_id IS NULL AND m.derived_branch IS NOT NULL""")
    conn.commit()
    log(f"  branch_id set on {cur.rowcount} periods")
    STATS["periods_branched"] = cur.rowcount


# ─────────────────────────────────────────────── 3. re-backfill
def rebackfill():
    log("\n-- re-running the BUSINESSUNIT_ID backfill --")
    todo = scalar(f"""
        SELECT COUNT(*) FROM sls.pay_employee_payroll r
          JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID
         WHERE r.ID > {MIGRATED_PAYROLL_MIN_ID} AND p.branch_id IS NOT NULL
           AND (r.BUSINESSUNIT_ID <> p.branch_id OR r.BUSINESSUNIT_ID IS NULL)""")
    log(f"  rows to correct: {todo:,}")
    STATS["bu_rows_to_correct"] = todo
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
             WHERE r.ID > {MIGRATED_PAYROLL_MIN_ID} AND p.branch_id IS NOT NULL
               AND r.BUSINESSUNIT_ID <> p.branch_id
               AND r.ID >= {lo} AND r.ID < {hi}""")
        total += cur.rowcount
        conn.commit()
        lo = hi
    log(f"  BUSINESSUNIT_ID corrected on {total:,} further rows")
    STATS["bu_rows_updated"] = total


def verify():
    log("\n-- verification --")
    for label, sql in [
        ("pay_period rows still without a branch",
         "SELECT COUNT(*) FROM sls.pay_period WHERE branch_id IS NULL"),
        ("payroll rows whose period still has no branch",
         """SELECT COUNT(*) FROM sls.pay_employee_payroll r
              JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID WHERE p.branch_id IS NULL"""),
        ("migrated payroll rows mismatching their period branch (should be 0)",
         f"""SELECT COUNT(*) FROM sls.pay_employee_payroll r
               JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID
              WHERE r.ID > {MIGRATED_PAYROLL_MIN_ID} AND p.branch_id IS NOT NULL
                AND r.BUSINESSUNIT_ID <> p.branch_id"""),
        ("periods whose branch does not belong to their company (should be 0)",
         """SELECT COUNT(*) FROM sls.pay_period p
              JOIN sls.branch_mst b ON b.branch_id = p.branch_id
             WHERE p.COMPANY_ID IS NOT NULL AND b.co_id <> p.COMPANY_ID"""),
        ("payroll total (must not change)", "SELECT COUNT(*) FROM sls.pay_employee_payroll"),
    ]:
        log(f"  {label}: {scalar(sql):,}")


log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}\n")
derive()
apply_branches()
rebackfill()
if COMMIT:
    verify()
    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
                   VALUES ('pay_period_missing_branch_fix', 'committed', NOW(), NOW(), %s)""",
                (json.dumps(STATS, default=str),))
    conn.commit()
    log("\nlogged to vowsls_to_sls_transfer.migration_log step 'pay_period_missing_branch_fix'")
else:
    log("\nDry run complete - nothing written (except the derivation table).")
conn.close()
