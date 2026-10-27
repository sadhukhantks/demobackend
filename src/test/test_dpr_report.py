"""Tests for the Daily Production Report endpoint + section builders.

src/juteProduction/dpr_reports.py (prefix /api/juteProductionReports).

Portal persona: DB + auth mocked via app.dependency_overrides keyed on the exact
symbols the router imports (get_tenant_db / get_current_user_with_refresh). The
section builders are pure functions and are unit-tested directly on stub rows;
the endpoint test feeds a fixed sequence of db.execute results matching the
call order in daily_production_report.

Covered:
  * missing co_id -> 400
  * missing / invalid report_date -> 400
  * build_spinning_section — shift bucketing, frame runs, kg/frame, utilization,
    quality roll-up with weighted act_count, missing-standard eff_pct (TKT-000116)
  * build_spinning_avg_count — average count by frames run / actual production /
    std expected production; blank standards excluded, not zero (TKT-000173)
  * build_winding_section — per-quality totals, distinct winders, kg/winder,
    twisting netted out of the totals (TKT-000127)
  * build_weaving_section — quality + shift pivots, weighted efficiency
  * build_emulsion_section — LOW/HIGH/OK band verdict + the two None cases
  * build_yarn_heavy_light — heavy/light verdict sign + obs fallback
  * build_jute_section — group + total roll-up; days-stock on spinning
    production (TKT-000172)
  * pick_cover_basis — report date's own production; 7-producing-day average
    on a holiday / part day; no data
  * build_hands_section — shift ordering and totals
  * hands_per_mt stage-wise — yarn-stage hands ÷ spinning MT + cloth-stage
    hands ÷ weaving MT (TKT-000185), with the TKT-000117 spinning basis as
    the fallback when no department resolves; MTD ratio; yarn balance;
    purchased-yarn lookup degrading on a tenant without the yarn tables
  * build_power_section — ITM-004 day / MTD kWh, DG and kWh/t (keyed, not
    keyed, table absent, zero tonnes); fetch_power_readings degrading on a
    tenant without jute_power_reading
  * happy path — full endpoint with branch, KPI wiring, unavailable list
"""

from datetime import date
from unittest.mock import MagicMock

from sqlalchemy.exc import ProgrammingError

from fastapi.testclient import TestClient

from src.main import app
from src.juteProduction.dpr_reports import (
    build_emulsion_section,
    build_hands_section,
    build_jute_section,
    build_power_section,
    build_shifts_worked,
    build_spinning_section,
    build_weaving_section,
    build_winding_section,
    build_yarn_heavy_light,
    fetch_power_readings,
    get_current_user_with_refresh,
    get_tenant_db,
    pick_cover_basis,
    sqc_count_basis,
)

client = TestClient(app)


def _row(mapping):
    row = MagicMock()
    row._mapping = mapping
    return row


def _rows(mappings):
    return [_row(m) for m in mappings]


def _result(mappings):
    res = MagicMock()
    res.fetchall.return_value = _rows(mappings)
    return res


def _first_result(mapping):
    """A db.execute(...) result read with .first() (emulsion, branch pair check)."""
    res = MagicMock()
    res.first.return_value = _row(mapping) if mapping is not None else None
    return res


# ---------------------------------------------------------------------------
# Builder unit tests
# ---------------------------------------------------------------------------


class TestBuildSpinningSection:
    def test_shift_and_quality_rollup(self):
        slice_rows = [
            # Two frames in A shift on the same item, one in C, one idle frame.
            {"shift_bucket": "A", "machine_id": 1, "item_id": 10, "item_code": "Y9",
             "item_name": "9 LB", "act_prod_doff": 400.0, "std_prod": 500.0,
             "target_prod": 450.0, "act_count": 9.0, "std_count": 9.0,
             "act_prod_wind": 300.0},
            {"shift_bucket": "A", "machine_id": 2, "item_id": 10, "item_code": "Y9",
             "item_name": "9 LB", "act_prod_doff": 200.0, "std_prod": 500.0,
             "target_prod": 450.0, "act_count": 9.6, "std_count": 9.0,
             "act_prod_wind": 100.0},
            {"shift_bucket": "C", "machine_id": 1, "item_id": 10, "item_code": "Y9",
             "item_name": "9 LB", "act_prod_doff": 100.0, "std_prod": 250.0,
             "target_prod": 200.0, "act_count": 9.0, "std_count": 9.0,
             "act_prod_wind": 0.0},
            {"shift_bucket": "B", "machine_id": 3, "item_id": 10, "item_code": "Y9",
             "item_name": "9 LB", "act_prod_doff": 0.0, "std_prod": 0.0,
             "target_prod": 0.0, "act_count": 0.0, "std_count": 9.0,
             "act_prod_wind": 0.0},
        ]
        mtd_rows = [
            {"shift_bucket": "A", "production_kg": 6000.0, "frame_runs": 20},
            {"shift_bucket": "C", "production_kg": 1000.0, "frame_runs": 5},
        ]
        out = build_spinning_section(slice_rows, mtd_rows, total_frames=4)

        shifts = {s["shift"]: s for s in out["shifts"]}
        assert shifts["A"]["production_kg"] == 600.0
        assert shifts["A"]["frame_runs"] == 2
        assert shifts["A"]["kg_per_frame"] == 300.0
        assert shifts["A"]["utilization_pct"] == 50.0  # 2 of 4 frames
        assert shifts["A"]["eff_pct"] == 60.0  # 600/1000
        # Idle frame (zero prod) contributes no frame run.
        assert "B" in shifts and shifts["B"]["frame_runs"] == 0

        assert out["total"]["production_kg"] == 700.0
        assert out["total"]["frame_runs"] == 3
        # 3 runs of 4 frames * 3 shifts.
        assert out["total"]["utilization_pct"] == 25.0

        assert out["mtd"]["production_kg"] == 7000.0
        assert out["mtd"]["frame_runs"] == 25

        q = out["quality"][0]
        assert q["item_code"] == "Y9"
        assert q["production_kg"] == 700.0
        # Weighted count: (400*9 + 200*9.6 + 100*9) / 700
        assert q["act_count"] == round((400 * 9 + 200 * 9.6 + 100 * 9) / 700, 2)
        assert q["winding_kg"] == 400.0

    def test_quality_frame_shifts_dedup_half_spells(self):
        # A1 and A2 are separate spell_ids collapsing to bucket A: the same
        # frame doffing in both must count ONE frame-shift (matches shift
        # table set-dedup and MTD COUNT DISTINCT), not two.
        base = {"item_id": 10, "item_code": "Y9", "item_name": "9 LB",
                "std_prod": 100.0, "target_prod": 90.0, "act_count": 9.0,
                "std_count": 9.0, "act_prod_wind": 0.0}
        slice_rows = [
            {**base, "shift_bucket": "A", "machine_id": 1, "act_prod_doff": 60.0},
            {**base, "shift_bucket": "A", "machine_id": 1, "act_prod_doff": 40.0},
            {**base, "shift_bucket": "B", "machine_id": 1, "act_prod_doff": 50.0},
        ]
        out = build_spinning_section(slice_rows, [], total_frames=1)
        q = out["quality"][0]
        assert q["frame_shifts"] == 2
        assert q["kg_per_frame_shift"] == 75.0  # 150 / 2, not 150 / 3
        assert q["target_per_frame_shift"] == 135.0  # 270 / 2
        assert out["total"]["frame_runs"] == 2

    def test_missing_standard_gives_null_eff_not_zero(self):
        # TKT-000116: sls co 2 has no id_type='qid' tpi/eff rows in
        # jute_prod_spng_target_map, so std_prod is 0 for every frame of that
        # quality. A blank means "not configured"; 0.0% read as measured failure
        # on a mill running normally.
        slice_rows = [
            # B shift quality has a standard — a real percentage must survive.
            {"shift_bucket": "B", "machine_id": 2, "item_id": 11, "item_code": "Y10",
             "item_name": "10 LB", "act_prod_doff": 450.0, "std_prod": 500.0,
             "target_prod": 480.0, "act_count": 10.0, "std_count": 10.0,
             "act_prod_wind": 0.0},
            # A shift quality has none.
            {"shift_bucket": "A", "machine_id": 1, "item_id": 10, "item_code": "Y9",
             "item_name": "9 LB", "act_prod_doff": 520.0, "std_prod": 0.0,
             "target_prod": 0.0, "act_count": 9.1, "std_count": 9.0,
             "act_prod_wind": 500.0},
        ]
        out = build_spinning_section(slice_rows, [], total_frames=4)
        shifts = {s["shift"]: s for s in out["shifts"]}
        assert shifts["A"]["eff_pct"] is None  # not 0.0
        assert shifts["B"]["eff_pct"] == 90.0  # 450 / 500
        by_code = {q["item_code"]: q for q in out["quality"]}
        assert by_code["Y9"]["eff_pct"] is None
        assert by_code["Y10"]["eff_pct"] == 90.0
        # Mixed day: the total measures ONLY the standardised subset — 450 of
        # 500, not 970 of 500. Counting the unconfigured quality's 520 kg in the
        # numerator while its absent standard stays out of the denominator would
        # print 194%, which is a worse lie than the 0% this ticket started from.
        # standards_missing is what tells the reader the total is partial.
        assert out["total"]["eff_pct"] == round(450.0 * 100 / 500.0, 2) == 90.0
        assert out["standards_missing"] == ["9 LB"]

    def test_standards_missing_names_only_unconfigured_qualities(self):
        base = {"shift_bucket": "A", "target_prod": 0.0, "act_count": 9.0,
                "std_count": 9.0, "act_prod_wind": 0.0}
        slice_rows = [
            {**base, "machine_id": 1, "item_id": 10, "item_code": "Y9",
             "item_name": "9 LB", "act_prod_doff": 400.0, "std_prod": 500.0},
            {**base, "machine_id": 2, "item_id": 11, "item_code": "Y10",
             "item_name": "10 LB", "act_prod_doff": 300.0, "std_prod": 0.0},
            # Falls back to item_code when the master has no name.
            {**base, "machine_id": 3, "item_id": 12, "item_code": "Y12",
             "item_name": None, "act_prod_doff": 200.0, "std_prod": 0.0},
        ]
        out = build_spinning_section(slice_rows, [], total_frames=3)
        assert out["standards_missing"] == ["10 LB", "Y12"]

    def test_standards_missing_empty_when_all_configured(self):
        slice_rows = [
            {"shift_bucket": "A", "machine_id": 1, "item_id": 10, "item_code": "Y9",
             "item_name": "9 LB", "act_prod_doff": 400.0, "std_prod": 500.0,
             "target_prod": 450.0, "act_count": 9.0, "std_count": 9.0,
             "act_prod_wind": 0.0},
        ]
        assert build_spinning_section(slice_rows, [], total_frames=1)["standards_missing"] == []

    # -- TKT-000234: blank (not 0.0) target + the reason it is blank ----------

    @staticmethod
    def _row(machine_id, item_id, name, *, prod=100.0, std_speed=3000.0, spindles=100.0,
             std_tpi=4.0, std_eff=65.0, target_eff=70.0, act_count=10.0,
             std_prod=None, target_prod=None, mech=None):
        has_p100 = std_speed > 0 and spindles > 0 and std_tpi > 0 and act_count > 0
        return {
            "shift_bucket": "A", "machine_id": machine_id, "mech_code": mech or f"F{machine_id}",
            "item_id": item_id, "item_code": name, "item_name": name,
            "act_prod_doff": prod, "act_count": act_count, "std_count": 10.0,
            "act_prod_wind": 0.0, "std_speed": std_speed, "spindles": spindles,
            "std_tpi": std_tpi, "std_eff": std_eff, "target_eff": target_eff,
            "p100prod": 200.0 if has_p100 else 0.0,
            "std_prod": std_prod if std_prod is not None else (2.0 * std_eff if has_p100 else 0.0),
            "target_prod": target_prod if target_prod is not None
            else (2.0 * target_eff if has_p100 else 0.0),
        }

    def test_target_falls_back_to_standard_when_target_eff_missing(self):
        # Owner ruling 2026-10-24: no target Eff % but a standard -> the target
        # is the standard production (std eff 65% -> 2 x 65 = 130 kg), flagged.
        out = build_spinning_section([self._row(1, 10, "10.5-SKWP", target_eff=0.0)], [], 1)
        q = out["quality"][0]
        assert q["target_per_frame_shift"] == 130.0
        assert q["target_basis"] == "standard"
        assert q["target_std_eff_pct"] == 65.0
        assert q["eff_pct"] == round(100.0 * 100 / 130.0, 2)
        assert q["missing"] == ["target_eff_fallback"]
        assert out["targets_missing"] == []
        assert out["standards_missing"] == []
        assert out["missing_summary"] == [{
            "code": "target_eff_fallback",
            "label": "Target uses Standard Eff % (Target Eff % not set)",
            "where": "Spinning Target Map > Quality > Target", "qualities": ["10.5-SKWP"],
        }]
        # Aggregates use the same fallback.
        assert out["total"]["avg_target_per_frame"] == 130.0

    def test_target_blank_when_standard_also_missing(self):
        out = build_spinning_section(
            [self._row(1, 10, "Y", std_eff=0.0, target_eff=0.0)], [], 1
        )
        q = out["quality"][0]
        assert q["target_per_frame_shift"] is None
        assert q["target_basis"] is None and q["target_std_eff_pct"] is None
        assert q["missing"] == ["std_eff", "target_eff"]
        assert out["targets_missing"] == ["Y"]
        assert out["total"]["avg_target_per_frame"] == 0.0

    def test_real_target_wins_over_standard(self):
        q = build_spinning_section([self._row(1, 10, "Y")], [], 1)["quality"][0]
        assert q["target_per_frame_shift"] == 140.0  # 2 x 70, not 2 x 65
        assert q["target_basis"] == "target" and q["target_std_eff_pct"] is None

    def test_reasons_per_quality_in_display_order(self):
        rows = [
            # sls co 106 shape: no quality std/target at all, SQC count present.
            self._row(1, 11, "12-S/WARP", std_tpi=0.0, std_eff=0.0, target_eff=0.0),
            # ... and no SQC count either.
            self._row(2, 12, "19-S/WEFT", std_tpi=0.0, std_eff=0.0, target_eff=0.0,
                      act_count=0.0),
            # Fully configured.
            self._row(3, 13, "10.5-SKWP"),
        ]
        out = build_spinning_section(rows, [], 3)
        by = {q["item_code"]: q for q in out["quality"]}
        assert by["12-S/WARP"]["missing"] == ["std_tpi", "std_eff", "target_eff"]
        assert by["19-S/WEFT"]["missing"] == ["std_tpi", "std_eff", "target_eff", "sqc_count"]
        assert by["10.5-SKWP"]["missing"] == []
        assert by["10.5-SKWP"]["target_per_frame_shift"] == 140.0
        assert by["12-S/WARP"]["target_per_frame_shift"] is None
        assert by["12-S/WARP"]["eff_pct"] is None
        assert out["targets_missing"] == ["12-S/WARP", "19-S/WEFT"]
        assert out["standards_missing"] == ["12-S/WARP", "19-S/WEFT"]
        summary = {m["code"]: m["qualities"] for m in out["missing_summary"]}
        assert list(summary) == ["std_tpi", "std_eff", "target_eff", "sqc_count"]
        assert summary["sqc_count"] == ["19-S/WEFT"]

    def test_machine_std_missing_names_frames(self):
        rows = [
            self._row(1, 10, "Y", std_speed=0.0, mech="SP-07"),
            self._row(2, 10, "Y"),
        ]
        out = build_spinning_section(rows, [], 2)
        q = out["quality"][0]
        assert q["missing"] == ["machine_std"]
        assert q["missing_frames"] == ["SP-07"]
        # Partial: the configured frame still yields a target.
        assert q["target_per_frame_shift"] == 70.0  # 140 / 2

    def test_unexplained_zero_capacity_is_flagged(self):
        row = self._row(1, 10, "Y", std_prod=0.0, target_prod=0.0)
        row["p100prod"] = 0.0  # inputs present, spell minutes 0
        q = build_spinning_section([row], [], 1)["quality"][0]
        assert q["missing"] == ["no_capacity"]

    def test_stub_rows_without_input_columns_report_nothing(self):
        # Older callers / stubs carry no std_speed / std_tpi columns: no false
        # "missing" claims, only the blank cell.
        rows = [{"shift_bucket": "A", "machine_id": 1, "item_id": 10, "item_code": "Y9",
                 "item_name": "9 LB", "act_prod_doff": 400.0, "std_prod": 0.0,
                 "target_prod": 0.0, "act_count": 9.0, "std_count": 9.0,
                 "act_prod_wind": 0.0}]
        q = build_spinning_section(rows, [], 1)["quality"][0]
        assert q["target_per_frame_shift"] is None
        assert q["missing"] == []


