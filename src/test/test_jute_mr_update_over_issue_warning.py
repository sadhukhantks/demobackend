"""
TKT-000196 — saving an MR whose lines now hold less weight than the floor has
already issued from them must WARN, not silently create negative stock.

sls MR 28256 (branch 29, gate entry 46): draft issues of 3,600 / 5,440 kg were
drawn on 21 Sep against lines that were then re-saved at 1,121 / 1,693 kg on
24 Sep. The save answered "MR updated successfully" and the Jute Stock report
read -2,479 / -3,747 kg for D TD-5 / D TD-6 the next day.

Rules pinned here:
- update_mr returns `warning` naming each over-issued line (issued vs received);
- the save is NOT refused — every line and header write still happens
  (owner ruling 2026-09-03: correct then 'Sync issued weights', never block);
- `warning` is null when nothing is over-issued;
- the check runs inside the transaction, after the line writes and before commit.

SQL-dispatching mock session, same shape as test_jute_godown_party_branch.py;
nothing here touches a database.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)

MR_UPDATE_URL = "/api/juteMR/update_mr/28256?co_id=2"


def _over_issued_row(li_id, item_name, actual_weight, issued):
    row = MagicMock()
    row._mapping = {
        "jute_mr_li_id": li_id,
        "item_name": item_name,
        "actual_weight": actual_weight,
        "issued": issued,
    }
    return row


def _make_execute(over_issued_rows):
    """Record every (normalized SQL, params); serve the over-issue probe."""
    calls = []
    hdr = {"qc_check": 1, "jute_mr_id": 28256, "status_id": 1, "party_id": "9500", "branch_id": 29}

    def _exec(sql, params=None):
        s = " ".join(str(sql).split()).upper()
        calls.append((s, params or {}))
        res = MagicMock()
        row = MagicMock()
        row._mapping = hdr
        res.fetchone.return_value = row
        res.fetchall.return_value = []
        if s.startswith("SELECT 1 FROM PARTY_BRANCH_MST") or s.startswith("SELECT 1 FROM WAREHOUSE_MST"):
            res.scalar.return_value = 1
        else:
            res.scalar.return_value = None
        if "HAVING COALESCE(SUM(JI.WEIGHT), 0) > COALESCE(LI.ACTUAL_WEIGHT, 0)" in s:
            res.fetchall.return_value = over_issued_rows
        return res

    _exec.calls = calls
    return _exec


def _session(over_issued_rows):
    session = MagicMock()
    exec_fn = _make_execute(over_issued_rows)
    session.execute.side_effect = exec_fn
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 24}
    app.dependency_overrides[get_tenant_db] = lambda: session
    return session, exec_fn


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _payload():
    return {
        "mr_weight": 2814,
        "party_id": "9500",
        "party_branch_id": 902,
        "remarks": None,
        "line_items": [
            {"jute_mr_li_id": 45727, "actual_item_grp_id": 1709, "actual_item_id": 56,
             "actual_qty": 90, "actual_weight": 1121, "allowable_moisture": 20,
             "actual_moisture": "20.00", "claim_dust": None, "shortage_kgs": 0,
             "accepted_weight": 1121, "rate": 12500, "claim_rate": 0, "claim_quality": None,
             "water_damage_amount": 0, "premium_amount": 0, "remarks": None, "warehouse_id": 53},
            {"jute_mr_li_id": 45728, "actual_item_grp_id": 1709, "actual_item_id": 57,
             "actual_qty": 136, "actual_weight": 1693, "allowable_moisture": 20,
             "actual_moisture": "20.00", "claim_dust": None, "shortage_kgs": 0,
             "accepted_weight": 1693, "rate": 12300, "claim_rate": 0, "claim_quality": None,
             "water_damage_amount": 0, "premium_amount": 0, "remarks": None, "warehouse_id": 53},
        ],
    }


def _li_updates(calls):
    return [(s, p) for s, p in calls if s.startswith("UPDATE JUTE_MR_LI")]


def _header_updates(calls):
    return [(s, p) for s, p in calls if s.startswith("UPDATE JUTE_MR SET")]


def test_update_warns_per_over_issued_line_and_still_saves():
    session, exec_fn = _session([
        _over_issued_row(45727, "D TD-5", 1121.0, 3600.0),
        _over_issued_row(45728, "D TD-6", 1693.0, 5440.0),
    ])

    resp = client.put(MR_UPDATE_URL, json=_payload())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    warning = body["warning"]
    assert "D TD-5: 3600 kg already issued against 1121 kg received" in warning
    assert "D TD-6: 5440 kg already issued against 1693 kg received" in warning
    assert "Sync issued weights" in warning

    # Warn, don't refuse: both lines and the header were written and committed.
    assert len(_li_updates(exec_fn.calls)) == 2
    assert len(_header_updates(exec_fn.calls)) == 1
    session.commit.assert_called_once()
    session.rollback.assert_not_called()


def test_update_warning_is_null_when_nothing_is_over_issued():
    session, exec_fn = _session([])

    resp = client.put(MR_UPDATE_URL, json=_payload())

    assert resp.status_code == 200, resp.text
    assert resp.json()["warning"] is None
    assert len(_li_updates(exec_fn.calls)) == 2
    session.commit.assert_called_once()


def test_over_issue_probe_runs_after_line_writes_and_before_commit():
    """The probe must see the weights just written, so it has to come after the
    line UPDATEs; and it is part of the same transaction (before commit)."""
    session, exec_fn = _session([_over_issued_row(45727, "D TD-5", 1121.0, 3600.0)])
    order = []
    original = session.execute.side_effect

    def _tracking_exec(sql, params=None):
        s = " ".join(str(sql).split()).upper()
        if s.startswith("UPDATE JUTE_MR_LI"):
            order.append("li")
        elif "HAVING COALESCE(SUM(JI.WEIGHT), 0) > COALESCE(LI.ACTUAL_WEIGHT, 0)" in s:
            order.append("probe")
        return original(sql, params)

    session.execute.side_effect = _tracking_exec
    session.commit.side_effect = lambda: order.append("commit")

    resp = client.put(MR_UPDATE_URL, json=_payload())

    assert resp.status_code == 200, resp.text
    assert order == ["li", "li", "probe", "commit"]


def test_probe_falls_back_to_line_id_when_item_has_no_name():
    _session([_over_issued_row(45727, None, 1121.0, 3600.0)])

    resp = client.put(MR_UPDATE_URL, json=_payload())

    assert resp.status_code == 200, resp.text
    assert "line 45727: 3600 kg already issued against 1121 kg received" in resp.json()["warning"]
