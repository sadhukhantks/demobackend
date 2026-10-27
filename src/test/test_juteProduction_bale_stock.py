"""Bale Stock & Inspection router (/api/baleStock) — src/juteProduction/bale_stock.py.

Mocked-session tests in the house style of test_juteProduction_finishing_stock.py:
the real app is imported and auth / DB are replaced through dependency_overrides.
execute() is driven with a side_effect list, one entry per statement the handler
runs, in order:

  /setup      qualities(fetchall) -> series(fetchall) -> bale-source probe(fetchone)
  /bales      rows(fetchall)                         (invoice_id rides as a bind, §7e)
  /suggest    rows(fetchall)                         (§7e; ordering + count cut in Python)
  /summary    bale-source probe(fetchone) -> summary(fetchall) -> series summary(fetchall)
  /invoices   heads(fetchall) -> [lines(fetchall) -> linked counts(fetchall)]  (only when heads)
  /inspect    rows for the id list(fetchall) -> one INSERT per bale
  /uninspect  one UPDATE (rowcount)
  /despatch   [invoice header(fetchone) — only when invoice_id is given]
              -> rows for the id list(fetchall) -> [UPDATE retiring void links]
              -> one INSERT per in-stock bale -> one UPDATE (rowcount) per bale
              relinked from a no-invoice despatch (§7b)
  /undespatch one UPDATE (rowcount)

Every statement is checked with _assert_binds_complete. The pure helpers
(serialize_bale_row, build_bale_stock_summary, build_bale_stock_series_lines,
derive_bale_source) are exercised directly with plain dicts.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.bale_stock import (
    ALREADY_DESPATCHED_MSG,
    ALREADY_DESPATCHED_NO_INVOICE_MSG,
    ALREADY_INSPECTED_MSG,
    DESPATCH_DATE_REQUIRED_MSG,
    DESPATCH_REASON_MSG,
    DESPATCH_REF_TOO_LONG_MSG,
    EMPTY_IDS_MSG,
    INSPECT_BEFORE_PRESS_MSG,
    INSPECT_SACKING_ONLY_MSG,
    INVOICE_BRANCH_MSG,
    INVOICE_CANCELLED_MSG,
    INVOICE_NOT_FOUND_MSG,
    INVOICE_TYPE_MSG,
    NO_ACTIVE_DESPATCH_MSG,
    NO_ACTIVE_INSPECTION_MSG,
    NOT_IN_STOCK_MSG,
    REF_TOO_LONG_MSG,
    RELINK_STALE_MSG,
    SUGGEST_COUNT_MSG,
    SUGGEST_PURPOSE_MSG,
    SUGGEST_SACKING_ONLY_MSG,
    TOO_MANY_IDS_MSG,
    build_bale_stock_series_lines,
    build_bale_stock_summary,
    derive_bale_source,
    serialize_bale_row,
    suggest_sort_key,
)
from src.juteProduction.bale_stock_query import (
    get_bale_stock_rows_query,
    get_bale_stock_series_summary_query,
    get_bale_stock_summary_query,
    insert_bale_despatch_query,
    update_bale_despatch_invoice_query,
)
from src.juteProduction.constants import (
    BALE_DESPATCH_REASONS,
    BALE_STOCK_MAX_IDS,
    BALE_STOCK_ROW_CAP,
)
from src.main import app

client = TestClient(app)

BASE = "/api/baleStock"
TODAY = date.today()
PAST = TODAY - timedelta(days=5)
FUTURE = TODAY + timedelta(days=1)
_UNSET = object()


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET, rowcount=_UNSET):
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    if rowcount is not _UNSET:
        res.rowcount = rowcount
    return res


def _assert_binds_complete(call):
    """Every named bind the statement declares must be a key in the params dict
    (call.args[0] = the text() statement, call.args[1] = the params dict)."""
    missing = set(call.args[0].compile().binds) - set(call.args[1])
    assert not missing, missing


def _flat(**over):
    """One get_bale_stock_rows_query() row: an in-stock sacking bale by default."""
    m = {
        "bale_entry_id": 17, "tran_date": PAST, "entry_type": "PROD",
        "bale_series_id": 1, "series_name": "PUNJAB", "bale_no": 612,
        "original_bale_no": None, "finishing_quality_id": 2, "item_id": 270011,
        "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2,
        "gross_wt": Decimal("340.000"), "net_wt": Decimal("290.000"),
        "weigh_time": datetime(2026, 10, 10, 9, 15, 30),
        "repacked_by_id": None, "repacked_by_no": None, "repacked_on": None,
        "bale_inspection_id": None, "inspection_date": None, "inspection_ref": None,
        "inspector": None, "inspection_remarks": None,
        "bale_despatch_id": None, "invoice_id": None, "despatch_date": None,
        "despatch_reason": None, "despatch_ref": None,
        "despatch_remarks": None, "invoice_no": None, "invoice_date": None,
        "invoice_type": None, "invoice_status_id": None, "party_name": None,
        "status": "IN_STOCK",
    }
    m.update(over)
    return m


def _inspected(**over):
    return _flat(
        bale_inspection_id=3, inspection_date=PAST, inspection_ref="IC/22",
        inspector="IJIRA", status="INSPECTED", **over,
    )


def _despatched(invoice_status_id=3, status="DESPATCHED", **over):
    return _flat(
        bale_despatch_id=5, invoice_id=171, despatch_date=PAST, invoice_no=52,
        invoice_date=PAST, invoice_type=3, invoice_status_id=invoice_status_id,
        party_name="BIHAR STATE FOOD CIVIL SUPPLIES", status=status, **over,
    )


def _despatched_no_invoice(**over):
    """§7b: an active despatch row WITHOUT invoice — status DESPATCHED, every
    si.* column NULL (the LEFT JOIN found nothing)."""
    m = dict(bale_despatch_id=6, invoice_id=None, despatch_date=PAST,
             despatch_reason="CHALLAN", despatch_ref="DC/41", status="DESPATCHED")
    m.update(over)
    return _flat(**m)


def _repacked(**over):
    return _flat(repacked_by_id=40, repacked_by_no=619, repacked_on=PAST,
                 status="REPACKED", **over)


def _summary_row(**over):
    m = {
        "finishing_quality_id": 2, "item_id": 270011,
        "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2,
        "bales": Decimal("120"), "inspected_bales": Decimal("80"),
        "total_kg": Decimal("34800.000"), "inspected_kg": Decimal("23200.000"),
        "despatched_bales": Decimal("45"),
    }
    m.update(over)
    return m


def _series_row(**over):
    """One get_bale_stock_series_summary_query() row: PUNJAB sacking by default."""
    m = {
        "bale_series_id": 1, "series_name": "PUNJAB",
        "finishing_quality_id": 2, "item_id": 270011,
        "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2,
        "bales": Decimal("70"), "inspected_bales": Decimal("50"),
        "total_kg": Decimal("20300.000"), "inspected_kg": Decimal("14500.000"),
        "despatched_bales": Decimal("30"),
    }
    m.update(over)
    return m


def _invoice(**over):
    attrs = {"invoice_id": 171, "branch_id": 2, "invoice_type": 3, "status_id": 3,
             "invoice_date": PAST, "active": 1}
    attrs.update(over)
    return _attr_row(**attrs)


def _inspect_body(**over):
    body = {"co_id": 1, "branch_id": 2, "bale_entry_ids": [17],
            "inspection_date": str(TODAY), "inspection_ref": "IC/22",
            "inspector": "IJIRA", "remarks": None}
    body.update(over)
    return body


def _despatch_body(**over):
    body = {"co_id": 1, "branch_id": 2, "bale_entry_ids": [17], "invoice_id": 171,
            "despatch_date": None, "despatch_reason": None, "despatch_ref": None,
            "remarks": None}
    body.update(over)
    return body


def _no_invoice_body(**over):
    """§7b despatch without invoice: date + reason required."""
    body = _despatch_body(invoice_id=None, despatch_date=str(PAST),
                          despatch_reason="CHALLAN", despatch_ref="DC/41")
    body.update(over)
    return body


DESPATCH_KEYS = {
    "bale_despatch_id", "invoice_id", "invoice_no", "invoice_date", "invoice_type",
    "invoice_status_id", "party_name", "despatch_date", "despatch_reason",
    "despatch_reason_label", "despatch_ref", "remarks",
}


# =============================================================================
# Pure helpers
# =============================================================================


class TestDeriveBaleSource:
    def test_values(self):
        assert derive_bale_source(True) == "BALE_PROD"
        assert derive_bale_source(False) == "MANUAL"


class TestSuggestSortKey:
    """§7e ordering, on serialised rows (tran_date is an ISO string)."""

    def test_despatch_inspected_first_then_oldest_then_bale_no(self):
        rows = [
            {"bale_entry_id": 1, "status": "IN_STOCK", "tran_date": "2026-10-01", "bale_no": 5},
            {"bale_entry_id": 2, "status": "INSPECTED", "tran_date": "2026-10-03", "bale_no": 1},
            {"bale_entry_id": 3, "status": "INSPECTED", "tran_date": "2026-10-02", "bale_no": 9},
            {"bale_entry_id": 4, "status": "IN_STOCK", "tran_date": "2026-10-01", "bale_no": 2},
        ]
        rows.sort(key=suggest_sort_key("despatch"))
        assert [r["bale_entry_id"] for r in rows] == [3, 2, 4, 1]

    def test_inspect_ignores_status_orders_date_then_bale_no(self):
        rows = [
            {"bale_entry_id": 1, "status": "IN_STOCK", "tran_date": "2026-10-02", "bale_no": 1},
            {"bale_entry_id": 2, "status": "INSPECTED", "tran_date": "2026-10-01", "bale_no": 7},
            {"bale_entry_id": 3, "status": "IN_STOCK", "tran_date": "2026-10-01", "bale_no": 3},
        ]
        rows.sort(key=suggest_sort_key("inspect"))
        assert [r["bale_entry_id"] for r in rows] == [3, 2, 1]

    def test_tie_broken_by_bale_entry_id_and_nulls_tolerated(self):
        rows = [
            {"bale_entry_id": 9, "status": "IN_STOCK", "tran_date": None, "bale_no": None},
            {"bale_entry_id": 4, "status": "IN_STOCK", "tran_date": None, "bale_no": None},
        ]
        rows.sort(key=suggest_sort_key("despatch"))
        assert [r["bale_entry_id"] for r in rows] == [4, 9]


class TestSerializeBaleRow:
    def test_in_stock_row_has_null_nests_and_plain_types(self):
        row = serialize_bale_row(_mock_row(_flat()))
        assert row["bale_entry_id"] == 17
        assert row["tran_date"] == str(PAST)
        assert row["bale_no"] == 612
        assert row["gross_wt"] == 340.0 and isinstance(row["gross_wt"], float)
        assert row["net_wt"] == 290.0
        assert row["weigh_time"] == "2026-10-10T09:15:30"
        assert row["status"] == "IN_STOCK"
        assert row["inspection"] is None
        assert row["despatch"] is None
        assert row["repacked_by"] is None
        assert set(row) == {
            "bale_entry_id", "tran_date", "entry_type", "bale_series_id", "series_name",
            "bale_no", "original_bale_no", "finishing_quality_id", "item_id", "item_name",
            "quality_type", "gross_wt", "net_wt", "weigh_time", "status", "inspection",
            "despatch", "repacked_by",
        }

    def test_inspection_nested(self):
        row = serialize_bale_row(_inspected(inspection_remarks="ok"))
        assert row["status"] == "INSPECTED"
        assert row["inspection"] == {
            "bale_inspection_id": 3, "inspection_date": str(PAST),
            "inspection_ref": "IC/22", "inspector": "IJIRA", "remarks": "ok",
        }

    def test_despatch_nested(self):
        row = serialize_bale_row(_despatched(despatch_remarks="lorry 1"))
        assert row["status"] == "DESPATCHED"
        assert row["despatch"] == {
            "bale_despatch_id": 5, "invoice_id": 171, "invoice_no": 52,
            "invoice_date": str(PAST), "invoice_type": 3, "invoice_status_id": 3,
            "party_name": "BIHAR STATE FOOD CIVIL SUPPLIES",
            "despatch_date": str(PAST), "despatch_reason": None,
            "despatch_reason_label": None, "despatch_ref": None, "remarks": "lorry 1",
        }
        assert set(row["despatch"]) == DESPATCH_KEYS

    def test_despatch_with_invoice_and_reason_carries_label(self):
        row = serialize_bale_row(_despatched(despatch_reason="SAMPLE", despatch_ref="GP/9"))
        assert row["despatch"]["invoice_id"] == 171
        assert row["despatch"]["despatch_reason"] == "SAMPLE"
        assert row["despatch"]["despatch_reason_label"] == "Sample"
        assert row["despatch"]["despatch_ref"] == "GP/9"

    def test_despatch_without_invoice_nested(self):
        """§7b: a no-invoice despatch is DESPATCHED; the invoice sub-fields are
        None and the reason code comes back with its label."""
        row = serialize_bale_row(_mock_row(_despatched_no_invoice(despatch_remarks="to Bihar")))
        assert row["status"] == "DESPATCHED"
        assert row["despatch"] == {
            "bale_despatch_id": 6, "invoice_id": None, "invoice_no": None,
            "invoice_date": None, "invoice_type": None, "invoice_status_id": None,
            "party_name": None, "despatch_date": str(PAST),
            "despatch_reason": "CHALLAN", "despatch_reason_label": "Delivery challan",
            "despatch_ref": "DC/41", "remarks": "to Bihar",
        }

    def test_despatch_without_invoice_masks_stray_invoice_columns(self):
        """Even if the row somehow carried si.* values with a NULL invoice_id,
        the invoice sub-fields are None — no invoice means no invoice."""
        row = serialize_bale_row(_despatched_no_invoice(invoice_no=52, party_name="X",
                                                        invoice_status_id=3))
        assert row["despatch"]["invoice_id"] is None
        assert row["despatch"]["invoice_no"] is None
        assert row["despatch"]["party_name"] is None
        assert row["despatch"]["invoice_status_id"] is None

    def test_despatch_unknown_reason_code_falls_back_to_code(self):
        row = serialize_bale_row(_despatched_no_invoice(despatch_reason="LEGACY"))
        assert row["despatch"]["despatch_reason"] == "LEGACY"
        assert row["despatch"]["despatch_reason_label"] == "LEGACY"

    def test_despatch_to_cancelled_invoice_is_returned_with_in_stock_status(self):
        """D4: the link row is still returned so the FE can show 'linked to
        cancelled invoice' — but the SQL status says the bale is back in stock."""
        row = serialize_bale_row(_despatched(invoice_status_id=6, status="IN_STOCK"))
        assert row["status"] == "IN_STOCK"
        assert row["despatch"]["invoice_status_id"] == 6
        assert row["despatch"]["invoice_id"] == 171

    def test_repacked_by_nested(self):
        row = serialize_bale_row(_repacked())
        assert row["status"] == "REPACKED"
        assert row["repacked_by"] == {"bale_entry_id": 40, "bale_no": 619,
                                      "tran_date": str(PAST)}


