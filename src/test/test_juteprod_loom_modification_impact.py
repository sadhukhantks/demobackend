"""Tests for the Loom Modification Impact report — src/juteProduction/loom_modification_impact.py
(+ loom_modification_impact_query.py), phase 2 of
docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md.

Portal persona: DB + auth mocked (no real DB). ``load_report_inputs`` is the only DB
touchpoint (execute order R1..R15 in the module docstring); the endpoint tests patch
it (test_juteprod_lowprod_weaving.py:449 pattern) or feed ``execute.side_effect`` in
that order for the cap paths. Everything else is a pure function over dicts.

Covered:
  * structural parity of the range read with the day slice (GREATEST/LEAST working
    hours form, 28.35/1000 kg constant, 36 x picks capacity, per-row COALESCE guards,
    binds, index hint switch)
  * baseline speed resolution — last date / newest id on same-day duplicates, actual
    role precedence, actual = 0 falls back to standard, first-in-window fallback,
    change events inside a span
  * loom-day building (stage 2 + 3) — yds / kg / std_prod_yds / cap_per_rpm,
    NULL-picks day gives std 0, unmanned-spell output kept out of the efficiency
    numerators (manned jugar)
  * windows — D/E per stage, before only for the first stage, next-event cut (any
    chain), window cut, last-data-date cut, other-chain drops, data holes and the
    producing-day extension (met / stopped by hole / capped)
  * aggregates — ratio of sums, None efficiency, coverage, cause counters,
    constant-yardstick efficiency
  * SE rule (pooled SD), verdict on efficiency alone (IMPROVED / WORSE /
    NO_CLEAR_CHANGE / NOT_ENOUGH_DATA / within noise / control sign rule), and
    NOT_ENOUGH_DATA with the cause named when coverage is short — no fallback metric
  * controls — band per study loom, ineligible (modified / in study / out of band),
    trimming to the cap by closest band, prefix bands, DiD per band weighted
  * flags S / H / W / D / N, the mechanical section (breakdown / mechanical
    stoppage per loom before | after + totals)
  * endpoint — param validation 400s, caps 400s (window, study looms, span), full
    happy path over synthetic inputs (contract keys), options endpoint
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.juteProduction import loom_modification_impact as lmi
from src.juteProduction.constants import (
    IMPACT_MAX_LOOMS,
    IMPACT_MAX_SPAN_DAYS,
    IMPACT_MAX_STUDY_LOOMS,
    IMPACT_MAX_WINDOW_DAYS,
    MIN_LOOM_DAYS_GROUP,
    SE_MULTIPLIER,
    VERDICT_THRESHOLD_PCT,
)
from src.juteProduction.loom_modification_impact import (
    aggregate,
    band_key,
    build_loom_days,
    build_picks_index,
    build_speed_index,
    build_mechanical,
    build_stages,
    compute_loom_windows,
    decide_extension,
    decide_verdict,
    did_by_band,
    eff_value,
    efficiency_data_reasons,
    find_data_holes,
    first_speed_in_window,
    get_current_user_with_refresh,
    get_tenant_db,
    loom_flags,
    phase_of,
    pooled_se,
    quality_row_metrics,
    resolve_speed,
    router,
    select_controls,
    speed_change_events,
)
from src.juteProduction.loom_modification_impact_query import (
    pick_range_index_hint,
    weaving_range_loomday_sql,
)
from src.juteProduction.weaving_query import weaving_day_slice_sql

URL = "/api/juteProductionReports/loom_modification_impact"
OPT_URL = "/api/juteProductionReports/loom_modification_impact_options"

app = FastAPI()
app.include_router(router, prefix="/api/juteProductionReports")
client = TestClient(app)

D = date


def _row(mapping: dict):
    r = MagicMock()
    r._mapping = mapping
    return r


def _res(rows=None, one=None):
    res = MagicMock()
    res.fetchall.return_value = [_row(m) for m in (rows or [])]
    res.fetchone.return_value = _row(one) if one is not None else None
    return res


# ---------------------------------------------------------------------------
# Fixtures (synthetic, no DB)
# ---------------------------------------------------------------------------

STD_ROWS = [
    {"weaving_target_map_id": 1, "machine_id": 1, "value_role": "standard", "value": 150, "effective_date": D(2026, 1, 1)},
    {"weaving_target_map_id": 2, "machine_id": 1, "value_role": "standard", "value": 160, "effective_date": D(2026, 8, 20)},
    {"weaving_target_map_id": 3, "machine_id": 2, "value_role": "standard", "value": 150, "effective_date": D(2026, 1, 1)},
    {"weaving_target_map_id": 4, "machine_id": 2, "value_role": "actual", "value": 158, "effective_date": D(2026, 3, 1)},
    # same-day duplicates on loom 3: newest id wins
    {"weaving_target_map_id": 5, "machine_id": 3, "value_role": "standard", "value": 140, "effective_date": D(2026, 5, 1)},
    {"weaving_target_map_id": 7, "machine_id": 3, "value_role": "standard", "value": 145, "effective_date": D(2026, 5, 1)},
    {"weaving_target_map_id": 6, "machine_id": 3, "value_role": "standard", "value": 130, "effective_date": D(2026, 5, 1)},
    # loom 4: actual row = 0 -> falls back to standard
    {"weaving_target_map_id": 8, "machine_id": 4, "value_role": "standard", "value": 470, "effective_date": D(2026, 1, 1)},
    {"weaving_target_map_id": 9, "machine_id": 4, "value_role": "actual", "value": 0, "effective_date": D(2026, 6, 1)},
    # loom 5: no row before the window, first standard inside it
    {"weaving_target_map_id": 10, "machine_id": 5, "value_role": "standard", "value": 152, "effective_date": D(2026, 7, 15)},
]


def _qrow(mid, d, cuts, qid=7, wh=16.0, avail=24.0, eb="100", is_control=0, stop=0.0, qname="HESSIAN"):
    """One loom-quality-day row as the range SQL returns it. quality: fl 100, ozs 10,
    njpc 10 -> yds = cuts x 100, kg = yds x 10 x 0.02835."""
    return {
        "machine_id": mid, "tran_date": d, "weaving_quality_id": qid, "weaving_quality_code": f"Q{qid}",
        "weaving_quality_name": qname, "finished_length": 100, "ozs_yds": 10, "no_of_jugar_per_cut": 10,
        "cuts": cuts, "total_jugar": cuts * 10, "total_jugar_manned": cuts * 10 if wh > 0 else 0,
        "working_hours": wh, "available_hours": avail,
        "stoppage_hours": stop, "rows_n": 2, "rows_with_hours": 2 if wh > 0 else 0, "eb_ids": eb, "is_control": is_control,
    }


def _picks(qids, d_from, d_to, picks=10.0):
    rows = []
    d = d_from
    while d <= d_to:
        for q in qids:
            rows.append({"weaving_quality_id": q, "entry_date": d, "picks": picks})
        d += timedelta(days=1)
    return rows


def _speed_rows(mids, value=150, eff=D(2026, 1, 1)):
    return [{"weaving_target_map_id": 100 + m, "machine_id": m, "value_role": "standard", "value": value, "effective_date": eff}
            for m in mids]


def _event(mid_ev, mod_date, kind="INITIAL", parent=None, root=None, settle=3, title="Shuttle change",
           mod_to=None, group=None):
    return {
        "modification_id": mid_ev, "co_id": 9, "branch_id": 12, "loom_group_id": group,
        "group_code": "G1" if group else None, "group_name": "Group 1" if group else None,
        "mod_date": mod_date, "mod_to_date": mod_to, "settle_days": settle, "mod_kind": kind,
        "parent_modification_id": parent, "root_modification_id": root or mid_ev,
        "mod_category": "MECHANICAL", "title": title, "done_by": "INHOUSE",
    }


def _snap(mid_ev, mid, d_m, e_m=None, name=None, line="11", dept=15009):
    return {"modification_id": mid_ev, "machine_id": mid, "machine_name": name or f"S{300 + mid}", "mech_code": None,
            "line_no": line, "dept_id": dept, "machine_active": 1, "done_date": None, "done_to_date": None,
            "d_m": d_m, "e_m": e_m or d_m}


def _span(mid, mod_id, root_id, d_m, e_m=None, settle=3, title="Other", kind="INITIAL"):
    return {"machine_id": mid, "modification_id": mod_id, "root_modification_id": root_id, "mod_kind": kind,
            "title": title, "settle_days": settle, "d_m": d_m, "e_m": e_m or d_m}


def _daily(mids, d_from, d_to, cuts_fn, is_control=0, eb="100", wh=16.0):
    rows, d = [], d_from
    while d <= d_to:
        for i, m in enumerate(mids):
            rows.append(_qrow(m, d, cuts_fn(d, i), is_control=is_control, eb=eb, wh=wh))
        d += timedelta(days=1)
    return rows


def _params(**kw):
    p = {"co_id": 9, "branch_id": 12, "modification_id": 1, "window_days": 30, "settle_days": None,
         "control": "speed_band", "control_group_id": None}
    p.update(kw)
    return p


def make_inputs(window_days=30, with_control=True, after_gain=0.4, stages_spec=None, last_data=D(2026, 9, 10),
                study=(1, 2, 3, 4, 5, 6), controls=(11, 12, 13, 14, 15, 16), control_mode="speed_band"):
    """Synthetic report inputs: 6 study looms modified 2026-08-01 (settle 3), before
    cuts 2.0/2.1 per day (50 / 52.5 % eff), after +after_gain; 6 controls flat at 2.0."""
    root = 1
    events = [_event(1, D(2026, 8, 1), group=5)]
    snapshot = [_snap(1, m, D(2026, 8, 1)) for m in study]
    if stages_spec:
        for ev, snaps in stages_spec:
            events.append(ev)
            snapshot.extend(snaps)
    stages, loom_info = build_stages(events, snapshot, None)
    spans = {m: [_span(m, 1, 1, D(2026, 8, 1))] for m in study}
    for ev, snaps in (stages_spec or []):
        for s in snaps:
            spans.setdefault(s["machine_id"], []).append(_span(s["machine_id"], ev["modification_id"], root, s["d_m"], s["e_m"], ev["settle_days"], ev["title"], ev["mod_kind"]))
    windows = compute_loom_windows(stages, spans, window_days, last_data, root, 0)
    span_from = min(w["before"][0] for w in windows.values())
    span_to = max((w["after"][1] if w["after"] else w["excluded"][1]) for lw in windows.values() for w in lw["stages"].values())

    def study_cuts(d, i):
        base = 2.0 if d.day % 2 else 2.1
        return base + (after_gain if d > D(2026, 8, 4) else 0.0)

    quality_rows = _daily(list(study), span_from, span_to, study_cuts)
    control_candidates = []
    if with_control:
        quality_rows += _daily(list(controls), span_from, span_to, lambda d, i: 2.0 if d.day % 2 else 2.1, is_control=1, eb="200")
        control_candidates = [{"machine_id": m, "machine_name": f"S{300 + m}", "mech_code": None, "line_no": "11", "dept_id": 15009, "branch_id": 12}
                              for m in controls]
    return {
        "root_id": root, "events": events, "snapshot": snapshot, "stages": stages, "loom_info": loom_info,
        "study_ids": list(study), "last_data_date": last_data, "all_spans": spans,
        "row_dates": [], "holes": [], "producing": set(),
        "extension": {"extension_days": 0, "met": True, "loom_days": 999, "days": 99, "stopped_by_hole": False, "capped": False,
                      "min_loom_days": 30, "min_days": 10},
        "windows": windows, "span": (span_from, span_to), "nominal_span": (span_from, span_to),
        "speed_rows": _speed_rows(list(study) + list(controls)), "control_candidates": control_candidates,
        "control_group": None, "control_ids": list(controls) if with_control else [],
        "picks": _picks([7], span_from, span_to), "quality_rows": quality_rows, "range_ms": 5, "index_hint": "idx_wd_jugar_chain",
        "mech_stoppage": [{"machine_id": 1, "tran_date": D(2026, 7, 10), "stoppage_hours": 2.5},
                          {"machine_id": 1, "tran_date": D(2026, 7, 11), "stoppage_hours": 1.0},
                          {"machine_id": 2, "tran_date": D(2026, 8, 2), "stoppage_hours": 9.0},     # EXCLUDED -> ignored
                          {"machine_id": 99, "tran_date": D(2026, 7, 10), "stoppage_hours": 9.0}],  # not a study loom
        "breakdowns": [{"machine_id": 1, "tran_date": D(2026, 8, 20), "breakdown_count": 1, "breakdown_minutes": 45},
                       {"machine_id": 3, "tran_date": D(2026, 8, 25), "breakdown_count": 2, "breakdown_minutes": 30}],
    }


# ---------------------------------------------------------------------------
# Structural parity with the day slice
# ---------------------------------------------------------------------------


class TestRangeSqlStructure:
    def test_working_hours_form_matches_slice(self):
        sql = weaving_range_loomday_sql().text
        form = "GREATEST(0, LEAST(COALESCE(sp.working_hours, 0)"
        assert form in sql and form in weaving_day_slice_sql()
        assert "COALESCE(att.att_hours, 0))" in sql
        assert "- COALESCE(stp.stoppage_hours, 0))" in sql

    def test_total_jugar_and_guards_match_slice(self):
        sql = weaving_range_loomday_sql().text
        assert "r.cuts * r.no_of_jugar_per_cut + r.close_jugar" in sql
        assert "- r.open_jugar - r.less_production" in sql
        for guard in ("COALESCE(wd.close_jugar, 0)", "COALESCE(wd.less_production, 0)", "COALESCE(wd.open_jugar, 0)",
                      "COALESCE(q.finished_length, 0)", "COALESCE(q.ozs_yds, 0)", "COALESCE(q.no_of_jugar_per_cut, 0)"):
            assert guard in sql and guard in weaving_day_slice_sql()

    def test_manned_jugar_column_for_the_efficiency_numerator(self):
        sql = weaving_range_loomday_sql().text
        assert "AS total_jugar_manned" in sql
        assert "CASE WHEN r.working_hours > 0" in sql

    def test_att_leg_keeps_straight_join_and_weaver_gate(self):
        sql = weaving_range_loomday_sql().text
        assert "STRAIGHT_JOIN daily_ebmc_attendance dea" in sql
        assert "LIKE 'weaver%'" in sql
        assert "dea.mc_id IN :machines" in sql
        assert "da.attendance_date BETWEEN :date_from AND :date_to" in sql

    def test_binds_and_branch_tolerance(self):
        q = weaving_range_loomday_sql()
        sql = q.text
        for b in (":co_id", ":branch_id", ":machines", ":control_machines", ":date_from", ":date_to"):
            assert b in sql
        assert "(:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)" in sql
        assert "vw_weaving_daily" not in sql
        assert "GROUP BY r.machine_id, r.tran_date, r.weaving_quality_id" in sql

    def test_index_hint_switch(self):
        assert "FORCE INDEX (idx_wd_jugar_chain)" in weaving_range_loomday_sql().text
        assert "FORCE INDEX" not in weaving_range_loomday_sql(None).text
        assert pick_range_index_hint(["idx_wd_jugar_chain"]) == "idx_wd_jugar_chain"
        assert pick_range_index_hint(["idx_wd_jugar_chain", "idx_wd_co_machine_date"]) is None
        assert pick_range_index_hint([]) is None

    def test_python_constants_are_the_slices(self):
        # production_kg = yds x ozs x 28.35 / 1000; std_prod_yds = speed x hours x 60 / (36 x picks)
        assert abs(lmi.KG_PER_OZ_YD - 28.35 / 1000) < 1e-12
        m = quality_row_metrics(_qrow(1, D(2026, 8, 1), 2.0), 150.0, 10.0)
        assert abs(m["yds"] - 200.0) < 1e-9
        assert abs(m["kg"] - 200.0 * 10 * 28.35 / 1000) < 1e-9
        assert abs(m["std_prod_yds"] - 150 * 16 * 60 / (36 * 10)) < 1e-9
        assert abs(m["cap_per_rpm"] - 16 * 60 / (36 * 10)) < 1e-9

    def test_null_picks_day_gives_zero_capacity_like_slice(self):
        m = quality_row_metrics(_qrow(1, D(2026, 8, 1), 2.0), 150.0, 0.0)
        assert m["std_prod_yds"] == 0 and m["cap_per_rpm"] == 0
        assert m["yds"] == 200.0 and m["std_picks_present"] is False


# ---------------------------------------------------------------------------
# Baseline speed resolution
# ---------------------------------------------------------------------------


class TestSpeedResolution:
    def setup_method(self):
        self.idx = build_speed_index([_row(r) for r in STD_ROWS])

    def test_last_effective_date(self):
        assert resolve_speed(self.idx, 1, D(2026, 8, 19))[0] == 150
        assert resolve_speed(self.idx, 1, D(2026, 8, 20))[0] == 160
        assert resolve_speed(self.idx, 1, D(2025, 12, 31)) == (None, None, None)

    def test_same_day_duplicates_newest_id_wins(self):
        v, role, eff = resolve_speed(self.idx, 3, D(2026, 6, 1))
        assert v == 145 and role == "standard" and eff == D(2026, 5, 1)

    def test_actual_role_precedence(self):
        assert resolve_speed(self.idx, 2, D(2026, 2, 1)) == (150, "standard", D(2026, 1, 1))
        assert resolve_speed(self.idx, 2, D(2026, 3, 1))[:2] == (158, "actual")

    def test_actual_zero_falls_back_to_standard(self):
        assert resolve_speed(self.idx, 4, D(2026, 7, 1))[:2] == (470, "standard")

    def test_first_speed_in_window_fallback(self):
        assert resolve_speed(self.idx, 5, D(2026, 7, 1))[0] is None
        assert first_speed_in_window(self.idx, 5, D(2026, 7, 1), D(2026, 7, 31)) == (152, D(2026, 7, 15))
        assert first_speed_in_window(self.idx, 5, D(2026, 6, 1), D(2026, 6, 30)) == (None, None)
        assert first_speed_in_window(self.idx, 99, D(2026, 6, 1), D(2026, 6, 30)) == (None, None)

    def test_change_events_inside_span(self):
        ch = speed_change_events(self.idx, 1, D(2026, 8, 1), D(2026, 8, 31))
        assert ch == [(D(2026, 8, 20), 150, 160, "standard")]
        assert speed_change_events(self.idx, 1, D(2026, 8, 21), D(2026, 8, 31)) == []
        assert speed_change_events(self.idx, 2, D(2026, 2, 1), D(2026, 3, 31)) == [(D(2026, 3, 1), 150, 158, "actual")]

    def test_picks_index_averages_exact_day(self):
        idx = build_picks_index([
            {"weaving_quality_id": 7, "entry_date": D(2026, 8, 1), "picks": 10},
            {"weaving_quality_id": 7, "entry_date": D(2026, 8, 1), "picks": 12},
            {"weaving_quality_id": 7, "entry_date": D(2026, 8, 2), "picks": None},
        ])
        assert idx == {(7, D(2026, 8, 1)): 11.0}


# ---------------------------------------------------------------------------
# Loom-day building
# ---------------------------------------------------------------------------


class TestBuildLoomDays:
    def test_rolls_qualities_to_loom_day(self):
        idx = build_speed_index(_speed_rows([1]))
        picks = {(7, D(2026, 8, 1)): 10.0, (8, D(2026, 8, 1)): 10.0}
        rows = [_qrow(1, D(2026, 8, 1), 2.0, qid=7, wh=8, avail=8), _qrow(1, D(2026, 8, 1), 1.0, qid=8, wh=8, avail=8, eb="101", qname="SACKING")]
        lds = build_loom_days(rows, idx, picks)
        assert len(lds) == 1
        ld = lds[0]
        assert ld["yds"] == 300.0 and ld["hours_run"] == 16.0 and ld["cuts"] == 3.0 and ld["rows_n"] == 4
        assert ld["eb_ids"] == {"100", "101"} and ld["days_run"] == 1 and ld["has_standard"]
        assert abs(ld["capacity_measured"] - 2 * (150 * 8 * 60 / 360)) < 1e-9

    def test_unmanned_spell_output_is_not_in_the_efficiency_numerators(self):
        """One loom-day, two spells: a manned spell (wh 8, 2 cuts) and an unmanned one
        (wh 0, 1 cut). yds counts both; actual_measured / actual_baseline only the
        manned 200 yds — the slice's per-row std_prod_yds > 0 rule."""
        idx = build_speed_index(_speed_rows([1]))
        row = _qrow(1, D(2026, 8, 1), 3.0, wh=8, avail=16)
        row["total_jugar_manned"] = 20
        row["rows_with_hours"] = 1
        ld = build_loom_days([row], idx, {(7, D(2026, 8, 1)): 10.0})[0]
        assert ld["yds"] == 300.0 and ld["kg"] > 0
        assert ld["actual_measured"] == 200.0 and ld["actual_baseline"] == 200.0
        assert abs(ld["capacity_measured"] - 150 * 8 * 60 / 360) < 1e-9
        agg = aggregate([ld], {1: 150.0})
        assert abs(agg["efficiency_pct"] - 200.0 * 100 / (150 * 8 * 60 / 360)) < 1e-9
        assert agg["efficiency_at_baseline_std_pct"] == agg["efficiency_pct"]
        # a row without the column: manned when the row itself has hours, else nothing
        m = quality_row_metrics({k: v for k, v in _qrow(1, D(2026, 8, 1), 2.0).items() if k != "total_jugar_manned"}, 150.0, 10.0)
        assert m["yds_manned"] == 200.0
        m0 = quality_row_metrics({k: v for k, v in _qrow(1, D(2026, 8, 1), 2.0, wh=0).items() if k != "total_jugar_manned"}, 150.0, 10.0)
        assert m0["yds"] == 200.0 and m0["yds_manned"] == 0.0 and m0["std_prod_yds"] == 0.0

    def test_no_output_no_hours_row(self):
        lds = build_loom_days([_qrow(1, D(2026, 8, 1), 0.0, wh=0, avail=8)], {}, {})
        assert lds[0]["days_run"] == 0 and lds[0]["has_standard"] is False and lds[0]["std_speed"] is None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class TestWindows:
    def _stages(self):
        events = [_event(1, D(2026, 8, 1)), _event(2, D(2026, 8, 20), "RECTIFY", parent=1, root=1, settle=2)]
        snaps = [_snap(1, 1, D(2026, 8, 1)), _snap(1, 2, D(2026, 8, 1), D(2026, 8, 3)), _snap(2, 1, D(2026, 8, 20))]
        return build_stages(events, snaps, None)

    def test_before_only_for_first_stage_and_after_cut_at_next_event(self):
        stages, _ = self._stages()
        spans = {1: [_span(1, 1, 1, D(2026, 8, 1)), _span(1, 2, 1, D(2026, 8, 20), settle=2)], 2: [_span(2, 1, 1, D(2026, 8, 1), D(2026, 8, 3))]}
        w = compute_loom_windows(stages, spans, 30, D(2026, 9, 30), 1)
        assert w[1]["before"] == (D(2026, 7, 2), D(2026, 7, 31))
        assert w[1]["stages"][1]["excluded"] == (D(2026, 8, 1), D(2026, 8, 4))
        assert w[1]["stages"][1]["after"] == (D(2026, 8, 5), D(2026, 8, 19))
        assert w[1]["stages"][1]["cut_reason"] == "next_event"
        assert w[1]["stages"][2]["excluded"] == (D(2026, 8, 20), D(2026, 8, 22))
        assert w[1]["stages"][2]["after"] == (D(2026, 8, 23), D(2026, 9, 21))
        assert w[1]["stages"][2]["cut_reason"] == "window"
        # loom 2: rolled out 01..03, no stage 2
        assert w[2]["stages"][1]["excluded"] == (D(2026, 8, 1), D(2026, 8, 6))
        assert w[2]["stages"][1]["after"] == (D(2026, 8, 7), D(2026, 9, 5))
        assert 2 not in w[2]["stages"]

    def test_cut_at_last_data_date_and_other_chain_drop(self):
        stages, _ = self._stages()
        spans = {1: [_span(1, 1, 1, D(2026, 8, 1)), _span(1, 2, 1, D(2026, 8, 20), settle=2), _span(1, 9, 9, D(2026, 7, 10), settle=1, title="Belt")],
                 2: [_span(2, 1, 1, D(2026, 8, 1), D(2026, 8, 3))]}
        w = compute_loom_windows(stages, spans, 30, D(2026, 9, 1), 1)
        assert w[1]["stages"][2]["after"] == (D(2026, 8, 23), D(2026, 9, 1))
        assert w[1]["stages"][2]["cut_reason"] == "last_data_date"
        assert w[1]["drops"] == [(D(2026, 7, 10), D(2026, 7, 11))]
        assert w[1]["other_after"][0][:2] == (D(2026, 7, 12), D(2026, 8, 10))
        assert phase_of(w[1], D(2026, 7, 10)) == ("DROPPED", None)
        assert phase_of(w[1], D(2026, 7, 20)) == ("BEFORE", None)
        assert phase_of(w[1], D(2026, 8, 2)) == ("EXCLUDED", 1)
        assert phase_of(w[1], D(2026, 8, 10)) == ("AFTER", 1)
        assert phase_of(w[1], D(2026, 8, 25)) == ("AFTER", 2)
        assert phase_of(w[1], D(2026, 9, 20)) is None

    def test_after_none_when_next_event_inside_settle(self):
        stages, _ = self._stages()
        spans = {1: [_span(1, 1, 1, D(2026, 8, 1)), _span(1, 7, 7, D(2026, 8, 3))], 2: []}
        w = compute_loom_windows(stages, spans, 30, None, 1)
        assert w[1]["stages"][1]["after"] is None

    def test_extension_days_shift_before_start(self):
        stages, _ = self._stages()
        w = compute_loom_windows(stages, {}, 30, None, 1, extension_days=15)
        assert w[1]["before"] == (D(2026, 6, 17), D(2026, 7, 31))

    def test_data_holes(self):
        present = [D(2026, 7, 1) + timedelta(days=i) for i in range(31) if not 5 <= i <= 12 and i != 20]
        holes = find_data_holes(present, D(2026, 7, 1), D(2026, 7, 31))
        assert holes == [(D(2026, 7, 6), D(2026, 7, 13))]  # the single missing day 21 is not a hole
        assert find_data_holes([_row({"tran_date": D(2026, 7, 31)})], D(2026, 7, 20), D(2026, 7, 31)) == [(D(2026, 7, 20), D(2026, 7, 30))]

    def test_extension_met_stopped_by_hole_and_capped(self):
        events = [_event(1, D(2026, 8, 1))]
        stages, _ = build_stages(events, [_snap(1, 1, D(2026, 8, 1))], None)
        spans = {1: [_span(1, 1, 1, D(2026, 8, 1))]}
        # single loom: needs 10 producing loom-days / 5 days; 30-day window holds 4, extension finds more
        producing = {(1, D(2026, 7, 31) - timedelta(days=i)) for i in (0, 2, 4, 6)} | {(1, D(2026, 6, 20) - timedelta(days=i)) for i in range(8)}
        ext = decide_extension(stages, spans, 30, None, 1, producing, [])
        assert ext["met"] and ext["extension_days"] == (D(2026, 7, 2) - D(2026, 6, 15)).days and ext["loom_days"] == 10
        # a hole between the window and the data stops the extension
        ext2 = decide_extension(stages, spans, 30, None, 1, producing, [(D(2026, 6, 22), D(2026, 6, 30))])
        assert ext2["stopped_by_hole"] and not ext2["met"] and ext2["extension_days"] == 1
        # nothing to find: capped at IMPACT_MAX_WINDOW_DAYS
        ext3 = decide_extension(stages, spans, 30, None, 1, set(), [])
        assert ext3["capped"] and ext3["extension_days"] == IMPACT_MAX_WINDOW_DAYS - 30 and not ext3["met"]


