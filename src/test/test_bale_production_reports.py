"""Tests for the Bale Production reports (src/juteProduction/bale_reports.py,
GET /api/juteProductionReports/bale_daily_sheet and /bale_heavy_light).

Pure-builder tests reproduce the numbers docs/bale-production-spec-sheet.md
§1.2 verified against the mill's Google Sheet (F1–F8), the MR fallback chain
item → group → none (F7), REPACK exclusion (D2/Q4) and the daily-sheet
grouping / sl_no / footers. Endpoint tests cover parameter validation and one
happy path per report through TestClient with the DB and auth dependencies
overridden by a MagicMock session — no database.
"""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.bale_reports import (
    BAD_BRANCH_MSG,
    MAX_RANGE_DAYS,
    build_daily_sheet,
    build_heavy_light,
    resolve_mr,
    router as bale_reports_router,
)
from src.main import app

PREFIX = "/api/juteProductionReports"
SHEET_URL = f"{PREFIX}/bale_daily_sheet"
HYLT_URL = f"{PREFIX}/bale_heavy_light"

# The router is registered in src/main.py under the shared jute-production-
# reports prefix; until that include lands, mount it here (idempotent) so the
# endpoint tests exercise the real prefix either way.
if SHEET_URL not in {getattr(r, "path", None) for r in app.routes}:
    app.include_router(bale_reports_router, prefix=PREFIX, tags=["jute-production-reports"])

client = TestClient(app)

D = date(2026, 1, 27)
D2 = date(2026, 1, 28)

# --- §1.2 sample: 27-Jan-2026, quality 40"-6.50 (Code 1189), 9 bales, Σgross 3435
#     with the 5 kg packsheet already netted → Σnet 3390; std 369, std MR 16, MR 19.11
Q40 = {"finishing_quality_id": 1, "item_id": 1189, "item_name": '40"-6.50', "quality_type": 1}
Q38 = {"finishing_quality_id": 2, "item_id": 1190, "item_name": '38"-7.14', "quality_type": 1}
QSACK = {"finishing_quality_id": 3, "item_id": 2124, "item_name": "22.50\"-6x6 A-TWILL", "quality_type": 2}


def _day(q, tran_date, bales, total_net, std, std_mr=16, weft=9.0):
    return {
        **q,
        "tran_date": tran_date,
        "bales": bales,
        "total_net": total_net,
        "std_bale_wt": std,
        "std_mr_pct": std_mr,
        "weft_count": weft,
    }


HESSIAN_DAY_ROWS = [
    _day(Q40, D, 9, 3390.0, 369.0),
    _day(Q38, D, 7, 3051.0, 437.0),
]
MR_ITEM_ROWS = [
    {"entry_date": D, "item_id": 1189, "avg_mr": 19.11},
    {"entry_date": D, "item_id": 1190, "avg_mr": 18.33},
]


def _row_by_qid(report, qid):
    return next(r for r in report["rows"] if r["finishing_quality_id"] == qid)


def _group(report, label):
    return next(g for g in report["groups"] if g["label"] == label)


# =============================================================================
# Heavy/Light — F1..F8
# =============================================================================


