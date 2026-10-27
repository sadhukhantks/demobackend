"""Assign pay schemes to sls branch 87 (Limelight / NJM) from vowsls company 1.

vowsls company_id = 1 is the same mill as sls branch_id = 87, so the employee's
pay-scheme assignment carries across. Employees are matched on emp_code (a
string, stable across the two id spaces); pay schemes are matched on NAME within
sls co_id = 106, because every company in sls holds its own copy of the same
scheme list under different ids (njm_main_payroll is 15 for one company and 2015
for another).

Existing assignments are OVERWRITTEN: an employee already carrying a different
active scheme has that row deactivated (STATUS = 0) and the vowsls one inserted.
An employee already on the same scheme is left completely alone, so re-running
this is a no-op rather than a churn of history rows.

Usage (from the backend repo root, with the repo venv):

    ./vowerp3be/Scripts/python.exe dbqueries/migrate_njm_payschemes_branch87.py
    ./vowerp3be/Scripts/python.exe dbqueries/migrate_njm_payschemes_branch87.py --apply

Without --apply it only reports. Rollback for a run is printed at the end.
"""
import argparse
import os
import sys
from collections import Counter

import pymysql
from dotenv import dotenv_values

SOURCE_DB = "vowsls"
SOURCE_COMPANY = 1
TARGET_DB = "sls"
TARGET_BRANCH = 87
TARGET_CO_ID = 106

# DISTINCT: tbl_hrms_ed_official_details carries duplicate active rows for the
# same eb_id, so without it one legacy employee yields two identical source rows
# and the loop below — which reads `current` once, before it starts — appends the
# same (eb_id, scheme_id) to `insert` twice.
SOURCE_SQL = """
    SELECT DISTINCT TRIM(o.emp_code) AS emp_code, ps.NAME AS scheme_name
    FROM vowsls.tbl_pay_employee_payscheme pe
    JOIN vowsls.tbl_hrms_ed_official_details o
         ON o.eb_id = pe.EMPLOYEEID AND o.is_active = 1
    JOIN vowsls.tbl_hrms_ed_personal_details p
         ON p.eb_id = pe.EMPLOYEEID AND p.company_id = %s
    LEFT JOIN vowsls.tbl_pay_scheme ps ON ps.ID = pe.PAY_SCHEME_ID
    WHERE pe.STATUS = 1
"""

TARGET_EMPLOYEES_SQL = """
    SELECT TRIM(od.emp_code) AS emp_code, od.eb_id
    FROM hrms_ed_official_details od
    JOIN hrms_ed_personal_details pd ON pd.eb_id = od.eb_id AND pd.active = 1
    WHERE od.active = 1 AND pd.branch_id = %s
"""

TARGET_SCHEMES_SQL = """
    SELECT payscheme_name, payscheme_id FROM pay_scheme_master WHERE co_id = %s
"""

CURRENT_ASSIGNMENTS_SQL = """
    SELECT t.ID, t.EMPLOYEEID, t.PAY_SCHEME_ID
    FROM pay_employee_payscheme t
    JOIN hrms_ed_personal_details pd
         ON pd.eb_id = t.EMPLOYEEID AND pd.branch_id = %s
    WHERE t.STATUS = 1
"""


def connect(cfg, database):
    return pymysql.connect(
        host=cfg["DATABASE_HOST"], port=int(cfg["DATABASE_PORT"]),
        user=cfg["DATABASE_USER"], password=cfg["DATABASE_PASSWORD"],
        database=database, cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=60,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = dotenv_values(os.path.join(root, "env", "database.env"))
    conn = connect(cfg, TARGET_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(SOURCE_SQL, (SOURCE_COMPANY,))
            source = cur.fetchall()
            cur.execute(TARGET_EMPLOYEES_SQL, (TARGET_BRANCH,))
            employees = {r["emp_code"]: r["eb_id"] for r in cur.fetchall()}
            cur.execute(TARGET_SCHEMES_SQL, (TARGET_CO_ID,))
            schemes = {r["payscheme_name"]: r["payscheme_id"] for r in cur.fetchall()}
            cur.execute(CURRENT_ASSIGNMENTS_SQL, (TARGET_BRANCH,))
            current = {}
            for r in cur.fetchall():
                current.setdefault(r["EMPLOYEEID"], []).append(
                    (r["ID"], r["PAY_SCHEME_ID"]))

        print(f"source rows (vowsls co {SOURCE_COMPANY}): {len(source)}")
        print(f"branch {TARGET_BRANCH} employees: {len(employees)}")
        print(f"co {TARGET_CO_ID} pay schemes: {len(schemes)}")
        print(f"employees already assigned: {len(current)}\n")

        insert, deactivate, unchanged = [], [], 0
        skipped = Counter()
        skipped_detail = {}
        for row in source:
            eb_id = employees.get(row["emp_code"])
            if eb_id is None:
                skipped["no matching employee on the branch"] += 1
                continue
            name = row["scheme_name"]
            if not name:
                skipped["source row carries no scheme"] += 1
                continue
            scheme_id = schemes.get(name)
            if scheme_id is None:
                skipped[f"no co-{TARGET_CO_ID} scheme named {name!r}"] += 1
                skipped_detail.setdefault(name, []).append(row["emp_code"])
                continue
            existing = current.get(eb_id, [])
            if any(sid == scheme_id for _, sid in existing):
                unchanged += 1
                continue
            deactivate.extend(row_id for row_id, _ in existing)
            insert.append((eb_id, scheme_id))

        print(f"to insert          : {len(insert)}")
        print(f"to deactivate      : {len(deactivate)}  (replaced assignments)")
        print(f"already correct    : {unchanged}")
        for reason, n in skipped.most_common():
            print(f"skipped            : {n}  {reason}")
        for name, codes in skipped_detail.items():
            print(f"    {name!r}: {', '.join(sorted(codes)[:10])}"
                  f"{' ...' if len(codes) > 10 else ''}")

        if not args.apply:
            print("\nDry run — pass --apply to write.")
            return 0

        with conn.cursor() as cur:
            if deactivate:
                cur.executemany(
                    "UPDATE pay_employee_payscheme SET STATUS = 0 WHERE ID = %s",
                    [(i,) for i in deactivate])
            if insert:
                cur.executemany(
                    "INSERT INTO pay_employee_payscheme "
                    "(EMPLOYEEID, PAY_SCHEME_ID, STATUS) VALUES (%s, %s, 1)",
                    insert)
        conn.commit()
        print(f"\nApplied: {len(insert)} inserted, {len(deactivate)} deactivated.")
        print("Rollback for THIS run: re-activate the deactivated ids and delete "
              "the inserted rows —\n"
              "  UPDATE pay_employee_payscheme SET STATUS=1 WHERE ID IN (...);\n"
              "  DELETE FROM pay_employee_payscheme WHERE ID IN (...);\n"
              "Capture the ids from the run log before relying on this.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
