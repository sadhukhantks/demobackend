"""Tests for the Low Performer Follow-up (src/juteProduction/low_performer_followup.py).

Portal persona: DB + auth mocked via app.dependency_overrides on the exact symbols
the router imports (the router is mounted on its own app, like the Low Producer
sibling tests). The validation rules and the performance / comparison builders are
pure functions and are tested directly.

Covered:
  * validation — dept codes, dates (future start, review before start, start after
    history), close rules, event rules per type (REMARK / NOTICE / HOURS_CUT),
    hour-cut fit against the spell (incl. hours already cut), the window resolver
    (explicit range, cap, default clipping, baseline before a same-day start)
  * builders — weaving / spinning / winding normalisation, winder -> machine map,
    person rows + summary, comparison (a) same machine other spells, (b) same
    quality same dates, (c) weekly trend anchored on the start date, row shaping
    (review_due, days_tracked)
  * endpoints — duplicate-open guard (409 on create and reopen), employee not in
    branch (400), create happy path, timeline closed guard, hour-cut over the spell
    (400), event_type immutable, tracked lookup, performance via the Low Producer
    fetch helpers (patched), param errors (400)
  * review fixes 2026-10-07 — menu access level per endpoint (403 below view /
    create / edit, stored branch for id-based calls), the DB unique key's
    IntegrityError mapped to 409 with the app guard bypassed, notice-scan gate on
    the generic attachment API, hour-cut caps per person + date across follow-ups
    (spell, shift letter A = A / A1 / A2, 24 h day), window defaults (closed date,
    whole weeks, 14-day weaving default), close not before the last entry,
    non-object / non-finite input -> 400, std_kg null without a standard, same
    machine treating A vs A1 / A2 as the same shift
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pymysql
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from src.common.attachments import router as attachments_router_module
from src.juteProduction import low_performer_followup as lpf
from src.juteProduction.low_performer_followup import (
    LEVEL_CREATE,
    LEVEL_EDIT,
    LEVEL_VIEW,
    build_person_rows,
    build_person_summary,
    build_same_machine,
    build_same_quality,
    build_weekly_trend,
    build_winder_machine_map,
    check_hours_cut_caps,
    check_notice_attachment_access,
    clean_event,
    followup_access_level,
    get_current_user_with_refresh,
    get_tenant_db,
    hour_cut_limits,
    low_performer_followup_router,
    normalize_spinning_rows,
    normalize_weaving_rows,
    normalize_winding_rows,
    resolve_window,
    shape_followup_row,
    validate_close,
    validate_dept,
    validate_followup_dates,
    validate_start_vs_history,
    week_no_of,
)
from src.juteProduction.low_performer_followup_models import JuteProdFollowupEvent
from src.juteProduction.low_performer_followup_query import (
    get_winding_machine_by_winder_range_query,
)

PREFIX = "/api/lowPerformerFollowup"
ATT = "/api/attachments"
app = FastAPI()
app.include_router(low_performer_followup_router, prefix=PREFIX)
app.include_router(attachments_router_module.router, prefix=ATT)
client = TestClient(app)

TODAY = date(2026, 10, 7)


def _status(exc_info) -> int:
    return exc_info.value.status_code


# =============================================================================
# Validation rules
# =============================================================================


class TestValidation:
    def test_dept_codes(self):
        assert validate_dept("weaving") == "WEAVING"
        assert validate_dept(" SPINNING ") == "SPINNING"
        assert validate_dept("WINDING") == "WINDING"
        for bad in ("DRAWING", "", None, 5):
            with pytest.raises(HTTPException) as e:
                validate_dept(bad)
            assert _status(e) == 400

    def test_followup_dates(self):
        validate_followup_dates(date(2026, 10, 1), date(2026, 10, 15), TODAY)
        validate_followup_dates(TODAY, None, TODAY)
        with pytest.raises(HTTPException, match="future"):
            validate_followup_dates(date(2026, 10, 8), None, TODAY)
        with pytest.raises(HTTPException, match="review_date"):
            validate_followup_dates(date(2026, 10, 5), date(2026, 10, 4), TODAY)

    def test_start_may_not_pass_history(self):
        validate_start_vs_history(date(2026, 9, 1), date(2026, 9, 1), None)
        with pytest.raises(HTTPException, match="first timeline entry"):
            validate_start_vs_history(date(2026, 9, 2), date(2026, 9, 1), None)
        with pytest.raises(HTTPException, match="closed_date"):
            validate_start_vs_history(date(2026, 9, 10), None, date(2026, 9, 5))

    def test_close_rules(self):
        assert validate_close(date(2026, 9, 1), TODAY, "improved", TODAY) == "IMPROVED"
        with pytest.raises(HTTPException, match="close_reason"):
            validate_close(date(2026, 9, 1), TODAY, "FIRED", TODAY)
        with pytest.raises(HTTPException, match="before start_date"):
            validate_close(date(2026, 9, 1), date(2026, 8, 31), "LEFT", TODAY)
        with pytest.raises(HTTPException, match="future"):
            validate_close(date(2026, 9, 1), date(2026, 10, 8), "LEFT", TODAY)
        # not before the last active timeline entry
        assert validate_close(date(2026, 9, 1), date(2026, 9, 10), "LEFT", TODAY,
                              date(2026, 9, 10)) == "LEFT"
        with pytest.raises(HTTPException, match="last timeline entry \\(2026-09-10\\)"):
            validate_close(date(2026, 9, 1), date(2026, 9, 9), "LEFT", TODAY, date(2026, 9, 10))

    def test_remark_needs_text(self):
        start = date(2026, 9, 1)
        out = clean_event({"event_date": "2026-09-05", "remarks": " ok "}, "REMARK", start, TODAY)
        assert out["remarks"] == "ok" and out["spell_id"] is None
        with pytest.raises(HTTPException, match="remarks is required"):
            clean_event({"event_date": "2026-09-05"}, "REMARK", start, TODAY)

    def test_notice_needs_type_and_drops_foreign_fields(self):
        start = date(2026, 9, 1)
        out = clean_event(
            {"event_date": "2026-09-05", "notice_type": "show_cause", "issued_by": "HoD",
             "spell_id": 3, "hours_cut": 2},
            "NOTICE", start, TODAY,
        )
        assert out["notice_type"] == "SHOW_CAUSE"
        assert out["issued_by"] == "HoD"
        assert out["spell_id"] is None and out["hours_cut"] is None
        with pytest.raises(HTTPException, match="notice_type"):
            clean_event({"event_date": "2026-09-05"}, "NOTICE", start, TODAY)
        with pytest.raises(HTTPException, match="notice_type"):
            clean_event({"event_date": "2026-09-05", "notice_type": "SACKED"}, "NOTICE", start, TODAY)

    def test_hours_cut_rules(self):
        start = date(2026, 9, 1)
        ok = {"event_date": "2026-09-05", "spell_id": "4", "hours_cut": "2.5", "remarks": "late"}
        out = clean_event(ok, "HOURS_CUT", start, TODAY)
        assert out["spell_id"] == 4 and out["hours_cut"] == 2.5
        with pytest.raises(HTTPException, match="spell_id"):
            clean_event({**ok, "spell_id": None}, "HOURS_CUT", start, TODAY)
        with pytest.raises(HTTPException, match="greater than 0"):
            clean_event({**ok, "hours_cut": 0}, "HOURS_CUT", start, TODAY)
        with pytest.raises(HTTPException, match="greater than 0"):
            clean_event({**ok, "hours_cut": -1}, "HOURS_CUT", start, TODAY)
        with pytest.raises(HTTPException, match="reason"):
            clean_event({**ok, "remarks": "  "}, "HOURS_CUT", start, TODAY)

    def test_event_dates(self):
        start = date(2026, 9, 1)
        with pytest.raises(HTTPException, match="future"):
            clean_event({"event_date": "2026-10-08", "remarks": "x"}, "REMARK", start, TODAY)
        with pytest.raises(HTTPException, match="before the follow-up start"):
            clean_event({"event_date": "2026-08-31", "remarks": "x"}, "REMARK", start, TODAY)
        with pytest.raises(HTTPException, match="YYYY-MM-DD"):
            clean_event({"event_date": "05/09/2026", "remarks": "x"}, "REMARK", start, TODAY)
        with pytest.raises(HTTPException, match="event_type"):
            clean_event({"event_date": "2026-09-05"}, "PRAISE", start, TODAY)

    def test_window_explicit_and_capped(self):
        start = date(2026, 9, 1)
        d_from, d_to, clipped, notes = resolve_window(
            start, date(2026, 9, 10), date(2026, 9, 20), TODAY, 31
        )
        assert (d_from, d_to, clipped, notes) == (date(2026, 9, 10), date(2026, 9, 20), False, [])
        with pytest.raises(HTTPException, match="at most 31"):
            resolve_window(start, date(2026, 8, 1), date(2026, 9, 1), TODAY, 31)
        with pytest.raises(HTTPException, match="before"):
            resolve_window(start, date(2026, 9, 20), date(2026, 9, 10), TODAY, 31)
        with pytest.raises(HTTPException, match="both"):
            resolve_window(start, date(2026, 9, 20), None, TODAY, 31)

    def test_window_default(self):
        # start .. yesterday when it fits
        assert resolve_window(date(2026, 9, 20), None, None, TODAY, 31)[:3] == (
            date(2026, 9, 20), date(2026, 10, 6), False,
        )
        # clipped to the last max_days, start moved UP to a week of the follow-up:
        # earliest 09-06 is 36 days after 08-01 -> 6 whole weeks -> 09-12
        d_from, d_to, clipped, notes = resolve_window(date(2026, 8, 1), None, None, TODAY, 31)
        assert (d_from, d_to, clipped) == (date(2026, 9, 12), date(2026, 10, 6), True)
        assert (d_from - date(2026, 8, 1)).days % 7 == 0
        assert notes and "the last 25 days" in notes[0] and "up to 31 days" in notes[0]
        # started today -> the week before as a baseline
        d_from, d_to, clipped, notes = resolve_window(TODAY, None, None, TODAY, 31)
        assert (d_from, d_to, clipped) == (date(2026, 9, 30), date(2026, 10, 6), False)
        assert "baseline" in notes[0]

    def test_window_default_days_shorter_than_max(self):
        # weaving: default 14 days, explicit dates still up to 31
        d_from, d_to, clipped, notes = resolve_window(
            date(2026, 8, 1), None, None, TODAY, 31, default_days=14
        )
        assert d_to == date(2026, 10, 6) and clipped
        assert (d_to - d_from).days + 1 <= 14 and (d_from - date(2026, 8, 1)).days % 7 == 0
        assert d_from == date(2026, 9, 26)
        assert "at most 14 days" in notes[0] and "up to 31 days" in notes[0]
        assert resolve_window(date(2026, 8, 1), date(2026, 9, 1), date(2026, 9, 30), TODAY, 31,
                              default_days=14)[:3] == (date(2026, 9, 1), date(2026, 9, 30), False)
        # whole weeks on every start weekday / window
        for offset in range(0, 60):
            start = date(2026, 6, 1) + timedelta(days=offset)
            f, t, c, _ = resolve_window(start, None, None, TODAY, 31, default_days=14)
            if c:
                assert (f - start).days % 7 == 0 and (t - f).days + 1 <= 14 and f <= t

    def test_window_closed_followup_ends_at_close(self):
        d_from, d_to, clipped, notes = resolve_window(
            date(2026, 9, 1), None, None, TODAY, 31, closed_date=date(2026, 9, 20)
        )
        assert (d_from, d_to, clipped) == (date(2026, 9, 1), date(2026, 9, 20), False)
        assert "closed on 2026-09-20" in notes[0]
        # closed long ago + clipping: whole weeks back from the close date
        d_from, d_to, clipped, _ = resolve_window(
            date(2026, 7, 1), None, None, TODAY, 31, default_days=14, closed_date=date(2026, 9, 20)
        )
        assert (d_from, d_to, clipped) == (date(2026, 9, 9), date(2026, 9, 20), True)
        # closed today / yesterday -> yesterday as usual
        assert resolve_window(date(2026, 9, 20), None, None, TODAY, 31,
                              closed_date=TODAY)[1] == date(2026, 10, 6)

    def test_non_finite_and_huge_numbers_are_400(self):
        for bad in (float("nan"), float("inf"), "-inf", "1e400", "abc", [1]):
            with pytest.raises(HTTPException) as e:
                lpf._opt_float(bad, "hours_cut")
            assert _status(e) == 400
        for bad in (float("inf"), "x", {"a": 1}):
            with pytest.raises(HTTPException) as e:
                lpf._req_int(bad, "co_id")
            assert _status(e) == 400
        assert lpf._opt_float("2.5", "hours_cut") == 2.5


# Branch spells as the hour-cut query returns them (spell hours + its shift's hours).
DEV3_SPELLS = [  # no full-shift A / B spell: A1 5 h + A2 3 h on an 8 h shift
    {"spell_id": 1, "spell_code": "A1", "working_hours": 5.0, "shift_hours": 8.0},
    {"spell_id": 3, "spell_code": "A2", "working_hours": 3.0, "shift_hours": 8.0},
    {"spell_id": 2, "spell_code": "B1", "working_hours": 3.0, "shift_hours": 8.0},
    {"spell_id": 4, "spell_code": "B2", "working_hours": 5.0, "shift_hours": 8.0},
    {"spell_id": 5, "spell_code": "C", "working_hours": 8.0, "shift_hours": 8.0},
]
SLS_SPELLS = [  # sls branch 29: a full-shift A spell next to A1 / A2
    {"spell_id": 109, "spell_code": "A", "working_hours": 8.0, "shift_hours": 8.0},
    {"spell_id": 97, "spell_code": "A1", "working_hours": 5.0, "shift_hours": 8.0},
    {"spell_id": 98, "spell_code": "A2", "working_hours": 3.0, "shift_hours": 8.0},
    {"spell_id": 101, "spell_code": "C", "working_hours": 7.5, "shift_hours": 7.5},
]
D = date(2026, 9, 5)


def _cut(code, hours):
    return {"spell_code": code, "hours_cut": hours}


class TestHourCutCaps:
    def test_limits(self):
        assert hour_cut_limits("A1", DEV3_SPELLS) == (5.0, "A", 8.0, ["A1", "A2"])
        assert hour_cut_limits("A", SLS_SPELLS) == (8.0, "A", 8.0, ["A", "A1", "A2"])
        assert hour_cut_limits("C", SLS_SPELLS) == (7.5, "C", 7.5, ["C"])
        # nothing configured -> 8 h fallback for both
        bare = [{"spell_code": "A1", "working_hours": None, "shift_hours": None}]
        assert hour_cut_limits("A1", bare) == (8.0, "A", 8.0, ["A1"])

    def test_spell_cap(self):
        check_hours_cut_caps(5.0, "A1", DEV3_SPELLS, [], D)
        with pytest.raises(HTTPException) as e:
            check_hours_cut_caps(2.5, "A1", DEV3_SPELLS, [_cut("A1", 3.0)], D)
        assert "spell A1 (5 h)" in e.value.detail
        assert "3 h already cut for this person, 2 h left" in e.value.detail

    def test_a1_a2_share_shift_a(self):
        # dev3: A1 5 h + A2 3 h fill the 8 h A shift exactly
        check_hours_cut_caps(3.0, "A2", DEV3_SPELLS, [_cut("A1", 5.0)], D)
        # sls: a full-shift A cut of 6 h leaves 2 h for A1 / A2 together
        check_hours_cut_caps(2.0, "A1", SLS_SPELLS, [_cut("A", 6.0)], D)
        with pytest.raises(HTTPException) as e:
            check_hours_cut_caps(3.0, "A1", SLS_SPELLS, [_cut("A", 6.0)], D)
        msg = e.value.detail
        assert "shift A (spells A, A1, A2 share it, 8 h)" in msg
        assert "6 h already cut for this person, 2 h left" in msg and "2026-09-05" in msg
        with pytest.raises(HTTPException, match="shift A"):
            check_hours_cut_caps(1.0, "A", SLS_SPELLS, [_cut("A1", 5.0), _cut("A2", 3.0)], D)
        # another shift letter is not affected
        check_hours_cut_caps(7.5, "C", SLS_SPELLS, [_cut("A", 8.0)], D)

    def test_day_cap(self):
        general = [{"spell_code": "G1", "working_hours": 12.0, "shift_hours": None}]
        cuts = [_cut("A", 8.0), _cut("B", 8.0), _cut("C", 8.0)]
        with pytest.raises(HTTPException) as e:
            check_hours_cut_caps(1.0, "G1", general, cuts, D)
        assert "the day (24 h)" in e.value.detail and "24 h already cut" in e.value.detail
        assert "0 h left" in e.value.detail
        check_hours_cut_caps(1.0, "G1", general, cuts[:2], D)


# =============================================================================
# Builders
# =============================================================================


def _row(d, spell, mc, eb, eff, q=7, prod=50.0, shift=None, name=None):
    return {
        "tran_date": d,
        "shift": shift or spell[0],
        "spell_code": spell,
        "machine_id": mc,
        "machine": None if mc is None else f"M{mc}",
        "eb_id": eb,
        "eb_no": None if eb is None else f"E{eb}",
        "worker_name": name or (None if eb is None else f"Worker {eb}"),
        "quality_id": q,
        "quality": f"Q{q}",
        "prod_kg": prod,
        "std_kg": 100.0,
        "eff_pct": eff,
        "hours": 8.0,
    }


class TestNormalisation:
    def test_weaving(self):
        out = normalize_weaving_rows([
            {"tran_date": "2026-09-01", "shift": "A", "spell_code": "A1", "machine_id": 3,
             "loom": "Loom 3", "mech_code": "L3", "eb_id": 9, "eb_no": "E9", "worker_name": "W",
             "quality_id": 7, "quality_name": "HESSIAN", "act_prod_kg": 61.234,
             "prod_100_kg": 100.0, "eff_pct": 0.0, "hours": 5.0},
        ])[0]
        assert out["machine"] == "Loom 3" and out["quality"] == "HESSIAN"
        assert out["prod_kg"] == 61.23 and out["std_kg"] == 100.0
        assert out["eff_pct"] is None  # zero eff never counts

    def test_spinning(self):
        out = normalize_spinning_rows([
            {"doff_date": "2026-09-01", "shift": "B", "spell_code": "B1", "machine_id": 11,
             "frame": "F11", "item_id": 55, "quality_name": "8 lbs", "eb_id": None,
             "eb_no": None, "worker_name": "Contract", "net_wt": 120.5, "std_prod": 150.1234,
             "eff_pct": 80.27, "whrs": 3.0},
        ])[0]
        assert out["tran_date"] == "2026-09-01" and out["machine"] == "F11"
        assert out["quality_id"] == 55 and out["std_kg"] == 150.123
        assert out["eff_pct"] == 80.27 and out["eb_id"] is None

    def test_winding_machine_from_map(self):
        mp = build_winder_machine_map([
            {"tran_date": date(2026, 9, 1), "spell_id": 1, "eb_id": 3, "machine_id": 19,
             "mech_code": " ", "machine_name": "Winder 19"},
            {"tran_date": date(2026, 9, 1), "spell_id": 2, "eb_id": 3, "machine_id": None,
             "mech_code": None, "machine_name": None},
        ])
        assert mp == {("2026-09-01", 1, 3): (19, "Winder 19")}
        rows = normalize_winding_rows([
            {"tran_date": "2026-09-01", "shift": "A", "spell_id": 1, "spell_code": "A1",
             "eb_id": 3, "eb_no": "10000", "worker_name": "T", "item_id": 5,
             "quality_name": "Q", "prod_kg": 70.0, "std_prod": 0.0, "eff_pct": None,
             "att_hrs": 5.0},
            {"tran_date": "2026-09-01", "shift": "B", "spell_id": 2, "spell_code": "B1",
             "eb_id": 3, "eb_no": "10000", "worker_name": "T", "item_id": 5,
             "quality_name": "Q", "prod_kg": 10.0, "std_prod": 20.0, "eff_pct": 50.0,
             "att_hrs": 3.0},
        ], mp)
        assert rows[0]["machine_id"] == 19 and rows[0]["machine"] == "Winder 19"
        assert rows[1]["machine_id"] is None and rows[1]["eff_pct"] == 50.0
        # no standard (0) -> null, never a 0 standard; a real one is kept
        assert rows[0]["std_kg"] is None and rows[1]["std_kg"] == 20.0

    def test_no_standard_is_null(self):
        spin = normalize_spinning_rows([
            {"doff_date": "2026-09-01", "shift": "A", "spell_code": "A1", "machine_id": 1,
             "frame": "F1", "item_id": 5, "quality_name": "Q", "eb_id": 3, "eb_no": "E3",
             "worker_name": "W", "net_wt": 50.0, "std_prod": std, "eff_pct": None, "whrs": 5.0}
            for std in (0.0, None, 12.3456)
        ])
        assert [r["std_kg"] for r in spin] == [None, None, 12.346]
        wind = normalize_winding_rows([
            {"tran_date": "2026-09-01", "shift": "A", "spell_id": 1, "spell_code": "A1",
             "eb_id": 3, "eb_no": "E3", "worker_name": "W", "item_id": 5, "quality_name": "Q",
             "prod_kg": 70.0, "std_prod": None, "eff_pct": None, "att_hrs": 5.0},
        ], {})
        assert wind[0]["std_kg"] is None
        assert build_person_summary(build_person_rows(spin, 3))["std_kg"] == 12.35


def _map_row(d, spell_id, eb_id, machine_id, mech_code=None, machine_name=None):
    return {"tran_date": d, "spell_id": spell_id, "eb_id": eb_id, "machine_id": machine_id,
            "mech_code": mech_code, "machine_name": machine_name}


class TestWinderMachineMap:
    """build_winder_machine_map indexes the ONE ranged winder -> machine query by
    (date, spell_id, eb_id), same result as the per-day loop it replaced."""

    def test_first_row_that_names_a_machine_wins(self):
        mp = build_winder_machine_map([
            _map_row("2026-09-01", 1, 3, None),                   # no machine: skipped
            _map_row("2026-09-01", 1, 3, 19, "W-19"),             # first with a machine
            _map_row("2026-09-01", 1, 3, 20, "W-20"),             # later row: ignored
        ])
        assert mp == {("2026-09-01", 1, 3): (19, "W-19")}

    def test_multiple_spells_dates_and_winders_are_separate_keys(self):
        mp = build_winder_machine_map([
            _map_row(date(2026, 9, 1), 1, 3, 19, "W-19"),
            _map_row(date(2026, 9, 1), 2, 3, 21, "W-21"),         # same day, other spell
            _map_row(date(2026, 9, 2), 1, 3, 22, "W-22"),         # next day, same spell
            _map_row(date(2026, 9, 1), 1, 4, 23, "W-23"),         # other winder
        ])
        assert mp == {
            ("2026-09-01", 1, 3): (19, "W-19"),
            ("2026-09-01", 2, 3): (21, "W-21"),
            ("2026-09-02", 1, 3): (22, "W-22"),
            ("2026-09-01", 1, 4): (23, "W-23"),
        }

    def test_missing_machine_leaves_the_winder_unmapped(self):
        mp = build_winder_machine_map([
            _map_row("2026-09-01", 1, 3, None, None, "ignored"),
            _map_row("2026-09-01", 2, 3, 19, "  ", "Winder 19"),  # blank code -> name
        ])
        assert mp == {("2026-09-01", 2, 3): (19, "Winder 19")}
        rows = normalize_winding_rows([
            {"tran_date": "2026-09-01", "shift": "A", "spell_id": 1, "spell_code": "A1",
             "eb_id": 3, "eb_no": "E3", "worker_name": "W", "item_id": 5, "quality_name": "Q",
             "prod_kg": 70.0, "std_prod": 0.0, "eff_pct": None, "att_hrs": 5.0},
        ], mp)
        assert (rows[0]["machine_id"], rows[0]["machine"]) == (None, None)

    def test_date_types_index_the_same(self):
        from datetime import datetime
        for d in (date(2026, 9, 1), datetime(2026, 9, 1), "2026-09-01"):
            assert build_winder_machine_map([_map_row(d, 1, 3, 19, "W")]) == {
                ("2026-09-01", 1, 3): (19, "W")}

    def test_range_query_on_sqlite_feeds_the_map(self):
        """The ranged SQL for real (SQLite): branch, inclusive range, active, winder
        and machine filters; rows come back date first, then in entry order, so
        the first entered row of a key wins."""
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE machine_mst (machine_id INTEGER PRIMARY KEY, "
                              "mech_code TEXT, machine_name TEXT)"))
            conn.execute(text("CREATE TABLE jute_prod_winding_daily_qlty ("
                              "winding_daily_qlty_id INTEGER PRIMARY KEY, branch_id INTEGER, "
                              "tran_date TEXT, spell_id INTEGER, eb_id INTEGER, "
                              "machine_id INTEGER, active INTEGER)"))
            conn.execute(text("INSERT INTO machine_mst VALUES (5, 'W-05', 'WINDING 05'), "
                              "(6, 'W-06', 'WINDING 06')"))
            for r in [
                # (id, branch, date, spell, eb, machine, active)
                (1, 29, "2026-09-02", 1, 10, 5, 1),   # in range (last day)
                (2, 29, "2026-09-01", 1, 10, 6, 1),   # first day
                (3, 29, "2026-09-01", 1, 10, 5, 1),   # same key, entered later -> loses
                (4, 29, "2026-09-01", 2, 10, None, 1),  # no machine -> excluded
                (5, 29, "2026-09-01", 2, 11, 5, 0),   # soft-deleted -> excluded
                (6, 29, "2026-09-01", 2, None, 5, 1),  # no winder -> excluded
                (7, 87, "2026-09-01", 1, 12, 5, 1),   # other branch -> excluded
                (8, 29, "2026-09-03", 1, 13, 5, 1),   # after the range -> excluded
                (9, 29, "2026-09-01", 3, 14, 6, None),  # NULL active counts as active
            ]:
                conn.execute(text("INSERT INTO jute_prod_winding_daily_qlty VALUES "
                                  "(:a, :b, :c, :d, :e, :f, :g)"), dict(zip("abcdefg", r)))
            rows = [dict(r._mapping) for r in conn.execute(
                get_winding_machine_by_winder_range_query(),
                {"branch_id": 29, "date_from": "2026-09-01", "date_to": "2026-09-02"},
            ).fetchall()]
        assert [(r["tran_date"], r["eb_id"], r["machine_id"]) for r in rows] == [
            ("2026-09-01", 10, 6), ("2026-09-01", 10, 5), ("2026-09-01", 14, 6),
            ("2026-09-02", 10, 5),
        ]
        assert build_winder_machine_map(rows) == {
            ("2026-09-01", 1, 10): (6, "W-06"),
            ("2026-09-01", 3, 14): (6, "W-06"),
            ("2026-09-02", 1, 10): (5, "W-05"),
        }

    def test_winding_fetch_reads_the_map_in_one_query(self):
        db = MagicMock()
        db.execute.return_value = _res_all([
            _map_row(date(2026, 9, 1), 1, 3, 19, "W-19"),
        ])
        recon = [{"tran_date": "2026-09-01", "shift": "A", "spell_id": 1, "spell_code": "A1",
                  "eb_id": 3, "eb_no": "E3", "worker_name": "W", "item_id": 5,
                  "quality_name": "Q", "prod_kg": 70.0, "std_prod": 100.0, "eff_pct": 70.0,
                  "att_hrs": 8.0}]
        with patch.object(lpf, "fetch_winding_lowprod_rows", return_value=(recon, 0)):
            rows = lpf.fetch_dept_rows(db, "WINDING", 1, 2, date(2026, 9, 1), date(2026, 10, 1))
        assert db.execute.call_count == 1          # was one per day (31 here)
        assert db.execute.call_args[0][1] == {
            "branch_id": 2, "date_from": date(2026, 9, 1), "date_to": date(2026, 10, 1)}
        assert (rows[0]["machine_id"], rows[0]["machine"]) == (19, "W-19")


class TestComparisons:
    ROWS = [
        # person 1 on machine 1 in A1 on two days, on machine 2 once
        _row("2026-09-01", "A1", 1, 1, 60.0),
        _row("2026-09-02", "A1", 1, 1, 50.0),
        _row("2026-09-02", "B1", 2, 1, 0.0),          # no eff -> excluded from averages
        # other spells on machine 1 the same dates
        _row("2026-09-01", "B1", 1, 2, 80.0),
        _row("2026-09-02", "C", 1, 3, 90.0),
        _row("2026-09-02", "A1", 1, 4, 99.0),         # SAME spell as the person -> left out
        _row("2026-09-03", "B1", 1, 2, 10.0),         # date the person did not work -> left out
        _row("2026-09-01", "C", 1, None, 70.0),       # contract worker, other spell -> kept
        # same quality elsewhere
        _row("2026-09-01", "A1", 5, 6, 40.0),
        _row("2026-09-03", "A1", 5, 6, 20.0, q=7),
        _row("2026-09-01", "A1", 6, 7, 88.0, q=8),    # other quality
    ]

    def test_person_rows_and_summary(self):
        rows = build_person_rows(self.ROWS, 1)
        assert [r["tran_date"] for r in rows] == ["2026-09-01", "2026-09-02", "2026-09-02"]
        assert "eb_id" not in rows[0]
        s = build_person_summary(rows)
        assert s == {"days_worked": 2, "rows": 3, "prod_kg": 150.0, "std_kg": 300.0,
                     "avg_eff": 55.0, "kg_per_hr": 6.25}

    def test_same_machine_other_spells(self):
        res = build_same_machine(self.ROWS, 1)
        m1 = next(s for s in res["summary"] if s["machine_id"] == 1)
        assert m1["person_days"] == 2
        assert m1["person_avg_eff"] == 55.0
        # others: 80 (B1 09-01), 70 (contract C 09-01), 90 (C 09-02); 99 same spell, 10 other date
        assert m1["others_rows"] == 3
        assert m1["others_avg_eff"] == 80.0
        assert m1["diff"] == -25.0
        assert m1["person_kg_per_hr"] == 6.25 and m1["others_kg_per_hr"] == 6.25
        others = {(r["tran_date"], r["other_spell_code"]) for r in res["rows"]}
        assert others == {("2026-09-01", "B1"), ("2026-09-01", "C"), ("2026-09-02", "C")}
        contract = next(r for r in res["rows"] if r["other_eb_id"] is None)
        assert contract["person_spells"] == "A1" and contract["person_eff"] == 60.0
        m2 = next(s for s in res["summary"] if s["machine_id"] == 2)
        assert m2["person_avg_eff"] is None and m2["others_rows"] == 0 and m2["diff"] is None

    def test_same_machine_overlapping_spells_are_the_same_shift(self):
        rows = [
            _row("2026-09-01", "A", 1, 1, 60.0),     # person: full A shift
            _row("2026-09-01", "A2", 1, 2, 90.0),    # overlaps A -> same shift, left out
            _row("2026-09-01", "A1", 1, 3, 95.0),    # overlaps A -> same shift, left out
            _row("2026-09-01", "B1", 1, 4, 70.0),    # another shift -> compared
            _row("2026-09-02", "A1", 1, 1, 50.0),    # person on A1 ...
            _row("2026-09-02", "A2", 1, 5, 80.0),    # ... A2 is still shift A -> left out
            _row("2026-09-02", "C", 1, 6, 40.0),     # another shift -> compared
        ]
        res = build_same_machine(rows, 1)
        assert {(r["tran_date"], r["other_spell_code"]) for r in res["rows"]} == {
            ("2026-09-01", "B1"), ("2026-09-02", "C")}
        assert res["summary"][0]["others_rows"] == 2 and res["summary"][0]["others_avg_eff"] == 55.0

    def test_same_machine_ignores_rows_without_machine(self):
        rows = [_row("2026-09-01", "A1", None, 1, 60.0), _row("2026-09-01", "B1", None, 2, 70.0)]
        assert build_same_machine(rows, 1) == {"summary": [], "rows": []}

    def test_same_quality_same_dates(self):
        res = build_same_quality(self.ROWS, 1)
        assert len(res) == 1
        q = res[0]
        assert q["quality_id"] == 7 and q["person_days"] == 2 and q["person_rows"] == 3
        assert q["person_avg_eff"] == 55.0
        # on 09-01 / 09-02, quality 7, others: 80, 90, 99, 70, 40
        assert q["others_avg_eff"] == 75.8
        assert q["others_workers"] == 5  # eb 2, 3, 4, 6 + the contract bucket
        assert q["dept_avg_eff"] == round((60 + 50 + 80 + 90 + 99 + 70 + 40) / 7, 2)
        # whole window adds 09-03 rows (10, 20)
        assert q["range_dept_avg_eff"] == round((60 + 50 + 80 + 90 + 99 + 10 + 70 + 40 + 20) / 9, 2)
        assert q["diff"] == -20.8

    def test_week_numbers(self):
        start = date(2026, 9, 1)
        assert week_no_of(date(2026, 9, 1), start) == 1
        assert week_no_of(date(2026, 9, 7), start) == 1
        assert week_no_of(date(2026, 9, 8), start) == 2
        assert week_no_of(date(2026, 8, 31), start) == 0
        assert week_no_of(date(2026, 8, 25), start) == 0
        assert week_no_of(date(2026, 8, 24), start) == -1

    def test_weekly_trend(self):
        rows = [
            _row("2026-08-31", "A1", 1, 1, 40.0),     # before marking -> week 0
            _row("2026-09-01", "A1", 1, 1, 50.0),
            _row("2026-09-03", "A1", 1, 1, 70.0),
            _row("2026-09-09", "A1", 1, 1, 80.0, prod=30.0),
            _row("2026-09-01", "B1", 2, 2, 90.0),     # peer, same date + quality
            _row("2026-09-02", "B1", 2, 2, 10.0),     # peer on a date the person did not work
            _row("2026-09-09", "B1", 2, 2, 85.0, q=8),  # other quality -> not a peer
        ]
        trend = build_weekly_trend(rows, 1, date(2026, 9, 1))
        assert [w["week_no"] for w in trend] == [0, 1, 2]
        assert trend[0]["week_start"] == "2026-08-25" and trend[0]["week_end"] == "2026-08-31"
        w1 = trend[1]
        assert (w1["days_worked"], w1["prod_kg"], w1["avg_eff"], w1["peer_avg_eff"]) == (2, 100.0, 60.0, 90.0)
        assert trend[2]["avg_eff"] == 80.0 and trend[2]["peer_avg_eff"] is None
        assert w1["kg_per_hr"] == 6.25 and w1["peer_kg_per_hr"] == 6.25
        assert trend[2]["kg_per_hr"] == 3.75 and trend[2]["peer_kg_per_hr"] is None

    def test_kg_per_hr_skips_rows_without_hours(self):
        rows = [_row("2026-09-01", "A1", 1, 1, 0.0, prod=40.0),
                {**_row("2026-09-01", "B1", 1, 1, 0.0, prod=99.0), "hours": None}]
        assert lpf.kg_per_hr(rows) == 5.0
        assert lpf.kg_per_hr([{**rows[0], "hours": 0}]) is None


class TestShapeRow:
    def _m(self, **kw):
        m = {"followup_id": 1, "co_id": 1, "branch_id": 2, "eb_id": 3, "eb_no": "10000",
             "worker_name": "T", "dept_code": "WINDING", "reason": None,
             "start_date": date(2026, 9, 1), "review_date": date(2026, 10, 7), "status": 1,
             "closed_date": None, "close_reason": None, "close_remarks": None,
             "remark_count": 2, "notice_count": 1, "hours_cut_count": 1, "total_hours_cut": 4.5,
             "last_event_date": date(2026, 9, 5), "last_notice_type": "VERBAL_WARNING",
             "updated_date_time": None}
        m.update(kw)
        return m

    def test_open_row(self):
        r = shape_followup_row(self._m(), TODAY)
        assert r["review_due"] is True and r["days_tracked"] == 36
        assert r["start_date"] == "2026-09-01" and r["total_hours_cut"] == 4.5

    def test_closed_row(self):
        r = shape_followup_row(self._m(status=0, closed_date=date(2026, 9, 11)), TODAY)
        assert r["review_due"] is False and r["days_tracked"] == 10


# =============================================================================
# Endpoints (mocked DB)
# =============================================================================


def _res_first(value):
    res = MagicMock()
    res.first.return_value = value
    return res


def _res_all(mappings):
    res = MagicMock()
    rows = []
    for m in mappings:
        row = MagicMock()
        row._mapping = m
        rows.append(row)
    res.fetchall.return_value = rows
    return res


def _mapped(m):
    row = MagicMock()
    row._mapping = m
    return row


def _event_mapping(**kw):
    m = {"followup_event_id": 9, "followup_id": 1, "event_type": "HOURS_CUT",
         "event_date": date(2026, 9, 5), "notice_type": None, "issued_by": None,
         "spell_id": None, "spell_code": None, "hours_cut": None, "remarks": "late",
         "attachment_id": None, "attachment_filename": None, "updated_by": 7,
         "updated_date_time": None}
    m.update(kw)
    return m


def _followup_mapping(**kw):
    m = {"followup_id": 1, "co_id": 1, "branch_id": 2, "eb_id": 3, "eb_no": "10000",
         "worker_name": "T", "dept_code": "WINDING", "reason": "slow",
         "start_date": date(2026, 9, 1), "review_date": None, "status": 1,
         "closed_date": None, "close_reason": None, "close_remarks": None,
         "remark_count": 0, "notice_count": 0, "hours_cut_count": 0, "total_hours_cut": 0,
         "last_event_date": None, "last_notice_type": None, "updated_date_time": None}
    m.update(kw)
    return m


class TestEndpoints:
    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        # Edit access by default; TestAccess covers the levels.
        self._access = patch.object(lpf, "followup_access_level", return_value=LEVEL_EDIT)
        self.access = self._access.start()

    def teardown_method(self):
        self._access.stop()
        app.dependency_overrides.clear()

    def _create_body(self, **kw):
        b = {"co_id": 1, "branch_id": 2, "eb_no": "10000", "dept_code": "WINDING",
             "start_date": "2026-09-01", "reason": "slow"}
        b.update(kw)
        return b

    def test_create_validation_400_before_db(self):
        for body in (
            self._create_body(dept_code="DRAWING"),
            self._create_body(start_date="2099-01-01"),
            self._create_body(review_date="2026-08-01"),
            self._create_body(eb_no="", eb_id=None),
            self._create_body(co_id=None),
        ):
            assert client.post(f"{PREFIX}/create", json=body).status_code == 400
        self.db.execute.assert_not_called()

    def test_create_employee_not_in_branch_400(self):
        self.db.execute.side_effect = [_res_first((1,)), _res_first(None)]
        resp = client.post(f"{PREFIX}/create", json=self._create_body())
        assert resp.status_code == 400
        assert "Employee not found" in resp.json()["detail"]

    def test_create_duplicate_open_409(self):
        self.db.execute.side_effect = [
            _res_first((1,)),                               # branch belongs to company
            _res_first(_mapped({"eb_id": 3})),              # EB no resolves in the branch
            _res_first((42,)),                              # an open follow-up exists
        ]
        resp = client.post(f"{PREFIX}/create", json=self._create_body())
        assert resp.status_code == 409
        assert "#42" in resp.json()["detail"]
        self.db.add.assert_not_called()

    def test_create_happy_path(self):
        self.db.execute.side_effect = [
            _res_first((1,)),
            _res_first(_mapped({"eb_id": 3})),
            _res_first(None),
            _res_all([_followup_mapping()]),
        ]

        def _refresh(obj):
            obj.followup_id = 1

        self.db.refresh.side_effect = _refresh
        resp = client.post(f"{PREFIX}/create", json=self._create_body())
        assert resp.status_code == 200, resp.text
        added = self.db.add.call_args[0][0]
        assert (added.co_id, added.branch_id, added.eb_id, added.dept_code) == (1, 2, 3, "WINDING")
        assert added.status == 1 and added.updated_by == 7
        assert resp.json()["data"]["followup_id"] == 1

    def _followup_obj(self, **kw):
        f = SimpleNamespace(followup_id=1, co_id=1, branch_id=2, eb_id=3, dept_code="WINDING",
                            start_date=date(2026, 9, 1), status=1, active=1, closed_date=None,
                            close_reason=None, close_remarks=None, reason=None,
                            review_date=None, updated_by=None)
        f.__dict__.update(kw)
        return f

    def test_event_on_closed_followup_400(self):
        self.db.get.return_value = self._followup_obj(status=0)
        resp = client.post(f"{PREFIX}/event_create", json={
            "co_id": 1, "followup_id": 1, "event_type": "REMARK",
            "event_date": "2026-09-05", "remarks": "x"})
        assert resp.status_code == 400
        assert "closed" in resp.json()["detail"]

    def test_followup_of_other_company_404(self):
        self.db.get.return_value = self._followup_obj(co_id=9)
        resp = client.get(f"{PREFIX}/events", params={"co_id": 1, "followup_id": 1})
        assert resp.status_code == 404

    def test_hours_cut_over_spell_400(self):
        self.db.get.return_value = self._followup_obj()
        self.db.execute.side_effect = [
            _res_all(DEV3_SPELLS),
            _res_all([_cut("B2", 3.0)]),                    # 3 h already cut that date + spell
        ]
        resp = client.post(f"{PREFIX}/event_create", json={
            "co_id": 1, "followup_id": 1, "event_type": "HOURS_CUT", "event_date": "2026-09-05",
            "spell_id": 4, "hours_cut": 2.5, "remarks": "late"})
        assert resp.status_code == 400
        assert "3 h already cut for this person, 2 h left" in resp.json()["detail"]
        self.db.add.assert_not_called()

    def test_hours_cut_counts_the_persons_cuts_across_followups(self):
        """A1 5 h already cut for this person that date (on ANY follow-up): A2 3 h
        still fits the 8 h A shift, A2 + B1 day totals are not capped by A."""
        self.db.get.return_value = self._followup_obj(eb_id=3, branch_id=2)
        self.db.execute.side_effect = [
            _res_all(DEV3_SPELLS),
            _res_all([_cut("A1", 5.0)]),
            _res_all([_event_mapping(spell_id=3, spell_code="A2", hours_cut=3.0)]),  # _event_out
        ]
        resp = client.post(f"{PREFIX}/event_create", json={
            "co_id": 1, "followup_id": 1, "event_type": "HOURS_CUT", "event_date": "2026-09-05",
            "spell_id": 3, "hours_cut": 3, "remarks": "late"})
        # the person-wide query is keyed on eb_id + date, not on the follow-up
        params = self.db.execute.call_args_list[1][0][1]
        assert params == {"eb_id": 3, "event_date": date(2026, 9, 5), "exclude_id": None}
        self.db.add.assert_called_once()
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["spell_code"] == "A2"

    def test_hours_cut_edit_excludes_itself(self):
        ev = SimpleNamespace(followup_event_id=5, followup_id=1, event_type="HOURS_CUT", active=1)
        self.db.get.side_effect = lambda model, _id: ev if model is JuteProdFollowupEvent else self._followup_obj()
        self.db.execute.side_effect = [_res_all(DEV3_SPELLS), _res_all([_cut("A2", 1.0)])]
        resp = client.post(f"{PREFIX}/event_update", json={
            "co_id": 1, "followup_event_id": 5, "event_date": "2026-09-05",
            "spell_id": 1, "hours_cut": 5.5, "remarks": "late"})
        assert resp.status_code == 400 and "spell A1 (5 h)" in resp.json()["detail"]
        assert self.db.execute.call_args_list[1][0][1]["exclude_id"] == 5

    def test_hours_cut_spell_not_in_branch_400(self):
        self.db.get.return_value = self._followup_obj()
        self.db.execute.side_effect = [_res_all([{"spell_id": 4, "working_hours": 5.0}])]
        resp = client.post(f"{PREFIX}/event_create", json={
            "co_id": 1, "followup_id": 1, "event_type": "HOURS_CUT", "event_date": "2026-09-05",
            "spell_id": 99, "hours_cut": 1, "remarks": "late"})
        assert resp.status_code == 400
        assert "spell" in resp.json()["detail"]

    def test_event_type_cannot_change(self):
        ev = SimpleNamespace(followup_event_id=5, followup_id=1, event_type="HOURS_CUT", active=1)
        f = self._followup_obj()
        self.db.get.side_effect = [ev, f]
        resp = client.post(f"{PREFIX}/event_update", json={
            "co_id": 1, "followup_event_id": 5, "event_type": "REMARK",
            "event_date": "2026-09-05", "remarks": "x"})
        assert resp.status_code == 400
        assert "cannot be changed" in resp.json()["detail"]

    def test_reopen_duplicate_409(self):
        self.db.get.return_value = self._followup_obj(status=0, closed_date=date(2026, 9, 20),
                                                      close_reason="IMPROVED")
        self.db.execute.side_effect = [_res_first((8,))]
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "followup_id": 1})
        assert resp.status_code == 409
        self.db.commit.assert_not_called()

    def test_reopen_keeps_close_details_as_remark(self):
        f = self._followup_obj(status=0, closed_date=date(2026, 9, 20), close_reason="IMPROVED",
                               close_remarks="better")
        self.db.get.return_value = f
        self.db.execute.side_effect = [_res_first(None), _res_all([_followup_mapping()])]
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "followup_id": 1})
        assert resp.status_code == 200, resp.text
        note = self.db.add.call_args[0][0]
        assert note.event_type == "REMARK" and "2026-09-20" in note.remarks
        assert "Improved" in note.remarks and "better" in note.remarks
        assert f.status == 1 and f.closed_date is None and f.close_reason is None

    def test_close_already_closed_400(self):
        self.db.get.return_value = self._followup_obj(status=0)
        resp = client.post(f"{PREFIX}/close", json={
            "co_id": 1, "followup_id": 1, "closed_date": "2026-09-20", "close_reason": "LEFT"})
        assert resp.status_code == 400

    def test_tracked(self):
        self.db.execute.return_value = _res_all([
            {"followup_id": 1, "eb_id": 3, "dept_code": "WEAVING",
             "start_date": date(2026, 9, 1), "review_date": None}])
        resp = client.get(f"{PREFIX}/tracked",
                          params={"co_id": 1, "branch_id": 2, "dept_code": "weaving"})
        assert resp.status_code == 200
        assert resp.json()["data"] == [{"followup_id": 1, "eb_id": 3, "dept_code": "WEAVING",
                                        "start_date": "2026-09-01", "review_date": None}]
        assert client.get(f"{PREFIX}/tracked", params={"co_id": 1, "branch_id": 2}).status_code == 400

    def test_list_bad_status_400(self):
        resp = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2, "status": 5})
        assert resp.status_code == 400

    def test_performance_uses_the_low_producer_fetch(self):
        self.db.execute.return_value = _res_all([_followup_mapping(dept_code="SPINNING", eb_id=3)])
        spin_rows = [
            {"doff_date": "2026-09-02", "shift": "A", "spell_code": "A1", "machine_id": 11,
             "frame": "F11", "item_id": 55, "quality_name": "8 lbs", "eb_id": 3, "eb_no": "10000",
             "worker_name": "T", "net_wt": 100.0, "std_prod": 200.0, "eff_pct": 50.0, "whrs": 5.0},
            {"doff_date": "2026-09-02", "shift": "B", "spell_code": "B1", "machine_id": 11,
             "frame": "F11", "item_id": 55, "quality_name": "8 lbs", "eb_id": 4, "eb_no": "10001",
             "worker_name": "U", "net_wt": 180.0, "std_prod": 200.0, "eff_pct": 90.0, "whrs": 3.0},
        ]
        with patch.object(lpf, "fetch_spinning_lowprod_rows", return_value=spin_rows) as fetch:
            resp = client.get(f"{PREFIX}/performance", params={
                "co_id": 1, "followup_id": 1, "date_from": "2026-09-01", "date_to": "2026-09-07"})
        assert resp.status_code == 200, resp.text
        args = fetch.call_args[0]
        assert args[1:] == (1, 2, date(2026, 9, 1), date(2026, 9, 7))
        d = resp.json()["data"]
        assert d["max_days"] == 62 and d["default_days"] == 62 and d["clipped"] is False
        assert len(d["person_rows"]) == 1 and d["person_summary"]["avg_eff"] == 50.0
        assert d["same_machine"]["summary"][0]["others_avg_eff"] == 90.0
        assert d["same_quality"][0]["others_avg_eff"] == 90.0
        assert d["weekly_trend"][0]["week_no"] == 1 and d["weekly_trend"][0]["peer_avg_eff"] == 90.0

    def test_performance_range_over_dept_cap_400(self):
        self.db.execute.return_value = _res_all([_followup_mapping(dept_code="WEAVING")])
        resp = client.get(f"{PREFIX}/performance", params={
            "co_id": 1, "followup_id": 1, "date_from": "2026-08-01", "date_to": "2026-09-15"})
        assert resp.status_code == 400
        assert "31 days" in resp.json()["detail"]

    def test_performance_not_found_404(self):
        self.db.execute.return_value = _res_all([])
        resp = client.get(f"{PREFIX}/performance", params={"co_id": 1, "followup_id": 99})
        assert resp.status_code == 404

    def test_performance_weaving_default_window_is_14_days(self):
        self.db.execute.return_value = _res_all([_followup_mapping(
            dept_code="WEAVING", start_date=date(2026, 8, 1))])
        with patch.object(lpf, "_today", return_value=TODAY), \
                patch.object(lpf, "fetch_weaving_lowprod_rows", return_value=[]) as fetch:
            resp = client.get(f"{PREFIX}/performance", params={"co_id": 1, "followup_id": 1})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert (d["max_days"], d["default_days"], d["clipped"]) == (31, 14, True)
        assert (d["date_from"], d["date_to"]) == ("2026-09-26", "2026-10-06")
        assert fetch.call_args[0][3:] == (date(2026, 9, 26), date(2026, 10, 6))
        assert "at most 14 days" in d["notes"][0]

    def test_performance_closed_followup_ends_at_close(self):
        self.db.execute.return_value = _res_all([_followup_mapping(
            dept_code="WINDING", status=0, closed_date=date(2026, 9, 20))])
        with patch.object(lpf, "_today", return_value=TODAY), \
                patch.object(lpf, "fetch_winding_lowprod_rows", return_value=([], [])):
            resp = client.get(f"{PREFIX}/performance", params={"co_id": 1, "followup_id": 1})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert (d["date_from"], d["date_to"]) == ("2026-09-01", "2026-09-20")

    def test_close_before_last_event_400(self):
        self.db.get.return_value = self._followup_obj()
        self.db.execute.side_effect = [_res_first((date(2026, 9, 10),))]
        resp = client.post(f"{PREFIX}/close", json={
            "co_id": 1, "followup_id": 1, "closed_date": "2026-09-05", "close_reason": "LEFT"})
        assert resp.status_code == 400
        assert "last timeline entry (2026-09-10)" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    @pytest.mark.parametrize("path", ["create", "update", "close", "reopen", "event_create",
                                      "event_update", "event_deactivate"])
    def test_post_body_must_be_a_json_object(self, path):
        for raw in (b"[1, 2]", b'"text"', b"42", b"null", b"{not json", b""):
            resp = client.post(f"{PREFIX}/{path}", content=raw,
                               headers={"Content-Type": "application/json"})
            assert resp.status_code == 400, (path, raw, resp.text)
        self.db.execute.assert_not_called()
        self.db.commit.assert_not_called()

    def test_hours_cut_nan_400(self):
        self.db.get.return_value = self._followup_obj()
        raw = (b'{"co_id": 1, "followup_id": 1, "event_type": "HOURS_CUT", '
               b'"event_date": "2026-09-05", "spell_id": 1, "hours_cut": NaN, "remarks": "x"}')
        resp = client.post(f"{PREFIX}/event_create", content=raw,
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 400 and "hours_cut" in resp.json()["detail"]
        raw = raw.replace(b"NaN", b"Infinity")
        resp = client.post(f"{PREFIX}/event_create", content=raw,
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 400
        self.db.add.assert_not_called()


# =============================================================================
# Access levels (menu juteProduction/report/lowPerformerFollowup)
# =============================================================================


def _integrity(code=1062, key="uq_jpfp_open"):
    orig = pymysql.err.IntegrityError(
        code, f"Duplicate entry '1-1-2-3-WINDING' for key 'jute_prod_followup_person.{key}'")
    return IntegrityError("INSERT INTO jute_prod_followup_person ...", {}, orig)


class TestAccess:
    """Every endpoint checks the caller's level on the follow-up menu for the
    branch: view (1) to read, create (3) to add, edit (4) to change."""

    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_access_level_query(self):
        db = MagicMock()
        db.execute.side_effect = [_res_first((1048,)), _res_first((3,))]
        assert followup_access_level(db, 7, 2) == 3
        menu_params = db.execute.call_args_list[0][0][1]
        assert menu_params == {"menu_path": "juteProduction/report/lowPerformerFollowup"}
        assert db.execute.call_args_list[1][0][1] == {"user_id": 7, "branch_id": 2, "menu_id": 1048}
        db = MagicMock()
        db.execute.side_effect = [_res_first((1048,)), _res_first((None,))]   # no grant
        assert followup_access_level(db, 7, 2) == 0
        db = MagicMock()
        db.execute.side_effect = [_res_first(None)]                           # no menu row
        assert followup_access_level(db, 7, 2) == 0
        db = MagicMock()
        assert followup_access_level(db, None, 2) == 0 and followup_access_level(db, 7, None) == 0
        db.execute.assert_not_called()

    def _followup(self, **kw):
        f = SimpleNamespace(followup_id=1, co_id=1, branch_id=2, eb_id=3, dept_code="WINDING",
                            start_date=date(2026, 9, 1), status=1, active=1, closed_date=None,
                            close_reason=None, close_remarks=None, reason=None,
                            review_date=None, updated_by=None)
        f.__dict__.update(kw)
        return f

    def _arrange(self, path):
        """DB state for one endpoint; returns (method, url, kwargs)."""
        ev = SimpleNamespace(followup_event_id=5, followup_id=1, event_type="REMARK", active=1)
        closed = path == "reopen"
        f = self._followup(status=0 if closed else 1, closed_date=date(2026, 9, 20) if closed else None,
                           close_reason="IMPROVED" if closed else None)
        self.db.get.side_effect = lambda model, _id: ev if model is JuteProdFollowupEvent else f
        self.db.execute.return_value = _res_all([_followup_mapping()])
        g = {"co_id": 1, "branch_id": 2}
        return {
            "setup": ("get", f"{PREFIX}/setup", {"params": g}),
            "list": ("get", f"{PREFIX}/list", {"params": g}),
            "tracked": ("get", f"{PREFIX}/tracked", {"params": {**g, "dept_code": "WINDING"}}),
            "get": ("get", f"{PREFIX}/get", {"params": {"co_id": 1, "followup_id": 1}}),
            "events": ("get", f"{PREFIX}/events", {"params": {"co_id": 1, "followup_id": 1}}),
            "performance": ("get", f"{PREFIX}/performance", {"params": {
                "co_id": 1, "followup_id": 1, "date_from": "2026-09-01", "date_to": "2026-09-02"}}),
            "create": ("post", f"{PREFIX}/create", {"json": {
                "co_id": 1, "branch_id": 2, "eb_no": "10000", "dept_code": "WINDING",
                "start_date": "2026-09-01"}}),
            "event_create": ("post", f"{PREFIX}/event_create", {"json": {
                "co_id": 1, "followup_id": 1, "event_type": "REMARK", "event_date": "2026-09-05",
                "remarks": "x"}}),
            # branch_id in an id-based body is ignored: the stored follow-up's branch counts
            "update": ("post", f"{PREFIX}/update", {"json": {
                "co_id": 1, "followup_id": 1, "branch_id": 99, "start_date": "2026-09-01"}}),
            "close": ("post", f"{PREFIX}/close", {"json": {
                "co_id": 1, "followup_id": 1, "closed_date": "2026-09-20", "close_reason": "LEFT"}}),
            "reopen": ("post", f"{PREFIX}/reopen", {"json": {"co_id": 1, "followup_id": 1}}),
            "event_update": ("post", f"{PREFIX}/event_update", {"json": {
                "co_id": 1, "followup_event_id": 5, "event_date": "2026-09-05", "remarks": "y"}}),
            "event_deactivate": ("post", f"{PREFIX}/event_deactivate", {"json": {
                "co_id": 1, "followup_event_id": 5}}),
        }[path]

    REQUIRED = {
        "setup": LEVEL_VIEW, "list": LEVEL_VIEW, "tracked": LEVEL_VIEW, "get": LEVEL_VIEW,
        "events": LEVEL_VIEW, "performance": LEVEL_VIEW,
        "create": LEVEL_CREATE, "event_create": LEVEL_CREATE,
        "update": LEVEL_EDIT, "close": LEVEL_EDIT, "reopen": LEVEL_EDIT,
        "event_update": LEVEL_EDIT, "event_deactivate": LEVEL_EDIT,
    }

    @pytest.mark.parametrize("path", list(REQUIRED))
    def test_level_below_required_is_403(self, path):
        need = self.REQUIRED[path]
        for level in range(0, need):
            self.db.reset_mock()
            method, url, kw = self._arrange(path)
            with patch.object(lpf, "followup_access_level", return_value=level) as acc, \
                    patch.object(lpf, "fetch_winding_lowprod_rows", return_value=([], [])):
                resp = getattr(client, method)(url, **kw)
            assert resp.status_code == 403, (path, level, resp.text)
            assert "access to Low Performer Follow-up" in resp.json()["detail"]
            # always checked on the follow-up's (stored) branch 2 for user 7
            assert acc.call_args[0][1:] == (7, 2)
            self.db.commit.assert_not_called()
            self.db.add.assert_not_called()

    @pytest.mark.parametrize("path", list(REQUIRED))
    def test_required_level_passes_the_gate(self, path):
        method, url, kw = self._arrange(path)
        with patch.object(lpf, "followup_access_level", return_value=self.REQUIRED[path]), \
                patch.object(lpf, "fetch_winding_lowprod_rows", return_value=([], [])):
            resp = getattr(client, method)(url, **kw)
        assert resp.status_code != 403, (path, resp.text)

    def test_level_4_can_write(self):
        self.db.get.return_value = self._followup()
        self.db.execute.side_effect = [_res_first((None,)), _res_all([_followup_mapping()])]
        with patch.object(lpf, "followup_access_level", return_value=LEVEL_EDIT):
            resp = client.post(f"{PREFIX}/update", json={
                "co_id": 1, "followup_id": 1, "start_date": "2026-09-01", "reason": "slower"})
        assert resp.status_code == 200, resp.text
        self.db.commit.assert_called_once()

    def test_client_co_id_must_match_the_stored_followup(self):
        self.db.get.return_value = self._followup(co_id=9)
        with patch.object(lpf, "followup_access_level", return_value=LEVEL_EDIT) as acc:
            resp = client.post(f"{PREFIX}/close", json={
                "co_id": 1, "followup_id": 1, "closed_date": "2026-09-20", "close_reason": "LEFT"})
        assert resp.status_code == 404
        acc.assert_not_called()

    def test_branch_of_another_company_is_400_before_access(self):
        self.db.execute.return_value = _res_first(None)    # branch 2 not in co 1
        with patch.object(lpf, "followup_access_level", return_value=LEVEL_EDIT) as acc:
            resp = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 400
        acc.assert_not_called()


# =============================================================================
# One open follow-up per person + branch + dept: DB unique key -> 409
# =============================================================================


class TestDuplicateRace:
    """The app guard is bypassed (as when two requests pass it at the same time):
    the functional unique key uq_jpfp_open refuses the commit -> 409 with the
    guard's own message."""

    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        self._patches = [
            patch.object(lpf, "followup_access_level", return_value=LEVEL_EDIT),
            patch.object(lpf, "_guard_duplicate", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        app.dependency_overrides.clear()

    def test_create_integrity_error_is_409(self):
        self.db.execute.side_effect = [
            _res_first((1,)),                         # branch belongs to company
            _res_first(_mapped({"eb_id": 3})),        # EB no resolves
            _res_first((42,)),                        # after rollback: the row that won
        ]
        self.db.commit.side_effect = _integrity()
        resp = client.post(f"{PREFIX}/create", json={
            "co_id": 1, "branch_id": 2, "eb_no": "10000", "dept_code": "WINDING",
            "start_date": "2026-09-01"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            "This person is already on an open Winding follow-up (#42) in this branch")
        self.db.rollback.assert_called()

    def test_reopen_integrity_error_is_409(self):
        f = SimpleNamespace(followup_id=1, co_id=1, branch_id=2, eb_id=3, dept_code="SPINNING",
                            start_date=date(2026, 9, 1), status=0, active=1,
                            closed_date=date(2026, 9, 20), close_reason="IMPROVED",
                            close_remarks=None, updated_by=None)
        self.db.get.return_value = f
        self.db.execute.side_effect = [_res_first((8,))]
        self.db.commit.side_effect = _integrity()
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "followup_id": 1})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            "This person is already on an open Spinning follow-up (#8) in this branch")
        # the duplicate look-up skips the row being reopened
        assert self.db.execute.call_args[0][1]["exclude_id"] == 1

    def test_other_integrity_errors_are_not_409(self):
        self.db.execute.side_effect = [_res_first((1,)), _res_first(_mapped({"eb_id": 3}))]
        self.db.commit.side_effect = _integrity(code=1452, key="fk_x")
        resp = client.post(f"{PREFIX}/create", json={
            "co_id": 1, "branch_id": 2, "eb_no": "10000", "dept_code": "WINDING",
            "start_date": "2026-09-01"})
        assert resp.status_code == 500


