"""
Batch Cost Report — "planned weight not appearing" message (TKT-000206).

Reporter (sls, br 29, 2026-10-02): "The planned weight is not appearing. Check
for the reason for the same. If the batch plan is not assigned for the day due
to which it is not showing then please display a message stating the same."

Root cause on sls: no jute_batch_daily_assign rows existed for 2026-09-30 and
2026-10-01 when the ticket was raised, so planned weight (plan % x actual
issue) was 0 for every row. The report now returns plan_status.messages:

1. issues but no Open/Approved assignment that day -> warning, whole day;
2. some yarn qualities unassigned -> warning naming them (Draft / Pending
   assignments are called out — they do not count);
3. nothing issued -> info (with or without a plan);
4. earlier days of the month with unplanned issues -> MTD info.

Reuses the SQLite fixture of the MTD tests (yarn 1 = HESSIAN WARP planned on
10-01, 10-02, 10-04 and only a Draft on 10-03; yarn 2 = SACKING WEFT never
planned).
"""

from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql

from src.juteProcurement.batchPlanCoverage import MAX_LISTED_DAYS, summarize_plan_coverage
from src.juteProcurement.reportQueries import get_batch_cost_plan_coverage_query
from src.juteProcurement.reports import get_batch_cost_report
from src.test.test_jute_batch_cost_mtd import ASSIGNS, BRANCH, MONTH_START, _engine

# "No assignment on this branch" (the fixture cannot insert an empty list).
OTHER_BRANCH_ONLY = [(30, "2026-10-01", 1, 1, 3)]


def _coverage(report_date, basis="actual", **fixture):
    with _engine(**fixture).connect() as c:
        rows = [dict(r._mapping) for r in c.execute(
            get_batch_cost_plan_coverage_query(basis=basis),
            {"branch_id": BRANCH, "month_start": MONTH_START, "report_date": report_date},
        )]
    return rows, summarize_plan_coverage(rows, report_date)


@pytest.mark.parametrize("basis", ["actual", "accounts"])
def test_query_binds_only_the_router_params(basis):
    compiled = get_batch_cost_plan_coverage_query(basis=basis).compile(dialect=mysql.dialect())
    assert set(compiled.params) == {"branch_id", "month_start", "report_date"}


def test_some_yarn_qualities_unplanned_are_named():
    _, out = _coverage("2026-10-04")
    assert out["day_has_issue"] and out["day_has_plan"]
    assert [y["yarn_type_name"] for y in out["day_unplanned_yarns"]] == ["SACKING WEFT"]
    assert out["day_unplanned_yarns"][0]["assignment"] == "none"
    warn = [m for m in out["messages"] if m["severity"] == "warning"]
    assert len(warn) == 1
    assert "not assigned on 04-10-2026 for: SACKING WEFT." in warn[0]["text"]
    assert "HESSIAN WARP" not in warn[0]["text"]


def test_whole_day_unplanned_draft_assignment_is_called_out():
    _, out = _coverage("2026-10-03")
    assert out["day_has_issue"] and not out["day_has_plan"]
    text = out["messages"][0]["text"]
    assert out["messages"][0]["severity"] == "warning"
    assert text.startswith("Batch plan is not assigned for 03-10-2026")
    assert "HESSIAN WARP (assignment still in Draft)" in text
    assert out["day_unplanned_yarns"][0]["assignment"] == "draft"


def test_pending_assignment_is_called_out():
    assigns = [a if a[1] != "2026-10-03" else (29, "2026-10-03", 1, 1, 20) for a in ASSIGNS]
    _, out = _coverage("2026-10-03", assigns=assigns)
    assert "HESSIAN WARP (assignment pending approval)" in out["messages"][0]["text"]


def test_no_assignment_at_all_matches_the_sls_case():
    """sls 2026-10-01 at ticket time: issues, zero assignment rows."""
    _, out = _coverage("2026-10-01", assigns=OTHER_BRANCH_ONLY)
    assert [m["severity"] for m in out["messages"]] == ["warning"]
    assert out["messages"][0]["text"].startswith(
        "Batch plan is not assigned for 01-10-2026, so planned weight is not shown."
    )
    assert "Not yet counted" not in out["messages"][0]["text"]


def test_fully_planned_day_has_no_day_message():
    _, out = _coverage("2026-10-01")
    assert out["messages"] == []
    assert out["day_unplanned_yarns"] == [] and out["mtd_unplanned_days"] == []


def test_nothing_issued_without_plan():
    _, out = _coverage("2026-10-06")
    assert out["day_has_issue"] is False
    assert out["messages"][0] == {
        "severity": "info",
        "text": "No jute was issued and no batch plan is assigned for 06-10-2026.",
    }


def test_nothing_issued_with_plan_explains_zero():
    assigns = ASSIGNS + [(29, "2026-10-06", 1, 1, 3)]
    rows, out = _coverage("2026-10-06", assigns=assigns)
    assert out["day_has_plan"] and not out["day_has_issue"]
    assert out["messages"][0]["text"].startswith("No jute was issued on 06-10-2026, so planned weight is 0")


def test_mtd_lists_earlier_unplanned_days_only():
    _, out = _coverage("2026-10-04")
    # 10-03 = Draft only; 10-04 itself is reported by the day message.
    assert out["mtd_unplanned_days"] == ["2026-10-03"]
    info = out["messages"][-1]
    assert info["severity"] == "info"
    assert "leaves out 1 earlier day of this month" in info["text"]
    assert info["text"].endswith(": 03-10-2026.")


def test_accounts_basis_uses_the_shifted_date():
    # Yarn 2's 09-29 issue moves to 10-02 (unplanned); its 10-04 issue moves out.
    _, out = _coverage("2026-10-04", basis="accounts")
    assert out["mtd_unplanned_days"] == ["2026-10-02", "2026-10-03"]
    assert out["day_unplanned_yarns"] == []
    assert all(m["severity"] == "info" for m in out["messages"])


def test_mtd_list_is_capped():
    rows = [
        {"d": f"2026-10-{d:02d}", "yarn_type_id": 1, "yarn_type_name": "Y",
         "weight": 10, "counted": 0, "pending": 0, "draft": 0}
        for d in range(1, 15)
    ]
    out = summarize_plan_coverage(rows, "2026-10-20")
    assert len(out["mtd_unplanned_days"]) == 14
    assert out["messages"][-1]["text"].endswith(f"and {14 - MAX_LISTED_DAYS} more.")


def test_endpoint_returns_plan_status():
    eng = _engine(assigns=OTHER_BRANCH_ONLY)
    with eng.connect() as c:
        class _Db:
            def execute(self, q, p):
                return c.execute(q, p)
        out = get_batch_cost_report(
            SimpleNamespace(query_params={"branch_id": str(BRANCH), "date": "2026-10-01"}),
            db=_Db(), token_data={},
        )
    assert out["data"], "rows still come back"
    assert all(r["planned_weight"] == 0 for r in out["data"])
    assert out["plan_status"]["messages"][0]["severity"] == "warning"
