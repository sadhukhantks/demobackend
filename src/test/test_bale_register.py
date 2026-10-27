"""Tests for the Bale Register report (src/juteProduction/bale_register.py,
GET /api/juteProductionReports/bale_register — spec §7c).

Pure-builder tests drive ``build_bale_register`` with a hand-built fixture of
13 bales over 2026-10-01..09 and a 3-day register 2026-10-06..08, proving the
opening / pressed / repacked_out / despatched / closing arithmetic, the REPACK
rules, void and no-invoice despatches, the calendar-complete ``days``, group
keys and filters, totals, and the despatch detail grouping with bale-number
compression. Endpoint tests mount the router on a LOCAL FastAPI app with the
DB and auth dependencies overridden by a MagicMock session — no database —
and check the 400 matrix plus one happy path with every bind present.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.bale_register import (
    BAD_GROUP_BY_MSG,
    DATE_ORDER_MSG,
    FUTURE_DATE_MSG,
    MAX_RANGE_DAYS,
    RANGE_TOO_LONG_MSG,
    build_bale_register,
    compress_bale_nos,
    router as bale_register_router,
)

PREFIX = "/api/juteProductionReports"
URL = f"{PREFIX}/bale_register"

test_app = FastAPI()
test_app.include_router(bale_register_router, prefix=PREFIX)
client = TestClient(test_app)

D_FROM = date(2026, 10, 6)
D_TO = date(2026, 10, 8)
DAYS = ["2026-10-06", "2026-10-07", "2026-10-08"]

# --- qualities / series --------------------------------------------------------
SACK = {"finishing_quality_id": 2, "item_id": 270011, "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2}
HESS = {"finishing_quality_id": 1, "item_id": 1189, "item_name": '40"-6.50', "quality_type": 1}
OLD = {"finishing_quality_id": 5, "item_id": 5005, "item_name": "OLD BAG", "quality_type": 2}
PUNJAB = {"bale_series_id": 1, "series_name": "PUNJAB"}
BIHAR = {"bale_series_id": 2, "series_name": "BIHAR"}
HESS_SERIES = {"bale_series_id": 3, "series_name": "HESS"}
OLD_SERIES = {"bale_series_id": 9, "series_name": "OLD"}


def _bale(bid, q, series, bale_no, tran, net, entry_type="PROD"):
    return {
        "bale_entry_id": bid,
        "tran_date": tran,
        "entry_type": entry_type,
        **series,
        **q,
        "bale_no": bale_no,
        "net_wt": Decimal(str(net)),
    }


# 2026-10-06..08 is the register; 10-05 and earlier is history, 10-09 is after date_to.
BALES = [
    _bale(1, SACK, PUNJAB, 601, date(2026, 10, 5), 290),   # in stock throughout
    _bale(2, SACK, PUNJAB, 602, date(2026, 10, 5), 289),   # despatched 10-05 (before the range)
    _bale(3, SACK, PUNJAB, 603, date(2026, 10, 5), 289),   # consumed by REPACK 7 on 10-07
    _bale(4, SACK, PUNJAB, 604, date(2026, 10, 6), 291),   # pressed day 1, despatched 10-07 (invoice 171)
    _bale(5, SACK, PUNJAB, 605, date(2026, 10, 6), 292),   # pressed day 1, VOID despatch (invoice 999 cancelled)
    _bale(6, SACK, PUNJAB, 606, date(2026, 10, 6), 288),   # pressed day 1, despatched 10-08 on challan
    _bale(7, SACK, PUNJAB, 650, date(2026, 10, 7), 287, entry_type="REPACK"),  # new bale from 603
    _bale(8, SACK, PUNJAB, 607, date(2026, 10, 6), 290),   # pressed day 1, despatched 10-08 on challan
    _bale(9, SACK, PUNJAB, 608, date(2026, 10, 9), 295),   # after date_to — must be ignored
    _bale(10, SACK, BIHAR, 1, date(2026, 10, 5), 288),     # opening, despatched 10-06 (invoice 171)
    _bale(11, HESS, HESS_SERIES, 101, date(2026, 10, 4), 370),  # hessian opening
    _bale(12, HESS, HESS_SERIES, 102, date(2026, 10, 8), 372),  # hessian pressed day 3
    _bale(13, OLD, OLD_SERIES, 1, date(2026, 10, 1), 280),      # despatched 10-02 — group omitted
]

REPACKS = [
    {"bale_entry_id": 7, "tran_date": date(2026, 10, 7), "bale_series_id": 1, "bale_no": 650,
     "original_bale_no": 603, "consumed_id": 3},
    # a REPACK whose original number has no earlier bale consumes nothing
    {"bale_entry_id": 99, "tran_date": date(2026, 10, 7), "bale_series_id": 1, "bale_no": 651,
     "original_bale_no": 9999, "consumed_id": None},
]

INV_171 = {"invoice_id": 171, "invoice_no": 52, "invoice_date": date(2026, 5, 8),
           "invoice_status_id": 3, "party_name": "ACME JUTE", "is_live": 1}
NO_INVOICE = {"invoice_id": None, "invoice_no": None, "invoice_date": None,
              "invoice_status_id": None, "party_name": None, "is_live": 1}


def _desp(did, bid, d, inv, reason=None, ref=None, remarks=None, **over):
    return {
        "bale_despatch_id": did, "bale_entry_id": bid, "despatch_date": d,
        "despatch_reason": reason, "despatch_ref": ref, "remarks": remarks,
        **inv, **over,
    }


DESPATCHES = [
    _desp(1, 2, date(2026, 10, 5), INV_171),
    _desp(2, 4, date(2026, 10, 7), INV_171),
    _desp(3, 5, date(2026, 10, 7), {**INV_171, "invoice_id": 999, "invoice_no": 77,
                                    "invoice_status_id": 6, "is_live": 0}),  # VOID
    _desp(4, 6, date(2026, 10, 8), NO_INVOICE, reason="CHALLAN", ref="CH-77"),
    _desp(5, 8, date(2026, 10, 8), NO_INVOICE, reason="CHALLAN", ref="CH-77", remarks="lorry WB-12"),
    _desp(6, 10, date(2026, 10, 6), INV_171),
    _desp(7, 13, date(2026, 10, 2), INV_171),
    _desp(8, 999, date(2026, 10, 6), NO_INVOICE, reason="CHALLAN"),  # unknown bale — ignored
]


def _build(**over):
    kw = {"date_from": D_FROM, "date_to": D_TO, "group_by": "quality"}
    kw.update(over)
    return build_bale_register(BALES, REPACKS, DESPATCHES, **kw)


def _group(report, key):
    return next(g for g in report["groups"] if g["key"] == key)


def _day(group, iso):
    return next(d for d in group["days"] if d["date"] == iso)


SACK_PERIOD = {
    "opening": 3, "opening_kg": 867.0, "opening_mt": 0.867,
    "pressed": 5, "repacked_out": 1, "despatched_invoice": 2, "despatched_no_invoice": 2,
    "despatched": 4, "closing": 3, "closing_kg": 869.0, "closing_mt": 0.869,
}
HESS_PERIOD = {
    "opening": 1, "opening_kg": 370.0, "opening_mt": 0.37,
    "pressed": 1, "repacked_out": 0, "despatched_invoice": 0, "despatched_no_invoice": 0,
    "despatched": 0, "closing": 2, "closing_kg": 742.0, "closing_mt": 0.742,
}
ZERO_PERIOD = {
    "opening": 0, "opening_kg": 0.0, "opening_mt": 0.0,
    "pressed": 0, "repacked_out": 0, "despatched_invoice": 0, "despatched_no_invoice": 0,
    "despatched": 0, "closing": 0, "closing_kg": 0.0, "closing_mt": 0.0,
}


# =============================================================================
# compress_bale_nos
# =============================================================================


class TestCompressBaleNos:
    def test_empty(self):
        assert compress_bale_nos([]) == ""
        assert compress_bale_nos([None]) == ""

    def test_single(self):
        assert compress_bale_nos([601]) == "601"

    def test_runs_and_gaps(self):
        assert compress_bale_nos([601, 603, 604]) == "601, 603-604"
        assert compress_bale_nos([606, 607]) == "606-607"
        assert compress_bale_nos([1, 2, 3, 7, 9, 10, 11, 20]) == "1-3, 7, 9-11, 20"

    def test_unsorted_and_duplicates(self):
        assert compress_bale_nos([604, 601, 603, 604, 603]) == "601, 603-604"
        assert compress_bale_nos(["5", 4, Decimal("6")]) == "4-6"


# =============================================================================
# build_bale_register — arithmetic
# =============================================================================


class TestRegisterArithmetic:
    def test_opening_excludes_bales_pressed_in_range_and_bales_out_before_it(self):
        g = _group(_build(), "q:2")
        # bales 1, 3, 10 are in stock at the start of 10-06; bale 2 left on
        # 10-05, bales 4/5/6/8 are pressed on 10-06, bale 13 is another quality
        assert g["period"]["opening"] == 3
        assert g["period"]["opening_kg"] == pytest.approx(290 + 289 + 288)
        assert g["period"]["opening_mt"] == pytest.approx(0.867)
        assert g["days"][0]["opening"] == 3

    def test_day_rows_sacking(self):
        g = _group(_build(), "q:2")
        assert _day(g, "2026-10-06") == {
            "date": "2026-10-06", "opening": 3, "pressed": 4, "repacked_out": 0,
            "despatched_invoice": 1, "despatched_no_invoice": 0, "despatched": 1, "closing": 6,
        }
        assert _day(g, "2026-10-07") == {
            "date": "2026-10-07", "opening": 6, "pressed": 1, "repacked_out": 1,
            "despatched_invoice": 1, "despatched_no_invoice": 0, "despatched": 1, "closing": 5,
        }
        assert _day(g, "2026-10-08") == {
            "date": "2026-10-08", "opening": 5, "pressed": 0, "repacked_out": 0,
            "despatched_invoice": 0, "despatched_no_invoice": 2, "despatched": 2, "closing": 3,
        }

    def test_repack_counts_pressed_and_consumed_bale_leaves_same_day(self):
        g = _group(_build(), "q:2")
        d = _day(g, "2026-10-07")
        assert d["pressed"] == 1  # REPACK row 7 is a new bale
        assert d["repacked_out"] == 1  # bale 3 (no 603) consumed
        # the REPACK with no earlier bale (consumed_id None) changed nothing
        assert g["period"]["repacked_out"] == 1

    def test_void_despatch_is_ignored(self):
        g = _group(_build(), "q:2")
        # bale 5's link points at cancelled invoice 999: not despatched, still in closing
        assert _day(g, "2026-10-07")["despatched_invoice"] == 1
        assert g["period"]["closing"] == 3
        assert g["period"]["closing_kg"] == pytest.approx(290 + 292 + 287)

    def test_no_invoice_despatch_counted_separately(self):
        g = _group(_build(), "q:2")
        d = _day(g, "2026-10-08")
        assert d["despatched_no_invoice"] == 2 and d["despatched_invoice"] == 0
        assert d["despatched"] == 2
        p = g["period"]
        assert (p["despatched_invoice"], p["despatched_no_invoice"], p["despatched"]) == (2, 2, 4)

    def test_period_blocks(self):
        rep = _build()
        assert _group(rep, "q:2")["period"] == SACK_PERIOD
        assert _group(rep, "q:1")["period"] == HESS_PERIOD

    def test_closing_equals_next_opening_every_day(self):
        for group_by in ("quality", "series"):
            for g in _build(group_by=group_by)["groups"]:
                days = g["days"]
                assert days[0]["opening"] == g["period"]["opening"]
                for prev, nxt in zip(days, days[1:]):
                    assert prev["closing"] == nxt["opening"], g["key"]
                    assert prev["closing"] == (
                        prev["opening"] + prev["pressed"] - prev["repacked_out"] - prev["despatched"]
                    )
                assert days[-1]["closing"] == g["period"]["closing"]

    def test_every_calendar_day_present_even_without_movement(self):
        rep = _build()
        for g in rep["groups"]:
            assert [d["date"] for d in g["days"]] == DAYS
        hess = _group(rep, "q:1")
        assert _day(hess, "2026-10-07") == {
            "date": "2026-10-07", "opening": 1, "pressed": 0, "repacked_out": 0,
            "despatched_invoice": 0, "despatched_no_invoice": 0, "despatched": 0, "closing": 1,
        }
        # a single-day register has exactly one day row
        one = build_bale_register(BALES, REPACKS, DESPATCHES, date_from=D_FROM, date_to=D_FROM, group_by="quality")
        assert all(len(g["days"]) == 1 for g in one["groups"])
        assert _group(one, "q:2")["period"]["closing"] == 6

    def test_bale_after_date_to_is_ignored(self):
        rep = _build()
        assert _group(rep, "q:2")["period"]["pressed"] == 5  # bale 9 (10-09) not counted
        assert _group(rep, "q:2")["period"]["closing_kg"] == pytest.approx(869.0)

    def test_repack_wins_over_a_live_despatch_on_the_same_bale(self):
        bales = [
            _bale(1, SACK, PUNJAB, 601, date(2026, 10, 5), 290),
            _bale(2, SACK, PUNJAB, 650, date(2026, 10, 7), 287, entry_type="REPACK"),
        ]
        repacks = [{"bale_entry_id": 2, "tran_date": date(2026, 10, 7), "consumed_id": 1}]
        despatches = [_desp(1, 1, date(2026, 10, 6), INV_171)]
        g = _group(
            build_bale_register(bales, repacks, despatches, date_from=D_FROM, date_to=D_TO, group_by="quality"),
            "q:2",
        )
        p = g["period"]
        assert (p["opening"], p["pressed"], p["repacked_out"], p["despatched"], p["closing"]) == (1, 1, 1, 0, 1)
        assert _build()["despatch_detail"] != []  # sanity: the main fixture still has detail rows
        assert build_bale_register(
            bales, repacks, despatches, date_from=D_FROM, date_to=D_TO, group_by="quality"
        )["despatch_detail"] == []

    def test_live_test_falls_back_to_status_when_is_live_missing(self):
        bales = [_bale(1, SACK, PUNJAB, 601, date(2026, 10, 5), 290)]
        live = _desp(1, 1, date(2026, 10, 6), {**INV_171, "is_live": None})
        void = _desp(1, 1, date(2026, 10, 6), {**INV_171, "invoice_status_id": 4, "is_live": None})
        legacy = _desp(1, 1, date(2026, 10, 6), {**INV_171, "invoice_status_id": None, "is_live": None})
        for rows, expected in ((live, 1), (void, 0), (legacy, 1)):
            rep = build_bale_register(bales, [], [rows], date_from=D_FROM, date_to=D_TO, group_by="quality")
            assert _group(rep, "q:2")["period"]["despatched"] == expected

    def test_accepts_iso_strings_for_dates(self):
        bales = [{**_bale(1, SACK, PUNJAB, 601, "2026-10-05", 290), "net_wt": "290.5"}]
        despatches = [_desp(1, 1, "2026-10-06 00:00:00", NO_INVOICE, reason="SAMPLE")]
        rep = build_bale_register(bales, [], despatches, date_from="2026-10-06", date_to="2026-10-08", group_by="quality")
        p = _group(rep, "q:2")["period"]
        assert p["opening"] == 1 and p["opening_kg"] == pytest.approx(290.5)
        assert p["despatched_no_invoice"] == 1 and p["closing"] == 0
        assert rep["despatch_detail"][0]["date"] == "2026-10-06"
        assert rep["despatch_detail"][0]["despatch_reason_label"] == "Sample"

    def test_empty_inputs(self):
        rep = build_bale_register([], [], [], date_from=D_FROM, date_to=D_TO, group_by="quality")
        assert rep["groups"] == [] and rep["despatch_detail"] == []
        assert rep["totals"] == {"hessian": ZERO_PERIOD, "sacking": ZERO_PERIOD, "all": ZERO_PERIOD}

    def test_rejects_bad_group_by_and_reversed_dates(self):
        with pytest.raises(ValueError):
            build_bale_register([], [], [], date_from=D_FROM, date_to=D_TO, group_by="party")
        with pytest.raises(ValueError):
            build_bale_register([], [], [], date_from=D_TO, date_to=D_FROM, group_by="quality")


# =============================================================================
# build_bale_register — groups, filters, totals
# =============================================================================


class TestRegisterGroups:
    def test_quality_groups_shape_order_and_omission(self):
        rep = _build()
        assert [g["key"] for g in rep["groups"]] == ["q:1", "q:2"]  # hessian first; q:5 omitted
        sack = _group(rep, "q:2")
        assert sack["finishing_quality_id"] == 2
        assert sack["item_name"] == "PRINTED TYPE A JUTE BAGS"
        assert sack["quality_type"] == 2
        assert sack["bale_series_id"] is None and sack["series_name"] is None
        assert set(sack) == {"key", "finishing_quality_id", "item_name", "quality_type",
                             "bale_series_id", "series_name", "days", "period"}

    def test_group_without_opening_or_movement_is_omitted(self):
        # bale 13 (quality 5) was pressed and despatched before the range
        assert all(g["key"] != "q:5" for g in _build()["groups"])
        # …but a group whose only movement is a despatch IS kept (BIHAR closes at 0)
        rep = _build(group_by="series")
        bihar = _group(rep, "s:2:2")
        assert bihar["period"]["opening"] == 1 and bihar["period"]["closing"] == 0

    def test_series_groups(self):
        rep = _build(group_by="series")
        assert [g["key"] for g in rep["groups"]] == ["s:3:1", "s:2:2", "s:1:2"]
        punjab = _group(rep, "s:1:2")
        assert punjab["bale_series_id"] == 1 and punjab["series_name"] == "PUNJAB"
        assert punjab["finishing_quality_id"] == 2 and punjab["quality_type"] == 2
        assert punjab["period"] == {
            "opening": 2, "opening_kg": 579.0, "opening_mt": 0.579,
            "pressed": 5, "repacked_out": 1, "despatched_invoice": 1, "despatched_no_invoice": 2,
            "despatched": 3, "closing": 3, "closing_kg": 869.0, "closing_mt": 0.869,
        }
        bihar = _group(rep, "s:2:2")
        assert bihar["series_name"] == "BIHAR"
        assert _day(bihar, "2026-10-06")["despatched_invoice"] == 1
        assert bihar["period"]["closing_kg"] == 0.0
        hess = _group(rep, "s:3:1")
        assert hess["series_name"] == "HESS" and hess["period"] == HESS_PERIOD
        # hessian bales group by their per-quality series the same way
        assert hess["quality_type"] == 1
        # series grouping never changes the totals
        assert rep["totals"] == _build()["totals"]

    def test_quality_type_filter(self):
        rep = _build(quality_type=1)
        assert [g["key"] for g in rep["groups"]] == ["q:1"]
        assert rep["totals"]["hessian"] == HESS_PERIOD
        assert rep["totals"]["sacking"] == ZERO_PERIOD
        assert rep["totals"]["all"] == HESS_PERIOD
        assert rep["despatch_detail"] == []

    def test_finishing_quality_filter(self):
        rep = _build(finishing_quality_id=2)
        assert [g["key"] for g in rep["groups"]] == ["q:2"]
        assert rep["totals"]["hessian"] == ZERO_PERIOD
        assert rep["totals"]["sacking"] == SACK_PERIOD

    def test_series_filter(self):
        rep = _build(bale_series_id=2)
        assert [g["key"] for g in rep["groups"]] == ["q:2"]
        p = _group(rep, "q:2")["period"]
        assert (p["opening"], p["pressed"], p["despatched_invoice"], p["closing"]) == (1, 0, 1, 0)
        assert p["opening_kg"] == pytest.approx(288.0) and p["closing_kg"] == 0.0
        assert [r["bale_nos"] for r in rep["despatch_detail"]] == ["1"]
        # filters compose
        assert _build(bale_series_id=2, quality_type=1)["groups"] == []

    def test_totals(self):
        t = _build()["totals"]
        assert set(t) == {"hessian", "sacking", "all"}
        assert t["hessian"] == HESS_PERIOD
        assert t["sacking"] == SACK_PERIOD
        assert t["all"] == {
            "opening": 4, "opening_kg": 1237.0, "opening_mt": 1.237,
            "pressed": 6, "repacked_out": 1, "despatched_invoice": 2, "despatched_no_invoice": 2,
            "despatched": 4, "closing": 5, "closing_kg": 1611.0, "closing_mt": 1.611,
        }

    def test_unknown_quality_type_counts_in_all_only(self):
        bales = [_bale(1, {**SACK, "quality_type": None, "finishing_quality_id": 77}, PUNJAB, 1, date(2026, 10, 5), 100)]
        rep = build_bale_register(bales, [], [], date_from=D_FROM, date_to=D_TO, group_by="quality")
        assert [g["key"] for g in rep["groups"]] == ["q:77"]
        assert rep["totals"]["all"]["opening"] == 1
        assert rep["totals"]["hessian"]["opening"] == 0 and rep["totals"]["sacking"]["opening"] == 0


# =============================================================================
# build_bale_register — despatch detail
# =============================================================================


class TestDespatchDetail:
    def test_rows_grouped_and_compressed(self):
        detail = _build()["despatch_detail"]
        assert len(detail) == 3  # bale 2 (10-05) and 13 (10-02) are outside the range; bale 5 is void
        assert [(r["date"], r["series_name"], r["bale_nos"], r["count"]) for r in detail] == [
            ("2026-10-06", "BIHAR", "1", 1),
            ("2026-10-07", "PUNJAB", "604", 1),
            ("2026-10-08", "PUNJAB", "606-607", 2),
        ]

    def test_invoice_row_fields(self):
        row = _build()["despatch_detail"][0]
        assert row == {
            "date": "2026-10-06", "series_name": "BIHAR", "item_name": "PRINTED TYPE A JUTE BAGS",
            "quality_type": 2, "bale_nos": "1", "count": 1,
            "invoice_id": 171, "invoice_no": 52, "invoice_date": "2026-05-08", "party_name": "ACME JUTE",
            "despatch_reason_label": None, "despatch_ref": None, "remarks": None,
        }

    def test_no_invoice_row_fields(self):
        row = _build()["despatch_detail"][2]
        assert row["invoice_id"] is None and row["invoice_no"] is None
        assert row["invoice_date"] is None and row["party_name"] is None
        assert row["despatch_reason_label"] == "Delivery challan"
        assert row["despatch_ref"] == "CH-77"
        assert row["remarks"] == "lorry WB-12"  # first non-empty remark of the group
        assert row["count"] == 2

    def test_different_reference_splits_the_group(self):
        despatches = DESPATCHES + [
            _desp(9, 1, date(2026, 10, 8), NO_INVOICE, reason="CHALLAN", ref="CH-78"),
            _desp(10, 5, date(2026, 10, 8), NO_INVOICE, reason="SAMPLE"),
        ]
        # bale 5's void link is replaced by a live sample despatch here
        despatches = [d for d in despatches if d["bale_despatch_id"] != 3]
        detail = build_bale_register(BALES, REPACKS, despatches, date_from=D_FROM, date_to=D_TO, group_by="quality")["despatch_detail"]
        day3 = [(r["bale_nos"], r["despatch_reason_label"], r["despatch_ref"]) for r in detail if r["date"] == "2026-10-08"]
        assert day3 == [
            ("606-607", "Delivery challan", "CH-77"),
            ("601", "Delivery challan", "CH-78"),
            ("605", "Sample", None),
        ]

    def test_unknown_reason_code_falls_back_to_the_code(self):
        bales = [_bale(1, SACK, PUNJAB, 601, date(2026, 10, 5), 290)]
        despatches = [_desp(1, 1, date(2026, 10, 6), NO_INVOICE, reason="MYSTERY")]
        rep = build_bale_register(bales, [], despatches, date_from=D_FROM, date_to=D_TO, group_by="quality")
        assert rep["despatch_detail"][0]["despatch_reason_label"] == "MYSTERY"


# =============================================================================
# Endpoint (local FastAPI app, mocked session)
# =============================================================================


def _mapping_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


def _result(rows=None, one=None):
    res = MagicMock()
    res.fetchall.return_value = rows or []
    res.fetchone.return_value = one
    return res


def _assert_binds_complete(call):
    """Every named bind the statement declares must be a key in the params dict
    (call.args[0] = the text() statement, call.args[1] = the params dict)."""
    missing = set(call.args[0].compile().binds) - set(call.args[1])
    assert not missing, missing


@pytest.fixture()
def overridden_client():
    mock_db = MagicMock()
    test_app.dependency_overrides[get_tenant_db] = lambda: mock_db
    test_app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    yield client, mock_db
    test_app.dependency_overrides.clear()


BASE = {"co_id": 1, "branch_id": 2, "date_from": "2026-10-06", "date_to": "2026-10-08"}
TODAY = date.today()


class TestEndpointValidation:
    def test_requires_params(self, overridden_client):
        c, db = overridden_client
        assert c.get(URL).status_code == 400
        r = c.get(URL, params={"co_id": 1})
        assert r.status_code == 400 and "branch_id" in r.json()["detail"]
        r = c.get(URL, params={"co_id": 1, "branch_id": 2})
        assert r.status_code == 400 and "date_from" in r.json()["detail"]
        r = c.get(URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-06"})
        assert r.status_code == 400 and "date_to" in r.json()["detail"]
        db.execute.assert_not_called()

    def test_rejects_bad_values(self, overridden_client):
        c, db = overridden_client
        assert c.get(URL, params={**BASE, "co_id": "abc"}).status_code == 400
        assert c.get(URL, params={**BASE, "branch_id": "x"}).status_code == 400
        r = c.get(URL, params={**BASE, "date_from": "06-10-2026"})
        assert r.status_code == 400 and "date_from" in r.json()["detail"]
        assert c.get(URL, params={**BASE, "quality_type": "sack"}).status_code == 400
        assert c.get(URL, params={**BASE, "finishing_quality_id": "q"}).status_code == 400
        assert c.get(URL, params={**BASE, "bale_series_id": "s"}).status_code == 400
        db.execute.assert_not_called()

    def test_rejects_from_after_to(self, overridden_client):
        c, _ = overridden_client
        r = c.get(URL, params={**BASE, "date_from": "2026-10-09"})
        assert r.status_code == 400 and r.json()["detail"] == DATE_ORDER_MSG

    def test_rejects_future_date_to(self, overridden_client):
        c, _ = overridden_client
        future = (TODAY + timedelta(days=1)).isoformat()
        r = c.get(URL, params={**BASE, "date_from": TODAY.isoformat(), "date_to": future})
        assert r.status_code == 400 and r.json()["detail"] == FUTURE_DATE_MSG

    def test_rejects_range_over_92_days(self, overridden_client):
        c, _ = overridden_client
        d_to = TODAY
        too_long = d_to - timedelta(days=MAX_RANGE_DAYS)  # 93 calendar days
        r = c.get(URL, params={**BASE, "date_from": too_long.isoformat(), "date_to": d_to.isoformat()})
        assert r.status_code == 400 and r.json()["detail"] == RANGE_TOO_LONG_MSG
        assert "92" in RANGE_TOO_LONG_MSG

    def test_rejects_bad_group_by(self, overridden_client):
        c, _ = overridden_client
        r = c.get(URL, params={**BASE, "group_by": "party"})
        assert r.status_code == 400 and r.json()["detail"] == BAD_GROUP_BY_MSG


class TestEndpointHappyPath:
    def _side_effect(self):
        return [
            _result(rows=[_mapping_row(b) for b in BALES]),
            _result(rows=[_mapping_row(r) for r in REPACKS]),
            _result(rows=[_mapping_row(d) for d in DESPATCHES]),
            _result(one=_attr_row(co_name="Empire", branch_name="Factory")),
        ]

    def test_success_shape_and_binds(self, overridden_client):
        c, db = overridden_client
        db.execute.side_effect = self._side_effect()
        r = c.get(URL, params=BASE)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["date_from"] == "2026-10-06" and data["date_to"] == "2026-10-08"
        assert data["group_by"] == "quality"
        assert data["co_name"] == "Empire" and data["branch_name"] == "Factory"
        assert [g["key"] for g in data["groups"]] == ["q:1", "q:2"]
        assert _group(data, "q:2")["period"] == SACK_PERIOD
        assert data["totals"]["all"]["closing"] == 5
        assert len(data["despatch_detail"]) == 3
        assert data["despatch_detail"][2]["bale_nos"] == "606-607"

        assert db.execute.call_count == 4
        for call in db.execute.call_args_list:
            _assert_binds_complete(call)
        bales_binds = db.execute.call_args_list[0].args[1]
        assert bales_binds == {"co_id": 1, "branch_id": 2, "date_to": date(2026, 10, 8)}
        repack_binds = db.execute.call_args_list[1].args[1]
        assert repack_binds["date_to"] == date(2026, 10, 8)
        desp_binds = db.execute.call_args_list[2].args[1]
        assert desp_binds["cancelled_status"] == [4, 6]
        assert desp_binds["date_to"] == date(2026, 10, 8)
        names_binds = db.execute.call_args_list[3].args[1]
        assert names_binds == {"co_id": 1, "branch_id": 2}

    def test_series_grouping_and_filters_reach_the_builder(self, overridden_client):
        c, db = overridden_client
        db.execute.side_effect = self._side_effect()
        r = c.get(URL, params={**BASE, "group_by": "SERIES", "quality_type": 2, "bale_series_id": 1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["group_by"] == "series"
        assert [g["key"] for g in data["groups"]] == ["s:1:2"]
        assert data["totals"]["hessian"] == ZERO_PERIOD
        assert data["totals"]["sacking"]["closing"] == 3

    def test_exactly_92_days_is_accepted_and_empty_unit_gives_zero_report(self, overridden_client):
        c, db = overridden_client
        db.execute.side_effect = [_result(), _result(), _result(), _result(one=None)]
        d_to = TODAY
        d_from = d_to - timedelta(days=MAX_RANGE_DAYS - 1)
        r = c.get(URL, params={**BASE, "date_from": d_from.isoformat(), "date_to": d_to.isoformat()})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["groups"] == [] and data["despatch_detail"] == []
        assert data["totals"]["all"] == ZERO_PERIOD
        assert data["co_name"] is None and data["branch_name"] is None

    def test_db_error_is_a_500(self, overridden_client):
        c, db = overridden_client
        db.execute.side_effect = RuntimeError("boom")
        r = c.get(URL, params=BASE)
        assert r.status_code == 500 and "boom" in r.json()["detail"]


def test_router_is_registered_in_main_under_the_reports_prefix():
    from src.main import app

    assert URL in {getattr(r, "path", None) for r in app.routes}