class TestBuildBaleStockSummary:
    def test_lines_and_totals(self):
        out = build_bale_stock_summary([
            _summary_row(),
            _summary_row(finishing_quality_id=1, item_id=269959, item_name="HESSIAN",
                         quality_type=1, bales=9, inspected_bales=0,
                         total_kg=Decimal("3619.700"), inspected_kg=0, despatched_bales=2),
        ])
        sack, hess = out["lines"]
        assert sack["bales"] == 120 and isinstance(sack["bales"], int)
        assert sack["inspected_bales"] == 80
        assert sack["total_kg"] == 34800.0
        assert sack["inspected_kg"] == 23200.0
        assert sack["per_wt_kg"] == 290.0
        assert sack["wt_mt"] == 34.8
        assert sack["despatched_bales"] == 45
        assert hess["per_wt_kg"] == round(3619.7 / 9, 3)
        assert hess["wt_mt"] == 3.62
        totals = out["totals"]
        assert totals["bales"] == 129
        assert totals["inspected_bales"] == 80
        assert totals["wt_mt"] == round(34.8 + 3.62, 3)
        assert totals["despatched_bales"] == 47
        assert totals["hessian"] == {"bales": 9, "inspected_bales": 0, "wt_mt": 3.62}
        assert totals["sacking"] == {"bales": 120, "inspected_bales": 80, "wt_mt": 34.8}

    def test_zero_bales_has_null_per_weight(self):
        out = build_bale_stock_summary([_summary_row(bales=0, total_kg=0, inspected_bales=0)])
        line = out["lines"][0]
        assert line["bales"] == 0
        assert line["per_wt_kg"] is None
        assert line["wt_mt"] == 0.0

    def test_empty(self):
        out = build_bale_stock_summary([])
        assert out["lines"] == []
        assert out["totals"]["bales"] == 0
        assert out["totals"]["hessian"]["wt_mt"] == 0.0
        assert out["series_lines"] == []  # positional call = no series rows

    # ---- §7a series breakup ------------------------------------------------

    def test_series_lines_fields_and_measures(self):
        out = build_bale_stock_summary([_summary_row()], [_series_row()])
        assert len(out["series_lines"]) == 1
        line = out["series_lines"][0]
        assert line["bale_series_id"] == 1 and isinstance(line["bale_series_id"], int)
        assert line["series_name"] == "PUNJAB"
        assert line["finishing_quality_id"] == 2
        assert line["item_id"] == 270011
        assert line["item_name"] == "PRINTED TYPE A JUTE BAGS"
        assert line["quality_type"] == 2
        assert line["bales"] == 70 and isinstance(line["bales"], int)
        assert line["inspected_bales"] == 50 and isinstance(line["inspected_bales"], int)
        assert line["total_kg"] == 20300.0
        assert line["inspected_kg"] == 14500.0
        assert line["per_wt_kg"] == 290.0  # total_kg / bales, same rule as the quality line
        assert line["wt_mt"] == 20.3
        assert line["despatched_bales"] == 30
        # the series line carries exactly the summary-line keys plus the two series keys
        assert set(line) == set(out["lines"][0]) | {"bale_series_id", "series_name"}

    def test_series_lines_do_not_change_totals(self):
        """A series line is a breakup of a quality line, never extra stock."""
        with_series = build_bale_stock_summary([_summary_row()], [_series_row(), _series_row(
            bale_series_id=2, series_name="BIHAR", bales=50, inspected_bales=30,
            total_kg=Decimal("14500.000"), inspected_kg=Decimal("8700.000"),
            despatched_bales=15,
        )])
        without = build_bale_stock_summary([_summary_row()])
        assert with_series["totals"] == without["totals"]
        assert with_series["lines"] == without["lines"]
        assert sum(l["bales"] for l in with_series["series_lines"]) == 120

    def test_series_lines_sorted_quality_type_series_item(self):
        """Hessian series first, then sacking by series name then item name —
        the statement's ORDER BY, re-applied so the order holds for any source."""
        rows = [
            _series_row(bale_series_id=2, series_name="PUNJAB", finishing_quality_id=7,
                        item_id=270012, item_name="B TWILL"),
            _series_row(bale_series_id=2, series_name="PUNJAB"),
            _series_row(bale_series_id=3, series_name="BIHAR"),
            _series_row(bale_series_id=1, series_name="40-6.50", finishing_quality_id=1,
                        item_id=269959, item_name="HESSIAN CLOTH 40-6.5", quality_type=1),
        ]
        lines = build_bale_stock_series_lines(rows)
        assert [(l["quality_type"], l["series_name"], l["item_name"]) for l in lines] == [
            (1, "40-6.50", "HESSIAN CLOTH 40-6.5"),
            (2, "BIHAR", "PRINTED TYPE A JUTE BAGS"),
            (2, "PUNJAB", "B TWILL"),
            (2, "PUNJAB", "PRINTED TYPE A JUTE BAGS"),
        ]
        assert build_bale_stock_summary([], rows)["series_lines"] == lines

    def test_series_line_zero_bales_has_null_per_weight(self):
        line = build_bale_stock_series_lines([
            _series_row(bales=0, inspected_bales=0, total_kg=0, inspected_kg=0)
        ])[0]
        assert line["bales"] == 0
        assert line["per_wt_kg"] is None
        assert line["wt_mt"] == 0.0

    def test_series_line_retired_series_keeps_null_name(self):
        """A bale whose series row is gone still counts (LEFT JOIN) and sorts
        first within its quality type instead of raising."""
        lines = build_bale_stock_series_lines([
            _series_row(), _series_row(bale_series_id=None, series_name=None),
        ])
        assert lines[0]["series_name"] is None and lines[0]["bale_series_id"] is None
        assert lines[1]["series_name"] == "PUNJAB"

    def test_series_lines_empty_inputs(self):
        assert build_bale_stock_series_lines([]) == []
        assert build_bale_stock_summary([], None)["series_lines"] == []
        assert build_bale_stock_summary([], [])["series_lines"] == []


