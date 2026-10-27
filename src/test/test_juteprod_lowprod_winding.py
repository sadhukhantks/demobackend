"""Winding Low Producer report — src/juteProduction/lowprod_winding.py.

The pivots and the efficiency arithmetic live in pure builders, so they are
exercised directly on stub rows (test_winding_quality_wise_report.py's
approach). Parameter validation and the wiring go through TestClient against a
throwaway app carrying only this router — the endpoint is registered in
src/main.py by the integrator, and this suite must not depend on that having
happened yet.

The case that matters most is the ATTENDANCE match: it is the whole point of
this report (the older winding reports have no attendance link and say so), and
its fallback must be visible rather than silent.

The second is where a FULL SHIFT's hours come from: spell_mst.working_hours, per
the user ruling — the hardcoded 8 / 7.5 survive only for a spell with nothing
configured. Every builder test therefore passes an explicit spell-hours map, and
both paths (configured / fallback) are asserted.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.juteProduction import lowprod_winding_query
from src.juteProduction.lowprod_winding import (
    _full_hours,
    build_base_rows,
    build_day_summary,
    build_notes,
    build_worker_summary,
    get_current_user_with_refresh,
    get_tenant_db,
    keep_row,
    router,
    std_and_eff,
    winders_of,
)
from src.juteProduction.lowprod_winding_query import get_winding_reconciliation_range_query
from src.juteProduction.winding_query import get_winding_reconciliation_query

app = FastAPI()
app.include_router(router, prefix="/api/juteProductionReports")
client = TestClient(app)

URL = "/api/juteProductionReports/winding_low_producer"

# spell_mst as the builder tests see it: 91 = A1, 92 = B1, 93 = C (a real
# 7.5-hour shift). Spells outside this map exercise the fallback.
SPELL_HOURS = {91: 8.0, 92: 8.0, 93: 7.5}


def _recon(tran_date, shift, spell_id, eb_id, item_id, kg, emp_code=None):
    """A row as get_winding_reconciliation_query returns it."""
    return {
        "tran_date": tran_date,
        "spell_id": spell_id,
        "spell_code": f"{shift}1",
        "shift": shift,
        "eb_id": eb_id,
        "emp_code": emp_code or f"E{eb_id}",
        "worker_name": f"WINDER {eb_id}",
        "item_id": item_id,
        "item_code": f"Q{item_id}",
        "item_name": f"QUALITY {item_id}",
        "production_kg": kg,
    }


def _rows(mappings):
    out = []
    for m in mappings:
        row = MagicMock()
        row._mapping = m
        out.append(row)
    return out


def _result(mappings):
    res = MagicMock()
    res.fetchall.return_value = _rows(mappings)
    return res


def _pair_ok(found=True):
    res = MagicMock()
    res.first.return_value = (1,) if found else None
    return res


def _target_rows(mapping):
    """resolve_winding_targets_query rows — read by attribute, not by _mapping."""
    res = MagicMock()
    res.fetchall.return_value = [
        SimpleNamespace(item_id=k, value=v) for k, v in mapping.items()
    ]
    return res


def _spell_rows(mapping):
    """get_spell_hours_query rows — attribute-read, same as the target rows."""
    res = MagicMock()
    res.fetchall.return_value = [
        SimpleNamespace(spell_id=k, spell_code=f"S{k}", working_hours=v)
        for k, v in mapping.items()
    ]
    return res


def _att(tran_date, spell_id, eb_id, hours, att_rows=1):
    return {
        "tran_date": tran_date,
        "spell_id": spell_id,
        "eb_id": eb_id,
        "att_hrs": hours,
        "att_rows": att_rows,
    }


# ---------------------------------------------------------------------------
# Efficiency formula
# ---------------------------------------------------------------------------


class TestEfficiencyFormula:
    def test_full_shift_at_target_is_100_pct(self):
        assert std_and_eff(120.0, 120.0, 8.0) == (120.0, 100.0)

    def test_half_shift_target_is_pro_rated(self):
        """The whole reason this report exists: 4 hours is measured against half
        a shift's target, so 60 kg in 4 hours is 100%, not 50%."""
        assert std_and_eff(60.0, 120.0, 4.0) == (60.0, 100.0)

    def test_low_producer_below_100(self):
        std, eff = std_and_eff(90.0, 120.0, 8.0)
        assert (std, eff) == (120.0, 75.0)

    def test_no_target_gives_blank_efficiency_not_zero(self):
        """0.0 would read as 'produced nothing against a real target'."""
        assert std_and_eff(90.0, 0.0, 8.0) == (0.0, None)

    def test_zero_hours_gives_blank_efficiency(self):
        assert std_and_eff(90.0, 120.0, 0.0) == (0.0, None)

    def test_winders_is_hours_over_a_full_shift(self):
        assert winders_of(8.0, 8.0) == 1.0
        assert winders_of(4.0, 8.0) == 0.5

    def test_a_full_shift_of_any_configured_length_is_exactly_one_winder(self):
        """The divisor is the row's own spell hours, so a 7.5- or 5-hour spell
        worked in full is one winder, not 0.94 or 0.63 of one."""
        assert winders_of(7.5, 7.5) == 1.0
        assert winders_of(5.0, 5.0) == 1.0
        assert winders_of(8.0, 7.5) == pytest.approx(1.067, abs=0.001)

    def test_missing_full_hours_falls_back_instead_of_dividing_by_zero(self):
        assert winders_of(8.0, 0) == 1.0
        assert winders_of(8.0, None) == 1.0

    def test_std_prod_always_pro_rates_on_the_target_masters_eight_hour_unit(self):
        """The 8 in ``target_kg_8h / 8`` is the master's UNIT, not a shift
        length — a 7.5-hour shift gets 7.5/8 of the stated target, never the
        whole of it."""
        assert std_and_eff(112.5, 120.0, 7.5) == (112.5, 100.0)


