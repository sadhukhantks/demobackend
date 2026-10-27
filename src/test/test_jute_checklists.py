"""Unit tests for the wage-reconciliation checklists (src/juteProduction/reports.py).

Winding / beaming / finishing / attendance are plain exception grids: the SQL
does the matching and aggregation, and every rule is a pure function over one
result row. So these are unit tests over the rule functions, the shared
parameter validation, and the rtype filter — no DB.
"""

from datetime import date

import pytest
from fastapi import HTTPException

from src.juteProduction.reports import (
    ATTENDANCE_CHECKLIST_MAX_DAYS,
    BEAMING_MACHINE_TYPE,
    CHECKLIST_MAX_DAYS,
    WINDER_DESIG_PATTERN,
    WINDING_MAX_KG_PER_SHIFT,
    _checklist_rows,
    _checklist_scope,
    _desig_matches,
    attendance_checklist,
    beaming_checklist,
    beaming_remarks,
    finishing_checklist,
    finishing_remarks,
    winding_attendance_remarks,
    winding_checklist,
    winding_production_remarks,
)
from src.juteProduction.reportQueries import (
    get_beaming_checklist_query,
    get_deactivate_rejected_attendance_query,
    get_finishing_checklist_query,
    get_winding_attendance_checklist_query,
    get_winding_production_checklist_query,
)


class Req:
    """Minimal stand-in for a Starlette Request — the checklists only ever read
    query_params."""

    def __init__(self, **params):
        self.query_params = {k: v for k, v in params.items() if v is not None}


def scope_params(**over) -> dict:
    base = {"co_id": "2", "date_from": "2026-08-01", "date_to": "2026-08-14"}
    base.update(over)
    return base


# --- designation matching --------------------------------------------------
# The SQL selects designations by LIKE pattern (ids differ per tenant); the
# reverse direction re-checks the same pattern in Python, so the two must agree.


@pytest.mark.parametrize(
    "desig",
    ["WINDER-COP", "WINDER SPOOL", "WINDER > 12 LBS", "WINDER TRAINEE (S4)", "winder"],
)
def test_winder_pattern_matches_real_designations(desig):
    assert _desig_matches(desig, WINDER_DESIG_PATTERN) is True


@pytest.mark.parametrize(
    "desig",
    ["PM FITTER M/ROLL WINDING", "SPINNER", "MAZDOOR,BALES OPENER", "", None],
)
def test_winder_pattern_rejects_non_winders(desig):
    # 'winder%' is a PREFIX match — a fitter on a winding machine is not a winder.
    assert _desig_matches(desig, WINDER_DESIG_PATTERN) is False


def test_no_finishing_name_pattern_survives():
    # The old '%finish%' pattern was 100% false positives on sls — it caught
    # carding/drawing "FINISHER FEEDER" and mechanics, and none of the real
    # hemming/herackle/sewing staff. Finishing is map-driven only now.
    import src.juteProduction.reports as reports

    assert not hasattr(reports, "FINISHING_DESIG_PATTERN")
    assert _desig_matches("FINISHER FEEDER", "%finish%") is True  # why it had to go


# --- winding, attendance direction ----------------------------------------


def w_att(**over) -> dict:
    """A winder present for a full shift with production booked."""
    base = {
        "daily_atten_id": 1,
        "spell_id": 102,
        "eb_id": 7,
        "designation": "WINDER-COP",
        "net_hours": 8.0,
        "doff_count": 1,
        "production_qty": 210.0,
    }
    base.update(over)
    return base


def test_winding_attendance_clean_row():
    assert winding_attendance_remarks(w_att()) == []


def test_winding_attendance_no_working_hours():
    assert "Present but no working hours" in winding_attendance_remarks(
        w_att(net_hours=0.0)
    )
    # idle_hours can exceed working_hours, so net can go negative
    assert "Present but no working hours" in winding_attendance_remarks(
        w_att(net_hours=-1.0)
    )


def test_winding_attendance_no_production():
    no_doff = winding_attendance_remarks(w_att(doff_count=0, production_qty=0.0))
    assert "Present, no winding production recorded" in no_doff
    # a doff row that weighed nothing is just as unpaid as no doff row at all
    assert "Present, no winding production recorded" in winding_attendance_remarks(
        w_att(doff_count=1, production_qty=0.0)
    )


