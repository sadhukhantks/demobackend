"""Unit tests for the Hands Report (src/juteProduction/hands_report.py).

Everything worth testing is a pure function over three result sets — parameter
validation, the effective-dated norm pick, the fixed/variable target split, and
the rollups — so no DB is involved. The handler tests only exercise the
validation that runs *before* the session is touched (db=None proves it).

The "machines worked from attendance" tests at the bottom (spec §11) add the
rebuild of ``jute_prod_machine_shift_worked``: the statements it runs, their
order and the deadlock retry are checked against a recording fake session.

The manual additions / exclusions of worked machines (spec §12) follow: the
builder rules (an ADD counts, an EXCLUDE does not, status precedence,
``suggested``) and the override statements of the rebuild. The three override
endpoints are in ``test_juteprod_hands_report_overrides.py``.
"""

from datetime import date, datetime

import pymysql
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from src.juteProduction.hands_report import (
    HANDS_REPORT_MAX_DAYS,
    SHIFTS,
    VIEWS,
    _kind,
    _norm_for,
    _norms_by_designation,
    _require_view,
    _scope,
    _targets,
    build_hands_report,
    hands_report,
    rebuild_machine_shift_worked,
)
from src.juteProduction.hands_report_query import (
    get_machine_shift_worked_apply_add_query,
    get_machine_shift_worked_apply_exclude_query,
    get_machine_shift_worked_delete_query,
    get_machine_shift_worked_insert_query,
    get_machine_shift_worked_rows_query,
    get_machine_shift_worked_source_query,
)


class Req:
    """Minimal stand-in for a Starlette Request — the report only reads
    query_params."""

    def __init__(self, **params):
        self.query_params = {k: str(v) for k, v in params.items() if v is not None}


def params(**over) -> dict:
    base = {
        "co_id": 2,
        "branch_id": 87,
        "date_from": "2026-08-01",
        "date_to": "2026-08-14",
    }
    base.update(over)
    return base


# --- parameter validation --------------------------------------------------


def test_scope_returns_typed_values():
    assert _scope(Req(**params())) == (2, 87, date(2026, 8, 1), date(2026, 8, 14))


@pytest.mark.parametrize("missing", ["co_id", "branch_id", "date_from", "date_to"])
def test_scope_requires_every_scope_param(missing):
    p = params()
    p.pop(missing)
    with pytest.raises(HTTPException) as e:
        _scope(Req(**p))
    assert e.value.status_code == 400


def test_scope_rejects_reversed_range():
    with pytest.raises(HTTPException) as e:
        _scope(Req(**params(date_from="2026-08-14", date_to="2026-08-01")))
    assert "before" in e.value.detail


def test_scope_rejects_range_over_the_cap():
    # Inclusive cap: the 62nd day is the first rejected one.
    ok = params(date_from="2026-06-01", date_to="2026-07-01")
    assert _scope(Req(**ok))[3] == date(2026, 7, 1)
    with pytest.raises(HTTPException) as e:
        _scope(Req(**params(date_from="2026-01-01", date_to="2026-12-31")))
    assert str(HANDS_REPORT_MAX_DAYS) in e.value.detail


def test_scope_rejects_garbage_ids_and_dates():
    for bad in (params(co_id="abc"), params(branch_id="x"), params(date_to="14-08-2026")):
        with pytest.raises(HTTPException) as e:
            _scope(Req(**bad))
        assert e.value.status_code == 400


def test_view_defaults_to_detailed_and_rejects_unknown():
    assert _require_view(Req()) == "detailed"
    assert _require_view(Req(view="BY_DATE")) == "by_date"
    with pytest.raises(HTTPException):
        _require_view(Req(view="by_worker"))


def test_handler_validates_before_touching_the_session():
    # db=None: any DB access would raise AttributeError, not HTTPException 400.
    with pytest.raises(HTTPException) as e:
        hands_report(Req(**params(co_id=None)), db=None, token_data={})
    assert e.value.status_code == 400


# --- effective-dated norm resolution ---------------------------------------


def norm(**over) -> dict:
    base = {
        "hands_norm_id": 1,
        "designation_id": 10,
        "branch_id": None,
        "direct_indirect": "D",
        "variable_fixed": "F",
        "shift_a": 4,
        "shift_b": 4,
        "shift_c": 2,
        "shift_g": 0,
        "machine_type_id": None,
        "hands_per_machine": None,
        "norms_text": None,
        "effective_date": date(2026, 1, 1),
    }
    base.update(over)
    return base


def test_norm_for_picks_the_latest_row_in_force():
    rows = _norms_by_designation(
        [
            norm(hands_norm_id=1, effective_date=date(2026, 1, 1), shift_a=4),
            norm(hands_norm_id=2, effective_date=date(2026, 6, 1), shift_a=6),
            norm(hands_norm_id=3, effective_date=date(2026, 9, 1), shift_a=9),
        ]
    )[10]
    assert _norm_for(rows, date(2025, 12, 31)) is None  # nothing in force yet
    assert _norm_for(rows, date(2026, 1, 1))["shift_a"] == 4  # boundary is inclusive
    assert _norm_for(rows, date(2026, 8, 25))["shift_a"] == 6  # future row ignored
    assert _norm_for(rows, date(2026, 9, 2))["shift_a"] == 9


def test_branch_specific_row_beats_the_null_branch_fallback_on_the_same_date():
    rows = _norms_by_designation(
        [
            norm(hands_norm_id=1, branch_id=None, shift_a=4),
            norm(hands_norm_id=2, branch_id=87, shift_a=7),
        ]
    )[10]
    assert _norm_for(rows, date(2026, 8, 1))["shift_a"] == 7


def test_a_newer_null_branch_row_still_supersedes_an_older_branch_row():
    # Effective date is the primary key of the pick; branch only breaks ties.
    rows = _norms_by_designation(
        [
            norm(hands_norm_id=1, branch_id=87, effective_date=date(2026, 1, 1), shift_a=7),
            norm(hands_norm_id=2, branch_id=None, effective_date=date(2026, 6, 1), shift_a=4),
        ]
    )[10]
    assert _norm_for(rows, date(2026, 8, 1))["shift_a"] == 4


# --- fixed vs variable targets ---------------------------------------------

D = date(2026, 8, 1)


def test_fixed_norm_targets_come_from_its_own_shift_columns():
    assert _targets(norm(shift_a=4, shift_b=4, shift_c=2, shift_g=1), D, {}) == {
        "a": 4.0,
        "b": 4.0,
        "c": 2.0,
        "g": 1.0,
    }


def test_variable_norm_multiplies_machines_that_produced_by_hands_per_machine():
    runs = {(D, "A", 5): 40, (D, "B", 5): 30, (D, "C", 5): 0}
    t = _targets(norm(variable_fixed="V", machine_type_id=5, hands_per_machine=0.5), D, runs)
    assert t == {"a": 20.0, "b": 15.0, "c": 0.0, "g": 0.0}


def test_variable_norm_on_a_machine_type_with_no_production_targets_zero():
    t = _targets(norm(variable_fixed="V", machine_type_id=99, hands_per_machine=2), D, {})
    assert set(t.values()) == {0.0}


def test_variable_norm_with_no_machine_link_is_unresolvable():
    t = _targets(norm(variable_fixed="V"), D, {(D, "A", 5): 40})
    assert t == {s: None for s in SHIFTS}


def test_no_norm_at_all_leaves_every_target_blank():
    assert _targets(None, D, {(D, "A", 5): 40}) == {s: None for s in SHIFTS}


def unlinked_machine_norm(**over) -> dict:
    """TKT-000165: what the EMPMILL12 seed put on sls for every per-machine
    norm — flagged 'F', a parsed hands_per_machine ('2 MC = 1 HAND' -> 0.5),
    zero shift columns and NO machine_type_id to count machines of."""
    base = {
        "variable_fixed": "F",
        "shift_a": 0,
        "shift_b": 0,
        "shift_c": 0,
        "shift_g": 0,
        "machine_type_id": None,
        "hands_per_machine": 0.5,
        "norms_text": "2 MC = 1 HAND",
    }
    base.update(over)
    return norm(**base)


def test_per_machine_norm_with_no_machine_type_is_unknown_not_a_zero_target():
    # Read as a fixed norm it printed 0 in every shift, i.e. a real-looking
    # target that made every weaver / spinner on sls an "excess" hand.
    t = _targets(unlinked_machine_norm(), D, {(D, "A", 5): 40})
    assert t == {s: None for s in SHIFTS}


def test_per_machine_norm_with_explicit_shift_counts_keeps_them():
    # Explicit shift counts are a genuine fixed target even with no machine link.
    t = _targets(unlinked_machine_norm(shift_a=3, shift_b=2), D, {})
    assert t == {"a": 3.0, "b": 2.0, "c": 0.0, "g": 0.0}


# --- report assembly -------------------------------------------------------


def actual(tran_date, designation_id, shift, hands, **over) -> dict:
    base = {
        "tran_date": tran_date,
        "designation_id": designation_id,
        "shift": shift,
        "designation": f"DESIG-{designation_id}",
        "desig_direct_indirect": "D",
        "dept_id": 1,
        "dept": "WEAVING",
        "hands": hands,
    }
    base.update(over)
    return base


def machine(tran_date, shift, machine_id, sources, machine_type_id=5, **over) -> dict:
    """One machine-shift with the comma-joined sources it was seen in, in the
    shape of get_hands_machine_runs_query() — i.e. WITHOUT the worked flags of
    jute_prod_machine_shift_worked, so the builder derives them: the machine
    worked when 'Attendance' is among the sources and it was not stopped all
    shift. ``worked_row`` below is the table-shaped fixture."""
    base = {
        "tran_date": tran_date,
        "shift": shift,
        "machine_id": machine_id,
        "machine_name": f"LOOM-{machine_id}",
        "mech_code": str(machine_id),
        "machine_type_id": machine_type_id,
        "machine_type_name": f"TYPE-{machine_type_id}",
        "dept_id": 1,
        "dept": "WEAVING",
        "sources": sources,
        "hands": None,
        "att_stoppage_hours": 0,
        "stoppage_hours": 0,
        "breakdown_min": 0,
        # Hands Norms v2: production the entries booked on the machine-shift.
        "prod_kg": None,
        "prod_cuts": None,
        "winding_kg": None,
    }
    base.update(over)
    return base


def looms(tran_date, shift, n, machine_type_id=5, sources="Weaving,Attendance") -> list:
    """``n`` looms that WORKED: a weaving entry and a weaver marked on each
    (owner, 2026-10-01 — a weaving entry alone no longer counts)."""
    return [
        machine(tran_date, shift, 100 + i, sources, machine_type_id=machine_type_id)
        for i in range(n)
    ]


def report(actuals, norms=(), runs=(), view="detailed", **over):
    kwargs = {
        "co_id": 2,
        "branch_id": 87,
        "d_from": date(2026, 8, 1),
        "d_to": date(2026, 8, 2),
        "view": view,
    }
    kwargs.update(over)
    return build_hands_report(list(actuals), list(norms), list(runs), **kwargs)


def test_detailed_row_carries_shifts_targets_and_the_excess_short_split():
    data = report(
        [actual(D, 10, "A", 5.0), actual(D, 10, "B", 4.0)],
        [norm(shift_a=4, shift_b=4, shift_c=0, shift_g=0)],
    )
    (row,) = data["rows"]
    assert (row["shift_a"], row["shift_b"], row["shift_c"], row["shift_g"]) == (5.0, 4.0, 0.0, 0.0)
    assert row["total_actual"] == 9.0 and row["total_target"] == 8.0
    assert (row["excess"], row["short"]) == (1.0, 0.0)
    assert row["date"] == "2026-08-01"


def test_short_is_the_other_side_of_the_same_comparison():
    (row,) = report([actual(D, 10, "A", 2.0)], [norm(shift_a=4, shift_b=0, shift_c=0, shift_g=0)])["rows"]
    assert (row["excess"], row["short"]) == (0.0, 2.0)


def test_unknown_spell_letters_fall_into_the_general_shift():
    (row,) = report([actual(D, 10, "X", 3.0), actual(D, 10, None, 1.0)], [norm()])["rows"]
    assert row["shift_g"] == 4.0


def test_designation_without_a_norm_keeps_actuals_and_blanks_the_target():
    data = report([actual(D, 77, "A", 6.0)], [])
    (row,) = data["rows"]
    assert row["total_actual"] == 6.0
    assert row["total_target"] is None and row["excess"] is None and row["short"] is None
    assert any("no jute_prod_hands_norms_mst row" in n for n in data["meta"]["notes"])


def test_rollups_sum_the_per_day_excess_instead_of_netting_it():
    # Day 1 over by 2, day 2 short by 2. A recomputed rollup would show 0/0;
    # summing the per-day figures keeps both, which is the whole point.
    d2 = date(2026, 8, 2)
    fixed = [norm(shift_a=4, shift_b=0, shift_c=0, shift_g=0)]
    rows = report(
        [actual(D, 10, "A", 6.0), actual(d2, 10, "A", 2.0)], fixed, view="by_occupation"
    )["rows"]
    (row,) = rows
    assert row["total_actual"] == 8.0 and row["total_target"] == 8.0
    assert (row["excess"], row["short"]) == (2.0, 2.0)


def test_rollup_of_designations_with_no_norm_reports_no_target_not_zero():
    # A zero here would read as "target met" on a shed that simply has no norms
    # configured yet — it has to stay blank all the way up the rollups.
    for view in ("by_date", "by_occupation", "by_department"):
        (row,) = report([actual(D, 77, "A", 6.0)], [], view=view)["rows"]
        assert row["total_actual"] == 6.0
        assert row["total_target"] is None
        assert row["excess"] is None and row["short"] is None


def test_rollup_mixing_known_and_unknown_targets_sums_only_the_known_ones():
    rows = report(
        [actual(D, 10, "A", 5.0), actual(D, 77, "A", 6.0, designation="NO-NORM")],
        [norm(shift_a=4, shift_b=0, shift_c=0, shift_g=0)],
        view="by_date",
    )["rows"]
    (row,) = rows
    assert row["total_actual"] == 11.0
    assert row["total_target"] == 4.0  # only designation 10 has a norm
    assert row["excess"] == 1.0


