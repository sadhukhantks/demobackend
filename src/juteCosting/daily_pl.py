"""Daily P&L / cost per tonne / per-100-spindle report
(GET /api/juteCostReports/daily_pl) — ITM-019, extended by ITM-027 into the
owner's five-head statement.

One row per calendar day of the range: cloth tonnes by product (sacking /
hessian from the weaving quality name), yarn tonnes, hands, spindle-hours,
jute and jute waste issued with their booked value, stores issued at lot rate
by category, dust kg and (when ITM-004's jute_power_reading exists) kWh by
meter type — priced with the costing parameters in force on that day
(params.resolve_params) into

    contribution     = realisation - raw material (jute + jute waste - dust sold)
                       - wages - power - stores                       (heads 1-4)
    operating profit = contribution - overheads                       (head 5)
    net              = operating profit - capital cost                (head 6)

and restated per tonne of cloth, per tonne of yarn and per 100 spindle-hours.
Wages are kept as their own head (the owner's five were raw material, stores,
electricity, overheads, capital): they are the largest conversion cost and the
₹2,000/t overhead rule of thumb cannot contain them. A parameter that is
missing blanks the line it prices and is listed in ``not_keyed`` with the
reason; power missing excludes power from the contribution and flags the day
(``flags.power_excluded``) rather than blanking it, because on dev3 today there
is no power table at all; overheads are never blank — when nothing is keyed
they are ESTIMATED at the benchmark ₹/t x cloth tonnes and flagged
(``flags.overheads_estimated``). No achievement %: the comparison line is
``std``, the 30-day median ending on the To date, so the range queries read
[min(date_from, date_to - 29), date_to] and the days before date_from feed
only the median.

Yarn kg/day reuses dprQueries.get_dpr_spinning_daily_production_query
unchanged; the other per-day inputs are range copies of the DPR / production
builders (daily_pl_query.py, source named per builder) because the DPR
builders are single-date day+MTD. The arithmetic lives in pure functions
(``build_day_inputs`` -> ``money_for_day`` -> ``build_daily_pl``, with
``build_capital`` and ``build_statement`` for the ITM-027 blocks) so it is
testable without a database; the router only validates, queries and wires.

Capital cost (head 6) prices the blocked capital — jute stock at MR rate
(vw_jute_stock_outstanding), finished goods in bale stock at the realisation
rate (bale_stock_query.get_bale_stock_summary_query) and the keyed
WORKING_CAPITAL_OTHER — at COST_OF_CAPITAL_PCT / 365 per day. Finished goods
are taken as on the To date (the bale register summary is an as-of-date
derivation, as_on = date_to); jute stock is the position as of NOW
(vw_jute_stock_outstanding is a live view, not as-of-date —
``data.capital.jute_stock_as_of`` = "now"). The same base is used for every
day of the range; ``data.capital`` carries the components once. The lazy
breakdown dialogs and the jute-issue data check are in daily_pl_breakdown.py
(same prefix).
"""

import statistics
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteCosting.daily_pl_query import (
    STORES_CATEGORIES,
    STORES_CONSUMABLES,
    STORES_GENERAL,
    STORES_PACKING,
    STORES_SPARES,
    get_cloth_by_quality_daily_query,
    get_dust_daily_query,
    get_hands_daily_query,
    get_jute_issue_daily_query,
    get_jute_stock_value_query,
    get_jute_waste_issue_daily_query,
    get_power_daily_by_type_query,
    get_purchased_yarn_daily_query,
    get_spindle_hours_daily_query,
    get_stores_value_daily_by_category_query,
)
from src.juteCosting.params import (
    BENCHMARK_DEFAULTS,
    BENCHMARK_SOURCE,
    PARAM_LABELS,
    param_value,
    resolve_params,
)
from src.juteCosting.params_query import get_cost_param_rows_for_resolution_query
from src.juteProduction.bale_stock import CANCELLED_INVOICE_STATUS_IDS
from src.juteProduction.bale_stock_query import get_bale_stock_summary_query
from src.juteProduction.constants import (
    FINISHING_QUALITY_TYPE_BAG,
    FINISHING_QUALITY_TYPE_CLOTH,
    SPINNING_MACHINE_TYPE_NAME,
)
from src.juteProduction.doff_analysis import (
    _iso,
    _optional_int,
    _require_date,
    _require_int,
    _verify_pair,
)
from src.juteProduction.dprQueries import get_dpr_spinning_daily_production_query

router = APIRouter()

# Two months. Every input is one aggregated row per day, so the cap is about
# screen width (the day table) rather than the database.
MAX_RANGE_DAYS = 62
# The std line is the median of the 30 days ending on date_to.
STD_WINDOW_DAYS = 30

PRODUCT_SACKING = "SACKING"
PRODUCT_HESSIAN = "HESSIAN"
SACKING_PREFIXES = ("SACK", "S4")
PRODUCTS = (PRODUCT_SACKING, PRODUCT_HESSIAN)
PRODUCT_LABELS = {PRODUCT_SACKING: "Sacking", PRODUCT_HESSIAN: "Hessian"}

# Power meter types (jute_power_meter_mst.meter_type; OTHER = reading with no
# master row). GRID and OTHER are priced at POWER_RATE_PER_KWH, DG at
# DG_RATE_PER_KWH (else the grid rate, flagged), SOLAR at SOLAR_RATE_PER_KWH
# (else ₹0 — own generation, flagged).
POWER_GRID, POWER_DG, POWER_SOLAR, POWER_OTHER = "GRID", "DG", "SOLAR", "OTHER"
POWER_TYPES = (POWER_GRID, POWER_DG, POWER_SOLAR, POWER_OTHER)
POWER_TYPE_LABELS = {
    POWER_GRID: "Grid",
    POWER_DG: "DG sets",
    POWER_SOLAR: "Solar",
    POWER_OTHER: "Other meters",
}

STORES_CATEGORY_LABELS = {
    STORES_SPARES: "Machine parts & spares",
    STORES_CONSUMABLES: "Process consumables (oil/ecofin, chemicals, dyes)",
    STORES_PACKING: "Packing material",
    STORES_GENERAL: "General stores & services",
}

# Overheads precedence: keyed sub-lines > FIXED_COST_PER_DAY > estimate at the
# benchmark ₹/t x cloth tonnes (flagged).
OVERHEAD_SUBLINE_CODES = ("OVERHEAD_STAFF_PER_DAY", "OVERHEAD_ADMIN_PER_DAY", "OVERHEAD_OTHER_PER_DAY")
OVERHEAD_TOTAL_CODE = "FIXED_COST_PER_DAY"
OVERHEAD_ESTIMATE_CODE = "BENCH_OVERHEAD_PER_T"
# Jute per kg of cloth when JUTE_PER_KG_CLOTH is not keyed (Sep 2026 gain/loss statement).
JUTE_PER_KG_CLOTH_STD = 1.06
DAYS_PER_YEAR = 365

MONEY_KEYS = (
    "realisation", "jute_cost", "jute_waste_cost", "wages", "power", "stores", "dust_credit",
    "contribution", "overheads", "operating_profit", "capital_cost", "net",
)
PER_T_CLOTH_KEYS = (
    "realisation", "jute_cost", "jute_waste_cost", "wages", "power", "stores", "dust_credit",
    "contribution", "overheads", "operating_profit", "capital_cost", "net",
)
PER_T_YARN_KEYS = ("jute_cost", "wages", "contribution")
SUM_KEYS_3DP = (
    "sacking_t", "hessian_t", "cloth_t", "yarn_t", "yarn_surplus_t", "jute_issued_t",
    "jute_unvalued_t", "jute_waste_t", "dust_kg",
)
SUM_KEYS_2DP = (
    "hands", "hours", "spindle_hours", "jute_issue_value", "stores_value", "dg_hours", "dg_litres",
)
SUM_KEYS_INT = ("zero_value_lines",)
# Period money lines summed over the PRICED days only when some day is
# unpriced, so contribution − overheads − capital cost reconcile (_period_row).
PRICED_DAYS_ONLY_KEYS = ("overheads", "capital_cost")
NULLABLE_SUM_KEYS = {"purchased_yarn_t": 3, "power_kwh": 2}

# not_keyed ordering: the pricing gaps first (power, its DG / solar rates, the
# jute rate, realisation, wages, dust), then the heads that fall back
# (overheads estimate, capital).
NOT_KEYED_ORDER = [
    "power", "DG_RATE_PER_KWH", "SOLAR_RATE_PER_KWH", "JUTE_RATE_PER_KG", "REALISATION_PER_T",
    "WAGE_PER_HAND_DAY", "DUST_SALE_PER_KG", "overheads", "COST_OF_CAPITAL_PCT",
    "WORKING_CAPITAL_OTHER", "capital",
]

NK_POWER_NO_TABLE = {"key": "power", "reason": "power: not keyed — jute_power_reading not available"}
NK_POWER_NO_RATE = {"key": "power", "reason": "power: not keyed — POWER_RATE_PER_KWH missing"}
NK_DG = {
    "key": "DG_RATE_PER_KWH",
    "reason": "DG kWh priced at the grid rate — DG_RATE_PER_KWH not keyed",
}
NK_SOLAR = {
    "key": "SOLAR_RATE_PER_KWH",
    "reason": "solar kWh priced at ₹0 (own generation) — SOLAR_RATE_PER_KWH not keyed",
}
NK_JUTE_RATE = {
    "key": "JUTE_RATE_PER_KG",
    "reason": "jute issued with no booked value and no MR rate, and no jute rate keyed — jute cost blank",
}
NK_WAGE = {"key": "WAGE_PER_HAND_DAY", "reason": "no wage per hand-day keyed — wages and contribution blank"}
NK_DUST = {"key": "DUST_SALE_PER_KG", "reason": "dust produced but no dust sale rate keyed — credit taken as 0"}
NK_CAPITAL_PCT = {
    "key": "COST_OF_CAPITAL_PCT",
    "reason": "cost of capital % not keyed — capital cost and net blank",
}
NK_CAPITAL_OTHER = {
    "key": "WORKING_CAPITAL_OTHER",
    "reason": "other blocked capital (receivables, advances) not keyed — taken as ₹0",
}
NK_CAPITAL_NA = {
    "key": "capital",
    "reason": "jute stock value and finished goods value not available on this tenant — capital cost and net blank",
}


