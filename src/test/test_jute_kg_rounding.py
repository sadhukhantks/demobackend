"""
Jute procurement stock is kept in WHOLE KG.

MR side already rounds to 0dp on approve; the issue side rounded to 2dp, so
sum(issue.weight) per MR line drifted from the line's received weight by
fractions of a kg and the stock reports never reconciled. One helper
(`round_kg`) is now the single rounding rule for every writer.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.totals import round_kg
from src.main import app

client = TestClient(app)
BRANCH = 29


# -- the rule ------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (545.0001, 545), (881.69, 882), (2.5, 3), (3.5, 4), (-5.5, -6),
    ("106.5", 107), (None, 0), (0, 0), (1000, 1000),
])
def test_round_kg_is_half_up_and_int(raw, expected):
    out = round_kg(raw)
    assert out == expected and isinstance(out, int)


# -- issue create / update ------------------------------------------------------

def _stock_row(bal_qty, bal_weight, rate=1500.0):
    return SimpleNamespace(
        jute_mr_li_id=24143, branch_id=BRANCH, unit_conversion="BALE",
        actual_rate=rate, bal_qty=bal_qty, bal_weight=bal_weight,
    )


def _existing_row(quantity=10.0, weight=100.0):
    return SimpleNamespace(
        jute_issue_id=1, status_id=21, jute_mr_li_id=24143, waste_entry_id=None,
        branch_id=BRANCH, issue_date="2026-08-10", quantity=quantity, weight=weight,
    )


def _router(stock_row=None, existing=None, prior=None):
    calls = []

    def _execute(sql, params=None):
        s = " ".join(str(sql).split())
        calls.append((s, params or {}))
        result = MagicMock()
        if "FROM jute_mr_li jml" in s:
            result.fetchone.return_value = stock_row
        elif "SELECT quantity, weight FROM jute_issue" in s:
            result.fetchone.return_value = prior
        elif "FROM jute_issue ji INNER JOIN branch_mst bm" in s:
            result.fetchone.return_value = existing
        elif "LAST_INSERT_ID" in s:
            result.fetchone.return_value = SimpleNamespace(id=4242)
        else:
            result.fetchone.return_value = None
        return result

    _execute.calls = calls
    return _execute


def _payload(**over):
    body = {
        "branch_id": BRANCH, "issue_date": "2026-08-10", "jute_mr_li_id": 24143,
        "yarn_type_id": 5, "item_id": 335637, "quantity": 10.0, "weight": 100.0,
        "unit_conversion": "BALE",
    }
    body.update(over)
    return body


def _written(calls, verb):
    return [p for s, p in calls if s.startswith(verb)]


class TestIssueWritesWholeKg:
    @pytest.fixture(autouse=True)
    def _overrides(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1, "sub": "t"}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        yield
        app.dependency_overrides.clear()

    def test_partial_issue_rounds_the_sent_weight_to_whole_kg(self):
        exec_fn = _router(stock_row=_stock_row(bal_qty=104, bal_weight=4168))
        self.db.execute.side_effect = exec_fn
        r = client.post("/api/juteIssue/create_issue?co_id=2",
                        json=_payload(quantity=3, weight=120.23076923))
        assert r.status_code == 200, r.text
        ins = _written(exec_fn.calls, "INSERT INTO jute_issue")
        assert ins and ins[0]["weight"] == 120 and isinstance(ins[0]["weight"], int)
        assert ins[0]["issue_value"] == pytest.approx(1800.0)
        assert r.json()["issue_value"] == pytest.approx(1800.0)

    def test_full_balance_issue_snaps_to_the_remaining_weight(self):
        """qty exhausts the line: the row must take the exact balance so the
        line closes at zero, even if the caller's unit-weight math was off."""
        exec_fn = _router(stock_row=_stock_row(bal_qty=10, bal_weight=1817))
        self.db.execute.side_effect = exec_fn
        r = client.post("/api/juteIssue/create_issue?co_id=2",
                        json=_payload(quantity=10, weight=1816.667))
        assert r.status_code == 200, r.text
        ins = _written(exec_fn.calls, "INSERT INTO jute_issue")
        assert ins[0]["weight"] == 1817

    def test_full_balance_snap_never_hides_a_real_over_draw(self):
        exec_fn = _router(stock_row=_stock_row(bal_qty=1, bal_weight=1))
        self.db.execute.side_effect = exec_fn
        r = client.post("/api/juteIssue/create_issue?co_id=2",
                        json=_payload(quantity=10, weight=100))
        assert r.status_code == 400

    def test_update_rounds_weight_to_whole_kg(self):
        exec_fn = _router(stock_row=_stock_row(bal_qty=0.0, bal_weight=0.0),
                          existing=_existing_row(), prior=SimpleNamespace(quantity=10.0, weight=100.0))
        self.db.execute.side_effect = exec_fn
        r = client.put("/api/juteIssue/update_issue/1?co_id=2", json={"quantity": 10.0, "weight": 99.6})
        assert r.status_code == 200, r.text
        upd = _written(exec_fn.calls, "UPDATE jute_issue SET")
        assert upd and upd[0]["weight"] == 100


