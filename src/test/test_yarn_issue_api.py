"""Yarn Issue / Return API (phase 3 of the purchased-yarn ledger).

Mocked-session tests in the house style (test_yarn_purchase_api.py): the
real app is imported, auth and the tenant DB are replaced through
dependency_overrides, and execute() is a SQL-dispatching side effect keyed
on the normalised statement text, so tests do not depend on call order.

Pinned here:
- /setup shape (menu_id from menu_mst by path, departments of the branch,
  issue types, yarn items);
- /godown_lots is FIFO-ordered and narrowed to issuable purchases with a
  positive balance; /dept_lots binds the department and returns floor_bal_kg;
- /create happy path for ISSUE and RETURN, each validation rule (400);
- ISSUE over the godown balance is a hard 400 naming the lot and balance;
- RETURN over the department floor balance is a warning on a 200;
- skipping an older lot with stock is a FIFO warning, never a block;
- /update refused with 409 when Pending Approval (20); the lot guard on
  update binds exclude_issue_id so the document's own lines are added back;
- /open assigns issue_no from get_next_yarn_issue_no_for_fy (per branch, per
  financial year of tran_date, one series per issue_type) only when NULL;
- /reopen maps 6 -> 21 and 4 -> 1; approve/reject delegate to the engine.

Nothing here touches a database.
"""

from datetime import date, datetime
from decimal import Decimal
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
    YARN_ISSUE_MENU_PATH,
    YARN_ISSUE_TYPES,
)
from src.juteProcurement.yarn_issue_query import get_next_yarn_issue_no_for_fy
from src.main import app

client = TestClient(app)

BASE = "/api/yarnIssue"
USER_ID = 24
MENU_ID = 988
CO_ID = 2
BRANCH_ID = 29
DEPT_ID = 12
DEPT_IDS = {12, 13}
ITEM_A = 335556  # 13 LBS SKWP
ITEM_B = 335557  # 10 LBS SKWP

# Godown lots as vw_yarn_godown_outstanding would show them (before any
# exclude_issue_id add-back). Lot 101 is the oldest of item A.
LOTS = {
    101: {"purchase_no": 1, "tran_date": date(2026, 9, 1), "co_id": CO_ID, "branch_id": BRANCH_ID,
          "item_id": ITEM_A, "purchase_status_id": 3, "bal_kg": Decimal("500.000"), "bal_pkgs": Decimal("100.000")},
    102: {"purchase_no": 2, "tran_date": date(2026, 9, 10), "co_id": CO_ID, "branch_id": BRANCH_ID,
          "item_id": ITEM_A, "purchase_status_id": 3, "bal_kg": Decimal("800.000"), "bal_pkgs": Decimal("160.000")},
    103: {"purchase_no": 3, "tran_date": date(2026, 9, 12), "co_id": CO_ID, "branch_id": BRANCH_ID,
          "item_id": ITEM_B, "purchase_status_id": 3, "bal_kg": Decimal("300.000"), "bal_pkgs": Decimal("60.000")},
    104: {"purchase_no": 4, "tran_date": date(2026, 9, 15), "co_id": CO_ID, "branch_id": BRANCH_ID,
          "item_id": ITEM_A, "purchase_status_id": 20, "bal_kg": Decimal("200.000"), "bal_pkgs": Decimal("40.000")},
    105: {"purchase_no": 5, "tran_date": date(2026, 9, 16), "co_id": CO_ID, "branch_id": BRANCH_ID,
          "item_id": ITEM_A, "purchase_status_id": 3, "bal_kg": Decimal("0.000"), "bal_pkgs": Decimal("0.000")},
    106: {"purchase_no": 6, "tran_date": date(2026, 9, 16), "co_id": 3, "branch_id": 40,
          "item_id": ITEM_A, "purchase_status_id": 3, "bal_kg": Decimal("100.000"), "bal_pkgs": Decimal("20.000")},
}
ITEM_NAMES = {ITEM_A: ("13 LBS SKWP", "Y13SK"), ITEM_B: ("10 LBS SKWP", "Y10SK")}


def _row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    row.__getitem__ = lambda self, i: list(mapping.values())[i]
    return row


def _norm(sql) -> str:
    return " ".join(str(sql).split()).upper()


def _picker_row(lot: dict) -> dict:
    """The picker queries do not return the guard-only columns."""
    return {k: v for k, v in lot.items() if k not in ("co_id", "branch_id", "purchase_status_id")}


# The FIFO picker is the only statement that narrows on the purchase status.
FIFO_SQL = "AND V.PURCHASE_STATUS_ID IN ("


