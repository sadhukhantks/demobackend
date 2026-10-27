"""Tests for the Winding Standards / Targets Map endpoints.

Tests for src/juteProduction/winding_target_map.py (prefix /api/windingTargetMap).

Portal persona: DB + auth mocked (no real DB). get_tenant_db /
get_current_user_with_refresh are imported into the router module's namespace and
resolved by FastAPI via Depends, so we override them through
app.dependency_overrides keyed by those exact symbols (same pattern as
test_weaving_target_map.py).

Winding is QUALITY-ONLY: id_type is always 'qid' (ref_id = yarn item_mst.item_id),
value_role is 'standard' | 'target', and there is ONE param 'prod' (KG per winder
per 8-hour shift). grid_params_for LOCKED CONTRACT:
  qid + standard -> prod
  qid + target   -> prod

Covered: setup / list / create / edit / delete happy paths, LAST-DATE resolution
(the grid takes the newest effective_date <= on_date and flags inherited cells),
bad param / value_role / id_type, negative + NaN value, missing co_id, and
bulk_save insert-vs-update-vs-clear.
"""

import json
from datetime import date
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.juteProduction.winding_target_map import (
    get_current_user_with_refresh,
    get_tenant_db,
    grid_params_for,
)
from src.main import app

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _exec_params(mock_session, call_index: int) -> dict:
    """Bound params of the Nth db.execute call."""
    return mock_session.execute.call_args_list[call_index].args[1]


