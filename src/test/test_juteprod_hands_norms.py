"""Tests for the Hands Norms master (TKT-000202, Hands Norms v2 2026-09-30).

src/juteProduction/hands_norms.py, prefix /api/handsNorms — the page that
maintains jute_prod_hands_norms_mst (and its machine groups), the man-machine
norms the Hands Report reads its targets from.

Portal persona: DB + auth mocked (no real DB), overridden through
app.dependency_overrides keyed by the symbols imported into the router module
(same pattern as test_winding_target_map.py). The menu access level is
patched in the module (as test_spinning_quality_map.py does) — Edit (4) by
default, TestAccess lowers it.

Covered: the save normalisation that keeps a row readable by the report (per
kind F/M/P/N/A: a fixed row loses any scope, an active M/P row needs exactly
one scope and a rate, N/A clear everything, legacy V/F and hands-per-machine
inputs are aliased, the legacy mirrors are rewritten), numeric guards, the
in-force / duplicate marking that mirrors the report's resolution rule,
setup / list / create / edit wiring including the duplicate-date refusal and
the bare retire path, the 403 access gate, and the machine-group endpoints.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.juteProduction.hands_norms import (
    HANDS_NORMS_MENU_PATH,
    get_current_user_with_refresh,
    get_tenant_db,
    mark_in_force,
    normalise_norm,
)
from src.main import app

client = TestClient(app)

MOD = "src.juteProduction.hands_norms"


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _result(rows=None, one=None, lastrowid=None):
    res = MagicMock()
    res.fetchall.return_value = rows or []
    res.fetchone.return_value = one
    res.lastrowid = lastrowid
    return res


def _sql(mock_session, i: int) -> str:
    return str(mock_session.execute.call_args_list[i].args[0])


def _params(mock_session, i: int) -> dict:
    return mock_session.execute.call_args_list[i].args[1]


class _Base:
    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        # Edit level on the Hands Norms menu unless a test says otherwise; the
        # real lookup is one more db.execute that would shift the side_effects.
        self._access = patch(f"{MOD}.user_menu_access_level", return_value=4)
        self.access = self._access.start()

    def teardown_method(self):
        self._access.stop()
        app.dependency_overrides.clear()


# =============================================================================
# normalise_norm — what gets stored
# =============================================================================


class TestNormaliseNorm:
    def test_fixed_row_drops_machine_link(self):
        # The report computes ANY row with a machine_type_id + hands_per_machine as
        # variable, so a fixed row must not keep them.
        out = normalise_norm(
            {
                "variable_fixed": "F",
                "effective_date": date(2026, 9, 1),
                "shift_a": 2,
                "shift_b": 2,
                "shift_c": 1,
                "machine_type_id": 7,
                "hands_per_machine": 0.5,
            }
        )
        assert out["machine_type_id"] is None
        assert out["hands_per_machine"] is None
        assert (out["shift_a"], out["shift_b"], out["shift_c"], out["shift_g"]) == (2, 2, 1, 0)

    def test_variable_row_keeps_link(self):
        out = normalise_norm(
            {
                "variable_fixed": "v",
                "effective_date": date(2026, 9, 1),
                "machine_type_id": 7,
                "hands_per_machine": 0.5,
                "direct_indirect": "direct",
            }
        )
        assert out["variable_fixed"] == "V"
        assert out["machine_type_id"] == 7
        assert out["hands_per_machine"] == 0.5
        assert out["direct_indirect"] == "D"

    @pytest.mark.parametrize(
        "machine_type_id, hands_per_machine, msg",
        [
            (None, 0.5, "machine type"),
            (7, None, "hands per machine"),
            (7, 0, "hands per machine"),
        ],
    )
    def test_active_variable_row_needs_link(self, machine_type_id, hands_per_machine, msg):
        with pytest.raises(HTTPException) as exc:
            normalise_norm(
                {
                    "variable_fixed": "V",
                    "effective_date": date(2026, 9, 1),
                    "machine_type_id": machine_type_id,
                    "hands_per_machine": hands_per_machine,
                }
            )
        assert exc.value.status_code == 400
        assert msg in exc.value.detail

    def test_retired_variable_row_may_stay_unlinked(self):
        out = normalise_norm(
            {"variable_fixed": "V", "effective_date": date(2026, 9, 1), "shift_a": 5},
            strict=False,
        )
        assert out["machine_type_id"] is None
        # v2: only a fixed norm reads shift counts — a machine row stores 0.
        assert out["shift_a"] == 0

    @pytest.mark.parametrize(
        "field, value",
        [
            ("shift_a", -1),
            ("shift_b", float("nan")),
            ("shift_c", float("inf")),
            ("shift_g", 100000),
            ("hands_per_machine", -0.5),
        ],
    )
    def test_numeric_guards(self, field, value):
        row = {
            "variable_fixed": "V",
            "effective_date": date(2026, 9, 1),
            "machine_type_id": 7,
            "hands_per_machine": 1,
        }
        row[field] = value
        with pytest.raises(HTTPException) as exc:
            normalise_norm(row)
        assert exc.value.status_code == 400

    @pytest.mark.parametrize(
        "row, msg",
        [
            ({"effective_date": date(2026, 9, 1)}, "Norm type is required"),
            ({"variable_fixed": "X", "effective_date": date(2026, 9, 1)}, "Norm type"),
            ({"variable_fixed": "F"}, "Effective date"),
            ({"variable_fixed": "F", "effective_date": date(2026, 9, 1), "direct_indirect": "Q"},
             "Direct/Indirect"),
            ({"variable_fixed": "F", "effective_date": date(2026, 9, 1), "norms_text": "x" * 81},
             "80 characters"),
        ],
    )
    def test_enum_date_and_text_guards(self, row, msg):
        with pytest.raises(HTTPException) as exc:
            normalise_norm(row)
        assert msg in exc.value.detail

    # ---- v2 kinds (spec §6.1) ------------------------------------------------

    def test_machine_norm_with_group_clears_machine_type(self):
        out = normalise_norm(
            {
                "norm_kind": "M",
                "effective_date": date(2026, 10, 1),
                "machine_group_id": 3,
                "machine_type_id": None,
                "units_per_hand": 2,
            }
        )
        assert out["norm_kind"] == "M"
        assert out["machine_group_id"] == 3
        assert out["machine_type_id"] is None
        assert out["scope_dept_id"] is None
        assert out["units_per_hand"] == 2

    def test_scope_is_exactly_one_of_group_type_department(self):
        base = {"norm_kind": "M", "effective_date": date(2026, 10, 1), "units_per_hand": 2}
        with pytest.raises(HTTPException) as exc:
            normalise_norm({**base, "machine_group_id": 3, "machine_type_id": 7})
        assert exc.value.status_code == 400
        assert "one scope" in exc.value.detail
        with pytest.raises(HTTPException) as exc:
            normalise_norm({**base, "machine_type_id": 7, "scope_dept_id": 19})
        assert "one scope" in exc.value.detail
        # even a retired row may not carry two scopes
        with pytest.raises(HTTPException):
            normalise_norm({**base, "machine_group_id": 3, "scope_dept_id": 19}, strict=False)
        out = normalise_norm({**base, "scope_dept_id": 19})
        assert (out["machine_group_id"], out["machine_type_id"], out["scope_dept_id"]) == (None, None, 19)

    @pytest.mark.parametrize(
        "row, msg",
        [
            ({"units_per_hand": 4}, "needs a scope"),
            ({"machine_group_id": 3}, "units per hand"),
            ({"machine_group_id": 3, "units_per_hand": 0}, "units per hand"),
            ({"machine_type_id": 7, "hands_per_unit": 0}, "units per hand"),
        ],
    )
    def test_active_machine_norm_needs_group_or_type_and_units_per_hand(self, row, msg):
        with pytest.raises(HTTPException) as exc:
            normalise_norm({"norm_kind": "M", "effective_date": date(2026, 10, 1), **row})
        assert exc.value.status_code == 400
        assert msg in exc.value.detail
        # a retired row may be incomplete (switch a legacy row off as it is)
        out = normalise_norm(
            {"norm_kind": "M", "effective_date": date(2026, 10, 1), **row}, strict=False
        )
        assert out["norm_kind"] == "M"

    def test_hands_per_unit_input_is_stored_as_units_per_hand(self):
        out = normalise_norm(
            {
                "norm_kind": "M",
                "effective_date": date(2026, 10, 1),
                "machine_group_id": 3,
                "hands_per_unit": 0.25,  # 1 hand per 4 units
            }
        )
        assert out["units_per_hand"] == 4
        assert out["hands_per_machine"] == 0.25
        # units_per_hand sent explicitly wins over the alias
        out = normalise_norm(
            {
                "norm_kind": "M",
                "effective_date": date(2026, 10, 1),
                "machine_type_id": 7,
                "units_per_hand": 2,
                "hands_per_unit": 0.25,
            }
        )
        assert out["units_per_hand"] == 2
        assert out["hands_per_machine"] == 0.5

    @pytest.mark.parametrize(
        "row, msg",
        [
            ({"qty_per_hand": 315, "qty_uom": "kg"}, "needs a scope"),
            ({"scope_dept_id": 19, "qty_uom": "kg"}, "qty per hand"),
            ({"scope_dept_id": 19, "qty_per_hand": 0, "qty_uom": "kg"}, "qty per hand"),
            ({"scope_dept_id": 19, "qty_per_hand": 315}, "needs a unit"),
            ({"scope_dept_id": 19, "qty_per_hand": 315, "qty_uom": "tons"}, "Unit must be one of"),
        ],
    )
    def test_production_norm_needs_qty_per_hand_and_uom(self, row, msg):
        with pytest.raises(HTTPException) as exc:
            normalise_norm({"norm_kind": "P", "effective_date": date(2026, 10, 1), **row})
        assert exc.value.status_code == 400
        assert msg in exc.value.detail

    def test_production_norm_stores_scope_qty_and_uom_and_no_units(self):
        out = normalise_norm(
            {
                "norm_kind": "P",
                "effective_date": date(2026, 10, 1),
                "scope_dept_id": 19,
                "qty_per_hand": 315,
                "qty_uom": "KG",
                "units_per_hand": 2,  # not a P field
                "shift_a": 3,
            }
        )
        assert out["norm_kind"] == "P"
        assert out["variable_fixed"] == "V"
        assert out["scope_dept_id"] == 19
        assert out["qty_per_hand"] == 315
        assert out["qty_uom"] == "kg"
        assert out["units_per_hand"] is None
        assert out["hands_per_machine"] is None
        assert out["shift_a"] == 0

    @pytest.mark.parametrize("kind", ["N", "A"])
    def test_no_norm_and_as_actual_clear_shifts_links_and_rates(self, kind):
        out = normalise_norm(
            {
                "norm_kind": kind,
                "effective_date": date(2026, 10, 1),
                "shift_a": 2,
                "shift_b": 2,
                "machine_group_id": 3,
                "machine_type_id": 7,
                "scope_dept_id": 19,
                "units_per_hand": 2,
                "qty_per_hand": 315,
                "qty_uom": "kg",
                "hands_per_machine": 0.5,
                "norms_text": "NO NORMS",
            }
        )
        assert out["norm_kind"] == kind
        assert out["variable_fixed"] == "F"
        assert (out["shift_a"], out["shift_b"], out["shift_c"], out["shift_g"]) == (0, 0, 0, 0)
        for f in ("machine_group_id", "machine_type_id", "scope_dept_id",
                  "units_per_hand", "qty_per_hand", "qty_uom", "hands_per_machine"):
            assert out[f] is None, f
        assert out["norms_text"] == "NO NORMS"

    def test_legacy_variable_fixed_body_maps_to_kind(self):
        v = normalise_norm(
            {"variable_fixed": "V", "effective_date": date(2026, 10, 1),
             "machine_type_id": 7, "hands_per_machine": 0.5}
        )
        assert v["norm_kind"] == "M"
        f = normalise_norm({"variable_fixed": "F", "effective_date": date(2026, 10, 1), "shift_a": 1})
        assert f["norm_kind"] == "F"
        # norm_kind wins when both are sent
        both = normalise_norm(
            {"variable_fixed": "V", "norm_kind": "N", "effective_date": date(2026, 10, 1)}
        )
        assert both["norm_kind"] == "N"
        with pytest.raises(HTTPException) as exc:
            normalise_norm({"norm_kind": "X", "effective_date": date(2026, 10, 1)})
        assert "F, M, P, N, A" in exc.value.detail

    def test_variable_fixed_and_hands_per_machine_mirrors_are_written(self):
        m = normalise_norm(
            {"norm_kind": "M", "effective_date": date(2026, 10, 1),
             "machine_group_id": 3, "units_per_hand": 4}
        )
        assert m["variable_fixed"] == "V"
        assert m["hands_per_machine"] == 0.25
        m3 = normalise_norm(
            {"norm_kind": "M", "effective_date": date(2026, 10, 1),
             "machine_group_id": 3, "units_per_hand": 3}
        )
        assert m3["hands_per_machine"] == 0.3333
        p = normalise_norm(
            {"norm_kind": "P", "effective_date": date(2026, 10, 1),
             "machine_group_id": 3, "qty_per_hand": 100, "qty_uom": "cuts"}
        )
        assert p["variable_fixed"] == "V"
        assert p["hands_per_machine"] is None
        f = normalise_norm({"norm_kind": "F", "effective_date": date(2026, 10, 1), "shift_a": 1})
        assert f["variable_fixed"] == "F"
        assert f["hands_per_machine"] is None
        for kind in ("N", "A"):
            assert normalise_norm({"norm_kind": kind, "effective_date": date(2026, 10, 1)})[
                "variable_fixed"] == "F"


# =============================================================================
# mark_in_force — same resolution rule as the Hands Report
# =============================================================================


def _row(i, desig, eff, branch=29, active=1):
    return {
        "hands_norm_id": i,
        "designation_id": desig,
        "effective_date": eff,
        "branch_id": branch,
        "active": active,
    }


class TestMarkInForce:
    def test_latest_effective_date_not_after_today_wins(self):
        rows = [
            _row(1, 10, date(2000, 1, 1)),
            _row(2, 10, date(2026, 9, 1)),
            _row(3, 10, date(2026, 12, 1)),  # future revision: not yet in force
        ]
        mark_in_force(rows, date(2026, 9, 30))
        assert [r["in_force"] for r in rows] == [False, True, False]

    def test_branch_row_beats_co_wide_row_on_same_date(self):
        rows = [_row(5, 10, date(2026, 1, 1), branch=29), _row(9, 10, date(2026, 1, 1), branch=None)]
        mark_in_force(rows, date(2026, 9, 30))
        assert rows[0]["in_force"] is True
        assert rows[1]["in_force"] is False

    def test_inactive_rows_never_in_force(self):
        rows = [_row(1, 10, date(2000, 1, 1)), _row(2, 10, date(2026, 1, 1), active=0)]
        mark_in_force(rows, date(2026, 9, 30))
        assert rows[0]["in_force"] is True
        assert rows[1]["in_force"] is False

    def test_same_date_duplicates_flagged_newest_id_wins(self):
        # sls carries such pairs from the legacy import (e.g. OILER, 2000-01-01).
        rows = [_row(17, 190, date(2000, 1, 1)), _row(199, 190, date(2000, 1, 1))]
        mark_in_force(rows, date(2026, 9, 30))
        assert [r["duplicate"] for r in rows] == [True, True]
        assert [r["in_force"] for r in rows] == [False, True]


# =============================================================================
# Endpoints
# =============================================================================


def _group_row(**over):
    base = {
        "machine_group_id": 3, "co_id": 2, "branch_id": 29, "group_code": "HESS",
        "group_name": "Hessian looms", "unit_label": "machine", "remarks": None,
        "active": 1, "member_count": 200, "total_units": 200, "norms_using": 1,
    }
    base.update(over)
    return _mock_row(base)


def _setup_results():
    """The five /setup queries in order: designations, machine types, groups,
    machines, departments."""
    return [
        _result(rows=[_mock_row({"designation_id": 4, "desig": "SELECTOR MALE", "dept_id": 14,
                                 "dept": "SELECTION", "direct_indirect": "D",
                                 "branch_id": 29, "branch_name": "FACTORY"})]),
        _result(rows=[_mock_row({"machine_type_id": 7, "machine_type_name": "Loom",
                                 "machine_count": 334})]),
        _result(rows=[_group_row()]),
        _result(rows=[_mock_row({"machine_id": 501, "machine_name": "Hessian Loom - 1",
                                 "mech_code": "HL1", "machine_type_id": 7,
                                 "machine_type_name": "Loom", "dept_id": 21, "dept": "WEAVING"})]),
        _result(rows=[_mock_row({"dept_id": 19, "dept_desc": "SPOOL WINDING"})]),
    ]


class TestSetup(_Base):
    def test_requires_branch(self):
        resp = client.get("/api/handsNorms/setup?co_id=2")
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_returns_designations_and_machine_types(self):
        self.db.execute.side_effect = _setup_results()
        resp = client.get("/api/handsNorms/setup?co_id=2&branch_id=29")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["designations"][0]["desig"] == "SELECTOR MALE"
        assert data["machine_types"] == [
            {"machine_type_id": 7, "machine_type_name": "Loom", "machine_count": 334}
        ]
        assert _params(self.db, 0) == {"co_id": 2, "branch_id": 29}
        assert _params(self.db, 1) == {"branch_id": 29}


def _list_row(**over):
    base = {
        "hands_norm_id": 130, "co_id": 2, "branch_id": 29, "designation_id": 20,
        "designation": "HARD WASTE TEASER FEEDER", "dept_id": 15, "dept": "BATCHING",
        "direct_indirect": "D", "variable_fixed": "V",
        "shift_a": 0, "shift_b": 0, "shift_c": 0, "shift_g": 0,
        "machine_type_id": None, "machine_type_name": None,
        "hands_per_machine": 2, "norms_text": "2 HANDS  PER MC",
        "effective_date": date(2000, 1, 1), "active": 1,
    }
    base.update(over)
    return _mock_row(base)


class TestList(_Base):
    def test_flags_unlinked_variable_norm_as_not_computable(self):
        self.db.execute.return_value = _result(
            rows=[_list_row(), _list_row(hands_norm_id=131, designation_id=21,
                                         variable_fixed="F", shift_a=1, hands_per_machine=None)]
        )
        resp = client.get("/api/handsNorms/list?co_id=2&branch_id=29")
        assert resp.status_code == 200
        rows = resp.json()["data"]
        assert rows[0]["computable"] is False
        assert rows[0]["in_force"] is True
        assert rows[0]["effective_date"] == "2000-01-01"
        assert rows[1]["computable"] is True
        assert _params(self.db, 0) == {"co_id": 2, "branch_id": 29, "include_inactive": 0}

    def test_fixed_row_with_rate_but_no_machine_type_is_not_computable(self):
        # sls's seed: 47 rows flagged 'F', zero shift counts, hands_per_machine
        # set ('2 MC = 1 HAND' -> 0.5) and no machine type. The report cannot
        # count machines for them, so the page must list them as needing a link.
        self.db.execute.return_value = _result(
            rows=[
                _list_row(variable_fixed="F", hands_per_machine=0.5, norms_text="2 MC = 1 HAND"),
                _list_row(hands_norm_id=132, designation_id=22, variable_fixed="F",
                          hands_per_machine=0.5, shift_a=2),  # explicit shifts: fixed wins
                _list_row(hands_norm_id=133, designation_id=23, variable_fixed="F",
                          hands_per_machine=0.5, machine_type_id=25),  # linked
            ]
        )
        rows = client.get("/api/handsNorms/list?co_id=2&branch_id=29").json()["data"]
        assert [r["computable"] for r in rows] == [False, True, True]

    def test_include_inactive_flag(self):
        self.db.execute.return_value = _result(rows=[])
        client.get("/api/handsNorms/list?co_id=2&branch_id=29&include_inactive=1")
        assert _params(self.db, 0)["include_inactive"] == 1

    def test_v2_rows_serialise_kind_scope_and_computable_per_kind(self):
        self.db.execute.return_value = _result(
            rows=[
                # complete M on a group
                _list_row(hands_norm_id=1, designation_id=1, variable_fixed="V", norm_kind="M",
                          machine_group_id=3, machine_group="Hessian looms", unit_label="machine",
                          hands_per_machine=0.5, units_per_hand=2),
                # incomplete M from the migration ('4 POINTS', no scope, no rate)
                _list_row(hands_norm_id=2, designation_id=2, variable_fixed="F", norm_kind="M",
                          hands_per_machine=None, norms_text="4 POINTS"),
                # complete P on a department
                _list_row(hands_norm_id=3, designation_id=3, variable_fixed="V", norm_kind="P",
                          hands_per_machine=None, scope_dept_id=19, scope_dept="SPOOL WINDING",
                          qty_per_hand=315, qty_uom="kg"),
                # incomplete P (scope but no qty)
                _list_row(hands_norm_id=4, designation_id=4, variable_fixed="F", norm_kind="P",
                          hands_per_machine=None, scope_dept_id=19, qty_uom="kg"),
                # N and A always compute (blank / as actual)
                _list_row(hands_norm_id=5, designation_id=5, variable_fixed="F", norm_kind="N",
                          hands_per_machine=None),
                _list_row(hands_norm_id=6, designation_id=6, variable_fixed="F", norm_kind="A",
                          hands_per_machine=None),
            ]
        )
        rows = client.get("/api/handsNorms/list?co_id=2&branch_id=29").json()["data"]
        assert [r["norm_kind"] for r in rows] == ["M", "M", "P", "P", "N", "A"]
        assert [r["computable"] for r in rows] == [True, False, True, False, True, True]
        assert rows[0]["machine_group"] == "Hessian looms"
        assert rows[0]["unit_label"] == "machine"
        assert rows[0]["units_per_hand"] == 2.0
        assert rows[2]["scope_dept"] == "SPOOL WINDING"
        assert rows[2]["qty_per_hand"] == 315.0
        assert rows[2]["qty_uom"] == "kg"
        for key in ("norm_kind", "machine_group_id", "machine_group", "unit_label",
                    "scope_dept_id", "scope_dept", "units_per_hand", "qty_per_hand", "qty_uom"):
            assert key in rows[4]


_CREATE = {
    "co_id": 2,
    "branch_id": 29,
    "designation_id": 20,
    "effective_date": "2026-10-01",
    "variable_fixed": "V",
    "machine_type_id": 12,
    "hands_per_machine": 2,
    "norms_text": "2 HANDS PER MC",
}


class TestCreate(_Base):
    def test_inserts_variable_norm_with_designation_default_di(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),  # designation
            _result(one=_mock_row({"machine_type_id": 12})),  # machine type exists
            _result(one=None),  # no duplicate
            _result(lastrowid=900),  # insert
        ]
        resp = client.post("/api/handsNorms/create", json=_CREATE)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["hands_norm_id"] == 900
        ins = _params(self.db, 3)
        assert ins["direct_indirect"] == "D"
        assert ins["variable_fixed"] == "V"
        assert ins["machine_type_id"] == 12
        assert ins["hands_per_machine"] == 2.0
        assert ins["branch_id"] == 29
        assert ins["effective_date"] == date(2026, 10, 1)
        assert "INSERT INTO jute_prod_hands_norms_mst" in _sql(self.db, 3)
        self.db.commit.assert_called_once()

    def test_designation_outside_company_is_400(self):
        self.db.execute.side_effect = [_result(one=None)]
        resp = client.post("/api/handsNorms/create", json=_CREATE)
        assert resp.status_code == 400
        assert "Designation" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_variable_without_machine_type_is_400(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),
        ]
        resp = client.post("/api/handsNorms/create", json={**_CREATE, "machine_type_id": None})
        assert resp.status_code == 400
        assert "machine type" in resp.json()["detail"]

    def test_duplicate_effective_date_is_400(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),
            _result(one=_mock_row({"machine_type_id": 12})),
            _result(one=_mock_row({"hands_norm_id": 130})),  # clash
        ]
        resp = client.post("/api/handsNorms/create", json=_CREATE)
        assert resp.status_code == 400
        assert "already starts on 2026-10-01" in resp.json()["detail"]
        self.db.rollback.assert_called()
        self.db.commit.assert_not_called()

    def test_inserts_group_scoped_machine_norm_with_v2_columns(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),
            _result(one=_mock_row({"machine_group_id": 3, "branch_id": 29})),  # group of the branch
            _result(one=None),  # no duplicate
            _result(lastrowid=901),
        ]
        body = {
            "co_id": 2, "branch_id": 29, "designation_id": 20, "effective_date": "2026-10-01",
            "norm_kind": "M", "machine_group_id": 3, "units_per_hand": 2,
            "norms_text": "2 MC = 1 HAND",
        }
        resp = client.post("/api/handsNorms/create", json=body)
        assert resp.status_code == 200, resp.text
        assert "jute_prod_machine_group_mst" in _sql(self.db, 1)
        assert _params(self.db, 1) == {"co_id": 2, "machine_group_id": 3}
        ins = _params(self.db, 3)
        assert ins["norm_kind"] == "M"
        assert ins["variable_fixed"] == "V"
        assert ins["machine_group_id"] == 3
        assert ins["machine_type_id"] is None
        assert ins["scope_dept_id"] is None
        assert ins["units_per_hand"] == 2.0
        assert ins["hands_per_machine"] == 0.5
        assert ins["qty_per_hand"] is None and ins["qty_uom"] is None
        assert "norm_kind" in _sql(self.db, 3) and ":units_per_hand" in _sql(self.db, 3)

    def test_group_of_another_branch_is_400(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),
            _result(one=_mock_row({"machine_group_id": 3, "branch_id": 30})),  # other branch
        ]
        body = {
            "co_id": 2, "branch_id": 29, "designation_id": 20, "effective_date": "2026-10-01",
            "norm_kind": "M", "machine_group_id": 3, "units_per_hand": 2,
        }
        resp = client.post("/api/handsNorms/create", json=body)
        assert resp.status_code == 400
        assert "another branch" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_unknown_group_is_400(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),
            _result(one=None),  # no active group of this co
        ]
        body = {
            "co_id": 2, "branch_id": 29, "designation_id": 20, "effective_date": "2026-10-01",
            "norm_kind": "M", "machine_group_id": 99, "units_per_hand": 2,
        }
        resp = client.post("/api/handsNorms/create", json=body)
        assert resp.status_code == 400
        assert "Machine group not found" in resp.json()["detail"]

    def test_department_scope_must_belong_to_the_branch(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),
            _result(one=None),  # dept lookup: not in branch 29
        ]
        body = {
            "co_id": 2, "branch_id": 29, "designation_id": 20, "effective_date": "2026-10-01",
            "norm_kind": "P", "scope_dept_id": 19, "qty_per_hand": 315, "qty_uom": "kg",
        }
        resp = client.post("/api/handsNorms/create", json=body)
        assert resp.status_code == 400
        assert "Department not found" in resp.json()["detail"]
        assert _params(self.db, 1) == {"co_id": 2, "branch_id": 29, "dept_id": 19}
        assert "dept_mst" in _sql(self.db, 1)

    def test_inserts_department_scoped_production_norm(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),
            _result(one=_mock_row({"dept_id": 19})),
            _result(one=None),
            _result(lastrowid=902),
        ]
        body = {
            "co_id": 2, "branch_id": 29, "designation_id": 20, "effective_date": "2026-10-01",
            "norm_kind": "P", "scope_dept_id": 19, "qty_per_hand": 315, "qty_uom": "kg",
            "norms_text": "315 KGS / WINDER",
        }
        resp = client.post("/api/handsNorms/create", json=body)
        assert resp.status_code == 200, resp.text
        ins = _params(self.db, 3)
        assert ins["norm_kind"] == "P"
        assert ins["scope_dept_id"] == 19
        assert ins["qty_per_hand"] == 315.0
        assert ins["qty_uom"] == "kg"
        assert ins["units_per_hand"] is None and ins["hands_per_machine"] is None
        assert (ins["shift_a"], ins["shift_b"], ins["shift_c"], ins["shift_g"]) == (0, 0, 0, 0)

    def test_no_norm_kind_needs_nothing_else(self):
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "I"})),
            _result(one=None),  # duplicate check (no scope lookups for N)
            _result(lastrowid=903),
        ]
        body = {"co_id": 2, "branch_id": 29, "designation_id": 20,
                "effective_date": "2026-10-01", "norm_kind": "N", "norms_text": "NO NORMS"}
        resp = client.post("/api/handsNorms/create", json=body)
        assert resp.status_code == 200, resp.text
        ins = _params(self.db, 2)
        assert ins["norm_kind"] == "N"
        assert ins["variable_fixed"] == "F"
        assert ins["direct_indirect"] == "I"
        assert "INSERT INTO jute_prod_hands_norms_mst" in _sql(self.db, 2)


def _existing(**over):
    base = {
        "hands_norm_id": 113, "co_id": 2, "branch_id": 29, "designation_id": 2,
        "direct_indirect": "I", "variable_fixed": "V",
        "shift_a": 5, "shift_b": 0, "shift_c": 0, "shift_g": 0,
        "machine_type_id": None, "hands_per_machine": None,
        "norms_text": "10 MT / HAND, 5 IN A", "effective_date": date(2000, 1, 1), "active": 1,
    }
    base.update(over)
    return _mock_row(base)


class TestEdit(_Base):
    def test_links_legacy_variable_norm_to_machine_type(self):
        self.db.execute.side_effect = [
            _result(one=_existing()),
            _result(one=_mock_row({"machine_type_id": 31})),  # machine type exists
            _result(one=None),  # no duplicate
            _result(),  # update
        ]
        resp = client.put(
            "/api/handsNorms/edit/113?co_id=2",
            json={"machine_type_id": 31, "hands_per_machine": 1.5},
        )
        assert resp.status_code == 200, resp.text
        upd = _params(self.db, 3)
        assert upd["machine_type_id"] == 31
        assert upd["hands_per_machine"] == 1.5
        assert upd["units_per_hand"] == 0.667  # legacy input converted (1 / 1.5)
        assert upd["variable_fixed"] == "V"
        assert upd["norm_kind"] == "M"
        assert upd["shift_a"] == 0  # v2: a machine row carries no shift counts
        assert upd["norms_text"] == "10 MT / HAND, 5 IN A"  # untouched fields keep their stored value
        assert upd["active"] == 1
        assert "UPDATE jute_prod_hands_norms_mst" in _sql(self.db, 3)

    def test_switching_to_fixed_clears_link(self):
        self.db.execute.side_effect = [
            _result(one=_existing(machine_type_id=31, hands_per_machine=1.5)),
            _result(one=None),  # no duplicate (no machine-type lookup: link cleared)
            _result(),
        ]
        resp = client.put(
            "/api/handsNorms/edit/113?co_id=2",
            json={"variable_fixed": "F", "shift_a": 5},
        )
        assert resp.status_code == 200, resp.text
        upd = _params(self.db, 2)
        assert upd["variable_fixed"] == "F"
        assert upd["machine_type_id"] is None
        assert upd["hands_per_machine"] is None

    def test_other_company_row_is_404(self):
        self.db.execute.side_effect = [_result(one=_existing(co_id=1))]
        resp = client.put("/api/handsNorms/edit/113?co_id=2", json={"active": 0})
        assert resp.status_code == 404

    def test_bare_retire_touches_only_active(self):
        # A legacy unlinked variable row can be switched off without being
        # "fixed" first, and nothing else about it is rewritten.
        self.db.execute.side_effect = [_result(one=_existing()), _result()]
        resp = client.put("/api/handsNorms/edit/113?co_id=2", json={"active": 0})
        assert resp.status_code == 200, resp.text
        assert "SET active = :active" in _sql(self.db, 1)
        assert _params(self.db, 1) == {"id": 113, "co_id": 2, "active": 0}

    def test_active_row_edit_still_needs_link(self):
        self.db.execute.side_effect = [_result(one=_existing())]
        resp = client.put("/api/handsNorms/edit/113?co_id=2", json={"norms_text": "10 MT / HAND"})
        assert resp.status_code == 400
        assert "machine type" in resp.json()["detail"]

    def test_requires_co_id(self):
        resp = client.put("/api/handsNorms/edit/113", json={"active": 0})
        assert resp.status_code == 400

    def test_picking_a_group_clears_the_stored_machine_type(self):
        # The dialog's scope radio sends the scope it picked; the stored other
        # scope must not survive the merge.
        self.db.execute.side_effect = [
            _result(one=_existing(norm_kind="M", machine_type_id=7, hands_per_machine=0.5,
                                  units_per_hand=2, shift_a=0)),
            _result(one=_mock_row({"machine_group_id": 3, "branch_id": 29})),  # group check
            _result(one=None),  # no duplicate
            _result(),
        ]
        resp = client.put("/api/handsNorms/edit/113?co_id=2", json={"machine_group_id": 3})
        assert resp.status_code == 200, resp.text
        upd = _params(self.db, 3)
        assert upd["machine_group_id"] == 3
        assert upd["machine_type_id"] is None
        assert upd["units_per_hand"] == 2.0  # stored rate kept

    def test_hands_per_unit_on_edit_replaces_stored_units(self):
        self.db.execute.side_effect = [
            _result(one=_existing(norm_kind="M", machine_type_id=7, hands_per_machine=0.5,
                                  units_per_hand=2, shift_a=0)),
            _result(one=_mock_row({"machine_type_id": 7})),
            _result(one=None),
            _result(),
        ]
        resp = client.put("/api/handsNorms/edit/113?co_id=2", json={"hands_per_unit": 0.25})
        assert resp.status_code == 200, resp.text
        upd = _params(self.db, 3)
        assert upd["units_per_hand"] == 4.0
        assert upd["hands_per_machine"] == 0.25

    def test_completing_a_migrated_production_norm(self):
        # '315 KGS / WINDER' landed as an incomplete P row (qty_uom kg, nothing
        # else); the owner picks the department and types the rate.
        self.db.execute.side_effect = [
            _result(one=_existing(norm_kind="P", variable_fixed="F", shift_a=0,
                                  qty_uom="kg", norms_text="315 KGS / WINDER")),
            _result(one=_mock_row({"dept_id": 19})),
            _result(one=None),
            _result(),
        ]
        resp = client.put(
            "/api/handsNorms/edit/113?co_id=2",
            json={"scope_dept_id": 19, "qty_per_hand": 315},
        )
        assert resp.status_code == 200, resp.text
        upd = _params(self.db, 3)
        assert upd["norm_kind"] == "P"
        assert upd["variable_fixed"] == "V"
        assert upd["scope_dept_id"] == 19
        assert upd["qty_per_hand"] == 315.0
        assert upd["qty_uom"] == "kg"


# =============================================================================
# Access — §6.3: writes need the level on the page's own menu row
# =============================================================================

_GROUP_CREATE = {
    "co_id": 2, "branch_id": 29, "group_code": "HESS", "group_name": "Hessian looms",
    "unit_label": "machine", "remarks": None,
    "members": [{"machine_id": 501, "units": 1}, {"machine_id": 502, "units": 1}],
}


class TestAccess(_Base):
    def test_view_role_gets_403_on_norm_create_edit_and_group_writes(self):
        self.access.return_value = 1
        for method, url, body in (
            ("post", "/api/handsNorms/create", _CREATE),
            ("put", "/api/handsNorms/edit/113?co_id=2", {"active": 0}),
            ("post", "/api/handsNorms/groups/create", _GROUP_CREATE),
            ("put", "/api/handsNorms/groups/edit/3?co_id=2", {"group_name": "x"}),
        ):
            resp = getattr(client, method)(url, json=body)
            assert resp.status_code == 403, (url, resp.text)
            assert resp.json()["detail"] == "This role can view hands norms but not change them"
        self.db.execute.assert_not_called()  # refused before any query
        self.db.commit.assert_not_called()

    def test_write_role_can_create_but_not_edit(self):
        self.access.return_value = 3
        self.db.execute.side_effect = [
            _result(one=_mock_row({"designation_id": 20, "direct_indirect": "D"})),
            _result(one=_mock_row({"machine_type_id": 12})),
            _result(one=None),
            _result(lastrowid=900),
        ]
        assert client.post("/api/handsNorms/create", json=_CREATE).status_code == 200
        assert client.put("/api/handsNorms/edit/113?co_id=2", json={"active": 0}).status_code == 403
        self.db.execute.side_effect = [
            _result(one=None),  # no duplicate code
            _result(rows=[_mock_row({"machine_id": 501}), _mock_row({"machine_id": 502})]),
            _result(lastrowid=3),
            _result(), _result(),
        ]
        assert client.post("/api/handsNorms/groups/create", json=_GROUP_CREATE).status_code == 200
        assert client.put(
            "/api/handsNorms/groups/edit/3?co_id=2", json={"group_name": "x"}
        ).status_code == 403

    def test_setup_and_list_stay_open_to_view_role(self):
        self.access.return_value = 1
        self.db.execute.side_effect = _setup_results()
        assert client.get("/api/handsNorms/setup?co_id=2&branch_id=29").status_code == 200
        self.db.execute.side_effect = None
        self.db.execute.return_value = _result(rows=[_list_row()])
        assert client.get("/api/handsNorms/list?co_id=2&branch_id=29").status_code == 200
        self.db.execute.return_value = _result(rows=[_group_row()])
        assert client.get("/api/handsNorms/groups?co_id=2&branch_id=29").status_code == 200
        self.db.execute.side_effect = [_result(one=_group_row()), _result(rows=[]), _result(rows=[])]
        assert client.get("/api/handsNorms/groups/3?co_id=2").status_code == 200
        self.access.assert_not_called()

    def test_access_check_uses_the_hands_norms_menu_path(self):
        assert HANDS_NORMS_MENU_PATH == "juteProduction/masters/handsNorms"
        self.access.return_value = 0
        client.post("/api/handsNorms/create", json=_CREATE)
        self.access.assert_called_once_with(self.db, 1, 2, 29, "juteProduction/masters/handsNorms")
        self.access.reset_mock()
        client.put("/api/handsNorms/edit/113?co_id=2&branch_id=29", json={"active": 0})
        self.access.assert_called_once_with(self.db, 1, 2, 29, "juteProduction/masters/handsNorms")
        self.access.reset_mock()
        # without a branch on the edit URL the user's best grant in the co counts
        client.put("/api/handsNorms/groups/edit/3?co_id=2", json={"group_name": "x"})
        self.access.assert_called_once_with(self.db, 1, 2, None, "juteProduction/masters/handsNorms")


# =============================================================================
# Machine groups — §6.2
# =============================================================================


def _member_row(machine_id, units=1, **over):
    base = {
        "machine_group_dtl_id": 1000 + machine_id, "machine_id": machine_id,
        "machine_name": f"Hessian Loom - {machine_id}", "mech_code": f"HL{machine_id}",
        "machine_type_id": 7, "machine_type_name": "Loom", "dept_id": 21, "dept": "WEAVING",
        "units": units,
    }
    base.update(over)
    return _mock_row(base)


class TestGroups(_Base):
    def test_create_group_with_members_and_units(self):
        self.db.execute.side_effect = [
            _result(one=None),  # no duplicate code
            _result(rows=[_mock_row({"machine_id": 501}), _mock_row({"machine_id": 502})]),  # in branch
            _result(lastrowid=3),  # header insert
            _result(),  # member 501
            _result(),  # member 502
        ]
        body = {**_GROUP_CREATE, "members": [{"machine_id": 501, "units": 1},
                                              {"machine_id": 502, "units": 10}]}
        resp = client.post("/api/handsNorms/groups/create", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"data": {"machine_group_id": 3}}
        assert _params(self.db, 0) == {"co_id": 2, "branch_id": 29, "group_code": "HESS", "exclude_id": None}
        assert _params(self.db, 1) == {"branch_id": 29, "machine_ids": [501, 502]}
        hdr = _params(self.db, 2)
        assert "INSERT INTO jute_prod_machine_group_mst" in _sql(self.db, 2)
        assert hdr["co_id"] == 2 and hdr["branch_id"] == 29
        assert hdr["group_code"] == "HESS" and hdr["group_name"] == "Hessian looms"
        assert hdr["unit_label"] == "machine" and hdr["updated_by"] == 1
        assert "INSERT INTO jute_prod_machine_group_dtl" in _sql(self.db, 3)
        assert _params(self.db, 3) == {"machine_group_id": 3, "machine_id": 501, "units": 1.0}
        assert _params(self.db, 4) == {"machine_group_id": 3, "machine_id": 502, "units": 10.0}
        self.db.commit.assert_called_once()

    def test_unit_label_defaults_and_code_name_required(self):
        # no members -> no branch lookup: dup-code check, then the header insert
        self.db.execute.side_effect = [_result(one=None), _result(lastrowid=4)]
        body = {**_GROUP_CREATE, "unit_label": "", "members": []}
        assert client.post("/api/handsNorms/groups/create", json=body).status_code == 200
        assert _params(self.db, 1)["unit_label"] == "machine"
        for bad, msg in (({"group_code": " "}, "Group code is required"),
                         ({"group_name": ""}, "Group name is required"),
                         ({"group_code": "x" * 31}, "30 characters")):
            resp = client.post("/api/handsNorms/groups/create", json={**_GROUP_CREATE, **bad})
            assert resp.status_code == 400
            assert msg in resp.json()["detail"]

    def test_member_outside_branch_is_400(self):
        self.db.execute.side_effect = [
            _result(one=None),
            _result(rows=[_mock_row({"machine_id": 501})]),  # 502 is not in branch 29 / inactive
        ]
        resp = client.post("/api/handsNorms/groups/create", json=_GROUP_CREATE)
        assert resp.status_code == 400
        assert "not active machines of this branch: 502" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    @pytest.mark.parametrize(
        "members, msg",
        [
            ([{"machine_id": 501}, {"machine_id": 501, "units": 2}], "more than once"),
            ([{"machine_id": 501, "units": 0}], "greater than 0"),
            ([{"machine_id": 501, "units": -1}], "negative"),
            ([{"machine_id": 501, "units": 1e7}], "cannot exceed"),  # DECIMAL(9,3)
        ],
    )
    def test_duplicate_member_or_zero_units_is_400(self, members, msg):
        resp = client.post("/api/handsNorms/groups/create", json={**_GROUP_CREATE, "members": members})
        assert resp.status_code == 400, resp.text
        assert msg in resp.json()["detail"]
        self.db.execute.assert_not_called()

    def test_duplicate_active_group_code_is_400(self):
        self.db.execute.side_effect = [_result(one=_mock_row({"machine_group_id": 2}))]
        resp = client.post("/api/handsNorms/groups/create", json=_GROUP_CREATE)
        assert resp.status_code == 400
        assert "code 'HESS' already exists" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_edit_replaces_members_soft_deleting_removed_ones(self):
        self.db.execute.side_effect = [
            _result(one=_group_row()),  # header
            _result(),  # header update
            _result(rows=[_mock_row({"machine_id": 502}), _mock_row({"machine_id": 503})]),  # in branch
            _result(rows=[_mock_row({"machine_group_dtl_id": 11, "machine_id": 501, "units": 1}),
                          _mock_row({"machine_group_dtl_id": 12, "machine_id": 502, "units": 1})]),
            _result(),  # retire 501
            _result(),  # units 502 -> 2
            _result(),  # insert 503
        ]
        resp = client.put(
            "/api/handsNorms/groups/edit/3?co_id=2",
            json={"group_name": "Hessian looms (all)",
                  "members": [{"machine_id": 502, "units": 2}, {"machine_id": 503, "units": 1}]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"data": {"machine_group_id": 3}}
        hdr = _params(self.db, 1)
        assert "UPDATE jute_prod_machine_group_mst" in _sql(self.db, 1)
        assert hdr["group_name"] == "Hessian looms (all)"
        assert hdr["group_code"] == "HESS" and hdr["active"] == 1  # untouched fields kept
        assert _params(self.db, 2) == {"branch_id": 29, "machine_ids": [502, 503]}
        assert "SET active = 0" in _sql(self.db, 4)
        assert _params(self.db, 4) == {"ids": [11]}
        assert "SET units = :units" in _sql(self.db, 5)
        assert _params(self.db, 5) == {"machine_group_dtl_id": 12, "units": 2.0}
        assert "INSERT INTO jute_prod_machine_group_dtl" in _sql(self.db, 6)
        assert _params(self.db, 6) == {"machine_group_id": 3, "machine_id": 503, "units": 1.0}
        self.db.commit.assert_called_once()

    def test_edit_without_members_leaves_them_alone(self):
        self.db.execute.side_effect = [_result(one=_group_row()), _result()]
        resp = client.put("/api/handsNorms/groups/edit/3?co_id=2", json={"remarks": "all 200"})
        assert resp.status_code == 200, resp.text
        assert self.db.execute.call_count == 2
        assert _params(self.db, 1)["remarks"] == "all 200"

    def test_retiring_group_used_by_an_active_norm_is_400(self):
        self.db.execute.side_effect = [
            _result(one=_group_row()),
            _result(rows=[_mock_row({"hands_norm_id": 7, "desig": "WEAVER(H)"}),
                          _mock_row({"hands_norm_id": 9, "desig": "WEAVER HELPER"})]),
        ]
        resp = client.put("/api/handsNorms/groups/edit/3?co_id=2", json={"active": 0})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "2 active norm(s) use this group: WEAVER(H), WEAVER HELPER"
        self.db.commit.assert_not_called()
        self.db.rollback.assert_called()

    def test_retiring_unused_group_and_other_company_404(self):
        self.db.execute.side_effect = [_result(one=_group_row()), _result(rows=[]), _result()]
        resp = client.put("/api/handsNorms/groups/edit/3?co_id=2", json={"active": 0})
        assert resp.status_code == 200, resp.text
        assert _params(self.db, 2)["active"] == 0
        self.db.execute.side_effect = [_result(one=_group_row(co_id=1))]
        assert client.put("/api/handsNorms/groups/edit/3?co_id=2", json={"active": 0}).status_code == 404

    def test_list_returns_member_count_total_units_and_norms_using(self):
        self.db.execute.return_value = _result(
            rows=[_group_row(), _group_row(machine_group_id=4, group_code="DRW1",
                                           group_name="1st Drawing F/S", unit_label="point",
                                           member_count=5, total_units=50, norms_using=0, active=0)]
        )
        resp = client.get("/api/handsNorms/groups?co_id=2&branch_id=29&include_inactive=1")
        assert resp.status_code == 200
        rows = resp.json()["data"]
        assert rows[0] == {
            "machine_group_id": 3, "co_id": 2, "branch_id": 29, "group_code": "HESS",
            "group_name": "Hessian looms", "unit_label": "machine", "remarks": None,
            "active": 1, "member_count": 200, "total_units": 200.0, "norms_using": 1,
        }
        assert (rows[1]["member_count"], rows[1]["total_units"], rows[1]["norms_using"],
                rows[1]["active"]) == (5, 50.0, 0, 0)
        assert _params(self.db, 0) == {"co_id": 2, "branch_id": 29, "include_inactive": 1}
        for col in ("member_count", "total_units", "norms_using"):
            assert col in _sql(self.db, 0)

    def test_get_group_returns_header_and_members(self):
        self.db.execute.side_effect = [
            _result(one=_group_row()),
            _result(rows=[_member_row(501), _member_row(502, units=10)]),
            _result(rows=[_mock_row({"hands_norm_id": 7, "desig": "WEAVER(H)"})]),
        ]
        resp = client.get("/api/handsNorms/groups/3?co_id=2")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["machine_group_id"] == 3 and data["group_code"] == "HESS"
        assert data["member_count"] == 2 and data["total_units"] == 11.0 and data["norms_using"] == 1
        assert data["members"][1] == {
            "machine_group_dtl_id": 1502, "machine_id": 502, "machine_name": "Hessian Loom - 502",
            "mech_code": "HL502", "machine_type_id": 7, "machine_type_name": "Loom",
            "dept_id": 21, "dept": "WEAVING", "units": 10.0,
        }
        self.db.execute.side_effect = [_result(one=None)]
        assert client.get("/api/handsNorms/groups/99?co_id=2").status_code == 404

    def test_setup_returns_machines_and_groups(self):
        self.db.execute.side_effect = _setup_results()
        resp = client.get("/api/handsNorms/setup?co_id=2&branch_id=29")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert set(data) == {"designations", "machine_types", "machine_groups", "machines", "departments"}
        assert data["machine_groups"][0]["group_code"] == "HESS"
        assert data["machine_groups"][0]["member_count"] == 200
        assert data["machine_groups"][0]["total_units"] == 200.0
        assert data["machines"] == [{
            "machine_id": 501, "machine_name": "Hessian Loom - 1", "mech_code": "HL1",
            "machine_type_id": 7, "machine_type_name": "Loom", "dept_id": 21, "dept": "WEAVING",
        }]
        assert data["departments"] == [{"dept_id": 19, "dept_desc": "SPOOL WINDING"}]
        assert _params(self.db, 2) == {"co_id": 2, "branch_id": 29, "include_inactive": 0}
        assert _params(self.db, 3) == {"branch_id": 29}
        assert _params(self.db, 4) == {"branch_id": 29}
        assert "machine_mst" in _sql(self.db, 3) and "dept_mst" in _sql(self.db, 4)
