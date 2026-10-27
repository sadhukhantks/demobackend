"""Tests for the Jute Mechanical utilities router — ITM-004 daily power readings.

Covers ``src/juteMechanical/utility.py`` (prefix ``/api/mechUtility``).

Three layers, deliberately:

1. **Pure rules** (``compute_kwh``, ``validate_tran_date``, ``is_locked`` /
   ``check_lock``, ``median_or_none``, ``spike_warning``, ``dg_warning``,
   ``report_window_from``) — imported and called directly. No DB, no HTTP: these
   carry the module's real risk and every clock is injected (``today=``).

2. **build_power_report** — the whole /power_report arithmetic on stub rows.

3. **Endpoint tests** on a THROWAWAY FastAPI app (as test_juteprod_spinning_trend.py
   does) so the file passes before main.py is wired. ``app.dependency_overrides``
   keyed on the exact symbols the router imports (``get_tenant_db`` /
   ``get_current_user_with_refresh``); the session is a MagicMock with an ORDERED
   ``execute.side_effect`` list matching the handler's call order. Rows expose
   ``._mapping`` for fetchall reads and plain attributes for fetchone reads.
   ``now_ist`` is swapped for a fixed clock so no test depends on the wall clock.

   Statement order per endpoint (the side_effect lists below follow it):
     GET  /meters          pair check, meters
     POST /meter_create    pair check, duplicate check, insert
     PUT  /meter_edit      meter by id, duplicate check, update
     GET  /readings        pair check, lock capability, spells, meters,
                           readings, previous closing, 30-day history
     POST /readings_save   pair check, [lock capability -- locked dates only],
                           meters, spell-shape conflict, 30-day history (all
                           meters), then per row: select by key, update | insert
     DELETE /reading_delete row by id, [lock capability -- locked dates only],
                           soft delete
     GET  /power_report    pair check, readings, yarn, cloth, meters

   The lock capability is the house menu-access query (``.scalar()`` = the
   highest access_type_id the user holds on the entry menu for the branch):
   4 = Edit = may change a locked date, anything lower = 403.

Never touches a real database.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from src.juteMechanical import utility as ut
from src.juteMechanical.constants import (
    POWER_ENTRY_MENU_PATH,
    POWER_LOCK_DAYS,
    POWER_LOCK_OVERRIDE_ACCESS_LEVEL,
    WHOLE_DAY_SPELL_ID,
)
from src.juteMechanical.utility import (
    BAD_METER_TYPE_MSG,
    BRANCH_NOT_IN_COMPANY_MSG,
    BRANCH_REQUIRED_MSG,
    CLOSING_BELOW_OPENING_MSG,
    FUTURE_DATE_MSG,
    KWH_NEGATIVE_MSG,
    KWH_REQUIRED_MSG,
    LOCKED_MSG,
    MAX_RANGE_DAYS,
    METER_DUP_INACTIVE_HINT,
    METER_DUP_MSG,
    METER_NOT_FOUND_MSG,
    NO_ROWS_MSG,
    READING_NOT_FOUND_MSG,
    SPELL_CONFLICT_PER_SPELL_MSG,
    SPELL_CONFLICT_WHOLE_DAY_MSG,
    UNKNOWN_METER_MSG,
    build_power_report,
    build_reading_rows,
    check_lock,
    compute_kwh,
    dg_warning,
    get_current_user_with_refresh,
    get_tenant_db,
    is_locked,
    median_or_none,
    report_window_from,
    router,
    spike_warning,
    validate_tran_date,
)
from src.juteProduction.dpr_reports import build_power_section

app = FastAPI()
app.include_router(router, prefix="/api/mechUtility")
client = TestClient(app)

BASE = "/api/mechUtility"

# A stable IST "now" for every date rule.
NOW = datetime(2026, 10, 18, 9, 30, 0)
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)
LOCKED_DATE = TODAY - timedelta(days=POWER_LOCK_DAYS + 1)
EDGE_DATE = TODAY - timedelta(days=POWER_LOCK_DAYS)


# =============================================================================
# Row / result builders (mirrors test_jute_mechanical_breakdown.py)
# =============================================================================


def _mock_row(mapping: dict):
    """A query row whose ._mapping is a plain dict (matches r._mapping usage)."""
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    """A fetchone() row exposing attributes (e.g. .power_reading_id, .active)."""
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


_UNSET = object()


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET, first=_UNSET, scalar=_UNSET):
    """execute() result mock; the _UNSET sentinel honours an explicit fetchone=None."""
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    if first is not _UNSET:
        res.first.return_value = first
    if scalar is not _UNSET:
        res.scalar.return_value = scalar
    return res


def _pair_ok():
    """The branch-in-company check passing (SELECT 1 ... .first())."""
    return _exec_result(first=_mock_row({"1": 1}))


def _pair_bad():
    return _exec_result(first=None)


def _access(level):
    """The lock-capability lookup: the user's highest access_type_id on the
    entry menu for the branch (None = no grant at all)."""
    return _exec_result(scalar=level)


def _no_conflict():
    """The spell-shape conflict guard finding nothing."""
    return _exec_result(first=None)


def _conflict():
    return _exec_result(first=_mock_row({"1": 1}))


def _rows(mappings):
    return _exec_result(fetchall=[_mock_row(m) for m in mappings])


def _meter(code, mtype, active=1, pid=None):
    return {
        "power_meter_id": pid or {"MAIN": 1, "DG1": 2, "OLD": 3}.get(code, 9),
        "co_id": 1,
        "branch_id": 2,
        "meter_code": code,
        "meter_name": {"MAIN": "Main grid meter", "DG1": "DG set 1"}.get(code, code),
        "meter_type": mtype,
        "active": active,
    }


METERS = [_meter("MAIN", "GRID"), _meter("DG1", "DG"), _meter("OLD", "DG", active=0)]


def _reading(code, kwh, d, dg_hours=None, dg_litres=None, kva=None, mtype=None, name=None):
    return {
        "tran_date": d,
        "meter_code": code,
        "meter_name": name or {"MAIN": "Main grid meter", "DG1": "DG set 1"}.get(code, code),
        "meter_type": mtype or ("DG" if code.startswith("DG") else "GRID"),
        "kwh": kwh,
        "dg_hours": dg_hours or 0,
        "dg_litres": dg_litres or 0,
        "max_demand_kva": kva,
    }


# =============================================================================
# 1. Pure rules — no DB, no mocks
# =============================================================================


class TestPureHelpers:
    def test_compute_kwh_both_bounds_is_the_difference(self):
        assert compute_kwh(1000.0, 1500.0, None) == 500.0

    def test_compute_kwh_ignores_typed_when_both_bounds_present(self):
        assert compute_kwh(1000.0, 1500.0, 999.0) == 500.0

    def test_compute_kwh_closing_below_opening_is_400(self):
        with pytest.raises(HTTPException) as ei:
            compute_kwh(1500.0, 1000.0, None)
        assert ei.value.status_code == 400
        assert ei.value.detail == CLOSING_BELOW_OPENING_MSG

    def test_compute_kwh_equal_bounds_is_zero(self):
        assert compute_kwh(1000.0, 1000.0, None) == 0.0

    def test_compute_kwh_typed_used_only_when_a_bound_is_missing(self):
        assert compute_kwh(None, 1500.0, 40.0) == 40.0
        assert compute_kwh(1000.0, None, 40.0) == 40.0
        assert compute_kwh(None, None, 0.0) == 0.0

    def test_compute_kwh_nothing_is_400(self):
        for args in ((None, None, None), (1000.0, None, None), (None, 1500.0, None)):
            with pytest.raises(HTTPException) as ei:
                compute_kwh(*args)
            assert ei.value.status_code == 400
            assert ei.value.detail == KWH_REQUIRED_MSG

    def test_compute_kwh_negative_typed_is_its_own_400(self):
        """A negative typed kWh is a mis-key, not a missing value: distinct message."""
        for args in ((None, None, -5.0), (1000.0, None, -0.01)):
            with pytest.raises(HTTPException) as ei:
                compute_kwh(*args)
            assert ei.value.status_code == 400
            assert ei.value.detail == KWH_NEGATIVE_MSG

    def test_validate_tran_date_future_vs_injected_today(self):
        with pytest.raises(HTTPException) as ei:
            validate_tran_date(TODAY + timedelta(days=1), TODAY)
        assert ei.value.status_code == 400
        assert ei.value.detail == FUTURE_DATE_MSG
        validate_tran_date(TODAY, TODAY)
        validate_tran_date(YESTERDAY, TODAY)

    def test_is_locked_boundary(self):
        assert is_locked(EDGE_DATE, TODAY) is False  # exactly 7 days old
        assert is_locked(LOCKED_DATE, TODAY) is True  # 8 days old
        assert is_locked(TODAY, TODAY) is False

    def test_check_lock_403_without_override_passes_with_it(self):
        with pytest.raises(HTTPException) as ei:
            check_lock(LOCKED_DATE, TODAY, False)
        assert ei.value.status_code == 403
        assert ei.value.detail == LOCKED_MSG
        check_lock(LOCKED_DATE, TODAY, True)
        check_lock(EDGE_DATE, TODAY, False)

    def test_median_or_none(self):
        assert median_or_none([]) is None
        assert median_or_none([None, None]) is None
        assert median_or_none([3, 1, 2]) == 2.0
        assert median_or_none([1, 2, 3, 4]) == 2.5
        assert median_or_none([None, 10, 30]) == 20.0

    def test_spike_warning_three_times_rule(self):
        w = spike_warning("MAIN", 60000.0, 18000.0)
        assert w is not None
        assert w.startswith("MAIN:") and "3x" in w and "60,000" in w and "18,000" in w
        assert spike_warning("MAIN", 54000.0, 18000.0) is None  # exactly 3x is not above
        assert spike_warning("MAIN", 20000.0, 18000.0) is None

    def test_spike_warning_without_history(self):
        assert spike_warning("MAIN", 1e6, None) is None
        assert spike_warning("MAIN", 1e6, 0.0) is None

    def test_dg_warning_both_cases(self):
        assert dg_warning("DG1", "DG", None, 50.0) == "DG1: DG litres keyed without DG hours"
        assert dg_warning("DG1", "DG", 0.0, 50.0) == "DG1: DG litres keyed without DG hours"
        assert dg_warning("MAIN", "GRID", 2.0, None) == "MAIN: DG hours keyed on a non-DG meter"
        assert dg_warning("DG1", "DG", 2.0, 50.0) is None
        assert dg_warning("MAIN", "GRID", None, None) is None

    def test_report_window_from_is_the_min_of_three(self):
        # 30-day window reaches furthest back.
        assert report_window_from(date(2026, 10, 10), date(2026, 10, 17)) == date(2026, 9, 18)
        # range start reaches furthest back.
        assert report_window_from(date(2026, 8, 1), date(2026, 8, 5)) == date(2026, 7, 7)
        assert report_window_from(date(2026, 10, 1), date(2026, 12, 31)) == date(2026, 10, 1)
        # month start reaches furthest back only when the range is short and late.
        assert report_window_from(date(2026, 12, 30), date(2026, 12, 31)) == date(2026, 12, 1)


# =============================================================================
# 2. build_power_report — pure arithmetic over stub rows
# =============================================================================

D15, D16, D17 = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)


def _report_inputs():
    readings = [
        _reading("MAIN", 18000.0, D15, kva=1200.0),
        _reading("DG1", 900.0, D15, dg_hours=2.5, dg_litres=310.0),
        _reading("MAIN", 19000.0, D17, kva=1250.0),
        _reading("DG1", 600.0, D17, dg_hours=1.5, dg_litres=200.0),
    ]
    yarn = [
        {"tran_date": D15, "production_kg": 30000.0, "shifts": 3},
        {"tran_date": D16, "production_kg": 29000.0, "shifts": 3},
        {"tran_date": D17, "production_kg": 31000.0, "shifts": 3},
    ]
    cloth = [
        {"tran_date": D15, "production_mt": 29.0},
        {"tran_date": D16, "production_mt": 28.0},
        {"tran_date": D17, "production_mt": 30.0},
    ]
    # Master given DG-first on purpose: the output must re-order GRID before DG.
    meters = [_meter("DG1", "DG"), _meter("MAIN", "GRID"), _meter("OLD", "DG", active=0)]
    return readings, yarn, cloth, meters


class TestBuildPowerReport:
    def test_per_day_split_ratios_and_unkeyed_day(self):
        out = build_power_report(*_report_inputs(), D15, D17)
        assert [m["meter_code"] for m in out["meters"]] == ["MAIN", "DG1"]  # GRID first, OLD dropped
        assert [d["date"] for d in out["days"]] == ["2026-10-15", "2026-10-16", "2026-10-17"]
        d0, d1, d2 = out["days"]
        assert d0["keyed"] is True and d0["weekday"] == "Thu"
        assert d0["kwh_total"] == 18900.0
        assert d0["kwh_grid"] == 18000.0 and d0["kwh_dg"] == 900.0 and d0["kwh_solar"] == 0.0
        assert d0["kwh_by_meter"] == {"MAIN": 18000.0, "DG1": 900.0}
        assert d0["dg_hours"] == 2.5 and d0["dg_litres"] == 310.0
        assert d0["max_demand_kva"] == 1200.0
        assert d0["yarn_t"] == 30.0 and d0["cloth_t"] == 29.0
        assert d0["kwh_per_t_cloth"] == round(18900.0 / 29.0, 2)
        assert d0["kwh_per_t_yarn"] == round(18900.0 / 30.0, 2)
        assert d0["dg_share_pct"] == round(900.0 * 100 / 18900.0, 2)
        # Unkeyed day: zero kWh, every meter None, ratios None, tonnes still shown.
        assert d1["keyed"] is False
        assert d1["kwh_total"] == 0.0
        assert d1["kwh_by_meter"] == {"MAIN": None, "DG1": None}
        assert d1["kwh_per_t_cloth"] is None and d1["kwh_per_t_yarn"] is None
        assert d1["dg_share_pct"] is None and d1["max_demand_kva"] is None
        assert d1["yarn_t"] == 29.0 and d1["cloth_t"] == 28.0
        assert d2["kwh_total"] == 19600.0

    def test_totals_over_keyed_days_only(self):
        out = build_power_report(*_report_inputs(), D15, D17)
        t = out["totals"]
        assert (t["days"], t["days_keyed"]) == (3, 2)
        assert t["kwh_total"] == 38500.0 and t["kwh_grid"] == 37000.0 and t["kwh_dg"] == 1500.0
        assert t["dg_hours"] == 4.0 and t["dg_litres"] == 510.0
        assert t["max_demand_kva"] == 1250.0
        # Tonnes of the keyed days (15 + 17), not the unkeyed 16th.
        assert t["cloth_t"] == 59.0 and t["yarn_t"] == 61.0
        assert t["kwh_per_t_cloth"] == round(38500.0 / 59.0, 2)
        assert t["kwh_per_t_yarn"] == round(38500.0 / 61.0, 2)
        assert t["dg_share_pct"] == round(1500.0 * 100 / 38500.0, 2)
        assert t["avg_kwh_per_keyed_day"] == 19250.0

    def test_mtd_block_is_the_month_of_date_to(self):
        out = build_power_report(*_report_inputs(), D16, D17)
        mtd = out["mtd"]
        assert (mtd["days"], mtd["days_keyed"]) == (17, 2)  # 1..17 Oct, 15th and 17th keyed
        assert mtd["kwh_total"] == 38500.0
        # The range itself only holds the 17th.
        assert (out["totals"]["days"], out["totals"]["days_keyed"]) == (2, 1)
        assert out["totals"]["kwh_total"] == 19600.0

    def test_std_is_the_median_over_the_30_day_window(self):
        out = build_power_report(*_report_inputs(), D15, D17)
        std = out["std"]
        assert std["basis"] == "30-day median ending 2026-10-17"
        assert std["days_used"] == 2
        assert std["kwh_total"] == 19250.0
        assert std["kwh_per_t_cloth"] == pytest.approx(
            (round(18900.0 / 29.0, 2) + round(19600.0 / 30.0, 2)) / 2, abs=0.01
        )
        assert std["kwh_per_t_yarn"] == pytest.approx(
            (round(18900.0 / 30.0, 2) + round(19600.0 / 31.0, 2)) / 2, abs=0.01
        )
        assert std["dg_share_pct"] == pytest.approx(
            (round(900.0 * 100 / 18900.0, 2) + round(600.0 * 100 / 19600.0, 2)) / 2, abs=0.01
        )

    def test_zero_tonnes_gives_none_ratio_never_zero(self):
        readings, yarn, cloth, meters = _report_inputs()
        cloth = [{"tran_date": D15, "production_mt": 0.0}]
        yarn = []
        out = build_power_report(readings, yarn, cloth, meters, D15, D15)
        d = out["days"][0]
        assert d["keyed"] is True and d["kwh_total"] == 18900.0
        assert d["kwh_per_t_cloth"] is None and d["kwh_per_t_yarn"] is None
        assert out["totals"]["kwh_per_t_cloth"] is None
        assert out["std"]["kwh_per_t_cloth"] is None  # no day had a ratio

    def test_inactive_meter_with_readings_in_window_is_listed(self):
        readings, yarn, cloth, meters = _report_inputs()
        readings.append(_reading("OLD", 50.0, D15, mtype="DG", name="Old DG"))
        out = build_power_report(readings, yarn, cloth, meters, D15, D17)
        assert [m["meter_code"] for m in out["meters"]] == ["MAIN", "DG1", "OLD"]
        assert out["days"][0]["kwh_by_meter"]["OLD"] == 50.0
        assert out["days"][0]["kwh_dg"] == 950.0

    def test_reading_without_master_row_falls_back_to_code(self):
        readings = [_reading("TEMP", 10.0, D15, mtype="GRID", name=None)]
        out = build_power_report(readings, [], [], [], D15, D15)
        assert out["meters"] == [{"meter_code": "TEMP", "meter_name": "TEMP", "meter_type": "GRID"}]
        assert out["days"][0]["kwh_grid"] == 10.0

    def test_empty_inputs_never_raise(self):
        out = build_power_report([], [], [], [], D15, D17)
        assert out["meters"] == []
        assert len(out["days"]) == 3 and not any(d["keyed"] for d in out["days"])
        assert out["days"][0]["kwh_by_meter"] == {}
        t = out["totals"]
        assert (t["days"], t["days_keyed"], t["kwh_total"]) == (3, 0, 0.0)
        assert t["kwh_per_t_cloth"] is None and t["avg_kwh_per_keyed_day"] is None
        assert t["dg_share_pct"] is None and t["max_demand_kva"] is None
        std = out["std"]
        assert std["days_used"] == 0
        assert std["kwh_total"] is None and std["kwh_per_t_cloth"] is None

    def test_iso_string_dates_are_accepted(self):
        readings = [_reading("MAIN", 100.0, "2026-10-15")]
        out = build_power_report(readings, [{"tran_date": "2026-10-15", "production_kg": 1000.0}], [], [], D15, D15)
        assert out["days"][0]["keyed"] is True
        assert out["days"][0]["kwh_per_t_yarn"] == 100.0


class TestBuildReadingRows:
    def test_active_meters_merged_with_saved_rows_and_inactive_kept(self):
        readings = [
            {"power_reading_id": 11, "meter_code": "MAIN", "meter_name": "Main grid meter",
             "meter_type": "GRID", "meter_active": 1, "opening_kwh": 1000, "closing_kwh": 1500,
             "kwh": 500, "dg_hours": None, "dg_litres": None, "max_demand_kva": 1200, "remarks": "ok"},
            {"power_reading_id": 12, "meter_code": "OLD", "meter_name": "Old DG",
             "meter_type": "DG", "meter_active": 0, "opening_kwh": None, "closing_kwh": None,
             "kwh": 20, "dg_hours": 1, "dg_litres": 10, "max_demand_kva": None, "remarks": None},
        ]
        prev = {"DG1": {"meter_code": "DG1", "closing_kwh": 250.5, "tran_date": D16}}
        rows = build_reading_rows(METERS, readings, prev, {"MAIN": 500.0, "DG1": 50.0})
        assert [r["meter_code"] for r in rows] == ["MAIN", "DG1", "OLD"]
        main, dg1, old = rows
        assert main["power_reading_id"] == 11 and main["kwh"] == 500.0 and main["remarks"] == "ok"
        assert main["median30_kwh"] == 500.0 and main["prefill_opening_kwh"] is None
        assert dg1["power_reading_id"] is None and dg1["kwh"] is None
        assert dg1["prefill_opening_kwh"] == 250.5 and dg1["prefill_from_date"] == "2026-10-16"
        assert dg1["median30_kwh"] == 50.0
        assert old["meter_active"] == 0 and old["power_reading_id"] == 12 and old["median30_kwh"] is None


# =============================================================================
# 2b. DPR <-> report reconciliation — pure
# =============================================================================


class TestDprReconciliation:
    """The DPR Power line and /power_report print the SAME MTD kWh/t for a
    month: both divide the kWh of the keyed days by the tonnes of those days."""

    def test_mtd_kwh_per_t_matches_between_dpr_and_power_report(self):
        readings, yarn, cloth, meters = _report_inputs()
        report = build_power_report(readings, yarn, cloth, meters, D15, D17)
        mtd = report["mtd"]
        # Keyed 15th + 17th only: 38,500 kWh over 59 t cloth / 61 t yarn.
        assert (mtd["kwh_total"], mtd["cloth_t"], mtd["yarn_t"]) == (38500.0, 59.0, 61.0)
        # The DPR row carries the same keyed-day tonnes (the query's scalar
        # subqueries), NOT the month's 87 t / 90 t.
        dpr = build_power_section(
            {
                "day_kwh": 19600.0, "day_dg_kwh": 600.0, "day_dg_hours": 1.5, "day_dg_litres": 200.0,
                "day_rows": 2, "mtd_kwh": 38500.0, "mtd_dg_kwh": 1500.0, "mtd_dg_hours": 4.0,
                "mtd_dg_litres": 510.0, "mtd_days": 2,
                "mtd_keyed_cloth_mt": 59.0, "mtd_keyed_yarn_kg": 61000.0,
            },
            31.0,
            30.0,
        )
        assert dpr["mtd"]["kwh_per_t_cloth"] == mtd["kwh_per_t_cloth"] == round(38500.0 / 59.0, 2)
        assert dpr["mtd"]["kwh_per_t_yarn"] == mtd["kwh_per_t_yarn"] == round(38500.0 / 61.0, 2)
        assert dpr["day"]["kwh_per_t_cloth"] == report["days"][2]["kwh_per_t_cloth"]


# =============================================================================
# 3. Endpoints — throwaway app, mocked session, fixed IST clock
# =============================================================================


class _EndpointBase:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        self._orig_now = ut.now_ist
        ut.now_ist = lambda: NOW

    def teardown_method(self):
        app.dependency_overrides.clear()
        ut.now_ist = self._orig_now

    def _binds(self, call_index: int) -> dict:
        """The bind dict of the n-th db.execute call."""
        return self._session.execute.call_args_list[call_index].args[1]


PAIR_BINDS = {"co_id": 1, "branch_id": 2}


class TestMeterEndpoints(_EndpointBase):
    def test_meters_requires_co_id(self):
        resp = client.get(f"{BASE}/meters", params={"branch_id": 2})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "co_id is required"
        self._session.execute.assert_not_called()

    def test_meters_requires_branch_id(self):
        resp = client.get(f"{BASE}/meters", params={"co_id": 1})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "branch_id is required"

    def test_meters_branch_of_another_company_400(self):
        self._session.execute.side_effect = [_pair_bad()]
        resp = client.get(f"{BASE}/meters", params={"co_id": 1, "branch_id": 29})
        assert resp.status_code == 400
        assert resp.json()["detail"] == BRANCH_NOT_IN_COMPANY_MSG
        assert self._session.execute.call_count == 1
        assert self._binds(0) == {"co_id": 1, "branch_id": 29}

    def test_meters_happy(self):
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS)]
        resp = client.get(f"{BASE}/meters", params={"co_id": 1, "branch_id": 2, "include_inactive": 1})
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert [m["meter_code"] for m in body["data"]] == ["MAIN", "DG1", "OLD"]
        assert body["data"][0]["meter_type_label"] == "Grid"
        assert body["data"][2]["active"] == 0
        # The option list rides under the house `master` key, not beside `data`.
        assert [t["code"] for t in body["master"]["meter_types"]] == ["GRID", "DG", "SOLAR"]
        assert "meter_types" not in body
        assert self._binds(0) == PAIR_BINDS
        assert self._binds(1) == {"co_id": 1, "branch_id": 2, "include_inactive": 1}

    def test_meters_default_excludes_inactive(self):
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS[:2])]
        resp = client.get(f"{BASE}/meters", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200
        assert self._binds(1)["include_inactive"] == 0

    def test_meter_create_bad_type_400_before_db(self):
        resp = client.post(
            f"{BASE}/meter_create",
            json={"co_id": 1, "branch_id": 2, "meter_code": "DG2", "meter_name": "DG set 2", "meter_type": "WIND"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == BAD_METER_TYPE_MSG
        self._session.execute.assert_not_called()

    def test_meter_create_requires_branch_id(self):
        resp = client.post(
            f"{BASE}/meter_create",
            json={"co_id": 1, "meter_code": "DG2", "meter_name": "DG set 2", "meter_type": "DG"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == BRANCH_REQUIRED_MSG
        self._session.execute.assert_not_called()

    def test_meter_create_branch_of_another_company_400(self):
        self._session.execute.side_effect = [_pair_bad()]
        resp = client.post(
            f"{BASE}/meter_create",
            json={"co_id": 1, "branch_id": 29, "meter_code": "DG2", "meter_name": "DG set 2", "meter_type": "DG"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == BRANCH_NOT_IN_COMPANY_MSG
        self._session.commit.assert_not_called()
        self._session.rollback.assert_called_once()

    def test_meter_create_duplicate_with_inactive_hint(self):
        self._session.execute.side_effect = [
            _pair_ok(),
            _exec_result(fetchone=_attr_row(power_meter_id=5, active=0)),
        ]
        resp = client.post(
            f"{BASE}/meter_create",
            json={"co_id": 1, "branch_id": 2, "meter_code": "dg2", "meter_name": "DG set 2", "meter_type": "DG"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == METER_DUP_MSG + METER_DUP_INACTIVE_HINT
        self._session.rollback.assert_called_once()
        self._session.commit.assert_not_called()

    def test_meter_create_duplicate_active(self):
        self._session.execute.side_effect = [
            _pair_ok(),
            _exec_result(fetchone=_attr_row(power_meter_id=5, active=1)),
        ]
        resp = client.post(
            f"{BASE}/meter_create",
            json={"co_id": 1, "branch_id": 2, "meter_code": "DG2", "meter_name": "DG set 2", "meter_type": "DG"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == METER_DUP_MSG

    def test_meter_create_happy_uppercases_code(self):
        self._session.execute.side_effect = [
            _pair_ok(),  # branch belongs to company
            _exec_result(fetchone=None),  # duplicate check
            _exec_result(lastrowid=77),  # insert
        ]
        resp = client.post(
            f"{BASE}/meter_create",
            json={"co_id": 1, "branch_id": 2, "meter_code": " dg2 ", "meter_name": "DG set 2", "meter_type": "DG"},
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json() == {"data": {"power_meter_id": 77}}
        self._session.commit.assert_called_once()
        assert self._binds(0) == PAIR_BINDS
        assert self._binds(1)["meter_code"] == "DG2" and self._binds(1)["power_meter_id"] is None
        ins = self._binds(2)
        assert ins["meter_code"] == "DG2" and ins["active"] == 1 and ins["updated_by"] == 1

    def test_meter_edit_404(self):
        self._session.execute.side_effect = [_exec_result(fetchone=None)]
        resp = client.put(
            f"{BASE}/meter_edit/9",
            json={"co_id": 1, "branch_id": 2, "meter_code": "DG1", "meter_name": "DG set 1", "meter_type": "DG", "active": 0},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == METER_NOT_FOUND_MSG
        self._session.commit.assert_not_called()

    def test_meter_edit_happy_deactivates_and_ignores_body_branch(self):
        """branch_id is optional on edit and never moves the meter: the
        duplicate check runs against the STORED branch (2), not the body's."""
        self._session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(power_meter_id=9, co_id=1, branch_id=2, meter_code="DG1")),
            _exec_result(fetchone=None),  # duplicate check (self excluded)
            _exec_result(),  # update
        ]
        resp = client.put(
            f"{BASE}/meter_edit/9",
            json={"co_id": 1, "branch_id": 99, "meter_code": "DG1", "meter_name": "DG set 1 (standby)", "meter_type": "DG", "active": 0},
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json() == {"data": {"power_meter_id": 9}}
        dup = self._binds(1)
        assert dup["power_meter_id"] == 9 and dup["branch_id"] == 2  # stored branch, self excluded
        upd = self._binds(2)
        assert upd["active"] == 0 and upd["meter_name"] == "DG set 1 (standby)" and upd["power_meter_id"] == 9
        assert "branch_id" not in upd
        self._session.commit.assert_called_once()

    def test_meter_edit_without_branch_id_is_accepted(self):
        self._session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(power_meter_id=9, co_id=1, branch_id=2, meter_code="DG1")),
            _exec_result(fetchone=None),
            _exec_result(),
        ]
        resp = client.put(
            f"{BASE}/meter_edit/9",
            json={"co_id": 1, "meter_code": "DG1", "meter_name": "DG set 1", "meter_type": "DG", "active": 1},
        )
        assert resp.status_code == 200, resp.json()


