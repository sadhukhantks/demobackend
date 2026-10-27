"""
Deactivate duplicate ACTIVE (employee, component) rows of one sls pay scheme in sls.pay_employee_structure.

Why: the 2026-08-15 migration merged co-1 and co-106 legacy clones of one person onto a single sls eb (many-to-one
_map_pay_eb), so some employees carry every component twice. The sync script's verification refuses to commit while
any duplicate pair exists. Keeps the lowest ID of each pair; aborts if the rows of a pair disagree on a value field,
because then the choice changes pay.

Writes by ID only (both structure tables carry just PRIMARY(ID)). Backup + rollback:
    UPDATE sls.pay_employee_structure SET STATUS = 1
     WHERE ID IN (SELECT ID FROM vowsls_to_sls_transfer._bak_dedupe_struct_<S>);

Usage:
    python run_dedupe_pay_employee_structure.py --sls 3096            # dry run
    python run_dedupe_pay_employee_structure.py --sls 3096 --commit
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import pymysql

ap = argparse.ArgumentParser()
ap.add_argument("--sls", type=int, required=True, help="sls PAYSCHEME_ID")
ap.add_argument("--commit", action="store_true")
A = ap.parse_args()
BAK = f"vowsls_to_sls_transfer._bak_dedupe_struct_{A.sls}"

ENV = {}
for line in (Path(__file__).resolve().parents[2] / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v
conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"], password=ENV["DATABASE_PASSWORD"],
                       port=int(ENV["DATABASE_PORT"]), autocommit=False, connect_timeout=30, read_timeout=1800)
cur = conn.cursor(pymysql.cursors.DictCursor)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

cur.execute("""select ID, EMPLOYEEID, COMPONENT_ID, AMOUNT, EFFECTIVE_FROM, ENDS_ON from sls.pay_employee_structure
               where PAYSCHEME_ID = %s and STATUS = 1""", (A.sls,))
pairs = defaultdict(list)
for r in cur.fetchall():
    pairs[(r["EMPLOYEEID"], r["COMPONENT_ID"])].append(r)

extra, conflicts = [], []
for key, rows in pairs.items():
    if len(rows) < 2:
        continue
    rows.sort(key=lambda r: r["ID"])
    if len({(r["AMOUNT"], r["EFFECTIVE_FROM"], r["ENDS_ON"]) for r in rows}) > 1:
        conflicts.append(rows)
    extra += [r["ID"] for r in rows[1:]]

emps = sorted({k[0] for k, rows in pairs.items() if len(rows) > 1})
print(f"scheme {A.sls}: {sum(map(len, pairs.values()))} active rows, {len(pairs)} pairs; "
      f"duplicate pairs {sum(len(r) > 1 for r in pairs.values())} on employees {emps}; rows to deactivate {len(extra)}")
if conflicts:
    for rows in conflicts[:20]:
        print("   CONFLICT", rows)
    sys.exit("duplicate rows disagree on a value - pick the right row by hand, nothing written")
if not extra or not A.commit:
    print("Dry run - nothing written." if extra else "No duplicates.")
    sys.exit(0)

marks = ",".join(["%s"] * len(extra))
cur.execute(f"create table if not exists {BAK} like sls.pay_employee_structure")  # implicit commit; nothing pending
cur.execute(f"insert ignore into {BAK} select * from sls.pay_employee_structure where ID in ({marks})", extra)
backed = cur.rowcount
updated = cur.execute(f"update sls.pay_employee_structure set STATUS = 0, updated_date_time = now() "
                      f"where STATUS = 1 and ID in ({marks})", extra)
cur.execute("""select count(*) n from (select 1 from sls.pay_employee_structure where PAYSCHEME_ID = %s and STATUS = 1
               group by EMPLOYEEID, COMPONENT_ID having count(*) > 1) d""", (A.sls,))
left = cur.fetchone()["n"]
if left or updated != len(extra):
    conn.rollback()
    sys.exit(f"VERIFICATION FAILED (updated {updated} of {len(extra)}, duplicate pairs left {left}) - rolled back")
cur.execute("""insert into vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
               values (%s, 'committed', now(), now(), %s)""",
            (f"dedupe_structure_{A.sls}", json.dumps({"deactivated_ids": extra, "backup": BAK, "backed_up": backed})))
conn.commit()
print(f"COMMITTED: deactivated {updated} rows, backed up {backed} to {BAK}, duplicate pairs left {left}")
