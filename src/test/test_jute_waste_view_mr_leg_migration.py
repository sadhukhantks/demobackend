"""
Guard for dbqueries/migrations/20260925_jute_waste_view_add_mr_lines.sql.

Owner ruling 2026-09-25 (D3): purchased jute waste — an ordinary jute_mr_li
line whose item sits in an item group of item_type_id 3 — joins the GENERATED
waste pool, so vw_jute_waste_stock_outstanding gains a UNION ALL leg. The view
is read by three query builders that name its columns, so the nine original
columns keep their names and ordinals and exactly three are appended
(source_type, jute_mr_li_id, rate). The MR leg must net issues and approved
raw-jute sales exactly as vw_jute_stock_outstanding does, and must not touch
that view.

No DB, no app — parses the migration text only.
"""
import re
from pathlib import Path

from src.juteProcurement.constants import (
    JUTE_MR_LI_EXCLUDED_STATUSES,
    JUTE_MR_STOCK_EXCLUDED_STATUSES,
    JUTE_WASTE_ITEM_TYPE_ID,
    RAW_JUTE_INVOICE_TYPE_ID,
    SALES_APPROVED_STATUS_ID,
)

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "dbqueries" / "migrations" / "20260925_jute_waste_view_add_mr_lines.sql"
)

# Order as defined by add_jute_waste_stock.sql (2026-08-10), live on dev3 + sls.
LEGACY_COLUMNS = [
    "waste_entry_id", "co_id", "branch_id", "inward_date", "item_id",
    "actual_qty", "actual_weight", "bal_qty", "bal_weight",
]
APPENDED_COLUMNS = ["source_type", "jute_mr_li_id", "rate"]


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _statements(sql: str) -> list[str]:
    """Exactly what the project runner executes: split on ';', drop comments."""
    return [
        c.strip() for c in sql.split(";")
        if c.strip() and not c.strip().startswith("--")
    ]


def _view_body(sql: str) -> str:
    m = re.search(
        r"CREATE OR REPLACE VIEW vw_jute_waste_stock_outstanding AS(.*?);", sql, re.S
    )
    assert m, "view definition not found"
    return re.sub(r"--[^\n]*", "", m.group(1))


def _legs(sql: str) -> tuple[str, str]:
    body = _view_body(sql)
    prod, mr = re.split(r"\bUNION ALL\b", body)
    return prod, mr


def _select_list(leg: str) -> str:
    m = re.search(r"SELECT(.*?)\sFROM\s", leg, re.S)
    assert m, "select list not found"
    return m.group(1)


def _aliases(select_list: str) -> list[str]:
    return re.findall(r"\bAS\s+([a-z_]+)\s*(?:,|$)", select_list, re.M)


def test_migration_is_a_single_view_statement_the_runner_can_apply():
    stmts = _statements(_sql())
    assert len(stmts) == 1, stmts
    assert stmts[0].startswith("CREATE OR REPLACE VIEW vw_jute_waste_stock_outstanding AS")


def test_comment_block_follows_the_statement_and_holds_no_semicolon():
    """The runner drops any chunk starting with '--', so a comment ABOVE the
    view would swallow it, and a ';' INSIDE the comment would split it."""
    sql = _sql()
    stmt_end = sql.index(";")
    trailer = sql[stmt_end + 1:]
    assert trailer.strip().startswith("--")
    assert ";" not in trailer
    assert "vw_jute_waste_stock_outstanding" in sql[:stmt_end]


def test_both_legs_keep_the_nine_original_columns_first_then_append_three():
    prod, mr = _legs(_sql())
    assert _aliases(_select_list(prod)) == LEGACY_COLUMNS + APPENDED_COLUMNS
    assert _aliases(_select_list(mr)) == LEGACY_COLUMNS + APPENDED_COLUMNS


def test_prod_leg_is_the_old_definition_tagged_prod():
    prod, _ = _legs(_sql())
    assert "FROM jute_prod_waste_entry w" in prod
    assert "INNER JOIN dept_mst d ON d.dept_id = w.dept_id" in prod
    assert "COALESCE(w.branch_id, d.branch_id)" in prod
    assert "WHERE w.active = 1" in prod
    assert "'PROD'" in _select_list(prod)
    sel = _select_list(prod)
    assert re.search(r"NULL\s+AS jute_mr_li_id", sel)
    assert re.search(r"NULL\s+AS rate", sel)
    # issued-total convention of the waste view: NULL status counts against stock
    assert "(ji.status_id IS NULL OR ji.status_id <> 4)" in prod


def test_mr_leg_selects_type_3_lines_only():
    _, mr = _legs(_sql())
    assert "FROM jute_mr jm" in mr
    assert "INNER JOIN jute_mr_li jml ON jml.jute_mr_id = jm.jute_mr_id" in mr
    assert "INNER JOIN item_mst im ON im.item_id = jml.actual_item_id" in mr
    assert f"ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID}" in mr
    assert "'MR'" in _select_list(mr)
    sel = _select_list(mr)
    assert re.search(r"jml\.jute_mr_li_id\s+AS jute_mr_li_id", sel)
    assert re.search(r"jml\.rate\s+AS rate", sel)
    assert re.search(r"NULL\s+AS waste_entry_id", sel)