def _readings_day_side_effects(access=1, readings=(), prev=(), history=()):
    """GET /readings execute order: pair check, lock capability, spells, meters,
    readings, prev closing, history."""
    return [
        _pair_ok(),
        _access(access),
        _rows([
            {"spell_id": 1, "spell_code": "A", "spell_name": "Morning", "working_hours": 8},
            {"spell_id": 4, "spell_code": "A", "spell_name": "Morning (dup)", "working_hours": 8},
            {"spell_id": 2, "spell_code": "B", "spell_name": "Evening", "working_hours": 8},
        ]),
        _rows(METERS),
        _rows(list(readings)),
        _rows(list(prev)),
        _rows(list(history)),
    ]


class TestReadingsEndpoints(_EndpointBase):
    def test_readings_requires_tran_date(self):
        resp = client.get(f"{BASE}/readings", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "tran_date is required"

    def test_readings_bad_tran_date(self):
        resp = client.get(f"{BASE}/readings", params={"co_id": 1, "branch_id": 2, "tran_date": "17-10-2026"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid tran_date format"

    def test_readings_branch_of_another_company_400(self):
        self._session.execute.side_effect = [_pair_bad()]
        resp = client.get(
            f"{BASE}/readings", params={"co_id": 1, "branch_id": 29, "tran_date": YESTERDAY.isoformat()}
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == BRANCH_NOT_IN_COMPANY_MSG
        assert self._session.execute.call_count == 1

    def test_readings_happy_merges_prefill_median_and_lock(self):
        self._session.execute.side_effect = _readings_day_side_effects(
            readings=[
                {"power_reading_id": 11, "co_id": 1, "branch_id": 2, "tran_date": YESTERDAY, "spell_id": 0,
                 "meter_code": "MAIN", "meter_name": "Main grid meter", "meter_type": "GRID", "meter_active": 1,
                 "opening_kwh": 1000, "closing_kwh": 1500, "kwh": 500, "dg_hours": None, "dg_litres": None,
                 "max_demand_kva": 1200, "remarks": None, "spell_code": None},
                {"power_reading_id": 12, "co_id": 1, "branch_id": 2, "tran_date": YESTERDAY, "spell_id": 0,
                 "meter_code": "OLD", "meter_name": "OLD", "meter_type": "DG", "meter_active": 0,
                 "opening_kwh": None, "closing_kwh": None, "kwh": 20, "dg_hours": 1, "dg_litres": 10,
                 "max_demand_kva": None, "remarks": "retired", "spell_code": None},
            ],
            prev=[{"meter_code": "DG1", "closing_kwh": 250.5, "tran_date": YESTERDAY - timedelta(days=1)}],
            history=[
                {"meter_code": "MAIN", "tran_date": YESTERDAY - timedelta(days=2), "kwh": 400},
                {"meter_code": "MAIN", "tran_date": YESTERDAY - timedelta(days=1), "kwh": 600},
                {"meter_code": "DG1", "tran_date": YESTERDAY - timedelta(days=1), "kwh": 50},
            ],
        )
        resp = client.get(
            f"{BASE}/readings", params={"co_id": 1, "branch_id": 2, "tran_date": YESTERDAY.isoformat()}
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        assert data["tran_date"] == YESTERDAY.isoformat()
        assert data["spell_id"] is None
        assert data["today"] == TODAY.isoformat()
        assert data["lock"] == {"locked": False, "can_override": False, "lock_days": POWER_LOCK_DAYS}
        assert [s["spell_code"] for s in data["spells"]] == ["A", "B"]  # de-duplicated by code
        rows = data["rows"]
        assert [r["meter_code"] for r in rows] == ["MAIN", "DG1", "OLD"]
        main, dg1, old = rows
        assert main["power_reading_id"] == 11 and main["kwh"] == 500.0 and main["median30_kwh"] == 500.0
        assert main["max_demand_kva"] == 1200.0
        assert dg1["power_reading_id"] is None
        assert dg1["prefill_opening_kwh"] == 250.5
        assert dg1["prefill_from_date"] == (YESTERDAY - timedelta(days=1)).isoformat()
        assert dg1["median30_kwh"] == 50.0
        assert old["meter_active"] == 0 and old["power_reading_id"] == 12 and old["remarks"] == "retired"
        # Execute order + binds: pair check, capability on the entry menu for
        # THIS branch, readings keyed on spell 0 (whole day), previous closing
        # told the spell being keyed, history over the 30 days before the date
        # for every meter.
        assert self._session.execute.call_count == 7
        assert self._binds(0) == PAIR_BINDS
        assert self._binds(1) == {"user_id": 1, "co_id": 1, "branch_id": 2, "menu_path": POWER_ENTRY_MENU_PATH}
        assert self._binds(3)["include_inactive"] == 1
        assert self._binds(4)["spell_id"] == 0
        prev = self._binds(5)
        assert prev["spell_id"] == WHOLE_DAY_SPELL_ID and prev["whole_day"] == WHOLE_DAY_SPELL_ID
        assert prev["tran_date"] == YESTERDAY
        hist = self._binds(6)
        assert hist["meter_code"] is None
        assert hist["date_from"] == YESTERDAY - timedelta(days=30)
        assert hist["date_to"] == YESTERDAY - timedelta(days=1)

    def test_readings_spell_passes_through_to_readings_and_prefill(self):
        self._session.execute.side_effect = _readings_day_side_effects()
        resp = client.get(
            f"{BASE}/readings", params={"co_id": 1, "branch_id": 2, "tran_date": YESTERDAY.isoformat(), "spell_id": 2}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["spell_id"] == 2
        assert self._binds(4)["spell_id"] == 2
        # Spell B's opening is offered from spell A's closing of the SAME day
        # (the query ranks same-day earlier spells first) -- the spell rides along.
        assert self._binds(5)["spell_id"] == 2 and self._binds(5)["whole_day"] == 0

    def test_readings_locked_date_with_edit_capability(self):
        self._session.execute.side_effect = _readings_day_side_effects(access=POWER_LOCK_OVERRIDE_ACCESS_LEVEL)
        resp = client.get(
            f"{BASE}/readings", params={"co_id": 1, "branch_id": 2, "tran_date": LOCKED_DATE.isoformat()}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["lock"] == {"locked": True, "can_override": True, "lock_days": POWER_LOCK_DAYS}

    def test_readings_locked_date_with_write_only_or_no_grant(self):
        for level in (3, None):
            self._session.execute.side_effect = _readings_day_side_effects(access=level)
            resp = client.get(
                f"{BASE}/readings", params={"co_id": 1, "branch_id": 2, "tran_date": LOCKED_DATE.isoformat()}
            )
            assert resp.status_code == 200
            assert resp.json()["data"]["lock"]["locked"] is True
            assert resp.json()["data"]["lock"]["can_override"] is False

    # ----- POST /readings_save -------------------------------------------------

    def _save(self, rows, tran_date=YESTERDAY, spell_id=None):
        return client.post(
            f"{BASE}/readings_save",
            json={"co_id": 1, "branch_id": 2, "tran_date": tran_date.isoformat(), "spell_id": spell_id, "rows": rows},
        )

    def _pre(self, history=()):
        """The statements before the per-row writes on an UNLOCKED date:
        pair check, meters, conflict guard, one history pass for every meter."""
        return [_pair_ok(), _rows(METERS), _no_conflict(), _rows(list(history))]

    def test_save_happy_insert_and_update_with_spike_warning(self):
        self._session.execute.side_effect = [
            _pair_ok(),  # 0 branch belongs to company
            _rows(METERS),  # 1 meters
            _no_conflict(),  # 2 spell-shape guard
            _rows([  # 3 ONE history pass for every meter (MAIN median 500, DG1 median 11)
                {"meter_code": "DG1", "tran_date": D15, "kwh": 10},
                {"meter_code": "DG1", "tran_date": D16, "kwh": 12},
                {"meter_code": "MAIN", "tran_date": D15, "kwh": 480},
                {"meter_code": "MAIN", "tran_date": D16, "kwh": 520},
            ]),
            _exec_result(fetchone=None),  # 4 MAIN by key -> new
            _exec_result(lastrowid=101),  # 5 MAIN insert
            _exec_result(fetchone=_attr_row(power_reading_id=55, active=0)),  # 6 DG1 by key -> soft-deleted row
            _exec_result(),  # 7 DG1 update
        ]
        resp = self._save([
            {"meter_code": "MAIN", "opening_kwh": 1000, "closing_kwh": 1500, "max_demand_kva": 1200},
            {"meter_code": "dg1", "opening_kwh": 100, "closing_kwh": 160, "dg_hours": 2, "dg_litres": 50, "remarks": " ran at peak "},
            {"meter_code": "GHOST"},  # nothing keyed -> skipped silently, never looked up
        ])
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        assert data["saved"] == 2
        assert data["ids"] == [101, 55]
        assert len(data["warnings"]) == 1
        assert data["warnings"][0].startswith("DG1:") and "3x" in data["warnings"][0]
        self._session.commit.assert_called_once()
        self._session.rollback.assert_not_called()
        assert self._session.execute.call_count == 8
        assert self._binds(0) == PAIR_BINDS
        assert self._binds(1)["include_inactive"] == 1
        guard = self._binds(2)
        assert guard == {"co_id": 1, "branch_id": 2, "tran_date": YESTERDAY, "spell_id": 0, "whole_day": 0}
        hist = self._binds(3)
        assert hist["meter_code"] is None  # every meter in one pass, not one query per row
        assert hist["date_from"] == YESTERDAY - timedelta(days=30) and hist["date_to"] == YESTERDAY - timedelta(days=1)
        key = self._binds(4)
        assert key == {"co_id": 1, "branch_id": 2, "tran_date": YESTERDAY, "spell_id": 0, "meter_code": "MAIN"}
        ins = self._binds(5)
        assert ins["kwh"] == 500.0 and ins["opening_kwh"] == 1000 and ins["closing_kwh"] == 1500
        assert ins["spell_id"] == 0 and ins["meter_code"] == "MAIN" and ins["updated_by"] == 1
        assert ins["max_demand_kva"] == 1200 and ins["remarks"] is None
        assert "active" not in ins  # literal 1 in the INSERT, not a bind
        upd = self._binds(7)
        assert upd["power_reading_id"] == 55 and upd["kwh"] == 60.0
        assert upd["dg_hours"] == 2 and upd["dg_litres"] == 50 and upd["remarks"] == "ran at peak"

    def test_save_branch_of_another_company_400_before_anything(self):
        self._session.execute.side_effect = [_pair_bad()]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}])
        assert resp.status_code == 400
        assert resp.json()["detail"] == BRANCH_NOT_IN_COMPANY_MSG
        assert self._session.execute.call_count == 1
        self._session.rollback.assert_called_once()
        self._session.commit.assert_not_called()

    def test_save_closing_below_opening_400_rolls_back(self):
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS)]
        resp = self._save([{"meter_code": "MAIN", "opening_kwh": 1500, "closing_kwh": 1000}])
        assert resp.status_code == 400
        assert resp.json()["detail"] == CLOSING_BELOW_OPENING_MSG
        self._session.rollback.assert_called_once()
        self._session.commit.assert_not_called()
        assert self._session.execute.call_count == 2  # nothing written, no per-row statement

    def test_save_negative_opening_is_422_from_the_model(self):
        resp = self._save([{"meter_code": "MAIN", "opening_kwh": -100, "closing_kwh": -50}])
        assert resp.status_code == 422
        self._session.execute.assert_not_called()

    def test_save_negative_typed_kwh_400_with_its_own_message(self):
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS)]
        resp = self._save([{"meter_code": "MAIN", "kwh": -10}])
        assert resp.status_code == 400
        assert resp.json()["detail"] == KWH_NEGATIVE_MSG

    def test_save_future_date_400_before_any_execute(self):
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}], tran_date=TODAY + timedelta(days=1))
        assert resp.status_code == 400
        assert resp.json()["detail"] == FUTURE_DATE_MSG
        self._session.execute.assert_not_called()

    def test_save_today_is_allowed_without_a_capability_lookup(self):
        self._session.execute.side_effect = [
            *self._pre(), _exec_result(fetchone=None), _exec_result(lastrowid=5),
        ]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}], tran_date=TODAY)
        assert resp.status_code == 200, resp.json()
        assert resp.json()["data"]["warnings"] == []  # no history -> no spike check
        # Inside the window the lock capability is never looked up.
        assert all("menu_path" not in self._binds(i) for i in range(self._session.execute.call_count))

    def test_save_locked_date_403_for_write_only_grant(self):
        self._session.execute.side_effect = [_pair_ok(), _access(3)]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}], tran_date=LOCKED_DATE)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LOCKED_MSG
        assert self._binds(1) == {"user_id": 1, "co_id": 1, "branch_id": 2, "menu_path": POWER_ENTRY_MENU_PATH}
        self._session.rollback.assert_called_once()
        self._session.commit.assert_not_called()

    def test_save_locked_date_403_without_any_grant(self):
        self._session.execute.side_effect = [_pair_ok(), _access(None)]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}], tran_date=LOCKED_DATE)
        assert resp.status_code == 403

    def test_save_locked_date_allowed_with_edit_capability(self):
        self._session.execute.side_effect = [
            _pair_ok(), _access(POWER_LOCK_OVERRIDE_ACCESS_LEVEL), _rows(METERS), _no_conflict(), _rows([]),
            _exec_result(fetchone=None), _exec_result(lastrowid=9),
        ]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}], tran_date=LOCKED_DATE)
        assert resp.status_code == 200, resp.json()
        assert resp.json()["data"]["ids"] == [9]

    def test_save_edge_date_not_locked(self):
        self._session.execute.side_effect = [
            *self._pre(), _exec_result(fetchone=None), _exec_result(lastrowid=9),
        ]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}], tran_date=EDGE_DATE)
        assert resp.status_code == 200, resp.json()

    def test_save_unknown_meter_400(self):
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS)]
        resp = self._save([{"meter_code": "SOLAR9", "kwh": 10}])
        assert resp.status_code == 400
        assert resp.json()["detail"] == UNKNOWN_METER_MSG
        self._session.commit.assert_not_called()

    def test_save_inactive_meter_is_accepted(self):
        self._session.execute.side_effect = [
            *self._pre(), _exec_result(fetchone=None), _exec_result(lastrowid=3),
        ]
        resp = self._save([{"meter_code": "OLD", "kwh": 10}])
        assert resp.status_code == 200, resp.json()

    def test_save_all_blank_rows_400(self):
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS)]
        resp = self._save([{"meter_code": "MAIN"}, {"meter_code": "DG1", "remarks": "  "}])
        assert resp.status_code == 400
        assert resp.json()["detail"] == NO_ROWS_MSG

    def test_save_missing_bound_without_kwh_400(self):
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS)]
        resp = self._save([{"meter_code": "MAIN", "closing_kwh": 1500}])
        assert resp.status_code == 400
        assert resp.json()["detail"] == KWH_REQUIRED_MSG

    def test_save_typed_kwh_on_meter_reset(self):
        self._session.execute.side_effect = [
            *self._pre(), _exec_result(fetchone=None), _exec_result(lastrowid=4),
        ]
        resp = self._save([{"meter_code": "MAIN", "closing_kwh": 20, "kwh": 320}])
        assert resp.status_code == 200, resp.json()
        ins = self._binds(5)
        assert ins["kwh"] == 320.0 and ins["opening_kwh"] is None and ins["closing_kwh"] == 20

    def test_save_dg_warning_does_not_block(self):
        self._session.execute.side_effect = [
            *self._pre(), _exec_result(fetchone=None), _exec_result(lastrowid=4),
        ]
        resp = self._save([{"meter_code": "DG1", "kwh": 30, "dg_litres": 40}])
        assert resp.status_code == 200, resp.json()
        assert resp.json()["data"]["warnings"] == ["DG1: DG litres keyed without DG hours"]

    def test_save_existing_key_updates_and_returns_existing_id(self):
        self._session.execute.side_effect = [
            *self._pre(),
            _exec_result(fetchone=_attr_row(power_reading_id=42, active=1)),
            _exec_result(),
        ]
        resp = self._save([{"meter_code": "MAIN", "opening_kwh": 10, "closing_kwh": 25}], spell_id=2)
        assert resp.status_code == 200, resp.json()
        assert resp.json()["data"] == {"saved": 1, "ids": [42], "warnings": []}
        assert self._binds(2)["spell_id"] == 2  # the guard looks for a whole-day row
        assert self._binds(4)["spell_id"] == 2
        assert self._binds(5)["power_reading_id"] == 42 and self._binds(5)["kwh"] == 15.0

    def test_save_whole_day_refused_when_spells_are_keyed(self):
        """Same (date, meter) keyed per spell AND as a whole day would be summed
        twice by every reader -- the whole-day save is refused."""
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS), _conflict()]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}], spell_id=None)
        assert resp.status_code == 400
        assert resp.json()["detail"] == SPELL_CONFLICT_WHOLE_DAY_MSG
        assert self._session.execute.call_count == 3  # nothing written
        self._session.rollback.assert_called_once()
        self._session.commit.assert_not_called()

    def test_save_spell_refused_when_whole_day_is_keyed(self):
        self._session.execute.side_effect = [_pair_ok(), _rows(METERS), _conflict()]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}], spell_id=2)
        assert resp.status_code == 400
        assert resp.json()["detail"] == SPELL_CONFLICT_PER_SPELL_MSG
        assert self._binds(2)["spell_id"] == 2 and self._binds(2)["whole_day"] == 0
        self._session.commit.assert_not_called()

    def test_save_insert_race_falls_back_to_update(self):
        """Two electricians save the same key at once: the loser's INSERT hits
        uq_jpr_scope under the savepoint, and the row is UPDATEd instead of a
        raw 500 'Duplicate entry'."""
        dup = IntegrityError("INSERT ...", {}, Exception("Duplicate entry '1-2-...' for key 'uq_jpr_scope'"))
        self._session.execute.side_effect = [
            *self._pre(),
            _exec_result(fetchone=None),  # 4 by key -> nothing (yet)
            dup,  # 5 insert loses the race
            _exec_result(fetchone=_attr_row(power_reading_id=77, active=1)),  # 6 re-select by key -> the winner's row
            _exec_result(),  # 7 update it in place
        ]
        resp = self._save([{"meter_code": "MAIN", "opening_kwh": 10, "closing_kwh": 25}])
        assert resp.status_code == 200, resp.json()
        assert resp.json()["data"] == {"saved": 1, "ids": [77], "warnings": []}
        self._session.begin_nested.assert_called_once()  # SAVEPOINT around the INSERT
        assert self._binds(7)["power_reading_id"] == 77 and self._binds(7)["kwh"] == 15.0
        self._session.commit.assert_called_once()
        self._session.rollback.assert_not_called()

    def test_save_db_error_500_rolls_back(self):
        self._session.execute.side_effect = [_pair_ok(), RuntimeError("db exploded")]
        resp = self._save([{"meter_code": "MAIN", "kwh": 10}])
        assert resp.status_code == 500
        assert "db exploded" in resp.json()["detail"]
        self._session.rollback.assert_called_once()

    # ----- DELETE /reading_delete/{id} ------------------------------------------

    def test_delete_requires_co_id(self):
        resp = client.delete(f"{BASE}/reading_delete/5")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "co_id is required"

    def test_old_delete_route_is_gone(self):
        resp = client.delete(f"{BASE}/reading/5", params={"co_id": 1})
        assert resp.status_code == 404

    def test_delete_404(self):
        self._session.execute.side_effect = [_exec_result(fetchone=None)]
        resp = client.delete(f"{BASE}/reading_delete/5", params={"co_id": 1})
        assert resp.status_code == 404
        assert resp.json()["detail"] == READING_NOT_FOUND_MSG
        self._session.commit.assert_not_called()

    def test_delete_locked_403(self):
        self._session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(power_reading_id=5, co_id=1, branch_id=2, tran_date=LOCKED_DATE, meter_code="MAIN")),
            _access(3),
        ]
        resp = client.delete(f"{BASE}/reading_delete/5", params={"co_id": 1})
        assert resp.status_code == 403
        assert resp.json()["detail"] == LOCKED_MSG
        assert self._session.execute.call_count == 2
        # The capability is checked on the ROW's branch, not a query param.
        assert self._binds(1) == {"user_id": 1, "co_id": 1, "branch_id": 2, "menu_path": POWER_ENTRY_MENU_PATH}
        self._session.commit.assert_not_called()

    def test_delete_happy_inside_window_skips_the_capability_lookup(self):
        self._session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(power_reading_id=5, co_id=1, branch_id=2, tran_date=YESTERDAY, meter_code="MAIN")),
            _exec_result(),
        ]
        resp = client.delete(f"{BASE}/reading_delete/5", params={"co_id": 1})
        assert resp.status_code == 200, resp.json()
        assert resp.json() == {"data": {"message": "Deleted"}}
        assert self._session.execute.call_count == 2
        assert self._binds(1) == {"power_reading_id": 5, "updated_by": 1}
        self._session.commit.assert_called_once()

    def test_delete_locked_allowed_with_edit_capability(self):
        self._session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(power_reading_id=5, co_id=1, branch_id=2, tran_date=LOCKED_DATE, meter_code="MAIN")),
            _access(POWER_LOCK_OVERRIDE_ACCESS_LEVEL),
            _exec_result(),
        ]
        resp = client.delete(f"{BASE}/reading_delete/5", params={"co_id": 1})
        assert resp.status_code == 200, resp.json()


