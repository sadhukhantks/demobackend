"""Extra Advance Payment rate-slab precedence check.

get_extra_advance_payment_query() picks one rate per attendance row out of
extra_advance_rate with a correlated ORDER BY: designation-specific beats the
catch-all, day-specific beats any-day, newest eff_date last. Get that order
wrong and the report silently pays the wrong amount, which is exactly the kind
of bug nothing else catches.

Runs against the dev3 QA tenant: it seeds a handful of daily_attendance and
extra_advance_rate rows on branch 2, asserts the amounts, and deletes them
again (dev3 has no real attendance data).

Run: vowerp3be/Scripts/python tests/test_extra_advance_rate_pick.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.hrms.reportQueries import get_extra_advance_payment_query

BRANCH_ID = 2          # dev3 "Factory", co_id 1
CO_ID = 1
WEEKDAY = "2099-01-02"  # a Friday, far outside any real data
SUNDAY = "2099-01-03"
DESIG_SPECIAL = 78      # gets its own pro-rata rate
DESIG_PLAIN = 79        # falls through to the catch-all slabs

# (eb_id, date, hours, designation) -> expected advance amount
CASES = [
    # Catch-all slabs, any day.
    ((90001, WEEKDAY, 4.0, DESIG_PLAIN), 150),
    ((90002, WEEKDAY, 6.0, DESIG_PLAIN), 200),
    ((90003, WEEKDAY, 8.0, DESIG_PLAIN), 300),
    # Below the lowest slab: no rate matches, so the row drops out entirely.
    ((90004, WEEKDAY, 2.0, DESIG_PLAIN), None),
    # Designation-specific pro-rata beats the catch-all slab for the same hours.
    ((90005, WEEKDAY, 8.0, DESIG_SPECIAL), 450),
    ((90006, WEEKDAY, 4.0, DESIG_SPECIAL), 225),   # ROUND(450 / 8 * 4)
    # Sunday-specific row beats the weekday one on a Sunday.
    ((90007, SUNDAY, 8.0, DESIG_SPECIAL), 300),
    # ... but the catch-all has no day-specific row, so it is unaffected.
    ((90008, SUNDAY, 8.0, DESIG_PLAIN), 300),
]

RATES = [
    # branch, desig, min_hours, max_hours, is_sunday, rate, pro_rata, eff_date
    (BRANCH_ID, None, 3.00, 4.99, None, 150, 0, "2098-01-01"),
    (BRANCH_ID, None, 5.00, 7.49, None, 200, 0, "2098-01-01"),
    (BRANCH_ID, None, 7.50, 24.00, None, 300, 0, "2098-01-01"),
    (BRANCH_ID, DESIG_SPECIAL, 3.00, 24.00, 0, 450, 1, "2098-01-01"),
    (BRANCH_ID, DESIG_SPECIAL, 3.00, 24.00, 1, 300, 1, "2098-01-01"),
    # Superseded by the row above it -- same scope, older eff_date. If the
    # eff_date tie-break is wrong this pays 999 instead of 450.
    (BRANCH_ID, DESIG_SPECIAL, 3.00, 24.00, 0, 999, 0, "2097-01-01"),
]


def main():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env"))
    url = (f"mysql+pymysql://{os.getenv('DATABASE_USER')}:"
           f"{os.getenv('DATABASE_PASSWORD')}@{os.getenv('DATABASE_HOST')}:"
           f"{os.getenv('DATABASE_PORT')}/dev3")
    engine = create_engine(url)

    with engine.begin() as conn:
        try:
            for (eb_id, date, hours, desig) in (c[0] for c in CASES):
                conn.execute(text("""
                    INSERT INTO daily_attendance
                        (attendance_date, attendance_type, branch_id, eb_id,
                         idle_hours, is_active, spell, status_id,
                         worked_department_id, worked_designation_id, working_hours)
                    VALUES (:d, 'O', :b, :eb, 0, 1, 'A1', '3', NULL, :desig, :h)
                """), {"d": date, "b": BRANCH_ID, "eb": eb_id,
                       "desig": desig, "h": hours})

            for r in RATES:
                conn.execute(text("""
                    INSERT INTO extra_advance_rate
                        (branch_id, desig_id, min_hours, max_hours, is_sunday,
                         rate, pro_rata, eff_date)
                    VALUES (:b, :desig, :mn, :mx, :sun, :rate, :pro, :eff)
                """), dict(zip(
                    ("b", "desig", "mn", "mx", "sun", "rate", "pro", "eff"), r)))

            rows = conn.execute(get_extra_advance_payment_query(), {
                "co_id": CO_ID, "branch_id": BRANCH_ID,
                "date_from": WEEKDAY, "date_to": SUNDAY, "shift": None,
            }).fetchall()
            by_hours = {}
            for row in rows:
                m = row._mapping
                by_hours[(str(m["attendance_date"]), float(m["work_hours"]))] = \
                    by_hours.get((str(m["attendance_date"]), float(m["work_hours"])), []) \
                    + [float(m["advance_amount"])]

            # The rows carry no employee identity on dev3 (no official details),
            # so match each expectation on (date, hours) and check the amount is
            # among what came back for that pair.
            failures = []
            for (eb_id, date, hours, desig), expected in CASES:
                got = by_hours.get((date, hours), [])
                if expected is None:
                    if got:
                        failures.append(f"eb {eb_id}: expected no row, got {got}")
                elif expected not in got:
                    failures.append(
                        f"eb {eb_id} ({date}, {hours}h, desig {desig}): "
                        f"expected {expected}, got {got}")

            # The shift filter must actually narrow: every seeded row is spell A1.
            n_b = len(conn.execute(get_extra_advance_payment_query(), {
                "co_id": CO_ID, "branch_id": BRANCH_ID,
                "date_from": WEEKDAY, "date_to": SUNDAY, "shift": "B",
            }).fetchall())
            if n_b:
                failures.append(f"shift=B returned {n_b} rows, expected 0")

            assert not failures, "\n  " + "\n  ".join(failures)
            print(f"OK - {len(CASES)} rate cases + shift filter")
        finally:
            conn.execute(text(
                "DELETE FROM daily_attendance WHERE eb_id BETWEEN 90001 AND 90008"))
            conn.execute(text(
                "DELETE FROM extra_advance_rate WHERE eff_date IN ('2098-01-01', '2097-01-01')"))


if __name__ == "__main__":
    main()
