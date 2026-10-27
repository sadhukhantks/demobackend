"""Tests for the Jute Gain/Loss Sheet (src/juteProduction/gain_loss.py,
prefix /api/juteGainLoss).

Two tiers, mirroring test_winding_quality_wise_report.py:
  * the pure builders (parse/figures/statement arithmetic) are called directly
    on stub dicts — no DB, no client;
  * parameter validation and wiring go through TestClient with the DB and auth
    dependencies overridden.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.main import app
from src.juteProduction.gain_loss import (
    build_statement,
    figures_map,
    fy_start_of,
    get_current_user_with_refresh,
    get_tenant_db,
    parse_month,
    prev_month_of,
    prod_totals_map,
    ps_scope_ids,
    ps_totals,
)

client = TestClient(app)

SETUP_URL = "/api/juteGainLoss/setup"
SHEET_URL = "/api/juteGainLoss/sheet"
SAVE_URL = "/api/juteGainLoss/save"
REPORT_URL = "/api/juteGainLoss/report"


# =============================================================================
# Pure helpers
# =============================================================================


def test_parse_month_accepts_year_month_and_full_date():
    assert parse_month("2026-01") == date(2026, 1, 1)
    assert parse_month("2026-01-17") == date(2026, 1, 1)


def test_parse_month_rejects_garbage():
    with pytest.raises(HTTPException) as e:
        parse_month("Jan-26")
    assert e.value.status_code == 400


def test_fy_start_is_april():
    assert fy_start_of(date(2026, 1, 1)) == date(2025, 4, 1)
    assert fy_start_of(date(2025, 4, 1)) == date(2025, 4, 1)
    assert fy_start_of(date(2025, 3, 1)) == date(2024, 4, 1)


def test_prev_month_crosses_year():
    assert prev_month_of(date(2026, 1, 1)) == date(2025, 12, 1)
    assert prev_month_of(date(2025, 7, 1)) == date(2025, 6, 1)


def test_figures_map_sums_month_range():
    rows = [
        {"gl_row_id": 1, "hess_qty": 10.0, "sack_qty": 20.0},
        {"gl_row_id": 1, "hess_qty": 1.5, "sack_qty": 2.5},
        {"gl_row_id": 2, "hess_qty": 0.0, "sack_qty": 7.0},
    ]
    m = figures_map(rows)
    assert m[1] == {"hess": 11.5, "sack": 22.5}
    assert m[2] == {"hess": 0.0, "sack": 7.0}


def test_prod_totals_map_is_name_case_insensitive():
    rows = [
        {"statement": "FNG", "quality_type": 1, "quality_name": "Hess 39-6", "qty": 2.0},
        {"statement": "FNG", "quality_type": 1, "quality_name": "HESS 39-6 ", "qty": 3.0},
    ]
    m = prod_totals_map(rows)
    assert m[("FNG", 1, "HESS 39-6")] == 5.0


DEPTS = [
    {"gl_dept_id": 1, "dept_code": "BATCHING", "dept_name": "Batching", "in_wvg": 1, "in_wdg": 0, "display_order": 10},
    {"gl_dept_id": 2, "dept_code": "WINDING", "dept_name": "Winding", "in_wvg": 1, "in_wdg": 1, "display_order": 20},
    {"gl_dept_id": 3, "dept_code": "FINISHING", "dept_name": "Finishing", "in_wvg": 0, "in_wdg": 0, "display_order": 30},
]


def test_ps_scope_per_statement():
    assert ps_scope_ids(DEPTS, "FNG") == [1, 2, 3]
    assert ps_scope_ids(DEPTS, "WVG") == [1, 2]
    assert ps_scope_ids(DEPTS, "WDG") == [2]


def test_ps_totals_only_counts_scope():
    ps = [
        {"gl_dept_id": 1, "opening_hess": 1, "opening_sack": 2, "closing_hess": 3, "closing_sack": 4},
        {"gl_dept_id": 3, "opening_hess": 10, "opening_sack": 10, "closing_hess": 10, "closing_sack": 10},
    ]
    t = ps_totals(ps, [1, 2])
    assert t["opening"] == {"hess": 1.0, "sack": 2.0}
    assert t["closing"] == {"hess": 3.0, "sack": 4.0}


# =============================================================================
# Statement arithmetic
# =============================================================================

ROWS = [
    {"gl_row_id": 10, "row_code": "JUTE_ISSUE", "row_name": "Jute Issue", "section": "INPUT",
     "calc_sign": 1, "in_fng": 1, "in_wvg": 1, "in_wdg": 0, "display_order": 10},
    {"gl_row_id": 11, "row_code": "WINDING_PRODUCTION", "row_name": "Winding Production", "section": "INPUT",
     "calc_sign": 1, "in_fng": 0, "in_wvg": 0, "in_wdg": 1, "display_order": 15},
    {"gl_row_id": 12, "row_code": "YARN_PURCHASE", "row_name": "Yarn Purchase", "section": "INPUT",
     "calc_sign": 1, "in_fng": 1, "in_wvg": 1, "in_wdg": 1, "display_order": 20},
    {"gl_row_id": 13, "row_code": "FRESH_COPS_SOLD", "row_name": "Fresh Cops Sold", "section": "INPUT",
     "calc_sign": -1, "in_fng": 1, "in_wvg": 1, "in_wdg": 1, "display_order": 60},
    {"gl_row_id": 20, "row_code": "SALE_YARN", "row_name": "Sale Yarn", "section": "OUTPUT",
     "calc_sign": 1, "in_fng": 1, "in_wvg": 1, "in_wdg": 1, "display_order": 20},
]

FIGURES = {
    10: {"hess": 100.0, "sack": 300.0},   # jute issue
    11: {"hess": 130.0, "sack": 400.0},   # winding production
    12: {"hess": 0.0, "sack": 16.0},      # yarn purchase
    13: {"hess": 0.0, "sack": 2.0},       # fresh cops sold (Less)
    20: {"hess": 1.0, "sack": 0.0},       # sale yarn (output)
}

PROD = [
    {"statement": "FNG", "quality_type": 1, "quality_name": "H CLOTH 39-6", "qty": 40.0, "display_order": 0},
    {"statement": "FNG", "quality_type": 2, "quality_name": "SACK A", "qty": 350.0, "display_order": 0},
    {"statement": "WVG", "quality_type": 1, "quality_name": "HESSIAN", "qty": 111.0, "display_order": 0},
    {"statement": "WVG", "quality_type": 2, "quality_name": "SACKING 6X7", "qty": 388.0, "display_order": 0},
]

PS = [
    {"gl_dept_id": 1, "opening_hess": 12.0, "opening_sack": 18.0, "closing_hess": 5.0, "closing_sack": 28.0},
    {"gl_dept_id": 2, "opening_hess": 10.0, "opening_sack": 20.0, "closing_hess": 8.0, "closing_sack": 18.0},
    {"gl_dept_id": 3, "opening_hess": 18.0, "opening_sack": 58.0, "closing_hess": 15.0, "closing_sack": 93.0},
]


def _build(statement):
    return build_statement(
        statement, ROWS, DEPTS,
        FIGURES, PROD, PS,
        {}, [], [],          # previous month empty
        FIGURES, prod_totals_map(PROD), PS,   # to-date = current (single month)
    )


def test_fng_net_consumed_and_gain():
    s = _build("FNG")
    # net = issue + yarn purchase - cops + opening(all depts) - closing(all)
    exp_hess = 100.0 + 0.0 - 0.0 + (12 + 10 + 18) - (5 + 8 + 15)
    exp_sack = 300.0 + 16.0 - 2.0 + (18 + 20 + 58) - (28 + 18 + 93)
    net = s["input"]["net_consumed"]
    assert net["hess"] == pytest.approx(exp_hess)
    assert net["sack"] == pytest.approx(exp_sack)
    # output = FNG production + sale yarn
    out = s["output"]["total_output"]
    assert out["hess"] == pytest.approx(40.0 + 1.0)
    assert out["sack"] == pytest.approx(350.0)
    gain = s["gain_loss"]
    assert gain["total"] == pytest.approx(out["total"] - net["total"])
    pct = s["gain_loss_pct"]
    assert pct["total"] == pytest.approx(gain["total"] / net["total"] * 100.0)


def test_wvg_scopes_process_stock_and_uses_weaving_production():
    s = _build("WVG")
    # WVG spans depts 1,2 only — finishing (id 3) excluded
    opening = s["input"]["opening_ps"]
    assert opening["hess"] == pytest.approx(12 + 10)
    assert opening["sack"] == pytest.approx(18 + 20)
    # output comes from the WVG production block
    assert s["output"]["hessian_total"]["hess"] == pytest.approx(111.0)
    assert s["output"]["sacking_total"]["sack"] == pytest.approx(388.0)
    # jute issue row IS included in WVG input
    labels = [r["label"] for r in s["input"]["rows"]]
    assert "Jute Issue" in labels and "Winding Production" not in labels


def test_wdg_uses_winding_input_and_wvg_output():
    s = _build("WDG")
    labels = [r["label"] for r in s["input"]["rows"]]
    assert "Winding Production" in labels and "Jute Issue" not in labels
    # WDG process stock scope = winding only (dept 2)
    assert s["input"]["opening_ps"]["total"] == pytest.approx(10 + 20)
    assert s["input"]["closing_ps"]["total"] == pytest.approx(8 + 18)
    # output block reuses the WVG production
    assert s["output"]["sub_total"]["total"] == pytest.approx(111.0 + 388.0)


def test_zero_consumption_yields_null_pct():
    s = build_statement("FNG", ROWS, DEPTS, {}, [], [], {}, [], [], {}, {}, [])
    assert s["gain_loss_pct"]["total"] is None


def test_process_stock_section_diff_against_prev_closing():
    prev_ps = [
        {"gl_dept_id": 1, "opening_hess": 0, "opening_sack": 0, "closing_hess": 12.0, "closing_sack": 18.0},
    ]
    s = build_statement(
        "FNG", ROWS, DEPTS, FIGURES, PROD, PS, {}, [], prev_ps,
        FIGURES, prod_totals_map(PROD), PS,
    )
    row = next(r for r in s["process_stock"]["rows"] if r["dept_code"] == "BATCHING")
    assert row["prev_total"] == pytest.approx(30.0)
    assert row["diff"] == pytest.approx((5.0 + 28.0) - 30.0)
    # opening (12+18) equals prev closing -> no mismatch flag
    assert row["opening_mismatch"] is False


# =============================================================================
# Endpoint validation (DB + auth overridden)
# =============================================================================


@pytest.fixture()
def overridden_client():
    mock_db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: mock_db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    yield client, mock_db
    app.dependency_overrides.clear()


def test_setup_requires_co_id(overridden_client):
    c, _ = overridden_client
    assert c.get(SETUP_URL).status_code == 400


def test_sheet_requires_month(overridden_client):
    c, _ = overridden_client
    r = c.get(SHEET_URL, params={"co_id": 1, "branch_id": 2})
    assert r.status_code == 400
    assert "month" in r.json()["detail"]


def test_sheet_rejects_bad_month(overridden_client):
    c, _ = overridden_client
    r = c.get(SHEET_URL, params={"co_id": 1, "branch_id": 2, "month": "26-01"})
    assert r.status_code == 400


def _save_body(**over):
    body = {
        "co_id": 1,
        "branch_id": 2,
        "month": "2026-01",
        "figures": [],
        "productions": [],
        "process_stock": [],
    }
    body.update(over)
    return body


def test_save_rejects_future_month(overridden_client):
    c, _ = overridden_client
    r = c.post(SAVE_URL, json=_save_body(month="2099-01"))
    assert r.status_code == 400


def test_save_rejects_bad_statement(overridden_client):
    c, _ = overridden_client
    body = _save_body(productions=[
        {"statement": "XXX", "quality_type": 1, "quality_name": "Q", "qty": 1}
    ])
    r = c.post(SAVE_URL, json=body)
    assert r.status_code == 400
    assert "statement" in r.json()["detail"]


def test_save_rejects_bad_quality_type(overridden_client):
    c, _ = overridden_client
    body = _save_body(productions=[
        {"statement": "FNG", "quality_type": 9, "quality_name": "Q", "qty": 1}
    ])
    assert c.post(SAVE_URL, json=body).status_code == 400


def test_save_rejects_blank_quality_name(overridden_client):
    c, _ = overridden_client
    body = _save_body(productions=[
        {"statement": "FNG", "quality_type": 1, "quality_name": "  ", "qty": 1}
    ])
    assert c.post(SAVE_URL, json=body).status_code == 400


def test_save_rejects_absurd_qty(overridden_client):
    c, _ = overridden_client
    body = _save_body(figures=[{"gl_row_id": 1, "hess_qty": 1e9, "sack_qty": 0}])
    assert c.post(SAVE_URL, json=body).status_code == 400


def test_report_requires_params(overridden_client):
    c, _ = overridden_client
    assert c.get(REPORT_URL).status_code == 400
    assert c.get(REPORT_URL, params={"co_id": 1}).status_code == 400
