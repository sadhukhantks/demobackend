"""Winding Quality-Wise + Quality-Group-Wise reports.

Port of the legacy admin/reports/Winding_quality_wise_report (two tables on one
page), served by src/juteProduction/winding_reports.py under the
/api/juteProductionReports prefix.

The pivots and the arithmetic live in pure builders (build_quality_wise /
build_quality_group_wise), so they are exercised directly on stub rows — the
same approach test_winding_stock_report.py takes with the difference-report
builders. Only the parameter validation and the wiring go through TestClient
with the DB + auth dependencies overridden (test_dpr_report.py's convention).

The target-resolution RULE (latest effective_date on or before the report date,
'target' beating 'standard') is SQL, so it is tested as SQL: the builder is run
against a throwaway in-memory SQLite table rather than asserted about as a
string. If the ranking changes, the test fails on values, not on grep.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.main import app
from src.juteProduction.winding_reports import (
    build_quality_group_wise,
    build_quality_wise,
    get_current_user_with_refresh,
    get_tenant_db,
)
from src.juteProduction.winding_query import resolve_winding_targets_query

client = TestClient(app)

QUALITY_WISE_URL = "/api/juteProductionReports/winding_quality_wise"
GROUP_WISE_URL = "/api/juteProductionReports/winding_quality_group_wise"


def _rows(mappings):
    out = []
    for m in mappings:
        row = MagicMock()
        row._mapping = m
        out.append(row)
    return out


def _result(mappings):
    res = MagicMock()
    res.fetchall.return_value = _rows(mappings)
    return res


def _branch_co(co_id):
    """What _co_id_of_branch reads: db.execute(...).first().co_id."""
    res = MagicMock()
    res.first.return_value = SimpleNamespace(co_id=co_id)
    return res


def _target_rows(mapping):
    """resolve_winding_targets_query rows — read by attribute, not by _mapping."""
    res = MagicMock()
    res.fetchall.return_value = [
        SimpleNamespace(item_id=k, value=v) for k, v in mapping.items()
    ]
    return res


def _recon(shift, eb_id, item_id, kg, code=None, name=None):
    return {
        "shift": shift,
        "eb_id": eb_id,
        "item_id": item_id,
        "item_code": code or f"Q{item_id}",
        "item_name": name or f"QUALITY {item_id}",
        "production_kg": kg,
    }


def _by_code(built, code):
    return next(r for r in built["rows"] if r["item_code"] == code)


# ---------------------------------------------------------------------------
# Table 1 — build_quality_wise
# ---------------------------------------------------------------------------


class TestQualityWisePivot:
    """Two qualities across A/B/C, with one winder working two shifts."""

    def _built(self):
        recon = [
            # Quality 1 — eb 10 works BOTH A and B, eb 11 only A, eb 12 only C.
            _recon("A", 10, 1, 100.0),
            _recon("A", 11, 1, 50.0),
            _recon("B", 10, 1, 80.0),
            _recon("C", 12, 1, 60.0),
            # Quality 2 — eb 20 twice in shift A (two spells inside one shift).
            _recon("A", 20, 2, 30.0),
            _recon("A", 20, 2, 20.0),
            _recon("B", 21, 2, 40.0),
        ]
        mtd = [
            {"item_id": 1, "item_code": "Q1", "item_name": "QUALITY 1",
             "mtd_prod": 1000.0, "mtd_winders": 20},
            {"item_id": 2, "item_code": "Q2", "item_name": "QUALITY 2",
             "mtd_prod": 500.0, "mtd_winders": 8},
        ]
        return build_quality_wise(recon, mtd, {1: 40.0, 2: 25.0})

    def test_per_shift_production(self):
        q1 = _by_code(self._built(), "Q1")
        assert (q1["prod_A"], q1["prod_B"], q1["prod_C"]) == (150.0, 80.0, 60.0)
        assert q1["prod_total"] == 290.0

    def test_winder_working_two_shifts_counts_twice(self):
        q1 = _by_code(self._built(), "Q1")
        # eb 10 appears in A and in B: 2 in A (10, 11), 1 in B (10), 1 in C (12).
        assert (q1["winders_A"], q1["winders_B"], q1["winders_C"]) == (2, 1, 1)
        assert q1["winders_total"] == 4

    def test_same_winder_twice_in_one_shift_counts_once(self):
        q2 = _by_code(self._built(), "Q2")
        # eb 20 has two spells inside shift A — one winder, both spells' KG.
        assert q2["winders_A"] == 1
        assert q2["prod_A"] == 50.0
        assert q2["winders_total"] == 2

    def test_rows_sorted_by_item_code(self):
        assert [r["item_code"] for r in self._built()["rows"]] == ["Q1", "Q2"]

    def test_mtd_columns_carried_through(self):
        q1 = _by_code(self._built(), "Q1")
        assert q1["mtd_prod"] == 1000.0
        assert q1["mtd_winders"] == 20
        assert q1["mtd_prod_per_winder"] == 50.0

    def test_mtd_only_quality_still_gets_a_row(self):
        """A quality run earlier in the month but not today must not vanish —
        dropping it would understate the Grand Total's month production."""
        built = build_quality_wise(
            [_recon("A", 10, 1, 100.0)],
            [
                {"item_id": 1, "item_code": "Q1", "item_name": "Q1",
                 "mtd_prod": 300.0, "mtd_winders": 3},
                {"item_id": 9, "item_code": "Q9", "item_name": "Q9",
                 "mtd_prod": 700.0, "mtd_winders": 7},
            ],
            {},
        )
        q9 = _by_code(built, "Q9")
        assert q9["prod_total"] == 0.0
        assert q9["mtd_prod"] == 700.0
        assert built["grand_total"]["mtd_prod"] == 1000.0

    def test_off_shift_production_excluded_and_reported(self):
        built = build_quality_wise(
            [_recon("A", 10, 1, 100.0), _recon(None, 11, 1, 25.0)], [], {}
        )
        assert built["rows"][0]["prod_total"] == 100.0
        assert built["off_shift_kg"] == 25.0


