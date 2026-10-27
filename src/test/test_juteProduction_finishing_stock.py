"""Finishing stock entry router (/api/finishingStock).

Mocked-session tests in the house style of test_winding_stock_entry.py: the real
app is imported and auth/DB are replaced through dependency_overrides. execute()
is driven with a side_effect list, one entry per statement the handler runs, in
order.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    FINISHING_STOCK_QTY_MAX,
    FINISHING_STOCK_WT_MAX,
)
from src.juteProduction.finishing_stock import (
    BACKDATE_MSG,
    OWN_BALE_DERIVED_MSG,
    unit_weights_for,
)
from src.main import app

client = TestClient(app)

PAST_DATE = str(date.today() - timedelta(days=10))  # inside the 60-day backdate window (spec §7d)
FUTURE_DATE = str(date.today() + timedelta(days=7))
OWN_CO_ID = 1
OTHER_CO_ID = 9
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


def _quality_row(**overrides):
    attrs = {
        "finishing_quality_id": 1,
        "co_id": OWN_CO_ID,
        "quality_type": 1,
        "item_id": 269959,
        "packsheet_wt": None,
        "std_bale_weight": 437.0,
        "no_of_bags": None,
    }
    attrs.update(overrides)
    return _attr_row(**attrs)


def _bale_payload(**overrides):
    payload = {
        "co_id": OWN_CO_ID,
        "branch_id": 2,
        "tran_date": PAST_DATE,
        "bucket": "BALE",
        "finishing_quality_id": 1,
        "stage": None,
        "work_by": 0,
        "owner_co_id": None,
        "qty": 70,
        "qty_uom": "BALE",
        "wt_per_unit_kg": None,
        "wt_kg": None,
        "remarks": None,
    }
    payload.update(overrides)
    return payload


def _loose_payload(**overrides):
    payload = {
        "co_id": OWN_CO_ID,
        "branch_id": 2,
        "tran_date": PAST_DATE,
        "bucket": "LOOSE",
        "finishing_quality_id": 1,
        "stage": "HEMMING",
        "work_by": 0,
        "owner_co_id": None,
        "qty": 130,
        "qty_uom": "CUTS",
        "wt_per_unit_kg": None,
        "wt_kg": 1885.0,
        "remarks": None,
    }
    payload.update(overrides)
    return payload


class TestFinishingStockEntry:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # ---- setup ------------------------------------------------------------

    def _setup_side_effect(self, qualities, cut_weights=(), pcs_per_bundle=(),
                           bale_source_found=False):
        """The five statements /setup runs, in order: qualities, companies,
        cut weights (per item), pcs_per_bundle (per finishing quality), and the
        bale-source probe (a row = the branch has bale production, D7)."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row(q) for q in qualities]),
            _exec_result(fetchall=[
                _mock_row({"co_id": 1, "co_name": "The Empire Jute Company Limited"})
            ]),
            _exec_result(fetchall=[_mock_row(r) for r in cut_weights]),
            _exec_result(fetchall=[_mock_row(r) for r in pcs_per_bundle]),
            _exec_result(fetchone=_attr_row(found=1) if bale_source_found else None),
        ]

    def test_setup_returns_qualities_stages_companies(self):
        self._setup_side_effect([{
            "finishing_quality_id": 1, "item_id": 269959, "item_code": "531010",
            "item_name": "HESSIAN CLOTH 40-6.5", "quality_type": 1,
            "std_bale_weight": 400.0, "packsheet_wt": 2.0, "no_of_bags": None,
        }])
        response = client.get("/api/finishingStock/setup?co_id=1&branch_id=2")
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["qualities"][0]["std_bale_weight"] == 400.0
        assert body["companies"][0]["co_name"] == "The Empire Jute Company Limited"
        assert {"code": "HEMMING", "label": "Hemming"} in body["stages"]
        assert body["loose_qty_uoms"] == ["CUTS", "BDL", "PCS"]
        assert body["bale_source"] == "MANUAL"
        _assert_binds_complete(self._mock_session.execute.call_args_list[0])
        _assert_binds_complete(self._mock_session.execute.call_args_list[4])

    def test_setup_bale_source_bale_prod_when_branch_has_bales(self):
        """D7: any active bale entry dated <= today flips the form into derived
        mode — own-company BALE lines are then keyed in Bale Production."""
        self._setup_side_effect([], bale_source_found=True)
        response = client.get("/api/finishingStock/setup?co_id=1&branch_id=2")
        assert response.status_code == 200
        assert response.json()["data"]["bale_source"] == "BALE_PROD"
        probe = self._mock_session.execute.call_args_list[4]
        assert probe.args[1] == {"co_id": 1, "branch_id": 2, "as_on": date.today()}

    def test_setup_requires_co_id(self):
        assert client.get("/api/finishingStock/setup").status_code == 400

    # ---- setup: derived unit weights (spec §6) ----------------------------

    def test_setup_derives_unit_weights(self):
        """bag = 290/500 = 0.58; bundle = 25 x 0.58 = 14.5; cut from the
        weaving-quality map."""
        self._setup_side_effect(
            [{
                "finishing_quality_id": 2, "item_id": 268938, "item_code": "532010",
                "item_name": "SACKING BAG", "quality_type": 2,
                "std_bale_weight": 290.0, "packsheet_wt": None, "no_of_bags": 500,
            }],
            cut_weights=[{"item_id": 268938, "cut_wt_kg": 49.5}],
            pcs_per_bundle=[{"finishing_quality_id": 2, "pcs_per_bundle": 25.0}],
        )
        response = client.get("/api/finishingStock/setup?co_id=1&branch_id=2")
        assert response.status_code == 200
        q = response.json()["data"]["qualities"][0]
        assert q["bag_wt_kg"] == 0.58
        assert q["bundle_wt_kg"] == 14.5
        assert q["cut_wt_kg"] == 49.5

    def test_setup_unit_weights_none_when_unresolvable(self):
        """No no_of_bags, no weaving quality, no spec row -> all three null, so
        the form leaves Weight kg manual instead of inventing one."""
        self._setup_side_effect([{
            "finishing_quality_id": 3, "item_id": 269959, "item_code": "531010",
            "item_name": "HESSIAN CLOTH 40-6.5", "quality_type": 1,
            "std_bale_weight": 400.0, "packsheet_wt": None, "no_of_bags": None,
        }])
        response = client.get("/api/finishingStock/setup?co_id=1&branch_id=2")
        assert response.status_code == 200
        q = response.json()["data"]["qualities"][0]
        assert q["cut_wt_kg"] is None
        assert q["bag_wt_kg"] is None
        assert q["bundle_wt_kg"] is None

    def test_setup_unit_weight_statements_bind_completely(self):
        self._setup_side_effect([])
        assert client.get("/api/finishingStock/setup?co_id=1").status_code == 200
        calls = self._mock_session.execute.call_args_list
        _assert_binds_complete(calls[2])   # cut weights
        _assert_binds_complete(calls[3])   # pcs_per_bundle
        assert calls[3].args[1]["process"] == "sacksewing"
        assert calls[3].args[1]["param"] == "pcs_per_bundle"
        assert calls[3].args[1]["value_role"] == "standard"
        assert calls[3].args[1]["id_type"] == "qid"


    # ---- create -----------------------------------------------------------

    def test_create_bale_defaults_per_bale_weight_from_quality(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row(std_bale_weight=437.0)),
            _exec_result(fetchone=None),  # bale-source probe: MANUAL
            _exec_result(lastrowid=11),
        ]
        response = client.post("/api/finishingStock/row_create", json=_bale_payload())
        assert response.status_code == 200
        assert response.json()["data"]["finishing_stock_id"] == 11
        insert_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(insert_call)
        assert insert_call.args[1]["wt_per_unit_kg"] == 437.0
        assert insert_call.args[1]["wt_kg"] is None
        assert insert_call.args[1]["stage"] is None
        self._mock_session.commit.assert_called()

    def test_create_bale_explicit_weight_wins(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row(std_bale_weight=400.0)),
            _exec_result(fetchone=None),  # bale-source probe: MANUAL
            _exec_result(lastrowid=12),
        ]
        response = client.post(
            "/api/finishingStock/row_create",
            json=_bale_payload(wt_per_unit_kg=437.384),
        )
        assert response.status_code == 200
        assert (
            self._mock_session.execute.call_args_list[-1].args[1]["wt_per_unit_kg"]
            == 437.384
        )

    def test_create_bale_without_std_weight_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row(std_bale_weight=None)),
        ]
        response = client.post("/api/finishingStock/row_create", json=_bale_payload())
        assert response.status_code == 400
        assert "per-bale weight" in response.json()["detail"].lower()

    def test_create_loose_stores_weighed_kg(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row(quality_type=2)),
            _exec_result(lastrowid=13),
        ]
        response = client.post("/api/finishingStock/row_create", json=_loose_payload())
        assert response.status_code == 200
        params = self._mock_session.execute.call_args_list[-1].args[1]
        assert params["wt_kg"] == 1885.0
        assert params["wt_per_unit_kg"] is None
        assert params["stage"] == "HEMMING"
        assert params["qty_uom"] == "CUTS"

    def test_create_loose_requires_wt_kg(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
        ]
        response = client.post(
            "/api/finishingStock/row_create", json=_loose_payload(wt_kg=None)
        )
        assert response.status_code == 400

    def test_create_bale_rejects_stage(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
        ]
        response = client.post(
            "/api/finishingStock/row_create", json=_bale_payload(stage="HEMMING")
        )
        assert response.status_code == 400

    def test_create_loose_rejects_owner(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=_attr_row(co_id=OTHER_CO_ID)),
        ]
        response = client.post(
            "/api/finishingStock/row_create",
            json=_loose_payload(owner_co_id=OTHER_CO_ID),
        )
        assert response.status_code == 400

    def test_create_rejects_future_date(self):
        response = client.post(
            "/api/finishingStock/row_create", json=_bale_payload(tran_date=FUTURE_DATE)
        )
        assert response.status_code == 400

    def test_create_rejects_older_than_backdate_window(self):
        """Spec §7d: entries may be backdated 60 days, not more — refused before any SQL."""
        too_old = str(date.today() - timedelta(days=61))
        response = client.post(
            "/api/finishingStock/row_create", json=_loose_payload(tran_date=too_old)
        )
        assert response.status_code == 400
        assert response.json()["detail"] == BACKDATE_MSG
        self._mock_session.execute.assert_not_called()

    def test_create_accepts_sixty_days_back(self):
        edge = str(date.today() - timedelta(days=60))
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row(quality_type=2)),
            _exec_result(lastrowid=99),
        ]
        response = client.post(
            "/api/finishingStock/row_create", json=_loose_payload(tran_date=edge)
        )
        assert response.status_code == 200

    def test_create_rejects_unknown_quality(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post("/api/finishingStock/row_create", json=_bale_payload())
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "overrides",
        [
            {"bucket": "NOT_A_BUCKET"},
            {"bucket": "LOOSE", "stage": "NOT_A_STAGE", "wt_kg": 10, "qty_uom": "CUTS"},
            {"bucket": "LOOSE", "stage": None, "wt_kg": 10, "qty_uom": "TONS"},
            {"qty_uom": "CUTS"},  # BALE line with a loose uom
            {"qty": 0},  # BALE needs qty > 0
            {"qty": FINISHING_STOCK_QTY_MAX + 1},
            {"wt_per_unit_kg": FINISHING_STOCK_WT_MAX + 1},
            {"wt_kg": 10},  # BALE must not carry a weighed total
            {"work_by": 1},  # BALE is never contract work
            {"remarks": "x" * 256},
        ],
    )
    def test_create_rejects_bad_bucket_stage_uom(self, overrides):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=_attr_row(co_id=OTHER_CO_ID)),
        ]
        payload = _bale_payload(**overrides)
        response = client.post("/api/finishingStock/row_create", json=payload)
        assert response.status_code == 400

    def test_create_owner_equal_to_co_normalised_to_null(self):
        """owner_co_id == co_id means "ours" — stored as NULL so own bales are
        one value, never two. No co_mst lookup is spent on it either."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=None),  # bale-source probe: MANUAL
            _exec_result(lastrowid=14),
        ]
        response = client.post(
            "/api/finishingStock/row_create", json=_bale_payload(owner_co_id=OWN_CO_ID)
        )
        assert response.status_code == 200
        assert self._mock_session.execute.call_count == 3  # no owner verify
        assert self._mock_session.execute.call_args_list[-1].args[1]["owner_co_id"] is None

    def test_create_unknown_owner_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=None),  # owner not in co_mst
        ]
        response = client.post(
            "/api/finishingStock/row_create", json=_bale_payload(owner_co_id=4242)
        )
        assert response.status_code == 400

    def test_create_bale_for_other_owner(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=_attr_row(co_id=OTHER_CO_ID)),
            _exec_result(fetchone=None),  # bale-source probe: MANUAL
            _exec_result(lastrowid=15),
        ]
        response = client.post(
            "/api/finishingStock/row_create",
            json=_bale_payload(owner_co_id=OTHER_CO_ID),
        )
        assert response.status_code == 200
        assert (
            self._mock_session.execute.call_args_list[-1].args[1]["owner_co_id"]
            == OTHER_CO_ID
        )
        # every BALE line spends the bale-source probe (owner ruling 2026-10-12)
        assert self._mock_session.execute.call_count == 4

    # ---- D7: own-company bales derived from Bale Production ---------------

    def test_create_own_bale_refused_when_derived(self):
        """Once the branch has bale entries dated <= tran_date, an own-company
        BALE line is refused: those bales are counted from Bale Production."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=_attr_row(found=1)),  # bale-source probe: BALE_PROD
        ]
        response = client.post("/api/finishingStock/row_create", json=_bale_payload())
        assert response.status_code == 400
        assert response.json()["detail"] == OWN_BALE_DERIVED_MSG
        probe = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(probe)
        assert probe.args[1] == {
            "co_id": OWN_CO_ID, "branch_id": 2, "as_on": date.fromisoformat(PAST_DATE),
        }
        self._mock_session.commit.assert_not_called()

    def test_create_other_owner_bale_refused_when_derived(self):
        """Owner ruling 2026-10-12: in derived mode the page takes loose stock
        only — a BALE line for ANOTHER company is refused too."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=_attr_row(co_id=OTHER_CO_ID)),
            _exec_result(fetchone=_attr_row(found=1)),  # bale-source probe: BALE_PROD
        ]
        response = client.post(
            "/api/finishingStock/row_create",
            json=_bale_payload(owner_co_id=OTHER_CO_ID),
        )
        assert response.status_code == 400
        assert response.json()["detail"] == OWN_BALE_DERIVED_MSG
        self._mock_session.commit.assert_not_called()

    def test_create_loose_never_probes_bale_source(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_quality_row(quality_type=2)),
            _exec_result(lastrowid=17),
        ]
        response = client.post("/api/finishingStock/row_create", json=_loose_payload())
        assert response.status_code == 200
        assert self._mock_session.execute.call_count == 2

    # ---- read -------------------------------------------------------------

    def test_rows_by_date_shape_and_total_kg(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(co_name="The Empire Jute Company Limited",
                                            branch_name="Factory")),
            _exec_result(fetchall=[
                _mock_row({
                    "finishing_stock_id": 1, "co_id": 1, "branch_id": 2,
                    "tran_date": date(2026, 9, 16), "bucket": "BALE",
                    "finishing_quality_id": 1, "item_id": 269959,
                    "item_code": "531010", "item_name": "HESSIAN CLOTH 40-6.5",
                    "quality_type": 1, "stage": None, "work_by": 0,
                    "owner_co_id": None, "owner_co_name": None,
                    "qty": 70, "qty_uom": "BALE", "wt_per_unit_kg": 437.384,
                    "wt_kg": None, "remarks": None,
                }),
                _mock_row({
                    "finishing_stock_id": 2, "co_id": 1, "branch_id": 2,
                    "tran_date": date(2026, 9, 16), "bucket": "LOOSE",
                    "finishing_quality_id": 1, "item_id": 269959,
                    "item_code": "531010", "item_name": "HESSIAN CLOTH 40-6.5",
                    "quality_type": 1, "stage": "HEMMING", "work_by": 1,
                    "owner_co_id": None, "owner_co_name": None,
                    "qty": 130, "qty_uom": "CUTS", "wt_per_unit_kg": None,
                    "wt_kg": 1885.0, "remarks": None,
                }),
            ]),
            _exec_result(fetchone=_attr_row(prev_entry_date=date(2026, 9, 15))),
            _exec_result(fetchone=None),  # bale-source probe: MANUAL
        ]
        response = client.get(
            "/api/finishingStock/rows_by_date?co_id=1&branch_id=2&tran_date=2026-09-16"
        )
        assert response.status_code == 200
        body = response.json()["data"]
        bale, loose = body["rows"]
        assert bale["total_kg"] == round(70 * 437.384, 3)
        assert bale["owner_co_name"] == "The Empire Jute Company Limited"
        assert bale["stage_label"] is None
        assert loose["total_kg"] == 1885.0
        assert loose["stage_label"] == "Hemming"
        assert body["prev_entry_date"] == "2026-09-15"
        assert bale["tran_date"] == "2026-09-16"
        # MANUAL mode: nothing derived, nothing superseded
        assert body["bale_source"] == "MANUAL"
        assert body["derived_bales"] == []
        assert body["derived_series"] == []
        assert bale["superseded"] is False
        assert loose["superseded"] is False
        # no summary and no series statement spent
        assert self._mock_session.execute.call_count == 4

    def _day_row(self, **over):
        m = {
            "finishing_stock_id": 1, "co_id": 1, "branch_id": 2,
            "tran_date": date(2026, 9, 16), "bucket": "BALE",
            "finishing_quality_id": 1, "item_id": 269959,
            "item_code": "531010", "item_name": "HESSIAN CLOTH 40-6.5",
            "quality_type": 1, "stage": None, "work_by": 0,
            "owner_co_id": None, "owner_co_name": None,
            "qty": 70, "qty_uom": "BALE", "wt_per_unit_kg": 437.384,
            "wt_kg": None, "remarks": None,
        }
        m.update(over)
        return _mock_row(m)

    def test_rows_by_date_bale_prod_derives_and_flags_superseded(self):
        """D7 in BALE_PROD mode: derived_bales carries the Bale Production
        summary lines for the date; manual own-company BALE rows (owner NULL or
        == co_id) are superseded, other owners' bales and loose rows are not."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(co_name="Empire", branch_name="Factory")),
            _exec_result(fetchall=[
                self._day_row(finishing_stock_id=1),
                self._day_row(finishing_stock_id=2, owner_co_id=OWN_CO_ID),
                self._day_row(finishing_stock_id=3, owner_co_id=OTHER_CO_ID,
                              owner_co_name="Nexgen"),
                self._day_row(finishing_stock_id=4, bucket="LOOSE", stage="HEMMING",
                              qty_uom="CUTS", wt_per_unit_kg=None, wt_kg=1885.0),
            ]),
            _exec_result(fetchone=_attr_row(prev_entry_date=None)),
            _exec_result(fetchone=_attr_row(found=1)),  # bale-source probe: BALE_PROD
            _exec_result(fetchall=[_mock_row({
                "finishing_quality_id": 2, "item_id": 270011,
                "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2,
                "bales": 120, "inspected_bales": 80, "total_kg": 34800.0,
                "inspected_kg": 23200.0, "despatched_bales": 45,
            })]),
            # 6th statement (§7a): the series breakup — a hessian series must be
            # filtered out, a zero-bale sacking series is kept (the day view
            # decides what to print)
            _exec_result(fetchall=[
                _mock_row({
                    "bale_series_id": 3, "series_name": "40-6.50",
                    "finishing_quality_id": 1, "item_id": 269959,
                    "item_name": "HESSIAN CLOTH 40-6.5", "quality_type": 1,
                    "bales": 9, "inspected_bales": 0, "total_kg": 3619.7,
                    "inspected_kg": 0.0, "despatched_bales": 2,
                }),
                _mock_row({
                    "bale_series_id": 2, "series_name": "BIHAR",
                    "finishing_quality_id": 2, "item_id": 270011,
                    "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2,
                    "bales": 0, "inspected_bales": 0, "total_kg": 0.0,
                    "inspected_kg": 0.0, "despatched_bales": 20,
                }),
                _mock_row({
                    "bale_series_id": 1, "series_name": "PUNJAB",
                    "finishing_quality_id": 2, "item_id": 270011,
                    "item_name": "PRINTED TYPE A JUTE BAGS", "quality_type": 2,
                    "bales": 120, "inspected_bales": 80, "total_kg": 34800.0,
                    "inspected_kg": 23200.0, "despatched_bales": 25,
                }),
            ]),
        ]
        response = client.get(
            "/api/finishingStock/rows_by_date?co_id=1&branch_id=2&tran_date=2026-09-16"
        )
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["bale_source"] == "BALE_PROD"
        assert body["prev_entry_date"] is None
        assert [r["superseded"] for r in body["rows"]] == [True, True, False, False]
        line = body["derived_bales"][0]
        assert line["bales"] == 120
        assert line["inspected_bales"] == 80
        assert line["per_wt_kg"] == 290.0
        assert line["wt_mt"] == 34.8
        # §7a: sacking series only, in series-name order, zero-bale series kept
        series = body["derived_series"]
        assert [(l["series_name"], l["bales"]) for l in series] == [("BIHAR", 0), ("PUNJAB", 120)]
        punjab = series[1]
        assert punjab["bale_series_id"] == 1
        assert punjab["finishing_quality_id"] == 2
        assert punjab["quality_type"] == 2
        assert punjab["inspected_bales"] == 80
        assert punjab["per_wt_kg"] == 290.0
        assert punjab["wt_mt"] == 34.8
        assert series[0]["per_wt_kg"] is None
        calls = self._mock_session.execute.call_args_list
        assert len(calls) == 6
        _assert_binds_complete(calls[3])
        _assert_binds_complete(calls[4])
        _assert_binds_complete(calls[5])
        assert calls[3].args[1]["as_on"] == date(2026, 9, 16)
        assert calls[4].args[1]["cancelled_status"] == [4, 6]
        assert calls[5].args[1] == calls[4].args[1]  # series statement, same binds
        assert "GROUP BY x.bale_series_id" in str(calls[5].args[0])

    def test_rows_by_date_requires_tran_date(self):
        response = client.get("/api/finishingStock/rows_by_date?co_id=1&branch_id=2")
        assert response.status_code == 400

    # ---- edit / delete ----------------------------------------------------

    def test_edit_wrong_co_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.put(
            "/api/finishingStock/row_edit/11?co_id=1",
            json={k: v for k, v in _bale_payload().items()
                  if k not in ("co_id", "branch_id", "tran_date")},
        )
        assert response.status_code == 404

    def test_edit_updates_row(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(finishing_stock_id=11, co_id=1, branch_id=2,
                                            tran_date=date(2026, 9, 16))),
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=None),  # bale-source probe: MANUAL
            _exec_result(rowcount=1),
        ]
        payload = {k: v for k, v in _bale_payload(qty=80, wt_per_unit_kg=437.384).items()
                   if k not in ("co_id", "branch_id", "tran_date")}
        response = client.put("/api/finishingStock/row_edit/11?co_id=1", json=payload)
        assert response.status_code == 200
        assert response.json()["data"] == {"updated": True}
        update_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(update_call)
        assert update_call.args[1]["qty"] == 80
        assert update_call.args[1]["id"] == 11
        assert update_call.args[1]["co_id"] == 1

    def test_edit_own_bale_refused_when_derived(self):
        """The probe uses the EXISTING row's (branch, tran_date) — those are
        immutable on edit and the body does not carry them."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(finishing_stock_id=11, co_id=1, branch_id=7,
                                            tran_date=date(2026, 9, 16))),
            _exec_result(fetchone=_quality_row()),
            _exec_result(fetchone=_attr_row(found=1)),  # bale-source probe: BALE_PROD
        ]
        payload = {k: v for k, v in _bale_payload(qty=80).items()
                   if k not in ("co_id", "branch_id", "tran_date")}
        response = client.put("/api/finishingStock/row_edit/11?co_id=1", json=payload)
        assert response.status_code == 400
        assert response.json()["detail"] == OWN_BALE_DERIVED_MSG
        probe = self._mock_session.execute.call_args_list[-1]
        assert probe.args[1] == {"co_id": 1, "branch_id": 7, "as_on": date(2026, 9, 16)}

    def test_edit_requires_co_id(self):
        response = client.put("/api/finishingStock/row_edit/11", json={
            "bucket": "BALE", "finishing_quality_id": 1, "stage": None, "work_by": 0,
            "owner_co_id": None, "qty": 1, "qty_uom": "BALE",
            "wt_per_unit_kg": 400, "wt_kg": None, "remarks": None,
        })
        assert response.status_code == 400

    def test_delete_soft(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(finishing_stock_id=11, co_id=1, branch_id=2,
                                            tran_date=date(2026, 9, 16))),
            _exec_result(rowcount=1),
        ]
        response = client.delete("/api/finishingStock/row_delete/11?co_id=1")
        assert response.status_code == 200
        assert response.json()["data"] == {"deleted": True}
        delete_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(delete_call)
        stmt = str(delete_call.args[0])
        assert "active = 0" in stmt
        assert "DELETE" not in stmt.upper()

    def test_delete_missing_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        assert client.delete("/api/finishingStock/row_delete/11?co_id=1").status_code == 404

    # ---- copy previous day ------------------------------------------------

    def test_copy_refuses_non_empty_day(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row({"finishing_stock_id": 1})]),
        ]
        response = client.post(
            "/api/finishingStock/copy_previous_day",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 400

    def test_copy_no_previous_404(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[]),
            _exec_result(fetchone=_attr_row(prev_entry_date=None)),
        ]
        response = client.post(
            "/api/finishingStock/copy_previous_day",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 404

    def test_copy_previous_day_happy_path(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[]),
            _exec_result(fetchone=_attr_row(prev_entry_date=date(2026, 9, 15))),
            _exec_result(rowcount=6),
        ]
        response = client.post(
            "/api/finishingStock/copy_previous_day",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 200
        assert response.json()["data"] == {"copied": 6, "from_date": "2026-09-15"}
        copy_call = self._mock_session.execute.call_args_list[-1]
        _assert_binds_complete(copy_call)
        assert copy_call.args[1]["from_date"] == date(2026, 9, 15)


class TestUnitWeightsFor:
    """The pure §6 arithmetic, no HTTP and no DB."""

    QUALITY = {"finishing_quality_id": 2, "item_id": 268938,
               "std_bale_weight": 290.0, "no_of_bags": 500}

    def test_all_three_resolve(self):
        assert unit_weights_for(self.QUALITY, {268938: 49.5}, {2: 25}) == {
            "cut_wt_kg": 49.5, "bag_wt_kg": 0.58, "bundle_wt_kg": 14.5,
        }

    def test_bundle_needs_a_bag_weight(self):
        q = dict(self.QUALITY, no_of_bags=None)
        assert unit_weights_for(q, {}, {2: 25}) == {
            "cut_wt_kg": None, "bag_wt_kg": None, "bundle_wt_kg": None,
        }

    def test_zero_bags_is_not_a_division(self):
        q = dict(self.QUALITY, no_of_bags=0)
        assert unit_weights_for(q, {}, {})["bag_wt_kg"] is None

    def test_cut_weight_is_per_item_not_per_quality(self):
        assert unit_weights_for(self.QUALITY, {268938: 49.5}, {})["cut_wt_kg"] == 49.5
        assert unit_weights_for(self.QUALITY, {999: 49.5}, {})["cut_wt_kg"] is None
