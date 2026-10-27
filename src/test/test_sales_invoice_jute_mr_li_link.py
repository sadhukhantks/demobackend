"""
sales_invoice_dtl.jute_mr_li_id carried through the sales-invoice save paths.

A raw-jute sales line (invoice_type 5) names the jute_mr_li line it drew from;
vw_jute_stock_outstanding subtracts the sold weight from that line. The link is
written by the sls transfer app / the 2026-08-23 backfill and may be sent by a
client as items[i].jute_mr_li_id. The ERP's update endpoint DELETEs and
re-inserts every line, so it must copy the stored link onto the row that
replaces it; create must store a sent value and NULL otherwise.

The column does not exist on every tenant yet (amcl, vownjm on 2026-10-03), so
the INSERT names it only when a line has a link to write, and the stored lines
are read with SELECT * — both paths must keep working where it is absent.

DB is mocked (MagicMock session), like the other sales-invoice tests. The
endpoint tests sniff the SQL text instead of relying on call order.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.sales.query import (
    get_invoice_line_link_rows,
    insert_invoice_line_item,
)
from src.sales.salesInvoice import (
    carry_forward_jute_mr_li_ids,
    match_existing_invoice_lines,
    resolve_requested_jute_mr_li_id,
)

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _stored(pk, item_id, qty, link, so_dtl=None, do_dtl=None):
    return {
        "invoice_line_item_id": pk,
        "item_id": item_id,
        "quantity": qty,
        "sales_weight": None,
        "sales_order_dtl_id": so_dtl,
        "delivery_order_dtl_id": do_dtl,
        "jute_mr_li_id": link,
    }


# =============================================================================
# Query text
# =============================================================================


class TestQueries:

    def test_insert_line_default_is_the_historical_statement(self):
        stmt = insert_invoice_line_item()
        assert "jute_mr_li_id" not in str(stmt)
        assert "jute_mr_li_id" not in stmt._bindparams
        for bind in ("invoice_id", "item_id", "quantity", "sales_weight", "sales_order_dtl_id"):
            assert bind in stmt._bindparams

    def test_insert_line_with_link_names_column_and_bind(self):
        stmt = insert_invoice_line_item(with_jute_mr_li_id=True)
        sql = str(stmt)
        assert "jute_mr_li_id" in sql.split("VALUES")[0]      # column list
        assert ":jute_mr_li_id" in sql.split("VALUES")[1]     # value list
        assert "jute_mr_li_id" in stmt._bindparams
        # the two statements differ only by that column
        assert set(stmt._bindparams) - set(insert_invoice_line_item()._bindparams) == {"jute_mr_li_id"}

    def test_link_rows_query_is_schema_tolerant(self):
        sql = " ".join(str(get_invoice_line_link_rows()).split())
        assert sql.startswith("SELECT * FROM sales_invoice_dtl WHERE invoice_id = :invoice_id")
        assert "ORDER BY invoice_line_item_id" in sql


# =============================================================================
# Pure helpers: payload field + line matching
# =============================================================================


class TestResolveRequestedLink:

    def test_absent_null_or_empty_is_not_explicit(self):
        assert resolve_requested_jute_mr_li_id({}, "f") == (False, None)
        assert resolve_requested_jute_mr_li_id({"jute_mr_li_id": None}, "f") == (False, None)
        assert resolve_requested_jute_mr_li_id({"jute_mr_li_id": ""}, "f") == (False, None)

    def test_value_is_explicit(self):
        assert resolve_requested_jute_mr_li_id({"jute_mr_li_id": 4711}, "f") == (True, 4711)
        assert resolve_requested_jute_mr_li_id({"jute_mr_li_id": "4711"}, "f") == (True, 4711)

    def test_zero_is_explicit_unlink(self):
        assert resolve_requested_jute_mr_li_id({"jute_mr_li_id": 0}, "f") == (True, None)
        assert resolve_requested_jute_mr_li_id({"jute_mr_li_id": "0"}, "f") == (True, None)

    def test_garbage_is_400(self):
        with pytest.raises(HTTPException) as exc:
            resolve_requested_jute_mr_li_id({"jute_mr_li_id": "abc"}, "items[1].jute_mr_li_id")
        assert exc.value.status_code == 400
        assert "items[1].jute_mr_li_id" in exc.value.detail


class TestMatchExistingLines:

    def test_primary_key_wins_and_frontend_temp_ids_are_ignored(self):
        stored = [_stored(11, 5, 100.0, 901), _stored(12, 5, 100.0, 902)]
        # client names row 12 for its first line; second line carries an unsaved FE id
        items = [
            {"item": "5", "quantity": "100", "invoice_line_item_id": "12"},
            {"item": "5", "quantity": "100", "invoice_line_item_id": "inv-line-7"},
        ]
        matched = match_existing_invoice_lines(items, stored)
        assert [m["invoice_line_item_id"] for m in matched] == [12, 11]

    def test_unknown_primary_key_falls_back_to_heuristics(self):
        stored = [_stored(11, 5, 100.0, 901)]
        items = [{"item": "5", "quantity": "100", "invoice_line_item_id": 999}]
        matched = match_existing_invoice_lines(items, stored)
        assert matched[0]["invoice_line_item_id"] == 11

    def test_order_line_reference_matches_before_item(self):
        stored = [_stored(11, 7, 50.0, 901, so_dtl=77), _stored(12, 7, 50.0, 902, so_dtl=78)]
        items = [
            {"item": "7", "quantity": "60", "sales_order_dtl_id": 78},
            {"item": "7", "quantity": "60", "sales_order_dtl_id": 77},
        ]
        matched = match_existing_invoice_lines(items, stored)
        assert [m["invoice_line_item_id"] for m in matched] == [12, 11]

    def test_delivery_order_line_reference(self):
        stored = [_stored(11, 7, 50.0, 901, do_dtl=31), _stored(12, 7, 50.0, 902, do_dtl=32)]
        items = [{"item": "7", "quantity": "1", "delivery_order_dtl_id": 32}]
        assert match_existing_invoice_lines(items, stored)[0]["invoice_line_item_id"] == 12

    def test_exact_quantity_match_is_not_stolen_by_an_edited_line(self):
        # line 1 had its qty edited (100 -> 150); line 2 untouched. A greedy
        # per-line item match would give line 1 the first stored row (qty 200)
        # and leave line 2 the wrong one; the pass order prevents that.
        stored = [_stored(11, 5, 200.0, 901), _stored(12, 5, 100.0, 902)]
        items = [{"item": "5", "quantity": "150"}, {"item": "5", "quantity": "200"}]
        matched = match_existing_invoice_lines(items, stored)
        assert [m["invoice_line_item_id"] for m in matched] == [12, 11]

    def test_item_only_fallback_prefers_same_position(self):
        stored = [_stored(11, 5, 100.0, 901), _stored(12, 5, 200.0, 902)]
        items = [{"item": "5", "quantity": "1"}, {"item": "5", "quantity": "2"}]
        matched = match_existing_invoice_lines(items, stored)
        assert [m["invoice_line_item_id"] for m in matched] == [11, 12]

    def test_dropped_line_stays_unmatched_and_new_line_gets_none(self):
        stored = [_stored(11, 5, 100.0, 901), _stored(12, 6, 10.0, 902)]
        # the client dropped item 6 and added item 9
        items = [{"item": "5", "quantity": "100"}, {"item": "9", "quantity": "3"}]
        matched = match_existing_invoice_lines(items, stored)
        assert matched[0]["invoice_line_item_id"] == 11
        assert matched[1] is None

    def test_each_stored_row_is_used_once(self):
        stored = [_stored(11, 5, 100.0, 901)]
        items = [{"item": "5", "quantity": "100"}, {"item": "5", "quantity": "100"}]
        matched = match_existing_invoice_lines(items, stored)
        assert matched[0]["invoice_line_item_id"] == 11
        assert matched[1] is None

    def test_no_stored_rows(self):
        assert match_existing_invoice_lines([{"item": "5", "quantity": "1"}], []) == [None]

    def test_non_dict_items_do_not_crash(self):
        assert match_existing_invoice_lines(["garbage", None], [_stored(11, 5, 1.0, 901)]) == [None, None]


class TestCarryForward:

    def test_keeps_stored_link_when_client_sends_nothing(self):
        stored = [_stored(11, 5, 100.0, 4711), _stored(12, 6, 10.0, None)]
        items = [{"item": "5", "quantity": "100"}, {"item": "6", "quantity": "10"}]
        assert carry_forward_jute_mr_li_ids(items, stored) == [4711, None]

    def test_explicit_value_wins_over_stored_link(self):
        stored = [_stored(11, 5, 100.0, 4711)]
        items = [{"item": "5", "quantity": "100", "jute_mr_li_id": 5555}]
        assert carry_forward_jute_mr_li_ids(items, stored) == [5555]

    def test_zero_unlinks(self):
        stored = [_stored(11, 5, 100.0, 4711)]
        items = [{"item": "5", "quantity": "100", "jute_mr_li_id": 0}]
        assert carry_forward_jute_mr_li_ids(items, stored) == [None]

    def test_null_in_payload_keeps_stored_link(self):
        stored = [_stored(11, 5, 100.0, 4711)]
        items = [{"item": "5", "quantity": "100", "jute_mr_li_id": None}]
        assert carry_forward_jute_mr_li_ids(items, stored) == [4711]

    def test_new_line_gets_null(self):
        stored = [_stored(11, 5, 100.0, 4711)]
        items = [{"item": "5", "quantity": "100"}, {"item": "9", "quantity": "1"}]
        assert carry_forward_jute_mr_li_ids(items, stored) == [4711, None]

    def test_stored_non_positive_link_normalises_to_null(self):
        stored = [_stored(11, 5, 100.0, 0)]
        assert carry_forward_jute_mr_li_ids([{"item": "5", "quantity": "100"}], stored) == [None]

    def test_tenant_without_the_column(self):
        """SELECT * on a tenant that lacks jute_mr_li_id yields rows without the key."""
        row = _stored(11, 5, 100.0, None)
        del row["jute_mr_li_id"]
        assert carry_forward_jute_mr_li_ids([{"item": "5", "quantity": "100"}], [row]) == [None]


# =============================================================================
# Endpoints (mocked DB, SQL sniffed so the call order is irrelevant)
# =============================================================================


def _line_inserts(calls):
    """(sql, params, stmt) of every INSERT INTO sales_invoice_dtl (not _gst/_jute…)."""
    out = []
    for sql, params, stmt in calls:
        low = " ".join(sql.lower().split())
        if "insert into sales_invoice_dtl (" in low:
            out.append((sql, params, stmt))
    return out


def _names_link_column(sql: str) -> bool:
    return "jute_mr_li_id" in sql


def _is_line_link_select(sql: str) -> bool:
    low = " ".join(sql.lower().split())
    return low.startswith("select * from sales_invoice_dtl where invoice_id = :invoice_id")


def _is_line_delete(sql: str) -> bool:
    low = " ".join(sql.lower().split())
    return low.startswith("delete from sales_invoice_dtl where")


def _assert_all_binds_supplied(inserts):
    """The mocked session cannot notice a missing bind; check it explicitly."""
    assert inserts
    for _, params, stmt in inserts:
        missing = set(stmt._bindparams) - set(params)
        assert not missing, missing


class _EndpointBase:

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        self.calls: list[tuple[str, dict, object]] = []
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def _install(self, stored_rows=None, next_line_id=500):
        """Sniffing db.execute: existence check, stored-line read, inserts."""
        counter = {"line_id": next_line_id}

        def side_effect(stmt, params=None):
            sql = str(stmt)
            self.calls.append((sql, params, stmt))
            low = " ".join(sql.lower().split())
            rv = MagicMock()
            if "select invoice_id, status_id, active from sales_invoice" in low:
                rv.fetchone.return_value = _mock_row({"invoice_id": params["id"], "status_id": 21, "active": 1})
                return rv
            if "select co_id from branch_mst" in low:
                rv.fetchone.return_value = _mock_row({"co_id": 1})
                return rv
            if _is_line_link_select(sql):
                rv.fetchall.return_value = [_mock_row(r) for r in (stored_rows or [])]
                return rv
            if "from item_mst where item_id in" in low:
                rv.fetchall.return_value = []
                return rv
            if low.startswith("insert into sales_invoice (") or low.startswith("insert into sales_invoice\n"):
                rv.lastrowid = 100
                return rv
            if "insert into sales_invoice_dtl (" in low:
                counter["line_id"] += 1
                rv.lastrowid = counter["line_id"]
                return rv
            rv.fetchone.return_value = None
            rv.fetchall.return_value = []
            rv.lastrowid = 1
            return rv

        self._mock_session.execute.side_effect = side_effect


def _raw_jute_line(item="101", qty=1000, **extra):
    line = {
        "item": item,
        "uom": "163",
        "quantity": qty,
        "rate": 50,
        "net_amount": qty * 50,
        "total_amount": qty * 50,
    }
    line.update(extra)
    return line


class TestUpdateCarriesLink(_EndpointBase):

    def _payload(self, items):
        return {
            "id": "100",
            "branch": "1",
            "party": "10",
            "date": "2026-10-03",
            "invoice_type": 5,
            "gross_amount": 50000,
            "items": items,
        }

    def test_update_keeps_stored_link_when_client_does_not_send_it(self):
        self._install(stored_rows=[_stored(11, 101, 1000.0, 4711)])

        response = client.put("/api/salesInvoice/update_sales_invoice",
                              json=self._payload([_raw_jute_line()]))

        assert response.status_code == 200, response.text
        inserts = _line_inserts(self.calls)
        assert len(inserts) == 1
        sql, params, _ = inserts[0]
        assert _names_link_column(sql)
        assert params["jute_mr_li_id"] == 4711
        _assert_all_binds_supplied(inserts)
        self._mock_session.commit.assert_called_once()

    def test_update_reads_stored_lines_before_deleting_them(self):
        self._install(stored_rows=[_stored(11, 101, 1000.0, 4711)])

        response = client.put("/api/salesInvoice/update_sales_invoice",
                              json=self._payload([_raw_jute_line()]))

        assert response.status_code == 200, response.text
        select_idx = [i for i, (sql, _, _) in enumerate(self.calls) if _is_line_link_select(sql)]
        delete_idx = [i for i, (sql, _, _) in enumerate(self.calls) if _is_line_delete(sql)]
        assert len(select_idx) == 1 and len(delete_idx) == 1
        assert select_idx[0] < delete_idx[0]
        assert self.calls[select_idx[0]][1] == {"invoice_id": 100}

    def test_update_with_explicit_new_value_writes_it(self):
        self._install(stored_rows=[_stored(11, 101, 1000.0, 4711)])

        response = client.put("/api/salesInvoice/update_sales_invoice",
                              json=self._payload([_raw_jute_line(jute_mr_li_id=5555)]))

        assert response.status_code == 200, response.text
        sql, params, _ = _line_inserts(self.calls)[0]
        assert _names_link_column(sql)
        assert params["jute_mr_li_id"] == 5555

    def test_update_with_zero_unlinks(self):
        self._install(stored_rows=[_stored(11, 101, 1000.0, 4711)])

        response = client.put("/api/salesInvoice/update_sales_invoice",
                              json=self._payload([_raw_jute_line(jute_mr_li_id=0)]))

        assert response.status_code == 200, response.text
        sql, params, _ = _line_inserts(self.calls)[0]
        assert params["jute_mr_li_id"] is None
        # nothing to write -> the historical statement (NULL by default)
        assert not _names_link_column(sql)

    def test_update_matches_by_line_id_when_sent_else_by_item(self):
        # two stored raw-jute lines with different links; the client resends
        # them in reverse order, the first with its stored id, the second
        # without (matched by item), and appends a brand-new line.
        self._install(stored_rows=[
            _stored(11, 101, 1000.0, 4711),
            _stored(12, 102, 500.0, 4712),
        ])
        items = [
            _raw_jute_line(item="102", qty=500, invoice_line_item_id="12"),
            _raw_jute_line(item="101", qty=1200),
            _raw_jute_line(item="103", qty=10),
        ]

        response = client.put("/api/salesInvoice/update_sales_invoice", json=self._payload(items))

        assert response.status_code == 200, response.text
        inserts = _line_inserts(self.calls)
        assert [p["jute_mr_li_id"] for _, p, _ in inserts] == [4712, 4711, None]
        assert [p["item_id"] for _, p, _ in inserts] == [102, 101, 103]
        assert all(_names_link_column(sql) for sql, _, _ in inserts)
        _assert_all_binds_supplied(inserts)

    def test_update_dropped_line_is_not_recreated(self):
        self._install(stored_rows=[
            _stored(11, 101, 1000.0, 4711),
            _stored(12, 102, 500.0, 4712),
        ])

        response = client.put("/api/salesInvoice/update_sales_invoice",
                              json=self._payload([_raw_jute_line(item="102", qty=500)]))

        assert response.status_code == 200, response.text
        inserts = _line_inserts(self.calls)
        assert len(inserts) == 1
        assert inserts[0][1]["item_id"] == 102
        assert inserts[0][1]["jute_mr_li_id"] == 4712

    def test_update_other_invoice_type_keeps_historical_statement(self):
        """No stored link and none sent: behaviour is exactly as before."""
        self._install(stored_rows=[_stored(11, 300, 10.0, None)])
        payload = self._payload([_raw_jute_line(item="300", qty=10)])
        payload["invoice_type"] = 1

        response = client.put("/api/salesInvoice/update_sales_invoice", json=payload)

        assert response.status_code == 200, response.text
        sql, params, _ = _line_inserts(self.calls)[0]
        assert not _names_link_column(sql)
        assert params["jute_mr_li_id"] is None
        _assert_all_binds_supplied(_line_inserts(self.calls))

    def test_update_on_tenant_without_the_column(self):
        """amcl / vownjm: SELECT * returns rows without jute_mr_li_id and the
        INSERT must not name the column."""
        row = _stored(11, 101, 1000.0, None)
        del row["jute_mr_li_id"]
        self._install(stored_rows=[row])

        response = client.put("/api/salesInvoice/update_sales_invoice",
                              json=self._payload([_raw_jute_line()]))

        assert response.status_code == 200, response.text
        sql, _, _ = _line_inserts(self.calls)[0]
        assert not _names_link_column(sql)

    def test_update_malformed_link_is_400_and_rolls_back(self):
        self._install(stored_rows=[_stored(11, 101, 1000.0, 4711)])

        response = client.put("/api/salesInvoice/update_sales_invoice",
                              json=self._payload([_raw_jute_line(jute_mr_li_id="abc")]))

        assert response.status_code == 400
        assert "jute_mr_li_id" in response.json()["detail"]
        assert _line_inserts(self.calls) == []
        self._mock_session.rollback.assert_called()
        self._mock_session.commit.assert_not_called()


class TestCreateStoresLink(_EndpointBase):

    def _payload(self, items):
        return {
            "branch": "1",
            "party": "10",
            "date": "2026-10-03",
            "invoice_type": 5,
            "gross_amount": 50000,
            "items": items,
        }

    def test_create_stores_sent_value(self):
        self._install()

        response = client.post("/api/salesInvoice/create_sales_invoice",
                               json=self._payload([_raw_jute_line(jute_mr_li_id=4711)]))

        assert response.status_code == 200, response.text
        assert response.json()["invoice_id"] == 100
        inserts = _line_inserts(self.calls)
        assert len(inserts) == 1
        sql, params, _ = inserts[0]
        assert _names_link_column(sql)
        assert params["jute_mr_li_id"] == 4711
        _assert_all_binds_supplied(inserts)

    def test_create_without_link_writes_null_via_historical_statement(self):
        self._install()

        response = client.post("/api/salesInvoice/create_sales_invoice",
                               json=self._payload([_raw_jute_line()]))

        assert response.status_code == 200, response.text
        sql, params, _ = _line_inserts(self.calls)[0]
        assert not _names_link_column(sql)          # safe on tenants without the column
        assert params["jute_mr_li_id"] is None
        _assert_all_binds_supplied(_line_inserts(self.calls))

    def test_create_per_line_values(self):
        self._install()
        items = [_raw_jute_line(item="101", jute_mr_li_id="4711"), _raw_jute_line(item="102"),
                 _raw_jute_line(item="103", jute_mr_li_id=0)]

        response = client.post("/api/salesInvoice/create_sales_invoice", json=self._payload(items))

        assert response.status_code == 200, response.text
        inserts = _line_inserts(self.calls)
        assert [p["jute_mr_li_id"] for _, p, _ in inserts] == [4711, None, None]
        # one line has a link -> every line of the invoice uses the link statement
        assert all(_names_link_column(sql) for sql, _, _ in inserts)
        _assert_all_binds_supplied(inserts)

    def test_create_malformed_link_is_400(self):
        self._install()

        response = client.post("/api/salesInvoice/create_sales_invoice",
                               json=self._payload([_raw_jute_line(jute_mr_li_id="x")]))

        assert response.status_code == 400
        assert "items[1].jute_mr_li_id" in response.json()["detail"]
        assert _line_inserts(self.calls) == []


class TestFreightCreateUntouched(_EndpointBase):
    """Type-7 (Govt Sacking Freight) builds its single line itself and never
    links an MR line: it keeps the historical INSERT."""

    def test_freight_line_insert_is_the_historical_statement(self):
        src_header = {
            "invoice_id": 12345, "invoice_no": 452, "invoice_date": "2025-12-15",
            "invoice_type": 3, "party_id": 77, "branch_id": 5, "co_id": 1,
            "billing_to_id": 77, "shipping_to_id": 77, "transporter_id": 99,
            "transporter_branch_id": 42, "sales_order_id": 169,
            "sales_delivery_order_id": 329, "delivery_order_no": 329,
            "buyer_order_no": "BO/169", "buyer_order_date": "2025-12-10",
            "pcso_no": "BRFCK/25/7890", "pcso_date": "2025-12-15",
            "administrative_office_address": "Patna", "destination_rail_head": "Katihar",
            "loading_point": "Kolkata", "mode_of_transport": "Road",
            "branch_prefix": "KOL", "co_prefix": "VOW",
            "branch_state_code": "19", "shipping_state_code": "10",
            "billing_state_code": "10", "intra_inter_state": 1,
            "vehicle_no": "WB19G5455", "transporter_doc_no": None, "transporter_doc_date": None,
            "transporter_name": None, "transporter_address": None,
            "transporter_state_code": None, "transporter_state_name": None,
        }
        src_line = {"invoice_line_item_id": 5001, "invoice_id": 12345, "item_id": 201,
                    "item_name": "PRINTED TYPE-A JUTE BAG", "quantity": 500}

        def side_effect(stmt, params=None):
            sql = str(stmt)
            self.calls.append((sql, params, stmt))
            low = " ".join(sql.lower().split())
            rv = MagicMock()
            if "from sales_invoice as si" in low and "where si.invoice_id = :invoice_id" in low:
                rv.fetchone.return_value = _mock_row(src_header)
                return rv
            if "from sales_invoice_dtl as ili" in low:
                rv.fetchall.return_value = [_mock_row(src_line)]
                return rv
            if low.startswith("insert into sales_invoice (") or low.startswith("insert into sales_invoice\n"):
                rv.lastrowid = 55555
                return rv
            if "insert into sales_invoice_dtl (" in low:
                rv.lastrowid = 77777
                return rv
            if "insert into sales_invoice_freight (" in low:
                rv.lastrowid = 111111
                return rv
            rv.fetchone.return_value = None
            rv.fetchall.return_value = []
            rv.lastrowid = 1
            return rv

        self._mock_session.execute.side_effect = side_effect

        with patch("src.sales.salesInvoice.find_or_create_freight_item", return_value=(999, 888)):
            response = client.post("/api/salesInvoice/create_sales_invoice", json={
                "invoice_type": 7, "co_id": 1, "date": "2026-01-19",
                "freight": {
                    "source_invoice_ids": [12345], "iw_bill_no": "CTKR2601480",
                    "iw_bill_date": "2026-01-19", "container_no": "CXNU1315111",
                    "vehicle_no": "WB19G5455", "freight_amount": 78879.00, "bales_qty": 48,
                },
                "items": [],
            })

        assert response.status_code == 200, response.text
        inserts = _line_inserts(self.calls)
        assert len(inserts) == 1
        sql, params, _ = inserts[0]
        assert not _names_link_column(sql)
        assert "jute_mr_li_id" not in params
        _assert_all_binds_supplied(inserts)