class FakeSession:
    """SQL-dispatching session. ``doc`` is the header row the approval-info
    query returns; ``line_ids`` the active line ids; ``totals`` the
    send-for-approval guard row; ``lots`` the godown view; ``floor`` the
    department floor balance per lot; ``own`` this document's own saved kg
    per lot (added back when exclude_issue_id matches the doc)."""

    def __init__(self, doc=None, line_ids=(), totals=None, menu_id=MENU_ID, next_lastrowid=4321,
                 lots=None, floor=None, own=None):
        self.doc = doc
        self.line_ids = list(line_ids)
        self.totals = totals or {"line_count": 0, "min_kg": None}
        self.menu_id = menu_id
        self.next_lastrowid = next_lastrowid
        self.lots = dict(LOTS if lots is None else lots)
        self.floor = dict(floor or {})
        self.own = dict(own or {})
        self.calls = []  # (normalised sql, params)
        self.commit = MagicMock()
        self.rollback = MagicMock()

    # -- lot helpers -------------------------------------------------------
    def _lot(self, lot_id, exclude_issue_id):
        lot = self.lots[lot_id]
        bal = float(lot["bal_kg"])
        if exclude_issue_id is not None and self.doc and exclude_issue_id == self.doc["yarn_issue_id"]:
            bal += self.own.get(lot_id, 0.0)
        name, code = ITEM_NAMES[lot["item_id"]]
        return {
            "yarn_purchase_li_id": lot_id, "yarn_purchase_id": 1000 + lot_id, "purchase_no": lot["purchase_no"],
            "tran_date": lot["tran_date"], "supp_name": "ABC Yarn Traders", "item_id": lot["item_id"],
            "item_name": name, "item_code": code, "package_type": "SPOOL", "received_kg": Decimal("1000.000"),
            "co_id": lot["co_id"], "branch_id": lot["branch_id"], "purchase_status_id": lot["purchase_status_id"],
            "bal_kg": Decimal(str(round(bal, 3))), "bal_pkgs": lot["bal_pkgs"],
        }

    def _godown_lots(self, p):
        rows = []
        for lot_id in self.lots:
            lot = self._lot(lot_id, p.get("exclude_issue_id"))
            if lot["co_id"] != p["co_id"] or lot["branch_id"] != p["branch_id"]:
                continue
            if lot["purchase_status_id"] != 3 or float(lot["bal_kg"]) <= 0:
                continue
            if p.get("item_id") is not None and lot["item_id"] != p["item_id"]:
                continue
            rows.append(_picker_row(lot))
        rows.sort(key=lambda r: (r["tran_date"], r["yarn_purchase_li_id"]))
        return [_row(r) for r in rows]

    def _dept_lots(self, p):
        rows = []
        for lot_id, floor_kg in self.floor.items():
            if floor_kg <= 0:
                continue
            lot = self._lot(lot_id, None)
            if p.get("item_id") is not None and lot["item_id"] != p["item_id"]:
                continue
            lot = _picker_row(lot)
            lot["floor_bal_kg"] = Decimal(str(floor_kg))
            lot["floor_bal_pkgs"] = Decimal("0.000")
            rows.append(lot)
        rows.sort(key=lambda r: (r["tran_date"], r["yarn_purchase_li_id"]))
        return [_row(r) for r in rows]

    # -- dispatcher --------------------------------------------------------
    def execute(self, stmt, params=None):
        s = _norm(stmt)
        params = params or {}
        self.calls.append((s, params))
        res = MagicMock()
        res.fetchone.return_value = None
        res.fetchall.return_value = []

        if "FROM MENU_MST" in s:
            res.fetchone.return_value = _row({"menu_id": self.menu_id}) if self.menu_id else None
        elif "FROM DEPT_MST D WHERE D.DEPT_ID = :DEPT_ID" in s:
            ok = params["dept_id"] in DEPT_IDS and params["branch_id"] == BRANCH_ID
            res.fetchone.return_value = _row({"dept_id": params["dept_id"]}) if ok else None
        elif "FROM DEPT_MST D WHERE D.BRANCH_ID = :BRANCH_ID" in s:
            res.fetchall.return_value = [
                _row({"dept_id": 12, "dept_desc": "WINDING", "dept_code": "WND"}),
                _row({"dept_id": 13, "dept_desc": "WEAVING", "dept_code": "WVG"}),
            ]
        elif "FROM ITEM_MST IM" in s:
            res.fetchall.return_value = [
                _row({"item_id": ITEM_A, "item_code": "Y13SK", "item_name": "13 LBS SKWP",
                      "quality_name": "13 LBS", "yarn_count": 13}),
            ]
        elif "WHERE V.YARN_PURCHASE_LI_ID = :YARN_PURCHASE_LI_ID" in s:
            lot_id = params["yarn_purchase_li_id"]
            res.fetchone.return_value = _row(self._lot(lot_id, params.get("exclude_issue_id"))) if lot_id in self.lots else None
        elif "AS FLOOR_BAL_KG" in s:
            res.fetchall.return_value = self._dept_lots(params)
        elif "FROM VW_YARN_GODOWN_OUTSTANDING V" in s:
            res.fetchall.return_value = self._godown_lots(params)
        elif s.startswith("SELECT H.YARN_ISSUE_ID, H.CO_ID, H.BRANCH_ID, H.STATUS_ID"):
            res.fetchone.return_value = _row(self.doc) if self.doc else None
        elif s.startswith("SELECT H.YARN_ISSUE_ID, H.CO_ID, H.BRANCH_ID, H.TRAN_DATE"):
            res.fetchone.return_value = _row(self._header_row()) if self.doc else None
        elif s.startswith("SELECT LI.YARN_ISSUE_LI_ID, LI.YARN_ISSUE_ID, LI.ITEM_ID, IM.ITEM_NAME"):
            res.fetchall.return_value = [_row(self._line_row(i)) for i in self.line_ids]
        elif s.startswith("SELECT COUNT(*) AS LINE_COUNT, MIN(WEIGHT_KG) AS MIN_KG"):
            res.fetchone.return_value = _row(self.totals)
        elif s.startswith("SELECT YARN_ISSUE_LI_ID FROM JUTE_YARN_ISSUE_LI"):
            res.fetchall.return_value = [_row({"yarn_issue_li_id": i}) for i in self.line_ids]
        elif s.startswith("SELECT COALESCE(MAX(ISSUE_NO), 0) + 1"):
            res.fetchone.return_value = _row({"next_no": 7})
        elif s.startswith("INSERT INTO JUTE_YARN_ISSUE ("):
            res.lastrowid = self.next_lastrowid
        elif s.startswith("INSERT INTO JUTE_YARN_ISSUE_LI"):
            res.lastrowid = 99
        return res

    def _header_row(self):
        return {
            **self.doc,
            "dept_id": DEPT_ID,
            "dept_desc": "WINDING",
            "status_name": "Open" if self.doc["status_id"] == 1 else "Other",
            "remarks": None,
            "active": 1,
            "updated_by": USER_ID,
            "updated_date_time": datetime(2026, 9, 25, 10, 30, 0),
            "_decimal_probe": Decimal("1.5"),
        }

    def _line_row(self, li_id):
        return {
            "yarn_issue_li_id": li_id,
            "yarn_issue_id": self.doc["yarn_issue_id"],
            "item_id": ITEM_A,
            "item_name": "13 LBS SKWP",
            "item_code": "Y13SK",
            "yarn_purchase_li_id": 101,
            "purchase_no": 1,
            "lot_tran_date": date(2026, 9, 1),
            "lot_bal_kg": Decimal("500.000"),
            "no_of_packages": Decimal("20.000"),
            "weight_kg": Decimal("100.000"),
            "remarks": None,
            "active": 1,
            "updated_by": USER_ID,
            "updated_date_time": datetime(2026, 9, 25, 10, 30, 0),
        }

    # helpers for assertions
    def calls_starting(self, prefix):
        return [(s, p) for s, p in self.calls if s.startswith(prefix.upper())]

    def calls_containing(self, fragment):
        return [(s, p) for s, p in self.calls if fragment.upper() in s]


