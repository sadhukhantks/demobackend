"""Spinning Production Trend report
(GET /api/juteProductionReports/spinning_production_trend).

The arithmetic lives in the pure builder (build_trend / build_target_units),
exercised on stub rows; parameter validation and wiring go through TestClient
on a throwaway app, as test_juteprod_doff_analysis.py does.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.juteProduction.spinning_trend import (
    ACH_BANDS,
    ATTN_SHORT_MIN,
    build_attention,
    MAX_RANGE_DAYS,
    STATUS_LOW,
    STATUS_OK,
    STATUS_WATCH,
    build_target_units,
    unit_stdc,
    build_trend,
    unit_tgt,
    get_current_user_with_refresh,
    get_tenant_db,
    spinning_trend_router,
)

URL = "/api/juteProductionReports/spinning_production_trend"

app = FastAPI()
app.include_router(spinning_trend_router, prefix="/api/juteProductionReports")
client = TestClient(app)

D1, D2, D3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
P1 = date(2026, 9, 28)


def _row(d, shift, machine_id, frame, item_id, net, doffs=1, quality=None, eb_id=None,
         emp_code=None):
    return {
        "doff_date": d,
        "shift": shift,
        "machine_id": machine_id,
        "frame": frame,
        "item_id": item_id,
        "quality_name": quality
        if quality is not None
        else (f"Q{item_id}" if item_id else None),
        "eb_id": eb_id,
        "emp_code": emp_code,
        "net_wt": net,
        "num_doffs": doffs,
    }


def _rows():
    return [
        # Day 1: frame 1 runs quality 5 in A and B, frame 2 runs quality 6 in A.
        _row(D1, "A", 1, "1", 5, 100.0, doffs=4),
        _row(D1, "B", 1, "1", 5, 120.0, doffs=4),
        _row(D1, "A", 2, "2", 6, 60.0, doffs=2),
        # Day 2: one shift only (a Sunday) — frame 1 and frame 10.
        _row(D2, "C", 1, "1", 5, 90.0, doffs=3),
        _row(D2, "C", 10, "10", 6, 30.0, doffs=1),
        # Day 3: frame 1 changes yarn mid-shift A (5 -> 6), frame 2 unmapped.
        _row(D3, "A", 1, "1", 5, 50.0, doffs=2),
        _row(D3, "A", 1, "1", 6, 40.0, doffs=2),
        _row(D3, "A", 2, "2", None, 20.0, doffs=1),
    ]


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


def _scalar(value):
    res = MagicMock()
    res.first.return_value = (value,)
    return res


# ---------------------------------------------------------------------------
# build_trend — actuals
# ---------------------------------------------------------------------------


class TestDays:
    def _days(self):
        return {d["date"]: d for d in build_trend(_rows(), D1, D3)["days"]}

    def test_day_totals_and_frame_shifts(self):
        d1 = self._days()["2026-10-01"]
        assert d1["kg"] == 280.0
        assert d1["frames"] == 2
        # frame 1 x (A, B) + frame 2 x A = 3 frame-shifts
        assert d1["frame_shifts"] == 3
        assert d1["kg_per_frame_shift"] == round(280 / 3, 1)
        assert d1["kg_per_frame"] == 140.0
        assert d1["doffs"] == 10
        assert d1["avg_doff_kg"] == 28.0
        assert d1["shifts"] == ["A", "B"]
        assert d1["weekday"] == "Thu"

    def test_one_shift_day_is_normalised_per_shift(self):
        d2 = self._days()["2026-10-02"]
        assert d2["frame_shifts"] == 2
        assert d2["kg_per_frame_shift"] == 60.0
        assert d2["shifts"] == ["C"]

    def test_mid_shift_yarn_change_is_one_frame_shift(self):
        d3 = self._days()["2026-10-03"]
        # frame 1 ran A under two yarns -> still ONE frame-shift; frame 2 -> one.
        assert d3["frame_shifts"] == 2
        assert d3["kg"] == 110.0

    def test_targets_blank_without_units(self):
        for d in self._days().values():
            assert d["std_kg"] is None
            assert d["std_ach_pct"] is None
            assert d["std_coverage_pct"] is None


class TestDayQualityAndShift:
    def test_day_quality_rows_sorted_by_date_then_kg(self):
        dq = build_trend(_rows(), D1, D3)["day_quality"]
        assert [(r["date"], r["quality"]) for r in dq] == [
            ("2026-10-01", "Q5"),
            ("2026-10-01", "Q6"),
            ("2026-10-02", "Q5"),
            ("2026-10-02", "Q6"),
            ("2026-10-03", "Q5"),
            ("2026-10-03", "Q6"),
            ("2026-10-03", "Unmapped"),
        ]
        q5d1 = dq[0]
        assert (q5d1["kg"], q5d1["frame_shifts"], q5d1["kg_per_frame_shift"]) == (220.0, 2, 110.0)

    def test_quality_frame_shifts_can_exceed_the_days(self):
        # Day 3: frame 1 A counts once under Q5 and once under Q6.
        dq = {(r["date"], r["quality"]): r for r in build_trend(_rows(), D1, D3)["day_quality"]}
        assert dq[("2026-10-03", "Q5")]["frame_shifts"] == 1
        assert dq[("2026-10-03", "Q6")]["frame_shifts"] == 1

    def test_day_shift_rows(self):
        ds = {(r["date"], r["shift"]): r for r in build_trend(_rows(), D1, D3)["day_shift"]}
        a = ds[("2026-10-01", "A")]
        assert (a["kg"], a["frames"], a["doffs"], a["frame_shifts"]) == (160.0, 2, 6, 2)
        assert a["kg_per_frame_shift"] == 80.0
        assert a["std_kg"] is None and a["std_ach_pct"] is None
        assert ds[("2026-10-01", "B")]["kg"] == 120.0
        assert ds[("2026-10-02", "C")]["frames"] == 2

    def test_other_shift_letters_bucket_as_g(self):
        rows = [_row(D1, "X", 1, "1", 5, 10.0), _row(D1, "", 1, "1", 5, 10.0)]
        ds = build_trend(rows, D1, D1)["day_shift"]
        assert [r["shift"] for r in ds] == ["G"]
        assert ds[0]["kg"] == 20.0


class TestQualities:
    def test_sorted_by_volume_with_share(self):
        qs = build_trend(_rows(), D1, D3)["qualities"]
        assert [q["quality"] for q in qs] == ["Q5", "Q6", "Unmapped"]
        q5 = qs[0]
        assert q5["kg"] == 360.0
        assert q5["share_pct"] == round(360 / 510 * 100, 1)
        assert q5["days"] == 3
        assert q5["frames"] == 1
        # frame 1: D1 A, D1 B, D2 C, D3 A = 4 frame-shifts on Q5
        assert q5["frame_shifts"] == 4
        assert q5["kg_per_frame_shift"] == 90.0
        assert q5["avg_doff_kg"] == round(360 / 13, 2)

    def test_unmapped_has_no_item_id(self):
        qs = build_trend(_rows(), D1, D3)["qualities"]
        assert qs[-1]["item_id"] is None and qs[-1]["kg"] == 20.0


class TestFrames:
    def test_frames_sort_numerically_and_carry_their_qualities(self):
        fr = build_trend(_rows(), D1, D3)["frames"]
        assert [f["frame"] for f in fr] == ["1", "2", "10"]
        f1 = fr[0]
        assert f1["kg"] == 400.0
        assert f1["frame_shifts"] == 4
        assert f1["kg_per_frame_shift"] == 100.0
        assert f1["qualities"] == ["Q5", "Q6"]
        assert f1["days"] == 3

    def test_expected_is_the_frames_own_quality_mix(self):
        fr = {f["frame"]: f for f in build_trend(_rows(), D1, D3)["frames"]}
        # Quality averages: Q5 = 90.0 (360/4), Q6 = 130/3 = 43.3, Unmapped = 20.
        # Frame 10 ran Q6 only -> expected 43.3, actual 30 -> 69.3 % -> LOW.
        f10 = fr["10"]
        assert f10["expected_kg_per_frame_shift"] == 43.3
        assert f10["vs_peers_pct"] == round(30 / 43.3 * 100, 1)
        assert f10["status"] == STATUS_LOW
        # Frame 1: Q5 x 4 frame-shifts + Q6 x 1 -> (90*4 + 43.3*1)/5 = 80.7
        f1 = fr["1"]
        assert f1["expected_kg_per_frame_shift"] == 80.7
        assert f1["status"] == STATUS_OK

    @pytest.mark.parametrize(
        "net,status",
        [(89.0, STATUS_LOW), (90.0, STATUS_WATCH), (96.9, STATUS_WATCH), (97.0, STATUS_OK)],
    )
    def test_status_thresholds(self, net, status):
        # Two frames on one quality: frame 2 fixed at 100, frame 1 at `net`,
        # quality average = (100 + net) / 2 -> vs = net / avg.
        # Pick frame 2 so the average lands on 100 exactly: use 3 frames.
        rows = [
            _row(D1, "A", 1, "1", 5, net),
            _row(D1, "A", 2, "2", 5, 100.0 + (100.0 - net)),  # keeps the mean at 100
        ]
        fr = {f["frame"]: f for f in build_trend(rows, D1, D1)["frames"]}
        assert fr["1"]["expected_kg_per_frame_shift"] == 100.0
        assert fr["1"]["status"] == status


class TestSummary:
    def test_period_totals(self):
        s = build_trend(_rows(), D1, D3)["summary"]
        assert s["production_kg"] == 510.0
        assert s["calendar_days"] == 3 and s["producing_days"] == 3
        assert s["avg_kg_per_day"] == 170.0
        assert s["frame_shifts"] == 7
        assert s["kg_per_frame_shift"] == round(510 / 7, 1)
        # frames per day: 2 + 2 + 2 = 6 / 3
        assert s["frames_avg_per_day"] == 2.0
        assert s["doffs"] == 19
        assert s["best_day"] == {"date": "2026-10-01", "kg": 280.0}
        assert s["worst_day"] == {"date": "2026-10-03", "kg": 110.0}
        assert s["previous"] is None and s["delta"] is None

    def test_calendar_days_count_blank_days(self):
        s = build_trend(_rows(), D1, date(2026, 10, 10))["summary"]
        assert s["calendar_days"] == 10 and s["producing_days"] == 3

    def test_previous_period_and_deltas(self):
        prev = [
            _row(P1, "A", 1, "1", 5, 100.0),
            _row(P1, "B", 1, "1", 5, 100.0),
            _row(date(2026, 9, 29), "A", 1, "1", 5, 55.0),
        ]
        s = build_trend(_rows(), D1, D3, prev, P1, date(2026, 9, 30))["summary"]
        p = s["previous"]
        assert p["date_from"] == "2026-09-28" and p["date_to"] == "2026-09-30"
        assert p["producing_days"] == 2
        assert p["production_kg"] == 255.0
        assert p["frame_shifts"] == 3
        assert p["kg_per_frame_shift"] == 85.0
        assert p["frames_avg_per_day"] == 1.0
        assert s["delta"]["production_kg"] == 100.0
        # Deltas are taken on the ROUNDED figures the tiles show (72.9 vs 85.0).
        assert s["delta"]["kg_per_frame_shift"] == round((72.9 - 85) / 85 * 100, 1)
        assert s["delta"]["frames_avg_per_day"] == 100.0

    def test_delta_blank_when_previous_is_empty(self):
        s = build_trend(_rows(), D1, D3, [], P1, date(2026, 9, 30))["summary"]
        assert s["previous"]["production_kg"] == 0.0
        assert s["previous"]["kg_per_frame_shift"] is None
        assert s["delta"] == {
            "production_kg": None,
            "avg_kg_per_day": None,
            "kg_per_frame_shift": None,
            "frames_avg_per_day": None,
        }

    def test_empty_window(self):
        out = build_trend([], D1, D3)
        assert out["days"] == [] and out["qualities"] == [] and out["frames"] == []
        s = out["summary"]
        assert s["production_kg"] == 0.0 and s["producing_days"] == 0
        assert s["kg_per_frame_shift"] is None
        assert s["best_day"] is None and s["worst_day"] is None


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def _target_row(d, spell, machine_id, frame, item_id, item_net, frozen=None, **std):
    base = {
        "doff_date": d,
        "spell_id": 1,
        "spell_code": spell,
        "working_hours": 8,
        "machine_id": machine_id,
        "frame": frame,
        "item_id": item_id,
        "item_net": item_net,
        "frame_net": item_net,
        "std_speed": None,
        "spindles": None,
        "std_tpi": None,
        "std_eff": None,
        "act_count": None,
        "std_count": None,
        "target_eff": None,
        "frozen_target": frozen,
        "frozen_tgt_prod": None,
        "frozen_act_count": None,
        "frozen_std_count": None,
    }
    base.update(std)
    return base


class TestTargets:
    def test_units_keep_actual_and_target(self):
        units = build_target_units([_target_row(D1, "A1", 1, "1", 5, 100.0, frozen=120.0)])
        u = units[0]
        assert (u["date"], u["shift"], u["machine_id"], u["item_id"]) == ("2026-10-01", "A", 1, 5)
        assert (u["actual"], u["target"], u["missing"]) == (100.0, 120.0, [])

    def test_unit_without_standards_has_no_target(self):
        u = build_target_units([_target_row(D1, "A1", 1, "1", 5, 100.0)])[0]
        assert u["target"] is None and u["missing"]

    def test_achievement_on_covered_units_only(self):
        units = build_target_units(
            [
                # Day 1, frame 1, Q5 split across A1/A2 (shift A): 100 actual vs 125 target.
                _target_row(D1, "A1", 1, "1", 5, 60.0, frozen=75.0),
                _target_row(D1, "A2", 1, "1", 5, 40.0, frozen=50.0),
                # Day 1, frame 1, Q5, B: 120 actual, NO standards -> no target.
                _target_row(D1, "B1", 1, "1", 5, 120.0),
                # Day 1, frame 2, Q6: 60 actual vs 50 target (over).
                _target_row(D1, "A", 2, "2", 6, 60.0, frozen=50.0),
            ]
        )
        out = build_trend(_rows(), D1, D3, units=units)
        d1 = {d["date"]: d for d in out["days"]}["2026-10-01"]
        assert d1["std_kg"] == 175.0
        # covered actual = 100 + 60 = 160 of a 280 kg day
        assert d1["std_ach_pct"] == round(160 / 175 * 100, 1)
        assert d1["std_coverage_pct"] == round(160 / 280 * 100, 1)

        q = {x["quality"]: x for x in out["qualities"]}
        assert q["Q5"]["std_kg"] == 125.0
        assert q["Q5"]["std_ach_pct"] == 80.0
        assert q["Q5"]["std_coverage_pct"] == round(100 / 360 * 100, 1)
        assert q["Q6"]["std_ach_pct"] == 120.0
        assert q["Unmapped"]["std_kg"] is None

        dq = {(x["date"], x["quality"]): x for x in out["day_quality"]}
        assert dq[("2026-10-01", "Q6")]["std_kg"] == 50.0
        assert dq[("2026-10-02", "Q5")]["std_kg"] is None

        fr = {f["frame"]: f for f in out["frames"]}
        assert fr["2"]["std_kg"] == 50.0 and fr["2"]["std_ach_pct"] == 120.0
        assert fr["10"]["std_kg"] is None

        s = out["summary"]
        assert s["std_kg"] == 175.0
        assert s["std_coverage_pct"] == round(160 / 510 * 100, 1)

    def test_unit_outside_the_window_is_ignored(self):
        units = build_target_units([_target_row(P1, "A", 1, "1", 5, 10.0, frozen=10.0)])
        s = build_trend(_rows(), D1, D3, units=units)["summary"]
        assert s["std_kg"] is None


# ---------------------------------------------------------------------------
# Endpoint — validation + wiring
# ---------------------------------------------------------------------------


def _mount(db):
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}


def _unmount():
    app.dependency_overrides.clear()


class TestEndpoint:
    def setup_method(self):
        self.db = MagicMock()
        _mount(self.db)

    def teardown_method(self):
        _unmount()

    def test_requires_co_branch_and_dates(self):
        assert client.get(URL).status_code == 400
        assert client.get(URL, params={"co_id": 1}).status_code == 400
        r = client.get(URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01"})
        assert r.status_code == 400 and "date_to" in r.json()["detail"]
        self.db.execute.assert_not_called()

    def test_rejects_reversed_and_oversized_ranges(self):
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-03", "date_to": "2026-10-01"},
        )
        assert r.status_code == 400
        too_long = date(2026, 1, 1).toordinal() + MAX_RANGE_DAYS
        r = client.get(
            URL,
            params={
                "co_id": 1,
                "branch_id": 2,
                "date_from": "2026-01-01",
                "date_to": date.fromordinal(too_long).isoformat(),
            },
        )
        assert r.status_code == 400 and str(MAX_RANGE_DAYS) in r.json()["detail"]
        self.db.execute.assert_not_called()

    def test_bad_item_id(self):
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01",
                    "date_to": "2026-10-03", "item_id": "x"},
        )
        assert r.status_code == 400

    def test_branch_not_of_company(self):
        self.db.execute.return_value = _scope_bad()
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01", "date_to": "2026-10-03"}
        )
        assert r.status_code == 400
        assert "branch_id" in r.json()["detail"]

    def test_wiring_splits_previous_period_and_overlays_targets(self):
        self.db.execute.side_effect = [
            _scope_ok(),
            _rows_result(
                _rows() + [_row(P1, "A", 1, "1", 5, 100.0), _row(date(2026, 9, 30), "B", 1, "1", 5, 100.0)]
            ),
            _rows_result(
                [
                    _target_row(D1, "A", 2, "2", 6, 60.0, frozen=50.0),
                    # Frame 1 on day 2 has no standards -> blank target + note.
                    _target_row(D2, "C", 1, "1", 5, 90.0),
                ]
            ),
            _scalar(40),  # installed frames
        ]
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01", "date_to": "2026-10-03"},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        s = data["summary"]
        assert s["production_kg"] == 510.0
        assert s["previous"]["date_from"] == "2026-09-28"
        assert s["previous"]["production_kg"] == 200.0
        assert s["std_kg"] == 50.0
        assert data["meta"]["date_from"] == "2026-10-01"
        assert data["meta"]["item_id"] is None
        assert any("Unmapped" in n for n in data["meta"]["notes"])
        assert any("changed yarn" in n for n in data["meta"]["notes"])
        assert any("Target is blank" in n for n in data["meta"]["notes"])
        assert data["loss"]["bridge"]["installed_frames"] == 40
        assert data["options"]["shifts"] == ["A", "B", "C"]
        assert data["meta"]["shift"] is None and data["meta"]["frame"] is None

        # The scan was asked for the widened window, the targets for the report window.
        scan_binds = self.db.execute.call_args_list[1].args[1]
        assert scan_binds["date_from"] == date(2026, 9, 28)
        assert scan_binds["date_to"] == date(2026, 10, 3)
        target_binds = self.db.execute.call_args_list[2].args[1]
        assert target_binds["date_from"] == date(2026, 10, 1)
        assert target_binds["frame"] is None and target_binds["spell_id"] is None

    def test_std_count_note_names_the_coverage(self):
        self.db.execute.side_effect = [
            _scope_ok(),
            _rows_result(_rows()),
            _rows_result(
                [
                    _target_row(D1, "A", 2, "2", 6, 60.0, frozen=50.0, frozen_act_count=10.0, frozen_std_count=11.0),
                    _target_row(D1, "B", 1, "1", 5, 120.0, frozen=125.0),  # no counts
                ]
            ),
            _scalar(40),
        ]
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01", "date_to": "2026-10-03"},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["summary"]["stdc_kg"] == 55.0
        note = next(n for n in data["meta"]["notes"] if "Std at std count" in n)
        assert "1 of 2 frame-shifts" in note

    def test_empty_range_skips_the_target_query(self):
        self.db.execute.side_effect = [_scope_ok(), _rows_result([])]
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01", "date_to": "2026-10-03"},
        )
        assert r.status_code == 200
        assert self.db.execute.call_count == 2
        assert any("No spinning doffs" in n for n in r.json()["data"]["meta"]["notes"])

    def test_target_query_failure_blanks_targets(self):
        self.db.execute.side_effect = [_scope_ok(), _rows_result(_rows()), RuntimeError("no table")]
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01", "date_to": "2026-10-03"},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["summary"]["std_kg"] is None
        assert any("could not be loaded" in n for n in data["meta"]["notes"])


# ---------------------------------------------------------------------------
# Loss analysis
# ---------------------------------------------------------------------------


def _loss_rows():
    """Two days, two frames, one quality. Frame 1 is a regular spinner E1 in A
    and B; frame 2 is a contract hand in A. Day 2: frame 1 runs A with a short
    shift (1 doff) and B normally."""
    return [
        _row(D1, "A", 1, "1", 5, 100.0, doffs=10, eb_id=7, emp_code="E1"),
        _row(D1, "B", 1, "1", 5, 120.0, doffs=10, eb_id=7, emp_code="E1"),
        _row(D1, "A", 2, "2", 5, 60.0, doffs=10),
        _row(D2, "A", 1, "1", 5, 20.0, doffs=1, eb_id=7, emp_code="E1"),
        _row(D2, "B", 1, "1", 5, 110.0, doffs=10, eb_id=8, emp_code="E2"),
    ]


def _loss_units():
    return build_target_units(
        [
            _target_row(D1, "A", 1, "1", 5, 100.0, frozen=125.0),
            _target_row(D1, "B", 1, "1", 5, 120.0, frozen=125.0),
            _target_row(D1, "A", 2, "2", 5, 60.0, frozen=125.0),
            _target_row(D2, "A", 1, "1", 5, 20.0, frozen=125.0),
            _target_row(D2, "B", 1, "1", 5, 110.0, frozen=125.0),
        ]
    )


class TestLoss:
    def _out(self, **kw):
        return build_trend(_loss_rows(), D1, D2, units=_loss_units(), installed_frames=4, **kw)

    def test_gap_and_bridge(self):
        out = self._out()
        s = out["summary"]
        assert s["std_kg"] == 625.0 and s["gap_kg"] == 215.0
        b = out["loss"]["bridge"]
        # 2 producing days x 2 shift letters seen x 4 installed = 16 capacity frame-shifts
        assert (b["installed_frames"], b["frames_ran"], b["shifts_per_day"]) == (4, 2, 2)
        assert b["capacity_frame_shifts"] == 16
        assert b["idle_frame_frame_shifts"] == 8          # 2 frames never ran x 2 days x 2
        assert b["idle_shift_frame_shifts"] == 3          # 2 x 2 x 2 = 8 possible, 5 run
        assert b["run_frame_shifts"] == 5
        assert b["std_per_frame_shift"] == 125.0
        assert b["utilization_pct"] == round(5 / 16 * 100, 1)
        steps = {x["step"]: x["kg"] for x in b["steps"]}
        assert steps["capacity"] == 2000.0
        assert steps["idle_frames"] == 1000.0
        assert steps["idle_shifts"] == 375.0
        assert steps["run_std"] == 625.0
        assert steps["efficiency"] == 215.0
        assert steps["actual"] == 410.0
        assert steps["capacity"] - steps["idle_frames"] - steps["idle_shifts"] == steps["run_std"]
        assert steps["run_std"] - steps["efficiency"] == steps["actual"]

    def test_bridge_respects_filters(self):
        assert self._out(item_filter=5)["loss"]["bridge"] is None
        b = self._out(shift_filter="A")["loss"]["bridge"]
        assert b["shifts_per_day"] == 1 and b["capacity_frame_shifts"] == 8
        # A frame filter arrives with rows of that frame only (the SQL did the cut).
        rows1 = [r for r in _loss_rows() if r["machine_id"] == 1]
        b = build_trend(rows1, D1, D2, units=_loss_units(), installed_frames=4, frame_filter="1")[
            "loss"
        ]["bridge"]
        assert b["installed_frames"] == 1 and b["idle_frame_frame_shifts"] == 0
        assert b["capacity_frame_shifts"] == 4 and b["idle_shift_frame_shifts"] == 0
        assert build_trend(_loss_rows(), D1, D2, units=_loss_units())["loss"]["bridge"] is None

    def test_installed_below_frames_ran_is_clamped(self):
        b = self._out()  # 4 installed, 2 ran
        assert b["loss"]["bridge"]["idle_frame_frame_shifts"] == 8
        b2 = build_trend(_loss_rows(), D1, D2, units=_loss_units(), installed_frames=1)
        assert b2["loss"]["bridge"]["installed_frames"] == 2
        assert b2["loss"]["bridge"]["idle_frame_frame_shifts"] == 0

    def test_by_shift_and_weekday(self):
        loss = self._out()["loss"]
        sh = {r["shift"]: r for r in loss["by_shift"]}
        assert sh["A"]["frame_shifts"] == 3 and sh["A"]["kg"] == 180.0
        assert sh["A"]["std_kg"] == 375.0 and sh["A"]["gap_kg"] == 195.0
        assert sh["A"]["gap_share_pct"] == round(195 / 215 * 100, 1)
        assert sh["B"]["std_ach_pct"] == 92.0
        wd = {r["weekday"]: r for r in loss["by_weekday"]}
        assert wd["Thu"]["kg"] == 280.0 and wd["Fri"]["kg"] == 130.0
        assert [r["weekday"] for r in loss["by_weekday"]] == ["Thu", "Fri"]

    def test_hands_and_spinners(self):
        loss = self._out()["loss"]
        hands = {h["kind"]: h for h in loss["hands"]}
        assert hands["regular"]["frame_shifts"] == 4 and hands["regular"]["kg"] == 350.0
        assert hands["contract"]["frame_shifts"] == 1 and hands["contract"]["kg"] == 60.0
        assert hands["contract"]["std_ach_pct"] == 48.0
        sp = {r["spinner"]: r for r in loss["spinners"]}
        assert set(sp) == {"E1", "E2"}
        assert sp["E1"]["gap_kg"] == 135.0 and sp["E1"]["frame_shifts"] == 3
        assert sp["E2"]["gap_kg"] == 15.0
        assert loss["spinners"][0]["spinner"] == "E1"
        assert loss["spinner_count"] == 2

    def test_target_split_between_two_spinners_by_kg_share(self):
        rows = [
            _row(D1, "A", 1, "1", 5, 75.0, doffs=5, eb_id=7, emp_code="E1"),
            _row(D1, "A", 1, "1", 5, 25.0, doffs=5, eb_id=8, emp_code="E2"),
        ]
        units = build_target_units([_target_row(D1, "A", 1, "1", 5, 100.0, frozen=200.0)])
        loss = build_trend(rows, D1, D1, units=units)["loss"]
        sp = {r["spinner"]: r for r in loss["spinners"]}
        assert sp["E1"]["std_kg"] == 150.0 and sp["E2"]["std_kg"] == 50.0
        assert loss["worst_frame_shifts"][0]["spinner"] == "E1 +1"

    def test_histogram_and_median(self):
        loss = self._out()["loss"]
        bands = {h["band"]: h["frame_shifts"] for h in loss["ach_histogram"]}
        assert [h["band"] for h in loss["ach_histogram"]] == ACH_BANDS
        # achs: 80, 96, 48, 16, 88
        assert bands["80-90"] == 2 and bands["90-100"] == 1 and bands["40-50"] == 1 and bands["<30"] == 1
        assert sum(bands.values()) == 5
        assert loss["median_ach_pct"] == 80.0

    def test_short_frame_shifts(self):
        short = self._out()["loss"]["short_frame_shifts"]
        # Frame 1 median doffs = 10; day-2 shift A had 1 doff -> short.
        assert short["count"] == 1
        assert short["share_pct"] == 20.0
        row = short["rows"][0]
        assert (row["date"], row["shift"], row["frame"], row["doffs"], row["median_doffs"]) == (
            "2026-10-02", "A", "1", 1, 10)
        assert row["gap_kg"] == 105.0 and row["ach_pct"] == 16.0
        assert short["gap_kg"] == 105.0

    def test_worst_frame_shifts_sorted_by_gap(self):
        worst = self._out()["loss"]["worst_frame_shifts"]
        assert [(w["date"], w["shift"], w["frame"]) for w in worst[:2]] == [
            ("2026-10-02", "A", "1"),
            ("2026-10-01", "A", "2"),
        ]
        assert worst[1]["spinner"] == "Contract" and worst[1]["qualities"] == "Q5"
        assert worst[0]["gap_kg"] == 105.0

    def test_heat_grid_worst_frame_first(self):
        heat = self._out()["loss"]["heat"]
        assert heat["dates"] == ["2026-10-01", "2026-10-02"]
        # Sorted by the frame's total gap to standard: frame 1 is 150 kg short,
        # frame 2 is 65 kg short.
        assert [f["frame"] for f in heat["frames"]] == ["1", "2"]
        f1 = heat["frames"][0]
        # day 1: 220 / 250 = 88 %, day 2: 130 / 250 = 52 %
        assert f1["cells"] == {"2026-10-01": 88.0, "2026-10-02": 52.0}
        assert f1["gap_cells"] == {"2026-10-01": -30.0, "2026-10-02": -120.0}
        assert f1["gap_kg"] == -150.0
        assert f1["mean_ach_pct"] == 70.0

    def test_frame_shift_points(self):
        pts = self._out()["loss"]["frame_shift_points"]
        assert len(pts) == 5
        p = {(x["date"], x["shift"], x["frame"]): x for x in pts}
        assert p[("2026-10-01", "A", "1")] == {
            "date": "2026-10-01", "shift": "A", "frame": "1", "kg": 100.0, "std_kg": 125.0, "tgt_kg": None,
        }
        assert p[("2026-10-02", "A", "1")]["kg"] == 20.0

    def test_frame_rows_carry_gap_and_consistency(self):
        fr = {f["frame"]: f for f in self._out()["frames"]}
        assert fr["1"]["gap_kg"] == 150.0
        assert fr["1"]["gap_share_pct"] == round(150 / 215 * 100, 1)
        assert fr["1"]["mean_ach_pct"] == 70.0 and fr["1"]["ach_sd"] > 0
        assert fr["2"]["ach_sd"] == 0.0

    def test_options_list_shifts_and_frames(self):
        out = self._out()
        assert out["options"] == {"shifts": ["A", "B"], "frames": ["1", "2"]}

    def test_no_targets_gives_empty_loss(self):
        loss = build_trend(_loss_rows(), D1, D2, installed_frames=4)["loss"]
        assert loss["gap_kg"] is None and loss["bridge"] is None
        assert loss["worst_frame_shifts"] == [] and loss["spinners"] == []
        assert loss["heat"] == {"dates": [], "frames": []}
        assert loss["short_frame_shifts"]["count"] == 1  # the doff rule needs no target


class TestFilterParams:
    def setup_method(self):
        self.db = MagicMock()
        _mount(self.db)

    def teardown_method(self):
        _unmount()

    def test_bad_shift(self):
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01",
                    "date_to": "2026-10-03", "shift": "X"},
        )
        assert r.status_code == 400
        self.db.execute.assert_not_called()

    def test_filters_reach_the_scan_and_the_units(self):
        self.db.execute.side_effect = [
            _scope_ok(),
            _rows_result([_row(D1, "A", 1, "1", 5, 100.0)]),
            _rows_result(
                [
                    _target_row(D1, "A1", 1, "1", 5, 60.0, frozen=75.0),
                    _target_row(D1, "B1", 1, "1", 5, 120.0, frozen=100.0),  # filtered out
                ]
            ),
            _scalar(10),
        ]
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01", "date_to": "2026-10-03",
                    "shift": "a", "frame": "1", "item_id": 5},
        )
        assert r.status_code == 200, r.text
        scan = self.db.execute.call_args_list[1].args[1]
        assert (scan["shift"], scan["frame"], scan["item_id"]) == ("A", "1", 5)
        targets = self.db.execute.call_args_list[2].args[1]
        assert targets["frame"] == "1" and targets["item_id"] == 5
        data = r.json()["data"]
        assert data["summary"]["std_kg"] == 75.0  # the B1 unit was dropped
        assert data["meta"]["shift"] == "A" and data["meta"]["frame"] == "1"
        assert data["loss"]["bridge"] is None  # quality filter -> no capacity bridge


class TestAttention:
    def test_rules_and_reasons(self):
        frames = [
            {"frame": "1", "machine_id": 1, "status": "low", "vs_peers_pct": 70.0,
             "mean_ach_pct": 50.0, "ach_sd": 20.0, "frame_shifts": 10, "gap_kg": 500.0},
            {"frame": "2", "machine_id": 2, "status": "ok", "vs_peers_pct": 99.0,
             "mean_ach_pct": 72.0, "ach_sd": 5.0, "frame_shifts": 10, "gap_kg": 900.0},
            {"frame": "3", "machine_id": 3, "status": "watch", "vs_peers_pct": 95.0,
             "mean_ach_pct": 68.0, "ach_sd": 30.0, "frame_shifts": 2, "gap_kg": 100.0},
            {"frame": "4", "machine_id": 4, "status": "ok", "vs_peers_pct": 100.0,
             "mean_ach_pct": 71.0, "ach_sd": 1.0, "frame_shifts": 10, "gap_kg": 50.0},
        ]
        out = build_attention(frames, 72.0, {"2": ATTN_SHORT_MIN, "4": 1}, {"2": 3})
        by = {a["frame"]: a for a in out}
        # Frame 4 fires nothing (ok, near median, steady, one short, not in worst).
        assert set(by) == {"1", "2", "3"}
        # Biggest gap first.
        assert [a["frame"] for a in out] == ["2", "1", "3"]
        r1 = by["1"]["reasons"]
        assert any(r.startswith("LOW vs peers") for r in r1)
        assert any("22.0 points under" in r for r in r1)
        assert any(r.startswith("erratic") for r in r1)
        r2 = by["2"]["reasons"]
        assert any("short frame-shifts" in r for r in r2)
        assert any("worst frame-shifts" in r for r in r2)
        assert by["2"]["short_count"] == ATTN_SHORT_MIN and by["2"]["worst_count"] == 3
        # Frame 3: WATCH, but too few frame-shifts for the erratic rule.
        assert by["3"]["reasons"] == ["WATCH vs peers (95.0% of expected)"]

    def test_attention_is_wired_into_the_response(self):
        out = build_trend(_loss_rows(), D1, D2, units=_loss_units(), installed_frames=4)
        attn = out["loss"]["attention"]
        # Frame 1: 16 % and 48 %-ish frame-shifts -> erratic + worst list; frame 2 LOW.
        frames = {a["frame"]: a for a in attn}
        assert "1" in frames and "2" in frames
        assert frames["1"]["worst_count"] >= 2
        assert all(a["reasons"] for a in attn)
        assert [a["gap_kg"] for a in attn] == sorted((a["gap_kg"] for a in attn), reverse=True)

    def test_no_targets_no_attention(self):
        out = build_trend(_loss_rows(), D1, D2, installed_frames=4)
        # Without targets only the peer rule can fire; the stub frames are
        # judged against each other, so just assert the shape.
        assert isinstance(out["loss"]["attention"], list)
        for a in out["loss"]["attention"]:
            assert a["reasons"] and a["gap_kg"] is None


# ---------------------------------------------------------------------------
# Standard per frame-shift + targets (when a target efficiency is entered)
# ---------------------------------------------------------------------------


class TestStdPerFrameShiftAndTargets:
    def test_std_per_frame_shift_divides_by_covered_frame_shifts(self):
        units = build_target_units(
            [
                _target_row(D1, "A", 1, "1", 5, 100.0, frozen=125.0),
                _target_row(D1, "B", 1, "1", 5, 120.0, frozen=125.0),
                # frame 2 shift A has no standard -> not a covered frame-shift
            ]
        )
        out = build_trend(_rows(), D1, D3, units=units)
        d1 = {d["date"]: d for d in out["days"]}["2026-10-01"]
        assert d1["frame_shifts"] == 3
        assert d1["std_frame_shifts"] == 2
        assert d1["std_kg"] == 250.0
        assert d1["std_kg_per_frame_shift"] == 125.0
        assert d1["tgt_kg"] is None and d1["tgt_kg_per_frame_shift"] is None

    def test_unit_tgt_from_target_eff(self):
        # p100 for speed 4000, 300 min, count 12.5, 120 spindles, tpi 4 = 394 (doc example)
        row = _target_row(
            D1, "A1", 1, "1", 5, 100.0,
            std_speed=4000, spindles=120, std_tpi=4, std_eff=80, act_count=12.5,
            working_hours=5, target_eff=90,
        )
        assert unit_tgt(row) == round(394 * 0.9, 2)
        # no target efficiency -> no target
        row["target_eff"] = None
        assert unit_tgt(row) is None
        # a frozen planning-grid target wins
        row["frozen_tgt_prod"] = 333.3
        assert unit_tgt(row) == 333.3

    def test_targets_roll_up_beside_the_standard(self):
        units = build_target_units(
            [
                _target_row(D1, "A", 1, "1", 5, 100.0, frozen=125.0, frozen_tgt_prod=140.0),
                _target_row(D1, "B", 1, "1", 5, 120.0, frozen=125.0),  # standard only
                _target_row(D1, "A", 2, "2", 6, 60.0, frozen=50.0, frozen_tgt_prod=55.0),
            ]
        )
        out = build_trend(_rows(), D1, D3, units=units)
        s = out["summary"]
        assert s["std_kg"] == 300.0 and s["tgt_kg"] == 195.0
        assert s["std_frame_shifts"] == 3 and s["tgt_frame_shifts"] == 2
        assert s["std_kg_per_frame_shift"] == 100.0
        assert s["tgt_kg_per_frame_shift"] == 97.5
        d1 = {d["date"]: d for d in out["days"]}["2026-10-01"]
        assert d1["tgt_kg"] == 195.0
        q = {x["quality"]: x for x in out["qualities"]}
        assert q["Q5"]["tgt_kg"] == 140.0 and q["Q6"]["tgt_kg"] == 55.0
        ds = {(x["date"], x["shift"]): x for x in out["day_shift"]}
        assert ds[("2026-10-01", "A")]["tgt_kg"] == 195.0
        assert ds[("2026-10-01", "B")]["tgt_kg"] is None
        fr = {f["frame"]: f for f in out["frames"]}
        assert fr["2"]["tgt_kg_per_frame_shift"] == 55.0
        worst = {(w["date"], w["shift"], w["frame"]): w for w in out["loss"]["worst_frame_shifts"]}
        assert worst[("2026-10-01", "A", "2")]["tgt_kg"] == 55.0
        assert worst[("2026-10-01", "B", "1")]["tgt_kg"] is None

    def test_target_split_follows_the_standard_split(self):
        rows = [
            _row(D1, "A", 1, "1", 5, 75.0, doffs=5, eb_id=7, emp_code="E1"),
            _row(D1, "A", 1, "1", 5, 25.0, doffs=5, eb_id=8, emp_code="E2"),
        ]
        units = build_target_units(
            [_target_row(D1, "A", 1, "1", 5, 100.0, frozen=200.0, frozen_tgt_prod=240.0)]
        )
        sp = {r["spinner"]: r for r in build_trend(rows, D1, D1, units=units)["loss"]["spinners"]}
        assert sp["E1"]["tgt_kg"] == 180.0 and sp["E2"]["tgt_kg"] == 60.0


# ---------------------------------------------------------------------------
# Standard at the yarn's standard count (stdc_*)
# ---------------------------------------------------------------------------


class TestStdAtStdCount:
    def test_recomputed_unit_uses_the_standard_count(self):
        # p100 at count 12.5 = 394 (doc example); at the standard count 10 it is 315.
        row = _target_row(
            D1, "A1", 1, "1", 5, 100.0,
            std_speed=4000, spindles=120, std_tpi=4, std_eff=80, act_count=12.5,
            working_hours=5, std_count=10.0,
        )
        u = build_target_units([row])[0]
        assert u["target"] == round(394 * 0.8, 2)
        assert u["stdc"] == round(315 * 0.8, 2)

    def test_frozen_unit_scales_by_the_counts_the_grid_stored(self):
        row = _target_row(
            D1, "A1", 1, "1", 5, 100.0, frozen=300.0,
            frozen_act_count=12.0, frozen_std_count=10.0,
            act_count=11.0, std_count=9.0,  # live counts drifted — ignored
        )
        assert unit_stdc(row, 300.0) == 250.0

    def test_frozen_unit_falls_back_to_the_live_counts(self):
        row = _target_row(D1, "A1", 1, "1", 5, 100.0, frozen=300.0, act_count=12.0, std_count=9.0)
        assert unit_stdc(row, 300.0) == 225.0

    def test_blank_without_both_counts_or_a_standard(self):
        no_std_count = _target_row(D1, "A1", 1, "1", 5, 100.0, frozen=300.0, act_count=12.0)
        assert unit_stdc(no_std_count, 300.0) is None
        no_act_count = _target_row(D1, "A1", 1, "1", 5, 100.0, frozen=300.0, std_count=9.0)
        assert unit_stdc(no_act_count, 300.0) is None
        assert unit_stdc(no_act_count, None) is None
        # a unit with a standard count but no standard at all
        u = build_target_units([_target_row(D1, "A1", 1, "1", 5, 100.0, std_count=9.0)])[0]
        assert u["target"] is None and u["stdc"] is None

    def test_rolls_up_on_every_cut(self):
        units = build_target_units(
            [
                # stdc = 125 x 10 / 12.5 = 100
                _target_row(D1, "A", 1, "1", 5, 100.0, frozen=125.0, frozen_act_count=12.5, frozen_std_count=10.0),
                _target_row(D1, "B", 1, "1", 5, 120.0, frozen=125.0),  # no counts -> standard only
                # stdc = 50 x 11 / 10 = 55
                _target_row(D1, "A", 2, "2", 6, 60.0, frozen=50.0, frozen_act_count=10.0, frozen_std_count=11.0),
            ]
        )
        out = build_trend(_rows(), D1, D3, units=units)
        s = out["summary"]
        assert s["std_kg"] == 300.0 and s["stdc_kg"] == 155.0
        assert s["std_frame_shifts"] == 3 and s["stdc_frame_shifts"] == 2
        assert s["stdc_kg_per_frame_shift"] == 77.5
        d1 = {d["date"]: d for d in out["days"]}["2026-10-01"]
        assert d1["stdc_kg"] == 155.0 and d1["stdc_kg_per_frame_shift"] == 77.5
        d2 = {d["date"]: d for d in out["days"]}["2026-10-02"]
        assert d2["stdc_kg"] is None
        q = {x["quality"]: x for x in out["qualities"]}
        assert q["Q5"]["stdc_kg"] == 100.0 and q["Q6"]["stdc_kg"] == 55.0
        dq = {(x["date"], x["quality"]): x for x in out["day_quality"]}
        assert dq[("2026-10-01", "Q6")]["stdc_kg_per_frame_shift"] == 55.0
        ds = {(x["date"], x["shift"]): x for x in out["day_shift"]}
        assert ds[("2026-10-01", "A")]["stdc_kg"] == 155.0
        assert ds[("2026-10-01", "B")]["stdc_kg"] is None
        fr = {f["frame"]: f for f in out["frames"]}
        assert fr["2"]["stdc_kg"] == 55.0 and fr["2"]["stdc_kg_per_frame_shift"] == 55.0
        sh = {x["shift"]: x for x in out["loss"]["by_shift"]}
        assert sh["A"]["stdc_kg"] == 155.0 and sh["B"]["stdc_kg"] is None

    def test_split_between_spinners_follows_kg_share(self):
        rows = [
            _row(D1, "A", 1, "1", 5, 75.0, doffs=5, eb_id=7, emp_code="E1"),
            _row(D1, "A", 1, "1", 5, 25.0, doffs=5, eb_id=8, emp_code="E2"),
        ]
        units = build_target_units(
            [_target_row(D1, "A", 1, "1", 5, 100.0, frozen=200.0, frozen_act_count=10.0, frozen_std_count=8.0)]
        )
        sp = {r["spinner"]: r for r in build_trend(rows, D1, D1, units=units)["loss"]["spinners"]}
        assert sp["E1"]["stdc_kg"] == 120.0 and sp["E2"]["stdc_kg"] == 40.0

    def test_blank_everywhere_without_units(self):
        out = build_trend(_rows(), D1, D3)
        assert out["summary"]["stdc_kg"] is None and out["summary"]["stdc_frame_shifts"] == 0
        assert all(d["stdc_kg"] is None for d in out["days"])
