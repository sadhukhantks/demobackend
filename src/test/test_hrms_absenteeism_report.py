"""Unit tests for the Absenteeism report (TKT-000151).

The report used to be bounded only from below, so on tenants that never mark
leavers (sls br 29: ~9,000 "active" employees against ~600 who clock in) it
returned everyone ever employed, 1,000-day gaps included. The SQL now carries
an upper bound and drops separated employees; these tests pin both.
"""

import pytest
from fastapi import HTTPException

from src.hrms.constants import RESIGN_DETAIL_STATUSES
from src.hrms.reportQueries import get_absenteeism_query
from src.hrms.reports import get_absenteeism_report


def sql() -> str:
    return str(get_absenteeism_query())


def test_query_is_bounded_on_both_sides():
    s = sql()
    assert "DATEDIFF(:as_on, seen.last_seen) >= :min_days" in s
    assert ":max_days IS NULL OR DATEDIFF(:as_on, seen.last_seen) <= :max_days" in s


def test_query_excludes_separated_employees_but_not_in_notice():
    s = sql()
    assert "pd.status_id NOT IN (" in s
    for status_id in RESIGN_DETAIL_STATUSES:  # 39, 41, 42, 46
        assert str(status_id) in s.split("pd.status_id NOT IN (")[1].split(")")[0]
    # IN NOTICE (40) is still on the roll and can still be absent.
    assert "40" not in s.split("pd.status_id NOT IN (")[1].split(")")[0]


def test_query_selects_the_sub_department_the_handler_returns():
    assert "sd.sub_dept_desc AS sub_department" in sql()


def call(**over):
    kwargs = {
        "request": None,
        "db": None,
        "token_data": {},
        "co_id": 2,
        "branch_id": 29,
        "as_on": "2026-09-02",
        "min_days": 7,
        "max_days": 30,
    }
    kwargs.update(over)
    return get_absenteeism_report(**kwargs)


@pytest.mark.parametrize("missing", ["co_id", "as_on"])
def test_required_params(missing):
    with pytest.raises(HTTPException) as exc:
        call(**{missing: None})
    assert exc.value.status_code == 400


def test_max_days_below_min_days_is_rejected():
    with pytest.raises(HTTPException) as exc:
        call(min_days=30, max_days=7)
    assert exc.value.status_code == 400
    assert "max_days" in exc.value.detail
