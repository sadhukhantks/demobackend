"""Seed tbl_wages_data_payschm_map_table (wages sheet column -> pay component)
from the legacy EMPMILL12 map, translating every id by CODE.

Legacy keyed the map on vowsls scheme ids and component ids, which is why
seed_njm_wages_masters.py left it alone. Here both sides are matched by their
codes instead, so nothing is copied by id:

    scheme     vowsls.tbl_pay_scheme.NAME        -> pay_scheme_master.payscheme_code
                                                    within --co-id
    component  vowsls.tbl_pay_components.CODE    -> pay_components.CODE

Rows whose scheme or component has no counterpart are reported and skipped;
rows already present (same scheme + data_column_no) are skipped. Re-running
is safe.

Usage (from the backend repo root, with the repo venv):

    .venv/Scripts/python.exe dbqueries/seed_njm_wages_payschm_map.py --database sls --co-id 106
    .venv/Scripts/python.exe dbqueries/seed_njm_wages_payschm_map.py --database sls --co-id 106 --apply

The legacy source lives on the same MySQL host as the tenants (EMPMILL12 and
vowsls), which is what makes the cross-database read possible.
"""
import argparse
import os

import pymysql
from dotenv import dotenv_values

SOURCE_DB = "EMPMILL12"
LEGACY_PAYROLL_DB = "vowsls"


def connect(cfg, database):
    return pymysql.connect(
        host=cfg["DATABASE_HOST"], port=int(cfg["DATABASE_PORT"]),
        user=cfg["DATABASE_USER"], password=cfg["DATABASE_PASSWORD"],
        database=database, cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=30,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True, help="target tenant DB (e.g. sls)")
    ap.add_argument("--co-id", type=int, required=True,
                    help="company whose schemes receive the map (NJM on sls is 106)")
    ap.add_argument("--apply", action="store_true",
                    help="actually insert; without it this is a dry run")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = dotenv_values(os.path.join(root, "env", "database.env"))

    src = connect(cfg, SOURCE_DB)
    dst = connect(cfg, args.database)
    try:
        with src.cursor() as c:
            c.execute(f"""
                SELECT s.NAME AS scheme_code, m.data_column_no, pc.CODE AS component_code,
                       m.payscheme_id AS legacy_scheme_id
                FROM tbl_wages_data_payschm_map_table m
                JOIN {LEGACY_PAYROLL_DB}.tbl_pay_scheme s ON s.id = m.payscheme_id
                JOIN {LEGACY_PAYROLL_DB}.tbl_pay_components pc ON pc.ID = m.payscheme_component_id
                WHERE m.data_column_no IS NOT NULL
                ORDER BY m.payscheme_id, m.wages_data_payschm_map_id
            """)
            legacy = c.fetchall()

        with dst.cursor() as c:
            c.execute("""SELECT payscheme_id, payscheme_code FROM pay_scheme_master
                         WHERE co_id = %s""", (args.co_id,))
            schemes = {r["payscheme_code"]: int(r["payscheme_id"]) for r in c.fetchall()}
            c.execute("SELECT ID, CODE FROM pay_components WHERE CODE IS NOT NULL")
            components = {r["CODE"]: int(r["ID"]) for r in c.fetchall()}
            c.execute("SELECT payscheme_id, data_column_no FROM tbl_wages_data_payschm_map_table")
            present = {(int(r["payscheme_id"]), r["data_column_no"]) for r in c.fetchall()}

        to_insert, no_scheme, no_component, already = [], set(), set(), 0
        for r in legacy:
            scheme_id = schemes.get(r["scheme_code"])
            if scheme_id is None:
                no_scheme.add(r["scheme_code"])
                continue
            component_id = components.get(r["component_code"])
            if component_id is None:
                no_component.add(r["component_code"])
                continue
            if (scheme_id, r["data_column_no"]) in present:
                already += 1
                continue
            to_insert.append((scheme_id, r["data_column_no"], component_id))

        print(f"{len(legacy)} legacy rows; {len(to_insert)} to insert, {already} already present")
        by_scheme = {}
        for scheme_id, col, _ in to_insert:
            by_scheme.setdefault(scheme_id, []).append(col)
        for scheme_id, cols in sorted(by_scheme.items()):
            print(f"  scheme {scheme_id}: {len(cols)} columns")
        if no_scheme:
            print(f"  skipped, no scheme with this code in co {args.co_id}: {sorted(no_scheme)}")
        if no_component:
            print(f"  skipped, no component with this CODE: {sorted(no_component)}")

        if to_insert and args.apply:
            with dst.cursor() as c:
                c.executemany("""INSERT INTO tbl_wages_data_payschm_map_table
                                 (payscheme_id, data_column_no, payscheme_component_id)
                                 VALUES (%s, %s, %s)""", to_insert)
            dst.commit()
            print(f"inserted {len(to_insert)} rows into {args.database}")
        elif to_insert:
            print("dry run — re-run with --apply to insert")
    finally:
        src.close()
        dst.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