class TestQualityWiseDerivedColumns:
    def test_prod_per_winder_guarded_on_zero_winders(self):
        built = build_quality_wise(
            [_recon("A", 10, 1, 100.0)],
            [{"item_id": 1, "mtd_prod": 0.0, "mtd_winders": 0}],
            {},
        )
        row = built["rows"][0]
        assert row["prod_per_winder_A"] == 100.0
        assert row["prod_per_winder_B"] == 0.0  # no winders, no ratio
        assert row["prod_per_winder_C"] == 0.0
        assert row["mtd_prod_per_winder"] == 0.0

    def test_avg_is_weighted_not_the_mean_of_shift_ratios(self):
        # A: 300 kg / 1 winder = 300; B: 100 kg / 4 winders = 25.
        # mean of ratios = 162.5 (wrong); weighted = 400/5 = 80.
        recon = [
            _recon("A", 1, 1, 300.0),
            _recon("B", 2, 1, 25.0),
            _recon("B", 3, 1, 25.0),
            _recon("B", 4, 1, 25.0),
            _recon("B", 5, 1, 25.0),
        ]
        row = build_quality_wise(recon, [], {})["rows"][0]
        assert row["prod_per_winder_A"] == 300.0
        assert row["prod_per_winder_B"] == 25.0
        assert row["prod_per_winder_avg"] == 80.0

    def test_target_is_per_winder_times_winders_and_difference_is_shortfall(self):
        # 2 winders in A at 40 kg each = 80 target vs 60 produced -> 20 short.
        recon = [_recon("A", 1, 1, 30.0), _recon("A", 2, 1, 30.0)]
        row = build_quality_wise(recon, [], {1: 40.0})["rows"][0]
        assert row["target_A"] == 80.0
        assert row["target_total"] == 80.0
        assert row["difference"] == 20.0  # positive = shortfall (legacy sign)

    def test_difference_negative_when_target_beaten(self):
        recon = [_recon("A", 1, 1, 100.0)]
        row = build_quality_wise(recon, [], {1: 40.0})["rows"][0]
        assert row["target_total"] == 40.0
        assert row["difference"] == -60.0

    def test_quality_with_no_target_reads_zero_and_is_counted(self):
        recon = [_recon("A", 1, 1, 100.0), _recon("A", 2, 2, 50.0)]
        built = build_quality_wise(recon, [], {1: 40.0})
        untargeted = _by_code(built, "Q2")
        assert untargeted["target_A"] == 0.0
        assert untargeted["target_total"] == 0.0
        assert untargeted["difference"] == -50.0
        assert built["no_target_qualities"] == 1