# ---------------------------------------------------------------------------
# Aggregates, SE, verdict
# ---------------------------------------------------------------------------


def _lds(mids, days, cuts, wh=16.0, is_control=False, picks=10.0, eb="100"):
    idx = build_speed_index(_speed_rows(list(mids)))
    rows = [_qrow(m, D(2026, 7, 1) + timedelta(days=i), cuts if not callable(cuts) else cuts(i), wh=wh, eb=eb) for m in mids for i in range(days)]
    pk = {(7, D(2026, 7, 1) + timedelta(days=i)): picks for i in range(days)} if picks else {}
    return build_loom_days(rows, idx, pk)


class TestAggregates:
    def test_ratio_of_sums_and_coverage(self):
        a = aggregate(_lds([1, 2], 5, 2.0), {1: 150, 2: 150})
        assert a["loom_days"] == 10 and a["days_run"] == 10 and a["producing_days"] == 5 and a["eff_days"] == 5
        assert a["efficiency_pct"] == 50.0 and a["efficiency_at_baseline_std_pct"] == 50.0
        assert a["std_coverage_pct"] == 100.0 and a["hours_coverage_pct"] == 100.0
        assert a["unmanned_days"] == 0 and a["no_pick_days"] == 0 and a["no_standard_days"] == 0
        for gone in ("picks_per_hour", "yds_per_hour", "yds_per_available_hour", "cuts_per_loom_day", "kg_per_loom_day",
                     "stoppage_hours_per_loom_day", "picks_coverage_pct"):
            assert gone not in a

    def test_efficiency_none_when_no_capacity(self):
        a = aggregate(_lds([1], 3, 2.0, picks=None), {1: 150})
        assert a["efficiency_pct"] is None and a["efficiency_at_baseline_std_pct"] is None
        assert a["std_coverage_pct"] == 0.0 and a["no_pick_days"] == 3
        assert aggregate([], {})["efficiency_pct"] is None and aggregate([], {})["std_coverage_pct"] is None
        assert eff_value(None) is None and eff_value(a) is None

    def test_constant_yardstick_uses_baseline_speed(self):
        # same output; effective speed 160 in the data but baseline 150 -> baseline eff higher
        idx = build_speed_index(_speed_rows([1], value=160))
        lds = build_loom_days([_qrow(1, D(2026, 8, 1), 2.0)], idx, {(7, D(2026, 8, 1)): 10.0})
        a = aggregate(lds, {1: 150})
        assert a["efficiency_pct"] == round(200 * 100 / (160 * 16 * 60 / 360), 2)
        assert a["efficiency_at_baseline_std_pct"] == 50.0

    def test_unmanned_loom_counts_zero_hours(self):
        a = aggregate(_lds([1], 2, 2.0, wh=0.0), {1: 150})
        assert a["days_run"] == 2 and a["hours_coverage_pct"] == 0.0 and a["efficiency_pct"] is None and a["unmanned_days"] == 2


