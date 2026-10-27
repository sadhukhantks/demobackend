"""Finishing stock report (/api/finishingStock/report).

build_finishing_stock_report is a pure function — no DB — so most of this file
exercises it directly with plain dicts. Only the last class touches the endpoint,
to prove every statement it runs gets every bind it declares.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.finishing_stock import build_finishing_stock_report
from src.main import app

client = TestClient(app)

OWN_CO_ID = 1
OWN_CO_NAME = "The Empire Jute Company Limited"
NEXGEN = "Nexgen Jute Limited"

# finishing_quality_id -> (item_id, item_name, quality_type)
Q_HESS_A = (1, 269959, "HESSIAN CLOTH 40-6.5", 1)
Q_HESS_B = (5, 268939, "HESSIAN CLOTH 38-8.12", 1)
Q_SACK = (2, 270011, "PRINTED TYPE A JUTE BAGS", 2)


def _quality(entry):
    qid, item_id, item_name, quality_type = entry
    return {
        "finishing_quality_id": qid,
        "item_id": item_id,
        "item_code": str(item_id),
        "item_name": item_name,
        "quality_type": quality_type,
        "std_bale_weight": 400.0,
        "packsheet_wt": None,
        "no_of_bags": None,
    }


def _row(entry, bucket, qty, *, wt_per_unit_kg=None, wt_kg=None, owner_co_id=None,
         owner_co_name=OWN_CO_NAME, stage=None, stage_label=None, work_by=0,
         qty_uom=None, finishing_stock_id=1):
    qid, item_id, item_name, quality_type = entry
    total_kg = wt_kg if wt_kg is not None else round(qty * (wt_per_unit_kg or 0), 3)
    return {
        "finishing_stock_id": finishing_stock_id,
        "co_id": OWN_CO_ID,
        "branch_id": 2,
        "tran_date": "2026-09-16",
        "bucket": bucket,
        "finishing_quality_id": qid,
        "item_id": item_id,
        "item_code": str(item_id),
        "item_name": item_name,
        "quality_type": quality_type,
        "stage": stage,
        "stage_label": stage_label,
        "work_by": work_by,
        "owner_co_id": owner_co_id,
        "owner_co_name": owner_co_name,
        "qty": qty,
        "qty_uom": qty_uom or ("BALE" if bucket == "BALE" else "CUTS"),
        "wt_per_unit_kg": wt_per_unit_kg,
        "wt_kg": wt_kg,
        "total_kg": total_kg,
        "remarks": None,
    }


def _build(rows, qualities=None, despatch=None, **kwargs):
    return build_finishing_stock_report(
        rows,
        [_quality(q) for q in (qualities or [Q_HESS_A, Q_HESS_B, Q_SACK])],
        despatch or [],
        OWN_CO_ID,
        OWN_CO_NAME,
        **kwargs,
    )


def _derived(entry, bales, total_kg, inspected_bales=0, inspected_kg=0.0,
             despatched_bales=0):
    """One build_bale_stock_summary() line, as /rows_by_date and /report get it."""
    qid, item_id, item_name, quality_type = entry
    return {
        "finishing_quality_id": qid,
        "item_id": item_id,
        "item_name": item_name,
        "quality_type": quality_type,
        "bales": bales,
        "inspected_bales": inspected_bales,
        "total_kg": total_kg,
        "inspected_kg": inspected_kg,
        "per_wt_kg": round(total_kg / bales, 3) if bales else None,
        "wt_mt": round(total_kg / 1000, 3),
        "despatched_bales": despatched_bales,
    }


# bale_series_id -> series_name
S_PUNJAB = (1, "PUNJAB")
S_BIHAR = (2, "BIHAR")
S_HESS = (3, "40-6.50")

ZERO_SERIES_TOTAL = {"bales": 0, "inspected_bales": 0, "wt_mt": 0.0}


def _derived_series(series, entry, bales, total_kg, inspected_bales=0, inspected_kg=0.0,
                    despatched_bales=0):
    """One bale_stock series line (build_bale_stock_series_lines), as /report gets it."""
    series_id, series_name = series
    line = _derived(entry, bales, total_kg, inspected_bales, inspected_kg, despatched_bales)
    return {"bale_series_id": series_id, "series_name": series_name, **line}


class TestBuildFinishingStockReport:
    def test_own_block_zero_fills_every_quality(self):
        """The Excel prints a 0.000 line for every quality the mill runs — a
        quality missing from the sheet must read as "counted zero", not as
        "forgotten"."""
        report = _build([_row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384)])
        block = report["bales"][0]
        assert block["is_own"] is True
        assert block["owner_co_name"] == OWN_CO_NAME
        assert len(block["hessian"]) == 2
        assert len(block["sacking"]) == 1
        zero = next(l for l in block["hessian"] if l["finishing_quality_id"] == 5)
        assert zero["bales"] == 0
        assert zero["wt_mt"] == 0.0
        assert zero["per_wt_kg"] is None
        assert block["sacking"][0]["bales"] == 0

    def test_other_owner_block_lists_only_rows(self):
        report = _build([
            _row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384),
            _row(Q_SACK, "BALE", 4, wt_per_unit_kg=290, owner_co_id=9,
                 owner_co_name=NEXGEN, finishing_stock_id=2),
        ])
        other = report["bales"][1]
        assert other["is_own"] is False
        assert other["owner_co_id"] == 9
        assert other["hessian"] == []
        assert [l["finishing_quality_id"] for l in other["sacking"]] == [2]

    def test_blocks_order_own_first_then_owner_name(self):
        report = _build([
            _row(Q_HESS_A, "BALE", 5, wt_per_unit_kg=400, owner_co_id=9,
                 owner_co_name="Zeta Jute", finishing_stock_id=2),
            _row(Q_HESS_A, "BALE", 5, wt_per_unit_kg=400, owner_co_id=7,
                 owner_co_name="Alpha Jute", finishing_stock_id=3),
            _row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384),
        ])
        assert [b["owner_co_name"] for b in report["bales"]] == [
            OWN_CO_NAME, "Alpha Jute", "Zeta Jute",
        ]

    def test_per_wt_is_weighted_average_over_lots(self):
        """Two lots of one quality pressed at different weights: the printed
        per-bale weight is the weighted average, never either lot's figure."""
        report = _build([
            _row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384),
            _row(Q_HESS_A, "BALE", 30, wt_per_unit_kg=400, finishing_stock_id=2),
        ])
        line = next(
            l for l in report["bales"][0]["hessian"] if l["finishing_quality_id"] == 1
        )
        total_kg = 70 * 437.384 + 30 * 400
        assert line["bales"] == 100
        assert line["per_wt_kg"] == round(total_kg / 100, 3)
        assert line["wt_mt"] == round(total_kg / 1000, 3)

    def test_loose_sums_lots_and_stages_per_quality(self):
        report = _build([
            _row(Q_SACK, "LOOSE", 19, wt_kg=1000.0, stage="HEMMING",
                 stage_label="Hemming"),
            _row(Q_SACK, "LOOSE", 45, wt_kg=885.0, stage="HERACKLE",
                 stage_label="Herackle", finishing_stock_id=2),
        ])
        loose = report["loose"]
        sack_line = next(l for l in loose["sacking"] if l["finishing_quality_id"] == 2)
        assert sack_line["wt_mt"] == 1.885
        assert loose["sacking_total_mt"] == 1.885
        assert loose["hessian_total_mt"] == 0.0  # zero-filled, both hessian qualities
        assert len(loose["hessian"]) == 2
        assert loose["total_mt"] == 1.885

    def test_loose_lines_carry_stage_labels(self):
        report = _build([
            _row(Q_SACK, "LOOSE", 130, wt_kg=1885.0, stage="HEMMING",
                 stage_label="Hemming", work_by=1, qty_uom="CUTS"),
        ])
        assert report["loose_lines"] == [{
            "stage": "HEMMING",
            "stage_label": "Hemming",
            "work_by": 1,
            "quality_type": 2,
            "item_name": "PRINTED TYPE A JUTE BAGS",
            "qty": 130,
            "qty_uom": "CUTS",
            "wt_mt": 1.885,
        }]

    def test_totals_stock_mt_equals_bales_plus_loose(self):
        report = _build([
            _row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384),
            _row(Q_SACK, "BALE", 4, wt_per_unit_kg=290, owner_co_id=9,
                 owner_co_name=NEXGEN, finishing_stock_id=2),
            _row(Q_SACK, "LOOSE", 19, wt_kg=1885.0, stage="HEMMING",
                 stage_label="Hemming", finishing_stock_id=3),
        ])
        totals = report["totals"]
        assert totals["bales_mt"] == round(
            sum(b["total"]["wt_mt"] for b in report["bales"]), 3
        )
        assert totals["loose_mt"] == report["loose"]["total_mt"]
        assert totals["stock_mt"] == round(totals["bales_mt"] + totals["loose_mt"], 3)

    def test_despatch_passthrough_bales_null_when_none(self):
        report = _build([], despatch=[
            {"item_id": 270002, "item_name": "SACKING BAGS", "bales": None,
             "qty": 6.123, "uom_name": "MT"},
            {"item_id": 269959, "item_name": "HESSIAN CLOTH", "bales": 22,
             "qty": 9.5, "uom_name": "MT"},
        ])
        assert report["despatch"] == [
            {"item_id": 270002, "item_name": "SACKING BAGS", "bales": None,
             "qty": 6.123, "uom_name": "MT"},
            {"item_id": 269959, "item_name": "HESSIAN CLOTH", "bales": 22.0,
             "qty": 9.5, "uom_name": "MT"},
        ]

    def test_unnormalised_own_owner_id_folds_into_own_block(self):
        """A row written by SQL (import, copy) can carry the company's own id in
        owner_co_id instead of NULL — it must not open a second own block."""
        report = _build([
            _row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384),
            _row(Q_HESS_A, "BALE", 30, wt_per_unit_kg=400, owner_co_id=OWN_CO_ID,
                 finishing_stock_id=2),
        ])
        assert len(report["bales"]) == 1
        line = next(
            l for l in report["bales"][0]["hessian"] if l["finishing_quality_id"] == 1
        )
        assert line["bales"] == 100

    def test_block_titles_are_always_strings(self):
        """FinishingStockBaleBlock.owner_co_name is not nullable on the FE — a
        co_mst row that fails to resolve must render as blank, not null."""
        report = build_finishing_stock_report(
            [_row(Q_HESS_A, "BALE", 3, wt_per_unit_kg=400, owner_co_id=9,
                  owner_co_name=None)],
            [_quality(Q_HESS_A)],
            [],
            OWN_CO_ID,
            None,
        )
        assert [b["owner_co_name"] for b in report["bales"]] == ["", ""]

    def test_loose_line_quality_type_falls_back_to_the_master(self):
        report = _build([
            _row(Q_SACK, "LOOSE", 5, wt_kg=500.0, stage="HEMMING",
                 stage_label="Hemming"),
        ])
        report["loose_lines"][0]["quality_type"] = 2  # sanity on the happy path

        orphan = dict(_row(Q_SACK, "LOOSE", 5, wt_kg=500.0))
        orphan["quality_type"] = None  # quality row gone from the master
        with_master = _build([orphan])
        assert with_master["loose_lines"][0]["quality_type"] == 2

        without_master = _build([orphan], qualities=[Q_HESS_A])
        assert without_master["loose_lines"] == []

    def test_loose_keeps_rows_whose_quality_left_the_master(self):
        """A quality deactivated after the count must not silently drop its
        tonnage out of the report."""
        report = _build(
            [_row(Q_SACK, "LOOSE", 5, wt_kg=500.0)], qualities=[Q_HESS_A]
        )
        assert report["loose"]["sacking"][0]["wt_mt"] == 0.5
        assert report["loose"]["total_mt"] == 0.5

    # ---- manual mode is the default and unchanged -------------------------

    def test_manual_mode_is_the_default(self):
        """The positional call every caller already makes still means MANUAL:
        bale lines carry inspected_bales = None and the body says so."""
        report = _build([
            _row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384),
            _row(Q_SACK, "BALE", 4, wt_per_unit_kg=290, owner_co_id=9,
                 owner_co_name=NEXGEN, finishing_stock_id=2),
        ])
        assert report["bale_source"] == "MANUAL"
        assert all(
            line["inspected_bales"] is None
            for block in report["bales"]
            for line in block["hessian"] + block["sacking"]
        )
        # §7a: no series breakup without Bale Production, on any block
        assert [b["series"] for b in report["bales"]] == [[], []]
        assert [b["series_total"] for b in report["bales"]] == [ZERO_SERIES_TOTAL] * 2

    def test_manual_mode_ignores_a_series_list(self):
        """derived_series is only meaningful with bale_source BALE_PROD."""
        report = _build(
            [_row(Q_SACK, "BALE", 4, wt_per_unit_kg=290)],
            derived_series=[_derived_series(S_PUNJAB, Q_SACK, 3, 870.0, inspected_bales=3)],
        )
        own = report["bales"][0]
        assert own["sacking"][0]["bales"] == 4  # counted, not derived
        assert own["series"] == []
        assert own["series_total"] == ZERO_SERIES_TOTAL


