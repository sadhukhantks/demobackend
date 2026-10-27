"""
Seed `sls.outsider_rate_approve` (Cash / Daily Rate Entry master) for company 2
(EJM) from the legacy `vowsls.worker_master.cash_rate` column.

WHY
---
`cashHands.py` prices cash-hand attendance from `outsider_rate_approve`; the
docstring there records that the rate used to come from `worker_master.cash_rate`,
a column the tenant schema dropped. Company 2 has 3,536 branch-29 workers
carrying a rate in legacy and exactly ONE rate row in sls, so every EJM cash-hand
row currently prices at 0.

SOURCE
------
`vowsls`, not `EMPMILL12` — EMPMILL12.worker_master has 0 rows with a cash_rate
for company 2 (the column is only populated on the vowsls side).

SCOPE: company_id = 2, cash_rate > 0, active = 'Y'  -> 3,538 workers
(3,536 branch 29 FACTORY + 2 branch 10 HO). The 17 active='N' rows are ex-workers.

EMP_CODE MAPPING
----------------
Matched on `emp_code` into `hrms_ed_official_details` (active = 1), constrained to
branches of co 2 so a code duplicated in another branch cannot be hit. Verified
before writing: 3,538 of 3,538 match, exactly one sls employee each, none ambiguous.

FIELDS
------
  rate_approve : ROUND(cash_rate) — the target column is int; only 10 of 3,555
                 legacy values carry paise.
  app_date     : --app-date (default 2022-01-01). The column is an *effective from*
                 date, not an exact key (see cashHands.process_insert_query), and
                 branch-29 attendance starts 2022-08-31, so this baseline covers all
                 history and is superseded by any later approval entered in the UI.
  app_shift    : NULL — one rate for every shift, as legacy had.
  desig_id     : the employee's own sls official designation, not legacy desg_id
                 (they disagree on 431 of 3,538).

PK offset 900000 + eb_id keeps this reversible and re-runnable.

Re-running is a no-op: INSERT IGNORE on the PK plus a NOT EXISTS guard on any
active rate the employee already has.

ROLLBACK:
  DELETE FROM sls.outsider_rate_approve WHERE outsider_rate_app_id >= 900000;

Usage:
  python run_seed_co2_outsider_rate_from_worker_master.py                  # dry run
  python run_seed_co2_outsider_rate_from_worker_master.py --commit
  python run_seed_co2_outsider_rate_from_worker_master.py --app-date 2026-04-01 --commit
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
APP_DATE = "2022-01-01"
if "--app-date" in sys.argv:
    APP_DATE = sys.argv[sys.argv.index("--app-date") + 1]

CO_ID = 2
PK_OFFSET = 900000

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


# Legacy worker -> sls employee, by emp_code inside company 2's branches.
MAPPED = f"""
    SELECT w.eb_id            AS legacy_eb,
           o.eb_id            AS nb,
           ROUND(w.cash_rate) AS rate,
           o.designation_id   AS desig_id
      FROM vowsls.worker_master w
      JOIN sls.hrms_ed_official_details o ON o.emp_code = w.emp_code AND o.active = 1
      JOIN sls.hrms_ed_personal_details p ON p.eb_id = o.eb_id
      JOIN sls.branch_mst b ON b.branch_id = p.branch_id AND b.co_id = {CO_ID}
     WHERE w.company_id = {CO_ID} AND w.cash_rate > 0 AND w.active = 'Y'
"""

source_rows = scalar(f"""
    SELECT COUNT(*) FROM vowsls.worker_master
     WHERE company_id = {CO_ID} AND cash_rate > 0 AND active = 'Y'""")
unmatched = scalar(f"""
    SELECT COUNT(*) FROM vowsls.worker_master w
     WHERE w.company_id = {CO_ID} AND w.cash_rate > 0 AND w.active = 'Y'
       AND NOT EXISTS (SELECT 1 FROM sls.hrms_ed_official_details o
                         JOIN sls.hrms_ed_personal_details p ON p.eb_id = o.eb_id
                         JOIN sls.branch_mst b ON b.branch_id = p.branch_id AND b.co_id = {CO_ID}
                        WHERE o.emp_code = w.emp_code AND o.active = 1)""")
ambiguous = scalar(f"SELECT COUNT(*) FROM (SELECT legacy_eb FROM ({MAPPED}) m "
                   f"GROUP BY legacy_eb HAVING COUNT(DISTINCT nb) > 1) x")
collisions = scalar(f"SELECT COUNT(*) FROM (SELECT nb FROM ({MAPPED}) m "
                    f"GROUP BY nb HAVING COUNT(DISTINCT rate) > 1) x")
target_before = scalar("SELECT COUNT(*) FROM sls.outsider_rate_approve")
to_insert = scalar(f"""
    SELECT COUNT(DISTINCT m.nb) FROM ({MAPPED}) m
     WHERE NOT EXISTS (SELECT 1 FROM sls.outsider_rate_approve t
                        WHERE t.eb_id = m.nb AND t.is_active = 1)""")

log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}   app_date: {APP_DATE}")
log(f"  legacy co{CO_ID} workers with rate : {source_rows:,}")
log(f"  unmatched by emp_code            : {unmatched}")
log(f"  emp_codes hitting >1 sls employee: {ambiguous}")
log(f"  sls employees with 2 rates       : {collisions}")
log(f"  sls rows before                  : {target_before:,}")
log(f"  rows to insert                   : {to_insert:,}")

if unmatched or ambiguous or collisions:
    log("  ABORT: mapping is no longer clean — re-check before loading.")
    sys.exit(1)

INSERT = f"""
    INSERT IGNORE INTO sls.outsider_rate_approve
           (outsider_rate_app_id, eb_id, rate_approve, is_active, app_date, app_shift, desig_id)
    SELECT {PK_OFFSET} + m.legacy_eb, m.nb, m.rate, 1, '{APP_DATE}', NULL, m.desig_id
      FROM ({MAPPED}) m
     WHERE NOT EXISTS (SELECT 1 FROM sls.outsider_rate_approve t
                        WHERE t.eb_id = m.nb AND t.is_active = 1)"""

if not COMMIT:
    log("  dry run — nothing written. Re-run with --commit.")
    sys.exit(0)

cur.execute("INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at) "
            "VALUES ('co2_outsider_rate_from_worker_master', 'running', NOW())")
log_id = cur.lastrowid

cur.execute(INSERT)
inserted = cur.rowcount

stats = {"legacy_rows": source_rows, "inserted": inserted, "app_date": APP_DATE,
         "target_rows_before": target_before}
cur.execute(f"""
    SELECT p.branch_id, COUNT(*) FROM sls.outsider_rate_approve t
      JOIN sls.hrms_ed_personal_details p ON p.eb_id = t.eb_id
     WHERE t.is_active = 1 AND t.outsider_rate_app_id >= {PK_OFFSET}
     GROUP BY p.branch_id""")
stats["by_branch"] = {str(b): c for b, c in cur.fetchall()}

cur.execute("UPDATE vowsls_to_sls_transfer.migration_log SET status='committed', finished_at=NOW(), "
            "details=%s WHERE id=%s", (json.dumps(stats), log_id))
conn.commit()

log(f"  inserted                         : {inserted:,}")
log(f"  by branch                        : {stats['by_branch']}")
log("  committed.")