def test_by_date_rolls_up_per_day_and_splits_direct_from_indirect():
    d2 = date(2026, 8, 2)
    rows = report(
        [
            actual(D, 10, "A", 5.0),
            actual(D, 11, "A", 3.0, designation="DESIG-11"),
            actual(d2, 10, "A", 1.0),
        ],
        [
            norm(designation_id=10, direct_indirect="D", shift_a=4, shift_b=0, shift_c=0, shift_g=0),
            norm(hands_norm_id=2, designation_id=11, direct_indirect="I", shift_a=2,
                 shift_b=0, shift_c=0, shift_g=0),
        ],
        view="by_date",
    )["rows"]
    assert [r["date"] for r in rows] == ["2026-08-01", "2026-08-02"]
    assert rows[0]["total_actual"] == 8.0 and rows[0]["total_target"] == 6.0
    assert rows[0]["direct_actual"] == 5.0 and rows[0]["indirect_actual"] == 3.0
    assert rows[1]["total_actual"] == 1.0


def test_by_department_adds_a_grand_total_row_object():
    data = report(
        [
            actual(D, 10, "A", 5.0, dept_id=1, dept="WEAVING"),
            actual(D, 11, "A", 3.0, dept_id=2, dept="SPINNING"),
        ],
        [norm(shift_a=4, shift_b=0, shift_c=0, shift_g=0)],
        view="by_department",
    )
    assert [r["dept"] for r in data["rows"]] == ["SPINNING", "WEAVING"]
    assert data["grand_total"]["total_actual"] == 8.0
    assert data["grand_total"]["dept"] == "GRAND TOTAL"
    # The other views must not carry one.
    assert "grand_total" not in report([actual(D, 10, "A", 1.0)], [norm()])


def test_variable_targets_use_the_machines_that_ran_that_date_and_shift():
    runs = looms(D, "A", 40)
    (row,) = report(
        [actual(D, 10, "A", 18.0)],
        [norm(variable_fixed="V", machine_type_id=5, hands_per_machine=0.5)],
        runs,
    )["rows"]
    assert row["target_a"] == 20.0 and row["target_b"] == 0.0
    assert row["short"] == 2.0


def test_a_variable_machine_type_that_never_produced_is_reported_in_the_notes():
    data = report(
        [actual(D, 10, "A", 4.0)],
        [norm(variable_fixed="V", machine_type_id=5, hands_per_machine=0.5)],
        looms(D, "A", 3, machine_type_id=9),
    )
    assert any("type(s) 5" in n for n in data["meta"]["notes"])


def test_unlinked_per_machine_norm_blanks_the_target_and_names_it_in_the_notes():
    # TKT-000165 (sls 10-Sep-2026): 36 such designations, ~320 of 672 hands.
    data = report(
        [
            actual(D, 10, "A", 29.0, designation="WEAVER(H)"),
            actual(D, 11, "A", 2.0, designation="HELPER(H)"),
        ],
        [
            unlinked_machine_norm(),  # designation 10
            norm(hands_norm_id=2, designation_id=11, shift_a=1, shift_b=0, shift_c=0),
        ],
        looms(D, "A", 40),
    )
    weaver, helper = sorted(data["rows"], key=lambda r: r["designation_id"])
    assert weaver["total_target"] is None
    assert weaver["excess"] is None and weaver["short"] is None
    assert weaver["hands_per_machine"] == 0.5
    # The fixed norm next to it is untouched, and only it feeds the totals.
    assert helper["total_target"] == 1.0 and helper["excess"] == 1.0
    assert data["meta"]["total_target"] == 1.0
    assert data["meta"]["total_excess"] == 1.0
    notes = data["meta"]["notes"]
    unlinked = [n for n in notes if "not linked to a machine type" in n]
    assert len(unlinked) == 1 and "WEAVER(H)" in unlinked[0]
    # Flagged 'F', so it must not be mislabelled as a variable norm.
    assert not any(n.startswith("Variable norm") and "WEAVER(H)" in n for n in notes)


def test_variable_norm_without_a_rate_is_still_reported_as_variable():
    data = report(
        [actual(D, 10, "A", 5.0, designation="JUTE ISSUE MAZDOOR")],
        [norm(variable_fixed="V", shift_a=5, norms_text="10 MT / HAND, 5 IN A")],
    )
    (row,) = data["rows"]
    assert row["total_target"] is None
    notes = data["meta"]["notes"]
    assert any(n.startswith("Variable norm") and "JUTE ISSUE MAZDOOR" in n for n in notes)
    assert not any("not linked to a machine type" in n for n in notes)


def test_dept_and_direct_indirect_filters_apply_after_norm_resolution():
    actuals = [
        actual(D, 10, "A", 5.0, dept_id=1, dept="WEAVING", desig_direct_indirect="I"),
        actual(D, 11, "A", 3.0, dept_id=2, dept="SPINNING", desig_direct_indirect="I"),
    ]
    # The norm says Direct even though the designation master says Indirect —
    # the norm has to win, so 10 must land in D and not in I.
    norms = [norm(designation_id=10, direct_indirect="D", shift_a=4, shift_b=0, shift_c=0, shift_g=0)]
    only_dept = report(actuals, norms, dept_id=1)["rows"]
    assert [r["designation_id"] for r in only_dept] == [10]
    direct = report(actuals, norms, direct_indirect="D")["rows"]
    assert [r["designation_id"] for r in direct] == [10]
    # 11 has no norm at all, so it falls back to the designation master's 'I'.
    indirect = report(actuals, norms, direct_indirect="I")["rows"]
    assert [r["designation_id"] for r in indirect] == [11]


def test_empty_window_still_returns_the_contract_shape():
    data = report([], [], view="by_department")
    assert data["rows"] == []
    assert data["grand_total"]["total_actual"] == 0.0
    meta = data["meta"]
    assert meta["co_id"] == 2 and meta["branch_id"] == 87
    assert meta["date_from"] == "2026-08-01" and meta["date_to"] == "2026-08-02"
    assert meta["filtered_avg_eff"] is None and meta["overall_avg_eff"] is None
    assert any("/ 8" in n for n in meta["notes"])


# --- machines run -----------------------------------------------------------


def test_machines_run_lists_one_row_per_machine_shift_with_its_sources():
    runs = [
        machine(D, "A", 1, "Attendance,Stoppage,Weaving", hands=2, stoppage_hours=1.5),
        machine(D, "B", 1, "Weaving"),
        machine(D, "A", 2, "Doff", machine_type_id=9, machine_type_name="SPINNING"),
    ]
    data = report([], [], runs, view="machines_run")
    rows = data["rows"]
    assert [(r["machine_id"], r["shift"]) for r in rows] == [(2, "A"), (1, "A"), (1, "B")]
    loom_a = rows[1]
    # RUN sources first in their canonical order, STOP sources last.
    assert loom_a["sources"] == "Weaving, Attendance, Stoppage"
    assert loom_a["hands"] == 2 and loom_a["stoppage_hours"] == 1.5
    assert loom_a["counted"] is True
    assert rows[0]["machine_type"] == "SPINNING" and rows[0]["hands"] is None
    # Only the loom with a worker marked on it counts (owner, 2026-10-01): the
    # weaving entry of shift B and the doff alone are listed, not counted.
    assert [r["counted"] for r in rows] == [False, True, False]
    assert data["meta"]["machine_shifts_run"] == 1
    assert data["meta"]["machines_run"] == 1
    assert data["meta"]["stopped_only_rows"] == 2


def test_a_machine_seen_only_in_stoppage_or_breakdown_is_listed_but_not_counted():
    runs = [
        machine(D, "A", 1, "Weaving,Attendance"),
        machine(D, "A", 2, "Stoppage", stoppage_hours=8),
        machine(D, "G", 3, "Breakdown", breakdown_min=240),
    ]
    data = report(
        [actual(D, 10, "A", 1.0)],
        [norm(variable_fixed="V", machine_type_id=5, hands_per_machine=1)],
        runs,
        view="machines_run",
    )
    by_id = {r["machine_id"]: r for r in data["rows"]}
    assert by_id[1]["counted"] is True
    assert by_id[2]["counted"] is False and by_id[2]["stoppage_hours"] == 8.0
    assert by_id[3]["counted"] is False and by_id[3]["breakdown_min"] == 240.0
    assert data["meta"]["machine_shifts_run"] == 1
    assert data["meta"]["stopped_only_rows"] == 2
    # ...and the variable target only multiplies the counted one.
    (row,) = report([actual(D, 10, "A", 1.0)],
                    [norm(variable_fixed="V", machine_type_id=5, hands_per_machine=1)],
                    runs)["rows"]
    assert row["target_a"] == 1.0 and row["target_g"] == 0.0


def test_stoppage_hours_take_the_higher_of_the_two_books_not_the_sum():
    (row,) = report(
        [], [], [machine(D, "A", 1, "Attendance,Stoppage", stoppage_hours=3, att_stoppage_hours=5)],
        view="machines_run",
    )["rows"]
    assert row["stoppage_hours"] == 5.0


def test_machines_run_type_summary_counts_shifts_and_distinct_machines():
    d2 = date(2026, 8, 2)
    runs = [
        machine(D, "A", 1, "Weaving,Attendance"),
        machine(D, "B", 1, "Weaving,Attendance"),
        machine(D, "A", 2, "Weaving,Attendance"),
        machine(D, "A", 9, "Stoppage"),  # not counted
        machine(D, "C", 50, "Doff,Attendance", machine_type_id=9, machine_type_name="SPINNING"),
        machine(d2, "A", 1, "Attendance"),
    ]
    data = report([], [], runs, view="machines_run")
    summary = data["type_summary"]
    assert [(r["date"], r["machine_type"]) for r in summary] == [
        ("2026-08-01", "SPINNING"),
        ("2026-08-01", "TYPE-5"),
        ("2026-08-02", "TYPE-5"),
    ]
    looms_d1 = summary[1]
    assert (looms_d1["run_a"], looms_d1["run_b"], looms_d1["run_c"], looms_d1["run_g"]) == (2, 1, 0, 0)
    assert looms_d1["machine_shifts"] == 3 and looms_d1["machines"] == 2
    # the stoppage-only loom is the type's one row that did not count
    assert (looms_d1["stopped_all_shift"], looms_d1["no_worker_marked"]) == (0, 1)


def test_machines_run_dept_filter_uses_the_machine_department():
    runs = [
        machine(D, "A", 1, "Weaving,Attendance", dept_id=1, dept="WEAVING"),
        machine(D, "A", 2, "Doff,Attendance", dept_id=2, dept="SPINNING", machine_type_id=9),
    ]
    data = report([], [], runs, view="machines_run", dept_id=2)
    assert [r["machine_id"] for r in data["rows"]] == [2]
    assert [r["machine_type_id"] for r in data["type_summary"]] == [9]
    # Window-wide meta figures stay unfiltered so the header still tells the
    # whole story.
    assert data["meta"]["machine_shifts_run"] == 2


def test_a_duplicate_machine_row_never_counts_twice():
    runs = [machine(D, "A", 1, "Weaving"), machine(D, "A", 1, "Attendance")]
    (row,) = report(
        [actual(D, 10, "A", 1.0)],
        [norm(variable_fixed="V", machine_type_id=5, hands_per_machine=1)],
        runs,
    )["rows"]
    assert row["target_a"] == 1.0


def test_machines_run_view_is_accepted():
    assert _require_view(Req(view="machines_run")) == "machines_run"


# --- month-to-date columns (TKT-000154) --------------------------------------


def test_rollup_views_carry_the_month_to_date_as_at_the_to_date():
    # Window = 2-Aug only, but attendance exists on 1-Aug too (the endpoint
    # fetches from the 1st of the month): the window totals stay the day's,
    # the MTD columns and meta add the earlier day in.
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    fixed = [norm(shift_a=4, shift_b=0, shift_c=0, shift_g=0)]
    actuals = [actual(d1, 10, "A", 6.0), actual(d2, 10, "A", 2.0)]
    for view in ("by_occupation", "by_department"):
        data = report(actuals, fixed, view=view, d_from=d2, d_to=d2)
        (row,) = data["rows"]
        assert row["total_actual"] == 2.0 and row["total_target"] == 4.0
        assert (row["excess"], row["short"]) == (0.0, 2.0)
        assert row["mtd_actual"] == 8.0 and row["mtd_target"] == 8.0
        # per-day excess/short summed, never netted (2 over on the 1st, 2 short on the 2nd)
        assert (row["mtd_excess"], row["mtd_short"]) == (2.0, 2.0)
        meta = data["meta"]
        assert meta["mtd_from"] == "2026-08-01"
        assert (meta["total_actual"], meta["mtd_actual"]) == (2.0, 8.0)
        assert (meta["mtd_target"], meta["mtd_excess"], meta["mtd_short"]) == (8.0, 2.0, 2.0)
    grand = report(actuals, fixed, view="by_department", d_from=d2, d_to=d2)["grand_total"]
    assert grand["total_actual"] == 2.0 and grand["mtd_actual"] == 8.0


def test_per_date_views_carry_a_running_mtd_that_resets_each_month():
    jul31, aug1, aug2 = date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2)
    fixed = [norm(shift_a=4, shift_b=0, shift_c=0, shift_g=0)]
    actuals = [actual(jul31, 10, "A", 5.0), actual(aug1, 10, "A", 3.0), actual(aug2, 10, "A", 4.0)]
    for view in ("detailed", "by_date"):
        rows = report(actuals, fixed, view=view, d_from=jul31, d_to=aug2)["rows"]
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-07-31"]["mtd_actual"] == 5.0
        assert by_date["2026-08-01"]["mtd_actual"] == 3.0  # July does not carry over
        assert by_date["2026-08-02"]["mtd_actual"] == 7.0
        assert by_date["2026-08-02"]["mtd_target"] == 8.0
        assert by_date["2026-08-02"]["mtd_short"] == 1.0  # short 1 on the 1st, 0 on the 2nd
        assert by_date["2026-08-02"]["mtd_excess"] == 0.0


def test_by_date_mtd_counts_designations_absent_that_day():
    # Designation 10 worked on the 1st only, 11 on the 2nd only: the 2nd's
    # MTD still includes the 1st's hands of designation 10.
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    rows = report(
        [actual(d1, 10, "A", 6.0), actual(d2, 11, "A", 2.0, designation="DESIG-11")],
        [],
        view="by_date",
        d_from=d1,
        d_to=d2,
    )["rows"]
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-08-02"]["total_actual"] == 2.0
    assert by_date["2026-08-02"]["mtd_actual"] == 8.0


