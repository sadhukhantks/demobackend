"""Tests for the Weaving Low Producer report.

src/juteProduction/lowprod_weaving.py (to be mounted at /api/juteProductionReports
by the integrator — the endpoint tests mount the router on their own app so they
do not depend on main.py registration).

Portal persona: DB + auth mocked via app.dependency_overrides keyed on the exact
symbols the router module imports. The section builders are pure functions over
already-shaped base rows and are unit-tested directly.

Covered:
  * param validation — co_id / branch_id / dates / shift / numeric filters
  * branch-not-in-company -> 400, DB error -> 500
  * loom_group banding (incl. the no-digits / loom-0 guards) and shift bucketing
  * build_base_rows — day-slice column mapping + worker name join
  * apply_row_filters — quality / shift / eb / loom-substring
  * build_worker_summary — zero-eff exclusion, days attended, avg looms run,
    worst-first ordering, blank (not 0) average for a worker who never ran
  * filter_worker_summary — days / eff thresholds
  * build_day_summary — shift pivot, count-weighted total, looms run = distinct
    PRODUCING looms, kg/loom
  * build_loom_group_summary — band averages + max/min
  * build_meta — filtered vs whole-range (shed) average + notes
  * happy path — full endpoint over a 2-day range
"""

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.juteProduction.lowprod_weaving import (
    apply_row_filters,
    build_base_rows,
    build_day_summary,
    build_loom_group_summary,
    build_meta,
    build_worker_summary,
    filter_worker_summary,
    get_current_user_with_refresh,
    get_tenant_db,
    loom_group,
    shift_of,
    weaving_lowprod_router,
)

URL = "/api/juteProductionReports/weaving_low_producer"

app = FastAPI()
app.include_router(weaving_lowprod_router, prefix="/api/juteProductionReports")
client = TestClient(app)


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