class TestBuildFinishingStockReportDerived:
    """D7 / §5: with bale production on the branch the own-company bale block
    comes from Bale Production, not from the counted lines."""

    def test_own_block_from_derived_lines_zero_filled(self):
        report = _build(
            [],
            derived_lines=[_derived(Q_SACK, 120, 34800.0, inspected_bales=80)],
            bale_source="BALE_PROD",
        )
        assert report["bale_source"] == "BALE_PROD"
        own = report["bales"][0]
        assert own["is_own"] is True
        sack = own["sacking"][0]
        assert sack["bales"] == 120
        assert sack["per_wt_kg"] == 290.0
        assert sack["wt_mt"] == 34.8
        assert sack["inspected_bales"] == 80
        # zero-filled over every active quality, with an inspected figure too
        assert [l["finishing_quality_id"] for l in own["hessian"]] == [1, 5]
        assert all(l["bales"] == 0 and l["inspected_bales"] == 0 and l["per_wt_kg"] is None
                   for l in own["hessian"])
        assert own["total"] == {"bales": 120, "wt_mt": 34.8}
        assert report["totals"]["bales_mt"] == 34.8

    def test_own_manual_bale_rows_are_dropped(self):
        """A counted own-company BALE line (owner NULL or == co_id) is
        superseded — it must not be added on top of the derived figure."""
        report = _build(
            [
                _row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384),
                _row(Q_HESS_A, "BALE", 30, wt_per_unit_kg=400, owner_co_id=OWN_CO_ID,
                     finishing_stock_id=2),
            ],
            derived_lines=[_derived(Q_HESS_A, 9, 3619.7)],
            bale_source="BALE_PROD",
        )
        own = report["bales"][0]
        line = next(l for l in own["hessian"] if l["finishing_quality_id"] == 1)
        assert line["bales"] == 9
        assert line["wt_mt"] == 3.62
        assert line["per_wt_kg"] == round(3619.7 / 9, 3)
        assert len(report["bales"]) == 1

    def test_other_owner_blocks_unchanged_in_derived_mode(self):
        report = _build(
            [
                _row(Q_HESS_A, "BALE", 70, wt_per_unit_kg=437.384),  # dropped (own)
                _row(Q_SACK, "BALE", 4, wt_per_unit_kg=290, owner_co_id=9,
                     owner_co_name=NEXGEN, finishing_stock_id=2),
            ],
            derived_lines=[],
            bale_source="BALE_PROD",
        )
        own, other = report["bales"]
        assert own["total"] == {"bales": 0, "wt_mt": 0.0}
        assert other["owner_co_name"] == NEXGEN
        assert other["sacking"][0]["bales"] == 4
        assert other["sacking"][0]["wt_mt"] == 1.16
        assert other["sacking"][0]["inspected_bales"] is None
        assert other["hessian"] == []

    def test_derived_quality_missing_from_master_still_prints(self):
        report = _build(
            [],
            qualities=[Q_HESS_A],
            derived_lines=[_derived(Q_SACK, 3, 870.0, inspected_bales=1)],
            bale_source="BALE_PROD",
        )
        own = report["bales"][0]
        assert [l["finishing_quality_id"] for l in own["sacking"]] == [2]
        assert own["sacking"][0]["inspected_bales"] == 1

    # ---- §7a: sacking bales by series --------------------------------------

    def test_own_block_series_sacking_only_zero_omitted(self):
        """PUNJAB and BIHAR print with their inspected counts; a series with
        nothing in stock and the hessian series are left out; the series total
        adds up the printed rows and the block total is NOT inflated by them."""
        report = _build(
            [],
            derived_lines=[
                _derived(Q_SACK, 5, 1450.0, inspected_bales=3),
                _derived(Q_HESS_A, 9, 3619.7),
            ],
            derived_series=[
                _derived_series(S_HESS, Q_HESS_A, 9, 3619.7),
                _derived_series(S_BIHAR, Q_SACK, 2, 580.0),
                _derived_series(S_PUNJAB, Q_SACK, 3, 870.0, inspected_bales=3),
                _derived_series((4, "FCI"), Q_SACK, 0, 0.0),
            ],
            bale_source="BALE_PROD",
        )
        own = report["bales"][0]
        assert own["series"] == [
            {
                "bale_series_id": 2, "series_name": "BIHAR", "finishing_quality_id": 2,
                "item_name": "PRINTED TYPE A JUTE BAGS", "bales": 2,
                "inspected_bales": 0, "per_wt_kg": 290.0, "wt_mt": 0.58,
            },
            {
                "bale_series_id": 1, "series_name": "PUNJAB", "finishing_quality_id": 2,
                "item_name": "PRINTED TYPE A JUTE BAGS", "bales": 3,
                "inspected_bales": 3, "per_wt_kg": 290.0, "wt_mt": 0.87,
            },
        ]
        assert all(isinstance(l["bales"], int) and isinstance(l["inspected_bales"], int)
                   for l in own["series"])
        assert own["series_total"] == {"bales": 5, "inspected_bales": 3, "wt_mt": 1.45}
        # the breakup equals the sacking line it breaks up, and the block total is unchanged
        assert own["series_total"]["bales"] == own["sacking"][0]["bales"]
        assert own["series_total"]["wt_mt"] == own["sacking_total"]["wt_mt"]
        assert own["total"] == {"bales": 14, "wt_mt": round(1.45 + 3.62, 3)}
        assert report["totals"]["bales_mt"] == round(1.45 + 3.62, 3)

    def test_series_per_wt_is_the_series_average(self):
        """Two series of one quality pressed at different weights: each series
        prints its own average, like _derived_bale_line does per quality."""
        report = _build(
            [],
            derived_lines=[_derived(Q_SACK, 7, 2100.0)],
            derived_series=[
                _derived_series(S_PUNJAB, Q_SACK, 4, 1240.0),
                _derived_series(S_BIHAR, Q_SACK, 3, 860.0),
            ],
            bale_source="BALE_PROD",
        )
        series = report["bales"][0]["series"]
        assert [(l["series_name"], l["per_wt_kg"], l["wt_mt"]) for l in series] == [
            ("PUNJAB", 310.0, 1.24), ("BIHAR", round(860.0 / 3, 3), 0.86),
        ]
        assert report["bales"][0]["series_total"]["wt_mt"] == 2.1

    def test_other_owner_block_series_empty_in_derived_mode(self):
        """Series are a breakup of the OWN company's derived bales; bales held
        for another owner are hand-counted and have no series."""
        report = _build(
            [_row(Q_SACK, "BALE", 4, wt_per_unit_kg=290, owner_co_id=9,
                  owner_co_name=NEXGEN, finishing_stock_id=2)],
            derived_lines=[_derived(Q_SACK, 3, 870.0, inspected_bales=3)],
            derived_series=[_derived_series(S_PUNJAB, Q_SACK, 3, 870.0, inspected_bales=3)],
            bale_source="BALE_PROD",
        )
        own, other = report["bales"]
        assert len(own["series"]) == 1
        assert other["owner_co_name"] == NEXGEN
        assert other["series"] == []
        assert other["series_total"] == ZERO_SERIES_TOTAL

    def test_derived_mode_without_series_gives_empty(self):
        """Callers that pass derived_lines only (the pre-§7a signature) still
        get a well-formed block."""
        report = _build(
            [],
            derived_lines=[_derived(Q_SACK, 3, 870.0)],
            bale_source="BALE_PROD",
        )
        own = report["bales"][0]
        assert own["series"] == []
        assert own["series_total"] == ZERO_SERIES_TOTAL
        assert own["sacking"][0]["bales"] == 3

    def test_series_of_quality_missing_from_master_still_prints(self):
        report = _build(
            [],
            qualities=[Q_HESS_A],
            derived_lines=[_derived(Q_SACK, 3, 870.0, inspected_bales=1)],
            derived_series=[_derived_series(S_PUNJAB, Q_SACK, 3, 870.0, inspected_bales=1)],
            bale_source="BALE_PROD",
        )
        own = report["bales"][0]
        assert [l["series_name"] for l in own["series"]] == ["PUNJAB"]
        assert own["series"][0]["inspected_bales"] == 1

    def test_loose_and_despatch_untouched_by_mode(self):
        rows = [_row(Q_SACK, "LOOSE", 19, wt_kg=1000.0, stage="HEMMING",
                     stage_label="Hemming")]
        manual = _build(rows)
        derived = _build(rows, derived_lines=[], bale_source="BALE_PROD")
        assert derived["loose"] == manual["loose"]
        assert derived["loose_lines"] == manual["loose_lines"]
        assert derived["despatch"] == manual["despatch"]


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