class TestHeavyLightFormulas:
    def test_f1_to_f5_reproduce_the_sheet_sample(self):
        r = _row_by_qid(build_heavy_light(HESSIAN_DAY_ROWS, MR_ITEM_ROWS, []), 1)
        assert r["bales"] == 9
        assert r["avg_mr"] == pytest.approx(19.11)
        assert r["mr_source"] == "item"
        assert r["std_bale_wt"] == pytest.approx(369.0)
        assert r["std_mr_pct"] == pytest.approx(16.0)
        assert r["weft_count"] == pytest.approx(9.0)
        assert r["obs_per_bale"] == pytest.approx(376.67)  # F1
        assert r["corr_per_bale"] == pytest.approx(366.83)  # F2
        assert r["obs_pct"] == pytest.approx(2.08)  # F3 (positive = heavy)
        assert r["corr_pct"] == pytest.approx(-0.59)  # F4 (negative = light)
        assert r["total_std"] == pytest.approx(3321.0)  # F5
        assert r["total_obs"] == pytest.approx(3390.0)
        # sheet prints 3301.49 from the unrounded per-bale figure
        assert r["total_corr"] == pytest.approx(3301.49, abs=0.005)

    def test_second_quality_row_38_inch(self):
        r = _row_by_qid(build_heavy_light(HESSIAN_DAY_ROWS, MR_ITEM_ROWS, []), 2)
        assert r["bales"] == 7
        assert r["obs_per_bale"] == pytest.approx(435.86)
        assert r["corr_per_bale"] == pytest.approx(427.27)
        assert r["obs_pct"] == pytest.approx(-0.26)
        assert r["corr_pct"] == pytest.approx(-2.23)
        assert r["total_std"] == pytest.approx(3059.0)
        assert r["total_obs"] == pytest.approx(3051.0)

    def test_f6_hessian_group_row(self):
        rep = build_heavy_light(HESSIAN_DAY_ROWS, MR_ITEM_ROWS, [])
        h = _group(rep, "HESSIAN")
        assert h["bales"] == 16
        assert h["total_std"] == pytest.approx(6380.0)
        assert h["total_obs"] == pytest.approx(6441.0)
        assert h["total_corr"] == pytest.approx(6292.41, abs=0.01)
        assert h["obs_pct"] == pytest.approx(0.96)
        assert h["corr_pct"] == pytest.approx(-1.37)
        # group rows always present, in sheet order SACKING, HESSIAN, OVERALL
        assert [g["label"] for g in rep["groups"]] == ["SACKING", "HESSIAN", "OVERALL"]
        s = _group(rep, "SACKING")
        assert s["bales"] == 0 and s["obs_pct"] is None and s["corr_pct"] is None
        o = _group(rep, "OVERALL")
        assert o["bales"] == 16 and o["total_obs"] == pytest.approx(6441.0)
        assert rep["notes"] == []

    def test_f7_mtd_sacking_check(self):
        # §1.2 F7: 278 bales, Σgross 79465 → Σnet 79465 − 278×5 = 78075; std 290; MR 19.52; std MR 20
        rows = [_day(QSACK, D, 278, 79465.0 - 278 * 5, 290.0, std_mr=20, weft=12.0)]
        rep = build_heavy_light(rows, [{"entry_date": D, "item_id": 2124, "avg_mr": 19.52}], [])
        r = _row_by_qid(rep, 3)
        assert r["obs_per_bale"] == pytest.approx(280.85, abs=0.005)
        assert r["corr_per_bale"] == pytest.approx(281.97, abs=0.005)
        assert r["obs_pct"] == pytest.approx(-3.16)
        assert r["corr_pct"] == pytest.approx(-2.77)
        assert _group(rep, "SACKING")["bales"] == 278
        assert _group(rep, "HESSIAN")["bales"] == 0

    def test_f8_bale_weighted_avg_mr_and_snapshot_std_across_days(self):
        # day 1: 9 bales, MR 19.11, std 369; day 2: 3 bales, MR 21.0, std 380 (standard edited)
        rows = [_day(Q40, D, 9, 3390.0, 369.0), _day(Q40, D2, 3, 1140.0, 380.0)]
        mr = [
            {"entry_date": D, "item_id": 1189, "avg_mr": 19.11},
            {"entry_date": D2, "item_id": 1189, "avg_mr": 21.0},
        ]
        r = _row_by_qid(build_heavy_light(rows, mr, []), 1)
        assert r["bales"] == 12
        assert r["avg_mr"] == pytest.approx((9 * 19.11 + 3 * 21.0) / 12, abs=0.005)
        # total_std = Σ n_day × std_day, not n × current standard
        assert r["total_std"] == pytest.approx(9 * 369 + 3 * 380)
        assert r["std_bale_wt"] == pytest.approx((9 * 369 + 3 * 380) / 12, abs=0.001)
        assert r["total_obs"] == pytest.approx(4530.0)

    def test_std_mr_defaults_by_type_when_master_blank(self):
        rows = [
            _day(Q40, D, 9, 3390.0, 369.0, std_mr=None),
            _day(QSACK, D, 10, 2900.0, 290.0, std_mr=None),
        ]
        rep = build_heavy_light(rows, [], [
            {"entry_date": D, "quality_group": "HESSIAN", "avg_mr": 19.11},
            {"entry_date": D, "quality_group": "SACKING", "avg_mr": 19.52},
        ])
        assert _row_by_qid(rep, 1)["std_mr_pct"] == pytest.approx(16.0)
        assert _row_by_qid(rep, 3)["std_mr_pct"] == pytest.approx(20.0)