def _nk_overheads(rate_per_t: float) -> dict:
    return {
        "key": "overheads",
        "reason": f"overheads not keyed — estimated at the ₹{rate_per_t:,.0f}/t rule of thumb",
    }


NOTES_ALWAYS = [
    "Product = SACKING when the weaving quality name starts with SACK or S4, otherwise "
    "HESSIAN (spec rule; every dev3 B-Twill quality therefore reads as HESSIAN).",
    "Hands = approved attendance (status 3) worked hours ÷ 8, the DPR rule.",
    "Spindle-hours = Σ spindles × minutes ÷ 60 from the spinning planning grid "
    "(jute_prod_spinning_daily); blank days have no planning rows.",
    "Head 1 raw material = jute issued at its booked issue value (a line with no value is "
    "priced at the keyed JUTE_RATE_PER_KG, else at its MR line rate ₹/qtl) + jute waste "
    "issued at booked value − dust sold at DUST_SALE_PER_KG.",
    "Head 2 stores = every stores issue at the receipt-lot rate, split by item group into "
    "spares / consumables / packing / general.",
    "Head 3 power = kWh by meter type × its rate: grid and unmapped meters at "
    "POWER_RATE_PER_KWH, DG at DG_RATE_PER_KWH (else the grid rate), solar at "
    "SOLAR_RATE_PER_KWH (else ₹0).",
    "Head 4 wages = worker hand-days × WAGE_PER_HAND_DAY — kept as its own head (not in the "
    "owner's five) because it is the largest conversion cost.",
    "Head 5 overheads = keyed OVERHEAD_*_PER_DAY sub-lines, else FIXED_COST_PER_DAY, else "
    "estimated at the ₹2,000/t rule of thumb × cloth tonnes (shown as est.).",
    "Head 6 capital cost = (jute stock at MR rate + finished goods in bale stock at "
    "realisation + WORKING_CAPITAL_OTHER) × COST_OF_CAPITAL_PCT ÷ 365 per day — generally "
    "not costed separately (jute bought on credit carries it in its price).",
    "Contribution = realisation − heads 1–4; operating profit = contribution − overheads; "
    "net = operating profit − capital cost. The statement covers the days whose "
    "contribution is priced; each per-tonne figure is over those days' cloth tonnes.",
    "Capital components: finished goods as on the To date (bale register); jute stock as of "
    "now (vw_jute_stock_outstanding is not as-of-date). The same base prices every day of the range.",
    "Std = 30-day median ending on the To date (no achievement %).",
]
NOTE_NO_DUST = "No dust entries in range (dev3 has no Dust waste item)"
NOTE_NO_PURCHASED = "Purchased yarn is not tracked on this tenant"
NOTE_FG_NEEDS_BRANCH = "Finished goods value needs a branch (the bale register is per branch)"
NOTE_NO_JUTE_STOCK = "Jute stock value not available (vw_jute_stock_outstanding missing)"
NOTE_NO_FG = "Finished goods value not available (bale register missing)"
NOTE_FG_ALL_PRODUCT_ROW = "FG_STOCK_T must be keyed per product (SACKING / HESSIAN) — an all-product row is ignored"
FG_SOURCE_KEYED = "keyed"
FG_SOURCE_REGISTER = "bale_register"
# data.capital.jute_stock_as_of: the stock view is a live position.
JUTE_STOCK_AS_OF = "now"


def _note_unrated_stock(kg: float, lines: int) -> str:
    return f"{kg:,.0f} kg of jute stock on {lines:,} MR lines has no rate yet and is valued at ₹0"


def _note_unrated_stock_priced(kg: float, lines: int, rate: float, value: float) -> str:
    return (
        f"{kg:,.0f} kg of jute stock on {lines:,} MR lines has no rate yet — valued at the keyed "
        f"JUTE_RATE_PER_KG ₹{rate:,.2f}/kg (₹{value:,.2f})"
    )


def _note_priced_days_only(priced: int, total: int) -> str:
    return (
        f"Overheads and capital cost cover the {priced} priced day{'' if priced == 1 else 's'} of "
        f"{total} only (the days whose contribution is priced), so contribution − overheads = "
        "operating profit and − capital cost = net."
    )


def _note_register_coverage(entered: int, despatched: int) -> str:
    return (
        f"bale register: {entered:,} bales entered, {despatched:,} recorded despatched — in-stock "
        "tonnes overstated until despatch / invoice links are keyed (ITM-021); key FG_STOCK_T to override"
    )


def _note_register_overridden(entered: int, despatched: int, keyed_t: float) -> str:
    return (
        f"bale register: {entered:,} bales entered, {despatched:,} recorded despatched — not used, "
        f"FG_STOCK_T keyed ({keyed_t:,.3f} t)"
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def product_of(quality_name) -> str:
    """SACKING when the weaving quality name starts with SACK or S4, else HESSIAN."""
    name = (str(quality_name).strip().upper()) if quality_name else ""
    return PRODUCT_SACKING if name.startswith(SACKING_PREFIXES) else PRODUCT_HESSIAN


def product_of_bale(quality_type, item_name) -> str:
    """Product of a bale-stock row: the finishing quality_type when it is one
    of the two known types (2 = sacking bags, 1 = hessian cloth), else the
    cloth name rule (product_of) on the item name."""
    try:
        qt = int(quality_type) if quality_type is not None else None
    except (TypeError, ValueError):
        qt = None
    if qt == FINISHING_QUALITY_TYPE_BAG:
        return PRODUCT_SACKING
    if qt == FINISHING_QUALITY_TYPE_CLOTH:
        return PRODUCT_HESSIAN
    return product_of(item_name)


def _f(v, default: float = 0.0) -> float:
    """DECIMAL / None -> float."""
    return default if v is None else float(v)


def _round(v, dp: int) -> Optional[float]:
    return None if v is None else round(float(v), dp)


def _ratio(num, den, dp: int = 2) -> Optional[float]:
    """num / den rounded; None when either side is missing or the denominator
    is zero — a blank, never a fake 0 that would drag a chart to the floor."""
    if num is None or not den:
        return None
    return round(float(num) / float(den), dp)


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _rows(db: Session, query, binds: dict) -> List[dict]:
    return [dict(r._mapping) for r in db.execute(query, binds).fetchall()]


# MySQL "the schema lacks it" errors: 1146 no such table (PyMySQL ProgrammingError),
# 1054 unknown column (PyMySQL OperationalError). Only these mean "optional
# input not on this tenant"; anything else is a defect and must surface.
OPTIONAL_TABLE_ERRNOS = (1146, 1054)


def _mysql_errno(e: Exception) -> Optional[int]:
    """The MySQL errno of a DBAPI error wrapped by SQLAlchemy (orig.args[0]), else None."""
    args = getattr(getattr(e, "orig", None), "args", None) or ()
    try:
        return int(args[0]) if args else None
    except (TypeError, ValueError):
        return None


def _fetch_optional(db: Session, query, binds: dict) -> Optional[List[dict]]:
    """Rows, or None when the table / column is absent on this tenant (MySQL
    1146 / 1054). The failed statement would otherwise poison every later
    query, so roll back first. Any other OperationalError / ProgrammingError
    (syntax, lost connection, lock wait, bad column after a schema change) is
    re-raised and becomes a 500 — a missing cost line must never pass for
    "not tracked"."""
    try:
        return _rows(db, query, binds)
    except (OperationalError, ProgrammingError) as e:
        db.rollback()
        if _mysql_errno(e) not in OPTIONAL_TABLE_ERRNOS:
            raise
        return None


def parse_range_params(request: Request) -> Tuple[int, Optional[int], date, date]:
    """co_id, branch_id, date_from, date_to from the query string, validated
    BEFORE any session is touched (400 on anything missing / malformed /
    inverted / longer than MAX_RANGE_DAYS). Shared by the report, the lazy
    breakdown and the jute-issue check."""
    co_id = _require_int(request, "co_id")
    branch_id = _optional_int(request, "branch_id")
    d_from = _require_date(request, "date_from")
    d_to = _require_date(request, "date_to")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large — at most {MAX_RANGE_DAYS} days",
        )
    return co_id, branch_id, d_from, d_to


# ---------------------------------------------------------------------------
# Day inputs
# ---------------------------------------------------------------------------


def _empty_input(d: date, purchased_tracked: bool, power_tracked: bool) -> dict:
    return {
        "date": d,
        "sacking_t": 0.0,
        "hessian_t": 0.0,
        "cloth_t": 0.0,
        "yarn_t": 0.0,
        "purchased_yarn_t": 0.0 if purchased_tracked else None,
        "hands": 0.0,
        "hours": 0.0,
        "spindle_hours": 0.0,
        "jute_issued_t": 0.0,
        "jute_issue_value": 0.0,
        # Weight issued with no booked value, and the part of it the MR line
        # rate (₹/qtl) can price.
        "jute_unvalued_t": 0.0,
        "jute_mr_rated_t": 0.0,
        "jute_mr_rate_value": 0.0,
        "zero_value_lines": 0,
        "jute_waste_t": 0.0,
        "jute_waste_value": 0.0,
        "stores_value": 0.0,
        "stores_by_category": {c: 0.0 for c in STORES_CATEGORIES},
        "dust_kg": 0.0,
        "power_kwh": 0.0 if power_tracked else None,
        "power_by_type": {t: 0.0 for t in POWER_TYPES} if power_tracked else None,
        "dg_hours": 0.0,
        "dg_litres": 0.0,
    }