class TestVerdict:
    def test_no_fallback_when_efficiency_coverage_short(self):
        """Standard coverage short (no SQC picks) or hours coverage short (no weaver
        marks) -> NOT_ENOUGH_DATA naming the cause; the old Auto fallback to picks /
        yds / cuts is gone."""
        good = aggregate(_lds([1, 2, 3], 12, 2.0), {1: 150, 2: 150, 3: 150})
        assert efficiency_data_reasons(good, good) == []
        no_picks = aggregate(_lds([1, 2, 3], 12, 2.0, picks=None), {1: 150, 2: 150, 3: 150})
        r = efficiency_data_reasons(no_picks, good)
        assert len(r) == 2 and r[0].startswith("before: standard coverage 0 % < 80 %") and "no SQC pick reading on 36 loom-days" in r[0]
        assert r[1] == "before: 0 days with measurable efficiency < 5"
        v = decide_verdict(no_picks, good, 0.1, 3, 3)
        assert v["verdict"] == "NOT_ENOUGH_DATA" and "no SQC pick reading" in v["reason"] and v["before"] is None
        assert set(v) == {"verdict", "reason", "before", "after", "delta_pts", "delta_pct", "within_noise", "se"}
        unmanned = aggregate(_lds([1, 2, 3], 12, 2.0, wh=0.0), {1: 150, 2: 150, 3: 150})
        v2 = decide_verdict(good, unmanned, 0.1, 3, 3)
        assert v2["verdict"] == "NOT_ENOUGH_DATA" and "after: standard coverage 0 %" in v2["reason"] and "no weaver marked on 36 loom-days" in v2["reason"]
        # hours coverage short while the standard is measured on the other days: named as hours coverage
        mixed = _lds([1, 2, 3], 12, 2.0) + _lds([4, 5, 6], 12, 2.0, wh=0.0)
        half = aggregate(mixed, {m: 150 for m in range(1, 7)})
        assert half["std_coverage_pct"] == 50.0 and half["hours_coverage_pct"] == 50.0
        assert efficiency_data_reasons(half, good)[0].startswith("before: standard coverage 50 % < 80 % (no weaver marked on 36 loom-days")
        # no standard speed at all
        no_std = build_loom_days([_qrow(m, D(2026, 7, 1) + timedelta(days=i), 2.0) for m in (1, 2, 3) for i in range(12)], {}, {(7, D(2026, 7, 1) + timedelta(days=i)): 10.0 for i in range(12)})
        r3 = efficiency_data_reasons(aggregate(no_std, {}), good)
        assert "no standard speed on 36 loom-days" in r3[0]

    def test_efficiency_needs_min_eff_days(self):
        thin = aggregate(_lds([1, 2, 3], 3, 2.0), {1: 150, 2: 150, 3: 150})
        assert any("3 days with measurable efficiency < 5" in r for r in efficiency_data_reasons(thin, thin))

    def test_pooled_se(self):
        n1, n2, s_p, se = pooled_se([50, 52, 50, 52], [60, 62, 60, 62])
        assert (n1, n2) == (4, 4)
        assert abs(s_p - 1.1547) < 1e-3 and abs(se - s_p * (0.5 ** 0.5)) < 1e-9
        assert pooled_se([50], [60])[3] is None

    def _pair(self, cuts_after, days=12, looms=(1, 2, 3)):
        bs = {m: 150 for m in looms}
        b = _lds(looms, days, lambda i: 2.0 if i % 2 else 2.1)
        a = _lds(looms, days, cuts_after)
        return aggregate(b, bs), aggregate(a, bs), b, a, bs

    def test_improved(self):
        ab, aa, b, a, bs = self._pair(lambda i: 2.4 if i % 2 else 2.5)
        s1 = [v for _, v in lmi.day_series(b, bs)]
        s2 = [v for _, v in lmi.day_series(a, bs)]
        _, _, _, se = pooled_se(s1, s2)
        v = decide_verdict(ab, aa, se, 3, 3)
        assert v["verdict"] == "IMPROVED" and v["delta_pct"] > VERDICT_THRESHOLD_PCT and v["within_noise"] is False
        assert v["delta_pts"] == 10.0 and v["before"] == 51.25 and v["after"] == 61.25

    def test_worse(self):
        ab, aa, b, a, bs = self._pair(lambda i: 1.6 if i % 2 else 1.7)
        v = decide_verdict(ab, aa, 0.4, 3, 3)
        assert v["verdict"] == "WORSE"

    def test_within_noise_and_below_threshold(self):
        ab, aa, b, a, bs = self._pair(lambda i: 2.1 if i % 2 else 2.2)  # +2.5 pts on 51.25 = +4.9 %
        v = decide_verdict(ab, aa, 2.0, 3, 3)  # SE 2 -> 2xSE = 4 > 2.5
        assert v["verdict"] == "NO_CLEAR_CHANGE" and v["within_noise"] is True and "variation" in v["reason"]
        ab2, aa2, *_ = self._pair(lambda i: 2.02 if i % 2 else 2.12)  # +1 %
        v2 = decide_verdict(ab2, aa2, 0.1, 3, 3)
        assert v2["verdict"] == "NO_CLEAR_CHANGE" and v2["within_noise"] is False and "threshold" in v2["reason"]

    def test_not_enough_data(self):
        ab, aa, *_ = self._pair(lambda i: 2.5, days=4)  # 12 loom-days < 30, 4 days < 10
        v = decide_verdict(ab, aa, 0.1, 3, 3)
        assert v["verdict"] == "NOT_ENOUGH_DATA" and "producing loom-days" in v["reason"] and v["delta_pct"] is not None
        ab, aa, *_ = self._pair(lambda i: 2.5)
        v = decide_verdict(ab, aa, 0.1, 3, 1)  # 1 of 3 looms meet the per-loom minimum
        assert v["verdict"] == "NOT_ENOUGH_DATA" and "per-loom minimum" in v["reason"]

    def test_control_sign_rule(self):
        ab, aa, *_ = self._pair(lambda i: 2.4 if i % 2 else 2.5)
        # study +10 pts, control also +10 pts -> net 0, no confirmation
        v = decide_verdict(ab, aa, 0.3, 3, 3, net_delta_pct=0.0, se_net=0.5, net_delta_units=0.0)
        assert v["verdict"] == "NO_CLEAR_CHANGE" and "control" in v["reason"]
        v2 = decide_verdict(ab, aa, 0.3, 3, 3, net_delta_pct=18.0, se_net=0.5, net_delta_units=9.5)
        assert v2["verdict"] == "IMPROVED"


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