def _install(session):
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": USER_ID}
    app.dependency_overrides[get_tenant_db] = lambda: session
    return session


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _doc(status_id, approval_level=None, issue_no=None, tran_date=date(2026, 9, 25), issue_type="ISSUE"):
    return {
        "yarn_issue_id": 55,
        "co_id": CO_ID,
        "branch_id": BRANCH_ID,
        "status_id": status_id,
        "approval_level": approval_level,
        "tran_date": tran_date,
        "issue_no": issue_no,
        "issue_type": issue_type,
    }


def _body(**overrides):
    body = {
        "co_id": CO_ID,
        "branch_id": BRANCH_ID,
        "tran_date": "2026-09-25",
        "issue_type": "ISSUE",
        "dept_id": DEPT_ID,
        "remarks": "morning issue",
        "lines": [
            {"item_id": ITEM_A, "yarn_purchase_li_id": 101, "no_of_packages": 20, "weight_kg": 100, "remarks": None},
            {"item_id": ITEM_B, "yarn_purchase_li_id": 103, "weight_kg": 50},
        ],
    }
    body.update(overrides)
    return body


def _action(**overrides):
    payload = {"yarn_issue_id": 55, "branch_id": BRANCH_ID, "menu_id": MENU_ID}
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
    assert set(data) == {"menu_id", "departments", "issue_types", "yarn_items"}
    assert data["menu_id"] == MENU_ID
    assert data["issue_types"] == list(YARN_ISSUE_TYPES) == ["ISSUE", "RETURN"]
    assert data["departments"] == [
        {"dept_id": 12, "dept_desc": "WINDING", "dept_code": "WND"},
        {"dept_id": 13, "dept_desc": "WEAVING", "dept_code": "WVG"},
    ]
    assert data["yarn_items"][0] == {
        "item_id": ITEM_A, "item_code": "Y13SK", "item_name": "13 LBS SKWP",
        "quality_name": "13 LBS", "yarn_count": 13.0,
    }
    # menu resolved by the canonical path, departments scoped to the branch, items to the company
    assert [p for s, p in session.calls if "FROM MENU_MST" in s] == [{"menu_path": YARN_ISSUE_MENU_PATH}]
    dept_calls = session.calls_containing("FROM DEPT_MST D WHERE D.BRANCH_ID")
    assert [p for _, p in dept_calls] == [{"branch_id": BRANCH_ID}]
    assert "ORDER BY D.ORDER_ID, D.DEPT_DESC" in dept_calls[0][0]
    assert [p for s, p in session.calls if "FROM ITEM_MST IM" in s] == [{"co_id": CO_ID}]