# ---------------------------------------------------------------------------
# Full-shift hours: spell_mst first, hardcoded values only as the fallback
# ---------------------------------------------------------------------------


class TestFullShiftHours:
    def test_configured_hours_are_used_for_any_shift(self):
        assert _full_hours(91, "A", {91: 8.0}) == 8.0
        assert _full_hours(91, "A", {91: 5.0}) == 5.0

    def test_configured_hours_win_over_the_legacy_c_shift_value(self):
        """spell_mst rules: a C spell configured at 8 hours IS an 8-hour shift,
        whatever the legacy 7.5 says."""
        assert _full_hours(93, "C", {93: 8.0}) == 8.0

    def test_unconfigured_spell_falls_back_to_the_legacy_values(self):
        assert _full_hours(999, "C", {}) == 7.5
        assert _full_hours(999, "A", {}) == 8.0
        assert _full_hours(999, "X", {}) == 8.0   # off-shift -> not the C value

    def test_zero_or_null_working_hours_count_as_unconfigured(self):
        assert _full_hours(93, "C", {93: 0}) == 7.5
        assert _full_hours(91, "A", {91: None}) == 8.0

    def test_decimal_hours_off_the_driver_are_accepted(self):
        assert _full_hours(93, "C", {93: Decimal("7.5")}) == 7.5


# ---------------------------------------------------------------------------
# Base rows + the attendance match
# ---------------------------------------------------------------------------


