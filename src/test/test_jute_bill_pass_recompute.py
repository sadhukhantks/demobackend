"""Bill Pass D7 (2026-09-03): totals are never taken from the request body —
total_amount / claim_amount / tds_amount / roundoff / net_total are always
recomputed from jute_mr_li lines via recompute_mr_money.

NOTE: the plan's draft test used /api/billPass/... — that prefix belongs to
the unrelated src.procurement.billpass router. src.juteProcurement.billPass
is actually mounted at /api/juteBillPass (src/main.py:272); URLs below use
the real prefix."""
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement import billPass as bp
from src.juteProcurement import mr as mr_module

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    yield db
    app.dependency_overrides.clear()


def _row(**cols):
    m = MagicMock(); m._mapping = cols
    for k, v in cols.items(): setattr(m, k, v)
    return m


def test_body_totals_are_ignored_and_recomputed(mock_db):
    mock_db.execute.return_value.fetchone.return_value = _row(
        jute_mr_id=1, status_id=3, total_amount=1.0, claim_amount=1.0, tds_amount=1.0,
        party_id="123", jute_mr_date=__import__("datetime").date(2026, 9, 3), branch_mr_no=12,
    )
    with patch.object(mr_module, "get_cumulative_mr_value_for_party_in_fy", return_value=0.0), \
         patch.object(bp, "recompute_mr_money", return_value={"total_amount": 50000.0, "claim_amount": 0.0,
                                                            "tds_amount": 0.0, "roundoff": 0.0, "net_total": 50000.0}) as rc, \
         patch.object(bp, "calculate_mr_amounts", return_value={"total_amount": 50000.0, "claim_amount": 0.0}):
        r = client.put("/api/juteBillPass/update_bill_pass/1?co_id=2",
                       json={"total_amount": 95000, "claim_amount": 5, "tds_amount": 9})
    assert r.status_code == 200
    rc.assert_called_once()
    sqls = " ".join(str(c.args[0]) for c in mock_db.execute.call_args_list)
    assert ":total_amount" not in sqls.split("UPDATE jute_mr")[-1] or "recompute" in sqls  # body totals never written directly


def test_calculate_tds_amount_called_with_calculated_total(mock_db):
    """The TDS re-derive uses calculate_mr_amounts' total_amount, not the body's, and
    places the MR by its STORED date / MR number / id (only earlier MRs count)."""
    mock_db.execute.return_value.fetchone.return_value = _row(
        jute_mr_id=1, status_id=3, total_amount=1.0, claim_amount=1.0, tds_amount=1.0,
        party_id="123", jute_mr_date=__import__("datetime").date(2026, 9, 3), branch_mr_no=12,
    )
    with patch.object(mr_module, "get_cumulative_mr_value_for_party_in_fy", return_value=6000000.0) as cum, \
         patch.object(mr_module, "calculate_tds_amount", wraps=mr_module.calculate_tds_amount) as tds, \
         patch.object(bp, "recompute_mr_money", return_value={}) as rc, \
         patch.object(bp, "calculate_mr_amounts", return_value={"total_amount": 100000.0, "claim_amount": 0.0}):
        r = client.put("/api/juteBillPass/update_bill_pass/1?co_id=2", json={"total_amount": 999999})
    assert r.status_code == 200
    cum.assert_called_once_with(mock_db, "123", __import__("datetime").date(2026, 9, 3),
                                exclude_mr_id=1, branch_mr_no=12)
    tds.assert_called_once_with(6000000.0, 100000.0)
    rc.assert_called_once()
    _, kwargs = rc.call_args
    assert kwargs.get("tds_amount") == 100.0  # (6.1M - 5M threshold) * 0.001


def test_not_found_when_not_approved(mock_db):
    mock_db.execute.return_value.fetchone.return_value = None
    r = client.put("/api/juteBillPass/update_bill_pass/1?co_id=2", json={"remarks": "x"})
    assert r.status_code == 404