def test_winding_attendance_shift_cap():
    remarks = winding_attendance_remarks(w_att(doff_count=2, production_qty=480.0))
    assert remarks == [
        f"Production 480 kg exceeds {WINDING_MAX_KG_PER_SHIFT} kg/shift — probable typo"
    ]
    # exactly at the cap is fine
    assert winding_attendance_remarks(w_att(production_qty=410.0)) == []


def test_winding_attendance_stacks_remarks():
    remarks = winding_attendance_remarks(
        w_att(net_hours=0.0, doff_count=0, production_qty=0.0)
    )
    assert remarks == [
        "Present but no working hours",
        "Present, no winding production recorded",
    ]


# --- winding, production direction ----------------------------------------


def w_prod(**over) -> dict:
    """Doffs credited to an eb who was present as a winder for a full shift."""
    base = {
        "spell_id": 102,
        "eb_id": 7,
        "designation": "WINDER-COP",
        "atten_rows": 1,
        "net_hours": 8.0,
        "doff_count": 1,
        "production_qty": 210.0,
    }
    base.update(over)
    return base


def test_winding_production_clean_row():
    assert winding_production_remarks(w_prod()) == []


def test_winding_production_without_attendance():
    remarks = winding_production_remarks(
        w_prod(atten_rows=0, net_hours=0.0, designation=None)
    )
    assert remarks == ["No attendance for this shift — wages at risk"]
    # the designation rule must NOT also fire — there is no attendance to judge
    assert not any("not a winding designation" in r for r in remarks)


def test_winding_production_under_wrong_designation():
    # No map rows for the scope -> the legacy 'winder%' name match still decides.
    assert winding_production_remarks(w_prod(designation="SPINNER")) == [
        "Present under 'SPINNER', not a winding designation"
    ]


def test_winding_production_designation_map_overrides_the_pattern():
    # Configured set wins both ways: an off-pattern name on the map is clean...
    assert winding_production_remarks(
        w_prod(designation="COP WINDING OPERATOR", worked_designation_id=55), {55}
    ) == []
    # ...and a 'winder%' name that is NOT on the map is flagged.
    assert winding_production_remarks(
        w_prod(designation="WINDER-COP", worked_designation_id=56), {55}
    ) == ["Present under 'WINDER-COP', not a winding designation"]


def test_winding_production_present_but_no_hours():
    assert winding_production_remarks(w_prod(net_hours=0.0)) == [
        "Present but no working hours"
    ]


def test_winding_production_blank_doff():
    assert winding_production_remarks(w_prod(production_qty=0.0)) == [
        "Blank/zero doff entry"
    ]


def test_winding_production_shift_cap():
    remarks = winding_production_remarks(w_prod(doff_count=2, production_qty=504.0))
    assert remarks == [
        f"Production 504 kg exceeds {WINDING_MAX_KG_PER_SHIFT} kg/shift — probable typo"
    ]


def test_winding_production_absent_and_over_cap_stack():
    assert winding_production_remarks(
        w_prod(atten_rows=0, net_hours=0.0, designation=None, production_qty=504.0)
    ) == [
        "No attendance for this shift — wages at risk",
        f"Production 504 kg exceeds {WINDING_MAX_KG_PER_SHIFT} kg/shift — probable typo",
    ]


# --- beaming ---------------------------------------------------------------


def test_beaming_machine_ran_without_hours():
    assert beaming_remarks({"net_hours": 0.0, "cuts": 141.0}) == [
        "Machine ran, no worker hours booked"
    ]


def test_beaming_hours_without_cuts():
    assert beaming_remarks({"net_hours": 8.0, "cuts": 0.0}) == [
        "Hours booked, no cuts produced"
    ]


def test_beaming_clean_and_empty_rows():
    assert beaming_remarks({"net_hours": 8.0, "cuts": 141.0}) == []
    # neither side booked anything — nothing to reconcile, so no remark
    assert beaming_remarks({"net_hours": 0.0, "cuts": 0.0}) == []


# --- finishing -------------------------------------------------------------


# The configured sls br-87 finishing set (jute_prod_checklist_map), trimmed.
FIN_MAP = {1702, 1864, 1865, 1872}


def fin(**over) -> dict:
    base = {
        "eb_id": 7,
        "designation": "HEMMING MAZDOOR",
        "worked_designation_id": 1702,
        "atten_rows": 1,
        "net_hours": 8.0,
        "prod_rows": 1,
        "prod_qty": 100.0,
        "prod_wt_kg": 50.0,
    }
    base.update(over)
    return base


def test_finishing_matched_row_is_clean():
    assert finishing_remarks(fin(), FIN_MAP) == []