def test_mtd_target_stays_blank_when_no_norm_resolved():
    (row,) = report([actual(D, 77, "A", 6.0)], [], view="by_occupation")["rows"]
    assert row["mtd_actual"] == 6.0
    assert row["mtd_target"] is None and row["mtd_excess"] is None and row["mtd_short"] is None


def test_rows_before_the_window_do_not_leak_into_the_rows_or_the_notes():
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    data = report(
        [actual(d1, 99, "A", 1.0, designation="ONLY-BEFORE"), actual(d2, 10, "A", 2.0)],
        [norm()],
        view="detailed",
        d_from=d2,
        d_to=d2,
    )
    assert [r["designation_id"] for r in data["rows"]] == [10]
    assert not any("ONLY-BEFORE" in n for n in data["meta"]["notes"])
    assert any("MTD columns" in n for n in data["meta"]["notes"])


def test_machines_run_rows_carry_no_mtd_but_meta_still_does():
    data = report([actual(D, 10, "A", 1.0)], [norm()], looms(D, "A", 2), view="machines_run")
    assert all("mtd_actual" not in r for r in data["rows"])
    assert data["meta"]["mtd_actual"] == 1.0


# --- TKT-000167: Machines Run window, Detailed department / day totals -------


def test_machines_run_sheet_covers_only_the_requested_window():
    # The endpoint fetches machine runs from the 1st of the month for the MTD
    # variable targets. The sheet, its type summary and the meta counts must
    # still cover the requested window only: on sls a one-day request for
    # 15-Sep listed every machine-shift from 1-Sep (7,608 rows).
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    runs = looms(d1, "A", 3) + [machine(d1, "B", 9, "Stoppage")] + looms(d2, "A", 2)
    data = report([], [], runs, view="machines_run", d_from=d2, d_to=d2)
    assert {r["date"] for r in data["rows"]} == {"2026-08-02"}
    assert [r["date"] for r in data["type_summary"]] == ["2026-08-02"]
    meta = data["meta"]
    assert (meta["machine_shifts_run"], meta["machines_run"], meta["stopped_only_rows"]) == (2, 2, 0)
    # ...on the manning views too, where the same meta figures are shown.
    manning = report([], [], runs, view="detailed", d_from=d2, d_to=d2)["meta"]
    assert manning["machine_shifts_run"] == 2


def test_machine_runs_before_the_window_still_feed_the_mtd_variable_target():
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    variable = [norm(variable_fixed="V", machine_type_id=5, hands_per_machine=1)]
    (row,) = report(
        [actual(d1, 10, "A", 3.0), actual(d2, 10, "A", 2.0)],
        variable,
        looms(d1, "A", 3) + looms(d2, "A", 2),
        d_from=d2,
        d_to=d2,
    )["rows"]
    assert row["total_target"] == 2.0
    assert row["mtd_target"] == 5.0


def test_a_variable_type_that_ran_only_before_the_window_is_noted():
    # Type 5 produced on the 1st but not on the 2nd: the 2nd's target is 0 and
    # the note has to say so rather than be silenced by the earlier day.
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    data = report(
        [actual(d2, 10, "A", 4.0)],
        [norm(variable_fixed="V", machine_type_id=5, hands_per_machine=1)],
        looms(d1, "A", 3),
        d_from=d2,
        d_to=d2,
    )
    assert any("type(s) 5" in n for n in data["meta"]["notes"])


def test_detailed_rows_are_sorted_by_department_then_occupation():
    rows = report(
        [
            actual(D, 10, "A", 1.0, designation="AAA", dept_id=2, dept="WEAVING"),
            actual(D, 11, "A", 1.0, designation="BBB", dept_id=1, dept="SPINNING"),
            actual(D, 12, "A", 1.0, designation="CCC", dept_id=2, dept="WEAVING"),
            actual(D, 13, "A", 1.0, designation="ZZZ", dept_id=1, dept="SPINNING"),
        ],
        [],
    )["rows"]
    assert [r["designation"] for r in rows] == ["BBB", "ZZZ", "AAA", "CCC"]


def test_detailed_view_returns_department_subtotals_and_day_totals():
    d2 = date(2026, 8, 2)
    fixed = [
        norm(designation_id=10, shift_a=4, shift_b=0, shift_c=0, shift_g=0),
        norm(hands_norm_id=2, designation_id=11, shift_a=2, shift_b=0, shift_c=0, shift_g=0),
    ]
    data = report(
        [
            actual(D, 10, "A", 6.0, dept_id=1, dept="WEAVING"),  # 2 over
            actual(D, 11, "A", 1.0, dept_id=1, dept="WEAVING", designation="DESIG-11"),  # 1 short
            actual(D, 12, "A", 3.0, dept_id=2, dept="SPINNING", designation="NO-NORM"),
            actual(d2, 10, "A", 4.0, dept_id=1, dept="WEAVING"),
        ],
        fixed,
    )
    # rows stay one per designation per date; the totals ride beside them
    assert len(data["rows"]) == 4
    dept = {(t["date"], t["dept"]): t for t in data["dept_totals"]}
    assert list(dept) == [
        ("2026-08-01", "SPINNING"),
        ("2026-08-01", "WEAVING"),
        ("2026-08-02", "WEAVING"),
    ]
    weaving = dept[("2026-08-01", "WEAVING")]
    assert (weaving["total_actual"], weaving["total_target"]) == (7.0, 6.0)
    assert (weaving["excess"], weaving["short"]) == (2.0, 1.0)  # summed, never netted
    assert weaving["shift_a"] == 7.0 and weaving["target_a"] == 6.0
    # a department with no norm at all keeps a blank target, not a 0
    spinning = dept[("2026-08-01", "SPINNING")]
    assert spinning["total_actual"] == 3.0 and spinning["total_target"] is None
    day = {t["date"]: t for t in data["day_totals"]}
    assert list(day) == ["2026-08-01", "2026-08-02"]
    assert (day["2026-08-01"]["total_actual"], day["2026-08-01"]["total_target"]) == (10.0, 6.0)
    assert (day["2026-08-01"]["excess"], day["2026-08-01"]["short"]) == (2.0, 1.0)
    # running MTD up to each total's own date, per department / for the day
    assert dept[("2026-08-02", "WEAVING")]["mtd_actual"] == 11.0
    assert day["2026-08-02"]["mtd_actual"] == 14.0
    assert day["2026-08-02"]["mtd_target"] == 10.0
    assert (day["2026-08-02"]["mtd_excess"], day["2026-08-02"]["mtd_short"]) == (2.0, 1.0)


def test_detailed_totals_cover_the_window_but_count_earlier_days_in_the_mtd():
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    data = report(
        [actual(d1, 10, "A", 6.0), actual(d2, 10, "A", 2.0)],
        [norm(shift_a=4, shift_b=0, shift_c=0, shift_g=0)],
        d_from=d2,
        d_to=d2,
    )
    assert [t["date"] for t in data["day_totals"]] == ["2026-08-02"]
    assert [t["date"] for t in data["dept_totals"]] == ["2026-08-02"]
    (day,) = data["day_totals"]
    assert day["total_actual"] == 2.0 and day["mtd_actual"] == 8.0
    assert day["total_actual"] == data["meta"]["total_actual"]


def test_detailed_totals_follow_the_filters_and_only_ride_on_the_detailed_view():
    actuals = [
        actual(D, 10, "A", 5.0, dept_id=1, dept="WEAVING"),
        actual(D, 11, "A", 3.0, dept_id=2, dept="SPINNING", designation="DESIG-11"),
    ]
    data = report(actuals, [norm()], dept_id=2)
    assert [t["dept"] for t in data["dept_totals"]] == ["SPINNING"]
    assert data["day_totals"][0]["total_actual"] == 3.0
    for view in ("by_date", "by_occupation", "by_department", "machines_run"):
        other = report(actuals, [norm()], view=view)
        assert "dept_totals" not in other and "day_totals" not in other


# --- Hands Norms v2: machine groups, production norms, kinds N / A ----------


def member(group_id, machine_id, units=1) -> dict:
    """One row of get_hands_group_members_query()."""
    return {"machine_group_id": group_id, "machine_id": machine_id, "units": units}


def dept_prod(tran_date, shift, dept_id, kg) -> dict:
    """One row of get_hands_dept_production_query(): person-keyed winding kg
    per (date, shift, department of the winder); dept_id None = no winder
    department could be found (left out and noted)."""
    return {"tran_date": tran_date, "shift": shift, "dept_id": dept_id, "prod_kg": kg}


def group_norm(**over) -> dict:
    """WEAVER(H) on sls: kind M, group 'Hessian looms', 2 looms per hand."""
    base = {
        "norm_kind": "M",
        "variable_fixed": "V",
        "shift_a": 0,
        "shift_b": 0,
        "shift_c": 0,
        "shift_g": 0,
        "machine_type_id": None,
        "machine_group_id": 7,
        "machine_group": "Hessian looms",
        "scope_dept_id": None,
        "hands_per_machine": 0.5,
        "units_per_hand": 2,
        "qty_per_hand": None,
        "qty_uom": None,
        "norms_text": "2 MC = 1 HAND",
    }
    base.update(over)
    return norm(**base)


def prod_norm(**over) -> dict:
    """'315 KGS / WINDER' on sls: kind P, department scope, kg."""
    base = {
        "norm_kind": "P",
        "variable_fixed": "V",
        "shift_a": 0,
        "shift_b": 0,
        "shift_c": 0,
        "shift_g": 0,
        "machine_type_id": None,
        "machine_group_id": None,
        "machine_group": None,
        "scope_dept_id": 19,
        "hands_per_machine": None,
        "units_per_hand": None,
        "qty_per_hand": 315,
        "qty_uom": "kg",
        "norms_text": "315 KGS / WINDER",
    }
    base.update(over)
    return norm(**base)


def test_group_norm_sums_member_units_of_machines_that_ran_and_divides_by_units_per_hand():
    # 180 Hessian looms and 30 S4 looms (all machine type 7) ran in A; only the
    # 180 members count: 180 units / 2 per hand = 90. Shift B: 20 members → 10.
    hessian = [member(7, 1000 + i) for i in range(180)]
    ran = (
        [machine(D, "A", 1000 + i, "Weaving,Attendance", machine_type_id=7) for i in range(180)]
        + [machine(D, "A", 5000 + i, "Weaving,Attendance", machine_type_id=7) for i in range(30)]
        + [machine(D, "B", 1000 + i, "Weaving,Attendance", machine_type_id=7) for i in range(20)]
    )
    (row,) = report([actual(D, 10, "A", 88.0), actual(D, 10, "B", 10.0)], [group_norm()], ran,
                    members=hessian)["rows"]
    assert (row["target_a"], row["target_b"], row["target_c"], row["target_g"]) == (90.0, 10.0, 0.0, 0.0)
    assert row["total_target"] == 100.0 and row["total_actual"] == 98.0
    assert (row["excess"], row["short"]) == (0.0, 2.0)  # per designation-day, as every kind
    assert row["norm_kind"] == "M"
    assert row["machine_group_id"] == 7 and row["machine_group"] == "Hessian looms"
    assert row["units_per_hand"] == 2.0 and row["hands_per_machine"] == 0.5
    assert row["variable_fixed"] == "V"  # kept for the frontend


def test_group_member_seen_only_in_stoppage_adds_no_units():
    members = [member(7, 1), member(7, 2), member(7, 3)]
    ran = [machine(D, "A", 1, "Weaving,Attendance", machine_type_id=7),
           machine(D, "A", 2, "Stoppage", machine_type_id=7, stoppage_hours=8),
           machine(D, "A", 3, "Breakdown", machine_type_id=7, breakdown_min=120)]
    (row,) = report([actual(D, 10, "A", 1.0)], [group_norm(units_per_hand=1)], ran,
                    members=members)["rows"]
    assert row["target_a"] == 1.0


def test_group_with_mixed_machine_types_counts_every_member():
    # Carding F/S on sls: breaker + finisher cards of different types in one
    # group, 2 cards per hand; a 1st-drawing frame is 10 points, 4 points/hand.
    cards = [member(3, 1), member(3, 2), member(3, 3), member(3, 4)]
    frames = [member(4, 50, units=10), member(4, 51, units=10), member(4, 52, units=10)]
    ran = (
        [machine(D, "A", 1, "Attendance", machine_type_id=32),
         machine(D, "A", 2, "Attendance", machine_type_id=32),
         machine(D, "A", 3, "Attendance", machine_type_id=33),
         machine(D, "A", 4, "Stoppage", machine_type_id=33)]
        + [machine(D, "A", 50, "Drawing,Attendance", machine_type_id=12),
           machine(D, "A", 51, "Drawing,Attendance", machine_type_id=12)]
    )
    norms = [
        group_norm(designation_id=10, machine_group_id=3, machine_group="Carding F/S", units_per_hand=2),
        group_norm(hands_norm_id=2, designation_id=11, machine_group_id=4,
                   machine_group="1st Drawing F/S", units_per_hand=4),
    ]
    rows = report([actual(D, 10, "A", 2.0), actual(D, 11, "A", 5.0, designation="DESIG-11")],
                  norms, ran, members=cards + frames)["rows"]
    carding, drawing = sorted(rows, key=lambda r: r["designation_id"])
    assert carding["target_a"] == 1.5  # 3 cards of two types / 2
    assert drawing["target_a"] == 5.0  # 2 frames x 10 points / 4


def test_type_norm_without_group_still_uses_hands_per_machine():
    # A pre-v2 row (no units_per_hand, no norm_kind) linked to a type.
    (row,) = report(
        [actual(D, 10, "A", 18.0)],
        [norm(variable_fixed="V", machine_type_id=5, hands_per_machine=0.5)],
        looms(D, "A", 40),
    )["rows"]
    assert row["target_a"] == 20.0 and row["norm_kind"] == "M"
    assert row["machine_group_id"] is None and row["units_per_hand"] is None


