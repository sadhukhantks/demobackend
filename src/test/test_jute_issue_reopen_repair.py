"""plan_rows() / rollback_sql() of dbqueries/repairs/jute_issue_reopen_20260913.py -- no DB."""
import datetime
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "dbqueries" / "repairs" / "jute_issue_reopen_20260913.py"
_spec = importlib.util.spec_from_file_location("jute_issue_reopen", _SCRIPT)
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)

D = datetime.date(2026, 4, 1)


def _r(jid, status, ub="system"):
    return {"jute_issue_id": jid, "issue_date": D, "status_id": status, "jute_mr_li_id": 45030,
            "quantity": 13.0, "weight": 1773.0, "updated_by": ub, "update_date_time": None}


def test_only_approved_rows_planned_and_rollback_restores_them():
    plan = fix.plan_rows([_r(86354, 3), _r(87721, 4), _r(86360, 3, "KGROUND-20260905")], fix.TARGETS["open"])
    assert [(p["jute_issue_id"], p["status_before"], p["status_after"]) for p in plan] == [(86354, 3, 1), (86360, 3, 1)]
    sql = fix.rollback_sql(plan)
    assert ("SET status_id = 3, updated_by = 'system', update_date_time = NULL "
            "WHERE jute_issue_id = 86354 AND updated_by = 'ISSUEREOPEN-20260913';") in sql
    assert "updated_by = 'KGROUND-20260905'" in sql  # prior repair tag survives a rollback
    assert sql.startswith("-- Rollback") and sql.rstrip().endswith("COMMIT;")


def test_draft_target():
    assert fix.plan_rows([_r(1, 3)], fix.TARGETS["draft"])[0]["status_after"] == 21
