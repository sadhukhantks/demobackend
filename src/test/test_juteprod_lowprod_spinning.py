"""Spinning Low Producer report (MIS port, spec section 2).

Served by src/juteProduction/lowprod_spinning.py under the
/api/juteProductionReports prefix. The router is registered in main.py by the
integrator, so the handler is called DIRECTLY here (test_jute_report_endpoint_
guards.py's convention) — no TestClient, no app registration needed.

The arithmetic that matters to a low-producer list splits in two:
  * per-row std_prod / eff_pct is SQL (it needs the date-effective target map),
    so it is not re-implemented here; what IS asserted is that the row's scope
    guards survive — the machine-spine co scope, the spinning weight types and
    the spinner designation gate;
  * the summaries and the "shed average" rule (zero efficiency never enters an
    average) are pure builders, exercised directly on stub rows.
"""

import re
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.juteProduction.lowprod_spinning import (
    apply_full_shift_filter,
    build_day_summary,
    build_meta,
    build_worker_summary,
    shape_rows,
    spinning_low_producer,
)
from src.juteProduction.lowprod_spinning_query import get_spinning_lowprod_base_query


GOOD = {
    "co_id": "1",
    "branch_id": "4",
    "date_from": "2026-08-01",
    "date_to": "2026-08-03",
}


def _request(**overrides):
    params = dict(GOOD)
    params.update(overrides)
    params = {k: v for k, v in params.items() if v is not None}
    return SimpleNamespace(query_params=params)


def _sql_row(**over):
    """A row as the base query emits it (before shape_rows)."""
    m = {
        "doff_date": date(2026, 8, 1),
        "spell_code": "A1",
        "shift": "A",
        "machine_id": 11,
        "frame": "F11",
        "item_id": 7,
        "quality_name": "8 LB HESSIAN",
        "eb_id": 501,
        "eb_no": "E501",
        "worker_name": "RAMESH DAS",
        "net_wt": Decimal("120.500"),
        "doff_count": 6,
        "attendance_type": "R",
        "att_approved": 1,
        "whrs": Decimal("8.00"),
        "spell_hours": Decimal("8.00"),
        "p100prod": Decimal("200.000"),
        "std_prod": Decimal("200.000"),
        "no_of_frame": Decimal("1.000"),
        "eff_pct": Decimal("60.25"),
    }
    m.update(over)
    return m


class _DB:
    """First execute = the branch/co pair check, second = the base query."""

    def __init__(self, pair=True, rows=(), explode=False):
        self.pair = pair
        self.rows = list(rows)
        self.explode = explode
        self.calls = []

    def execute(self, stmt, params=None):
        self.calls.append(params)
        res = MagicMock()
        if len(self.calls) == 1:
            res.first.return_value = (1,) if self.pair else None
            return res
        if self.explode:
            raise RuntimeError(
                '(pymysql.err.OperationalError) (1525, "Incorrect DATE value")\n'
                "[SQL: WITH doff AS (SELECT d.doff_date ...)]"
            )
        res.fetchall.return_value = [SimpleNamespace(_mapping=r) for r in self.rows]
        return res


def _call(db=None, **overrides):
    db = db if db is not None else _DB(rows=[_sql_row()])
    return spinning_low_producer(_request(**overrides), db=db, token_data={})


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["co_id", "branch_id", "date_from", "date_to"])
def test_every_scope_param_is_required(missing):
    with pytest.raises(HTTPException) as exc:
        _call(**{missing: None})
    assert exc.value.status_code == 400
    assert missing.split("_")[0] in exc.value.detail


@pytest.mark.parametrize("bad", ["01-08-2026", "not-a-date", "2026-13-45"])
def test_malformed_dates_are_400_and_never_reach_the_db(bad):
    db = _DB(explode=True)
    with pytest.raises(HTTPException) as exc:
        _call(db=db, date_from=bad)
    assert exc.value.status_code == 400
    assert db.calls == []


def test_reversed_range_rejected():
    with pytest.raises(HTTPException) as exc:
        _call(date_from="2026-08-10", date_to="2026-08-01")
    assert exc.value.status_code == 400


def test_range_capped_at_62_days():
    with pytest.raises(HTTPException) as exc:
        _call(date_from="2026-01-01", date_to="2026-04-30")
    assert exc.value.status_code == 400
    assert "62" in exc.value.detail
    # the last accepted window is 62 days inclusive-exclusive
    assert _call(date_from="2026-01-01", date_to="2026-03-02")["data"]


