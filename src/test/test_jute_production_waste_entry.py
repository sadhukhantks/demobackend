"""Tests for Jute Waste Entry endpoints.

Tests for src/juteProduction/waste_entry.py (prefix /api/juteWasteProd).

Portal persona: dependencies get_tenant_db + get_current_user_with_refresh are
overridden via app.dependency_overrides (same pattern as test_stoppage_entry.py).
The DB session is a MagicMock; rows expose a dict ._mapping for fetchall reads,
and attribute access (.branch_id, .spell_id, .dept_id, ...) for fetchone reads.
There is NO database connectivity in this suite — every read is mocked.

Coverage:
* entry_create_setup: 200-with-data success (spells/departments/machines/
  wastage_areas/waste_items) + missing co_id -> 400
* entry_create: success (200, returns waste_entry_id), rejects waste_qty <= 0
  (400), rejects an over-wide waste_qty (400), rejects an item that is not an
  active type-3 ('jute wastage') item of the company (400), rejects a future
  tran_date (400), rejects a machine or a wastage area that is not in the
  submitted department (400), rejects a body with no wastage_area_id (422),
  and derives branch_id from dept_mst when omitted
* entries_by_date: 200-with-data success (qty float / date string) + missing
  co_id / tran_date -> 400
* entry_edit: success (200), re-validates and updates only editable fields,
  re-checks an area change against the row's existing dept, and refuses to save
  a legacy row that would stay area-less
* entry_delete: soft delete (active = 0 via the UPDATE) + 404 when missing
* Wastage Area master (area_list / area_create / area_edit): list success +
  missing co_id, create success / blank name / duplicate / unknown dept, edit
  success (excluding self from the duplicate guard) + co_id scoping
"""

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import JUTE_WASTAGE_ITEM_TYPE_ID, WASTE_QTY_MAX
from src.main import app

client = TestClient(app)


def _mock_row(mapping: dict):
    """A query row whose ._mapping is a plain dict (matches r._mapping usage)."""
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    """A fetchone() row exposing attributes (e.g. .branch_id, .dept_id)."""
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


_UNSET = object()


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET):
    """Build a MagicMock execute() result with the given return values.

    Uses an _UNSET sentinel so an explicit ``fetchone=None`` (e.g. "row not
    found") is honored rather than leaving fetchone() returning a truthy mock.
    """
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    return res


PAST_DATE = "2026-06-01"
FUTURE_DATE = str(date.today() + timedelta(days=7))


def _create_payload(**overrides):
    """A valid /entry_create body; override single fields per test."""
    payload = {
        "co_id": 1,
        "branch_id": 1,
        "tran_date": PAST_DATE,
        "spell_id": 7,
        "dept_id": 50,
        "wastage_area_id": 40,
        "machine_id": 10,
        "item_id": 3301,
        "waste_qty": 12.5,
        "remarks": "morning sweep",
    }
    payload.update(overrides)
    return payload


