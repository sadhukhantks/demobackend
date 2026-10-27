"""Open-indent PO quantity rule.

Open indents carry no committed qty, so the ordinary "qty <= indent balance"
check can never pass for them. The branch min/max position governs instead:

    lots    = ROUNDUP((maxqty - cur_stock) / min_order_qty)
    ceiling = lots * min_order_qty

Rounding up is deliberate — the reorder lot is indivisible, so the ceiling may
exceed maxqty. Outstanding PO qty is not deducted.
"""

import pytest

from src.procurement.po import is_open_indent_type, validate_indent_line_qty


class _Row:
    def __init__(self, mapping):
        self._mapping = mapping


class _FakeDB:
    """Returns the limits row for the open-indent query; qty for the regular one."""

    def __init__(self, limits, indent_qty=0.0, other_po_qty=0.0):
        self.limits = limits
        self.indent_qty = indent_qty
        self.other_po_qty = other_po_qty
        self._calls = 0

    def execute(self, query, params=None):
        sql = str(query)
        self._calls += 1
        if "indent_type_id" in sql:
            return _Result(_Row(self.limits) if self.limits is not None else None)
        if "SELECT qty FROM proc_indent_dtl" in sql:
            return _Result((self.indent_qty,))
        return _Result((self.other_po_qty,))


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


def limits(**kw):
    base = {
        "indent_type": "OPEN",
        "branch_id": 29,
        "item_id": 280208,
        "maxqty": 420.0,
        "minqty": 210.0,
        "min_order_qty": 210.0,
        "cur_stock": 0.5,
    }
    base.update(kw)
    return base


@pytest.mark.parametrize("value", ["Open", "OPEN", "open", " Open "])
def test_open_type_detected_regardless_of_tenant_casing(value):
    # sls stores OPEN, dev3 stores Open.
    assert is_open_indent_type(value) is True


@pytest.mark.parametrize("value", ["Regular", "REGULAR", "BOM", "", None])
def test_non_open_types(value):
    assert is_open_indent_type(value) is False


# --- the reported bug ------------------------------------------------------

def test_open_line_is_not_rejected_against_zero_indent_balance():
    """The reported failure: qty 500 vs 'indent balance 0.0'.

    500 is still refused, but for the right reason (it exceeds the 420 ceiling),
    not because the open indent has no committed qty.
    """
    db = _FakeDB(limits(), indent_qty=0.0)
    err = validate_indent_line_qty(db, 25921, 500.0, po_dtl_id=31503)
    assert err is not None
    assert "indent balance" not in err
    assert "420" in err


@pytest.mark.parametrize("qty", [210.0, 420.0])
def test_allowed_quantities_for_the_reference_item(qty):
    """max 420, stock 0.5, reorder 210 -> ceil(419.5/210)=2 lots -> {210, 420}."""
    db = _FakeDB(limits())
    assert validate_indent_line_qty(db, 25921, qty) is None


@pytest.mark.parametrize("qty", [180.0, 200.0, 315.0])
def test_non_multiples_and_sub_minimum_are_refused(qty):
    # 180 is what PO 12718 actually holds — below min 210 and not a lot multiple.
    db = _FakeDB(limits())
    assert validate_indent_line_qty(db, 25921, qty) is not None


def test_ceiling_may_exceed_maxqty_because_the_lot_is_indivisible():
    # max 100, stock 8, reorder 10 -> ceil(92/10)=10 lots -> 100.
    db = _FakeDB(limits(maxqty=100.0, minqty=20.0, min_order_qty=10.0, cur_stock=8.0))
    assert validate_indent_line_qty(db, 1, 100.0) is None
    assert validate_indent_line_qty(db, 1, 110.0) is not None


def test_second_worked_example():
    # max 100, stock 2, reorder 5 -> ceil(98/5)=20 lots -> 100.
    db = _FakeDB(limits(maxqty=100.0, minqty=20.0, min_order_qty=5.0, cur_stock=2.0))
    assert validate_indent_line_qty(db, 1, 100.0) is None
    assert validate_indent_line_qty(db, 1, 105.0) is not None
    assert validate_indent_line_qty(db, 1, 15.0) is not None  # below min 20


def test_rounding_up_crosses_the_lot_boundary():
    # max 100, stock 1, reorder 30 -> ceil(99/30)=4 lots -> 120 > maxqty.
    db = _FakeDB(limits(maxqty=100.0, minqty=30.0, min_order_qty=30.0, cur_stock=1.0))
    assert validate_indent_line_qty(db, 1, 120.0) is None
    assert validate_indent_line_qty(db, 1, 150.0) is not None


# --- unconfigured / edge data ---------------------------------------------

def test_no_minmax_row_leaves_the_line_unconstrained():
    # 13 of indent 3953's 14 items are in this state on sls today.
    db = _FakeDB(limits(maxqty=None, minqty=None, min_order_qty=None, cur_stock=None))
    assert validate_indent_line_qty(db, 25921, 9999.0) is None


def test_no_reorder_lot_falls_back_to_plain_max():
    db = _FakeDB(limits(min_order_qty=None))
    assert validate_indent_line_qty(db, 1, 420.0) is None
    assert validate_indent_line_qty(db, 1, 421.0) is not None


def test_stock_at_or_above_max_blocks_ordering():
    db = _FakeDB(limits(cur_stock=420.0))
    err = validate_indent_line_qty(db, 1, 210.0)
    assert err is not None
    assert "nothing can be ordered" in err


def test_missing_indent_line_is_not_validated():
    assert validate_indent_line_qty(_FakeDB(None), 999, 100.0) is None
    assert validate_indent_line_qty(_FakeDB(limits()), None, 100.0) is None


# --- regression: Regular/BOM keep the indent-balance rule -------------------

def test_regular_line_still_checks_indent_balance():
    db = _FakeDB(limits(indent_type="Regular"), indent_qty=100.0, other_po_qty=30.0)
    assert validate_indent_line_qty(db, 1, 70.0) is None
    err = validate_indent_line_qty(db, 1, 71.0)
    assert err is not None
    assert "exceeds indent balance" in err


def test_regular_line_ignores_minmax_ceiling():
    """A Regular line within its indent balance is fine even past the max qty."""
    db = _FakeDB(limits(indent_type="REGULAR"), indent_qty=1000.0, other_po_qty=0.0)
    assert validate_indent_line_qty(db, 1, 900.0) is None
