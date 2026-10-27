"""Unit tests for the Loom Checklist audit rules (src/juteProduction/reports.py).

The checklist is the pre-wages gate over weaving production: every rule is a
pure function over a day-slice / frozen-log row mapping, so these are plain
unit tests with no DB.
"""

from datetime import date

from src.juteProduction.reports import (
    LOOM_EFF_HIGH,
    loom_check_remarks,
    loom_eb_overload_remarks,
    loom_jugar_chain_remarks,
    loom_row_is_empty,
    loom_row_is_idle,
    loom_sync_states,
    parse_machine_limit,
)


def row(**over) -> dict:
    """A clean, mapped, producing loom row — override one field per test."""
    base = {
        "weaving_daily_id": 1,
        "tran_date": date(2026, 8, 1),
        "spell_id": 1,
        "spell_code": "A1",
        "branch_id": 87,
        "machine_id": 10,
        "mech_code": "L-10",
        "weaving_quality_id": 5,
        "eb_id": 7,
        "cuts": 4.0,
        "close_jugar": 2.0,
        "open_jugar": 0.0,
        "less_production": 0.0,
        "production_yds": 100.0,
        "efficiency": 60.0,
        "std_eff": 0.0,
        "working_hours": 8.0,
        "std_picks": 10.0,
        "std_speed": 120.0,
        "eff_speed": 120.0,
    }
    base.update(over)
    return base


# --- per-row rules ---------------------------------------------------------


def test_clean_row_has_no_remarks():
    assert loom_check_remarks(row()) == []


def test_quality_not_mapped_suppresses_derived_rules():
    r = row(weaving_quality_id=None, production_yds=0.0, efficiency=0.0)
    remarks = loom_check_remarks(r)
    assert remarks == ["No quality map", "EB-No Prod Entry"]
    # d/e/g/h stayed silent even though cuts+jugar > 0 and prod == 0
    assert "Cuts/Jugar, No Prod" not in remarks


def test_quality_not_mapped_still_reports_negative_production():
    remarks = loom_check_remarks(row(weaving_quality_id=None, production_yds=-5.0))
    assert "Neg Prod" in remarks


def test_quality_missing_reason_names_the_pending_action():
    r = row(weaving_quality_id=None, eb_id=None, production_yds=0.0, efficiency=0.0)
    # never synced + no §6.6 map row: mapping is the blocker, sync alone won't help
    assert "No quality map — map + Sync" in loom_check_remarks(
        r, sync_state="not_synced", map_exists=False
    )
    # never synced but the map row exists: sync is the only pending step
    assert "Not synced — run Sync" in loom_check_remarks(
        r, sync_state="not_synced", map_exists=True
    )
    # synced yet still NULL: the plain unmapped message stands
    assert "No quality map" in loom_check_remarks(r, sync_state="synced")


def test_quality_missing_day_without_picks_adds_sqc_remark():
    r = row(weaving_quality_id=None, eb_id=None, production_yds=0.0, efficiency=0.0)
    remarks = loom_check_remarks(
        r, sync_state="not_synced", map_exists=False, day_has_picks=False
    )
    assert "No Picks" in remarks
    # a quality-MAPPED row does not get the day-level pick remark from here
    # (the producing case is covered by the zero-efficiency cause split)
    mapped = loom_check_remarks(row(), day_has_picks=False)
    assert mapped == []


def test_eb_without_production():
    assert "EB-No Prod Entry" in loom_check_remarks(
        row(production_yds=0.0, efficiency=0.0, cuts=0.0, close_jugar=0.0)
    )


def test_production_without_eb():
    assert "No EB" in loom_check_remarks(row(eb_id=None))


def test_eb_rules_gated_off_when_not_synced():
    unsynced = loom_check_remarks(row(eb_id=None), sync_state="not_synced")
    assert "No EB" not in unsynced
    assert unsynced == []
    blank = row(production_yds=0.0, efficiency=0.0, cuts=0.0, close_jugar=0.0)
    assert loom_check_remarks(blank, sync_state="not_synced") == []
    assert loom_check_remarks(blank, sync_state="locked") == ["EB-No Prod Entry"]


