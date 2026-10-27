"""
Re-evaluate on the Batch Cost report (owner request 2026-10-08): issues stamped at
value 0 (MR line had no rate yet) are re-priced from the line's CURRENT rate.

Pinned:
- value = weight/100 x line rate; only changed issues are written;
- a line still without a rate is reported (its MR id), never zeroed;
- weights are never part of the update;
- endpoint validates co_id / range and commits the planned updates.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.juteProcurement.issue import JuteIssueReprice, plan_issue_reprice, reprice_issues


def _r(i, weight, value, rate, mr=1):
    return SimpleNamespace(jute_issue_id=i, weight=weight, issue_value=value, line_rate=rate, jute_mr_id=mr)


def test_zero_value_is_priced_from_the_line_rate():
    plan = plan_issue_reprice([_r(1, 732.0, 0.0, 13650.0)])
    assert plan["updates"] == [(1, 99918.0)]
    assert plan["value_before"] == 0 and plan["value_after"] == 99918.0


def test_correct_value_is_left_alone():
    assert plan_issue_reprice([_r(1, 100.0, 13650.0, 13650.0)])["updates"] == []


def test_stale_nonzero_value_follows_the_current_rate():
    assert plan_issue_reprice([_r(1, 100.0, 13250.0, 13350.0)])["updates"] == [(1, 13350.0)]


def test_line_without_rate_is_reported_not_zeroed():
    plan = plan_issue_reprice([_r(1, 50.0, 0.0, 0.0, mr=28592), _r(2, 50.0, 500.0, 0.0, mr=7)])
    assert plan["updates"] == []
    assert plan["no_rate_mr_ids"] == [28592]
    assert plan["value_after"] == 500.0


def _req(co_id="2"):
    req = MagicMock()
    req.query_params = {"co_id": co_id} if co_id else {}
    return req


def test_endpoint_writes_only_values_and_commits():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [_r(1, 732.0, 0.0, 13650.0), _r(2, 100.0, 13650.0, 13650.0)]
    out = reprice_issues(JuteIssueReprice(branch_id=29, date_from=date(2026, 10, 1), date_to=date(2026, 10, 7)),
                         _req(), db, {"sub": "tester"})
    assert out["data"]["issues_checked"] == 2 and out["data"]["issues_changed"] == 1
    updates = [c for c in db.execute.call_args_list if "UPDATE jute_issue" in str(c.args[0])]
    assert len(updates) == 1 and "weight" not in str(updates[0].args[0]).split("SET")[1].split("WHERE")[0]
    assert updates[0].args[1]["v"] == 99918.0
    db.commit.assert_called_once()


@pytest.mark.parametrize("co_id,dfrom,dto", [(None, date(2026, 10, 1), date(2026, 10, 7)),
                                              ("2", date(2026, 10, 7), date(2026, 10, 1)),
                                              ("2", date(2026, 1, 1), date(2026, 10, 1))])
def test_endpoint_rejects_bad_input(co_id, dfrom, dto):
    with pytest.raises(HTTPException) as e:
        reprice_issues(JuteIssueReprice(branch_id=29, date_from=dfrom, date_to=dto), _req(co_id), MagicMock(), {})
    assert e.value.status_code == 400
