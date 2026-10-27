"""
Guard for dbqueries/migrations/20260925_vw_jute_stock_outstanding_waste_kg_qty.sql.

Owner ruling 2026-09-25 (D3/D4 follow-up): a purchased-waste MR line (item in
a type-3 group) is bought in bales/loose units but issued in kg slices of the
grouped waste pool, so vw_jute_stock_outstanding's unit-based qty ledger went
negative on the first pooled issue (sls line 45659: 108 units, one 500 kg
slice, bal_qty -392) and the MR-in-stock / MR-wise issue reports showed
negative unit balances. The migration puts actual_qty, bal_qty and sold_qty of
a type-3 line on the weight ledger and must leave everything else — every
other column, the sold sub-select and the WHERE clause — byte-identical to the
definition live on dev3 and sls (add_jute_mr_returned_status_48.sql).

No DB, no app — parses the migration text only.
"""
import re
from pathlib import Path

from src.juteProcurement.constants import JUTE_WASTE_ITEM_TYPE_ID

MIGRATIONS = Path(__file__).resolve().parents[2] / "dbqueries" / "migrations"
NEW = MIGRATIONS / "20260925_vw_jute_stock_outstanding_waste_kg_qty.sql"
LIVE = MIGRATIONS / "add_jute_mr_returned_status_48.sql"


def _statements(sql: str) -> list[str]:
    """Exactly what the project runner executes: split on ';', drop comments."""
    return [
        c.strip() for c in sql.split(";")
        if c.strip() and not c.strip().startswith("--")
    ]


def _view(sql: str) -> str:
    m = re.search(r"CREATE OR REPLACE VIEW vw_jute_stock_outstanding AS(.*?);", sql, re.S)
    assert m, "view definition not found"
    return re.sub(r"--[^\n]*", "", m.group(1))


def _norm(s: str) -> str:
    return " ".join(s.split())


def test_migration_is_a_single_view_statement_the_runner_can_apply():
    stmts = _statements(NEW.read_text(encoding="utf-8"))
    assert len(stmts) == 1, stmts
    assert stmts[0].startswith("CREATE OR REPLACE VIEW vw_jute_stock_outstanding AS")


def test_type_3_lines_report_qty_on_the_weight_ledger():
    body = _norm(_view(NEW.read_text(encoding="utf-8")))
    flag = f"CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN"
    assert f"{flag} jml.actual_weight ELSE jml.actual_qty END AS actual_qty" in body
    assert (
        f"{flag} ROUND((jml.actual_weight - IFNULL(iss.isswt, 0) - IFNULL(sold.soldwt, 0)), 3) "
        "ELSE (jml.actual_qty - IFNULL(iss.issqty, 0)" in body
    ), "bal_qty of a waste line must be its kg balance"
    assert f"{flag} IFNULL(sold.soldwt, 0) WHEN jml.actual_weight > 0" in body
    assert "LEFT JOIN item_mst im ON im.item_id = jml.actual_item_id" in body
    assert "LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in body


def test_the_group_is_a_left_join_and_never_a_filter():
    body = _norm(_view(NEW.read_text(encoding="utf-8")))
    where = body.partition(" WHERE jm.status_id")[2]
    assert "item_type_id" not in where
    assert "INNER JOIN item" not in body and "JOIN item_mst im" in body


def test_everything_else_is_verbatim_from_the_live_definition():
    new = _norm(_view(NEW.read_text(encoding="utf-8")))
    live = _norm(_view(LIVE.read_text(encoding="utf-8")))

    # WHERE clause, sold sub-select and issued sub-select: byte-identical.
    for marker in ("WHERE jm.status_id", "LEFT JOIN ( SELECT sid.jute_mr_li_id", "LEFT JOIN ( SELECT ji.jute_mr_li_id"):
        assert marker in new and marker in live, marker
    assert new.partition("WHERE jm.status_id")[2] == live.partition("WHERE jm.status_id")[2]
    sold = lambda s: s.partition("LEFT JOIN ( SELECT sid.jute_mr_li_id")[2].partition(") sold")[0]
    assert sold(new) == sold(live)
    iss = lambda s: s.partition("LEFT JOIN ( SELECT ji.jute_mr_li_id")[2].partition(") iss")[0]
    assert iss(new) == iss(live)

    # Untouched columns keep their exact expressions.
    for col in (
        "jml.jute_mr_li_id AS jute_mr_li_id",
        "jm.out_date AS inward_date",
        "jml.actual_weight AS actual_weight",
        "ROUND((jml.actual_weight - IFNULL(iss.isswt, 0) - IFNULL(sold.soldwt, 0)), 3) AS bal_weight",
        "jml.accepted_weight AS accepted_weight",
        "jml.rate AS rate",
        "jml.actual_rate AS actual_rate",
        "jm.status_id AS mr_status_id",
        "IFNULL(sold.soldwt, 0) AS sold_weight",
    ):
        assert col in new and col in live, col

    # Same column list, same order (the ORM view model and every reader rely on it).
    aliases = lambda s: re.findall(r"\bAS (\w+)\s*(?:,| FROM jute_mr jm)", s.partition(" FROM jute_mr jm")[0] + " FROM jute_mr jm")
    assert aliases(new) == aliases(live)


def test_commentary_sits_after_the_statement_and_names_the_ordering():
    sql = NEW.read_text(encoding="utf-8")
    assert sql.lstrip().startswith("CREATE OR REPLACE VIEW"), "a leading comment would swallow the statement"
    tail = sql.partition(";")[2]
    assert "20260925_jute_waste_view_add_mr_lines.sql" in tail
    assert "ROLLBACK" in tail and "add_jute_mr_returned_status_48.sql" in tail
    assert ";" not in tail
