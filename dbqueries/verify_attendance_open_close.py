"""Round-trip check for the Attendance Open/Close SQL against a live tenant DB.

Seeds a handful of throwaway `daily_attendance` rows on a synthetic future
date, drives the EXACT query builders the endpoints use
(``src.hrms.attendance``), asserts the roll-up and both flip directions, then
deletes the seeded rows again.

It has to run against a real DB because every risk in this feature is a schema
detail a string assertion cannot see:
  * `status_id` is a VARCHAR, so the comparison goes through CAST AS UNSIGNED
  * `spell_id` is NULL on rows written before the backfill, so the slice also
    matches on the legacy `spell` name string
  * open/close must move ONLY rows in the source status and leave every other
    status (rejected, pending) alone
  * opening is capped to rows created within OPEN_WINDOW_HOURS, and that cutoff
    is evaluated in DB time — only a live DB proves the comparison lines up

Usage (from the repo root, with the repo venv — a global Python may carry a
FastAPI too new for src.authorization to import):

    ./vowerp3be/Scripts/python.exe dbqueries/verify_attendance_open_close.py --database dev3

Connection defaults come from env/database.env. Exit 0 = all assertions pass.
"""

import argparse
import os
import sys

import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.hrms.attendance import (  # noqa: E402
    OPEN_WINDOW_HOURS,
    STATUS_APPROVED,
    STATUS_OPEN,
    get_attendance_open_close_status_query,
    get_attendance_open_close_stuck_query,
    get_attendance_open_close_update_query,
)

# Synthetic slice — far-future date so it can never collide with real data.
TEST_DATE = "2099-12-31"
BRANCH_ID = 1
SPELL_ID = 9901
SPELL_NAME = "VERIFY-SPELL"
DEPT_A = 9001
DEPT_B = 9002
DEPT_C = 9003  # rows created outside the open window
STATUS_REJECTED = 4


def env_defaults():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "env", "database.env")
    vals = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
    return vals


def _to_pyformat(sql, params):
    """Render a SQLAlchemy text() query as raw SQL with %(name)s placeholders.
    Longest name first so no param name can eat a shorter one's prefix."""
    for name in sorted(params, key=len, reverse=True):
        sql = sql.replace(f":{name}", f"%({name})s")
    return sql


