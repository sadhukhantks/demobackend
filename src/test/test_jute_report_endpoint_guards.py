"""
Guards for the jute report endpoints themselves (not their SQL).

Two regressions this locks down:

1. `/stock` and `/batch-cost` validated `branch_id` but passed `date` straight
   into the query, so '21-08-2026' reached MySQL and came back as
   OperationalError 1525 -> HTTP 500 instead of a 400.
2. Every handler wrapped `str(e)` into `detail`, and a SQLAlchemy error's str
   carries the whole statement -- so a malformed date replied to the client
   with the full report SQL. The exception is already logged with exc_info,
   so the detail never needed it.

No DB and no app needed: the handlers only touch `request.query_params.get`.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.juteProcurement.reports import (
    get_batch_cost_report,
    get_jute_stock_report,
)

ENDPOINTS = [
    ("stock", get_jute_stock_report),
    ("batch_cost", get_batch_cost_report),
]

# What SQLAlchemy actually hands you: message + the statement that failed.
SQLA_STYLE_ERROR = (
    '(pymysql.err.OperationalError) (1525, "Incorrect DATE value: \'x\'")\n'
    "[SQL: WITH RECURSIVE group_path AS (SELECT igm.item_grp_id ...)]"
)


def _request(**params):
    return SimpleNamespace(query_params=params)


def _explode(*_args, **_kwargs):
    raise RuntimeError(SQLA_STYLE_ERROR)


@pytest.mark.parametrize("name,handler", ENDPOINTS)
@pytest.mark.parametrize("bad_date", ["21-08-2026", "not-a-date", "", "2026-13-45"])
def test_malformed_date_is_400_and_never_reaches_the_db(name, handler, bad_date):
    db = SimpleNamespace(execute=_explode)
    with pytest.raises(HTTPException) as exc:
        handler(_request(branch_id="1", date=bad_date), db=db, token_data={})
    assert exc.value.status_code == 400, f"{name} let {bad_date!r} through"
    # '' trips the earlier "required" guard; anything else must name the format.
    assert "YYYY-MM-DD" in exc.value.detail or bad_date == ""


@pytest.mark.parametrize("name,handler", ENDPOINTS)
def test_db_errors_do_not_leak_sql_to_the_client(name, handler):
    db = SimpleNamespace(execute=_explode)
    with pytest.raises(HTTPException) as exc:
        handler(_request(branch_id="1", date="2026-08-21"), db=db, token_data={})
    assert exc.value.status_code == 500
    detail = str(exc.value.detail)
    assert "SQL" not in detail and "SELECT" not in detail, f"{name} leaked: {detail}"


@pytest.mark.parametrize("name,handler", ENDPOINTS)
def test_missing_params_still_400(name, handler):
    db = SimpleNamespace(execute=_explode)
    for params in ({"date": "2026-08-21"}, {"branch_id": "1"}, {"branch_id": "x", "date": "2026-08-21"}):
        with pytest.raises(HTTPException) as exc:
            handler(_request(**params), db=db, token_data={})
        assert exc.value.status_code == 400, f"{name} {params}"
