from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7, "sub": "qa"}
    yield db
    app.dependency_overrides.clear()


def _r(**c):
    m = MagicMock()
    for k, v in c.items(): setattr(m, k, v)
    return m


def test_sync_rescales_from_bale_unit_weight(mock_db):
    mock_db.execute.return_value.fetchall.return_value = [
        _r(jute_mr_li_id=10, actual_qty=104.0, actual_weight=4168.0, actual_rate=4500.0,
           jute_issue_id=87688, quantity=22.0, weight=900.0, issue_value=40500.0, status_id=3),
    ]
    r = client.post("/api/juteIssue/sync_issue_weights?co_id=2", json={"jute_mr_id": 5, "branch_id": 29})
    assert r.status_code == 200
    row = r.json()["data"][0]
    assert row["weight_after"] == 882 and row["issue_value_after"] == 39690.0  # whole kg
    assert r.json()["totals"]["issues_changed"] == 1


def test_sync_scales_down_when_no_qty_ratio(mock_db):
    mock_db.execute.return_value.fetchall.return_value = [
        _r(jute_mr_li_id=10, actual_qty=0, actual_weight=1000.0, actual_rate=100.0,
           jute_issue_id=1, quantity=0, weight=800.0, issue_value=800.0, status_id=1),
        _r(jute_mr_li_id=10, actual_qty=0, actual_weight=1000.0, actual_rate=100.0,
           jute_issue_id=2, quantity=0, weight=400.0, issue_value=400.0, status_id=1),
    ]
    r = client.post("/api/juteIssue/sync_issue_weights?co_id=2", json={"jute_mr_id": 5, "branch_id": 29})
    rows = r.json()["data"]
    assert [x["weight_after"] for x in rows] == [667, 333]  # whole kg, last row closes the line


def _waste_line(li_id, actual_rate, rate, issues):
    """A type-3 (jute waste) MR line as sls really books it: bought in BALES
    (actual_qty 108) against 10,042 kg, issued in kg slices from the grouped
    waste pool. actual_qty != actual_weight on every one of the 486 sls lines."""
    return [
        _r(jute_mr_li_id=li_id, actual_qty=108.0, actual_weight=10042.0,
           actual_rate=actual_rate, rate=rate, is_waste=1,
           jute_issue_id=iid, quantity=q, weight=w, issue_value=v, status_id=21)
        for iid, q, w, v in issues
    ]


def test_sync_leaves_purchased_waste_kg_slices_alone_and_reprices_from_the_line(mock_db):
    """Owner ruling 2026-09-25 (D4): a purchased-waste slice IS its kg. The
    bale unit-weight math (10042/108 = 93 kg per unit) must not touch it — it
    would turn a 500 kg slice into 46,500 kg and, because 500 "units" >= 108,
    then "close" the line by rewriting the last slice to the whole 10,042 kg.
    Only the value is re-derived, at the line's rate."""
    mock_db.execute.return_value.fetchall.return_value = _waste_line(
        45659, actual_rate=None, rate=13500.0,
        issues=[(9001, 500.0, 500.0, 0.0), (9002, 120.0, 120.0, 16200.0)],
    )
    r = client.post("/api/juteIssue/sync_issue_weights?co_id=2", json={"jute_mr_id": 5, "branch_id": 27})
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert [(x["weight_after"], x["quantity_after"]) for x in rows] == [(500.0, 500.0), (120.0, 120.0)]
    # priced at jml.rate (the QC-written rate) because the line has no actual_rate
    assert [x["issue_value_after"] for x in rows] == [67500.0, 16200.0]
    assert r.json()["totals"] == {
        "lines_processed": 1, "issues_processed": 2, "issues_changed": 1,
        "weight_before": 620.0, "weight_after": 620.0,
    }
    upd = [c for c in mock_db.execute.call_args_list if "UPDATE jute_issue" in str(c[0][0])]
    assert [c[0][1]["id"] for c in upd] == [9001]
    assert upd[0][0][1]["w"] == 500.0 and upd[0][0][1]["q"] == 500.0 and upd[0][0][1]["v"] == 67500.0


def test_sync_prices_a_waste_line_at_its_saved_actual_rate_when_it_has_one(mock_db):
    mock_db.execute.return_value.fetchall.return_value = _waste_line(
        45659, actual_rate=14000.0, rate=13500.0,
        issues=[(9001, 500.0, 500.0, 67500.0)],
    )
    r = client.post("/api/juteIssue/sync_issue_weights?co_id=2", json={"jute_mr_id": 5, "branch_id": 27})
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["issue_value_after"] == 70000.0
    assert r.json()["data"][0]["weight_after"] == 500.0


def test_sync_query_flags_waste_lines_from_the_item_group_and_carries_the_qc_rate():
    from src.juteProcurement.constants import JUTE_WASTE_ITEM_TYPE_ID
    from src.juteProcurement.query import get_jute_mr_lines_and_issues_for_sync_query

    sql = " ".join(str(get_jute_mr_lines_and_issues_for_sync_query()).split())
    assert "LEFT JOIN item_mst im ON im.item_id = jml.actual_item_id" in sql
    assert "LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in sql
    assert f"CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN 1 ELSE 0 END AS is_waste" in sql
    assert "jml.rate," in sql
    # a flag, not a filter: every line of the MR is still synced
    assert "item_type_id" not in sql.partition("WHERE")[2]