def _base(**kw):
    """A shaped base row with sane defaults; override what the test cares about."""
    row = {
        "tran_date": "2026-08-01",
        "shift": "A",
        "spell_code": "A1",
        "machine_id": 1,
        "loom": "Loom 42001",
        "mech_code": "L42001",
        "eb_id": 100,
        "eb_no": "E100",
        "worker_name": "Asha Devi",
        "quality_id": 7,
        "quality_name": "HESSIAN",
        "loom_group": "1999to2004",
        "act_prod_kg": 50.0,
        "prod_100_kg": 100.0,
        "eff_pct": 50.0,
        "hours": 8.0,
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestLoomGroup:
    def test_bands_of_six(self):
        assert loom_group("Loom 1") == "1to6"
        assert loom_group("Loom 6") == "1to6"
        assert loom_group("Loom 7") == "7to12"
        assert loom_group("42007") == "42007to42012"  # whole trailing digit run

    def test_no_digits_or_zero_is_unknown(self):
        assert loom_group("Loom A") == "Unknown"
        assert loom_group(None) == "Unknown"
        assert loom_group("Loom 0") == "Unknown"  # never '-5to0'


class TestShiftOf:
    def test_first_char_of_spell_code(self):
        assert shift_of("A1") == "A"
        assert shift_of("A2") == "A"
        assert shift_of("b2") == "B"
        assert shift_of("C") == "C"

    def test_unknown_spell_buckets_to_g(self):
        assert shift_of("G1") == "G"
        assert shift_of(None) == "G"
        assert shift_of("") == "G"


class TestBuildBaseRows:
    def test_maps_slice_columns_and_worker(self):
        slice_rows = [
            {
                "tran_date": "2026-08-01", "spell_code": "A1", "machine_id": 3,
                "machine_name": "Loom 8", "mech_code": "L8", "eb_id": 100,
                "weaving_quality_id": 7, "weaving_quality_name": "HESSIAN",
                # Realistic slice: a sacking quality whose std oz/yd equals its
                # actual oz/yd, so std_prod_kg == production_kg (TKT-000177).
                "production_yds": 266.5, "std_prod_yds": 387.73,
                "ozs_yds": 9.44, "std_ozs_yds": 9.44,
                "production_kg": 71.322, "std_prod_kg": 71.322,
                "efficiency": 68.73, "working_hours": 8.0,
            }
        ]
        out = build_base_rows(slice_rows, {100: {"eb_no": "E100", "worker_name": "Asha"}})
        assert len(out) == 1
        r = out[0]
        assert r["shift"] == "A"
        assert r["loom"] == "Loom 8"
        assert r["loom_group"] == "7to12"
        assert r["worker_name"] == "Asha"
        assert r["eb_no"] == "E100"
        assert r["quality_id"] == 7
        assert r["act_prod_kg"] == 71.32
        assert r["eff_pct"] == 68.73
        assert r["hours"] == 8.0

    def test_prod_100_is_capacity_not_actual_reweighed(self):
        """TKT-000177: "100% Prod" must be std_prod_yds at the quality's oz/yd —
        the 100%-efficiency capacity — not std_prod_kg (actual yards at std
        oz/yd), which equals the actual kg whenever std_ozs_yds == ozs_yds."""
        slice_rows = [
            {
                "tran_date": "2026-09-15", "spell_code": "C", "machine_id": 7,
                "machine_name": "S4 LOOM - 7", "mech_code": "S4-7", "eb_id": 100,
                "weaving_quality_id": 9, "weaving_quality_name": "SACKING 22.5",
                "production_yds": 266.5, "std_prod_yds": 387.73,
                "ozs_yds": 9.44, "std_ozs_yds": 9.44,
                "production_kg": 71.322, "std_prod_kg": 71.322,
                "efficiency": 68.73, "working_hours": 8.0,
            }
        ]
        r = build_base_rows(slice_rows, {})[0]
        assert r["prod_100_kg"] == 103.77  # 387.73 * 9.44 * 28.35 / 1000
        assert r["prod_100_kg"] != r["act_prod_kg"]
        # Actual / 100% reproduces the slice's own (yards-based) efficiency.
        assert round(r["act_prod_kg"] * 100 / r["prod_100_kg"], 1) == 68.7

    def test_prod_100_is_zero_without_capacity(self):
        slice_rows = [
            {"tran_date": "2026-09-15", "spell_code": "A", "machine_id": 1,
             "machine_name": "Loom 1", "mech_code": "L1", "eb_id": None,
             "production_kg": 10.0, "std_prod_kg": 10.0, "ozs_yds": 9.44,
             "efficiency": 0.0, "working_hours": 8.0},
        ]
        assert build_base_rows(slice_rows, {})[0]["prod_100_kg"] == 0.0

    def test_missing_machine_name_falls_back_to_mech_code(self):
        out = build_base_rows(
            [{"tran_date": "2026-08-01", "spell_code": "C", "machine_id": 3,
              "machine_name": None, "mech_code": "L11", "eb_id": None}],
            {},
        )
        assert out[0]["loom"] == "L11"
        assert out[0]["worker_name"] is None
        assert out[0]["act_prod_kg"] == 0.0  # NULL production is 0, not a crash


class TestApplyRowFilters:
    def _sample(self):
        return [
            _base(machine_id=1, loom="Loom 1", mech_code="L1", quality_id=7, shift="A",
                  eb_id=100, eb_no="E100"),
            _base(machine_id=2, loom="Loom 2", mech_code="L2", quality_id=8, shift="B",
                  spell_code="B1", eb_id=200, eb_no="E200"),
        ]

    def test_quality_and_shift(self):
        rows = self._sample()
        assert [r["machine_id"] for r in apply_row_filters(rows, quality_id=8)] == [2]
        assert [r["machine_id"] for r in apply_row_filters(rows, shift="A")] == [1]

    def test_eb_matches_emp_code_or_raw_id(self):
        rows = self._sample()
        assert len(apply_row_filters(rows, eb_no="e200")) == 1
        assert len(apply_row_filters(rows, eb_no="100")) == 1
        assert apply_row_filters(rows, eb_no="nobody") == []

    def test_loom_substring_matches_name_or_mech_code(self):
        rows = self._sample()
        assert [r["machine_id"] for r in apply_row_filters(rows, loom="loom 2")] == [2]
        assert [r["machine_id"] for r in apply_row_filters(rows, loom="l1")] == [1]


class TestBuildWorkerSummary:
    def test_zero_eff_excluded_from_averages_but_day_still_counts(self):
        rows = [
            _base(eb_id=1, tran_date="2026-08-01", eff_pct=40.0),
            _base(eb_id=1, tran_date="2026-08-01", machine_id=2, eff_pct=0.0),
            _base(eb_id=1, tran_date="2026-08-02", machine_id=3, eff_pct=60.0),
        ]
        w = build_worker_summary(rows)[0]
        assert w["days_attended"] == 2
        assert w["avg_looms_run"] == 1.5  # 3 loom-shifts over 2 days
        assert w["avg_eff"] == 50.0  # the 0.0 row is ignored, not averaged in
        assert w["eff_a"] == 50.0

    def test_shift_columns_split(self):
        rows = [
            _base(eb_id=1, shift="A", eff_pct=40.0),
            _base(eb_id=1, shift="B", spell_code="B1", eff_pct=80.0),
        ]
        w = build_worker_summary(rows)[0]
        assert (w["eff_a"], w["eff_b"], w["eff_c"]) == (40.0, 80.0, None)
        assert w["avg_eff"] == 60.0

    def test_worker_with_no_measurable_eff_reads_blank_and_sorts_last(self):
        rows = [
            _base(eb_id=1, eff_pct=0.0),
            _base(eb_id=2, eff_pct=30.0),
            _base(eb_id=3, eff_pct=90.0),
        ]
        out = build_worker_summary(rows)
        assert [w["eb_id"] for w in out] == [2, 3, 1]  # worst first, blanks last
        assert out[-1]["avg_eff"] is None  # never 0.0

    def test_rows_without_eb_are_dropped(self):
        assert build_worker_summary([_base(eb_id=None)]) == []


class TestFilterWorkerSummary:
    def _workers(self):
        return [
            {"eb_id": 1, "days_attended": 2, "avg_eff": 40.0},
            {"eb_id": 2, "days_attended": 20, "avg_eff": 80.0},
            {"eb_id": 3, "days_attended": 10, "avg_eff": None},
        ]

    def test_day_thresholds(self):
        out = filter_worker_summary(self._workers(), min_days=5, max_days=15)
        assert [w["eb_id"] for w in out] == [3]

    def test_eff_thresholds_drop_blank_workers(self):
        out = filter_worker_summary(self._workers(), max_eff=50.0)
        assert [w["eb_id"] for w in out] == [1]

    def test_no_filters_keeps_everything(self):
        assert len(filter_worker_summary(self._workers())) == 3


class TestBuildDaySummary:
    def test_pivot_totals_and_count_weighted_eff(self):
        rows = [
            _base(tran_date="2026-08-01", shift="A", machine_id=1, act_prod_kg=50.0, eff_pct=50.0),
            _base(tran_date="2026-08-01", shift="A", machine_id=2, act_prod_kg=30.0, eff_pct=30.0),
            _base(tran_date="2026-08-01", shift="B", spell_code="B1", machine_id=1,
                  act_prod_kg=20.0, eff_pct=80.0),
        ]
        d = build_day_summary(rows)[0]
        assert d["act_prod_a"] == 80.0
        assert d["act_prod_b"] == 20.0
        assert d["act_prod_total"] == 100.0
        assert d["avg_eff_a"] == 40.0
        assert d["avg_eff_b"] == 80.0
        # count-weighted across shifts: (40*2 + 80*1)/3
        assert d["avg_eff_total"] == 53.33
        assert (d["looms_run_a"], d["looms_run_b"]) == (2, 1)
        assert d["looms_run_total"] == 3  # loom-shifts, so loom 1 counts twice
        assert d["kg_per_loom_a"] == 40.0
        assert d["kg_per_loom_total"] == 33.33

    def test_loom_entered_but_not_run_is_not_counted_as_run(self):
        rows = [
            _base(tran_date="2026-08-01", machine_id=1, act_prod_kg=60.0, eff_pct=60.0),
            _base(tran_date="2026-08-01", machine_id=2, act_prod_kg=0.0, eff_pct=0.0),
        ]
        d = build_day_summary(rows)[0]
        assert d["looms_run_a"] == 1
        assert d["kg_per_loom_a"] == 60.0  # not 30.0
        assert d["avg_eff_a"] == 60.0

    def test_dates_sorted(self):
        rows = [_base(tran_date="2026-08-03"), _base(tran_date="2026-08-01")]
        assert [d["date"] for d in build_day_summary(rows)] == ["2026-08-01", "2026-08-03"]


class TestBuildLoomGroupSummary:
    def test_band_averages_and_extremes(self):
        rows = [
            _base(loom_group="1to6", shift="A", eff_pct=40.0),
            _base(loom_group="1to6", shift="A", eff_pct=60.0),
            _base(loom_group="1to6", shift="C", spell_code="C", eff_pct=90.0),
            _base(loom_group="1to6", eff_pct=0.0),  # excluded everywhere
            _base(loom_group="7to12", shift="B", spell_code="B1", eff_pct=70.0),
        ]
        out = build_loom_group_summary(rows)
        assert [g["loom_group"] for g in out] == ["1to6", "7to12"]  # numeric order
        g = out[0]
        assert g["avg_eff_a"] == 50.0
        assert g["avg_eff_b"] is None
        assert g["avg_eff_c"] == 90.0
        assert g["avg_eff"] == round((40 + 60 + 90) / 3, 2)
        assert (g["max_eff"], g["min_eff"]) == (90.0, 40.0)


class TestBuildMeta:
    def test_filtered_vs_shed_average_and_notes(self):
        from datetime import date

        all_rows = [_base(eff_pct=20.0), _base(eff_pct=80.0), _base(eff_pct=0.0)]
        rows = [all_rows[0]]
        meta = build_meta(2, 5, date(2026, 8, 1), date(2026, 8, 5), all_rows, rows)
        assert meta["filtered_avg_eff"] == 20.0
        assert meta["overall_avg_eff"] == 50.0  # 0.0 row excluded from the shed avg
        assert meta["date_from"] == "2026-08-01"
        assert meta["branch_id"] == 5

    def test_notes_flag_missing_workers_and_idle_looms(self):
        from datetime import date

        rows = [_base(eb_id=None, act_prod_kg=0.0, eff_pct=0.0, machine_id=9)]
        notes = " ".join(
            build_meta(2, 5, date(2026, 8, 1), date(2026, 8, 1), rows, rows)["notes"]
        )
        assert "no worker" in notes
        assert "produced nothing" in notes


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class TestWeavingLowProducerEndpoint:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def _params(self, **kw):
        p = {"co_id": 2, "branch_id": 5, "date_from": "2026-08-01", "date_to": "2026-08-02"}
        p.update(kw)
        return p

    def _pair_ok(self):
        res = MagicMock()
        res.first.return_value = (1,)
        return res

    def test_missing_co_id_400(self):
        p = self._params()
        p.pop("co_id")
        assert client.get(URL, params=p).status_code == 400

    def test_missing_branch_id_400(self):
        p = self._params()
        p.pop("branch_id")
        resp = client.get(URL, params=p)
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_missing_dates_400(self):
        p = self._params()
        p.pop("date_to")
        resp = client.get(URL, params=p)
        assert resp.status_code == 400
        assert "date_to" in resp.json()["detail"]

    def test_reversed_range_400(self):
        resp = client.get(URL, params=self._params(date_from="2026-08-10", date_to="2026-08-01"))
        assert resp.status_code == 400

    def test_range_over_cap_400(self):
        resp = client.get(URL, params=self._params(date_from="2026-06-01", date_to="2026-08-01"))
        assert resp.status_code == 400
        assert "31 days" in resp.json()["detail"]

    def test_bad_shift_400(self):
        resp = client.get(URL, params=self._params(shift="D"))
        assert resp.status_code == 400
        assert "shift" in resp.json()["detail"]

    def test_bad_numeric_filter_400(self):
        resp = client.get(URL, params=self._params(min_eff="abc"))
        assert resp.status_code == 400
        assert "min_eff" in resp.json()["detail"]

    def test_branch_not_in_company_400(self):
        res = MagicMock()
        res.first.return_value = None
        self._session.execute.return_value = res
        resp = client.get(URL, params=self._params())
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_db_error_500(self):
        self._session.execute.side_effect = RuntimeError("db exploded")
        resp = client.get(URL, params=self._params())
        assert resp.status_code == 500
        assert "db exploded" in resp.json()["detail"]

    def test_happy_path(self):
        self._session.execute.side_effect = [
            self._pair_ok(),
            _result([{"eb_id": 100, "eb_no": "E100", "worker_name": "Asha Devi"}]),
        ]
        day1 = [
            {"tran_date": "2026-08-01", "spell_code": "A1", "machine_id": 1,
             "machine_name": "Loom 1", "mech_code": "L1", "eb_id": 100,
             "weaving_quality_id": 7, "weaving_quality_name": "HESSIAN",
             "production_kg": 60.0, "std_prod_kg": 100.0, "std_prod_yds": 373.68, "ozs_yds": 9.44, "efficiency": 60.0,
             "working_hours": 8.0},
        ]
        day2 = [
            {"tran_date": "2026-08-02", "spell_code": "C", "machine_id": 1,
             "machine_name": "Loom 1", "mech_code": "L1", "eb_id": 100,
             "weaving_quality_id": 7, "weaving_quality_name": "HESSIAN",
             "production_kg": 40.0, "std_prod_kg": 100.0, "std_prod_yds": 373.68, "ozs_yds": 9.44, "efficiency": 40.0,
             "working_hours": 7.5},
        ]
        with patch(
            "src.juteProduction.lowprod_weaving.fetch_weaving_day_rows",
            side_effect=[day1, day2],
        ) as fetch:
            resp = client.get(URL, params=self._params())

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert fetch.call_count == 2  # one day-slice call per date in the range
        assert len(data["base_rows"]) == 2
        assert data["base_rows"][0]["worker_name"] == "Asha Devi"
        assert data["worker_summary"][0]["avg_eff"] == 50.0
        assert data["worker_summary"][0]["days_attended"] == 2
        assert [d["date"] for d in data["day_summary"]] == ["2026-08-01", "2026-08-02"]
        assert data["day_summary"][1]["act_prod_c"] == 40.0
        assert data["loom_group_summary"][0]["loom_group"] == "1to6"
        assert data["meta"]["filtered_avg_eff"] == 50.0
        assert data["meta"]["overall_avg_eff"] == 50.0

    def test_filters_narrow_base_rows_but_meta_keeps_the_shed_average(self):
        self._session.execute.side_effect = [
            self._pair_ok(),
            _result([{"eb_id": 100, "eb_no": "E100", "worker_name": "Asha Devi"}]),
        ]
        rows = [
            {"tran_date": "2026-08-01", "spell_code": "A1", "machine_id": 1,
             "machine_name": "Loom 1", "mech_code": "L1", "eb_id": 100,
             "weaving_quality_id": 7, "weaving_quality_name": "HESSIAN",
             "production_kg": 20.0, "std_prod_kg": 100.0, "std_prod_yds": 373.68, "ozs_yds": 9.44, "efficiency": 20.0,
             "working_hours": 8.0},
            {"tran_date": "2026-08-01", "spell_code": "B1", "machine_id": 2,
             "machine_name": "Loom 2", "mech_code": "L2", "eb_id": 100,
             "weaving_quality_id": 8, "weaving_quality_name": "SACKING",
             "production_kg": 80.0, "std_prod_kg": 100.0, "std_prod_yds": 373.68, "ozs_yds": 9.44, "efficiency": 80.0,
             "working_hours": 8.0},
        ]
        with patch(
            "src.juteProduction.lowprod_weaving.fetch_weaving_day_rows",
            side_effect=[rows, []],
        ):
            resp = client.get(URL, params=self._params(shift="A"))

        data = resp.json()["data"]
        assert len(data["base_rows"]) == 1
        assert data["meta"]["filtered_avg_eff"] == 20.0
        assert data["meta"]["overall_avg_eff"] == 50.0
