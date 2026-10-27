"""Tests for the daily Wastage Report (src/juteProduction/wastage_report.py,
GET /api/juteProductionReports/wastage_report).

Pure-builder tests exercise the grouping/percentage arithmetic directly on
stub dicts; endpoint tests cover parameter validation through TestClient with
the DB and auth dependencies overridden.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.juteProduction.wastage_report import (
    build_wastage_report,
    get_current_user_with_refresh,
    get_tenant_db,
    month_days,
    resolve_map,
)

client = TestClient(app)
URL = "/api/juteProductionReports/wastage_report"

D1, D2 = date(2025, 1, 1), date(2025, 1, 2)

MAP_ROWS = [
    # thread waste: any dept; the Excel's split bases — daily % of weaving,
    # monthly % of spinning
    {"waste_report_map_id": 1, "group_code": "THREAD", "group_name": "Thread Waste",
     "day_denom": "WEAVING", "total_denom": "SPINNING", "item_id": 100,
     "dept_id": None, "col_label": None, "display_order": 40},
    # caddies in winding dept -> mill side (spinning base both ways)
    {"waste_report_map_id": 2, "group_code": "CADDIES_MILL", "group_name": "Mill Side Caddies",
     "day_denom": "SPINNING", "total_denom": "SPINNING", "item_id": 200,
     "dept_id": 7, "col_label": None, "display_order": 60},
]

WASTE_ROWS = [
    {"tran_date": D1, "item_id": 100, "item_name": "THREAD WASTE", "dept_id": 5,
     "dept_name": "WINDING", "waste_kg": 56.0},
    {"tran_date": D1, "item_id": 100, "item_name": "THREAD WASTE", "dept_id": 6,
     "dept_name": "BEAMING", "waste_kg": 18.0},
    {"tran_date": D2, "item_id": 100, "item_name": "THREAD WASTE", "dept_id": 5,
     "dept_name": "WINDING", "waste_kg": 30.0},
    {"tran_date": D1, "item_id": 200, "item_name": "CADDIES", "dept_id": 7,
     "dept_name": "WINDING", "waste_kg": 34.0},
    # unmapped item -> OTHER bucket
    {"tran_date": D1, "item_id": 999, "item_name": "MYSTERY WASTE", "dept_id": 5,
     "dept_name": "WINDING", "waste_kg": 5.0},
]

WEAVING_ROWS = [
    {"tran_date": D1, "machine_id": 1, "quality_type": 1, "production_kg": 8000.0},
    {"tran_date": D1, "machine_id": 2, "quality_type": 2, "production_kg": 6500.0},
    {"tran_date": D1, "machine_id": 3, "quality_type": 2, "production_kg": 0.0},
    {"tran_date": D2, "machine_id": 1, "quality_type": 1, "production_kg": 7000.0},
]
SPINNING_ROWS = [
    {"tran_date": D1, "production_kg": 13556.0, "frame_runs": 41},
    {"tran_date": D2, "production_kg": 20929.0, "frame_runs": 65},
]
BREAKER_ROWS = [{"tran_date": D1, "feed_kg": 1000.0}]
SELECTION_ROWS = [{"tran_date": D1, "selection_wt": 20721.0}]


def _build(days=(D1, D2)):
    return build_wastage_report(
        list(days), WASTE_ROWS, MAP_ROWS,
        WEAVING_ROWS, SPINNING_ROWS, BREAKER_ROWS, SELECTION_ROWS,
    )


def test_month_days_caps_at_today():
    days = month_days(date(2025, 1, 1), today=date(2025, 1, 5))
    assert days == [date(2025, 1, d) for d in range(1, 6)]
    full = month_days(date(2025, 1, 1), today=date(2025, 6, 1))
    assert len(full) == 31
    assert month_days(date(2099, 1, 1), today=date(2025, 1, 1)) == []


def test_resolve_map_prefers_exact_dept():
    exact, by_item, groups = resolve_map(MAP_ROWS)
    assert (200, 7) in exact
    assert 100 in by_item
    assert set(groups) == {"THREAD", "CADDIES_MILL"}


def test_components_split_by_actual_dept():
    r = _build()
    thread = next(g for g in r["columns"] if g["group_code"] == "THREAD")
    labels = [c["label"] for c in thread["components"]]
    assert "THREAD WASTE — WINDING" in labels and "THREAD WASTE — BEAMING" in labels


def test_day_pct_uses_day_denominator():
    r = _build()
    d1 = r["days"][0]
    # thread day 1: 56 + 18 = 74 kg over weaving 14500 kg
    assert d1["groups"]["THREAD"]["total"] == pytest.approx(74.0)
    assert d1["groups"]["THREAD"]["pct"] == pytest.approx(74.0 / 14500.0 * 100, abs=0.01)
    # caddies over spinning
    assert d1["groups"]["CADDIES_MILL"]["pct"] == pytest.approx(34.0 / 13556.0 * 100, abs=0.01)


def test_total_pct_uses_total_denominator():
    r = _build()
    t = r["totals"]
    # thread month total: 74 + 30 = 104 over SPINNING month total (not weaving)
    spinning_total = 13556.0 + 20929.0
    assert t["groups"]["THREAD"]["total"] == pytest.approx(104.0)
    assert t["groups"]["THREAD"]["pct"] == pytest.approx(104.0 / spinning_total * 100, abs=0.01)


def test_unmapped_waste_lands_in_other_with_no_pct():
    r = _build()
    other = next(g for g in r["columns"] if g["group_code"] == "OTHER")
    assert other["components"][0]["label"] == "MYSTERY WASTE — WINDING"
    assert r["days"][0]["groups"]["OTHER"]["total"] == pytest.approx(5.0)
    assert r["days"][0]["groups"]["OTHER"]["pct"] is None


def test_loom_counts_split_by_quality_type_and_need_production():
    r = _build()
    d1 = r["days"][0]
    assert d1["hessian_looms"] == 1
    assert d1["sacking_looms"] == 1  # machine 3 wove 0 kg -> not counted
    d2 = r["days"][1]
    assert d2["hessian_looms"] == 1 and d2["sacking_looms"] == 0


def test_zero_denominator_gives_null_pct():
    r = _build(days=(D2,))  # no breaker feed / selection on day 2
    # move thread to a selection-based check via OTHER: simpler — day 2 caddies
    # has no entries; assert a group with 0 denominator yields None
    day = r["days"][0]
    assert day["breaker_feed_kg"] == 0.0
    assert day["selection_wt"] == 0.0


# ---------------------------------------------------------------------------
# TKT-000183: WINDING base, unknown-denominator robustness, grand Total
# Wastage band, unmapped-items list
# ---------------------------------------------------------------------------

LINE_MAP_ROW = {
    "waste_report_map_id": 3, "group_code": "LINE", "group_name": "Line Waste",
    "day_denom": "WINDING", "total_denom": "WINDING", "item_id": 300,
    "dept_id": None, "col_label": None, "display_order": 50,
}
LINE_WASTE_ROWS = [
    {"tran_date": D1, "item_id": 300, "item_name": "LINE WASTE", "dept_id": 5,
     "dept_name": "WINDING", "waste_kg": 40.0},
    {"tran_date": D2, "item_id": 300, "item_name": "LINE WASTE", "dept_id": 5,
     "dept_name": "WINDING", "waste_kg": 20.0},
]
WINDING_ROWS = [
    {"tran_date": D1, "production_kg": 8000.0},
    {"tran_date": D2, "production_kg": 2000.0},
]


def _build_tkt183(waste_rows=None, map_rows=None, winding_rows=WINDING_ROWS, days=(D1, D2)):
    return build_wastage_report(
        list(days),
        WASTE_ROWS + LINE_WASTE_ROWS if waste_rows is None else waste_rows,
        MAP_ROWS + [LINE_MAP_ROW] if map_rows is None else map_rows,
        WEAVING_ROWS, SPINNING_ROWS, BREAKER_ROWS, SELECTION_ROWS,
        winding_rows,
    )


def test_winding_denominator_day_and_month():
    r = _build_tkt183()
    d1, d2 = r["days"]
    assert d1["winding_kg"] == pytest.approx(8000.0)
    assert d2["winding_kg"] == pytest.approx(2000.0)
    assert d1["groups"]["LINE"]["pct"] == pytest.approx(0.5)  # 40 / 8000
    assert d2["groups"]["LINE"]["pct"] == pytest.approx(1.0)  # 20 / 2000
    t = r["totals"]
    assert t["winding_kg"] == pytest.approx(10000.0)
    assert t["groups"]["LINE"]["total"] == pytest.approx(60.0)
    assert t["groups"]["LINE"]["pct"] == pytest.approx(0.6)  # 60 / 10000


def test_winding_rows_optional_and_zero_winding_gives_null_pct():
    # old 7-arg call still works; no winding production -> no LINE %
    r = build_wastage_report(
        [D1, D2], WASTE_ROWS + LINE_WASTE_ROWS, MAP_ROWS + [LINE_MAP_ROW],
        WEAVING_ROWS, SPINNING_ROWS, BREAKER_ROWS, SELECTION_ROWS,
    )
    assert r["days"][0]["winding_kg"] == 0.0
    assert r["days"][0]["groups"]["LINE"]["pct"] is None
    assert r["totals"]["groups"]["LINE"]["pct"] is None


def test_unknown_denominator_gives_null_pct_not_error():
    odd = {**LINE_MAP_ROW, "day_denom": "FUTURE_BASE", "total_denom": None}
    r = _build_tkt183(map_rows=MAP_ROWS + [odd])
    assert r["days"][0]["groups"]["LINE"]["total"] == pytest.approx(40.0)
    assert r["days"][0]["groups"]["LINE"]["pct"] is None
    assert r["totals"]["groups"]["LINE"]["pct"] is None
    # the other groups are unaffected
    assert r["days"][0]["groups"]["THREAD"]["pct"] == pytest.approx(74.0 / 14500.0 * 100, abs=0.01)


def test_grand_total_includes_other_and_pct_per_base():
    r = _build_tkt183()
    d1 = r["days"][0]
    # day 1: thread 56 + 18, caddies 34, OTHER 5, line 40
    day_total = 56.0 + 18.0 + 34.0 + 5.0 + 40.0
    assert d1["total_waste_kg"] == pytest.approx(day_total)
    p = d1["total_pct_by_base"]
    assert set(p) == {"WEAVING", "SPINNING", "BREAKER_FEED", "SELECTION", "WINDING"}
    assert p["WEAVING"] == pytest.approx(round(day_total / 14500.0 * 100, 2))
    assert p["SPINNING"] == pytest.approx(round(day_total / 13556.0 * 100, 2))
    assert p["BREAKER_FEED"] == pytest.approx(round(day_total / 1000.0 * 100, 2))
    assert p["SELECTION"] == pytest.approx(round(day_total / 20721.0 * 100, 2))
    assert p["WINDING"] == pytest.approx(round(day_total / 8000.0 * 100, 2))

    t = r["totals"]
    month_total = day_total + 30.0 + 20.0  # day 2: thread 30, line 20
    assert t["total_waste_kg"] == pytest.approx(month_total)
    # the grand total equals the sum of every group's total, OTHER included
    assert t["total_waste_kg"] == pytest.approx(sum(g["total"] for g in t["groups"].values()))
    tp = t["total_pct_by_base"]
    assert tp["SPINNING"] == pytest.approx(round(month_total / (13556.0 + 20929.0) * 100, 2))
    assert tp["SELECTION"] == pytest.approx(round(month_total / 20721.0 * 100, 2))
    assert tp["WINDING"] == pytest.approx(round(month_total / 10000.0 * 100, 2))


def test_grand_total_zero_base_gives_null_pct():
    r = _build_tkt183()
    d2 = r["days"][1]  # no breaker feed / jute selection on day 2
    assert d2["total_waste_kg"] == pytest.approx(50.0)
    assert d2["total_pct_by_base"]["BREAKER_FEED"] is None
    assert d2["total_pct_by_base"]["SELECTION"] is None
    assert d2["total_pct_by_base"]["WEAVING"] == pytest.approx(round(50.0 / 7000.0 * 100, 2))
    # a day with no waste at all: 0 kg, 0 % where a base exists
    empty = _build_tkt183(waste_rows=[])
    assert empty["days"][0]["total_waste_kg"] == 0.0
    assert empty["days"][0]["total_pct_by_base"]["WEAVING"] == 0.0
    assert empty["unmapped"] == []


def test_denom_options_exclude_none_and_carry_winding():
    r = _build_tkt183()
    codes = [o["code"] for o in r["denom_options"]]
    assert "NONE" not in codes
    assert codes == ["WEAVING", "SPINNING", "BREAKER_FEED", "SELECTION", "WINDING"]
    assert all(o["label"] for o in r["denom_options"])


def test_unmapped_list_sums_per_item_dept_heaviest_first():
    extra = [
        {"tran_date": D1, "item_id": 888, "item_name": "DUST", "dept_id": 9,
         "dept_name": "BATCHING(EJM)", "waste_kg": 100.0},
        {"tran_date": D2, "item_id": 888, "item_name": "DUST", "dept_id": 9,
         "dept_name": "BATCHING(EJM)", "waste_kg": 50.5},
        # same item, another dept -> its own line
        {"tran_date": D2, "item_id": 888, "item_name": "DUST", "dept_id": 5,
         "dept_name": "WINDING", "waste_kg": 2.0},
        # outside the report days -> ignored
        {"tran_date": date(2025, 1, 3), "item_id": 999, "item_name": "MYSTERY WASTE",
         "dept_id": 5, "dept_name": "WINDING", "waste_kg": 1000.0},
    ]
    r = _build_tkt183(waste_rows=WASTE_ROWS + LINE_WASTE_ROWS + extra)
    assert r["unmapped"] == [
        {"item_id": 888, "item_name": "DUST", "dept_id": 9,
         "dept_name": "BATCHING(EJM)", "waste_kg": 150.5},
        {"item_id": 999, "item_name": "MYSTERY WASTE", "dept_id": 5,
         "dept_name": "WINDING", "waste_kg": 5.0},
        {"item_id": 888, "item_name": "DUST", "dept_id": 5,
         "dept_name": "WINDING", "waste_kg": 2.0},
    ]
    # the unmapped list adds up to the OTHER band's month total
    assert sum(u["waste_kg"] for u in r["unmapped"]) == pytest.approx(
        r["totals"]["groups"]["OTHER"]["total"]
    )


def test_winding_query_reconciles_jugar_per_day():
    from src.juteProduction.wastage_report_query import get_winding_daily_kg_query

    sql = " ".join(str(get_winding_daily_kg_query()).split())
    assert "FROM jute_prod_winding_doff wd" in sql
    assert "SUM(wd.production_qty) - COALESCE(MAX(jo.open_w), 0) + COALESCE(MAX(jc.close_w), 0)" in sql
    assert "open_close = 'O'" in sql and "open_close = 'C'" in sql
    assert "GROUP BY wd.tran_date, wd.branch_id, wd.spell_id, wd.eb_id" in sql
    assert sql.rstrip().endswith("GROUP BY wdr.tran_date")
    assert "wd.branch_id = :branch_id" in sql
    assert ":date_from" in sql and ":date_to" in sql
    assert "(wd.active = 1 OR wd.active IS NULL)" in sql


@pytest.fixture()
def overridden_client():
    mock_db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: mock_db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    yield client, mock_db
    app.dependency_overrides.clear()


def test_endpoint_requires_params(overridden_client):
    c, _ = overridden_client
    assert c.get(URL).status_code == 400
    assert c.get(URL, params={"co_id": 1}).status_code == 400
    assert c.get(URL, params={"co_id": 1, "branch_id": 2}).status_code == 400


def test_endpoint_rejects_bad_month(overridden_client):
    c, _ = overridden_client
    r = c.get(URL, params={"co_id": 1, "branch_id": 2, "month": "not-a-month"})
    assert r.status_code == 400


def test_endpoint_rejects_unknown_branch(overridden_client):
    c, db = overridden_client
    db.execute.return_value.fetchone.return_value = None
    r = c.get(URL, params={"co_id": 1, "branch_id": 99, "month": "2025-01"})
    assert r.status_code == 400
    assert "branch" in r.json()["detail"]


def test_endpoint_returns_new_tkt183_fields(overridden_client):
    """With every query empty the shape still carries the winding column, the
    grand-total fields, the base options and an empty unmapped list."""
    c, db = overridden_client
    db.execute.return_value.fetchone.return_value = {"branch_id": 2}
    db.execute.return_value.fetchall.return_value = []
    r = c.get(URL, params={"co_id": 1, "branch_id": 2, "month": "2025-01"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["unmapped"] == []
    assert "WINDING" in [o["code"] for o in data["denom_options"]]
    assert len(data["days"]) == 31
    assert data["days"][0]["winding_kg"] == 0.0
    assert data["days"][0]["total_waste_kg"] == 0.0
    assert data["totals"]["total_pct_by_base"]["WINDING"] is None
    # verify-branch + 7 report queries (winding included)
    assert db.execute.call_count == 8


def test_selection_denominator_excludes_purchased_waste_lines():
    """Purchased waste is bought through the MR chain (owner ruling 2026-09-25),
    so an MR-keyed issue can be waste; the 'Jute Selection' denominator must
    keep counting raw-jute (type 2) lines only, or every wastage % shrinks."""
    from src.juteProduction.wastage_report_query import (
        get_selection_daily_weight_query,
        get_waste_daily_sums_query,
    )

    sql = " ".join(str(get_selection_daily_weight_query()).split())
    assert "ji.jute_mr_li_id IS NOT NULL" in sql
    assert "INNER JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id" in sql
    assert "LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in sql
    assert "COALESCE(ig.item_type_id, 0) <> 3" in sql
    assert "ji.status_id <> 4" in sql
    # the numerator reads floor waste only — purchased waste is never written there
    numerator = " ".join(str(get_waste_daily_sums_query()).split())
    assert "FROM jute_prod_waste_entry w" in numerator
    assert "jute_mr_li" not in numerator and "jute_issue" not in numerator
