"""Tests for the "Attendance With PaySchemes" checklist (NJM Wages Data Preparation)."""

import pytest
from sqlalchemy import text

from src.hrms.reportQueries import (
    get_attendance_payscheme_query,
    get_deactivate_rejected_attendance_query,
)


class TestAttendancePayschemeQuery:
    def test_returns_text(self):
        assert isinstance(get_attendance_payscheme_query(), type(text("")))

    def test_has_required_binds(self):
        sql = str(get_attendance_payscheme_query())
        for bind in (":branch_id", ":date_from", ":date_to", ":sub_dept_id",
                     ":pay_scheme_id"):
            assert bind in sql

    def test_scheme_name_comes_from_pay_scheme_master(self):
        # pay_scheme is empty in migrated tenants; payscheme_id is the real key.
        sql = str(get_attendance_payscheme_query())
        assert "pay_scheme_master" in sql
        assert "psm.payscheme_id = pep.PAY_SCHEME_ID" in sql

    def test_department_follows_the_legacy_column(self):
        # Legacy selected department_master.dept_desc; vowerp3 reaches it
        # through the employee's sub-dept. sub_dept_desc alone is ambiguous
        # ('SPG MAIN A' exists under both SPINNING(LC) and SPINNING(NJM)).
        sql = str(get_attendance_payscheme_query())
        assert "dm.dept_desc AS department" in sql
        assert "dm.dept_id = sd.dept_id" in sql

    def test_worked_hours_are_net_of_idle(self):
        assert "working_hours - COALESCE(da.idle_hours, 0)" in str(
            get_attendance_payscheme_query()
        )

    def test_unmapped_employees_survive_the_scheme_filter(self):
        # Legacy LEFT JOINed the payscheme and never applied att_payschm, so
        # attendance with no active scheme -- the thing the checklist is for --
        # stayed visible. An INNER JOIN would hide 319 of branch 87's 321.
        sql = str(get_attendance_payscheme_query())
        assert "LEFT JOIN pay_employee_payscheme" in sql
        assert "OR pep.ID IS NULL" in sql

    def test_matches_what_the_wages_build_consumes(self):
        sql = str(get_attendance_payscheme_query())
        assert "da.attendance_type IN ('R', 'O')" in sql
        assert "da.is_active = 1" in sql
        assert "da.branch_id = :branch_id" in sql

    def test_rejected_attendance_is_excluded_but_null_status_kept(self):
        # NOT IN / <> on a bare NULL is NULL (false) and would silently drop
        # the legacy bio-imported rows that carry no status at all.
        assert "COALESCE(da.status_id, 0) <> 4" in str(
            get_attendance_payscheme_query()
        )


class TestDeactivateRejectedAttendanceQuery:
    def test_scoped_to_branch_and_period(self):
        sql = str(get_deactivate_rejected_attendance_query())
        # IGNORE: the unique key includes is_active, so an existing inactive
        # twin must skip the row, not abort the checklist.
        assert sql.lstrip().startswith("UPDATE IGNORE daily_attendance")
        assert "SET is_active = 0" in sql
        assert "status_id = 4" in sql
        for bind in (":branch_id", ":date_from", ":date_to"):
            assert bind in sql