def test_finishing_production_without_attendance():
    assert finishing_remarks(
        fin(atten_rows=0, net_hours=0.0, designation=None, worked_designation_id=None),
        FIN_MAP,
    ) == ["No attendance for this shift — wages at risk"]


def test_finishing_production_under_wrong_designation():
    # Off-map designation — by id, not by name. "FINISHER FEEDER" is exactly the
    # carding designation the old '%finish%' pattern wrongly counted as finishing.
    assert finishing_remarks(
        fin(designation="FINISHER FEEDER", worked_designation_id=91), FIN_MAP
    ) == ["Present under 'FINISHER FEEDER', not finishing"]


def test_finishing_designation_judged_by_map_not_by_name():
    # A name that looks nothing like "finishing" is fine if it is on the map...
    assert finishing_remarks(
        fin(designation="J BAG SEWER", worked_designation_id=1870), FIN_MAP | {1870}
    ) == []
    # ...and a name containing "finishing" is not, if it is off the map.
    assert finishing_remarks(
        fin(designation="OILER (MECH.FINISHING)", worked_designation_id=404), FIN_MAP
    ) == ["Present under 'OILER (MECH.FINISHING)', not finishing"]


def test_finishing_attendance_without_production():
    assert finishing_remarks(
        fin(prod_rows=0, prod_qty=0.0, prod_wt_kg=0.0), FIN_MAP
    ) == ["Present in finishing, no work entered"]


def test_finishing_production_without_eb_reports_only_that():
    # An eb-less production row can never pair with attendance; the absent-worker
    # rule would be a false accusation.
    assert finishing_remarks(
        fin(eb_id=None, atten_rows=0, designation=None, worked_designation_id=None),
        FIN_MAP,
    ) == ["No EB on the production entry — cannot match attendance"]


# --- shared parameter validation ------------------------------------------


def test_scope_requires_co_id_and_dates():
    with pytest.raises(HTTPException) as e:
        _checklist_scope(Req(date_from="2026-08-01", date_to="2026-08-02"), 62)
    assert e.value.status_code == 400
    assert "co_id" in e.value.detail

    with pytest.raises(HTTPException) as e:
        _checklist_scope(Req(co_id="2", date_from="2026-08-01"), 62)
    assert e.value.status_code == 400
    assert "date_from and date_to are required" in e.value.detail


@pytest.mark.parametrize(
    "over",
    [{"co_id": "abc"}, {"date_from": "01-08-2026"}, {"branch_id": "x"}],
)
def test_scope_rejects_unparseable_values(over):
    with pytest.raises(HTTPException) as e:
        _checklist_scope(Req(**scope_params(**over)), 62)
    assert e.value.status_code == 400


def test_scope_rejects_reversed_range():
    with pytest.raises(HTTPException) as e:
        _checklist_scope(
            Req(**scope_params(date_from="2026-08-14", date_to="2026-08-01")), 62
        )
    assert e.value.detail == "date_to is before date_from"


def test_scope_caps_the_window_at_max_days():
    # 62 days inclusive is allowed, 63 is not
    ok = scope_params(date_from="2026-06-01", date_to="2026-08-01")
    assert _checklist_scope(Req(**ok), CHECKLIST_MAX_DAYS)[2].isoformat() == "2026-06-01"
    with pytest.raises(HTTPException) as e:
        _checklist_scope(Req(**scope_params(date_from="2026-06-01", date_to="2026-08-02")), CHECKLIST_MAX_DAYS)
    assert e.value.detail == f"Date range too large — at most {CHECKLIST_MAX_DAYS} days"


def test_attendance_listing_has_the_tighter_cap():
    assert ATTENDANCE_CHECKLIST_MAX_DAYS == 31
    # 31 days pass the attendance cap...
    _checklist_scope(
        Req(**scope_params(date_from="2026-08-01", date_to="2026-08-31")),
        ATTENDANCE_CHECKLIST_MAX_DAYS,
    )
    # ...32 do not, even though the other checklists would accept them
    over = scope_params(date_from="2026-08-01", date_to="2026-09-01")
    _checklist_scope(Req(**over), CHECKLIST_MAX_DAYS)
    with pytest.raises(HTTPException) as e:
        attendance_checklist(Req(**over), db=None, token_data=None)
    assert e.value.status_code == 400
    assert "at most 31 days" in e.value.detail


