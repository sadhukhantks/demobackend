"""Phase 1 of the purchased-yarn ledger: the ORM models, the DDL migration and
the constants must describe the same schema.

The migration is the schema source of truth; these tests parse it and compare
column names with the mapped classes so the two cannot drift silently.
"""

import re
from pathlib import Path

import pytest

from src.juteProcurement import constants as jp
from src.juteProcurement.yarn_models import (
    JuteYarnIssue,
    JuteYarnIssueLi,
    JuteYarnPurchase,
    JuteYarnPurchaseLi,
    VwYarnGodownOutstanding,
)
from src.juteProduction import constants as jprod
from src.juteProduction.winding_stock_models import JuteProdWindingStock

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "dbqueries" / "migrations" / "20260925_yarn_purchase_issue_tables.sql"
)


def _ddl_columns(sql: str, table: str) -> set[str]:
    m = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
    assert m, f"CREATE TABLE {table} not found"
    cols = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith(("INDEX", "PRIMARY", "UNIQUE")):
            continue
        cols.add(line.split()[0])
    return cols


def _model_columns(model) -> set[str]:
    return {c.name for c in model.__table__.columns}


@pytest.fixture(scope="module")
def sql() -> str:
    return MIGRATION.read_text()


@pytest.mark.parametrize(
    "table, model",
    [
        ("jute_yarn_purchase", JuteYarnPurchase),
        ("jute_yarn_purchase_li", JuteYarnPurchaseLi),
        ("jute_yarn_issue", JuteYarnIssue),
        ("jute_yarn_issue_li", JuteYarnIssueLi),
    ],
)
def test_model_columns_match_ddl(sql, table, model):
    assert model.__tablename__ == table
    assert _model_columns(model) == _ddl_columns(sql, table)


def test_view_columns_match_ddl(sql):
    m = re.search(r"CREATE OR REPLACE VIEW vw_yarn_godown_outstanding AS\s*SELECT(.*?)FROM jute_yarn_purchase p", sql, re.S)
    assert m
    ddl_cols = set()
    for line in m.group(1).split(",\n"):
        line = line.strip().rstrip(",")
        alias = re.search(r"AS\s+(\w+)\s*$", line)
        ddl_cols.add(alias.group(1) if alias else line.split(".")[-1].strip())
    assert _model_columns(VwYarnGodownOutstanding) == ddl_cols


def test_lot_is_mandatory_on_issue_lines():
    # owner ruling 5 (2026-09-25): every issue line names the purchase lot
    assert JuteYarnIssueLi.__table__.c.yarn_purchase_li_id.nullable is False


def test_winding_stock_gains_location_and_dept(sql):
    cols = JuteProdWindingStock.__table__.c
    assert cols.stock_loc.nullable is False
    assert cols.stock_loc.server_default.arg == "FLOOR"
    assert cols.dept_id.nullable is True
    assert "ADD COLUMN stock_loc VARCHAR(10) NOT NULL DEFAULT 'FLOOR'" in sql
    assert "ADD COLUMN dept_id INT NULL" in sql
    assert jprod.WINDING_STOCK_LOCATIONS == ("GODOWN", "FLOOR")


def test_view_exclusions_match_constants(sql):
    excluded = ", ".join(str(s) for s in jp.YARN_DOC_STOCK_EXCLUDED_STATUSES)
    # both the purchase header and the issue header use the same exclusion
    assert sql.count(f"status_id NOT IN ({excluded})") == 2
    assert set(jp.YARN_PURCHASE_ISSUABLE_STATUSES).isdisjoint(jp.YARN_DOC_STOCK_EXCLUDED_STATUSES)
    assert jp.YARN_ISSUE_TYPES == ("ISSUE", "RETURN")
    assert JuteYarnIssue.__table__.c.issue_type.server_default.arg == jp.YARN_ISSUE_TYPE_ISSUE
