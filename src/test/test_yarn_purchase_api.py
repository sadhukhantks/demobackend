"""Yarn Purchase API (phase 2 of the purchased-yarn ledger).

Mocked-session tests in the house style (test_jute_mr_update_over_issue_warning.py):
the real app is imported, auth and the tenant DB are replaced through
dependency_overrides, and execute() is a SQL-dispatching side effect keyed on
the normalised statement text, so tests do not depend on call order.

Pinned here:
- /setup shape (menu_id from menu_mst by path, yarn items, parties, package types);
- /create happy path and each validation rule (400 with a clear detail);
- /update refused with 409 when the document is Pending Approval (20);
- /open assigns purchase_no from get_next_yarn_purchase_no_for_fy (per branch,
  per financial year of tran_date) only when it is still NULL;
- /send_for_approval refuses a document with no active line;
- /reopen maps 6 -> 21 and 4 -> 1;
- amount = round(kg * rate_per_kg, 2); NULL without a rate;
- kg is the only unit: no uom is read or written anywhere.

Nothing here touches a database.
"""

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.constants import (
    YARN_DOC_STATUS_CANCELLED,
    YARN_DOC_STATUS_DRAFT,
    YARN_DOC_STATUS_OPEN,
    YARN_DOC_STATUS_PENDING,
    YARN_DOC_STATUS_REJECTED,
    YARN_PACKAGE_TYPES,
    YARN_PURCHASE_MENU_PATH,
)
from src.juteProcurement.yarn_purchase import compute_line_amount
from src.juteProcurement.yarn_purchase_query import get_next_yarn_purchase_no_for_fy
from src.main import app

client = TestClient(app)

BASE = "/api/yarnPurchase"
USER_ID = 24
MENU_ID = 987
CO_ID = 2
BRANCH_ID = 29
YARN_ITEM_IDS = {335556, 335557}


def _row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    row.__getitem__ = lambda self, i: list(mapping.values())[i]
    return row


def _norm(sql) -> str:
    return " ".join(str(sql).split()).upper()


