"""Load the EJM "hands" CUMLDATA.DBF (FoxPro fortnight cumulative file) into
cumulative_data_table.

The DBF columns are the legacy Ejmcumld.Csv columns (Data_entry::exportdbfdata,
holget=20) in the same order as the table: FNEDATE is the fortnight-end date
(date_to) and EBNO is the employee code -> eb_id via
hrms_ed_official_details.emp_code. date_from is derived: the 1st for a
15th-end row, the 16th for a month-end row (EJM pays fortnightly).

    python dbqueries/load_ejm_cumldata_dbf.py --dbf E:\\ejmhands\\CUMLDATA.DBF.DBF --database sls [--apply]

Dry-run by default; --apply inserts, and only into an empty table.
"""
import argparse
import calendar
import struct
import sys
from decimal import Decimal

import pymysql
from dotenv import dotenv_values

DBF_FIELDS = ["FNEDATE", "EBNO", "WHOURS", "NSHRS", "FESTHRS", "STLDAYS", "PFGROSS", "PFCONT",
              "BFAMT", "CFAMT", "FEST_AMT", "PUJAADVBAL", "COLNBAL", "TOTALEARN"]
TABLE_COLS = ["working_hours", "night_shift_hours", "fest_hours", "stl_days", "pf_gross",
              "pf_contibution", "bf_amount", "cf_amount", "fest_amount", "puja_adv_bal",
              "co_loan_bal", "total_earnings"]


def read_dbf(path):
    """dBase III: 32-byte header, 32-byte field descriptors, then fixed-width rows."""
    with open(path, "rb") as f:
        nrec, hlen, rlen = struct.unpack("<IHH", f.read(32)[4:12])
        fields = []
        while True:
            fd = f.read(32)
            if fd[0] == 0x0D:
                break
            fields.append((fd[:11].split(b"\0")[0].decode(), fd[16]))
        assert [n for n, _ in fields] == DBF_FIELDS, [n for n, _ in fields]
        f.seek(hlen)
        for _ in range(nrec):
            rec = f.read(rlen)
            if len(rec) < rlen or rec[0:1] == b"*":  # short tail / deleted
                continue
            pos, vals = 1, []
            for _, ln in fields:
                vals.append(rec[pos:pos + ln].decode("latin1").strip())
                pos += ln
            yield vals


def period(fnedate):
    y, m, d = int(fnedate[:4]), int(fnedate[4:6]), int(fnedate[6:])
    if d == 15:
        start = 1
    elif d == calendar.monthrange(y, m)[1]:
        start = 16
    else:
        raise ValueError(f"FNEDATE {fnedate} is neither the 15th nor month-end")
    return f"{y:04d}-{m:02d}-{start:02d}", f"{y:04d}-{m:02d}-{d:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbf", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = dotenv_values(".env")
    cn = pymysql.connect(host=cfg["DATABASE_HOST"], port=int(cfg["DATABASE_PORT"]),
                         user=cfg["DATABASE_USER"], password=cfg["DATABASE_PASSWORD"],
                         database=args.database, connect_timeout=30)
    cur = cn.cursor()
    cur.execute("SELECT emp_code, eb_id FROM hrms_ed_official_details WHERE emp_code <> ''")
    eb_by_code = {}
    for code, eb in cur.fetchall():
        eb_by_code.setdefault(code.strip(), set()).add(eb)

    rows, blank, unmatched = [], 0, set()
    for vals in read_dbf(args.dbf):
        fnedate, ebno, nums = vals[0], vals[1], vals[2:]
        if not ebno:
            blank += 1
            continue
        ebs = eb_by_code.get(ebno)
        if not ebs or len(ebs) > 1:
            unmatched.add(ebno)
            continue
        date_from, date_to = period(fnedate)
        rows.append((date_from, date_to, next(iter(ebs)), *[Decimal(n or "0") for n in nums]))

    periods = sorted({r[1] for r in rows})
    print(f"rows={len(rows)} blank_ebno_skipped={blank} periods={len(periods)} "
          f"{periods[0] if periods else '-'}..{periods[-1] if periods else '-'} "
          f"employees={len({r[2] for r in rows})}")
    if unmatched:
        print(f"ABORT: {len(unmatched)} EBNO without a unique emp_code match: {sorted(unmatched)[:20]}")
        sys.exit(1)
    if not args.apply:
        print("dry-run; pass --apply to insert")
        return

    cur.execute("SELECT COUNT(*) FROM cumulative_data_table")
    if cur.fetchone()[0]:
        print("ABORT: cumulative_data_table is not empty")
        sys.exit(1)
    sql = (f"INSERT INTO cumulative_data_table (date_from, date_to, eb_id, {', '.join(TABLE_COLS)}) "
           f"VALUES ({', '.join(['%s'] * (3 + len(TABLE_COLS)))})")
    try:
        for i in range(0, len(rows), 5000):
            cur.executemany(sql, rows[i:i + 5000])
        cn.commit()
    except Exception:
        cn.rollback()
        raise
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT date_to), MIN(date_from), MAX(date_to) FROM cumulative_data_table")
    print("inserted; table now:", cur.fetchone())


if __name__ == "__main__":
    assert period("20220115") == ("2022-01-01", "2022-01-15")
    assert period("20240229") == ("2024-02-16", "2024-02-29")
    main()