def _sp(machine_id, shift, item_id, name, count, prod, std=0.0, act=0.0, corr=0.0):
    return {"shift_bucket": shift, "machine_id": machine_id, "item_id": item_id,
            "item_code": name, "item_name": name, "act_prod_doff": prod,
            "std_prod": std, "target_prod": 0.0, "act_count": act,
            "act_corr_count": corr, "std_count": count, "act_prod_wind": 0.0}


class TestSpinningAverageCount:
    """TKT-000173 — mill average count on three weightings."""

    def test_three_weightings(self):
        rows = [
            # 8 lb: two frame-shifts, 1000 kg, std expects 1500 kg.
            _sp(1, "A", 10, "8 LB", 8.0, 600.0, 750.0),
            _sp(1, "B", 10, "8 LB", 8.0, 400.0, 750.0),
            # 16 lb: one frame-shift, 1000 kg, std expects 500 kg.
            _sp(2, "A", 20, "16 LB", 16.0, 1000.0, 500.0),
        ]
        avg = build_spinning_section(rows, [], total_frames=2)["avg_count"]
        assert avg["by_frames"] == round((8 * 2 + 16 * 1) / 3, 2) == 10.67
        assert avg["frame_shifts"] == 3
        assert avg["by_production"] == 12.0  # (8*1000 + 16*1000) / 2000
        assert avg["production_kg"] == 2000.0
        assert avg["by_std_production"] == 10.0  # (8*1500 + 16*500) / 2000
        assert avg["std_production_kg"] == 2000.0
        assert (avg["std_qualities"], avg["qualities"]) == (2, 2)
        assert avg["count_missing"] == []

    def test_a1_a2_half_spells_are_one_frame_shift(self):
        # The slice is at (frame, spell, item) grain; A1/A2 share bucket A.
        rows = [_sp(1, "A", 10, "8 LB", 8.0, 300.0), _sp(1, "A", 10, "8 LB", 8.0, 200.0),
                _sp(2, "A", 20, "16 LB", 16.0, 500.0)]
        avg = build_spinning_section(rows, [], total_frames=2)["avg_count"]
        assert avg["frame_shifts"] == 2
        assert avg["by_frames"] == 12.0

    def test_blank_standard_is_excluded_not_zero(self):
        # Owner rule: a standard that was never entered is ignored. The 16 lb
        # quality has none, so the std-weighted average is the 8 lb alone —
        # NOT (8*1500 + 16*0) / 1500 dressed up as a mix figure, and not 0.
        rows = [_sp(1, "A", 10, "8 LB", 8.0, 1000.0, 1500.0),
                _sp(2, "A", 20, "16 LB", 16.0, 1000.0, 0.0)]
        avg = build_spinning_section(rows, [], total_frames=2)["avg_count"]
        assert avg["by_std_production"] == 8.0
        assert avg["std_production_kg"] == 1500.0
        assert (avg["std_qualities"], avg["qualities"]) == (1, 2)
        # The other two bases still cover both qualities.
        assert avg["by_production"] == 12.0

    def test_no_standards_at_all_is_none(self):
        """sls co 2 br 29, 18-09-2026: no quality-level std TPI / eff entered."""
        rows = (
            [_sp(i, "A", 335563, "13-SKWP", 13.0, 8189.5 / 25) for i in range(25)]
            + [_sp(100 + i, "A", 335865, "16(96)-SKWP", 16.0, 1978.7 / 6) for i in range(6)]
            + [_sp(200 + i, "A", 335564, "16-SKWP", 16.0, 9246.8 / 25) for i in range(25)]
            + [_sp(300, "A", 335961, "7.5 LBS-HSWT", 7.5, 187.5),
               _sp(301, "A", 335560, "9-HSWP", 9.0, 255.1),
               _sp(302, "A", 335676, "9.5(TWINE)-HSWP", 9.5, 280.0)]
        )
        avg = build_spinning_section(rows, [], total_frames=58)["avg_count"]
        assert avg["frame_shifts"] == 59
        assert avg["by_frames"] == 14.36  # 847 / 59
        assert avg["by_production"] == 14.52  # 292,433.65 / 20,137.6
        assert avg["by_std_production"] is None  # "—", never 0
        assert avg["std_qualities"] == 0

    def test_idle_rows_and_missing_count(self):
        rows = [
            _sp(1, "A", 10, "8 LB", 8.0, 1000.0, 1200.0),
            _sp(2, "A", 10, "8 LB", 8.0, 0.0, 1200.0),  # idle helper row: ignored
            _sp(3, "A", 30, "NO COUNT", 0.0, 500.0, 600.0),  # yarn master has no count
        ]
        avg = build_spinning_section(rows, [], total_frames=3)["avg_count"]
        assert avg["frame_shifts"] == 1
        assert avg["by_frames"] == avg["by_production"] == avg["by_std_production"] == 8.0
        assert avg["production_kg"] == 1000.0
        assert avg["count_missing"] == ["NO COUNT"]

    def test_empty_day(self):
        avg = build_spinning_section([], [], total_frames=4)["avg_count"]
        assert avg["by_frames"] is None
        assert avg["by_production"] is None
        assert avg["by_std_production"] is None