@pytest.mark.parametrize("bad", ["D", "shift-a", "1"])
def test_bad_shift_rejected(bad):
    with pytest.raises(HTTPException) as exc:
        _call(shift=bad)
    assert exc.value.status_code == 400


def test_shift_is_case_insensitive_and_bound_as_a_letter():
    db = _DB(rows=[_sql_row()])
    _call(db=db, shift="b")
    assert db.calls[1]["shift"] == "B"


def test_bad_item_id_rejected():
    with pytest.raises(HTTPException) as exc:
        _call(item_id="abc")
    assert exc.value.status_code == 400


def test_frame_and_eb_no_are_bound_as_substring_patterns():
    db = _DB(rows=[_sql_row()])
    _call(db=db, frame="11", eb_no="E50")
    assert db.calls[1]["frame_like"] == "%11%"
    assert db.calls[1]["eb_no_like"] == "%E50%"


def test_absent_optional_filters_bind_null():
    db = _DB(rows=[_sql_row()])
    _call(db=db)
    binds = db.calls[1]
    assert (binds["shift"], binds["item_id"], binds["frame_like"], binds["eb_no_like"]) == (
        None,
        None,
        None,
        None,
    )
    assert binds["desig_prefix"] == "spinner%"


# ---------------------------------------------------------------------------
# co/branch pair check
# ---------------------------------------------------------------------------


def test_branch_of_another_company_is_400_before_the_report_runs():
    db = _DB(pair=False)
    with pytest.raises(HTTPException) as exc:
        _call(db=db)
    assert exc.value.status_code == 400
    assert "does not belong" in exc.value.detail
    assert len(db.calls) == 1  # the base query never ran


def test_db_errors_do_not_leak_sql_to_the_client():
    with pytest.raises(HTTPException) as exc:
        _call(db=_DB(explode=True))
    assert exc.value.status_code == 500
    assert "SQL:" not in exc.value.detail


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def test_response_shape():
    out = _call()
    assert set(out) == {"data"}
    assert set(out["data"]) == {"base_rows", "worker_summary", "day_summary", "meta"}
    assert set(out["data"]["meta"]) >= {
        "co_id",
        "branch_id",
        "date_from",
        "date_to",
        "filtered_avg_eff",
        "overall_avg_eff",
        "notes",
    }
    row = out["data"]["base_rows"][0]
    assert set(row) >= {
        "doff_date",
        "shift",
        "spell_code",
        "attendance_type",
        "eb_id",
        "eb_label",
        "worker_name",
        "machine_id",
        "frame",
        "item_id",
        "quality_name",
        "net_wt",
        "whrs",
        "std_prod",
        "no_of_frame",
        "eff_pct",
    }


def test_shape_rows_makes_the_row_json_safe():
    row = shape_rows([SimpleNamespace(_mapping=_sql_row())])[0]
    assert row["doff_date"] == "2026-08-01"
    assert isinstance(row["net_wt"], float) and row["net_wt"] == 120.5
    assert isinstance(row["eff_pct"], float)
    assert row["eb_label"] == "E501"


def test_contract_rows_are_labelled_and_named_contract():
    row = shape_rows(
        [
            SimpleNamespace(
                _mapping=_sql_row(
                    eb_id=None, eb_no=None, worker_name=None,
                    attendance_type="Contract",
                )
            )
        ]
    )[0]
    assert row["eb_label"] == "Contract"
    assert row["worker_name"] == "Contract"


def test_eb_without_an_emp_code_falls_back_to_the_eb_id():
    row = shape_rows([SimpleNamespace(_mapping=_sql_row(eb_no=None))])[0]
    assert row["eb_label"] == "501"


def test_notes_report_missing_attendance_and_missing_standards():
    rows = [
        _sql_row(),
        _sql_row(eb_id=None, eb_no=None, attendance_type="Contract"),
        _sql_row(machine_id=12, std_prod=Decimal("0"), eff_pct=Decimal("0")),
    ]
    notes = _call(db=_DB(rows=rows))["data"]["meta"]["notes"]
    assert any("contract" in n for n in notes)
    assert any("no standard" in n for n in notes)


