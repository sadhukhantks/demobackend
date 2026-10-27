"""
Put contractor 30 (AJAY SHAW) workers into sls.tbl_worker_sublocation_table so the
EJM Cash/Outsider payment build pays them (it pays only daily_others_pay_duration 'T').

  * eb_id 61832 (T20311): subloca_id 3, 'T' — updated if the row exists, else inserted.
  * every other active branch-29 employee with contractor_id 30 and no row yet:
    inserted with subloca_id 3, 'T'. Existing rows are never changed.

Default is a DRY RUN (lists, no writes). --commit applies it in one transaction.

ROLLBACK: DELETE FROM sls.tbl_worker_sublocation_table WHERE worker_subloca_id >= <first id printed>;
          (and, if 61832 already had a row, restore its old values printed below)

Usage:
  python run_worker_sublocation_cont30.py            # dry run
  python run_worker_sublocation_cont30.py --commit
"""
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
BRANCH, CONT, SUBLOCA, EB = 29, 30, 3, 61832

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

MISSING = f"""
    SELECT DISTINCT od.eb_id, TRIM(od.emp_code) AS emp_code
    FROM hrms_ed_official_details od
    JOIN hrms_ed_personal_details p ON p.eb_id = od.eb_id AND p.branch_id = {BRANCH}
    WHERE od.active = 1 AND od.contractor_id = {CONT}
      AND NOT EXISTS (SELECT 1 FROM tbl_worker_sublocation_table w WHERE w.eb_id = od.eb_id)
"""

cur.execute(f"SELECT worker_subloca_id, subloca_id, daily_others_pay_duration "
            f"FROM tbl_worker_sublocation_table WHERE eb_id = {EB}")
eb_row = cur.fetchall()
cur.execute(MISSING + " ORDER BY emp_code")
missing = cur.fetchall()
cur.execute("SELECT subloca_id, sub_location FROM tbl_sublocation WHERE subloca_id = %s", (SUBLOCA,))
loc = cur.fetchone()

print(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}   sublocation {loc}")
print(f"  eb {EB} current row(s): {eb_row or 'none — will be inserted'}")
print(f"  contractor {CONT} employees missing ({len(missing)}): {[m[1] for m in missing]}")

if not COMMIT:
    print("  dry run — nothing written. Re-run with --commit.")
    sys.exit(0)

if eb_row:
    cur.execute(f"UPDATE tbl_worker_sublocation_table SET subloca_id = {SUBLOCA}, "
                f"daily_others_pay_duration = 'T' WHERE eb_id = {EB}")
    print(f"  eb {EB} updated: {cur.rowcount}")
cur.execute("SELECT IFNULL(MAX(worker_subloca_id), 0) FROM tbl_worker_sublocation_table")
first_id = cur.fetchone()[0] + 1
cur.execute(f"""INSERT INTO tbl_worker_sublocation_table (eb_id, subloca_id, daily_others_pay_duration)
                SELECT m.eb_id, {SUBLOCA}, 'T' FROM ({MISSING}) m""")
print(f"  inserted: {cur.rowcount}  (worker_subloca_id >= {first_id})")
cur.execute(f"""SELECT COUNT(*) FROM tbl_worker_sublocation_table w
                JOIN hrms_ed_official_details od ON od.eb_id = w.eb_id AND od.active = 1
                WHERE od.contractor_id = {CONT} AND w.daily_others_pay_duration = 'T'""")
print(f"  contractor {CONT} workers now marked 'T': {cur.fetchone()[0]}")
conn.commit()
print("  committed.")