def test_scope_returns_parsed_values():
    co_id, branch_id, d_from, d_to = _checklist_scope(
        Req(**scope_params(branch_id="87")), 62
    )
    assert (co_id, branch_id) == (2, 87)
    assert (d_from.isoformat(), d_to.isoformat()) == ("2026-08-01", "2026-08-14")
    # branch_id stays None when omitted (NULL-tolerant, co-wide read)
    assert _checklist_scope(Req(**scope_params()), 62)[1] is None


def test_winding_checklist_rejects_unknown_direction():
    with pytest.raises(HTTPException) as e:
        winding_checklist(
            Req(**scope_params(direction="sideways")), db=None, token_data=None
        )
    assert e.value.status_code == 400
    assert "direction must be" in e.value.detail


# --- rtype filtering -------------------------------------------------------


class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class FakeDB:
    def __init__(self, rows):
        self._rows = [FakeRow(r) for r in rows]
        self.binds = None

    def execute(self, query, binds):
        self.binds = binds
        return self

    def fetchall(self):
        return self._rows


def _run(rtype):
    rows = [
        w_att(daily_atten_id=1, eb_id=7),
        w_att(daily_atten_id=2, eb_id=8, doff_count=0, production_qty=0.0),
    ]
    db = FakeDB(rows)
    return _checklist_rows(
        db,
        "sql",
        {"co_id": 2},
        lambda m: {"eb_id": m["eb_id"]},
        winding_attendance_remarks,
        rtype,
    )


def test_rtype_chk_returns_only_remarked_rows():
    out = _run("chk")
    assert [r["eb_id"] for r in out] == [8]
    assert out[0]["remarks"] == "Present, no winding production recorded"


def test_rtype_all_returns_every_matched_row():
    out = _run("all")
    assert [r["eb_id"] for r in out] == [7, 8]
    assert out[0]["remarks"] == ""


def test_remarks_are_semicolon_joined():
    db = FakeDB([w_att(net_hours=0.0, doff_count=0, production_qty=0.0)])
    out = _checklist_rows(
        db, "sql", {}, lambda m: {}, winding_attendance_remarks, "chk"
    )
    assert out[0]["remarks"] == (
        "Present but no working hours; Present, no winding production recorded"
    )


# --- jute_prod_checklist_map wiring ---------------------------------------
# DB-only config table (no UI): which designations / machine types each
# checklist counts, per branch. Finishing is map-driven ONLY; winding and
# beaming fall back to their name match when the scope has no rows.


class SeqDB:
    """FakeDB for endpoints that query twice — the map first, then the grid."""

    def __init__(self, *results):
        self._results = [[FakeRow(r) for r in rows] for rows in results]
        self.calls = []
        self._cur = []

    def execute(self, query, binds):
        self._cur = self._results[len(self.calls)]
        self.calls.append((query, dict(binds)))
        return self

    def fetchall(self):
        return self._cur


def map_rows(*ids) -> list:
    return [{"ref_id": i} for i in ids]


def fin_sql_row(**over) -> dict:
    base = {
        "tran_date": date(2026, 8, 4),
        "spell_id": 102,
        "spell_code": "A",
        "eb_id": 7,
        "eb_no": "1234",
        "worker_name": "SEWER",
        "designation": "HEMMING MAZDOOR",
        "worked_designation_id": 1702,
        "quality": "DW BAG 44X26 - DW4426",
        "atten_rows": 1,
        "net_hours": 8.0,
        "prod_rows": 1,
        "prod_qty": 100.0,
        "prod_wt_kg": 50.0,
    }
    base.update(over)
    return base


def test_finishing_unconfigured_branch_returns_empty_data():
    db = SeqDB(map_rows())  # no map rows -> the grid query never runs
    assert finishing_checklist(
        Req(**scope_params(branch_id="87")), db=db, token_data=None
    ) == {"data": []}
    assert len(db.calls) == 1
    assert db.calls[0][1] == {
        "co_id": 2,
        "branch_id": 87,
        "checklist": "finishing",
        "ref_type": "designation",
    }


def test_finishing_binds_the_configured_designation_ids():
    db = SeqDB(map_rows(1702, 1864), [fin_sql_row()])
    out = finishing_checklist(
        Req(**scope_params(branch_id="87", rtype="all")), db=db, token_data=None
    )
    assert db.calls[1][1]["desig_ids"] == [1702, 1864]
    assert "desig_pattern" not in db.calls[1][1]
    assert [r["remarks"] for r in out["data"]] == [""]