class FakeSession:
    """SQL-dispatching session. ``doc`` is the header row the approval-info
    query returns; ``lines`` the active line ids; ``totals`` the
    send-for-approval guard row."""

    def __init__(self, doc=None, line_ids=(), totals=None, menu_id=MENU_ID, next_lastrowid=4321):
        self.doc = doc
        self.line_ids = list(line_ids)
        self.totals = totals or {"line_count": 0, "min_kg": None}
        self.menu_id = menu_id
        self.next_lastrowid = next_lastrowid
        self.calls = []  # (normalised sql, params)
        self.commit = MagicMock()
        self.rollback = MagicMock()

    def execute(self, stmt, params=None):
        s = _norm(stmt)
        params = params or {}
        self.calls.append((s, params))
        res = MagicMock()
        res.fetchone.return_value = None
        res.fetchall.return_value = []

        if "FROM MENU_MST" in s:
            res.fetchone.return_value = _row({"menu_id": self.menu_id}) if self.menu_id else None
        elif "FROM ITEM_MST IM" in s and "WHERE IM.ITEM_ID = :ITEM_ID" in s:
            res.fetchone.return_value = (
                _row({"item_id": params["item_id"]}) if params["item_id"] in YARN_ITEM_IDS else None
            )
        elif "FROM ITEM_MST IM" in s:
            res.fetchall.return_value = [
                _row({"item_id": 335556, "item_code": "Y13SK", "item_name": "13 LBS SKWP",
                      "quality_name": "13 LBS", "yarn_count": 13}),
            ]
        elif "FROM PARTY_MST PM WHERE PM.CO_ID" in s:
            res.fetchall.return_value = [_row({"party_id": 9500, "supp_name": "ABC Yarn Traders"})]
        elif s.startswith("SELECT H.YARN_PURCHASE_ID, H.CO_ID, H.BRANCH_ID, H.STATUS_ID"):
            res.fetchone.return_value = _row(self.doc) if self.doc else None
        elif s.startswith("SELECT H.YARN_PURCHASE_ID, H.CO_ID, H.BRANCH_ID, H.TRAN_DATE"):
            res.fetchone.return_value = _row(self._header_row()) if self.doc else None
        elif s.startswith("SELECT LI.YARN_PURCHASE_LI_ID, LI.YARN_PURCHASE_ID, LI.ITEM_ID, IM.ITEM_NAME"):
            res.fetchall.return_value = [_row(self._line_row(i)) for i in self.line_ids]
        elif s.startswith("SELECT COUNT(*) AS LINE_COUNT, MIN(WEIGHT_KG) AS MIN_KG"):
            res.fetchone.return_value = _row(self.totals)
        elif s.startswith("SELECT YARN_PURCHASE_LI_ID FROM JUTE_YARN_PURCHASE_LI"):
            res.fetchall.return_value = [_row({"yarn_purchase_li_id": i}) for i in self.line_ids]
        elif s.startswith("SELECT COALESCE(MAX(PURCHASE_NO), 0) + 1"):
            res.fetchone.return_value = _row({"next_no": 7})
        elif s.startswith("INSERT INTO JUTE_YARN_PURCHASE ("):
            res.lastrowid = self.next_lastrowid
        elif s.startswith("INSERT INTO JUTE_YARN_PURCHASE_LI"):
            res.lastrowid = 99
        return res

    def _header_row(self):
        from decimal import Decimal  # local: keep module imports minimal
        return {
            **self.doc,
            "supp_name": "ABC Yarn Traders",
            "challan_no": "CH-101",
            "challan_date": date(2026, 9, 24),
            "vehicle_no": "WB23A1234",
            "inward_id": None,
            "status_name": "Open" if self.doc["status_id"] == 1 else "Other",
            "remarks": None,
            "active": 1,
            "updated_by": USER_ID,
            "updated_date_time": datetime(2026, 9, 25, 10, 30, 0),
            "_decimal_probe": Decimal("1.5"),
        }

    def _line_row(self, li_id):
        from decimal import Decimal
        return {
            "yarn_purchase_li_id": li_id,
            "yarn_purchase_id": self.doc["yarn_purchase_id"],
            "item_id": 335556,
            "item_name": "13 LBS SKWP",
            "item_code": "Y13SK",
            "quality_name": "13 LBS",
            "package_type": "SPOOL",
            "no_of_packages": Decimal("200.000"),
            "wt_per_package": Decimal("5.500"),
            "weight_kg": Decimal("1100.000"),
            "rate_per_kg": Decimal("85.00"),
            "amount": Decimal("93500.00"),
            "remarks": None,
            "active": 1,
            "updated_by": USER_ID,
            "updated_date_time": datetime(2026, 9, 25, 10, 30, 0),
        }

    # helpers for assertions
    def calls_starting(self, prefix):
        return [(s, p) for s, p in self.calls if s.startswith(prefix.upper())]


def _install(session):
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": USER_ID}
    app.dependency_overrides[get_tenant_db] = lambda: session
    return session


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _doc(status_id, approval_level=None, purchase_no=None, tran_date=date(2026, 9, 25)):
    return {
        "yarn_purchase_id": 55,
        "co_id": CO_ID,
        "branch_id": BRANCH_ID,
        "status_id": status_id,
        "approval_level": approval_level,
        "tran_date": tran_date,
        "purchase_no": purchase_no,
    }


def _body(**overrides):
    body = {
        "co_id": CO_ID,
        "branch_id": BRANCH_ID,
        "tran_date": "2026-09-25",
        "party_id": 9500,
        "challan_no": "CH-101",
        "challan_date": "2026-09-24",
        "vehicle_no": "WB23A1234",
        "inward_id": None,
        "remarks": "first lorry",
        "lines": [
            {"item_id": 335556, "package_type": "SPOOL", "no_of_packages": 200,
             "wt_per_package": 5.5, "weight_kg": 1100, "rate_per_kg": 85, "remarks": None},
            {"item_id": 335557, "package_type": "BAG", "no_of_packages": 10,
             "wt_per_package": 50, "weight_kg": 500},
        ],
    }
    body.update(overrides)
    return body


def _action(**overrides):
    payload = {"yarn_purchase_id": 55, "branch_id": BRANCH_ID, "menu_id": MENU_ID}
    payload.update(overrides)
    return payload


# =============================================================================
# setup
# =============================================================================


