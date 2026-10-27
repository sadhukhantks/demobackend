"""Daily P&L report (src/juteCosting/daily_pl.py, GET /api/juteCostReports/daily_pl).

The arithmetic is pure (build_day_inputs -> money_for_day -> build_daily_pl) and
exercised on stub rows; validation and the query wiring go through TestClient
on a throwaway app, as test_juteprod_spinning_trend.py does.
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pymysql.err
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, ProgrammingError

from src.juteCosting.daily_pl import (
    MAX_RANGE_DAYS,
    STD_WINDOW_DAYS,
    build_daily_pl,
    build_day_inputs,
    get_current_user_with_refresh,
    get_tenant_db,
    money_for_day,
    product_of,
    router,
)
from src.juteCosting.daily_pl_query import get_dust_daily_query, get_jute_issue_daily_query
from src.juteCosting.params import resolve_params

URL = "/api/juteCostReports/daily_pl"

app = FastAPI()
app.include_router(router, prefix="/api/juteCostReports")
client = TestClient(app)

D = date(2026, 7, 12)


def _p(cost_param_id, code, value, eff=date(2026, 1, 1), product=None, branch_id=2, uom=None):
    return {
        "cost_param_id": cost_param_id,
        "co_id": 1,
        "branch_id": branch_id,
        "effective_date": eff,
        "param_code": code,
        "param_value": value,
        "uom": uom,
        "product": product,
        "source": None,
        "remarks": None,
        "active": 1,
    }


BASE_PARAMS = [
    _p(1, "REALISATION_PER_T", 182000, product="SACKING", uom="₹/t"),
    _p(2, "REALISATION_PER_T", 210000, product="HESSIAN", uom="₹/t"),
    _p(3, "WAGE_PER_HAND_DAY", 400, uom="₹/hand-day"),
    _p(4, "DUST_SALE_PER_KG", 1, uom="₹/kg"),
]


def _inputs(d=D, **over):
    """One hand-calculated day; keyword overrides replace a row list."""
    rows = {
        "cloth_rows": [
            {"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": 10},
            {"tran_date": d, "weaving_quality_name": "B-Twill 37 x 22.5", "production_mt": 2},
        ],
        "yarn_rows": [{"tran_date": d, "production_kg": 11500}],
        "hands_rows": [{"tran_date": d, "hours": 4000, "hands": 500}],
        "spindle_rows": [{"tran_date": d, "spindle_hours": 2900}],
        "jute_rows": [{"tran_date": d, "issued_kg": 6000, "issue_value": 900000}],
        "stores_rows": [{"tran_date": d, "stores_value": 50000}],
        "dust_rows": [{"tran_date": d, "dust_kg": 100}],
        "purchased_rows": [{"tran_date": d, "purchased_kg": 500}],
        "power_rows": None,
    }
    rows.update(over)
    return build_day_inputs(d, d, **rows)


def _nk(data_or_m):
    items = data_or_m["not_keyed"]
    return {x["key"]: x["reason"] for x in items}


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


class TestProductOf:
    def test_mapping(self):
        assert product_of("SACK A") == "SACKING"
        assert product_of("S4 twill") == "SACKING"
        assert product_of("sack b") == "SACKING"
        assert product_of("B-Twill 37 x 22.5") == "HESSIAN"
        assert product_of("Hessian 10 oz") == "HESSIAN"
        assert product_of(None) == "HESSIAN"


class TestHandCalculatedDay:
    def test_inputs(self):
        inp = _inputs()[D]
        assert inp["sacking_t"] == 10.0 and inp["hessian_t"] == 2.0 and inp["cloth_t"] == 12.0
        assert inp["yarn_t"] == 11.5 and inp["purchased_yarn_t"] == 0.5
        assert inp["hands"] == 500.0 and inp["spindle_hours"] == 2900.0
        assert inp["jute_issued_t"] == 6.0 and inp["jute_issue_value"] == 900000.0
        assert inp["power_kwh"] is None

    def test_money(self):
        day = build_daily_pl(_inputs(), BASE_PARAMS, D, D, D)["days"][0]
        m = day["money"]
        assert m["realisation"] == 2240000.0
        assert m["jute_cost"] == 900000.0
        assert m["wages"] == 200000.0
        assert m["stores"] == 50000.0
        assert m["dust_credit"] == 100.0
        assert m["power"] is None
        assert m["jute_waste_cost"] == 0.0
        assert m["contribution"] == 1090100.0
        # Nothing keyed for overheads: estimated at ₹2,000/t x 12 t (flagged).
        assert m["overheads"] == 24000.0 and m["operating_profit"] == 1066100.0
        assert m["capital_cost"] is None and m["net"] is None
        assert day["flags"] == {
            "power_excluded": True,
            "realisation_partial": False,
            "dg_at_grid_rate": False,
            "solar_at_zero": False,
            "overheads_estimated": True,
        }
        assert day["date"] == "2026-07-12" and day["weekday"] == "Sun"
        assert day["cloth_t"] == 12.0 and day["yarn_surplus_t"] == 0.0

    def test_per_lines(self):
        day = build_daily_pl(_inputs(), BASE_PARAMS, D, D, D)["days"][0]
        assert day["per_t_cloth"]["contribution"] == 90841.67
        assert day["per_t_cloth"]["realisation"] == round(2240000 / 12, 2)
        assert day["per_t_cloth"]["power"] is None
        assert day["per_t_yarn"]["contribution"] == round(1090100 / 11.5, 2)
        assert day["per_100_spindles"]["contribution_per_100_spindle_hours"] == 37589.66
        assert day["per_100_spindles"]["contribution_per_100_spindle_days"] == round(
            1090100 / (2900 / 24 / 100), 2
        )

    def test_params_used_one_entry_per_pick(self):
        data = build_daily_pl(_inputs(), BASE_PARAMS, D, D, D)
        used = {p["cost_param_id"]: p for p in data["params_used"]}
        assert set(used) == {1, 2, 3, 4}
        assert used[3] == {
            "param_code": "WAGE_PER_HAND_DAY",
            "param_label": "Wage per hand-day",
            "product": None,
            "value": 400.0,
            "uom": "₹/hand-day",
            "effective_date": "2026-01-01",
            "branch_id": 2,
            "scope": "branch",
            "source": None,
            "cost_param_id": 3,
            "days_used": 1,
            "used_in": ["days"],
        }

    def test_not_keyed_order_overheads_and_capital(self):
        data = build_daily_pl(_inputs(), BASE_PARAMS, D, D, D)
        keys = [x["key"] for x in data["not_keyed"]]
        assert keys == ["power", "overheads", "capital"]
        nk = _nk(data)
        assert nk["power"] == "power: not keyed — jute_power_reading not available"
        assert nk["overheads"] == "overheads not keyed — estimated at the ₹2,000/t rule of thumb"
        assert nk["capital"] == (
            "jute stock value and finished goods value not available on this tenant — "
            "capital cost and net blank"
        )

    def test_mtd_block(self):
        mtd = build_daily_pl(_inputs(), BASE_PARAMS, D, D, D)["mtd"]
        assert mtd["label"] == "Period"  # the 12th is not the 1st
        assert mtd["cloth_t"] == 12.0 and mtd["producing_days"] == 1 and mtd["calendar_days"] == 1
        assert mtd["money"]["contribution"] == 1090100.0
        assert mtd["contribution_per_producing_day"] == 1090100.0
        assert mtd["per_t_cloth"]["contribution"] == 90841.67
        assert mtd["power_kwh"] is None and mtd["purchased_yarn_t"] == 0.5
        assert mtd["unpriced_days"] == 0 and mtd["flags"]["contribution_partial"] is False
        assert mtd["jute_unvalued_t"] == 0.0


def _jute(d, issued_kg, issue_value, unvalued_kg=0, mr_rated_kg=0, mr_rate_value=0):
    """One get_jute_issue_daily_query row."""
    return {
        "tran_date": d,
        "issued_kg": issued_kg,
        "issue_value": issue_value,
        "unvalued_kg": unvalued_kg,
        "mr_rated_kg": mr_rated_kg,
        "mr_rate_value": mr_rate_value,
    }


class TestJuteCost:
    RATE = [_p(5, "JUTE_RATE_PER_KG", 60, uom="₹/kg")]

    def test_rate_used_only_when_issue_value_is_zero(self):
        params = BASE_PARAMS + self.RATE
        with_value = build_daily_pl(_inputs(), params, D, D, D)["days"][0]
        assert with_value["money"]["jute_cost"] == 900000.0
        no_value = build_daily_pl(
            _inputs(jute_rows=[_jute(D, 6000, 0, unvalued_kg=6000)]), params, D, D, D
        )["days"][0]
        assert no_value["money"]["jute_cost"] == 6 * 60 * 1000.0
        assert no_value["jute_unvalued_t"] == 6.0

    def test_mixed_day_prices_the_unvalued_lines_too(self):
        # 4,000 kg valued at 5,00,000 and 2,000 kg with no value: the keyed
        # rate prices the 2,000 kg (never the whole day's value-or-rate).
        day = build_daily_pl(
            _inputs(jute_rows=[_jute(D, 6000, 500000, unvalued_kg=2000)]),
            BASE_PARAMS + self.RATE, D, D, D,
        )["days"][0]
        assert day["money"]["jute_cost"] == 500000.0 + 2000 * 60.0

    def test_mr_rate_prices_unvalued_lines_when_no_rate_is_keyed(self):
        data = build_daily_pl(
            _inputs(jute_rows=[_jute(D, 6000, 500000, unvalued_kg=2000, mr_rated_kg=2000, mr_rate_value=110000)]),
            BASE_PARAMS, D, D, D,
        )
        day = data["days"][0]
        assert day["money"]["jute_cost"] == 610000.0
        assert "JUTE_RATE_PER_KG" not in _nk(data)
        assert 5 not in {p["cost_param_id"] for p in data["params_used"]}

    def test_keyed_rate_overrides_the_mr_rate(self):
        data = build_daily_pl(
            _inputs(jute_rows=[_jute(D, 6000, 500000, unvalued_kg=2000, mr_rated_kg=2000, mr_rate_value=110000)]),
            BASE_PARAMS + self.RATE, D, D, D,
        )
        assert data["days"][0]["money"]["jute_cost"] == 500000.0 + 120000.0
        assert 5 in {p["cost_param_id"] for p in data["params_used"]}

    def test_weight_no_rate_can_price_blanks_jute_cost(self):
        # 2,000 kg unvalued, only 1,500 kg of it carries an MR rate, no param.
        data = build_daily_pl(
            _inputs(jute_rows=[_jute(D, 6000, 500000, unvalued_kg=2000, mr_rated_kg=1500, mr_rate_value=80000)]),
            BASE_PARAMS, D, D, D,
        )
        day = data["days"][0]
        assert day["money"]["jute_cost"] is None and day["money"]["contribution"] is None
        assert _nk(data)["JUTE_RATE_PER_KG"] == (
            "jute issued with no booked value and no MR rate, and no jute rate keyed — jute cost blank"
        )

    def test_no_rate_and_no_value_blanks_jute_cost(self):
        data = build_daily_pl(
            _inputs(jute_rows=[_jute(D, 6000, 0, unvalued_kg=6000)]), BASE_PARAMS, D, D, D
        )
        day = data["days"][0]
        assert day["money"]["jute_cost"] is None and day["money"]["contribution"] is None
        assert "JUTE_RATE_PER_KG" in _nk(data)

    def test_query_returns_the_unvalued_split(self):
        sql = str(get_jute_issue_daily_query())
        assert "AS unvalued_kg" in sql and "AS mr_rated_kg" in sql and "AS mr_rate_value" in sql
        assert "/ 100 * mrli.rate" in sql  # mrli.rate is ₹/qtl

    def test_nothing_issued_is_zero_not_blank(self):
        day = build_daily_pl(_inputs(jute_rows=[]), BASE_PARAMS, D, D, D)["days"][0]
        assert day["money"]["jute_cost"] == 0.0 and day["money"]["contribution"] is not None


class TestPower:
    def test_table_missing(self):
        data = build_daily_pl(_inputs(power_rows=None), BASE_PARAMS, D, D, D)
        assert _nk(data)["power"] == "power: not keyed — jute_power_reading not available"
        assert data["days"][0]["power_kwh"] is None

    def test_rate_missing(self):
        data = build_daily_pl(
            _inputs(power_rows=[{"tran_date": D, "power_kwh": 1000}]), BASE_PARAMS, D, D, D
        )
        day = data["days"][0]
        assert day["power_kwh"] == 1000.0 and day["money"]["power"] is None
        assert day["flags"]["power_excluded"] is True
        assert day["money"]["contribution"] == 1090100.0  # computed WITHOUT power
        assert _nk(data)["power"] == "power: not keyed — POWER_RATE_PER_KWH missing"

    def test_priced(self):
        params = BASE_PARAMS + [_p(6, "POWER_RATE_PER_KWH", 8, uom="₹/kWh")]
        data = build_daily_pl(
            _inputs(power_rows=[{"tran_date": D, "power_kwh": 1000}]), params, D, D, D
        )
        day = data["days"][0]
        assert day["money"]["power"] == 8000.0
        assert day["money"]["contribution"] == 1090100.0 - 8000.0
        assert day["flags"]["power_excluded"] is False
        assert "power" not in _nk(data)


class TestRealisation:
    def test_partial_when_a_produced_product_has_no_rate(self):
        params = [p for p in BASE_PARAMS if p["product"] != "HESSIAN"]
        data = build_daily_pl(_inputs(), params, D, D, D)
        day = data["days"][0]
        assert day["money"]["realisation"] == 1820000.0
        assert day["flags"]["realisation_partial"] is True
        assert _nk(data)["REALISATION_PER_T:HESSIAN"] == "no realisation rate for HESSIAN"
        assert day["money"]["contribution"] == 1820000.0 - 900000 - 200000 - 50000 + 100

    def test_null_when_no_product_has_a_rate(self):
        params = [p for p in BASE_PARAMS if p["param_code"] != "REALISATION_PER_T"]
        day = build_daily_pl(_inputs(), params, D, D, D)["days"][0]
        assert day["money"]["realisation"] is None and day["money"]["contribution"] is None

    def test_all_product_fallback(self):
        params = [p for p in BASE_PARAMS if p["param_code"] != "REALISATION_PER_T"]
        params.append(_p(9, "REALISATION_PER_T", 200000, product=None))
        day = build_daily_pl(_inputs(), params, D, D, D)["days"][0]
        assert day["money"]["realisation"] == 12 * 200000.0


class TestWagesAndDust:
    def test_wages_missing_blanks_contribution(self):
        params = [p for p in BASE_PARAMS if p["param_code"] != "WAGE_PER_HAND_DAY"]
        data = build_daily_pl(_inputs(), params, D, D, D)
        day = data["days"][0]
        assert day["money"]["wages"] is None and day["money"]["contribution"] is None
        assert "WAGE_PER_HAND_DAY" in _nk(data)

    def test_dust_without_rate_is_zero_credit(self):
        params = [p for p in BASE_PARAMS if p["param_code"] != "DUST_SALE_PER_KG"]
        data = build_daily_pl(_inputs(), params, D, D, D)
        assert data["days"][0]["money"]["dust_credit"] == 0.0
        assert "DUST_SALE_PER_KG" in _nk(data)
        no_dust = build_daily_pl(_inputs(dust_rows=[]), params, D, D, D)
        assert "DUST_SALE_PER_KG" not in _nk(no_dust)

    def test_fixed_cost_is_the_overheads_total(self):
        params = BASE_PARAMS + [_p(7, "FIXED_COST_PER_DAY", 100000)]
        data = build_daily_pl(_inputs(), params, D, D, D)
        m = data["days"][0]["money"]
        assert m["overheads"] == 100000.0 and m["operating_profit"] == 990100.0
        assert m["net"] is None  # capital cost not computable without stock values
        assert "overheads" not in _nk(data) and "FIXED_COST_PER_DAY" not in _nk(data)
        assert data["days"][0]["flags"]["overheads_estimated"] is False


class TestBlankDay:
    def test_ratios_are_none_never_zero(self):
        inputs = build_day_inputs(D, D, [], [], [], [], [], [], [], None, None)
        day = build_daily_pl(inputs, BASE_PARAMS, D, D, D)["days"][0]
        assert day["cloth_t"] == 0.0 and day["money"]["realisation"] == 0.0
        assert day["money"]["contribution"] == 0.0  # 0 - 0 - 0 - 0 + 0
        assert all(v is None for v in day["per_t_cloth"].values())
        assert all(v is None for v in day["per_t_yarn"].values())
        assert all(v is None for v in day["per_100_spindles"].values())
        assert day["purchased_yarn_t"] is None and day["yarn_surplus_t"] == 0.0

    def test_money_for_day_direct(self):
        inp = _inputs()[D]
        m = money_for_day(inp, resolve_params(BASE_PARAMS, D))
        assert m["money"]["contribution"] == 1090100.0
        assert m["flags"]["power_excluded"] is True
        assert {p["cost_param_id"] for p in m["used"]} == {1, 2, 3, 4}


class TestPeriodRollUp:
    """The MTD / period restatement must not divide priced money by unpriced tonnes."""

    def _two_days(self):
        d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
        cloth = [{"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": 10} for d in (d1, d2)]
        hands = [{"tran_date": d, "hours": 800, "hands": 100} for d in (d1, d2)]
        yarn = [{"tran_date": d, "production_kg": 10000} for d in (d1, d2)]
        spindle = [{"tran_date": d, "spindle_hours": 2000} for d in (d1, d2)]
        # Day 1: jute issued with no value, no MR rate and no JUTE_RATE keyed -> unpriced.
        # Day 2: fully priced -> contribution = 10*182000 - 300000 - 40000 = 14,80,000 (1,48,000 /t).
        jute = [_jute(d1, 5000, 0, unvalued_kg=5000), _jute(d2, 5000, 300000)]
        inputs = build_day_inputs(d1, d2, cloth, yarn, hands, spindle, jute, [], [], None, None)
        return build_daily_pl(inputs, BASE_PARAMS, d1, d2, d1)

    def test_per_unit_lines_cover_the_priced_days_only(self):
        data = self._two_days()
        day1, day2 = data["days"]
        assert day1["money"]["contribution"] is None
        assert day2["money"]["contribution"] == 1480000.0 and day2["per_t_cloth"]["contribution"] == 148000.0
        mtd = data["mtd"]
        assert mtd["cloth_t"] == 20.0 and mtd["producing_days"] == 2 and mtd["calendar_days"] == 2
        assert mtd["money"]["contribution"] == 1480000.0  # the priced day
        assert mtd["per_t_cloth"]["contribution"] == 148000.0  # NOT 74,000
        assert mtd["contribution_per_producing_day"] == 1480000.0  # NOT 7,40,000
        assert mtd["per_t_yarn"]["contribution"] == 148000.0
        assert mtd["per_100_spindles"]["contribution_per_100_spindle_hours"] == round(1480000 / 20, 2)
        # Jute cost is priced on day 2 only: 3,00,000 / 10 t, not / 20 t.
        assert mtd["per_t_cloth"]["jute_cost"] == 30000.0
        # Realisation was priced on BOTH days, so its per-tonne spans both.
        assert mtd["money"]["realisation"] == 3640000.0 and mtd["per_t_cloth"]["realisation"] == 182000.0
        assert mtd["unpriced_days"] == 1 and mtd["flags"]["contribution_partial"] is True
        assert mtd["jute_unvalued_t"] == 5.0

    def test_all_days_unpriced(self):
        d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
        cloth = [{"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": 10} for d in (d1, d2)]
        jute = [_jute(d, 5000, 0, unvalued_kg=5000) for d in (d1, d2)]
        inputs = build_day_inputs(d1, d2, cloth, [], [], [], jute, [], [], None, None)
        mtd = build_daily_pl(inputs, BASE_PARAMS, d1, d2, d1)["mtd"]
        assert mtd["money"]["contribution"] is None and mtd["per_t_cloth"]["contribution"] is None
        assert mtd["contribution_per_producing_day"] is None
        assert mtd["unpriced_days"] == 2 and mtd["flags"]["contribution_partial"] is True


class TestDustQuery:
    def test_exact_waste_item_not_a_name_prefix(self):
        # 'DUST MASK' / 'DUST REMOVAL NOZZLE' are store items on both tenants;
        # the dust sold is the jute-waste item named 'Dust' (sls 335884).
        sql = str(get_dust_daily_query())
        assert "UPPER(TRIM(im.item_name)) = 'DUST'" in sql
        assert "LIKE 'DUST%'" not in sql
        assert "ig.item_type_id = 3" in sql and "item_grp_mst" in sql


class TestStdAndRange:
    def _window(self):
        d_to = date(2026, 10, 5)
        d_from = date(2026, 10, 4)
        median_from = d_to - timedelta(days=STD_WINDOW_DAYS - 1)
        producing = {
            date(2026, 9, 10): ("SACK A", 10, 100),   # per t = (10*182000 - 100*400)/10 = 178000
            date(2026, 9, 20): ("SACK A", 5, 100),    # per t = (5*182000 - 40000)/5 = 174000
            date(2026, 10, 4): ("SACK A", 8, 100),    # per t = (8*182000 - 40000)/8 = 177000
            date(2026, 10, 5): ("B-Twill", 4, 100),   # hessian without a rate -> contribution None
        }
        cloth = [
            {"tran_date": d, "weaving_quality_name": q, "production_mt": t}
            for d, (q, t, _) in producing.items()
        ]
        hands = [{"tran_date": d, "hours": h * 8, "hands": h} for d, (_, _, h) in producing.items()]
        inputs = build_day_inputs(median_from, d_to, cloth, [], hands, [], [], [], [], None, None)
        params = [p for p in BASE_PARAMS if p["product"] != "HESSIAN"]
        return build_daily_pl(inputs, params, d_from, d_to, median_from), d_from, d_to

    def test_std_median_uses_days_before_date_from_and_skips_null_days(self):
        data, d_from, d_to = self._window()
        assert [d["date"] for d in data["days"]] == ["2026-10-04", "2026-10-05"]
        std = data["std"]
        assert std["window_from"] == "2026-09-06" and std["window_to"] == "2026-10-05"
        assert std["days_used"] == 3
        assert std["contribution_per_t"] == 177000.0  # median of 178000, 174000, 177000
        assert std["contribution_per_day"] == 1416000.0  # median of 1780000, 870000, 1416000
        assert std["realisation_per_t"] == 182000.0
        assert std["wages_per_t"] == 5000.0  # median of 4000, 8000, 5000
        assert std["contribution_per_100_spindle_hours"] is None  # no spindle rows

    def test_std_empty_when_no_producing_day(self):
        inputs = build_day_inputs(D, D, [], [], [], [], [], [], [], None, None)
        std = build_daily_pl(inputs, BASE_PARAMS, D, D, D)["std"]
        assert std["days_used"] == 0 and std["contribution_per_t"] is None

    def test_param_change_mid_range(self):
        d_from, d_to = date(2026, 10, 1), date(2026, 10, 5)
        params = BASE_PARAMS + [_p(5, "WAGE_PER_HAND_DAY", 420, eff=date(2026, 10, 3))]
        days = [d_from + timedelta(days=i) for i in range(5)]
        hands = [{"tran_date": d, "hours": 800, "hands": 100} for d in days]
        inputs = build_day_inputs(d_from, d_to, [], [], hands, [], [], [], [], None, None)
        data = build_daily_pl(inputs, params, d_from, d_to, d_from)
        wages = [d["money"]["wages"] for d in data["days"]]
        assert wages == [40000.0, 40000.0, 42000.0, 42000.0, 42000.0]
        used = {p["cost_param_id"]: p["days_used"] for p in data["params_used"]}
        assert used[3] == 2 and used[5] == 3
        assert data["mtd"]["money"]["wages"] == sum(wages)
        assert data["mtd"]["label"] == "MTD"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _rows_result(mappings):
    res = MagicMock()
    res.fetchall.return_value = [SimpleNamespace(_mapping=m) for m in mappings]
    return res


def _scope_ok():
    res = MagicMock()
    res.first.return_value = (1,)
    return res


def _scope_bad():
    res = MagicMock()
    res.first.return_value = None
    return res


def _missing_table():
    """What PyMySQL raises for MySQL 1146, wrapped the way SQLAlchemy does."""
    return ProgrammingError(
        "SELECT 1", {}, pymysql.err.ProgrammingError(1146, "Table 'dev3.jute_power_reading' doesn't exist")
    )


def _missing_column():
    """MySQL 1054 (unknown column) — an OperationalError in PyMySQL."""
    return OperationalError("SELECT 1", {}, pymysql.err.OperationalError(1054, "Unknown column 'kwh'"))


def _side_effects(d, power=None, purchased=None, stock=None, fg=None):
    """The documented 14-result order: scope, params, cloth, yarn, hands,
    spindle, jute, stores, dust, purchased, power, waste, jute stock value,
    finished goods (bale summary — only asked for when a branch is given)."""
    return [
        _scope_ok(),
        _rows_result(BASE_PARAMS),
        _rows_result([{"tran_date": d, "weaving_quality_name": "B-Twill", "production_mt": 2}]),
        _rows_result([{"tran_date": d, "production_kg": 2500, "shifts": 3}]),
        _rows_result([{"tran_date": d, "hours": 800, "hands": 100}]),
        _rows_result([{"tran_date": d, "spindle_hours": 2900}]),
        _rows_result([{"tran_date": d, "issued_kg": 3000, "issue_value": 180000}]),
        _rows_result([{"tran_date": d, "stores_value": 5000}]),
        _rows_result([]),
        purchased if purchased is not None else _rows_result([]),
        power if power is not None else _missing_table(),
        _rows_result([]),
        stock if stock is not None else _rows_result([{"value": 55000000, "kg": 630000}]),
        fg if fg is not None else _rows_result([]),
    ]


class TestEndpoint:
    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_validation_before_any_query(self):
        r = client.get(URL)
        assert r.status_code == 400 and r.json()["detail"] == "co_id is required"
        r = client.get(URL, params={"co_id": 1, "date_from": "01-07-2026", "date_to": "2026-07-14"})
        assert r.status_code == 400 and r.json()["detail"] == "Invalid date_from format"
        r = client.get(URL, params={"co_id": 1, "date_from": "2026-07-14", "date_to": "2026-07-01"})
        assert r.status_code == 400 and r.json()["detail"] == "date_to is before date_from"
        r = client.get(URL, params={"co_id": 1, "date_from": "2026-07-01", "date_to": "2026-09-01"})
        assert r.status_code == 400
        assert r.json()["detail"] == f"Date range too large — at most {MAX_RANGE_DAYS} days"
        assert MAX_RANGE_DAYS == 62
        r = client.get(URL, params={"co_id": "x", "date_from": "2026-07-01", "date_to": "2026-07-02"})
        assert r.status_code == 400 and r.json()["detail"] == "Invalid co_id"
        self.db.execute.assert_not_called()

    def test_62_days_accepted(self):
        self.db.execute.side_effect = _side_effects(date(2026, 8, 31))
        r = client.get(
            URL,
            params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-08-31"},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]["days"]) == 62

    def test_branch_not_of_company(self):
        self.db.execute.return_value = _scope_bad()
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-07-14"}
        )
        assert r.status_code == 400 and r.json()["detail"] == "branch_id does not belong to co_id"
        assert self.db.execute.call_count == 1

    def test_happy_path_wiring(self):
        d = date(2026, 7, 12)
        self.db.execute.side_effect = _side_effects(d)
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-07-14"}
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert len(data["days"]) == 14
        assert data["meta"]["date_from"] == "2026-07-01" and data["meta"]["date_to"] == "2026-07-14"
        assert data["meta"]["std_window_from"] == "2026-06-15"
        assert data["meta"]["std_window_days"] == 30 and data["meta"]["branch_id"] == 2
        # Query order and binds.
        calls = self.db.execute.call_args_list
        assert calls[1][0][1] == {"co_id": 1, "branch_id": 2, "on_date": date(2026, 7, 14)}
        assert calls[2][0][1] == {
            "co_id": 1,
            "branch_id": 2,
            "date_from": date(2026, 6, 15),  # median_from = date_to - 29
            "date_to": date(2026, 7, 14),
        }
        yarn_binds = calls[3][0][1]
        assert yarn_binds["spinning_type"] == "Spinning"
        assert yarn_binds["window_from"] == date(2026, 6, 15)
        assert yarn_binds["report_date"] == date(2026, 7, 14)
        assert "daily_doff_tbl" in str(calls[3][0][0])
        assert "jute_power_reading" in str(calls[10][0][0])
        # The one producing day is priced.
        day = {x["date"]: x for x in data["days"]}["2026-07-12"]
        assert day["hessian_t"] == 2.0 and day["yarn_t"] == 2.5
        assert day["money"]["realisation"] == 420000.0
        assert day["money"]["contribution"] == 420000.0 - 180000 - 40000 - 5000
        # Power table missing -> rollback once, not keyed, still 200.
        self.db.rollback.assert_called_once()
        nk = _nk(data)
        assert nk["power"] == "power: not keyed — jute_power_reading not available"
        # Stock values came back, the pct is not keyed -> capital blank for that reason.
        assert "COST_OF_CAPITAL_PCT" in nk and "capital" not in nk
        assert data["capital"]["jute_stock_value"] == 55000000.0
        assert data["capital"]["fg_value"] == 0.0 and data["capital"]["available"] is False
        assert "jute_prod_bale_entry" in str(calls[13][0][0])
        assert calls[13][0][1]["cancelled_status"] == [4, 6]
        assert calls[12][0][1] == {"co_id": 1, "branch_id": 2}
        assert data["statement"]["heads"][0]["key"] == "raw_material"
        notes = data["meta"]["notes"]
        assert any(n.startswith("Product = SACKING") for n in notes)
        assert any("No dust entries" in n for n in notes)
        assert not any("Purchased yarn is not tracked" in n for n in notes)
        assert data["mtd"]["purchased_yarn_t"] == 0.0

    def test_purchased_yarn_table_missing(self):
        d = date(2026, 7, 12)
        self.db.execute.side_effect = _side_effects(
            d,
            purchased=_missing_table(),
            power=_rows_result([]),
        )
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-07-14"}
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert any("Purchased yarn is not tracked" in n for n in data["meta"]["notes"])
        assert data["mtd"]["purchased_yarn_t"] is None
        assert _nk(data)["power"] == "power: not keyed — POWER_RATE_PER_KWH missing"

    def test_unknown_column_is_optional_too(self):
        d = date(2026, 7, 12)
        self.db.execute.side_effect = _side_effects(d, power=_missing_column())
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-07-14"}
        )
        assert r.status_code == 200, r.text
        assert _nk(r.json()["data"])["power"] == "power: not keyed — jute_power_reading not available"

    def test_optional_stock_and_fg_tables_missing(self):
        """vw_jute_stock_outstanding and the bale register both absent (MySQL
        1146): still 200, capital not available, both notes given."""
        d = date(2026, 7, 12)
        self.db.execute.side_effect = _side_effects(d, stock=_missing_table(), fg=_missing_table())
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-07-14"}
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        cap = data["capital"]
        assert cap["available"] is False and cap["jute_stock_value"] is None and cap["fg_value"] is None
        assert cap["notes"] == [NOTE_NO_JUTE_STOCK, NOTE_NO_FG]
        assert NOTE_NO_JUTE_STOCK in data["meta"]["notes"] and NOTE_NO_FG in data["meta"]["notes"]
        assert "capital" in _nk(data) and data["mtd"]["money"]["capital_cost"] is None
        assert self.db.rollback.call_count == 3  # power, jute stock, bale register
        # Finished goods are asked for as on the To date.
        assert self.db.execute.call_args_list[13][0][1]["as_on"] == date(2026, 7, 14)

    @pytest.mark.parametrize(
        "err",
        [
            ProgrammingError("SELECT 1", {}, pymysql.err.ProgrammingError(1064, "You have an error in your SQL syntax")),
            OperationalError("SELECT 1", {}, pymysql.err.OperationalError(2013, "Lost connection to MySQL server")),
            OperationalError("SELECT 1", {}, pymysql.err.OperationalError(1205, "Lock wait timeout exceeded")),
            OperationalError("SELECT 1", {}, Exception("no errno")),
        ],
    )
    def test_other_db_errors_on_optional_queries_are_500(self, err):
        """A real SQL defect / transient fault must not pass for 'power: not keyed'."""
        d = date(2026, 7, 12)
        self.db.execute.side_effect = _side_effects(d, power=err)
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-07-14"}
        )
        assert r.status_code == 500, r.text
        self.db.rollback.assert_called_once()

    def test_syntax_error_on_purchased_yarn_is_500(self):
        d = date(2026, 7, 12)
        self.db.execute.side_effect = _side_effects(
            d,
            purchased=ProgrammingError("SELECT 1", {}, pymysql.err.ProgrammingError(1064, "syntax")),
            power=_rows_result([]),
        )
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-07-14"}
        )
        assert r.status_code == 500

    def test_no_branch_skips_scope_check(self):
        effects = _side_effects(date(2026, 7, 12))[1:]
        self.db.execute.side_effect = effects
        r = client.get(URL, params={"co_id": 1, "date_from": "2026-07-01", "date_to": "2026-07-14"})
        assert r.status_code == 200, r.text
        assert self.db.execute.call_args_list[0][0][1]["branch_id"] is None
        assert r.json()["data"]["meta"]["branch_id"] is None

    def test_generic_error_is_500(self):
        self.db.execute.side_effect = [_scope_ok(), _rows_result(BASE_PARAMS), Exception("boom")]
        r = client.get(
            URL, params={"co_id": 1, "branch_id": 2, "date_from": "2026-07-01", "date_to": "2026-07-14"}
        )
        assert r.status_code == 500 and r.json()["detail"] == "boom"


# ---------------------------------------------------------------------------
# ITM-027: five heads, statement, capital
# ---------------------------------------------------------------------------

from src.juteCosting.daily_pl import (  # noqa: E402
    BENCHMARK_DEFAULTS,
    BENCHMARK_SOURCE,
    CAPITAL_MEMO,
    MONEY_KEYS,
    NOTE_NO_FG,
    NOTE_NO_JUTE_STOCK,
    NOTES_ALWAYS,
    PER_T_CLOTH_KEYS,
    RAW_MATERIAL_BENCH_LABEL_STD,
    RAW_MATERIAL_BENCH_LABEL_UNKEYED,
    RECONCILE_MAX_PER_DAY,
    STORES_CATEGORY_LABELS,
    _reconcile_lines,
    build_capital,
    build_statement,
    capital_param_picks,
    product_of_bale,
)
from src.juteCosting.daily_pl_query import (  # noqa: E402
    STORES_CATEGORIES,
    get_jute_stock_value_query,
    get_jute_waste_issue_daily_query,
    get_power_daily_by_type_query,
    get_stores_value_daily_by_category_query,
    stores_category_sql,
)


def _stores(d, **by_cat):
    return [{"tran_date": d, "category": c, "stores_value": v} for c, v in by_cat.items()]


def _power(d, **by_type):
    return [{"tran_date": d, "meter_type": t, "power_kwh": v} for t, v in by_type.items()]


POWER_RATE = _p(6, "POWER_RATE_PER_KWH", 8, uom="₹/kWh")


class TestStoresCategories:
    def test_label_map_covers_every_category(self):
        assert STORES_CATEGORIES == ("SPARES", "CONSUMABLES", "PACKING", "GENERAL")
        assert set(STORES_CATEGORY_LABELS) == set(STORES_CATEGORIES)
        assert STORES_CATEGORY_LABELS["CONSUMABLES"].startswith("Process consumables")

    def test_sql_contains_every_pattern_and_defaults_to_spares(self):
        case = stores_category_sql("ig")
        for pat in (
            "'PACKING%'", "'%LUBRICANT%'", "'OIL/%'", "'%CHEMICAL%'", "'DYES%'",
            "'GENERAL STORE%'", "'MISC EXPNS%'", "'OTHER CHARGES%'", "'OTHER ITEM%'",
            "'BUILDING MATERIAL%'", "'STATIONERY%'",
        ):
            assert pat in case, pat
        assert case.startswith("CASE WHEN UPPER(ig.item_grp_name)") and case.endswith("ELSE 'SPARES' END")
        sql = str(get_stores_value_daily_by_category_query())
        assert "AS category" in sql and "item_grp_mst ig" in sql and "lot_rate" not in sql
        assert "accepted_rate" in sql  # lot_rate_sql('pid') valuation kept

    def test_split_sums_to_the_stores_line(self):
        rows = _stores(D, SPARES=30000, CONSUMABLES=12000, PACKING=5000, GENERAL=3000)
        rows.append({"tran_date": D, "category": "weird", "stores_value": 1000})  # unknown -> SPARES
        rows.append({"tran_date": D, "stores_value": 500})  # old shape, no category -> SPARES
        day = build_daily_pl(_inputs(stores_rows=rows), BASE_PARAMS, D, D, D)["days"][0]
        assert day["stores_by_category"] == {
            "SPARES": 31500.0, "CONSUMABLES": 12000.0, "PACKING": 5000.0, "GENERAL": 3000.0,
        }
        assert day["money"]["stores"] == 51500.0 == sum(day["stores_by_category"].values())


class TestPowerByType:
    def test_query_joins_the_meter_master(self):
        sql = str(get_power_daily_by_type_query())
        assert "jute_power_meter_mst m" in sql and "UPPER(COALESCE(m.meter_type, 'OTHER'))" in sql
        assert "AS dg_hours" in sql and "AS dg_litres" in sql

    def test_dg_and_solar_fallbacks_are_flagged(self):
        rows = _power(D, GRID=1000, DG=200, SOLAR=300, OTHER=50)
        rows[1]["dg_hours"] = 4
        rows[1]["dg_litres"] = 60
        data = build_daily_pl(_inputs(power_rows=rows), BASE_PARAMS + [POWER_RATE], D, D, D)
        day = data["days"][0]
        # DG at the grid rate, solar at ₹0, OTHER at the grid rate.
        assert day["money"]["power"] == (1000 + 200 + 50) * 8.0
        assert day["power_by_type"] == {
            "GRID": {"kwh": 1000.0, "cost": 8000.0},
            "DG": {"kwh": 200.0, "cost": 1600.0},
            "SOLAR": {"kwh": 300.0, "cost": 0.0},
            "OTHER": {"kwh": 50.0, "cost": 400.0},
        }
        assert day["flags"]["dg_at_grid_rate"] is True and day["flags"]["solar_at_zero"] is True
        assert day["power_kwh"] == 1550.0 and day["dg_hours"] == 4.0 and day["dg_litres"] == 60.0
        nk = _nk(data)
        assert nk["DG_RATE_PER_KWH"] == "DG kWh priced at the grid rate — DG_RATE_PER_KWH not keyed"
        assert nk["SOLAR_RATE_PER_KWH"].startswith("solar kWh priced at ₹0")
        assert [x["key"] for x in data["not_keyed"]][:2] == ["DG_RATE_PER_KWH", "SOLAR_RATE_PER_KWH"]
        assert data["mtd"]["flags"]["dg_at_grid_rate"] is True

    def test_keyed_dg_and_solar_rates(self):
        params = BASE_PARAMS + [
            POWER_RATE, _p(11, "DG_RATE_PER_KWH", 12), _p(12, "SOLAR_RATE_PER_KWH", 2),
        ]
        rows = _power(D, GRID=1000, DG=200, SOLAR=300, OTHER=50)
        data = build_daily_pl(_inputs(power_rows=rows), params, D, D, D)
        day = data["days"][0]
        assert day["money"]["power"] == 8000 + 2400 + 600 + 400
        assert day["flags"]["dg_at_grid_rate"] is False and day["flags"]["solar_at_zero"] is False
        assert "DG_RATE_PER_KWH" not in _nk(data)
        assert {11, 12} <= {p["cost_param_id"] for p in data["params_used"]}

    def test_no_dg_kwh_no_flag_and_no_lookup(self):
        data = build_daily_pl(_inputs(power_rows=_power(D, GRID=1000)), BASE_PARAMS + [POWER_RATE], D, D, D)
        day = data["days"][0]
        assert day["flags"]["dg_at_grid_rate"] is False and "DG_RATE_PER_KWH" not in _nk(data)
        assert day["power_by_type"]["DG"] == {"kwh": 0.0, "cost": 0.0}

    def test_untracked_power_by_type_is_all_none(self):
        day = build_daily_pl(_inputs(power_rows=None), BASE_PARAMS, D, D, D)["days"][0]
        assert all(v == {"kwh": None, "cost": None} for v in day["power_by_type"].values())

    def test_grid_rate_missing_keeps_kwh_but_no_cost(self):
        day = build_daily_pl(_inputs(power_rows=_power(D, GRID=1000, DG=10)), BASE_PARAMS, D, D, D)["days"][0]
        assert day["power_by_type"]["GRID"] == {"kwh": 1000.0, "cost": None}
        assert day["money"]["power"] is None and day["flags"]["dg_at_grid_rate"] is False


class TestJuteWaste:
    def test_waste_is_its_own_cost_line_inside_contribution(self):
        waste = [{"tran_date": D, "waste_kg": 500, "waste_value": 7500}]
        day = build_daily_pl(
            build_day_inputs(D, D, **{**_inputs.__wrapped__(D)}) if False else _inputs_with_waste(waste),
            BASE_PARAMS, D, D, D,
        )["days"][0]
        assert day["money"]["jute_waste_cost"] == 7500.0 and day["jute_waste_t"] == 0.5
        assert day["money"]["jute_cost"] == 900000.0  # unchanged by waste
        assert day["money"]["contribution"] == 1090100.0 - 7500.0
        assert day["per_t_cloth"]["jute_waste_cost"] == 625.0


def _inputs_with_waste(waste_rows, d=D):
    base = {
        "cloth_rows": [
            {"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": 10},
            {"tran_date": d, "weaving_quality_name": "B-Twill 37 x 22.5", "production_mt": 2},
        ],
        "yarn_rows": [{"tran_date": d, "production_kg": 11500}],
        "hands_rows": [{"tran_date": d, "hours": 4000, "hands": 500}],
        "spindle_rows": [{"tran_date": d, "spindle_hours": 2900}],
        "jute_rows": [{"tran_date": d, "issued_kg": 6000, "issue_value": 900000}],
        "stores_rows": [{"tran_date": d, "stores_value": 50000}],
        "dust_rows": [{"tran_date": d, "dust_kg": 100}],
        "purchased_rows": [{"tran_date": d, "purchased_kg": 500}],
        "power_rows": None,
    }
    return build_day_inputs(d, d, waste_rows=waste_rows, **base)


class TestOverheads:
    def _day(self, params):
        return build_daily_pl(_inputs(), params, D, D, D)["days"][0]

    def test_sub_lines_beat_the_total(self):
        params = BASE_PARAMS + [
            _p(20, "OVERHEAD_STAFF_PER_DAY", 50000),
            _p(21, "OVERHEAD_ADMIN_PER_DAY", 20000),
            _p(22, "FIXED_COST_PER_DAY", 999999),  # ignored once a sub-line is keyed
        ]
        day = self._day(params)
        assert day["money"]["overheads"] == 70000.0 and day["flags"]["overheads_estimated"] is False
        assert [(l["code"], l["value_per_day"], l["amount"]) for l in day["overheads_lines"]] == [
            ("OVERHEAD_STAFF_PER_DAY", 50000.0, 50000.0),
            ("OVERHEAD_ADMIN_PER_DAY", 20000.0, 20000.0),
        ]
        assert day["overheads_lines"][0]["label"] == "Overheads: staff salaries per day"
        assert day["money"]["operating_profit"] == 1090100.0 - 70000.0

    def test_total_when_no_sub_line(self):
        day = self._day(BASE_PARAMS + [_p(22, "FIXED_COST_PER_DAY", 80000)])
        assert day["money"]["overheads"] == 80000.0
        assert day["overheads_lines"][0]["code"] == "FIXED_COST_PER_DAY"
        assert day["overheads_lines"][0]["label"].startswith("Overheads: total per day")

    def test_estimate_when_nothing_keyed(self):
        day = self._day(BASE_PARAMS)
        assert day["money"]["overheads"] == 2000.0 * 12 and day["flags"]["overheads_estimated"] is True
        line = day["overheads_lines"][0]
        assert line["code"] == "BENCH_OVERHEAD_PER_T" and line["value_per_day"] is None
        assert line["rate_per_t"] == 2000.0 and line["amount"] == 24000.0
        assert "₹2,000/t" in line["label"]

    def test_keyed_benchmark_drives_the_estimate(self):
        data = build_daily_pl(_inputs(), BASE_PARAMS + [_p(23, "BENCH_OVERHEAD_PER_T", 2500)], D, D, D)
        day = data["days"][0]
        assert day["money"]["overheads"] == 30000.0 and day["flags"]["overheads_estimated"] is True
        assert _nk(data)["overheads"] == "overheads not keyed — estimated at the ₹2,500/t rule of thumb"
        assert 23 in {p["cost_param_id"] for p in data["params_used"]}

    def test_estimate_on_a_blank_day_is_zero(self):
        inputs = build_day_inputs(D, D, [], [], [], [], [], [], [], None, None)
        day = build_daily_pl(inputs, BASE_PARAMS, D, D, D)["days"][0]
        assert day["money"]["overheads"] == 0.0 and day["money"]["operating_profit"] == 0.0


CAPITAL_PARAMS = BASE_PARAMS + [
    _p(30, "COST_OF_CAPITAL_PCT", 14, uom="%"),
    _p(31, "WORKING_CAPITAL_OTHER", 1000000, uom="₹"),
]


def _fg_rows():
    return [
        {"item_name": "B.Twill 40x26", "quality_type": 2, "total_kg": 10000, "bales": 40, "despatched_bales": 1},   # sacking by type
        {"item_name": "Hessian 10 oz", "quality_type": 1, "total_kg": 5000, "bales": 20, "despatched_bales": 0},    # hessian by type
        {"item_name": "SACK Z", "quality_type": None, "total_kg": 1000, "bales": 4, "despatched_bales": 0},         # falls to the name rule
    ]


REGISTER_NOTE = (
    "bale register: 65 bales entered, 1 recorded despatched — in-stock tonnes overstated until "
    "despatch / invoice links are keyed (ITM-021); key FG_STOCK_T to override"
)


class TestCapital:
    def test_product_of_bale(self):
        assert product_of_bale(2, "B.Twill") == "SACKING" and product_of_bale(1, "SACK A") == "HESSIAN"
        assert product_of_bale(None, "SACK A") == "SACKING" and product_of_bale("x", "Hessian") == "HESSIAN"

    def test_build_capital(self):
        resolved = resolve_params(CAPITAL_PARAMS, D)
        cap = build_capital([{"value": 55348315.35, "kg": 632854}], _fg_rows(), resolved, D, 2)
        assert cap["jute_stock_value"] == 55348315.35 and cap["jute_stock_kg"] == 632854.0
        assert cap["fg_t"] == 16.0
        assert cap["fg_by_product"]["SACKING"] == {"tonnes": 11.0, "rate": 182000.0, "value": 2002000.0}
        assert cap["fg_by_product"]["HESSIAN"] == {"tonnes": 5.0, "rate": 210000.0, "value": 1050000.0}
        assert cap["fg_value"] == 3052000.0
        assert cap["other"] == 1000000.0 and cap["pct"] == 14.0 and cap["available"] is True
        assert cap["as_on"] == "2026-07-12" and cap["notes"] == [REGISTER_NOTE]
        assert cap["fg_source"] == "bale_register"
        assert cap["bales_entered"] == 65 and cap["bales_despatched"] == 1

    def test_build_capital_unavailable_sources(self):
        resolved = resolve_params(CAPITAL_PARAMS, D)
        cap = build_capital(None, None, resolved, D, None)
        assert cap["jute_stock_value"] is None and cap["fg_value"] is None and cap["available"] is False
        assert "needs a branch" in cap["notes"][1] and "vw_jute_stock_outstanding" in cap["notes"][0]
        assert cap["fg_source"] is None and cap["bales_entered"] is None and cap["bales_despatched"] is None
        cap = build_capital(None, None, resolved, D, 2)
        assert "bale register missing" in cap["notes"][1]
        # Bales in stock but no realisation rate at all: unknown, not ₹0.
        no_rates = resolve_params([p for p in CAPITAL_PARAMS if p["param_code"] != "REALISATION_PER_T"], D)
        cap = build_capital([{"value": 1, "kg": 1}], _fg_rows(), no_rates, D, 2)
        assert cap["fg_value"] is None and cap["fg_t"] == 16.0
        assert len(cap["notes"]) == 3 and cap["notes"][-1] == REGISTER_NOTE
        # Empty bale register: ₹0, not unknown, and no coverage note to give.
        cap = build_capital([{"value": 1, "kg": 1}], [], resolved, D, 2)
        assert cap["fg_value"] == 0.0 and cap["fg_t"] == 0.0 and cap["fg_source"] == "bale_register"
        assert cap["bales_entered"] == 0 and cap["bales_despatched"] == 0 and cap["notes"] == []

    def test_keyed_fg_stock_overrides_the_register(self):
        params = CAPITAL_PARAMS + [
            _p(50, "FG_STOCK_T", 120, product="SACKING", uom="t"),
            _p(51, "FG_STOCK_T", 8.5, product="HESSIAN", uom="t"),
        ]
        resolved = resolve_params(params, D)
        cap = build_capital([{"value": 1000, "kg": 1}], _fg_rows(), resolved, D, 2)
        assert cap["fg_source"] == "keyed" and cap["fg_t"] == 128.5
        assert cap["fg_by_product"]["SACKING"] == {"tonnes": 120.0, "rate": 182000.0, "value": 21840000.0}
        assert cap["fg_by_product"]["HESSIAN"] == {"tonnes": 8.5, "rate": 210000.0, "value": 1785000.0}
        assert cap["fg_value"] == 23625000.0
        # The register is still read for its coverage, and the note says it was not used.
        assert cap["bales_entered"] == 65 and cap["bales_despatched"] == 1
        assert cap["notes"] == [
            "bale register: 65 bales entered, 1 recorded despatched — not used, FG_STOCK_T keyed (128.500 t)"
        ]
        # One product keyed: the other reads 0 t (never the register's figure).
        one = resolve_params(CAPITAL_PARAMS + [_p(50, "FG_STOCK_T", 120, product="SACKING", uom="t")], D)
        cap = build_capital([{"value": 1000, "kg": 1}], _fg_rows(), one, D, 2)
        assert cap["fg_t"] == 120.0 and cap["fg_by_product"]["HESSIAN"]["tonnes"] == 0.0
        # Keyed rows work without a register / branch too.
        cap = build_capital([{"value": 1000, "kg": 1}], None, resolved, D, None)
        assert cap["fg_source"] == "keyed" and cap["fg_value"] == 23625000.0 and cap["bales_entered"] is None
        assert not any("needs a branch" in n for n in cap["notes"])
        # An all-product row cannot be split by product: ignored with a note.
        bad = resolve_params(CAPITAL_PARAMS + [_p(52, "FG_STOCK_T", 50, product=None, uom="t"),
                                               _p(50, "FG_STOCK_T", 120, product="SACKING", uom="t")], D)
        cap = build_capital([{"value": 1000, "kg": 1}], [], bad, D, 2)
        assert cap["fg_t"] == 120.0 and any(n.startswith("FG_STOCK_T must be keyed per product") for n in cap["notes"])
        # Keyed override flows into the capital cost.
        data = build_daily_pl(_inputs(), params, D, D, D, build_capital([{"value": 1000, "kg": 1}], _fg_rows(), resolved, D, 2))
        assert data["days"][0]["money"]["capital_cost"] == round((1000 + 23625000 + 1000000) * 0.14 / 365, 2)
        assert data["capital"]["fg_source"] == "keyed"

    def test_capital_cost_and_net(self):
        resolved = resolve_params(CAPITAL_PARAMS, D)
        cap = build_capital([{"value": 55348315.35, "kg": 632854}], _fg_rows(), resolved, D, 2)
        data = build_daily_pl(_inputs(), CAPITAL_PARAMS, D, D, D, cap)
        m = data["days"][0]["money"]
        expected = round((55348315.35 + 3052000.0 + 1000000.0) * 14 / 100 / 365, 2)
        assert m["capital_cost"] == expected == 22783.68
        assert m["net"] == round(m["operating_profit"] - expected, 2)
        assert data["days"][0]["per_t_cloth"]["capital_cost"] == round(expected / 12, 2)
        assert data["capital"] is cap
        assert not {"capital", "COST_OF_CAPITAL_PCT", "WORKING_CAPITAL_OTHER"} & set(_nk(data))
        assert {30, 31} <= {p["cost_param_id"] for p in data["params_used"]}

    def test_other_capital_defaults_to_zero(self):
        params = [p for p in CAPITAL_PARAMS if p["param_code"] != "WORKING_CAPITAL_OTHER"]
        cap = build_capital([{"value": 3650000, "kg": 1}], [], resolve_params(params, D), D, 2)
        data = build_daily_pl(_inputs(), params, D, D, D, cap)
        assert data["days"][0]["money"]["capital_cost"] == round(3650000 * 0.14 / 365, 2) == 1400.0
        assert _nk(data)["WORKING_CAPITAL_OTHER"].endswith("taken as ₹0")

    def test_none_cases(self):
        # pct not keyed
        cap = build_capital([{"value": 1000, "kg": 1}], [], resolve_params(BASE_PARAMS, D), D, 2)
        data = build_daily_pl(_inputs(), BASE_PARAMS, D, D, D, cap)
        m = data["days"][0]["money"]
        assert m["capital_cost"] is None and m["net"] is None and m["operating_profit"] is not None
        assert _nk(data)["COST_OF_CAPITAL_PCT"] == "cost of capital % not keyed — capital cost and net blank"
        # both stock values unavailable
        cap = build_capital(None, None, resolve_params(CAPITAL_PARAMS, D), D, 2)
        data = build_daily_pl(_inputs(), CAPITAL_PARAMS, D, D, D, cap)
        assert data["days"][0]["money"]["capital_cost"] is None and "capital" in _nk(data)
        # capital never computed
        data = build_daily_pl(_inputs(), CAPITAL_PARAMS, D, D, D)
        assert data["days"][0]["money"]["net"] is None and data["capital"]["available"] is False
        # operating profit None (wages missing) -> net None even with a capital cost
        params = [p for p in CAPITAL_PARAMS if p["param_code"] != "WAGE_PER_HAND_DAY"]
        cap = build_capital([{"value": 1000, "kg": 1}], [], resolve_params(params, D), D, 2)
        m = build_daily_pl(_inputs(), params, D, D, D, cap)["days"][0]["money"]
        assert m["capital_cost"] is not None and m["operating_profit"] is None and m["net"] is None


# JUTE_PER_KG_CLOTH keyed at 1.12 so the keyed std is distinguishable from the 1.06 default.
FULL_PARAMS = CAPITAL_PARAMS + [POWER_RATE, _p(32, "JUTE_PER_KG_CLOTH", 1.12), _p(33, "JUTE_RATE_PER_KG", 60)]


class TestStatement:
    def _data(self, params=None, **over):
        params = FULL_PARAMS if params is None else params
        resolved = resolve_params(params, D)
        cap = build_capital([{"value": 55348315.35, "kg": 632854}], _fg_rows(), resolved, D, 2)
        rows = dict(
            stores_rows=_stores(D, SPARES=30000, CONSUMABLES=15000, PACKING=3000, GENERAL=2000),
            power_rows=_power(D, GRID=1000, DG=200),
        )
        rows.update(over)
        return build_daily_pl(_inputs(**rows), params, D, D, D, cap)

    def test_heads_sum_to_the_results(self):
        data = self._data()
        st = data["statement"]
        m = data["mtd"]["money"]
        heads = {h["key"]: h for h in st["heads"]}
        assert [h["no"] for h in st["heads"]] == [1, 2, 3, 4, 5, 6]
        assert st["priced_days"] == 1 and st["calendar_days"] == 1 and st["cloth_t"] == 12.0
        assert st["realisation"]["period"] == 2240000.0 == m["realisation"]
        assert [(l["key"], l["tonnes"], l["rate"], l["period"]) for l in st["realisation"]["lines"]] == [
            ("SACKING", 10.0, 182000.0, 1820000.0),
            ("HESSIAN", 2.0, 210000.0, 420000.0),
        ]
        rm, stores, power, wages, oh, cap = (heads[k] for k in ("raw_material", "stores", "power", "wages", "overheads", "capital"))
        assert rm["period"] == 900000.0 - 100.0  # jute + waste(0) − dust credit
        assert stores["period"] == 50000.0 and power["period"] == 9600.0 and wages["period"] == 200000.0
        assert st["contribution"]["period"] == m["contribution"] == round(
            2240000 - rm["period"] - stores["period"] - power["period"] - wages["period"], 2
        )
        assert oh["period"] == 24000.0 and oh["estimated"] is True
        assert st["operating_profit"]["period"] == st["contribution"]["period"] - 24000.0 == m["operating_profit"]
        assert cap["period"] == m["capital_cost"] == 22783.68
        assert st["net"]["period"] == round(st["operating_profit"]["period"] - 22783.68, 2) == m["net"]
        # Every per_t is over the priced days' cloth tonnes.
        for h in st["heads"]:
            assert h["per_t"] == round(h["period"] / 12, 2)
            for line in h["lines"]:
                if line["period"] is not None:
                    assert line["per_t"] == round(line["period"] / 12, 2)
        assert st["contribution"]["per_t"] == data["mtd"]["per_t_cloth"]["contribution"]
        # Sub-lines add up to their head exactly (rounding residual reconciled).
        for h in st["heads"]:
            if h["lines"] and all(l["period"] is not None for l in h["lines"]):
                assert round(sum(l["period"] for l in h["lines"]), 2) == h["period"], h["key"]
        assert round(sum(l["period"] for l in st["realisation"]["lines"]), 2) == st["realisation"]["period"]
        assert st["conversion_cost"]["period"] == 50000 + 9600 + 200000 + 24000
        assert st["conversion_cost"]["per_t"] == round((50000 + 9600 + 200000 + 24000) / 12, 2)

    def test_raw_material_lines_and_kpis(self):
        data = self._data(jute_rows=[_jute(D, 6000, 500000, unvalued_kg=2000)])
        rm = data["statement"]["heads"][0]
        lines = {l["key"]: l for l in rm["lines"]}
        assert lines["jute_booked"] == {"key": "jute_booked", "label": "Raw jute at booked issue value", "qty_t": 4.0, "period": 500000.0, "per_t": round(500000 / 12, 2)}
        assert lines["jute_priced"]["qty_t"] == 2.0 and lines["jute_priced"]["period"] == 120000.0  # 2,000 kg x ₹60
        assert lines["jute_waste"]["period"] == 0.0 and lines["jute_waste"]["qty_t"] == 0.0
        assert lines["dust_credit"]["period"] == -100.0 and lines["dust_credit"]["per_t"] == round(-100 / 12, 2)
        assert lines["dust_credit"]["qty_t"] == 0.1
        assert rm["period"] == 500000 + 120000 - 100
        assert rm["kpis"] == {
            "jute_kg_per_kg_cloth": 0.5, "jute_kg_per_kg_cloth_std": 1.12,
            "jute_issued_t": 6.0, "jute_unvalued_t": 2.0, "zero_value_lines": 0,
        }
        assert rm["has_breakdown"] is True and rm["has_check"] is True and rm["available"] is True
        # Both keyed: the keyed 1.12 kg/kg x ₹60/kg x 1000.
        assert rm["benchmark_per_t"] == round(1.12 * 60 * 1000, 2) == 67200.0 and rm["benchmark_label"] == "keyed"

    def test_raw_material_benchmark_needs_the_jute_rate(self):
        # No jute rate: no benchmark, whatever JUTE_PER_KG_CLOTH says.
        params = [p for p in FULL_PARAMS if p["param_code"] != "JUTE_RATE_PER_KG"]
        rm = self._data(params)["statement"]["heads"][0]
        assert rm["benchmark_per_t"] is None and rm["benchmark_label"] == RAW_MATERIAL_BENCH_LABEL_UNKEYED
        assert rm["kpis"]["jute_kg_per_kg_cloth_std"] == 1.12
        no_std = [p for p in params if p["param_code"] != "JUTE_PER_KG_CLOTH"]
        assert self._data(no_std)["statement"]["heads"][0]["kpis"]["jute_kg_per_kg_cloth_std"] == 1.06
        # Only the jute rate keyed: the 1.06 std default x the keyed rate.
        rate_only = [p for p in FULL_PARAMS if p["param_code"] != "JUTE_PER_KG_CLOTH"]
        rm = self._data(rate_only)["statement"]["heads"][0]
        assert rm["benchmark_per_t"] == round(1.06 * 60 * 1000, 2) == 63600.0
        assert rm["benchmark_label"] == RAW_MATERIAL_BENCH_LABEL_STD == "std 1.06 kg/kg × keyed jute rate"
        assert rm["kpis"]["jute_kg_per_kg_cloth_std"] == 1.06

    def test_conversion_benchmarks_default_and_keyed(self):
        st = self._data()["statement"]
        heads = {h["key"]: h for h in st["heads"]}
        assert (heads["stores"]["benchmark_per_t"], heads["stores"]["benchmark_label"]) == (4000.0, BENCHMARK_SOURCE)
        assert (heads["power"]["benchmark_per_t"], heads["power"]["benchmark_label"]) == (4000.0, BENCHMARK_SOURCE)
        assert (heads["overheads"]["benchmark_per_t"], heads["overheads"]["benchmark_label"]) == (2000.0, BENCHMARK_SOURCE)
        assert heads["wages"]["benchmark_per_t"] is None and heads["capital"]["benchmark_per_t"] is None
        assert st["conversion_cost"]["benchmark_per_t"] == 10000.0
        assert st["conversion_cost"]["benchmark_label"] == BENCHMARK_SOURCE
        assert BENCHMARK_DEFAULTS == {"stores": 4000, "power": 4000, "overheads": 2000}
        keyed = self._data(FULL_PARAMS + [_p(40, "BENCH_STORES_PER_T", 4500)])["statement"]
        kh = {h["key"]: h for h in keyed["heads"]}
        assert (kh["stores"]["benchmark_per_t"], kh["stores"]["benchmark_label"]) == (4500.0, "keyed")
        assert keyed["conversion_cost"]["benchmark_per_t"] == 10500.0
        assert keyed["conversion_cost"]["benchmark_label"] == "keyed + rule of thumb"

    def test_stores_power_wages_lines(self):
        st = self._data()["statement"]
        heads = {h["key"]: h for h in st["heads"]}
        assert [(l["key"], l["period"]) for l in heads["stores"]["lines"]] == [
            ("SPARES", 30000.0), ("CONSUMABLES", 15000.0), ("PACKING", 3000.0), ("GENERAL", 2000.0),
        ]
        assert heads["stores"]["lines"][1]["label"] == STORES_CATEGORY_LABELS["CONSUMABLES"]
        power_lines = {l["key"]: l for l in heads["power"]["lines"]}
        assert power_lines["GRID"] == {"key": "GRID", "label": "Grid", "kwh": 1000.0, "rate": 8.0, "period": 8000.0, "per_t": round(8000 / 12, 2)}
        assert power_lines["DG"]["kwh"] == 200.0 and power_lines["DG"]["period"] == 1600.0
        assert power_lines["SOLAR"] == {"key": "SOLAR", "label": "Solar", "kwh": 0.0, "rate": None, "period": 0.0, "per_t": 0.0}
        assert heads["power"]["kpis"] == {"kwh_per_t": 100.0, "dg_hours": 0.0, "dg_litres": 0.0}
        assert heads["power"]["available"] is True and heads["power"]["not_keyed"] is None
        w = heads["wages"]
        assert w["label"] == "Wages (worker hand-days)" and w["lines"] == [
            {"key": "hands", "label": "Hand-days × wage per hand-day", "qty": 500.0, "rate": 400.0, "period": 200000.0, "per_t": round(200000 / 12, 2)}
        ]
        assert w["kpis"] == {"hands": 500.0, "hands_per_t": round(500 / 12, 2), "hours": 4000.0}

    def test_overheads_and_capital_lines(self):
        st = self._data(FULL_PARAMS + [_p(20, "OVERHEAD_STAFF_PER_DAY", 50000)])["statement"]
        heads = {h["key"]: h for h in st["heads"]}
        assert heads["overheads"]["estimated"] is False and heads["overheads"]["has_breakdown"] is False
        assert heads["overheads"]["lines"] == [
            {"code": "OVERHEAD_STAFF_PER_DAY", "label": "Overheads: staff salaries per day", "value_per_day": 50000.0, "days": 1, "period": 50000.0, "per_t": round(50000 / 12, 2)}
        ]
        cap = heads["capital"]
        assert cap["memo"] == CAPITAL_MEMO and cap["kpis"] == {"pct": 14.0, "days": 1}
        assert cap["label"] == "Capital cost (interest on blocked capital)"
        assert [l["key"] for l in cap["lines"]] == ["jute_stock", "finished_goods", "other"]
        assert [l["base_value"] for l in cap["lines"]] == [55348315.35, 3052000.0, 1000000.0]
        assert abs(sum(l["period"] for l in cap["lines"]) - cap["period"]) < 0.02  # pro-rata, rounding only

    def test_unavailable_heads_carry_the_reason(self):
        data = self._data(BASE_PARAMS, power_rows=None)
        st = data["statement"]
        heads = {h["key"]: h for h in st["heads"]}
        assert heads["power"]["available"] is False and heads["power"]["period"] is None
        assert heads["power"]["not_keyed"] == "power: not keyed — jute_power_reading not available"
        assert all(l["kwh"] is None for l in heads["power"]["lines"])
        assert heads["capital"]["available"] is False
        assert heads["capital"]["not_keyed"] == "cost of capital % not keyed — capital cost and net blank"
        assert st["net"]["period"] is None and st["operating_profit"]["period"] is not None
        assert st["conversion_cost"]["period"] == 50000 + 200000 + 24000  # power excluded

    def test_statement_covers_the_priced_days_only(self):
        d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
        cloth = [{"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": 10} for d in (d1, d2)]
        hands = [{"tran_date": d, "hours": 800, "hands": 100} for d in (d1, d2)]
        jute = [_jute(d1, 5000, 0, unvalued_kg=5000), _jute(d2, 5000, 300000)]
        inputs = build_day_inputs(d1, d2, cloth, [], hands, [], jute, [], [], None, None)
        data = build_daily_pl(inputs, BASE_PARAMS, d1, d2, d1)
        st = data["statement"]
        assert st["priced_days"] == 1 and st["calendar_days"] == 2 and st["cloth_t"] == 10.0
        assert st["realisation"]["period"] == 1820000.0  # day 2 only (mtd.money.realisation spans both)
        assert data["mtd"]["money"]["realisation"] == 3640000.0
        assert st["contribution"]["period"] == data["mtd"]["money"]["contribution"] == 1480000.0
        heads = {h["key"]: h for h in st["heads"]}
        assert heads["raw_material"]["period"] == 300000.0 and heads["raw_material"]["per_t"] == 30000.0
        assert heads["raw_material"]["kpis"]["jute_issued_t"] == 10.0  # physical KPI: every day
        assert st["contribution"]["per_t"] == 148000.0

    def test_empty_range_statement(self):
        inputs = build_day_inputs(D, D, [], [], [], [], [], [], [], None, None)
        st = build_daily_pl(inputs, BASE_PARAMS, D, D, D)["statement"]
        assert st["cloth_t"] == 0.0 and st["contribution"]["period"] == 0.0 and st["contribution"]["per_t"] is None
        assert all(h["per_t"] is None for h in st["heads"])

    def test_build_statement_direct_with_no_days(self):
        st = build_statement([], resolve_params(FULL_PARAMS, D), None, [])
        assert st["priced_days"] == 0 and st["realisation"]["period"] is None
        assert all(h["available"] is False for h in st["heads"])


class TestPeriodRollUpNewKeys:
    def test_money_keys_and_per_t_keys(self):
        assert MONEY_KEYS == (
            "realisation", "jute_cost", "jute_waste_cost", "wages", "power", "stores", "dust_credit",
            "contribution", "overheads", "operating_profit", "capital_cost", "net",
        )
        assert "fixed" not in MONEY_KEYS and set(PER_T_CLOTH_KEYS) == set(MONEY_KEYS)

    def test_two_days_roll_up(self):
        d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
        cloth = [{"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": 10} for d in (d1, d2)]
        hands = [{"tran_date": d, "hours": 800, "hands": 100} for d in (d1, d2)]
        jute = [_jute(d, 5000, 300000) for d in (d1, d2)]
        waste = [{"tran_date": d1, "waste_kg": 100, "waste_value": 1500}]
        stores = _stores(d1, SPARES=1000, PACKING=500) + _stores(d2, SPARES=2000, GENERAL=300)
        power = _power(d1, GRID=1000, DG=100) + _power(d2, GRID=1200)
        inputs = build_day_inputs(d1, d2, cloth, [], hands, [], jute, stores, [], None, power, waste)
        params = CAPITAL_PARAMS + [POWER_RATE, _p(22, "FIXED_COST_PER_DAY", 10000)]
        cap = build_capital([{"value": 3650000, "kg": 100}], [], resolve_params(params, d2), d2, 2)
        data = build_daily_pl(inputs, params, d1, d2, d1, cap)
        mtd = data["mtd"]
        m = mtd["money"]
        assert m["jute_waste_cost"] == 1500.0 and mtd["jute_waste_t"] == 0.1
        assert m["stores"] == 3800.0 and mtd["stores_by_category"] == {"SPARES": 3000.0, "CONSUMABLES": 0.0, "PACKING": 500.0, "GENERAL": 300.0}
        assert m["power"] == (1000 + 100 + 1200) * 8.0
        assert mtd["power_by_type"]["GRID"] == {"kwh": 2200.0, "cost": 17600.0}
        assert mtd["power_by_type"]["DG"] == {"kwh": 100.0, "cost": 800.0}
        assert m["overheads"] == 20000.0 and mtd["overheads_lines"] == [
            {"code": "FIXED_COST_PER_DAY", "label": "Overheads: total per day (when sub-lines not keyed)", "value_per_day": 10000.0, "rate_per_t": None, "days": 2, "amount": 20000.0}
        ]
        contribution = 2 * (10 * 182000 - 300000 - 40000) - 1500 - 3800 - 18400
        assert m["contribution"] == contribution
        assert m["operating_profit"] == contribution - 20000.0
        daily_cc = round((3650000 + 1000000) * 0.14 / 365, 2)
        assert m["capital_cost"] == round(2 * daily_cc, 2)
        assert m["net"] == round(m["operating_profit"] - 2 * daily_cc, 2)
        for k in ("jute_waste_cost", "overheads", "operating_profit", "capital_cost", "net"):
            assert mtd["per_t_cloth"][k] == round(m[k] / 20, 2), k
        assert mtd["realisation_by_product"]["SACKING"] == {"tonnes": 20.0, "rate": 182000.0, "amount": 3640000.0}
        assert mtd["realisation_by_product"]["HESSIAN"] == {"tonnes": 0.0, "rate": None, "amount": 0.0}
        assert mtd["flags"]["dg_at_grid_rate"] is True and mtd["flags"]["overheads_estimated"] is False
        assert data["std"]["operating_profit_per_t"] == round((contribution - 20000) / 2 / 10, 2)
        assert mtd["zero_value_lines"] == 0 and mtd["dg_hours"] == 0.0


class TestReconcileLines:
    def test_residual_goes_on_the_largest_line(self):
        lines = [{"period": 10.01, "per_t": None}, {"period": 5.0, "per_t": None}, {"period": None, "per_t": None}]
        out = _reconcile_lines(lines, 15.03, 4.0)
        assert out[0]["period"] == 10.03 and out[0]["per_t"] == 2.51
        assert out[1]["period"] == 5.0 and out[2]["period"] is None
        assert round(sum(l["period"] for l in out[:2]), 2) == 15.03

    def test_exact_lines_are_untouched(self):
        lines = [{"period": -10.0, "per_t": -5.0}, {"period": 4.0, "per_t": 2.0}]
        assert _reconcile_lines(lines, -6.0, 2.0) == [{"period": -10.0, "per_t": -5.0}, {"period": 4.0, "per_t": 2.0}]

    def test_large_residual_is_a_defect_not_rounding(self):
        assert RECONCILE_MAX_PER_DAY == 0.05
        lines = [{"period": 10.0, "per_t": 5.0}]
        assert _reconcile_lines(lines, 11.0, 2.0)[0]["period"] == 10.0
        # The cap scales with the days of the range (one rounding per day).
        assert _reconcile_lines([{"period": 10.0, "per_t": 5.0}], 10.08, 2.0, 1)[0]["period"] == 10.0
        assert _reconcile_lines([{"period": 10.0, "per_t": 5.0}], 10.08, 2.0, 2)[0]["period"] == 10.08

    def test_none_total_or_no_priced_line(self):
        assert _reconcile_lines([{"period": 1.0, "per_t": 1.0}], None, 1.0)[0]["period"] == 1.0
        assert _reconcile_lines([{"period": None, "per_t": None}], 5.0, 1.0)[0]["period"] is None

    def test_stores_sub_lines_sum_to_the_head_over_many_days(self):
        # Per-day category values that each round differently from the day total.
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(6)]
        cloth = [{"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": 10} for d in days]
        hands = [{"tran_date": d, "hours": 800, "hands": 100} for d in days]
        stores = []
        for d in days:
            stores += _stores(d, SPARES=100.005, CONSUMABLES=200.005, GENERAL=0.004)
        inputs = build_day_inputs(days[0], days[-1], cloth, [], hands, [], [], stores, [], None, None)
        st = build_daily_pl(inputs, BASE_PARAMS, days[0], days[-1], days[0])["statement"]
        head = st["heads"][1]
        assert round(sum(l["period"] for l in head["lines"]), 2) == head["period"]


# ---------------------------------------------------------------------------
# ITM-027 review fixes (2026-10-20)
# ---------------------------------------------------------------------------


class TestDailyQueriesKeepTheWasteSplit:
    def test_raw_jute_and_waste_predicates(self):
        raw = str(get_jute_issue_daily_query())
        assert "COALESCE(ig.item_type_id, 0) <> 3" in raw and "ig.item_type_id = 3" not in raw
        waste = str(get_jute_waste_issue_daily_query())
        assert "ig.item_type_id = 3" in waste and "AS waste_kg" in waste
        assert "COALESCE(ig.item_type_id, 0) <> 3" not in waste


def _three_days_one_unpriced():
    """Day 1 unpriced (jute with no value, no MR rate, no JUTE_RATE keyed);
    days 2 and 3 priced. Fractions of a paisa in the cloth, hands, jute value,
    stores and capital base so per-day rounding cannot re-sum exactly."""
    d1, d2, d3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
    days = (d1, d2, d3)
    cloth = [{"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": 10.001} for d in days]
    hands = [{"tran_date": d, "hours": 801, "hands": 100.125} for d in days]
    jute = [_jute(d1, 5000, 0, unvalued_kg=5000), _jute(d2, 5000, 300000.005), _jute(d3, 5000, 300000.005)]
    stores = _stores(d2, SPARES=1000.004, PACKING=500.004) + _stores(d3, SPARES=2000.004)
    inputs = build_day_inputs(d1, d3, cloth, [], hands, [], jute, stores, [], None, None)
    cap = build_capital([{"value": 1234567.89, "kg": 1}], [], resolve_params(CAPITAL_PARAMS, d3), d3, 2)
    return build_daily_pl(inputs, CAPITAL_PARAMS, d1, d3, d1, cap)


class TestIdentitiesToThePaisa:
    def test_statement_results_derive_from_the_heads(self):
        data = _three_days_one_unpriced()
        st = data["statement"]
        assert st["priced_days"] == 2 and st["calendar_days"] == 3
        h = {x["key"]: x["period"] for x in st["heads"]}
        assert h["power"] is None  # power not tracked: out of the contribution, as on each day
        c = st["contribution"]["period"]
        assert c == round(st["realisation"]["period"] - h["raw_material"] - h["stores"] - h["wages"], 2)
        assert st["operating_profit"]["period"] == round(c - h["overheads"], 2)
        assert st["net"]["period"] == round(st["operating_profit"]["period"] - h["capital"], 2)
        for k in ("contribution", "operating_profit", "net"):
            assert st[k]["per_t"] == round(st[k]["period"] / st["cloth_t"], 2), k

    def test_mtd_overheads_and_capital_cover_the_priced_days(self):
        data = _three_days_one_unpriced()
        mtd, days = data["mtd"], data["days"]
        m = mtd["money"]
        priced = [d for d in days if d["money"]["contribution"] is not None]
        assert len(priced) == 2 and mtd["flags"]["contribution_partial"] is True
        assert mtd["flags"]["overheads_priced_days_only"] is True
        assert m["overheads"] == round(sum(d["money"]["overheads"] for d in priced), 2)
        assert m["overheads"] < round(sum(d["money"]["overheads"] for d in days), 2)  # day 1 left out
        assert m["capital_cost"] == round(sum(d["money"]["capital_cost"] for d in priced), 2)
        assert m["operating_profit"] == round(m["contribution"] - m["overheads"], 2)
        assert m["net"] == round(m["operating_profit"] - m["capital_cost"], 2)
        heads = {h["key"]: h for h in data["statement"]["heads"]}
        assert m["overheads"] == heads["overheads"]["period"] and m["capital_cost"] == heads["capital"]["period"]
        priced_t = sum(d["cloth_t"] for d in priced)
        assert mtd["per_t_cloth"]["overheads"] == round(m["overheads"] / priced_t, 2)
        assert mtd["per_t_cloth"]["net"] == round(m["net"] / priced_t, 2)
        assert [line["days"] for line in mtd["overheads_lines"]] == [2]
        assert mtd["notes"] == [
            "Overheads and capital cost cover the 2 priced days of 3 only (the days whose contribution "
            "is priced), so contribution − overheads = operating profit and − capital cost = net."
        ]
        # Every other money line still sums each day it is priced on.
        assert m["realisation"] == round(sum(d["money"]["realisation"] for d in days), 2)
        assert m["stores"] == round(sum(d["money"]["stores"] for d in days), 2)

    def test_no_unpriced_day_no_flag(self):
        mtd = build_daily_pl(_inputs(), BASE_PARAMS, D, D, D)["mtd"]
        assert mtd["flags"]["overheads_priced_days_only"] is False and mtd["notes"] == []
        assert mtd["money"]["overheads"] == 24000.0 and mtd["money"]["operating_profit"] == 1066100.0


class TestParamsUsedCoversStatementAndCapital:
    def test_statement_and_capital_picks_are_listed(self):
        params = FULL_PARAMS + [
            _p(40, "BENCH_STORES_PER_T", 4500),
            _p(20, "OVERHEAD_STAFF_PER_DAY", 50000),
            _p(22, "FIXED_COST_PER_DAY", 999999),  # ignored: a sub-line is keyed
            _p(50, "FG_STOCK_T", 120, product="SACKING", uom="t"),
        ]
        resolved = resolve_params(params, D)
        cap = build_capital([{"value": 1000, "kg": 1}], _fg_rows(), resolved, D, 2)
        data = build_daily_pl(_inputs(), params, D, D, D, cap)
        used = {p["cost_param_id"]: p for p in data["params_used"]}
        assert used[40]["param_code"] == "BENCH_STORES_PER_T" and used[40]["used_in"] == ["statement"]
        assert used[40]["days_used"] == data["statement"]["priced_days"] == 1
        assert used[50]["param_code"] == "FG_STOCK_T" and used[50]["used_in"] == ["capital"]
        assert used[50]["days_used"] == 0 and used[50]["scope"] == "branch"
        assert 22 not in used
        assert used[32]["used_in"] == ["statement"]  # JUTE_PER_KG_CLOTH: the raw-material std
        assert used[33]["used_in"] == ["statement"]  # JUTE_RATE_PER_KG: nothing unvalued that day, benchmark only
        assert used[30]["used_in"] == ["days", "capital"] and used[30]["days_used"] == 1  # COST_OF_CAPITAL_PCT
        assert used[1]["used_in"] == ["days", "capital"]  # SACKING realisation also valued the FG tonnes
        assert used[20]["used_in"] == ["days"]

    def test_no_capital_no_capital_picks(self):
        assert capital_param_picks(None, resolve_params(FULL_PARAMS, D)) == []


class TestUnratedJuteStock:
    STOCK = [{"value": 55348315.35, "kg": 632854, "unrated_kg": 45160, "unrated_lines": 10}]

    def test_query_returns_the_unrated_split(self):
        sql = str(get_jute_stock_value_query())
        assert "AS unrated_kg" in sql and "AS unrated_lines" in sql
        assert "COALESCE(vs.rate, 0) = 0 AND vs.bal_weight > 0" in sql

    def test_unrated_kg_valued_at_zero_with_a_note(self):
        resolved = resolve_params(CAPITAL_PARAMS, D)
        cap = build_capital(self.STOCK, [], resolved, D, 2)
        assert cap["jute_stock_value"] == 55348315.35 and cap["jute_stock_kg"] == 632854.0
        assert cap["jute_stock_unrated_kg"] == 45160.0 and cap["jute_stock_unrated_lines"] == 10
        assert cap["jute_stock_unrated_value"] == 0.0
        assert cap["notes"] == ["45,160 kg of jute stock on 10 MR lines has no rate yet and is valued at ₹0"]
        assert all(p["param_code"] != "JUTE_RATE_PER_KG" for p in capital_param_picks(cap, resolved))

    def test_keyed_jute_rate_prices_the_unrated_kg(self):
        params = CAPITAL_PARAMS + [_p(33, "JUTE_RATE_PER_KG", 60)]
        resolved = resolve_params(params, D)
        cap = build_capital(self.STOCK, [], resolved, D, 2)
        assert cap["jute_stock_unrated_value"] == 2709600.0
        assert cap["jute_stock_value"] == round(55348315.35 + 2709600.0, 2) == 58057915.35
        assert cap["notes"] == [
            "45,160 kg of jute stock on 10 MR lines has no rate yet — valued at the keyed "
            "JUTE_RATE_PER_KG ₹60.00/kg (₹2,709,600.00)"
        ]
        data = build_daily_pl(_inputs(), params, D, D, D, cap)
        assert "capital" in {p["cost_param_id"]: p for p in data["params_used"]}[33]["used_in"]
        assert data["days"][0]["money"]["capital_cost"] == round((58057915.35 + 1000000) * 0.14 / 365, 2)

    def test_no_unrated_kg_no_note(self):
        cap = build_capital(
            [{"value": 10, "kg": 1, "unrated_kg": 0, "unrated_lines": 0}], [], resolve_params(CAPITAL_PARAMS, D), D, 2
        )
        assert cap["notes"] == [] and cap["jute_stock_unrated_kg"] == 0.0 and cap["jute_stock_unrated_lines"] == 0


class TestCapitalDatesAndOneSided:
    def test_fg_as_on_date_to_and_jute_stock_as_of_now(self):
        cap = build_capital([{"value": 1, "kg": 1}], [], resolve_params(CAPITAL_PARAMS, D), D, 2)
        assert cap["as_on"] == "2026-07-12" and cap["jute_stock_as_of"] == "now"
        data = build_daily_pl(_inputs(), BASE_PARAMS, D, D, D)  # capital never computed
        assert data["capital"]["jute_stock_as_of"] == "now" and data["capital"]["as_on"] == "2026-07-12"
        assert data["capital"]["jute_stock_unrated_kg"] is None

    def test_notes_say_which_date_each_component_reads(self):
        assert [n for n in NOTES_ALWAYS if n.startswith("Capital components")] == [
            "Capital components: finished goods as on the To date (bale register); jute stock as of "
            "now (vw_jute_stock_outstanding is not as-of-date). The same base prices every day of the range."
        ]
        assert not any("TODAY" in n for n in NOTES_ALWAYS)

    def test_one_sided_capital_bale_register_only(self):
        resolved = resolve_params(CAPITAL_PARAMS, D)
        cap = build_capital(None, _fg_rows(), resolved, D, 2)
        assert cap["jute_stock_value"] is None and cap["fg_value"] == 3052000.0 and cap["available"] is True
        assert cap["notes"][0] == NOTE_NO_JUTE_STOCK
        data = build_daily_pl(_inputs(), CAPITAL_PARAMS, D, D, D, cap)
        m = data["days"][0]["money"]
        assert m["capital_cost"] == round((3052000.0 + 1000000.0) * 0.14 / 365, 2) == 1554.19
        assert m["net"] == round(m["operating_profit"] - m["capital_cost"], 2)
        head = {h["key"]: h for h in data["statement"]["heads"]}["capital"]
        lines = {line["key"]: line for line in head["lines"]}
        assert head["available"] is True and lines["jute_stock"]["base_value"] is None
        assert lines["jute_stock"]["period"] is None