def test_finishing_flags_a_row_whose_designation_is_off_the_map():
    db = SeqDB(
        map_rows(1702),
        [fin_sql_row(designation="FINISHER FEEDER", worked_designation_id=91)],
    )
    out = finishing_checklist(Req(**scope_params()), db=db, token_data=None)
    assert out["data"][0]["remarks"] == "Present under 'FINISHER FEEDER', not finishing"


def test_finishing_co_wide_read_leaves_branch_null():
    db = SeqDB(map_rows())
    finishing_checklist(Req(**scope_params()), db=db, token_data=None)
    assert db.calls[0][1]["branch_id"] is None


def beam_sql_row(**over) -> dict:
    base = {
        "tran_date": date(2026, 8, 4),
        "spell_id": 102,
        "spell_code": "A",
        "machine_id": 9,
        "mech_code": "BM1",
        "machine_name": "BEAM 1",
        "workers": "1234",
        "quality_code": "HW1",
        "quality": "HESSIAN WARP",
        "net_hours": 8.0,
        "cuts": 0.0,
        "beams": 3,
    }
    base.update(over)
    return base


def test_beaming_falls_back_to_the_machine_type_name():
    db = SeqDB(map_rows(), [beam_sql_row()])
    out = beaming_checklist(Req(**scope_params()), db=db, token_data=None)
    assert db.calls[0][1]["ref_type"] == "machine_type"
    assert db.calls[1][1]["beaming_type"] == BEAMING_MACHINE_TYPE
    assert "mt_ids" not in db.calls[1][1]
    assert out["data"][0]["remarks"] == "Hours booked, no cuts produced"


def test_beaming_configured_machine_types_replace_the_name_match():
    db = SeqDB(map_rows(3, 11), [beam_sql_row()])
    beaming_checklist(Req(**scope_params(branch_id="29")), db=db, token_data=None)
    assert db.calls[1][1]["mt_ids"] == [3, 11]
    assert "beaming_type" not in db.calls[1][1]


def wind_sql_row(**over) -> dict:
    base = {
        "daily_atten_id": 1,
        "tran_date": date(2026, 8, 4),
        "spell_id": 102,
        "spell_code": "A",
        "eb_id": 7,
        "eb_no": "1234",
        "worker_name": "WINDER",
        "designation": "WINDER-COP",
        "quality": "JUTE YARN 8 LBS - JY8",
        "net_hours": 8.0,
        "doff_count": 0,
        "production_qty": 0.0,
    }
    base.update(over)
    return base


def test_winding_falls_back_to_the_winder_pattern():
    db = SeqDB(map_rows(), [wind_sql_row()])
    out = winding_checklist(Req(**scope_params()), db=db, token_data=None)
    assert db.calls[0][1]["checklist"] == "winding"
    assert db.calls[1][1]["desig_pattern"] == WINDER_DESIG_PATTERN
    assert "desig_ids" not in db.calls[1][1]
    assert out["data"][0]["remarks"] == "Present, no winding production recorded"


def test_winding_configured_designations_replace_the_pattern():
    db = SeqDB(map_rows(55, 56), [wind_sql_row()])
    winding_checklist(Req(**scope_params(branch_id="29")), db=db, token_data=None)
    assert db.calls[1][1]["desig_ids"] == [55, 56]
    assert "desig_pattern" not in db.calls[1][1]


def test_winding_production_direction_never_binds_a_designation_filter():
    # The production side matches every designation in SQL and judges it in
    # Python, so neither the ids nor the pattern belong in its binds.
    db = SeqDB(map_rows(55), [dict(wind_sql_row(), atten_rows=0, worked_designation_id=None)])
    winding_checklist(
        Req(**scope_params(direction="production")), db=db, token_data=None
    )
    assert "desig_ids" not in db.calls[1][1]
    assert "desig_pattern" not in db.calls[1][1]


# --- quality column --------------------------------------------------------
# Rows are aggregates over a (date, spell, eb|machine) key, so the quality is a
# GROUP_CONCAT of "<name> - <code>" inside the aggregate derived table. It must
# survive the shape() whitelist in every handler.


def _sql(q) -> str:
    return " ".join(str(q).split())