class TestBaseRows:
    RECON = [
        _recon("2026-08-01", "A", 91, 10, 1, 100.0),
        _recon("2026-08-01", "B", 92, 11, 1, 60.0),
    ]
    TARGETS = {1: 120.0}

    def test_attendance_hours_are_used_when_matched(self):
        att = {("2026-08-01", 91, 10): 4.0}
        rows = build_base_rows(self.RECON, att, self.TARGETS, SPELL_HOURS)
        matched = next(r for r in rows if r["eb_id"] == 10)
        assert matched["att_hrs"] == 4.0
        assert matched["att_source"] == "attendance"
        assert matched["std_prod"] == 60.0
        assert matched["eff_pct"] == pytest.approx(166.67)

    def test_measured_hours_are_a_fraction_of_the_spells_own_full_shift(self):
        """2.5 hours of a 5-hour spell is half a winder — the divisor follows
        the spell, not a fixed 8."""
        row = build_base_rows(
            [_recon("2026-08-01", "A", 91, 10, 1, 50.0)],
            {("2026-08-01", 91, 10): 2.5},
            self.TARGETS,
            {91: 5.0},
        )[0]
        assert row["att_hrs"] == 2.5
        assert row["no_of_winders"] == 0.5

    def test_missing_attendance_falls_back_to_the_configured_hours_and_is_flagged(self):
        rows = build_base_rows(self.RECON, {}, self.TARGETS, SPELL_HOURS)
        assert {r["att_source"] for r in rows} == {"default8"}
        assert {r["att_hrs"] for r in rows} == {8.0}   # spells 91/92 configured at 8

    def test_defaulted_hours_follow_the_spell_not_a_fixed_eight(self):
        """Both paths in one row set: a configured 5-hour spell, an UNconfigured
        C spell (legacy 7.5), and a C spell configured at 8 (configuration wins).
        std_prod still pro-rates on the target master's 8-hour unit throughout."""
        recon = [
            _recon("2026-08-01", "A", 91, 10, 1, 100.0),
            _recon("2026-08-01", "C", 93, 11, 1, 100.0),
            _recon("2026-08-01", "C", 94, 12, 1, 100.0),
        ]
        rows = {
            r["eb_id"]: r
            for r in build_base_rows(recon, {}, self.TARGETS, {91: 5.0, 94: 8.0})
        }
        assert (rows[10]["att_hrs"], rows[10]["no_of_winders"]) == (5.0, 1.0)
        assert (rows[11]["att_hrs"], rows[11]["no_of_winders"]) == (7.5, 1.0)
        assert (rows[12]["att_hrs"], rows[12]["no_of_winders"]) == (8.0, 1.0)
        assert all(r["att_source"] == "default8" for r in rows.values())
        assert rows[10]["std_prod"] == 75.0      # 120 / 8 * 5
        assert rows[11]["std_prod"] == 112.5     # 120 / 8 * 7.5
        assert rows[12]["std_prod"] == 120.0

    def test_attendance_matches_on_spell_id_not_shift(self):
        """A hit on the worker's OTHER spell must not be borrowed."""
        rows = build_base_rows(
            self.RECON, {("2026-08-01", 999, 10): 4.0}, self.TARGETS, SPELL_HOURS
        )
        assert all(r["att_source"] == "default8" for r in rows)

    def test_datetime_dates_index_the_same_as_strings(self):
        from datetime import date

        recon = [_recon(date(2026, 8, 1), "A", 91, 10, 1, 100.0)]
        row = build_base_rows(
            recon, {("2026-08-01", 91, 10): 8.0}, self.TARGETS, SPELL_HOURS
        )[0]
        assert row["tran_date"] == "2026-08-01"
        assert row["att_source"] == "attendance"

    def test_unresolved_spell_buckets_to_g(self):
        recon = [_recon("2026-08-01", "X", 91, 10, 1, 100.0)]
        assert build_base_rows(recon, {}, {}, {})[0]["shift"] == "G"

    def test_row_shape_carries_the_contract_columns(self):
        row = build_base_rows(self.RECON, {}, self.TARGETS, SPELL_HOURS)[0]
        for key in (
            "tran_date", "shift", "spell_code", "eb_id", "worker_name", "item_id",
            "quality_name", "prod_kg", "att_hrs", "att_source", "target_kg_8h",
            "std_prod", "eff_pct", "no_of_winders",
        ):
            assert key in row