def build_day_inputs(
    d_from_window: date,
    d_to: date,
    cloth_rows: Optional[list],
    yarn_rows: Optional[list],
    hands_rows: Optional[list],
    spindle_rows: Optional[list],
    jute_rows: Optional[list],
    stores_rows: Optional[list],
    dust_rows: Optional[list],
    purchased_rows: Optional[list],
    power_rows: Optional[list],
    waste_rows: Optional[list] = None,
) -> Dict[date, dict]:
    """One input dict per calendar day of [d_from_window, d_to] (zeros when a
    day has no rows). purchased_yarn_t / power_kwh (and power_by_type) are
    None for EVERY day when their rows are None (table not on this tenant) —
    None means "cannot tell", 0 means "nothing booked". A stores row with no
    / unknown ``category`` counts as SPARES and a power row with no / unknown
    ``meter_type`` as OTHER (the SQL defaults, mirrored)."""
    out: Dict[date, dict] = {}
    d = d_from_window
    while d <= d_to:
        out[d] = _empty_input(d, purchased_rows is not None, power_rows is not None)
        d += timedelta(days=1)

    def _inp(r: dict) -> Optional[dict]:
        dd = _as_date(r.get("tran_date"))
        return out.get(dd) if dd is not None else None

    for r in cloth_rows or []:
        inp = _inp(r)
        if inp is None:
            continue
        t = _f(r.get("production_mt"))
        if product_of(r.get("weaving_quality_name")) == PRODUCT_SACKING:
            inp["sacking_t"] += t
        else:
            inp["hessian_t"] += t
    for r in yarn_rows or []:
        inp = _inp(r)
        if inp is not None:
            inp["yarn_t"] += _f(r.get("production_kg")) / 1000.0
    for r in hands_rows or []:
        inp = _inp(r)
        if inp is not None:
            inp["hands"] += _f(r.get("hands"))
            inp["hours"] += _f(r.get("hours"))
    for r in spindle_rows or []:
        inp = _inp(r)
        if inp is not None:
            inp["spindle_hours"] += _f(r.get("spindle_hours"))
    for r in jute_rows or []:
        inp = _inp(r)
        if inp is not None:
            inp["jute_issued_t"] += _f(r.get("issued_kg")) / 1000.0
            inp["jute_issue_value"] += _f(r.get("issue_value"))
            inp["jute_unvalued_t"] += _f(r.get("unvalued_kg")) / 1000.0
            inp["jute_mr_rated_t"] += _f(r.get("mr_rated_kg")) / 1000.0
            inp["jute_mr_rate_value"] += _f(r.get("mr_rate_value"))
            inp["zero_value_lines"] += int(_f(r.get("zero_value_lines")))
    for r in waste_rows or []:
        inp = _inp(r)
        if inp is not None:
            inp["jute_waste_t"] += _f(r.get("waste_kg")) / 1000.0
            inp["jute_waste_value"] += _f(r.get("waste_value"))
    for r in stores_rows or []:
        inp = _inp(r)
        if inp is not None:
            cat = str(r.get("category") or STORES_SPARES).upper()
            if cat not in STORES_CATEGORIES:
                cat = STORES_SPARES
            v = _f(r.get("stores_value"))
            inp["stores_value"] += v
            inp["stores_by_category"][cat] += v
    for r in dust_rows or []:
        inp = _inp(r)
        if inp is not None:
            inp["dust_kg"] += _f(r.get("dust_kg"))
    for r in purchased_rows or []:
        inp = _inp(r)
        if inp is not None:
            inp["purchased_yarn_t"] = (inp["purchased_yarn_t"] or 0.0) + _f(r.get("purchased_kg")) / 1000.0
    for r in power_rows or []:
        inp = _inp(r)
        if inp is not None:
            mt = str(r.get("meter_type") or POWER_OTHER).upper()
            if mt not in POWER_TYPES:
                mt = POWER_OTHER
            kwh = _f(r.get("power_kwh"))
            inp["power_kwh"] = (inp["power_kwh"] or 0.0) + kwh
            inp["power_by_type"][mt] += kwh
            inp["dg_hours"] += _f(r.get("dg_hours"))
            inp["dg_litres"] += _f(r.get("dg_litres"))

    for inp in out.values():
        inp["sacking_t"] = round(inp["sacking_t"], 3)
        inp["hessian_t"] = round(inp["hessian_t"], 3)
        inp["cloth_t"] = round(inp["sacking_t"] + inp["hessian_t"], 3)
        inp["yarn_t"] = round(inp["yarn_t"], 3)
        inp["jute_issued_t"] = round(inp["jute_issued_t"], 3)
        inp["jute_unvalued_t"] = round(inp["jute_unvalued_t"], 3)
        inp["jute_mr_rated_t"] = round(inp["jute_mr_rated_t"], 3)
        inp["jute_waste_t"] = round(inp["jute_waste_t"], 3)
        if inp["purchased_yarn_t"] is not None:
            inp["purchased_yarn_t"] = round(inp["purchased_yarn_t"], 3)
    return out


# ---------------------------------------------------------------------------
# Capital base (as-on, shared by every day)
# ---------------------------------------------------------------------------


def _bench(resolved: dict, code: str, key: str) -> Tuple[float, str, Optional[dict]]:
    """(₹/t, label, pick): the keyed BENCH_* value labelled "keyed", else the
    BENCHMARK_DEFAULTS entry labelled BENCHMARK_SOURCE."""
    pick = param_value(resolved, code)
    if pick is not None:
        return float(pick["value"]), "keyed", pick
    return float(BENCHMARK_DEFAULTS[key]), BENCHMARK_SOURCE, None


def build_capital(
    stock_rows: Optional[list],
    fg_rows: Optional[list],
    resolved: dict,
    as_on: date,
    branch_id: Optional[int],
) -> dict:
    """The blocked-capital components, read once for the range.

    stock_rows: the one-row result of get_jute_stock_value_query (None = view
    not on this tenant). fg_rows: get_bale_stock_summary_query rows (None =
    bale register not on this tenant, or no branch given — the register is
    per branch). Finished goods tonnes come from the keyed FG_STOCK_T rows
    (per product) when any is in force on ``as_on`` — ``fg_source`` "keyed" —
    else from the register's in-stock total_kg per product (product_of_bale)
    — ``fg_source`` "bale_register"; either way each product's tonnes are
    priced at its resolved REALISATION_PER_T. The register's coverage
    (``bales_entered`` = in stock + despatched, ``bales_despatched``) is
    always reported from the same pull, with a note: on sls despatches /
    invoice links are not keyed (ITM-021), so the derived in-stock tonnes
    overstate real stock until FG_STOCK_T is keyed. ``other`` is the keyed
    WORKING_CAPITAL_OTHER (0 when not keyed), ``pct`` the keyed
    COST_OF_CAPITAL_PCT (None when not keyed). ``available`` = a capital cost
    can be computed (pct keyed and at least one stock value known).

    Jute stock lines with no rate yet (``unrated_kg`` / ``unrated_lines`` of
    the stock row) count in the kg but are valued at ₹0 — said in a note —
    unless JUTE_RATE_PER_KG is keyed, which then prices those kg (added to
    jute_stock_value, ``jute_stock_unrated_value``; the note says so).
    ``as_on`` is the finished-goods date (the bale register summary is
    as-of-date); ``jute_stock_as_of`` is "now" (the stock view is live).
    """
    notes: List[str] = []
    jute_value = jute_kg = None
    unrated_kg = unrated_value = None
    unrated_lines = None
    if stock_rows is None:
        notes.append(NOTE_NO_JUTE_STOCK)
    else:
        r = stock_rows[0] if stock_rows else {}
        jute_value = round(_f(r.get("value")), 2)
        jute_kg = round(_f(r.get("kg")), 3)
        unrated_kg = round(_f(r.get("unrated_kg")), 3)
        unrated_lines = int(_f(r.get("unrated_lines")))
        unrated_value = 0.0
        if unrated_kg > 0:
            rate_pick = param_value(resolved, "JUTE_RATE_PER_KG")
            if rate_pick is None:
                notes.append(_note_unrated_stock(unrated_kg, unrated_lines))
            else:
                unrated_value = round(unrated_kg * float(rate_pick["value"]), 2)
                jute_value = round(jute_value + unrated_value, 2)
                notes.append(
                    _note_unrated_stock_priced(unrated_kg, unrated_lines, float(rate_pick["value"]), unrated_value)
                )

    fg_value = fg_t = None
    fg_source = None
    bales_entered = bales_despatched = None
    fg_by_product = {p: {"tonnes": 0.0, "rate": None, "value": None} for p in PRODUCTS}
    keyed = {p: resolved.get(("FG_STOCK_T", p)) for p in PRODUCTS}
    if fg_rows is not None:
        bales_despatched = int(sum(_f(r.get("despatched_bales")) for r in fg_rows))
        bales_entered = int(sum(_f(r.get("bales")) for r in fg_rows)) + bales_despatched
    if any(keyed.values()):
        fg_source = FG_SOURCE_KEYED
        for p, pick in keyed.items():
            if pick is not None:
                fg_by_product[p]["tonnes"] = float(pick["value"])
        if resolved.get(("FG_STOCK_T", None)) is not None:
            notes.append(NOTE_FG_ALL_PRODUCT_ROW)
    elif fg_rows is None:
        notes.append(NOTE_FG_NEEDS_BRANCH if branch_id is None else NOTE_NO_FG)
    else:
        fg_source = FG_SOURCE_REGISTER
        for r in fg_rows:
            p = product_of_bale(r.get("quality_type"), r.get("item_name"))
            fg_by_product[p]["tonnes"] += _f(r.get("total_kg")) / 1000.0
    if fg_source is not None:
        fg_t = 0.0
        valued = 0.0
        unvalued_t = 0.0
        for p, e in fg_by_product.items():
            e["tonnes"] = round(e["tonnes"], 3)
            fg_t += e["tonnes"]
            pick = param_value(resolved, "REALISATION_PER_T", p)
            if pick is not None:
                e["rate"] = pick["value"]
                e["value"] = round(e["tonnes"] * pick["value"], 2)
                valued += e["value"]
            elif e["tonnes"] > 0:
                unvalued_t += e["tonnes"]
                notes.append(f"{PRODUCT_LABELS[p]} bales in stock have no REALISATION_PER_T — not valued")
        fg_t = round(fg_t, 3)
        # Bales in stock that nothing could value: unknown, not ₹0.
        fg_value = None if (fg_t > 0 and valued == 0 and unvalued_t > 0) else round(valued, 2)
    if bales_entered:
        if fg_source == FG_SOURCE_KEYED:
            notes.append(_note_register_overridden(bales_entered, bales_despatched, fg_t or 0.0))
        else:
            notes.append(_note_register_coverage(bales_entered, bales_despatched))

    pct = param_value(resolved, "COST_OF_CAPITAL_PCT")
    other = param_value(resolved, "WORKING_CAPITAL_OTHER")
    return {
        "jute_stock_value": jute_value,
        "jute_stock_kg": jute_kg,
        "jute_stock_unrated_kg": unrated_kg,
        "jute_stock_unrated_lines": unrated_lines,
        "jute_stock_unrated_value": unrated_value,
        "jute_stock_as_of": JUTE_STOCK_AS_OF,
        "fg_value": fg_value,
        "fg_t": fg_t,
        "fg_source": fg_source,
        "bales_entered": bales_entered,
        "bales_despatched": bales_despatched,
        "fg_by_product": fg_by_product,
        "other": float(other["value"]) if other is not None else 0.0,
        "pct": float(pct["value"]) if pct is not None else None,
        "available": pct is not None and (jute_value is not None or fg_value is not None),
        "as_on": _iso(as_on),
        "notes": notes,
    }