# =============================================================================
# Notice scans through the generic attachment API
# =============================================================================


def _scope(**kw):
    m = {"followup_event_id": 5, "event_type": "NOTICE", "event_active": 1, "followup_id": 1,
         "co_id": 1, "branch_id": 2, "status": 1, "followup_active": 1}
    m.update(kw)
    return _res_first(_mapped(m))


class TestNoticeAttachmentGate:
    def test_gate_rules(self):
        cases = [
            # (scope row, action, level, expected status or None = allowed)
            (_res_first(None), "read", 4, 404),
            (_scope(co_id=9), "read", 4, 404),                 # another company
            (_scope(followup_active=0), "read", 4, 404),
            (_scope(), "read", 0, 403),
            (_scope(), "read", 1, None),
            (_scope(status=0, event_active=0), "read", 1, None),   # history stays readable
            (_scope(), "upload", 2, 403),
            (_scope(), "upload", 3, None),
            (_scope(event_type="REMARK"), "upload", 3, 400),
            (_scope(event_active=0), "upload", 3, 400),
            (_scope(status=0), "upload", 3, 400),              # follow-up closed
            (_scope(), "delete", 3, 403),
            (_scope(), "delete", 4, None),
            (_scope(status=0), "delete", 4, 400),
        ]
        for scope, action, level, expected in cases:
            db = MagicMock()
            db.execute.return_value = scope
            with patch.object(lpf, "followup_access_level", return_value=level) as acc:
                if expected is None:
                    check_notice_attachment_access(db, {"user_id": 7}, 1, 5, action)
                else:
                    with pytest.raises(HTTPException) as e:
                        check_notice_attachment_access(db, {"user_id": 7}, 1, 5, action)
                    assert _status(e) == expected, (action, level, expected)
            if acc.called:
                assert acc.call_args[0][1:] == (7, 2)          # the follow-up's branch