class TestQualityWiseGrandTotal:
    def _built(self):
        recon = [
            _recon("A", 1, 1, 300.0),   # 1 winder, 300 kg
            _recon("A", 2, 2, 100.0),   # 1 winder, 100 kg
            _recon("A", 3, 2, 100.0),   # 1 winder, 100 kg
        ]
        mtd = [
            {"item_id": 1, "mtd_prod": 900.0, "mtd_winders": 3},
            {"item_id": 2, "mtd_prod": 600.0, "mtd_winders": 12},
        ]
        return build_quality_wise(recon, mtd, {1: 250.0, 2: 80.0})

    def test_components_are_summed(self):
        gt = self._built()["grand_total"]
        assert gt["prod_A"] == 500.0
        assert gt["winders_A"] == 3
        assert gt["target_A"] == 410.0  # 250*1 + 80*2
        assert gt["mtd_prod"] == 1500.0
        assert gt["mtd_winders"] == 15

    def test_ratios_are_recomputed_not_summed(self):
        rows = self._built()["rows"]
        gt = self._built()["grand_total"]
        # Row ratios are 300 and 100 — summing them would give 400, averaging 200.
        assert sorted(r["prod_per_winder_A"] for r in rows) == [100.0, 300.0]
        assert gt["prod_per_winder_A"] == pytest.approx(166.67)  # 500 / 3
        assert gt["prod_per_winder_avg"] == pytest.approx(166.67)
        # Same for MTD: 300 and 50 per winder on the rows, 100 overall.
        assert gt["mtd_prod_per_winder"] == 100.0
        # difference recomputed from the summed columns: 410 - 500.
        assert gt["difference"] == -90.0

    def test_labelled_as_grand_total(self):
        gt = self._built()["grand_total"]
        assert gt["item_id"] is None
        assert gt["item_code"] is None
        assert gt["item_name"] == "Grand Total"

    def test_empty_report_totals_to_zero(self):
        built = build_quality_wise([], [], {})
        assert built["rows"] == []
        assert built["grand_total"]["prod_total"] == 0.0
        assert built["grand_total"]["prod_per_winder_avg"] == 0.0


# ---------------------------------------------------------------------------
# Table 2 — build_quality_group_wise
# ---------------------------------------------------------------------------


GROUPS = {
    1: {"item_grp_id": 70, "item_grp_name": "SKWP"},
    2: {"item_grp_id": 70, "item_grp_name": "SKWP"},
    3: {"item_grp_id": 71, "item_grp_name": "HSWP"},
}


def _spg(shift, grp_id, grp_name, prod, wind=0.0):
    return {
        "item_grp_id": grp_id,
        "item_grp_name": grp_name,
        "shift": shift,
        "spg_prod": prod,
        "spg_winding_qty": wind,
    }


def _by_group(built, name):
    return next(r for r in built["rows"] if r["item_grp_name"] == name)