def test_note_flags_attendance_hours_longer_than_the_spell():
    """sls records a full day's 8 hours on a 5-hour A1 attendance row, which
    scales that worker's standard to 1.6x the frame's full-spell production."""
    rows = [_sql_row(whrs=Decimal("8"), spell_hours=Decimal("5"))]
    notes = _call(db=_DB(rows=rows))["data"]["meta"]["notes"]
    assert any("more attended hours than the spell is long" in n for n in notes)


# ---------------------------------------------------------------------------
# "Shed average": zero efficiency never enters an average
# ---------------------------------------------------------------------------


def _shaped(*rows):
    return shape_rows([SimpleNamespace(_mapping=r) for r in rows])


def test_zero_eff_rows_are_excluded_from_the_filtered_average_only():
    rows = _shaped(
        _sql_row(eff_pct=Decimal("40")),
        _sql_row(machine_id=12, eff_pct=Decimal("60")),
        _sql_row(machine_id=13, std_prod=Decimal("0"), eff_pct=Decimal("0")),
    )
    meta = build_meta(1, 4, date(2026, 8, 1), date(2026, 8, 3), rows, [])
    assert meta["filtered_avg_eff"] == 50.0
    assert meta["overall_avg_eff"] == round(100 / 3, 2)


def test_averages_are_blank_not_zero_when_there_is_nothing_to_average():
    """An empty average is None: 0.0 would read as a measured shed running at
    nothing, which is a different and much worse claim than 'no data'."""
    empty = build_meta(1, 4, date(2026, 8, 1), date(2026, 8, 3), [], [])
    assert empty["filtered_avg_eff"] is None
    assert empty["overall_avg_eff"] is None

    no_standard = _shaped(_sql_row(std_prod=Decimal("0"), eff_pct=Decimal("0")))
    meta = build_meta(1, 4, date(2026, 8, 1), date(2026, 8, 3), no_standard, [])
    assert meta["filtered_avg_eff"] is None   # nothing had a standard
    assert meta["overall_avg_eff"] == 0.0     # the unfiltered figure keeps the 0


def test_worker_average_ignores_the_zero_eff_day_but_still_counts_the_day():
    rows = _shaped(
        _sql_row(eff_pct=Decimal("40")),
        _sql_row(doff_date=date(2026, 8, 2), std_prod=Decimal("0"), eff_pct=Decimal("0")),
    )
    worker = build_worker_summary(rows)[0]
    assert worker["avg_eff"] == 40.0
    assert worker["days_attended"] == 2


def test_missing_attendance_defaults_hours_without_breaking_the_summary():
    """A contract row (no spinner attendance) carries the spell's full hours and
    an A1 frame fraction of 5/8 — it must still aggregate, not vanish."""
    rows = _shaped(
        _sql_row(
            eb_id=None, eb_no=None, worker_name=None, attendance_type="Contract",
            whrs=Decimal("5"), spell_hours=Decimal("5"), no_of_frame=Decimal("0.625"),
            eff_pct=Decimal("55"),
        )
    )
    worker = build_worker_summary(rows)[0]
    assert worker["eb_id"] is None
    assert worker["worker_name"] == "Contract"
    assert worker["avg_frames_run"] == 0.62  # 5/8, half-even to 2dp
    assert worker["avg_eff"] == 55.0


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def test_worker_summary_is_shift_split_and_sorted_worst_first():
    rows = _shaped(
        _sql_row(eb_id=1, eb_no="E1", worker_name="A WORKER", eff_pct=Decimal("80")),
        _sql_row(eb_id=1, eb_no="E1", worker_name="A WORKER", shift="B",
                 spell_code="B1", eff_pct=Decimal("60")),
        _sql_row(eb_id=2, eb_no="E2", worker_name="B WORKER", shift="C",
                 spell_code="C", eff_pct=Decimal("30")),
    )
    summary = build_worker_summary(rows)
    assert [s["eb_id"] for s in summary] == [2, 1]
    first, second = summary
    assert (first["eff_c"], first["avg_eff"]) == (30.0, 30.0)
    assert (second["eff_a"], second["eff_b"], second["avg_eff"]) == (80.0, 60.0, 70.0)


def test_a_worker_with_no_standard_at_all_gets_a_blank_average_and_still_sorts():
    rows = _shaped(
        _sql_row(eb_id=1, eb_no="E1", worker_name="A WORKER", eff_pct=Decimal("60")),
        _sql_row(eb_id=2, eb_no="E2", worker_name="B WORKER",
                 std_prod=Decimal("0"), eff_pct=Decimal("0")),
    )
    summary = build_worker_summary(rows)
    assert {s["eb_id"]: s["avg_eff"] for s in summary} == {1: 60.0, 2: None}