class TestHeavyLightMrFallback:
    GROUP_ROWS = [{"entry_date": D, "quality_group": "HESSIAN", "avg_mr": 18.5}]

    def test_item_reading_wins(self):
        mr, src = resolve_mr(str(D), 1189, 1, {(str(D), 1189): 19.11}, {(str(D), "HESSIAN"): 18.5})
        assert (mr, src) == (19.11, "item")

    def test_falls_back_to_group_then_none(self):
        assert resolve_mr(str(D), 1189, 1, {}, {(str(D), "HESSIAN"): 18.5}) == (18.5, "group")
        assert resolve_mr(str(D), 1189, 1, {}, {}) == (None, "none")
        # a reading on another day never leaks in
        assert resolve_mr(str(D2), 1189, 1, {(str(D), 1189): 19.11}, {(str(D), "HESSIAN"): 18.5}) == (None, "none")

    def test_group_fallback_flags_row_and_computes_corr(self):
        rep = build_heavy_light([_day(Q40, D, 9, 3390.0, 369.0)], [], self.GROUP_ROWS)
        r = _row_by_qid(rep, 1)
        assert r["mr_source"] == "group"
        assert r["avg_mr"] == pytest.approx(18.5)
        assert r["corr_per_bale"] == pytest.approx(376.6667 * 116 / 118.5, abs=0.005)
        assert rep["notes"] == []

    def test_no_reading_blanks_corrected_columns_and_notes(self):
        rows = [_day(Q40, D, 9, 3390.0, 369.0), _day(Q38, D, 7, 3051.0, 437.0)]
        rep = build_heavy_light(rows, [{"entry_date": D, "item_id": 1189, "avg_mr": 19.11}], [])
        r40, r38 = _row_by_qid(rep, 1), _row_by_qid(rep, 2)
        assert r40["mr_source"] == "item" and r40["corr_per_bale"] is not None
        assert r38["mr_source"] == "none"
        assert r38["avg_mr"] is None
        assert r38["corr_per_bale"] is None and r38["corr_pct"] is None and r38["total_corr"] is None
        # observed side still reported
        assert r38["obs_per_bale"] == pytest.approx(435.86)
        assert r38["obs_pct"] == pytest.approx(-0.26)
        assert len(rep["notes"]) == 1
        assert "1 quality had no MR reading" in rep["notes"][0]
        assert '38"-7.14' in rep["notes"][0]
        # group corr compares Σcorr with the Σstd of the qualities that HAVE a corr
        h = _group(rep, "HESSIAN")
        assert h["total_obs"] == pytest.approx(6441.0)
        assert h["total_corr"] == pytest.approx(r40["total_corr"], abs=0.01)
        assert h["corr_pct"] == pytest.approx(-0.59)
        assert h["obs_pct"] == pytest.approx(0.96)

    def test_all_qualities_without_mr_gives_null_group_corr(self):
        rep = build_heavy_light([_day(Q40, D, 9, 3390.0, 369.0)], [], [])
        h = _group(rep, "HESSIAN")
        assert h["total_corr"] is None and h["corr_pct"] is None
        assert h["obs_pct"] == pytest.approx(2.08)
        assert _group(rep, "OVERALL")["total_corr"] is None

    def test_partial_coverage_is_noted(self):
        rows = [_day(Q40, D, 9, 3390.0, 369.0), _day(Q40, D2, 3, 1140.0, 369.0)]
        rep = build_heavy_light(rows, [{"entry_date": D, "item_id": 1189, "avg_mr": 19.11}], [])
        r = _row_by_qid(rep, 1)
        assert r["mr_source"] == "item"
        assert r["avg_mr"] == pytest.approx(19.11)  # only the covered day
        assert any("1 of 2 days" in n for n in rep["notes"])

    def test_empty_input(self):
        rep = build_heavy_light([], [], [])
        assert rep["rows"] == []
        assert [g["label"] for g in rep["groups"]] == ["SACKING", "HESSIAN", "OVERALL"]
        assert all(g["bales"] == 0 for g in rep["groups"])
        assert rep["notes"] == []


