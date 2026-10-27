"""Doff Analysis report (GET /api/juteProductionReports/doff_analysis).

The arithmetic lives in pure builders (build_details / build_frames), so it is
exercised directly on stub rows — the approach test_winding_quality_wise_report.py
takes. Only parameter validation and wiring go through TestClient.

The router is mounted on a throwaway FastAPI app here rather than imported from
src.main: registration in main.py belongs to the integrator, and these tests must
pass before (and after) that happens.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.juteProduction.doff_analysis import (
    BAND_ORDER,
    build_details,
    build_frames,
    build_units,
    doff_analysis_router,
    get_current_user_with_refresh,
    get_tenant_db,
    target_notes,
    unit_target,
)

URL = "/api/juteProductionReports/doff_analysis"

app = FastAPI()
app.include_router(doff_analysis_router, prefix="/api/juteProductionReports")
client = TestClient(app)


def _raw(doff_id, spell_code, machine_id, frame, item_id, net, eb_id=None,
         emp_code=None, quality=None):
    return {
        "doff_id": doff_id,
        "machine_id": machine_id,
        "frame": frame,
        "spell_id": 1,
        "spell_code": spell_code,
        "item_id": item_id,
        "quality_name": quality if quality is not None else (f"Q{item_id} - YARN {item_id}" if item_id else None),
        "eb_id": eb_id,
        "emp_code": emp_code,
        "net_wt": net,
    }


def _agg(d, shift, frame, item_id, net, num=1, mx=None, mn=None, eb_id=None,
         emp_code=None, quality=None, machine_id=1):
    return {
        "doff_date": d,
        "machine_id": machine_id,
        "frame": frame,
        "item_id": item_id,
        "quality_name": quality if quality is not None else f"Q{item_id} - YARN {item_id}",
        "shift": shift,
        "eb_id": eb_id,
        "emp_code": emp_code,
        "net_wt": net,
        "num_doffs": num,
        "max_wt": net if mx is None else mx,
        "min_wt": net if mn is None else mn,
    }


def _rows_result(mappings):
    res = MagicMock()
    res.fetchall.return_value = [SimpleNamespace(_mapping=m) for m in mappings]
    return res


def _scope_ok():
    res = MagicMock()
    res.first.return_value = (1,)
    return res


def _scope_bad():
    res = MagicMock()
    res.first.return_value = None
    return res


# ---------------------------------------------------------------------------
# build_details
# ---------------------------------------------------------------------------


class TestBuildDetails:
    def _built(self):
        rows = [
            # Frame 1, spell A1, quality 5 — three doffs, then a yarn change.
            _raw(1, "A1", 101, "1", 5, 30.0, eb_id=7, emp_code="E7"),
            _raw(2, "A1", 101, "1", 5, 26.0, eb_id=7, emp_code="E7"),
            _raw(3, "A1", 101, "1", 5, 28.0, eb_id=7, emp_code="E7"),
            _raw(9, "A1", 101, "1", 6, 40.0, eb_id=7, emp_code="E7"),
            # Frame 2, spell A1 — one doff by a contract hand (no eb).
            _raw(4, "A1", 102, "2", 5, 20.0),
            # Frame 10, spell B1 — unmapped yarn, worker with no emp_code.
            _raw(5, "B1", 110, "10", None, 50.0, eb_id=8),
        ]
        return build_details(rows)

    def test_summary_groups_by_spell_frame_quality(self):
        s = self._built()["doff_summary"]
        assert len(s) == 4
        first = s[0]
        assert (first["spell_code"], first["frame"], first["item_id"]) == ("A1", "1", 5)
        assert (first["net_wt"], first["num_doffs"]) == (84.0, 3)
        assert (first["avg_wt"], first["max_wt"], first["min_wt"]) == (28.0, 30.0, 26.0)

    def test_last_doff_is_max_pk_per_spell_and_frame(self):
        # Frame 1 ran two qualities in A1; both rows carry doff 9's weight.
        rows = {(r["frame"], r["item_id"]): r for r in self._built()["doff_summary"]}
        assert rows[("1", 5)]["last_doff_wt"] == 40.0
        assert rows[("1", 6)]["last_doff_wt"] == 40.0
        assert rows[("2", 5)]["last_doff_wt"] == 20.0

    def test_frames_sort_numerically_not_lexically(self):
        s = self._built()["doff_summary"]
        assert [r["frame"] for r in s] == ["1", "1", "2", "10"]

    def test_spell_totals(self):
        # No target inputs supplied -> the target side of every total is blank.
        assert self._built()["spell_totals"] == [
            {"spell_code": "A1", "net_wt": 144.0, "target_wt": None, "diff_wt": None,
             "target_rows": 0, "rows": 3},
            {"spell_code": "B1", "net_wt": 50.0, "target_wt": None, "diff_wt": None,
             "target_rows": 0, "rows": 1},
        ]

    def test_summary_rows_without_target_inputs_are_blank_not_zero(self):
        for row in self._built()["doff_summary"]:
            assert row["target_wt"] is None
            assert row["diff_wt"] is None
            assert "achv_pct" not in row

    def test_weight_bands_are_complete_and_ordered(self):
        bands = self._built()["weight_bands"]
        assert [b["band"] for b in bands] == BAND_ORDER
        by_band = {b["band"]: b for b in bands}
        # avg 28.0 -> '28-30', avg 40 -> '38-40', avg 20 -> '<26', avg 50 -> '>45'
        assert by_band["28-30"]["frame_count"] == 1
        assert by_band["28-30"]["spell_breakup"] == {"A1": 1}
        assert by_band["<26"]["frame_count"] == 1
        assert by_band[">45"]["spell_breakup"] == {"B1": 1}
        assert by_band["30-32"]["frame_count"] == 0

    @pytest.mark.parametrize(
        "avg,band",
        [(0, "<26"), (25.99, "<26"), (26, "26-28"), (27.9, "26-28"), (44, "44-45"),
         (45, "44-45"), (45.01, ">45")],
    )
    def test_band_edges(self, avg, band):
        built = build_details([_raw(1, "A1", 1, "1", 5, avg)])
        hit = [b["band"] for b in built["weight_bands"] if b["frame_count"]]
        assert hit == [band]

    def test_eb_labels(self):
        raw = {r["doff_id"]: r for r in self._built()["raw_rows"]}
        assert raw[1]["eb_label"] == "E7"       # emp_code wins
        assert raw[4]["eb_label"] == "Contract"  # no eb_id at all
        assert raw[5]["eb_label"] == "EB8"       # eb without an emp_code

    def test_unmapped_quality_label(self):
        raw = {r["doff_id"]: r for r in self._built()["raw_rows"]}
        assert raw[5]["quality_name"] == "Unmapped"

    def test_empty_day(self):
        built = build_details([])
        assert built["doff_summary"] == [] and built["spell_totals"] == []
        assert len(built["weight_bands"]) == len(BAND_ORDER)
        assert all(b["frame_count"] == 0 for b in built["weight_bands"])


# ---------------------------------------------------------------------------
# build_frames
# ---------------------------------------------------------------------------


class TestBuildFrames:
    def _rows(self):
        d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
        return [
            # Frame 1 / quality 5: day 1 ran A and B (300 over 2 shifts -> 150),
            # day 2 ran A only (120 over 1 shift -> 120).
            _agg(d1, "A", "1", 5, 100.0, num=4, mx=30.0, mn=20.0, eb_id=7, emp_code="E7"),
            _agg(d1, "A", "1", 5, 60.0, num=2, mx=32.0, mn=28.0, eb_id=8, emp_code="E8"),
            _agg(d1, "B", "1", 5, 140.0, num=5, mx=35.0, mn=25.0),
            _agg(d2, "A", "1", 5, 120.0, num=4, mx=31.0, mn=29.0, eb_id=7, emp_code="E7"),
            # Frame 2 / quality 5: only day 2.
            _agg(d2, "C", "2", 5, 90.0, num=3, machine_id=2),
        ]

    def test_pivot_normalises_by_distinct_shifts(self):
        piv = {(r["frame"], r["quality_name"]): r for r in build_frames(self._rows())["frame_pivot"]}
        f1 = piv[("1", "Q5 - YARN 5")]
        assert f1["per_date"] == {"2026-08-01": 150.0, "2026-08-02": 120.0}
        assert (f1["avg"], f1["max"], f1["min"]) == (135.0, 150.0, 120.0)

    def test_pivot_ignores_missing_days(self):
        # Frame 2 has one day only — the blank day must not drag the average to 45.
        piv = {r["frame"]: r for r in build_frames(self._rows())["frame_pivot"]}
        assert piv["2"]["per_date"] == {"2026-08-02": 90.0}
        assert (piv["2"]["avg"], piv["2"]["min"]) == (90.0, 90.0)

    def test_quality_shift_average_is_per_date_and_frame(self):
        avg = {(r["quality_name"], r["shift"]): r["avg_net_wt"]
               for r in build_frames(self._rows())["quality_shift_avg"]}
        # A: frame1/day1 160, frame1/day2 120 -> 140
        assert avg[("Q5 - YARN 5", "A")] == 140.0
        assert avg[("Q5 - YARN 5", "B")] == 140.0
        assert avg[("Q5 - YARN 5", "C")] == 90.0

    def test_dates_listed_in_order(self):
        assert build_frames(self._rows())["dates"] == ["2026-08-01", "2026-08-02"]

    def test_no_frame_detail_unless_frame_given(self):
        assert "frame_detail" not in build_frames(self._rows())

    def test_frame_detail_shift_and_events(self):
        det = build_frames(self._rows(), frame="1")["frame_detail"]
        blank = {"target_wt": None, "diff_wt": None}
        assert det["date_shift"] == [
            {"date": "2026-08-01", "shift": "A", "net_wt": 160.0, **blank},
            {"date": "2026-08-01", "shift": "B", "net_wt": 140.0, **blank},
            {"date": "2026-08-02", "shift": "A", "net_wt": 120.0, **blank},
        ]
        ev = {(e["date"], e["shift"], e["eb_label"]): e for e in det["doff_events"]}
        e7 = ev[("2026-08-01", "A", "E7")]
        assert (e7["num_doffs"], e7["production"]) == (4, 100.0)
        assert (e7["avg_doff_wt"], e7["max_doff"], e7["min_doff"]) == (25.0, 30.0, 20.0)
        assert ev[("2026-08-01", "B", "Contract")]["num_doffs"] == 5
        # Two workers on day 1 shift A, one on shift B, one on day 2 — and frame
        # 2's day-2 C row must not leak into frame 1's drill.
        assert len(ev) == 4
        assert not [e for e in det["doff_events"] if e["shift"] == "C"]

    def test_frame_detail_unknown_frame_is_empty_not_error(self):
        det = build_frames(self._rows(), frame="99")["frame_detail"]
        assert det == {"date_shift": [], "doff_events": []}

    @pytest.mark.parametrize("raw,shift", [("A", "A"), ("b", "B"), ("C", "C"),
                                           ("D", "G"), ("", "G"), (None, "G")])
    def test_shift_letter_normalisation(self, raw, shift):
        rows = [_agg(date(2026, 8, 1), raw, "1", 5, 10.0)]
        assert build_frames(rows)["quality_shift_avg"][0]["shift"] == shift

    def test_two_non_abc_shifts_collapse_into_one_g_bucket(self):
        d = date(2026, 8, 1)
        rows = [_agg(d, "D", "1", 5, 10.0), _agg(d, "X", "1", 5, 30.0)]
        piv = build_frames(rows)["frame_pivot"][0]
        assert piv["per_date"] == {"2026-08-01": 40.0}  # 40 / 1 distinct shift


# ---------------------------------------------------------------------------
# Endpoint wiring + validation
# ---------------------------------------------------------------------------


class TestDoffAnalysisEndpoint:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_missing_co_id_400(self):
        self._session.execute.return_value = _scope_ok()
        r = client.get(URL, params={"branch_id": 29, "report_date": "2026-08-01"})
        assert r.status_code == 400 and "co_id" in r.json()["detail"]

    def test_missing_branch_id_400(self):
        self._session.execute.return_value = _scope_ok()
        r = client.get(URL, params={"co_id": 2, "report_date": "2026-08-01"})
        assert r.status_code == 400 and "branch_id" in r.json()["detail"]

    def test_branch_not_in_company_400(self):
        self._session.execute.return_value = _scope_bad()
        r = client.get(URL, params={"co_id": 2, "branch_id": 999, "report_date": "2026-08-01"})
        assert r.status_code == 400 and "branch_id" in r.json()["detail"]

    def test_unknown_mode_400(self):
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "nope"})
        assert r.status_code == 400 and "mode" in r.json()["detail"]

    def test_details_missing_report_date_400(self):
        self._session.execute.return_value = _scope_ok()
        r = client.get(URL, params={"co_id": 2, "branch_id": 29})
        assert r.status_code == 400 and "report_date" in r.json()["detail"]

    def test_details_bad_report_date_400(self):
        self._session.execute.return_value = _scope_ok()
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "report_date": "01-08-2026"})
        assert r.status_code == 400 and "report_date" in r.json()["detail"]

    def test_frames_missing_dates_400(self):
        self._session.execute.return_value = _scope_ok()
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "frames"})
        assert r.status_code == 400 and "date_from" in r.json()["detail"]

    def test_frames_reversed_range_400(self):
        self._session.execute.return_value = _scope_ok()
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "frames",
                                    "date_from": "2026-08-10", "date_to": "2026-08-01"})
        assert r.status_code == 400

    def test_frames_range_cap_31_days(self):
        self._session.execute.side_effect = [_scope_ok(), _rows_result([])]
        ok = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "frames",
                                     "date_from": "2026-08-01", "date_to": "2026-08-31"})
        assert ok.status_code == 200
        self._session.execute.side_effect = None
        self._session.execute.return_value = _scope_ok()
        too_big = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "frames",
                                          "date_from": "2026-08-01", "date_to": "2026-09-01"})
        assert too_big.status_code == 400 and "31" in too_big.json()["detail"]

    def test_details_happy_path_shape(self):
        self._session.execute.side_effect = [
            _scope_ok(),
            _rows_result([_raw(1, "A1", 101, "1", 5, 30.0, eb_id=7, emp_code="E7")]),
        ]
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "report_date": "2026-08-01"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert set(data) == {"doff_summary", "spell_totals", "weight_bands", "raw_rows", "meta"}
        assert data["meta"]["co_id"] == 2 and data["meta"]["branch_id"] == 29
        assert data["meta"]["date_from"] == data["meta"]["date_to"] == "2026-08-01"
        assert data["doff_summary"][0]["net_wt"] == 30.0

    def test_frames_happy_path_shape(self):
        self._session.execute.side_effect = [
            _scope_ok(),
            _rows_result([_agg(date(2026, 8, 1), "A", "1", 5, 100.0)]),
        ]
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "frames",
                                    "date_from": "2026-08-01", "date_to": "2026-08-05"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert set(data) == {"quality_shift_avg", "frame_pivot", "dates", "meta"}
        assert data["frame_pivot"][0]["per_date"] == {"2026-08-01": 100.0}

    def test_db_failure_is_500(self):
        self._session.execute.side_effect = [_scope_ok(), Exception("boom")]
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "report_date": "2026-08-01"})
        assert r.status_code == 500

    def test_binds_use_none_not_null_strings(self):
        self._session.execute.side_effect = [_scope_ok(), _rows_result([])]
        client.get(URL, params={"co_id": 2, "branch_id": 29, "report_date": "2026-08-01"})
        binds = self._session.execute.call_args_list[1][0][1]
        assert binds["spell_id"] is None and binds["item_id"] is None
        assert binds["report_date"] == date(2026, 8, 1)
        assert binds["co_id"] == 2 and binds["branch_id"] == 29


# ---------------------------------------------------------------------------
# Target / difference from target (TKT-000168)
# ---------------------------------------------------------------------------


def _tin(spell_code="A1", machine_id=101, frame="1", item_id=5, item_net=84.0,
         frame_net=84.0, d=date(2026, 8, 1), working_hours=None, std_speed=4000,
         spindles=120, std_tpi=4, std_eff=80, act_count=12.5, frozen=None,
         spell_id=1):
    """One row of get_doff_target_inputs_query."""
    return {
        "doff_date": d, "spell_id": spell_id, "spell_code": spell_code,
        "working_hours": working_hours, "machine_id": machine_id, "frame": frame,
        "item_id": item_id, "item_net": item_net, "frame_net": frame_net,
        "std_speed": std_speed, "spindles": spindles, "std_tpi": std_tpi,
        "std_eff": std_eff, "act_count": act_count, "frozen_target": frozen,
    }


class TestUnitTarget:
    def test_planning_formula_worked_example(self):
        # spinning_rules doc example: 4000 rpm, 300 min (A1), count 12.5,
        # 120 spindles, tpi 4 -> p100prod 394 kg; STD eff 80% -> 315.2 kg.
        assert unit_target(_tin()) == (315.2, [])

    def test_working_hours_win_over_the_spell_code_fallback(self):
        # 8 h spell = 480 min -> p100prod 630 -> 504.0 at 80%.
        assert unit_target(_tin(spell_code="A", working_hours=8)) == (504.0, [])

    def test_mid_spell_yarn_change_splits_minutes_by_weight_share(self):
        # 84 of 124 kg -> round(300 * 84/124) = 203 min -> p100prod 266 -> 212.8.
        target, missing = unit_target(_tin(item_net=84.0, frame_net=124.0))
        assert (target, missing) == (212.8, [])

    def test_target_eff_is_not_an_input(self):
        # Owner decision: target = Std Prod. A Target Eff % in the row is ignored.
        assert unit_target({**_tin(), "target_eff": 95}) == (315.2, [])
        assert unit_target({**_tin(std_eff=None), "target_eff": 95})[0] is None

    def test_frozen_processed_std_prod_wins(self):
        assert unit_target(_tin(frozen=301.456, std_tpi=None)) == (301.46, [])

    def test_frozen_zero_does_not_win(self):
        # A day processed before the standards existed stores 0 — recompute.
        assert unit_target(_tin(frozen=0)) == (315.2, [])

    @pytest.mark.parametrize("key,label", [
        ("std_speed", "Std Speed (frame)"),
        ("spindles", "Spindles (frame)"),
        ("std_tpi", "Std TPI (quality)"),
        ("std_eff", "Std Eff % (quality)"),
        ("act_count", "SQC spinning count for the day"),
    ])
    @pytest.mark.parametrize("blank", [None, 0])
    def test_missing_input_means_no_target_and_is_named(self, key, label, blank):
        target, missing = unit_target(_tin(**{key: blank}))
        assert target is None and missing == [label]

    def test_spell_without_hours_has_no_target(self):
        target, missing = unit_target(_tin(spell_code="G"))
        assert target is None and missing == ["spell working hours"]


class TestDetailsTargets:
    def _built(self):
        rows = [
            _raw(1, "A1", 101, "1", 5, 300.0),
            _raw(2, "A1", 102, "2", 5, 330.0),
            _raw(3, "A1", 103, "3", 6, 250.0),   # quality 6: no standards entered
            _raw(4, "B1", 101, "1", 5, 100.0),
        ]
        units = build_units([
            _tin("A1", 101, "1", 5, 300.0, 300.0),
            _tin("A1", 102, "2", 5, 330.0, 330.0),
            _tin("A1", 103, "3", 6, 250.0, 250.0, std_tpi=None, std_eff=None),
            _tin("B1", 101, "1", 5, 100.0, 100.0, working_hours=5),
        ])
        return build_details(rows, units), units

    def test_diff_is_target_minus_actual(self):
        data, _ = self._built()
        rows = {(r["spell_code"], r["frame"]): r for r in data["doff_summary"]}
        short = rows[("A1", "1")]          # 300 kg against 315.2 -> 15.2 short
        assert (short["target_wt"], short["diff_wt"]) == (315.2, 15.2)
        over = rows[("A1", "2")]           # 330 kg -> 14.8 over target
        assert (over["target_wt"], over["diff_wt"]) == (315.2, -14.8)
        assert "achv_pct" not in short

    def test_row_without_standards_is_blank(self):
        data, _ = self._built()
        row = next(r for r in data["doff_summary"] if r["frame"] == "3")
        assert row["target_wt"] is None and row["diff_wt"] is None

    def test_spell_total_skips_rows_without_a_target(self):
        data, _ = self._built()
        a1 = data["spell_totals"][0]
        # net counts all three frames; target/diff only the two that have one.
        assert a1 == {"spell_code": "A1", "net_wt": 880.0, "target_wt": 630.4,
                      "diff_wt": 0.4, "target_rows": 2, "rows": 3}

    def test_note_names_what_is_not_entered(self):
        _, units = self._built()
        (note,) = target_notes(units)
        assert "1 of 4" in note
        assert "Std TPI (quality) (1 rows)" in note and "Std Eff % (quality) (1 rows)" in note
        assert "Target Eff" not in note
        assert "Std Speed" not in note

    def test_no_note_when_every_row_has_a_target(self):
        assert target_notes(build_units([_tin()])) == []
        assert target_notes([]) == []


class TestFrameDetailTargets:
    def _detail(self, units):
        rows = [
            _agg(date(2026, 8, 1), "A", "1", 5, 300.0, machine_id=101),
            _agg(date(2026, 8, 1), "B", "1", 5, 200.0, machine_id=101),
            _agg(date(2026, 8, 1), "B", "1", 6, 50.0, machine_id=101),
            _agg(date(2026, 8, 1), "A", "2", 5, 999.0, machine_id=102),
        ]
        out = build_frames(rows, "1", build_units(units))["frame_detail"]["date_shift"]
        return {r["shift"]: r for r in out}

    def test_shift_row_sums_the_targets_of_its_spells(self):
        by = self._detail([
            _tin("A1", 101, "1", 5, 180.0, 180.0),                 # 315.2
            _tin("A2", 101, "1", 5, 120.0, 120.0, spell_id=2),     # 180 min -> 188.8
            _tin("A1", 102, "2", 5, 999.0, 999.0),                 # other frame: ignored
        ])
        a = by["A"]
        assert (a["target_wt"], a["diff_wt"]) == (504.0, 204.0)

    def test_shift_row_is_blank_when_one_unit_has_no_target(self):
        by = self._detail([
            _tin("B1", 101, "1", 5, 200.0, 250.0, working_hours=8),
            _tin("B1", 101, "1", 6, 50.0, 250.0, working_hours=8, std_tpi=None),
        ])
        assert by["B"]["target_wt"] is None and by["B"]["diff_wt"] is None
        assert by["A"]["target_wt"] is None   # no unit at all for shift A

    def test_unstamped_doffs_blank_the_shift_target(self):
        rows = [
            _agg(date(2026, 8, 1), "A", "1", 5, 300.0, machine_id=101),
            {**_agg(date(2026, 8, 1), "A", "1", 5, 40.0, machine_id=101), "item_id": None},
        ]
        units = build_units([_tin("A1", 101, "1", 5, 300.0, 300.0)])
        (row,) = build_frames(rows, "1", units)["frame_detail"]["date_shift"]
        assert row["net_wt"] == 340.0 and row["target_wt"] is None


class TestQualityShiftTargets:
    """TKT-000212: average standard per frame-shift and deviation on the
    Frame Analysis Quality x Shift grid."""

    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)

    def _rows(self):
        d1, d2 = self.d1, self.d2
        return [
            # day 1 shift A, frame 1: two workers, 300 kg over spells A1 + A2
            _agg(d1, "A", "1", 5, 200.0, machine_id=101, eb_id=7),
            _agg(d1, "A", "1", 5, 100.0, machine_id=101, eb_id=8),
            _agg(d1, "A", "2", 5, 250.0, machine_id=102),
            _agg(d2, "A", "1", 5, 280.0, machine_id=101),
            _agg(d1, "B", "1", 5, 260.0, machine_id=101),
        ]

    def _units(self, with_d2=True):
        d1, d2 = self.d1, self.d2
        tins = [
            _tin("A1", 101, "1", 5, d=d1, frozen=200.0),
            _tin("A2", 101, "1", 5, d=d1, frozen=120.0, spell_id=2),
            _tin("A1", 102, "2", 5, d=d1, frozen=260.0),
            _tin("B1", 101, "1", 5, d=d1, std_tpi=None),       # no standard
        ]
        if with_d2:
            tins.append(_tin("A1", 101, "1", 5, d=d2, frozen=300.0))
        return build_units(tins)

    def _by_shift(self, rows, units):
        return {r["shift"]: r for r in build_frames(rows, units=units)["quality_shift_avg"]}

    def test_standard_is_mean_of_frame_shift_standards(self):
        a = self._by_shift(self._rows(), self._units())["A"]
        # cells (actual, std): (300, 200+120), (250, 260), (280, 300)
        assert a["avg_net_wt"] == 276.67
        assert a["target_wt"] == 293.33
        assert a["diff_wt"] == 16.67              # target - actual: short
        assert a["diff_pct"] == 5.68
        assert (a["target_cells"], a["cells"]) == (3, 3)

    def test_diff_is_negative_when_over_standard(self):
        rows = [_agg(self.d1, "A", "1", 5, 330.0, machine_id=101)]
        units = build_units([_tin("A1", 101, "1", 5, d=self.d1, frozen=300.0)])
        (a,) = build_frames(rows, units=units)["quality_shift_avg"]
        assert (a["diff_wt"], a["diff_pct"]) == (-30.0, -10.0)

    def test_frame_shift_without_standard_is_left_out_not_zero(self):
        a = self._by_shift(self._rows(), self._units(with_d2=False))["A"]
        # d2 has no unit -> only (300, 320) and (250, 260) compare
        assert a["avg_net_wt"] == 276.67          # actual average is unchanged
        assert (a["target_wt"], a["diff_wt"]) == (290.0, 15.0)
        assert a["diff_pct"] == 5.17
        assert (a["target_cells"], a["cells"]) == (2, 3)

    def test_row_with_no_standard_at_all_is_blank(self):
        b = self._by_shift(self._rows(), self._units())["B"]
        assert b["avg_net_wt"] == 260.0
        assert (b["target_wt"], b["diff_wt"], b["diff_pct"]) == (None, None, None)
        assert (b["target_cells"], b["cells"]) == (0, 1)

    def test_one_spell_missing_blanks_that_frame_shift(self):
        units = [u for u in self._units() if u["spell_code"] != "A2"]
        units.append({**units[0], "spell_code": "A2", "target": None, "missing": ["x"]})
        a = self._by_shift(self._rows(), units)["A"]
        assert (a["target_cells"], a["cells"]) == (2, 3)
        assert a["target_wt"] == 280.0            # (260 + 300) / 2

    def test_unstamped_doffs_have_no_standard(self):
        rows = [{**_agg(self.d1, "A", "1", None, 120.0, machine_id=101),
                 "quality_name": None}]
        (a,) = build_frames(rows, units=self._units())["quality_shift_avg"]
        assert a["quality_name"] == "Unmapped" and a["target_wt"] is None

    def test_no_units_means_blank_standards(self):
        for units in (None, []):
            a = self._by_shift(self._rows(), units)["A"]
            assert a["avg_net_wt"] == 276.67 and a["target_wt"] is None
            assert a["target_cells"] == 0 and a["cells"] == 3


class TestTargetWiring:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_details_loads_targets_with_typed_binds(self):
        self._session.execute.side_effect = [
            _scope_ok(),
            _rows_result([_raw(1, "A1", 101, "1", 5, 300.0)]),
            _rows_result([_tin("A1", 101, "1", 5, 300.0, 300.0)]),
        ]
        r = client.get(URL, params={"co_id": 2, "branch_id": 29,
                                    "report_date": "2026-08-01", "item_id": 5})
        assert r.status_code == 200
        row = r.json()["data"]["doff_summary"][0]
        assert (row["target_wt"], row["diff_wt"]) == (315.2, 15.2)
        binds = self._session.execute.call_args_list[2][0][1]
        assert binds == {"co_id": 2, "branch_id": 29, "date_from": date(2026, 8, 1),
                         "date_to": date(2026, 8, 1), "spell_id": None, "item_id": 5,
                         "frame": None}

    def test_frames_loads_targets_for_the_quality_shift_standard(self):
        # TKT-000212: the range pivot now carries a standard, so frames mode
        # always loads the units — for EVERY frame (frame bind None).
        self._session.execute.side_effect = [
            _scope_ok(),
            _rows_result([_agg(date(2026, 8, 1), "A", "1", 5, 300.0, machine_id=101)]),
            _rows_result([_tin("A1", 101, "1", 5, 300.0, 300.0)]),
        ]
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "frames",
                                    "date_from": "2026-08-01", "date_to": "2026-08-05"})
        assert r.status_code == 200
        (qs,) = r.json()["data"]["quality_shift_avg"]
        assert (qs["avg_net_wt"], qs["target_wt"], qs["diff_wt"]) == (300.0, 315.2, 15.2)
        assert qs["diff_pct"] == 4.82 and (qs["target_cells"], qs["cells"]) == (1, 1)
        binds = self._session.execute.call_args_list[2][0][1]
        assert binds["frame"] is None and binds["date_to"] == date(2026, 8, 5)

    def test_frames_with_no_doffs_runs_no_target_query(self):
        self._session.execute.side_effect = [_scope_ok(), _rows_result([])]
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "frames",
                                    "date_from": "2026-08-01", "date_to": "2026-08-05"})
        assert r.status_code == 200
        assert self._session.execute.call_count == 2

    def test_frames_with_a_frame_drills_from_the_unfiltered_units(self):
        self._session.execute.side_effect = [
            _scope_ok(),
            _rows_result([
                _agg(date(2026, 8, 1), "A", "1", 5, 300.0, machine_id=101),
                _agg(date(2026, 8, 1), "A", "2", 5, 280.0, machine_id=102),
            ]),
            _rows_result([
                _tin("A1", 101, "1", 5, 300.0, 300.0),
                _tin("A1", 102, "2", 5, 280.0, 280.0, frozen=290.0),
            ]),
        ]
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "mode": "frames",
                                    "date_from": "2026-08-01", "date_to": "2026-08-05",
                                    "frame": "1"})
        assert r.status_code == 200
        data = r.json()["data"]
        (row,) = data["frame_detail"]["date_shift"]
        assert row["target_wt"] == 315.2          # frame 2's unit is not summed in
        (qs,) = data["quality_shift_avg"]
        assert qs["target_wt"] == 302.6 and qs["cells"] == 2   # (315.2 + 290) / 2
        assert self._session.execute.call_args_list[2][0][1]["frame"] is None

    def test_target_query_failure_keeps_the_report_alive(self):
        # e.g. a tenant without the spinning-standards tables yet.
        self._session.execute.side_effect = [
            _scope_ok(),
            _rows_result([_raw(1, "A1", 101, "1", 5, 300.0)]),
            Exception("Table 'x.jute_prod_spng_target_map' doesn't exist"),
        ]
        r = client.get(URL, params={"co_id": 2, "branch_id": 29, "report_date": "2026-08-01"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["doff_summary"][0]["target_wt"] is None
        assert any("Targets could not be loaded" in n for n in data["meta"]["notes"])


class TestTargetQuery:
    def test_sql_shape(self):
        from src.juteProduction.doff_analysis_query import get_doff_target_inputs_query

        sql = str(get_doff_target_inputs_query())
        for bind in (":co_id", ":branch_id", ":date_from", ":date_to", ":spell_id",
                     ":item_id", ":frame"):
            assert bind in sql
        assert "jute_prod_spng_target_map" in sql and "jute_sqc_spinning_count" in sql
        # Target = Std Prod: standard eff, and the frozen figure is std_prod —
        # never the Target-Eff based target_prod.
        assert "AS std_eff" in sql and "sd.std_prod" in sql
        assert "target_prod" not in sql and "value_role = 'target'" not in sql
        # last-date resolution per doff date, and spinning weight types only
        assert "t2.effective_date <= dts.d" in sql
        assert "dd.weight_type IN" in sql
        # the yarn/spell filters must sit OUTSIDE the unit CTE (frame_net needs
        # every yarn of the frame+spell)
        unit_cte = sql.split("dts AS")[0]
        assert ":item_id" not in unit_cte and ":spell_id" not in unit_cte
