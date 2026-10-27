"""
Tests for FY-aware PO number minting in jute_po_create.

The handler computes the April 1 -> March 31 Indian FY window of
payload.po_date and reads MAX(po_no) in that window per branch_id. Since the
numbering guard (po_numbering.py) that read runs on the dedicated
numbering-lock connection, not on the request session. These tests assert:

- the correct fy_start / fy_end bind values are passed for a given po_date
- the SQL still contains the FY date-range filter (regression guard)
- the returned po_num reflects the FY-scoped sequence

DB calls are mocked, same style as test_jute_po_percentage.py; the session
routes each statement by a fragment of its SQL, the lock connection answers
GET_LOCK / MAX(po_no) / RELEASE_LOCK.
"""
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh


client = TestClient(app)


# =============================================================================
# Shared fixture
# =============================================================================


def _make_mock_session(next_po_value: int = 1, vehicle_weight_qtl: float = 100.0):
    """
    Build a MagicMock session that simulates the DB calls made during
    jute_po_create. Returns (session, added_objects, execute_calls) where
    execute_calls captures every (sql_text, params) pair sent to db.execute
    AND to the numbering-lock connection (where the MAX(po_no) read lives).
    """
    session = MagicMock()
    added_objects = []
    session.add.side_effect = lambda obj: added_objects.append(obj)

    execute_calls: list[tuple[str, dict]] = []

    branch_row = MagicMock()
    branch_row.co_id = 1

    supplier_row = (1,)

    vehicle_row = MagicMock()
    vehicle_row.weight = vehicle_weight_qtl

    prefix_row = MagicMock()
    prefix_row.co_prefix = "CO"
    prefix_row.branch_prefix = "BR"

    def _execute(query, params=None):
        # Capture the SQL text and params for later inspection.
        sql_text = str(getattr(query, "text", query))
        execute_calls.append((sql_text, dict(params) if params else {}))

        result = MagicMock()
        if "FROM branch_mst WHERE branch_id" in sql_text:
            result.fetchone.return_value = branch_row      # _validate_branch_belongs_to_co
        elif "jute_supp_party_map" in sql_text:
            result.fetchone.return_value = supplier_row    # _validate_supplier_party_for_co
        elif "jute_lorry_mst" in sql_text:
            result.fetchone.return_value = vehicle_row     # vehicle weight lookup
        elif "co_prefix" in sql_text:
            result.fetchone.return_value = prefix_row      # prefix lookup for the formatter
        else:
            result.fetchone.return_value = None
        return result

    session.execute.side_effect = _execute

    # Numbering-lock connection: GET_LOCK -> 1, MAX(po_no) -> next_po_value, RELEASE_LOCK -> 1
    lock_conn = MagicMock()

    def _lock_execute(query, params=None):
        sql_text = str(getattr(query, "text", query))
        execute_calls.append((sql_text, dict(params) if params else {}))
        result = MagicMock()
        result.scalar.return_value = next_po_value if "MAX(po_no)" in sql_text else 1
        return result

    lock_conn.execute.side_effect = _lock_execute
    session.get_bind.return_value.engine.connect.return_value = lock_conn

    # flush assigns jute_po_id on the header row (first add()).
    def _flush():
        if added_objects:
            first = added_objects[0]
            if not getattr(first, "jute_po_id", None):
                first.jute_po_id = 12345

    session.flush.side_effect = _flush

    return session, added_objects, execute_calls


def _base_payload(po_date: str = "2026-04-13"):
    return {
        "co_id": 1,
        "branch_id": 2,
        "po_date": po_date,
        "mukam_id": 307,
        "jute_unit": "BALE",
        "supplier_id": 857,
        "party_id": None,
        "vehicle_type_id": 1,
        "vehicle_quantity": 2,
        "channel_code": "DOMESTIC",
        "line_items": [
            # Percentage path: tight by construction, avoids vehicle-weight
            # tolerance complications unrelated to the FY logic under test.
            {"item_id": 269847, "percentage": 100.0, "rate": 10000, "jute_unit": "BALE"},
        ],
    }


def _find_max_po_call(execute_calls: list[tuple[str, dict]]) -> tuple[str, dict]:
    """Locate the FY-scoped MAX(po_no) call among the captured executes."""
    for sql_text, params in execute_calls:
        if "MAX(po_no)" in sql_text and "jute_po" in sql_text:
            return sql_text, params
    raise AssertionError(
        f"MAX(po_no) call not found in executed SQL. Seen SQL: "
        f"{[s[:80] for s, _ in execute_calls]}"
    )


# =============================================================================
# TestJutePOFYBounds: correct fy_start / fy_end for various po_date values
# =============================================================================