class TestSpinningAverageActualCount:
    """TKT-000201 — the same three weightings on the SQC actual count
    (slice act_count = the day's AVG(jute_sqc_spinning_count.observed_count))."""

    def test_actual_count_weightings_beside_std(self):
        rows = [
            # 8 lb std, SQC 8.4: two frame-shifts, 1000 kg, std expects 1500 kg.
            _sp(1, "A", 10, "8 LB", 8.0, 600.0, 750.0, act=8.4),
            _sp(1, "B", 10, "8 LB", 8.0, 400.0, 750.0, act=8.4),
            # 16 lb std, SQC 15.0: one frame-shift, 1000 kg, std expects 500 kg.
            _sp(2, "A", 20, "16 LB", 16.0, 1000.0, 500.0, act=15.0),
        ]
        avg = build_spinning_section(rows, [], total_frames=2)["avg_count"]
        # Std column unchanged from TKT-000173.
        assert (avg["by_frames"], avg["by_production"], avg["by_std_production"]) == (
            10.67, 12.0, 10.0)
        assert avg["act_by_frames"] == round((8.4 * 2 + 15.0) / 3, 2) == 10.6
        assert avg["act_by_production"] == 11.7  # (8.4*1000 + 15*1000) / 2000
        assert avg["act_by_std_production"] == 10.05  # (8.4*1500 + 15*500) / 2000
        assert avg["act_measured_kg"] == 2000.0
        assert avg["act_fallback"] == []

    def test_no_sqc_reading_falls_back_to_std_and_is_named(self):
        rows = [
            _sp(1, "A", 10, "8 LB", 8.0, 1000.0, 1000.0, act=8.5),
            # No SQC count today: std stands in (and the slice's std_prod is 0,
            # since its 100% production is computed from the SQC count).
            _sp(2, "A", 20, "16 LB", 16.0, 1000.0, 0.0, act=0.0),
        ]
        avg = build_spinning_section(rows, [], total_frames=2)["avg_count"]
        assert avg["act_by_production"] == 12.25  # (8.5*1000 + 16*1000) / 2000
        assert avg["act_by_frames"] == 12.25
        assert avg["act_by_std_production"] == 8.5  # only the measured quality
        assert avg["act_measured_kg"] == 1000.0
        assert avg["act_fallback"] == ["16 LB"]

    def test_no_sqc_at_all_repeats_std_with_zero_coverage(self):
        rows = [_sp(1, "A", 10, "8 LB", 8.0, 1000.0), _sp(2, "A", 20, "16 LB", 16.0, 1000.0)]
        avg = build_spinning_section(rows, [], total_frames=2)["avg_count"]
        assert avg["act_by_production"] == avg["by_production"] == 12.0
        assert avg["act_by_frames"] == avg["by_frames"]
        assert avg["act_measured_kg"] == 0.0
        assert avg["act_fallback"] == ["16 LB", "8 LB"]

    def test_sqc_count_without_yarn_master_count(self):
        # No std count on the yarn master: out of the std column (count_missing)
        # but the SQC reading still counts in the actual column.
        rows = [_sp(1, "A", 10, "8 LB", 8.0, 1000.0, act=8.0),
                _sp(2, "A", 30, "NO STD", 0.0, 1000.0, act=12.0)]
        avg = build_spinning_section(rows, [], total_frames=2)["avg_count"]
        assert avg["by_production"] == 8.0
        assert avg["act_by_production"] == 10.0
        assert avg["count_missing"] == ["NO STD"]
        assert avg["act_fallback"] == []

    def test_neither_count_is_left_out_of_both(self):
        rows = [_sp(1, "A", 10, "8 LB", 8.0, 1000.0, act=8.2),
                _sp(2, "A", 30, "BLANK", 0.0, 1000.0)]
        avg = build_spinning_section(rows, [], total_frames=2)["avg_count"]
        assert avg["act_by_production"] == 8.2
        assert avg["count_missing"] == ["BLANK"]
        assert avg["act_fallback"] == []

    def test_empty_day(self):
        avg = build_spinning_section([], [], total_frames=4)["avg_count"]
        assert avg["act_by_frames"] is None
        assert avg["act_by_production"] is None
        assert avg["act_by_std_production"] is None
        assert avg["act_measured_kg"] == 0.0

    def test_sls_branch_29_2026_10_08(self):
        """sls co 2 br 29, 08-10-2026 (per-quality totals read from the slice):
        16-SKWP spun at 14.52 against a 16 lb standard, so the actual mill
        average by production is ~0.4 lb finer than the std-count figure."""
        q = [  # item, name, std, sqc, kg, frame-shifts, std kg
            (335563, "13-SKWP", 13.0, 13.4121429, 12707.85, 35, 17344.0),
            (335865, "16(96)-SKWP", 16.0, 15.3933333, 2805.2, 9, 3086.4),
            (335564, "16-SKWP", 16.0, 14.5191667, 12184.4, 30, 16447.6),
            (335985, "7.5 LBS-HSWP", 7.5, 7.99, 796.4, 5, 1324.0),
            (335961, "7.5 LBS-HSWT", 7.5, 7.82, 543.1, 3, 883.2),
            (335676, "9.5(TWINE)-HSWP", 9.5, 10.5, 1123.4, 5, 2058.8),
        ]
        rows = []
        for item, name, std, sqc, kg, fs, std_kg in q:
            rows += [_sp(item * 100 + i, "A", item, name, std, kg / fs, std_kg / fs, act=sqc)
                     for i in range(fs)]
        avg = build_spinning_section(rows, [], total_frames=100)["avg_count"]
        assert avg["by_production"] == 14.12  # what the page showed
        assert avg["act_by_production"] == 13.69
        assert avg["act_by_frames"] == 13.33  # std-count figure: 13.64
        assert avg["act_measured_kg"] == round(sum(r[4] for r in q), 3)
        assert avg["act_fallback"] == []

    # Owner ruling 2026-10-13: from Oct-2026 the actual figures are on the
    # moisture-corrected count; earlier months keep the observed count.
    def test_corrected_basis_reads_the_moisture_corrected_count(self):
        rows = [
            _sp(1, "A", 10, "8 LB", 8.0, 1000.0, 1000.0, act=8.4, corr=8.1),
            _sp(2, "A", 20, "16 LB", 16.0, 1000.0, 1000.0, act=15.0, corr=14.6),
        ]
        sp = build_spinning_section(rows, [], total_frames=2, count_basis="corrected")
        avg = sp["avg_count"]
        assert avg["count_basis"] == "corrected"
        assert avg["act_by_production"] == 11.35  # (8.1 + 14.6) / 2
        assert avg["act_by_frames"] == 11.35
        assert avg["act_by_std_production"] == 11.35
        # The quality table's Act Count column follows the same basis.
        assert {q["item_code"]: q["act_count"] for q in sp["quality"]} == {
            "8 LB": 8.1, "16 LB": 14.6}
        # Std column and std production / efficiency untouched.
        assert avg["by_production"] == 12.0
        assert sp["total"]["eff_pct"] == 100.0

    def test_observed_basis_is_the_default_and_ignores_corrected(self):
        rows = [_sp(1, "A", 10, "8 LB", 8.0, 1000.0, 1000.0, act=8.4, corr=8.1)]
        sp = build_spinning_section(rows, [], total_frames=1)
        assert sp["avg_count"]["count_basis"] == "observed"
        assert sp["avg_count"]["act_by_production"] == 8.4
        assert sp["quality"][0]["act_count"] == 8.4

    def test_corrected_missing_falls_back_to_observed(self):
        rows = [_sp(1, "A", 10, "8 LB", 8.0, 1000.0, 1000.0, act=8.4, corr=0.0)]
        avg = build_spinning_section(rows, [], total_frames=1, count_basis="corrected")["avg_count"]
        assert avg["act_by_production"] == 8.4
        assert avg["act_fallback"] == []

    def test_sqc_count_basis_switches_on_1_oct_2026(self):
        assert sqc_count_basis(date(2026, 9, 30)) == "observed"
        assert sqc_count_basis(date(2026, 10, 1)) == "corrected"
        assert sqc_count_basis(date(2027, 1, 15)) == "corrected"


class TestBuildWindingSection:
    def test_quality_rollup_and_winders(self):
        day = [
            {"item_id": 10, "item_code": "Y9", "item_name": "9 LB", "eb_id": 1,
             "production_kg": 120.0},
            {"item_id": 10, "item_code": "Y9", "item_name": "9 LB", "eb_id": 2,
             "production_kg": 80.0},
            # Same winder, second spell — distinct eb_id count stays 2.
            {"item_id": 10, "item_code": "Y9", "item_name": "9 LB", "eb_id": 1,
             "production_kg": 100.0},
        ]
        mtd = [{"item_id": 10, "production_kg": 3000.0, "winder_shifts": 30}]
        out = build_winding_section(day, mtd, set())
        q = out["qualities"][0]
        assert q["production_kg"] == 300.0
        assert q["winders"] == 2
        assert q["kg_per_winder"] == 150.0
        assert q["mtd_production_kg"] == 3000.0
        assert q["mtd_kg_per_winder_shift"] == 100.0
        assert out["total"]["production_kg"] == 300.0

    def test_mtd_only_quality_gets_zero_row(self):
        day = [{"item_id": 10, "item_code": "Y9", "item_name": "9 LB", "eb_id": 1,
                "production_kg": 100.0}]
        mtd = [
            {"item_id": 10, "item_code": "Y9", "item_name": "9 LB",
             "production_kg": 1000.0, "winder_shifts": 10},
            {"item_id": 11, "item_code": "Y8", "item_name": "8 LB",
             "production_kg": 500.0, "winder_shifts": 5},
        ]
        out = build_winding_section(day, mtd, set())
        by_code = {q["item_code"]: q for q in out["qualities"]}
        assert by_code["Y8"]["production_kg"] == 0.0
        assert by_code["Y8"]["winders"] == 0
        assert by_code["Y8"]["mtd_production_kg"] == 500.0
        # MTD column now sums to the total's MTD.
        assert sum(q["mtd_production_kg"] for q in out["qualities"]) == out["total"]["mtd_production_kg"]
        assert out["total"]["winders"] == 1

    def test_twisting_netted_out_of_totals(self):
        # TKT-000127: twisting has no module, so ply output is keyed as winding —
        # the same tonnage is booked once as single yarn and again as ply. Only
        # the TOTAL is reported net; the twisted quality's own row stays whole.
        day = [
            {"item_id": 10, "item_code": "Y9", "item_name": "9 LB", "eb_id": 1,
             "production_kg": 180.0},
            {"item_id": 10, "item_code": "Y9", "item_name": "9 LB", "eb_id": 2,
             "production_kg": 120.0},
            {"item_id": 20, "item_code": "Y9P", "item_name": "9 LB PLY", "eb_id": 3,
             "production_kg": 120.0},
        ]
        mtd = [
            {"item_id": 10, "item_code": "Y9", "item_name": "9 LB",
             "production_kg": 9000.0, "winder_shifts": 60},
            {"item_id": 20, "item_code": "Y9P", "item_name": "9 LB PLY",
             "production_kg": 2500.0, "winder_shifts": 25},
        ]
        out = build_winding_section(day, mtd, {20})

        total = out["total"]
        assert total["production_kg"] == 300.0  # 420 gross - 120 twisted
        assert total["twisting_kg"] == 120.0
        assert total["gross_production_kg"] == 420.0
        assert total["mtd_production_kg"] == 9000.0  # 11500 gross - 2500 twisted
        assert total["mtd_twisting_kg"] == 2500.0
        assert total["mtd_gross_production_kg"] == 11500.0
        # TKT-000174: every column of the winding total is winding ALONE —
        # 300 kg over its own 2 winders, not net kg beside a gross head count.
        assert total["winders"] == 2
        assert total["kg_per_winder"] == 150.0
        # The twisting winder is not dropped: they are counted in their own block.
        assert total["twisting"] == {
            "production_kg": 120.0, "winders": 1, "kg_per_winder": 120.0,
            "mtd_production_kg": 2500.0,
        }
        # Gross line: the only place winding and twisting are added together.
        assert total["gross"] == {
            "production_kg": 420.0, "winders": 3, "kg_per_winder": 140.0,
            "mtd_production_kg": 11500.0,
        }

        by_code = {q["item_code"]: q for q in out["qualities"]}
        assert by_code["Y9P"]["is_twisted"] == 1
        assert by_code["Y9P"]["production_kg"] == 120.0  # row left whole
        assert by_code["Y9P"]["mtd_production_kg"] == 2500.0
        assert by_code["Y9"]["is_twisted"] == 0
        assert out["twisting_available"] is True

    def test_no_twisting_flag_leaves_total_gross(self):
        # Tenant without the is_twisted migration: nothing could be split, so the
        # total is still gross — say so rather than let a 0 read as "no twisting".
        day = [{"item_id": 10, "item_code": "Y9", "item_name": "9 LB", "eb_id": 1,
                "production_kg": 420.0}]
        mtd = [{"item_id": 10, "production_kg": 11500.0, "winder_shifts": 85}]
        out = build_winding_section(day, mtd, set(), twisting_available=False)
        assert out["twisting_available"] is False
        assert out["total"]["twisting_kg"] == 0.0
        assert out["total"]["production_kg"] == out["total"]["gross_production_kg"] == 420.0
        assert out["total"]["mtd_production_kg"] == out["total"]["mtd_gross_production_kg"] == 11500.0
        assert out["qualities"][0]["is_twisted"] == 0
        # Nothing split: the twisting block is empty and gross == winding.
        assert out["total"]["twisting"]["production_kg"] == 0.0
        assert out["total"]["twisting"]["winders"] == 0
        assert out["total"]["gross"]["production_kg"] == 420.0
        assert out["total"]["gross"]["winders"] == out["total"]["winders"] == 1

    def test_sls_golden_case_18_sep(self):
        """TKT-000174, sls co 2 br 29 18-09-2026: 3PLY-YARN 642 kg by one winder.

        Winding total 20,518 kg / 61 winders; twisting 642 kg / 1 winder; gross
        21,160 kg / 62 winders. Blocks add up to the gross line and to nothing else.
        """
        day = [{"item_id": 1, "item_code": "13-SKWP", "item_name": "13-SKWP",
                "eb_id": i, "production_kg": 20518.0 / 61} for i in range(61)]
        day.append({"item_id": 9, "item_code": "3PLY-YARN", "item_name": "3PLY-YARN",
                    "eb_id": 900, "production_kg": 642.0})
        mtd = [
            {"item_id": 1, "item_code": "13-SKWP", "item_name": "13-SKWP",
             "production_kg": 342374.8, "winder_shifts": 1000},
            {"item_id": 9, "item_code": "3PLY-YARN", "item_name": "3PLY-YARN",
             "production_kg": 10809.0, "winder_shifts": 17},
        ]
        total = build_winding_section(day, mtd, {9})["total"]
        assert total["production_kg"] == 20518.0
        assert (total["winders"], total["kg_per_winder"]) == (61, 336.4)
        assert total["twisting"]["production_kg"] == 642.0
        assert total["twisting"]["mtd_production_kg"] == 10809.0
        assert total["gross"]["production_kg"] == 21160.0
        assert total["gross"]["winders"] == 62
        assert total["gross"]["kg_per_winder"] == 341.3
        assert total["mtd_production_kg"] + total["twisting"]["mtd_production_kg"] == total["gross"]["mtd_production_kg"]