def main():
    env = env_defaults()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=env.get("DATABASE_HOST"))
    ap.add_argument("--port", type=int, default=int(env.get("DATABASE_PORT", 3306)))
    ap.add_argument("--user", default=env.get("DATABASE_USER"))
    ap.add_argument("--password", default=env.get("DATABASE_PASSWORD"))
    ap.add_argument("--database", required=True)
    args = ap.parse_args()

    conn = pymysql.connect(host=args.host, port=args.port, user=args.user,
                           passwd=args.password, db=args.database,
                           cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()

    day_params = {"branch_id": BRANCH_ID, "attendance_date": TEST_DATE}
    slice_params = {**day_params, "spell_id": SPELL_ID, "spell_name": SPELL_NAME}

    def status_map():
        """The status endpoint's rows, keyed by department. It is NOT spell-
        scoped — every seeded row groups under the one SPELL_NAME label, so the
        filter here also asserts the grouping actually lands on the label."""
        cur.execute(_to_pyformat(str(get_attendance_open_close_status_query()),
                                 day_params), day_params)
        rows = cur.fetchall()
        assert {r["spell_name"] for r in rows} == {SPELL_NAME}, \
            f"expected one spell group, got {sorted({r['spell_name'] for r in rows})}"
        return {r["dept_id"]: r for r in rows}

    def flip(dept_id, from_status, to_status):
        """Same wiring the endpoint uses: the window is enforced on open only.
        dept_id 0 is what the page's "All Departments" option submits."""
        params = {
            **slice_params,
            "worked_department_id": dept_id,
            "from_status": from_status,
            "to_status": str(to_status),
            "now": "2099-12-31 00:00:00",
            "updated_by": 1,
            "enforce_open_window": 1 if to_status == STATUS_OPEN else 0,
        }
        cur.execute(_to_pyformat(str(get_attendance_open_close_update_query()),
                                 params), params)
        return cur.rowcount

    def stuck(dept_id, from_status):
        params = {**slice_params, "worked_department_id": dept_id,
                  "from_status": from_status}
        cur.execute(_to_pyformat(str(get_attendance_open_close_stuck_query()),
                                 params), params)
        return cur.fetchone()["stuck"]

    def cleanup():
        cur.execute(
            "DELETE FROM daily_attendance WHERE attendance_date = %s AND branch_id = %s",
            (TEST_DATE, BRANCH_ID))
        conn.commit()

    cleanup()
    try:
        # Dept A: 2 closed rows + 1 rejected (must never move).
        # Dept B: 1 closed row carrying NO spell_id — only the legacy name
        #         string matches it, which is the fallback under test.
        # Dept C: 1 closed row created just outside the open window, and 1 just
        #         inside it — the pair that proves the cutoff lands where it
        #         should instead of blocking or letting through everything.
        #         created_ago_hours = None means "now" (the column default).
        rows = [
            (99001, DEPT_A, SPELL_ID, SPELL_NAME, STATUS_APPROVED, None),
            (99002, DEPT_A, SPELL_ID, SPELL_NAME, STATUS_APPROVED, None),
            (99003, DEPT_A, SPELL_ID, SPELL_NAME, STATUS_REJECTED, None),
            (99004, DEPT_B, None, SPELL_NAME, STATUS_APPROVED, None),
            (99005, DEPT_C, SPELL_ID, SPELL_NAME, STATUS_APPROVED,
             OPEN_WINDOW_HOURS + 1),
            (99006, DEPT_C, SPELL_ID, SPELL_NAME, STATUS_APPROVED,
             OPEN_WINDOW_HOURS - 1),
        ]
        cur.executemany("""
            INSERT INTO daily_attendance
                (attendance_date, branch_id, is_active, worked_department_id,
                 spell_id, spell, status_id, eb_id, created_date_time)
            VALUES (%s, %s, 1, %s, %s, %s, %s, %s,
                    CASE WHEN %s IS NULL THEN NOW()
                         ELSE NOW() - INTERVAL %s HOUR END)
        """, [(TEST_DATE, BRANCH_ID, d, sid, sname, str(st), eb, ago, ago)
              for (eb, d, sid, sname, st, ago) in rows])
        conn.commit()

        # 1. Everything starts closed (no row sits in status 1).
        st = status_map()
        assert set(st) == {DEPT_A, DEPT_B, DEPT_C}, f"departments: {sorted(st)}"
        assert st[DEPT_A]["open_count"] == 0 and st[DEPT_A]["total_count"] == 3
        assert st[DEPT_B]["open_count"] == 0 and st[DEPT_B]["total_count"] == 1
        assert st[DEPT_C]["open_count"] == 0 and st[DEPT_C]["total_count"] == 2

        # 1b. spell_id is what the page's Spell dropdown submits: resolved where
        #     any row of the group carries one, NULL when none does (dept B).
        assert st[DEPT_A]["spell_id"] == SPELL_ID, st[DEPT_A]
        assert st[DEPT_B]["spell_id"] is None, st[DEPT_B]

        # 2. Open dept A: the 2 approved rows move, the rejected one does not.
        assert flip(DEPT_A, STATUS_APPROVED, STATUS_OPEN) == 2
        conn.commit()
        st = status_map()
        assert st[DEPT_A]["open_count"] == 2, st[DEPT_A]
        assert st[DEPT_B]["open_count"] == 0, "opening A must not touch B"

        # 3. Dept B is reachable only through the legacy spell-name fallback.
        assert flip(DEPT_B, STATUS_APPROVED, STATUS_OPEN) == 1
        conn.commit()
        assert status_map()[DEPT_B]["open_count"] == 1

        # 4. Close dept A again — back to 0 open, rejected row still untouched.
        assert flip(DEPT_A, STATUS_OPEN, STATUS_APPROVED) == 2
        conn.commit()
        st = status_map()
        assert st[DEPT_A]["open_count"] == 0, st[DEPT_A]
        cur.execute("""SELECT COUNT(*) AS n FROM daily_attendance
                       WHERE attendance_date = %s AND worked_department_id = %s
                         AND CAST(status_id AS UNSIGNED) = %s""",
                    (TEST_DATE, DEPT_A, STATUS_REJECTED))
        assert cur.fetchone()["n"] == 1, "rejected row was moved"

        # 5. Closing what is already closed is a no-op, not an error.
        assert flip(DEPT_A, STATUS_OPEN, STATUS_APPROVED) == 0
        conn.commit()

        # 6. Dept C: opening reaches only the row created inside the window,
        #    and the leftover probe reports the one it held back.
        assert flip(DEPT_C, STATUS_APPROVED, STATUS_OPEN) == 1
        conn.commit()
        assert status_map()[DEPT_C]["open_count"] == 1
        assert stuck(DEPT_C, STATUS_APPROVED) == 1, "window leftover miscounted"

        # 7. Closing ignores the window — the stale row still locks back down.
        assert flip(DEPT_C, STATUS_OPEN, STATUS_APPROVED) == 1
        conn.commit()
        assert status_map()[DEPT_C]["open_count"] == 0

        # 8. Re-opening now finds nothing inside the window (that row's clock
        #    did not move), so both dept C rows are stuck.
        assert flip(DEPT_C, STATUS_APPROVED, STATUS_OPEN) == 1
        conn.commit()
        assert stuck(DEPT_C, STATUS_APPROVED) == 1

        # 9. "All Departments" (dept 0) reaches every department of the spell —
        #    including the NULL-department row, which no single-department flip
        #    can touch. Closing all leaves nothing open anywhere.
        cur.execute("""
            INSERT INTO daily_attendance
                (attendance_date, branch_id, is_active, worked_department_id,
                 spell_id, spell, status_id, eb_id, created_date_time)
            VALUES (%s, %s, 1, NULL, %s, %s, %s, 99007, NOW())
        """, (TEST_DATE, BRANCH_ID, SPELL_ID, SPELL_NAME, str(STATUS_OPEN)))
        conn.commit()
        assert status_map()[None]["open_count"] == 1, "NULL-dept row not seeded open"

        # A, B and C between them hold 2 open rows (B from step 3, C from
        # step 8) plus the NULL-dept one just seeded.
        assert flip(0, STATUS_OPEN, STATUS_APPROVED) == 3
        conn.commit()
        st = status_map()
        assert all(r["open_count"] == 0 for r in st.values()), st
        # The rejected row still never moved — "all" widens departments, not
        # statuses.
        cur.execute("""SELECT COUNT(*) AS n FROM daily_attendance
                       WHERE attendance_date = %s AND branch_id = %s
                         AND CAST(status_id AS UNSIGNED) = %s""",
                    (TEST_DATE, BRANCH_ID, STATUS_REJECTED))
        assert cur.fetchone()["n"] == 1, "all-departments close moved a rejected row"

        print("OK - attendance open/close round-trip passed on", args.database)
    finally:
        cleanup()
        conn.close()


if __name__ == "__main__":
    main()
