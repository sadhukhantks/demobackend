"""Import e:/ejmhands/biomap.xlsx into sls.tbl_master_bio_link_mst.

Matching is on emp_code only -- never on name. The sheet's names are too inconsistent
to filter on ("DilipHela" unspaced, "Venugopal" vs sls "TARUR VENLUGOPAL").

Columns are read by HEADER, not position: the sheet has been reissued with a different
layout (was EmployeeId|EmployeeName|EmployeeCode, now
EmployeeName|EmployeeCode|EmployeeCodeInDevice) and the device code moved. Reading by
position silently loaded the wrong column once already.

bio_dev_id comes from EmployeeCodeInDevice -- the employee's enrolled code on the
reader. The old sheet's "EmployeeId" was NOT that (it said 17 for BFSPL157 where the
device code is 509) and must not be used.

"if not exists" = skip when the (master_id, bio_dev_id) PAIR is already linked; an
employee legitimately holds several enrollments.

    python dbqueries/migrations/run_import_biomap_sls_link.py            # dry run
    python dbqueries/migrations/run_import_biomap_sls_link.py --commit
"""

import argparse
from collections import defaultdict
from pathlib import Path

import openpyxl
import pymysql
from dotenv import dotenv_values

XLSX = Path("e:/ejmhands/biomap.xlsx")
SKIPPED = Path("e:/ejmhands/biomap_sls_skipped.csv")
MATCH_TYPE = "E"

CODE_HEADERS = ("employeecode",)
DEV_HEADERS = ("employeecodeindevice",)  # deliberately NOT "employeeid" -- wrong column


def is_foreign_code(code: str) -> bool:
    """All-numeric codes are code3i/Vizag legacy ids, not sls emp_codes.

    They cannot be matched on emp_code: of those that resolve to exactly one active sls
    employee, every one checked was a DIFFERENT person -- '30250 GOPAL DAGA' resolves to
    KOYYAPU PYDI SURY, '30215 SOWMYA RAO' to KAREPU SA NARAYANA. Real sls emp_codes here
    are alphanumeric (ACPL030, BFSPL157, EJCL477, U0091). Override with --include-numeric.
    """
    return code.isdigit()


def read_sheet() -> list[tuple[str, str]]:
    """-> [(emp_code, device_code)], columns located by header name."""
    grid = list(openpyxl.load_workbook(XLSX, read_only=True)["Sheet1"].iter_rows(values_only=True))
    header = [str(h or "").strip().lower().replace(" ", "") for h in grid[0]]

    def col(names: tuple[str, ...], label: str) -> int:
        for i, h in enumerate(header):
            if h in names:
                return i
        raise SystemExit(f"sheet has no {label} column; headers = {grid[0]}")

    ci, di = col(CODE_HEADERS, "emp_code"), col(DEV_HEADERS, "device code")
    return [
        (str(r[ci]).strip(), str(r[di]).strip())
        for r in grid[1:]
        if r[ci] is not None and r[di] is not None
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--include-numeric", action="store_true", help="also import code3i numeric codes")
    args = ap.parse_args()

    rows = read_sheet()

    env = dotenv_values("env/database.env")
    conn = pymysql.connect(
        host=env["DATABASE_HOST"],
        port=int(env["DATABASE_PORT"]),
        user=env["DATABASE_USER"],
        password=env["DATABASE_PASSWORD"],
        database="sls",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    cur = conn.cursor()

    codes = sorted({c for c, _ in rows})
    ph = ",".join(["%s"] * len(codes))
    cur.execute(
        f"""SELECT emp_code, eb_id FROM hrms_ed_official_details
            WHERE emp_code IN ({ph}) AND active = 1""",
        codes,
    )
    by_code = defaultdict(set)
    for r in cur.fetchall():
        by_code[r["emp_code"]].add(r["eb_id"])

    cur.execute("SELECT master_id, bio_dev_id FROM tbl_master_bio_link_mst")
    existing = cur.fetchall()
    linked_pair = {(r["master_id"], r["bio_dev_id"]) for r in existing}
    dev_owner = {r["bio_dev_id"]: r["master_id"] for r in existing}

    insert, skipped = [], []
    for code, dev_code in rows:
        ebs = by_code.get(code, set())
        if not dev_code.isdigit():
            skipped.append((code, dev_code, "device code not numeric"))
        elif is_foreign_code(code) and not args.include_numeric:
            skipped.append((code, dev_code, "numeric code3i/Vizag id, not an sls emp_code"))
        elif not ebs:
            skipped.append((code, dev_code, "emp_code not found / not active in sls"))
        elif len(ebs) > 1:
            skipped.append((code, dev_code, f"ambiguous: {len(ebs)} active eb_id ({','.join(map(str, sorted(ebs)))})"))
        else:
            eb_id, dev = next(iter(ebs)), int(dev_code)
            if (eb_id, dev) in linked_pair:
                skipped.append((code, dev_code, f"already linked: eb {eb_id} -> {dev}"))
            elif dev_owner.get(dev, eb_id) != eb_id:
                skipped.append((code, dev_code, f"device code {dev} belongs to eb {dev_owner[dev]}"))
            else:
                insert.append((eb_id, dev, code))
                linked_pair.add((eb_id, dev))
                dev_owner[dev] = eb_id

    SKIPPED.write_text(
        "\n".join(
            ['"EmployeeCode","EmployeeCodeInDevice","reason"']
            + [",".join(f'"{v}"' for v in s) for s in skipped]
        ),
        encoding="utf-8",
    )
    print(f"sheet {len(rows)} | insert {len(insert)} | skipped {len(skipped)} -> {SKIPPED}")
    for eb, dev, code in insert:
        print(f"  master_id={eb:<6} bio_dev_id={dev:<7} {code}")

    if not args.commit:
        print("DRY RUN — re-run with --commit to write")
        return 0

    cur.executemany(
        "INSERT INTO tbl_master_bio_link_mst (master_id, match_type, bio_dev_id) VALUES (%s, %s, %s)",
        [(eb, MATCH_TYPE, dev) for eb, dev, _ in insert],
    )
    conn.commit()
    print(f"COMMITTED {len(insert)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
