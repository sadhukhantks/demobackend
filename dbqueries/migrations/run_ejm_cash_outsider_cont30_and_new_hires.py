"""
EJM Cash/Outsider (sls branch 29) data fixes, decided 2026-09-14:

  1. TA for contractor 30 (AJAY SHAW): legacy paid a 30-per-8-hours TA to
     contractor 10 only; contractor 30 gets the same. The process reads the TA
     from tbl_outsider_occupation_rate.ta_rate by contac_id, so its active rows
     get ta_rate = 30.
  2. Contractor 30's flat time-rated rows (prod_calc 1/2/3) pay the `rate`
     column (620 / 560): the flat group pays rate_on_production, so it is set
     equal to rate on those rows.
  3. New hires: the payment build pays only workers marked 'T' in
     tbl_worker_sublocation_table, which was frozen at the EMPMILL12 copy.
     Every active branch-29 worker whose emp_code starts with 'T', who has
     attendance on or after --since (default 2026-08-01) and has no row yet, is
     added with daily_others_pay_duration 'T' and no sublocation.

Default is a DRY RUN that only counts (no writes). --commit applies all three
in one transaction.

ROLLBACK (after --commit; the script prints the exact ids it inserted):
  UPDATE sls.tbl_outsider_occupation_rate SET ta_rate = 0 WHERE contac_id = 30;
  UPDATE sls.tbl_outsider_occupation_rate SET rate_on_production = 520
   WHERE contac_id = 30 AND prod_calc IN (1, 2, 3);
  DELETE FROM sls.tbl_worker_sublocation_table WHERE worker_subloca_id >= <first id printed>;

Usage:
  python run_ejm_cash_outsider_cont30_and_new_hires.py                 # dry run
  python run_ejm_cash_outsider_cont30_and_new_hires.py --commit
  python run_ejm_cash_outsider_cont30_and_new_hires.py --since 2026-06-01 --commit
"""
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
SINCE = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else "2026-08-01"
BRANCH = 29
CONT = 30
TA = 30

REPO = Path(__file__).resolve().parents[2]
ENV = {}
for line in (REPO / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       database="sls", autocommit=False, connect_timeout=30)
cur = conn.cursor()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TA_WHERE = f"contac_id = {CONT} AND is_active = 1 AND IFNULL(ta_rate, 0) <> {TA}"
FLAT_WHERE = (f"contac_id = {CONT} AND is_active = 1 AND prod_calc IN (1, 2, 3) "
              f"AND rate IS NOT NULL AND rate_on_production <> rate")
NEW_HIRES = f"""
    SELECT DISTINCT od.eb_id, TRIM(od.emp_code) AS emp_code
    FROM hrms_ed_official_details od
    JOIN hrms_ed_personal_details p ON p.eb_id = od.eb_id AND p.branch_id = {BRANCH}
    WHERE od.active = 1
      AND LEFT(TRIM(od.emp_code), 1) = BINARY 'T'
      AND EXISTS (SELECT 1 FROM daily_attendance da
                  WHERE da.eb_id = od.eb_id AND da.branch_id = {BRANCH}
                    AND da.is_active = 1 AND da.attendance_date >= '{SINCE}')
      AND NOT EXISTS (SELECT 1 FROM tbl_worker_sublocation_table w WHERE w.eb_id = od.eb_id)
"""

cur.execute(f"SELECT COUNT(*) FROM tbl_outsider_occupation_rate WHERE {TA_WHERE}")
ta_n = cur.fetchone()[0]
cur.execute(f"""SELECT occupation_rate_id, desig_id, rate_on_production, rate
                FROM tbl_outsider_occupation_rate WHERE {FLAT_WHERE} ORDER BY desig_id""")
flat = cur.fetchall()
cur.execute(NEW_HIRES + " ORDER BY emp_code")
hires = cur.fetchall()

print(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (counts only, no writes)'}   since: {SINCE}")
print(f"  1. contractor {CONT} rows to get ta_rate {TA}: {ta_n}")
print(f"  2. contractor {CONT} flat rows to pay 'rate' (id, desig, rop -> rate): "
      f"{[(r[0], r[1], r[2], float(r[3])) for r in flat]}")
print(f"  3. branch {BRANCH} T workers to add as 'T': {len(hires)}")
print(f"     {[h[1] for h in hires]}")

if not COMMIT:
    print("  dry run — nothing written. Re-run with --commit.")
    sys.exit(0)

cur.execute(f"UPDATE tbl_outsider_occupation_rate SET ta_rate = {TA} WHERE {TA_WHERE}")
print("  1. ta_rate rows updated:", cur.rowcount)
cur.execute(f"UPDATE tbl_outsider_occupation_rate SET rate_on_production = rate WHERE {FLAT_WHERE}")
print("  2. flat rows updated:", cur.rowcount)
cur.execute("SELECT IFNULL(MAX(worker_subloca_id), 0) FROM tbl_worker_sublocation_table")
first_id = cur.fetchone()[0] + 1
cur.execute(f"""INSERT INTO tbl_worker_sublocation_table (eb_id, subloca_id, daily_others_pay_duration)
                SELECT h.eb_id, NULL, 'T' FROM ({NEW_HIRES}) h""")
print(f"  3. workers added: {cur.rowcount}  (worker_subloca_id >= {first_id})")
conn.commit()
print("  committed.")
