"""Monthwise working / night-shift / festival hours for EJM pay schemes 1151 & 1125.

Builds the ATT (working_hours + day-OT), NSH (ns_hours + night-OT) and FES
(fest_hours) arms of the EJM wages data-collection process (src/hrms/ejmWagesData.py)
for every calendar month in 2026-02-01 .. 2026-09-15, employee-wise, into
sls.tbl_ejm_wages_data_collection. Both schemes are co 2 / branch 29 (EMPIRE_MAIN_PAYROLL
and EJM_VOUCHER_WORKER).

Why a script and not the EjmWagesProcess screen: the screen's /ejm_wages_data_build
only accepts periods that are an exact fortnight in forthnight_period_mst. The ask is
calendar months, so the fortnight/period validation is skipped and the canonical arm
SQL (imported, not retranscribed) is run directly per month.

Differences from the endpoint, on purpose:
  * Period = calendar month (Feb 01-28 ... Aug 01-31, Sep 01-15 partial), not a fortnight.
  * Clear-before-insert matches the month's EXACT (date_from, date_to) only -- NOT the
    endpoint's "any range inside" rule -- so the existing fortnightly Aug/Sep rows the
    payroll already consumed are left untouched. Re-runnable: a month's ATT/NSH/FES rows
    are replaced on each run.
  * period_id stays NULL (a calendar month is not a fortnight id).
  * The endpoint hard-fails on any unapproved attendance; here each month only reports its
    unapproved count. The arms count approved (status_id='3') attendance only -- Sep 2026 is
    entirely Open (status 1) today, so Sep yields 0 rows until it is approved, then re-run.

Usage:
    python run_ejm_monthwise_hours_1151_1125.py            # dry run (rolls back), prints plan + summary
    python run_ejm_monthwise_hours_1151_1125.py --commit   # write + employee-wise summary CSV
"""
import argparse
import calendar
import csv
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.config.db import get_tenant_engine  # noqa: E402
from src.hrms.constants import ATTENDANCE_APPROVED  # noqa: E402
from src.hrms.ejmWagesData import BUILD_ARMS  # noqa: E402

TENANT = "sls"
SCHEMES = (1151, 1125)
BRANCH_ID = 29
ARM_CODES = ("ATT", "NSH", "FES")          # working hours, night-shift+night-OT, festival
PERIOD_START = date(2026, 2, 1)
PERIOD_END = date(2026, 9, 15)             # last month is partial

# The arm builders this run owns, taken straight from the process (ATT, NSH-ns, NSH-ot, FES).
ARMS = [(code, label, build) for code, label, build in BUILD_ARMS if code in ARM_CODES]

ap = argparse.ArgumentParser()
ap.add_argument("--commit", action="store_true")
A = ap.parse_args()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def months(start: date, end: date):
    """Yield (date_from, date_to) per calendar month; the last month ends at `end`."""
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        last = date(y, m, calendar.monthrange(y, m)[1])
        df = date(y, m, 1) if (y, m) != (start.year, start.month) else start
        dt = min(last, end)
        yield df, dt
        m = 1 if m == 12 else m + 1
        y = y + 1 if m == 1 else y


