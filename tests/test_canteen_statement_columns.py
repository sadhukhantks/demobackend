"""The canteen statement's day columns are named in two places — the SQL alias
and the `columns` list the screen renders from. When those drifted apart the
sheet still drew 31 day columns but read keys that no longer existed, so every
day cell came out blank while meals and total looked right.
"""
import re

import pytest
from fastapi import HTTPException

from src.hrms.njmWagesData import (
    _STATEMENT_MAX_DAYS,
    _canteen_statement_sql,
    _day_label,
    _period_days,
    _stl_statement_sql,
)

# Both day-wise statements share the hazard.
BUILDERS = [_canteen_statement_sql, _stl_statement_sql]


def _aliases(sql: str) -> list[str]:
    """Day aliases in the order the SELECT lists them."""
    return re.findall(r"AS `(\d{2})`", sql)


@pytest.mark.parametrize("build", BUILDERS)
def test_sql_aliases_match_the_returned_column_labels(build):
    days = _period_days("2026-08-01", "2026-08-31")
    assert _aliases(build(days)) == [_day_label(d) for d in days]


@pytest.mark.parametrize("build", BUILDERS)
def test_a_period_across_two_months_still_has_unique_day_columns(build):
    days = _period_days("2026-06-26", "2026-07-25")
    labels = [_day_label(d) for d in days]
    assert len(set(labels)) == len(labels)
    assert _aliases(build(days)) == labels


def test_period_longer_than_a_wage_month_is_refused():
    with pytest.raises(HTTPException) as err:
        _period_days("2026-06-01", "2026-07-15")
    assert err.value.status_code == 400
    assert str(_STATEMENT_MAX_DAYS) in err.value.detail