class TestWasteEntryEndpoints:
    """Tests for Jute Waste Entry API endpoints."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # ----------------------------------------------------------------- setup
    def test_entry_create_setup_success(self):
        """Setup returns spells / departments / machines / waste_items."""
        spell_row = _mock_row(
            {
                "spell_id": 7,
                "spell_code": "A1",
                "spell_name": "Shift A1",
                "working_hours": 8.0,
            }
        )
        dept_row = _mock_row(
            {"dept_id": 50, "dept_desc": "Spinning", "branch_id": 1}
        )
        machine_row = _mock_row(
            {
                "machine_id": 10,
                "machine_name": "Frame 1",
                "mech_code": "MC1",
                "machine_type_id": 3,
                "machine_type_name": "Spinning",
                "dept_id": 50,
                "dept_name": "Spinning",
                "branch_id": 1,
            }
        )
        area_row = _mock_row(
            {"wastage_area_id": 40, "area_name": "Card Floor", "dept_id": 50, "branch_id": 1}
        )
        item_row = _mock_row(
            {"item_id": 3301, "item_name": "CADDIS", "item_code": "W001"}
        )
        # Order of execute().fetchall(): spells, departments, machines, areas, items
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [spell_row],
            [dept_row],
            [machine_row],
            [area_row],
            [item_row],
        ]

        response = client.get(
            "/api/juteWasteProd/entry_create_setup?co_id=1&branch_id=1"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["spells"][0]["spell_id"] == 7
        assert data["spells"][0]["working_hours"] == 8.0
        assert data["departments"][0]["dept_id"] == 50
        assert data["departments"][0]["dept_desc"] == "Spinning"
        assert data["machines"][0]["machine_id"] == 10
        assert data["machines"][0]["dept_id"] == 50
        # areas carry dept_id so the FE can cascade Department -> Area client-side
        assert data["wastage_areas"] == [
            {
                "wastage_area_id": 40,
                "area_name": "Card Floor",
                "dept_id": 50,
                "branch_id": 1,
            }
        ]
        # waste types are items of item type 3 ('jute wastage'), company-scoped
        assert data["waste_items"] == [
            {"item_id": 3301, "item_name": "CADDIS", "item_code": "W001"}
        ]
        assert "waste_types" not in data
        item_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert item_params["item_type_id"] == JUTE_WASTAGE_ITEM_TYPE_ID
        assert item_params["co_id"] == 1

    def test_entry_create_setup_missing_co_id(self):
        response = client.get("/api/juteWasteProd/entry_create_setup")
        assert response.status_code == 400
        assert "co_id" in response.json().get("detail", "").lower()

    # ------------------------------------------------------------ by_date
    def test_entries_by_date_success(self):
        """entries_by_date returns a list under data with qty/date serialized."""
        row = _mock_row(
            {
                "waste_entry_id": 201,
                "tran_date": PAST_DATE,
                "spell_id": 7,
                "spell_code": "A1",
                "spell_name": "Shift A1",
                "dept_id": 50,
                "dept_name": "Spinning",
                "wastage_area_id": 40,
                "area_name": "Card Floor",
                "machine_id": 10,
                "machine_name": "Frame 1",
                "item_id": 3301,
                "item_name": "CADDIS",
                "item_code": "W001",
                "waste_qty": 12.5,
                "remarks": "morning sweep",
                "branch_id": 1,
            }
        )
        self._mock_session.execute.return_value.fetchall.return_value = [row]

        response = client.get(
            f"/api/juteWasteProd/entries_by_date?co_id=1&branch_id=1&tran_date={PAST_DATE}"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["waste_entry_id"] == 201
        assert data[0]["waste_qty"] == 12.5
        assert data[0]["item_id"] == 3301
        assert data[0]["item_name"] == "CADDIS"
        assert data[0]["dept_name"] == "Spinning"
        assert data[0]["area_name"] == "Card Floor"
        assert data[0]["tran_date"] == PAST_DATE

    def test_entries_by_date_missing_co_id(self):
        response = client.get(
            f"/api/juteWasteProd/entries_by_date?tran_date={PAST_DATE}"
        )
        assert response.status_code == 400
        assert "co_id" in response.json().get("detail", "").lower()

    def test_entries_by_date_missing_tran_date(self):
        response = client.get("/api/juteWasteProd/entries_by_date?co_id=1")
        assert response.status_code == 400
        assert "tran_date" in response.json().get("detail", "").lower()

    # ------------------------------------------------------------ create
    def test_entry_create_success(self):
        """Valid event inserts a row and returns the new waste_entry_id."""
        # execute(): item guard, spell exists, dept branch, area, machine, insert.
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(item_id=3301)),             # item
            _exec_result(fetchone=_attr_row(spell_id=7)),               # spell
            _exec_result(fetchone=_attr_row(dept_id=50, branch_id=1)),  # dept
            _exec_result(fetchone=_attr_row(wastage_area_id=40)),       # area
            _exec_result(fetchone=_attr_row(machine_id=10)),            # machine
            _exec_result(lastrowid=201),                                # insert
        ]

        response = client.post(
            "/api/juteWasteProd/entry_create", json=_create_payload()
        )

        assert response.status_code == 200
        assert response.json()["data"]["waste_entry_id"] == 201
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["dept_id"] == 50
        assert insert_params["wastage_area_id"] == 40
        assert insert_params["item_id"] == 3301
        assert insert_params["waste_qty"] == 12.5
        assert insert_params["updated_by"] == 1
        self._mock_session.commit.assert_called()

    def test_entry_create_derives_branch_when_omitted(self):
        """branch_id None -> derived from dept_mst.branch_id (SPEC §10)."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(item_id=3301)),             # item
            _exec_result(fetchone=_attr_row(spell_id=7)),               # spell
            _exec_result(fetchone=_attr_row(dept_id=50, branch_id=9)),  # dept
            _exec_result(fetchone=_attr_row(wastage_area_id=40)),       # area
            _exec_result(lastrowid=202),                                # insert
        ]
        payload = _create_payload(machine_id=None)
        payload.pop("branch_id")

        response = client.post("/api/juteWasteProd/entry_create", json=payload)

        assert response.status_code == 200
        assert response.json()["data"]["waste_entry_id"] == 202
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["branch_id"] == 9
        assert insert_params["machine_id"] is None

    def test_entry_create_rejects_non_positive_qty(self):
        """waste_qty <= 0 -> 400 (before any DB read)."""
        response = client.post(
            "/api/juteWasteProd/entry_create", json=_create_payload(waste_qty=0)
        )

        assert response.status_code == 400
        assert "waste_qty" in response.json().get("detail", "").lower()
        self._mock_session.execute.assert_not_called()

    def test_entry_create_rejects_over_wide_qty(self):
        """waste_qty > 99999.999 -> 400 rather than a MySQL truncation."""
        response = client.post(
            "/api/juteWasteProd/entry_create",
            json=_create_payload(waste_qty=WASTE_QTY_MAX + 1),
        )

        assert response.status_code == 400
        assert "waste_qty" in response.json().get("detail", "").lower()

    def test_entry_create_rejects_non_wastage_item(self):
        """An item that is not an active type-3 item of the company -> 400.

        The single guard query returns no row for an unknown / inactive /
        wrong-type / other-company item, so nothing is inserted.
        """
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # item guard -> no match
        ]

        response = client.post(
            "/api/juteWasteProd/entry_create",
            json=_create_payload(item_id=999999),
        )

        assert response.status_code == 400
        assert "item_id" in response.json().get("detail", "").lower()
        assert self._mock_session.execute.call_count == 1
        item_params = self._mock_session.execute.call_args_list[0].args[1]
        assert item_params["item_type_id"] == JUTE_WASTAGE_ITEM_TYPE_ID
        assert item_params["co_id"] == 1

    def test_entry_create_rejects_future_tran_date(self):
        """tran_date in the future -> 400 (before any DB read)."""
        response = client.post(
            "/api/juteWasteProd/entry_create",
            json=_create_payload(tran_date=FUTURE_DATE),
        )

        assert response.status_code == 400
        assert "future" in response.json().get("detail", "").lower()
        self._mock_session.execute.assert_not_called()

    def test_entry_create_rejects_unknown_spell(self):
        """spell_id absent from spell_mst -> 400."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(item_id=3301)),  # item
            _exec_result(fetchone=None),                     # spell lookup -> none
        ]

        response = client.post(
            "/api/juteWasteProd/entry_create", json=_create_payload()
        )

        assert response.status_code == 400
        assert "spell_id" in response.json().get("detail", "").lower()

    def test_entry_create_rejects_unknown_dept(self):
        """dept_id absent from dept_mst -> 400."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(item_id=3301)),  # item
            _exec_result(fetchone=_attr_row(spell_id=7)),    # spell
            _exec_result(fetchone=None),                     # dept lookup -> none
        ]

        response = client.post(
            "/api/juteWasteProd/entry_create", json=_create_payload()
        )

        assert response.status_code == 400
        assert "dept_id" in response.json().get("detail", "").lower()

    def test_entry_create_rejects_machine_not_in_dept(self):
        """machine_mst.dept_id != submitted dept_id -> 400 (SPEC §10)."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(item_id=3301)),             # item
            _exec_result(fetchone=_attr_row(spell_id=7)),               # spell
            _exec_result(fetchone=_attr_row(dept_id=50, branch_id=1)),  # dept
            _exec_result(fetchone=_attr_row(wastage_area_id=40)),       # area
            _exec_result(fetchone=None),                                # machine guard
        ]

        response = client.post(
            "/api/juteWasteProd/entry_create", json=_create_payload(machine_id=99)
        )

        assert response.status_code == 400
        assert "machine_id" in response.json().get("detail", "").lower()
        # Nothing was inserted.
        assert self._mock_session.execute.call_count == 5

    def test_entry_create_rejects_area_not_in_dept(self):
        """wastage_area_mst.dept_id != submitted dept_id -> 400, nothing inserted.

        Unknown area, inactive area, another company's area and an area of a
        different department all collapse to this same guard.
        """
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(item_id=3301)),             # item
            _exec_result(fetchone=_attr_row(spell_id=7)),               # spell
            _exec_result(fetchone=_attr_row(dept_id=50, branch_id=1)),  # dept
            _exec_result(fetchone=None),                                # area guard
        ]

        response = client.post(
            "/api/juteWasteProd/entry_create",
            json=_create_payload(wastage_area_id=999999),
        )

        assert response.status_code == 400
        assert "wastage_area_id" in response.json().get("detail", "").lower()
        assert self._mock_session.execute.call_count == 4
        area_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert area_params["dept_id"] == 50
        assert area_params["co_id"] == 1

    def test_entry_create_requires_wastage_area(self):
        """A body with no wastage_area_id is rejected by the schema (422)."""
        payload = _create_payload()
        payload.pop("wastage_area_id")

        response = client.post("/api/juteWasteProd/entry_create", json=payload)

        assert response.status_code == 422
        self._mock_session.execute.assert_not_called()

    # --------------------------------------------------------------- edit
    def test_entry_edit_success(self):
        """Edit re-validates and updates only the editable fields."""
        existing = _attr_row(
            waste_entry_id=201,
            co_id=1,
            branch_id=1,
            tran_date=PAST_DATE,
            spell_id=7,
            dept_id=50,
            wastage_area_id=40,
            machine_id=10,
            item_id=3301,
            waste_qty=12.5,
            remarks="old",
        )
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=existing),                 # active-by-id
            _exec_result(fetchone=_attr_row(item_id=3302)),  # item guard
            _exec_result(),                                  # update
        ]
        payload = {
            "item_id": 3302,
            "waste_qty": 20.25,
            "remarks": "new",
        }

        response = client.put("/api/juteWasteProd/entry_edit/201?co_id=1", json=payload)

        assert response.status_code == 200
        assert response.json()["data"]["waste_entry_id"] == 201
        update_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert update_params["item_id"] == 3302
        assert update_params["waste_qty"] == 20.25
        assert update_params["remarks"] == "new"
        # untouched fields fall back to the existing row
        assert update_params["spell_id"] == 7
        assert update_params["machine_id"] == 10
        assert update_params["wastage_area_id"] == 40

    def test_entry_edit_rejects_non_wastage_item(self):
        """An edit cannot smuggle in an item outside the type-3 / company set."""
        existing = _attr_row(
            waste_entry_id=201,
            spell_id=7,
            dept_id=50,
            wastage_area_id=40,
            machine_id=10,
            item_id=3301,
            waste_qty=12.5,
            remarks="old",
        )
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=existing),  # active-by-id
            _exec_result(fetchone=None),      # item guard -> no match
        ]

        response = client.put(
            "/api/juteWasteProd/entry_edit/201?co_id=1", json={"item_id": 999999}
        )

        assert response.status_code == 400
        assert "item_id" in response.json().get("detail", "").lower()

    def test_entry_edit_rejects_machine_not_in_dept(self):
        """A machine change is re-checked against the row's existing dept_id."""
        existing = _attr_row(
            waste_entry_id=201,
            spell_id=7,
            dept_id=50,
            wastage_area_id=40,
            machine_id=10,
            item_id=3301,
            waste_qty=12.5,
            remarks="old",
        )
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=existing),  # active-by-id
            _exec_result(fetchone=None),      # machine guard -> mismatch
        ]

        response = client.put(
            "/api/juteWasteProd/entry_edit/201?co_id=1", json={"machine_id": 99}
        )

        assert response.status_code == 400
        assert "machine_id" in response.json().get("detail", "").lower()

    def test_entry_edit_rejects_area_not_in_dept(self):
        """An area change is re-checked against the row's EXISTING dept_id.

        dept_id is immutable on edit, so this is the only thing stopping a
        client from attaching an area belonging to another department.
        """
        existing = _attr_row(
            waste_entry_id=201,
            spell_id=7,
            dept_id=50,
            wastage_area_id=40,
            machine_id=10,
            item_id=3301,
            waste_qty=12.5,
            remarks="old",
        )
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=existing),  # active-by-id
            _exec_result(fetchone=None),      # area guard -> mismatch
        ]

        response = client.put(
            "/api/juteWasteProd/entry_edit/201?co_id=1", json={"wastage_area_id": 99}
        )

        assert response.status_code == 400
        assert "wastage_area_id" in response.json().get("detail", "").lower()
        area_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert area_params["dept_id"] == 50  # the row's dept, not a client-supplied one

    def test_entry_edit_rejects_legacy_row_without_area(self):
        """A row booked before the master existed cannot be saved area-less.

        wastage_area_id is NULL on those rows and an omitted field means
        "unchanged", so without this gate an edit would silently keep the row
        without an area even though the field is mandatory.
        """
        existing = _attr_row(
            waste_entry_id=201,
            spell_id=7,
            dept_id=50,
            wastage_area_id=None,
            machine_id=10,
            item_id=3301,
            waste_qty=12.5,
            remarks="old",
        )
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=existing),  # active-by-id
        ]

        response = client.put(
            "/api/juteWasteProd/entry_edit/201?co_id=1", json={"waste_qty": 5.0}
        )

        assert response.status_code == 400
        assert "wastage_area_id" in response.json().get("detail", "").lower()
        # No UPDATE was issued.
        assert self._mock_session.execute.call_count == 1

    def test_entry_edit_not_found_returns_404(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # active-by-id -> none
        ]

        response = client.put(
            "/api/juteWasteProd/entry_edit/999?co_id=1", json={"waste_qty": 1.0}
        )

        assert response.status_code == 404

    # ------------------------------------------------------------- delete
    def test_entry_delete_soft_deletes(self):
        """Delete loads the active row then issues the soft-delete UPDATE."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(waste_entry_id=201)),  # active-by-id
            _exec_result(),                                        # soft delete
        ]

        response = client.delete("/api/juteWasteProd/entry_delete/201?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"]["waste_entry_id"] == 201
        # The soft-delete UPDATE (last execute call) targets this row by id.
        delete_stmt = str(self._mock_session.execute.call_args_list[-1].args[0])
        assert "active = 0" in delete_stmt
        delete_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert delete_params["id"] == 201
        assert delete_params["updated_by"] == 1
        self._mock_session.commit.assert_called()

    def test_entry_delete_not_found_returns_404(self):
        """Deleting a missing/inactive row -> 404."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # active-by-id -> none
        ]

        response = client.delete("/api/juteWasteProd/entry_delete/999?co_id=1")

        assert response.status_code == 404

    # ------------------------------------------------- company scoping guards
    # A tenant DB holds every company's rows, so locating a row by primary key
    # alone would let a user scoped to company A mutate company B's row by
    # guessing an id. These pin the co_id predicate on the by-id path.

    def test_entry_edit_requires_co_id(self):
        """Edit without co_id -> 400, before any row is loaded."""
        response = client.put(
            "/api/juteWasteProd/entry_edit/201", json={"waste_qty": 1.0}
        )

        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()
        self._mock_session.execute.assert_not_called()

    def test_entry_delete_requires_co_id(self):
        """Delete without co_id -> 400, before any row is loaded."""
        response = client.delete("/api/juteWasteProd/entry_delete/201")

        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()
        self._mock_session.execute.assert_not_called()

    def test_entry_edit_scopes_lookup_by_co_id(self):
        """The active-by-id lookup binds co_id, so another company's row 404s."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # no row matches (id, co_id=99)
        ]

        response = client.put(
            "/api/juteWasteProd/entry_edit/201?co_id=99", json={"waste_qty": 1.0}
        )

        assert response.status_code == 404
        lookup_stmt = str(self._mock_session.execute.call_args_list[0].args[0])
        assert "co_id = :co_id" in lookup_stmt
        assert self._mock_session.execute.call_args_list[0].args[1]["co_id"] == 99

    def test_entry_delete_scopes_lookup_by_co_id(self):
        """Same guard on the delete path."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # no row matches (id, co_id=99)
        ]

        response = client.delete("/api/juteWasteProd/entry_delete/201?co_id=99")

        assert response.status_code == 404
        lookup_stmt = str(self._mock_session.execute.call_args_list[0].args[0])
        assert "co_id = :co_id" in lookup_stmt
        assert self._mock_session.execute.call_args_list[0].args[1]["co_id"] == 99

    def test_entry_edit_update_statement_carries_co_id(self):
        """The UPDATE itself is co_id-scoped, not just the preceding SELECT."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(waste_entry_id=201, wastage_area_id=40)),
            _exec_result(),                                        # update
        ]

        response = client.put(
            "/api/juteWasteProd/entry_edit/201?co_id=1", json={"waste_qty": 5.0}
        )

        assert response.status_code == 200
        update_stmt = str(self._mock_session.execute.call_args_list[-1].args[0])
        assert "co_id = :co_id" in update_stmt
        assert self._mock_session.execute.call_args_list[-1].args[1]["co_id"] == 1

    def test_entry_delete_update_statement_carries_co_id(self):
        """The soft-delete UPDATE is co_id-scoped too."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(waste_entry_id=201)),  # active-by-id
            _exec_result(),                                        # soft delete
        ]

        response = client.delete("/api/juteWasteProd/entry_delete/201?co_id=1")

        assert response.status_code == 200
        delete_stmt = str(self._mock_session.execute.call_args_list[-1].args[0])
        assert "co_id = :co_id" in delete_stmt
        assert self._mock_session.execute.call_args_list[-1].args[1]["co_id"] == 1