def test_units_per_hand_beats_hands_per_machine_when_both_present():
    # The mirror is rounded (1/36 → 0.0278); the units column is exact.
    n = norm(norm_kind="M", machine_type_id=5, units_per_hand=36, hands_per_machine=0.0278)
    (row,) = report([actual(D, 10, "A", 1.0)], [n], looms(D, "A", 36))["rows"]
    assert row["target_a"] == 1.0  # 36 / 36, not 36 x 0.0278 = 1.0008
    # ...and directly, with a per-type count index.
    assert _targets(n, D, {(D, "A", 5): 72}, {}, {})["a"] == 2.0


def test_production_norm_divides_shift_kg_by_qty_per_hand_and_adds_shifts_for_the_day():
    # Beaming machines of type 20, 500 kg per hand: A produced 2,000 kg on two
    # machines, B 1,000 kg on one → 4 + 2 = 6 hands for the day.
    ran = [
        machine(D, "A", 1, "Beaming", machine_type_id=20, prod_kg=1200),
        machine(D, "A", 2, "Beaming", machine_type_id=20, prod_kg=800),
        machine(D, "B", 1, "Beaming", machine_type_id=20, prod_kg=1000),
        machine(D, "A", 9, "Beaming", machine_type_id=21, prod_kg=5000),  # other type
    ]
    n = prod_norm(scope_dept_id=None, machine_type_id=20, qty_per_hand=500)
    (row,) = report([actual(D, 10, "A", 5.0), actual(D, 10, "B", 2.0)], [n], ran)["rows"]
    assert (row["target_a"], row["target_b"], row["target_c"]) == (4.0, 2.0, 0.0)
    assert row["total_target"] == 6.0 and row["total_actual"] == 7.0
    assert (row["excess"], row["short"]) == (1.0, 0.0)
    assert row["norm_kind"] == "P" and row["qty_per_hand"] == 500.0 and row["qty_uom"] == "kg"


def test_production_norm_with_no_production_targets_zero_not_blank():
    # Complete P norm, machines ran but booked nothing (or never ran): a real 0.
    n = prod_norm(scope_dept_id=None, machine_type_id=20, qty_per_hand=500)
    (row,) = report([actual(D, 10, "A", 3.0)], [n],
                    [machine(D, "A", 1, "Attendance", machine_type_id=20)])["rows"]
    assert row["total_target"] == 0.0 and row["excess"] == 3.0 and row["short"] == 0.0
    (row,) = report([actual(D, 10, "A", 3.0)], [n], [])["rows"]
    assert row["total_target"] == 0.0
    assert any("type(s) 20" in n for n in report([actual(D, 10, "A", 3.0)], [n], [])["meta"]["notes"])


def test_production_norm_in_cuts_reads_prod_cuts():
    # Looms carry cuts, never kg: a kg norm on them is 0, a cuts norm works.
    ran = [machine(D, "A", 1, "Weaving", prod_cuts=30), machine(D, "A", 2, "Weaving", prod_cuts=10)]
    cuts = prod_norm(scope_dept_id=None, machine_type_id=5, qty_per_hand=8, qty_uom="cuts")
    (row,) = report([actual(D, 10, "A", 5.0)], [cuts], ran)["rows"]
    assert row["target_a"] == 5.0 and row["qty_uom"] == "cuts"
    kg = prod_norm(scope_dept_id=None, machine_type_id=5, qty_per_hand=8, qty_uom="kg")
    assert report([actual(D, 10, "A", 5.0)], [kg], ran)["rows"][0]["target_a"] == 0.0


def test_department_scoped_production_norm_adds_unattributed_winding_kg_by_winder_department():
    # Department 19 (winding): one winding machine carries 1,000 kg through
    # its winders' quality rows, another 260 kg of doffs belong to winders of
    # the department with no machine that shift. A machine of department 20
    # produced 5,000 kg and must not count. 315 kg per winder → 4 hands.
    ran = [
        machine(D, "A", 1, "Winding", machine_type_id=39, dept_id=19, dept="WINDING",
                prod_kg=1000, winding_kg=1000),
        machine(D, "A", 2, "Beaming", machine_type_id=20, dept_id=20, dept="BEAMING", prod_kg=5000),
    ]
    # The per-department query carries EVERY doff of the department's winders
    # (the 1,000 kg on machine 1 included) — the machine's share is left out
    # of the machine sum so it counts once.
    winding = [dept_prod(D, "A", 19, 1260), dept_prod(D, "A", 20, 40), dept_prod(D, "A", None, 15)]
    data = report([actual(D, 10, "A", 3.0, dept_id=19, dept="WINDING")], [prod_norm()], ran,
                  dept_production=winding)
    (row,) = data["rows"]
    assert row["target_a"] == 4.0  # (1000 - 1000 + 1260) / 315
    assert row["short"] == 1.0
    assert row["scope_dept_id"] == 19
    # 15 kg had neither a machine nor an attendance department: said, not lost silently.
    assert any(n.startswith("15.0 kg of winding doffs") for n in data["meta"]["notes"])
    # A branch whose winders have no machines at all (sls branch 87): the
    # department kg alone carries the target.
    (row,) = report([actual(D, 10, "A", 3.0)], [prod_norm()], [],
                    dept_production=[dept_prod(D, "A", 19, 630)])["rows"]
    assert row["target_a"] == 2.0
    # No unattributed kg → no note about it.
    assert not any("winding doffs" in n for n in
                   report([actual(D, 10, "A", 3.0)], [prod_norm()], [],
                          dept_production=[dept_prod(D, "A", 19, 630)])["meta"]["notes"])


def test_department_scoped_machine_norm_counts_every_machine_of_the_department():
    # Kind M on a department: every machine of that department that ran, units 1.
    ran = [
        machine(D, "A", 1, "Weaving,Attendance", dept_id=7, dept="HESSIAN"),
        machine(D, "A", 2, "Attendance", dept_id=7, dept="HESSIAN", machine_type_id=9),
        machine(D, "A", 3, "Stoppage", dept_id=7, dept="HESSIAN"),
        machine(D, "A", 4, "Weaving,Attendance", dept_id=8, dept="SACKING"),
    ]
    n = group_norm(machine_group_id=None, machine_group=None, scope_dept_id=7, units_per_hand=2)
    (row,) = report([actual(D, 10, "A", 1.0)], [n], ran)["rows"]
    assert row["target_a"] == 1.0  # machines 1 and 2 (any type), not 3 (stopped) or 4
    assert row["norm_kind"] == "M" and row["scope_dept_id"] == 7


def test_no_norm_kind_blanks_the_target_and_is_excluded_from_excess_short_and_named_in_notes():
    data = report(
        [actual(D, 10, "A", 6.0, designation="SWEEPER"), actual(D, 11, "A", 2.0, designation="DESIG-11")],
        [norm(norm_kind="N", norms_text="NO NORMS"),
         norm(hands_norm_id=2, designation_id=11, shift_a=1, shift_b=0, shift_c=0, shift_g=0)],
        view="detailed",
    )
    sweeper, other = sorted(data["rows"], key=lambda r: r["designation_id"])
    assert sweeper["norm_kind"] == "N"
    assert sweeper["total_actual"] == 6.0
    assert sweeper["total_target"] is None and sweeper["excess"] is None and sweeper["short"] is None
    assert all(sweeper[f"target_{s}"] is None for s in SHIFTS)
    assert sweeper["mtd_target"] is None
    # Totals: actuals in, targets untouched (exactly as a designation with no row).
    meta = data["meta"]
    assert meta["total_actual"] == 8.0 and meta["total_target"] == 1.0
    assert meta["total_excess"] == 1.0 and meta["total_short"] == 0.0
    notes = meta["notes"]
    assert any("no norm by design (kind N)" in n and "SWEEPER" in n for n in notes)
    # Not a configuration gap: none of the "cannot be computed" notes name it.
    assert not any(("not linked" in n or n.startswith("Variable norm")) and "SWEEPER" in n for n in notes)
    assert not any("no jute_prod_hands_norms_mst row" in n for n in notes)


def test_as_actual_kind_targets_the_hands_present_so_excess_and_short_are_zero():
    data = report(
        [actual(D, 10, "A", 6.0, designation="MISTRY"), actual(D, 10, "B", 2.5, designation="MISTRY")],
        [norm(norm_kind="A", norms_text="AS ACTUAL")],
        view="by_occupation",
    )
    (row,) = data["rows"]
    assert row["norm_kind"] == "A"
    assert (row["target_a"], row["target_b"], row["target_c"], row["target_g"]) == (6.0, 2.5, 0.0, 0.0)
    assert row["total_target"] == 8.5 and row["total_actual"] == 8.5
    assert (row["excess"], row["short"]) == (0.0, 0.0)
    assert data["meta"]["total_target"] == 8.5
    assert any("targeted as actual (kind A)" in n and "MISTRY" in n for n in data["meta"]["notes"])


def test_rows_without_norm_kind_resolve_from_the_legacy_flag():
    # Pre-migration rows / fixtures carry no norm_kind: V → M, a type link → M,
    # a per-machine rate with no shift counts → M, everything else → F.
    assert _kind(norm()) == "F"
    assert _kind(norm(variable_fixed="V")) == "M"
    assert _kind(norm(machine_type_id=5)) == "M"
    assert _kind(unlinked_machine_norm()) == "M"
    assert _kind(unlinked_machine_norm(shift_a=3)) == "F"  # explicit counts stay fixed
    assert _kind(norm(norm_kind="p")) == "P"  # the column wins, case-insensitively
    assert _kind(norm(norm_kind="", variable_fixed="V")) == "M"
    (row,) = report([actual(D, 10, "A", 1.0)], [norm(variable_fixed="V", machine_type_id=5,
                                                     hands_per_machine=1)], looms(D, "A", 3))["rows"]
    assert row["norm_kind"] == "M" and row["target_a"] == 3.0
    assert report([actual(D, 10, "A", 1.0)], [norm()])["rows"][0]["norm_kind"] == "F"
    assert report([actual(D, 10, "A", 1.0)], [])["rows"][0]["norm_kind"] is None


def test_incomplete_machine_norm_is_blank_and_noted_but_n_and_a_are_not():
    # The migration's REGEXP rows: kind M / P with no scope and no rate, and an
    # M with a rate but no scope. Blank + noted. N and A next to them are not.
    norms = [
        norm(hands_norm_id=1, designation_id=10, norm_kind="M", norms_text="4 POINTS"),
        norm(hands_norm_id=2, designation_id=11, norm_kind="P", qty_uom="kg", norms_text="315 KGS / WINDER"),
        norm(hands_norm_id=3, designation_id=12, norm_kind="M", units_per_hand=2, norms_text="2 MC = 1 HAND"),
        norm(hands_norm_id=4, designation_id=13, norm_kind="N"),
        norm(hands_norm_id=5, designation_id=14, norm_kind="A"),
    ]
    actuals = [actual(D, i, "A", 1.0, designation=f"D{i}") for i in (10, 11, 12, 13, 14)]
    data = report(actuals, norms, looms(D, "A", 5))
    by_id = {r["designation_id"]: r for r in data["rows"]}
    for i in (10, 11, 12, 13):
        assert by_id[i]["total_target"] is None
    assert by_id[14]["total_target"] == 1.0
    notes = data["meta"]["notes"]
    (linked,) = [n for n in notes if "not linked to a machine type" in n]
    assert "D12" in linked and "D10" not in linked and "D11" not in linked
    (generic,) = [n for n in notes if n.startswith("Variable norm")]
    assert "D10" in generic and "D11" in generic and "D12" not in generic
    for n in notes:
        if "not linked" in n or n.startswith("Variable norm"):
            assert "D13" not in n and "D14" not in n
    assert any("kind N" in n and "D13" in n for n in notes)
    assert any("kind A" in n and "D14" in n for n in notes)


def test_machines_run_rows_and_type_summary_carry_prod_kg_and_cuts():
    runs = [
        machine(D, "A", 1, "Weaving", prod_cuts=12),
        machine(D, "A", 2, "Weaving,Stoppage", prod_cuts=8.5),
        machine(D, "B", 1, "Weaving", prod_cuts=10),
        machine(D, "A", 50, "Doff", machine_type_id=9, machine_type_name="SPINNING", prod_kg=1234.567),
        machine(D, "A", 51, "Doff,Attendance", machine_type_id=9, machine_type_name="SPINNING", prod_kg=100),
        machine(D, "A", 60, "Stoppage", machine_type_id=9, machine_type_name="SPINNING", prod_kg=999),
        machine(D, "A", 70, "Attendance", machine_type_id=11, machine_type_name="DRAWING"),
    ]
    data = report([], [], runs, view="machines_run")
    by_id = {(r["machine_id"], r["shift"]): r for r in data["rows"]}
    assert by_id[(1, "A")]["prod_cuts"] == 12.0 and by_id[(1, "A")]["prod_kg"] is None
    assert by_id[(50, "A")]["prod_kg"] == 1234.57 and by_id[(50, "A")]["prod_cuts"] is None
    assert by_id[(70, "A")]["prod_kg"] is None and by_id[(70, "A")]["prod_cuts"] is None
    summary = {r["machine_type"]: r for r in data["type_summary"]}
    assert summary["TYPE-5"]["prod_cuts"] == 30.5 and summary["TYPE-5"]["prod_kg"] is None
    # Production adds up over EVERY listed row of the type, worked or not (kg
    # produced is kg produced — what a P norm sums); the machine counts do not:
    # only frame 51 has a worker marked, and no loom has.
    assert summary["SPINNING"]["prod_kg"] == 2333.57 and summary["SPINNING"]["machine_shifts"] == 1
    assert summary["SPINNING"]["no_worker_marked"] == 2
    assert summary["TYPE-5"]["machine_shifts"] == 0 and summary["TYPE-5"]["no_worker_marked"] == 3
    assert summary["DRAWING"]["prod_kg"] is None and summary["DRAWING"]["prod_cuts"] is None
    assert any("Prod kg / cuts" in n for n in data["meta"]["notes"])