class TestBuildWeavingSection:
    def test_quality_and_shift_pivot(self):
        # Stub rows mirror get_weaving_entries_by_date_query's REAL projection:
        # it emits spell_code but NOT shift_bucket (the builder derives the
        # bucket from spell_code's first letter).
        slice_rows = [
            {"weaving_quality_id": 1, "weaving_quality_code": "H1",
             "weaving_quality_name": "Hessian", "spell_code": "A1", "machine_id": 11,
             "production_kg": 1000.0, "production_yds": 500.0, "std_prod_yds": 1000.0,
             "ozs_yds": 10.0},
            {"weaving_quality_id": 1, "weaving_quality_code": "H1",
             "weaving_quality_name": "Hessian", "spell_code": "B2", "machine_id": 11,
             "production_kg": 500.0, "production_yds": 250.0, "std_prod_yds": 500.0,
             "ozs_yds": 10.0},
        ]
        mtd = [{"weaving_quality_id": 1, "production_mt": 40.0}]
        out = build_weaving_section(slice_rows, mtd, total_looms=10)
        q = out["qualities"][0]
        assert q["production_mt"] == 1.5
        assert q["shift_A_mt"] == 1.0
        assert q["shift_B_mt"] == 0.5
        assert q["looms_run"] == 1
        assert q["eff_pct"] == 50.0
        assert q["mtd_production_mt"] == 40.0
        assert out["total"]["loom_runs"] == 1
        assert out["total"]["looms_run"] == 1
        assert out["total"]["utilization_pct"] == 10.0
        assert out["shift_totals"]["A"] == 1.0
        # std capacity: (1000 + 500) yds * 10 oz/yd * 28.35 / 1000
        assert out["total"]["std_capacity_kg"] == round(1500 * 10 * 28.35 / 1000, 1)

    def test_null_quality_skipped_and_mtd_only_quality_row(self):
        slice_rows = [
            # Loom with no quality mapped: slice LEFT JOIN yields NULL id/code, 0 kg.
            {"weaving_quality_id": None, "weaving_quality_code": None,
             "weaving_quality_name": None, "spell_code": "A1", "machine_id": 12,
             "production_kg": 0.0, "production_yds": 0.0, "std_prod_yds": 0.0,
             "ozs_yds": None},
            {"weaving_quality_id": 1, "weaving_quality_code": "H1",
             "weaving_quality_name": "Hessian", "spell_code": "A1", "machine_id": 11,
             "production_kg": 1000.0, "production_yds": 500.0, "std_prod_yds": 1000.0,
             "ozs_yds": 10.0},
        ]
        mtd = [
            {"weaving_quality_id": 1, "weaving_quality_code": "H1",
             "weaving_quality_name": "Hessian", "production_mt": 1.0},
            {"weaving_quality_id": 2, "weaving_quality_code": "S1",
             "weaving_quality_name": "Sacking", "production_mt": 2.0},
        ]
        out = build_weaving_section(slice_rows, mtd, total_looms=10)
        codes = [q["quality_code"] for q in out["qualities"]]
        assert None not in codes and codes == ["H1", "S1"]
        s1 = out["qualities"][1]
        assert s1["production_mt"] == 0.0 and s1["looms_run"] == 0
        assert s1["mtd_production_mt"] == 2.0
        assert sum(q["mtd_production_mt"] for q in out["qualities"]) == out["total"]["mtd_production_mt"]
        assert out["total"]["looms_run"] == 1

    def test_missing_standard_gives_null_eff_not_zero(self):
        # Same TKT-000116 rule as spinning: no std_prod_yds configured for the
        # quality -> blank, never a confident 0.0%.
        slice_rows = [
            {"weaving_quality_id": 1, "weaving_quality_code": "H1",
             "weaving_quality_name": "Hessian", "spell_code": "A1", "machine_id": 11,
             "production_kg": 1000.0, "production_yds": 500.0, "std_prod_yds": 0.0,
             "ozs_yds": 10.0},
            {"weaving_quality_id": 2, "weaving_quality_code": "S1",
             "weaving_quality_name": "Sacking", "spell_code": "A1", "machine_id": 12,
             "production_kg": 800.0, "production_yds": 400.0, "std_prod_yds": 500.0,
             "ozs_yds": 16.0},
        ]
        out = build_weaving_section(slice_rows, [], total_looms=10)
        by_code = {q["quality_code"]: q for q in out["qualities"]}
        assert by_code["H1"]["eff_pct"] is None  # not 0.0
        assert by_code["S1"]["eff_pct"] == 80.0  # 400 / 500
        # Same measured-subset rule as spinning: only S1's standardised 400 yds
        # reach the numerator, so the total is 80% — not the 180% you get by
        # dividing all 900 yds by the one configured quality's standard.
        assert out["total"]["eff_pct"] == round(400.0 * 100 / 500.0, 2) == 80.0
        # And weaving now names its unconfigured qualities too.
        assert out["standards_missing"] == ["Hessian"]

    def test_total_eff_null_when_no_quality_has_a_standard(self):
        slice_rows = [
            {"weaving_quality_id": 1, "weaving_quality_code": "H1",
             "weaving_quality_name": "Hessian", "spell_code": "A1", "machine_id": 11,
             "production_kg": 1000.0, "production_yds": 500.0, "std_prod_yds": 0.0,
             "ozs_yds": 10.0},
        ]
        out = build_weaving_section(slice_rows, [], total_looms=10)
        assert out["total"]["eff_pct"] is None


class TestBuildEmulsionSection:
    # R-08-02 emulsion log: oil_pct_in_emulsion IS the JBO/Ecofin % the DPR wants
    # (TKT-000116). Realistic sls figures: 1000 L tank, ~150 L oil, band 15-16%.
    BASE = {
        "day_jbo_ltr": 120.0, "day_eco_fin_ltr": 30.0, "day_oil_used_ltr": 150.0,
        "day_tank_capacity_ltr": 1000.0, "day_oil_pct": 15.5,
        "day_std_low": 15.0, "day_std_high": 16.0, "day_rows": 2,
        "mtd_jbo_ltr": 2880.0, "mtd_eco_fin_ltr": 720.0,
        "mtd_avg_oil_pct": 15.4, "mtd_days": 24,
    }

    def test_status_ok_inside_band(self):
        out = build_emulsion_section(_row({**self.BASE, "day_oil_pct": 15.5}))
        assert out["day"]["status"] == "OK"
        assert out["day"]["oil_pct"] == 15.5
        # Measured and tank-derived ship side by side — they disagree in the
        # live data and picking one would hide a recipe-vs-tank discrepancy.
        assert out["day"]["derived_oil_pct"] == 15.0
        assert out["mtd"]["avg_oil_pct"] == 15.4
        assert out["mtd"]["days"] == 24

    def test_status_low_below_band(self):
        out = build_emulsion_section(_row({**self.BASE, "day_oil_pct": 14.2}))
        assert out["day"]["status"] == "LOW"

    def test_status_high_above_band(self):
        out = build_emulsion_section(_row({**self.BASE, "day_oil_pct": 17.1}))
        assert out["day"]["status"] == "HIGH"

    def test_none_row_gives_none(self):
        assert build_emulsion_section(None) is None

    def test_no_mtd_days_gives_none(self):
        # Aggregate query always returns one row; an empty window is mtd_days 0.
        assert build_emulsion_section(_row({**self.BASE, "mtd_days": 0})) is None

    def test_no_day_rows_keeps_mtd(self):
        out = build_emulsion_section(_row({**self.BASE, "day_rows": 0}))
        assert out["day"] is None
        assert out["mtd"]["jbo_ltr"] == 2880.0
        assert out["mtd"]["days"] == 24


class TestBuildYarnHeavyLight:
    def test_verdicts_with_dead_band(self):
        rows = [
            # +0.06 lb deviation — inside the ±0.2 lb R-08-16 dead band -> OK,
            # but the percentage is still reported.
            {"item_id": 1, "item_code": "Y88", "item_name": "8.8 LB",
             "std_count": 8.8, "obs_count": 8.99, "corr_count": 8.86,
             "day_tests": 3, "mtd_obs_count": 8.9, "mtd_corr_count": 8.85},
            # -1.51 lb — well beyond the band -> LIGHT.
            {"item_id": 2, "item_code": "Y10", "item_name": "10 LB",
             "std_count": 10.0, "obs_count": 8.62, "corr_count": 8.49,
             "day_tests": 2, "mtd_obs_count": None, "mtd_corr_count": None},
            # No corrected value -> falls back to observed; zero deviation.
            {"item_id": 3, "item_code": "Y95", "item_name": "9.5 LB",
             "std_count": 9.5, "obs_count": 9.5, "corr_count": None,
             "day_tests": 1, "mtd_obs_count": None, "mtd_corr_count": None},
            # +0.55 lb — beyond the band -> HEAVY.
            {"item_id": 4, "item_code": "Y9", "item_name": "9 LB",
             "std_count": 9.0, "obs_count": 9.7, "corr_count": 9.55,
             "day_tests": 2, "mtd_obs_count": 9.6, "mtd_corr_count": 9.5},
        ]
        out = build_yarn_heavy_light(rows)
        assert out[0]["remark"] == "OK"
        assert out[0]["hy_lt_pct"] == round((8.86 - 8.8) / 8.8 * 100, 2)
        assert out[1]["remark"] == "LIGHT"
        assert out[2]["remark"] == "OK"
        assert out[2]["hy_lt_pct"] == 0.0
        assert out[3]["remark"] == "HEAVY"