# =============================================================================
# Daily sheet
# =============================================================================


def _bale(qid, q, sl, series, bale_no, gross, net, entry_type="PROD", original=None,
          spell="A1", trolly="T-61", std=369.0, packsheet=5.0, weigh=None):
    return {
        "bale_entry_id": qid * 100 + sl,
        "entry_type": entry_type,
        "finishing_quality_id": qid,
        "item_id": q["item_id"],
        "item_name": q["item_name"],
        "quality_type": q["quality_type"],
        "std_bale_wt": std,
        "quality_std_bale_wt": 999.0,  # master's current value — must NOT win over the snapshot
        "packsheet_wt": packsheet,
        "quality_packsheet_wt": 7.0,
        "bale_series_id": 1,
        "series_name": series,
        "bale_no": bale_no,
        "original_bale_no": original,
        "spell_id": 1,
        "spell_code": spell,
        "machine_id": 2,
        "gross_wt": gross,
        "trolly_id": 61,
        "trolly_name": trolly,
        "trolly_wt": 0.0,
        "net_wt": net,
        "weigh_time": weigh or datetime(2026, 1, 27, 8, sl, 0),
        "remarks": None,
    }


SHEET_ROWS = [
    _bale(1, Q40, 1, '40"-6.50', 201, 380.0, 375.0),
    _bale(1, Q40, 2, '40"-6.50', 202, 370.0, 365.0),
    _bale(1, Q40, 3, '40"-6.50', 203, 372.0, 367.0, entry_type="REPACK", original=150),
    _bale(3, QSACK, 1, "PUNJAB", 24747, 294.0, 289.0, std=290.0),
    _bale(3, QSACK, 2, "PUNJAB", 24748, 296.0, 291.0, std=290.0),
]