def main():
    engine = get_tenant_engine(TENANT)
    db = Session(engine, autoflush=False)
    periods = list(months(PERIOD_START, PERIOD_END))
    print(f"Tenant {TENANT} | schemes {SCHEMES} | branch {BRANCH_ID} | arms {ARM_CODES}")
    print(f"{len(periods)} calendar months: " + ", ".join(f"{a}..{b}" for a, b in periods))
    print(f"MODE: {'COMMIT' if A.commit else 'DRY RUN (rollback)'}\n")

    grand = 0
    try:
        for ps in SCHEMES:
            for df, dt in periods:
                p = {"date_from": df.isoformat(), "date_to": dt.isoformat(),
                     "pay_scheme_id": ps, "branch_id": BRANCH_ID}
                # Transparency: how much attendance in this month is not yet approved
                # (the arms silently skip it). Open attendance => fewer/zero hours.
                appr = db.execute(text("""
                    SELECT SUM(da.status_id = :ok) ok, SUM(da.status_id IS NULL OR da.status_id <> :ok) bad
                    FROM daily_attendance da
                    JOIN pay_employee_payscheme t
                      ON t.EMPLOYEEID = da.eb_id AND t.PAY_SCHEME_ID = :pay_scheme_id AND t.STATUS = 1
                    WHERE da.is_active = 1 AND da.branch_id = :branch_id
                      AND da.attendance_date BETWEEN :date_from AND :date_to
                """), {**p, "ok": ATTENDANCE_APPROVED}).mappings().one()

                # Per-month savepoint: a clear-then-rebuild is only safe when the
                # rebuild actually produces rows. If it would clear existing rows and
                # insert nothing (e.g. Sep 2026, whose attendance is still Open), roll
                # the month back so the previously-built rows survive untouched.
                sp = db.begin_nested()
                cleared = db.execute(text("""
                    DELETE FROM tbl_ejm_wages_data_collection
                    WHERE pay_scheme_id = :pay_scheme_id
                      AND update_for IN :codes
                      AND date_from = :date_from AND date_to = :date_to
                """).bindparams(codes=ARM_CODES), p).rowcount

                counts = {label: db.execute(build(), p).rowcount for _, label, build in ARMS}
                ins = sum(counts.values())
                bad = int(appr["bad"] or 0)
                if ins == 0 and cleared > 0:
                    sp.rollback()
                    print(f"scheme {ps}  {df}..{dt}  approved_att={int(appr['ok'] or 0):>5}  "
                          f"SKIPPED: rebuild empty, {cleared} existing row(s) left intact"
                          + (f"  !! {bad} unapproved attendance" if bad else ""))
                    continue
                sp.commit()
                grand += ins
                warn = f"  !! {bad} unapproved attendance -> skipped" if bad else ""
                detail = ", ".join(f"{n} {lbl}" for lbl, n in counts.items() if n) or "nothing"
                print(f"scheme {ps}  {df}..{dt}  approved_att={int(appr['ok'] or 0):>5}  "
                      f"cleared={cleared:>4}  built: {detail}{warn}")

        if A.commit:
            db.commit()
            print(f"\nCOMMITTED. {grand} rows built across {len(SCHEMES)} schemes x {len(periods)} months.")
            _summary(db, periods, write_csv=True)
        else:
            db.rollback()
            print(f"\nDRY RUN rolled back. Would build {grand} rows. Re-run with --commit.")
            _summary(db, periods, write_csv=False, dry=True)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _summary(db, periods, write_csv, dry=False):
    """Employee-wise monthwise summary of the three hour buckets."""
    if dry:
        print("(summary reflects pre-existing rows only; dry run inserted nothing)")
    period_pairs = [(a.isoformat(), b.isoformat()) for a, b in periods]
    rows = db.execute(text("""
        SELECT c.pay_scheme_id, c.date_from, c.date_to, c.eb_id,
               COALESCE(o.emp_code, '') emp_code,
               TRIM(CONCAT(COALESCE(pd.first_name,''),' ',COALESCE(pd.last_name,''))) emp_name,
               ROUND(SUM(c.working_hours), 2) working_hours,
               ROUND(SUM(c.ns_hours), 2)      night_shift_hours,
               ROUND(SUM(c.ot_hours), 2)      ot_hours,
               ROUND(SUM(c.fest_hours), 2)    fest_hours
        FROM tbl_ejm_wages_data_collection c
        LEFT JOIN hrms_ed_official_details o  ON o.eb_id = c.eb_id AND o.active = 1
        LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = c.eb_id
        WHERE c.pay_scheme_id IN :schemes
          AND c.update_for IN :codes
          AND (c.date_from, c.date_to) IN :pairs
        GROUP BY c.pay_scheme_id, c.date_from, c.date_to, c.eb_id, o.emp_code, emp_name
        ORDER BY c.pay_scheme_id, c.date_from, emp_code
    """).bindparams(schemes=SCHEMES, codes=ARM_CODES, pairs=tuple(period_pairs))).mappings().all()

    print(f"\nEmployee-wise monthwise summary: {len(rows)} (scheme x employee x month) rows")
    tot = {}
    for r in rows:
        k = (r["pay_scheme_id"], str(r["date_from"]))
        t = tot.setdefault(k, [0, 0.0, 0.0, 0.0])
        t[0] += 1
        t[1] += float(r["working_hours"] or 0)
        t[2] += float(r["night_shift_hours"] or 0)
        t[3] += float(r["fest_hours"] or 0)
    for (ps, mfrom), (n, wh, ns, fs) in sorted(tot.items()):
        print(f"  scheme {ps}  {mfrom[:7]}  emps={n:>4}  work_hrs={wh:>10.2f}  ns_hrs={ns:>8.2f}  fest_hrs={fs:>8.2f}")

    if write_csv:
        out = REPO / "dbqueries" / "migrations" / "out_ejm_monthwise_hours_1151_1125.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pay_scheme_id", "month_from", "month_to", "eb_id", "emp_code", "emp_name",
                        "working_hours", "night_shift_hours", "ot_hours", "fest_hours"])
            for r in rows:
                w.writerow([r["pay_scheme_id"], r["date_from"], r["date_to"], r["eb_id"],
                            r["emp_code"], r["emp_name"], r["working_hours"],
                            r["night_shift_hours"], r["ot_hours"], r["fest_hours"]])
        print(f"\nCSV written: {out}")


if __name__ == "__main__":
    main()
