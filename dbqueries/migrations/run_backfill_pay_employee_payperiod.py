"""
Rebuild `sls.pay_employee_payperiod` from the migrated payroll — fixes the empty Pay Register detail.

SYMPTOM
-------
Pay Register > View shows "No rows" for every migrated period (and the list's Net Pay column is
blank), even though the payroll rows are there — e.g. period 105806 (NJM_CONT_WAGES_NEW, Jul-2026,
branch 4) has 11,076 rows in pay_employee_payroll and 0 in pay_employee_payperiod.

CAUSE
-----
`get_pay_register_salary` (src/hrms/query.py:620) drives the whole detail grid off
pay_employee_payperiod and INNER JOINs pay_employee_payroll to it:

    FROM pay_employee_payperiod epp
    JOIN pay_employee_payroll ep ON ep.EMPLOYEEID  = epp.EMPLOYEEID
                                AND ep.PAYPERIOD_ID = epp.PAY_PERIOD_ID
                                AND ep.PAYSCHEME_ID = epp.PAY_SCHEME_ID

and the list's Net Pay is `SUM(epp.NET)` (query.py:531). `vowsls.tbl_pay_employee_payperiod` was
EMPTY (0 rows) — it is a summary table the new sls payroll run writes, and legacy never populated
it — so the 2026-08-15 payroll migration had nothing to copy and left it with only the 185
sls-native rows covering periods 1–18. No epp row => no register rows, for all 5,730 migrated
periods.

FIX
---
Derive one epp row per distinct (EMPLOYEEID, PAYPERIOD_ID, PAYSCHEME_ID) present in
pay_employee_payroll — 363,302 groups. Component ids were confirmed against real sls-native data
(period 18, eb 31526: epp.NET 6157.00 / epp.GROSS 7623.03):

    NET   = component 21  `C_NET` "Net Payble"   — present in 362,947 of the 363,302 groups (99.9%)
            falling back to 357 `C_finalnetpay_`
    GROSS = component 350 `C_gross1amt_`, else 349 `C_gross2amt_`
    BASIC = component 7   `C_BASIC_`

NET is the one that matters — it is the only field either screen reads. GROSS and BASIC are
best-effort: they are scheme-specific aggregates (350/349 appear in worker schemes only) and are
not read by the register list or detail. sls-native epp rows mostly carry 0 in all three anyway.
Gross is deliberately NOT computed as SUM(type=1) — the earnings set already contains running
aggregates (totalwagesamt, gross1amt, PFG), so summing double-counts.

Existing epp rows are never touched: the insert is guarded by a NOT-EXISTS on
(EMPLOYEEID, PAY_PERIOD_ID, PAY_SCHEME_ID), so re-running is a no-op.

ROLLBACK:
  DELETE FROM sls.pay_employee_payperiod WHERE ID > <max_id_before, printed at start and logged>;

Usage:
  python run_backfill_pay_employee_payperiod.py            # dry run (counts only)
  python run_backfill_pay_employee_payperiod.py --commit
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv

C_BASIC, C_NET, C_NET_FALLBACK = 7, 21, 357
C_GROSS, C_GROSS_FALLBACK = 350, 349
PERIOD_CHUNK = 20_000

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


def scalar(sql):
    cur.execute(sql)
    return cur.fetchone()[0]


max_before = scalar("SELECT COALESCE(MAX(ID), 0) FROM sls.pay_employee_payperiod")
existing = scalar("SELECT COUNT(*) FROM sls.pay_employee_payperiod")
missing = scalar("""
    SELECT COUNT(*) FROM (
        SELECT r.EMPLOYEEID, r.PAYPERIOD_ID, r.PAYSCHEME_ID
          FROM sls.pay_employee_payroll r
          LEFT JOIN sls.pay_employee_payperiod e
                 ON e.EMPLOYEEID = r.EMPLOYEEID
                AND e.PAY_PERIOD_ID = r.PAYPERIOD_ID
                AND e.PAY_SCHEME_ID = r.PAYSCHEME_ID
         WHERE e.ID IS NULL
         GROUP BY r.EMPLOYEEID, r.PAYPERIOD_ID, r.PAYSCHEME_ID) x""")

log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}")
log(f"  pay_employee_payperiod rows now : {existing:,}  (MAX(ID) = {max_before})")
log(f"  groups to create                : {missing:,}")
log(f"  ROLLBACK KEY -> DELETE FROM sls.pay_employee_payperiod WHERE ID > {max_before};")
STATS["existing_rows"] = existing
STATS["groups_to_create"] = missing
STATS["max_id_before"] = max_before

INSERT = f"""
    INSERT INTO sls.pay_employee_payperiod
           (EMPLOYEEID, PAY_PERIOD_ID, PAY_SCHEME_ID, BASIC, NET, GROSS, STATUS, updated_by)
    SELECT r.EMPLOYEEID, r.PAYPERIOD_ID, r.PAYSCHEME_ID,
           COALESCE(MAX(CASE WHEN r.COMPONENT_ID = {C_BASIC} THEN r.AMOUNT END), 0),
           COALESCE(MAX(CASE WHEN r.COMPONENT_ID = {C_NET} THEN r.AMOUNT END),
                    MAX(CASE WHEN r.COMPONENT_ID = {C_NET_FALLBACK} THEN r.AMOUNT END), 0),
           COALESCE(MAX(CASE WHEN r.COMPONENT_ID = {C_GROSS} THEN r.AMOUNT END),
                    MAX(CASE WHEN r.COMPONENT_ID = {C_GROSS_FALLBACK} THEN r.AMOUNT END), 0),
           1, 0
      FROM sls.pay_employee_payroll r
      LEFT JOIN (SELECT EMPLOYEEID, PAY_PERIOD_ID, PAY_SCHEME_ID
                   FROM sls.pay_employee_payperiod) e
             ON e.EMPLOYEEID = r.EMPLOYEEID
            AND e.PAY_PERIOD_ID = r.PAYPERIOD_ID
            AND e.PAY_SCHEME_ID = r.PAYSCHEME_ID
     WHERE e.EMPLOYEEID IS NULL
       AND r.PAYPERIOD_ID >= %d AND r.PAYPERIOD_ID < %d
     GROUP BY r.EMPLOYEEID, r.PAYPERIOD_ID, r.PAYSCHEME_ID"""

if COMMIT:
    max_period = scalar("SELECT MAX(PAYPERIOD_ID) FROM sls.pay_employee_payroll")
    total, lo = 0, 0
    while lo <= max_period:
        hi = lo + PERIOD_CHUNK
        cur.execute(INSERT % (lo, hi))
        total += cur.rowcount
        conn.commit()
        if cur.rowcount:
            log(f"    periods {lo:,}–{hi - 1:,}: +{cur.rowcount:,}  (total {total:,})")
        lo = hi
    log(f"  inserted {total:,} rows")
    STATS["inserted"] = total

    log("\n-- verification --")
    for label, sql in [
        ("payroll groups still without an epp row (should be 0)", """
            SELECT COUNT(*) FROM (
                SELECT 1 FROM sls.pay_employee_payroll r
                  LEFT JOIN sls.pay_employee_payperiod e
                         ON e.EMPLOYEEID = r.EMPLOYEEID AND e.PAY_PERIOD_ID = r.PAYPERIOD_ID
                        AND e.PAY_SCHEME_ID = r.PAYSCHEME_ID
                 WHERE e.ID IS NULL
                 GROUP BY r.EMPLOYEEID, r.PAYPERIOD_ID, r.PAYSCHEME_ID) x"""),
        ("epp rows with no matching payroll (should be 0 beyond the native 185)", """
            SELECT COUNT(*) FROM sls.pay_employee_payperiod e
             WHERE NOT EXISTS (SELECT 1 FROM sls.pay_employee_payroll r
                                WHERE r.EMPLOYEEID = e.EMPLOYEEID
                                  AND r.PAYPERIOD_ID = e.PAY_PERIOD_ID
                                  AND r.PAYSCHEME_ID = e.PAY_SCHEME_ID)"""),
        ("epp rows with NET = 0 (informational — scheme had no C_NET component)",
         "SELECT COUNT(*) FROM sls.pay_employee_payperiod WHERE NET = 0"),
        ("Jul-2026 NJM_CONT_WAGES_NEW branch-4 period 105806: register rows", """
            SELECT COUNT(*) FROM sls.pay_employee_payperiod WHERE PAY_PERIOD_ID = 105806"""),
        ("...its Net Pay total", """
            SELECT COALESCE(SUM(NET), 0) FROM sls.pay_employee_payperiod
             WHERE PAY_PERIOD_ID = 105806 AND PAY_SCHEME_ID = 1163"""),
    ]:
        log(f"  {label}: {scalar(sql):,}")

    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
                   VALUES ('pay_employee_payperiod_backfill', 'committed', NOW(), NOW(), %s)""",
                (json.dumps(STATS, default=str),))
    conn.commit()
    log("\nlogged to vowsls_to_sls_transfer.migration_log step 'pay_employee_payperiod_backfill'")
else:
    log("\nDry run complete - nothing written.")
conn.close()