class TestFinishingStockReportEndpoint:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def _wire(self):
        names = MagicMock()
        names.fetchone.return_value = _attr_row(
            co_name=OWN_CO_NAME, branch_name="Factory"
        )
        qualities = MagicMock()
        qualities.fetchall.return_value = [_mock_row(_quality(Q_HESS_A))]
        stock = MagicMock()
        stock.fetchall.return_value = [
            _mock_row({
                "finishing_stock_id": 1, "co_id": 1, "branch_id": 2,
                "tran_date": date(2026, 9, 16), "bucket": "BALE",
                "finishing_quality_id": 1, "item_id": 269959,
                "item_code": "531010", "item_name": "HESSIAN CLOTH 40-6.5",
                "quality_type": 1, "stage": None, "work_by": 0,
                "owner_co_id": None, "owner_co_name": None,
                "qty": 70, "qty_uom": "BALE", "wt_per_unit_kg": 437.384,
                "wt_kg": None, "remarks": None,
            })
        ]
        despatch = MagicMock()
        despatch.fetchall.return_value = [
            _mock_row({"item_id": 270002, "item_name": "SACKING BAGS",
                       "bales": None, "qty": 6.123, "uom_name": "MT"})
        ]
        # 5th statement: the bale-source probe (no row = MANUAL, nothing derived)
        source = MagicMock()
        source.fetchone.return_value = None
        self._mock_session.execute.side_effect = [names, qualities, stock, despatch, source]

    def _wire_derived(self):
        self._wire()
        source = MagicMock()
        source.fetchone.return_value = _attr_row(found=1)
        summary = MagicMock()
        summary.fetchall.return_value = [_mock_row({
            "finishing_quality_id": 1, "item_id": 269959,
            "item_name": "HESSIAN CLOTH 40-6.5", "quality_type": 1,
            "bales": 9, "inspected_bales": 0, "total_kg": 3619.7,
            "inspected_kg": 0.0, "despatched_bales": 2,
        })]
        # 7th statement (§7a): the series breakup — hessian series filtered
        # out of the block, the sacking one printed
        series = MagicMock()
        series.fetchall.return_value = [
            _mock_row({
                "bale_series_id": 3, "series_name": "40-6.50",
                "finishing_quality_id": 1, "item_id": 269959,
                "item_name": "HESSIAN CLOTH 40-6.5", "quality_type": 1,
                "bales": 9, "inspected_bales": 0, "total_kg": 3619.7,
                "inspected_kg": 0.0, "despatched_bales": 2,
            }),
            _mock_row({
                "bale_series_id": 1, "series_name": "PUNJAB",
                "finishing_quality_id": 2, "item_id": 270011,
                "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2,
                "bales": 3, "inspected_bales": 3, "total_kg": 870.0,
                "inspected_kg": 870.0, "despatched_bales": 0,
            }),
        ]
        self._mock_session.execute.side_effect = (
            list(self._mock_session.execute.side_effect)[:4] + [source, summary, series]
        )

    def test_report_endpoint_binds_complete(self):
        self._wire()
        response = client.get(
            "/api/finishingStock/report?co_id=1&branch_id=2&tran_date=2026-09-16"
        )
        assert response.status_code == 200
        for call in self._mock_session.execute.call_args_list:
            declared = set(call.args[0].compile().binds)
            passed = set(call.args[1])
            assert not declared - passed, declared - passed
            # and nothing dead in the other direction: a bind the statement does
            # not declare makes the check above vacuous for that key.
            assert not passed - declared, passed - declared

    def test_despatch_statement_binds_its_constants(self):
        """The status list and the cloth item type are BINDS, not f-string
        interpolation: `NOT IN (4,)` — what a one-element tuple renders as —
        is a MySQL syntax error that would 500 every report."""
        self._wire()
        client.get(
            "/api/finishingStock/report?co_id=1&branch_id=2&tran_date=2026-09-16"
        )
        despatch_call = self._mock_session.execute.call_args_list[3]
        declared = set(despatch_call.args[0].compile().binds)
        assert {"excluded_status", "cloth_item_type_id"} <= declared
        assert "(4, 6)" not in str(despatch_call.args[0])  # not interpolated
        params = despatch_call.args[1]
        assert params["excluded_status"] == [4, 6]
        assert params["cloth_item_type_id"] == 5
        assert "co_id" not in params  # the statement declares none

    def test_report_endpoint_shape(self):
        self._wire()
        body = client.get(
            "/api/finishingStock/report?co_id=1&branch_id=2&tran_date=2026-09-16"
        ).json()["data"]
        assert body["tran_date"] == "2026-09-16"
        assert body["co_name"] == OWN_CO_NAME
        assert body["branch_name"] == "Factory"
        assert body["bales"][0]["owner_co_name"] == OWN_CO_NAME
        assert body["bales"][0]["hessian"][0]["bales"] == 70
        assert body["despatch"][0]["bales"] is None
        assert body["totals"]["stock_mt"] == round(70 * 437.384 / 1000, 3)
        assert body["bale_source"] == "MANUAL"
        assert body["bales"][0]["hessian"][0]["inspected_bales"] is None
        assert body["bales"][0]["series"] == []
        assert body["bales"][0]["series_total"] == ZERO_SERIES_TOTAL
        assert len(self._mock_session.execute.call_args_list) == 5  # no series statement

    def test_report_endpoint_derived_mode(self):
        """BALE_PROD: the summary statement runs as on tran_date and the own
        block prints the derived count instead of the counted 70 bales."""
        self._wire_derived()
        response = client.get(
            "/api/finishingStock/report?co_id=1&branch_id=2&tran_date=2026-09-16"
        )
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["bale_source"] == "BALE_PROD"
        line = body["bales"][0]["hessian"][0]
        assert line["bales"] == 9
        assert line["inspected_bales"] == 0
        assert line["wt_mt"] == 3.62
        # the derived quality lines come from the summary statement only — the
        # series rows (which name a sacking quality too) must not add a line
        assert [l["finishing_quality_id"] for l in body["bales"][0]["sacking"]] == []
        assert body["totals"]["bales_mt"] == 3.62
        # §7a: the sacking series printed, the hessian one not
        own = body["bales"][0]
        assert own["series"] == [{
            "bale_series_id": 1, "series_name": "PUNJAB", "finishing_quality_id": 2,
            "item_name": "PRINTED TYPE A JUTE BAGS", "bales": 3, "inspected_bales": 3,
            "per_wt_kg": 290.0, "wt_mt": 0.87,
        }]
        assert own["series_total"] == {"bales": 3, "inspected_bales": 3, "wt_mt": 0.87}
        calls = self._mock_session.execute.call_args_list
        assert len(calls) == 7
        for call in calls:
            declared = set(call.args[0].compile().binds)
            passed = set(call.args[1])
            assert declared == passed, declared ^ passed
        assert calls[4].args[1] == {"co_id": 1, "branch_id": 2, "as_on": date(2026, 9, 16)}
        assert calls[5].args[1]["cancelled_status"] == [4, 6]
        assert calls[6].args[1] == calls[5].args[1]  # series statement, same binds
        assert "GROUP BY x.bale_series_id" in str(calls[6].args[0])

    def test_report_requires_tran_date(self):
        response = client.get("/api/finishingStock/report?co_id=1&branch_id=2")
        assert response.status_code == 400
