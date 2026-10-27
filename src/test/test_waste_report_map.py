"""Tests for the Wastage Report Mapping master (TKT-000183).

Tests for src/juteProduction/waste_report_map.py (prefix /api/wasteReportMap).

Portal persona: DB + auth mocked (no real DB). get_tenant_db /
get_current_user_with_refresh are overridden through app.dependency_overrides
keyed by the symbols the router module imported (same pattern as
test_winding_target_map.py). Each db.execute call is answered in order from a
side_effect list, so every test also pins the statement sequence.

Covered: setup / list shape, create / update / deactivate happy paths, group
propagation (same transaction, excludes the saved row), every validation
error (denominators, group code format + reserved OTHER, group name, item and
department company guards, display order), the NULL-safe duplicate guard on
create and on update (excluding the row itself), and 404s for unknown /
other-company rows.
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.juteProduction.constants import (
    JUTE_WASTAGE_ITEM_TYPE_ID,
    WASTE_REPORT_DENOM_LABELS,
    WASTE_REPORT_DENOMS,
)
from src.juteProduction.waste_report_map import (
    denominator_options,
    get_current_user_with_refresh,
    get_tenant_db,
    normalize_group_code,
)
from src.juteProduction.waste_report_map_query import (
    find_duplicate_map_row_query,
    propagate_group_fields_query,
)
from src.main import app

client = TestClient(app)

BASE = "/api/wasteReportMap"


def _row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _res(fetchone=None, fetchall=None, lastrowid=None, rowcount=0):
    """A mocked db.execute() result."""
    res = MagicMock()
    res.fetchone.return_value = fetchone
    res.fetchall.return_value = fetchall if fetchall is not None else []
    res.lastrowid = lastrowid
    res.rowcount = rowcount
    return res


def _sql(mock_session, idx: int) -> str:
    return str(mock_session.execute.call_args_list[idx].args[0])


def _params(mock_session, idx: int) -> dict:
    return mock_session.execute.call_args_list[idx].args[1]


def _payload(**overrides) -> dict:
    body = {
        "co_id": 1,
        "group_code": "line_waste",
        "group_name": "  Line Waste ",
        "day_denom": "WINDING",
        "total_denom": "SPINNING",
        "item_id": 270197,
        "dept_id": None,
        "col_label": "",
        "display_order": 15,
    }
    body.update(overrides)
    return body


class _Base:
    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        app.dependency_overrides[get_tenant_db] = lambda: self.db

    def teardown_method(self):
        app.dependency_overrides.clear()


# =============================================================================
# GET /setup
# =============================================================================


class TestSetup(_Base):
    def test_setup_returns_items_departments_denominators_groups(self):
        self.db.execute.side_effect = [
            _res(fetchall=[_row({"item_id": 270197, "item_code": "3", "item_name": "Sweepings"})]),
            _res(
                fetchall=[
                    _row(
                        {
                            "dept_id": 3,
                            "dept_desc": "Batching Dept",
                            "dept_code": "BAT",
                            "branch_id": 2,
                            "branch_name": "Factory",
                        }
                    )
                ]
            ),
            _res(
                fetchall=[
                    _row(
                        {
                            "group_code": "SWEEP",
                            "group_name": "Spinning Sweep",
                            "day_denom": "SPINNING",
                            "total_denom": "SPINNING",
                            "display_order": 80,
                            "item_count": 1,
                        }
                    )
                ]
            ),
        ]

        response = client.get(f"{BASE}/setup?co_id=1")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["waste_items"][0]["item_name"] == "Sweepings"
        assert data["departments"][0]["branch_name"] == "Factory"
        assert [d["code"] for d in data["denominators"]] == list(WASTE_REPORT_DENOMS)
        assert data["denominators"][0]["label"] == WASTE_REPORT_DENOM_LABELS[WASTE_REPORT_DENOMS[0]]
        assert data["groups"] == [
            {
                "group_code": "SWEEP",
                "group_name": "Spinning Sweep",
                "day_denom": "SPINNING",
                "total_denom": "SPINNING",
                "display_order": 80,
                "item_count": 1,
            }
        ]
        # Items are company-scoped jute-wastage items; departments and groups by company.
        assert _params(self.db, 0) == {"co_id": 1, "item_type_id": JUTE_WASTAGE_ITEM_TYPE_ID}
        assert _params(self.db, 1) == {"co_id": 1}
        assert _params(self.db, 2) == {"co_id": 1}

    def test_setup_missing_co_id_returns_400(self):
        response = client.get(f"{BASE}/setup")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"]
        self.db.execute.assert_not_called()


# =============================================================================
# GET /list
# =============================================================================


class TestList(_Base):
    def test_list_returns_rows_with_labels(self):
        self.db.execute.side_effect = [
            _res(
                fetchall=[
                    _row(
                        {
                            "waste_report_map_id": 1,
                            "co_id": 1,
                            "group_code": "SWEEP",
                            "group_name": "Spinning Sweep",
                            "day_denom": "SPINNING",
                            "total_denom": "WINDING",
                            "item_id": 270197,
                            "item_code": "3",
                            "item_name": "Sweepings",
                            "dept_id": None,
                            "dept_desc": None,
                            "branch_id": None,
                            "branch_name": None,
                            "col_label": None,
                            "display_order": 80,
                            "active": 1,
                            "updated_by": 1,
                            "updated_date_time": "2026-08-26 22:50:02",
                        }
                    )
                ]
            )
        ]

        response = client.get(f"{BASE}/list?co_id=1")

        assert response.status_code == 200
        row = response.json()["data"][0]
        assert row["waste_report_map_id"] == 1
        assert row["day_denom_label"] == WASTE_REPORT_DENOM_LABELS["SPINNING"]
        assert row["total_denom_label"] == WASTE_REPORT_DENOM_LABELS["WINDING"]
        assert row["dept_id"] is None
        assert row["display_order"] == 80
        assert _params(self.db, 0) == {"co_id": 1}
        assert "m.active = 1" in _sql(self.db, 0)

    def test_list_missing_co_id_returns_400(self):
        response = client.get(f"{BASE}/list")
        assert response.status_code == 400

    def test_list_invalid_co_id_returns_400(self):
        response = client.get(f"{BASE}/list?co_id=abc")
        assert response.status_code == 400


# =============================================================================
# POST /create
# =============================================================================


class TestCreate(_Base):
    def test_create_any_department_happy_path(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row({"item_id": 270197})),  # item guard
            _res(fetchone=None),  # duplicate guard
            _res(lastrowid=42),  # insert
            _res(rowcount=2),  # propagate
        ]

        response = client.post(f"{BASE}/create", json=_payload())

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "waste_report_map_id": 42,
            "group_code": "LINE_WASTE",
            "group_rows_updated": 2,
        }
        # Item guard: company + jute wastage type.
        assert _params(self.db, 0) == {
            "item_id": 270197,
            "co_id": 1,
            "item_type_id": JUTE_WASTAGE_ITEM_TYPE_ID,
        }
        # No department -> no department guard; duplicate check binds NULL dept.
        assert _params(self.db, 1) == {
            "co_id": 1,
            "item_id": 270197,
            "dept_id": None,
            "exclude_id": None,
        }
        insert = _params(self.db, 2)
        assert "INSERT INTO jute_waste_report_map" in _sql(self.db, 2)
        assert insert["group_code"] == "LINE_WASTE"
        assert insert["group_name"] == "Line Waste"
        assert insert["day_denom"] == "WINDING"
        assert insert["total_denom"] == "SPINNING"
        assert insert["col_label"] is None
        assert insert["dept_id"] is None
        assert insert["display_order"] == 15
        assert insert["updated_by"] == 7
        # Group-level fields pushed to the rest of the group, excluding the new row.
        prop = _params(self.db, 3)
        assert prop == {
            "co_id": 1,
            "group_code": "LINE_WASTE",
            "group_name": "Line Waste",
            "day_denom": "WINDING",
            "total_denom": "SPINNING",
            "display_order": 15,
            "updated_by": 7,
            "id": 42,
        }
        self.db.commit.assert_called_once()
        self.db.rollback.assert_not_called()

    def test_create_with_department_checks_department(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row({"item_id": 270197})),
            _res(fetchone=_row({"dept_id": 3})),  # dept guard
            _res(fetchone=None),
            _res(lastrowid=43),
            _res(rowcount=0),
        ]

        response = client.post(
            f"{BASE}/create", json=_payload(dept_id=3, col_label=" Batching ")
        )

        assert response.status_code == 200, response.text
        assert _params(self.db, 1) == {"dept_id": 3, "co_id": 1}
        assert _params(self.db, 2)["dept_id"] == 3
        assert _params(self.db, 3)["col_label"] == "Batching"
        assert response.json()["data"]["group_rows_updated"] == 0
        self.db.commit.assert_called_once()

    def test_create_duplicate_rejected(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row({"item_id": 270197})),
            _res(
                fetchone=_row(
                    {"waste_report_map_id": 1, "group_code": "SWEEP", "group_name": "Spinning Sweep"}
                )
            ),
        ]

        response = client.post(f"{BASE}/create", json=_payload())

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "already mapped" in detail
        assert "Spinning Sweep" in detail
        # Nothing written.
        assert self.db.execute.call_count == 2
        self.db.commit.assert_not_called()
        self.db.rollback.assert_called_once()

    def test_create_item_of_other_company_rejected(self):
        self.db.execute.side_effect = [_res(fetchone=None)]

        response = client.post(f"{BASE}/create", json=_payload())

        assert response.status_code == 400
        assert "Waste item" in response.json()["detail"]
        assert self.db.execute.call_count == 1
        self.db.commit.assert_not_called()

    def test_create_department_of_other_company_rejected(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row({"item_id": 270197})),
            _res(fetchone=None),
        ]

        response = client.post(f"{BASE}/create", json=_payload(dept_id=999))

        assert response.status_code == 400
        assert "Department" in response.json()["detail"]
        assert self.db.execute.call_count == 2
        self.db.commit.assert_not_called()

    def test_create_invalid_day_denom_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(day_denom="LOOMS"))
        assert response.status_code == 400
        assert "Daily" in response.json()["detail"]
        self.db.execute.assert_not_called()
        self.db.rollback.assert_called_once()

    def test_create_invalid_total_denom_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(total_denom=None))
        assert response.status_code == 400
        assert "Monthly" in response.json()["detail"]
        self.db.execute.assert_not_called()

    def test_create_reserved_other_group_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(group_code=" other "))
        assert response.status_code == 400
        assert "reserved" in response.json()["detail"]
        self.db.execute.assert_not_called()

    def test_create_bad_group_code_characters_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(group_code="LINE-WASTE"))
        assert response.status_code == 400
        assert "Group code" in response.json()["detail"]
        self.db.execute.assert_not_called()

    def test_create_too_long_group_code_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(group_code="A" * 41))
        assert response.status_code == 400
        self.db.execute.assert_not_called()

    def test_create_missing_group_code_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(group_code="  "))
        assert response.status_code == 400
        assert "Group code is required" in response.json()["detail"]

    def test_create_blank_group_name_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(group_name="   "))
        assert response.status_code == 400
        assert "Group name" in response.json()["detail"]
        self.db.execute.assert_not_called()

    def test_create_missing_item_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(item_id=None))
        assert response.status_code == 400
        assert "Waste item is required" in response.json()["detail"]
        self.db.execute.assert_not_called()

    def test_create_negative_display_order_rejected(self):
        response = client.post(f"{BASE}/create", json=_payload(display_order=-1))
        assert response.status_code == 400
        assert "Display order" in response.json()["detail"]
        self.db.execute.assert_not_called()

    def test_create_db_error_rolls_back_500(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row({"item_id": 270197})),
            _res(fetchone=None),
            RuntimeError("boom"),
        ]
        response = client.post(f"{BASE}/create", json=_payload())
        assert response.status_code == 500
        self.db.commit.assert_not_called()
        self.db.rollback.assert_called_once()


# =============================================================================
# POST /update
# =============================================================================


def _existing(co_id=1, row_id=5, group_code="SWEEP"):
    return _row(
        {
            "waste_report_map_id": row_id,
            "co_id": co_id,
            "group_code": group_code,
            "item_id": 270197,
            "dept_id": None,
            "active": 1,
        }
    )


class TestUpdate(_Base):
    def test_update_happy_path_propagates_to_group(self):
        self.db.execute.side_effect = [
            _res(fetchone=_existing()),  # row load
            _res(fetchone=_row({"item_id": 270197})),  # item guard
            _res(fetchone=None),  # duplicate guard
            _res(rowcount=1),  # update
            _res(rowcount=3),  # propagate
        ]

        response = client.post(
            f"{BASE}/update",
            json=_payload(waste_report_map_id=5, group_code="SWEEP", group_name="Sweepings (all)"),
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "waste_report_map_id": 5,
            "group_code": "SWEEP",
            "group_rows_updated": 3,
        }
        assert _params(self.db, 0) == {"id": 5}
        # Duplicate guard excludes the row being edited.
        assert _params(self.db, 2)["exclude_id"] == 5
        upd = _params(self.db, 3)
        assert "UPDATE jute_waste_report_map" in _sql(self.db, 3)
        assert upd["id"] == 5
        assert upd["group_name"] == "Sweepings (all)"
        assert upd["updated_by"] == 7
        prop = _params(self.db, 4)
        assert prop["group_code"] == "SWEEP"
        assert prop["group_name"] == "Sweepings (all)"
        assert prop["id"] == 5
        self.db.commit.assert_called_once()

    def test_update_moving_to_another_group_propagates_new_group(self):
        self.db.execute.side_effect = [
            _res(fetchone=_existing(group_code="SWEEP")),
            _res(fetchone=_row({"item_id": 270197})),
            _res(fetchone=None),
            _res(rowcount=1),
            _res(rowcount=0),
        ]

        response = client.post(
            f"{BASE}/update", json=_payload(waste_report_map_id=5, group_code="line_waste")
        )

        assert response.status_code == 200, response.text
        assert _params(self.db, 3)["group_code"] == "LINE_WASTE"
        assert _params(self.db, 4)["group_code"] == "LINE_WASTE"

    def test_update_duplicate_rejected(self):
        self.db.execute.side_effect = [
            _res(fetchone=_existing()),
            _res(fetchone=_row({"item_id": 270196})),
            _res(fetchone=_row({"waste_report_map_id": 9, "group_code": "SOFT", "group_name": "Soft"})),
        ]

        response = client.post(
            f"{BASE}/update", json=_payload(waste_report_map_id=5, item_id=270196)
        )

        assert response.status_code == 400
        assert "already mapped" in response.json()["detail"]
        assert self.db.execute.call_count == 3
        self.db.commit.assert_not_called()
        self.db.rollback.assert_called_once()

    def test_update_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        response = client.post(f"{BASE}/update", json=_payload(waste_report_map_id=999))
        assert response.status_code == 404
        self.db.commit.assert_not_called()

    def test_update_other_company_row_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=_existing(co_id=2))]
        response = client.post(f"{BASE}/update", json=_payload(waste_report_map_id=5))
        assert response.status_code == 404
        assert self.db.execute.call_count == 1
        self.db.commit.assert_not_called()

    def test_update_validation_runs_before_db(self):
        response = client.post(
            f"{BASE}/update", json=_payload(waste_report_map_id=5, day_denom="BAD")
        )
        assert response.status_code == 400
        self.db.execute.assert_not_called()


# =============================================================================
# POST /deactivate
# =============================================================================


class TestDeactivate(_Base):
    def test_deactivate_sets_active_zero(self):
        self.db.execute.side_effect = [_res(fetchone=_existing()), _res(rowcount=1)]

        response = client.post(f"{BASE}/deactivate", json={"co_id": 1, "waste_report_map_id": 5})

        assert response.status_code == 200
        assert response.json()["data"] == {"waste_report_map_id": 5, "active": 0}
        assert "active = 0" in _sql(self.db, 1)
        assert _params(self.db, 1) == {"id": 5, "updated_by": 7}
        self.db.commit.assert_called_once()

    def test_deactivate_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        response = client.post(f"{BASE}/deactivate", json={"co_id": 1, "waste_report_map_id": 5})
        assert response.status_code == 404
        self.db.commit.assert_not_called()
        self.db.rollback.assert_called_once()

    def test_deactivate_other_company_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=_existing(co_id=3))]
        response = client.post(f"{BASE}/deactivate", json={"co_id": 1, "waste_report_map_id": 5})
        assert response.status_code == 404
        assert self.db.execute.call_count == 1


# =============================================================================
# Pure helpers / SQL contract
# =============================================================================


class TestHelpers:
    def test_normalize_group_code_uppercases_and_trims(self):
        assert normalize_group_code("  line_waste1 ") == "LINE_WASTE1"

    def test_denominator_options_cover_every_code(self):
        opts = denominator_options()
        assert [o["code"] for o in opts] == list(WASTE_REPORT_DENOMS)
        assert all(o["label"] == WASTE_REPORT_DENOM_LABELS[o["code"]] for o in opts)

    def test_duplicate_guard_is_null_safe_and_excludes_self(self):
        sql = str(find_duplicate_map_row_query())
        assert "m.dept_id <=> :dept_id" in sql
        assert "m.active = 1" in sql
        assert ":exclude_id IS NULL OR m.waste_report_map_id <> :exclude_id" in sql

    def test_propagation_scoped_to_active_group_rows_excluding_self(self):
        sql = str(propagate_group_fields_query())
        assert "co_id = :co_id" in sql
        assert "group_code = :group_code" in sql
        assert "active = 1" in sql
        assert "waste_report_map_id <> :id" in sql
        # Case-only renames must still propagate under the _ai_ci collation.
        assert "CAST(group_name AS BINARY)" in sql