def capital_param_picks(capital: Optional[dict], resolved: dict) -> List[dict]:
    """The parameter picks build_capital consulted to produce ``capital``
    (``resolved`` = the set it was given, on the To date) — for params_used:
    COST_OF_CAPITAL_PCT and WORKING_CAPITAL_OTHER when keyed, the per-product
    FG_STOCK_T rows when they set the tonnes (fg_source "keyed"; an ignored
    all-product row is not listed), the REALISATION_PER_T pick that valued
    each product's tonnes, and JUTE_RATE_PER_KG when it priced unrated jute
    stock. Read back from build_capital's own outputs (fg_source,
    fg_by_product rates, jute_stock_unrated_value) so the two cannot drift."""
    if capital is None:
        return []
    picks: List[dict] = []
    for code in ("COST_OF_CAPITAL_PCT", "WORKING_CAPITAL_OTHER"):
        pick = param_value(resolved, code)
        if pick is not None:
            picks.append(pick)
    if capital.get("fg_source") == FG_SOURCE_KEYED:
        for p in PRODUCTS:
            pick = resolved.get(("FG_STOCK_T", p))
            if pick is not None:
                picks.append(pick)
    if capital.get("fg_source") is not None:
        for p in PRODUCTS:
            if (capital.get("fg_by_product") or {}).get(p, {}).get("rate") is not None:
                pick = param_value(resolved, "REALISATION_PER_T", p)
                if pick is not None:
                    picks.append(pick)
    if (capital.get("jute_stock_unrated_value") or 0) > 0:
        pick = param_value(resolved, "JUTE_RATE_PER_KG")
        if pick is not None:
            picks.append(pick)
    return picks


def _capital_unavailable(as_on: date) -> dict:
    return {
        "jute_stock_value": None,
        "jute_stock_kg": None,
        "jute_stock_unrated_kg": None,
        "jute_stock_unrated_lines": None,
        "jute_stock_unrated_value": None,
        "jute_stock_as_of": JUTE_STOCK_AS_OF,
        "fg_value": None,
        "fg_t": None,
        "fg_source": None,
        "bales_entered": None,
        "bales_despatched": None,
        "fg_by_product": {p: {"tonnes": 0.0, "rate": None, "value": None} for p in PRODUCTS},
        "other": 0.0,
        "pct": None,
        "available": False,
        "as_on": _iso(as_on),
        "notes": [NK_CAPITAL_NA["reason"]],
    }


# ---------------------------------------------------------------------------
# Money for one day
# ---------------------------------------------------------------------------


