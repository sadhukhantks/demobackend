"""TKT-000207 (sls br29, 2026-10-02): the MR list showed "-" for MR Weight on some MRs.

jute_mr.mr_weight was only written by MR save (and by gate entry saves that carry
line items). An MR that went Gate IN -> QC -> Gate OUT -> Pending/Approved without
ever pressing Save kept a NULL/0 header, while its lines held the real weight.

Fix, three layers:
  * the MR list / Excel query falls back to the line sum (else "-"; no gate-net fallback),
  * gate-out, Pending/Approve and MR save keep the header in step with the lines.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement import mr as mr_module
from src.juteProcurement.query import JUTE_MR_LIST_WEIGHT_SQL, get_jute_mr_table_query
from src.juteProcurement.totals import redistribute_mr_line_weights, sync_mr_weight_from_lines
from src.main import app

client = TestClient(app)


def _sql(call) -> str:
    return " ".join(str(call.args[0]).split())


# -- list query -------------------------------------------------------------------

def test_list_query_selects_derived_mr_weight_not_the_raw_header():
    sql = " ".join(str(get_jute_mr_table_query(co_id=2)).split())
    expr = " ".join(JUTE_MR_LIST_WEIGHT_SQL.split())
    assert f"{expr} AS mr_weight" in sql
    assert "jm.mr_weight," not in sql.replace(expr, "")  # raw header column no longer selected as-is


def test_list_weight_expression_order_header_then_lines_then_blank():
    expr = " ".join(JUTE_MR_LIST_WEIGHT_SQL.split())
    header, lines = (expr.index(t) for t in (
        "NULLIF(jm.mr_weight, 0)", "SUM(COALESCE(li.accepted_weight, li.actual_weight, 0))"))
    assert header < lines
    # no line weight -> NULL ("-"): never the gate-weighed lorry net, never a raw 0
    # (owner ruling 2026-10-03, sls MR 28427)
    assert "jm.actual_weight" not in expr
    assert expr.endswith("(li.active = 1 OR li.active IS NULL)) )")
    # a stored non-blank header must win, never be recomputed
    assert expr.startswith("COALESCE( NULLIF(jm.mr_weight, 0),")
    # inactive lines are not counted
    assert "li.active = 1 OR li.active IS NULL" in expr


# -- sync helper --------------------------------------------------------------------

def test_sync_default_only_fills_a_blank_header_and_is_one_statement():
    db = MagicMock()
    sync_mr_weight_from_lines(db, 28370)
    assert db.execute.call_count == 1
    sql = _sql(db.execute.call_args)
    assert "COALESCE(jm.mr_weight, 0) = 0" in sql
    assert "COALESCE(accepted_weight, actual_weight, 0)" in sql
    assert "s.line_kg > 0" in sql  # lines with no weight never zero/overwrite the header
    assert db.execute.call_args.args[1] == {"mr_id": 28370}


def test_sync_forced_overwrites_a_stored_header():
    db = MagicMock()
    sync_mr_weight_from_lines(db, 5, only_if_blank=False)
    assert "COALESCE(jm.mr_weight, 0) = 0" not in _sql(db.execute.call_args)


# -- gate-out ----------------------------------------------------------------------

def _row(**cols):
    m = MagicMock()
    m._mapping = cols
    for k, v in cols.items():
        setattr(m, k, v)
    return m


def test_gate_out_redistribute_also_syncs_header_weight():
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = _row(actual_weight=11130)
    db.execute.return_value.fetchall.return_value = [
        _row(jute_mr_li_id=1, actual_qty=20, allowable_moisture=None, actual_moisture=None),
        _row(jute_mr_li_id=2, actual_qty=55, allowable_moisture=None, actual_moisture=None),
    ]
    assert redistribute_mr_line_weights(db, 28173) == 2
    last = _sql(db.execute.call_args_list[-1])
    assert last.startswith("UPDATE jute_mr jm JOIN") and "SET jm.mr_weight" in last
    assert "COALESCE(jm.mr_weight, 0) = 0" not in last  # lines were just rewritten: header follows


def test_gate_out_without_header_weight_touches_nothing():
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = _row(actual_weight=0)
    assert redistribute_mr_line_weights(db, 28173) == 0
    assert not [c for c in db.execute.call_args_list if "UPDATE" in str(c.args[0])]


# -- Pending / Approve / bill pass ---------------------------------------------------

def test_recompute_mr_money_fills_a_blank_header_weight():
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = _row(total_amount=1000.0, claim_amount=0.0)
    mr_module.recompute_mr_money(db, 28370)
    syncs = [_sql(c) for c in db.execute.call_args_list if "SET jm.mr_weight" in _sql(c)]
    assert len(syncs) == 1
    assert "COALESCE(jm.mr_weight, 0) = 0" in syncs[0]  # never overwrites a stored weight


# -- MR save -----------------------------------------------------------------------

@pytest.fixture
def mr_save_client():
    calls = []
    hdr = {"qc_check": 0, "jute_mr_id": 100, "status_id": 1, "party_id": "7", "branch_id": 2}

    def _exec(sql, params=None):
        s = " ".join(str(sql).split()).upper()
        calls.append((s, params or {}))
        res = MagicMock()
        res.fetchone.return_value = _row(**hdr)
        res.fetchall.return_value = []
        res.scalar.return_value = 1
        return res

    session = MagicMock()
    session.execute.side_effect = _exec
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: session
    yield calls
    app.dependency_overrides.clear()


def _header_weight(calls):
    upd = [p for s, p in calls if s.startswith("UPDATE JUTE_MR SET")]
    assert upd
    return upd[0]["mr_weight"]


def test_mr_save_with_zero_header_weight_uses_the_line_total(mr_save_client):
    resp = client.put("/api/juteMR/update_mr/100?co_id=1", json={
        "mr_weight": 0,
        "line_items": [{"jute_mr_li_id": 501, "actual_weight": 7496, "actual_qty": 187},
                       {"jute_mr_li_id": 502, "actual_weight": 1884, "actual_qty": 47}],
    })
    assert resp.status_code == 200, resp.text
    assert _header_weight(mr_save_client) == 9380


def test_mr_save_keeps_an_explicit_header_weight(mr_save_client):
    resp = client.put("/api/juteMR/update_mr/100?co_id=1", json={
        "mr_weight": 9000,
        "line_items": [{"jute_mr_li_id": 501, "actual_weight": 9380, "actual_qty": 234}],
    })
    assert resp.status_code == 200, resp.text
    assert _header_weight(mr_save_client) == 9000