def test_zero_efficiency_cause_split():
    assert "Full-spell stoppage + Prod" in loom_check_remarks(
        row(efficiency=0.0, working_hours=0.0)
    )
    assert "Eff 0, No Picks" in loom_check_remarks(row(efficiency=0.0, std_picks=None))
    assert "Eff 0, No Speed Std" in loom_check_remarks(
        row(efficiency=0.0, eff_speed=0, std_speed=0)
    )
    assert "Eff 0" in loom_check_remarks(row(efficiency=0.0))


def test_zero_efficiency_emits_exactly_one_cause():
    remarks = loom_check_remarks(row(efficiency=0.0, working_hours=0.0, std_picks=None))
    assert len(remarks) == 1


def test_cuts_jugar_without_production():
    assert "Cuts/Jugar, No Prod" in loom_check_remarks(
        row(eb_id=None, production_yds=0.0, efficiency=0.0)
    )


def test_negative_production():
    assert "Neg Prod" in loom_check_remarks(row(production_yds=-5.0, efficiency=0.0))


def test_efficiency_bounds_fall_back_to_85_without_standard():
    assert f"Eff > {LOOM_EFF_HIGH}%" in loom_check_remarks(row(efficiency=90.0))
    assert "Eff < 10%" in loom_check_remarks(row(efficiency=5.0))
    assert loom_check_remarks(row(efficiency=85.0)) == []
    assert loom_check_remarks(row(efficiency=10.0)) == []


def test_efficiency_high_limit_is_quality_aware():
    # std_eff 60 -> limit 69; 90 is fine against the 85 fallback but not here.
    assert "Eff > 69%" in loom_check_remarks(row(efficiency=90.0, std_eff=60.0))
    assert loom_check_remarks(row(efficiency=68.0, std_eff=60.0)) == []
    # a generous standard raises the bar above the flat 85
    assert loom_check_remarks(row(efficiency=90.0, std_eff=80.0)) == []


def test_stale_unit_remark():
    assert "Stale — re-sync" in loom_check_remarks(row(), sync_state="stale")
    assert "Stale — re-sync" not in loom_check_remarks(row(), sync_state="synced")


def test_branchless_row_flagged_only_for_branch_scoped_request():
    r = row(branch_id=None)
    assert "No branch on row" in loom_check_remarks(r, request_branch_id=87)
    assert loom_check_remarks(r, request_branch_id=None) == []


# --- sync state ------------------------------------------------------------


def header(**over) -> dict:
    base = {"spell_id": 1, "branch_id": None, "is_locked": 0, "reprocess_needed": 0}
    base.update(over)
    return base


def test_sync_states():
    assert loom_sync_states([header()], None) == {1: "synced"}
    assert loom_sync_states([header(is_locked=1)], None) == {1: "locked"}
    assert loom_sync_states([header(reprocess_needed=1)], None) == {1: "stale"}
    # a branch-scoped header cannot cover a co-wide read
    assert loom_sync_states([header(branch_id=87)], None) == {1: "not_synced"}
    assert loom_sync_states([header(branch_id=87)], 87) == {1: "synced"}
    # one stale sibling header makes the whole spell stale
    assert loom_sync_states(
        [header(branch_id=None, is_locked=1), header(branch_id=87, reprocess_needed=1)], 87
    ) == {1: "stale"}
    # a spell with no header at all never appears
    assert loom_sync_states([], None) == {}


# --- cross-row rules -------------------------------------------------------


def looms(*machine_ids, **over) -> list:
    """One EB (7) across N looms of the same date+spell."""
    return [row(weaving_daily_id=i, machine_id=m, mech_code=f"L-{m}", **over)
            for i, m in enumerate(machine_ids, start=1)]


def test_eb_within_default_limit_not_flagged():
    # two looms is the normal allowance when no designation limit is configured
    assert loom_eb_overload_remarks(looms(10, 11), {}) == {}
    assert loom_eb_overload_remarks(looms(10), {}) == {}
    # same loom twice (two entries) is one machine, never an overload
    assert loom_eb_overload_remarks([row(weaving_daily_id=1), row(weaving_daily_id=2)], {}) == {}