def test_mtd_adds_production_targets_day_by_day():
    # Window = the 2nd; the 1st produced too (fetched from the 1st of the
    # month): the day's target is the 2nd's kg, the MTD adds the 1st's.
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    ran = [
        machine(d1, "A", 1, "Beaming", machine_type_id=20, prod_kg=1500),
        machine(d1, "B", 1, "Beaming", machine_type_id=20, prod_kg=500),
        machine(d2, "A", 1, "Beaming", machine_type_id=20, prod_kg=1000),
    ]
    n = prod_norm(scope_dept_id=None, machine_type_id=20, qty_per_hand=500)
    for view in ("detailed", "by_occupation"):
        (row,) = report([actual(d1, 10, "A", 3.0), actual(d1, 10, "B", 1.0), actual(d2, 10, "A", 3.0)],
                        [n], ran, view=view, d_from=d2, d_to=d2)["rows"]
        assert row["total_target"] == 2.0 and row["total_actual"] == 3.0
        assert row["mtd_target"] == 6.0 and row["mtd_actual"] == 7.0
        assert (row["mtd_excess"], row["mtd_short"]) == (1.0, 0.0)
    # ...and a department-scoped one adds the per-department winding kg day by day.
    winding = [dept_prod(d1, "A", 19, 630), dept_prod(d2, "A", 19, 315)]
    (row,) = report([actual(d1, 10, "A", 2.0), actual(d2, 10, "A", 1.0)], [prod_norm()], [],
                    view="by_occupation", d_from=d2, d_to=d2, dept_production=winding)["rows"]
    assert row["total_target"] == 1.0 and row["mtd_target"] == 3.0


# --- §11 machines worked from attendance (owner, 2026-10-01) ------------------

SYNCED = datetime(2026, 8, 2, 6, 15, 0)


def worked_row(tran_date, shift, machine_id, sources, workers=0, shift_hours=8, **over) -> dict:
    """One row of get_machine_shift_worked_rows_query(): a machine-shift as the
    rebuild stores it in jute_prod_machine_shift_worked — ``workers_marked``,
    ``shift_hours`` and the ``stopped_all_shift`` / ``worked`` flags computed
    the way the INSERT computes them (overridable)."""
    row = machine(tran_date, shift, machine_id, sources, **over)
    stoppage = max(
        float(row["stoppage_hours"] or 0),
        float(row["att_stoppage_hours"] or 0),
        float(row["breakdown_min"] or 0) / 60,
    )
    stopped = 1 if stoppage >= shift_hours else 0
    row.update(
        {
            "hands": workers or None,
            "workers_marked": workers,
            "shift_hours": shift_hours,
            "stopped_all_shift": stopped,
            "worked": 1 if workers > 0 and not stopped else 0,
            "synced_at": SYNCED,
        }
    )
    for k in ("workers_marked", "stopped_all_shift", "worked", "synced_at"):
        if k in over:
            row[k] = over[k]
    return row


def test_machine_counts_only_when_a_worker_is_marked():
    # Three looms of type 5 in shift A: one with a weaver marked, one with a
    # weaving entry only, one with waste booked only. One machine counts.
    runs = [
        worked_row(D, "A", 1, "Attendance,Weaving", workers=1, prod_cuts=10),
        worked_row(D, "A", 2, "Weaving", prod_cuts=12),
        worked_row(D, "A", 3, "Waste"),
    ]
    variable = [norm(norm_kind="M", machine_type_id=5, units_per_hand=1)]
    (row,) = report([actual(D, 10, "A", 1.0)], variable, runs)["rows"]
    assert row["target_a"] == 1.0  # not 3: production / waste alone do not count
    data = report([actual(D, 10, "A", 1.0)], variable, runs, view="machines_run")
    assert {r["machine_id"]: r["counted"] for r in data["rows"]} == {1: True, 2: False, 3: False}
    assert data["meta"]["machine_shifts_run"] == 1 and data["meta"]["machines_run"] == 1
    # The same holds for a group- and a department-scoped machine norm.
    grouped = group_norm(units_per_hand=1)
    (row,) = report([actual(D, 10, "A", 1.0)], [grouped], runs,
                    members=[member(7, 1), member(7, 2), member(7, 3)])["rows"]
    assert row["target_a"] == 1.0
    by_dept = group_norm(machine_group_id=None, machine_group=None, scope_dept_id=1, units_per_hand=1)
    assert report([actual(D, 10, "A", 1.0)], [by_dept], runs)["rows"][0]["target_a"] == 1.0
    # The explanatory note of every view says how a machine counts.
    for view in VIEWS:
        notes = report([actual(D, 10, "A", 1.0)], variable, runs, view=view)["meta"]["notes"]
        assert any("marked on it in attendance" in n and "stopped" in n for n in notes)


def test_production_without_a_worker_mark_is_listed_not_counted_and_noted():
    herackle = {"machine_type_id": 31, "machine_type_name": "Herackle"}
    hemming = {"machine_type_id": 30, "machine_type_name": "Hemming"}
    runs = [
        worked_row(D, "A", 1, "Finishing", prod_kg=400, **herackle),
        worked_row(D, "B", 1, "Finishing", prod_kg=300, **herackle),
        worked_row(D, "A", 2, "Finishing", prod_kg=250, **hemming),
        worked_row(D, "A", 3, "Attendance,Finishing", workers=2, prod_kg=100, **hemming),
        # not production: a stoppage entry and waste alone are not "production entered"
        worked_row(D, "A", 4, "Stoppage", stoppage_hours=2, **hemming),
        worked_row(D, "A", 5, "Waste", **hemming),
    ]
    data = report([], [], runs, view="machines_run")
    by_key = {(r["machine_id"], r["shift"]): r for r in data["rows"]}
    assert len(data["rows"]) == 6  # every machine-shift is still listed
    assert by_key[(1, "A")]["counted"] is False
    assert by_key[(1, "A")]["status"] == "NO_WORKER_MARKED" and by_key[(1, "A")]["prod_kg"] == 400.0
    assert by_key[(3, "A")]["counted"] is True and by_key[(3, "A")]["status"] == "WORKED"
    meta = data["meta"]
    assert (meta["machine_shifts_run"], meta["no_worker_marked"], meta["stopped_all_shift"]) == (1, 5, 0)
    (note,) = [n for n in meta["notes"] if "production entered but no worker marked" in n]
    # 3 machine-shifts with a PRODUCTION entry, by machine type, the worst first.
    assert note.startswith("3 machine-shift(s) have production entered but no worker marked")
    assert "not counted for machine targets: Herackle 2, Hemming 1" in note
    summary = {r["machine_type"]: r for r in data["type_summary"]}
    # A type with no machine worked still shows, with the number to fix.
    assert summary["Herackle"]["machine_shifts"] == 0 and summary["Herackle"]["no_worker_marked"] == 2
    assert summary["Hemming"]["machine_shifts"] == 1 and summary["Hemming"]["no_worker_marked"] == 3
    # The manning views carry the same note; a window with nothing to fix carries none.
    manning = report([], [], runs, view="detailed")["meta"]["notes"]
    assert any("production entered but no worker marked" in n for n in manning)
    clean = report([], [], [worked_row(D, "A", 3, "Attendance,Finishing", workers=2)],
                   view="machines_run")["meta"]["notes"]
    assert not any("no worker marked in attendance" in n for n in clean)


def test_marked_machine_stopped_all_shift_is_not_counted_and_noted():
    runs = [
        worked_row(D, "B", 1, "Attendance,Stoppage,Weaving", workers=1, stoppage_hours=8),
        worked_row(D, "B", 2, "Attendance,Stoppage,Weaving", workers=1, stoppage_hours=7.5),
        # C is a 7.5 h shift on sls branch 29: 7.5 h stopped IS the whole shift
        worked_row(D, "C", 3, "Attendance,Stoppage", workers=1, stoppage_hours=7.5, shift_hours=7.5),
        # the attendance mark's own stoppage and a breakdown count the same way
        worked_row(D, "B", 4, "Attendance", workers=1, att_stoppage_hours=8),
        worked_row(D, "B", 5, "Attendance,Breakdown", workers=2, breakdown_min=480),
        worked_row(D, "B", 6, "Attendance,Breakdown", workers=2, breakdown_min=120),
        # the books are compared, never added: 5 h + 5 h is not a stopped shift
        worked_row(D, "B", 7, "Attendance,Stoppage", workers=1, stoppage_hours=5, att_stoppage_hours=5),
    ]
    variable = [norm(norm_kind="M", machine_type_id=5, units_per_hand=1)]
    data = report([actual(D, 10, "B", 3.0)], variable, runs, view="machines_run")
    status = {r["machine_id"]: r["status"] for r in data["rows"]}
    assert status == {
        1: "STOPPED_ALL_SHIFT",
        2: "WORKED",
        3: "STOPPED_ALL_SHIFT",
        4: "STOPPED_ALL_SHIFT",
        5: "STOPPED_ALL_SHIFT",
        6: "WORKED",
        7: "WORKED",
    }
    assert all(r["counted"] is (r["status"] == "WORKED") for r in data["rows"])
    meta = data["meta"]
    assert (meta["machine_shifts_run"], meta["stopped_all_shift"], meta["no_worker_marked"]) == (3, 4, 0)
    assert meta["stopped_only_rows"] == 4
    assert any(
        n.startswith("4 machine-shift(s) had a worker marked but were stopped the whole shift")
        for n in meta["notes"]
    )
    (summary,) = [t for t in data["type_summary"] if t["machine_type_id"] == 5]
    assert (summary["run_b"], summary["run_c"], summary["stopped_all_shift"]) == (3, 0, 4)
    # ...and the machine target only multiplies the three that worked.
    (row,) = report([actual(D, 10, "B", 3.0)], variable, runs)["rows"]
    assert row["target_b"] == 3.0 and row["target_c"] == 0.0
    # A stopped machine nobody was marked on is "no worker marked", not "stopped".
    alone = report([], [], [worked_row(D, "B", 9, "Stoppage", stoppage_hours=8)], view="machines_run")
    assert alone["rows"][0]["status"] == "NO_WORKER_MARKED"
    assert alone["meta"]["stopped_all_shift"] == 0 and alone["meta"]["no_worker_marked"] == 1
    assert not any("stopped the whole shift" in n for n in alone["meta"]["notes"])


def test_production_norm_still_sums_kg_of_unmarked_machines():
    # Bale presses of type 20, 500 kg per hand. Press 1 has a worker marked,
    # press 2 produced with nobody marked, press 3 was marked but stopped all
    # shift (and still booked 200 kg): kg produced is kg produced.
    runs = [
        worked_row(D, "A", 1, "Attendance,Bale Press", workers=2, machine_type_id=20, prod_kg=1000),
        worked_row(D, "A", 2, "Bale Press", machine_type_id=20, prod_kg=800),
        worked_row(D, "A", 3, "Attendance,Bale Press,Stoppage", workers=1, machine_type_id=20,
                   prod_kg=200, stoppage_hours=8),
        worked_row(D, "A", 4, "Bale Press", machine_type_id=21, prod_kg=5000),  # other type
    ]
    kg_norm = prod_norm(scope_dept_id=None, machine_type_id=20, qty_per_hand=500)
    data = report([actual(D, 10, "A", 4.0)], [kg_norm], runs)
    (row,) = data["rows"]
    assert row["target_a"] == 4.0  # (1000 + 800 + 200) / 500
    # ...while a MACHINE norm on the same type counts press 1 only.
    per_machine = norm(norm_kind="M", machine_type_id=20, units_per_hand=1)
    assert report([actual(D, 10, "A", 4.0)], [per_machine], runs)["rows"][0]["target_a"] == 1.0
    # Type 20 produced, so the production norm is not reported as idle...
    assert not any("type(s) 20" in n for n in data["meta"]["notes"])
    # ...even when none of its machines worked; the machine norm then is.
    unmarked = [worked_row(D, "A", 2, "Bale Press", machine_type_id=20, prod_kg=800)]
    data = report([actual(D, 10, "A", 4.0)], [kg_norm], unmarked)
    assert data["rows"][0]["target_a"] == 1.6
    assert not any("type(s) 20" in n for n in data["meta"]["notes"])
    assert any("type(s) 20" in n
               for n in report([actual(D, 10, "A", 4.0)], [per_machine], unmarked)["meta"]["notes"])
    # Group and department scopes read the same production index; cuts too.
    grouped = prod_norm(scope_dept_id=None, machine_group_id=7, machine_group="Presses", qty_per_hand=500)
    (row,) = report([actual(D, 10, "A", 4.0)], [grouped], runs,
                    members=[member(7, 1), member(7, 2)])["rows"]
    assert row["target_a"] == 3.6  # (1000 + 800) / 500
    by_dept = prod_norm(scope_dept_id=1, qty_per_hand=1000)
    assert report([actual(D, 10, "A", 4.0)], [by_dept], runs)["rows"][0]["target_a"] == 7.0
    cuts = prod_norm(scope_dept_id=None, machine_type_id=5, qty_per_hand=8, qty_uom="cuts")
    looms_cut = [worked_row(D, "A", 1, "Weaving", prod_cuts=24), worked_row(D, "A", 2, "Weaving", prod_cuts=16)]
    assert report([actual(D, 10, "A", 5.0)], [cuts], looms_cut)["rows"][0]["target_a"] == 5.0


def test_machine_rows_carry_status_workers_and_groups():
    runs = [
        worked_row(D, "A", 1, "Attendance,Weaving", workers=2, prod_cuts=10),
        worked_row(D, "A", 2, "Weaving", prod_cuts=12),
        worked_row(D, "C", 3, "Attendance,Stoppage", workers=1, stoppage_hours=7.5, shift_hours=7.5),
    ]
    members = [
        {**member(7, 1), "group_name": "WVGHESS"},
        {**member(8, 1), "group_name": "All looms"},
        {**member(8, 2), "group_name": "All looms"},
        member(9, 2),  # a members row without a name (older caller) falls back to #id
    ]
    data = report([], [], runs, view="machines_run", members=members)
    by_id = {r["machine_id"]: r for r in data["rows"]}
    one, two, three = by_id[1], by_id[2], by_id[3]
    assert (one["status"], one["workers"], one["hands"], one["counted"]) == ("WORKED", 2, 2, True)
    assert (two["status"], two["workers"], two["hands"], two["counted"]) == ("NO_WORKER_MARKED", 0, None, False)
    assert (three["status"], three["workers"], three["counted"]) == ("STOPPED_ALL_SHIFT", 1, False)
    assert one["shift_hours"] == 8.0 and three["shift_hours"] == 7.5
    # groups the machine belongs to, by name
    assert one["group_ids"] == [8, 7] and one["groups"] == "All looms, WVGHESS"
    assert two["group_ids"] == [9, 8] and two["groups"] == "#9, All looms"
    assert three["group_ids"] == [] and three["groups"] == ""
    # the existing fields stay
    for f in ("date", "shift", "dept_id", "dept", "machine_type_id", "machine_type", "machine_id",
              "machine", "mech_code", "sources", "hands", "stoppage_hours", "breakdown_min",
              "prod_kg", "prod_cuts", "counted"):
        assert f in one
    meta = data["meta"]
    assert (meta["machine_shifts_run"], meta["stopped_all_shift"], meta["no_worker_marked"]) == (1, 1, 1)
    assert meta["synced_at"] == "2026-08-02T06:15:00"
    # the manning views carry the same meta figures
    manning = report([], [], runs, view="by_department", members=members)["meta"]
    assert (manning["stopped_all_shift"], manning["no_worker_marked"]) == (1, 1)
    assert manning["synced_at"] == "2026-08-02T06:15:00"
    # nothing rebuilt / no rows -> no stamp
    assert report([], [], [], view="machines_run")["meta"]["synced_at"] is None
    # a group name known only from a norm scoped to it is used too
    named_by_norm = report([actual(D, 10, "A", 1.0)], [group_norm(machine_group_id=9, machine_group="Rapier")],
                           runs, view="machines_run", members=[member(9, 2)])
    assert {r["machine_id"]: r["groups"] for r in named_by_norm["rows"]}[2] == "Rapier"


