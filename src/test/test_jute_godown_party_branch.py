"""
Godown (warehouse) + party branch, as written by the QC page and the MR page.

Covers the rules this feature depends on:
- the line's `warehouse_id` reaches jute_mr_li, on both the UPDATE and INSERT paths
- a godown can be CLEARED (null must persist, not silently keep the old value)
- party and party_branch are written as a pair — a new party never keeps the old
  party's branch attached
- ids that don't belong to the record are rejected before anything is written
  (branch not of that party; godown not of that branch)

Plus the MR-side rule that a caller omitting party_id must not blank the party.
SQL-dispatching mock session throughout; no DB required.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)

COMPLETE_URL = "/api/juteMaterialInspection/complete_inspection?co_id=1"
MR_UPDATE_URL = "/api/juteMR/update_mr/100?co_id=1"


def _make_execute(party_branch_ok=True, warehouse_ok=True):
    """Record every (normalized SQL, params); route the two ownership guards."""
    calls = []
    hdr = {
        "qc_check": 0,
        "jute_mr_id": 100,
        "status_id": 1,
        "party_id": "7",
        "branch_id": 2,
    }

    def _exec(sql, params=None):
        s = " ".join(str(sql).split()).upper()
        calls.append((s, params or {}))
        res = MagicMock()

        row = MagicMock()
        row._mapping = hdr
        res.fetchone.return_value = row
        res.fetchall.return_value = []

        if s.startswith("SELECT 1 FROM PARTY_BRANCH_MST"):
            res.scalar.return_value = 1 if party_branch_ok else None
        elif s.startswith("SELECT 1 FROM WAREHOUSE_MST"):
            res.scalar.return_value = 1 if warehouse_ok else None
        else:
            res.scalar.return_value = None

        res.lastrowid = 999
        return res

    _exec.calls = calls
    return _exec


def _session(**kw):
    session = MagicMock()
    exec_fn = _make_execute(**kw)
    session.execute.side_effect = exec_fn
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: session
    return exec_fn


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _header_updates(calls):
    return [c for c in calls if c[0].startswith("UPDATE JUTE_MR SET")]


def _li_updates(calls):
    return [
        c for c in calls
        if c[0].startswith("UPDATE JUTE_MR_LI") and "SET ACTIVE = 0" not in c[0]
    ]


def _li_inserts(calls):
    return [c for c in calls if c[0].startswith("INSERT INTO JUTE_MR_LI")]


def _qc_body(**over):
    body = {
        "jute_mr_id": 100,
        "save_only": True,
        "line_items": [{
            "jute_mr_li_id": 501,
            "actual_item_id": 7,
            "actual_qty": 10,
            "actual_weight": 100,
            "warehouse_id": 33,
        }],
    }
    body.update(over)
    return body


# --- godown reaches the line, on both write paths ---------------------------

def test_qc_save_binds_line_godown():
    exec_fn = _session()
    resp = client.post(COMPLETE_URL, json=_qc_body())
    assert resp.status_code == 200, resp.text

    updates = _li_updates(exec_fn.calls)
    assert updates, "the QC line must be written"
    assert any(c[1].get("warehouse_id") == 33 for c in updates), \
        "godown chosen on the QC page must reach jute_mr_li.warehouse_id"


def test_qc_insert_path_carries_godown():
    """A fresh (PO-filled) line still carries its godown on INSERT."""
    exec_fn = _session()
    resp = client.post(COMPLETE_URL, json=_qc_body(line_items=[{
        "jute_mr_li_id": 0,
        "jute_po_li_id": None,
        "actual_item_id": 7,
        "actual_qty": 10,
        "warehouse_id": 33,
    }]))
    assert resp.status_code == 200, resp.text

    inserts = _li_inserts(exec_fn.calls)
    assert inserts, "a new line INSERTs"
    assert inserts[0][1].get("warehouse_id") == 33


def test_qc_can_clear_a_godown():
    """null must persist — COALESCE here would silently keep the old godown."""
    exec_fn = _session()
    resp = client.post(COMPLETE_URL, json=_qc_body(line_items=[{
        "jute_mr_li_id": 501,
        "actual_item_id": 7,
        "actual_qty": 10,
        "warehouse_id": None,
    }]))
    assert resp.status_code == 200, resp.text

    updates = _li_updates(exec_fn.calls)
    assert updates
    sql, params = updates[0]
    assert "WAREHOUSE_ID = :WAREHOUSE_ID" in sql, \
        "godown must bind directly so a cleared value persists"
    assert params.get("warehouse_id") is None


# --- party / party_branch move together ------------------------------------

def test_qc_save_binds_header_party_branch():
    exec_fn = _session()
    resp = client.post(COMPLETE_URL, json=_qc_body(party_id="7", party_branch_id=55))
    assert resp.status_code == 200, resp.text

    hdr = _header_updates(exec_fn.calls)
    assert hdr
    assert any(
        "PARTY_BRANCH_ID = :PARTY_BRANCH_ID" in c[0] and c[1].get("party_branch_id") == 55
        for c in hdr
    )


def test_changing_party_clears_a_branch_the_new_party_does_not_own():
    """party_id sent + party_branch_id null -> the branch column must be nulled,
    never left pointing at the previous party's branch."""
    exec_fn = _session()
    resp = client.post(COMPLETE_URL, json=_qc_body(party_id="9"))
    assert resp.status_code == 200, resp.text

    hdr = _header_updates(exec_fn.calls)
    assert hdr
    assert any(
        "PARTY_ID = :PARTY_ID" in c[0]
        and "PARTY_BRANCH_ID = :PARTY_BRANCH_ID" in c[0]
        and c[1].get("party_branch_id") is None
        for c in hdr
    ), "party and party_branch must be written atomically"