def test_eb_over_default_limit_flags_every_involved_row():
    assert loom_eb_overload_remarks(looms(10, 11, 12), {}) == {
        0: "EB on 3 looms (limit 2)",
        1: "EB on 3 looms (limit 2)",
        2: "EB on 3 looms (limit 2)",
    }


def test_eb_limit_from_designation_raises_the_bar():
    assert loom_eb_overload_remarks(looms(10, 11, 12), {7: 6}) == {}
    # ...and a limit of 1 lowers it
    assert loom_eb_overload_remarks(looms(10, 11), {7: 1}) == {
        0: "EB on 2 looms (limit 1)",
        1: "EB on 2 looms (limit 1)",
    }
    # the limit is per EB — another EB still gets the default
    mixed = looms(10, 11, 12) + [row(weaving_daily_id=9, machine_id=20, eb_id=8),
                                 row(weaving_daily_id=10, machine_id=21, eb_id=8),
                                 row(weaving_daily_id=11, machine_id=22, eb_id=8)]
    assert set(loom_eb_overload_remarks(mixed, {7: 6})) == {3, 4, 5}


def test_eb_overload_groups_by_date_and_spell_id():
    # different spell_id -> different group, even with the same spell_code
    other_spell = [
        row(weaving_daily_id=1, machine_id=10, spell_id=1, spell_code="A1"),
        row(weaving_daily_id=2, machine_id=11, spell_id=3, spell_code="A1"),
    ]
    assert loom_eb_overload_remarks(other_spell, {7: 1}) == {}
    # different date -> different group
    other_day = [
        row(weaving_daily_id=1, machine_id=10),
        row(weaving_daily_id=2, machine_id=11, tran_date=date(2026, 8, 2)),
    ]
    assert loom_eb_overload_remarks(other_day, {7: 1}) == {}
    # eb_id NULL rows are never grouped
    assert loom_eb_overload_remarks(looms(10, 11, 12, eb_id=None), {}) == {}


def test_parse_machine_limit():
    assert parse_machine_limit("6") == 6
    assert parse_machine_limit(" 6 ") == 6
    assert parse_machine_limit(6) == 6
    # blank / NULL / zero / junk -> no usable limit, caller uses the default
    assert parse_machine_limit("") is None
    assert parse_machine_limit(None) is None
    assert parse_machine_limit("0") is None
    assert parse_machine_limit("two") is None


def test_jugar_chain_break_never_flags_the_first_row():
    rows = [
        row(weaving_daily_id=1, spell_code="A1", open_jugar=99.0, close_jugar=2.0),
        row(weaving_daily_id=2, spell_code="B1", open_jugar=2.0, close_jugar=3.0),
        row(weaving_daily_id=3, spell_code="A2", open_jugar=7.0),
    ]
    assert loom_jugar_chain_remarks(rows) == {2: "Jugar chain break"}


def test_jugar_chain_orders_by_date_then_spell_rank():
    # listed out of order; the chain is day1/A1 -> day1/C -> day2/A1
    rows = [
        row(weaving_daily_id=3, tran_date=date(2026, 8, 2), spell_code="A1",
            open_jugar=5.0, close_jugar=6.0),
        row(weaving_daily_id=1, spell_code="A1", open_jugar=0.0, close_jugar=4.0),
        row(weaving_daily_id=2, spell_code="C", open_jugar=4.0, close_jugar=5.0),
    ]
    assert loom_jugar_chain_remarks(rows) == {}


def test_jugar_chain_tolerance_and_scope():
    tiny = [
        row(weaving_daily_id=1, spell_code="A1", close_jugar=2.0),
        row(weaving_daily_id=2, spell_code="B1", open_jugar=2.0005),
    ]
    assert loom_jugar_chain_remarks(tiny) == {}
    over = [
        row(weaving_daily_id=1, spell_code="A1", close_jugar=2.0),
        row(weaving_daily_id=2, spell_code="B1", open_jugar=2.01),
    ]
    assert 1 in loom_jugar_chain_remarks(over)
    # quality is not a criterion: a quality change on the same loom continues the chain
    split = [
        row(weaving_daily_id=1, spell_code="A1", weaving_quality_id=5, close_jugar=2.0),
        row(weaving_daily_id=2, spell_code="B1", weaving_quality_id=6, open_jugar=9.0),
    ]
    assert loom_jugar_chain_remarks(split) == {1: "Jugar chain break"}