def test_scopes_list_the_machine_and_production_norms():
    norms = [
        group_norm(hands_norm_id=1, designation_id=10, designation="WEAVER(H)",
                   unit_label="loom"),  # group 7, 2 looms per hand
        norm(hands_norm_id=2, designation_id=11, designation="HEMMING OPERATOR", norm_kind="M",
             machine_type_id=30, machine_type="Hemming", units_per_hand=1.5),
        prod_norm(hands_norm_id=3, designation_id=12, designation="WINDER", scope_dept="WINDING"),
        # legacy V row: type link, hands per machine only -> units per hand derived
        norm(hands_norm_id=4, designation_id=13, variable_fixed="V", machine_type_id=5,
             hands_per_machine=0.5),
        norm(hands_norm_id=5, designation_id=14, designation="SWEEPER"),  # F: no scope
        norm(hands_norm_id=6, designation_id=15, norm_kind="N"),
        norm(hands_norm_id=7, designation_id=16, norm_kind="M", units_per_hand=2),  # incomplete
        # not in force until after the window
        group_norm(hands_norm_id=8, designation_id=17, effective_date=date(2026, 9, 1)),
    ]
    runs = [worked_row(D, "A", 1, "Attendance", workers=1)]  # type 5 is "TYPE-5" on the rows
    data = report([actual(D, 13, "A", 1.0, designation="HELPER")], norms, runs, view="machines_run")
    assert data["scopes"] == [
        # names missing on the norm row come from the attendance / machine rows
        {"designation_id": 13, "designation": "HELPER", "norm_kind": "M", "scope": "type",
         "scope_id": 5, "scope_name": "TYPE-5", "units_per_hand": 2.0, "qty_per_hand": None,
         "qty_uom": None, "unit_label": None},
        {"designation_id": 11, "designation": "HEMMING OPERATOR", "norm_kind": "M", "scope": "type",
         "scope_id": 30, "scope_name": "Hemming", "units_per_hand": 1.5, "qty_per_hand": None,
         "qty_uom": None, "unit_label": None},
        {"designation_id": 10, "designation": "WEAVER(H)", "norm_kind": "M", "scope": "group",
         "scope_id": 7, "scope_name": "Hessian looms", "units_per_hand": 2.0, "qty_per_hand": None,
         "qty_uom": None, "unit_label": "loom"},
        {"designation_id": 12, "designation": "WINDER", "norm_kind": "P", "scope": "dept",
         "scope_id": 19, "scope_name": "WINDING", "units_per_hand": None, "qty_per_hand": 315.0,
         "qty_uom": "kg", "unit_label": None},
    ]
    # A production norm in cuts says so; a blank unit reads as kg (as the target does).
    in_cuts = [prod_norm(scope_dept_id=None, machine_type_id=5, qty_per_hand=8, qty_uom="cuts"),
               prod_norm(hands_norm_id=2, designation_id=11, qty_uom=None)]
    assert [x["qty_uom"] for x in report([], in_cuts, [], view="machines_run")["scopes"]] == ["cuts", "kg"]
    # A group's name / unit label missing on the norm row come from the members rows.
    bare = [group_norm(machine_group=None)]
    (scope,) = report([], bare, [], view="machines_run",
                      members=[{**member(7, 1), "group_name": "WVGHESS", "unit_label": "point"}])["scopes"]
    assert (scope["scope_name"], scope["unit_label"]) == ("WVGHESS", "point")
    (scope,) = report([], bare, [], view="machines_run")["scopes"]
    assert (scope["scope_name"], scope["unit_label"]) == ("#7", None)
    # Not attendance-driven: the scopes are there with no attendance at all...
    assert len(report([], norms, [], view="machines_run")["scopes"]) == 4
    # ...a revision coming into force inside the window is listed with the old one's designation once...
    revised = norms[:1] + [group_norm(hands_norm_id=9, designation_id=10, designation="WEAVER(H)",
                                      effective_date=date(2026, 8, 2), units_per_hand=3)]
    (scope,) = report([], revised, [], view="machines_run")["scopes"]
    assert scope["units_per_hand"] == 3.0
    # ...and only the machines view carries them.
    for view in ("detailed", "by_date", "by_occupation", "by_department"):
        assert "scopes" not in report([], norms, runs, view=view)


def test_rows_without_worked_flag_derive_it_from_sources():
    # Rows in the pre-table shape (no workers_marked / worked / stopped flags):
    # worked = 'Attendance' among the sources and not stopped all shift (8 h).
    runs = [
        machine(D, "A", 1, "Attendance,Weaving", hands=2),
        machine(D, "A", 2, "Weaving"),
        machine(D, "A", 3, "Attendance,Stoppage", hands=1, stoppage_hours=8),
        machine(D, "A", 4, "Attendance", hands=1, att_stoppage_hours=3, stoppage_hours=7.9),
        machine(D, "A", 5, "Attendance,Breakdown", hands=1, breakdown_min=480),
        machine(D, "A", 6, "Stoppage", stoppage_hours=8),
        machine(D, "A", 7, "Attendance"),  # a mark with no worker count still is a mark
    ]
    data = report([], [], runs, view="machines_run")
    got = {r["machine_id"]: (r["status"], r["counted"], r["workers"]) for r in data["rows"]}
    assert got == {
        1: ("WORKED", True, 2),
        2: ("NO_WORKER_MARKED", False, 0),
        3: ("STOPPED_ALL_SHIFT", False, 1),
        4: ("WORKED", True, 1),
        5: ("STOPPED_ALL_SHIFT", False, 1),
        6: ("NO_WORKER_MARKED", False, 0),
        7: ("WORKED", True, 0),
    }
    assert all(r["shift_hours"] is None for r in data["rows"])
    # The flag wins over the sources when the row carries one.
    flagged = [machine(D, "A", 1, "Attendance,Weaving", hands=2, worked=0, stopped_all_shift=1,
                       workers_marked=2),
               machine(D, "A", 2, "Weaving", worked=1, workers_marked=1, stopped_all_shift=0)]
    got = {r["machine_id"]: (r["status"], r["counted"])
           for r in report([], [], flagged, view="machines_run")["rows"]}
    assert got == {1: ("STOPPED_ALL_SHIFT", False), 2: ("WORKED", True)}


def test_stopped_and_no_worker_counts_follow_the_rows_the_machines_view_returns():
    weaving = {"dept_id": 1, "dept": "WEAVING"}
    finishing = {"dept_id": 2, "dept": "FINISHING", "machine_type_id": 31, "machine_type_name": "Herackle"}
    runs = [
        worked_row(D, "A", 1, "Attendance,Weaving", workers=1, **weaving),
        worked_row(D, "A", 2, "Attendance,Stoppage", workers=1, stoppage_hours=8, **weaving),
        worked_row(D, "A", 3, "Weaving", **weaving),
        worked_row(D, "A", 4, "Finishing", prod_kg=100, **finishing),
        worked_row(D, "B", 4, "Finishing", prod_kg=100, **finishing),
        worked_row(D, "A", 5, "Attendance,Finishing", workers=1, **finishing),
    ]
    # No department filter: the whole window.
    meta = report([], [], runs, view="machines_run")["meta"]
    assert (meta["machine_shifts_run"], meta["machines_run"]) == (2, 2)
    assert (meta["stopped_all_shift"], meta["no_worker_marked"]) == (1, 3)
    # With one: the two counts and their notes are those of the rows returned...
    data = report([], [], runs, view="machines_run", dept_id=2)
    assert [r["machine_id"] for r in data["rows"]] == [4, 5, 4]
    meta = data["meta"]
    assert (meta["stopped_all_shift"], meta["no_worker_marked"]) == (0, 2)
    assert [r["status"] for r in data["rows"]].count("NO_WORKER_MARKED") == meta["no_worker_marked"]
    assert any(n.startswith("2 machine-shift(s) have production entered but no worker marked")
               and n.endswith("Herackle 2") for n in meta["notes"])
    assert not any("stopped the whole shift" in n for n in meta["notes"])
    # ...while the worked figures stay window-wide, as they always were.
    assert (meta["machine_shifts_run"], meta["machines_run"], meta["stopped_only_rows"]) == (2, 2, 4)
    # A manning view lists no machine rows: its counts are the whole window's.
    manning = report([], [], runs, view="detailed", dept_id=2)["meta"]
    assert (manning["stopped_all_shift"], manning["no_worker_marked"]) == (1, 3)


# --- §11.2 rebuild of jute_prod_machine_shift_worked ---------------------------


class Row:
    """A SQLAlchemy Row stand-in: the handler only reads ``_mapping``."""

    def __init__(self, **cols):
        self._mapping = cols


class Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class RecordingDb:
    """Session stand-in that records every statement / commit / rollback in
    order. ``insert_errors`` are raised, one per INSERT into the table, before
    it "runs" (``update_errors`` likewise for the UPDATE of the table — the
    EXCLUDE overrides); ``machine_rows`` is what the read of the table
    returns."""

    def __init__(self, insert_errors=(), machine_rows=(), update_errors=()):
        self.log = []
        self.insert_errors = list(insert_errors)
        self.update_errors = list(update_errors)
        self.machine_rows = list(machine_rows)

    def execute(self, stmt, binds=None):
        sql = " ".join(str(stmt).split())
        self.log.append(("execute", sql, binds))
        if sql.startswith("INSERT INTO jute_prod_machine_shift_worked") and self.insert_errors:
            raise self.insert_errors.pop(0)
        if sql.startswith("UPDATE jute_prod_machine_shift_worked") and self.update_errors:
            raise self.update_errors.pop(0)
        if "FROM branch_mst WHERE branch_id" in sql:
            return Result([Row(ok=1)])
        if "FROM jute_prod_machine_shift_worked w" in sql:
            return Result([Row(**r) for r in self.machine_rows])
        return Result()

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))

    def statements(self) -> list:
        """The log reduced to its verbs, e.g. ['SET', 'DELETE', 'INSERT', 'commit']."""
        out = []
        for entry in self.log:
            out.append(entry[1].split()[0] if entry[0] == "execute" else entry[0])
        return out


def mysql_error(code, message="x") -> OperationalError:
    """What SQLAlchemy raises for a PyMySQL OperationalError."""
    return OperationalError("INSERT ...", {}, pymysql.err.OperationalError(code, message))


WINDOW = (date(2026, 9, 1), date(2026, 9, 30))

# One rebuild transaction (after the rollback that ends the read transaction):
# the window is deleted, re-inserted from the sources, then the manual
# overrides in force are applied — ADD rows upserted, EXCLUDE rows updated
# (spec §12.2) — and committed.
REBUILD = ["SET", "DELETE", "INSERT", "INSERT", "UPDATE", "commit"]


def test_rebuild_deletes_then_inserts_the_window_in_one_transaction():
    db = RecordingDb()
    assert rebuild_machine_shift_worked(db, 2, 29, *WINDOW) is True
    # the read transaction is ended, then ONE transaction: delete, insert, the
    # manual overrides (ADD upsert, EXCLUDE update), commit
    assert db.statements() == ["rollback"] + REBUILD
    executed = [e for e in db.log if e[0] == "execute"]
    assert executed[0][1] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    binds = {"co_id": 2, "branch_id": 29, "date_from": WINDOW[0], "date_to": WINDOW[1]}
    delete, insert = executed[1], executed[2]
    assert delete[2] == binds and insert[2] == binds
    assert delete[1].startswith("DELETE FROM jute_prod_machine_shift_worked WHERE co_id = :co_id")
    assert "branch_id = :branch_id" in delete[1]
    assert "tran_date BETWEEN :date_from AND :date_to" in delete[1]
    assert insert[1].startswith("INSERT INTO jute_prod_machine_shift_worked")