def test_setup_shape():
    session = _install(FakeSession())
    resp = client.get(f"{BASE}/setup", params={"co_id": CO_ID, "branch_id": BRANCH_ID})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data) == {"menu_id", "yarn_items", "parties", "package_types"}
    assert data["menu_id"] == MENU_ID
    assert data["package_types"] == list(YARN_PACKAGE_TYPES) == ["SPOOL", "BAG", "CONE", "BUNDLE"]
    assert data["yarn_items"][0] == {
        "item_id": 335556, "item_code": "Y13SK", "item_name": "13 LBS SKWP",
        "quality_name": "13 LBS", "yarn_count": 13.0,
    }
    assert data["parties"] == [{"party_id": 9500, "supp_name": "ABC Yarn Traders"}]
    # menu resolved by the canonical path, items scoped to the company
    menu_calls = [p for s, p in session.calls if "FROM MENU_MST" in s]
    assert menu_calls == [{"menu_path": YARN_PURCHASE_MENU_PATH}]
    item_calls = [p for s, p in session.calls if "FROM ITEM_MST IM" in s]
    assert item_calls == [{"co_id": CO_ID}]


def test_setup_requires_co_id():
    _install(FakeSession())
    resp = client.get(f"{BASE}/setup")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "co_id is required"


# =============================================================================
# get / list
# =============================================================================


def test_get_returns_header_lines_and_permissions():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, purchase_no=3), line_ids=(1, 2)))
    perms = {"canApprove": False, "canReject": False, "canOpen": False, "canSendForApproval": True,
             "canCancelDraft": False, "canReopen": False, "canViewApprovalLog": False,
             "canClone": False, "canSave": True}
    with patch("src.juteProcurement.yarn_purchase.calculate_approval_permissions", return_value=perms) as cap:
        resp = client.get(f"{BASE}/get/55")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data) == {"header", "lines", "permissions"}
    h = data["header"]
    assert h["yarn_purchase_id"] == 55 and h["purchase_no"] == 3
    assert h["supp_name"] == "ABC Yarn Traders" and h["status_name"] == "Open"
    assert h["tran_date"] == "2026-09-25" and h["challan_date"] == "2026-09-24"
    assert h["_decimal_probe"] == 1.5                       # Decimals become floats
    assert len(data["lines"]) == 2
    l1 = data["lines"][0]
    assert l1["yarn_purchase_li_id"] == 1 and l1["item_name"] == "13 LBS SKWP"
    assert l1["item_code"] == "Y13SK" and l1["quality_name"] == "13 LBS"
    assert l1["weight_kg"] == 1100.0 and l1["amount"] == 93500.0 and l1["rate_per_kg"] == 85.0
    assert "uom" not in l1 and "uom" not in h
    assert data["permissions"] == perms
    # permissions evaluated for the caller against the canonical menu and the header's branch/status
    args = cap.call_args.args
    assert args[:5] == (USER_ID, MENU_ID, BRANCH_ID, YARN_DOC_STATUS_OPEN, None)
    assert args[5] is session


def test_get_404_when_missing():
    _install(FakeSession(doc=None))
    resp = client.get(f"{BASE}/get/55")
    assert resp.status_code == 404


