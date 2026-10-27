"""TKT-000149: inventory valuations must net the SR line discount.

Item Ledger (and every other inventory valuation) priced receipts, issues and
returns at the gross COALESCE(accepted_rate, rate) while the SR screen, bill
pass and accounting posting use qty*rate - discount_amount. One shared
expression (lot_rate_sql) now feeds them all.
"""

import re
import sqlite3

from src.inventory.query import (
    get_available_inward_inventory_query,
    lot_rate_sql,
)
from src.inventory.reportQueries import (
    get_inventory_stock_report_query,
    get_issue_consumption_query,
    get_item_ledger_opening_query,
    get_item_ledger_txns_query,
)

GROSS_RATE = re.compile(r"COALESCE\(\w+\.accepted_rate,\s*\w+\.rate")


def _lot_value(approved_qty, rate, accepted_rate, discount_amount):
    """Evaluate approved_qty * lot_rate_sql('t') on stdlib sqlite (same COALESCE/NULLIF semantics)."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (approved_qty REAL, rate REAL, accepted_rate REAL, discount_amount REAL)")
    con.execute("INSERT INTO t VALUES (?, ?, ?, ?)", (approved_qty, rate, accepted_rate, discount_amount))
    return round(con.execute(f"SELECT approved_qty * {lot_rate_sql('t')} FROM t").fetchone()[0], 2)


def test_lot_rate_nets_discount_like_the_sr_line():
    # sls lot 23600 (LC/FACTORY/26-27/000035, item 101-0088A): 9 x 569 @ 40% -> SR amount 3072.60
    assert _lot_value(9, 569.0, None, 2048.4) == 3072.60
    # sls lot 42116: price lives in accepted_rate, rate = 0 -> 2 x 497 - 397.6
    assert _lot_value(2, 0.0, 497.0, 397.6) == 596.40
    # no discount -> unchanged gross valuation
    assert _lot_value(12, 281.7, None, 0.0) == 3380.40
    assert _lot_value(12, 281.7, None, None) == 3380.40
    # fully rejected line (approved 0, discount stored) must not go negative
    assert _lot_value(0, 337.0, None, 7683.6) == 0.0


def test_every_inventory_valuation_uses_the_shared_net_rate():
    queries = [
        str(get_inventory_stock_report_query()),
        str(get_item_ledger_opening_query()),
        str(get_item_ledger_txns_query()),
        str(get_issue_consumption_query("dept")),
        str(get_available_inward_inventory_query()),
    ]
    for sql in queries:
        assert "discount_amount" in sql
        # every gross-rate read must be wrapped by the net-rate expression
        for m in GROSS_RATE.finditer(sql):
            tail = sql[m.end(): m.end() + 120]
            assert "discount_amount / NULLIF" in tail, sql[m.start() - 40: m.end() + 120]


def test_item_ledger_values_all_three_movement_types_net():
    sql = str(get_item_ledger_txns_query())
    # receipts (pid), issues + returns (lot) each valued at the net lot rate
    assert sql.count("pid.discount_amount / NULLIF(pid.approved_qty, 0)") == 1
    assert sql.count("lot.discount_amount / NULLIF(lot.approved_qty, 0)") == 2