def test_mr_leg_derives_co_id_from_branch_mst():
    """jute_mr has no co_id column; the picker filters the view on v.co_id."""
    _, mr = _legs(_sql())
    assert "INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id" in mr
    assert re.search(r"bm\.co_id\s+AS co_id", _select_list(mr))
    assert re.search(r"jm\.branch_id\s+AS branch_id", _select_list(mr))
    assert re.search(r"jm\.out_date\s+AS inward_date", _select_list(mr))


def test_mr_leg_is_kilograms_only():
    """actual_qty = actual_weight = jml.actual_weight (D7: kg, no UOM change)."""
    _, mr = _legs(_sql())
    sel = _select_list(mr)
    assert re.search(r"jml\.actual_weight\s+AS actual_qty", sel)
    assert re.search(r"jml\.actual_weight\s+AS actual_weight", sel)
    assert "actual_qty * " not in sel  # no bale pro-rating on this leg


def test_mr_leg_eligibility_mirrors_vw_jute_stock_outstanding():
    _, mr = _legs(_sql())
    # The 2026-09-25 file is the historic definition (48 excluded). The live one is
    # 20261003_vw_jute_stock_outstanding_returned_in_stock.sql -- see the test below.
    mr_rule = f"jm.status_id NOT IN ({', '.join(str(s) for s in (*JUTE_MR_STOCK_EXCLUDED_STATUSES, 48))})"
    li_rule = f"jml.status NOT IN ({', '.join(repr(s) for s in JUTE_MR_LI_EXCLUDED_STATUSES)})"
    assert mr_rule in mr
    assert "jml.active = 1" in mr
    assert f"(jml.status IS NULL OR {li_rule})" in mr


def test_mr_leg_nets_issues_keyed_on_the_mr_line():
    _, mr = _legs(_sql())
    assert "ji.jute_mr_li_id IS NOT NULL" in mr
    assert "(ji.status_id IS NULL OR ji.status_id <> 4)" in mr
    assert "GROUP BY ji.jute_mr_li_id" in mr
    assert "mriss.jute_mr_li_id = jml.jute_mr_li_id" in mr
    sel = _select_list(mr)
    bal_qty = re.search(r"ROUND\((.*?), 3\)\s+AS bal_qty", sel, re.S).group(1)
    bal_wt = re.search(r"ROUND\((.*?), 3\)\s+AS bal_weight", sel, re.S).group(1)
    for bal in (bal_qty, bal_wt):
        assert "jml.actual_weight" in bal
        assert "mriss.isswt" in bal
        assert "sold.soldwt" in bal


def test_mr_leg_nets_approved_raw_jute_sales_like_the_mr_view():
    """Copied verbatim from add_jute_mr_returned_status_48.sql — same invoice
    type, approval status and active predicate, so the same line nets the
    same way in both views."""
    _, mr = _legs(_sql())
    assert "FROM sales_invoice_dtl sid" in mr
    assert f"sinv.invoice_type = {RAW_JUTE_INVOICE_TYPE_ID}" in mr
    assert f"sinv.status_id = {SALES_APPROVED_STATUS_ID}" in mr
    assert "COALESCE(sinv.active, 1) = 1" in mr
    assert "sid.jute_mr_li_id IS NOT NULL" in mr
    assert "SUM(COALESCE(NULLIF(sid.sales_weight, 0), sid.quantity, 0)) AS soldwt" in mr


def test_vw_jute_stock_outstanding_is_not_touched():
    stmts = _statements(_sql())
    assert not any("VIEW vw_jute_stock_outstanding" in s for s in stmts)


def test_rollback_is_the_old_definition_as_a_comment():
    sql = _sql()
    trailer = sql[sql.index(";") + 1:]
    assert "ROLLBACK" in trailer
    assert "-- CREATE OR REPLACE VIEW vw_jute_waste_stock_outstanding AS" in trailer
    assert "-- FROM jute_prod_waste_entry w" in trailer
    # the old body had no MR leg; the rollback must not either
    rollback_body = trailer.split("ROLLBACK", 1)[1]
    assert "UNION ALL" not in rollback_body
    assert "jute_mr_li" not in rollback_body


def test_returned_in_stock_migration_matches_constant():
    """2026-10-03: Returned (48) retained material is stock; both views drop the 48 exclusion."""
    sql = (MIGRATION.parent / "20261003_vw_jute_stock_outstanding_returned_in_stock.sql").read_text(encoding="utf-8")
    rule = f"jm.status_id NOT IN ({', '.join(str(s) for s in JUTE_MR_STOCK_EXCLUDED_STATUSES)})"
    assert 48 not in JUTE_MR_STOCK_EXCLUDED_STATUSES
    assert re.sub(r"--.*", "", sql).count(rule) == 2
    assert "VIEW vw_jute_stock_outstanding AS" in sql
    assert "VIEW vw_jute_waste_stock_outstanding AS" in sql
    assert "48" not in re.sub(r"--.*", "", sql).replace("(4, 6, 21)", "")