def test_list_binds_filters_and_returns_rows():
    session = _install(FakeSession())
    resp = client.get(f"{BASE}/list", params={"co_id": CO_ID, "branch_id": BRANCH_ID,
                                              "from_date": "2026-04-01", "to_date": "2026-09-30", "status_id": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": []}
    s, p = session.calls_starting("SELECT H.YARN_PURCHASE_ID, H.PURCHASE_NO")[0]
    assert p == {"co_id": CO_ID, "branch_id": BRANCH_ID, "from_date": date(2026, 4, 1),
                 "to_date": date(2026, 9, 30), "status_id": 1}
    assert "ORDER BY H.TRAN_DATE DESC, H.YARN_PURCHASE_ID DESC" in s
    assert "FROM STATUS_MST" in s or "JOIN STATUS_MST" in s


def test_list_status_optional_and_requires_branch():
    session = _install(FakeSession())
    assert client.get(f"{BASE}/list", params={"co_id": CO_ID, "branch_id": BRANCH_ID}).status_code == 200
    assert session.calls[-1][1]["status_id"] is None
    resp = client.get(f"{BASE}/list", params={"co_id": CO_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "branch_id is required"


# =============================================================================
# create
# =============================================================================


def test_create_happy_path_inserts_header_draft_and_lines():
    session = _install(FakeSession(next_lastrowid=4321))
    resp = client.post(f"{BASE}/create", json=_body())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": {"yarn_purchase_id": 4321, "status_id": YARN_DOC_STATUS_DRAFT}}

    hdr = session.calls_starting("INSERT INTO JUTE_YARN_PURCHASE (")
    assert len(hdr) == 1
    hp = hdr[0][1]
    assert hp["co_id"] == CO_ID and hp["branch_id"] == BRANCH_ID
    assert hp["tran_date"] == date(2026, 9, 25)
    assert hp["challan_date"] == date(2026, 9, 24)
    assert hp["status_id"] == YARN_DOC_STATUS_DRAFT
    assert hp["updated_by"] == USER_ID
    assert hp["party_id"] == 9500 and hp["inward_id"] is None

    lines = session.calls_starting("INSERT INTO JUTE_YARN_PURCHASE_LI")
    assert len(lines) == 2
    l1, l2 = lines[0][1], lines[1][1]
    assert l1["yarn_purchase_id"] == 4321 and l2["yarn_purchase_id"] == 4321
    assert l1["item_id"] == 335556 and l1["package_type"] == "SPOOL"
    assert l1["weight_kg"] == 1100 and l1["rate_per_kg"] == 85
    assert l1["amount"] == 93500.0          # 1100 kg * 85
    assert l2["rate_per_kg"] is None and l2["amount"] is None
    # kg is the only unit: nothing binds a uom
    for s, p in session.calls:
        assert "UOM" not in s and "uom" not in p
    session.commit.assert_called_once()
    session.rollback.assert_not_called()


@pytest.mark.parametrize(
    "overrides, detail",
    [
        ({"co_id": None}, "co_id is required"),
        ({"branch_id": ""}, "branch_id is required"),
        ({"tran_date": None}, "tran_date is required"),
        ({"tran_date": "25/09/2026"}, "Invalid tran_date (expected YYYY-MM-DD)"),
        ({"lines": []}, "At least one line is required"),
        ({"lines": [{"item_id": 335556, "weight_kg": 0}]}, "weight_kg must be greater than 0 on line 1"),
        ({"lines": [{"item_id": 335556, "weight_kg": -5}]}, "weight_kg must be greater than 0 on line 1"),
        ({"lines": [{"item_id": 335556}]}, "weight_kg is required on line 1"),
        ({"lines": [{"weight_kg": 10}]}, "item_id is required on line 1"),
        ({"lines": [{"item_id": 335556, "weight_kg": 10, "package_type": "ROLL"}]},
         "package_type on line 1 must be one of SPOOL, BAG, CONE, BUNDLE"),
        ({"lines": [{"item_id": 334018, "weight_kg": 10}]},
         "item_id 334018 on line 1 is not an active yarn item of company 2"),
    ],
)
def test_create_validation_failures(overrides, detail):
    session = _install(FakeSession())
    resp = client.post(f"{BASE}/create", json=_body(**overrides))
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == detail
    assert not session.calls_starting("INSERT INTO")
    session.commit.assert_not_called()


def test_create_rejects_non_yarn_item_even_when_active_elsewhere():
    """Item 334018 is the legacy Stores 'spools/yarn' row (item_type 1) —
    exactly the item the design says must never be booked here."""
    session = _install(FakeSession())
    resp = client.post(f"{BASE}/create", json=_body(lines=[{"item_id": 334018, "weight_kg": 100}]))
    assert resp.status_code == 400
    verify = [p for s, p in session.calls if "WHERE IM.ITEM_ID = :ITEM_ID" in s]
    assert verify == [{"item_id": 334018, "co_id": CO_ID}]


def test_create_package_type_is_normalised_to_upper():
    session = _install(FakeSession())
    resp = client.post(f"{BASE}/create", json=_body(lines=[{"item_id": 335556, "weight_kg": 10, "package_type": "cone"}]))
    assert resp.status_code == 200, resp.text
    assert session.calls_starting("INSERT INTO JUTE_YARN_PURCHASE_LI")[0][1]["package_type"] == "CONE"


# =============================================================================
# amount formula
# =============================================================================


def test_amount_is_kg_times_rate_per_kg():
    assert compute_line_amount(1100, 85) == 93500.0
    assert compute_line_amount(250, 80) == 20000.0
    assert compute_line_amount(33.333, 81.2345) == round(33.333 * 81.2345, 2)
    assert compute_line_amount(1100, None) is None


# =============================================================================
# update
# =============================================================================


def test_update_refused_when_pending_approval():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_PENDING, approval_level=1)))
    resp = client.put(f"{BASE}/update/55", json=_body())
    assert resp.status_code == 409, resp.text
    assert "status_id 20 cannot be updated" in resp.json()["detail"]
    assert not session.calls_starting("UPDATE JUTE_YARN_PURCHASE")
    assert not session.calls_starting("INSERT INTO")
    session.commit.assert_not_called()


def test_update_replaces_lines_in_one_transaction():
    # existing active lines 1, 2, 3: keep 1 (edited), drop 2 and 3, add one new
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, purchase_no=3), line_ids=(1, 2, 3)))
    body = _body(lines=[
        {"yarn_purchase_li_id": 1, "item_id": 335556, "weight_kg": 900, "rate_per_kg": 80},
        {"item_id": 335557, "weight_kg": 40},
    ])
    resp = client.put(f"{BASE}/update/55", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": {"yarn_purchase_id": 55, "status_id": YARN_DOC_STATUS_OPEN}}

    hdr = session.calls_starting("UPDATE JUTE_YARN_PURCHASE SET TRAN_DATE")
    assert len(hdr) == 1 and hdr[0][1]["yarn_purchase_id"] == 55
    assert "STATUS_ID" not in hdr[0][0] and "PURCHASE_NO" not in hdr[0][0]

    deleted = session.calls_starting("UPDATE JUTE_YARN_PURCHASE_LI SET ACTIVE = 0")
    assert sorted(p["yarn_purchase_li_id"] for _, p in deleted) == [2, 3]
    updated = session.calls_starting("UPDATE JUTE_YARN_PURCHASE_LI SET ITEM_ID")
    assert [p["yarn_purchase_li_id"] for _, p in updated] == [1]
    assert updated[0][1]["amount"] == 72000.0
    inserted = session.calls_starting("INSERT INTO JUTE_YARN_PURCHASE_LI")
    assert len(inserted) == 1 and inserted[0][1]["item_id"] == 335557
    session.commit.assert_called_once()
    session.rollback.assert_not_called()


def test_update_rejects_foreign_line_id():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT), line_ids=(1,)))
    resp = client.put(f"{BASE}/update/55", json=_body(lines=[{"yarn_purchase_li_id": 77, "item_id": 335556, "weight_kg": 5}]))
    assert resp.status_code == 400
    assert "77" in resp.json()["detail"]
    session.commit.assert_not_called()