def _day(d, kg, shifts):
    return {"tran_date": d, "production_kg": kg, "shifts": shifts}


# sls co 2 br 29 spinning kg per day, 04-09 .. 13-09-2026 (13-09 = Sunday, shift A).
SLS_WEEK = [
    _day(date(2026, 9, 4), 18802.4, 3),
    _day(date(2026, 9, 5), 20907.4, 3),
    _day(date(2026, 9, 6), 3989.7, 1),
    _day(date(2026, 9, 7), 21279.0, 3),
    _day(date(2026, 9, 8), 21827.1, 3),
    _day(date(2026, 9, 9), 21983.0, 3),
    _day(date(2026, 9, 10), 21722.3, 3),
    _day(date(2026, 9, 11), 22764.2, 3),
    _day(date(2026, 9, 12), 25077.1, 3),
    _day(date(2026, 9, 13), 5766.8, 1),
]


class TestPickCoverBasis:
    """TKT-000172 — the spinning production the jute cover divides by."""

    def test_report_date_when_it_was_a_full_day(self):
        rows = [
            _day(date(2026, 9, 16), 23888.9, 3),
            _day(date(2026, 9, 17), 24497.3, 3),
            _day(date(2026, 9, 18), 20137.6, 3),
        ]
        out = pick_cover_basis(rows, date(2026, 9, 18))
        assert out["mode"] == "report_date"
        assert out["date"] == out["date_from"] == out["date_to"] == "2026-09-18"
        assert out["days"] == 1
        assert out["production_kg"] == 20137.6
        assert out["is_report_date"] is True
        assert out["basis_label"] == "spinning production"

    def test_one_shift_sunday_uses_the_seven_producing_day_average(self):
        # Owner's rule: a part day divides by the mill's weekly run rate, the
        # part day itself counted as it is.
        out = pick_cover_basis(SLS_WEEK, date(2026, 9, 13))
        assert out["mode"] == "average"
        assert out["is_report_date"] is False
        assert (out["date_from"], out["date_to"], out["days"]) == ("2026-09-07", "2026-09-13", 7)
        # (21279.0 + 21827.1 + 21983.0 + 21722.3 + 22764.2 + 25077.1 + 5766.8) / 7
        assert out["production_kg"] == round(140419.5 / 7, 3) == 20059.929
        assert out["basis_label"] == "average daily spinning production"

    def test_holiday_with_no_production_averages_the_days_before_it(self):
        rows = [r for r in SLS_WEEK if r["tran_date"] != date(2026, 9, 13)]
        rows.append(_day(date(2026, 9, 13), 0.0, 0))  # a zero row is no production
        out = pick_cover_basis(rows, date(2026, 9, 13))
        assert out["mode"] == "average"
        # Seven PRODUCING days ending at the last one before the closed day
        # (the earlier one-shift Sunday 06-09 is counted as it is).
        assert (out["date_from"], out["date_to"], out["days"]) == ("2026-09-06", "2026-09-12", 7)
        # (3989.7 + 21279.0 + 21827.1 + 21983.0 + 21722.3 + 22764.2 + 25077.1) / 7
        assert out["production_kg"] == round(138642.4 / 7, 3) == 19806.057

    def test_fewer_than_seven_producing_days_averages_what_there_is(self):
        rows = [_day(date(2026, 9, 11), 22000.0, 3), _day(date(2026, 9, 12), 24000.0, 3),
                _day(date(2026, 9, 13), 5000.0, 1)]
        out = pick_cover_basis(rows, date(2026, 9, 13))
        assert out["mode"] == "average"
        assert out["days"] == 3
        assert out["production_kg"] == 17000.0

    def test_step_down_in_shifts_is_not_treated_as_part_days(self):
        # Mill moved from 3 shifts to 2: "usual" is the window's most common
        # count, so the 2-shift report date is a full day once that is the norm.
        rows = [
            _day(date(2026, 9, 1), 21000.0, 3),
            _day(date(2026, 9, 2), 21000.0, 3),
            _day(date(2026, 9, 3), 14000.0, 2),
            _day(date(2026, 9, 4), 14100.0, 2),
            _day(date(2026, 9, 5), 14200.0, 2),
        ]
        out = pick_cover_basis(rows, date(2026, 9, 5))
        assert out["mode"] == "report_date"
        assert out["production_kg"] == 14200.0

    def test_rows_after_the_report_date_are_ignored(self):
        rows = [_day(date(2026, 9, 17), 24497.3, 3), _day(date(2026, 9, 19), 19137.5, 3)]
        out = pick_cover_basis(rows, date(2026, 9, 18))
        # 18-09 itself has no production -> average of what precedes it.
        assert out["mode"] == "average"
        assert (out["date_to"], out["days"], out["production_kg"]) == ("2026-09-17", 1, 24497.3)

    def test_no_production_in_window_is_none(self):
        assert pick_cover_basis([], date(2026, 9, 18)) is None
        assert pick_cover_basis([_day(date(2026, 9, 18), 0.0, 0)], date(2026, 9, 18)) is None


class TestBuildJuteSection:
    ROWS = [
        {"item_group_name": "Raw Jute", "opening_weight": 50000.0,
         "receipt_weight": 17000.0, "issue_weight": 8000.0, "sold_weight": 0.0,
         "closing_weight": 59000.0, "mtd_receipt_weight": 100000.0,
         "mtd_issue_weight": 240000.0, "is_waste": 0},
        {"item_group_name": "Raw Jute", "opening_weight": 1000.0,
         "receipt_weight": 0.0, "issue_weight": 176.0, "sold_weight": 0.0,
         "closing_weight": 824.0, "mtd_receipt_weight": 0.0,
         "mtd_issue_weight": 0.0, "is_waste": 0},
    ]
    BASIS = {"mode": "report_date", "date": "2026-01-24", "date_from": "2026-01-24",
             "date_to": "2026-01-24", "days": 1, "production_kg": 20000.0, "shifts": 3,
             "is_report_date": True, "basis_label": "spinning production"}

    def test_totals_and_days_stock_on_production(self):
        out = build_jute_section(self.ROWS, num_days=24, cover_basis=self.BASIS)
        assert out["totals"]["closing_weight"] == 59824.0
        assert len(out["groups"]) == 1
        # TKT-000172: total stock / previous working day's spinning production.
        assert out["days_stock"] == round(59824.0 / 20000.0, 1) == 3.0
        assert out["cover_basis"] == self.BASIS
        # The superseded MTD-issue-rate figure still ships for comparison.
        assert out["days_stock_mtd_issue"] == round(59824.0 / (240000.0 / 24), 1)

    def test_sls_golden_case_18_sep(self):
        """sls co 2 br 29, 18-09-2026: 125,494 kg / 20,137.6 kg = 6.2 days.

        The MTD-issue basis read 11.7 because no jute issue was keyed after
        14-09, so 193,779 kg was spread over 18 calendar days.
        """
        rows = [
            {"item_group_name": "JUTE > DESI", "closing_weight": 75883.0,
             "mtd_issue_weight": 193779.0, "is_waste": 0},
            {"item_group_name": "Jute Waste", "closing_weight": 49611.0,
             "mtd_issue_weight": 0.0, "is_waste": 1},
        ]
        basis = {**self.BASIS, "date": "2026-09-18", "production_kg": 20137.6}
        out = build_jute_section(rows, num_days=18, cover_basis=basis)
        assert out["totals"]["closing_weight"] == 125494.0
        assert out["days_stock"] == 6.2
        assert out["days_stock_mtd_issue"] == 11.7
        # Cover on purchased raw jute alone, process waste left out.
        assert out["waste_closing_weight"] == 49611.0
        assert out["raw_jute_closing_weight"] == 75883.0
        assert out["days_stock_raw_jute"] == 3.8
        # Owner: waste stays in the total but is stated separately from jute.
        assert out["days_stock_waste"] == 2.5
        assert out["jute_totals"]["closing_weight"] == 75883.0
        assert out["jute_totals"]["mtd_issue_weight"] == 193779.0
        assert out["waste_totals"]["closing_weight"] == 49611.0
        assert [(g["item_group_name"], g["is_waste"]) for g in out["groups"]] == [
            ("JUTE > DESI", False),
            ("Jute Waste", True),
        ]

    def test_sls_golden_case_sunday_13_sep(self):
        """sls co 2 br 29, Sunday 13-09-2026 (shift A only, 5,766.8 kg).

        Divisor = 7-producing-day average 140,419.5 / 7 = 20,059.929 kg/day.
        77,634 / 20,059.929 = 3.87 -> 3.9 days; raw jute 39,327 / 20,059.929 =
        1.96 -> 2.0. Dividing by the Sunday alone would have read 13.5 days.
        """
        rows = [
            {"item_group_name": "JUTE > DESI", "closing_weight": 39327.0,
             "mtd_issue_weight": 174505.0, "is_waste": 0},
            {"item_group_name": "Jute Waste", "closing_weight": 38307.0,
             "mtd_issue_weight": 0.0, "is_waste": 1},
        ]
        basis = pick_cover_basis(SLS_WEEK, date(2026, 9, 13))
        out = build_jute_section(rows, num_days=13, cover_basis=basis)
        assert out["totals"]["closing_weight"] == 77634.0
        assert out["days_stock"] == 3.9
        assert out["days_stock_raw_jute"] == 2.0  # same divisor
        assert out["cover_basis"]["mode"] == "average"
        assert out["days_stock_mtd_issue"] == 5.8  # superseded figure, unchanged

    def test_zero_days_guard(self):
        out = build_jute_section([], num_days=0)
        assert out["days_stock"] is None
        assert out["days_stock_mtd_issue"] is None

    def test_no_production_basis_gives_null_days_stock(self):
        out = build_jute_section(self.ROWS, num_days=10, cover_basis=None)
        assert out["totals"]["closing_weight"] == 59824.0
        assert out["days_stock"] is None  # not 0.0 "no cover"
        assert out["days_stock_raw_jute"] is None
        assert out["cover_basis"] is None

    def test_no_mtd_issue_no_longer_blanks_the_cover(self):
        rows = [{"item_group_name": "Raw Jute", "opening_weight": 100.0,
                 "receipt_weight": 0.0, "issue_weight": 0.0, "sold_weight": 0.0,
                 "closing_weight": 100.0, "mtd_receipt_weight": 0.0,
                 "mtd_issue_weight": 0.0}]
        out = build_jute_section(rows, num_days=10, cover_basis={**self.BASIS, "production_kg": 50.0})
        assert out["days_stock"] == 2.0
        assert out["days_stock_mtd_issue"] is None


