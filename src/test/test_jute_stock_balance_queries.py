"""
Guard for the Cluster A jute balance reports (/stock, /qty-wise, /with-value).

The bug these reports had: the receipt side and the issue side used DIFFERENT
eligibility rules, so material left stock that never arrived (sls branch 29 read
-52,720 qtl against a true balance of 0). These are structural checks — they
fail if the two sides ever drift apart again. No DB needed.
"""

import pytest
from sqlalchemy.dialects import mysql

from src.juteProcurement.constants import (
    JUTE_WASTE_ITEM_TYPE_ID,
    RAW_JUTE_ITEM_TYPE_ID,
    JUTE_MR_LI_EXCLUDED_STATUSES,
    JUTE_MR_STOCK_EXCLUDED_STATUSES,
    RAW_JUTE_INVOICE_TYPE_ID,
    SALES_APPROVED_STATUS_ID,
)
from src.juteProcurement.reportQueries import (
    get_jute_qty_wise_report_query,
    get_jute_stock_report_query,
    get_jute_with_value_query,
)

BALANCE_QUERIES = [
    ("stock", get_jute_stock_report_query, {"branch_id", "report_date"}),
    ("qty_wise", get_jute_qty_wise_report_query, {"branch_id", "date_from", "date_to"}),
    ("with_value", get_jute_with_value_query, {"branch_id", "date_from", "date_to"}),
]


@pytest.mark.parametrize("name,builder,caller_params", BALANCE_QUERIES)
def test_query_compiles_with_only_caller_params(name, builder, caller_params):
    """Compiles as MySQL, and the router's param dict is still enough."""
    stmt = builder()
    compiled = stmt.compile(dialect=mysql.dialect())
    required = set(compiled.params) - set(stmt._bindparams)
    assert required <= caller_params, f"{name} needs unexpected params: {required}"
    # The sales-side ids are pre-bound, not inline literals.
    assert compiled.params["raw_jute_invoice_type_id"] == RAW_JUTE_INVOICE_TYPE_ID
    assert compiled.params["approved_status_id"] == SALES_APPROVED_STATUS_ID


@pytest.mark.parametrize("name,builder,_", BALANCE_QUERIES)
def test_receipt_and_issue_share_one_eligibility_rule(name, builder, _):
    sql = str(builder())
    mr_rule = f"status_id NOT IN ({', '.join(str(s) for s in JUTE_MR_STOCK_EXCLUDED_STATUSES)})"
    li_rule = f"NOT IN ({', '.join(repr(s) for s in JUTE_MR_LI_EXCLUDED_STATUSES)})"

    # One receipt CTE and one issue CTE per period -> the rule appears an even
    # number of times, and identically on both sides.
    assert sql.count(mr_rule) >= 2 and sql.count(mr_rule) % 2 == 0, name
    assert sql.count(li_rule) == sql.count(mr_rule), name
    assert sql.count("mrli.active = 1") == sql.count(mr_rule), name

    # The old asymmetric rules must be gone.
    assert "IN (1, 3, 13)" not in sql, f"{name} still uses the old receipt rule"
    assert "ji.status_id IN (1, 3)" not in sql, f"{name} still uses the old issue rule"
    # Issues attribute to the MR line, never to a COALESCE that can go NULL.
    assert "COALESCE(ji.item_id" not in sql, f"{name} still COALESCEs the issue item"


@pytest.mark.parametrize("name,builder,_", BALANCE_QUERIES)
def test_both_stock_sources_and_both_sinks_present(name, builder, _):
    sql = str(builder())
    assert "jute_prod_waste_entry w" in sql, f"{name} misses waste as a stock source"
    assert "ji.waste_entry_id IS NOT NULL" in sql, f"{name} misses waste issues"
    assert "sales_invoice sinv" in sql, f"{name} misses raw-jute sales"
    assert "COALESCE(jm.out_date, jm.jute_mr_date, jm.jute_gate_entry_date)" in sql


class TestWasteVsRawJuteSplit:
    """The stock report tags each row's stock TYPE so callers can split totals.

    The Jute Stock Report's summary cards break Receipt / Issue (and their MTD
    twins) into purchased raw jute vs process waste. That split has to come from
    the item group's own item_type_id — the master flag — and never from a name
    match on the group, which would silently misclassify any group someone
    renames.
    """

    def test_report_flags_waste_rows_from_the_item_type_master(self):
        sql = str(get_jute_stock_report_query())
        assert (
            f"CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN 1 ELSE 0 END AS is_waste"
            in sql
        )

    def test_the_split_is_not_a_name_match(self):
        sql = str(get_jute_stock_report_query())
        assert "item_grp_name LIKE" not in sql
        assert "'Jute Waste'" not in sql

    def test_waste_type_id_is_the_one_the_issue_picker_uses(self):
        """Both must agree, or stock issuable as waste could report as raw jute."""
        from src.juteProcurement.query import get_jute_issue_create_setup_query

        assert JUTE_WASTE_ITEM_TYPE_ID == 3
        assert f"ig.item_type_id IN ({RAW_JUTE_ITEM_TYPE_ID}, {JUTE_WASTE_ITEM_TYPE_ID})" in str(
            get_jute_issue_create_setup_query()
        )