def test_setup_requires_co_id_and_branch_id():
    _install(FakeSession())
    resp = client.get(f"{BASE}/setup")
    assert resp.status_code == 400 and resp.json()["detail"] == "co_id is required"
    resp = client.get(f"{BASE}/setup", params={"co_id": CO_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "branch_id is required"


# =============================================================================
# lot pickers
# =============================================================================


def test_godown_lots_fifo_order_shape_and_filters():
    session = _install(FakeSession())
    resp = client.get(f"{BASE}/godown_lots", params={"co_id": CO_ID, "branch_id": BRANCH_ID})
    assert resp.status_code == 200, resp.text
    lots = resp.json()["data"]
    # issuable (status 3), positive balance, this co/branch only, oldest first
    assert [l["yarn_purchase_li_id"] for l in lots] == [101, 102, 103]
    assert set(lots[0]) == {"yarn_purchase_li_id", "yarn_purchase_id", "purchase_no", "tran_date", "supp_name",
                            "item_id", "item_name", "item_code", "package_type", "received_kg", "bal_kg", "bal_pkgs"}
    assert lots[0]["purchase_no"] == 1 and lots[0]["tran_date"] == "2026-09-01"
    assert lots[0]["bal_kg"] == 500.0 and lots[0]["bal_pkgs"] == 100.0 and lots[0]["received_kg"] == 1000.0
    assert lots[0]["item_name"] == "13 LBS SKWP" and lots[0]["supp_name"] == "ABC Yarn Traders"
    s, p = session.calls_containing("FROM VW_YARN_GODOWN_OUTSTANDING V")[0]
    assert p == {"co_id": CO_ID, "branch_id": BRANCH_ID, "item_id": None, "exclude_issue_id": None}
    assert "ORDER BY V.TRAN_DATE ASC, V.YARN_PURCHASE_LI_ID ASC" in s
    assert "V.PURCHASE_STATUS_ID IN (3)" in s
    assert "> 0" in s
    # the own-lines add-back is keyed on the excluded document and skips draft/rejected/cancelled
    assert "I.YARN_ISSUE_ID = :EXCLUDE_ISSUE_ID" in s and "I.STATUS_ID NOT IN (4, 6, 21)" in s


def test_godown_lots_binds_item_and_exclude_issue_id():
    session = _install(FakeSession())
    resp = client.get(f"{BASE}/godown_lots", params={"co_id": CO_ID, "branch_id": BRANCH_ID,
                                                     "item_id": ITEM_B, "exclude_issue_id": 55})
    assert resp.status_code == 200, resp.text
    assert [l["yarn_purchase_li_id"] for l in resp.json()["data"]] == [103]
    _, p = session.calls_containing("FROM VW_YARN_GODOWN_OUTSTANDING V")[0]
    assert p["item_id"] == ITEM_B and p["exclude_issue_id"] == 55
    assert client.get(f"{BASE}/godown_lots", params={"co_id": CO_ID}).status_code == 400


def test_dept_lots_requires_dept_and_returns_floor_balance():
    session = _install(FakeSession(floor={102: 120.0, 101: 30.0, 103: 0.0}))
    resp = client.get(f"{BASE}/dept_lots", params={"co_id": CO_ID, "branch_id": BRANCH_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "dept_id is required"
    resp = client.get(f"{BASE}/dept_lots", params={"co_id": CO_ID, "branch_id": BRANCH_ID, "dept_id": DEPT_ID,
                                                   "exclude_issue_id": 55})
    assert resp.status_code == 200, resp.text
    lots = resp.json()["data"]
    assert [l["yarn_purchase_li_id"] for l in lots] == [101, 102]      # oldest first, zero floor dropped
    assert lots[0]["floor_bal_kg"] == 30.0 and lots[1]["floor_bal_kg"] == 120.0
    assert "floor_bal_pkgs" in lots[0] and "bal_kg" in lots[0]
    s, p = session.calls_containing("AS FLOOR_BAL_KG")[0]
    assert p == {"co_id": CO_ID, "branch_id": BRANCH_ID, "dept_id": DEPT_ID, "item_id": None, "exclude_issue_id": 55}
    assert "I.DEPT_ID = :DEPT_ID" in s and "I.YARN_ISSUE_ID <> :EXCLUDE_ISSUE_ID" in s
    assert "ORDER BY V.TRAN_DATE ASC, V.YARN_PURCHASE_LI_ID ASC" in s


# =============================================================================
# get / list
# =============================================================================


def test_get_returns_header_lines_and_permissions():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, issue_no=3), line_ids=(1, 2)))
    perms = {"canApprove": False, "canReject": False, "canOpen": False, "canSendForApproval": True,
             "canCancelDraft": False, "canReopen": False, "canViewApprovalLog": False,
             "canClone": False, "canSave": True}
    with patch("src.juteProcurement.yarn_issue.calculate_approval_permissions", return_value=perms) as cap:
        resp = client.get(f"{BASE}/get/55")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data) == {"header", "lines", "permissions"}
    h = data["header"]
    assert h["yarn_issue_id"] == 55 and h["issue_no"] == 3 and h["issue_type"] == "ISSUE"
    assert h["dept_id"] == DEPT_ID and h["dept_desc"] == "WINDING" and h["status_name"] == "Open"
    assert h["tran_date"] == "2026-09-25"
    assert h["_decimal_probe"] == 1.5                       # Decimals become floats
    assert len(data["lines"]) == 2
    l1 = data["lines"][0]
    assert l1["yarn_issue_li_id"] == 1 and l1["item_name"] == "13 LBS SKWP" and l1["item_code"] == "Y13SK"
    assert l1["yarn_purchase_li_id"] == 101 and l1["purchase_no"] == 1
    assert l1["lot_tran_date"] == "2026-09-01" and l1["lot_bal_kg"] == 500.0
    assert l1["weight_kg"] == 100.0 and l1["no_of_packages"] == 20.0
    assert data["permissions"] == perms
    args = cap.call_args.args
    assert args[:5] == (USER_ID, MENU_ID, BRANCH_ID, YARN_DOC_STATUS_OPEN, None)
    assert args[5] is session


def test_get_404_when_missing():
    _install(FakeSession(doc=None))
    resp = client.get(f"{BASE}/get/55")
    assert resp.status_code == 404