def test_rebuild_insert_is_built_from_the_run_legs_and_upserts():
    insert = " ".join(str(get_machine_shift_worked_insert_query()).split())
    source = " ".join(str(get_machine_shift_worked_source_query()).split())
    assert source in insert  # the INSERT writes exactly what the read-only SELECT returns
    # every leg of the all-sources runs query feeds the table
    for table in ("jute_prod_weaving_daily", "daily_doff_tbl", "jute_prod_spinning_daily",
                  "jute_prod_beaming_daily", "jute_prod_finishing_daily", "spreader_prod_entry",
                  "jute_prod_drawing_entry", "jute_prod_winding_daily_qlty", "jute_prod_bale_entry",
                  "jute_prod_waste_entry", "daily_ebmc_attendance", "jute_prod_stoppage_hours",
                  "jute_mech_breakdown"):
        assert table in insert
    columns = insert[insert.index("(") + 1: insert.index(")")].replace(" ", "").split(",")
    assert columns == [
        "co_id", "branch_id", "tran_date", "shift", "machine_id", "workers_marked",
        "att_stoppage_hours", "stoppage_hours", "breakdown_min", "shift_hours", "prod_kg",
        "prod_cuts", "winding_kg", "sources", "stopped_all_shift", "worked", "synced_at",
    ]
    # ...which are the columns of the migration's table, in its order (minus the PK)
    with open("dbqueries/migrations/20261001_machine_shift_worked_table.sql", encoding="utf-8") as f:
        ddl = f.read()
    ddl = ddl[ddl.index("CREATE TABLE IF NOT EXISTS jute_prod_machine_shift_worked"):]
    ddl_columns = [line.split()[0] for line in ddl.splitlines()[1:]
                   if line.startswith("  ") and line.split()[0].islower()]
    assert ddl_columns == ["machine_shift_worked_id"] + columns
    # the INSERT's SELECT is wrapped (src) so ON DUPLICATE cannot be read as a JOIN ... ON
    assert ") src ON DUPLICATE KEY UPDATE" in insert
    assert [c.strip() for c in insert[insert.index("SELECT src."): insert.index(" FROM (")]
            .replace("SELECT ", "").split(",")] == [f"src.{c}" for c in columns]
    # the source SELECT returns the same columns in the same order
    select_list = source[: source.index(" FROM ( SELECT k.tran_date")]
    positions = [select_list.index(f" AS {c}") if f" AS {c}" in select_list
                 else select_list.index(f"x.{c}") for c in columns]
    assert positions == sorted(positions)
    # shift hours: the branch's full-shift spell of the letter, fallback 8
    assert "UPPER(TRIM(sp.spell_code)) = 'A'" in insert and "sm.branch_id = :branch_id" in insert
    assert "COALESCE(sh.hrs_c, 8)" in insert
    # stopped all shift = the higher of the three books against the shift hours
    assert "GREATEST(x.att_stoppage_hours, x.stoppage_hours, x.breakdown_min / 60) >=" in insert
    assert "x.workers_marked > 0 AND" in insert and "NOW() AS synced_at" in insert
    # two simultaneous opens must not fail on uq_msw
    upsert = insert[insert.index("ON DUPLICATE KEY UPDATE"):]
    for c in columns[5:]:
        assert f"{c} = VALUES({c})" in upsert
    # binds: the window and the scope only
    assert set(get_machine_shift_worked_insert_query().compile().params) == {
        "co_id", "branch_id", "date_from", "date_to"}
    assert set(get_machine_shift_worked_delete_query().compile().params) == {
        "co_id", "branch_id", "date_from", "date_to"}


def test_read_query_returns_the_builder_columns_plus_the_worked_flags():
    sql = " ".join(str(get_machine_shift_worked_rows_query()).split())
    assert "FROM jute_prod_machine_shift_worked w INNER JOIN machine_mst m" in sql
    assert "LEFT JOIN machine_type_mst mt" in sql and "LEFT JOIN dept_mst dp" in sql
    select_list = sql[: sql.index(" FROM ")]
    for col in ("w.tran_date", "w.shift", "w.machine_id", "m.machine_name", "m.mech_code",
                "m.machine_type_id", "mt.machine_type_name", "m.dept_id", "dp.dept_desc AS dept",
                "w.sources", "AS hands", "w.att_stoppage_hours", "w.stoppage_hours",
                "w.breakdown_min", "w.prod_kg", "w.prod_cuts", "w.winding_kg",
                "w.workers_marked", "w.shift_hours", "w.stopped_all_shift", "w.worked",
                "w.synced_at"):
        assert col in select_list
    assert "w.co_id = :co_id" in sql and "w.branch_id = :branch_id" in sql
    assert "w.tran_date BETWEEN :date_from AND :date_to" in sql


def test_rebuild_retries_once_on_deadlock():
    # Deadlock against a simultaneous open: rolled back and tried again.
    db = RecordingDb(insert_errors=[mysql_error(1213, "Deadlock found")])
    assert rebuild_machine_shift_worked(db, 2, 29, *WINDOW) is True
    assert db.statements() == (
        ["rollback", "SET", "DELETE", "INSERT", "rollback"] + ["rollback"] + REBUILD
    )
    # A lock-wait timeout is retried the same way (one failed source INSERT,
    # then the source INSERT and the ADD upsert of the second attempt).
    db = RecordingDb(insert_errors=[mysql_error(1205, "Lock wait timeout exceeded")])
    assert rebuild_machine_shift_worked(db, 2, 29, *WINDOW) is True
    assert db.statements().count("INSERT") == 3 and db.statements()[-1] == "commit"
    # Twice in a row: give way, nothing committed, no error — the request then
    # reads what the other open wrote.
    db = RecordingDb(insert_errors=[mysql_error(1213), mysql_error(1205)])
    assert rebuild_machine_shift_worked(db, 2, 29, *WINDOW) is False
    assert db.statements().count("INSERT") == 2 and "commit" not in db.statements()
    assert db.statements()[-1] == "rollback"
    # Anything else (here: the table is missing) is rolled back and raised, not retried.
    db = RecordingDb(insert_errors=[mysql_error(1146, "Table doesn't exist")])
    with pytest.raises(OperationalError):
        rebuild_machine_shift_worked(db, 2, 29, *WINDOW)
    assert db.statements().count("INSERT") == 1 and db.statements()[-1] == "rollback"


def test_endpoint_rebuilds_before_reading():
    table_rows = [worked_row(date(2026, 8, 12), "A", 1, "Attendance,Weaving", workers=2),
                  worked_row(date(2026, 8, 12), "A", 2, "Weaving")]
    for view in VIEWS:  # every view, not only the machines tab
        db = RecordingDb(machine_rows=table_rows)
        out = hands_report(
            Req(**params(date_from="2026-08-10", date_to="2026-08-14", view=view)),
            db=db, token_data={},
        )
        verbs = db.statements()
        # company / branch check, then the rebuild (overrides included), then the reads
        assert verbs[:8] == ["SELECT", "rollback"] + REBUILD
        assert set(verbs[8:]) == {"SELECT"}
        executed = [e for e in db.log if e[0] == "execute"]
        (read,) = [e for e in executed if "FROM jute_prod_machine_shift_worked w" in e[1]]
        assert db.log.index(read) > db.log.index(("commit",))
        # the window rebuilt and read is the 1st of the From Date's month .. To Date
        window = {"co_id": 2, "branch_id": 87, "date_from": date(2026, 8, 1), "date_to": date(2026, 8, 14)}
        (delete,) = [e for e in executed if e[1].startswith("DELETE")]
        insert, add_upsert = [e for e in executed if e[1].startswith("INSERT")]
        (exclude_update,) = [e for e in executed if e[1].startswith("UPDATE")]
        assert delete[2] == window and insert[2] == window and read[2] == window
        assert add_upsert[2] == window and exclude_update[2] == window
        meta = out["data"]["meta"]
        assert (meta["machine_shifts_run"], meta["no_worker_marked"]) == (1, 1)
        assert meta["synced_at"] == "2026-08-02T06:15:00"
    assert [r["status"] for r in out["data"]["rows"]] == ["WORKED", "NO_WORKER_MARKED"]
    assert out["data"]["scopes"] == [] and "type_summary" in out["data"]
    # When the rebuild gives way to a concurrent one the request still reads the table.
    db = RecordingDb(insert_errors=[mysql_error(1213), mysql_error(1213)], machine_rows=table_rows)
    out = hands_report(Req(**params(view="machines_run")), db=db, token_data={})
    assert "commit" not in db.statements() and len(out["data"]["rows"]) == 2
    # A branch of another company is refused before anything is rebuilt.
    class NoPair(RecordingDb):
        def execute(self, stmt, binds=None):
            super().execute(stmt, binds)
            return Result()
    db = NoPair()
    with pytest.raises(HTTPException) as e:
        hands_report(Req(**params()), db=db, token_data={})
    assert e.value.status_code == 400 and db.statements() == ["SELECT"]


# --- §12 manual additions / exclusions of worked machines (owner, 2026-10-01) ---

OVERRIDE_AT = datetime(2026, 8, 2, 9, 30, 0)


def overridden(row: dict, action: str, override_id=501, reason="Weaver not marked",
               by="Supervisor One", **over) -> dict:
    """``row`` (a ``worked_row``) as the rebuild leaves it once an override is
    in force: ``manual_action`` stamped and ``worked`` forced (ADD -> 1,
    EXCLUDE -> 0), plus what the read joins from the override table."""
    row = dict(row)
    row.update(
        {
            "manual_action": action,
            "worked": 1 if action == "ADD" else 0,
            "override_id": override_id,
            "manual_reason": reason,
            "manual_by": by,
            "manual_at": OVERRIDE_AT,
        }
    )
    row.update(over)
    return row


def added_without_entry(tran_date, shift, machine_id, **over) -> dict:
    """The row the rebuild writes for an ADD of a machine-shift NO entry names:
    no sources, no workers, no stoppage, no production."""
    return overridden(worked_row(tran_date, shift, machine_id, None, **over), "ADD")


def test_added_machine_counts_even_without_any_entry():
    # Loom 1 has a weaver marked. Loom 2 is in no entry at all — attendance
    # missed it — and the supervisor added it.
    runs = [
        worked_row(D, "A", 1, "Attendance,Weaving", workers=1, prod_cuts=10),
        added_without_entry(D, "A", 2),
    ]
    variable = [norm(norm_kind="M", machine_type_id=5, units_per_hand=1)]
    (row,) = report([actual(D, 10, "A", 2.0)], variable, runs)["rows"]
    assert row["target_a"] == 2.0  # both looms count
    # ...for a group- and a department-scoped machine norm too
    grouped = group_norm(units_per_hand=1)
    (row,) = report([actual(D, 10, "A", 2.0)], [grouped], runs,
                    members=[member(7, 1), member(7, 2)])["rows"]
    assert row["target_a"] == 2.0
    by_dept = group_norm(machine_group_id=None, machine_group=None, scope_dept_id=1, units_per_hand=1)
    assert report([actual(D, 10, "A", 2.0)], [by_dept], runs)["rows"][0]["target_a"] == 2.0

    data = report([actual(D, 10, "A", 2.0)], variable, runs, view="machines_run")
    added = {r["machine_id"]: r for r in data["rows"]}[2]
    assert (added["status"], added["counted"], added["workers"], added["hands"]) == (
        "ADDED_MANUALLY", True, 0, None)
    assert added["sources"] == "" and added["prod_kg"] is None and added["prod_cuts"] is None
    assert (added["manual_action"], added["override_id"], added["manual_reason"],
            added["manual_by"], added["manual_at"]) == (
        "ADD", 501, "Weaver not marked", "Supervisor One", "2026-08-02T09:30:00")
    assert added["suggested"] is False
    meta = data["meta"]
    # worked machine-shifts / distinct worked machines include the added one
    assert (meta["machine_shifts_run"], meta["machines_run"], meta["stopped_only_rows"]) == (2, 2, 0)
    assert (meta["added_manually"], meta["excluded_manually"]) == (1, 0)
    assert (meta["stopped_all_shift"], meta["no_worker_marked"]) == (0, 0)
    (summary,) = data["type_summary"]
    assert (summary["run_a"], summary["machine_shifts"], summary["machines"]) == (2, 2, 2)
    assert (summary["added_manually"], summary["excluded_manually"]) == (1, 0)
    note = "1 machine-shift(s) were added manually and 0 excluded manually — see the Machines Worked tab."
    for view in VIEWS:  # the manning views carry the note too
        assert note in report([actual(D, 10, "A", 2.0)], variable, runs, view=view)["meta"]["notes"]
    # No override in the window: no such note, zero counts.
    clean = report([], [], runs[:1], view="machines_run")["meta"]
    assert (clean["added_manually"], clean["excluded_manually"]) == (0, 0)
    assert not any("added manually and" in n for n in clean["notes"])
    # A row without the table's flags (older fixture shape) is read by the same rule.
    legacy = machine(D, "A", 3, "", manual_action="ADD")
    assert report([], [], [legacy], view="machines_run")["rows"][0]["counted"] is True


def test_excluded_machine_does_not_count_though_a_worker_is_marked():
    # Two looms with a weaver marked; loom 2's mark is wrong and was excluded.
    runs = [
        worked_row(D, "A", 1, "Attendance,Weaving", workers=1, prod_cuts=10),
        overridden(worked_row(D, "A", 2, "Attendance,Weaving", workers=2, prod_cuts=7),
                   "EXCLUDE", override_id=77, reason="Marked on the wrong loom"),
    ]
    variable = [norm(norm_kind="M", machine_type_id=5, units_per_hand=1)]
    (row,) = report([actual(D, 10, "A", 2.0)], variable, runs)["rows"]
    assert row["target_a"] == 1.0  # not 2
    data = report([actual(D, 10, "A", 2.0)], variable, runs, view="machines_run")
    excluded = {r["machine_id"]: r for r in data["rows"]}[2]
    assert (excluded["status"], excluded["counted"], excluded["workers"]) == (
        "EXCLUDED_MANUALLY", False, 2)
    assert (excluded["manual_action"], excluded["override_id"], excluded["manual_reason"]) == (
        "EXCLUDE", 77, "Marked on the wrong loom")
    assert excluded["suggested"] is False
    meta = data["meta"]
    assert (meta["machine_shifts_run"], meta["machines_run"], meta["stopped_only_rows"]) == (1, 1, 1)
    assert (meta["added_manually"], meta["excluded_manually"]) == (0, 1)
    # an excluded row is neither "no worker marked" nor "stopped all shift"
    assert (meta["stopped_all_shift"], meta["no_worker_marked"]) == (0, 0)
    (summary,) = data["type_summary"]
    assert (summary["machine_shifts"], summary["excluded_manually"], summary["no_worker_marked"]) == (1, 1, 0)
    assert ("0 machine-shift(s) were added manually and 1 excluded manually — see the "
            "Machines Worked tab.") in meta["notes"]
    # The override has the last word even if the row's own flag were stale.
    stale = [runs[0], {**runs[1], "worked": 1}]
    assert report([actual(D, 10, "A", 2.0)], variable, stale)["rows"][0]["target_a"] == 1.0
    # Production is untouched: the excluded loom's cuts still feed a production norm.
    by_cuts = [prod_norm(machine_type_id=5, scope_dept_id=None, qty_per_hand=17, qty_uom="cuts")]
    (row,) = report([actual(D, 10, "A", 2.0)], by_cuts, runs)["rows"]
    assert row["target_a"] == 1.0  # (10 + 7) cuts / 17