class TestWastageAreaMasterEndpoints:
    """Tests for the Wastage Area master (same router, prefix /api/juteWasteProd)."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def _area_payload(self, **overrides):
        payload = {
            "co_id": 1,
            "branch_id": 1,
            "dept_id": 50,
            "area_name": "Card Floor",
            "active": 1,
        }
        payload.update(overrides)
        return payload

    # ----------------------------------------------------------------- list
    def test_area_list_success(self):
        row = _mock_row(
            {
                "wastage_area_id": 40,
                "co_id": 1,
                "branch_id": 1,
                "dept_id": 50,
                "area_name": "Card Floor",
                "active": 1,
                "dept_name": "Spinning",
                "branch_name": "Factory",
            }
        )
        self._mock_session.execute.return_value.fetchall.return_value = [row]

        response = client.get("/api/juteWasteProd/area_list?co_id=1&branch_id=1")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data[0]["wastage_area_id"] == 40
        assert data[0]["dept_name"] == "Spinning"

    def test_area_list_missing_co_id(self):
        response = client.get("/api/juteWasteProd/area_list")
        assert response.status_code == 400
        assert "co_id" in response.json().get("detail", "").lower()

    # --------------------------------------------------------------- create
    def test_area_create_success(self):
        # execute(): dept-exists guard, duplicate guard, insert.
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(dept_id=50, branch_id=1)),
            _exec_result(fetchone=None),
            _exec_result(lastrowid=40),
        ]

        response = client.post(
            "/api/juteWasteProd/area_create", json=self._area_payload()
        )

        assert response.status_code == 200
        assert response.json()["data"]["wastage_area_id"] == 40
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["dept_id"] == 50
        assert insert_params["area_name"] == "Card Floor"
        assert insert_params["updated_by"] == 1
        self._mock_session.commit.assert_called()

    def test_area_create_trims_name_and_rejects_blank(self):
        response = client.post(
            "/api/juteWasteProd/area_create", json=self._area_payload(area_name="   ")
        )

        assert response.status_code == 400
        assert "area_name" in response.json().get("detail", "").lower()
        self._mock_session.execute.assert_not_called()

    def test_area_create_rejects_duplicate(self):
        """The UNIQUE key answers as a 400, not a raw MySQL 1062 -> 500."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(dept_id=50, branch_id=1)),  # dept
            _exec_result(fetchone=_attr_row(wastage_area_id=40)),       # duplicate
        ]

        response = client.post(
            "/api/juteWasteProd/area_create", json=self._area_payload()
        )

        assert response.status_code == 400
        assert "already exists" in response.json().get("detail", "").lower()
        # Nothing was inserted.
        assert self._mock_session.execute.call_count == 2

    def test_area_create_rejects_dept_from_another_branch(self):
        """An area filed under branch A with a branch-B department would be
        invisible in the entry picker (which reads by branch) yet still pass the
        dept-keyed write guard — so it is rejected outright."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(dept_id=50, branch_id=2)),  # dept in br 2
        ]

        response = client.post(
            "/api/juteWasteProd/area_create", json=self._area_payload(branch_id=1)
        )

        assert response.status_code == 400
        assert "branch" in response.json().get("detail", "").lower()
        assert self._mock_session.execute.call_count == 1

    def test_area_create_rejects_unknown_dept(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # dept lookup -> none
        ]

        response = client.post(
            "/api/juteWasteProd/area_create", json=self._area_payload(dept_id=999999)
        )

        assert response.status_code == 400
        assert "dept_id" in response.json().get("detail", "").lower()

    # ----------------------------------------------------------------- edit
    def test_area_edit_success(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(wastage_area_id=40, co_id=1)),  # by-id
            _exec_result(fetchone=_attr_row(dept_id=51, branch_id=1)),      # dept
            _exec_result(fetchone=None),                                    # duplicate
            _exec_result(),                                                 # update
        ]

        response = client.put(
            "/api/juteWasteProd/area_edit/40?co_id=1",
            json={"branch_id": 1, "dept_id": 51, "area_name": "Bale Yard", "active": 0},
        )

        assert response.status_code == 200
        update_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert update_params["dept_id"] == 51
        assert update_params["area_name"] == "Bale Yard"
        assert update_params["active"] == 0
        # The duplicate guard excludes the row being edited.
        dup_params = self._mock_session.execute.call_args_list[2].args[1]
        assert dup_params["exclude_id"] == 40

    def test_area_edit_requires_co_id(self):
        response = client.put(
            "/api/juteWasteProd/area_edit/40",
            json={"branch_id": 1, "dept_id": 50, "area_name": "X", "active": 1},
        )

        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()
        self._mock_session.execute.assert_not_called()

    def test_area_edit_scopes_lookup_by_co_id(self):
        """Another company's area reads as 404 rather than being editable."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # no row matches (id, co_id=99)
        ]

        response = client.put(
            "/api/juteWasteProd/area_edit/40?co_id=99",
            json={"branch_id": 1, "dept_id": 50, "area_name": "X", "active": 1},
        )

        assert response.status_code == 404
        lookup_params = self._mock_session.execute.call_args_list[0].args[1]
        assert lookup_params["co_id"] == 99