def test_list_binds_filters_and_returns_rows():
    session = _install(FakeSession())
    resp = client.get(f"{BASE}/list", params={"co_id": CO_ID, "branch_id": BRANCH_ID, "from_date": "2026-04-01",
                                              "to_date": "2026-09-30", "status_id": 1, "issue_type": "return"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": []}
    s, p = session.calls_starting("SELECT H.YARN_ISSUE_ID, H.ISSUE_NO")[0]
    assert p == {"co_id": CO_ID, "branch_id": BRANCH_ID, "from_date": date(2026, 4, 1),
                 "to_date": date(2026, 9, 30), "status_id": 1, "issue_type": "RETURN"}
    assert "ORDER BY H.TRAN_DATE DESC, H.YARN_ISSUE_ID DESC" in s
    assert "JOIN STATUS_MST" in s and "JOIN DEPT_MST" in s


def test_list_optional_filters_and_bad_issue_type():
    session = _install(FakeSession())
    assert client.get(f"{BASE}/list", params={"co_id": CO_ID, "branch_id": BRANCH_ID}).status_code == 200
    assert session.calls[-1][1]["status_id"] is None and session.calls[-1][1]["issue_type"] is None
    resp = client.get(f"{BASE}/list", params={"co_id": CO_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "branch_id is required"
    resp = client.get(f"{BASE}/list", params={"co_id": CO_ID, "branch_id": BRANCH_ID, "issue_type": "TRANSFER"})
    assert resp.status_code == 400 and resp.json()["detail"] == "issue_type must be one of ISSUE, RETURN"


# =============================================================================
# create
# =============================================================================


def test_create_issue_happy_path_inserts_header_draft_and_lines():
    session = _install(FakeSession(next_lastrowid=4321))
    resp = client.post(f"{BASE}/create", json=_body())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": {"yarn_issue_id": 4321, "status_id": YARN_DOC_STATUS_DRAFT, "warnings": []}}

    hdr = session.calls_starting("INSERT INTO JUTE_YARN_ISSUE (")
    assert len(hdr) == 1
    hp = hdr[0][1]
    assert hp["co_id"] == CO_ID and hp["branch_id"] == BRANCH_ID
    assert hp["tran_date"] == date(2026, 9, 25)
    assert hp["issue_type"] == "ISSUE" and hp["dept_id"] == DEPT_ID
    assert hp["status_id"] == YARN_DOC_STATUS_DRAFT and hp["updated_by"] == USER_ID

    lines = session.calls_starting("INSERT INTO JUTE_YARN_ISSUE_LI")
    assert len(lines) == 2
    l1, l2 = lines[0][1], lines[1][1]
    assert l1["yarn_issue_id"] == 4321 and l2["yarn_issue_id"] == 4321
    assert l1["item_id"] == ITEM_A and l1["yarn_purchase_li_id"] == 101 and l1["weight_kg"] == 100
    assert l1["no_of_packages"] == 20 and l2["no_of_packages"] is None
    assert l2["item_id"] == ITEM_B and l2["yarn_purchase_li_id"] == 103
    # department verified against the branch; lots looked up with no exclusion (new document)
    assert [p for _, p in session.calls_containing("FROM DEPT_MST D WHERE D.DEPT_ID")] == [{"dept_id": DEPT_ID, "branch_id": BRANCH_ID}]
    lot_calls = [p for _, p in session.calls_containing("WHERE V.YARN_PURCHASE_LI_ID = :YARN_PURCHASE_LI_ID")]
    assert lot_calls == [{"yarn_purchase_li_id": 101, "exclude_issue_id": None},
                         {"yarn_purchase_li_id": 103, "exclude_issue_id": None}]
    for s, p in session.calls:
        assert "UOM" not in s and "uom" not in p
    session.commit.assert_called_once()
    session.rollback.assert_not_called()


def test_create_return_happy_path_within_floor_balance():
    session = _install(FakeSession(floor={101: 150.0, 103: 60.0}))
    resp = client.post(f"{BASE}/create", json=_body(issue_type="return"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["warnings"] == []
    assert session.calls_starting("INSERT INTO JUTE_YARN_ISSUE (")[0][1]["issue_type"] == "RETURN"
    # floor balance read for the header's department, no godown-balance block on a return
    _, p = session.calls_containing("AS FLOOR_BAL_KG")[0]
    assert p == {"co_id": CO_ID, "branch_id": BRANCH_ID, "dept_id": DEPT_ID, "item_id": None, "exclude_issue_id": None}
    assert not session.calls_containing(FIFO_SQL)
    session.commit.assert_called_once()


@pytest.mark.parametrize(
    "overrides, detail",
    [
        ({"co_id": None}, "co_id is required"),
        ({"branch_id": ""}, "branch_id is required"),
        ({"tran_date": None}, "tran_date is required"),
        ({"tran_date": "25/09/2026"}, "Invalid tran_date (expected YYYY-MM-DD)"),
        ({"issue_type": None}, "issue_type is required"),
        ({"issue_type": "TRANSFER"}, "issue_type must be one of ISSUE, RETURN"),
        ({"dept_id": None}, "dept_id is required"),
        ({"dept_id": 99}, "dept_id 99 is not a department of branch 29"),
        ({"lines": []}, "At least one line is required"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 0}]},
         "weight_kg must be greater than 0 on line 1"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": -5}]},
         "weight_kg must be greater than 0 on line 1"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 101}]}, "weight_kg is required on line 1"),
        ({"lines": [{"yarn_purchase_li_id": 101, "weight_kg": 10}]}, "item_id is required on line 1"),
        ({"lines": [{"item_id": ITEM_A, "weight_kg": 10}]}, "yarn_purchase_li_id (lot) is required on line 1"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 0, "weight_kg": 10}]},
         "yarn_purchase_li_id (lot) is required on line 1"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 10, "no_of_packages": -1}]},
         "no_of_packages cannot be negative on line 1"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 999, "weight_kg": 10}]},
         "yarn_purchase_li_id 999 on line 1 is not a godown lot of an active Yarn Purchase"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 106, "weight_kg": 10}]},
         "Lot PUR-6 on line 1 belongs to company 3 / branch 40, not company 2 / branch 29"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 10},
                    {"item_id": ITEM_A, "yarn_purchase_li_id": 103, "weight_kg": 10}]},
         "Lot PUR-3 on line 2 is 10 LBS SKWP (item_id 335557), not item_id 335556"),
        ({"lines": [{"item_id": ITEM_A, "yarn_purchase_li_id": 104, "weight_kg": 10}]},
         "Lot PUR-4 cannot be issued: its Yarn Purchase has status_id 20 (expected 3)"),
    ],
)
def test_create_validation_failures(overrides, detail):
    session = _install(FakeSession())
    resp = client.post(f"{BASE}/create", json=_body(**overrides))
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == detail
    assert not session.calls_starting("INSERT INTO")
    session.commit.assert_not_called()