class TestControls:
    def _cands(self, ids, prefix="S"):
        return [{"machine_id": m, "machine_name": f"{prefix}{300 + m}", "dept_id": 15009} for m in ids]

    def test_band_key(self):
        assert band_key(150.4, "S301") == ("rpm", 150)
        assert band_key(None, "S4 LOOM 3") == ("prefix", "S")
        assert band_key(0, "HL-12") == ("prefix", "HL-")

    def test_select_in_band_and_drop_reasons(self):
        idx = build_speed_index(_speed_rows([11, 12, 13], 150) + _speed_rows([14], 200) + _speed_rows([15], 160))
        spans = {12: [_span(12, 40, 40, D(2026, 8, 15), title="Belt")]}
        sel = select_controls(self._cands([1, 11, 12, 13, 14, 15]), {1: ("rpm", 150)}, idx, D(2026, 7, 2), spans,
                              (D(2026, 7, 2), D(2026, 9, 3)), 30, {1}, 90)
        ids = [c["machine_id"] for c in sel["selected"]]
        assert ids == [11, 13, 15] and sel["trimmed"] == 0
        reasons = dict(sel["dropped"])
        assert "in the study" in reasons["S301"]
        assert reasons["S312"].startswith("modified 15-Aug")
        assert "outside" in reasons["S314"]
        dist = [c["distance"] for c in sel["selected"]]
        assert dist[:2] == [0.0, 0.0] and abs(dist[2] - (160 / 150 - 1)) < 1e-9

    def test_modified_before_span_but_window_overlaps_is_dropped(self):
        idx = build_speed_index(_speed_rows([11], 150))
        spans = {11: [_span(11, 40, 40, D(2026, 6, 20))]}  # 6-20 + 3 + 30 = 7-23 overlaps span from 7-02
        sel = select_controls(self._cands([11]), {1: ("rpm", 150)}, idx, D(2026, 7, 2), spans, (D(2026, 7, 2), D(2026, 9, 3)), 30, {1}, 90)
        assert sel["selected"] == [] and sel["dropped"][0][1].startswith("modified")

    def test_trim_to_cap_by_closest_band(self):
        idx = build_speed_index([{"weaving_target_map_id": m, "machine_id": m, "value_role": "standard", "value": 150 + m, "effective_date": D(2026, 1, 1)} for m in range(11, 21)])
        sel = select_controls(self._cands(range(11, 21)), {1: ("rpm", 150)}, idx, D(2026, 7, 2), {}, (D(2026, 7, 2), D(2026, 9, 3)), 30, {1}, 3)
        # 161..165 rpm are inside 150 +/- 10 % (5 looms), the closest 3 are kept
        assert [c["machine_id"] for c in sel["selected"]] == [11, 12, 13] and sel["trimmed"] == 2
        assert [d[1] for d in dict(sel["dropped"]).items()].count("outside every study speed band") == 5

    def test_prefix_band_when_no_standards(self):
        sel = select_controls(self._cands([11, 12]) + self._cands([13], prefix="H"), {1: ("prefix", "S")}, {}, D(2026, 7, 2), {}, (D(2026, 7, 2), D(2026, 9, 3)), 30, {1}, 90)
        assert [c["machine_id"] for c in sel["selected"]] == [11, 12]

    def test_same_dept_mode_ignores_band(self):
        idx = build_speed_index(_speed_rows([11], 400))
        sel = select_controls(self._cands([11]), {1: ("rpm", 150)}, idx, D(2026, 7, 2), {}, (D(2026, 7, 2), D(2026, 9, 3)), 30, {1}, 90, require_band=False)
        assert [c["machine_id"] for c in sel["selected"]] == [11]

    def test_did_by_band_weighted(self):
        bs = {m: 150 for m in (1, 2, 11, 12)}
        bs.update({3: 470, 13: 470})
        b = {1: _lds([1], 10, 2.0), 2: _lds([2], 10, 2.0), 3: _lds([3], 5, 2.0), 11: _lds([11], 10, 2.0), 12: _lds([12], 10, 2.0), 13: _lds([13], 5, 2.0)}
        a = {1: _lds([1], 10, 2.4), 2: _lds([2], 10, 2.4), 3: _lds([3], 5, 2.0), 11: _lds([11], 10, 2.2), 12: _lds([12], 10, 2.2), 13: _lds([13], 5, 2.2)}
        did = did_by_band({1: ("rpm", 150), 2: ("rpm", 150), 3: ("rpm", 470)}, {11: ("rpm", 150), 12: ("rpm", 150), 13: ("rpm", 470)},
                          b, a, bs)
        bands = {r["band"]: r for r in did["bands"]}
        assert bands["150 rpm"]["did_pct"] == 10.0 and bands["150 rpm"]["weight"] == 40
        assert bands["470 rpm"]["did_pct"] == -10.0 and bands["470 rpm"]["weight"] == 10
        assert did["net_delta_pct"] == round((10.0 * 40 - 10.0 * 10) / 50, 2)