class TestRowFilters:
    ROW = {"shift": "A", "eb_no": "E10", "worker_name": "LAXMI DEBI", "eb_id": 10}

    def test_shift_filter(self):
        assert keep_row(self.ROW, "A", None)
        assert not keep_row(self.ROW, "B", None)

    def test_eb_no_matches_code_or_name_case_insensitively(self):
        assert keep_row(self.ROW, None, "e10")
        assert keep_row(self.ROW, None, "laxmi")
        assert not keep_row(self.ROW, None, "E11")

    def test_no_filters_keeps_everything(self):
        assert keep_row(self.ROW, None, None)


# ---------------------------------------------------------------------------
# Worker summary
# ---------------------------------------------------------------------------


class TestWorkerSummary:
    def _built(self):
        recon = [
            # eb 10: two spells on one day (A + B) plus a second day.
            _recon("2026-08-01", "A", 91, 10, 1, 60.0),
            _recon("2026-08-01", "B", 92, 10, 1, 120.0),
            _recon("2026-08-02", "A", 91, 10, 1, 120.0),
            # eb 11: one day, on target.
            _recon("2026-08-01", "A", 91, 11, 1, 120.0),
        ]
        return build_worker_summary(build_base_rows(recon, {}, {1: 120.0}, SPELL_HOURS))

    def test_days_attended_counts_distinct_dates_not_rows(self):
        eb10 = next(r for r in self._built() if r["eb_id"] == 10)
        assert eb10["days_attended"] == 2

    def test_per_shift_and_overall_averages(self):
        eb10 = next(r for r in self._built() if r["eb_id"] == 10)
        assert eb10["eff_a"] == 75.0     # (50 + 100) / 2
        assert eb10["eff_b"] == 100.0
        assert eb10["eff_c"] is None
        assert eb10["avg_eff"] == pytest.approx(83.33)  # (50 + 100 + 100) / 3
        assert eb10["total_prod_kg"] == 300.0

    def test_worst_producer_sorts_first(self):
        assert [r["eb_id"] for r in self._built()] == [10, 11]

    def test_worker_without_any_efficiency_sorts_last(self):
        recon = [
            _recon("2026-08-01", "A", 91, 10, 1, 10.0),   # eff 8.33
            _recon("2026-08-01", "A", 91, 12, 2, 500.0),  # quality 2 has no target
        ]
        rows = build_worker_summary(build_base_rows(recon, {}, {1: 120.0}, SPELL_HOURS))
        assert [r["eb_id"] for r in rows] == [10, 12]
        assert rows[1]["avg_eff"] is None

    def test_zero_efficiency_rows_are_excluded_from_the_average(self):
        """The 'shed average': a no-target row must not drag the mean to half."""
        recon = [
            _recon("2026-08-01", "A", 91, 10, 1, 120.0),  # 100%
            _recon("2026-08-02", "A", 91, 10, 2, 50.0),   # no target -> blank
        ]
        row = build_worker_summary(build_base_rows(recon, {}, {1: 120.0}, SPELL_HOURS))[0]
        assert row["avg_eff"] == 100.0


# ---------------------------------------------------------------------------
# Day summary
# ---------------------------------------------------------------------------