def test_worker_summary_averages_frames_over_the_days_worked():
    rows = _shaped(
        _sql_row(no_of_frame=Decimal("1")),
        _sql_row(machine_id=12, no_of_frame=Decimal("1")),
        _sql_row(doff_date=date(2026, 8, 2), no_of_frame=Decimal("2")),
    )
    assert build_worker_summary(rows)[0]["avg_frames_run"] == 2.0


def test_day_summary_splits_by_shift_and_counts_distinct_frames():
    rows = _shaped(
        _sql_row(net_wt=Decimal("100"), eff_pct=Decimal("50")),
        # same frame, second spell of the A shift -> still ONE frame run
        _sql_row(spell_code="A2", net_wt=Decimal("50"), eff_pct=Decimal("70")),
        _sql_row(machine_id=12, frame="F12", shift="B", spell_code="B1",
                 net_wt=Decimal("60"), eff_pct=Decimal("0"), std_prod=Decimal("0")),
        _sql_row(doff_date=date(2026, 8, 2), net_wt=Decimal("10"),
                 eff_pct=Decimal("20")),
    )
    day1, day2 = build_day_summary(rows)
    assert day1["date"] == "2026-08-01"
    assert (day1["net_wt_a"], day1["net_wt_b"], day1["net_wt_total"]) == (150.0, 60.0, 210.0)
    assert (day1["frames_run_a"], day1["frames_run_b"], day1["frames_run_total"]) == (1, 1, 2)
    assert day1["avg_eff_a"] == 60.0
    # Its only B row has no standard, so the shift has NO efficiency at all —
    # blank, not a zero that would read as "produced nothing against a target".
    assert day1["avg_eff_b"] is None
    assert day1["avg_eff_total"] == 60.0     # count-weighted over non-zero rows
    assert day2["net_wt_total"] == 10.0


def test_non_abc_spells_fold_into_the_totals_as_g():
    rows = _shaped(_sql_row(shift="G", spell_code="GEN", net_wt=Decimal("20"),
                            eff_pct=Decimal("90")))
    day = build_day_summary(rows)[0]
    assert (day["net_wt_a"], day["net_wt_b"], day["net_wt_c"]) == (0.0, 0.0, 0.0)
    assert day["net_wt_total"] == 20.0
    assert day["avg_eff_total"] == 90.0
    assert day["frames_run_total"] == 1


# ---------------------------------------------------------------------------
# full_shift_regular_only (the legacy "whatsapp" view)
# ---------------------------------------------------------------------------


def test_full_shift_filter_keeps_only_attendance_backed_full_rows_worst_first():
    rows = _shaped(
        _sql_row(eb_id=1, whrs=Decimal("8"), eff_pct=Decimal("70")),
        _sql_row(eb_id=2, whrs=Decimal("8"), eff_pct=Decimal("40")),
        _sql_row(eb_id=3, whrs=Decimal("5")),                       # part shift
        _sql_row(eb_id=None, attendance_type="Contract", whrs=Decimal("8")),
    )
    kept = apply_full_shift_filter(rows)
    assert [r["eb_id"] for r in kept] == [2, 1]


def test_full_shift_is_the_rows_own_spell_length_not_a_fixed_eight():
    """A 5-hour A1 spell worked in full IS a full shift and must survive the
    filter; 8 hours booked against that same 5-hour spell is not, and a spell
    with no length at all cannot be judged."""
    rows = _shaped(
        _sql_row(eb_id=1, whrs=Decimal("5"), spell_hours=Decimal("5"),
                 eff_pct=Decimal("70")),
        _sql_row(eb_id=2, whrs=Decimal("8"), spell_hours=Decimal("5"),
                 eff_pct=Decimal("40")),
        _sql_row(eb_id=3, whrs=Decimal("7.5"), spell_hours=Decimal("7.5"),
                 eff_pct=Decimal("30")),
        _sql_row(eb_id=4, whrs=Decimal("0"), spell_hours=Decimal("0"),
                 eff_pct=Decimal("90")),
    )
    assert [r["eb_id"] for r in apply_full_shift_filter(rows)] == [3, 1]


