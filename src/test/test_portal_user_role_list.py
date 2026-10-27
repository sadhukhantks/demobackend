"""
Tests for the tenant-portal role/user list queries and endpoints.

Regression guard: both endpoints used to return `total = len(rows)` (the size of
the current page), which made the grid show a single page and broke search —
the query functions ignored :search entirely.

Covers src/common/query.py and src/common/portal/{roles,users}.py
"""

import pytest
from sqlalchemy import text

from src.common.query import (
    get_roles_tenant,
    get_roles_tenant_count,
    get_users_tenant,
    get_users_tenant_count,
    get_roles_tenant_admin,
    get_roles_tenant_admin_count,
    get_roles_ctrldsk_admin,
    get_roles_ctrldsk_admin_count,
)
from src.common.ctrldskAdmin.query import (
    get_portal_menu_details,
    get_portal_menu_details_count,
    orgmodulename,
    orgmodulename_count,
)
from src.common.portal.roles import get_roles as get_roles_endpoint
from src.common.portal.users import get_roles as get_users_endpoint


class TestPortalListQueries:
    """The paged query filters + orders; the count query does neither limit nor offset."""

    @pytest.mark.parametrize(
        "page_fn, count_fn, order_col",
        [
            (get_roles_tenant, get_roles_tenant_count, "role_id"),
            (get_users_tenant, get_users_tenant_count, "user_id"),
        ],
    )
    def test_page_query_binds_search_limit_offset(self, page_fn, count_fn, order_col):
        sql = str(page_fn())
        assert isinstance(page_fn(), type(text("")))
        assert ":search" in sql, "search bind missing — search would be a no-op"
        assert ":limit" in sql and ":offset" in sql
        assert f"ORDER BY {order_col}" in sql, "unordered LIMIT/OFFSET repeats/skips rows"

    @pytest.mark.parametrize(
        "count_fn", [get_roles_tenant_count, get_users_tenant_count]
    )
    def test_count_query_is_filtered_but_unpaged(self, count_fn):
        sql = str(count_fn())
        assert "COUNT(*)" in sql
        assert ":search" in sql, "count must honour the same filter as the page"
        assert ":limit" not in sql and ":offset" not in sql, "count must span all pages"

    def test_users_search_covers_name_and_email(self):
        sql = str(get_users_tenant())
        assert "name LIKE :search" in sql
        assert "email_id LIKE :search" in sql


class TestConsoleListQueries:
    """Same guards for the Tenant Admin / Control Desk list queries."""

    @pytest.mark.parametrize(
        "page_fn, count_fn, order_col",
        [
            (get_roles_tenant_admin, get_roles_tenant_admin_count, "con_role_id"),
            (get_roles_ctrldsk_admin, get_roles_ctrldsk_admin_count, "con_role_id"),
        ],
    )
    def test_console_role_queries(self, page_fn, count_fn, order_col):
        page_sql, count_sql = str(page_fn()), str(count_fn())
        assert ":search" in page_sql and "con_role_name LIKE :search" in page_sql
        assert f"ORDER BY {order_col}" in page_sql
        assert ":limit" in page_sql and ":offset" in page_sql
        assert "COUNT(*)" in count_sql and ":search" in count_sql
        assert ":limit" not in count_sql and ":offset" not in count_sql

    def test_portal_menu_details_is_actually_paged(self):
        # Regression: this query had no LIMIT at all, so every page returned
        # the full menu table and `total` reported the whole set.
        sql = str(get_portal_menu_details())
        assert ":limit" in sql and ":offset" in sql
        assert "ORDER BY" in sql

    def test_portal_menu_details_binds_search_only_when_given(self):
        assert ":search" not in str(get_portal_menu_details())
        assert "menu.menu_name LIKE :search" in str(get_portal_menu_details("jute"))

    def test_portal_menu_details_count_is_unpaged(self):
        sql = str(get_portal_menu_details_count())
        assert "COUNT(*)" in sql
        assert ":limit" not in sql and ":offset" not in sql

    def test_orgmodulename_paged_flag_controls_binds(self):
        # The unpaged caller (/orgmodulemapdetails1) executes with NO params,
        # so unbound :limit/:offset there would raise at execute time.
        unpaged = str(orgmodulename())
        assert ":limit" not in unpaged and ":offset" not in unpaged
        assert ":search" not in unpaged

        paged = str(orgmodulename(paged=True))
        assert ":limit" in paged and ":offset" in paged
        assert "ORDER BY com.con_org_id" in paged
        assert "com.con_org_name LIKE :search" in str(orgmodulename("acme", paged=True))

    def test_orgmodulename_count_is_unpaged(self):
        sql = str(orgmodulename_count())
        assert "COUNT(*)" in sql
        assert ":limit" not in sql and ":offset" not in sql


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class _FakeSession:
    """Returns a page of rows for the first execute, the total for the second."""

    def __init__(self, rows, total):
        self._results = [_FakeResult(rows=rows), _FakeResult(scalar=total)]
        self.params = []

    def execute(self, _query, params=None):
        self.params.append(params)
        return self._results.pop(0)


@pytest.mark.parametrize(
    "endpoint, row",
    [
        (get_roles_endpoint, {"role_id": 1, "role_name": "Admin", "active": 1}),
        (get_users_endpoint, {"user_id": 1, "name": "A", "email_id": "a@b.c", "active": 1}),
    ],
)
def test_endpoint_returns_full_total_not_page_length(endpoint, row):
    session = _FakeSession(rows=[_FakeRow(row)], total=137)

    result = endpoint(
        request=None,
        page=1,
        limit=20,
        search=None,
        token_data={"user_id": 1},
        tenant_session=session,
    )

    assert result["total"] == 137, "total must come from the count query, not len(rows)"
    assert result["data"] == [row]


@pytest.mark.parametrize("endpoint", [get_roles_endpoint, get_users_endpoint])
def test_endpoint_wraps_search_and_computes_offset(endpoint):
    session = _FakeSession(rows=[], total=0)

    endpoint(
        request=None,
        page=3,
        limit=20,
        search="abc",
        token_data={"user_id": 1},
        tenant_session=session,
    )

    page_params, count_params = session.params
    assert page_params["offset"] == 40
    assert page_params["search"] == "%abc%"
    assert count_params["search"] == "%abc%", "count must filter on the same term"


@pytest.mark.parametrize("endpoint", [get_roles_endpoint, get_users_endpoint])
def test_endpoint_rejects_token_without_user_id(endpoint):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        endpoint(
            request=None,
            page=1,
            limit=20,
            search=None,
            token_data={},
            tenant_session=_FakeSession(rows=[], total=0),
        )
    assert exc.value.status_code == 403