# -- sync -----------------------------------------------------------------------

def _r(**c):
    m = MagicMock()
    for k, v in c.items():
        setattr(m, k, v)
    return m


class TestSyncWholeKg:
    @pytest.fixture(autouse=True)
    def _overrides(self):
        self.db = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7, "sub": "qa"}
        yield
        app.dependency_overrides.clear()

    def _sync(self, rows):
        self.db.execute.return_value.fetchall.return_value = rows
        r = client.post("/api/juteIssue/sync_issue_weights?co_id=2", json={"jute_mr_id": 5, "branch_id": 29})
        assert r.status_code == 200, r.text
        return r.json()["data"]

    def test_partial_line_rounds_each_issue(self):
        rows = self._sync([
            _r(jute_mr_li_id=10, actual_qty=104.0, actual_weight=4168.0, actual_rate=4500.0,
               jute_issue_id=87688, quantity=22.0, weight=900.0, issue_value=40500.0, status_id=3),
        ])
        assert rows[0]["weight_after"] == 882 and rows[0]["issue_value_after"] == 39690.0

    def test_fully_issued_line_sums_exactly_to_received_weight(self):
        rows = self._sync([
            _r(jute_mr_li_id=10, actual_qty=3.0, actual_weight=1000.0, actual_rate=100.0,
               jute_issue_id=i, quantity=1.0, weight=333.33, issue_value=333.33, status_id=3)
            for i in (1, 2, 3)
        ])
        after = [x["weight_after"] for x in rows]
        assert after == [333, 333, 334] and sum(after) == 1000

    def test_bale_over_issue_cuts_the_last_row_to_close_the_line(self):
        """71 bales / 9685 kg received, 77 bales drawn: the last row loses the
        6 extra bales and whatever kg it takes to land on exactly 9685."""
        rows = self._sync([
            _r(jute_mr_li_id=10, actual_qty=71.0, actual_weight=9685.0, actual_rate=12900.0,
               jute_issue_id=i, quantity=q, weight=w, issue_value=0, status_id=3)
            for i, q, w in [(1, 8, 1089), (2, 14, 1904), (3, 14, 1930), (4, 14, 1905),
                            (5, 13, 1773), (6, 14, 1910)]
        ])
        assert [x["quantity_after"] for x in rows] == [8, 14, 14, 14, 13, 8]
        after = [x["weight_after"] for x in rows]
        assert sum(after) == 9685 and after[-1] == 9685 - sum(after[:-1])

    def test_no_qty_ratio_scales_down_to_whole_kg(self):
        rows = self._sync([
            _r(jute_mr_li_id=10, actual_qty=0, actual_weight=1000.0, actual_rate=100.0,
               jute_issue_id=1, quantity=0, weight=800.0, issue_value=800.0, status_id=1),
            _r(jute_mr_li_id=10, actual_qty=0, actual_weight=1000.0, actual_rate=100.0,
               jute_issue_id=2, quantity=0, weight=400.0, issue_value=400.0, status_id=1),
        ])
        assert [x["weight_after"] for x in rows] == [667, 333]


# -- MR approve writes the rounded received weight --------------------------------

def test_mr_update_writes_actual_weight_as_whole_kg():
    calls = []
    hdr = {"qc_check": 0, "jute_mr_id": 100, "status_id": 1, "party_id": "7", "branch_id": 2}

    def _exec(sql, params=None):
        s = " ".join(str(sql).split()).upper()
        calls.append((s, params or {}))
        res = MagicMock()
        row = MagicMock()
        row._mapping = hdr
        res.fetchone.return_value = row
        res.fetchall.return_value = []
        res.scalar.return_value = 1
        return res

    session = MagicMock()
    session.execute.side_effect = _exec
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: session
    try:
        resp = client.put("/api/juteMR/update_mr/100?co_id=1", json={
            "mr_weight": 999.6,
            "line_items": [{"jute_mr_li_id": 501, "actual_weight": 999.6, "actual_qty": 25}],
        })
        assert resp.status_code == 200, resp.text
        li = [p for s, p in calls if s.startswith("UPDATE JUTE_MR_LI") and "SET ACTIVE = 0" not in s]
        assert li and li[0]["actual_weight"] == 1000 and li[0]["accepted_weight"] == 1000
        hdr_upd = [p for s, p in calls if s.startswith("UPDATE JUTE_MR SET")]
        assert hdr_upd and hdr_upd[0]["mr_weight"] == 1000
    finally:
        app.dependency_overrides.clear()