class TestDailySheetBuilder:
    def test_blocks_grouped_by_quality_with_sl_no_restarting(self):
        sheet = build_daily_sheet(SHEET_ROWS)
        assert [b["finishing_quality_id"] for b in sheet["blocks"]] == [1, 3]
        b40 = sheet["blocks"][0]
        assert [r["sl_no"] for r in b40["rows"]] == [1, 2, 3]
        assert [r["sl_no"] for r in sheet["blocks"][1]["rows"]] == [1, 2]
        assert b40["item_name"] == '40"-6.50' and b40["quality_type"] == 1
        # snapshot wins over the master's current value
        assert b40["std_bale_wt"] == pytest.approx(369.0)
        assert b40["packsheet_wt"] == pytest.approx(5.0)

    def test_row_shape(self):
        b40 = build_daily_sheet(SHEET_ROWS)["blocks"][0]
        first = b40["rows"][0]
        for key in ("sl_no", "entry_type", "series_name", "bale_no", "original_bale_no",
                    "spell_code", "gross_wt", "trolly_name", "net_wt", "weigh_time"):
            assert key in first
        assert first["entry_type"] == "PROD" and first["original_bale_no"] is None
        assert first["weigh_time"] == "2026-01-27T08:01:00"
        repack = b40["rows"][2]
        assert repack["entry_type"] == "REPACK"
        assert repack["original_bale_no"] == 150 and repack["bale_no"] == 203

    def test_block_footer_counts_repack_separately(self):
        b40 = build_daily_sheet(SHEET_ROWS)["blocks"][0]
        assert b40["prod_bales"] == 2 and b40["repack_bales"] == 1
        # production totals are PROD rows only
        assert b40["total_gross"] == pytest.approx(750.0)
        assert b40["total_net"] == pytest.approx(740.0)
        assert b40["repack_total_gross"] == pytest.approx(372.0)
        assert b40["repack_total_net"] == pytest.approx(367.0)
        assert b40["avg_net"] == pytest.approx(370.0)
        assert b40["dev_pct"] == pytest.approx((370.0 - 369.0) / 369.0 * 100, abs=0.005)

    def test_grand_footers_by_type_and_overall(self):
        totals = build_daily_sheet(SHEET_ROWS)["totals"]
        assert [t["quality_type"] for t in totals["by_type"]] == [1, 2]
        hess, sack = totals["by_type"]
        assert (hess["prod_bales"], hess["repack_bales"]) == (2, 1)
        assert hess["total_net"] == pytest.approx(740.0)
        assert (sack["prod_bales"], sack["repack_bales"]) == (2, 0)
        assert sack["total_gross"] == pytest.approx(590.0)
        assert sack["total_net"] == pytest.approx(580.0)
        o = totals["overall"]
        assert (o["prod_bales"], o["repack_bales"]) == (4, 1)
        assert o["total_gross"] == pytest.approx(1340.0)
        assert o["total_net"] == pytest.approx(1320.0)

    def test_dev_pct_none_without_std_or_prod_rows(self):
        rows = [_bale(1, Q40, 1, "X", 1, 380.0, 375.0, std=None)]
        rows[0]["quality_std_bale_wt"] = None
        b = build_daily_sheet(rows)["blocks"][0]
        assert b["std_bale_wt"] is None and b["dev_pct"] is None
        assert b["avg_net"] == pytest.approx(375.0)
        # only REPACK rows -> no production average
        rows = [_bale(1, Q40, 1, "X", 1, 380.0, 375.0, entry_type="REPACK", original=9)]
        b = build_daily_sheet(rows)["blocks"][0]
        assert b["prod_bales"] == 0 and b["avg_net"] is None and b["dev_pct"] is None
        assert b["repack_bales"] == 1

    def test_master_values_used_when_rows_have_no_snapshot(self):
        rows = [_bale(1, Q40, 1, "X", 1, 380.0, 375.0, std=None, packsheet=None)]
        b = build_daily_sheet(rows)["blocks"][0]
        assert b["std_bale_wt"] == pytest.approx(999.0)
        assert b["packsheet_wt"] == pytest.approx(7.0)

    def test_empty_day(self):
        sheet = build_daily_sheet([])
        assert sheet["blocks"] == []
        assert sheet["totals"]["by_type"] == []
        assert sheet["totals"]["overall"] == {
            "prod_bales": 0, "repack_bales": 0, "total_gross": 0.0, "total_net": 0.0,
        }


# =============================================================================
# Endpoints (TestClient, mocked session)
# =============================================================================


def _mapping_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _result(rows=None, one=None):
    res = MagicMock()
    res.fetchall.return_value = rows or []
    res.fetchone.return_value = one
    return res


@pytest.fixture()
def overridden_client():
    mock_db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: mock_db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    yield client, mock_db
    app.dependency_overrides.clear()


BRANCH_ROW = _mapping_row({"branch_id": 2, "branch_name": "Factory"})