@pytest.mark.parametrize(
    "po_date_str, expected_fy_start, expected_fy_end",
    [
        # User's example: 13-Apr-2026 falls in FY 2026-27
        ("2026-04-13", date(2026, 4, 1), date(2027, 3, 31)),
        # Lower boundary of FY 2026-27
        ("2026-04-01", date(2026, 4, 1), date(2027, 3, 31)),
        # Upper boundary of FY 2026-27
        ("2027-03-31", date(2026, 4, 1), date(2027, 3, 31)),
        # Day before FY flip: March 31 2026 -> FY 2025-26
        ("2026-03-31", date(2025, 4, 1), date(2026, 3, 31)),
        # Pre-April date: Jan 2025 -> FY 2024-25
        ("2025-01-20", date(2024, 4, 1), date(2025, 3, 31)),
        # December stays in same FY (FY started previous April)
        ("2025-12-31", date(2025, 4, 1), date(2026, 3, 31)),
    ],
)
class TestJutePOFYBounds:
    """The fy_start / fy_end bind values must match the FY of po_date."""

    def setup_method(self):
        self.session, self.added, self.execute_calls = _make_mock_session(next_po_value=1)
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.session

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_fy_bounds_match_po_date(
        self, po_date_str, expected_fy_start, expected_fy_end
    ):
        payload = _base_payload(po_date=po_date_str)
        response = client.post("/api/jutePO/jute_po_create", json=payload)
        assert response.status_code == 200, response.text

        _, params = _find_max_po_call(self.execute_calls)
        assert params.get("fy_start") == expected_fy_start, (
            f"fy_start mismatch for po_date={po_date_str}: "
            f"expected {expected_fy_start}, got {params.get('fy_start')}"
        )
        assert params.get("fy_end") == expected_fy_end, (
            f"fy_end mismatch for po_date={po_date_str}: "
            f"expected {expected_fy_end}, got {params.get('fy_end')}"
        )
        assert params.get("branch_id") == payload["branch_id"]


# =============================================================================
# TestJutePOFYSQLShape: regression guard for FY filter in SQL
# =============================================================================


class TestJutePOFYSQLShape:
    """The MAX(po_no) SQL must filter by branch_id AND by po_date FY window."""

    def setup_method(self):
        self.session, self.added, self.execute_calls = _make_mock_session(next_po_value=1)
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.session

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_sql_contains_fy_date_filter(self):
        response = client.post(
            "/api/jutePO/jute_po_create", json=_base_payload("2026-04-13")
        )
        assert response.status_code == 200, response.text

        sql_text, _ = _find_max_po_call(self.execute_calls)
        # Normalise whitespace so assertions are formatting-agnostic.
        normalised = " ".join(sql_text.split())

        assert "branch_id = :branch_id" in normalised, (
            "MAX(po_no) query must scope by branch_id"
        )
        assert "po_date >= :fy_start" in normalised, (
            "MAX(po_no) query must include FY lower bound"
        )
        assert "po_date <= :fy_end" in normalised, (
            "MAX(po_no) query must include FY upper bound"
        )


# =============================================================================
# TestJutePONextNoSimulation: end-to-end increment + formatted number
# =============================================================================


class TestJutePONextNoSimulation:
    """Assert the returned po_no / po_num reflect the FY-scoped MAX + FY string."""

    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_next_po_increments_within_fy(self):
        # Simulate: 46 POs already minted in FY 2026-27 for this branch.
        session, added, _ = _make_mock_session(next_po_value=47)
        app.dependency_overrides[get_tenant_db] = lambda: session

        response = client.post(
            "/api/jutePO/jute_po_create", json=_base_payload("2026-04-13")
        )
        assert response.status_code == 200, response.text

        # Header row is the first object added.
        header = added[0]
        assert type(header).__name__ == "JutePo"
        assert header.po_no == 47

        # Formatted po_num should use FY 26-27 and zero-pad the sequence.
        body = response.json()
        assert body["po_num"].endswith("/26-27/00047"), body["po_num"]

    def test_formatted_po_num_uses_po_date_fy_for_march_date(self):
        # March 26, 2026 -> FY 25-26, so the formatter should emit /25-26/.
        session, added, _ = _make_mock_session(next_po_value=1)
        app.dependency_overrides[get_tenant_db] = lambda: session

        response = client.post(
            "/api/jutePO/jute_po_create", json=_base_payload("2026-03-26")
        )
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["po_num"].endswith("/25-26/00001"), body["po_num"]

    def test_first_po_in_new_fy_starts_at_one(self):
        # DB has zero POs in this FY -> COALESCE(MAX(po_no),0)+1 = 1.
        session, added, _ = _make_mock_session(next_po_value=1)
        app.dependency_overrides[get_tenant_db] = lambda: session

        response = client.post(
            "/api/jutePO/jute_po_create", json=_base_payload("2026-04-01")
        )
        assert response.status_code == 200, response.text

        header = added[0]
        assert header.po_no == 1
        body = response.json()
        assert body["po_num"].endswith("/26-27/00001"), body["po_num"]