class TestQualityGroupWise:
    def test_two_qualities_collapse_into_one_group(self):
        recon = [_recon("A", 10, 1, 100.0), _recon("A", 11, 2, 40.0)]
        built = build_quality_group_wise(recon, GROUPS, [])
        assert len(built["rows"]) == 1
        row = built["rows"][0]
        assert row["item_grp_id"] == 70
        assert row["wnd_prod_A"] == 140.0
        assert row["wnd_winders_A"] == 2

    def test_variance_computed_on_every_shift_including_A(self):
        """The legacy page never compared shift A — that was the bug, so A is
        the column this test cares about most."""
        recon = [
            _recon("A", 10, 1, 100.0),
            _recon("B", 11, 1, 200.0),
            _recon("C", 12, 1, 50.0),
        ]
        spg = [
            _spg("A", 70, "SKWP", 90.0, wind=95.0),
            _spg("B", 70, "SKWP", 250.0),
            _spg("C", 70, "SKWP", 50.0),
        ]
        row = build_quality_group_wise(recon, GROUPS, spg)["rows"][0]
        assert row["variance_A"] == 10.0    # winding ahead
        assert row["variance_B"] == -50.0   # spinning ahead
        assert row["variance_C"] == 0.0     # agreed
        assert row["wnd_prod_total"] == 350.0
        assert row["spg_prod_total"] == 390.0
        assert row["variance_total"] == -40.0
        assert row["spg_winding_qty"] == 95.0

    def test_group_seen_only_by_spinning_still_appears(self):
        built = build_quality_group_wise(
            [_recon("A", 10, 1, 100.0)], GROUPS, [_spg("A", 71, "HSWP", 60.0)]
        )
        hswp = _by_group(built, "HSWP")
        assert hswp["wnd_prod_total"] == 0.0
        assert hswp["spg_prod_A"] == 60.0
        assert hswp["variance_A"] == -60.0

    def test_unmapped_quality_is_bucketed_not_dropped(self):
        recon = [_recon("A", 10, 1, 100.0), _recon("A", 11, 99, 25.0)]
        built = build_quality_group_wise(recon, GROUPS, [])
        unmapped = _by_group(built, "Unmapped")
        assert unmapped["item_grp_id"] is None
        assert unmapped["wnd_prod_A"] == 25.0
        assert built["unmapped_qualities"] == 1
        assert built["grand_total"]["wnd_prod_total"] == 125.0

    def test_spinning_row_without_a_group_joins_the_unmapped_row(self):
        built = build_quality_group_wise(
            [_recon("A", 10, 99, 25.0)], {}, [_spg("A", None, None, 10.0)]
        )
        unmapped = _by_group(built, "Unmapped")
        assert unmapped["wnd_prod_A"] == 25.0
        assert unmapped["spg_prod_A"] == 10.0
        assert unmapped["variance_A"] == 15.0

    def test_unmapped_sorts_last(self):
        recon = [
            _recon("A", 10, 3, 10.0),   # HSWP
            _recon("A", 11, 1, 10.0),   # SKWP
            _recon("A", 12, 99, 10.0),  # unmapped
        ]
        built = build_quality_group_wise(recon, GROUPS, [])
        assert [r["item_grp_name"] for r in built["rows"]] == [
            "HSWP", "SKWP", "Unmapped",
        ]

    def test_grand_total_recomputes_variance_from_sums(self):
        recon = [_recon("A", 10, 1, 100.0), _recon("A", 11, 3, 40.0)]
        spg = [_spg("A", 70, "SKWP", 90.0, wind=5.0),
               _spg("A", 71, "HSWP", 60.0, wind=7.0)]
        gt = build_quality_group_wise(recon, GROUPS, spg)["grand_total"]
        assert gt["item_grp_id"] is None
        assert gt["item_grp_name"] == "Grand Total"
        assert gt["wnd_prod_A"] == 140.0
        assert gt["spg_prod_A"] == 150.0
        assert gt["variance_A"] == -10.0
        assert gt["variance_total"] == -10.0
        assert gt["spg_winding_qty"] == 12.0


# ---------------------------------------------------------------------------
# Target resolution — the SQL rule, run for real on in-memory SQLite
# ---------------------------------------------------------------------------


