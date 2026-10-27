# src/test/test_inward_update_qty.py
"""
Regression tests for PUT /procurementInward/update_inward line reconciliation.

Bug 1: lines were matched ONLY by po_dtl_id, so no-PO inwards (po_dtl_id NULL)
were silently skipped — API returned 200 but challan_qty/inward_qty never
changed. Also the UPDATE never set challan_qty at all.

Bug 2: a line added in the edit screen (no inward_dtl_id, no matching
po_dtl_id) was rejected with 400 "could not be matched to an existing inward
line", and a line removed in the edit screen stayed on the inward.

Covers:
- update matches line by inward_dtl_id when payload carries it (no po_dtl_id)
- update falls back to po_dtl_id when inward_dtl_id absent
- new line (no identifier) -> INSERT, not 400
- omitted line -> soft-deleted (active = 0)
- stale inward_dtl_id -> 400 (client bug, not a silent insert)
- update_proc_inward_dtl SQL sets BOTH challan_qty and inward_qty
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh


client = TestClient(app)


def _override_auth():
    return {"user_id": 1}


def _make_mock_db_override(mock_session):
    def _override():
        yield mock_session
    return _override


class _Row(tuple):
    """Row supporting both positional access and ._mapping."""

    def __new__(cls, inward_dtl_id, po_dtl_id, status_id):
        row = super().__new__(cls, (inward_dtl_id, po_dtl_id, status_id))
        row._mapping = {
            "inward_dtl_id": inward_dtl_id,
            "po_dtl_id": po_dtl_id,
            "status_id": status_id,
        }
        return row


class TestUpdateProcInwardDtlQuery:
    def test_sql_updates_both_qty_columns(self):
        from src.procurement.query import update_proc_inward_dtl
        sql = str(update_proc_inward_dtl())
        assert "challan_qty = :inward_qty" in sql
        assert "inward_qty = :inward_qty" in sql
        assert "WHERE inward_dtl_id = :inward_dtl_id" in sql


class TestUpdateInwardEndpoint:

    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    @staticmethod
    def _payload(items):
        return {
            "id": "106",
            "branch": "12",
            "supplier": "100000077",
            "inward_date": "2026-07-14",
            "challan_no": "122",
            "challan_date": "2026-07-14",
            "items": items,
        }

    def _run(self, items, existing_rows):
        """Drive update_inward with mocked execute sequence and return response.

        Execute order: inspection SELECT, cancelled-count SELECT, header UPDATE,
        existing-lines SELECT, then one call per payload line, then (if any line
        was dropped) the soft-delete UPDATE.
        """
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(0,))),               # inspection_check falsy
            MagicMock(fetchone=MagicMock(return_value=(1,))),               # 1 active line
            MagicMock(),                                                    # header UPDATE
            MagicMock(fetchall=MagicMock(return_value=existing_rows)),      # existing lines
        ] + [MagicMock() for _ in range(len(items) + 1)]                    # line writes + delete
        return client.put("/api/procurementInward/update_inward", json=self._payload(items))

    def _line_calls(self):
        """Execute calls after the existing-lines SELECT."""
        return self.mock_session.execute.call_args_list[4:]

    def test_update_matches_by_inward_dtl_id_without_po(self):
        """No-PO inward: payload has inward_dtl_id, no po_dtl_id — line must update."""
        response = self._run(
            [{"inward_dtl_id": "273", "item": "269919", "quantity": 90, "uom": "143"}],
            existing_rows=[_Row(273, None, 21)],
        )
        assert response.status_code == 200, response.text

        sql, params = self._line_calls()[0].args
        assert "UPDATE proc_inward_dtl" in str(sql)
        assert params["inward_dtl_id"] == 273
        assert params["inward_qty"] == 90.0
        self.mock_session.commit.assert_called_once()

    def test_update_falls_back_to_po_dtl_id(self):
        response = self._run(
            [{"po_dtl_id": "55", "item": "269919", "quantity": 42, "uom": "143"}],
            existing_rows=[_Row(273, 55, 21)],
        )
        assert response.status_code == 200, response.text
        sql, params = self._line_calls()[0].args
        assert "UPDATE proc_inward_dtl" in str(sql)
        assert params["inward_dtl_id"] == 273
        assert params["inward_qty"] == 42.0

    def test_new_line_without_identifier_is_inserted(self):
        """Line added in the edit screen must INSERT, not 400."""
        response = self._run(
            [
                {"inward_dtl_id": "273", "item": "269919", "quantity": 90, "uom": "143"},
                {"item": "335644", "quantity": 6, "uom": "143", "hsn_code": "8448"},
            ],
            existing_rows=[_Row(273, None, 21)],
        )
        assert response.status_code == 200, response.text

        sql, params = self._line_calls()[1].args
        assert "INSERT INTO proc_inward_dtl" in str(sql)
        assert params["inward_id"] == 106
        assert params["item_id"] == 335644
        assert params["inward_qty"] == 6.0
        assert params["challan_qty"] == 6.0
        assert params["po_dtl_id"] is None
        assert params["active"] == 1
        assert params["status_id"] == 21  # inherited from the existing line
        self.mock_session.commit.assert_called_once()

    def test_omitted_line_is_soft_deleted(self):
        response = self._run(
            [{"inward_dtl_id": "273", "item": "269919", "quantity": 90, "uom": "143"}],
            existing_rows=[_Row(273, None, 21), _Row(274, None, 21)],
        )
        assert response.status_code == 200, response.text

        sql, params = self._line_calls()[-1].args
        assert "active = 0" in str(sql)
        assert params["removed_ids"] == [274]
        assert params["inward_id"] == 106

    def test_no_soft_delete_when_nothing_removed(self):
        response = self._run(
            [{"inward_dtl_id": "273", "item": "269919", "quantity": 90, "uom": "143"}],
            existing_rows=[_Row(273, None, 21)],
        )
        assert response.status_code == 200, response.text
        assert all("active = 0" not in str(c.args[0]) for c in self._line_calls())

    def test_update_stale_inward_dtl_id_is_400(self):
        response = self._run(
            [{"inward_dtl_id": "999", "item": "269919", "quantity": 90, "uom": "143"}],
            existing_rows=[_Row(273, None, 21)],
        )
        assert response.status_code == 400
        assert "matched" in response.json()["detail"].lower()
        self.mock_session.commit.assert_not_called()
        self.mock_session.rollback.assert_called_once()

    def test_empty_items_is_400(self):
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(0,))),
            MagicMock(fetchone=MagicMock(return_value=(1,))),
            MagicMock(),
        ]
        response = client.put(
            "/api/procurementInward/update_inward", json=self._payload([])
        )
        assert response.status_code == 400
        self.mock_session.commit.assert_not_called()