def money_for_day(inp: dict, resolved: dict, capital: Optional[dict] = None) -> dict:
    """Price one day's inputs with the resolved parameters.

    Returns {"money": DayMoney, "flags": {...}, "not_keyed": [...], "used": [picks],
    "detail": {...}}. DayMoney = the MONEY_KEYS (2 dp, None where the pricing
    parameter is missing). ``used`` lists every pick consulted (for
    params_used); ``detail`` carries the day's sub-lines (realisation by
    product, power by type, stores by category, overhead lines) for the day
    row and the statement. ``capital`` is build_capital's dict (None = not
    computed): its stock values are the same every day, the pct and the
    other-capital are resolved per day.
    """
    not_keyed: List[dict] = []
    used: Dict[int, dict] = {}
    flags = {
        "power_excluded": False,
        "realisation_partial": False,
        "dg_at_grid_rate": False,
        "solar_at_zero": False,
        "overheads_estimated": False,
    }

    def _use(code: str, product: Optional[str] = None) -> Optional[dict]:
        pick = param_value(resolved, code, product)
        if pick is not None:
            used.setdefault(pick["cost_param_id"], pick)
        return pick

    # Realisation: Σ product tonnes x the product's rate. A produced product with
    # no rate is skipped and flagged; None only when nothing produced had a rate.
    realisation: Optional[float] = 0.0
    produced = priced = 0
    realisation_by_product: Dict[str, dict] = {}
    for product, tonnes in ((PRODUCT_SACKING, inp["sacking_t"]), (PRODUCT_HESSIAN, inp["hessian_t"])):
        entry = {"tonnes": round(tonnes, 3), "rate": None, "amount": 0.0}
        realisation_by_product[product] = entry
        if tonnes <= 0:
            continue
        produced += 1
        pick = _use("REALISATION_PER_T", product)
        if pick is None:
            entry["amount"] = None
            flags["realisation_partial"] = True
            not_keyed.append(
                {"key": f"REALISATION_PER_T:{product}", "reason": f"no realisation rate for {product}"}
            )
            continue
        priced += 1
        entry["rate"] = pick["value"]
        entry["amount"] = round(tonnes * pick["value"], 2)
        realisation += tonnes * pick["value"]
    if produced and not priced:
        realisation = None

    # Jute: the value booked on each issue line, plus the lines that carry no
    # value priced LINE-WISE — at the keyed JUTE_RATE_PER_KG when there is
    # one (the override), else at their MR line rate (₹/qtl, "live from MRs").
    # Weight that neither can price blanks the line (never a fake 0).
    # Nothing issued = nothing to price.
    jute_cost: Optional[float] = inp["jute_issue_value"]
    unvalued_kg = inp["jute_unvalued_t"] * 1000.0
    if unvalued_kg > 0:
        pick = _use("JUTE_RATE_PER_KG")
        if pick is not None:
            jute_cost += unvalued_kg * pick["value"]
        elif round(inp["jute_unvalued_t"] - inp["jute_mr_rated_t"], 3) > 0:
            jute_cost = None
            not_keyed.append(dict(NK_JUTE_RATE))
        else:
            jute_cost += inp["jute_mr_rate_value"]

    # Jute waste issued: booked value only (0 when none).
    jute_waste_cost = inp["jute_waste_value"]

    pick = _use("WAGE_PER_HAND_DAY")
    if pick is None:
        wages: Optional[float] = None
        not_keyed.append(dict(NK_WAGE))
    else:
        wages = inp["hands"] * pick["value"]

    # Power by meter type. The grid rate is the gate (no grid rate = power not
    # priced, as before); DG and SOLAR fall back when their own rate is not
    # keyed and are flagged only when such kWh exist on the day.
    power: Optional[float] = None
    power_by_type = {t: {"kwh": None, "cost": None} for t in POWER_TYPES}
    if inp["power_kwh"] is None:
        not_keyed.append(dict(NK_POWER_NO_TABLE))
    else:
        kwh = inp["power_by_type"]
        for t in POWER_TYPES:
            power_by_type[t]["kwh"] = round(kwh[t], 2)
        grid = _use("POWER_RATE_PER_KWH")
        if grid is None:
            not_keyed.append(dict(NK_POWER_NO_RATE))
        else:
            rates = {POWER_GRID: grid["value"], POWER_OTHER: grid["value"], POWER_DG: grid["value"], POWER_SOLAR: 0.0}
            if kwh[POWER_DG] > 0:
                dg = _use("DG_RATE_PER_KWH")
                if dg is None:
                    flags["dg_at_grid_rate"] = True
                    not_keyed.append(dict(NK_DG))
                else:
                    rates[POWER_DG] = dg["value"]
            if kwh[POWER_SOLAR] > 0:
                solar = _use("SOLAR_RATE_PER_KWH")
                if solar is None:
                    flags["solar_at_zero"] = True
                    not_keyed.append(dict(NK_SOLAR))
                else:
                    rates[POWER_SOLAR] = solar["value"]
            power = 0.0
            for t in POWER_TYPES:
                cost = kwh[t] * rates[t]
                power_by_type[t]["cost"] = round(cost, 2)
                power += cost
    if power is None:
        flags["power_excluded"] = True

    stores = inp["stores_value"]
    stores_by_category = {c: round(v, 2) for c, v in inp["stores_by_category"].items()}

    dust_credit = 0.0
    if inp["dust_kg"] > 0:
        pick = _use("DUST_SALE_PER_KG")
        if pick is None:
            not_keyed.append(dict(NK_DUST))
        else:
            dust_credit = inp["dust_kg"] * pick["value"]

    contribution: Optional[float] = None
    if realisation is not None and jute_cost is not None and wages is not None:
        contribution = (
            realisation - jute_cost - jute_waste_cost - wages - (power or 0.0) - stores + dust_credit
        )

    # Overheads: keyed sub-lines > FIXED_COST_PER_DAY > estimate (flagged).
    overheads = 0.0
    overheads_lines: List[dict] = []
    sub_picks = [(code, _use(code)) for code in OVERHEAD_SUBLINE_CODES]
    keyed_subs = [(code, p) for code, p in sub_picks if p is not None]
    if keyed_subs:
        for code, p in keyed_subs:
            overheads += p["value"]
            overheads_lines.append(
                {"code": code, "label": PARAM_LABELS[code], "value_per_day": p["value"], "amount": round(p["value"], 2)}
            )
    else:
        total = _use(OVERHEAD_TOTAL_CODE)
        if total is not None:
            overheads = total["value"]
            overheads_lines.append(
                {
                    "code": OVERHEAD_TOTAL_CODE,
                    "label": PARAM_LABELS[OVERHEAD_TOTAL_CODE],
                    "value_per_day": total["value"],
                    "amount": round(total["value"], 2),
                }
            )
        else:
            rate_per_t, _label, bench_pick = _bench(resolved, OVERHEAD_ESTIMATE_CODE, "overheads")
            if bench_pick is not None:
                used.setdefault(bench_pick["cost_param_id"], bench_pick)
            overheads = rate_per_t * inp["cloth_t"]
            flags["overheads_estimated"] = True
            not_keyed.append(_nk_overheads(rate_per_t))
            overheads_lines.append(
                {
                    "code": OVERHEAD_ESTIMATE_CODE,
                    "label": f"Estimated at ₹{rate_per_t:,.0f}/t of cloth (rule of thumb)",
                    "value_per_day": None,
                    "rate_per_t": rate_per_t,
                    "amount": round(overheads, 2),
                }
            )

    operating_profit = contribution - overheads if contribution is not None else None

    # Capital cost: blocked capital x pct / 365 per day. None when the
    # components are unknown or the pct is not keyed.
    capital_cost: Optional[float] = None
    if capital is None or (capital["jute_stock_value"] is None and capital["fg_value"] is None):
        not_keyed.append(dict(NK_CAPITAL_NA))
    else:
        pct = _use("COST_OF_CAPITAL_PCT")
        if pct is None:
            not_keyed.append(dict(NK_CAPITAL_PCT))
        else:
            other = _use("WORKING_CAPITAL_OTHER")
            if other is None:
                not_keyed.append(dict(NK_CAPITAL_OTHER))
            base = (
                (capital["jute_stock_value"] or 0.0)
                + (capital["fg_value"] or 0.0)
                + (other["value"] if other is not None else 0.0)
            )
            capital_cost = base * pct["value"] / 100.0 / DAYS_PER_YEAR

    net = (
        operating_profit - capital_cost
        if (operating_profit is not None and capital_cost is not None)
        else None
    )

    money = {
        "realisation": _round(realisation, 2),
        "jute_cost": _round(jute_cost, 2),
        "jute_waste_cost": _round(jute_waste_cost, 2),
        "wages": _round(wages, 2),
        "power": _round(power, 2),
        "stores": _round(stores, 2),
        "dust_credit": _round(dust_credit, 2),
        "contribution": _round(contribution, 2),
        "overheads": _round(overheads, 2),
        "operating_profit": _round(operating_profit, 2),
        "capital_cost": _round(capital_cost, 2),
        "net": _round(net, 2),
    }
    detail = {
        "realisation_by_product": realisation_by_product,
        "power_by_type": power_by_type,
        "stores_by_category": stores_by_category,
        "overheads_lines": overheads_lines,
    }
    return {"money": money, "flags": flags, "not_keyed": not_keyed, "used": list(used.values()), "detail": detail}


def _per_lines(money: dict, cloth_t: float, yarn_t: float, spindle_hours: float) -> dict:
    return {
        "per_t_cloth": {k: _ratio(money.get(k), cloth_t) for k in PER_T_CLOTH_KEYS},
        "per_t_yarn": {k: _ratio(money.get(k), yarn_t) for k in PER_T_YARN_KEYS},
        "per_100_spindles": {
            "contribution_per_100_spindle_hours": _ratio(money.get("contribution"), spindle_hours / 100.0),
            "contribution_per_100_spindle_days": _ratio(
                money.get("contribution"), spindle_hours / 24.0 / 100.0
            ),
        },
    }


def _day_row(inp: dict, m: dict) -> dict:
    purchased = inp["purchased_yarn_t"]
    row = {
        "date": _iso(inp["date"]),
        "weekday": inp["date"].strftime("%a"),
        "sacking_t": round(inp["sacking_t"], 3),
        "hessian_t": round(inp["hessian_t"], 3),
        "cloth_t": round(inp["cloth_t"], 3),
        "yarn_t": round(inp["yarn_t"], 3),
        "purchased_yarn_t": _round(purchased, 3),
        "yarn_surplus_t": round(inp["yarn_t"] + (purchased or 0.0) - inp["cloth_t"], 3),
        "hands": round(inp["hands"], 2),
        "hours": round(inp["hours"], 2),
        "spindle_hours": round(inp["spindle_hours"], 2),
        "jute_issued_t": round(inp["jute_issued_t"], 3),
        "jute_issue_value": round(inp["jute_issue_value"], 2),
        "jute_unvalued_t": round(inp["jute_unvalued_t"], 3),
        "zero_value_lines": int(inp["zero_value_lines"]),
        "jute_waste_t": round(inp["jute_waste_t"], 3),
        "stores_value": round(inp["stores_value"], 2),
        "dust_kg": round(inp["dust_kg"], 3),
        "power_kwh": _round(inp["power_kwh"], 2),
        "dg_hours": round(inp["dg_hours"], 2),
        "dg_litres": round(inp["dg_litres"], 2),
        "money": m["money"],
        "flags": m["flags"],
        "realisation_by_product": m["detail"]["realisation_by_product"],
        "power_by_type": m["detail"]["power_by_type"],
        "stores_by_category": m["detail"]["stores_by_category"],
        "overheads_lines": m["detail"]["overheads_lines"],
    }
    row.update(_per_lines(m["money"], inp["cloth_t"], inp["yarn_t"], inp["spindle_hours"]))
    return row


# ---------------------------------------------------------------------------
# Period, std, roll-ups
# ---------------------------------------------------------------------------


def _sum_or_none(vals: list) -> Optional[float]:
    """Sum of the non-None values; None when every value is None."""
    present = [v for v in vals if v is not None]
    return sum(present) if present else None


def _sum_nullable(vals: list) -> Optional[float]:
    """Sum, or None as soon as one value is None (cannot tell)."""
    if any(v is None for v in vals):
        return None
    return sum(vals)


def _priced_ratio(days: List[dict], money_key: str, qty_key: str, den_div: float = 1.0) -> Optional[float]:
    """Σ money[money_key] ÷ (Σ qty_key ÷ den_div) over the days whose money_key
    is priced (not None). A day that could not be priced leaves BOTH sides
    out, so its tonnes cannot dilute the per-unit figure of the priced days
    (two 10 t days, one unpriced, one at 148,000 ₹/t -> 148,000, not 74,000)."""
    priced = [d for d in days if d["money"][money_key] is not None]
    if not priced:
        return None
    return _ratio(
        sum(d["money"][money_key] for d in priced),
        sum(d[qty_key] for d in priced) / den_div,
    )


def _merge_overhead_lines(days: List[dict]) -> List[dict]:
    """The overhead lines used over the days, one per code: value_per_day (the
    latest keyed value), the number of days the line applied and the amount."""
    merged: Dict[str, dict] = {}
    for d in days:
        for line in d["overheads_lines"]:
            e = merged.get(line["code"])
            if e is None:
                e = merged[line["code"]] = {
                    "code": line["code"],
                    "label": line["label"],
                    "value_per_day": line.get("value_per_day"),
                    "rate_per_t": line.get("rate_per_t"),
                    "days": 0,
                    "amount": 0.0,
                }
            if line.get("value_per_day") is not None:
                e["value_per_day"] = line["value_per_day"]
            e["days"] += 1
            e["amount"] += line["amount"] or 0.0
    for e in merged.values():
        e["amount"] = round(e["amount"], 2)
    return list(merged.values())