class TestDailySheetEndpoint:
    def test_requires_params(self, overridden_client):
        c, _ = overridden_client
        assert c.get(SHEET_URL).status_code == 400
        assert c.get(SHEET_URL, params={"co_id": 1}).status_code == 400
        r = c.get(SHEET_URL, params={"co_id": 1, "branch_id": 2})
        assert r.status_code == 400 and "tran_date" in r.json()["detail"]

    def test_rejects_bad_values(self, overridden_client):
        c, _ = overridden_client
        base = {"co_id": 1, "branch_id": 2, "tran_date": "2026-01-27"}
        assert c.get(SHEET_URL, params={**base, "tran_date": "27-01-2026"}).status_code == 400
        assert c.get(SHEET_URL, params={**base, "co_id": "abc"}).status_code == 400
        assert c.get(SHEET_URL, params={**base, "spell_id": "x"}).status_code == 400
        r = c.get(SHEET_URL, params={**base, "entry_type": "BOGUS"})
        assert r.status_code == 400 and "entry_type" in r.json()["detail"]

    def test_rejects_branch_not_in_company(self, overridden_client):
        c, db = overridden_client
        db.execute.return_value = _result(one=None)
        r = c.get(SHEET_URL, params={"co_id": 1, "branch_id": 99, "tran_date": "2026-01-27"})
        assert r.status_code == 400
        assert r.json()["detail"] == BAD_BRANCH_MSG

    def test_success_shape_and_binds(self, overridden_client):
        c, db = overridden_client
        db.execute.side_effect = [
            _result(one=BRANCH_ROW),
            _result(rows=[_mapping_row(r) for r in SHEET_ROWS]),
        ]
        r = c.get(
            SHEET_URL,
            params={"co_id": 1, "branch_id": 2, "tran_date": "2026-01-27", "entry_type": "prod"},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["tran_date"] == "2026-01-27"
        assert data["branch_name"] == "Factory"
        assert data["entry_type"] == "PROD"
        assert len(data["blocks"]) == 2
        assert data["totals"]["overall"]["prod_bales"] == 4
        # the rows query received typed binds, None for the optional spell
        binds = db.execute.call_args_list[1].args[1]
        assert binds["co_id"] == 1 and binds["branch_id"] == 2
        assert binds["tran_date"] == date(2026, 1, 27)
        assert binds["spell_id"] is None and binds["entry_type"] == "PROD"


class TestHeavyLightEndpoint:
    BASE = {"co_id": 1, "branch_id": 2, "date_from": "2026-01-01", "date_to": "2026-01-27"}

    def test_requires_params(self, overridden_client):
        c, _ = overridden_client
        assert c.get(HYLT_URL).status_code == 400
        assert c.get(HYLT_URL, params={"co_id": 1, "branch_id": 2}).status_code == 400
        r = c.get(HYLT_URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-01-01"})
        assert r.status_code == 400 and "date_to" in r.json()["detail"]

    def test_rejects_bad_dates(self, overridden_client):
        c, _ = overridden_client
        assert c.get(HYLT_URL, params={**self.BASE, "date_from": "01/01/2026"}).status_code == 400
        r = c.get(HYLT_URL, params={**self.BASE, "date_to": "2025-12-31"})
        assert r.status_code == 400
        assert "before" in r.json()["detail"]
        r = c.get(HYLT_URL, params={**self.BASE, "date_from": "2024-01-01"})
        assert r.status_code == 400
        assert str(MAX_RANGE_DAYS) in r.json()["detail"]

    def test_rejects_branch_not_in_company(self, overridden_client):
        c, db = overridden_client
        db.execute.return_value = _result(one=None)
        r = c.get(HYLT_URL, params=self.BASE)
        assert r.status_code == 400
        assert r.json()["detail"] == BAD_BRANCH_MSG

    def test_success_shape_and_prod_only_bind(self, overridden_client):
        c, db = overridden_client
        db.execute.side_effect = [
            _result(one=_mapping_row({"branch_id": 2})),
            _result(rows=[_mapping_row(r) for r in HESSIAN_DAY_ROWS]),
            _result(rows=[_mapping_row(r) for r in MR_ITEM_ROWS]),
            _result(rows=[]),
        ]
        r = c.get(HYLT_URL, params=self.BASE)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["date_from"] == "2026-01-01" and data["date_to"] == "2026-01-27"
        assert {k for k in ("rows", "groups", "notes")} <= set(data)
        assert data["rows"][0]["obs_per_bale"] == pytest.approx(376.67)
        assert data["rows"][0]["corr_per_bale"] == pytest.approx(366.83)
        assert [g["label"] for g in data["groups"]] == ["SACKING", "HESSIAN", "OVERALL"]
        day_binds = db.execute.call_args_list[1].args[1]
        assert day_binds["entry_type"] == "PROD"
        assert day_binds["date_from"] == date(2026, 1, 1)
        assert day_binds["date_to"] == date(2026, 1, 27)
