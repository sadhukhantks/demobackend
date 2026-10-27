"""Costing parameters master (src/juteCosting/params.py, prefix /api/juteCostParam).

The resolution rule (resolve_params / param_value) is pure and exercised on
plain dicts; the endpoints go through TestClient on a throwaway app, as
test_juteprod_spinning_trend.py does — the router is wired into src/main.py by
the integrator, so these tests must pass before that happens.
"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.juteCosting.params as params_module
from src.juteCosting.params import (
    ACTIVE_WITH_NEW_DATE_MSG,
    COST_PARAM_NOT_FOUND_MSG,
    DUPLICATE_MSG,
    EDIT_KEY_CHANGE_MSG,
    INACTIVE_BASE_MSG,
    PARAM_CODES,
    PRODUCTS,
    get_current_user_with_refresh,
    get_tenant_db,
    not_keyed_codes,
    param_value,
    pick_key,
    resolve_params,
    router,
    today_ist,
)

PREFIX = "/api/juteCostParam"

app = FastAPI()
app.include_router(router, prefix=PREFIX)
client = TestClient(app)

ON = date(2026, 10, 10)


@pytest.fixture(autouse=True)
def db():
    session = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: session
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    yield session
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Row / result helpers
# ---------------------------------------------------------------------------


def _row(
    cost_param_id,
    code,
    value,
    eff,
    branch_id=2,
    product=None,
    active=1,
    uom=None,
    source=None,
    remarks=None,
    co_id=1,
    branch_name="Factory",
):
    return {
        "cost_param_id": cost_param_id,
        "co_id": co_id,
        "branch_id": branch_id,
        "branch_name": branch_name if branch_id is not None else None,
        "effective_date": eff,
        "param_code": code,
        "param_value": value,
        "uom": uom,
        "product": product,
        "source": source,
        "remarks": remarks,
        "active": active,
    }


def _mock_row(mapping: dict):
    return SimpleNamespace(_mapping=mapping)


def _attr_row(**attrs):
    """fetchone() row: attribute access AND ._mapping."""
    return SimpleNamespace(_mapping=dict(attrs), **attrs)


def _rows_result(mappings):
    res = MagicMock()
    res.fetchall.return_value = [_mock_row(m) for m in mappings]
    return res


def _one_result(mapping):
    res = MagicMock()
    res.fetchone.return_value = _attr_row(**mapping) if mapping else None
    return res


def _cnt_result(cnt):
    res = MagicMock()
    res.fetchone.return_value = _attr_row(cnt=cnt)
    return res


def _insert_result(lastrowid):
    res = MagicMock()
    res.lastrowid = lastrowid
    return res


def _rowcount_result(n):
    res = MagicMock()
    res.rowcount = n
    return res


def _scope_ok():
    """branch_mst scope check (doff_analysis._verify_pair): .first() -> a row."""
    res = MagicMock()
    res.first.return_value = (1,)
    return res


def _scope_bad():
    res = MagicMock()
    res.first.return_value = None
    return res


def _binds(db, i):
    return db.execute.call_args_list[i][0][1]


def _sql(db, i):
    return str(db.execute.call_args_list[i][0][0])


# ---------------------------------------------------------------------------
# Pure: resolve_params / param_value
# ---------------------------------------------------------------------------


class TestResolveParams:
    def test_branch_specific_beats_newer_all_branch_row(self):
        rows = [
            _row(1, "WAGE_PER_HAND_DAY", 400, date(2026, 9, 1), branch_id=2),
            _row(2, "WAGE_PER_HAND_DAY", 450, date(2026, 10, 1), branch_id=None),
        ]
        pick = param_value(resolve_params(rows, ON), "WAGE_PER_HAND_DAY")
        assert pick["cost_param_id"] == 1 and pick["value"] == 400.0

    def test_same_scope_latest_effective_date_wins(self):
        rows = [
            _row(1, "WAGE_PER_HAND_DAY", 400, date(2026, 9, 1)),
            _row(2, "WAGE_PER_HAND_DAY", 420, date(2026, 10, 1)),
        ]
        assert param_value(resolve_params(rows, ON), "WAGE_PER_HAND_DAY")["cost_param_id"] == 2

    def test_same_date_highest_id_wins(self):
        rows = [
            _row(3, "WAGE_PER_HAND_DAY", 400, date(2026, 10, 1)),
            _row(4, "WAGE_PER_HAND_DAY", 410, date(2026, 10, 1)),
        ]
        assert param_value(resolve_params(rows, ON), "WAGE_PER_HAND_DAY")["cost_param_id"] == 4

    def test_future_rows_are_ignored(self):
        rows = [
            _row(1, "WAGE_PER_HAND_DAY", 400, date(2026, 9, 1)),
            _row(5, "WAGE_PER_HAND_DAY", 999, date(2026, 11, 1)),
        ]
        assert param_value(resolve_params(rows, ON), "WAGE_PER_HAND_DAY")["cost_param_id"] == 1
        # String dates (as a driver might hand them back) are accepted too.
        rows[1]["effective_date"] = "2026-11-01"
        assert param_value(resolve_params(rows, ON), "WAGE_PER_HAND_DAY")["cost_param_id"] == 1

    def test_product_pick_and_fallback(self):
        rows = [
            _row(1, "REALISATION_PER_T", 182000, date(2026, 10, 1), product="SACKING"),
            _row(2, "REALISATION_PER_T", 200000, date(2026, 10, 1), product=None),
        ]
        resolved = resolve_params(rows, ON)
        assert param_value(resolved, "REALISATION_PER_T", "SACKING")["cost_param_id"] == 1
        # No HESSIAN row -> the all-product row.
        assert param_value(resolved, "REALISATION_PER_T", "HESSIAN")["cost_param_id"] == 2
        only_sacking = resolve_params(rows[:1], ON)
        assert param_value(only_sacking, "REALISATION_PER_T", "HESSIAN") is None
        assert param_value(only_sacking, "WAGE_PER_HAND_DAY") is None

    def test_inactive_rows_are_ignored(self):
        rows = [
            _row(1, "WAGE_PER_HAND_DAY", 400, date(2026, 9, 1)),
            _row(2, "WAGE_PER_HAND_DAY", 999, date(2026, 10, 1), active=0),
        ]
        assert param_value(resolve_params(rows, ON), "WAGE_PER_HAND_DAY")["cost_param_id"] == 1

    def test_pick_fields(self):
        rows = [
            _row(7, "DUST_SALE_PER_KG", "1.0000", "2026-10-01", branch_id=None, uom="₹/kg", source="owner")
        ]
        resolved = resolve_params(rows, ON)
        assert set(resolved) == {("DUST_SALE_PER_KG", None)}
        pick = resolved[("DUST_SALE_PER_KG", None)]
        assert {"value", "uom", "effective_date", "branch_id", "product", "source", "cost_param_id"} <= set(pick)
        assert pick["value"] == 1.0 and isinstance(pick["value"], float)
        assert pick["effective_date"] == date(2026, 10, 1)
        assert pick["branch_id"] is None and pick["product"] is None
        assert pick["source"] == "owner" and pick["uom"] == "₹/kg"

    def test_pick_key_order(self):
        all_branch = _row(9, "X", 1, date(2026, 10, 1), branch_id=None)
        branch = _row(1, "X", 1, date(2026, 1, 1), branch_id=2)
        assert pick_key(branch) > pick_key(all_branch)

    def test_not_keyed_codes(self):
        rows = [_row(1, "REALISATION_PER_T", 182000, date(2026, 10, 1), product="SACKING")]
        missing = not_keyed_codes(resolve_params(rows, ON))
        assert "REALISATION_PER_T" not in missing  # a product pick counts as keyed
        assert missing[:1] == ["JUTE_RATE_PER_KG"]
        assert len(missing) == len(PARAM_CODES) - 1


# ---------------------------------------------------------------------------
# GET /cost_param_setup
# ---------------------------------------------------------------------------


class TestSetup:
    def test_setup_lists_codes_in_order(self, db):
        r = client.get(f"{PREFIX}/cost_param_setup", params={"co_id": 1})
        assert r.status_code == 200
        data = r.json()["data"]
        assert [c["code"] for c in data["param_codes"]] == [p["code"] for p in PARAM_CODES]
        assert len(data["param_codes"]) == 21  # 11 (ITM-019) + 10 (ITM-027)
        assert data["param_codes"][0] == {
            "code": "REALISATION_PER_T",
            "label": "Realisation per tonne",
            "uom": "₹/t",
            "product_scoped": True,
        }
        assert data["products"] == PRODUCTS == ["SACKING", "HESSIAN", "YARN"]
        db.execute.assert_not_called()

    def test_setup_requires_co_id(self):
        r = client.get(f"{PREFIX}/cost_param_setup")
        assert r.status_code == 400 and r.json()["detail"] == "co_id is required"


# ---------------------------------------------------------------------------
# GET /cost_param_list
# ---------------------------------------------------------------------------


class TestList:
    def test_list_serialises_and_flags_in_force(self, db):
        db.execute.return_value = _rows_result(
            [
                _row(1, "WAGE_PER_HAND_DAY", 400, date(2026, 10, 1), uom="₹/hand-day"),
                _row(2, "WAGE_PER_HAND_DAY", 380, date(2026, 9, 1), uom="₹/hand-day"),
                _row(3, "DUST_SALE_PER_KG", 1, date(2026, 10, 1), branch_id=None, uom="₹/kg"),
                _row(4, "PAYMENT_DAYS", 10, date(2026, 10, 1), active=0),
            ]
        )
        r = client.get(f"{PREFIX}/cost_param_list", params={"co_id": 1, "branch_id": 2})
        assert r.status_code == 200, r.text
        assert _binds(db, 0) == {
            "co_id": 1,
            "branch_id": 2,
            "param_code": None,
            "product": None,
            "active": None,
        }
        rows = {x["cost_param_id"]: x for x in r.json()["data"]}
        assert rows[1]["effective_date"] == "2026-10-01"
        assert rows[1]["param_label"] == "Wage per hand-day"
        assert rows[1]["scope"] == "branch" and rows[1]["branch_name"] == "Factory"
        assert rows[3]["scope"] == "all" and rows[3]["branch_id"] is None
        assert rows[1]["in_force"] is True
        assert rows[2]["in_force"] is False  # older same-scope row
        assert rows[3]["in_force"] is True
        assert rows[4]["in_force"] is False and rows[4]["active"] == 0
        assert isinstance(rows[1]["param_value"], float) and rows[1]["param_value"] == 400.0

    def test_list_optional_filters_bound(self, db):
        db.execute.return_value = _rows_result([])
        r = client.get(
            f"{PREFIX}/cost_param_list",
            params={"co_id": 1, "param_code": "DUST_SALE_PER_KG", "product": "SACKING", "active": "1"},
        )
        assert r.status_code == 200
        assert _binds(db, 0) == {
            "co_id": 1,
            "branch_id": None,
            "param_code": "DUST_SALE_PER_KG",
            "product": "SACKING",
            "active": 1,
        }

    def test_list_rejects_bad_filters(self, db):
        assert client.get(f"{PREFIX}/cost_param_list").status_code == 400
        r = client.get(f"{PREFIX}/cost_param_list", params={"co_id": 1, "param_code": "FOO"})
        assert r.status_code == 400 and r.json()["detail"] == "Invalid param_code 'FOO'"
        r = client.get(f"{PREFIX}/cost_param_list", params={"co_id": 1, "active": "x"})
        assert r.status_code == 400
        r = client.get(f"{PREFIX}/cost_param_list", params={"co_id": 1, "product": "CLOTH"})
        assert r.status_code == 400
        db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# GET /cost_param_by_id
# ---------------------------------------------------------------------------


class TestById:
    def test_found(self, db):
        row = _row(5, "WAGE_PER_HAND_DAY", 400, date(2026, 10, 1))
        db.execute.side_effect = [_one_result(row), _rows_result([row])]
        r = client.get(f"{PREFIX}/cost_param_by_id/5", params={"co_id": 1})
        assert r.status_code == 200
        assert r.json()["data"]["cost_param_id"] == 5
        assert r.json()["data"]["in_force"] is True
        assert _binds(db, 0) == {"id": 5, "co_id": 1}

    def test_not_found(self, db):
        db.execute.return_value = _one_result(None)
        r = client.get(f"{PREFIX}/cost_param_by_id/5", params={"co_id": 1})
        assert r.status_code == 404 and r.json()["detail"] == COST_PARAM_NOT_FOUND_MSG


# ---------------------------------------------------------------------------
# POST /cost_param_create
# ---------------------------------------------------------------------------


def _create_body(**over):
    body = {
        "co_id": 1,
        "branch_id": 2,
        "effective_date": "2026-10-01",
        "param_code": "WAGE_PER_HAND_DAY",
        "param_value": 400,
    }
    body.update(over)
    return body


class TestCreate:
    def test_happy_path(self, db):
        db.execute.side_effect = [_scope_ok(), _cnt_result(0), _insert_result(7)]
        r = client.post(f"{PREFIX}/cost_param_create", json=_create_body(source="proxy"))
        assert r.status_code == 200, r.text
        assert r.json() == {"data": {"cost_param_id": 7}}
        db.commit.assert_called_once()
        # Scope check first (the branch must belong to the company), then the
        # duplicate guard, then the insert.
        assert _binds(db, 0) == {"co_id": 1, "branch_id": 2}
        assert "branch_mst" in _sql(db, 0)
        dup = _binds(db, 1)
        assert dup == {
            "co_id": 1,
            "branch_id": 2,
            "effective_date": date(2026, 10, 1),
            "param_code": "WAGE_PER_HAND_DAY",
            "product": None,
            "id": None,
        }
        ins = _binds(db, 2)
        assert ins["product"] is None  # never the string 'null'
        assert ins["uom"] == "₹/hand-day"  # defaults to the code's uom
        assert ins["param_value"] == 400.0 and ins["updated_by"] == 1
        assert ins["source"] == "proxy" and ins["remarks"] is None

    def test_branch_of_another_company_is_400(self, db):
        db.execute.side_effect = [_scope_bad()]
        r = client.post(f"{PREFIX}/cost_param_create", json=_create_body(branch_id=29))
        assert r.status_code == 400 and r.json()["detail"] == "branch_id does not belong to co_id"
        assert db.execute.call_count == 1
        db.commit.assert_not_called()

    def test_all_branch_row_skips_the_scope_check(self, db):
        db.execute.side_effect = [_cnt_result(0), _insert_result(8)]
        r = client.post(f"{PREFIX}/cost_param_create", json=_create_body(branch_id=None))
        assert r.status_code == 200, r.text
        assert db.execute.call_count == 2 and _binds(db, 0)["branch_id"] is None

    def test_duplicate_is_400(self, db):
        db.execute.side_effect = [_scope_ok(), _cnt_result(1)]
        r = client.post(f"{PREFIX}/cost_param_create", json=_create_body())
        assert r.status_code == 400 and r.json()["detail"] == DUPLICATE_MSG
        db.rollback.assert_called()
        db.commit.assert_not_called()

    def test_bad_param_code(self, db):
        r = client.post(f"{PREFIX}/cost_param_create", json=_create_body(param_code="FOO"))
        assert r.status_code == 400 and r.json()["detail"] == "Invalid param_code 'FOO'"
        db.execute.assert_not_called()

    def test_bad_product(self, db):
        r = client.post(f"{PREFIX}/cost_param_create", json=_create_body(product="CLOTH"))
        assert r.status_code == 400 and "Invalid product" in r.json()["detail"]

    def test_negative_value_is_422(self, db):
        r = client.post(f"{PREFIX}/cost_param_create", json=_create_body(param_value=-1))
        assert r.status_code == 422
        db.execute.assert_not_called()

    def test_missing_co_id_is_422(self, db):
        body = _create_body()
        body.pop("co_id")
        assert client.post(f"{PREFIX}/cost_param_create", json=body).status_code == 422


# ---------------------------------------------------------------------------
# PUT /cost_param_edit/{id}
# ---------------------------------------------------------------------------


EXISTING = _row(5, "WAGE_PER_HAND_DAY", 400, date(2026, 10, 1), uom="₹/hand-day", source="proxy")


class TestEdit:
    def test_new_effective_date_files_a_new_row(self, db):
        db.execute.side_effect = [_one_result(EXISTING), _cnt_result(0), _insert_result(9)]
        r = client.put(
            f"{PREFIX}/cost_param_edit/5",
            params={"co_id": 1},
            json={"effective_date": "2026-10-18", "param_value": 420},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"data": {"cost_param_id": 9, "superseded_id": 5, "created_new": True}}
        assert db.execute.call_count == 3
        assert _binds(db, 0) == {"id": 5, "co_id": 1}
        dup = _binds(db, 1)
        assert dup["effective_date"] == date(2026, 10, 18) and dup["product"] is None
        ins = _binds(db, 2)
        assert ins["param_value"] == 420.0
        assert ins["uom"] == "₹/hand-day" and ins["source"] == "proxy"  # overlay keeps old values
        assert ins["effective_date"] == date(2026, 10, 18)
        assert "UPDATE" not in _sql(db, 2)
        db.commit.assert_called_once()

    def test_new_effective_date_clash_is_400(self, db):
        db.execute.side_effect = [_one_result(EXISTING), _cnt_result(1)]
        r = client.put(
            f"{PREFIX}/cost_param_edit/5",
            params={"co_id": 1},
            json={"effective_date": "2026-10-18", "param_value": 420},
        )
        assert r.status_code == 400 and r.json()["detail"] == DUPLICATE_MSG

    def test_same_date_updates_in_place(self, db):
        db.execute.side_effect = [_one_result(EXISTING), MagicMock()]
        r = client.put(
            f"{PREFIX}/cost_param_edit/5",
            params={"co_id": 1},
            json={"effective_date": "2026-10-01", "param_value": 410, "remarks": "checked"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"data": {"cost_param_id": 5, "created_new": False}}
        sql = _sql(db, 1)
        assert sql.startswith("UPDATE jute_cost_param SET ")
        assert "param_value = :param_value" in sql and "remarks = :remarks" in sql
        assert "uom =" not in sql and "source =" not in sql and "active =" not in sql
        assert _binds(db, 1) == {
            "id": 5,
            "co_id": 1,
            "updated_by": 1,
            "param_value": 410.0,
            "remarks": "checked",
        }
        db.commit.assert_called_once()

    def test_active_only_update(self, db):
        db.execute.side_effect = [_one_result(EXISTING), MagicMock()]
        r = client.put(f"{PREFIX}/cost_param_edit/5", params={"co_id": 1}, json={"active": 0})
        assert r.status_code == 200
        assert "active = :active" in _sql(db, 1) and _binds(db, 1)["active"] == 0

    def test_new_date_keeps_the_new_row_active_when_asked_active(self, db):
        db.execute.side_effect = [_one_result(EXISTING), _cnt_result(0), _insert_result(9)]
        r = client.put(
            f"{PREFIX}/cost_param_edit/5",
            params={"co_id": 1},
            json={"effective_date": "2026-10-18", "param_value": 420, "active": 1},
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["created_new"] is True

    def test_new_date_with_inactive_status_is_400(self, db):
        # The new row is always active: refuse rather than file it silently.
        db.execute.side_effect = [_one_result(EXISTING)]
        r = client.put(
            f"{PREFIX}/cost_param_edit/5",
            params={"co_id": 1},
            json={"effective_date": "2026-10-18", "active": 0},
        )
        assert r.status_code == 400 and r.json()["detail"] == ACTIVE_WITH_NEW_DATE_MSG
        assert db.execute.call_count == 1
        db.commit.assert_not_called()

    def test_new_date_from_an_inactive_row_is_400(self, db):
        inactive = dict(EXISTING, active=0)
        db.execute.side_effect = [_one_result(inactive)]
        r = client.put(
            f"{PREFIX}/cost_param_edit/5",
            params={"co_id": 1},
            json={"effective_date": "2026-10-18", "param_value": 420},
        )
        assert r.status_code == 400 and r.json()["detail"] == INACTIVE_BASE_MSG
        db.commit.assert_not_called()

    def test_reactivating_runs_the_duplicate_guard(self, db):
        inactive = dict(EXISTING, active=0)
        # Another active row already sits at the key -> 400, no UPDATE.
        db.execute.side_effect = [_one_result(inactive), _cnt_result(1)]
        r = client.put(f"{PREFIX}/cost_param_edit/5", params={"co_id": 1}, json={"active": 1})
        assert r.status_code == 400 and r.json()["detail"] == DUPLICATE_MSG
        assert _binds(db, 1) == {
            "co_id": 1,
            "branch_id": 2,
            "effective_date": date(2026, 10, 1),
            "param_code": "WAGE_PER_HAND_DAY",
            "product": None,
            "id": 5,
        }
        db.commit.assert_not_called()
        # Key free -> the row is reactivated in place.
        db.reset_mock()
        db.execute.side_effect = [_one_result(inactive), _cnt_result(0), MagicMock()]
        r = client.put(f"{PREFIX}/cost_param_edit/5", params={"co_id": 1}, json={"active": 1})
        assert r.status_code == 200, r.text
        assert "active = :active" in _sql(db, 2) and _binds(db, 2)["active"] == 1
        db.commit.assert_called_once()

    def test_active_on_an_active_row_needs_no_duplicate_guard(self, db):
        db.execute.side_effect = [_one_result(EXISTING), MagicMock()]
        r = client.put(f"{PREFIX}/cost_param_edit/5", params={"co_id": 1}, json={"active": 1})
        assert r.status_code == 200
        assert db.execute.call_count == 2 and "UPDATE" in _sql(db, 1)

    def test_product_or_code_change_refused(self, db):
        db.execute.side_effect = [_one_result(EXISTING)]
        r = client.put(
            f"{PREFIX}/cost_param_edit/5", params={"co_id": 1}, json={"product": "SACKING"}
        )
        assert r.status_code == 400 and r.json()["detail"] == EDIT_KEY_CHANGE_MSG
        db.execute.side_effect = [_one_result(EXISTING)]
        r = client.put(
            f"{PREFIX}/cost_param_edit/5", params={"co_id": 1}, json={"param_code": "PAYMENT_DAYS"}
        )
        assert r.status_code == 400 and r.json()["detail"] == EDIT_KEY_CHANGE_MSG
        db.commit.assert_not_called()

    def test_unknown_id_is_404(self, db):
        db.execute.return_value = _one_result(None)
        r = client.put(f"{PREFIX}/cost_param_edit/99", params={"co_id": 1}, json={"param_value": 1})
        assert r.status_code == 404 and r.json()["detail"] == COST_PARAM_NOT_FOUND_MSG

    def test_requires_co_id(self, db):
        r = client.put(f"{PREFIX}/cost_param_edit/5", json={"param_value": 1})
        assert r.status_code == 400 and r.json()["detail"] == "co_id is required"


# ---------------------------------------------------------------------------
# DELETE /cost_param_delete/{id}
# ---------------------------------------------------------------------------


class TestDelete:
    def test_not_found(self, db):
        db.execute.return_value = _rowcount_result(0)
        r = client.delete(f"{PREFIX}/cost_param_delete/5", params={"co_id": 1})
        assert r.status_code == 404 and r.json()["detail"] == COST_PARAM_NOT_FOUND_MSG
        db.commit.assert_not_called()

    def test_deleted(self, db):
        db.execute.return_value = _rowcount_result(1)
        r = client.delete(f"{PREFIX}/cost_param_delete/5", params={"co_id": 1})
        assert r.status_code == 200 and r.json() == {"data": {"message": "Deleted"}}
        assert _binds(db, 0) == {"id": 5, "co_id": 1, "updated_by": 1}
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# GET /cost_param_resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_lists_picks_and_not_keyed(self, db):
        db.execute.return_value = _rows_result(
            [
                _row(1, "REALISATION_PER_T", 182000, date(2026, 10, 1), product="SACKING", uom="₹/t"),
                _row(2, "REALISATION_PER_T", 210000, date(2026, 10, 1), product="HESSIAN", uom="₹/t"),
                _row(3, "WAGE_PER_HAND_DAY", 400, date(2026, 10, 1), uom="₹/hand-day"),
                _row(4, "DUST_SALE_PER_KG", 1, date(2026, 10, 1), branch_id=None, uom="₹/kg"),
            ]
        )
        r = client.get(
            f"{PREFIX}/cost_param_resolve",
            params={"co_id": 1, "branch_id": 2, "on_date": "2026-10-10"},
        )
        assert r.status_code == 200, r.text
        assert _binds(db, 0) == {"co_id": 1, "branch_id": 2, "on_date": date(2026, 10, 10)}
        data = r.json()["data"]
        assert data["on_date"] == "2026-10-10"
        assert set(data["params"]) == {
            "REALISATION_PER_T:SACKING",
            "REALISATION_PER_T:HESSIAN",
            "WAGE_PER_HAND_DAY",
            "DUST_SALE_PER_KG",
        }
        pick = data["params"]["WAGE_PER_HAND_DAY"]
        assert pick == {
            "value": 400.0,
            "uom": "₹/hand-day",
            "effective_date": "2026-10-01",
            "branch_id": 2,
            "scope": "branch",
            "product": None,
            "source": None,
            "cost_param_id": 3,
        }
        assert data["params"]["DUST_SALE_PER_KG"]["scope"] == "all"
        assert data["not_keyed"] == [
            "JUTE_RATE_PER_KG",
            "JUTE_PER_KG_CLOTH",
            "POWER_RATE_PER_KWH",
            "COST_OF_CAPITAL_PCT",
            "RESTART_CAPEX_PER_FRAME",
            "PAYMENT_DAYS",
            "BALE_WEIGHT_TOLERANCE",
            "FIXED_COST_PER_DAY",
            "DG_RATE_PER_KWH",
            "SOLAR_RATE_PER_KWH",
            "OVERHEAD_STAFF_PER_DAY",
            "OVERHEAD_ADMIN_PER_DAY",
            "OVERHEAD_OTHER_PER_DAY",
            "WORKING_CAPITAL_OTHER",
            "BENCH_STORES_PER_T",
            "BENCH_POWER_PER_T",
            "BENCH_OVERHEAD_PER_T",
            "FG_STOCK_T",
        ]

    def test_defaults_to_today_and_validates_date(self, db):
        db.execute.return_value = _rows_result([])
        r = client.get(f"{PREFIX}/cost_param_resolve", params={"co_id": 1})
        assert r.status_code == 200
        assert r.json()["data"]["on_date"] == today_ist().isoformat()
        assert _binds(db, 0)["branch_id"] is None
        r = client.get(f"{PREFIX}/cost_param_resolve", params={"co_id": 1, "on_date": "10-10-2026"})
        assert r.status_code == 400 and "on_date" in r.json()["detail"]


class TestTodayIsIst:
    """'Today' is the mill's IST date, not the UTC server date: at 01:00 IST on
    the 20th (19:30 UTC on the 19th) a row effective the 20th is in force."""

    @pytest.fixture(autouse=True)
    def _clock(self, monkeypatch):
        monkeypatch.setattr(params_module, "now_ist", lambda: datetime(2026, 10, 20, 1, 0))

    def test_resolve_defaults_to_ist_today(self, db):
        db.execute.return_value = _rows_result([])
        r = client.get(f"{PREFIX}/cost_param_resolve", params={"co_id": 1})
        assert r.json()["data"]["on_date"] == "2026-10-20"
        assert _binds(db, 0)["on_date"] == date(2026, 10, 20)

    def test_list_flags_a_row_effective_today_in_force(self, db):
        db.execute.return_value = _rows_result(
            [
                _row(1, "WAGE_PER_HAND_DAY", 400, date(2026, 10, 1)),
                _row(2, "WAGE_PER_HAND_DAY", 420, date(2026, 10, 20)),
            ]
        )
        r = client.get(f"{PREFIX}/cost_param_list", params={"co_id": 1, "branch_id": 2})
        rows = {x["cost_param_id"]: x for x in r.json()["data"]}
        assert rows[2]["in_force"] is True and rows[1]["in_force"] is False

    def test_by_id_binds_ist_today(self, db):
        row = _row(2, "WAGE_PER_HAND_DAY", 420, date(2026, 10, 20))
        db.execute.side_effect = [_one_result(row), _rows_result([row])]
        r = client.get(f"{PREFIX}/cost_param_by_id/2", params={"co_id": 1})
        assert r.json()["data"]["in_force"] is True
        assert _binds(db, 1)["on_date"] == date(2026, 10, 20)