def _period_row(days: List[dict], d_from: date) -> dict:
    """The range summed. Physical inputs sum every day; each money line sums
    the days it is priced on; every per-unit line and the contribution per
    producing day are restated over the priced days ONLY (see _priced_ratio).
    ``unpriced_days`` counts the days whose contribution is None and
    ``flags.contribution_partial`` says so, so the page can say why the
    totals cover fewer days than the range. The sub-line roll-ups
    (stores_by_category, power_by_type, realisation_by_product,
    overheads_lines) follow the same rules as their money line.

    Overheads and capital cost are known on EVERY day, but the operating
    profit and net they feed are only known on the priced days — so when
    some day is unpriced (``flags.contribution_partial``) both sum the PRICED
    days only (with their per-tonne figures and overheads_lines), flagged
    ``flags.overheads_priced_days_only`` with a line in ``notes``; and
    operating_profit / net are derived from the summed lines (contribution −
    overheads, − capital cost, rounded once) so the identities hold to the
    paisa. With no priced day at all nothing is reconciled and both sum
    every day."""
    out: dict = {"label": "MTD" if d_from.day == 1 else "Period"}
    priced_days = [d for d in days if d["money"]["contribution"] is not None]
    priced_only = bool(priced_days) and len(priced_days) < len(days)
    cost_days = priced_days if priced_only else days
    for k in SUM_KEYS_3DP:
        out[k] = round(sum(d[k] for d in days), 3)
    for k in SUM_KEYS_2DP:
        out[k] = round(sum(d[k] for d in days), 2)
    for k in SUM_KEYS_INT:
        out[k] = int(sum(d[k] for d in days))
    for k, dp in NULLABLE_SUM_KEYS.items():
        out[k] = _round(_sum_nullable([d[k] for d in days]), dp)
    money = {k: _round(_sum_or_none([d["money"][k] for d in days]), 2) for k in MONEY_KEYS}
    for k in PRICED_DAYS_ONLY_KEYS:
        money[k] = _round(_sum_or_none([d["money"][k] for d in cost_days]), 2)
    contribution = money["contribution"]
    money["operating_profit"] = (
        None if contribution is None else round(contribution - (money["overheads"] or 0.0), 2)
    )
    money["net"] = (
        None
        if (money["operating_profit"] is None or money["capital_cost"] is None)
        else round(money["operating_profit"] - money["capital_cost"], 2)
    )
    out["money"] = money
    out["per_t_cloth"] = {k: _priced_ratio(days, k, "cloth_t") for k in PER_T_CLOTH_KEYS}
    for k in PRICED_DAYS_ONLY_KEYS + ("operating_profit", "net"):
        basis = cost_days if k in PRICED_DAYS_ONLY_KEYS else days
        out["per_t_cloth"][k] = _ratio(
            money[k], sum(d["cloth_t"] for d in basis if d["money"][k] is not None)
        )
    out["per_t_yarn"] = {k: _priced_ratio(days, k, "yarn_t") for k in PER_T_YARN_KEYS}
    out["per_100_spindles"] = {
        "contribution_per_100_spindle_hours": _priced_ratio(days, "contribution", "spindle_hours", 100.0),
        "contribution_per_100_spindle_days": _priced_ratio(days, "contribution", "spindle_hours", 2400.0),
    }
    out["stores_by_category"] = {
        c: round(sum(d["stores_by_category"][c] for d in days), 2) for c in STORES_CATEGORIES
    }
    out["power_by_type"] = {
        t: {
            "kwh": _round(_sum_nullable([d["power_by_type"][t]["kwh"] for d in days]), 2),
            "cost": _round(_sum_or_none([d["power_by_type"][t]["cost"] for d in days]), 2),
        }
        for t in POWER_TYPES
    }
    out["realisation_by_product"] = {}
    for p in PRODUCTS:
        entries = [d["realisation_by_product"][p] for d in days]
        amount = _sum_or_none([e["amount"] for e in entries])
        tonnes = sum(e["tonnes"] for e in entries if e["amount"] is not None)
        out["realisation_by_product"][p] = {
            "tonnes": round(tonnes, 3),
            "rate": _ratio(amount, tonnes),
            "amount": _round(amount, 2),
        }
    out["overheads_lines"] = _merge_overhead_lines(cost_days)
    out["producing_days"] = sum(1 for d in days if d["cloth_t"] > 0)
    out["calendar_days"] = len(days)
    out["unpriced_days"] = len(days) - len(priced_days)
    out["contribution_per_producing_day"] = _ratio(
        money["contribution"], sum(1 for d in priced_days if d["cloth_t"] > 0)
    )
    out["flags"] = {
        "power_excluded": any(d["flags"]["power_excluded"] for d in days),
        "realisation_partial": any(d["flags"]["realisation_partial"] for d in days),
        "contribution_partial": out["unpriced_days"] > 0,
        "dg_at_grid_rate": any(d["flags"]["dg_at_grid_rate"] for d in days),
        "solar_at_zero": any(d["flags"]["solar_at_zero"] for d in days),
        "overheads_estimated": any(d["flags"]["overheads_estimated"] for d in days),
        "overheads_priced_days_only": priced_only,
    }
    out["notes"] = [_note_priced_days_only(len(priced_days), len(days))] if priced_only else []
    return out


def _median(vals: list) -> Optional[float]:
    present = [float(v) for v in vals if v is not None]
    return round(statistics.median(present), 2) if present else None


def _std_row(rows: List[dict], w_from: date, w_to: date) -> dict:
    """Median over the producing days (cloth_t > 0, contribution not None) of
    the 30-day window — None when there is no such day."""
    return {
        "window_from": _iso(w_from),
        "window_to": _iso(w_to),
        "days_used": len(rows),
        "contribution_per_t": _median([r["per_t_cloth"]["contribution"] for r in rows]),
        "contribution_per_day": _median([r["money"]["contribution"] for r in rows]),
        "contribution_per_100_spindle_hours": _median(
            [r["per_100_spindles"]["contribution_per_100_spindle_hours"] for r in rows]
        ),
        "realisation_per_t": _median([r["per_t_cloth"]["realisation"] for r in rows]),
        "jute_cost_per_t": _median([r["per_t_cloth"]["jute_cost"] for r in rows]),
        "wages_per_t": _median([r["per_t_cloth"]["wages"] for r in rows]),
        "operating_profit_per_t": _median([r["per_t_cloth"]["operating_profit"] for r in rows]),
    }


def _nk_rank(key: str) -> tuple:
    base = key.split(":")[0]
    idx = NOT_KEYED_ORDER.index(base) if base in NOT_KEYED_ORDER else len(NOT_KEYED_ORDER)
    return (idx, key)


def _param_used(pick: dict) -> dict:
    return {
        "param_code": pick["param_code"],
        "param_label": PARAM_LABELS.get(pick["param_code"], pick["param_code"]),
        "product": pick["product"],
        "value": pick["value"],
        "uom": pick["uom"],
        "effective_date": _iso(pick["effective_date"]) if pick["effective_date"] else None,
        "branch_id": pick["branch_id"],
        "scope": "branch" if pick["branch_id"] is not None else "all",
        "source": pick["source"],
        "cost_param_id": pick["cost_param_id"],
        "days_used": 0,
        # Where the pick was consulted: "days" (priced at least one day),
        # "statement" (benchmarks / raw-material std, resolved on the To
        # date) and / or "capital" (the blocked-capital base).
        "used_in": [],
    }


def _merge_used(used: Dict[int, dict], pick: dict, where: str, days_used: int) -> None:
    """Record ``pick`` in params_used under ``where``; a pick first seen
    here (not consulted by any day) gets ``days_used``."""
    entry = used.get(pick["cost_param_id"])
    if entry is None:
        entry = used[pick["cost_param_id"]] = _param_used(pick)
        entry["days_used"] = days_used
    if where not in entry["used_in"]:
        entry["used_in"].append(where)


# ---------------------------------------------------------------------------
# Statement (the owner's order: realisation, six heads, three results)
# ---------------------------------------------------------------------------

RAW_MATERIAL_BENCH_LABEL_UNKEYED = (
    "std 1.06 kg jute per kg cloth × keyed jute rate — key JUTE_RATE_PER_KG"
)
# Jute rate keyed, JUTE_PER_KG_CLOTH not: the benchmark uses the 1.06 default.
RAW_MATERIAL_BENCH_LABEL_STD = f"std {JUTE_PER_KG_CLOTH_STD:g} kg/kg × keyed jute rate"
CAPITAL_MEMO = (
    "generally not costed separately — jute bought on 60–90 days credit carries it in the "
    "jute price (owner, 2026-10-20)"
)


# Largest rounding residual a reconciliation may absorb, PER DAY of the
# range (each day's sub-lines can each round 2 dp away from the day total):
# anything bigger is a logic error, not rounding, and must stay visible.
RECONCILE_MAX_PER_DAY = 0.05


def _reconcile_lines(
    lines: List[dict], total: Optional[float], cloth_t: float, n_days: int = 1
) -> List[dict]:
    """Make the sub-lines add up to the head EXACTLY: the per-day 2-dp rounding
    of each sub-line and of the head's total can differ by a paisa or two per
    day, so the residual goes on the line with the largest amount (standard
    statement presentation) and that line's per_t is restated. Lines whose
    period is None are left alone; a residual beyond RECONCILE_MAX_PER_DAY x
    n_days is not absorbed."""
    if total is None:
        return lines
    priced = [l for l in lines if l.get("period") is not None]
    if not priced:
        return lines
    residual = round(total - sum(l["period"] for l in priced), 2)
    if residual and abs(residual) <= RECONCILE_MAX_PER_DAY * max(1, n_days):
        target = max(priced, key=lambda l: abs(l["period"]))
        target["period"] = round(target["period"] + residual, 2)
        target["per_t"] = _ratio(target["period"], cloth_t)
    return lines