class TestDaySummary:
    def _built(self):
        recon = [
            _recon("2026-08-01", "A", 91, 10, 1, 120.0),  # 100%
            _recon("2026-08-01", "A", 91, 11, 1, 60.0),   # 50%
            _recon("2026-08-01", "C", 93, 12, 1, 120.0),  # 100%, C shift
            _recon("2026-08-02", "B", 92, 10, 1, 30.0),   # 25%
        ]
        return build_day_summary(build_base_rows(recon, {}, {1: 120.0}, SPELL_HOURS))

    def test_one_row_per_date_in_date_order(self):
        assert [r["date"] for r in self._built()] == ["2026-08-01", "2026-08-02"]

    def test_per_shift_production_and_winders(self):
        day1 = self._built()[0]
        assert day1["act_prod_a"] == 180.0
        assert day1["winders_run_a"] == 2.0
        assert day1["act_prod_c"] == 120.0
        # Spell 93 is configured at 7.5 hours, so a defaulted C row is exactly
        # one winder — and is measured against 7.5/8 of the stated target.
        assert day1["winders_run_c"] == 1.0
        assert day1["act_prod_total"] == 300.0

    def test_avg_eff_total_is_count_weighted_not_the_mean_of_shift_means(self):
        day1 = self._built()[0]
        assert day1["avg_eff_a"] == 75.0
        assert day1["avg_eff_c"] == pytest.approx(106.67)  # 120 kg vs 112.5 std
        # Mean of the shift means would be 90.84; the count-weighted figure is
        # (100 + 50 + 106.67) / 3.
        assert day1["avg_eff_total"] == pytest.approx(85.56)

    def test_kg_per_winder(self):
        day1 = self._built()[0]
        assert day1["kg_per_winder_a"] == 90.0

    def test_off_shift_rows_stay_in_the_totals(self):
        recon = [
            _recon("2026-08-01", "A", 91, 10, 1, 100.0),
            _recon("2026-08-01", "X", 99, 11, 1, 40.0),
        ]
        day = build_day_summary(build_base_rows(recon, {}, {1: 120.0}, SPELL_HOURS))[0]
        assert day["act_prod_a"] == 100.0
        assert day["act_prod_total"] == 140.0

    def test_empty_range(self):
        assert build_day_summary([]) == []


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class TestNotes:
    def test_fallback_count_is_stated(self):
        rows = build_base_rows(
            [
                _recon("2026-08-01", "A", 91, 10, 1, 100.0),
                _recon("2026-08-01", "B", 92, 11, 1, 100.0),
            ],
            {("2026-08-01", 91, 10): 8.0},
            {1: 120.0},
            SPELL_HOURS,
        )
        note = build_notes(rows, 0, 0)[0]
        assert "1 of 2 row(s) found NO matching attendance row" in note
        # The fallback is the SPELL's configured length, and the note says so.
        assert "spell's configured full-shift hours" in note

    def test_no_stale_limitation_claim(self):
        """The older winding reports say targets are NOT pro-rated by hours. This
        one does pro-rate, so that sentence must not survive the port."""
        blob = " ".join(build_notes([], 0, 0)).lower()
        assert "not pro-rated" not in blob
        assert "no winding target master or attendance link" not in blob
        # Nor may it still claim a fixed 8-hour default now that spell_mst rules.
        assert "default to 8" not in blob
        assert "fall back to 8" not in blob

    def test_untargeted_qualities_are_disclosed(self):
        assert any("no target row" in n for n in build_notes([], 2, 0))
        assert not any("no target row" in n for n in build_notes([], 0, 0))

    def test_duplicate_attendance_keys_are_disclosed(self):
        assert any("more than" in n for n in build_notes([], 0, 3))


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class TestEndpointValidation:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    BASE = {"co_id": 2, "branch_id": 29, "date_from": "2026-08-01", "date_to": "2026-08-05"}

    def _params(self, **over):
        p = dict(self.BASE)
        p.update(over)
        for k, v in list(p.items()):
            if v is None:
                p.pop(k)
        return p

    @pytest.mark.parametrize("missing", ["co_id", "branch_id"])
    def test_missing_scope_400(self, missing):
        resp = client.get(URL, params=self._params(**{missing: None}))
        assert resp.status_code == 400
        assert missing in resp.json()["detail"]

    @pytest.mark.parametrize("bad", ["co_id", "branch_id"])
    def test_non_numeric_scope_400(self, bad):
        resp = client.get(URL, params=self._params(**{bad: "abc"}))
        assert resp.status_code == 400
        assert bad in resp.json()["detail"]

    def test_missing_dates_400(self):
        resp = client.get(URL, params=self._params(date_to=None))
        assert resp.status_code == 400
        assert "date_from and date_to" in resp.json()["detail"]

    def test_malformed_date_400(self):
        resp = client.get(URL, params=self._params(date_from="01-08-2026"))
        assert resp.status_code == 400
        assert "date_from" in resp.json()["detail"]

    def test_reversed_range_400(self):
        resp = client.get(URL, params=self._params(date_from="2026-08-05", date_to="2026-08-01"))
        assert resp.status_code == 400
        assert "before" in resp.json()["detail"]

    def test_range_cap_400(self):
        resp = client.get(URL, params=self._params(date_from="2026-01-01", date_to="2026-03-01"))
        assert resp.status_code == 400
        assert "too large" in resp.json()["detail"]

    def test_range_at_the_cap_is_allowed(self):
        # No producers in the 31 days (one ranged reconciliation call), so
        # attendance is skipped; the spell-hours read still runs, after the targets.
        self._session.execute.side_effect = [
            _pair_ok(), _result([]), _target_rows({}), _spell_rows(SPELL_HOURS),
        ]
        resp = client.get(URL, params=self._params(date_from="2026-08-01", date_to="2026-08-31"))
        assert resp.status_code == 200

    def test_bad_shift_400(self):
        resp = client.get(URL, params=self._params(shift="Z"))
        assert resp.status_code == 400
        assert "shift" in resp.json()["detail"]

    def test_bad_item_id_400(self):
        resp = client.get(URL, params=self._params(item_id="abc"))
        assert resp.status_code == 400
        assert "item_id" in resp.json()["detail"]

    def test_branch_not_in_company_400(self):
        self._session.execute.return_value = _pair_ok(found=False)
        resp = client.get(URL, params=self._params())
        assert resp.status_code == 400
        assert "does not belong" in resp.json()["detail"]

    def test_db_error_500(self):
        self._session.execute.side_effect = RuntimeError("db exploded")
        resp = client.get(URL, params=self._params())
        assert resp.status_code == 500
        assert "db exploded" in resp.json()["detail"]