class _Base:
    def setup_method(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session

    def teardown_method(self):
        app.dependency_overrides.clear()


# =============================================================================
# GET /target_map_setup
# =============================================================================


class TestTargetMapSetup(_Base):
    def test_success_returns_yarn_qualities_and_enums(self):
        yarn_rows = [
            _mock_row(
                {
                    "item_id": 4321,
                    "item_code": "13-SKWP",
                    "item_name": "13 LBS SACKING WARP",
                    "std_count": 13.0,
                    "std_mr_pct": 20.0,
                }
            ),
        ]
        exec_yarn = MagicMock()
        exec_yarn.fetchall.return_value = yarn_rows
        self._mock_session.execute.return_value = exec_yarn

        response = client.get("/api/windingTargetMap/target_map_setup?co_id=1")

        assert response.status_code == 200
        data = response.json()["data"]
        q = data["qualities"][0]
        # Yarn item identity + the normalised ref_* aliases the grid uses.
        assert q["item_id"] == 4321
        assert q["item_code"] == "13-SKWP"
        assert q["ref_id"] == 4321
        assert q["ref_name"] == "13 LBS SACKING WARP"
        # Quality-only: no machine dimension.
        assert data["id_types"] == ["qid"]
        assert data["value_roles"] == ["standard", "target"]
        assert data["params"] == ["prod"]
        # Weaving/beaming params must NOT leak into winding.
        assert "speed" not in data["params"]
        # The yarn picker is company-scoped.
        assert _exec_params(self._mock_session, 0)["co_id"] == 1

    def test_missing_co_id_returns_400(self):
        response = client.get("/api/windingTargetMap/target_map_setup")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()


# =============================================================================
# GET /target_map_list
# =============================================================================


class TestTargetMapList(_Base):
    def test_list_casts_value_and_date(self):
        rows = [
            _mock_row(
                {
                    "winding_target_map_id": 9,
                    "co_id": 1,
                    "branch_id": 2,
                    "effective_date": "2026-08-01",
                    "ref_id": 4321,
                    "id_type": "qid",
                    "value_role": "standard",
                    "param": "prod",
                    "value": 42.5,
                    "active": 1,
                    "ref_code": "13-SKWP",
                    "ref_name": "13 LBS SACKING WARP",
                }
            ),
        ]
        exec_rows = MagicMock()
        exec_rows.fetchall.return_value = rows
        self._mock_session.execute.return_value = exec_rows

        response = client.get(
            "/api/windingTargetMap/target_map_list"
            "?co_id=1&id_type=qid&value_role=standard&param=prod&ref_id=4321"
        )

        assert response.status_code == 200
        row = response.json()["data"][0]
        assert row["winding_target_map_id"] == 9
        assert row["effective_date"] == "2026-08-01"
        assert row["value"] == 42.5
        assert row["ref_code"] == "13-SKWP"
        # Optional filters are bound through as-is.
        params = _exec_params(self._mock_session, 0)
        assert params["id_type"] == "qid"
        assert params["value_role"] == "standard"
        assert params["param"] == "prod"
        assert params["ref_id"] == 4321

    def test_list_binds_none_when_no_filters(self):
        exec_rows = MagicMock()
        exec_rows.fetchall.return_value = []
        self._mock_session.execute.return_value = exec_rows

        response = client.get("/api/windingTargetMap/target_map_list?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"] == []
        params = _exec_params(self._mock_session, 0)
        # SQL NULL, never the string "null".
        assert params["branch_id"] is None
        assert params["id_type"] is None
        assert params["ref_id"] is None
        assert params["value_role"] is None
        assert params["param"] is None

    def test_list_invalid_param_returns_400(self):
        response = client.get("/api/windingTargetMap/target_map_list?co_id=1&param=speed")
        assert response.status_code == 400
        assert "param" in response.json()["detail"].lower()

    def test_list_missing_co_id_returns_400(self):
        response = client.get("/api/windingTargetMap/target_map_list")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()


# =============================================================================
# POST /target_map_create
# =============================================================================


class TestTargetMapCreate(_Base):
    def _body(self, **overrides):
        body = {
            "co_id": 1,
            "branch_id": 2,
            "effective_date": "2026-08-19",
            "ref_id": 4321,
            "id_type": "qid",
            "value_role": "standard",
            "param": "prod",
            "value": 42.5,
        }
        body.update(overrides)
        return body

    def test_create_success(self):
        self._mock_session.execute.return_value.lastrowid = 11

        response = client.post(
            "/api/windingTargetMap/target_map_create", json=self._body()
        )

        assert response.status_code == 200
        assert response.json()["data"]["winding_target_map_id"] == 11
        self._mock_session.commit.assert_called_once()
        params = _exec_params(self._mock_session, 0)
        assert params["id_type"] == "qid"
        assert params["value_role"] == "standard"
        assert params["param"] == "prod"
        assert params["ref_id"] == 4321
        assert params["value"] == 42.5
        assert params["updated_by"] == 1

    def test_create_target_role_success(self):
        self._mock_session.execute.return_value.lastrowid = 12

        response = client.post(
            "/api/windingTargetMap/target_map_create",
            json=self._body(value_role="target", value=40.0),
        )

        assert response.status_code == 200
        assert _exec_params(self._mock_session, 0)["value_role"] == "target"

    def test_create_invalid_id_type_rolls_back(self):
        response = client.post(
            "/api/windingTargetMap/target_map_create", json=self._body(id_type="mcid")
        )

        assert response.status_code == 400
        assert "id_type" in response.json()["detail"].lower()
        self._mock_session.rollback.assert_called_once()
        self._mock_session.commit.assert_not_called()

    def test_create_invalid_value_role_rolls_back(self):
        response = client.post(
            "/api/windingTargetMap/target_map_create",
            json=self._body(value_role="actual"),
        )

        assert response.status_code == 400
        assert "value_role" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_create_invalid_param_rolls_back(self):
        response = client.post(
            "/api/windingTargetMap/target_map_create", json=self._body(param="speed")
        )

        assert response.status_code == 400
        assert "param" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_create_negative_value_rejected(self):
        response = client.post(
            "/api/windingTargetMap/target_map_create", json=self._body(value=-5)
        )

        assert response.status_code == 400
        assert "negative" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_create_nan_value_rejected(self):
        response = client.post(
            "/api/windingTargetMap/target_map_create",
            content=json.dumps(self._body(value=float("nan"))),
            headers={"Content-Type": "application/json"},
        )

        # NaN is rejected by the handler's finite guard, never reaches the INSERT.
        assert response.status_code == 400
        assert "finite" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_create_infinite_value_rejected(self):
        response = client.post(
            "/api/windingTargetMap/target_map_create",
            content=json.dumps(self._body(value=float("inf"))),
            headers={"Content-Type": "application/json"},
        )

        # +inf would insert and blow up at the DB; the finite guard 400s first.
        assert response.status_code == 400
        assert "finite" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()


# =============================================================================
# PUT /target_map_edit/{id}
# =============================================================================


class TestTargetMapEdit(_Base):
    def test_edit_updates_provided_fields(self):
        found = MagicMock()
        found.fetchone.return_value = _mock_row(
            {"winding_target_map_id": 5, "co_id": 1, "param": "prod"}
        )
        self._mock_session.execute.side_effect = [found, MagicMock()]

        response = client.put(
            "/api/windingTargetMap/target_map_edit/5?co_id=1",
            json={"value": 55.5, "effective_date": "2026-09-01", "active": 1},
        )

        assert response.status_code == 200
        assert response.json()["data"]["winding_target_map_id"] == 5
        self._mock_session.commit.assert_called_once()
        params = _exec_params(self._mock_session, 1)
        assert params["value"] == 55.5
        assert params["effective_date"] == date(2026, 9, 1)
        assert params["active"] == 1
        assert params["updated_by"] == 1

    def test_edit_other_company_row_returns_404(self):
        found = MagicMock()
        found.fetchone.return_value = _mock_row(
            {"winding_target_map_id": 5, "co_id": 9, "param": "prod"}
        )
        self._mock_session.execute.return_value = found

        response = client.put(
            "/api/windingTargetMap/target_map_edit/5?co_id=1", json={"value": 1.0}
        )

        assert response.status_code == 404
        self._mock_session.commit.assert_not_called()

    def test_edit_not_found_returns_404(self):
        self._mock_session.execute.return_value.fetchone.return_value = None

        response = client.put(
            "/api/windingTargetMap/target_map_edit/999?co_id=1", json={"value": 1.0}
        )

        assert response.status_code == 404
        self._mock_session.rollback.assert_called_once()

    def test_edit_missing_co_id_returns_400(self):
        response = client.put(
            "/api/windingTargetMap/target_map_edit/5", json={"value": 1.0}
        )
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()


# =============================================================================
# DELETE /target_map_delete/{id}
# =============================================================================


class TestTargetMapDelete(_Base):
    def test_delete_soft_sets_active_zero(self):
        found = MagicMock()
        found.fetchone.return_value = _mock_row(
            {"winding_target_map_id": 5, "co_id": 1, "param": "prod"}
        )
        self._mock_session.execute.side_effect = [found, MagicMock()]

        response = client.delete("/api/windingTargetMap/target_map_delete/5?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"]["message"] == "Deleted"
        self._mock_session.commit.assert_called_once()
        assert _exec_params(self._mock_session, 1)["id"] == 5

    def test_delete_not_found_returns_404(self):
        self._mock_session.execute.return_value.fetchone.return_value = None

        response = client.delete("/api/windingTargetMap/target_map_delete/999?co_id=1")

        assert response.status_code == 404
        self._mock_session.rollback.assert_called_once()


# =============================================================================
# GET /target_map_grid  (LAST-DATE resolution)
# =============================================================================


class TestTargetMapGrid(_Base):
    @staticmethod
    def _yarn_exec(*items):
        e = MagicMock()
        e.fetchall.return_value = [
            _mock_row(
                {
                    "item_id": i,
                    "item_code": f"Y-{i}",
                    "item_name": f"Yarn {i}",
                    "std_count": None,
                    "std_mr_pct": None,
                }
            )
            for i in items
        ]
        return e

    @staticmethod
    def _batch_cell(ref_id, param, value, effective_date):
        """One row of resolve_winding_grid_cells_batch_query (attr access)."""
        c = MagicMock()
        c.ref_id = ref_id
        c.param = param
        c.value = value
        c.effective_date = effective_date
        return c

    def test_grid_lists_yarn_refs_and_resolves_prod(self):
        batch = MagicMock()
        batch.fetchall.return_value = [
            self._batch_cell(4321, "prod", 42.5, "2026-08-19"),
        ]
        self._mock_session.execute.side_effect = [self._yarn_exec(4321), batch]

        response = client.get(
            "/api/windingTargetMap/target_map_grid"
            "?co_id=1&id_type=qid&value_role=standard&effective_date=2026-08-19"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["params"] == ["prod"]
        row = data["rows"][0]
        assert row["ref_id"] == 4321
        assert row["ref_code"] == "Y-4321"
        assert row["ref_name"] == "Yarn 4321"
        assert row["cells"]["prod"]["value"] == 42.5
        assert row["cells"]["prod"]["is_exact"] is True
        # Resolution is by on_date, branch-agnostic (no branch_id bind).
        resolve_params = _exec_params(self._mock_session, 1)
        assert resolve_params["on_date"] == date(2026, 8, 19)
        assert "branch_id" not in resolve_params

    def test_grid_last_date_resolution_marks_inherited_cell(self):
        """The batch query returns the newest effective_date <= on_date; a cell
        sourced from an EARLIER date is inherited, so is_exact is False."""
        batch = MagicMock()
        batch.fetchall.return_value = [
            self._batch_cell(4321, "prod", 38.0, "2026-07-01"),
        ]
        self._mock_session.execute.side_effect = [self._yarn_exec(4321), batch]

        response = client.get(
            "/api/windingTargetMap/target_map_grid"
            "?co_id=1&id_type=qid&value_role=target&effective_date=2026-08-19"
        )

        assert response.status_code == 200
        cells = response.json()["data"]["rows"][0]["cells"]
        assert cells["prod"]["value"] == 38.0
        assert cells["prod"]["source_date"] == "2026-07-01"
        assert cells["prod"]["is_exact"] is False

    def test_grid_omits_unresolved_cell(self):
        batch = MagicMock()
        batch.fetchall.return_value = [
            self._batch_cell(4321, "prod", 42.5, "2026-08-19"),
            self._batch_cell(4322, "prod", None, "2026-08-19"),  # NULL -> omitted
        ]
        self._mock_session.execute.side_effect = [
            self._yarn_exec(4321, 4322, 4323),
            batch,
        ]

        response = client.get(
            "/api/windingTargetMap/target_map_grid"
            "?co_id=1&id_type=qid&value_role=standard&effective_date=2026-08-19"
        )

        assert response.status_code == 200
        rows = {r["ref_id"]: r for r in response.json()["data"]["rows"]}
        assert "prod" in rows[4321]["cells"]
        assert rows[4322]["cells"] == {}  # value NULL
        assert rows[4323]["cells"] == {}  # nothing resolved at all

    def test_grid_invalid_value_role_returns_400(self):
        response = client.get(
            "/api/windingTargetMap/target_map_grid"
            "?co_id=1&id_type=qid&value_role=actual&effective_date=2026-08-19"
        )
        assert response.status_code == 400
        assert "value_role" in response.json()["detail"].lower()

    def test_grid_invalid_id_type_returns_400(self):
        response = client.get(
            "/api/windingTargetMap/target_map_grid"
            "?co_id=1&id_type=mcid&value_role=standard&effective_date=2026-08-19"
        )
        assert response.status_code == 400
        assert "id_type" in response.json()["detail"].lower()

    def test_grid_missing_co_id_returns_400(self):
        response = client.get(
            "/api/windingTargetMap/target_map_grid"
            "?id_type=qid&value_role=standard&effective_date=2026-08-19"
        )
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()


# =============================================================================
# POST /target_map_bulk_save
# =============================================================================


class TestTargetMapBulkSave(_Base):
    def _payload(self, cells, **overrides):
        body = {
            "co_id": 1,
            "branch_id": 2,
            "effective_date": "2026-08-19",
            "id_type": "qid",
            "value_role": "standard",
            "cells": cells,
        }
        body.update(overrides)
        return body

    def test_bulk_save_inserted_updated_cleared_in_one_txn(self):
        existing = MagicMock()
        existing.winding_target_map_id = 321

        no_existing = MagicMock()
        no_existing.fetchone.return_value = None
        found_update = MagicMock()
        found_update.fetchone.return_value = existing
        found_clear = MagicMock()
        found_clear.fetchone.return_value = existing

        # find(cell1), insert, find(cell2), update, find(cell3), clear
        self._mock_session.execute.side_effect = [
            no_existing, MagicMock(),
            found_update, MagicMock(),
            found_clear, MagicMock(),
        ]

        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            json=self._payload(
                [
                    {"ref_id": 4321, "param": "prod", "value": 42.5},  # insert
                    {"ref_id": 4322, "param": "prod", "value": 44.0},  # update
                    {"ref_id": 4323, "param": "prod", "value": None},  # clear
                ]
            ),
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data == {"inserted": 1, "updated": 1, "cleared": 1}
        self._mock_session.commit.assert_called_once()

        insert_params = _exec_params(self._mock_session, 1)
        assert insert_params["id_type"] == "qid"
        assert insert_params["value_role"] == "standard"
        assert insert_params["param"] == "prod"
        assert insert_params["ref_id"] == 4321
        assert insert_params["value"] == 42.5
        assert insert_params["branch_id"] == 2
        # Update targets the exact row the find returned.
        assert _exec_params(self._mock_session, 3)["id"] == 321

    def test_bulk_save_clear_with_no_existing_row_is_noop(self):
        no_existing = MagicMock()
        no_existing.fetchone.return_value = None
        self._mock_session.execute.return_value = no_existing

        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            json=self._payload([{"ref_id": 4321, "param": "prod", "value": None}]),
        )

        assert response.status_code == 200
        assert response.json()["data"] == {"inserted": 0, "updated": 0, "cleared": 0}
        # Only the find ran; nothing was written.
        assert self._mock_session.execute.call_count == 1

    def test_bulk_save_target_role_insert(self):
        no_existing = MagicMock()
        no_existing.fetchone.return_value = None
        self._mock_session.execute.side_effect = [no_existing, MagicMock()]

        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            json=self._payload(
                [{"ref_id": 4321, "param": "prod", "value": 40.0}],
                value_role="target",
            ),
        )

        assert response.status_code == 200
        assert response.json()["data"]["inserted"] == 1
        assert _exec_params(self._mock_session, 1)["value_role"] == "target"

    def test_bulk_save_invalid_param_returns_400(self):
        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            json=self._payload([{"ref_id": 4321, "param": "speed", "value": 200.0}]),
        )

        assert response.status_code == 400
        assert "invalid param" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_bulk_save_invalid_value_role_returns_400(self):
        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            json=self._payload(
                [{"ref_id": 4321, "param": "prod", "value": 1.0}], value_role="actual"
            ),
        )

        assert response.status_code == 400
        assert "value_role" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_bulk_save_invalid_id_type_returns_400(self):
        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            json=self._payload(
                [{"ref_id": 4321, "param": "prod", "value": 1.0}], id_type="mcid"
            ),
        )

        assert response.status_code == 400
        assert "id_type" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_bulk_save_negative_value_returns_400(self):
        self._mock_session.execute.return_value.fetchone.return_value = None

        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            json=self._payload([{"ref_id": 4321, "param": "prod", "value": -1.0}]),
        )

        assert response.status_code == 400
        assert "negative" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_bulk_save_nan_value_returns_400(self):
        self._mock_session.execute.return_value.fetchone.return_value = None

        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            content=json.dumps(
                self._payload([{"ref_id": 4321, "param": "prod", "value": float("nan")}])
            ),
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert "finite" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_bulk_save_bad_cell_rolls_back_whole_batch(self):
        """A later invalid cell aborts the batch — nothing is committed."""
        no_existing = MagicMock()
        no_existing.fetchone.return_value = None
        self._mock_session.execute.return_value = no_existing

        response = client.post(
            "/api/windingTargetMap/target_map_bulk_save",
            json=self._payload(
                [
                    {"ref_id": 4321, "param": "prod", "value": 42.5},  # valid
                    {"ref_id": 4322, "param": "eff", "value": 90.0},   # invalid
                ]
            ),
        )

        assert response.status_code == 400
        self._mock_session.commit.assert_not_called()
        self._mock_session.rollback.assert_called_once()


# =============================================================================
# grid_params_for contract
# =============================================================================


class TestGridParamsForContract:
    def test_locked_contract(self):
        assert grid_params_for("qid", "standard") == ["prod"]
        assert grid_params_for("qid", "target") == ["prod"]
        # No machine dimension, no 'actual' role in winding.
        assert grid_params_for("mcid", "standard") == []
        assert grid_params_for("qid", "actual") == []
        assert grid_params_for("qid", "bogus") == []