def test_create_issue_over_godown_balance_is_hard_block():
    """Two lines on lot 101 (bal 500) totalling 600 kg: refused, nothing written,
    the message names the lot and its balance."""
    session = _install(FakeSession())
    body = _body(lines=[
        {"item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 350},
        {"item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 250},
    ])
    resp = client.post(f"{BASE}/create", json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Issue of 600 kg on lot PUR-1 (13 LBS SKWP) exceeds the godown balance of 500 kg"
    assert not session.calls_starting("INSERT INTO")
    session.commit.assert_not_called()


def test_create_issue_exactly_the_balance_is_allowed():
    session = _install(FakeSession())
    resp = client.post(f"{BASE}/create", json=_body(lines=[{"item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 500}]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["warnings"] == []


def test_create_issue_from_exhausted_lot_names_zero_balance():
    session = _install(FakeSession())
    resp = client.post(f"{BASE}/create", json=_body(lines=[{"item_id": ITEM_A, "yarn_purchase_li_id": 105, "weight_kg": 1}]))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Issue of 1 kg on lot PUR-5 (13 LBS SKWP) exceeds the godown balance of 0 kg"
    session.commit.assert_not_called()


def test_create_return_over_floor_balance_is_warning_not_block():
    session = _install(FakeSession(floor={101: 40.0}))       # dept holds 40 kg of lot 101, nothing of lot 103
    resp = client.post(f"{BASE}/create", json=_body(issue_type="RETURN"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["yarn_issue_id"] == 4321 and data["status_id"] == YARN_DOC_STATUS_DRAFT
    assert data["warnings"] == [
        "Return of 100 kg on lot PUR-1 exceeds the department's floor balance of 40 kg",
        "Return of 50 kg on lot PUR-3 exceeds the department's floor balance of 0 kg",
    ]
    assert len(session.calls_starting("INSERT INTO JUTE_YARN_ISSUE_LI")) == 2
    session.commit.assert_called_once()


def test_create_issue_fifo_skip_is_warning_not_block():
    """Drawing item A from lot 102 while the older lot 101 still has 500 kg."""
    session = _install(FakeSession())
    resp = client.post(f"{BASE}/create", json=_body(lines=[{"item_id": ITEM_A, "yarn_purchase_li_id": 102, "weight_kg": 10}]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["warnings"] == ["Older lot PUR-1 (2026-09-01, 500 kg) skipped for 13 LBS SKWP"]
    # FIFO check reads the item's godown lots, oldest first, with no exclusion on create
    fifo = session.calls_containing(FIFO_SQL)
    assert [p for _, p in fifo] == [{"co_id": CO_ID, "branch_id": BRANCH_ID, "item_id": ITEM_A, "exclude_issue_id": None}]
    session.commit.assert_called_once()


def test_create_issue_no_fifo_warning_when_oldest_lot_drawn():
    """Drawing from lots 101 and 102 together: nothing older than 102 is skipped.
    Lots 104 (not issuable) and 105 (exhausted) never count as skipped."""
    session = _install(FakeSession())
    resp = client.post(f"{BASE}/create", json=_body(lines=[
        {"item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 500},
        {"item_id": ITEM_A, "yarn_purchase_li_id": 102, "weight_kg": 10},
    ]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["warnings"] == []


def test_create_return_does_not_fifo_check():
    session = _install(FakeSession(floor={102: 500.0}))
    resp = client.post(f"{BASE}/create", json=_body(issue_type="RETURN",
                                                    lines=[{"item_id": ITEM_A, "yarn_purchase_li_id": 102, "weight_kg": 10}]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["warnings"] == []


# =============================================================================
# update
# =============================================================================


def test_update_refused_when_pending_approval():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_PENDING, approval_level=1)))
    resp = client.put(f"{BASE}/update/55", json=_body())
    assert resp.status_code == 409, resp.text
    assert "status_id 20 cannot be updated" in resp.json()["detail"]
    assert not session.calls_starting("UPDATE JUTE_YARN_ISSUE")
    assert not session.calls_starting("INSERT INTO")
    session.commit.assert_not_called()


def test_update_replaces_lines_and_adds_own_lines_back_into_balance():
    """Open doc 55 already holds 300 kg of lot 101 (view bal 500 = 800 - 300).
    Re-saving with 700 kg on lot 101 must pass: the guard sees 500 + 300."""
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, issue_no=3), line_ids=(1, 2, 3), own={101: 300.0}))
    body = _body(lines=[
        {"yarn_issue_li_id": 1, "item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 700},
        {"item_id": ITEM_B, "yarn_purchase_li_id": 103, "weight_kg": 40},
    ])
    resp = client.put(f"{BASE}/update/55", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": {"yarn_issue_id": 55, "status_id": YARN_DOC_STATUS_OPEN, "warnings": []}}

    lot_calls = [p for _, p in session.calls_containing("WHERE V.YARN_PURCHASE_LI_ID = :YARN_PURCHASE_LI_ID")]
    assert lot_calls == [{"yarn_purchase_li_id": 101, "exclude_issue_id": 55},
                         {"yarn_purchase_li_id": 103, "exclude_issue_id": 55}]
    fifo = [p for _, p in session.calls_containing(FIFO_SQL)]
    assert all(p["exclude_issue_id"] == 55 for p in fifo) and len(fifo) == 2

    hdr = session.calls_starting("UPDATE JUTE_YARN_ISSUE SET TRAN_DATE")
    assert len(hdr) == 1 and hdr[0][1]["yarn_issue_id"] == 55
    assert hdr[0][1]["issue_type"] == "ISSUE" and hdr[0][1]["dept_id"] == DEPT_ID
    assert "STATUS_ID" not in hdr[0][0] and "ISSUE_NO" not in hdr[0][0]

    deleted = session.calls_starting("UPDATE JUTE_YARN_ISSUE_LI SET ACTIVE = 0")
    assert sorted(p["yarn_issue_li_id"] for _, p in deleted) == [2, 3]
    updated = session.calls_starting("UPDATE JUTE_YARN_ISSUE_LI SET ITEM_ID")
    assert [p["yarn_issue_li_id"] for _, p in updated] == [1]
    assert updated[0][1]["weight_kg"] == 700 and updated[0][1]["yarn_purchase_li_id"] == 101
    inserted = session.calls_starting("INSERT INTO JUTE_YARN_ISSUE_LI")
    assert len(inserted) == 1 and inserted[0][1]["item_id"] == ITEM_B
    session.commit.assert_called_once()
    session.rollback.assert_not_called()


def test_update_over_balance_still_blocks_with_own_lines_added_back():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, issue_no=3), line_ids=(1,), own={101: 300.0}))
    body = _body(lines=[{"yarn_issue_li_id": 1, "item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 801}])
    resp = client.put(f"{BASE}/update/55", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Issue of 801 kg on lot PUR-1 (13 LBS SKWP) exceeds the godown balance of 800 kg"
    assert not session.calls_starting("UPDATE JUTE_YARN_ISSUE SET TRAN_DATE")
    session.commit.assert_not_called()


def test_update_rejects_foreign_line_id():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT), line_ids=(1,)))
    resp = client.put(f"{BASE}/update/55", json=_body(lines=[{"yarn_issue_li_id": 77, "item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 5}]))
    assert resp.status_code == 400
    assert "77" in resp.json()["detail"]
    session.commit.assert_not_called()


def test_update_rejects_type_change_on_numbered_document():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, issue_no=3, issue_type="ISSUE"), line_ids=(1,)))
    resp = client.put(f"{BASE}/update/55", json=_body(issue_type="RETURN"))
    assert resp.status_code == 400
    assert "issue_type cannot be changed" in resp.json()["detail"]
    session.commit.assert_not_called()


def test_update_404_when_missing():
    _install(FakeSession(doc=None))
    resp = client.put(f"{BASE}/update/55", json=_body())
    assert resp.status_code == 404


# =============================================================================
# open
# =============================================================================


@pytest.mark.parametrize("issue_type", ["ISSUE", "RETURN"])
def test_open_assigns_issue_no_from_fy_helper_per_type(issue_type):
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT, issue_no=None, tran_date=date(2026, 9, 25), issue_type=issue_type)))
    with patch("src.juteProcurement.yarn_issue.get_next_yarn_issue_no_for_fy", return_value=42) as helper:
        resp = client.post(f"{BASE}/open", json=_action())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == {"yarn_issue_id": 55, "status_id": YARN_DOC_STATUS_OPEN, "issue_no": 42}
    helper.assert_called_once()
    _, args_branch, args_date, args_type = helper.call_args.args
    assert (args_branch, args_date, args_type) == (BRANCH_ID, date(2026, 9, 25), issue_type)

    upd = session.calls_starting("UPDATE JUTE_YARN_ISSUE SET STATUS_ID")
    assert len(upd) == 1
    p = upd[0][1]
    assert p["status_id"] == YARN_DOC_STATUS_OPEN and p["approval_level"] is None
    assert p["issue_no"] == 42 and p["updated_by"] == USER_ID
    session.commit.assert_called_once()


def test_open_keeps_existing_issue_no():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT, issue_no=9)))
    with patch("src.juteProcurement.yarn_issue.get_next_yarn_issue_no_for_fy") as helper:
        resp = client.post(f"{BASE}/open", json=_action())
    assert resp.status_code == 200
    helper.assert_not_called()
    assert session.calls_starting("UPDATE JUTE_YARN_ISSUE SET STATUS_ID")[0][1]["issue_no"] == 9


def test_open_refused_from_wrong_status():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN)))
    resp = client.post(f"{BASE}/open", json=_action())
    assert resp.status_code == 400
    assert "Expected status 21" in resp.json()["detail"]
    session.commit.assert_not_called()


def test_fy_helper_uses_april_to_march_year_and_issue_type():
    session = FakeSession()
    assert get_next_yarn_issue_no_for_fy(session, BRANCH_ID, date(2026, 2, 10), "RETURN") == 7
    s, p = session.calls[-1]
    assert p == {"branch_id": BRANCH_ID, "issue_type": "RETURN", "fy_string": "25-26"}   # Feb 2026 is FY 25-26
    assert "FROM JUTE_YARN_ISSUE" in s and "ISSUE_NO IS NOT NULL" in s and "MONTH(TRAN_DATE)" in s
    assert "ISSUE_TYPE = :ISSUE_TYPE" in s
    get_next_yarn_issue_no_for_fy(session, BRANCH_ID, date(2026, 4, 1), "ISSUE")
    assert session.calls[-1][1]["fy_string"] == "26-27" and session.calls[-1][1]["issue_type"] == "ISSUE"


# =============================================================================
# cancel / send_for_approval / reopen
# =============================================================================


def test_cancel_draft_to_cancelled():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT)))
    resp = client.post(f"{BASE}/cancel", json=_action())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status_id"] == YARN_DOC_STATUS_CANCELLED
    p = session.calls_starting("UPDATE JUTE_YARN_ISSUE SET STATUS_ID")[0][1]
    assert p["status_id"] == YARN_DOC_STATUS_CANCELLED and p["issue_no"] is None