# --- ownership guards -------------------------------------------------------

def test_party_branch_of_another_party_is_rejected():
    exec_fn = _session(party_branch_ok=False)
    resp = client.post(COMPLETE_URL, json=_qc_body(party_id="7", party_branch_id=999))
    assert resp.status_code == 400
    assert "party branch" in resp.json()["detail"].lower()
    assert not _header_updates(exec_fn.calls), "must reject before writing anything"


def test_godown_of_another_branch_is_rejected():
    exec_fn = _session(warehouse_ok=False)
    resp = client.post(COMPLETE_URL, json=_qc_body())
    assert resp.status_code == 400
    assert "godown" in resp.json()["detail"].lower()
    assert not _li_updates(exec_fn.calls), "must reject before writing anything"


def test_mr_update_rejects_cross_branch_godown_too():
    """Same rule on the MR page — a guard on one writer only leaves the other open."""
    _session(warehouse_ok=False)
    resp = client.put(MR_UPDATE_URL, json={
        "mr_weight": 100,
        "line_items": [{"jute_mr_li_id": 501, "actual_weight": 100, "warehouse_id": 33}],
    })
    assert resp.status_code == 400
    assert "godown" in resp.json()["detail"].lower()


# --- MR party write ---------------------------------------------------------

def test_mr_update_writes_party_when_sent():
    exec_fn = _session()
    resp = client.put(MR_UPDATE_URL, json={
        "mr_weight": 100,
        "party_id": "7",
        "party_branch_id": 55,
        "line_items": [],
    })
    assert resp.status_code == 200, resp.text

    hdr = _header_updates(exec_fn.calls)
    assert hdr
    assert any("PARTY_ID = :PARTY_ID" in c[0] and c[1].get("party_id") == "7" for c in hdr)


def test_mr_update_omitting_party_leaves_it_untouched():
    exec_fn = _session()
    resp = client.put(MR_UPDATE_URL, json={
        "mr_weight": 100,
        "party_branch_id": 55,
        "line_items": [],
    })
    assert resp.status_code == 200, resp.text

    hdr = _header_updates(exec_fn.calls)
    assert hdr
    assert not any("PARTY_ID = :PARTY_ID" in c[0] for c in hdr), \
        "a caller that omits party_id must not blank the stored party"


def test_mr_update_coalesces_a_null_claim_rate():
    """jute_mr_li.claim_rate is NOT NULL DEFAULT 0.

    Clearing the claim cell in the MR grid sends null, which used to surface as a
    raw pymysql IntegrityError (SQL + every bind value) in a toast to the user.
    Blank means "no claim", so the statement must coalesce it to 0.
    """
    exec_fn = _session()
    resp = client.put(MR_UPDATE_URL, json={
        "mr_weight": 100,
        "line_items": [{"jute_mr_li_id": 501, "actual_weight": 100, "claim_rate": None}],
    })
    assert resp.status_code == 200, resp.text

    li = _li_updates(exec_fn.calls)
    assert li
    assert any("COALESCE(:CLAIM_RATE, 0)" in c[0] for c in li), \
        "a null claim_rate must be coalesced, not written straight into a NOT NULL column"