def _head(no: int, key: str, label: str, **over) -> dict:
    head = {
        "no": no,
        "key": key,
        "label": label,
        "period": None,
        "per_t": None,
        "benchmark_per_t": None,
        "benchmark_label": None,
        "estimated": False,
        "available": True,
        "not_keyed": None,
        "lines": [],
        "kpis": {},
        "has_breakdown": False,
        "has_check": False,
    }
    head.update(over)
    return head


def build_statement(
    days: List[dict],
    resolved: dict,
    capital: Optional[dict],
    not_keyed: List[dict],
    used: Optional[Dict[int, dict]] = None,
) -> dict:
    """The P&L statement over the PRICED days of the range (contribution not
    None), so realisation − heads 1–4 = contribution and − head 5 = operating
    profit hold to the rupee. Every per_t is over the cloth tonnes of those
    days (the _priced_ratio rule applied with one denominator). ``resolved``
    is the parameter set in force on date_to — it supplies the benchmarks and
    the raw-material standard; ``capital`` is build_capital's dict (None when
    not computed); ``not_keyed`` is the report's merged list, from which a
    head that is not available takes its reason.

    The three results are DERIVED from the head totals, rounded once —
    contribution = realisation − heads 1–4, operating profit = contribution −
    head 5, net = operating profit − head 6 — so the statement adds up to the
    paisa (re-summing the per-day rounded results drifted by a few paise
    over a range). ``used``, when given, collects every parameter pick the
    statement consulted ({cost_param_id: pick}: the BENCH_* rows,
    JUTE_PER_KG_CLOTH, JUTE_RATE_PER_KG) for params_used.
    """
    priced = [d for d in days if d["money"]["contribution"] is not None]

    def _consulted(pick: Optional[dict]) -> Optional[dict]:
        if pick is not None and used is not None:
            used.setdefault(pick["cost_param_id"], pick)
        return pick

    cloth_t = round(sum(d["cloth_t"] for d in priced), 3)
    all_cloth_t = round(sum(d["cloth_t"] for d in days), 3)
    reasons = {x["key"]: x["reason"] for x in not_keyed}

    def pt(amount) -> Optional[float]:
        return _ratio(amount, cloth_t)

    def msum(key: str) -> Optional[float]:
        return _round(_sum_or_none([d["money"][key] for d in priced]), 2)

    # --- Realisation -------------------------------------------------------
    real_lines = []
    for p in PRODUCTS:
        entries = [d["realisation_by_product"][p] for d in priced]
        amount = _sum_or_none([e["amount"] for e in entries])
        tonnes = round(sum(e["tonnes"] for e in entries if e["amount"] is not None), 3)
        real_lines.append(
            {
                "key": p,
                "label": PRODUCT_LABELS[p],
                "tonnes": tonnes,
                "rate": _ratio(amount, tonnes),
                "period": _round(amount, 2),
                "per_t": pt(amount),
            }
        )
    realisation_total = msum("realisation")
    realisation = {
        "period": realisation_total,
        "per_t": pt(realisation_total),
        "lines": _reconcile_lines(real_lines, realisation_total, cloth_t, len(priced)),
    }

    # --- Head 1: raw material ----------------------------------------------
    jute = msum("jute_cost")
    waste = msum("jute_waste_cost")
    dust = msum("dust_credit")
    booked_value = round(sum(d["jute_issue_value"] for d in priced), 2)
    booked_t = round(sum(d["jute_issued_t"] - d["jute_unvalued_t"] for d in priced), 3)
    unvalued_t = round(sum(d["jute_unvalued_t"] for d in priced), 3)
    priced_value = None if jute is None else round(jute - booked_value, 2)
    rm_period = None if jute is None else round(jute + (waste or 0.0) - (dust or 0.0), 2)
    jpk = _consulted(param_value(resolved, "JUTE_PER_KG_CLOTH"))
    jute_rate = _consulted(param_value(resolved, "JUTE_RATE_PER_KG"))
    jpk_value = float(jpk["value"]) if jpk is not None else JUTE_PER_KG_CLOTH_STD
    if jute_rate is not None:
        # jpk_value already defaults to the 1.06 std when JUTE_PER_KG_CLOTH is not keyed.
        rm_bench: Optional[float] = round(jpk_value * float(jute_rate["value"]) * 1000.0, 2)
        rm_bench_label = "keyed" if jpk is not None else RAW_MATERIAL_BENCH_LABEL_STD
    else:
        rm_bench = None
        rm_bench_label = RAW_MATERIAL_BENCH_LABEL_UNKEYED
    raw_material = _head(
        1,
        "raw_material",
        "Raw material",
        period=rm_period,
        per_t=pt(rm_period),
        benchmark_per_t=rm_bench,
        benchmark_label=rm_bench_label,
        available=rm_period is not None,
        not_keyed=None if rm_period is not None else reasons.get("JUTE_RATE_PER_KG"),
        lines=[
            {
                "key": "jute_booked",
                "label": "Raw jute at booked issue value",
                "qty_t": booked_t,
                "period": None if jute is None else booked_value,
                "per_t": None if jute is None else pt(booked_value),
            },
            {
                "key": "jute_priced",
                "label": "Raw jute with no booked value, priced at MR / keyed rate",
                "qty_t": unvalued_t,
                "period": priced_value,
                "per_t": pt(priced_value),
            },
            {
                "key": "jute_waste",
                "label": "Jute waste issued (booked value)",
                "qty_t": round(sum(d["jute_waste_t"] for d in priced), 3),
                "period": waste,
                "per_t": pt(waste),
            },
            {
                "key": "dust_credit",
                "label": "Less: dust sold",
                "qty_t": round(sum(d["dust_kg"] for d in priced) / 1000.0, 3),
                "period": None if dust is None else round(-dust, 2),
                "per_t": None if dust is None else _ratio(-dust, cloth_t),
            },
        ],
        kpis={
            "jute_kg_per_kg_cloth": _ratio(sum(d["jute_issued_t"] for d in days), all_cloth_t, 3),
            "jute_kg_per_kg_cloth_std": jpk_value,
            "jute_issued_t": round(sum(d["jute_issued_t"] for d in days), 3),
            "jute_unvalued_t": round(sum(d["jute_unvalued_t"] for d in days), 3),
            "zero_value_lines": int(sum(d["zero_value_lines"] for d in days)),
        },
        has_breakdown=True,
        has_check=True,
    )

    # --- Head 2: stores ----------------------------------------------------
    stores_total = msum("stores")
    bench_stores, label_stores, pick = _bench(resolved, "BENCH_STORES_PER_T", "stores")
    _consulted(pick)
    stores_lines = []
    for c in STORES_CATEGORIES:
        amount = round(sum(d["stores_by_category"][c] for d in priced), 2)
        stores_lines.append(
            {"key": c, "label": STORES_CATEGORY_LABELS[c], "period": amount, "per_t": pt(amount)}
        )
    stores = _head(
        2,
        "stores",
        "Stores (parts, consumables, packing, general)",
        period=stores_total,
        per_t=pt(stores_total),
        benchmark_per_t=bench_stores,
        benchmark_label=label_stores,
        available=stores_total is not None,
        lines=_reconcile_lines(stores_lines, stores_total, cloth_t, len(priced)),
        has_breakdown=True,
    )

    # --- Head 3: power -----------------------------------------------------
    power_total = msum("power")
    bench_power, label_power, pick = _bench(resolved, "BENCH_POWER_PER_T", "power")
    _consulted(pick)
    power_lines = []
    for t in POWER_TYPES:
        kwh = _round(_sum_nullable([d["power_by_type"][t]["kwh"] for d in priced]), 2)
        cost = _round(_sum_or_none([d["power_by_type"][t]["cost"] for d in priced]), 2)
        power_lines.append(
            {
                "key": t,
                "label": POWER_TYPE_LABELS[t],
                "kwh": kwh,
                "rate": _ratio(cost, kwh, 4),
                "period": cost,
                "per_t": pt(cost),
            }
        )
    total_kwh = _round(_sum_nullable([d["power_kwh"] for d in priced]), 2)
    power = _head(
        3,
        "power",
        "Electricity (grid, DG, solar)",
        period=power_total,
        per_t=pt(power_total),
        benchmark_per_t=bench_power,
        benchmark_label=label_power,
        available=power_total is not None,
        not_keyed=None if power_total is not None else reasons.get("power"),
        lines=_reconcile_lines(power_lines, power_total, cloth_t, len(priced)),
        kpis={
            "kwh_per_t": _ratio(total_kwh, cloth_t),
            "dg_hours": round(sum(d["dg_hours"] for d in priced), 2),
            "dg_litres": round(sum(d["dg_litres"] for d in priced), 2),
        },
        has_breakdown=True,
    )

    # --- Head 4: wages -----------------------------------------------------
    wages_total = msum("wages")
    hands = round(sum(d["hands"] for d in priced), 2)
    hours = round(sum(d["hours"] for d in priced), 2)
    wages = _head(
        4,
        "wages",
        "Wages (worker hand-days)",
        period=wages_total,
        per_t=pt(wages_total),
        available=wages_total is not None,
        not_keyed=None if wages_total is not None else reasons.get("WAGE_PER_HAND_DAY"),
        lines=[
            {
                "key": "hands",
                "label": "Hand-days × wage per hand-day",
                "qty": hands,
                "rate": _ratio(wages_total, hands),
                "period": wages_total,
                "per_t": pt(wages_total),
            }
        ],
        kpis={"hands": hands, "hands_per_t": _ratio(hands, cloth_t), "hours": hours},
        has_breakdown=True,
    )

    # --- Head 5: overheads -------------------------------------------------
    overheads_total = msum("overheads")
    bench_oh, label_oh, pick = _bench(resolved, "BENCH_OVERHEAD_PER_T", "overheads")
    _consulted(pick)
    overhead_lines = []
    for line in _merge_overhead_lines(priced):
        overhead_lines.append(
            {
                "code": line["code"],
                "label": line["label"],
                "value_per_day": line["value_per_day"],
                "days": line["days"],
                "period": line["amount"],
                "per_t": pt(line["amount"]),
            }
        )
    overheads = _head(
        5,
        "overheads",
        "Overheads (staff, admin, other fixed)",
        period=overheads_total,
        per_t=pt(overheads_total),
        benchmark_per_t=bench_oh,
        benchmark_label=label_oh,
        estimated=any(d["flags"]["overheads_estimated"] for d in priced),
        available=overheads_total is not None,
        lines=_reconcile_lines(overhead_lines, overheads_total, cloth_t, len(priced)),
    )

    # --- Head 6: capital ---------------------------------------------------
    capital_total = msum("capital_cost")
    cap = capital or {}
    components = [
        ("jute_stock", "Jute stock at MR rate", cap.get("jute_stock_value")),
        ("finished_goods", "Finished goods (bales) at realisation", cap.get("fg_value")),
        ("other", "Other blocked capital (keyed)", cap.get("other") if capital else None),
    ]
    base_total = sum(v for _, _, v in components if v is not None)
    capital_lines = []
    for key, label, base in components:
        share = (
            round(capital_total * base / base_total, 2)
            if (capital_total is not None and base is not None and base_total)
            else None
        )
        capital_lines.append(
            {"key": key, "label": label, "base_value": base, "period": share, "per_t": pt(share)}
        )
    capital_reason = None
    if capital_total is None:
        capital_reason = reasons.get("COST_OF_CAPITAL_PCT") or reasons.get("capital")
    capital_head = _head(
        6,
        "capital",
        "Capital cost (interest on blocked capital)",
        period=capital_total,
        per_t=pt(capital_total),
        available=capital_total is not None,
        not_keyed=capital_reason,
        lines=_reconcile_lines(capital_lines, capital_total, cloth_t, len(priced)),
        kpis={"pct": cap.get("pct") if capital else None, "days": len(priced)},
        memo=CAPITAL_MEMO,
    )

    # --- Results (derived from the heads, rounded once) ---------------------
    contribution_total: Optional[float] = None
    if realisation_total is not None and rm_period is not None and wages_total is not None:
        contribution_total = round(
            realisation_total - rm_period - (stores_total or 0.0) - (power_total or 0.0) - wages_total, 2
        )
    op_total = (
        None if contribution_total is None else round(contribution_total - (overheads_total or 0.0), 2)
    )
    net_total = (
        None if (op_total is None or capital_total is None) else round(op_total - capital_total, 2)
    )
    conv_period = (
        None
        if wages_total is None
        else round((stores_total or 0.0) + (power_total or 0.0) + wages_total + (overheads_total or 0.0), 2)
    )
    keyed_benches = sum(1 for lbl in (label_stores, label_power, label_oh) if lbl == "keyed")
    conv_label = "keyed" if keyed_benches == 3 else (BENCHMARK_SOURCE if keyed_benches == 0 else "keyed + rule of thumb")

    return {
        "priced_days": len(priced),
        "calendar_days": len(days),
        "cloth_t": cloth_t,
        "realisation": realisation,
        "heads": [raw_material, stores, power, wages, overheads, capital_head],
        "contribution": {"period": contribution_total, "per_t": pt(contribution_total)},
        "operating_profit": {"period": op_total, "per_t": pt(op_total)},
        "net": {"period": net_total, "per_t": pt(net_total)},
        "conversion_cost": {
            "period": conv_period,
            "per_t": pt(conv_period),
            "benchmark_per_t": round(bench_stores + bench_power + bench_oh, 2),
            "benchmark_label": conv_label,
        },
    }