class TestPurchasedWasteFlowsThroughTheReportCtes:
    """Owner ruling 2026-09-25 (D5): purchased waste — an MR line of a type-3
    item, issued through kg slices keyed by jute_mr_li_id — flows through the
    MR arms of both CTEs like any MR line for WEIGHT and VALUE. Its QUANTITY
    is the exception: the line is bought in bales/loose units (sls line 45659:
    108 units against 10,042 kg — 0 of 486 sls waste lines have actual_qty ==
    actual_weight) but issued in kg, so booking the receipt in units and the
    issue in kg closed the item at -392 in the qty column. For a type-3 line
    the qty ledger is therefore the weight ledger on both sides.

    Proven structurally (the item group is LEFT JOINed for the flag, never as
    a filter, and the qty expression switches on it) and by executing the two
    CTEs on an in-memory SQLite fixture seeded with the real bale/kg shape."""

    QTY_CASE = f"CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN"

    def test_receipt_mr_arm_joins_the_item_group_as_a_flag_not_a_filter(self):
        from src.juteProcurement.reportQueries import _receipt_cte

        sql = " ".join(_receipt_cte("{d} <= :report_date").split())
        mr_arm = sql.split("UNION ALL")[0]
        assert "FROM jute_mr jm" in mr_arm
        assert "LEFT JOIN item_mst im ON im.item_id = mrli.actual_item_id" in mr_arm
        assert "LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in mr_arm
        assert "item_type_id" not in mr_arm.partition("WHERE")[2], "flag, not filter"
        assert "mrli.actual_item_id AS item_id" in mr_arm
        assert (
            f"{self.QTY_CASE} COALESCE(mrli.actual_weight, 0) "
            "ELSE COALESCE(mrli.actual_qty, 0) END AS qty" in mr_arm
        )

    def test_receipt_accounts_basis_books_waste_qty_at_the_book_weight(self):
        from src.juteProcurement.reportQueries import _MR_ACCOUNTS_WEIGHT, _receipt_cte

        sql = " ".join(_receipt_cte("{d} <= :report_date", basis="accounts").split())
        assert (
            f"{self.QTY_CASE} {_MR_ACCOUNTS_WEIGHT} ELSE COALESCE(mrli.actual_qty, 0) END AS qty"
            in sql
        )

    def test_issue_mr_arm_counts_mr_keyed_slices_once_and_in_kg(self):
        from src.juteProcurement.reportQueries import _issue_cte

        sql = " ".join(_issue_cte("{d} <= :report_date").split())
        mr_arm, waste_arm = sql.split("UNION ALL")
        assert "INNER JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id" in mr_arm
        assert "LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in mr_arm
        assert "item_type_id" not in mr_arm.partition("WHERE")[2], "flag, not filter"
        assert (
            f"{self.QTY_CASE} COALESCE(ji.weight, 0) ELSE COALESCE(ji.quantity, 0) END AS qty"
            in mr_arm
        )
        # the waste arm keys on waste_entry_id and REFUSES an MR-keyed row
        assert "ji.waste_entry_id IS NOT NULL" in waste_arm
        assert "ji.jute_mr_li_id IS NULL" in waste_arm

    def test_is_waste_comes_from_the_item_group_type_not_the_stock_source(self):
        sql = str(get_jute_stock_report_query())
        assert "INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in sql
        assert f"CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN 1 ELSE 0 END AS is_waste" in sql

    @pytest.fixture
    def ledger(self):
        """Branch 27: one raw-jute MR line (item 10, 20 bales / 3,000 kg), one
        purchased-waste MR line booked as sls books it (item 30 in a type-3
        group, 108 BALES / 10,042 kg, no actual_rate), one floor waste booking
        (item 30, 56 kg), and issues: a 2-bale issue on line 1, a 500 kg FIFO
        slice keyed by the waste MR line (quantity 500 = kg, as create_issue
        stores a grouped pick), a 56 kg slice keyed by the waste booking, and a
        rejected slice."""
        from sqlalchemy import create_engine, text

        eng = create_engine("sqlite://")
        with eng.begin() as c:
            c.execute(text("CREATE TABLE jute_mr (jute_mr_id INT, branch_id INT, out_date TEXT, "
                           "jute_mr_date TEXT, jute_gate_entry_date TEXT, status_id INT)"))
            c.execute(text("CREATE TABLE jute_mr_li (jute_mr_li_id INT, jute_mr_id INT, actual_item_id INT, "
                           "actual_qty REAL, actual_weight REAL, accepted_weight REAL, rate REAL, "
                           "total_price REAL, active INT, status TEXT)"))
            c.execute(text("CREATE TABLE item_mst (item_id INT, item_grp_id INT)"))
            c.execute(text("CREATE TABLE item_grp_mst (item_grp_id INT, item_type_id INT)"))
            c.execute(text("CREATE TABLE dept_mst (dept_id INT, branch_id INT)"))
            c.execute(text("CREATE TABLE jute_prod_waste_entry (waste_entry_id INT, item_id INT, "
                           "waste_qty REAL, dept_id INT, branch_id INT, tran_date TEXT, active INT)"))
            c.execute(text("CREATE TABLE jute_issue (jute_issue_id INT, branch_id INT, issue_date TEXT, "
                           "jute_mr_li_id INT, waste_entry_id INT, item_id INT, quantity REAL, "
                           "weight REAL, issue_value REAL, status_id INT)"))
            c.execute(text("INSERT INTO item_grp_mst VALUES (11, 2), (42, 3)"))
            c.execute(text("INSERT INTO item_mst VALUES (10, 11), (30, 42)"))
            c.execute(text("INSERT INTO jute_mr VALUES (1, 27, '2026-09-01', '2026-09-01', '2026-09-01', 3)"))
            c.execute(text("INSERT INTO jute_mr_li VALUES (11, 1, 10, 20, 3000, 3000, 5000, 150000, 1, NULL)"))
            c.execute(text("INSERT INTO jute_mr_li VALUES (45659, 1, 30, 108, 10042, 10042, 13500, 1355670, 1, NULL)"))
            c.execute(text("INSERT INTO dept_mst VALUES (7, 27)"))
            c.execute(text("INSERT INTO jute_prod_waste_entry VALUES (101, 30, 56, 7, NULL, '2026-09-02', 1)"))
            c.execute(text("INSERT INTO jute_issue VALUES (1, 27, '2026-09-03', 11, NULL, 10, 2, 300, 15000, 3)"))
            c.execute(text("INSERT INTO jute_issue VALUES (2, 27, '2026-09-03', 45659, NULL, 30, 500, 500, 67500, 21)"))
            c.execute(text("INSERT INTO jute_issue VALUES (3, 27, '2026-09-03', NULL, 101, 30, 56, 56, 476, 21)"))
            c.execute(text("INSERT INTO jute_issue VALUES (4, 27, '2026-09-03', 45659, NULL, 30, 50, 50, 6750, 4)"))
        return eng

    @staticmethod
    def _run(ledger, cte):
        from sqlalchemy import text

        sql = f"SELECT item_id, wt, qty, val FROM ({cte}) x ORDER BY item_id"
        with ledger.connect() as c:
            return [tuple(r) for r in c.execute(text(sql), {"branch_id": 27, "report_date": "2026-09-30"})]

    def test_executed_receipt_books_the_purchased_waste_line_in_kg_by_its_item(self, ledger):
        from src.juteProcurement.reportQueries import _receipt_cte

        assert self._run(ledger, _receipt_cte("{d} <= :report_date")) == [
            (10, 3000.0, 20.0, 150000.0),          # raw jute: qty stays in bales
            (30, 10098.0, 10098.0, 1355670.0),     # 10,042 kg purchased (qty = kg, not 108 bales) + 56 kg floor
        ]

    def test_executed_issue_counts_the_mr_keyed_waste_slice_once_in_kg(self, ledger):
        from src.juteProcurement.reportQueries import _issue_cte

        assert self._run(ledger, _issue_cte("{d} <= :report_date")) == [
            (10, 300.0, 2.0, 15000.0),
            # 500 kg via the MR arm (jute_mr_li_id) + 56 kg via the waste arm;
            # the rejected 50 kg slice is out; nothing is double counted.
            (30, 556.0, 556.0, 67976.0),
        ]

    def test_executed_closing_qty_of_the_waste_item_is_not_negative(self, ledger):
        """Before: receipt qty 108 (bales) - issue qty 500 (kg) = -392."""
        from src.juteProcurement.reportQueries import _issue_cte, _receipt_cte

        receipt = dict((r[0], r) for r in self._run(ledger, _receipt_cte("{d} <= :report_date")))
        issue = dict((r[0], r) for r in self._run(ledger, _issue_cte("{d} <= :report_date")))
        closing_qty = receipt[30][2] - issue[30][2]
        closing_wt = receipt[30][1] - issue[30][1]
        assert closing_qty == closing_wt == pytest.approx(9542.0)