class TestEndpointHappyPath:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    PARAMS = {"co_id": 2, "branch_id": 29, "date_from": "2026-08-01", "date_to": "2026-08-02"}

    # working_hours arrives off the driver as Decimal — the endpoint must not
    # care, so the default map ships one.
    SPELLS_FROM_DB = {91: 8.0, 92: Decimal("8.00"), 93: Decimal("7.5")}

    def _wire(self, day1, day2, attendance, targets, spell_hours=None):
        """The endpoint's query order: pair check, ONE reconciliation for the whole
        range (day 1's rows then day 2's), attendance (SKIPPED when nobody
        produced), targets, spell hours."""
        results = [_pair_ok(), _result(day1 + day2)]
        if day1 or day2:
            results.append(_result(attendance))
        results.append(_target_rows(targets))
        results.append(
            _spell_rows(self.SPELLS_FROM_DB if spell_hours is None else spell_hours)
        )
        self._session.execute.side_effect = results

    def test_full_response(self):
        self._wire(
            [
                _recon("2026-08-01", "A", 91, 10, 1, 60.0),
                _recon("2026-08-01", "A", 91, 11, 1, 120.0),
            ],
            [_recon("2026-08-02", "B", 92, 10, 1, 120.0)],
            [_att("2026-08-01", 91, 10, 4.0)],
            {1: 120.0},
        )
        resp = client.get(URL, params=self.PARAMS)
        assert resp.status_code == 200
        data = resp.json()["data"]

        assert len(data["base_rows"]) == 3
        matched = next(r for r in data["base_rows"] if r["att_source"] == "attendance")
        assert matched["att_hrs"] == 4.0
        assert matched["eff_pct"] == 100.0     # 60 kg on half a shift's target
        assert matched["no_of_winders"] == 0.5

        # The other two rows fell back to 8 hours.
        assert sum(1 for r in data["base_rows"] if r["att_source"] == "default8") == 2

        assert [r["date"] for r in data["day_summary"]] == ["2026-08-01", "2026-08-02"]
        assert data["worker_summary"][0]["eb_id"] == 10  # worst average first

        meta = data["meta"]
        assert (meta["co_id"], meta["branch_id"]) == (2, 29)
        assert meta["date_from"] == "2026-08-01"
        assert meta["date_to"] == "2026-08-02"
        assert meta["filtered_avg_eff"] == 100.0
        assert meta["overall_avg_eff"] == 100.0
        assert "default8" in meta["notes"][0]

    def test_untargeted_rows_are_excluded_from_both_averages(self):
        """A blank efficiency is not a zero one: it enters NEITHER average."""
        self._wire(
            [
                _recon("2026-08-01", "A", 91, 10, 1, 120.0),  # 100%
                _recon("2026-08-01", "A", 91, 11, 2, 50.0),   # no target -> blank
            ],
            [],
            [],
            {1: 120.0},
        )
        meta = client.get(URL, params=self.PARAMS).json()["data"]["meta"]
        assert meta["filtered_avg_eff"] == 100.0
        assert meta["overall_avg_eff"] == 100.0
        assert any("no target row" in n for n in meta["notes"])

    def test_overall_average_is_pre_filter_while_filtered_is_post_filter(self):
        """The two averages differ exactly when a worker FILTER drops rows —
        both drop zeros, but only ``filtered_avg_eff`` sees the filter."""
        self._wire(
            [
                _recon("2026-08-01", "A", 91, 10, 1, 120.0),  # 100%
                _recon("2026-08-01", "B", 92, 11, 1, 60.0),   # 50%
            ],
            [],
            [],
            {1: 120.0},
        )
        meta = client.get(URL, params={**self.PARAMS, "shift": "A"}).json()["data"]["meta"]
        assert meta["filtered_avg_eff"] == 100.0   # the A row alone
        assert meta["overall_avg_eff"] == 75.0     # both rows, before the filter

    def test_spell_hours_reach_the_defaulted_rows(self):
        """End to end: a C spell configured at 7.5 defaults to 7.5 hours and one
        winder; a spell absent from spell_mst falls back to 8."""
        self._wire(
            [
                _recon("2026-08-01", "C", 93, 10, 1, 120.0),
                _recon("2026-08-01", "A", 95, 11, 1, 120.0),  # unconfigured spell
            ],
            [],
            [],
            {1: 120.0},
            spell_hours={93: Decimal("7.5")},
        )
        rows = {
            r["eb_id"]: r
            for r in client.get(URL, params=self.PARAMS).json()["data"]["base_rows"]
        }
        assert (rows[10]["att_hrs"], rows[10]["no_of_winders"]) == (7.5, 1.0)
        assert rows[10]["std_prod"] == 112.5
        assert (rows[11]["att_hrs"], rows[11]["no_of_winders"]) == (8.0, 1.0)
        assert rows[11]["std_prod"] == 120.0

    def test_shift_filter_is_applied_to_every_block(self):
        self._wire(
            [
                _recon("2026-08-01", "A", 91, 10, 1, 120.0),
                _recon("2026-08-01", "B", 92, 11, 1, 60.0),
            ],
            [],
            [],
            {1: 120.0},
        )
        data = client.get(URL, params={**self.PARAMS, "shift": "B"}).json()["data"]
        assert [r["eb_id"] for r in data["base_rows"]] == [11]
        assert [r["eb_id"] for r in data["worker_summary"]] == [11]
        assert data["day_summary"][0]["act_prod_a"] == 0.0
        assert data["day_summary"][0]["act_prod_total"] == 60.0

    def test_eb_no_filter(self):
        self._wire(
            [
                _recon("2026-08-01", "A", 91, 10, 1, 120.0, emp_code="02413"),
                _recon("2026-08-01", "A", 91, 11, 1, 60.0, emp_code="02999"),
            ],
            [],
            [],
            {1: 120.0},
        )
        data = client.get(URL, params={**self.PARAMS, "eb_no": "02413"}).json()["data"]
        assert [r["eb_no"] for r in data["base_rows"]] == ["02413"]

    def test_binds_scope_dates_and_item_filter(self):
        self._wire([], [], [], {})
        resp = client.get(URL, params={**self.PARAMS, "item_id": 77})
        assert resp.status_code == 200
        calls = self._session.execute.call_args_list
        binds = [c.args[1] if len(c.args) > 1 else None for c in calls]
        assert binds[0] == {"branch_id": 29, "co_id": 2}
        # ONE reconciliation call for the whole range, branch- and item-scoped.
        assert (binds[1]["date_from"].isoformat(), binds[1]["date_to"].isoformat()) == (
            "2026-08-01", "2026-08-02",
        )
        assert binds[1]["branch_id"] == 29 and binds[1]["item_id"] == 77
        assert binds[1]["spell_id"] is None and "tran_date" not in binds[1]
        # Nobody produced, so attendance is skipped: targets, then spell hours.
        assert len(calls) == 4
        # Targets resolve as of date_to, for the company that owns the branch.
        assert binds[2]["co_id"] == 2
        assert binds[2]["on_date"].isoformat() == self.PARAMS["date_to"]
        # spell_mst is read once, unparameterised, AFTER the targets.
        assert binds[3] is None

    def test_attendance_is_not_queried_when_nobody_produced(self):
        """An empty expanding IN list is not valid SQL — the attendance query
        must be skipped. The spell-hours read still runs."""
        self._wire([], [], [], {})
        resp = client.get(URL, params=self.PARAMS)
        assert resp.status_code == 200
        assert resp.json()["data"]["base_rows"] == []
        assert self._session.execute.call_count == 4
        assert not any(
            "eb_ids" in (c.args[1] or {})
            for c in self._session.execute.call_args_list
            if len(c.args) > 1
        )

    def test_attendance_query_is_restricted_to_the_producing_winders(self):
        self._wire(
            [_recon("2026-08-01", "A", 91, 10, 1, 120.0)],
            [_recon("2026-08-02", "A", 91, 11, 1, 120.0)],
            [],
            {1: 120.0},
        )
        client.get(URL, params=self.PARAMS)
        att_binds = self._session.execute.call_args_list[2].args[1]
        assert att_binds["eb_ids"] == [10, 11]
        assert att_binds["branch_id"] == 29


class TestReconciliationRangeQuery:
    """The ranged reconciliation is the single-date SQL with only the date
    predicate and the leading sort key changed (no forked copy)."""

    def test_only_the_date_predicate_and_order_change(self):
        one_day = get_winding_reconciliation_query().text
        ranged = get_winding_reconciliation_range_query().text
        assert ":tran_date" not in ranged
        assert ranged.count("AND tran_date BETWEEN :date_from AND :date_to") == 3
        assert "ORDER BY doff.tran_date, sp.spell_code, emp_code" in ranged
        assert ranged == one_day.replace(
            "AND tran_date = :tran_date", "AND tran_date BETWEEN :date_from AND :date_to"
        ).replace("ORDER BY sp.spell_code, emp_code",
                  "ORDER BY doff.tran_date, sp.spell_code, emp_code")

    def test_a_changed_single_date_query_fails_loudly(self, monkeypatch):
        monkeypatch.setattr(
            lowprod_winding_query, "get_winding_reconciliation_query",
            lambda: text("SELECT 1 FROM jute_prod_winding_doff WHERE tran_date = :tran_date"),
        )
        with pytest.raises(RuntimeError, match="changed shape"):
            get_winding_reconciliation_range_query()