class TestNoticeAttachmentRoutes:
    """The gate wired into /api/attachments for module jute_prod_followup only."""

    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def _upload(self):
        return client.post(
            f"{ATT}/upload?co_id=1",
            data={"module": "jute_prod_followup", "entity_id": "5", "file_type": "notice"},
            files={"file": ("n.pdf", b"%PDF-1.4 notice", "application/pdf")},
            headers={"host": "dev3.vowerp.co.in"},
        )

    def test_upload_without_create_access_is_403_before_s3(self):
        self.db.execute.side_effect = [_scope()]
        with patch.object(lpf, "followup_access_level", return_value=2), \
                patch.object(attachments_router_module.s3_client, "upload_bytes") as up:
            resp = self._upload()
        assert resp.status_code == 403
        up.assert_not_called()
        self.db.commit.assert_not_called()

    def test_upload_on_closed_followup_is_400(self):
        self.db.execute.side_effect = [_scope(status=0)]
        with patch.object(lpf, "followup_access_level", return_value=4), \
                patch.object(attachments_router_module.s3_client, "upload_bytes") as up:
            resp = self._upload()
        assert resp.status_code == 400
        up.assert_not_called()

    def test_upload_with_create_access(self):
        insert = MagicMock()
        insert.lastrowid = 77
        self.db.execute.side_effect = [_scope(), MagicMock(), insert]
        with patch.object(lpf, "followup_access_level", return_value=3), \
                patch.object(attachments_router_module.s3_client, "upload_bytes") as up, \
                patch.object(attachments_router_module.s3_client, "get_bucket", return_value="b"):
            resp = self._upload()
        assert resp.status_code == 200, resp.text
        up.assert_called_once()
        # the slot's previous scan is retired only inside the same company
        soft_delete_params = self.db.execute.call_args_list[1][0][1]
        assert soft_delete_params["co_id"] == 1
        assert "co_id = :co_id" in str(self.db.execute.call_args_list[1][0][0])

    def _att_row(self, **kw):
        m = {"attachment_id": 77, "co_id": 1, "module": "jute_prod_followup", "entity_id": 5,
             "file_type": "notice", "s3_key": "k", "active": 1}
        m.update(kw)
        row = MagicMock()
        row._mapping = m
        res = MagicMock()
        res.fetchone.return_value = row
        return res

    def test_delete_needs_edit(self):
        self.db.execute.side_effect = [self._att_row(), _scope()]
        with patch.object(lpf, "followup_access_level", return_value=3):
            assert client.delete(f"{ATT}/77?co_id=1").status_code == 403
        self.db.commit.assert_not_called()
        self.db.execute.side_effect = [self._att_row(), _scope(), MagicMock()]
        with patch.object(lpf, "followup_access_level", return_value=4):
            assert client.delete(f"{ATT}/77?co_id=1").status_code == 200
        self.db.commit.assert_called_once()

    def test_download_and_list_need_view(self):
        self.db.execute.side_effect = [self._att_row(), _scope()]
        with patch.object(lpf, "followup_access_level", return_value=0):
            assert client.get(f"{ATT}/77/download?co_id=1").status_code == 403
        self.db.execute.side_effect = [self._att_row(), _scope()]
        with patch.object(lpf, "followup_access_level", return_value=1), \
                patch.object(attachments_router_module.s3_client, "presigned_get_url",
                             return_value="https://x"):
            assert client.get(f"{ATT}/77/download?co_id=1").status_code == 200
        self.db.execute.side_effect = [_scope()]
        with patch.object(lpf, "followup_access_level", return_value=0):
            resp = client.get(f"{ATT}", params={"co_id": 1, "module": "jute_prod_followup",
                                                "entity_id": 5})
        assert resp.status_code == 403
        # another company's follow-up: not found
        self.db.execute.side_effect = [_scope(co_id=9)]
        with patch.object(lpf, "followup_access_level", return_value=4):
            resp = client.get(f"{ATT}", params={"co_id": 1, "module": "jute_prod_followup",
                                                "entity_id": 5})
        assert resp.status_code == 404

    def test_other_modules_skip_the_gate(self):
        res = MagicMock()
        res.fetchall.return_value = []
        self.db.execute.return_value = res
        with patch.object(lpf, "check_notice_attachment_access") as gate:
            resp = client.get(f"{ATT}", params={"co_id": 1, "module": "billpass", "entity_id": 5})
        assert resp.status_code == 200
        gate.assert_not_called()
