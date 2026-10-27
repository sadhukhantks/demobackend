"""Winding stock — location (GODOWN / FLOOR) and department on a counted line.

Phase 4 of the purchased-yarn design (docs/superpowers/specs/
2026-09-25-yarn-purchase-issue-consumption-design.md §3.3, rulings 2 and 4).
Same mocked-session style as test_winding_stock_entry.py: execute() is driven
with a side_effect list, one entry per statement the handler runs in order.

The contract these tests pin:
- a request that omits both fields is a legacy FLOOR line with no department
  (the live sls page sends neither), so nothing already deployed changes;
- stock_loc is case-normalised and must be one of the constants;
- dept_id must be a dept_mst row of the line's branch;
- a GODOWN line never carries a department — it is forced NULL, not rejected;
- (stock_loc, dept_id) are part of the duplicate key, so the same package in
  the godown and on a floor is two legitimate lines.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    WINDING_STOCK_BUCKET_MILL_USE,
    WINDING_STOCK_BUCKET_PURCHASE,
    WINDING_STOCK_LOC_FLOOR,
    WINDING_STOCK_LOC_GODOWN,
    WINDING_STOCK_LOCATIONS,
)
from src.main import app

client = TestClient(app)

PAST_DATE = "2026-01-27"
_UNSET = object()
GATE = "src.juteProduction.winding_stock.require_edit_if_locked"


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET, rowcount=_UNSET):
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    if rowcount is not _UNSET:
        res.rowcount = rowcount
    return res


def _assert_binds_complete(call):
    missing = set(call.args[0].compile().binds) - set(call.args[1])
    assert not missing, missing


def _create_payload(**overrides):
    payload = {
        "co_id": 1,
        "branch_id": 2,
        "tran_date": PAST_DATE,
        "bucket": WINDING_STOCK_BUCKET_PURCHASE,
        "item_id": 335538,
        "package_label": "PURCHASE PW FOR S4",
        "wastage_area_id": 40,
        "no_of_samples": 100,
        "wt_per_sample": 5.0,
        "remarks": None,
    }
    payload.update(overrides)
    return payload


def _existing_row(**overrides):
    attrs = dict(
        winding_stock_id=501, co_id=1, branch_id=2,
        tran_date=date(2026, 1, 27), bucket=WINDING_STOCK_BUCKET_PURCHASE,
        item_id=335538, package_label="PURCHASE PW FOR S4", wastage_area_id=40,
        stock_loc=WINDING_STOCK_LOC_FLOOR, dept_id=None,
        no_of_samples=100, wt_per_sample=5.0, remarks=None,
    )
    attrs.update(overrides)
    return _attr_row(**attrs)


ITEM_OK = lambda: _exec_result(fetchone=_attr_row(item_id=335538))  # noqa: E731
AREA_OK = lambda: _exec_result(fetchone=_attr_row(wastage_area_id=40))  # noqa: E731
DEPT_OK = lambda: _exec_result(fetchone=_attr_row(dept_id=7))  # noqa: E731
NO_DUP = lambda: _exec_result(fetchone=None)  # noqa: E731


class TestWindingStockLocation:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def _last_call(self):
        return self._mock_session.execute.call_args_list[-1]

    # ---- setup ------------------------------------------------------------

    def test_setup_carries_locations_and_branch_departments(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[]),  # yarn items
            _exec_result(fetchall=[]),  # areas
            _exec_result(fetchall=[
                _mock_row({"dept_id": 7, "dept_desc": "WEAVING", "dept_code": "WV"}),
                _mock_row({"dept_id": 8, "dept_desc": "SPINNING", "dept_code": "SP"}),
            ]),
        ]
        response = client.get("/api/windingStock/setup?co_id=1&branch_id=2")
        assert response.status_code == 200
        body = response.json()["data"]
        # every pre-existing key survives
        assert set(body) >= {"buckets", "yarn_items", "areas"}
        assert body["locations"] == list(WINDING_STOCK_LOCATIONS)
        assert body["locations"] == [WINDING_STOCK_LOC_GODOWN, WINDING_STOCK_LOC_FLOOR]
        assert body["departments"] == [
            {"dept_id": 7, "dept_desc": "WEAVING", "dept_code": "WV"},
            {"dept_id": 8, "dept_desc": "SPINNING", "dept_code": "SP"},
        ]
        dept_call = self._last_call()
        _assert_binds_complete(dept_call)
        assert dept_call.args[1] == {"branch_id": 2}

    def test_setup_without_branch_offers_no_departments(self):
        """dept_mst is branch-scoped only: no branch, no list — never every
        branch's departments. And no third query is run."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[]),  # yarn items
            _exec_result(fetchall=[]),  # areas
        ]
        response = client.get("/api/windingStock/setup?co_id=1")
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["departments"] == []
        assert body["locations"] == list(WINDING_STOCK_LOCATIONS)
        assert self._mock_session.execute.call_count == 2

    # ---- create -----------------------------------------------------------

    def test_create_defaults_to_floor_with_null_dept_when_fields_absent(self):
        """The live sls page sends neither field: it must keep working, and
        its rows must land as legacy FLOOR / no-department lines. No dept
        lookup is run (three queries, exactly as before the change)."""
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                ITEM_OK(), AREA_OK(), NO_DUP(), _exec_result(lastrowid=601),
            ]
            response = client.post("/api/windingStock/row_create", json=_create_payload())
        assert response.status_code == 200
        insert_call = self._last_call()
        _assert_binds_complete(insert_call)
        assert insert_call.args[1]["stock_loc"] == WINDING_STOCK_LOC_FLOOR
        assert insert_call.args[1]["dept_id"] is None
        assert self._mock_session.execute.call_count == 4

    def test_create_floor_with_department(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                ITEM_OK(), AREA_OK(), DEPT_OK(), NO_DUP(), _exec_result(lastrowid=602),
            ]
            response = client.post(
                "/api/windingStock/row_create",
                json=_create_payload(stock_loc="floor", dept_id=7),
            )
        assert response.status_code == 200
        calls = self._mock_session.execute.call_args_list
        dept_call = calls[2]
        _assert_binds_complete(dept_call)
        assert dept_call.args[1] == {"dept_id": 7, "branch_id": 2}  # row's branch
        insert = calls[-1].args[1]
        assert insert["stock_loc"] == WINDING_STOCK_LOC_FLOOR  # case-normalised
        assert insert["dept_id"] == 7

    def test_create_rejects_unknown_location(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [ITEM_OK(), AREA_OK()]
            response = client.post(
                "/api/windingStock/row_create", json=_create_payload(stock_loc="SHED")
            )
        assert response.status_code == 400
        assert "stock_loc" in response.json()["detail"]
        self._mock_session.commit.assert_not_called()

    def test_create_rejects_dept_not_of_branch(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                ITEM_OK(), AREA_OK(),
                _exec_result(fetchone=None),  # dept verify misses
            ]
            response = client.post(
                "/api/windingStock/row_create",
                json=_create_payload(stock_loc="FLOOR", dept_id=999),
            )
        assert response.status_code == 400
        assert "dept_id" in response.json()["detail"]
        self._mock_session.commit.assert_not_called()

    def test_create_godown_forces_dept_null_without_error(self):
        """Rule, not error: a dept sent with GODOWN is dropped. The dept is
        still validated (it must exist on the branch) but never stored."""
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                ITEM_OK(), AREA_OK(), DEPT_OK(), NO_DUP(), _exec_result(lastrowid=603),
            ]
            response = client.post(
                "/api/windingStock/row_create",
                json=_create_payload(stock_loc="godown", dept_id=7),
            )
        assert response.status_code == 200
        insert = self._last_call().args[1]
        assert insert["stock_loc"] == WINDING_STOCK_LOC_GODOWN
        assert insert["dept_id"] is None

    def test_duplicate_probe_binds_location_and_department(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                ITEM_OK(), AREA_OK(), DEPT_OK(), NO_DUP(), _exec_result(lastrowid=604),
            ]
            client.post(
                "/api/windingStock/row_create",
                json=_create_payload(stock_loc="FLOOR", dept_id=7),
            )
        probe = self._mock_session.execute.call_args_list[3]
        _assert_binds_complete(probe)
        assert "stock_loc = :stock_loc" in str(probe.args[0])
        assert "dept_id <=> :dept_id" in str(probe.args[0])
        assert probe.args[1]["stock_loc"] == WINDING_STOCK_LOC_FLOOR
        assert probe.args[1]["dept_id"] == 7
        assert probe.args[1]["exclude_id"] is None

    def test_duplicate_probe_for_godown_binds_null_dept(self):
        """The forced-NULL dept must be what the probe compares on, or a
        GODOWN line would collide with / miss the wrong FLOOR line."""
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                ITEM_OK(), AREA_OK(), DEPT_OK(), NO_DUP(), _exec_result(lastrowid=605),
            ]
            client.post(
                "/api/windingStock/row_create",
                json=_create_payload(stock_loc="GODOWN", dept_id=7),
            )
        probe = self._mock_session.execute.call_args_list[3].args[1]
        assert probe["stock_loc"] == WINDING_STOCK_LOC_GODOWN
        assert probe["dept_id"] is None

    def test_create_still_rejects_true_duplicate(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                ITEM_OK(), AREA_OK(), DEPT_OK(),
                _exec_result(fetchone=_attr_row(winding_stock_id=490)),
            ]
            response = client.post(
                "/api/windingStock/row_create",
                json=_create_payload(stock_loc="FLOOR", dept_id=7),
            )
        assert response.status_code == 400
        assert "already" in response.json()["detail"].lower()

    # ---- edit -------------------------------------------------------------

    def test_edit_moves_line_to_godown_and_clears_dept(self):
        """Existing FLOOR/dept 7 line moved to GODOWN with no dept in the body:
        the carried-over dept must still be forced NULL."""
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_existing_row(dept_id=7)),
                NO_DUP(),
                _exec_result(rowcount=1),
            ]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1", json={"stock_loc": "GODOWN"}
            )
        assert response.status_code == 200
        update_call = self._last_call()
        _assert_binds_complete(update_call)
        assert update_call.args[1]["stock_loc"] == WINDING_STOCK_LOC_GODOWN
        assert update_call.args[1]["dept_id"] is None
        probe = self._mock_session.execute.call_args_list[1].args[1]
        assert probe["stock_loc"] == WINDING_STOCK_LOC_GODOWN
        assert probe["dept_id"] is None
        assert probe["exclude_id"] == 501

    def test_edit_sets_department_on_floor_line(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_existing_row()),
                DEPT_OK(),
                NO_DUP(),
                _exec_result(rowcount=1),
            ]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1", json={"dept_id": 7}
            )
        assert response.status_code == 200
        dept_call = self._mock_session.execute.call_args_list[1]
        assert dept_call.args[1] == {"dept_id": 7, "branch_id": 2}  # existing row's branch
        update = self._last_call().args[1]
        assert update["stock_loc"] == WINDING_STOCK_LOC_FLOOR  # carried over
        assert update["dept_id"] == 7

    def test_edit_explicit_null_clears_department(self):
        """The page sends dept_id: null to take a floor line off its
        department — an explicit null clears, only an absent key keeps."""
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_existing_row(dept_id=7)),
                NO_DUP(),
                _exec_result(rowcount=1),
            ]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1",
                json={"stock_loc": "FLOOR", "dept_id": None},
            )
        assert response.status_code == 200
        update = self._last_call().args[1]
        assert update["stock_loc"] == WINDING_STOCK_LOC_FLOOR
        assert update["dept_id"] is None

    def test_edit_absent_dept_keeps_department(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_existing_row(dept_id=7)),
                NO_DUP(),
                _exec_result(rowcount=1),
            ]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1", json={"remarks": "x"}
            )
        assert response.status_code == 200
        assert self._last_call().args[1]["dept_id"] == 7

    def test_edit_rejects_unknown_location(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_existing_row()),
            ]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1", json={"stock_loc": "SHED"}
            )
        assert response.status_code == 400
        self._mock_session.commit.assert_not_called()

    def test_edit_rejects_dept_not_of_branch(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_existing_row()),
                _exec_result(fetchone=None),  # dept verify misses
            ]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1", json={"dept_id": 999}
            )
        assert response.status_code == 400
        self._mock_session.commit.assert_not_called()

    # ---- read -------------------------------------------------------------

    def test_rows_by_date_returns_location_and_department(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[
                _mock_row({
                    "winding_stock_id": 1, "co_id": 1, "branch_id": 2,
                    "tran_date": date(2026, 1, 27),
                    "bucket": WINDING_STOCK_BUCKET_PURCHASE,
                    "item_id": 335538, "item_code": "X", "item_name": "Y",
                    "quality_name": "SKWP", "yarn_count": 9.5,
                    "package_label": "PURCHASE PW FOR S4", "wastage_area_id": 40,
                    "area_name": "COP GODOWN",
                    "stock_loc": WINDING_STOCK_LOC_GODOWN, "dept_id": None,
                    "dept_desc": None,
                    "no_of_samples": 100, "wt_per_sample": 5, "remarks": None,
                }),
                _mock_row({
                    "winding_stock_id": 2, "co_id": 1, "branch_id": 2,
                    "tran_date": date(2026, 1, 27),
                    "bucket": WINDING_STOCK_BUCKET_MILL_USE,
                    "item_id": 335538, "item_code": "X", "item_name": "Y",
                    "quality_name": "SKWP", "yarn_count": 9.5,
                    "package_label": "HESSIAN WEFT", "wastage_area_id": 40,
                    "area_name": "COP GODOWN",
                    "stock_loc": WINDING_STOCK_LOC_FLOOR, "dept_id": 7,
                    "dept_desc": "WEAVING",
                    "no_of_samples": 185, "wt_per_sample": 14, "remarks": None,
                }),
            ]),
            _exec_result(fetchone=_attr_row(prev_entry_date=date(2026, 1, 26))),
            _exec_result(fetchone=None),  # lock lookup
        ]
        response = client.get(
            "/api/windingStock/rows_by_date?co_id=1&branch_id=2&tran_date=2026-01-27"
        )
        assert response.status_code == 200
        rows = response.json()["data"]["rows"]
        assert rows[0]["stock_loc"] == WINDING_STOCK_LOC_GODOWN
        assert rows[0]["dept_id"] is None
        assert rows[0]["dept_desc"] is None
        assert rows[1]["stock_loc"] == WINDING_STOCK_LOC_FLOOR
        assert rows[1]["dept_id"] == 7
        assert rows[1]["dept_desc"] == "WEAVING"
        # the count itself is untouched by the new labels
        assert rows[0]["stock_mt"] == 0.5
        assert rows[1]["stock_mt"] == 2.59

    # ---- copy previous day ------------------------------------------------

    def test_copy_previous_day_carries_location_and_department(self):
        with patch(GATE):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchall=[]),  # target day empty
                _exec_result(fetchone=_attr_row(prev_entry_date=date(2026, 1, 26))),
                _exec_result(rowcount=5),  # copy INSERT
            ]
            response = client.post(
                "/api/windingStock/copy_previous_day",
                json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
            )
        assert response.status_code == 200
        copy_call = self._last_call()
        _assert_binds_complete(copy_call)
        sql = str(copy_call.args[0])
        # both columns are named in the INSERT list AND selected from the
        # source day — a column in one list but not the other would shift
        # every value one place to the right.
        insert_cols = sql.split("INSERT INTO jute_prod_winding_stock")[1].split("SELECT")[0]
        select_cols = sql.split("SELECT")[1].split("FROM")[0]
        assert "stock_loc" in insert_cols and "dept_id" in insert_cols
        assert "stock_loc" in select_cols and "dept_id" in select_cols
        assert insert_cols.count(",") == select_cols.count(",")
