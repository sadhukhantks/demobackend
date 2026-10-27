"""Tests for the Jute SQC Standard Moisture Master (src/juteSQC/std_mr_master.py).

Covers the master CRUD endpoints (setup / table / save / delete) and the two shared helpers
the four yarn-group-keyed SQC pages import:

  * resolve_std_mr_pct — returns the master value, and raises the SINGLE UI-actionable 400
    when the (co, yarn group, process) row is missing (owner decision: no constant fallback).
  * std_mr_map_for_process — the {item_grp_id: std_mr_pct} read-only map.

Endpoint tests use FastAPI dependency_overrides for get_tenant_db + get_current_user_with_refresh.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteSQC.std_mr_master import resolve_std_mr_pct, std_mr_map_for_process
from src.main import app

client = TestClient(app)


def _mock_row(mapping: dict):
    """A result row exposing BOTH ._mapping (dict-style readers) and attributes."""
    row = MagicMock()
    row._mapping = mapping
    row.configure_mock(**mapping)
    return row


GROUP_ROW = _mock_row({"item_grp_id": 7, "item_grp_name": "HESSIAN YARN", "co_id": 1})


# ===========================================================================
# UNIT — the shared helpers
# ===========================================================================
class TestSharedHelpers:
    def test_resolve_returns_master_value(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _mock_row({"std_mr_pct": 18.5})
        assert resolve_std_mr_pct(db, 1, 7, "BREAKER", "HESSIAN YARN") == 18.5

    def test_resolve_blocks_with_400_naming_group_and_process(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        with pytest.raises(HTTPException) as exc:
            resolve_std_mr_pct(db, 1, 7, "FIN_DRAW", "HESSIAN YARN")
        assert exc.value.status_code == 400
        assert "No standard moisture set for yarn group 'HESSIAN YARN'" in exc.value.detail
        assert "Finisher Drawing" in exc.value.detail
        assert "Std Moisture Master" in exc.value.detail

    def test_resolve_falls_back_to_group_id_label(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        with pytest.raises(HTTPException) as exc:
            resolve_std_mr_pct(db, 1, 7, "CARDING")
        assert "group #7" in exc.value.detail

    def test_map_for_process_is_id_keyed_floats(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [
            _mock_row({"item_grp_id": 7, "std_mr_pct": 16.0}),
            _mock_row({"item_grp_id": 9, "std_mr_pct": 20.0}),
        ]
        assert std_mr_map_for_process(db, 1, "BREAKER") == {7: 16.0, 9: 20.0}


# ===========================================================================
# ENDPOINT — dependency_overrides
# ===========================================================================
class TestStdMrMasterEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # -- setup --------------------------------------------------------------
    def test_setup_success(self):
        self._mock_session.execute.return_value.fetchall.return_value = [GROUP_ROW]

        response = client.get("/api/juteSQC/std_mr_master/setup?co_id=1")

        assert response.status_code == 200
        data = response.json()["data"]
        assert {"code": "BREAKER", "label": "Breaker Card"} in data["processes"]
        assert [p["code"] for p in data["processes"]] == [
            "BREAKER",
            "CARDING",
            "DRAWHEAD",
            "FIN_DRAW",
        ]
        assert data["item_groups"][0]["item_grp_id"] == 7

    def test_setup_missing_co_id(self):
        response = client.get("/api/juteSQC/std_mr_master/setup")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    # -- table --------------------------------------------------------------
    def test_table_success(self):
        self._mock_session.execute.return_value.fetchall.return_value = [
            _mock_row(
                {
                    "std_mr_id": 3,
                    "co_id": 1,
                    "item_grp_id": 7,
                    "item_grp_name": "HESSIAN YARN",
                    "process": "CARDING",
                    "std_mr_pct": 20.0,
                    "active": 1,
                }
            )
        ]

        response = client.get("/api/juteSQC/std_mr_master/table?co_id=1&process=CARDING")

        assert response.status_code == 200
        row = response.json()["data"][0]
        assert row["std_mr_id"] == 3
        assert row["std_mr_pct"] == 20.0
        assert row["process_label"] == "Inter Card / Tow Breaker / Hopper"

    def test_table_missing_co_id(self):
        response = client.get("/api/juteSQC/std_mr_master/table")
        assert response.status_code == 400

    # -- save ---------------------------------------------------------------
    def _valid_group(self):
        self._mock_session.execute.return_value.fetchall.return_value = [GROUP_ROW]

    def test_save_insert_when_absent(self):
        self._valid_group()
        self._mock_session.query.return_value.filter.return_value.first.return_value = None
        self._mock_session.refresh = MagicMock(
            side_effect=lambda rec: setattr(rec, "std_mr_id", 11)
        )

        response = client.post(
            "/api/juteSQC/std_mr_master/save",
            json={"co_id": 1, "item_grp_id": 7, "process": "BREAKER", "std_mr_pct": 16.5},
        )

        assert response.status_code == 200
        assert response.json()["data"]["std_mr_id"] == 11
        saved = self._mock_session.add.call_args[0][0]
        assert saved.process == "BREAKER"
        assert saved.std_mr_pct == 16.5
        assert saved.active == 1

    def test_save_revives_and_updates_existing_row(self):
        """The unique key is (co, group, process) — a soft-deleted row is updated + revived,
        never re-inserted (that would be a duplicate-key 500)."""
        self._valid_group()
        existing = MagicMock(std_mr_id=4, active=0)
        self._mock_session.query.return_value.filter.return_value.first.return_value = existing

        response = client.post(
            "/api/juteSQC/std_mr_master/save",
            json={
                "co_id": 1,
                "item_grp_id": 7,
                "process": "BREAKER",
                "std_mr_pct": 17.0,
                "std_mr_id": None,
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["std_mr_id"] == 4
        assert existing.std_mr_pct == 17.0
        assert existing.active == 1
        self._mock_session.add.assert_not_called()

    def test_save_invalid_process_400(self):
        response = client.post(
            "/api/juteSQC/std_mr_master/save",
            json={"co_id": 1, "item_grp_id": 7, "process": "SPINNING", "std_mr_pct": 16.0},
        )
        assert response.status_code == 400
        assert "BREAKER" in response.json()["detail"]

    @pytest.mark.parametrize("pct", [0, -1, 100, 250.5])
    def test_save_out_of_range_pct_400(self, pct):
        response = client.post(
            "/api/juteSQC/std_mr_master/save",
            json={"co_id": 1, "item_grp_id": 7, "process": "BREAKER", "std_mr_pct": pct},
        )
        assert response.status_code == 400
        assert "std_mr_pct" in response.json()["detail"]

    def test_save_unknown_group_400(self):
        self._valid_group()  # only group 7 is a live jute-yarn group of co 1
        response = client.post(
            "/api/juteSQC/std_mr_master/save",
            json={"co_id": 1, "item_grp_id": 99, "process": "BREAKER", "std_mr_pct": 16.0},
        )
        assert response.status_code == 400
        assert "jute-yarn item group" in response.json()["detail"]

    # -- delete -------------------------------------------------------------
    def test_delete_soft_deletes(self):
        self._mock_session.execute.return_value.fetchone.return_value = _mock_row(
            {"std_mr_id": 4, "co_id": 1, "item_grp_id": 7, "process": "BREAKER",
             "std_mr_pct": 16.0, "active": 1}
        )
        record = MagicMock(active=1)
        self._mock_session.get.return_value = record

        response = client.post(
            "/api/juteSQC/std_mr_master/delete", json={"co_id": 1, "std_mr_id": 4}
        )

        assert response.status_code == 200
        assert response.json()["data"] == {"std_mr_id": 4, "active": 0}
        assert record.active == 0
        self._mock_session.commit.assert_called_once()

    def test_delete_other_company_404(self):
        self._mock_session.execute.return_value.fetchone.return_value = None
        response = client.post(
            "/api/juteSQC/std_mr_master/delete", json={"co_id": 2, "std_mr_id": 4}
        )
        assert response.status_code == 404
