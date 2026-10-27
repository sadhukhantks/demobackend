"""
Guard for dbqueries/migrations/add_sales_dtl_jute_mr_li_link_and_view_sold.sql.

vw_jute_stock_outstanding is read with `SELECT *`-style consumers and by position
in places, so its column order is a contract: every pre-existing column keeps its
name and ordinal, new columns are appended. The sold-side predicate must also
mirror reportQueries._sold_cte (raw-jute invoice type / approved status), or the
line ledger and the branch reports disagree on what counts as a sale.

No DB, no app — parses the migration text only.
"""
import re
from pathlib import Path

from src.juteProcurement.constants import RAW_JUTE_INVOICE_TYPE_ID, SALES_APPROVED_STATUS_ID

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "dbqueries" / "migrations" / "add_sales_dtl_jute_mr_li_link_and_view_sold.sql"
)

# Order as live on sls + dev3 before this migration (eligibility migration, 2026-08-13).
LEGACY_COLUMNS = [
    "jute_mr_li_id", "inward_date", "branch_id", "branch_mr_no", "jute_gate_entry_no",
    "warehouse_name", "actual_quality", "actual_item_id", "actual_qty", "actual_weight",
    "unit_conversion", "bal_qty", "bal_weight", "accepted_weight", "bal_accepted_weight",
    "rate", "actual_rate", "mr_status_id",
]
APPENDED_COLUMNS = ["sold_qty", "sold_weight"]


def _view_select_list(sql: str) -> str:
    m = re.search(
        r"CREATE OR REPLACE VIEW vw_jute_stock_outstanding AS\s*SELECT(.*?)\sFROM jute_mr jm",
        sql, re.S,
    )
    assert m, "view definition not found"
    body = m.group(1)
    # strip -- comments so an alias inside a comment never counts
    return re.sub(r"--[^\n]*", "", body)


def _aliases(select_list: str) -> list[str]:
    return re.findall(r"\bAS\s+([a-z_]+)\s*(?:,|$)", select_list, re.M)


def test_view_column_contract_preserved():
    sql = MIGRATION.read_text(encoding="utf-8")
    aliases = _aliases(_view_select_list(sql))
    assert aliases == LEGACY_COLUMNS + APPENDED_COLUMNS, aliases


def test_sold_predicate_mirrors_sold_cte():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert f"sinv.invoice_type = {RAW_JUTE_INVOICE_TYPE_ID}" in sql
    assert f"sinv.status_id = {SALES_APPROVED_STATUS_ID}" in sql
    assert "COALESCE(sinv.active, 1) = 1" in sql
    assert "sid.jute_mr_li_id IS NOT NULL" in sql


def test_balance_subtracts_sold_on_both_ledgers():
    sql = MIGRATION.read_text(encoding="utf-8")
    sel = _view_select_list(sql)
    bal_qty = re.search(r"\((.*?)\)\s+AS bal_qty", sel, re.S).group(1)
    bal_wt = re.search(r"ROUND\(\((.*?)\), 3\)\s+AS bal_weight", sel, re.S).group(1)
    assert "iss.issqty" in bal_qty and "sold.soldwt" in bal_qty
    assert "iss.isswt" in bal_wt and "sold.soldwt" in bal_wt
    # pro-rata qty must be guarded against a zero/NULL receipt weight
    assert "jml.actual_weight > 0" in bal_qty


def test_column_is_nullable_and_ddl_has_no_semicolon_inside_strings():
    # tools/run_sql_migration.py splits on ';' — a ';' inside the COMMENT string
    # would cut the prepared statement in half (bit us once, 2026-08-23).
    sql = MIGRATION.read_text(encoding="utf-8")
    ddl = re.search(r"'ALTER TABLE sales_invoice_dtl ADD COLUMN[^\n]*", sql).group(0)
    assert "BIGINT NULL" in ddl
    assert ";" not in ddl