def test_send_for_approval_refuses_empty_document():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, issue_no=3), totals={"line_count": 0, "min_kg": None}))
    resp = client.post(f"{BASE}/send_for_approval", json=_action())
    assert resp.status_code == 400, resp.text
    assert "has no lines" in resp.json()["detail"]
    assert not session.calls_starting("UPDATE JUTE_YARN_ISSUE SET STATUS_ID")
    session.commit.assert_not_called()


def test_send_for_approval_refuses_zero_weight_line():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, issue_no=3), totals={"line_count": 2, "min_kg": 0}))
    resp = client.post(f"{BASE}/send_for_approval", json=_action())
    assert resp.status_code == 400
    assert "weight_kg greater than 0" in resp.json()["detail"]
    session.commit.assert_not_called()


def test_send_for_approval_open_to_pending_level_1():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, issue_no=3), totals={"line_count": 2, "min_kg": 40}))
    resp = client.post(f"{BASE}/send_for_approval", json=_action())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"yarn_issue_id": 55, "status_id": YARN_DOC_STATUS_PENDING, "approval_level": 1}
    p = session.calls_starting("UPDATE JUTE_YARN_ISSUE SET STATUS_ID")[0][1]
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
    session = _install(FakeSession(doc=_doc(from_status, approval_level=1, issue_no=3)))
    resp = client.post(f"{BASE}/reopen", json=_action())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"yarn_issue_id": 55, "status_id": to_status, "approval_level": None}
    p = session.calls_starting("UPDATE JUTE_YARN_ISSUE SET STATUS_ID")[0][1]
    assert p["status_id"] == to_status and p["approval_level"] is None and p["issue_no"] is None
    session.commit.assert_called_once()