class TestBuildHandsSection:
    def test_shift_order_and_totals(self):
        rows = [
            {"shift_bucket": "C", "day_hours": 15.0, "day_hands": 2.0,
             "mtd_hours": 150.0, "mtd_hands": 20.0},
            {"shift_bucket": "A", "day_hours": 2862.0, "day_hands": 357.75,
             "mtd_hours": 40000.0, "mtd_hands": 5000.0},
        ]
        out = build_hands_section(rows)
        assert [s["shift"] for s in out["shifts"]] == ["A", "C"]
        assert out["total"]["day_hands"] == 359.75

    def test_hands_query_uses_flat_8h_norm(self):
        # Every shift is 8h — the old 7.5h C-shift norm over-counted C workers
        # (their rows carry working_hours = 8 → 1.07 hands each).
        from src.juteProduction.dprQueries import get_dpr_hands_query

        sql = str(get_dpr_hands_query())
        assert "7.5" not in sql
        assert sql.count("/ 8") == 2  # day_hands + mtd_hands


class TestBuildShiftsWorked:
    """TKT-000152 — shifts worked today and to date."""

    ROWS = [
        {"section": "Spinning", "tran_date": date(2026, 8, 25), "shift_bucket": "A"},
        {"section": "Spinning", "tran_date": date(2026, 8, 25), "shift_bucket": "B"},
        {"section": "Spinning", "tran_date": date(2026, 8, 24), "shift_bucket": "A"},
        {"section": "Weaving", "tran_date": date(2026, 8, 25), "shift_bucket": "A"},
        {"section": "Weaving", "tran_date": date(2026, 8, 24), "shift_bucket": "A"},
        {"section": "Winding", "tran_date": date(2026, 8, 24), "shift_bucket": "C"},
    ]

    def test_mill_total_is_union_not_sum(self):
        out = build_shifts_worked(self.ROWS, date(2026, 8, 25))
        # Spinning A+B and weaving A on the day -> 2 worked shifts, not 3.
        assert out["day"] == 2
        assert out["day_shifts"] == ["A", "B"]
        # MTD pairs: (24,A), (24,C), (25,A), (25,B).
        assert out["mtd"] == 4

    def test_per_section_counts_and_order(self):
        out = build_shifts_worked(self.ROWS, date(2026, 8, 25))
        assert [s["section"] for s in out["sections"]] == [
            "Spinning",
            "Winding",
            "Weaving",
        ]
        by = {s["section"]: s for s in out["sections"]}
        assert (by["Spinning"]["day"], by["Spinning"]["mtd"]) == (2, 3)
        assert (by["Weaving"]["day"], by["Weaving"]["mtd"]) == (1, 2)
        # Winding ran only on the 24th — nothing today.
        assert (by["Winding"]["day"], by["Winding"]["mtd"]) == (0, 1)
        assert by["Winding"]["day_shifts"] == []

    def test_empty_rows(self):
        out = build_shifts_worked([], date(2026, 8, 25))
        assert out == {"day": 0, "mtd": 0, "day_shifts": [], "sections": []}

    def test_unresolved_spell_still_counts_and_sorts_last(self):
        out = build_shifts_worked(
            [
                {"section": "Spinning", "tran_date": date(2026, 8, 25), "shift_bucket": "?"},
                {"section": "Spinning", "tran_date": date(2026, 8, 25), "shift_bucket": "B"},
            ],
            date(2026, 8, 25),
        )
        assert out["day"] == 2
        assert out["day_shifts"] == ["B", "?"]

    def test_query_scopes_every_section_leg(self):
        from src.juteProduction.dprQueries import get_dpr_shifts_worked_query

        sql = str(get_dpr_shifts_worked_query())
        for table in (
            "daily_doff_tbl",
            "jute_prod_winding_doff",
            "jute_prod_weaving_daily",
            "jute_prod_finishing_daily",
        ):
            assert table in sql
        # Every leg carries the co scope and the nullable-branch guard.
        assert sql.count(":co_id") == 4
        assert sql.count(":branch_id IS NULL") == 4
        # A1/A2 collapse to A, matching the DPR shift_bucket convention.
        assert sql.count("LEFT(sp.spell_code, 1)") == 4


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


