"""Winding stock entry router.

Mocked-session tests in the house style: the real app is imported and auth/DB
are replaced through dependency_overrides. execute() is driven with a
side_effect list, one entry per statement the handler runs in order.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    WINDING_STOCK_BUCKET_MILL_USE,
    WINDING_STOCK_SAMPLES_MAX,
    WINDING_STOCK_WT_MAX,
)
from src.main import app

client = TestClient(app)

PAST_DATE = "2026-01-27"
FUTURE_DATE = str(date.today() + timedelta(days=7))
_UNSET = object()


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
    """Every named bind the statement declares must be a key in the params dict
    (call.args[0] = the text() statement, call.args[1] = the params dict)."""
    missing = set(call.args[0].compile().binds) - set(call.args[1])
    assert not missing, missing


def _create_payload(**overrides):
    payload = {
        "co_id": 1,
        "branch_id": 2,
        "tran_date": PAST_DATE,
        "bucket": WINDING_STOCK_BUCKET_MILL_USE,
        "item_id": 335538,
        "package_label": "MACKROLL SPOOL",
        "wastage_area_id": 40,
        "no_of_samples": 954,
        "wt_per_sample": 5.68,
        "remarks": None,
    }
    payload.update(overrides)
    return payload


class TestWindingStockEntry:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # ---- create -----------------------------------------------------------

    def test_create_inserts_and_returns_id(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_attr_row(item_id=335538)),   # item verify
                _exec_result(fetchone=_attr_row(wastage_area_id=40)),  # area verify
                _exec_result(fetchone=None),                        # duplicate probe
                _exec_result(lastrowid=501),                        # insert
            ]
            response = client.post("/api/windingStock/row_create", json=_create_payload())

        assert response.status_code == 200
        assert response.json()["data"]["winding_stock_id"] == 501
        insert_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(insert_call)
        params = insert_call.args[1]
        assert params["bucket"] == WINDING_STOCK_BUCKET_MILL_USE
        assert params["no_of_samples"] == 954
        assert params["wt_per_sample"] == 5.68
        self._mock_session.commit.assert_called()

    def test_create_rejects_future_date(self):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(tran_date=FUTURE_DATE)
        )
        assert response.status_code == 400

    def test_create_rejects_unknown_bucket(self):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(bucket="NOT_A_BUCKET")
        )
        assert response.status_code == 400

    def test_create_rejects_blank_package_label(self):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(package_label="   ")
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("samples", [0, -1, WINDING_STOCK_SAMPLES_MAX + 1])
    def test_create_rejects_out_of_range_samples(self, samples):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(no_of_samples=samples)
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("wt", [0, -1, WINDING_STOCK_WT_MAX + 1])
    def test_create_rejects_out_of_range_weight(self, wt):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(wt_per_sample=wt)
        )
        assert response.status_code == 400

    def test_create_rejects_remarks_too_long(self):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(remarks="x" * 256)
        )
        assert response.status_code == 400

    def test_create_allows_null_item_for_unmastered_lines(self):
        """BEAM ON FLOOR-HESS and the waste lines have no yarn master row."""
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_attr_row(wastage_area_id=40)),  # area only
                _exec_result(fetchone=None),                            # duplicate probe
                _exec_result(lastrowid=502),
            ]
            response = client.post(
                "/api/windingStock/row_create",
                json=_create_payload(item_id=None, package_label="THREAD WASTE"),
            )
        assert response.status_code == 200
        insert_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(insert_call)
        assert insert_call.args[1]["item_id"] is None

    def test_create_rejects_duplicate_line(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_attr_row(item_id=335538)),
                _exec_result(fetchone=_attr_row(wastage_area_id=40)),
                _exec_result(fetchone=_attr_row(winding_stock_id=490)),  # duplicate hit
            ]
            response = client.post("/api/windingStock/row_create", json=_create_payload())
        assert response.status_code == 400
        assert "already" in response.json()["detail"].lower()

    # ---- read -------------------------------------------------------------

    def test_rows_by_date_computes_stock_mt(self):
        """bucket_totals / grand_total_mt are dead payload — the entry page
        computes those client-side (utils/stockCalc.ts) — so rows_by_date no
        longer queries or returns them; only per-row stock_mt does."""
        self._mock_session.execute.side_effect = [
            _exec_result(
                fetchall=[
                    _mock_row({
                        "winding_stock_id": 1, "co_id": 1, "branch_id": 2,
                        "tran_date": date(2026, 1, 27),
                        "bucket": WINDING_STOCK_BUCKET_MILL_USE,
                        "item_id": 335538, "item_code": "X", "item_name": "Y",
                        "quality_name": "SKWP", "yarn_count": 9.5,
                        "package_label": "HESSIAN WEFT", "wastage_area_id": 40,
                        "area_name": "COP GODOWN",
                        "no_of_samples": 185, "wt_per_sample": 14, "remarks": None,
                    })
                ]
            ),
            _exec_result(fetchone=_attr_row(prev_entry_date=date(2026, 1, 26))),
            _exec_result(fetchone=None),  # lock lookup
        ]
        response = client.get(
            "/api/windingStock/rows_by_date?co_id=1&branch_id=2&tran_date=2026-01-27"
        )
        assert response.status_code == 200
        body = response.json()["data"]
        # 185 pieces x 14 kg = 2590 kg = 2.590 MT (the reference sheet's first row)
        assert body["rows"][0]["stock_mt"] == 2.59
        assert "bucket_totals" not in body
        assert "grand_total_mt" not in body
        assert body["prev_entry_date"] == "2026-01-26"
        assert body["locked"] is False

    def test_rows_by_date_requires_branch(self):
        response = client.get("/api/windingStock/rows_by_date?co_id=1&tran_date=2026-01-27")
        assert response.status_code == 400

    def test_wt_default_returns_last_saved_value(self):
        self._mock_session.execute.return_value.fetchone.return_value = _attr_row(
            wt_per_sample=5.68
        )
        response = client.get(
            "/api/windingStock/wt_default"
            "?co_id=1&branch_id=2&item_id=335538&package_label=MACKROLL%20SPOOL"
        )
        assert response.status_code == 200
        assert response.json()["data"]["wt_per_sample"] == 5.68

    def test_wt_default_returns_null_for_unseen_key(self):
        self._mock_session.execute.return_value.fetchone.return_value = None
        response = client.get(
            "/api/windingStock/wt_default?co_id=1&branch_id=2&package_label=NEW%20THING"
        )
        assert response.status_code == 200
        assert response.json()["data"]["wt_per_sample"] is None

    # ---- edit / delete ----------------------------------------------------

    def test_edit_requires_co_id(self):
        response = client.put("/api/windingStock/row_edit/501", json={"no_of_samples": 10})
        assert response.status_code == 400

    def test_edit_404s_for_foreign_company_row(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1", json={"no_of_samples": 10}
            )
        assert response.status_code == 404

    def test_edit_updates_row(self):
        """Happy path: reaches update_winding_stock_row_query, and the locked-day
        gate is fed the EXISTING row's branch_id/tran_date — the update body
        carries neither field, so those values can only come from the row."""
        existing_row = _attr_row(
            winding_stock_id=501, co_id=1, branch_id=2,
            tran_date=date(2026, 1, 27), bucket=WINDING_STOCK_BUCKET_MILL_USE,
            item_id=335538, package_label="MACKROLL SPOOL", wastage_area_id=40,
            stock_loc="FLOOR", dept_id=None,
            no_of_samples=954, wt_per_sample=5.68, remarks=None,
        )
        with patch(
            "src.juteProduction.winding_stock.require_edit_if_locked"
        ) as mock_gate:
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=existing_row),  # existing lookup
                _exec_result(fetchone=None),            # duplicate probe
                _exec_result(rowcount=1),                # UPDATE
            ]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1",
                json={"no_of_samples": 1000, "wt_per_sample": 6.0},
            )
        assert response.status_code == 200
        assert response.json()["data"]["winding_stock_id"] == 501

        update_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(update_call)
        assert update_call.args[1]["no_of_samples"] == 1000
        assert update_call.args[1]["wt_per_sample"] == 6.0
        assert update_call.args[1]["item_id"] == 335538  # carried over unchanged
        assert update_call.args[1]["package_label"] == "MACKROLL SPOOL"
        assert update_call.args[1]["stock_loc"] == "FLOOR"  # legacy row, untouched
        assert update_call.args[1]["dept_id"] is None

        mock_gate.assert_called_once_with(
            self._mock_session, {"user_id": 1}, 1, 2, date(2026, 1, 27)
        )

    def test_delete_soft_deletes(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_attr_row(
                    winding_stock_id=501, co_id=1, branch_id=2,
                    tran_date=date(2026, 1, 27),
                )),
                _exec_result(rowcount=1),
            ]
            response = client.delete("/api/windingStock/row_delete/501?co_id=1")
        assert response.status_code == 200
        delete_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(delete_call)
        stmt = str(delete_call.args[0])
        assert "active = 0" in stmt
        assert "DELETE" not in stmt.upper()

    # ---- copy previous day ------------------------------------------------

    def test_copy_previous_day_refuses_when_target_has_rows(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchall=[_mock_row({"winding_stock_id": 1})]),  # existing rows
            ]
            response = client.post(
                "/api/windingStock/copy_previous_day",
                json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
            )
        assert response.status_code == 400

    def test_copy_previous_day_404s_when_no_earlier_entry(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchall=[]),
                _exec_result(fetchone=_attr_row(prev_entry_date=None)),
            ]
            response = client.post(
                "/api/windingStock/copy_previous_day",
                json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
            )
        assert response.status_code == 404

    def test_copy_previous_day_happy_path(self):
        """Reaches copy_winding_stock_previous_day_query — verifies the **binds
        spread plus from_date/updated_by leaves no bind unfilled."""
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
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
        body = response.json()["data"]
        assert body["copied"] == 5
        assert body["from_date"] == "2026-01-26"

        copy_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(copy_call)
        assert copy_call.args[1]["from_date"] == date(2026, 1, 26)

    # ---- lock -------------------------------------------------------------

    def test_lock_inserts_when_no_header_exists(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # lock probe
            _exec_result(rowcount=1),     # insert
        ]
        response = client.post(
            "/api/windingStock/lock",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 200
        assert response.json()["data"]["locked"] is True
        _assert_binds_complete(self._mock_session.execute.call_args_list[-1])

    def test_lock_updates_existing_header(self):
        """Header already exists (e.g. previously unlocked) -> UPDATE branch,
        reaching update_winding_stock_lock_query rather than the INSERT."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(winding_stock_lock_id=7, is_locked=0)),
            _exec_result(rowcount=1),  # UPDATE
        ]
        response = client.post(
            "/api/windingStock/lock",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 200
        assert response.json()["data"]["locked"] is True

        update_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(update_call)
        assert update_call.args[1]["id"] == 7
        assert update_call.args[1]["co_id"] == 1

    def test_unlock_clears_existing_header(self):
        """require_edit_if_locked's own lock probe finds nothing locked (so
        the gate no-ops with a single query) and unlock's own probe then
        finds the header to clear — one extra side_effect entry versus
        before the permission gate was wired in."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # permission gate's lock probe
            _exec_result(fetchone=_attr_row(winding_stock_lock_id=7, is_locked=1)),
            _exec_result(rowcount=1),
        ]
        response = client.post(
            "/api/windingStock/unlock",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 200
        assert response.json()["data"]["locked"] is False

        clear_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(clear_call)
        assert clear_call.args[1]["id"] == 7
        assert clear_call.args[1]["co_id"] == 1

    def test_unlock_rejects_write_only_user(self):
        """A locked day + an access level below EDIT_LEVEL (4) must 403
        before /unlock does anything — otherwise a Write-only user could
        unlock, edit freely, and re-lock, making the lock decorative."""
        scalar_result = MagicMock()
        scalar_result.scalar.return_value = 3  # Write only
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(winding_stock_lock_id=7, is_locked=1)),
            scalar_result,
        ]
        response = client.post(
            "/api/windingStock/unlock",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 403
