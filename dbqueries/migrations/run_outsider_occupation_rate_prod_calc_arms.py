"""
Make sls.tbl_outsider_occupation_rate drive the EJM Cash/Outsider production prep
(`/hrmsMasters/ejm_cash_outsider_process`) instead of code constants.

prod_calc now names the production arm a designation is measured by:
  0 / NULL  no production arm (occupation rate only, used by the payment build)
  1, 2, 3   flat time-rated — always rate_on_production
  4         helper — production of the frames the worker was assigned to
  5         spinner — the worker's own eb-stamped doffs
  6         winder — the worker's winding doff production
  44        reliever (legacy arm commented out, not processed)
rate_on_production = rate when production meets standard, rate = when it misses.
contac_id = the contractor; a row with contac_id NULL applies to every contractor
that has no row of its own for that designation.

CHANGES
  1. spinner rows (desig 50, 195, 213, contac_id 10): prod_calc 4 -> 5
  2. winder rows (desig 199, 200, 201, 202, 330, contac_id 10): prod_calc 4 -> 6
  3. new rows for winding by any other contractor (legacy "winding others"):
     desig 199, 200, 201, 202, 330, contac_id NULL, prod_calc 6, 520 / 400
Piecer (51, 196) keeps the table's 600 / 490 (user decision 2026-09-14; legacy
code paid 520 / 390).

Re-running is a no-op (updates are idempotent, inserts guarded by NOT EXISTS).

ROLLBACK:
  UPDATE sls.tbl_outsider_occupation_rate SET prod_calc = 4
   WHERE contac_id = 10 AND desig_id IN (50,195,213,199,200,201,202,330);
  DELETE FROM sls.tbl_outsider_occupation_rate
   WHERE contac_id IS NULL AND prod_calc = 6;

Usage:
  python run_outsider_occupation_rate_prod_calc_arms.py            # dry run
  python run_outsider_occupation_rate_prod_calc_arms.py --commit
"""
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
SPINNER = (50, 195, 213)
WINDER = (199, 200, 201, 202, 330)

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


def show(title):
    cur.execute("""SELECT occupation_rate_id, desig_id, contac_id, prod_calc,
                          rate_on_production, rate, is_active
                   FROM tbl_outsider_occupation_rate
                   WHERE desig_id IN %s ORDER BY desig_id, occupation_rate_id""",
                (SPINNER + WINDER,))
    print(title)
    for r in cur.fetchall():
        print("  ", r)


show(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'} — before:")

cur.execute("""UPDATE tbl_outsider_occupation_rate SET prod_calc = 5
               WHERE contac_id = 10 AND desig_id IN %s AND prod_calc = 4""", (SPINNER,))
print("spinner rows recoded:", cur.rowcount)
cur.execute("""UPDATE tbl_outsider_occupation_rate SET prod_calc = 6
               WHERE contac_id = 10 AND desig_id IN %s AND prod_calc = 4""", (WINDER,))
print("winder rows recoded :", cur.rowcount)
inserted = 0
for d in WINDER:
    cur.execute("""INSERT INTO tbl_outsider_occupation_rate
                       (desig_id, rate, is_active, wef_date, status,
                        rate_on_production, prod_calc, contac_id)
                   SELECT %s, 400, 1, CURDATE(), 3, 520, 6, NULL FROM DUAL
                   WHERE NOT EXISTS (SELECT 1 FROM tbl_outsider_occupation_rate
                                     WHERE desig_id = %s AND contac_id IS NULL
                                       AND prod_calc = 6 AND is_active = 1)""", (d, d))
    inserted += cur.rowcount
print("winding-others rows inserted:", inserted)

show("after:")
if COMMIT:
    conn.commit()
    print("committed.")
else:
    conn.rollback()
    print("dry run — rolled back. Re-run with --commit.")