# =============================================================================
# Statement text (the §7b derivation and write builders)
# =============================================================================


class TestDespatchStatements:
    def test_rows_query_status_case_treats_no_invoice_as_despatched(self):
        """DESPATCHED when a despatch row exists AND (no invoice OR the invoice
        is not cancelled) — the clause text, exactly as the spec writes it."""
        stmt = str(get_bale_stock_rows_query())
        assert "dp.invoice_id IS NULL" in stmt
        assert "WHEN dp.bale_despatch_id IS NOT NULL" in stmt
        assert "si.status_id NOT IN" in stmt
        # the no-invoice clause sits INSIDE the DESPATCHED arm, before the invoice test
        arm = stmt[stmt.index("WHEN dp.bale_despatch_id IS NOT NULL"):stmt.index("THEN 'DESPATCHED'")]
        assert "dp.invoice_id IS NULL" in arm and "si.status_id IS NULL" in arm
        assert "si.status_id NOT IN" in arm
        # the two new columns ride along with the row
        assert "dp.despatch_reason" in stmt and "dp.despatch_ref" in stmt

    def test_rows_query_invoice_filter_inside_inner_select_only(self):
        """§7e: ``:invoice_id`` is an optional filter on the active despatch row,
        placed INSIDE the derivation (before ') x'), always declared; the two
        summary builders do not carry it."""
        q = get_bale_stock_rows_query()
        stmt = str(q)
        clause = "AND (:invoice_id IS NULL OR dp.invoice_id = :invoice_id)"
        assert clause in stmt
        assert stmt.index(clause) < stmt.index(") x")
        assert "invoice_id" in set(q.compile().binds)
        for builder in (get_bale_stock_summary_query, get_bale_stock_series_summary_query):
            assert "invoice_id" not in set(builder().compile().binds)
            assert clause not in str(builder())

    @pytest.mark.parametrize("builder", [
        get_bale_stock_summary_query, get_bale_stock_series_summary_query,
    ])
    def test_summary_queries_share_the_same_case(self, builder):
        """despatched_bales reads the derived status column, so both summaries
        inherit the no-invoice clause from the shared inner select."""
        stmt = str(builder())
        assert "dp.invoice_id IS NULL" in stmt
        assert "x.status = 'DESPATCHED' THEN 1 ELSE 0 END" in stmt
        assert "AS despatched_bales" in stmt

    def test_insert_carries_reason_and_ref(self):
        stmt = str(insert_bale_despatch_query())
        assert "despatch_reason, despatch_ref" in stmt
        assert ":despatch_reason, :despatch_ref" in stmt
        binds = set(insert_bale_despatch_query().compile().binds)
        assert {"invoice_id", "despatch_reason", "despatch_ref", "despatch_date",
                "remarks", "updated_by", "co_id", "branch_id", "bale_entry_id",
                "invoice_line_item_id"} == binds

    def test_update_relink_statement_shape(self):
        q = update_bale_despatch_invoice_query()
        stmt = str(q)
        assert stmt.lstrip().startswith("UPDATE jute_prod_bale_despatch dp")
        assert "JOIN jute_prod_bale_entry be ON be.bale_entry_id = dp.bale_entry_id" in stmt
        assert "dp.invoice_id = :invoice_id" in stmt
        assert "COALESCE(:despatch_reason, dp.despatch_reason)" in stmt
        assert "COALESCE(:despatch_ref, dp.despatch_ref)" in stmt
        assert "COALESCE(:remarks, dp.remarks)" in stmt
        assert "dp.bale_despatch_id = :bale_despatch_id" in stmt
        assert "be.co_id = :co_id" in stmt
        assert "dp.active = 1" in stmt and "dp.invoice_id IS NULL" in stmt
        assert "despatch_date" not in stmt  # the day it left is never rewritten
        assert set(q.compile().binds) == {
            "invoice_id", "despatch_reason", "despatch_ref", "remarks", "updated_by",
            "bale_despatch_id", "co_id",
        }


# =============================================================================
# Endpoints
# =============================================================================


class TestBaleStockEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @property
    def calls(self):
        return self._mock_session.execute.call_args_list

    def _all_binds_complete(self):
        for call in self.calls:
            _assert_binds_complete(call)

    # ---- setup ------------------------------------------------------------

    def test_setup_shape_and_bale_source(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row({
                "finishing_quality_id": 2, "item_id": 270011, "item_code": "2124",
                "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2,
                "std_bale_weight": Decimal("290.000"), "packsheet_wt": None,
                "std_mr_pct": Decimal("20.00"), "weft_yarn_item_id": None, "weft_count": None,
            })]),
            _exec_result(fetchall=[_mock_row({
                "bale_series_id": 1, "series_name": "PUNJAB", "quality_type": 2, "item_id": None,
            })]),
            _exec_result(fetchone=_attr_row(found=1)),
        ]
        response = client.get(f"{BASE}/setup?co_id=1&branch_id=2")
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["qualities"][0]["std_bale_weight"] == 290.0
        assert body["qualities"][0]["std_mr_pct"] == 20.0
        assert body["series"] == [{"bale_series_id": 1, "series_name": "PUNJAB",
                                   "quality_type": 2, "item_id": None}]
        assert [s["code"] for s in body["statuses"]] == [
            "STOCK", "INSPECTED", "NOT_INSPECTED", "DESPATCHED", "REPACKED", "ALL",
        ]
        assert body["statuses"][0]["label"] == "In stock (all)"
        # §7b: the no-invoice reason picker, in constants order
        assert body["despatch_reasons"] == [
            {"code": code, "label": label} for code, label in BALE_DESPATCH_REASONS
        ]
        assert [r["code"] for r in body["despatch_reasons"]] == [
            "CHALLAN", "DELIVERY_ORDER", "TRANSFER", "SAMPLE", "OTHER",
        ]
        assert body["despatch_reasons"][0]["label"] == "Delivery challan"
        assert body["bale_source"] == "BALE_PROD"
        self._all_binds_complete()
        assert self.calls[2].args[1] == {"co_id": 1, "branch_id": 2, "as_on": TODAY}

    def test_setup_manual_when_no_bales(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[]), _exec_result(fetchall=[]), _exec_result(fetchone=None),
        ]
        body = client.get(f"{BASE}/setup?co_id=1&branch_id=2").json()["data"]
        assert body["bale_source"] == "MANUAL"

    def test_setup_requires_co_id(self):
        assert client.get(f"{BASE}/setup").status_code == 400

    # ---- bales ------------------------------------------------------------

    def test_bales_default_filter_stock_and_as_on_today(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat()), _mock_row(_inspected(bale_entry_id=18))]),
        ]
        response = client.get(f"{BASE}/bales?co_id=1&branch_id=2")
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["as_on"] == str(TODAY)
        assert body["truncated"] is False
        assert [r["status"] for r in body["rows"]] == ["IN_STOCK", "INSPECTED"]
        assert body["rows"][0]["inspection"] is None
        assert body["rows"][1]["inspection"]["inspection_ref"] == "IC/22"
        self._all_binds_complete()
        params = self.calls[0].args[1]
        assert params["status_filter"] == "STOCK"
        assert params["as_on"] == TODAY
        assert params["cancelled_status"] == [4, 6]
        assert params["bale_entry_ids_set"] == 0 and params["bale_entry_ids"] == [0]
        assert params["row_cap"] == BALE_STOCK_ROW_CAP + 1
        assert params["quality_type"] is None and params["date_from"] is None
        assert params["invoice_id"] is None                 # §7e bind, off by default
        assert "(4, 6)" not in str(self.calls[0].args[0])  # bound, not interpolated

    def test_bales_filters_and_all_maps_to_null(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[])]
        response = client.get(
            f"{BASE}/bales?co_id=1&branch_id=2&as_on={PAST}&status=all&quality_type=2"
            "&finishing_quality_id=2&bale_series_id=1&bale_no_from=600&bale_no_to=700"
            f"&date_from={PAST}&date_to={TODAY}"
        )
        assert response.status_code == 200
        params = self.calls[0].args[1]
        assert params["status_filter"] is None
        assert params["as_on"] == PAST
        assert params["quality_type"] == 2
        assert params["finishing_quality_id"] == 2
        assert params["bale_series_id"] == 1
        assert params["bale_no_from"] == 600 and params["bale_no_to"] == 700
        assert params["date_from"] == PAST and params["date_to"] == TODAY

    def test_bales_truncated_when_cap_exceeded(self):
        rows = [_mock_row(_flat(bale_entry_id=i, bale_no=i)) for i in range(BALE_STOCK_ROW_CAP + 1)]
        self._mock_session.execute.side_effect = [_exec_result(fetchall=rows)]
        body = client.get(f"{BASE}/bales?co_id=1&branch_id=2").json()["data"]
        assert body["truncated"] is True
        assert len(body["rows"]) == BALE_STOCK_ROW_CAP

    def test_bales_rejects_future_as_on(self):
        response = client.get(f"{BASE}/bales?co_id=1&branch_id=2&as_on={FUTURE}")
        assert response.status_code == 400
        assert "as_on" in response.json()["detail"]

    def test_bales_rejects_bad_status(self):
        assert client.get(f"{BASE}/bales?co_id=1&branch_id=2&status=SOLD").status_code == 400

    def test_bales_rejects_bad_date(self):
        assert client.get(f"{BASE}/bales?co_id=1&branch_id=2&as_on=12-10-2026").status_code == 400

    def test_bales_requires_branch(self):
        assert client.get(f"{BASE}/bales?co_id=1").status_code == 400

    def test_bales_invoice_id_filter_is_bound(self):
        """§7e: the invoice page lists the bales linked to one invoice — live
        AND void — so it passes status=ALL; the id rides as the bind."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[
                _mock_row(_despatched(bale_entry_id=17)),
                _mock_row(_despatched(bale_entry_id=18, bale_no=613, invoice_status_id=6,
                                      status="IN_STOCK")),
            ]),
        ]
        response = client.get(f"{BASE}/bales?co_id=1&branch_id=2&invoice_id=171&status=ALL")
        assert response.status_code == 200
        body = response.json()["data"]
        assert [(r["status"], r["despatch"]["invoice_id"]) for r in body["rows"]] == [
            ("DESPATCHED", 171), ("IN_STOCK", 171),
        ]
        self._all_binds_complete()
        params = self.calls[0].args[1]
        assert params["invoice_id"] == 171
        assert params["status_filter"] is None
        assert params["bale_entry_ids_set"] == 0
        assert ":invoice_id" in str(self.calls[0].args[0])
        assert "171" not in str(self.calls[0].args[0])  # bound, not interpolated

    def test_bales_rejects_non_int_invoice_id(self):
        response = client.get(f"{BASE}/bales?co_id=1&branch_id=2&invoice_id=abc")
        assert response.status_code == 400
        assert "invoice_id" in response.json()["detail"]
        self._mock_session.execute.assert_not_called()

    # ---- suggest (§7e) ----------------------------------------------------

    def _suggest(self, **over):
        params = {"co_id": 1, "branch_id": 2, "bale_series_id": 1, "count": 3,
                  "purpose": "despatch"}
        params.update(over)
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        return client.get(f"{BASE}/suggest?{query}")

    def test_suggest_despatch_inspected_first_then_oldest_then_bale_no(self):
        """Rows arrive in the statement's order (bale no); the proposal puts
        the INSPECTED bales first (oldest first), then the uninspected ones."""
        d0, d1, d2 = PAST - timedelta(days=2), PAST - timedelta(days=1), PAST
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[
                _mock_row(_flat(bale_entry_id=10, bale_no=610, tran_date=d0)),
                _mock_row(_inspected(bale_entry_id=11, bale_no=611, tran_date=d2)),
                _mock_row(_inspected(bale_entry_id=12, bale_no=612, tran_date=d1)),
                _mock_row(_flat(bale_entry_id=13, bale_no=613, tran_date=d1)),
                _mock_row(_flat(bale_entry_id=14, bale_no=614, tran_date=d0)),
            ]),
        ]
        response = self._suggest(count=5)
        assert response.status_code == 200
        body = response.json()["data"]
        assert [r["bale_no"] for r in body["rows"]] == [612, 611, 610, 614, 613]
        assert [r["status"] for r in body["rows"]] == [
            "INSPECTED", "INSPECTED", "IN_STOCK", "IN_STOCK", "IN_STOCK",
        ]
        assert body["available"] == 5
        assert body["purpose"] == "despatch"
        assert body["bale_series_id"] == 1
        assert set(body) == {"rows", "available", "purpose", "bale_series_id"}
        # full BaleStockRow shape, not a slim id list
        assert body["rows"][0]["inspection"]["inspection_ref"] == "IC/22"
        assert body["rows"][0]["net_wt"] == 290.0

    def test_suggest_inspect_orders_by_press_date_then_bale_no(self):
        d0, d1 = PAST - timedelta(days=1), PAST
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[
                _mock_row(_flat(bale_entry_id=10, bale_no=610, tran_date=d1)),
                _mock_row(_flat(bale_entry_id=11, bale_no=611, tran_date=d0)),
                _mock_row(_flat(bale_entry_id=12, bale_no=612, tran_date=d0)),
            ]),
        ]
        response = self._suggest(purpose="inspect", count=3)
        assert response.status_code == 200
        body = response.json()["data"]
        assert [r["bale_no"] for r in body["rows"]] == [611, 612, 610]
        assert body["available"] == 3 and body["purpose"] == "inspect"

    def test_suggest_cuts_to_count_and_reports_available(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[
                _mock_row(_flat(bale_entry_id=i, bale_no=600 + i, tran_date=PAST - timedelta(days=i)))
                for i in range(1, 5)
            ]),
        ]
        response = self._suggest(count=2)
        assert response.status_code == 200
        body = response.json()["data"]
        assert len(body["rows"]) == 2
        assert [r["bale_no"] for r in body["rows"]] == [604, 603]  # the two oldest
        assert body["available"] == 4

    def test_suggest_count_above_available_returns_all(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat())]),
        ]
        body = self._suggest(count=12).json()["data"]
        assert len(body["rows"]) == 1 and body["available"] == 1

    @pytest.mark.parametrize("purpose,status_filter", [
        ("inspect", "NOT_INSPECTED"), ("despatch", "STOCK"),
    ])
    def test_suggest_binds_complete_and_status_filter_per_purpose(self, purpose, status_filter):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[_mock_row(_flat())])]
        response = self._suggest(purpose=purpose, as_on=PAST, finishing_quality_id=2)
        assert response.status_code == 200
        assert len(self.calls) == 1  # one statement
        self._all_binds_complete()
        params = self.calls[0].args[1]
        assert params["status_filter"] == status_filter
        assert params["bale_series_id"] == 1
        assert params["finishing_quality_id"] == 2
        assert params["as_on"] == PAST
        assert params["co_id"] == 1 and params["branch_id"] == 2
        assert params["row_cap"] == BALE_STOCK_ROW_CAP + 1
        assert params["cancelled_status"] == [4, 6]
        assert params["bale_entry_ids_set"] == 0 and params["bale_entry_ids"] == [0]
        assert params["invoice_id"] is None
        assert params["quality_type"] is None
        assert params["bale_no_from"] is None and params["bale_no_to"] is None
        assert params["date_from"] is None and params["date_to"] is None
        assert "jute_prod_bale_entry be" in str(self.calls[0].args[0])  # the rows query

    def test_suggest_defaults_as_on_today_and_quality_null(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[])]
        assert self._suggest().status_code == 200
        params = self.calls[0].args[1]
        assert params["as_on"] == TODAY and params["finishing_quality_id"] is None

    def test_suggest_purpose_case_insensitive(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[])]
        response = self._suggest(purpose="DESPATCH")
        assert response.status_code == 200
        assert response.json()["data"]["purpose"] == "despatch"
        assert self.calls[0].args[1]["status_filter"] == "STOCK"

    @pytest.mark.parametrize("count", ["0", "-1", str(BALE_STOCK_MAX_IDS + 1), "abc", "2.5", None])
    def test_suggest_bad_count_400(self, count):
        response = self._suggest(count=count)
        assert response.status_code == 400
        assert response.json()["detail"] == SUGGEST_COUNT_MSG
        self._mock_session.execute.assert_not_called()

    def test_suggest_count_bounds_accepted(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[]), _exec_result(fetchall=[])]
        assert self._suggest(count=1).status_code == 200
        assert self._suggest(count=BALE_STOCK_MAX_IDS).status_code == 200

    @pytest.mark.parametrize("purpose", ["sell", "", None])
    def test_suggest_bad_purpose_400(self, purpose):
        response = self._suggest(purpose=purpose)
        assert response.status_code == 400
        assert response.json()["detail"] == SUGGEST_PURPOSE_MSG
        self._mock_session.execute.assert_not_called()

    def test_suggest_missing_series_400(self):
        response = self._suggest(bale_series_id=None)
        assert response.status_code == 400
        assert "bale_series_id" in response.json()["detail"]
        self._mock_session.execute.assert_not_called()

    def test_suggest_bad_series_400(self):
        response = self._suggest(bale_series_id="PUNJAB")
        assert response.status_code == 400
        assert "bale_series_id" in response.json()["detail"]
        self._mock_session.execute.assert_not_called()

    def test_suggest_requires_co_and_branch(self):
        assert self._suggest(co_id=None).status_code == 400
        assert self._suggest(branch_id=None).status_code == 400
        self._mock_session.execute.assert_not_called()

    def test_suggest_future_as_on_400(self):
        response = self._suggest(as_on=FUTURE)
        assert response.status_code == 400
        assert "as_on" in response.json()["detail"]
        self._mock_session.execute.assert_not_called()

    def test_suggest_hessian_series_400_for_inspect(self):
        """O3: hessian never gets the inspection mark — the message names the
        series, and nothing but the one read ran."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[
                _mock_row(_flat(bale_entry_id=30, bale_no=201, quality_type=1,
                                series_name="40-6.50", bale_series_id=4,
                                item_name="HESSIAN CLOTH 40-6.5")),
            ]),
        ]
        response = self._suggest(purpose="inspect", bale_series_id=4)
        assert response.status_code == 400
        assert response.json()["detail"] == SUGGEST_SACKING_ONLY_MSG.format(name="40-6.50")
        assert self._mock_session.execute.call_count == 1

    def test_suggest_hessian_series_allowed_for_despatch(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[
                _mock_row(_flat(bale_entry_id=30, bale_no=201, quality_type=1,
                                series_name="40-6.50", bale_series_id=4)),
            ]),
        ]
        response = self._suggest(purpose="despatch", bale_series_id=4)
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["available"] == 1 and body["rows"][0]["quality_type"] == 1

    def test_suggest_empty_series_200_with_nothing(self):
        """A series with no eligible bale (or an unknown id) is not an error:
        rows [] and available 0, so the UI can say 'none available'."""
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[])]
        for purpose in ("inspect", "despatch"):
            response = self._suggest(purpose=purpose, bale_series_id=99)
            assert response.status_code == 200
            assert response.json()["data"] == {
                "rows": [], "available": 0, "purpose": purpose, "bale_series_id": 99,
            }
            self._mock_session.execute.side_effect = [_exec_result(fetchall=[])]

    # ---- summary ----------------------------------------------------------

    def test_summary_shape(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(found=1)),
            _exec_result(fetchall=[_mock_row(_summary_row())]),
            _exec_result(fetchall=[
                _mock_row(_series_row()),
                _mock_row(_series_row(bale_series_id=2, series_name="BIHAR", bales=50,
                                      inspected_bales=30, total_kg=Decimal("14500.000"),
                                      inspected_kg=Decimal("8700.000"), despatched_bales=15)),
            ]),
        ]
        response = client.get(f"{BASE}/summary?co_id=1&branch_id=2&as_on={PAST}")
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["as_on"] == str(PAST)
        assert body["bale_source"] == "BALE_PROD"
        assert body["lines"][0]["bales"] == 120
        assert body["lines"][0]["per_wt_kg"] == 290.0
        assert body["totals"]["sacking"]["wt_mt"] == 34.8
        # §7a: the series breakup rides along, sorted by series name
        assert [(l["series_name"], l["bales"], l["inspected_bales"]) for l in body["series_lines"]] == [
            ("BIHAR", 50, 30), ("PUNJAB", 70, 50),
        ]
        assert body["series_lines"][1]["bale_series_id"] == 1
        assert body["series_lines"][1]["per_wt_kg"] == 290.0
        assert body["series_lines"][1]["wt_mt"] == 20.3
        assert len(self.calls) == 3
        self._all_binds_complete()
        summary_binds = {
            "co_id": 1, "branch_id": 2, "as_on": PAST, "cancelled_status": [4, 6],
        }
        assert self.calls[1].args[1] == summary_binds
        assert self.calls[2].args[1] == summary_binds  # same binds, series statement
        assert "GROUP BY x.bale_series_id" in str(self.calls[2].args[0])
        assert "GROUP BY x.bale_series_id" not in str(self.calls[1].args[0])

    def test_summary_series_lines_empty_when_no_series_rows(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),
            _exec_result(fetchall=[]),
            _exec_result(fetchall=[]),
        ]
        body = client.get(f"{BASE}/summary?co_id=1&branch_id=2&as_on={PAST}").json()["data"]
        assert body["bale_source"] == "MANUAL"
        assert body["lines"] == []
        assert body["series_lines"] == []
        self._all_binds_complete()

    def test_summary_rejects_future(self):
        assert client.get(f"{BASE}/summary?co_id=1&branch_id=2&as_on={FUTURE}").status_code == 400

    # ---- invoices ---------------------------------------------------------

    def test_invoices_with_lines_and_linked_counts(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[
                _mock_row({"invoice_id": 171, "invoice_no": 52, "invoice_date": PAST,
                           "invoice_type": 3, "invoice_type_name": "Govt Sacking",
                           "party_id": 9, "party_name": "BIHAR STATE FOOD CIVIL SUPPLIES",
                           "status_id": 3}),
                _mock_row({"invoice_id": 170, "invoice_no": 51, "invoice_date": PAST,
                           "invoice_type": 2, "invoice_type_name": "Hessian",
                           "party_id": 8, "party_name": "X", "status_id": None}),
            ]),
            _exec_result(fetchall=[
                _mock_row({"invoice_id": 171, "invoice_line_item_id": 900, "item_id": 269961,
                           "item_name": "BAGS", "quantity": 240.0, "uom_name": "100 PCS",
                           "qty_bales": None}),
            ]),
            _exec_result(fetchall=[_mock_row({"invoice_id": 171, "linked_bales": 7})]),
        ]
        response = client.get(f"{BASE}/invoices?co_id=1&branch_id=2&as_on={TODAY}&days=30")
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["days"] == 30
        first, second = body["rows"]
        assert first["invoice_id"] == 171
        assert first["invoice_type_name"] == "Govt Sacking"
        assert first["invoice_date"] == str(PAST)
        assert first["lines"] == [{
            "invoice_line_item_id": 900, "item_id": 269961, "item_name": "BAGS",
            "quantity": 240.0, "uom_name": "100 PCS", "qty_bales": None,
        }]
        assert first["linked_bales"] == 7
        assert second["lines"] == [] and second["linked_bales"] == 0
        assert second["status_id"] is None
        self._all_binds_complete()
        heads = self.calls[0].args[1]
        assert heads["invoice_types"] == [1, 2, 3]
        assert heads["cancelled_status"] == [4, 6]
        assert heads["date_from"] == TODAY - timedelta(days=30)
        assert heads["as_on"] == TODAY
        assert self.calls[1].args[1] == {"invoice_ids": [171, 170], "cloth_item_type_id": 5}
        assert self.calls[2].args[1] == {"invoice_ids": [171, 170]}

    def test_invoices_default_days_and_no_line_queries_when_empty(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[])]
        response = client.get(f"{BASE}/invoices?co_id=1&branch_id=2")
        assert response.status_code == 200
        assert response.json()["data"] == {"as_on": str(TODAY), "days": 120, "rows": []}
        assert self._mock_session.execute.call_count == 1
        assert self.calls[0].args[1]["date_from"] == TODAY - timedelta(days=120)

    def test_invoices_rejects_bad_days(self):
        assert client.get(f"{BASE}/invoices?co_id=1&branch_id=2&days=-1").status_code == 400

    # ---- inspect ----------------------------------------------------------

    def test_inspect_happy_path(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat(bale_entry_id=17)),
                                   _mock_row(_flat(bale_entry_id=18, bale_no=613))]),
            _exec_result(lastrowid=1),
            _exec_result(lastrowid=2),
        ]
        response = client.post(
            f"{BASE}/inspect",
            json=_inspect_body(bale_entry_ids=[17, 18, 17], inspection_date=str(PAST),
                               remarks="  lot A  "),
        )
        assert response.status_code == 200
        assert response.json()["data"] == {"marked": 2}
        self._all_binds_complete()
        fetch = self.calls[0].args[1]
        assert fetch["bale_entry_ids_set"] == 1
        assert fetch["bale_entry_ids"] == [17, 18]       # de-duplicated
        assert fetch["row_cap"] == 3                      # len(ids) + 1
        assert fetch["status_filter"] is None
        assert fetch["as_on"] == TODAY                    # current state decides
        assert fetch["invoice_id"] is None                # §7e bind, never a gate here
        assert fetch["co_id"] == 1 and fetch["branch_id"] == 2
        ins = self.calls[1].args[1]
        assert ins == {
            "co_id": 1, "branch_id": 2, "bale_entry_id": 17, "inspection_date": PAST,
            "inspection_ref": "IC/22", "inspector": "IJIRA", "remarks": "lot A",
            "updated_by": 1,
        }
        assert self.calls[2].args[1]["bale_entry_id"] == 18
        self._mock_session.commit.assert_called_once()

    def test_inspect_unknown_id_404_names_it(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat(bale_entry_id=17))]),
        ]
        response = client.post(f"{BASE}/inspect", json=_inspect_body(bale_entry_ids=[17, 999]))
        assert response.status_code == 404
        assert "999" in response.json()["detail"]
        self._mock_session.commit.assert_not_called()
        self._mock_session.rollback.assert_called()

    def test_inspect_hessian_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat(quality_type=1, item_name="HESSIAN CLOTH",
                                                   series_name="40-6.50", bale_no=201))]),
        ]
        response = client.post(f"{BASE}/inspect", json=_inspect_body())
        assert response.status_code == 400
        assert response.json()["detail"] == INSPECT_SACKING_ONLY_MSG.format(
            no=201, series="40-6.50", item="HESSIAN CLOTH", type="hessian"
        )
        assert self._mock_session.execute.call_count == 1  # no INSERT

    def test_inspect_already_inspected_400(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[_mock_row(_inspected())])]
        response = client.post(f"{BASE}/inspect", json=_inspect_body())
        assert response.status_code == 400
        assert response.json()["detail"] == ALREADY_INSPECTED_MSG.format(
            no=612, series="PUNJAB", on=str(PAST)
        )

    def test_inspect_despatched_400(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[_mock_row(_despatched())])]
        response = client.post(f"{BASE}/inspect", json=_inspect_body())
        assert response.status_code == 400
        assert response.json()["detail"] == NOT_IN_STOCK_MSG.format(
            no=612, series="PUNJAB", status="despatched"
        )

    def test_inspect_repacked_400(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[_mock_row(_repacked())])]
        response = client.post(f"{BASE}/inspect", json=_inspect_body())
        assert response.status_code == 400
        assert response.json()["detail"] == NOT_IN_STOCK_MSG.format(
            no=612, series="PUNJAB", status="repacked"
        )

    def test_inspect_void_despatch_link_is_still_in_stock(self):
        """A link to a cancelled invoice does not block inspection — the bale
        is back in stock (D4)."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_despatched(invoice_status_id=6, status="IN_STOCK"))]),
            _exec_result(lastrowid=1),
        ]
        response = client.post(f"{BASE}/inspect", json=_inspect_body())
        assert response.status_code == 200

    def test_inspect_before_press_date_400(self):
        pressed = PAST
        earlier = PAST - timedelta(days=1)
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat(tran_date=pressed))]),
        ]
        response = client.post(
            f"{BASE}/inspect", json=_inspect_body(inspection_date=str(earlier))
        )
        assert response.status_code == 400
        assert response.json()["detail"] == INSPECT_BEFORE_PRESS_MSG.format(
            no=612, series="PUNJAB", pressed=str(pressed), on=str(earlier)
        )

    def test_inspect_future_date_400(self):
        response = client.post(f"{BASE}/inspect", json=_inspect_body(inspection_date=str(FUTURE)))
        assert response.status_code == 400
        assert "inspection_date" in response.json()["detail"]
        self._mock_session.execute.assert_not_called()

    def test_inspect_older_than_backdate_window_400(self):
        """Spec §7d: 60 days back is allowed, 61 is not — refused before any SQL."""
        too_old = TODAY - timedelta(days=61)
        response = client.post(f"{BASE}/inspect", json=_inspect_body(inspection_date=str(too_old)))
        assert response.status_code == 400
        assert "60 days" in response.json()["detail"]
        self._mock_session.execute.assert_not_called()

    def test_despatch_older_than_backdate_window_400(self):
        too_old = TODAY - timedelta(days=61)
        response = client.post(
            f"{BASE}/despatch",
            json=_despatch_body(invoice_id=None, despatch_date=str(too_old), despatch_reason="CHALLAN"),
        )
        assert response.status_code == 400
        assert "60 days" in response.json()["detail"]
        self._mock_session.execute.assert_not_called()

    def test_bales_accepts_old_as_on(self):
        """The as-on VIEW date has no floor — history stays viewable (spec §7d)."""
        self._mock_session.execute.side_effect = [_exec_result(fetchall=[])]
        old = TODAY - timedelta(days=400)
        response = client.get(f"{BASE}/bales?co_id=1&branch_id=2&as_on={old}")
        assert response.status_code == 200

    def test_inspect_empty_ids_400(self):
        response = client.post(f"{BASE}/inspect", json=_inspect_body(bale_entry_ids=[]))
        assert response.status_code == 400
        assert response.json()["detail"] == EMPTY_IDS_MSG

    def test_inspect_too_many_ids_400(self):
        response = client.post(
            f"{BASE}/inspect",
            json=_inspect_body(bale_entry_ids=list(range(1, BALE_STOCK_MAX_IDS + 2))),
        )
        assert response.status_code == 400
        assert response.json()["detail"] == TOO_MANY_IDS_MSG

    def test_inspect_ref_too_long_400(self):
        response = client.post(f"{BASE}/inspect", json=_inspect_body(inspection_ref="x" * 101))
        assert response.status_code == 400
        assert response.json()["detail"] == REF_TOO_LONG_MSG

    @pytest.mark.parametrize("field,value", [("inspector", "x" * 101), ("remarks", "x" * 256)])
    def test_inspect_other_lengths_400(self, field, value):
        response = client.post(f"{BASE}/inspect", json=_inspect_body(**{field: value}))
        assert response.status_code == 400
        self._mock_session.execute.assert_not_called()

    def test_inspect_requires_body_fields(self):
        assert client.post(f"{BASE}/inspect", json={"co_id": 1}).status_code == 422

    # ---- uninspect --------------------------------------------------------

    def test_uninspect_happy(self):
        self._mock_session.execute.side_effect = [_exec_result(rowcount=2)]
        response = client.post(f"{BASE}/uninspect", json={"co_id": 1, "bale_entry_ids": [17, 18]})
        assert response.status_code == 200
        assert response.json()["data"] == {"unmarked": 2}
        self._all_binds_complete()
        params = self.calls[0].args[1]
        assert params == {"bale_entry_ids": [17, 18], "co_id": 1, "updated_by": 1}
        stmt = str(self.calls[0].args[0])
        assert "active = 0" in stmt and "DELETE" not in stmt.upper()
        assert "be.co_id = :co_id" in stmt
        self._mock_session.commit.assert_called_once()

    def test_uninspect_404_when_nothing_active(self):
        self._mock_session.execute.side_effect = [_exec_result(rowcount=0)]
        response = client.post(f"{BASE}/uninspect", json={"co_id": 1, "bale_entry_ids": [17]})
        assert response.status_code == 404
        assert response.json()["detail"] == NO_ACTIVE_INSPECTION_MSG
        self._mock_session.commit.assert_not_called()

    def test_uninspect_empty_ids_400(self):
        assert client.post(f"{BASE}/uninspect", json={"co_id": 1, "bale_entry_ids": []}).status_code == 400

    # ---- despatch ---------------------------------------------------------

    def test_despatch_happy_counts_uninspected_sacking(self):
        """Three bales: an inspected sacking, an uninspected sacking and a
        hessian — only the uninspected sacking counts for the D9 warning.
        despatch_date defaults to the invoice date."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[
                _mock_row(_inspected(bale_entry_id=17)),
                _mock_row(_flat(bale_entry_id=18, bale_no=613)),
                _mock_row(_flat(bale_entry_id=19, bale_no=201, quality_type=1,
                                series_name="40-6.50")),
            ]),
            _exec_result(lastrowid=1),
            _exec_result(lastrowid=2),
            _exec_result(lastrowid=3),
        ]
        response = client.post(
            f"{BASE}/despatch", json=_despatch_body(bale_entry_ids=[17, 18, 19], remarks="lorry")
        )
        assert response.status_code == 200
        assert response.json()["data"] == {"linked": 3, "relinked": 0, "uninspected_sacking": 1}
        self._all_binds_complete()
        assert self.calls[0].args[1] == {"invoice_id": 171}
        fetch = self.calls[1].args[1]
        assert fetch["bale_entry_ids"] == [17, 18, 19] and fetch["bale_entry_ids_set"] == 1
        assert fetch["row_cap"] == 4 and fetch["as_on"] == TODAY
        assert fetch["invoice_id"] is None
        ins = self.calls[2].args[1]
        assert ins == {
            "co_id": 1, "branch_id": 2, "bale_entry_id": 17, "invoice_id": 171,
            "invoice_line_item_id": None, "despatch_date": PAST,
            "despatch_reason": None, "despatch_ref": None, "remarks": "lorry",
            "updated_by": 1,
        }
        assert [c.args[1]["bale_entry_id"] for c in self.calls[2:]] == [17, 18, 19]
        self._mock_session.commit.assert_called_once()

    def test_despatch_explicit_date_wins(self):
        other = PAST + timedelta(days=1)
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[_mock_row(_flat())]),
            _exec_result(lastrowid=1),
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body(despatch_date=str(other)))
        assert response.status_code == 200
        assert self.calls[2].args[1]["despatch_date"] == other

    def test_despatch_retires_void_link_before_relinking(self):
        """A bale whose existing link points at a cancelled invoice is in stock
        (D4); re-linking it soft-deletes the void link in the same transaction
        so the bale never carries two active rows."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[_mock_row(_despatched(invoice_status_id=6, status="INSPECTED",
                                                         bale_inspection_id=3, inspection_date=PAST))]),
            _exec_result(rowcount=1),   # UPDATE: retire the void link
            _exec_result(lastrowid=9),  # INSERT: the new link
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 200
        assert response.json()["data"] == {"linked": 1, "relinked": 0, "uninspected_sacking": 0}
        self._all_binds_complete()
        retire = self.calls[2]
        assert "jute_prod_bale_despatch" in str(retire.args[0]) and "active = 0" in str(retire.args[0])
        assert retire.args[1] == {"bale_entry_ids": [17], "co_id": 1, "updated_by": 1}
        assert "INSERT" in str(self.calls[3].args[0]).upper()

    def test_despatch_invoice_not_found_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 404
        assert response.json()["detail"] == INVOICE_NOT_FOUND_MSG

    def test_despatch_inactive_invoice_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=_invoice(active=0))]
        assert client.post(f"{BASE}/despatch", json=_despatch_body()).status_code == 404

    def test_despatch_invoice_other_branch_400(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=_invoice(branch_id=3))]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 400
        assert response.json()["detail"] == INVOICE_BRANCH_MSG

    @pytest.mark.parametrize("invoice_type", [4, 5, 7, None])
    def test_despatch_invoice_bad_type_400(self, invoice_type):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice(invoice_type=invoice_type)),
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 400
        assert response.json()["detail"] == INVOICE_TYPE_MSG

    @pytest.mark.parametrize("status_id", [4, 6])
    def test_despatch_invoice_cancelled_400(self, status_id):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=_invoice(status_id=status_id))]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 400
        assert response.json()["detail"] == INVOICE_CANCELLED_MSG

    def test_despatch_invoice_null_status_is_live(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice(status_id=None)),
            _exec_result(fetchall=[_mock_row(_flat())]),
            _exec_result(lastrowid=1),
        ]
        assert client.post(f"{BASE}/despatch", json=_despatch_body()).status_code == 200

    def test_despatch_already_despatched_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[_mock_row(_despatched())]),
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 400
        assert response.json()["detail"] == ALREADY_DESPATCHED_MSG.format(
            no=612, series="PUNJAB", invoice=52
        )
        assert self._mock_session.execute.call_count == 2

    def test_despatch_repacked_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[_mock_row(_repacked())]),
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 400
        assert response.json()["detail"] == NOT_IN_STOCK_MSG.format(
            no=612, series="PUNJAB", status="repacked"
        )

    def test_despatch_unknown_bale_404(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[]),
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 404
        assert "17" in response.json()["detail"]

    def test_despatch_before_press_date_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice(invoice_date=PAST - timedelta(days=2))),
            _exec_result(fetchall=[_mock_row(_flat(tran_date=PAST))]),
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 400
        assert "pressed on" in response.json()["detail"]

    def test_despatch_future_date_400(self):
        response = client.post(f"{BASE}/despatch", json=_despatch_body(despatch_date=str(FUTURE)))
        assert response.status_code == 400
        self._mock_session.execute.assert_not_called()

    def test_despatch_empty_and_too_many_ids_400(self):
        assert client.post(f"{BASE}/despatch", json=_despatch_body(bale_entry_ids=[])).status_code == 400
        assert client.post(
            f"{BASE}/despatch",
            json=_despatch_body(bale_entry_ids=list(range(BALE_STOCK_MAX_IDS + 1))),
        ).status_code == 400

    def test_despatch_with_invoice_optional_reason_and_ref_are_stored(self):
        """With an invoice the reason is optional but, when given, validated
        (case-insensitive) and stored together with the reference."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[_mock_row(_flat())]),
            _exec_result(lastrowid=1),
        ]
        response = client.post(
            f"{BASE}/despatch",
            json=_despatch_body(despatch_reason="delivery_order", despatch_ref="  DO/77 "),
        )
        assert response.status_code == 200
        ins = self.calls[2].args[1]
        assert ins["invoice_id"] == 171
        assert ins["despatch_reason"] == "DELIVERY_ORDER"
        assert ins["despatch_ref"] == "DO/77"

    @pytest.mark.parametrize("with_invoice", [True, False])
    def test_despatch_bad_reason_400_before_any_statement(self, with_invoice):
        body = _despatch_body(despatch_reason="LORRY") if with_invoice \
            else _no_invoice_body(despatch_reason="LORRY")
        response = client.post(f"{BASE}/despatch", json=body)
        assert response.status_code == 400
        assert response.json()["detail"] == DESPATCH_REASON_MSG
        self._mock_session.execute.assert_not_called()

    def test_despatch_ref_too_long_400(self):
        response = client.post(f"{BASE}/despatch", json=_despatch_body(despatch_ref="x" * 101))
        assert response.status_code == 400
        assert response.json()["detail"] == DESPATCH_REF_TOO_LONG_MSG
        self._mock_session.execute.assert_not_called()

    # ---- despatch WITHOUT invoice (§7b) -------------------------------------

    def test_despatch_without_invoice_happy(self):
        """No invoice: no invoice fetch, the body date is the despatch date,
        and the INSERT carries invoice_id NULL + reason + ref."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat(bale_entry_id=17)),
                                   _mock_row(_inspected(bale_entry_id=18, bale_no=613))]),
            _exec_result(lastrowid=1),
            _exec_result(lastrowid=2),
        ]
        response = client.post(
            f"{BASE}/despatch",
            json=_no_invoice_body(bale_entry_ids=[17, 18], remarks="lorry WB-11"),
        )
        assert response.status_code == 200
        assert response.json()["data"] == {"linked": 2, "relinked": 0, "uninspected_sacking": 1}
        self._all_binds_complete()
        assert len(self.calls) == 3
        fetch = self.calls[0].args[1]
        assert "jute_prod_bale_entry be" in str(self.calls[0].args[0])  # rows query first
        assert fetch["bale_entry_ids"] == [17, 18] and fetch["as_on"] == TODAY
        ins = self.calls[1].args[1]
        assert ins == {
            "co_id": 1, "branch_id": 2, "bale_entry_id": 17, "invoice_id": None,
            "invoice_line_item_id": None, "despatch_date": PAST,
            "despatch_reason": "CHALLAN", "despatch_ref": "DC/41", "remarks": "lorry WB-11",
            "updated_by": 1,
        }
        assert "INSERT INTO jute_prod_bale_despatch" in str(self.calls[1].args[0])
        assert self.calls[2].args[1]["bale_entry_id"] == 18
        self._mock_session.commit.assert_called_once()

    def test_despatch_without_invoice_requires_date(self):
        response = client.post(f"{BASE}/despatch", json=_no_invoice_body(despatch_date=None))
        assert response.status_code == 400
        assert response.json()["detail"] == DESPATCH_DATE_REQUIRED_MSG
        self._mock_session.execute.assert_not_called()

    def test_despatch_without_invoice_rejects_future_date(self):
        response = client.post(f"{BASE}/despatch", json=_no_invoice_body(despatch_date=str(FUTURE)))
        assert response.status_code == 400
        assert "despatch_date" in response.json()["detail"]
        self._mock_session.execute.assert_not_called()

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_despatch_without_invoice_requires_reason(self, reason):
        response = client.post(f"{BASE}/despatch", json=_no_invoice_body(despatch_reason=reason))
        assert response.status_code == 400
        assert response.json()["detail"] == DESPATCH_REASON_MSG
        self._mock_session.execute.assert_not_called()

    def test_despatch_without_invoice_ref_optional(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat())]),
            _exec_result(lastrowid=1),
        ]
        response = client.post(f"{BASE}/despatch", json=_no_invoice_body(despatch_ref=None))
        assert response.status_code == 200
        assert self.calls[1].args[1]["despatch_ref"] is None

    def test_despatch_without_invoice_before_press_date_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_flat(tran_date=PAST))]),
        ]
        response = client.post(
            f"{BASE}/despatch", json=_no_invoice_body(despatch_date=str(PAST - timedelta(days=1)))
        )
        assert response.status_code == 400
        assert "pressed on" in response.json()["detail"]

    def test_despatch_without_invoice_on_no_invoice_bale_400(self):
        """Already despatched without invoice + no invoice in the body = nothing
        to do; the message names the reason and the day it left."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_despatched_no_invoice())]),
        ]
        response = client.post(f"{BASE}/despatch", json=_no_invoice_body())
        assert response.status_code == 400
        assert response.json()["detail"] == ALREADY_DESPATCHED_NO_INVOICE_MSG.format(
            no=612, series="PUNJAB", reason="Delivery challan", on=str(PAST)
        )
        assert self._mock_session.execute.call_count == 1  # no write
        self._mock_session.commit.assert_not_called()

    def test_despatch_without_invoice_on_live_invoice_bale_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_despatched())]),
        ]
        response = client.post(f"{BASE}/despatch", json=_no_invoice_body())
        assert response.status_code == 400
        assert response.json()["detail"] == ALREADY_DESPATCHED_MSG.format(
            no=612, series="PUNJAB", invoice=52
        )

    def test_despatch_without_invoice_repacked_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(_repacked())]),
        ]
        response = client.post(f"{BASE}/despatch", json=_no_invoice_body())
        assert response.status_code == 400
        assert response.json()["detail"] == NOT_IN_STOCK_MSG.format(
            no=612, series="PUNJAB", status="repacked"
        )

    # ---- link later: relink a no-invoice despatch to an invoice (§7b) --------

    def test_despatch_relink_happy(self):
        """A bale despatched without invoice + an invoice in the body: the
        existing row is UPDATEd (invoice_id filled in, date untouched), nothing
        is inserted, and it counts as relinked."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[_mock_row(_despatched_no_invoice())]),
            _exec_result(rowcount=1),   # UPDATE: link the existing row
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body(remarks="inv raised"))
        assert response.status_code == 200
        assert response.json()["data"] == {"linked": 0, "relinked": 1, "uninspected_sacking": 1}
        self._all_binds_complete()
        assert len(self.calls) == 3
        stmt = str(self.calls[2].args[0])
        assert stmt.lstrip().startswith("UPDATE jute_prod_bale_despatch dp")
        assert "INSERT" not in stmt.upper()
        assert self.calls[2].args[1] == {
            "invoice_id": 171, "despatch_reason": None, "despatch_ref": None,
            "remarks": "inv raised", "updated_by": 1, "bale_despatch_id": 6, "co_id": 1,
        }
        assert "despatch_date" not in self.calls[2].args[1]
        self._mock_session.commit.assert_called_once()

    def test_despatch_relink_inspected_bale_not_counted_uninspected(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[_mock_row(_despatched_no_invoice(
                bale_inspection_id=3, inspection_date=PAST))]),
            _exec_result(rowcount=1),
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 200
        assert response.json()["data"] == {"linked": 0, "relinked": 1, "uninspected_sacking": 0}

    def test_despatch_relink_mixed_with_fresh_bales(self):
        """One in-stock bale and one no-invoice bale in the same call: INSERT
        for the first, UPDATE for the second, counted separately."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[
                _mock_row(_despatched_no_invoice(bale_entry_id=17)),
                _mock_row(_flat(bale_entry_id=18, bale_no=613)),
            ]),
            _exec_result(lastrowid=9),  # INSERT bale 18
            _exec_result(rowcount=1),   # UPDATE bale 17's row
        ]
        response = client.post(
            f"{BASE}/despatch", json=_despatch_body(bale_entry_ids=[17, 18], despatch_ref="DO/5")
        )
        assert response.status_code == 200
        assert response.json()["data"] == {"linked": 1, "relinked": 1, "uninspected_sacking": 2}
        self._all_binds_complete()
        assert "INSERT" in str(self.calls[2].args[0]).upper()
        assert self.calls[2].args[1]["bale_entry_id"] == 18
        assert self.calls[2].args[1]["despatch_ref"] == "DO/5"
        assert str(self.calls[3].args[0]).lstrip().startswith("UPDATE jute_prod_bale_despatch dp")
        assert self.calls[3].args[1]["bale_despatch_id"] == 6
        assert self.calls[3].args[1]["despatch_ref"] == "DO/5"

    def test_despatch_relink_stale_row_400(self):
        """rowcount 0 on the relink UPDATE = the row was linked or retired
        between the read and the write; nothing is committed."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_invoice()),
            _exec_result(fetchall=[_mock_row(_despatched_no_invoice())]),
            _exec_result(rowcount=0),
        ]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 400
        assert response.json()["detail"] == RELINK_STALE_MSG.format(no=612, series="PUNJAB")
        self._mock_session.commit.assert_not_called()
        self._mock_session.rollback.assert_called()

    def test_despatch_relink_still_runs_invoice_gates(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=_invoice(status_id=6))]
        response = client.post(f"{BASE}/despatch", json=_despatch_body())
        assert response.status_code == 400
        assert response.json()["detail"] == INVOICE_CANCELLED_MSG

    def test_despatch_requires_body_fields(self):
        assert client.post(f"{BASE}/despatch", json={"co_id": 1}).status_code == 422

    # ---- undespatch -------------------------------------------------------

    def test_undespatch_happy(self):
        self._mock_session.execute.side_effect = [_exec_result(rowcount=1)]
        response = client.post(f"{BASE}/undespatch", json={"co_id": 1, "bale_entry_ids": [17]})
        assert response.status_code == 200
        assert response.json()["data"] == {"unlinked": 1}
        self._all_binds_complete()
        stmt = str(self.calls[0].args[0])
        assert "jute_prod_bale_despatch" in stmt and "active = 0" in stmt
        assert self.calls[0].args[1] == {"bale_entry_ids": [17], "co_id": 1, "updated_by": 1}

    def test_undespatch_404_when_nothing_active(self):
        self._mock_session.execute.side_effect = [_exec_result(rowcount=0)]
        response = client.post(f"{BASE}/undespatch", json={"co_id": 1, "bale_entry_ids": [17]})
        assert response.status_code == 404
        assert response.json()["detail"] == NO_ACTIVE_DESPATCH_MSG