# ---------------------------------------------------------------------------
# Flags and the mechanical section
# ---------------------------------------------------------------------------


class TestFlagsAndMechanical:
    def test_flags(self):
        b = _lds([1], 10, 2.0, eb="100")
        a = _lds([1], 10, 2.0, eb="200")
        assert loom_flags(b, a, speed_changed=True, has_standard=True) == ["S", "W"]
        assert loom_flags(b, b, False, False) == ["N"]
        # H: hours coverage 100 -> 0; D: std coverage < 50 on one side
        a2 = _lds([1], 10, 2.0, wh=0.0)
        f = loom_flags(b, a2, False, True)
        assert "H" in f and "D" in f
        # the quality flag is gone: a disjoint quality set raises nothing
        idx = build_speed_index(_speed_rows([1]))
        a3 = build_loom_days([_qrow(1, D(2026, 9, 1) + timedelta(days=i), 2.0, qid=8, qname="SACKING") for i in range(10)], idx, {})
        assert loom_flags(b, a3, False, True) == ["D"]
        assert "Q" not in lmi.FLAGS_LEGEND and set(lmi.FLAGS_LEGEND) == {"S", "H", "W", "D", "N"}

    def test_mechanical_section(self):
        """Per study loom BEFORE | AFTER: mechanical stoppage hours + breakdown count /
        minutes, then a total row per side. Excluded / dropped dates and non-study
        looms are ignored."""
        inp = make_inputs(study=(1, 2, 3), with_control=False)
        rows = build_mechanical(inp["mech_stoppage"], inp["breakdowns"], inp["windows"], inp["study_ids"], inp["loom_info"])
        assert [r["row_kind"] for r in rows] == ["loom"] * 6 + ["total", "total"]
        assert set(rows[0]) == {"machine_id", "loom", "side", "mech_stoppage_hours", "breakdown_count", "breakdown_minutes", "row_kind"}
        by = {(r["machine_id"], r["side"]): r for r in rows}
        assert by[(1, "BEFORE")]["mech_stoppage_hours"] == 3.5 and by[(1, "BEFORE")]["breakdown_count"] == 0
        assert by[(1, "AFTER")] == {"machine_id": 1, "loom": "S301", "side": "AFTER", "mech_stoppage_hours": 0.0,
                                    "breakdown_count": 1, "breakdown_minutes": 45, "row_kind": "loom"}
        assert by[(2, "BEFORE")]["mech_stoppage_hours"] == 0.0 and by[(2, "AFTER")]["mech_stoppage_hours"] == 0.0  # 02-Aug is EXCLUDED
        assert by[(3, "AFTER")]["breakdown_count"] == 2 and by[(3, "AFTER")]["breakdown_minutes"] == 30
        assert by[(None, "BEFORE")] == {"machine_id": None, "loom": "TOTAL", "side": "BEFORE", "mech_stoppage_hours": 3.5,
                                        "breakdown_count": 0, "breakdown_minutes": 0, "row_kind": "total"}
        assert by[(None, "AFTER")]["breakdown_count"] == 3 and by[(None, "AFTER")]["breakdown_minutes"] == 75
        # rows are accepted as Row-like objects too
        rows2 = build_mechanical([_row(m) for m in inp["mech_stoppage"]], [_row(m) for m in inp["breakdowns"]], inp["windows"], inp["study_ids"], inp["loom_info"])
        assert rows2 == rows
        assert build_mechanical(None, None, inp["windows"], inp["study_ids"], inp["loom_info"])[-1]["mech_stoppage_hours"] == 0.0