class TestTargetResolution:
    """LAST-DATE resolution with 'target' preferred over 'standard'.

    The rule lives entirely in resolve_winding_targets_query's ranking, so it is
    executed rather than string-matched. SQLite stands in for MySQL: the builder
    uses only ROW_NUMBER() and a boolean ordering expression, both portable.
    """

    ROWS = [
        # (id, co, eff_date, ref_id, role, value)
        (1, 1, "2026-01-01", 100, "standard", 30.0),
        (2, 1, "2026-02-01", 100, "target", 40.0),
        (3, 1, "2026-03-01", 100, "target", 45.0),   # latest <= 2026-03-15 -> wins
        (4, 1, "2026-04-01", 100, "target", 99.0),   # in the future, ignored
        (5, 1, "2026-01-01", 200, "standard", 20.0),
        (6, 1, "2026-02-01", 200, "standard", 25.0),  # no target row -> fallback
        (7, 1, "2026-02-01", 300, "target", 77.0),
        (8, 1, "2026-03-01", 300, "target", 88.0),
        (9, 1, "2026-03-01", 300, "standard", 11.0),  # newer standard must NOT win
        (10, 2, "2026-03-01", 100, "target", 12345.0),  # another company
        (11, 1, "2026-03-01", 400, "target", 66.0),  # inactive
    ]

    def _resolved(self, on_date="2026-03-15", co_id=1):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text(
                """
                CREATE TABLE jute_prod_winding_target_map (
                    winding_target_map_id INTEGER PRIMARY KEY,
                    co_id INTEGER, branch_id INTEGER, effective_date TEXT,
                    ref_id INTEGER, id_type TEXT, value_role TEXT, param TEXT,
                    value REAL, active INTEGER
                )
                """
            ))
            for rid, co, eff, ref, role, val in self.ROWS:
                conn.execute(
                    text(
                        "INSERT INTO jute_prod_winding_target_map VALUES "
                        "(:id, :co, NULL, :eff, :ref, 'qid', :role, 'prod', :val, :act)"
                    ),
                    {"id": rid, "co": co, "eff": eff, "ref": ref, "role": role,
                     "val": val, "act": 0 if rid == 11 else 1},
                )
            rows = conn.execute(
                resolve_winding_targets_query(), {"co_id": co_id, "on_date": on_date}
            ).fetchall()
        return {r.item_id: (float(r.value), r.value_role, r.effective_date) for r in rows}

    def test_latest_effective_date_on_or_before_the_report_date_wins(self):
        assert self._resolved()[100] == (45.0, "target", "2026-03-01")

    def test_future_rows_ignored(self):
        assert self._resolved(on_date="2026-01-15")[100] == (30.0, "standard", "2026-01-01")

    def test_standard_used_only_when_no_target_resolves(self):
        assert self._resolved()[200] == (25.0, "standard", "2026-02-01")

    def test_target_beats_a_same_day_standard(self):
        assert self._resolved()[300] == (88.0, "target", "2026-03-01")

    def test_other_companies_and_inactive_rows_excluded(self):
        resolved = self._resolved()
        assert resolved[100][0] != 12345.0
        assert 400 not in resolved

    def test_one_row_per_quality(self):
        assert sorted(self._resolved()) == [100, 200, 300]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class TestWindingQualityReportEndpoints:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    @pytest.mark.parametrize("url", [QUALITY_WISE_URL, GROUP_WISE_URL])
    def test_missing_branch_id_400(self, url):
        resp = client.get(url, params={"report_date": "2026-08-18"})
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    @pytest.mark.parametrize("url", [QUALITY_WISE_URL, GROUP_WISE_URL])
    def test_invalid_branch_id_400(self, url):
        resp = client.get(url, params={"branch_id": "abc", "report_date": "2026-08-18"})
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    @pytest.mark.parametrize("url", [QUALITY_WISE_URL, GROUP_WISE_URL])
    def test_missing_report_date_400(self, url):
        resp = client.get(url, params={"branch_id": 29})
        assert resp.status_code == 400
        assert "report_date" in resp.json()["detail"]

    @pytest.mark.parametrize("url", [QUALITY_WISE_URL, GROUP_WISE_URL])
    def test_malformed_report_date_400(self, url):
        resp = client.get(url, params={"branch_id": 29, "report_date": "18-08-2026"})
        assert resp.status_code == 400
        assert "report_date" in resp.json()["detail"]

    def test_unknown_branch_400(self):
        missing = MagicMock()
        missing.first.return_value = None
        self._session.execute.return_value = missing
        resp = client.get(
            QUALITY_WISE_URL, params={"branch_id": 999, "report_date": "2026-08-18"}
        )
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_db_error_500(self):
        self._session.execute.side_effect = RuntimeError("db exploded")
        resp = client.get(
            QUALITY_WISE_URL, params={"branch_id": 29, "report_date": "2026-08-18"}
        )
        assert resp.status_code == 500
        assert "db exploded" in resp.json()["detail"]

    def test_quality_wise_happy_path(self):
        self._session.execute.side_effect = [
            _branch_co(2),
            _result([  # day reconciliation
                _recon("A", 10, 1, 100.0, code="Q1"),
                _recon("B", 11, 1, 60.0, code="Q1"),
                _recon("A", 12, 2, 40.0, code="Q2"),
            ]),
            _result([  # MTD
                {"item_id": 1, "item_code": "Q1", "item_name": "QUALITY 1",
                 "mtd_prod": 900.0, "mtd_winders": 9},
            ]),
            _target_rows({1: 50.0}),
        ]
        resp = client.get(
            QUALITY_WISE_URL,
            params={"branch_id": 29, "report_date": "2026-08-18"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["report_date"] == "2026-08-18"
        assert data["mtd_from"] == "2026-08-01"
        assert data["shifts"] == ["A", "B", "C"]

        q1 = next(r for r in data["rows"] if r["item_code"] == "Q1")
        assert q1["prod_total"] == 160.0
        assert q1["winders_total"] == 2
        assert q1["target_total"] == 100.0  # 50 per winder x 2 winder-shifts
        assert q1["difference"] == -60.0
        assert q1["mtd_prod_per_winder"] == 100.0

        q2 = next(r for r in data["rows"] if r["item_code"] == "Q2")
        assert q2["target_total"] == 0.0  # no target row for this quality

        assert data["grand_total"]["item_name"] == "Grand Total"
        assert data["grand_total"]["prod_total"] == 200.0
        assert "1 quality(ies) on this report have NO target row" in data["note"]
        assert "not pro-rated" in data["note"].lower()

    def test_quality_wise_accepts_tran_date_alias(self):
        self._session.execute.side_effect = [
            _branch_co(2), _result([]), _result([]), _target_rows({}),
        ]
        resp = client.get(QUALITY_WISE_URL, params={"branch_id": 29, "tran_date": "2026-08-18"})
        assert resp.status_code == 200
        assert resp.json()["data"]["report_date"] == "2026-08-18"

    def test_quality_wise_binds_branch_date_and_derived_co(self):
        self._session.execute.side_effect = [
            _branch_co(7), _result([]), _result([]), _target_rows({}),
        ]
        client.get(QUALITY_WISE_URL, params={"branch_id": 29, "report_date": "2026-08-18"})
        binds = [call.args[1] for call in self._session.execute.call_args_list]
        assert binds[0] == {"branch_id": 29}                       # branch -> co
        assert binds[1]["branch_id"] == 29                         # day reconciliation
        assert binds[1]["tran_date"] == date(2026, 8, 18)
        assert binds[2]["branch_id"] == 29                         # MTD window
        assert binds[2]["date_from"] == date(2026, 8, 1)
        assert binds[2]["report_date"] == date(2026, 8, 18)
        # co_id is DERIVED from the branch, never taken from the query string.
        assert binds[3] == {"co_id": 7, "on_date": date(2026, 8, 18)}

    def test_group_wise_happy_path(self):
        self._session.execute.side_effect = [
            _branch_co(2),
            _result([  # day reconciliation
                _recon("A", 10, 1, 100.0),
                _recon("B", 11, 1, 60.0),
                _recon("A", 12, 99, 25.0),  # quality with no group
            ]),
            _result([  # item -> group map
                {"item_id": 1, "item_grp_id": 70, "item_grp_name": "SKWP"},
                {"item_id": 99, "item_grp_id": None, "item_grp_name": None},
            ]),
            _result([  # spinning day slice
                {"item_grp_id": 70, "item_grp_name": "SKWP", "shift": "A",
                 "spg_prod": 90.0, "spg_winding_qty": 85.0},
                {"item_grp_id": 70, "item_grp_name": "SKWP", "shift": "B",
                 "spg_prod": 70.0, "spg_winding_qty": 0.0},
            ]),
        ]
        resp = client.get(GROUP_WISE_URL, params={"branch_id": 29, "report_date": "2026-08-18"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["report_date"] == "2026-08-18"
        assert data["shifts"] == ["A", "B", "C"]

        skwp = next(r for r in data["rows"] if r["item_grp_name"] == "SKWP")
        assert skwp["wnd_prod_A"] == 100.0
        assert skwp["spg_prod_A"] == 90.0
        assert skwp["variance_A"] == 10.0
        assert skwp["variance_B"] == -10.0
        assert skwp["variance_total"] == 0.0
        assert skwp["spg_winding_qty"] == 85.0

        unmapped = next(r for r in data["rows"] if r["item_grp_name"] == "Unmapped")
        assert unmapped["item_grp_id"] is None
        assert unmapped["wnd_prod_A"] == 25.0

        assert data["grand_total"]["item_grp_name"] == "Grand Total"
        assert data["grand_total"]["wnd_prod_total"] == 185.0
        assert "1 winding quality(ies) resolve to no item group" in data["note"]
        assert "co_id AND branch_id" in data["note"]

    def test_group_wise_skips_the_group_lookup_when_no_production(self):
        """No qualities means no ids to expand — an empty IN list is not a query."""
        self._session.execute.side_effect = [_branch_co(2), _result([]), _result([])]
        resp = client.get(GROUP_WISE_URL, params={"branch_id": 29, "report_date": "2026-08-18"})
        assert resp.status_code == 200
        assert resp.json()["data"]["rows"] == []
        assert self._session.execute.call_count == 3

    def test_group_wise_binds_branch_scoped_spinning(self):
        self._session.execute.side_effect = [_branch_co(7), _result([]), _result([])]
        client.get(GROUP_WISE_URL, params={"branch_id": 29, "report_date": "2026-08-18"})
        spinning_binds = self._session.execute.call_args_list[2].args[1]
        assert spinning_binds == {
            "co_id": 7, "branch_id": 29, "tran_date": date(2026, 8, 18),
        }
