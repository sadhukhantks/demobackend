"""Tests for the Jute Production Planning endpoints (SPEC §3).

Covers src/juteProduction/planning.py (prefix /api/juteProdPlanning).

Portal persona: DB + auth mocked (no real DB). get_tenant_db /
get_current_user_with_refresh are imported into the router module's namespace and
resolved by FastAPI via Depends, so we override them through
app.dependency_overrides keyed by those exact symbols (same approach as
test_beaming_target_map.py / test_weaving_*.py).

Covered (SPEC §3 "Tests"):
  * missing co_id -> 400 on planning_setup / planning_table / planning_save
  * planning_save happy path (header insert + plan_no stamp + line inserts, returns
    planning_id / plan_no, one commit)
  * duplicate plan_date -> 400, nothing committed
  * planning_by_id not found -> 404
  * norms_bulk_save happy path (insert what changed, skip what didn't)
  * norms_grid happy path (refs x params, resolved cells)
Plus the norm-param contract and the ORM table names.
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.juteProduction.planning import (
    get_current_user_with_refresh,
    get_tenant_db,
)
from src.main import app

client = TestClient(app)


def _mock_row(mapping: dict):
    """A SQLAlchemy-ish Row: ._mapping for dict() reads, attributes for direct reads."""
    row = MagicMock()
    row._mapping = mapping
    for key, value in mapping.items():
        setattr(row, key, value)
    return row


def _fetchone(value):
    exec_result = MagicMock()
    exec_result.fetchone.return_value = value
    return exec_result


def _fetchall(rows):
    exec_result = MagicMock()
    exec_result.fetchall.return_value = rows
    return exec_result


class _OverrideBase:
    """Shared dependency-override lifecycle for every endpoint test class."""

    def setup_method(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session

    def teardown_method(self):
        app.dependency_overrides.clear()


# =============================================================================
# Missing co_id -> 400
# =============================================================================


class TestMissingCoId(_OverrideBase):
    def test_planning_setup_missing_co_id_returns_400(self):
        response = client.get("/api/juteProdPlanning/planning_setup")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_planning_table_missing_co_id_returns_400(self):
        response = client.get("/api/juteProdPlanning/planning_table")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_planning_save_missing_co_id_returns_400(self):
        response = client.post(
            "/api/juteProdPlanning/planning_save",
            json={"plan_date": "2026-08-08", "loom_lines": []},
        )
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()


# =============================================================================
# planning_setup happy path
# =============================================================================


class TestPlanningSetup(_OverrideBase):
    """GET /api/juteProdPlanning/planning_setup"""

    def test_setup_returns_qualities_yarns_stages_and_norms(self):
        cloth_rows = [
            _mock_row(
                {
                    "item_id": 101,
                    "item_code": "SACK-A",
                    "item_name": "B.Twill 580g",
                    "item_grp_id": 625,
                    "grp_name": "SACKING",
                    "item_type_id": 5,
                }
            )
        ]
        yarn_rows = [
            _mock_row(
                {
                    "item_id": 201,
                    "item_code": "8LB",
                    "item_name": "8 lb Weft",
                    "item_grp_id": 700,
                    "grp_name": "YARN",
                    "yarn_count": 8.0,
                }
            )
        ]
        # Machine counts are keyed by LOWER(machine_type_name); 'Carding' is absent
        # from this tenant, which must resolve to 0 (not an error).
        machine_rows = [
            _mock_row({"machine_type_name": "loom", "machine_count": 40}),
            _mock_row({"machine_type_name": "spinning", "machine_count": 12}),
        ]
        norm_rows = [
            _mock_row(
                {
                    "id_type": "qid",
                    "ref_id": 101,
                    "param": "kg_per_loom_100",
                    "value": 120.5,
                    "effective_date": "2026-08-01",
                    "planning_norm_id": 9,
                }
            )
        ]
        # Execute order: cloth qualities, yarn items, machine counts, norms, latest plan.
        self._mock_session.execute.side_effect = [
            _fetchall(cloth_rows),
            _fetchall(yarn_rows),
            _fetchall(machine_rows),
            _fetchall(norm_rows),
            _fetchone(_mock_row({"planning_id": 55})),
        ]

        response = client.get(
            "/api/juteProdPlanning/planning_setup?co_id=1&branch_id=2&plan_date=2026-08-08"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cloth_qualities"][0]["item_id"] == 101
        assert data["cloth_qualities"][0]["grp_name"] == "SACKING"
        assert data["yarn_items"][0]["yarn_count"] == 8.0
        # All 7 stages always come back, in flow order.
        assert [s["stage_code"] for s in data["stages"]] == [
            "BATCHING",
            "CARDING",
            "DRAWING",
            "SPINNING",
            "WINDING",
            "BEAMING",
            "WEAVING",
        ]
        by_code = {s["stage_code"]: s for s in data["stages"]}
        assert by_code["WEAVING"]["machines_installed"] == 40
        assert by_code["SPINNING"]["machines_installed"] == 12
        # A machine type the tenant does not have -> count 0 (documented, not an error).
        assert by_code["CARDING"]["machines_installed"] == 0
        assert data["norms"] == [
            {"id_type": "qid", "ref_id": 101, "param": "kg_per_loom_100", "value": 120.5}
        ]
        assert data["latest_planning_id"] == 55


# =============================================================================
# planning_table happy path
# =============================================================================


class TestPlanningTable(_OverrideBase):
    """GET /api/juteProdPlanning/planning_table"""

    def test_table_returns_headers_with_totals(self):
        rows = [
            _mock_row(
                {
                    "planning_id": 12,
                    "plan_no": "JPP-00012",
                    "plan_date": "2026-08-08",
                    "plan_name": "S4 Plan",
                    "remarks": None,
                    "branch_id": 2,
                    "branch_name": "Factory",
                    "status_id": 1,
                    "updated_by": 7,
                    "updated_by_name": "Tester",
                    "updated_date_time": "2026-08-08 10:00:00",
                    "total_cloth_kg": 15000.0,
                    "raw_jute_kg": 18500.0,
                }
            )
        ]
        self._mock_session.execute.return_value = _fetchall(rows)

        response = client.get(
            "/api/juteProdPlanning/planning_table?co_id=1&branch_id=2"
            "&from_date=2026-08-01&to_date=2026-08-31"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data[0]["planning_id"] == 12
        assert data[0]["plan_no"] == "JPP-00012"
        assert data[0]["total_cloth_kg"] == 15000.0
        assert data[0]["raw_jute_kg"] == 18500.0


# =============================================================================
# planning_save
# =============================================================================


def _save_payload(**overrides):
    payload = {
        "co_id": 1,
        "branch_id": 2,
        "plan_date": "2026-08-08",
        "plan_name": "S4 Plan",
        "loom_lines": [
            {
                "product_group": "sacking",
                "quality_item_id": 101,
                "quality_desc": "B.Twill 580g",
                "looms_a": 10,
                "looms_b": 10,
                "looms_c": 8,
                "kg_per_loom_100": 100,
                "eff_pct": 85,
                "kg_per_loom": 85,
                "prod_a": 850,
                "prod_b": 850,
                "prod_c": 680,
                "warp_yarn_item_id": 201,
                "weft_yarn_item_id": 202,
                "warp_pct": 40,
                "weft_pct": 60,
            }
        ],
        "yarn_lines": [
            {
                "line_kind": "CLOTH",
                "section": "SACKING",
                "yarn_item_id": 201,
                "warp_weft": "wp",
                "req_kg_day": 952.0,
                "kg_per_frame_100": 500,
                "eff_pct": 90,
                "kg_per_frame": 450,
                "frames_required": 2.12,
                "frames_a": 0.8,
                "frames_b": 0.8,
                "frames_c": 0.6,
            }
        ],
        "dept_lines": [
            {
                "stage_code": "BATCHING",
                "output_kg_day": 17000,
                "waste_pct": 8,
                "input_kg_day": 18360,
                "kg_per_machine": 4000,
                "machines_required": 4.25,
                "machines_a": 2,
                "machines_b": 2,
                "machines_c": 1,
                "machines_installed": 5,
                "machines_maint": 1,
                "machines_ready": 4,
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestPlanningSave(_OverrideBase):
    """POST /api/juteProdPlanning/planning_save"""

    def test_create_happy_path_inserts_header_lines_and_stamps_plan_no(self):
        """No duplicate -> INSERT header -> stamp plan_no -> one INSERT per line."""
        insert_header = MagicMock()
        insert_header.lastrowid = 501
        # Execute order: duplicate check, header insert, plan_no stamp,
        # loom line insert, yarn line insert, dept line insert.
        self._mock_session.execute.side_effect = [
            _fetchone(None),   # no duplicate active header for this date
            insert_header,     # INSERT jute_prod_planning
            MagicMock(),       # UPDATE ... plan_no = CONCAT('JPP-', LPAD(...))
            MagicMock(),       # INSERT loom line
            MagicMock(),       # INSERT yarn line
            MagicMock(),       # INSERT dept line
        ]

        response = client.post(
            "/api/juteProdPlanning/planning_save", json=_save_payload()
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["planning_id"] == 501
        assert data["plan_no"] == "JPP-00501"
        self._mock_session.commit.assert_called_once()

        calls = self._mock_session.execute.call_args_list
        assert len(calls) == 6

        # The header INSERT carries the plan header + status 1 (no approval workflow).
        header_params = calls[1].args[1]
        assert header_params["co_id"] == 1
        assert header_params["branch_id"] == 2
        assert str(header_params["plan_date"]) == "2026-08-08"
        assert header_params["status_id"] == 1
        assert header_params["updated_by"] == 7

        # plan_no is stamped from the auto-increment id (SPEC §3).
        stamp_sql = str(calls[2].args[0])
        assert "LPAD" in stamp_sql.upper()
        assert "plan_no" in stamp_sql
        assert calls[2].args[1]["planning_id"] == 501

        # Lines are re-inserted against the new planning_id with a fresh line_order,
        # and discriminator tokens are normalised to upper case.
        loom_params = calls[3].args[1]
        assert loom_params["planning_id"] == 501
        assert loom_params["line_order"] == 0
        assert loom_params["product_group"] == "SACKING"
        assert loom_params["kg_per_loom"] == 85.0

        yarn_params = calls[4].args[1]
        assert yarn_params["planning_id"] == 501
        assert yarn_params["warp_weft"] == "WP"

        # stage_seq / stage_label are resolved from PLANNING_STAGES.
        dept_params = calls[5].args[1]
        assert dept_params["stage_seq"] == 1
        assert dept_params["stage_code"] == "BATCHING"
        assert dept_params["stage_label"] == "Batching (Spreader)"
        assert dept_params["input_kg_day"] == 18360.0

    def test_duplicate_plan_date_returns_400(self):
        """Another ACTIVE header on the same (co_id, branch_id, plan_date) -> 400."""
        self._mock_session.execute.side_effect = [
            _fetchone(_mock_row({"planning_id": 99})),
        ]

        response = client.post(
            "/api/juteProdPlanning/planning_save", json=_save_payload()
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "A plan already exists for this date"
        self._mock_session.commit.assert_not_called()
        self._mock_session.rollback.assert_called_once()

    def test_update_replaces_lines_wholesale(self):
        """planning_id present -> existence check, UPDATE header, DELETE all three
        child sections, then re-INSERT the supplied lines."""
        self._mock_session.execute.side_effect = [
            _fetchone(
                _mock_row({"planning_id": 501, "co_id": 1, "plan_no": "JPP-00501"})
            ),                 # existence check
            _fetchone(None),   # no other plan on that date
            MagicMock(),       # UPDATE header
            MagicMock(),       # DELETE loom lines
            MagicMock(),       # DELETE yarn lines
            MagicMock(),       # DELETE dept lines
            MagicMock(),       # INSERT loom line
            MagicMock(),       # INSERT yarn line
            MagicMock(),       # INSERT dept line
        ]

        response = client.post(
            "/api/juteProdPlanning/planning_save", json=_save_payload(planning_id=501)
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["planning_id"] == 501
        # An update keeps the existing plan_no (it is stamped once, on insert).
        assert data["plan_no"] == "JPP-00501"
        self._mock_session.commit.assert_called_once()

        calls = self._mock_session.execute.call_args_list
        assert "DELETE FROM jute_prod_planning_loom" in str(calls[3].args[0])
        assert "DELETE FROM jute_prod_planning_yarn" in str(calls[4].args[0])
        assert "DELETE FROM jute_prod_planning_dept" in str(calls[5].args[0])

    def test_update_norms_inserts_only_changed_values(self):
        """update_norms=true upserts norms at effective_date = plan_date, inserting a
        row only where the value differs from the currently-resolved one."""
        insert_header = MagicMock()
        insert_header.lastrowid = 502

        # One loom line (qid, quality 101) + one dept line, no yarn line -> norm
        # entries: qid kg_per_loom_100 / warp_pct / weft_pct / warp_yarn_item /
        # weft_yarn_item / std_eff, then stage kg_per_machine / waste_pct.
        payload = _save_payload(update_norms=True, yarn_lines=[])

        # Every norm resolve returns "unchanged" except the first one (None = no norm
        # exists yet -> insert).
        def _resolved(value):
            return _fetchone(_mock_row({"planning_norm_id": 1, "value": value}))

        self._mock_session.execute.side_effect = [
            _fetchone(None),   # duplicate check
            insert_header,     # INSERT header
            MagicMock(),       # stamp plan_no
            MagicMock(),       # INSERT loom line
            MagicMock(),       # INSERT dept line
            _fetchone(None), MagicMock(),   # qid kg_per_loom_100 -> insert
            _resolved(40.0),                # qid warp_pct      -> unchanged, skip
            _resolved(60.0),                # qid weft_pct      -> unchanged, skip
            _resolved(201.0),               # qid warp_yarn_item-> unchanged, skip
            _resolved(202.0),               # qid weft_yarn_item-> unchanged, skip
            _resolved(85.0),                # qid std_eff       -> unchanged, skip
            _resolved(4000.0),              # stage kg_per_machine -> unchanged, skip
            _resolved(8.0),                 # stage waste_pct      -> unchanged, skip
        ]

        response = client.post("/api/juteProdPlanning/planning_save", json=payload)

        assert response.status_code == 200
        assert response.json()["data"]["norms_inserted"] == 1
        self._mock_session.commit.assert_called_once()

        norm_insert_params = self._mock_session.execute.call_args_list[6].args[1]
        assert norm_insert_params["id_type"] == "qid"
        assert norm_insert_params["ref_id"] == 101
        assert norm_insert_params["param"] == "kg_per_loom_100"
        assert norm_insert_params["value"] == 100.0
        assert str(norm_insert_params["effective_date"]) == "2026-08-08"
        assert norm_insert_params["value_role"] == "standard"

    def test_invalid_dept_stage_returns_400_before_any_write(self):
        """Lines are normalised (and validated) BEFORE the first DB write, so a bad
        stage token 400s without executing anything."""
        payload = _save_payload(dept_lines=[{"stage_code": "NOT-A-STAGE"}])

        response = client.post("/api/juteProdPlanning/planning_save", json=payload)

        assert response.status_code == 400
        assert "stage" in response.json()["detail"].lower()
        self._mock_session.execute.assert_not_called()
        self._mock_session.commit.assert_not_called()


# =============================================================================
# planning_by_id
# =============================================================================


class TestPlanningById(_OverrideBase):
    """GET /api/juteProdPlanning/planning_by_id/{planning_id}"""

    def test_not_found_returns_404(self):
        self._mock_session.execute.side_effect = [_fetchone(None)]

        response = client.get("/api/juteProdPlanning/planning_by_id/999?co_id=1")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_returns_header_with_three_line_sections(self):
        header = _mock_row(
            {
                "planning_id": 12,
                "co_id": 1,
                "branch_id": 2,
                "branch_name": "Factory",
                "plan_date": "2026-08-08",
                "plan_no": "JPP-00012",
                "plan_name": "S4 Plan",
                "remarks": None,
                "status_id": 1,
                "active": 1,
                "updated_by": 7,
                "updated_by_name": "Tester",
                "updated_date_time": "2026-08-08 10:00:00",
            }
        )
        loom_rows = [
            _mock_row(
                {
                    "planning_loom_id": 1,
                    "planning_id": 12,
                    "product_group": "SACKING",
                    "quality_item_id": 101,
                    "quality_desc": "B.Twill 580g",
                    "quality_item_code": "SACK-A",
                    "quality_item_name": "B.Twill 580g",
                    "looms_a": 10,
                    "looms_b": 10,
                    "looms_c": 8,
                    "kg_per_loom_100": 100,
                    "eff_pct": 85,
                    "kg_per_loom": 85,
                    "prod_a": 850,
                    "prod_b": 850,
                    "prod_c": 680,
                    "warp_yarn_item_id": 201,
                    "weft_yarn_item_id": 202,
                    "warp_pct": 40,
                    "weft_pct": 60,
                    "line_order": 0,
                    "remarks": None,
                }
            )
        ]
        yarn_rows = [
            _mock_row(
                {
                    "planning_yarn_id": 3,
                    "planning_id": 12,
                    "line_kind": "CLOTH",
                    "section": "SACKING",
                    "yarn_item_id": 201,
                    "yarn_desc": None,
                    "yarn_item_code": "8LB",
                    "yarn_item_name": "8 lb Weft",
                    "warp_weft": "WP",
                    "pct_of_prod": None,
                    "req_kg_a": 340,
                    "req_kg_b": 340,
                    "req_kg_c": 272,
                    "req_kg_day": 952,
                    "kg_per_frame_100": 500,
                    "eff_pct": 90,
                    "kg_per_frame": 450,
                    "frames_required": 2.12,
                    "frames_a": 0.8,
                    "frames_b": 0.8,
                    "frames_c": 0.6,
                    "priority": None,
                    "order_mt": None,
                    "days_needed": None,
                    "delivery_date": None,
                    "line_order": 0,
                    "remarks": None,
                }
            )
        ]
        dept_rows = [
            _mock_row(
                {
                    "planning_dept_id": 5,
                    "planning_id": 12,
                    "stage_seq": 1,
                    "stage_code": "BATCHING",
                    "stage_label": "Batching (Spreader)",
                    "output_kg_day": 17000,
                    "waste_pct": 8,
                    "input_kg_day": 18360,
                    "kg_per_machine": 4000,
                    "machines_required": 4.25,
                    "machines_a": 2,
                    "machines_b": 2,
                    "machines_c": 1,
                    "machines_installed": 5,
                    "machines_maint": 1,
                    "machines_ready": 4,
                    "line_order": 0,
                    "remarks": None,
                }
            )
        ]
        self._mock_session.execute.side_effect = [
            _fetchone(header),
            _fetchall(loom_rows),
            _fetchall(yarn_rows),
            _fetchall(dept_rows),
        ]

        response = client.get("/api/juteProdPlanning/planning_by_id/12?co_id=1")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["header"]["plan_no"] == "JPP-00012"
        assert data["loom_lines"][0]["prod_a"] == 850.0
        assert data["yarn_lines"][0]["warp_weft"] == "WP"
        assert data["dept_lines"][0]["input_kg_day"] == 18360.0


# =============================================================================
# Norms
# =============================================================================


class TestNormsBulkSave(_OverrideBase):
    """POST /api/juteProdPlanning/norms_bulk_save"""

    def test_inserts_changed_and_skips_unchanged(self):
        """Entry 1 has no resolving row -> INSERT; entry 2 equals the resolved value
        -> skipped (no duplicate norm row). One commit for the whole batch."""
        unchanged = _fetchone(_mock_row({"planning_norm_id": 4, "value": 90.0}))
        self._mock_session.execute.side_effect = [
            _fetchone(None), MagicMock(),   # entry 1: resolve -> none, then insert
            unchanged,                      # entry 2: resolve -> same value, skip
        ]

        payload = {
            "co_id": 1,
            "branch_id": 2,
            "effective_date": "2026-08-08",
            "entries": [
                {"id_type": "qid", "ref_id": 101, "param": "kg_per_loom_100", "value": 120.5},
                {"id_type": "yid", "ref_id": 201, "param": "std_eff", "value": 90.0},
            ],
        }

        response = client.post("/api/juteProdPlanning/norms_bulk_save", json=payload)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["inserted"] == 1
        assert data["skipped"] == 1
        self._mock_session.commit.assert_called_once()

        insert_params = self._mock_session.execute.call_args_list[1].args[1]
        assert insert_params["co_id"] == 1
        assert insert_params["branch_id"] == 2
        assert insert_params["id_type"] == "qid"
        assert insert_params["ref_id"] == 101
        assert insert_params["param"] == "kg_per_loom_100"
        assert insert_params["value"] == 120.5
        assert insert_params["value_role"] == "standard"
        assert str(insert_params["effective_date"]) == "2026-08-08"

    def test_invalid_param_for_id_type_returns_400(self):
        payload = {
            "co_id": 1,
            "effective_date": "2026-08-08",
            # kg_per_frame_100 is a YARN (yid) param, not a cloth-quality (qid) one.
            "entries": [
                {"id_type": "qid", "ref_id": 101, "param": "kg_per_frame_100", "value": 5},
            ],
        }

        response = client.post("/api/juteProdPlanning/norms_bulk_save", json=payload)

        assert response.status_code == 400
        assert "invalid param" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()

    def test_negative_value_returns_400(self):
        payload = {
            "co_id": 1,
            "effective_date": "2026-08-08",
            "entries": [
                {"id_type": "stage", "ref_id": 4, "param": "waste_pct", "value": -2},
            ],
        }

        response = client.post("/api/juteProdPlanning/norms_bulk_save", json=payload)

        assert response.status_code == 400
        assert "negative" in response.json()["detail"].lower()
        self._mock_session.commit.assert_not_called()


class TestNormsGrid(_OverrideBase):
    """GET /api/juteProdPlanning/norms_grid"""

    def test_qid_grid_returns_refs_and_resolved_cells(self):
        cloth_rows = [
            _mock_row(
                {
                    "item_id": 101,
                    "item_code": "SACK-A",
                    "item_name": "B.Twill 580g",
                    "item_grp_id": 625,
                    "grp_name": "SACKING",
                    "item_type_id": 5,
                }
            )
        ]
        norm_rows = [
            _mock_row(
                {
                    "id_type": "qid",
                    "ref_id": 101,
                    "param": "kg_per_loom_100",
                    "value": 120.5,
                    "effective_date": "2026-08-08",
                    "planning_norm_id": 9,
                }
            ),
            _mock_row(
                {
                    "id_type": "qid",
                    "ref_id": 101,
                    "param": "std_eff",
                    "value": 85.0,
                    "effective_date": "2026-07-01",
                    "planning_norm_id": 3,
                }
            ),
        ]
        # Execute order: refs (cloth qualities), then ONE bulk norm resolution.
        self._mock_session.execute.side_effect = [
            _fetchall(cloth_rows),
            _fetchall(norm_rows),
        ]

        response = client.get(
            "/api/juteProdPlanning/norms_grid?co_id=1&id_type=qid&on_date=2026-08-08"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["params"] == [
            "kg_per_loom_100",
            "warp_pct",
            "weft_pct",
            "warp_yarn_item",
            "weft_yarn_item",
            "std_eff",
        ]
        row = data["rows"][0]
        assert row["ref_id"] == 101
        assert row["ref_code"] == "SACK-A"
        assert row["cells"]["kg_per_loom_100"]["value"] == 120.5
        # Set exactly on this date vs inherited from an earlier one.
        assert row["cells"]["kg_per_loom_100"]["is_exact"] is True
        assert row["cells"]["std_eff"]["is_exact"] is False
        assert row["cells"]["std_eff"]["source_date"] == "2026-07-01"
        # Params with no resolving row are omitted from the cell map.
        assert "warp_pct" not in row["cells"]

    def test_stage_grid_uses_the_seven_constant_stages(self):
        """id_type='stage' takes its refs from PLANNING_STAGES (no ref table), so only
        the norm resolution hits the DB."""
        self._mock_session.execute.side_effect = [_fetchall([])]

        response = client.get(
            "/api/juteProdPlanning/norms_grid?co_id=1&id_type=stage&on_date=2026-08-08"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["params"] == ["kg_per_machine", "waste_pct"]
        assert [r["ref_id"] for r in data["rows"]] == [1, 2, 3, 4, 5, 6, 7]
        assert data["rows"][6]["ref_code"] == "WEAVING"
        assert data["rows"][6]["ref_group"] == "Loom"

    def test_invalid_id_type_returns_400(self):
        response = client.get("/api/juteProdPlanning/norms_grid?co_id=1&id_type=mcid")
        assert response.status_code == 400
        assert "id_type" in response.json()["detail"].lower()


class TestNormsList(_OverrideBase):
    """GET /api/juteProdPlanning/norms_list"""

    def test_returns_history_rows(self):
        rows = [
            _mock_row(
                {
                    "planning_norm_id": 9,
                    "co_id": 1,
                    "branch_id": None,
                    "effective_date": "2026-08-08",
                    "ref_id": 101,
                    "id_type": "qid",
                    "value_role": "standard",
                    "param": "kg_per_loom_100",
                    "value": 120.5,
                    "active": 1,
                    "updated_by": 7,
                    "updated_date_time": "2026-08-08 10:00:00",
                }
            )
        ]
        self._mock_session.execute.return_value = _fetchall(rows)

        response = client.get(
            "/api/juteProdPlanning/norms_list?co_id=1&id_type=qid&ref_id=101"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data[0]["planning_norm_id"] == 9
        assert data[0]["value"] == 120.5
        assert data[0]["value_role"] == "standard"


# =============================================================================
# Contracts
# =============================================================================


class TestNormParamContract:
    """norm_params_for LOCKED CONTRACT (SPEC §2.5)."""

    def test_locked_contract(self):
        from src.juteProduction.planning import norm_params_for

        assert norm_params_for("qid") == [
            "kg_per_loom_100",
            "warp_pct",
            "weft_pct",
            "warp_yarn_item",
            "weft_yarn_item",
            "std_eff",
        ]
        assert norm_params_for("yid") == ["kg_per_frame_100", "std_eff"]
        assert norm_params_for("stage") == ["kg_per_machine", "waste_pct"]
        assert norm_params_for("mcid") == []  # unknown dimension

    def test_planning_stages_constant(self):
        from src.juteProduction.constants import PLANNING_STAGES

        assert PLANNING_STAGES == (
            (1, "BATCHING", "Batching (Spreader)", "Spreader"),
            (2, "CARDING", "Carding", "Carding"),
            (3, "DRAWING", "Drawing", "Drawing"),
            (4, "SPINNING", "Spinning", "Spinning"),
            (5, "WINDING", "Winding", "Winding"),
            (6, "BEAMING", "Beaming", "Beaming"),
            (7, "WEAVING", "Weaving", "Loom"),
        )


class TestPlanningOrmModels:
    """The five ORM models mirror the migration's table names."""

    def test_table_names(self):
        from src.juteProduction.planning_models import (
            JuteProdPlanning,
            JuteProdPlanningDept,
            JuteProdPlanningLoom,
            JuteProdPlanningNorm,
            JuteProdPlanningYarn,
        )

        assert JuteProdPlanning.__tablename__ == "jute_prod_planning"
        assert JuteProdPlanningLoom.__tablename__ == "jute_prod_planning_loom"
        assert JuteProdPlanningYarn.__tablename__ == "jute_prod_planning_yarn"
        assert JuteProdPlanningDept.__tablename__ == "jute_prod_planning_dept"
        assert JuteProdPlanningNorm.__tablename__ == "jute_prod_planning_norm"