def test_update_404_when_missing():
    _install(FakeSession(doc=None))
    resp = client.put(f"{BASE}/update/55", json=_body())
    assert resp.status_code == 404


# =============================================================================
# open
# =============================================================================


def test_open_assigns_purchase_no_from_fy_helper():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT, purchase_no=None, tran_date=date(2026, 9, 25))))
    with patch("src.juteProcurement.yarn_purchase.get_next_yarn_purchase_no_for_fy", return_value=42) as helper:
        resp = client.post(f"{BASE}/open", json=_action())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == {"yarn_purchase_id": 55, "status_id": YARN_DOC_STATUS_OPEN, "purchase_no": 42}
    helper.assert_called_once()
    _, args_branch, args_date = helper.call_args.args
    assert (args_branch, args_date) == (BRANCH_ID, date(2026, 9, 25))

    upd = session.calls_starting("UPDATE JUTE_YARN_PURCHASE SET STATUS_ID")
    assert len(upd) == 1
    p = upd[0][1]
    assert p["status_id"] == YARN_DOC_STATUS_OPEN and p["approval_level"] is None
    assert p["purchase_no"] == 42 and p["updated_by"] == USER_ID
    session.commit.assert_called_once()


def test_open_keeps_existing_purchase_no():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT, purchase_no=9)))
    with patch("src.juteProcurement.yarn_purchase.get_next_yarn_purchase_no_for_fy") as helper:
        resp = client.post(f"{BASE}/open", json=_action())
    assert resp.status_code == 200
    helper.assert_not_called()
    assert session.calls_starting("UPDATE JUTE_YARN_PURCHASE SET STATUS_ID")[0][1]["purchase_no"] == 9


