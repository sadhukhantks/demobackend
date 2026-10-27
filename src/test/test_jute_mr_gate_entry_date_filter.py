"""TKT-000226 (sls br29): the MR list only filtered by MR date; the reporter also
wants to bound the list by gate entry date (the MR list is ordered by it).

/get_mr_table and /download_mr_table accept ge_from_date / ge_to_date beside
from_date / to_date; both pairs are optional and combine. The clause lives in
one constant shared by the list, its COUNT and the Excel download.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.mr import mr_download_date_suffix
from src.juteProcurement.query import (
    JUTE_MR_LIST_DATE_SQL,
    get_jute_mr_table_count_query,
    get_jute_mr_table_query,
)
from src.main import app

client = TestClient(app)


def _norm(sql) -> str:
    return " ".join(str(sql).split())


@pytest.fixture
def mock_session():
    session = MagicMock()
    count_row = MagicMock()
    count_row.total = 0
    session.execute.return_value.fetchone.return_value = count_row
    session.execute.return_value.fetchall.return_value = []
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: session
    yield session
    app.dependency_overrides.clear()


def _bound_params(session):
    """Bind-param dicts of every db.execute call (count + data)."""
    return [c.args[1] for c in session.execute.call_args_list if len(c.args) > 1]


# -- SQL ----------------------------------------------------------------------

def test_shared_date_clause_bounds_both_mr_date_and_gate_entry_date():
    clause = _norm(JUTE_MR_LIST_DATE_SQL)
    assert "(:date_from IS NULL OR jm.jute_mr_date >= :date_from)" in clause
    assert "(:date_to IS NULL OR jm.jute_mr_date <= :date_to)" in clause
    assert "(:ge_date_from IS NULL OR jm.jute_gate_entry_date >= :ge_date_from)" in clause
    assert "(:ge_date_to IS NULL OR jm.jute_gate_entry_date <= :ge_date_to)" in clause


@pytest.mark.parametrize("builder", [get_jute_mr_table_query, get_jute_mr_table_count_query])
def test_list_and_count_queries_carry_the_gate_entry_bounds(builder):
    sql = _norm(builder(co_id=2))
    assert _norm(JUTE_MR_LIST_DATE_SQL) in sql
    # the search variant too (different f-string branch)
    assert _norm(JUTE_MR_LIST_DATE_SQL) in _norm(builder(co_id=2, search="x"))


# -- endpoints ----------------------------------------------------------------

@pytest.mark.parametrize("url", ["/api/juteMR/get_mr_table", "/api/juteMR/download_mr_table"])
def test_endpoints_bind_gate_entry_dates_from_the_query_string(mock_session, url):
    response = client.get(f"{url}?co_id=1&ge_from_date=2026-10-01&ge_to_date=2026-10-12")
    assert response.status_code == 200, response.text
    params = _bound_params(mock_session)
    assert params, "no db.execute call captured"
    for p in params:
        assert str(p["ge_date_from"]) == "2026-10-01"
        assert str(p["ge_date_to"]) == "2026-10-12"
        # MR-date bounds stay optional and are bound as NULL when absent
        assert p["date_from"] is None and p["date_to"] is None


@pytest.mark.parametrize("url", ["/api/juteMR/get_mr_table", "/api/juteMR/download_mr_table"])
def test_endpoints_without_gate_entry_dates_bind_null(mock_session, url):
    response = client.get(f"{url}?co_id=1&from_date=2026-09-01")
    assert response.status_code == 200, response.text
    for p in _bound_params(mock_session):
        assert p["ge_date_from"] is None and p["ge_date_to"] is None
        assert str(p["date_from"]) == "2026-09-01"


@pytest.mark.parametrize("url", ["/api/juteMR/get_mr_table", "/api/juteMR/download_mr_table"])
@pytest.mark.parametrize("param", ["ge_from_date", "ge_to_date"])
def test_endpoints_reject_a_bad_gate_entry_date(mock_session, url, param):
    response = client.get(f"{url}?co_id=1&{param}=12-10-2026")
    assert response.status_code == 400
    assert param in response.json()["detail"]


# -- download file name -------------------------------------------------------

def test_download_file_name_names_the_range_in_use():
    assert mr_download_date_suffix("", "") == "all"
    assert mr_download_date_suffix("2026-09-01", "2026-09-30") == "2026-09-01_2026-09-30"
    assert mr_download_date_suffix("", "", "2026-10-01", "2026-10-12") == "ge_2026-10-01_2026-10-12"
    assert mr_download_date_suffix("2026-09-01", "", "", "2026-10-12") == "2026-09-01_ge_2026-10-12"
