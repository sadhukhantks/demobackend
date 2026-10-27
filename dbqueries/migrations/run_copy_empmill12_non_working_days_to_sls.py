"""
Copy EMPMILL12.tbl_non_working_days (legacy EJM non-working / off days) into sls.

Creates sls.tbl_non_working_days with the legacy structure (CREATE TABLE ... LIKE)
and copies every row verbatim, ids included. Re-runnable: rows already present
(same non_working_days_id) are skipped.

  python run_copy_empmill12_non_working_days_to_sls.py            # dry run
  python run_copy_empmill12_non_working_days_to_sls.py --commit
"""
import os
import sys

import pymysql
from dotenv import load_dotenv

load_dotenv(r"D:\vownextjs\vowerp3be\.env")
commit = "--commit" in sys.argv

conn = pymysql.connect(host=os.getenv("DATABASE_HOST"), user=os.getenv("DATABASE_USER"),
                       password=os.getenv("DATABASE_PASSWORD"), port=int(os.getenv("DATABASE_PORT")),
                       autocommit=False, connect_timeout=30)
cur = conn.cursor()

cur.execute("CREATE TABLE IF NOT EXISTS sls.tbl_non_working_days LIKE EMPMILL12.tbl_non_working_days")
inserted = cur.execute("INSERT IGNORE INTO sls.tbl_non_working_days SELECT * FROM EMPMILL12.tbl_non_working_days")
cur.execute("SELECT COUNT(*) FROM EMPMILL12.tbl_non_working_days")
source = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM sls.tbl_non_working_days")
target = cur.fetchone()[0]
print(f"source {source}, inserted {inserted}, sls now {target}")

if commit:
    conn.commit()  # the CREATE above is DDL and already auto-committed
    print("committed")
else:
    conn.rollback()
    print("dry run - rows rolled back (table was created: DDL auto-commits)")
conn.close()