def test_open_refused_from_wrong_status():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN)))
    resp = client.post(f"{BASE}/open", json=_action())
    assert resp.status_code == 400
    assert "Expected status 21" in resp.json()["detail"]
    session.commit.assert_not_called()


def test_fy_helper_uses_april_to_march_year_of_tran_date():
    session = FakeSession()
    assert get_next_yarn_purchase_no_for_fy(session, BRANCH_ID, date(2026, 2, 10)) == 7
    s, p = session.calls[-1]
    assert p == {"branch_id": BRANCH_ID, "fy_string": "25-26"}   # Feb 2026 is FY 25-26
    assert "FROM JUTE_YARN_PURCHASE" in s and "PURCHASE_NO IS NOT NULL" in s and "MONTH(TRAN_DATE)" in s
    get_next_yarn_purchase_no_for_fy(session, BRANCH_ID, date(2026, 4, 1))
    assert session.calls[-1][1]["fy_string"] == "26-27"


# =============================================================================
# cancel / send_for_approval / reopen
# =============================================================================


def test_cancel_draft_to_cancelled():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT)))
    resp = client.post(f"{BASE}/cancel", json=_action())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status_id"] == YARN_DOC_STATUS_CANCELLED
    p = session.calls_starting("UPDATE JUTE_YARN_PURCHASE SET STATUS_ID")[0][1]
    assert p["status_id"] == YARN_DOC_STATUS_CANCELLED and p["purchase_no"] is None


def test_send_for_approval_refuses_empty_document():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, purchase_no=3), totals={"line_count": 0, "min_kg": None}))
    resp = client.post(f"{BASE}/send_for_approval", json=_action())
    assert resp.status_code == 400, resp.text
    assert "has no lines" in resp.json()["detail"]
    assert not session.calls_starting("UPDATE JUTE_YARN_PURCHASE SET STATUS_ID")
    session.commit.assert_not_called()


def test_send_for_approval_refuses_zero_weight_line():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, purchase_no=3), totals={"line_count": 2, "min_kg": 0}))
    resp = client.post(f"{BASE}/send_for_approval", json=_action())
    assert resp.status_code == 400
    assert "weight_kg greater than 0" in resp.json()["detail"]
    session.commit.assert_not_called()


def test_send_for_approval_open_to_pending_level_1():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, purchase_no=3), totals={"line_count": 2, "min_kg": 40}))
    resp = client.post(f"{BASE}/send_for_approval", json=_action())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"yarn_purchase_id": 55, "status_id": YARN_DOC_STATUS_PENDING, "approval_level": 1}
    p = session.calls_starting("UPDATE JUTE_YARN_PURCHASE SET STATUS_ID")[0][1]
    assert p["status_id"] == YARN_DOC_STATUS_PENDING and p["approval_level"] == 1
    session.commit.assert_called_once()


def test_send_for_approval_refused_from_draft():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT), totals={"line_count": 1, "min_kg": 5}))
    resp = client.post(f"{BASE}/send_for_approval", json=_action())
    assert resp.status_code == 400
    assert "Expected status 1" in resp.json()["detail"]


@pytest.mark.parametrize(
    "from_status, to_status",
    [(YARN_DOC_STATUS_CANCELLED, YARN_DOC_STATUS_DRAFT), (YARN_DOC_STATUS_REJECTED, YARN_DOC_STATUS_OPEN)],
)
def test_reopen_mapping(from_status, to_status):
    session = _install(FakeSession(doc=_doc(from_status, approval_level=1, purchase_no=3)))
    resp = client.post(f"{BASE}/reopen", json=_action())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"yarn_purchase_id": 55, "status_id": to_status, "approval_level": None}
    p = session.calls_starting("UPDATE JUTE_YARN_PURCHASE SET STATUS_ID")[0][1]
    assert p["status_id"] == to_status and p["approval_level"] is None and p["purchase_no"] is None
    session.commit.assert_called_once()


def test_reopen_refused_from_approved():
    _install(FakeSession(doc=_doc(3)))
    resp = client.post(f"{BASE}/reopen", json=_action())
    assert resp.status_code == 400
    assert "Only Cancelled (6) or Rejected (4)" in resp.json()["detail"]