def build_daily_pl(
    day_inputs: Dict[date, dict],
    params_rows: List[dict],
    d_from: date,
    d_to: date,
    median_from: date,
    capital: Optional[dict] = None,
) -> dict:
    """days / mtd / std / statement / capital / params_used / not_keyed from
    the day inputs.

    Parameters are resolved per day (a mid-range change prices each side with
    its own row). Days outside [d_from, d_to] (the median window before
    date_from) feed only the std median. ``capital`` is build_capital's dict
    (None = not computed: capital cost blank, listed in not_keyed).
    """
    std_from = d_to - timedelta(days=STD_WINDOW_DAYS - 1)
    days: List[dict] = []
    std_rows: List[dict] = []
    not_keyed: Dict[str, str] = {}
    used: Dict[int, dict] = {}
    for d in sorted(day_inputs):
        if d < median_from or d > d_to:
            continue
        inp = day_inputs[d]
        m = money_for_day(inp, resolve_params(params_rows, d), capital)
        row = _day_row(inp, m)
        if d_from <= d <= d_to:
            days.append(row)
            for item in m["not_keyed"]:
                not_keyed.setdefault(item["key"], item["reason"])
            for pick in m["used"]:
                entry = used.get(pick["cost_param_id"])
                if entry is None:
                    entry = used[pick["cost_param_id"]] = _param_used(pick)
                    entry["used_in"].append("days")
                entry["days_used"] += 1
        if std_from <= d <= d_to and inp["cloth_t"] > 0 and row["money"]["contribution"] is not None:
            std_rows.append(row)
    not_keyed_list = [{"key": k, "reason": not_keyed[k]} for k in sorted(not_keyed, key=_nk_rank)]
    # The statement and the capital base read the parameters in force on
    # date_to; what they consulted joins params_used (a pick no day used
    # counts the statement's priced days, or 0 for the capital base).
    resolved_to = resolve_params(params_rows, d_to)
    statement_used: Dict[int, dict] = {}
    statement = build_statement(days, resolved_to, capital, not_keyed_list, statement_used)
    for pick in statement_used.values():
        _merge_used(used, pick, "statement", statement["priced_days"])
    for pick in capital_param_picks(capital, resolved_to):
        _merge_used(used, pick, "capital", 0)
    return {
        "days": days,
        "mtd": _period_row(days, d_from),
        "std": _std_row(std_rows, std_from, d_to),
        "statement": statement,
        "capital": capital if capital is not None else _capital_unavailable(d_to),
        "params_used": sorted(
            used.values(),
            key=lambda p: (p["param_code"], p["product"] or "", p["effective_date"] or ""),
        ),
        "not_keyed": not_keyed_list,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/daily_pl")
def daily_pl(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    # Every parameter is validated BEFORE the session is touched.
    co_id, branch_id, d_from, d_to = parse_range_params(request)
    # The std median always sees 30 days ending on date_to.
    median_from = min(d_from, d_to - timedelta(days=STD_WINDOW_DAYS - 1))

    try:
        if branch_id is not None:
            _verify_pair(db, co_id, branch_id)
        params_rows = _rows(
            db,
            get_cost_param_rows_for_resolution_query(),
            {"co_id": co_id, "branch_id": branch_id, "on_date": d_to},
        )
        range_binds = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": median_from,
            "date_to": d_to,
        }
        cloth = _rows(db, get_cloth_by_quality_daily_query(), range_binds)
        yarn = _rows(
            db,
            get_dpr_spinning_daily_production_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "window_from": median_from,
                "report_date": d_to,
                "spinning_type": SPINNING_MACHINE_TYPE_NAME,
            },
        )
        hands = _rows(db, get_hands_daily_query(), range_binds)
        spindle = _rows(db, get_spindle_hours_daily_query(), range_binds)
        jute = _rows(db, get_jute_issue_daily_query(), range_binds)
        stores = _rows(db, get_stores_value_daily_by_category_query(), range_binds)
        dust = _rows(db, get_dust_daily_query(), range_binds)
        purchased = _fetch_optional(db, get_purchased_yarn_daily_query(), range_binds)
        power = _fetch_optional(db, get_power_daily_by_type_query(), range_binds)
        waste = _rows(db, get_jute_waste_issue_daily_query(), range_binds)
        # Capital components, once for the range: jute stock as of NOW (the view
        # is not as-of-date), finished goods as on date_to (the bale register
        # summary IS an as-of-date derivation).
        jute_stock = _fetch_optional(
            db, get_jute_stock_value_query(), {"co_id": co_id, "branch_id": branch_id}
        )
        fg = None
        if branch_id is not None:
            fg = _fetch_optional(
                db,
                get_bale_stock_summary_query(),
                {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "as_on": d_to,
                    "cancelled_status": list(CANCELLED_INVOICE_STATUS_IDS),
                },
            )
        capital = build_capital(jute_stock, fg, resolve_params(params_rows, d_to), d_to, branch_id)

        inputs = build_day_inputs(
            median_from, d_to, cloth, yarn, hands, spindle, jute, stores, dust, purchased, power, waste
        )
        data = build_daily_pl(inputs, params_rows, d_from, d_to, median_from, capital)

        notes = list(NOTES_ALWAYS)
        if (data["mtd"].get("dust_kg") or 0) == 0:
            notes.append(NOTE_NO_DUST)
        if purchased is None:
            notes.append(NOTE_NO_PURCHASED)
        notes.extend(capital["notes"])
        data["meta"] = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": _iso(d_from),
            "date_to": _iso(d_to),
            "std_window_from": _iso(d_to - timedelta(days=STD_WINDOW_DAYS - 1)),
            "std_window_days": STD_WINDOW_DAYS,
            "notes": notes,
        }
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
