"""Import e:/ejmhands/biomap.xlsx into vowsls.bio_metricempmap.

The sheet has (EmployeeId, EmployeeName, EmployeeCode) but NO device column, and
emp_bio_device_id is only unique per device (2798 distinct ids over 3052 rows), so
--device is required. company_id is taken from the employee, not passed in.

Only rows whose EmployeeCode resolves to exactly one active eb_id are imported;
everything else is written to skipped.csv with a reason.

    python dbqueries/migrations/run_import_biomap_ejmhands.py --device 10          # dry run
    python dbqueries/migrations/run_import_biomap_ejmhands.py --device 10 --commit
"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import openpyxl
import pymysql
from dotenv import dotenv_values

XLSX = Path("e:/ejmhands/biomap.xlsx")
SKIPPED = Path("e:/ejmhands/biomap_skipped.csv")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, required=True, help="bio_device_master.device_id")
    ap.add_argument("--created-by", type=int, default=0)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--force", action="store_true", help="import despite id-ownership conflicts")
    args = ap.parse_args()

    ws = openpyxl.load_workbook(XLSX, read_only=True)["Sheet1"]
    rows = [
        (str(a).strip(), str(b or "").strip(), str(c).strip())
        for a, b, c in list(ws.iter_rows(values_only=True))[1:]
        if a is not None and c is not None
    ]

    env = dotenv_values("env/database.env")
    conn = pymysql.connect(
        host=env["DATABASE_HOST"],
        port=int(env["DATABASE_PORT"]),
        user=env["DATABASE_USER"],
        password=env["DATABASE_PASSWORD"],
        database="vowsls",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    cur = conn.cursor()

    codes = [c for _, _, c in rows]
    ph = ",".join(["%s"] * len(codes))
    cur.execute(
        f"""SELECT o.emp_code, o.eb_id, o.is_active, p.company_id
            FROM tbl_hrms_ed_official_details o
            JOIN tbl_hrms_ed_personal_details p ON p.eb_id = o.eb_id
            WHERE o.emp_code IN ({ph})""",
        codes,
    )
    by_code = defaultdict(set)
    for r in cur.fetchall():
        if r["is_active"] == 1:
            by_code[r["emp_code"]].add((r["eb_id"], r["company_id"]))

    # "if not exists" is per device: the same emp_bio_device_id is reused across devices,
    # and the same employee legitimately has one mapping per device they punch on.
    cur.execute(
        """SELECT m.eb_id, m.emp_bio_device_id, o.emp_code
           FROM bio_metricempmap m
           LEFT JOIN tbl_hrms_ed_official_details o ON o.eb_id = m.eb_id
           WHERE m.bio_device_id = %s""",
        (args.device,),
    )
    existing = cur.fetchall()
    have_eb = {r["eb_id"] for r in existing}
    have_dev = {str(r["emp_bio_device_id"]).strip() for r in existing}

    # An id already on this device must denote the SAME employee, or the sheet is not
    # this device's numbering and importing it would misattribute punches. No Excel id
    # agrees with any existing device (checked 1,3,4,7,9,10,11), hence this guard.
    owner = {str(r["emp_bio_device_id"]).strip(): r["emp_code"] for r in existing}
    conflicts = [
        (dev_id, code, owner[dev_id])
        for dev_id, _, code in rows
        if dev_id in owner and owner[dev_id] != code
    ]
    if conflicts and not args.force:
        print(f"ABORT: {len(conflicts)} id(s) on device {args.device} belong to other employees:")
        for dev_id, code, other in conflicts[:10]:
            print(f"  id {dev_id}: sheet says {code}, device says {other}")
        print("The sheet is not this device's numbering. Use the correct device, or --force.")
        return 1

    insert, skipped = [], []
    for dev_id, name, code in rows:
        matches = by_code.get(code)
        if not matches:
            skipped.append((dev_id, name, code, "emp_code not found / not active in vowsls"))
        elif len(matches) > 1:
            ebs = ",".join(str(e) for e, _ in sorted(matches))
            skipped.append((dev_id, name, code, f"ambiguous: {len(matches)} active eb_id ({ebs})"))
        else:
            (eb_id, company_id), = matches
            if eb_id in have_eb:
                skipped.append((dev_id, name, code, f"eb_id {eb_id} already mapped on device {args.device}"))
            elif dev_id in have_dev:
                skipped.append((dev_id, name, code, f"emp_bio_device_id {dev_id} already used on device {args.device}"))
            else:
                insert.append((eb_id, company_id, dev_id))
                have_eb.add(eb_id)
                have_dev.add(dev_id)

    SKIPPED.write_text(
        "\n".join(
            [",".join(["EmployeeId", "EmployeeName", "EmployeeCode", "reason"])]
            + [",".join(f'"{v}"' for v in s) for s in skipped]
        ),
        encoding="utf-8",
    )
    print(f"excel {len(rows)} | insert {len(insert)} | skipped {len(skipped)} -> {SKIPPED}")
    for eb, co, dv in insert:
        print(f"  eb_id={eb} company_id={co} emp_bio_device_id={dv} bio_device_id={args.device}")

    if not args.commit:
        print("DRY RUN — re-run with --commit to write")
        return 0

    cur.executemany(
        """INSERT INTO bio_metricempmap
           (active_status, bio_device_id, company_id, created_by, eb_id, emp_bio_device_id)
           VALUES (1, %s, %s, %s, %s, %s)""",
        [(args.device, co, args.created_by, eb, dv) for eb, co, dv in insert],
    )
    conn.commit()
    print(f"COMMITTED {len(insert)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