class TestDailyProductionReportEndpoint:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_missing_co_id_400(self):
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"report_date": "2026-01-24"},
        )
        assert resp.status_code == 400
        assert "co_id" in resp.json()["detail"]

    def test_missing_report_date_400(self):
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": 2},
        )
        assert resp.status_code == 400
        assert "report_date" in resp.json()["detail"]

    def test_invalid_report_date_400(self):
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": 2, "report_date": "24-01-2026"},
        )
        assert resp.status_code == 400

    def test_invalid_co_id_400(self):
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": "abc", "report_date": "2026-01-24"},
        )
        assert resp.status_code == 400
        assert "co_id" in resp.json()["detail"]

    def test_branch_not_in_company_400(self):
        pair_check = MagicMock()
        pair_check.first.return_value = None  # branch not under this co
        self._session.execute.return_value = pair_check
        self._session.execute.side_effect = None
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": 1, "branch_id": 42, "report_date": "2026-01-24"},
        )
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_db_error_500(self):
        self._session.execute.side_effect = RuntimeError("db exploded")
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": 2, "report_date": "2026-01-24"},
        )
        assert resp.status_code == 500
        assert "db exploded" in resp.json()["detail"]

    def test_happy_path_with_branch(self):
        # db.execute results in the endpoint's call order:
        self._session.execute.side_effect = [
            _first_result({"1": 1}),  # branch-in-company validation
            _result([  # machine counts
                {"machine_type_name": "Spinning", "total_machines": 4},
                {"machine_type_name": "Loom", "total_machines": 10},
            ]),
            _result([  # spinning day slice
                {"shift_bucket": "A", "machine_id": 1, "item_id": 10,
                 "item_code": "Y9", "item_name": "9 LB", "act_prod_doff": 600.0,
                 "std_prod": 1000.0, "target_prod": 900.0, "act_count": 9.3,
                 "std_count": 9.0, "act_prod_wind": 400.0},
            ]),
            _result([  # spinning MTD
                {"shift_bucket": "A", "production_kg": 6000.0, "frame_runs": 20},
            ]),
            _result([  # winding day reconciliation
                {"item_id": 10, "item_code": "Y9", "item_name": "9 LB",
                 "eb_id": 7, "production_kg": 350.0},
            ]),
            _result([  # winding MTD
                {"item_id": 10, "item_code": "Y9", "item_name": "9 LB",
                 "production_kg": 9000.0, "winder_shifts": 90},
            ]),
            _result([]),  # twisted yarn item ids (none flagged)
            _result([]),  # weaving day sync/lock headers (none -> one live slice call)
            _result([  # weaving day slice (real projection: spell_code, no shift_bucket)
                {"weaving_quality_id": 1, "weaving_quality_code": "H1",
                 "weaving_quality_name": "Hessian", "spell_code": "A1",
                 "machine_id": 11, "production_kg": 2000.0,
                 "production_yds": 1000.0, "std_prod_yds": 2000.0,
                 "ozs_yds": 10.0},
            ]),
            _result([  # weaving MTD
                {"weaving_quality_id": 1, "production_mt": 50.0},
            ]),
            _result([  # finishing
                {"process": "balepress", "prod_uom": "bale", "day_qty": 16.0,
                 "day_wt_kg": 2400.0, "mtd_qty": 272.0, "mtd_wt_kg": 40000.0},
            ]),
            _first_result({  # emulsion (read with .first(), not fetchall)
                "day_jbo_ltr": 120.0, "day_eco_fin_ltr": 30.0,
                "day_oil_used_ltr": 150.0, "day_tank_capacity_ltr": 1000.0,
                "day_oil_pct": 15.5, "day_std_low": 15.0, "day_std_high": 16.0,
                "day_rows": 2, "mtd_jbo_ltr": 2880.0, "mtd_eco_fin_ltr": 720.0,
                "mtd_avg_oil_pct": 15.4, "mtd_days": 24,
            }),
            _result([  # yarn heavy-light
                {"item_id": 10, "item_code": "Y9", "item_name": "9 LB",
                 "std_count": 9.0, "obs_count": 9.7, "corr_count": 9.55,
                 "day_tests": 2, "mtd_obs_count": 9.6, "mtd_corr_count": 9.5},
            ]),
            _result([  # jute stock
                {"item_group_name": "Raw Jute", "opening_weight": 50000.0,
                 "receipt_weight": 17000.0, "issue_weight": 8176.0,
                 "sold_weight": 0.0, "closing_weight": 58824.0,
                 "mtd_receipt_weight": 100000.0, "mtd_issue_weight": 196224.0},
            ]),
            _result([  # spinning production per day (jute cover basis, TKT-000172)
                {"tran_date": date(2026, 1, 23), "production_kg": 19000.0, "shifts": 3},
                {"tran_date": date(2026, 1, 24), "production_kg": 600.0, "shifts": 3},
            ]),
            _result([  # hands
                {"shift_bucket": "A", "day_hours": 2862.0, "day_hands": 357.75,
                 "mtd_hours": 40000.0, "mtd_hands": 5000.0},
            ]),
            _result([  # hands by department (TKT-000185): 200 + 150 direct, 7.75 common
                {"dept_id": 17, "dept_desc": "SPINNING(EJM)", "day_hands": 200.0,
                 "mtd_hands": 3000.0},
                {"dept_id": 21, "dept_desc": "WEAVING(EJM)", "day_hands": 150.0,
                 "mtd_hands": 1800.0},
                {"dept_id": 29, "dept_desc": "GENERAL(EJM)", "day_hands": 7.75,
                 "mtd_hands": 200.0},
            ]),
            _result([  # shifts worked (section, date, bucket triples)
                {"section": "Spinning", "tran_date": date(2026, 1, 24), "shift_bucket": "A"},
                {"section": "Spinning", "tran_date": date(2026, 1, 24), "shift_bucket": "B"},
                {"section": "Spinning", "tran_date": date(2026, 1, 23), "shift_bucket": "A"},
                {"section": "Weaving", "tran_date": date(2026, 1, 24), "shift_bucket": "A"},
            ]),
            _first_result({"day_kg": 500.0, "mtd_kg": 5000.0}),  # purchased yarn issued
            _first_result({  # power readings (ITM-004) — the LAST execute of the sheet
                "day_kwh": 18420.0, "day_dg_kwh": 900.0, "day_dg_hours": 2.5,
                "day_dg_litres": 310.0, "day_rows": 2, "mtd_kwh": 400000.0,
                "mtd_dg_kwh": 12000.0, "mtd_dg_hours": 40.0, "mtd_dg_litres": 5000.0,
                "mtd_days": 22, "mtd_keyed_cloth_mt": 50.0, "mtd_keyed_yarn_kg": 6000.0,
            }),
        ]
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": 2, "branch_id": 4, "report_date": "2026-01-24"},
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]

        assert data["report_date"] == "2026-01-24"
        assert data["date_from"] == "2026-01-01"
        assert data["spinning"]["total"]["production_kg"] == 600.0
        assert data["spinning"]["total_frames"] == 4
        assert data["winding"]["total"]["production_kg"] == 350.0
        assert data["weaving"]["total"]["production_mt"] == 2.0
        assert data["weaving"]["total_looms"] == 10
        # Shift pivot derives from spell_code ('A1' -> A).
        assert data["weaving"]["qualities"][0]["shift_A_mt"] == 2.0
        assert data["weaving"]["shift_totals"]["A"] == 2.0
        assert data["finishing"]["processes"][0]["process"] == "balepress"
        # +0.55 lb over std — beyond the ±0.2 dead band.
        assert data["yarn_heavy_light"][0]["remark"] == "HEAVY"
        assert data["jute"]["totals"]["closing_weight"] == 58824.0
        assert data["hands"]["total"]["day_hands"] == 357.75
        assert data["emulsion"]["day"]["status"] == "OK"
        assert data["emulsion"]["mtd"]["days"] == 24
        # KPI wiring
        assert data["kpi"]["spinning_mt"] == 0.6
        assert data["kpi"]["weaving_mt"] == 2.0
        assert data["kpi"]["winding_kg"] == 350.0
        assert data["kpi"]["winding_mtd_kg"] == 9000.0
        assert data["kpi"]["twisting_kg"] == 0.0
        assert data["kpi"]["jbo_pct"] == 15.5
        # Raw stored unit — deliberately NOT converted to MT (unit varies by tenant).
        assert data["kpi"]["jute_closing_weight"] == 58824.0
        # TKT-000172: closing stock / the report day's own spinning production.
        assert data["jute"]["cover_basis"]["date"] == "2026-01-24"
        assert data["kpi"]["jute_days_stock"] == round(58824.0 / 600.0, 1)
        assert data["kpi"]["jute_cover_basis_kg"] == 600.0
        assert data["kpi"]["jute_cover_basis_date"] == "2026-01-24"
        # Hands/MT stage-wise (TKT-000185): common 7.75 spread 200:150, yarn
        # stage over spinning 0.6 MT, cloth stage over weaving 2.0 MT, headline
        # the sum — not all 357.75 hands over spinning alone (596.25).
        share = 200.0 / 350.0
        yarn_hpm = round((200.0 + 7.75 * share) / 0.6, 2)
        cloth_hpm = round((150.0 + 7.75 * (1 - share)) / 2.0, 2)
        assert data["kpi"]["hands_stage_basis"] is True
        assert data["kpi"]["hands_per_mt_yarn"] == yarn_hpm
        assert data["kpi"]["hands_per_mt_cloth"] == cloth_hpm
        assert data["kpi"]["hands_per_mt"] == round(yarn_hpm + cloth_hpm, 2)
        assert data["kpi"]["hands_per_mt"] != round(357.75 / 0.6, 2)
        # The previous figure rides along for continuity (owner, 2026-10-11).
        assert data["kpi"]["hands_per_mt_spinning_basis"] == round(357.75 / 0.6, 2)
        assert data["hands"]["spinning_basis"]["day_mt"] == 0.6
        assert data["hands"]["spinning_basis"]["mtd_hands_per_mt"] == round(5000.0 / 6.0, 2)
        # MTD: 3000 + 200 * 3000/4800 over 6.0 MT, 1800 + 200 * 1800/4800 over 50 MT.
        mtd_share = 3000.0 / 4800.0
        assert data["kpi"]["hands_per_mt_mtd"] == round(
            round((3000.0 + 200.0 * mtd_share) / 6.0, 2)
            + round((1800.0 + 200.0 * (1 - mtd_share)) / 50.0, 2),
            2,
        )
        stages = data["hands"]["stages"]
        assert stages["available"] is True
        assert stages["yarn"]["depts"] == ["SPINNING(EJM)"]
        assert stages["cloth"]["depts"] == ["WEAVING(EJM)"]
        assert stages["common"]["depts"] == ["GENERAL(EJM)"]
        assert stages["total"]["day_hands"] == 357.75  # every hand, once
        # Yarn balance: spun 0.6 + purchased 0.5 − woven 2.0 = −0.9 MT.
        yb = data["hands"]["yarn_balance"]
        assert yb["purchased_available"] is True
        assert yb["day"] == {"spun_mt": 0.6, "purchased_issued_mt": 0.5,
                             "woven_mt": 2.0, "surplus_mt": -0.9}
        assert yb["mtd"]["purchased_issued_mt"] == 5.0
        assert yb["mtd"]["surplus_mt"] == round(6.0 + 5.0 - 50.0, 3)
        # Machine-run analysis: spinning std basis — avg std/frame 1000 kg,
        # 600 kg produced -> should_run 0.6 vs 1 actual run -> excess 0.4.
        sp_std = next(
            m for m in data["machine_run"]
            if m["section"] == "Spinning" and m["basis"] == "std"
        )
        assert sp_std["should_run"] == 0.6
        assert sp_std["excess_machines"] == 0.4
        wv_std = next(
            m for m in data["machine_run"] if m["section"] == "Weaving"
        )
        # capacity 2000 yds * 10 oz * 28.35/1000 = 567 kg per the single run.
        assert wv_std["avg_per_machine"] == 567.0
        # Data-capture gaps declared
        gaps = [g["section"] for g in data["unavailable"]]
        assert "Twisting Production" in gaps
        # Shifts worked: mill = union over sections (spinning A+B, weaving A
        # on the day = 2 shifts, not 3), MTD = distinct (date, shift) pairs.
        assert data["shifts_worked"]["day"] == 2
        assert data["shifts_worked"]["day_shifts"] == ["A", "B"]
        assert data["shifts_worked"]["mtd"] == 3
        assert data["kpi"]["shifts_worked"] == 2
        assert data["kpi"]["shifts_worked_mtd"] == 3
        sp_shifts = next(
            x for x in data["shifts_worked"]["sections"] if x["section"] == "Spinning"
        )
        assert (sp_shifts["day"], sp_shifts["mtd"]) == (2, 3)
        # Power (ITM-004): day kWh over the day's 2.0 MT of cloth, MTD over the
        # 50 MT woven on the KEYED days (carried on the row, not the month's total).
        power = data["power"]
        assert power["keyed"] is True
        assert power["day"]["kwh"] == 18420.0
        assert power["day"]["grid_kwh"] == 17520.0 and power["day"]["dg_kwh"] == 900.0
        assert power["day"]["kwh_per_t_cloth"] == round(18420.0 / 2.0, 2)
        assert power["day"]["kwh_per_t_yarn"] == round(18420.0 / 0.6, 2)
        assert power["mtd"]["days"] == 22
        assert power["mtd"]["kwh_per_t_cloth"] == round(400000.0 / 50.0, 2)
        assert power["mtd"]["kwh_per_t_yarn"] == round(400000.0 / 6.0, 2)
        assert power["mtd"]["tonnes_basis"] == "keyed days"
        assert self._session.execute.call_args_list[19].args[1]["spinning_type"] == "Spinning"
        assert data["kpi"]["power_kwh"] == 18420.0
        assert data["kpi"]["dg_hours"] == 2.5
        assert data["kpi"]["kwh_per_t_cloth"] == round(18420.0 / 2.0, 2)
        assert self._session.execute.call_count == 20  # power query (ITM-004) is the last execute

    def test_no_branch_skips_branch_keyed_sections(self):
        self._session.execute.side_effect = [
            _result([]),  # machine counts
            _result([]),  # spinning slice
            _result([]),  # spinning MTD
            # No branch -> no winding, so no twisted-item-id lookup either.
            _result([]),  # weaving day sync/lock headers
            _result([]),  # weaving slice
            _result([]),  # weaving MTD
            _result([]),  # finishing
            _first_result(None),  # emulsion — no log rows at all
            _result([]),  # heavy-light
            _result([]),  # hands
            _result([]),  # hands by department
            _result([]),  # shifts worked
            # No jute_yarn_issue table on this tenant -> degrade, never 500.
            ProgrammingError("SELECT ...", {}, Exception("1146 Table doesn't exist")),
            _first_result(None),  # power (ITM-004): table exists, nothing keyed
        ]
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": 2, "report_date": "2026-01-24"},
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        # The failed statement is rolled back so later queries are not poisoned.
        self._session.rollback.assert_called_once()
        assert data["power"]["keyed"] is False
        assert data["power"]["day"]["kwh"] is None and data["power"]["mtd"]["days"] == 0
        assert data["kpi"]["power_kwh"] is None and data["kpi"]["dg_hours"] is None
        assert data["kpi"]["kwh_per_t_cloth"] is None
        assert data["hands"]["yarn_balance"]["purchased_available"] is False
        assert data["hands"]["yarn_balance"]["day"]["purchased_issued_mt"] is None
        assert data["hands"]["stages"]["available"] is False
        assert data["kpi"]["hands_stage_basis"] is False
        assert data["kpi"]["hands_per_mt"] is None  # nothing produced, not 0.0
        assert data["winding"] is None
        assert data["jute"] is None
        assert data["emulsion"] is None
        assert data["kpi"]["winding_kg"] is None
        assert data["kpi"]["jute_days_stock"] is None
        assert data["kpi"]["jute_cover_basis_kg"] is None
        assert data["kpi"]["twisting_kg"] is None
        assert data["kpi"]["jbo_pct"] is None
        assert data["shifts_worked"]["day"] == 0
        assert data["shifts_worked"]["sections"] == []
        assert self._session.execute.call_count == 14  # power query is the last execute

    def test_hands_per_mt_falls_back_to_spinning_without_departments(self):
        """TKT-000117 golden case (405.13 hands over 15.165 MT spinning) is now
        the FALLBACK: when no attendance row resolves to a department the
        stage split is impossible, and the sheet divides all hands by spinning
        output as before — never by the spinning + weaving sum, which double
        counts the day's yarn as the day's cloth.
        """
        self._session.execute.side_effect = [
            _result([{"machine_type_name": "Spinning", "total_machines": 40},
                     {"machine_type_name": "Loom", "total_machines": 120}]),
            _result([  # spinning day slice -> 15.165 MT
                {"shift_bucket": "A", "machine_id": 1, "item_id": 10,
                 "item_code": "Y9", "item_name": "9 LB", "act_prod_doff": 15165.0,
                 "std_prod": 18000.0, "target_prod": 17000.0, "act_count": 9.2,
                 "std_count": 9.0, "act_prod_wind": 14000.0},
            ]),
            _result([]),  # spinning MTD
            _result([]),  # weaving day sync/lock headers
            _result([  # weaving day slice -> 11.529 MT
                {"weaving_quality_id": 1, "weaving_quality_code": "H1",
                 "weaving_quality_name": "Hessian", "spell_code": "A1",
                 "machine_id": 11, "production_kg": 11529.0,
                 "production_yds": 24000.0, "std_prod_yds": 26000.0,
                 "ozs_yds": 10.0},
            ]),
            _result([]),  # weaving MTD
            _result([]),  # finishing
            _first_result(None),  # emulsion
            _result([]),  # heavy-light
            _result([  # hands -> 405.13
                {"shift_bucket": "A", "day_hours": 1620.52, "day_hands": 405.13,
                 "mtd_hours": 38000.0, "mtd_hands": 9500.0},
            ]),
            _result([  # hands by department: nothing resolves
                {"dept_id": None, "dept_desc": None, "day_hands": 405.13,
                 "mtd_hands": 9500.0},
            ]),
            _result([]),  # shifts worked
            _first_result(None),  # purchased yarn: tables exist, nothing issued
            _first_result(None),  # power (ITM-004): nothing keyed
        ]
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": 2, "report_date": "2026-01-24"},
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        assert data["spinning"]["total"]["production_mt"] == 15.165
        assert data["weaving"]["total"]["production_mt"] == 11.529
        assert data["hands"]["stages"]["available"] is False
        assert data["hands"]["basis_label"].startswith("spinning output")
        assert data["hands"]["hands_per_mt"] == 26.71  # 405.13 / 15.165
        assert data["hands"]["mtd_hands_per_mt"] is None  # no MTD spinning rows
        assert data["kpi"]["hands_per_mt"] == 26.71
        assert data["kpi"]["hands_stage_basis"] is False
        assert data["kpi"]["hands_per_mt_yarn"] is None
        assert data["kpi"]["hands_per_mt_cloth"] is None
        assert data["kpi"]["hands_per_mt_spinning_basis"] == 26.71
        assert data["hands"]["spinning_basis"]["day_mt"] == 15.165
        # The old summed basis produced this — it must NOT come back.
        assert data["kpi"]["hands_per_mt"] != round(405.13 / (15.165 + 11.529), 2)
        yb = data["hands"]["yarn_balance"]
        assert yb["purchased_available"] is True
        assert yb["day"] == {"spun_mt": 15.165, "purchased_issued_mt": 0.0,
                             "woven_mt": 11.529, "surplus_mt": 3.636}

    def test_hands_per_mt_stage_wise_sls_9_oct(self):
        """TKT-000185 golden case: sls co 2 / br 29, 9 Oct 2026 — 840.22 hands,
        spun 30.031 MT, woven 29.073 MT. Yarn stage 296.43 direct + its share
        of 139.77 common over spinning, cloth stage 404.02 + the rest over
        weaving: 11.84 + 16.67 = 28.51 hands per MT of cloth (all-hands ÷
        spinning would say 27.98, ÷ weaving 28.90).
        """
        dept_rows = [
            {"dept_desc": "WEAVING(EJM)", "day_hands": 159.63, "mtd_hands": 159.63},
            {"dept_desc": "SPINNING(EJM)", "day_hands": 158.06, "mtd_hands": 158.06},
            {"dept_desc": "SPOOL WINDING(EJM)", "day_hands": 101.13, "mtd_hands": 101.13},
            {"dept_desc": "PREPARING(EJM)", "day_hands": 89.31, "mtd_hands": 89.31},
            {"dept_desc": "SACK SEWING(EJM)", "day_hands": 61.63, "mtd_hands": 61.63},
            {"dept_desc": "MILL MECHANICAL(EJM)", "day_hands": 47.0, "mtd_hands": 47.0},
            {"dept_desc": "FINISHING(EJM)", "day_hands": 46.63, "mtd_hands": 46.63},
            {"dept_desc": "BATCHING(EJM)", "day_hands": 39.06, "mtd_hands": 39.06},
            {"dept_desc": "FACTORY MECHANICAL(EJM)", "day_hands": 34.0, "mtd_hands": 34.0},
            {"dept_desc": "BEAMING(EJM)", "day_hands": 29.0, "mtd_hands": 29.0},
            {"dept_desc": "GENERAL(EJM)", "day_hands": 21.38, "mtd_hands": 21.38},
            {"dept_desc": "OTHERS GENERAL(EJM)", "day_hands": 17.38, "mtd_hands": 17.38},
            {"dept_desc": "GENERAL MECHANICAL(EJM)", "day_hands": 13.13, "mtd_hands": 13.13},
            {"dept_desc": "JUTE(EJM)", "day_hands": 10.0, "mtd_hands": 10.0},
            {"dept_desc": "ELECTRICAL(EJM)", "day_hands": 6.88, "mtd_hands": 6.88},
            {"dept_desc": "COP WINDING(EJM)", "day_hands": 6.0, "mtd_hands": 6.0},
        ]
        self._session.execute.side_effect = [
            _result([{"machine_type_name": "Spinning", "total_machines": 40},
                     {"machine_type_name": "Loom", "total_machines": 334}]),
            _result([  # spinning day slice -> 30.031 MT
                {"shift_bucket": "A", "machine_id": 1, "item_id": 10,
                 "item_code": "Y9", "item_name": "9 LB", "act_prod_doff": 30031.0,
                 "std_prod": 32000.0, "target_prod": 31000.0, "act_count": 9.2,
                 "std_count": 9.0, "act_prod_wind": 29000.0},
            ]),
            _result([{"shift_bucket": "A", "production_kg": 30031.0, "frame_runs": 40}]),
            _result([]),  # weaving day sync/lock headers
            _result([  # weaving day slice -> 29.073 MT
                {"weaving_quality_id": 1, "weaving_quality_code": "H1",
                 "weaving_quality_name": "Hessian", "spell_code": "A1",
                 "machine_id": 11, "production_kg": 29073.0,
                 "production_yds": 60000.0, "std_prod_yds": 65000.0,
                 "ozs_yds": 10.0},
            ]),
            _result([{"weaving_quality_id": 1, "production_mt": 29.073}]),
            _result([]),  # finishing
            _first_result(None),  # emulsion
            _result([]),  # heavy-light
            _result([  # hands -> 840.22
                {"shift_bucket": "A", "day_hours": 6721.76, "day_hands": 840.22,
                 "mtd_hours": 6721.76, "mtd_hands": 840.22},
            ]),
            _result(dept_rows),
            _result([]),  # shifts worked
            _first_result({"day_kg": 0.0, "mtd_kg": 0.0}),  # no purchase that day
            _first_result(None),  # power (ITM-004): nothing keyed
        ]
        resp = client.get(
            "/api/juteProductionReports/daily_production_report",
            params={"co_id": 2, "report_date": "2026-10-09"},
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        kpi, stages = data["kpi"], data["hands"]["stages"]
        assert kpi["hands_stage_basis"] is True
        assert kpi["hands_per_mt_yarn"] == 11.84
        assert kpi["hands_per_mt_cloth"] == 16.67
        assert kpi["hands_per_mt"] == 28.51
        assert kpi["hands_per_mt_mtd"] == 28.51  # one-day month, same figure
        assert kpi["hands_per_mt_spinning_basis"] == 27.98  # 840.22 / 30.031, shown beside it
        assert data["hands"]["hands_per_mt"] == 28.51
        assert stages["yarn"]["direct_day_hands"] == 296.43
        assert stages["cloth"]["direct_day_hands"] == 404.02
        assert stages["common"]["day_hands"] == 139.77
        assert stages["common"]["yarn_share_pct"] == 42.3
        assert stages["total"]["day_hands"] == 840.22
        assert data["hands"]["yarn_balance"]["day"]["surplus_mt"] == 0.958


class TestBuildPowerSection:
    """ITM-004 — the DPR's Power line from the one aggregate row."""

    ROW = {
        "day_kwh": 18420.0, "day_dg_kwh": 900.0, "day_dg_hours": 2.5, "day_dg_litres": 310.0,
        "day_rows": 2, "mtd_kwh": 400000.0, "mtd_dg_kwh": 12000.0, "mtd_dg_hours": 40.0,
        "mtd_dg_litres": 5000.0, "mtd_days": 22,
        # Tonnes of the KEYED days only (the query's scalar subqueries), the
        # MTD denominators -- never the full month's totals.
        "mtd_keyed_cloth_mt": 50.0, "mtd_keyed_yarn_kg": 6000.0,
    }

    def test_keyed_row_gives_day_and_mtd_ratios(self):
        out = build_power_section(dict(self.ROW), 0.6, 2.0)
        assert out["keyed"] is True
        day = out["day"]
        assert day["kwh"] == 18420.0
        assert day["grid_kwh"] == 17520.0
        assert day["dg_kwh"] == 900.0
        assert day["dg_hours"] == 2.5 and day["dg_litres"] == 310.0
        assert day["kwh_per_t_cloth"] == round(18420.0 / 2.0, 2)
        assert day["kwh_per_t_yarn"] == round(18420.0 / 0.6, 2)
        assert day["dg_share_pct"] == round(900.0 * 100 / 18420.0, 2)
        mtd = out["mtd"]
        assert mtd["days"] == 22
        assert mtd["kwh"] == 400000.0 and mtd["dg_kwh"] == 12000.0 and mtd["grid_kwh"] == 388000.0
        assert mtd["kwh_per_t_cloth"] == round(400000.0 / 50.0, 2)
        assert mtd["kwh_per_t_yarn"] == round(400000.0 / 6.0, 2)
        assert mtd["dg_share_pct"] == 3.0
        assert (mtd["cloth_t"], mtd["yarn_t"], mtd["tonnes_basis"]) == (50.0, 6.0, "keyed days")

    def test_mtd_ratio_uses_keyed_day_tonnes_not_the_month(self):
        """5 of 22 days keyed: the kWh of those 5 days divides by THEIR tonnes
        (the row's keyed tonnes), so the figure matches /power_report's MTD."""
        row = {**self.ROW, "mtd_kwh": 90000.0, "mtd_days": 5,
               "mtd_keyed_cloth_mt": 11.0, "mtd_keyed_yarn_kg": 1400.0}
        out = build_power_section(row, 0.6, 2.0)
        assert out["mtd"]["kwh_per_t_cloth"] == round(90000.0 / 11.0, 2)
        assert out["mtd"]["kwh_per_t_yarn"] == round(90000.0 / 1.4, 2)

    def test_no_rows_for_the_day_keeps_mtd(self):
        row = {**self.ROW, "day_kwh": 0.0, "day_dg_kwh": 0.0, "day_dg_hours": 0.0,
               "day_dg_litres": 0.0, "day_rows": 0}
        out = build_power_section(row, 0.6, 2.0)
        assert out["keyed"] is False
        assert all(v is None for v in out["day"].values())
        assert out["mtd"]["days"] == 22 and out["mtd"]["kwh"] == 400000.0

    def test_empty_month_gives_none_values_not_zero(self):
        out = build_power_section({}, 0.6, 2.0)
        assert out["keyed"] is False
        assert out["mtd"]["days"] == 0
        assert out["mtd"]["kwh"] is None and out["mtd"]["kwh_per_t_cloth"] is None

    def test_none_row_is_none(self):
        assert build_power_section(None, 0.6, 2.0) is None

    def test_zero_tonnes_gives_none_ratio_never_zero(self):
        row = {**self.ROW, "mtd_keyed_cloth_mt": 0.0, "mtd_keyed_yarn_kg": None}
        out = build_power_section(row, 0.0, 0.0)
        assert out["day"]["kwh"] == 18420.0
        assert out["day"]["kwh_per_t_cloth"] is None and out["day"]["kwh_per_t_yarn"] is None
        assert out["mtd"]["kwh_per_t_cloth"] is None and out["mtd"]["kwh_per_t_yarn"] is None

    def test_zero_kwh_gives_none_dg_share(self):
        row = {**self.ROW, "day_kwh": 0.0, "day_dg_kwh": 0.0, "day_rows": 1}
        out = build_power_section(row, 0.6, 2.0)
        assert out["keyed"] is True and out["day"]["kwh"] == 0.0
        assert out["day"]["dg_share_pct"] is None

    def test_fetch_swallows_missing_table_and_rolls_back(self):
        """No jute_power_reading on this tenant -> None (section omitted), and
        the failed statement is rolled back so later queries are not poisoned."""
        session = MagicMock()
        session.execute.side_effect = ProgrammingError(
            "SELECT ...", {}, Exception("1146 Table 'x.jute_power_reading' doesn't exist")
        )
        assert fetch_power_readings(session, {"co_id": 1}) is None
        session.rollback.assert_called_once()

    def test_fetch_returns_mapping_or_empty_dict(self):
        session = MagicMock()
        session.execute.return_value = _first_result({"day_kwh": 5.0, "day_rows": 1, "mtd_days": 1})
        assert fetch_power_readings(session, {"co_id": 1})["day_kwh"] == 5.0
        session.execute.return_value = _first_result(None)
        assert fetch_power_readings(session, {"co_id": 1}) == {}
        session.rollback.assert_not_called()