def test_full_shift_flag_filters_the_endpoint_and_notes_it():
    rows = [
        _sql_row(eb_id=1, whrs=Decimal("8")),
        _sql_row(eb_id=None, eb_no=None, attendance_type="Contract", whrs=Decimal("5")),
    ]
    data = _call(db=_DB(rows=rows), full_shift_regular_only="true")["data"]
    assert [r["eb_id"] for r in data["base_rows"]] == [1]
    assert any("Full-shift regular only" in n for n in data["meta"]["notes"])
    # The note must state the spell-relative rule, not a fixed 8 hours.
    assert any(
        "equal their spell's configured hours" in n for n in data["meta"]["notes"]
    )


def test_full_shift_flag_is_off_by_default():
    rows = [_sql_row(eb_id=1, whrs=Decimal("8")), _sql_row(eb_id=2, whrs=Decimal("5"))]
    assert len(_call(db=_DB(rows=rows))["data"]["base_rows"]) == 2


# ---------------------------------------------------------------------------
# Scope guards in the SQL itself
# ---------------------------------------------------------------------------


class TestBaseQueryScope:
    """daily_doff_tbl has no co_id and daily_attendance has none either — if any
    of these guards is edited away the report silently serves another company's
    frames, which no row-level assertion above would catch."""

    sql = str(get_spinning_lowprod_base_query())

    def test_company_scope_rides_the_machine_spine(self):
        assert "machine_mst mm" in self.sql
        assert "bmm.co_id = :co_id" in self.sql

    def test_attendance_is_company_scoped_by_branch_list(self):
        assert "FROM branch_mst b WHERE b.co_id = :co_id" in self.sql

    def test_only_spinning_weight_types_count(self):
        assert "d.weight_type IN ('SPG1', 'Auto')" in self.sql

    def test_attendance_gate_is_the_spinner_designation_prefix(self):
        assert "LOWER(COALESCE(dm.desig, '')) LIKE :desig_prefix" in self.sql

    def test_spells_and_attendance_join_on_spell_id_never_on_spell_code(self):
        # spell_code repeats once per shift generation (sls 'A1' = 91/97/102),
        # so a name join rakes in another generation's rows.
        assert "sp.spell_id = df.spell_id" in self.sql
        assert "sp.spell_id = u.spell_id" in self.sql
        assert "at.spell_id = df.spell_id" in self.sql

    def test_spell_hours_come_from_spell_mst_with_the_legacy_map_as_fallback(self):
        """Per the ruling: working_hours is the source, the hardcoded MIS values
        survive only for a spell that has none configured."""
        assert "COALESCE(NULLIF(sp.working_hours, 0)," in self.sql
        assert "WHEN 'C'  THEN 7.5" in self.sql   # fallback only, inside the COALESCE

    def test_the_standard_and_frame_fraction_divide_by_the_spell_hours(self):
        assert (
            "ROUND(b.p100prod * b.whrs / NULLIF(b.spell_hours, 0), 3) AS std_prod"
            in self.sql
        )
        assert "ROUND(b.whrs / NULLIF(b.spell_hours, 0), 3) AS no_of_frame" in self.sql

    def test_non_zero_frozen_p100prod_wins_but_a_frozen_zero_falls_through(self):
        # TKT-000179: NULLIF so a unit Processed before the standards existed
        # (frozen p100prod = 0) takes the live recompute instead of std 0.
        assert "COALESCE(\n                       NULLIF(lg.p100prod, 0)," in self.sql

    def test_the_date_filter_leads_the_doff_scan(self):
        first_where = self.sql.split("WHERE", 1)[1].strip()
        assert first_where.startswith("d.doff_date BETWEEN :d_from AND :d_to")


# ---------------------------------------------------------------------------
# TKT-000179: a unit Processed before the Spinning Standards were entered
# froze p100prod = 0 (the column is NOT NULL DEFAULT 0). That 0 must NOT beat
# the live recompute once the standards exist, or Std Prod / Eff stay 0 for
# those days forever (sls Aug 2026). Same rule as doff_analysis_query.
# ---------------------------------------------------------------------------
def test_frozen_zero_p100prod_falls_through_to_the_live_standard():
    from src.juteProduction.lowprod_spinning_query import get_spinning_lowprod_base_query

    sql = str(get_spinning_lowprod_base_query())
    assert "NULLIF(lg.p100prod, 0)" in sql
    # The old form: COALESCE(lg.p100prod, <live>) — a frozen 0 is not NULL and wins.
    assert re.search(r"COALESCE\(\s*lg\.p100prod\s*,", sql) is None