@pytest.mark.parametrize(
    "query, join, expr",
    [
        (
            get_winding_attendance_checklist_query(),
            "LEFT JOIN item_mst im ON im.item_id = wd.item_id",
            "CONCAT_WS(' - ', im.item_name, im.item_code)",
        ),
        (
            get_winding_production_checklist_query(),
            "LEFT JOIN item_mst im ON im.item_id = wd.item_id",
            "CONCAT_WS(' - ', im.item_name, im.item_code)",
        ),
        (
            get_finishing_checklist_query(),
            "LEFT JOIN jute_prod_finishing_quality fq ON fq.finishing_quality_id = fd.finishing_quality_id",
            "CONCAT_WS(' - ', im.item_name, im.item_code)",
        ),
    ],
)
def test_checklist_sql_group_concats_the_quality(query, join, expr):
    sql = _sql(query)
    assert join in sql
    assert f"GROUP_CONCAT(DISTINCT {expr} SEPARATOR ', ') AS quality" in sql
    # item_mst has no co_id — co-scope comes from item_grp_mst, so the join must
    # not grow a company filter.
    assert "im.co_id" not in sql


def test_finishing_quality_reaches_item_mst_through_the_quality_master():
    sql = _sql(get_finishing_checklist_query())
    assert "LEFT JOIN item_mst im ON im.item_id = fq.item_id" in sql


# TKT-000131: beaming splits code and name into two columns and adds the beam
# count. A machine-spell can run several qualities (sls: 7 of 114 keys), so
# both lists come from a per-quality inner aggregate and share one ORDER BY.
def test_beaming_sql_splits_quality_code_and_name_and_sums_beams():
    sql = _sql(get_beaming_checklist_query())
    assert "LEFT JOIN jute_prod_bm_quality q ON q.bm_quality_id = bd.bm_quality_id" in sql
    assert "SUM(COALESCE(bd.no_of_beam, 0)) AS beams" in sql
    assert "GROUP BY bd.tran_date, bd.spell_id, bd.machine_id, q.bm_quality_code, q.bm_quality_name" in sql
    assert "GROUP_CONCAT(y.bm_quality_code ORDER BY y.bm_quality_code SEPARATOR ', ') AS quality_code" in sql
    assert "GROUP_CONCAT(y.bm_quality_name ORDER BY y.bm_quality_code SEPARATOR ', ') AS quality" in sql
    assert "COALESCE(c.beams, 0) AS beams" in sql


def test_beaming_quality_code_and_beams_survive_the_response_shaping():
    db = SeqDB(map_rows(), [beam_sql_row(quality_code="HW1, HW2", quality="HESSIAN WARP, HESSIAN WEFT", beams=5)])
    out = beaming_checklist(Req(**scope_params(rtype="all")), db=db, token_data=None)
    row = out["data"][0]
    assert row["quality_code"] == "HW1, HW2"
    assert row["quality"] == "HESSIAN WARP, HESSIAN WEFT"
    assert row["beams"] == 5
    # No production side -> NULL lists and a zero beam count, not a crash.
    db = SeqDB(map_rows(), [beam_sql_row(quality_code=None, quality=None, beams=None, cuts=0.0)])
    row = beaming_checklist(Req(**scope_params()), db=db, token_data=None)["data"][0]
    assert row["quality_code"] is None and row["quality"] is None and row["beams"] == 0


@pytest.mark.parametrize(
    "handler, row, expected",
    [
        (winding_checklist, wind_sql_row(), "JUTE YARN 8 LBS - JY8"),
        (beaming_checklist, beam_sql_row(), "HESSIAN WARP"),
        (finishing_checklist, fin_sql_row(), "DW BAG 44X26 - DW4426"),
    ],
)
def test_quality_survives_the_response_shaping(handler, row, expected):
    db = SeqDB(map_rows(1702), [row])
    out = handler(Req(**scope_params(rtype="all")), db=db, token_data=None)
    assert out["data"][0]["quality"] == expected


def test_quality_is_null_when_nothing_was_produced():
    # Attendance-without-production rows come back with a NULL quality (the
    # production aggregate never matched) — shaping must pass the None through.
    db = SeqDB(map_rows(), [wind_sql_row(quality=None)])
    out = winding_checklist(Req(**scope_params()), db=db, token_data=None)
    assert out["data"][0]["quality"] is None


def test_retire_rejected_attendance_is_scoped_like_the_checklists():
    # Every attendance-reading checklist runs this before its select; it must
    # stay inside the same co / branch / date window the grid shows.
    sql = str(get_deactivate_rejected_attendance_query())
    # IGNORE: the unique key includes is_active, so an existing inactive twin
    # must skip the row, not abort the search.
    assert sql.lstrip().startswith("UPDATE IGNORE daily_attendance")
    assert "SET is_active = 0" in sql
    assert "status_id = 4" in sql
    for bind in (":co_id", ":branch_id", ":d_from", ":d_to"):
        assert bind in sql