# ---------------------------------------------------------------------------
# Report builder over synthetic inputs (no DB)
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_happy_path_improved_with_control(self):
        inp = make_inputs()
        data = lmi.build_report(inp, _params())
        assert set(data) == {"chain", "windows", "verdict", "segments", "per_loom", "daywise", "mechanical", "evidence", "meta"}
        v = data["verdict"]
        assert set(v) == {"verdict", "stage_seq", "since_date", "stage_label", "efficiency", "efficiency_at_baseline_std",
                          "looms_improved", "looms_total", "eff_days_before", "eff_days_after",
                          "coverage_before_pct", "coverage_after_pct", "reason", "other_stage_note", "comparison"}
        assert set(v["efficiency"]) == {"before", "after", "delta_pts", "delta_pct", "net_delta_pct", "se", "within_noise"}
        assert v["verdict"] == "IMPROVED" and v["efficiency_at_baseline_std"] is None
        # before 02..31-Jul: 15 odd / 15 even days -> 51.25; after 05-Aug..03-Sep: 16 odd / 14 even -> 61.17
        assert v["efficiency"]["before"] == 51.25 and v["efficiency"]["after"] == 61.17
        assert v["efficiency"]["delta_pts"] == 9.92 and v["efficiency"]["delta_pct"] == round(9.92 * 100 / 51.25, 2)
        assert v["efficiency"]["net_delta_pct"] is not None and v["efficiency"]["net_delta_pct"] > 15
        assert v["looms_improved"] == 6 and v["looms_total"] == 6 and v["stage_seq"] == 1
        assert v["since_date"] == "2026-08-05" and v["eff_days_before"] == 30 and v["eff_days_after"] == 30
        assert v["coverage_before_pct"] == 100.0 and v["coverage_after_pct"] == 100.0
        labels = [s["label"] for s in data["segments"]]
        assert labels == ["Before", "Before (control)", "After-1 (Initial)", "After-1 (Initial) (control)"]
        seg = data["segments"][2]
        assert set(seg) == {"seq", "label", "stage_seq", "kind", "date_from", "date_to", "days", "looms_n", "looms_total", "loom_days",
                            "days_run", "producing_days", "eff_days", "hours_run", "std_coverage_pct", "hours_coverage_pct",
                            "efficiency_pct", "efficiency_at_baseline_std_pct", "delta_vs_baseline_pts", "delta_vs_baseline_pct",
                            "delta_vs_previous_pts", "delta_vs_previous_pct", "net_vs_control_pct", "se", "verdict", "is_control"}
        assert seg["delta_vs_baseline_pct"] == v["efficiency"]["delta_pct"] and seg["verdict"] == "IMPROVED" and seg["efficiency_pct"] == 61.17
        assert data["segments"][3]["is_control"] and abs(data["segments"][3]["delta_vs_baseline_pct"]) < 0.2  # flat control (odd/even day mix)
        assert data["windows"]["before"] == {"date_from": "2026-07-02", "date_to": "2026-07-31", "days": 30, "extended": False}
        assert data["windows"]["after"][0] == {"stage_seq": 1, "date_from": "2026-08-05", "date_to": "2026-09-03", "days": 30, "cut_reason": "window"}
        assert data["windows"]["excluded"] == [{"stage_seq": 1, "date_from": "2026-08-01", "date_to": "2026-08-04"}]
        kinds = [r["row_kind"] for r in data["per_loom"]]
        assert kinds.count("loom") == 6 and kinds[-2:] == ["study_total", "control_total"]
        pl = data["per_loom"][0]
        assert set(pl) == {"machine_id", "loom", "line_no", "speed_band", "since_date", "producing_days_before", "producing_days_after",
                           "eff_days_before", "eff_days_after", "eff_before", "eff_after", "delta_pts", "delta_pct",
                           "eff_baseline_std_before", "eff_baseline_std_after", "std_speed_baseline", "std_speed_latest",
                           "flags", "in_stage", "row_kind"}
        assert pl["eff_before"] == 51.25 and pl["eff_after"] == 61.17 and pl["delta_pts"] == 9.92 and pl["speed_band"] == "150 rpm"
        assert all(r["flags"] == [] for r in data["per_loom"] if r["row_kind"] == "loom")
        assert data["per_loom"][-2]["loom"] == "GROUP TOTAL" and data["per_loom"][-2]["eff_after"] == 61.17
        assert len(data["daywise"]) == (D(2026, 9, 3) - D(2026, 7, 2)).days + 1
        dw = data["daywise"][0]
        assert set(dw) == {"tran_date", "phase", "study_looms_run", "study_eff", "study_rolling7", "control_eff", "control_rolling7", "gap", "markers"}
        assert dw["phase"] == "BEFORE" and data["daywise"][30]["phase"] == "EXCLUDED" and data["daywise"][-1]["phase"] == "AFTER-1"
        assert any("Stage 1" in m for m in data["daywise"][30]["markers"])
        assert data["daywise"][-1]["study_rolling7"] is not None and data["daywise"][-1]["gap"] > 0
        mech = {(c["machine_id"], c["side"]): c for c in data["mechanical"]}
        assert mech[(1, "BEFORE")]["mech_stoppage_hours"] == 3.5 and mech[(1, "AFTER")]["breakdown_count"] == 1
        assert mech[(None, "AFTER")]["row_kind"] == "total" and mech[(None, "AFTER")]["breakdown_minutes"] == 75
        assert set(data["meta"]) == {"source", "thresholds", "notes", "server_ms", "control", "flags_legend"}
        assert isinstance(data["meta"]["server_ms"], int)
        assert data["meta"]["control"]["looms_n"] == 6 and data["meta"]["thresholds"]["n1"] == 30
        assert set(data["meta"]["flags_legend"]) == {"S", "H", "W", "D", "N"}
        stage0 = data["chain"]["stages"][0]
        assert stage0["loom_count"] == 6 and data["chain"]["loom_group"]["group_name"] == "Group 1"
        assert stage0["done_by_label"] == "In-house" and "total_cost" not in stage0
        assert data["evidence"][0] == {"label": "Yardstick", "value": "Efficiency %"}
        assert not any("headline" in n for n in data["meta"]["notes"])

    def test_no_control_and_flat_after_is_no_clear_change(self):
        inp = make_inputs(with_control=False, after_gain=0.02)
        data = lmi.build_report(inp, _params(control="none"))
        assert data["verdict"]["verdict"] == "NO_CLEAR_CHANGE"
        assert data["meta"]["control"]["looms_n"] == 0 and data["verdict"]["efficiency"]["net_delta_pct"] is None
        assert [s["label"] for s in data["segments"]] == ["Before", "After-1 (Initial)"]
        assert data["per_loom"][-1]["row_kind"] == "study_total"

    def test_second_stage_thin_shows_stage_one_in_strip(self):
        ev2 = _event(2, D(2026, 9, 1), "RECTIFY", parent=1, root=1, settle=3, title="Tweak")
        snaps2 = [_snap(2, m, D(2026, 9, 1)) for m in (1, 2, 3)]
        inp = make_inputs(stages_spec=[(ev2, snaps2)], last_data=D(2026, 9, 8))
        data = lmi.build_report(inp, _params())
        assert len(data["chain"]["stages"]) == 2 and data["chain"]["stages"][1]["loom_count"] == 3
        assert data["verdict"]["stage_seq"] == 1 and "verdict shown for stage 1" in data["verdict"]["other_stage_note"]
        seg2 = [s for s in data["segments"] if s["stage_seq"] == 2 and not s["is_control"]][0]
        assert seg2["verdict"] == "NOT_ENOUGH_DATA" and seg2["looms_total"] == 3
        # looms 1..3 have their stage-1 after cut at 31-Aug by the next event; the summary row
        # reports the reason of the looms that set the max date (4..6, full window)
        assert inp["windows"][1]["stages"][1]["after"] == (D(2026, 8, 5), D(2026, 8, 31))
        assert inp["windows"][1]["stages"][1]["cut_reason"] == "next_event"
        assert data["windows"]["after"][0]["cut_reason"] == "window" and data["windows"]["after"][0]["date_to"] == "2026-09-03"
        assert data["windows"]["after"][1]["cut_reason"] == "last_data_date"

    def test_speed_change_inside_span_sets_s_flag_and_baseline_yardstick(self):
        inp = make_inputs(with_control=False)
        inp["speed_rows"].append({"weaving_target_map_id": 999, "machine_id": 1, "value_role": "standard", "value": 165, "effective_date": D(2026, 8, 10)})
        data = lmi.build_report(inp, _params(control="none"))
        row = [r for r in data["per_loom"] if r["machine_id"] == 1][0]
        assert "S" in row["flags"] and row["std_speed_baseline"] == 150 and row["std_speed_latest"] == 165
        assert row["eff_baseline_std_after"] > row["eff_after"]
        # per-loom delta is on the verdict basis (baseline standard) for this stage
        assert row["delta_pts"] == round(row["eff_baseline_std_after"] - row["eff_baseline_std_before"], 3)
        v = data["verdict"]
        assert v["efficiency_at_baseline_std"] is not None and set(v["efficiency_at_baseline_std"]) == {"before", "after", "delta_pts", "delta_pct"}
        assert v["efficiency_at_baseline_std"]["after"] > v["efficiency"]["after"]
        assert data["evidence"][0]["value"] == "Efficiency % at the baseline standard"
        assert any("flag S" in n for n in data["meta"]["notes"])
        assert any("STD SPEED 150 → 165" in m for d in data["daywise"] for m in d["markers"])

    def test_no_picks_is_not_enough_data_with_cause_no_fallback(self):
        """Without SQC pick readings the old report fell back to yds per hour; now the
        verdict is NOT_ENOUGH_DATA and the reason / note name the cause."""
        inp = make_inputs(with_control=False)
        inp["picks"] = []
        data = lmi.build_report(inp, _params(control="none"))
        v = data["verdict"]
        assert v["verdict"] == "NOT_ENOUGH_DATA" and v["efficiency"]["before"] is None and v["efficiency"]["after"] is None
        assert "before: standard coverage 0 % < 80 % (no SQC pick reading on 180 loom-days)" in v["reason"]
        assert v["coverage_before_pct"] == 0.0 and v["eff_days_before"] == 0
        assert any(n.startswith("verdict NOT_ENOUGH_DATA for stage 1") and "no fallback" in n for n in data["meta"]["notes"])
        assert any("no SQC pick reading" in n for n in data["meta"]["notes"])
        seg = [s for s in data["segments"] if s["stage_seq"] == 1][0]
        assert seg["verdict"] == "NOT_ENOUGH_DATA" and seg["efficiency_pct"] is None and seg["std_coverage_pct"] == 0.0
        # no weaver marks: hours coverage 0 -> standard coverage 0 too, cause named
        inp2 = make_inputs(with_control=False)
        for r in inp2["quality_rows"]:
            r["working_hours"] = 0.0
            r["total_jugar_manned"] = 0
        data2 = lmi.build_report(inp2, _params(control="none"))
        assert data2["verdict"]["verdict"] == "NOT_ENOUGH_DATA" and "no weaver marked on 180 loom-days" in data2["verdict"]["reason"]

    # verdict.comparison — owner 2026-09-29: "a comparison of the change in this loom
    # set vs others where this was not done", side by side, from the same aggregates
    # as the ladder rows of the judged stage.

    def test_comparison_with_control(self):
        data = lmi.build_report(make_inputs(), _params())
        v = data["verdict"]
        c = v["comparison"]
        assert set(c) == {"basis", "stage_seq", "description", "study", "control", "net_delta_pts", "net_delta_pct"}
        assert c["basis"] == "efficiency" and c["stage_seq"] == 1
        assert c["description"] == data["meta"]["control"]["description"]
        assert c["description"].startswith("6 un-modified looms of the same dept in the study speed bands")
        assert set(c["study"]) == set(c["control"]) == {"looms_n", "before", "after", "delta_pts", "delta_pct"}
        e = v["efficiency"]
        assert c["study"] == {"looms_n": 6, "before": e["before"], "after": e["after"],
                              "delta_pts": e["delta_pts"], "delta_pct": e["delta_pct"]}
        # the control side is exactly the ladder's control rows of the stage
        cb = [s for s in data["segments"] if s["is_control"] and s["seq"] == 0][0]
        ca = [s for s in data["segments"] if s["is_control"] and s["seq"] == 1][0]
        assert c["control"]["looms_n"] == 6 == data["meta"]["control"]["looms_n"]
        assert c["control"]["before"] == cb["efficiency_pct"] and c["control"]["after"] == ca["efficiency_pct"]
        assert c["control"]["delta_pts"] == ca["delta_vs_baseline_pts"] and c["control"]["delta_pct"] == ca["delta_vs_baseline_pct"]
        assert abs(c["control"]["delta_pts"]) < 0.2  # flat control
        assert c["net_delta_pts"] == round(c["study"]["delta_pts"] - c["control"]["delta_pts"], 3)
        assert c["net_delta_pts"] > 9
        assert c["net_delta_pct"] is not None and c["net_delta_pct"] == e["net_delta_pct"]

    def test_comparison_without_control(self):
        data = lmi.build_report(make_inputs(with_control=False, after_gain=0.02), _params(control="none"))
        v = data["verdict"]
        c = v["comparison"]
        assert c["control"] is None and c["net_delta_pts"] is None and c["net_delta_pct"] is None
        assert c["basis"] == "efficiency" and c["stage_seq"] == 1 and c["description"] == "none"
        e = v["efficiency"]
        assert c["study"] == {"looms_n": 6, "before": e["before"], "after": e["after"],
                              "delta_pts": e["delta_pts"], "delta_pct": e["delta_pct"]}
        # a control mode with no eligible candidates: still None, and the note names it
        data2 = lmi.build_report(make_inputs(with_control=False), _params(control="same_dept"))
        assert data2["verdict"]["comparison"]["control"] is None
        assert any(n.startswith("No eligible controls") for n in data2["meta"]["notes"])

    def test_comparison_on_baseline_std_basis(self):
        inp = make_inputs()
        inp["speed_rows"].append({"weaving_target_map_id": 999, "machine_id": 1, "value_role": "standard", "value": 165, "effective_date": D(2026, 8, 10)})
        data = lmi.build_report(inp, _params())
        v = data["verdict"]
        c = v["comparison"]
        assert c["basis"] == "efficiency_at_baseline_std" and c["stage_seq"] == 1
        b = v["efficiency_at_baseline_std"]
        assert c["study"] == {"looms_n": 6, "before": b["before"], "after": b["after"],
                              "delta_pts": b["delta_pts"], "delta_pct": b["delta_pct"]}
        assert c["study"]["after"] > v["efficiency"]["after"]
        # the control side is on the same basis as the ladder's control rows of the
        # stage (whatever those carry — today the constant-yardstick figure is only
        # computed for study looms, so the control and net figures follow the ladder)
        cb = [s for s in data["segments"] if s["is_control"] and s["seq"] == 0][0]
        ca = [s for s in data["segments"] if s["is_control"] and s["seq"] == 1][0]
        assert c["control"]["looms_n"] == 6
        assert c["control"]["before"] == cb["efficiency_at_baseline_std_pct"] and c["control"]["after"] == ca["efficiency_at_baseline_std_pct"]
        assert c["control"]["delta_pts"] == ca["delta_vs_baseline_pts"] and c["control"]["delta_pct"] == ca["delta_vs_baseline_pct"]
        assert c["net_delta_pct"] == v["efficiency"]["net_delta_pct"]
        assert (c["net_delta_pts"] is None) == (c["control"]["delta_pts"] is None)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class TestEndpoint:
    def setup_method(self):
        self.session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def _q(self, **kw):
        q = {"co_id": 9, "branch_id": 12, "modification_id": 1, "window_days": 30}
        q.update(kw)
        return q

    def test_param_validation_400s(self):
        assert client.get(URL, params={"branch_id": 12, "modification_id": 1}).status_code == 400
        assert "branch_id" in client.get(URL, params={"co_id": 9, "modification_id": 1}).json()["detail"]
        assert "modification_id" in client.get(URL, params={"co_id": 9, "branch_id": 12}).json()["detail"]
        r = client.get(URL, params=self._q(window_days=IMPACT_MAX_WINDOW_DAYS + 1))
        assert r.status_code == 400 and str(IMPACT_MAX_WINDOW_DAYS) in r.json()["detail"]
        assert "control" in client.get(URL, params=self._q(control="magic")).json()["detail"]
        assert "control_group_id" in client.get(URL, params=self._q(control="control_group")).json()["detail"]
        assert client.get(URL, params=self._q(window_days="x")).status_code == 400
        assert client.get(URL, params=self._q(settle_days=-1)).status_code == 400

    def test_unknown_modification_400(self):
        self.session.execute.return_value = _res(one=None)
        r = client.get(URL, params=self._q())
        assert r.status_code == 400 and "not found" in r.json()["detail"]

    def test_study_loom_cap_400(self):
        snaps = [_snap(1, m, D(2026, 8, 1)) for m in range(1, IMPACT_MAX_STUDY_LOOMS + 2)]
        self.session.execute.side_effect = [
            _res(one={"modification_id": 1, "root_modification_id": 1, "branch_id": 12}),   # R1
            _res(rows=[_event(1, D(2026, 8, 1))]),                                            # R2
            _res(rows=snaps),                                                                 # R3
        ]
        r = client.get(URL, params=self._q(control="none"))
        assert r.status_code == 400 and str(IMPACT_MAX_STUDY_LOOMS) in r.json()["detail"]

    def test_span_cap_400(self):
        ev2 = _event(2, D(2026, 12, 1), "FOLLOWUP", parent=1, root=1)
        self.session.execute.side_effect = [
            _res(one={"modification_id": 2, "root_modification_id": 1, "branch_id": 12}),   # R1 (a stage id resolves to the root)
            _res(rows=[_event(1, D(2026, 8, 1)), ev2]),                                       # R2
            _res(rows=[_snap(1, 1, D(2026, 8, 1)), _snap(2, 1, D(2026, 12, 1))]),             # R3
            _res(one={"last_data_date": D(2027, 1, 31)}),                                     # R4
            _res(rows=[]),                                                                    # R5 spans
            _res(rows=[]),                                                                    # R6 row dates
            _res(rows=[]),                                                                    # R7 producing
            _res(rows=[]),                                                                    # R8 speed rows
        ]
        r = client.get(URL, params=self._q(window_days=60, control="none"))
        assert r.status_code == 400 and str(IMPACT_MAX_SPAN_DAYS) in r.json()["detail"]

    def test_happy_path_patches_the_query_layer(self):
        with patch("src.juteProduction.loom_modification_impact.load_report_inputs", return_value=make_inputs()) as load:
            r = client.get(URL, params=self._q())
        assert r.status_code == 200
        assert load.call_count == 1 and load.call_args[0][1]["window_days"] == 30
        assert set(load.call_args[0][1]) == {"co_id", "branch_id", "modification_id", "window_days", "settle_days", "control", "control_group_id"}
        data = r.json()["data"]
        assert data["verdict"]["verdict"] == "IMPROVED"
        assert data["verdict"]["efficiency"]["within_noise"] is False
        assert isinstance(data["meta"]["server_ms"], int) and "metrics" not in data["meta"]
        assert data["windows"]["last_data_date"] == "2026-09-10"

    def test_removed_params_are_ignored(self):
        with patch("src.juteProduction.loom_modification_impact.load_report_inputs", return_value=make_inputs()) as load:
            r = client.get(URL, params=self._q(metric="kg_per_loom_day", same_quality_only="1", weaving_quality_id=7))
        assert r.status_code == 200
        p = load.call_args[0][1]
        assert "metric" not in p and "same_quality_only" not in p and "weaving_quality_id" not in p

    def test_db_error_500(self):
        self.session.execute.side_effect = RuntimeError("db exploded")
        r = client.get(URL, params=self._q())
        assert r.status_code == 500 and "db exploded" in r.json()["detail"]

    def test_options_endpoint(self):
        self.session.execute.side_effect = [
            _res(rows=[{"modification_id": 1, "mod_date": D(2026, 8, 1), "title": "Shuttle change", "mod_kind": "INITIAL",
                        "loom_group_name": "Group 1", "loom_count": 6, "stage_count": 2}]),
            _res(rows=[{"loom_group_id": 9, "group_code": "C1", "group_name": "Line 4 controls"}]),
        ]
        r = client.get(OPT_URL, params={"co_id": 9, "branch_id": 12})
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["modifications"][0]["label"] == "01-08-26 · Shuttle change · Group 1 / 6 looms"
        assert d["modifications"][0]["stage_count"] == 2
        assert d["control_groups"] == [{"loom_group_id": 9, "group_name": "Line 4 controls"}]
        assert set(d) == {"modifications", "control_groups", "window_presets", "default_window_days"}
        assert self.session.execute.call_count == 2
        assert d["window_presets"] == [30, 60, 90, 180] and d["default_window_days"] == 60
        assert client.get(OPT_URL, params={"branch_id": 12}).status_code == 400


class TestConstantsPublished:
    def test_thresholds_in_meta(self):
        data = lmi.build_report(make_inputs(with_control=False), _params(control="none"))
        th = data["meta"]["thresholds"]
        assert th["MIN_LOOM_DAYS_GROUP"] == MIN_LOOM_DAYS_GROUP and th["SE_MULTIPLIER"] == SE_MULTIPLIER
        assert th["IMPACT_MAX_LOOMS"] == IMPACT_MAX_LOOMS and th["VERDICT_THRESHOLD_PCT"] == VERDICT_THRESHOLD_PCT
        assert {"n1", "n2", "s_p", "se"} <= set(th)
        assert any(e["label"] == "SE of the difference" for e in data["evidence"])