def test_reopen_refused_from_approved():
    _install(FakeSession(doc=_doc(3)))
    resp = client.post(f"{BASE}/reopen", json=_action())
    assert resp.status_code == 400
    assert "Only Cancelled (6) or Rejected (4)" in resp.json()["detail"]


def test_action_payload_requires_ids():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_DRAFT)))
    resp = client.post(f"{BASE}/open", json={"branch_id": BRANCH_ID, "menu_id": MENU_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "yarn_issue_id is required"
    resp = client.post(f"{BASE}/open", json={"yarn_issue_id": 55, "branch_id": BRANCH_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "menu_id is required"


# =============================================================================
# approve / reject go through the shared engine
# =============================================================================


def test_approve_delegates_to_process_approval():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_PENDING, approval_level=1)))
    with patch("src.juteProcurement.yarn_issue.process_approval",
               return_value={"status": "success", "new_status_id": 3, "new_approval_level": 1, "message": "ok"}) as pa:
        resp = client.post(f"{BASE}/approve", json=_action())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"yarn_issue_id": 55, "status_id": 3, "approval_level": 1}
    kw = pa.call_args.kwargs
    assert kw["doc_id"] == 55 and kw["user_id"] == USER_ID and kw["menu_id"] == MENU_ID
    assert kw["id_param_name"] == "yarn_issue_id" and kw["doc_name"] == "Yarn Issue"
    assert kw["extra_update_params"] == {"issue_no": None}
    doc_sql = _norm(kw["get_doc_fn"]())
    assert "H.STATUS_ID" in doc_sql and "H.APPROVAL_LEVEL" in doc_sql and "H.BRANCH_ID" in doc_sql
    binds = set(kw["update_status_fn"]().compile().binds)
    assert binds == {"yarn_issue_id", "status_id", "approval_level", "updated_by", "updated_date_time", "issue_no"}


def test_reject_delegates_to_process_rejection():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_PENDING, approval_level=1)))
    with patch("src.juteProcurement.yarn_issue.process_rejection",
               return_value={"status": "success", "new_status_id": 4, "new_approval_level": None, "message": "ok"}) as pr:
        resp = client.post(f"{BASE}/reject", json={"yarn_issue_id": 55, "co_id": CO_ID, "menu_id": MENU_ID, "reason": "wrong dept"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status_id"] == YARN_DOC_STATUS_REJECTED
    kw = pr.call_args.kwargs
    assert kw["doc_id"] == 55 and kw["menu_id"] == MENU_ID and kw["reason"] == "wrong dept"
    assert kw["id_param_name"] == "yarn_issue_id" and kw["doc_name"] == "Yarn Issue"
    assert kw["extra_update_params"] == {"issue_no": None}


def test_reject_requires_co_id():
    _install(FakeSession(doc=_doc(YARN_DOC_STATUS_PENDING, approval_level=1)))
    resp = client.post(f"{BASE}/reject", json={"yarn_issue_id": 55, "menu_id": MENU_ID})
    assert resp.status_code == 400 and resp.json()["detail"] == "co_id is required"


# =============================================================================
# every statement binds what it declares
# =============================================================================


def test_all_executed_statements_have_complete_binds():
    session = _install(FakeSession(doc=_doc(YARN_DOC_STATUS_OPEN, issue_no=3), line_ids=(1,),
                                   totals={"line_count": 1, "min_kg": 5}, floor={101: 50.0}))
    client.get(f"{BASE}/setup", params={"co_id": CO_ID, "branch_id": BRANCH_ID})
    client.get(f"{BASE}/godown_lots", params={"co_id": CO_ID, "branch_id": BRANCH_ID})
    client.get(f"{BASE}/dept_lots", params={"co_id": CO_ID, "branch_id": BRANCH_ID, "dept_id": DEPT_ID})
    client.post(f"{BASE}/create", json=_body())
    client.post(f"{BASE}/create", json=_body(issue_type="RETURN"))
    client.put(f"{BASE}/update/55", json=_body(lines=[{"yarn_issue_li_id": 1, "item_id": ITEM_A, "yarn_purchase_li_id": 101, "weight_kg": 5},
                                                      {"item_id": ITEM_B, "yarn_purchase_li_id": 103, "weight_kg": 6}]))
    client.get(f"{BASE}/list", params={"co_id": CO_ID, "branch_id": BRANCH_ID, "from_date": "2026-04-01", "to_date": "2026-09-30"})
    client.get(f"{BASE}/get/55")
    client.post(f"{BASE}/send_for_approval", json=_action())
    session.doc = _doc(YARN_DOC_STATUS_DRAFT)
    client.post(f"{BASE}/open", json=_action())
    from src.juteProcurement import yarn_issue_query as q
    stmts = {
        name: getattr(q, name)()
        for name in dir(q)
        if name.endswith("_query") or name in ("update_yarn_issue_status", "get_yarn_issue_with_approval_info")
    }
    seen = 0
    for s, p in session.calls:
        for name, stmt in stmts.items():
            if _norm(stmt) == s:
                missing = set(stmt.compile().binds) - set(p)
                assert not missing, (name, missing)
                seen += 1
                break
    assert seen >= 20
