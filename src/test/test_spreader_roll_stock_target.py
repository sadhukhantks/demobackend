"""Roll Stock Reports > Stock As At: target vs actual roll stock (TKT-000170)."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.issuePlanner import build_issue_planner_report
from src.juteProduction import roll_stock_target as rst
from src.main import app

client = TestClient(app)


def _stock_row(item_id, item_name, current_kg, item_id_2=None, item_name_2=None,
               ratio_1=1, ratio_2=None):
    row = MagicMock()
    row._mapping = {
        "item_id": item_id, "item_name": item_name, "item_id_2": item_id_2,
        "item_name_2": item_name_2, "ratio_1": ratio_1, "ratio_2": ratio_2,
        "current_kg": current_kg,
    }
    return row


def _planner_row(item_id, name, target_mt, clamped=False, source="master"):
    return {
        "item_id": item_id, "quality_name": name, "maturity_hours": 48.0,
        "maturity_source": source, "target_stock_date": "2026-10-09",
        "target_date_clamped": clamped, "target_stock_mt": target_mt,
    }


def _result(rows):
    res = MagicMock()
    res.fetchall.return_value = rows
    return res


class TestTargetMillDay:
    @pytest.mark.parametrize(
        "as_of, expected",
        [
            (datetime(2026, 10, 8, 6, 0), date(2026, 10, 7)),   # end of spell C closes D-1
            (datetime(2026, 10, 8, 7, 0), date(2026, 10, 8)),
            (datetime(2026, 10, 8, 14, 0), date(2026, 10, 8)),
            (datetime(2026, 10, 8, 23, 0), date(2026, 10, 8)),
            (datetime(2026, 10, 9, 0, 0), date(2026, 10, 8)),
            (datetime(2026, 10, 9, 5, 0), date(2026, 10, 8)),
            (datetime(2026, 10, 9, 6, 0), date(2026, 10, 8)),
            (datetime(2026, 10, 1, 3, 0), date(2026, 9, 30)),  # month boundary
        ],
    )
    def test_mill_day_boundaries(self, as_of, expected):
        assert rst.target_mill_day(as_of) == expected


class TestSplitStockByQuality:
    def test_pure_and_blend_rows(self):
        kg, names = rst.split_stock_by_quality([
            _stock_row(501, "Q1", 1000.0),
            _stock_row(501, "Q1", 900.0, item_id_2=502, item_name_2="Q2", ratio_1=2, ratio_2=1),
        ])
        assert kg == {501: pytest.approx(1600.0), 502: pytest.approx(300.0)}
        assert names == {501: "Q1", 502: "Q2"}

    def test_blend_with_null_ratio_2_treated_as_1(self):
        kg, _ = rst.split_stock_by_quality([
            _stock_row(501, "Q1", 1000.0, item_id_2=502, item_name_2="Q2", ratio_1=1, ratio_2=None),
        ])
        assert kg == {501: pytest.approx(500.0), 502: pytest.approx(500.0)}


class TestClassify:
    @pytest.mark.parametrize(
        "target, actual, status",
        [
            (10.0, 10.0, rst.STATUS_ON_TARGET),
            (10.0, 9.0, rst.STATUS_ON_TARGET),   # -10% is inside the band
            (10.0, 11.0, rst.STATUS_ON_TARGET),
            (10.0, 8.9, rst.STATUS_SHORT),
            (10.0, 11.1, rst.STATUS_EXCESS),
            (0.0, 5.0, rst.STATUS_NO_TARGET),
        ],
    )
    def test_status_band(self, target, actual, status):
        _pct, got = rst.classify(target, actual, 10.0)
        assert got == status

    def test_pct_is_none_without_target(self):
        assert rst.classify(0.0, 3.0, 10.0)[0] is None


class TestCompareTargetVsActual:
    def test_union_of_planned_and_stocked_qualities(self):
        rows, totals = rst.compare_target_vs_actual(
            [_planner_row(501, "Q1", 10.0), _planner_row(502, "Q2", 4.0)],
            {501: 8000.0, 503: 2000.0},
            {501: "Q1", 503: "Q3"},
        )
        by_id = {r["item_id"]: r for r in rows}
        assert set(by_id) == {501, 502, 503}
        assert by_id[501]["actual_mt"] == 8.0
        assert by_id[501]["diff_mt"] == -2.0
        assert by_id[501]["diff_pct"] == -20.0
        assert by_id[501]["status"] == rst.STATUS_SHORT
        assert by_id[502]["actual_mt"] == 0.0 and by_id[502]["diff_pct"] == -100.0
        assert by_id[503]["quality_name"] == "Q3"
        assert by_id[503]["target_mt"] == 0.0 and by_id[503]["status"] == rst.STATUS_NO_TARGET
        assert totals["target_mt"] == 14.0
        assert totals["actual_mt"] == 10.0
        assert totals["diff_mt"] == -4.0
        assert totals["diff_pct"] == pytest.approx(-28.6)
        assert totals["short_count"] == 2 and totals["excess_count"] == 0

    def test_rows_sorted_by_quality_name(self):
        rows, _ = rst.compare_target_vs_actual(
            [_planner_row(2, "B", 1.0), _planner_row(1, "A", 1.0)], {}, {}
        )
        assert [r["quality_name"] for r in rows] == ["A", "B"]

    def test_zero_target_and_zero_stock_dropped(self):
        rows, totals = rst.compare_target_vs_actual([_planner_row(501, "Q1", 0.0)], {}, {})
        assert rows == []
        assert totals["target_mt"] == 0.0 and totals["diff_pct"] is None

    def test_unstocked_waste_excluded_but_reported(self):
        rows, totals = rst.compare_target_vs_actual(
            [_planner_row(501, "Q1", 10.0), _planner_row(900, "HABIJABI", 2.5)],
            {501: 10000.0},
            {},
            waste_item_ids={900},
        )
        assert [r["item_id"] for r in rows] == [501]
        assert totals["target_mt"] == 10.0
        assert totals["excluded_waste_target_mt"] == 2.5
        assert totals["excluded_waste_count"] == 1

    def test_waste_with_actual_rolls_is_kept(self):
        rows, _ = rst.compare_target_vs_actual(
            [_planner_row(900, "HABIJABI", 2.5)], {900: 2500.0}, {}, waste_item_ids={900}
        )
        assert rows[0]["status"] == rst.STATUS_ON_TARGET


class TestBuildRollStockTargetVsActual:
    def _planner(self, co_id=2):
        return {
            "summary": {"co_id": co_id, "window_from": "2026-10-07", "window_to": "2026-10-11"},
            "required_issue": [_planner_row(501, "Q1", 10.0), _planner_row(900, "WASTE", 1.0)],
        }

    def test_uses_mill_day_and_as_of_cutoff(self, monkeypatch):
        calls = {}

        def fake_planner(db, branch_id, report_date, include_godown_cover=True):
            calls.update(branch_id=branch_id, report_date=report_date,
                         include_godown_cover=include_godown_cover)
            return self._planner()

        monkeypatch.setattr(rst, "build_issue_planner_report", fake_planner)
        db = MagicMock()
        db.execute.side_effect = [
            _result([_stock_row(501, "Q1", 9500.0)]),
            _result([SimpleNamespace(item_id=501, item_type_id=2),
                     SimpleNamespace(item_id=900, item_type_id=3)]),
        ]
        as_of = datetime(2026, 10, 8, 6, 0)
        data = rst.build_roll_stock_target_vs_actual(db, 2, 29, as_of)

        assert calls == {"branch_id": 29, "report_date": date(2026, 10, 7),
                         "include_godown_cover": False}
        stock_params = db.execute.call_args_list[0][0][1]
        assert stock_params == {"co_id": 2, "branch_id": 29, "as_of_dt": as_of}
        assert data["target_day"] == "2026-10-07"
        assert data["target_day_from"] == "2026-10-07 06:00:00"
        assert data["target_day_to"] == "2026-10-08 06:00:00"
        assert [r["item_id"] for r in data["rows"]] == [501]
        assert data["rows"][0]["status"] == rst.STATUS_ON_TARGET
        assert data["totals"]["excluded_waste_target_mt"] == 1.0

    def test_explicit_target_day_overrides_mill_day(self, monkeypatch):
        seen = {}

        def fake_planner(db, branch_id, report_date, include_godown_cover=True):
            seen["day"] = report_date
            return self._planner()

        monkeypatch.setattr(rst, "build_issue_planner_report", fake_planner)
        db = MagicMock()
        db.execute.side_effect = [_result([]), _result([])]
        rst.build_roll_stock_target_vs_actual(
            db, 2, 29, datetime(2026, 10, 8, 14), target_day=date(2026, 10, 1)
        )
        assert seen["day"] == date(2026, 10, 1)

    def test_branch_of_other_company_rejected(self, monkeypatch):
        monkeypatch.setattr(
            rst, "build_issue_planner_report", lambda *a, **k: self._planner(co_id=99)
        )
        with pytest.raises(rst.BranchCompanyMismatchError):
            rst.build_roll_stock_target_vs_actual(MagicMock(), 2, 29, datetime(2026, 10, 8, 14))


class TestPlannerWithoutGodownCover:
    def test_skips_godown_query_and_coverage(self):
        db = MagicMock()
        results = [MagicMock() for _ in range(5)]
        results[0].scalar.return_value = 2
        for r in results[1:]:
            r.fetchall.return_value = []
        db.execute.side_effect = results
        report = build_issue_planner_report(
            db, 29, date(2026, 10, 8), include_godown_cover=False
        )
        assert db.execute.call_count == 5  # branch, prod, plan, maturity, snapshot
        assert report["stock_coverage"] == []


class TestTargetVsActualEndpoint:
    URL = "/api/spreaderProd/roll_stock_target_vs_actual"

    @pytest.fixture(autouse=True)
    def setup_overrides(self, monkeypatch):
        self.session = MagicMock()
        self.calls = []

        def fake_build(db, co_id, branch_id, as_of_dt, target_day=None):
            self.calls.append((co_id, branch_id, as_of_dt, target_day))
            return {"rows": [], "totals": {}}

        import src.juteProduction.spreader_stock as ss
        monkeypatch.setattr(ss, "build_roll_stock_target_vs_actual", fake_build)
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.session
        yield
        app.dependency_overrides.clear()

    def test_happy_path(self):
        resp = client.get(f"{self.URL}?co_id=2&branch_id=29&as_of_dt=2026-10-08T06:00")
        assert resp.status_code == 200
        assert resp.json() == {"data": {"rows": [], "totals": {}}}
        assert self.calls == [(2, 29, datetime(2026, 10, 8, 6, 0), None)]

    def test_target_date_passed_through(self):
        resp = client.get(
            f"{self.URL}?co_id=2&branch_id=29&as_of_dt=2026-10-08T06:00&target_date=2026-10-08"
        )
        assert resp.status_code == 200
        assert self.calls[0][3] == date(2026, 10, 8)

    def test_branch_required(self):
        resp = client.get(f"{self.URL}?co_id=2")
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_co_required(self):
        assert client.get(f"{self.URL}?branch_id=29").status_code == 400

    def test_invalid_target_date(self):
        resp = client.get(f"{self.URL}?co_id=2&branch_id=29&target_date=08-10-2026")
        assert resp.status_code == 400
        assert "target_date" in resp.json()["detail"]

    def test_invalid_as_of_dt(self):
        assert client.get(f"{self.URL}?co_id=2&branch_id=29&as_of_dt=bad").status_code == 400

    def test_mismatch_maps_to_400_and_unknown_branch_to_404(self, monkeypatch):
        import src.juteProduction.spreader_stock as ss
        from src.juteProcurement.issuePlanner import UnknownBranchError

        def mismatch(*a, **k):
            raise rst.BranchCompanyMismatchError("nope")

        def unknown(*a, **k):
            raise UnknownBranchError("missing")

        monkeypatch.setattr(ss, "build_roll_stock_target_vs_actual", mismatch)
        assert client.get(f"{self.URL}?co_id=2&branch_id=29").status_code == 400
        monkeypatch.setattr(ss, "build_roll_stock_target_vs_actual", unknown)
        assert client.get(f"{self.URL}?co_id=2&branch_id=29").status_code == 404
