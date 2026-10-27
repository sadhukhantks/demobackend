"""Seed the two code-keyed NJM wages masters from the legacy EMPMILL12 schema.

Pairs with dbqueries/migrations/njm_wages_masters.sql, which creates the tables.
Only these two travel: both are keyed by a STRING code, so they carry across
without going through the eb_id map.

    tbl_wages_quality_master   keyed by q_code
    tbl_njm_food_allowance     keyed by eb_no (an emp_code)

tbl_company_advance and tbl_wages_data_payschm_map_table are deliberately NOT
seeded — they key on legacy eb_id / payscheme_id / component_id, a different id
space in the tenant DBs, and copying them straight over would attach advances
and pay components to the wrong people.

Usage (from the backend repo root, with the repo venv):

    ./vowerp3be/Scripts/python.exe dbqueries/seed_njm_wages_masters.py --database sls
    ./vowerp3be/Scripts/python.exe dbqueries/seed_njm_wages_masters.py --database sls --apply

Without --apply it only reports what it would insert. Re-running is safe: rows
whose code already exists in the target are skipped, never duplicated.
"""
import argparse
import os
import sys

import pymysql
from dotenv import dotenv_values

SOURCE_DB = "EMPMILL12"

TABLES = (
    # (table, natural key column, columns to copy)
    ("tbl_wages_quality_master", "q_code",
     ("q_code", "quality", "rate1", "rate2", "unit", "std_rate_pu",
      "curr_rate_pu", "master_dept_id", "min_prod", "max_prod", "target_prod",
      "is_active", "company_id", "branch_id", "cnt_rate")),
    ("tbl_njm_food_allowance", "eb_no",
     ("eb_no", "da_rate", "fa_rate")),
)


def connect(cfg, database):
    return pymysql.connect(
        host=cfg["DATABASE_HOST"], port=int(cfg["DATABASE_PORT"]),
        user=cfg["DATABASE_USER"], password=cfg["DATABASE_PASSWORD"],
        database=database, cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=30,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True,
                    help="target tenant DB (e.g. sls, dev3)")
    ap.add_argument("--apply", action="store_true",
                    help="actually insert; without it this is a dry run")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = dotenv_values(os.path.join(root, "env", "database.env"))

    src = connect(cfg, SOURCE_DB)
    dst = connect(cfg, args.database)
    total = 0
    try:
        for table, key, columns in TABLES:
            with dst.cursor() as c:
                c.execute("""SELECT COUNT(*) n FROM information_schema.tables
                             WHERE table_schema = %s AND table_name = %s""",
                          (args.database, table))
                if not c.fetchone()["n"]:
                    print(f"{table}: MISSING in {args.database} — "
                          "apply njm_wages_masters.sql first")
                    return 1
                c.execute(f"SELECT `{key}` FROM `{table}`")
                have = {str(r[key]) for r in c.fetchall()}

            with src.cursor() as c:
                c.execute(f"SELECT {', '.join(columns)} FROM `{table}`")
                rows = c.fetchall()

            fresh = [r for r in rows if str(r[key]) not in have]
            print(f"{table}: {len(rows)} in {SOURCE_DB}, {len(have)} already in "
                  f"{args.database}, {len(fresh)} to insert")
            if fresh and args.apply:
                placeholders = ", ".join(["%s"] * len(columns))
                sql = (f"INSERT INTO `{table}` ({', '.join(columns)}) "
                       f"VALUES ({placeholders})")
                with dst.cursor() as c:
                    c.executemany(sql, [tuple(r[col] for col in columns)
                                        for r in fresh])
                total += len(fresh)
        if args.apply:
            dst.commit()
            print(f"\nInserted {total} rows into {args.database}")
        else:
            print("\nDry run — pass --apply to insert")
    finally:
        src.close()
        dst.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