def test_add_beats_stopped_all_shift():
    # A weaver is marked and the stoppage book says 8 h of an 8 h shift: not
    # counted on the sources alone...
    stopped = worked_row(D, "A", 1, "Attendance,Stoppage", workers=1, stoppage_hours=8)
    variable = [norm(norm_kind="M", machine_type_id=5, units_per_hand=1)]
    assert report([actual(D, 10, "A", 1.0)], variable, [stopped])["rows"][0]["target_a"] == 0.0
    # ...but the supervisor said it worked.
    runs = [overridden(stopped, "ADD", reason="Ran after the stoppage was booked")]
    assert runs[0]["stopped_all_shift"] == 1  # the books still say so
    assert report([actual(D, 10, "A", 1.0)], variable, runs)["rows"][0]["target_a"] == 1.0
    data = report([actual(D, 10, "A", 1.0)], variable, runs, view="machines_run")
    (row,) = data["rows"]
    assert (row["status"], row["counted"], row["stoppage_hours"]) == ("ADDED_MANUALLY", True, 8.0)
    meta = data["meta"]
    assert (meta["machine_shifts_run"], meta["added_manually"], meta["stopped_all_shift"]) == (1, 1, 0)
    assert not any("stopped the whole shift" in n for n in meta["notes"])
    # The same through a breakdown that covers the shift, with nobody marked.
    broken = overridden(worked_row(D, "B", 2, "Breakdown", breakdown_min=480), "ADD")
    (row,) = report([], [], [broken], view="machines_run")["rows"]
    assert (row["status"], row["counted"]) == ("ADDED_MANUALLY", True)


def test_status_precedence_with_overrides():
    marked = dict(workers=1)
    marked_stopped = dict(workers=1, stoppage_hours=8)
    cases = [
        # (sources, row extras, override) -> (status, counted)
        ("Attendance,Weaving", marked, None, "WORKED", True),
        ("Attendance,Stoppage", marked_stopped, None, "STOPPED_ALL_SHIFT", False),
        ("Weaving", {}, None, "NO_WORKER_MARKED", False),
        ("Stoppage", dict(stoppage_hours=8), None, "NO_WORKER_MARKED", False),
        # ADD outranks everything the sources say
        ("Attendance,Weaving", marked, "ADD", "ADDED_MANUALLY", True),
        ("Attendance,Stoppage", marked_stopped, "ADD", "ADDED_MANUALLY", True),
        ("Weaving", {}, "ADD", "ADDED_MANUALLY", True),
        (None, {}, "ADD", "ADDED_MANUALLY", True),
        # EXCLUDE outranks stopped-all-shift, worked and no-worker-marked
        ("Attendance,Weaving", marked, "EXCLUDE", "EXCLUDED_MANUALLY", False),
        ("Attendance,Stoppage", marked_stopped, "EXCLUDE", "EXCLUDED_MANUALLY", False),
        ("Weaving", {}, "EXCLUDE", "EXCLUDED_MANUALLY", False),
    ]
    runs = []
    for i, (sources, extras, action, _status, _counted) in enumerate(cases):
        row = worked_row(D, "A", i + 1, sources, **extras)
        runs.append(overridden(row, action) if action else row)
    data = report([], [], runs, view="machines_run")
    got = {r["machine_id"]: (r["status"], r["counted"]) for r in data["rows"]}
    assert got == {i + 1: (status, counted) for i, (*_x, status, counted) in enumerate(cases)}
    meta = data["meta"]
    assert (meta["added_manually"], meta["excluded_manually"]) == (4, 3)
    assert (meta["stopped_all_shift"], meta["no_worker_marked"]) == (1, 2)
    assert meta["machine_shifts_run"] == 5  # 1 worked + 4 added
    # every listed row is in exactly one of the five statuses
    assert (meta["machine_shifts_run"] + meta["excluded_manually"] + meta["stopped_all_shift"]
            + meta["no_worker_marked"]) == len(cases)
    # A row with no override carries no override fields at all — even if the
    # read handed it stray ones — and anything but ADD / EXCLUDE is no override.
    stray = worked_row(D, "A", 99, "Attendance", workers=1, manual_action=None, override_id=5,
                       manual_reason="x", manual_by="y", manual_at=OVERRIDE_AT)
    odd = worked_row(D, "A", 98, "Attendance", workers=1, manual_action="maybe")
    for row in report([], [], [stray, odd], view="machines_run")["rows"]:
        assert row["status"] == "WORKED"
        assert (row["manual_action"], row["override_id"], row["manual_reason"],
                row["manual_by"], row["manual_at"]) == (None, None, None, None, None)
    # the action is read case-insensitively
    lower = overridden(worked_row(D, "A", 97, "Weaving"), "ADD", manual_action="add")
    assert report([], [], [lower], view="machines_run")["rows"][0]["status"] == "ADDED_MANUALLY"


def test_suggested_flags_production_without_mark_and_no_override():
    runs = [
        worked_row(D, "A", 1, "Weaving", prod_cuts=12),                    # production, nobody marked
        worked_row(D, "A", 2, "Finishing,Stoppage", prod_kg=40),           # production next to a stoppage
        worked_row(D, "A", 3, "Waste"),                                    # waste is not production
        worked_row(D, "A", 4, "Stoppage", stoppage_hours=2),               # negative evidence only
        worked_row(D, "A", 5, "Attendance,Weaving", workers=1),            # marked: nothing to suggest
        worked_row(D, "A", 6, "Attendance,Weaving", workers=1, stoppage_hours=8),  # marked, stopped
        overridden(worked_row(D, "A", 7, "Weaving", prod_cuts=5), "ADD"),  # already decided
        overridden(worked_row(D, "A", 8, "Weaving", prod_cuts=5), "EXCLUDE"),
        added_without_entry(D, "A", 9),
    ]
    data = report([], [], runs, view="machines_run")
    assert {r["machine_id"]: r["suggested"] for r in data["rows"]} == {
        1: True, 2: True, 3: False, 4: False, 5: False, 6: False, 7: False, 8: False, 9: False}
    # the suggested rows are exactly the ones the "no worker marked" note counts
    assert any(n.startswith("2 machine-shift(s) have production entered but no worker marked")
               for n in data["meta"]["notes"])
    # a legacy-shaped row (no table flags) is read by the same rule
    legacy = [machine(D, "B", 1, "Weaving"), machine(D, "B", 2, "Weaving,Attendance")]
    assert [r["suggested"] for r in report([], [], legacy, view="machines_run")["rows"]] == [True, False]


def test_meta_can_edit_is_what_the_endpoint_resolved():
    for view in VIEWS:
        assert report([], [], [], view=view)["meta"]["can_edit"] is False
        assert report([], [], [], view=view, can_edit=True)["meta"]["can_edit"] is True
        meta = report([], [], [], view=view)["meta"]
        assert (meta["added_manually"], meta["excluded_manually"]) == (0, 0)


def test_rebuild_applies_overrides_after_the_source_insert():
    db = RecordingDb()
    assert rebuild_machine_shift_worked(db, 2, 29, *WINDOW) is True
    executed = [e for e in db.log if e[0] == "execute"]
    _set, _delete, source_insert, add_upsert, exclude_update = executed
    # same transaction (one commit, at the very end), in this order
    assert db.statements() == ["rollback"] + REBUILD
    binds = {"co_id": 2, "branch_id": 29, "date_from": WINDOW[0], "date_to": WINDOW[1]}
    assert source_insert[2] == binds and add_upsert[2] == binds and exclude_update[2] == binds
    assert add_upsert[1] == " ".join(str(get_machine_shift_worked_apply_add_query()).split())
    assert exclude_update[1] == " ".join(str(get_machine_shift_worked_apply_exclude_query()).split())

    # The source INSERT rebuilds a row from the sources: a stamp left by a
    # concurrent rebuild is cleared, then set again by the override statements.
    assert "manual_action = NULL" in source_insert[1][source_insert[1].index("ON DUPLICATE KEY UPDATE"):]

    add = add_upsert[1]
    assert add.startswith("INSERT INTO jute_prod_machine_shift_worked")
    assert "FROM jute_prod_machine_shift_override o INNER JOIN machine_mst m" in add
    assert "o.co_id = :co_id AND o.branch_id = :branch_id" in add
    assert "o.tran_date BETWEEN :date_from AND :date_to" in add
    assert "o.action = 'ADD' AND o.active = 1 AND NOT EXISTS" in add
    # a machine-shift with no source row gets one: no sources, no workers,
    # worked, stamped, with the shift's hours
    columns = add[add.index("(") + 1: add.index(")")].replace(" ", "").split(",")
    assert columns == [
        "co_id", "branch_id", "tran_date", "shift", "machine_id", "workers_marked",
        "att_stoppage_hours", "stoppage_hours", "breakdown_min", "shift_hours", "sources",
        "stopped_all_shift", "worked", "manual_action", "synced_at",
    ]
    assert ("SELECT src.co_id, src.branch_id, src.tran_date, src.shift, src.machine_id, "
            "0, 0, 0, 0, src.shift_hours, NULL, 0, 1, 'ADD', src.synced_at FROM (") in add
    assert "CASE UPPER(o.shift) WHEN 'A' THEN COALESCE(sh.hrs_a, 8)" in add
    # one the sources already wrote keeps its figures and only gets worked + the stamp
    upsert = add[add.index(") src ON DUPLICATE KEY UPDATE"):]
    assert upsert == (") src ON DUPLICATE KEY UPDATE worked = 1, manual_action = 'ADD', "
                      "synced_at = VALUES(synced_at)")

    exclude = exclude_update[1]
    assert exclude.startswith(
        "UPDATE jute_prod_machine_shift_worked w INNER JOIN jute_prod_machine_shift_override o ON")
    for cond in ("o.branch_id = w.branch_id", "o.tran_date = w.tran_date", "o.shift = w.shift",
                 "o.machine_id = w.machine_id"):
        assert cond in exclude
    assert "SET w.worked = 0, w.manual_action = 'EXCLUDE' WHERE" in exclude
    assert "w.co_id = :co_id AND w.branch_id = :branch_id" in exclude
    assert "w.tran_date BETWEEN :date_from AND :date_to" in exclude
    assert "o.co_id = :co_id AND o.action = 'EXCLUDE' AND o.active = 1 AND NOT EXISTS" in exclude
    # only the override IN FORCE applies: active, and the newest active one of
    # the machine-shift (two racing saves cannot both apply)
    for sql in (add, exclude):
        assert ("newer.active = 1 AND newer.machine_shift_override_id > "
                "o.machine_shift_override_id") in sql
    assert set(get_machine_shift_worked_apply_add_query().compile().params) == set(binds)
    assert set(get_machine_shift_worked_apply_exclude_query().compile().params) == set(binds)

    # A deadlock on an override statement is retried like one on the INSERT...
    db = RecordingDb(update_errors=[mysql_error(1213, "Deadlock found")])
    assert rebuild_machine_shift_worked(db, 2, 29, *WINDOW) is True
    assert db.statements() == (
        ["rollback", "SET", "DELETE", "INSERT", "INSERT", "UPDATE", "rollback"]
        + ["rollback"] + REBUILD
    )
    # ...twice in a row gives way, and anything else (the migration is missing) raises.
    db = RecordingDb(update_errors=[mysql_error(1205), mysql_error(1213)])
    assert rebuild_machine_shift_worked(db, 2, 29, *WINDOW) is False
    assert "commit" not in db.statements()
    db = RecordingDb(update_errors=[mysql_error(1054, "Unknown column 'w.manual_action'")])
    with pytest.raises(OperationalError):
        rebuild_machine_shift_worked(db, 2, 29, *WINDOW)
    assert "commit" not in db.statements() and db.statements()[-1] == "rollback"


def test_read_query_returns_the_override_in_force_of_each_row():
    sql = " ".join(str(get_machine_shift_worked_rows_query()).split())
    select_list = sql[: sql.index(" FROM ")]
    for col in ("w.manual_action", "o.machine_shift_override_id AS override_id",
                "o.reason AS manual_reason", "AS manual_by", "o.updated_date_time AS manual_at"):
        assert col in select_list
    # the user's name, else the e-mail
    assert "COALESCE(NULLIF(TRIM(u.name), ''), u.email_id) AS manual_by" in select_list
    assert "LEFT JOIN user_mst u ON u.user_id = o.updated_by" in sql
    join = sql[sql.index("LEFT JOIN jute_prod_machine_shift_override o"): sql.index("LEFT JOIN user_mst")]
    for cond in ("o.branch_id = w.branch_id", "o.tran_date = w.tran_date", "o.shift = w.shift",
                 "o.machine_id = w.machine_id", "o.co_id = w.co_id", "o.action = w.manual_action",
                 "o.active = 1", "NOT EXISTS"):
        assert cond in join  # at most one override per row: the join cannot double a machine-shift
    assert set(get_machine_shift_worked_rows_query().compile().params) == {
        "co_id", "branch_id", "date_from", "date_to"}


def test_override_migration_matches_the_statements():
    with open("dbqueries/migrations/20261001_machine_shift_override.sql", encoding="utf-8") as f:
        ddl = f.read()
    # the run-migration skill splits on semicolons and skips comment chunks
    statements = [c.strip() for c in ddl.split(";") if c.strip() and not c.strip().startswith("--")]
    create, alter = statements
    assert create.startswith("CREATE TABLE IF NOT EXISTS jute_prod_machine_shift_override (")
    columns = [line.split()[0] for line in create.splitlines()[1:]
               if line.startswith("  ") and line.split()[0].islower()]
    assert columns == [
        "machine_shift_override_id", "co_id", "branch_id", "tran_date", "shift", "machine_id",
        "action", "reason", "active", "updated_by", "updated_date_time",
    ]
    assert "KEY idx_mso_window (co_id, branch_id, tran_date, active)" in create
    assert "KEY idx_mso_machine (machine_id, tran_date)" in create
    assert alter.startswith("ALTER TABLE jute_prod_machine_shift_worked")
    assert "ADD COLUMN manual_action VARCHAR(10) NULL" in alter and alter.endswith("AFTER worked")
    # no comment line carries a semicolon of its own (it would split a comment
    # into a chunk the runner executes)
    for line in ddl.splitlines():
        if line.lstrip().startswith("--"):
            assert ";" not in line
    assert "DROP COLUMN manual_action" in ddl and "DROP TABLE IF EXISTS jute_prod_machine_shift_override" in ddl