def test_action_payload_requires_ids():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT)))
    resp = client.post(f"{BASE}/open", json={"branch_id": BRANCH_ID, "menu_id": MENU_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "yarn_purchase_id is required"
    resp = client.post(f"{BASE}/open", json={"yarn_purchase_id": 55, "branch_id": BRANCH_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "menu_id is required"


# =============================================================================
# approve / reject go through the shared engine
# =============================================================================


def test_approve_delegates_to_process_approval():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_PENDING, approval_level=1)))
    with patch("src.juteProcurement.yarn_purchase.process_approval",
               return_value={"status": "success", "new_status_id": 3, "new_approval_level": 1, "message": "ok"}) as pa:
        resp = client.post(f"{BASE}/approve", json=_action())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"yarn_purchase_id": 55, "status_id": 3, "approval_level": 1}
    kw = pa.call_args.kwargs
    assert kw["doc_id"] == 55 and kw["user_id"] == USER_ID and kw["menu_id"] == MENU_ID
    assert kw["id_param_name"] == "yarn_purchase_id" and kw["doc_name"] == "Yarn Purchase"
    assert kw["extra_update_params"] == {"purchase_no": None}
    # the engine's contract: doc query yields status_id/approval_level/branch_id,
    # update query binds the five standard names + purchase_no
    doc_sql = _norm(kw["get_doc_fn"]())
    assert "H.STATUS_ID" in doc_sql and "H.APPROVAL_LEVEL" in doc_sql and "H.BRANCH_ID" in doc_sql
    binds = set(kw["update_status_fn"]().compile().binds)
    assert binds == {"yarn_purchase_id", "status_id", "approval_level", "updated_by", "updated_date_time", "purchase_no"}


def test_reject_delegates_to_process_rejection():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_PENDING, approval_level=1)))
    with patch("src.juteProcurement.yarn_purchase.process_rejection",
               return_value={"status": "success", "new_status_id": 4, "new_approval_level": None, "message": "ok"}) as pr:
        resp = client.post(f"{BASE}/reject", json={"yarn_purchase_id": 55, "co_id": CO_ID, "menu_id": MENU_ID, "reason": "wrong lorry"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status_id"] == YARN_DOC_STATUS_REJECTED
    kw = pr.call_args.kwargs
    assert kw["doc_id"] == 55 and kw["menu_id"] == MENU_ID and kw["reason"] == "wrong lorry"
    assert kw["id_param_name"] == "yarn_purchase_id"


def test_reject_requires_co_id():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_PENDING, approval_level=1)))
    resp = client.post(f"{BASE}/reject", json={"yarn_purchase_id": 55, "menu_id": MENU_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "co_id is required"


# =============================================================================
# every statement binds what it declares
# =============================================================================


def test_all_executed_statements_have_complete_binds():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, purchase_no=3), line_ids=(1,), totals={"line_count": 1, "min_kg": 5}))
    client.post(f"{BASE}/create", json=_body())
    client.put(f"{BASE}/update/55", json=_body(lines=[{"yarn_purchase_li_id": 1, "item_id": 335556, "weight_kg": 5}, {"item_id": 335557, "weight_kg": 6}]))
    client.get(f"{BASE}/list", params={"co_id": CO_ID, "branch_id": BRANCH_ID, "from_date": "2026-04-01", "to_date": "2026-09-30"})
    client.get(f"{BASE}/get/55")
    client.post(f"{BASE}/send_for_approval", json=_action())
    session.doc = _doc(YARN_DOC_STATUS_DRAFT)
    client.post(f"{BASE}/open", json=_action())
    from src.juteProcurement import yarn_purchase_query as q
    stmts = {
        name: getattr(q, name)()
        for name in dir(q)
        if name.endswith("_query") or name in ("update_yarn_purchase_status", "get_yarn_purchase_with_approval_info")
    }
    # each executed statement's declared binds must be present in its params
    seen = 0
    for s, p in session.calls:
        for name, stmt in stmts.items():
            if _norm(stmt) == s:
                missing = set(stmt.compile().binds) - set(p)
                assert not missing, (name, missing)
                seen += 1
                break
    assert seen >= 10
