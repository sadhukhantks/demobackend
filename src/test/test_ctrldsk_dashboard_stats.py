"""
Tests for the Control Desk dashboard stats endpoint.

/api/ctrldskAdmin/dashboard_stats — active tenant count from
vowconsole3.con_org_master plus cross-database company / active-user counts
per tenant. Tenants without a resolvable schema must come back with null
counts, never break the whole response.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from src.common.ctrldskAdmin.query import (
    get_active_orgs_query,
    get_tenant_counts_query,
)
from src.common.ctrldskAdmin.orgs import get_dashboard_stats


class TestQueries:
    def test_active_orgs_query_filters_active(self):
        sql = str(get_active_orgs_query())
        assert isinstance(get_active_orgs_query(), type(text("")))
        assert "con_org_master" in sql
        assert "active = 1" in sql

    def test_tenant_counts_query_interpolates_db(self):
        sql = str(get_tenant_counts_query("dev3"))
        assert "`dev3`.co_mst" in sql
        assert "`dev3`.user_mst" in sql
        assert "active = 1" in sql  # users filtered to active only


def _org_row(org_id, name, shortname):
    row = MagicMock()
    row._mapping = {
        "con_org_id": org_id,
        "con_org_name": name,
        "con_org_shortname": shortname,
    }
    return row


def _session_for(orgs, schemas, counts):
    """Fake Session whose execute() dispatches on the SQL it receives."""
    session = MagicMock()

    def execute(query, *a, **kw):
        sql = str(query)
        result = MagicMock()
        if "con_org_master" in sql:
            result.fetchall.return_value = [_org_row(*o) for o in orgs]
        elif "schemata" in sql:
            result.fetchall.return_value = [(s,) for s in schemas]
        else:
            db = sql.split("`")[1]
            if db not in counts:
                raise Exception("table missing")
            row = MagicMock()
            row.companies, row.active_users = counts[db]
            result.fetchone.return_value = row
        return result

    session.execute.side_effect = execute
    return session


def _run(orgs, schemas, counts):
    session = _session_for(orgs, schemas, counts)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    with patch("src.common.ctrldskAdmin.orgs.Session", return_value=ctx):
        return get_dashboard_stats(request=MagicMock(), token_data={"user_id": 1})


class TestEndpoint:
    def test_counts_per_tenant(self):
        resp = _run(
            orgs=[(38, "dev3", "dev3"), (1, "SLS Enterprise", "sls")],
            schemas=["dev3", "sls", "vowconsole3"],
            counts={"dev3": (27, 19), "sls": (88, 50)},
        )
        data = resp["data"]
        assert data["active_tenants"] == 2
        by_id = {t["con_org_id"]: t for t in data["tenants"]}
        assert by_id[38]["companies"] == 27
        assert by_id[38]["active_users"] == 19
        assert by_id[1]["companies"] == 88

    def test_blank_or_missing_shortname_gives_null_counts(self):
        resp = _run(
            orgs=[(2, "No Shortname", ""), (10, "EMPIRE", None)],
            schemas=["dev3"],
            counts={},
        )
        for t in resp["data"]["tenants"]:
            assert t["companies"] is None
            assert t["active_users"] is None
        assert resp["data"]["active_tenants"] == 2

    def test_shortname_case_resolves_to_actual_schema(self):
        resp = _run(
            orgs=[(39, "AMCL Mechineries", "AMCL")],
            schemas=["amcl"],
            counts={"amcl": (1, 2)},
        )
        t = resp["data"]["tenants"][0]
        assert t["companies"] == 1
        assert t["active_users"] == 2

    def test_schema_without_tables_gives_null_not_500(self):
        resp = _run(
            orgs=[(31, "demo", "dem")],
            schemas=["dem"],
            counts={},  # dispatch raises for dem — endpoint must swallow it
        )
        t = resp["data"]["tenants"][0]
        assert t["companies"] is None
        assert t["active_users"] is None

    def test_shortname_not_a_schema_gives_null(self):
        resp = _run(
            orgs=[(40, "Quality Assurance", "qa")],
            schemas=["dev3"],
            counts={},
        )
        t = resp["data"]["tenants"][0]
        assert t["companies"] is None