class TestPowerReportEndpoint(_EndpointBase):
    URL = f"{BASE}/power_report"

    def _params(self, **over):
        p = {"co_id": 1, "branch_id": 2, "date_from": "2026-10-15", "date_to": "2026-10-17"}
        p.update(over)
        return p

    def test_missing_branch_id_400(self):
        p = self._params()
        del p["branch_id"]
        resp = client.get(self.URL, params=p)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "branch_id is required"
        self._session.execute.assert_not_called()

    def test_missing_co_id_400(self):
        p = self._params()
        del p["co_id"]
        resp = client.get(self.URL, params=p)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "co_id is required"

    def test_missing_date_400(self):
        p = self._params()
        del p["date_to"]
        resp = client.get(self.URL, params=p)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "date_to is required"

    def test_bad_date_400(self):
        resp = client.get(self.URL, params=self._params(date_from="15/10/2026"))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid date_from format"

    def test_date_to_before_date_from_400(self):
        resp = client.get(self.URL, params=self._params(date_from="2026-10-17", date_to="2026-10-15"))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "date_to is before date_from"
        self._session.execute.assert_not_called()

    def test_94_day_range_refused_without_db(self):
        resp = client.get(self.URL, params=self._params(date_from="2026-07-16", date_to="2026-10-17"))
        assert resp.status_code == 400
        assert resp.json()["detail"] == f"Date range too large — at most {MAX_RANGE_DAYS} days"
        self._session.execute.assert_not_called()

    def test_93_day_range_accepted(self):
        self._session.execute.side_effect = [_pair_ok(), _rows([]), _rows([]), _rows([]), _rows([])]
        resp = client.get(self.URL, params=self._params(date_from="2026-07-17", date_to="2026-10-17"))
        assert resp.status_code == 200, resp.json()
        assert resp.json()["data"]["totals"]["days"] == 93

    def test_branch_not_in_company_400(self):
        self._session.execute.side_effect = [_pair_bad()]
        resp = client.get(self.URL, params=self._params())
        assert resp.status_code == 400
        assert resp.json()["detail"] == BRANCH_NOT_IN_COMPANY_MSG

    def test_happy_path(self):
        readings, yarn, cloth, meters = _report_inputs()
        self._session.execute.side_effect = [
            _pair_ok(),  # 1 pair check
            _rows(readings),  # 2 readings over the window
            _rows(yarn),  # 3 yarn per day (DPR spinning feed)
            _rows(cloth),  # 4 cloth per day
            _rows(meters),  # 5 meters
        ]
        resp = client.get(self.URL, params=self._params())
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        assert len(data["days"]) == 3
        assert data["days"][0]["kwh_per_t_cloth"] == round(18900.0 / 29.0, 2)
        assert data["days"][1]["keyed"] is False
        assert data["totals"]["days_keyed"] == 2
        assert set(data["std"]) == {"basis", "days_used", "kwh_total", "kwh_per_t_cloth", "kwh_per_t_yarn", "dg_share_pct"}
        assert data["std"]["days_used"] == 2
        assert [m["meter_code"] for m in data["meters"]] == ["MAIN", "DG1"]
        meta = data["meta"]
        assert meta["window_from"] == "2026-09-18"  # 30-day std window reaches furthest back
        assert (meta["co_id"], meta["branch_id"], meta["days_keyed"]) == (1, 2, 2)
        assert (meta["date_from"], meta["date_to"]) == ("2026-10-15", "2026-10-17")
        assert not any("No power readings" in n for n in meta["notes"])
        # Execute order / binds: every feed is fetched from window_from to date_to.
        assert self._session.execute.call_count == 5
        assert self._binds(0) == PAIR_BINDS
        assert self._binds(1) == {"co_id": 1, "branch_id": 2, "window_from": date(2026, 9, 18), "date_to": date(2026, 10, 17)}
        yarn_b = self._binds(2)
        assert yarn_b["window_from"] == date(2026, 9, 18) and yarn_b["report_date"] == date(2026, 10, 17)
        assert yarn_b["spinning_type"] == "Spinning"
        cloth_b = self._binds(3)
        assert cloth_b["date_from"] == date(2026, 9, 18) and cloth_b["report_date"] == date(2026, 10, 17)
        assert self._binds(4)["include_inactive"] == 1

    def test_empty_range_is_200_all_unkeyed(self):
        self._session.execute.side_effect = [_pair_ok(), _rows([]), _rows([]), _rows([]), _rows(METERS[:2])]
        resp = client.get(self.URL, params=self._params())
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        assert len(data["days"]) == 3 and not any(d["keyed"] for d in data["days"])
        assert data["totals"]["kwh_total"] == 0.0 and data["totals"]["kwh_per_t_cloth"] is None
        assert data["std"]["kwh_total"] is None
        assert [m["meter_code"] for m in data["meters"]] == ["MAIN", "DG1"]  # active meters still listed
        assert "No power readings keyed in this range" in data["meta"]["notes"]

    def test_db_error_500(self):
        self._session.execute.side_effect = RuntimeError("db exploded")
        resp = client.get(self.URL, params=self._params())
        assert resp.status_code == 500
        assert "db exploded" in resp.json()["detail"]