def test_jugar_chain_includes_quality_unmapped_rows():
    unmapped = [
        row(weaving_daily_id=1, spell_code="A1", weaving_quality_id=None, close_jugar=2.0),
        row(weaving_daily_id=2, spell_code="B1", weaving_quality_id=None, open_jugar=2.0),
    ]
    assert loom_jugar_chain_remarks(unmapped) == {}


def test_jugar_chain_orders_a_b_c_spells():
    # A -> B -> C scheme: C must come after B, not before A
    rows = [
        row(weaving_daily_id=3, spell_code="C", open_jugar=6.0, close_jugar=1.0),
        row(weaving_daily_id=1, spell_code="A", open_jugar=0.0, close_jugar=4.0),
        row(weaving_daily_id=2, spell_code="B", open_jugar=4.0, close_jugar=6.0),
    ]
    assert loom_jugar_chain_remarks(rows) == {}


# --- 'chk' idle filter -----------------------------------------------------


def test_idle_row_is_filtered_from_checklist():
    # The TKT-000122 screenshot case: carry-forward jugar (close == open),
    # zero cuts, no EB, no production — dropped whatever remarks it carries.
    idle = row(eb_id=None, cuts=0.0, open_jugar=9.0, close_jugar=9.0,
               production_yds=0.0, efficiency=0.0)
    assert loom_row_is_idle(idle) is True


def test_idle_filter_keeps_rows_with_entries_or_wage_risk():
    idle = dict(row(eb_id=None, cuts=0.0, open_jugar=9.0, close_jugar=9.0,
                    production_yds=0.0, efficiency=0.0))
    # cuts entered
    assert loom_row_is_idle(dict(idle, cuts=1.0)) is False
    # jugar moved
    assert loom_row_is_idle(dict(idle, close_jugar=10.0)) is False
    # EB credited on a do-nothing loom = the wage risk the audit exists for
    assert loom_row_is_idle(dict(idle, eb_id=7)) is False
    # production present (positive or negative) is never idle
    assert loom_row_is_idle(dict(idle, production_yds=5.0)) is False
    assert loom_row_is_idle(dict(idle, production_yds=-5.0)) is False


def test_idle_filter_indifferent_to_sync_and_quality():
    # Nothing entered = nothing to audit, even on not-synced / unmapped rows;
    # the sync gate stays visible through rows that DO carry entries.
    idle = row(eb_id=None, cuts=0.0, open_jugar=9.0, close_jugar=9.0,
               production_yds=0.0, efficiency=0.0, weaving_quality_id=None)
    assert loom_row_is_idle(idle) is True


# --- 'All' filter ----------------------------------------------------------


def test_all_filter_hides_only_truly_empty_rows():
    empty = row(eb_id=None, cuts=0.0, close_jugar=0.0, less_production=0.0,
                production_yds=0.0, efficiency=0.0)
    assert loom_row_is_empty(empty, []) is True
    # remarked rows are never hidden...
    assert loom_row_is_empty(empty, ["anything"]) is False
    # ...nor is a row carrying an EB, cuts, jugar, less_production or production
    assert loom_row_is_empty(row(**{**empty, "eb_id": 7}), []) is False
    assert loom_row_is_empty(dict(empty, cuts=1.0), []) is False
    assert loom_row_is_empty(dict(empty, less_production=3.0), []) is False
    assert loom_row_is_empty(dict(empty, production_yds=-5.0), []) is False


def test_negative_production_row_survives_the_all_filter():
    r = row(production_yds=-5.0, efficiency=0.0)
    remarks = loom_check_remarks(r)
    assert "Neg Prod" in remarks
    assert loom_row_is_empty(r, remarks) is False
