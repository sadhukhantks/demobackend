"""Tests for the Jute Issue Planner report (/juteReports/issue-planner)."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.juteProcurement.issuePlanner import (
    UnknownBranchError,
    _split_blend_kg,
    build_issue_planner_report,
)
from src.juteProcurement.issuePlannerQueries import (
    get_plan_window_query,
    get_reference_production_query,
    get_roll_stock_snapshot_query,
)

T = date(2026, 8, 22)


def _result(rows=None, scalar=None):
    res = MagicMock()
    res.fetchall.return_value = rows or []
    res.scalar.return_value = scalar
    return res


def _mock_db(branch_co, prod, plan, maturity, snapshot, godown):
    db = MagicMock()
    db.execute.side_effect = [
        _result(scalar=branch_co),
        _result(rows=prod),
        _result(rows=plan),
        _result(rows=maturity),
        _result(rows=snapshot),
        _result(rows=godown),
    ]
    return db


def _plan_row(assign_date, yarn_id, yarn_item_id, yarn_name, plan_id, plan_name,
              item_id, quality_name, percentage, status_id=1):
    return SimpleNamespace(
        assign_date=assign_date, jute_yarn_id=yarn_id, yarn_item_id=yarn_item_id,
        yarn_name=yarn_name, batch_plan_id=plan_id, plan_name=plan_name,
        status_id=status_id, item_id=item_id, quality_name=quality_name,
        percentage=percentage,
    )


def _snapshot_row(grp, item_id, item_name, current_kg, item_id_2=None,
                  item_name_2=None, ratio_1=1, ratio_2=None):
    return SimpleNamespace(
        entry_id_grp=grp, item_id=item_id, item_name=item_name,
        item_id_2=item_id_2, item_name_2=item_name_2,
        ratio_1=ratio_1, ratio_2=ratio_2,
        produced_kg=current_kg, issued_kg=0, current_kg=current_kg,
    )


def _standard_db():
    """Two planned yarns, one without production; blend + pure roll stock."""
    prod = [
        SimpleNamespace(item_id=101, yarn_name="Yarn A", jute_yarn_id=11,
                        production_kg=12000.0),
    ]
    plan = []
    for offset in (0, 1, 2):
        d = T + timedelta(days=offset)
        plan.append(_plan_row(d, 11, 101, "Yarn A", 7, "Plan P1", 501, "Q-TOSSA", 60.0))
        plan.append(_plan_row(d, 11, 101, "Yarn A", 7, "Plan P1", 502, "Q-WHITE", 40.0))
    plan.append(_plan_row(T, 12, 102, "Yarn B", 8, "Plan P2", 501, "Q-TOSSA", 100.0,
                          status_id=21))
    maturity = [SimpleNamespace(item_id=501, maturity_hours=72)]
    snapshot = [
        _snapshot_row(1, 501, "Q-TOSSA", 30000.0),
        _snapshot_row(2, 501, "Q-TOSSA", 10000.0, item_id_2=502,
                      item_name_2="Q-WHITE", ratio_1=1, ratio_2=1),
    ]
    godown = [
        SimpleNamespace(item_id=501, item_name="Q-TOSSA", closing_weight=90000.0),
        SimpleNamespace(item_id=502, item_name="Q-WHITE", closing_weight=20000.0),
    ]
    return _mock_db(1, prod, plan, maturity, snapshot, godown)


class TestSplitBlendKg:
    def test_pure_roll_goes_to_single_quality(self):
        row = _snapshot_row(1, 501, "Q", 900.0)
        assert _split_blend_kg(row) == {501: 900.0}

    def test_blend_split_by_ratio(self):
        row = _snapshot_row(1, 501, "Q1", 900.0, item_id_2=502, item_name_2="Q2",
                            ratio_1=2, ratio_2=1)
        assert _split_blend_kg(row) == {501: 600.0, 502: 300.0}

    def test_zero_ratio_denominator_falls_back_to_first_quality(self):
        row = _snapshot_row(1, 501, "Q1", 900.0, item_id_2=502, item_name_2="Q2",
                            ratio_1=0, ratio_2=0)
        assert _split_blend_kg(row) == {501: 900.0}


class TestBuildIssuePlannerReport:
    def test_unknown_branch_raises(self):
        db = MagicMock()
        db.execute.side_effect = [_result(scalar=None)]
        with pytest.raises(UnknownBranchError):
            build_issue_planner_report(db, 999, T)

    def test_summary_window_and_dates(self):
        report = build_issue_planner_report(_standard_db(), 21, T)
        s = report["summary"]
        assert s["report_date"] == "2026-08-22"
        assert s["production_date"] == "2026-08-21"
        assert s["window_from"] == "2026-08-22"
        assert s["window_to"] == "2026-08-26"
        assert s["snapshot_at"] == "2026-08-22 06:00:00"
        assert s["co_id"] == 1 and s["branch_id"] == 21

    def test_reference_production_default_for_unproduced_yarn(self):
        report = build_issue_planner_report(_standard_db(), 21, T)
        by_name = {r["yarn_name"]: r for r in report["reference_production"]}
        assert by_name["Yarn A"]["reference_source"] == "actual"
        assert by_name["Yarn A"]["reference_kg"] == 12000.0
        assert by_name["Yarn A"]["planned"] is True
        assert by_name["Yarn B"]["reference_source"] == "default"
        assert by_name["Yarn B"]["reference_kg"] == 1000.0

    def test_batch_plan_deduped_per_assign(self):
        report = build_issue_planner_report(_standard_db(), 21, T)
        # 3 dates x Yarn A + 1 x Yarn B, not one row per composition line
        assert len(report["batch_plan"]) == 4
        drafts = [r for r in report["batch_plan"] if r["status_id"] == 21]
        assert len(drafts) == 1 and drafts[0]["yarn_name"] == "Yarn B"

    def test_quality_requirement_math(self):
        report = build_issue_planner_report(_standard_db(), 21, T)
        req = {(r["assign_date"], r["item_id"]): r["requirement_kg"]
               for r in report["quality_requirement"]}
        # Issue day: Yarn A 12000*60% + Yarn B default 1000*100%
        assert req[("2026-08-22", 501)] == 8200.0
        assert req[("2026-08-22", 502)] == 4800.0
        # Later days: only Yarn A assigned
        assert req[("2026-08-23", 501)] == 7200.0
        assert ("2026-08-25", 501) not in req

    def test_required_issue_with_master_maturity(self):
        report = build_issue_planner_report(_standard_db(), 21, T)
        rows = {r["item_id"]: r for r in report["required_issue"]}
        q = rows[502]  # no maturity row -> default 48h -> target T+2
        assert q["maturity_source"] == "default"
        assert q["target_stock_date"] == "2026-08-24"
        # 4.8 MT/day requirement x 2 maturity days
        assert q["target_stock_mt"] == 9.6
        # blend half of 10000 kg
        assert q["current_roll_stock_mt"] == 5.0
        assert q["plan_issue_mt"] == 4.8
        # 9.6 - (5.0 - 4.8)
        assert q["required_issue_mt"] == 9.4
        assert q["required_issue_bales"] == round(9400 / 148.0, 3)

    def test_required_issue_zero_target_when_no_plan_on_target_date(self):
        report = build_issue_planner_report(_standard_db(), 21, T)
        rows = {r["item_id"]: r for r in report["required_issue"]}
        q = rows[501]  # 72h maturity -> target T+3, no assign that day
        assert q["maturity_source"] == "master"
        assert q["target_stock_date"] == "2026-08-25"
        assert q["target_stock_mt"] == 0.0
        assert q["current_roll_stock_mt"] == 35.0
        # 0 - (35.0 - 8.2) => excess stock, negative required issue
        assert q["required_issue_mt"] == -26.8

    def test_total_required_excludes_negative_rows(self):
        report = build_issue_planner_report(_standard_db(), 21, T)
        assert report["summary"]["total_required_issue_mt"] == 9.4

    def test_stock_coverage_days(self):
        report = build_issue_planner_report(_standard_db(), 21, T)
        cov = {r["item_id"]: r for r in report["stock_coverage"]}
        # 501: window requirement 8200+7200+7200 over a 5-day window
        assert cov[501]["avg_daily_requirement_mt"] == 4.52
        assert cov[501]["days_of_stock"] == round(90000 / 4520.0, 1)
        assert cov[502]["days_of_stock"] == round(20000 / 2880.0, 1)

    def test_unplanned_quality_with_stock_has_no_days(self):
        db = _mock_db(
            1, [], [], [],
            [_snapshot_row(1, 601, "Q-EXTRA", 5000.0)],
            [SimpleNamespace(item_id=601, item_name="Q-EXTRA", closing_weight=3000.0)],
        )
        report = build_issue_planner_report(db, 21, T)
        cov = {r["item_id"]: r for r in report["stock_coverage"]}
        assert cov[601]["days_of_stock"] is None
        rows = {r["item_id"]: r for r in report["required_issue"]}
        assert rows[601]["target_stock_mt"] == 0.0
        assert rows[601]["required_issue_mt"] == -5.0


class TestQueryBuilders:
    def test_reference_production_binds_and_filters(self):
        sql = str(get_reference_production_query())
        assert ":production_date" in sql and ":branch_id" in sql
        assert "daily_doff_tbl" in sql
        assert "item_id IS NOT NULL" in sql
        assert "weight_type IN" in sql

    def test_plan_window_binds_and_statuses(self):
        sql = str(get_plan_window_query())
        assert ":branch_id" in sql and ":date_from" in sql and ":date_to" in sql
        assert "jute_batch_daily_assign" in sql
        assert "21, 1, 3" in sql

    def test_snapshot_binds_and_cutoff(self):
        sql = str(get_roll_stock_snapshot_query())
        assert ":co_id" in sql and ":branch_id" in sql
        assert sql.count(":cutoff_dt") == 2  # production AND issue legs
        assert "spreader_prod_entry" in sql and "spreader_roll_issue" in sql
        assert "active = 1" in sql

    def test_snapshot_keeps_negative_groups(self):
        # An issue against rolls produced after the snapshot must net the
        # quality total down, so groups are filtered on <> 0, not > 0.
        sql = str(get_roll_stock_snapshot_query())
        assert "(p.produced_kg - COALESCE(iss.issued_kg, 0)) <> 0" in sql


class TestReviewFixes:
    def test_half_day_maturity_rounds_half_up(self):
        # 60 h = 2.5 days must target T+3 (half-up), not banker's-rounded T+2.
        db = _mock_db(
            1, [],
            [_plan_row(T + timedelta(days=3), 11, 101, "Yarn A", 7, "Plan P1",
                       501, "Q-TOSSA", 100.0)],
            [SimpleNamespace(item_id=501, maturity_hours=60)],
            [], [],
        )
        report = build_issue_planner_report(db, 21, T)
        row = {r["item_id"]: r for r in report["required_issue"]}[501]
        assert row["target_stock_date"] == "2026-08-25"
        # 1 MT/day default-reference requirement x 2.5 maturity days
        assert row["target_stock_mt"] == 2.5

    def test_negative_snapshot_group_nets_quality_total(self):
        snapshot = [
            _snapshot_row(1, 501, "Q-TOSSA", 30000.0),
            _snapshot_row(2, 501, "Q-TOSSA", -350.0),
        ]
        db = _mock_db(1, [], [], [], snapshot, [])
        report = build_issue_planner_report(db, 21, T)
        row = {r["item_id"]: r for r in report["required_issue"]}[501]
        assert row["current_roll_stock_mt"] == 29.65

    def test_zero_production_planned_yarn_shows_default_reference(self):
        # A planned yarn whose doff rows net to 0 kg uses the 1 MT default in
        # the requirement math — the reference section must show the same.
        prod = [SimpleNamespace(item_id=101, yarn_name="Yarn A", jute_yarn_id=11,
                                production_kg=0.0)]
        plan = [_plan_row(T, 11, 101, "Yarn A", 7, "Plan P1", 501, "Q-TOSSA", 100.0)]
        db = _mock_db(1, prod, plan, [], [], [])
        report = build_issue_planner_report(db, 21, T)
        ref = {r["item_id"]: r for r in report["reference_production"]}[101]
        assert ref["production_kg"] == 0.0
        assert ref["reference_kg"] == 1000.0
        assert ref["reference_source"] == "default"
        req = {(r["assign_date"], r["item_id"]): r["requirement_kg"]
               for r in report["quality_requirement"]}
        assert req[("2026-08-22", 501)] == 1000.0

    def test_reference_rows_have_unique_identity(self):
        # Two planned yarns sharing one unproduced item must stay separately
        # identifiable (item_id + jute_yarn_id is the FE row key).
        plan = [
            _plan_row(T, 11, 101, "Yarn A", 7, "Plan P1", 501, "Q-TOSSA", 100.0),
            _plan_row(T, 12, 101, "Yarn A", 8, "Plan P2", 502, "Q-WHITE", 100.0),
        ]
        db = _mock_db(1, [], plan, [], [], [])
        report = build_issue_planner_report(db, 21, T)
        keys = [(r["item_id"], r["jute_yarn_id"]) for r in report["reference_production"]]
        assert len(keys) == len(set(keys)) == 2
