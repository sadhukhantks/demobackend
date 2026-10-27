"""Purchased Yarn Ledger (jute production reports).

``GET /api/juteProductionReports/purchased_yarn_ledger?co_id&branch_id&month=YYYY-MM``

Phase 5 of docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-
design.md §3.5, under owner rulings 3 and 4 (§5): consumption of purchased
yarn is never keyed — it is DERIVED from the department floor counts — and
the godown is counted daily with the winding stock count, so any mismatch
between the godown book and the godown count is reported, not absorbed.

Per yarn item for one calendar month, all in KILOGRAMS at 3 dp:

    godown opening        last PURCHASE/GODOWN count before the month
                          (meta.opening_basis 'count'), else the document book
                          (approved purchases - issues + returns before it).
                          Under the count basis an item with a book balance
                          that is missing from that count keeps its book
                          opening and is flagged ITEM_IN_BOOK_NOT_COUNTED
    purchases             approved Yarn Purchase in the month
    issues, returns       Yarn Issue / Return in the month (not rejected,
                          cancelled or draft)
    godown book closing   opening + purchases - issues + returns
    godown count closing  PURCHASE/GODOWN count on the last count date IN the
                          month (null when the month holds no godown count —
                          a count from before the month is never a closing)
    godown difference     count - book (null when no count)
    floor opening/closing PURCHASE/FLOOR counts per department, each on that
                          department's own last count date before / within
                          the month (0 when never counted; a department not
                          counted in the month closes at 0 and is flagged
                          DEPT_NO_FLOOR_COUNT when it drew issues)
    consumption           floor opening + issues - returns - floor closing,
                          per department; the item figure sums them
    days of stock         floor closing / (consumption / days_in_window),
                          where the window runs from the first of the month
                          to the latest closing count date in it (godown or
                          floor), or to today for a month still in progress
                          with no count yet — never the full calendar month
                          of a month that has not finished (meta.days_in_window)

Yarn Issue and Yarn Return are moves between the godown and a floor, so they
appear on BOTH sides of the ledger and cancel in the site total — that is the
check: what left the godown must be on a floor or consumed.

All arithmetic is in build_ledger (plain dicts in, plain dicts out, no I/O)
so the worked example is testable without a database; the endpoint only
fetches and hands over. Mirrors winding_reports.py for conventions
(get_tenant_db + get_current_user_with_refresh, {"data": ...}, the
HTTPException / ValueError 400 / Exception 500 ladder).
"""

import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.constants import (
    YARN_ISSUE_TYPE_ISSUE,
    YARN_ISSUE_TYPE_RETURN,
)
from src.juteProduction.constants import (
    YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT,
    YARN_LEDGER_EXC_FLOOR_DAYS_OVER,
    YARN_LEDGER_EXC_GODOWN_BOOK_NEGATIVE,
    YARN_LEDGER_EXC_GODOWN_DIFF,
    YARN_LEDGER_EXC_ITEM_IN_BOOK_NOT_COUNTED,
    YARN_LEDGER_EXC_ITEM_WITHOUT_YARN_MASTER,
    YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS,
    YARN_LEDGER_FLOOR_DAYS_MAX,
    YARN_LEDGER_GODOWN_TOL_KG,
    YARN_LEDGER_GODOWN_TOL_PCT,
    YARN_LEDGER_OPENING_BOOK,
    YARN_LEDGER_OPENING_COUNT,
)
from src.juteProduction.winding_reports import (
    _f,
    legacy_stores_yarn_warning,
    purchase_qty_to_mt,
)
from src.juteProduction.winding_stock_query import get_yarn_purchase_qty_query
from src.juteProduction.yarn_ledger_query import (
    get_yarn_ledger_book_opening_query,
    get_yarn_ledger_floor_closing_count_query,
    get_yarn_ledger_floor_count_query,
    get_yarn_ledger_godown_closing_count_query,
    get_yarn_ledger_godown_count_query,
    get_yarn_ledger_movement_rows_query,
    get_yarn_ledger_purchase_rows_query,
)

router = APIRouter()

# Row-level numeric keys; ``totals`` carries exactly these. The first ten are
# summed across rows; days_of_stock is recomputed from the summed floor
# closing and consumption (summing days would be meaningless).
LEDGER_NUMERIC_KEYS = (
    "godown_opening_kg",
    "purchases_kg",
    "issues_kg",
    "returns_kg",
    "godown_book_closing_kg",
    "godown_count_closing_kg",
    "godown_diff_kg",
    "floor_opening_kg",
    "floor_closing_kg",
    "consumption_kg",
    "days_of_stock",
)
_SUMMED_KEYS = LEDGER_NUMERIC_KEYS[:-1]
_NULLABLE_KEYS = ("godown_count_closing_kg", "godown_diff_kg", "days_of_stock")

NO_DEPT_LABEL = "(no department)"

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def month_window(month: str):
    """'YYYY-MM' -> (first day, last day) of that calendar month.

    ValueError for anything else — a two-digit month is REQUIRED so the
    parameter can never be mistaken for a date or a bare year.
    """
    m = _MONTH_RE.match(month or "")
    if not m:
        raise ValueError("month must be YYYY-MM")
    first = date(int(m.group(1)), int(m.group(2)), 1)  # ValueError on month 13
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first, last


def _kg(v) -> float:
    """3 dp, and never -0.0 (which JSON would print as a signed zero)."""
    return round(_f(v), 3) + 0.0


def _days_of_stock(floor_closing_kg, consumption_kg, days: int):
    """Floor stock in days of average consumption, 1 dp; None when nothing
    was consumed (a negative consumption is a count error, not a rate)."""
    if _f(consumption_kg) <= 0 or days <= 0:
        return None
    return round(_f(floor_closing_kg) / (_f(consumption_kg) / days), 1) + 0.0


def days_in_window(from_date, to_date, last_count_date, today) -> int:
    """Days the consumption rate is averaged over: from_date up to the latest
    closing count date in the period, else up to today (a month still in
    progress with nothing counted yet), never past to_date and never below 1.

    Dividing an in-progress month's consumption by the full calendar month
    would understate the daily rate and overstate days of stock.
    """
    cutoff = last_count_date if last_count_date is not None else today
    end = min(to_date, cutoff)
    return max(1, (end - from_date).days + 1)


def _in_period(rows, from_date):
    """Closing-count rows dated before from_date are not closings of this
    period (the SQL already bounds them; this keeps build_ledger honest on
    its own). Rows without a date are kept."""
    return [r for r in rows if r.get("tran_date") is None or r.get("tran_date") >= from_date]


def _label_sort_key(slot):
    label = slot["item_name"] or slot["item_code"] or ""
    return (label.upper(), slot["item_id"] or 0)


def build_ledger(
    *,
    from_date,
    to_date,
    purchase_rows,
    book_rows,
    movement_rows,
    godown_opening_rows,
    godown_closing_rows,
    floor_opening_rows,
    floor_closing_rows,
    legacy_stores_kg=0.0,
    legacy_unknown_uoms=(),
    today=None,
) -> dict:
    """The whole ledger from plain row dicts. See the module docstring for
    the definitions; see test_yarn_ledger.py for the worked example.

    Row shapes (all carry item_id, item_code, item_name, yarn_count):
      purchase_rows        kg, has_yarn_master          (window, per item)
      book_rows            purchases_kg, issues_kg, returns_kg (before from_date)
      movement_rows        dept_id, dept_desc, issue_type, kg (window)
      godown_*_rows        tran_date, kg                (the last count date)
      floor_*_rows         dept_id, dept_desc, tran_date, kg (per dept's date)

    legacy_stores_kg / legacy_unknown_uoms are the converted legacy Stores
    figure and the unit names purchase_qty_to_mt refused to convert (the
    transition guard). today bounds days_in_window for a month in progress
    with no closing count yet; the endpoint passes date.today().

    Returns {"from_date", "to_date", "meta", "rows", "totals", "exceptions"};
    the endpoint adds "month".
    """
    if today is None:
        today = date.today()
    godown_closing_rows = _in_period(godown_closing_rows, from_date)
    floor_closing_rows = _in_period(floor_closing_rows, from_date)
    items: dict = {}
    dept_labels: dict = {}

    def slot(r):
        key = r.get("item_id")
        e = items.get(key)
        if e is None:
            e = items[key] = {
                "item_id": key,
                "item_code": r.get("item_code"),
                "item_name": r.get("item_name"),
                "yarn_count": None if r.get("yarn_count") is None else _f(r.get("yarn_count")),
                "godown_opening": 0.0,
                "purchases": 0.0,
                "issues": 0.0,
                "returns": 0.0,
                "godown_count": 0.0,
                "has_yarn_master": True,
                "book_not_counted": None,
                "depts": {},
            }
        else:
            for k in ("item_code", "item_name"):
                if e[k] is None and r.get(k) is not None:
                    e[k] = r.get(k)
            if e["yarn_count"] is None and r.get("yarn_count") is not None:
                e["yarn_count"] = _f(r.get("yarn_count"))
        return e

    def dept_slot(e, r):
        d_id = r.get("dept_id")
        if r.get("dept_desc") is not None:
            dept_labels[d_id] = r.get("dept_desc")
        d = e["depts"].get(d_id)
        if d is None:
            d = e["depts"][d_id] = {
                "floor_opening": 0.0, "issues": 0.0, "returns": 0.0, "floor_closing": 0.0,
            }
        return d

    # --- godown opening: count if one precedes the month, else the book ---
    godown_opening_date = None
    if godown_opening_rows:
        godown_opening_date = godown_opening_rows[0].get("tran_date")
        opening_basis = YARN_LEDGER_OPENING_COUNT
        for r in godown_opening_rows:
            slot(r)["godown_opening"] += _f(r.get("kg"))
        # An item the book carries that the count missed would otherwise
        # vanish (or open at 0): keep its book opening and say so.
        counted = set(items)
        for r in book_rows:
            if r.get("item_id") in counted:
                continue
            net = _f(r.get("purchases_kg")) - _f(r.get("issues_kg")) + _f(r.get("returns_kg"))
            if round(net, 3) != 0:
                e = slot(r)
                e["godown_opening"] += net
                e["book_not_counted"] = _f(e["book_not_counted"]) + net
    else:
        opening_basis = YARN_LEDGER_OPENING_BOOK
        for r in book_rows:
            net = _f(r.get("purchases_kg")) - _f(r.get("issues_kg")) + _f(r.get("returns_kg"))
            if round(net, 3) != 0:
                slot(r)["godown_opening"] += net

    # --- documents in the month ------------------------------------------
    for r in purchase_rows:
        e = slot(r)
        e["purchases"] += _f(r.get("kg"))
        if r.get("has_yarn_master") is not None and not int(_f(r.get("has_yarn_master"))):
            e["has_yarn_master"] = False

    for r in movement_rows:
        kind = (r.get("issue_type") or "").upper()
        if kind not in (YARN_ISSUE_TYPE_ISSUE, YARN_ISSUE_TYPE_RETURN):
            continue
        e = slot(r)
        d = dept_slot(e, r)
        field = "issues" if kind == YARN_ISSUE_TYPE_ISSUE else "returns"
        e[field] += _f(r.get("kg"))
        d[field] += _f(r.get("kg"))

    # --- godown closing count ---------------------------------------------
    godown_closing_date = None
    if godown_closing_rows:
        godown_closing_date = godown_closing_rows[0].get("tran_date")
        for r in godown_closing_rows:
            slot(r)["godown_count"] += _f(r.get("kg"))

    # --- floor counts per department --------------------------------------
    for r in floor_opening_rows:
        dept_slot(slot(r), r)["floor_opening"] += _f(r.get("kg"))
    floor_count_dates: dict = {}
    for r in floor_closing_rows:
        dept_slot(slot(r), r)["floor_closing"] += _f(r.get("kg"))
        td = r.get("tran_date")
        if td is not None:
            floor_count_dates[r.get("dept_id")] = td

    # --- rows ---------------------------------------------------------------
    count_dates = [d for d in (godown_closing_date, *floor_count_dates.values()) if d is not None]
    days = days_in_window(from_date, to_date, max(count_dates) if count_dates else None, today)
    has_closing_count = godown_closing_date is not None
    rows = []
    exceptions = []
    dept_issues_without_count: dict = {}

    for e in sorted(items.values(), key=_label_sort_key):
        depts = []
        floor_opening = floor_closing = consumption = 0.0
        for d_id, d in sorted(
            e["depts"].items(),
            key=lambda kv: ((dept_labels.get(kv[0]) or "").upper(), kv[0] or 0),
        ):
            d_cons = d["floor_opening"] + d["issues"] - d["returns"] - d["floor_closing"]
            depts.append(
                {
                    "dept_id": d_id,
                    "dept_desc": dept_labels.get(d_id) or (NO_DEPT_LABEL if d_id is None else None),
                    "floor_opening_kg": _kg(d["floor_opening"]),
                    "issues_kg": _kg(d["issues"]),
                    "returns_kg": _kg(d["returns"]),
                    "floor_closing_kg": _kg(d["floor_closing"]),
                    "consumption_kg": _kg(d_cons),
                }
            )
            floor_opening += d["floor_opening"]
            floor_closing += d["floor_closing"]
            consumption += d_cons
            if d["issues"] > 0 and d_id not in floor_count_dates:
                dept_issues_without_count[d_id] = (
                    dept_issues_without_count.get(d_id, 0.0) + d["issues"]
                )

        book_closing = e["godown_opening"] + e["purchases"] - e["issues"] + e["returns"]
        count_closing = e["godown_count"] if has_closing_count else None
        diff = None if count_closing is None else count_closing - book_closing
        days_of_stock = _days_of_stock(floor_closing, consumption, days)

        row = {
            "item_id": e["item_id"],
            "item_name": e["item_name"],
            "item_code": e["item_code"],
            "yarn_count": e["yarn_count"],
            "godown_opening_kg": _kg(e["godown_opening"]),
            "purchases_kg": _kg(e["purchases"]),
            "issues_kg": _kg(e["issues"]),
            "returns_kg": _kg(e["returns"]),
            "godown_book_closing_kg": _kg(book_closing),
            "godown_count_closing_kg": None if count_closing is None else _kg(count_closing),
            "godown_diff_kg": None if diff is None else _kg(diff),
            "floor_opening_kg": _kg(floor_opening),
            "floor_closing_kg": _kg(floor_closing),
            "consumption_kg": _kg(consumption),
            "days_of_stock": days_of_stock,
            "depts": depts,
        }
        rows.append(row)

        label = e["item_name"] or e["item_code"] or f"item {e['item_id']}"
        if e["book_not_counted"] is not None:
            exceptions.append(
                {
                    "code": YARN_LEDGER_EXC_ITEM_IN_BOOK_NOT_COUNTED,
                    "message": (
                        f"{label}: {_kg(e['book_not_counted']):.3f} kg in the document book "
                        f"before {from_date} but not on the godown count of "
                        f"{godown_opening_date} — book balance used as the opening"
                    ),
                    "item_id": e["item_id"],
                    "value": _kg(e["book_not_counted"]),
                }
            )
        if not e["has_yarn_master"]:
            exceptions.append(
                {
                    "code": YARN_LEDGER_EXC_ITEM_WITHOUT_YARN_MASTER,
                    "message": f"{label}: on a Yarn Purchase but has no yarn master (jute_yarn_mst) row",
                    "item_id": e["item_id"],
                }
            )
        if row["godown_book_closing_kg"] < 0:
            exceptions.append(
                {
                    "code": YARN_LEDGER_EXC_GODOWN_BOOK_NEGATIVE,
                    "message": (
                        f"{label}: godown book closing is {row['godown_book_closing_kg']:.3f} kg "
                        "— more issued than purchased"
                    ),
                    "item_id": e["item_id"],
                    "value": row["godown_book_closing_kg"],
                }
            )
        if diff is not None:
            tol = max(
                YARN_LEDGER_GODOWN_TOL_KG,
                (e["purchases"] + e["godown_opening"]) * YARN_LEDGER_GODOWN_TOL_PCT / 100.0,
            )
            if abs(diff) > tol:
                exceptions.append(
                    {
                        "code": YARN_LEDGER_EXC_GODOWN_DIFF,
                        "message": (
                            f"{label}: godown count {row['godown_count_closing_kg']:.3f} kg differs "
                            f"from book {row['godown_book_closing_kg']:.3f} kg by "
                            f"{row['godown_diff_kg']:.3f} kg (tolerance {round(tol, 3):.3f} kg)"
                        ),
                        "item_id": e["item_id"],
                        "value": row["godown_diff_kg"],
                    }
                )
        if days_of_stock is not None and days_of_stock > YARN_LEDGER_FLOOR_DAYS_MAX:
            exceptions.append(
                {
                    "code": YARN_LEDGER_EXC_FLOOR_DAYS_OVER,
                    "message": (
                        f"{label}: {days_of_stock} days of stock on department floors "
                        f"(limit {YARN_LEDGER_FLOOR_DAYS_MAX:g})"
                    ),
                    "item_id": e["item_id"],
                    "value": days_of_stock,
                }
            )

    for d_id, issued in sorted(
        dept_issues_without_count.items(),
        key=lambda kv: ((dept_labels.get(kv[0]) or "").upper(), kv[0] or 0),
    ):
        d_label = dept_labels.get(d_id) or (NO_DEPT_LABEL if d_id is None else f"dept {d_id}")
        exceptions.append(
            {
                "code": YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT,
                "message": (
                    f"{d_label}: {_kg(issued):.3f} kg issued in the period but no "
                    f"purchased-yarn floor count in the period ({from_date} to {to_date})"
                ),
                "dept_id": d_id,
                "value": _kg(issued),
            }
        )

    # --- totals -------------------------------------------------------------
    totals = {k: _kg(sum(_f(r[k]) for r in rows)) for k in _SUMMED_KEYS}
    if not has_closing_count:
        totals["godown_count_closing_kg"] = None
        totals["godown_diff_kg"] = None
    totals["days_of_stock"] = _days_of_stock(
        totals["floor_closing_kg"], totals["consumption_kg"], days
    )

    # --- transition guard: Stores still carries the yarn, no document does --
    # (or carries it in a unit that cannot be converted: value None then,
    # because the tonnage is exactly what is unknown)
    legacy_mt = round(_f(legacy_stores_kg) / 1000.0, 3)
    msg = legacy_stores_yarn_warning(
        legacy_mt, totals["purchases_kg"] / 1000.0, to_date, legacy_unknown_uoms
    )
    if msg is not None:
        exceptions.append(
            {
                "code": YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS,
                "message": msg,
                "value": _kg(legacy_stores_kg) if legacy_mt > 0 else None,
            }
        )

    return {
        "from_date": str(from_date),
        "to_date": str(to_date),
        "meta": {
            "godown_opening_count_date": (
                None if godown_opening_date is None else str(godown_opening_date)
            ),
            "godown_closing_count_date": (
                None if godown_closing_date is None else str(godown_closing_date)
            ),
            "floor_count_dates": {k: str(v) for k, v in floor_count_dates.items()},
            "opening_basis": opening_basis,
            "days_in_window": days,
        },
        "rows": rows,
        "totals": totals,
        "exceptions": exceptions,
    }


# =============================================================================
# Endpoint
# =============================================================================


def _require_int(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _rows(db: Session, stmt, binds) -> list:
    return [dict(r._mapping) for r in db.execute(stmt, binds).fetchall()]


@router.get("/purchased_yarn_ledger")
def purchased_yarn_ledger(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Purchased yarn per item for one month: godown book vs count, floor
    counts per department, derived consumption, and the exceptions list.

    co_id AND branch_id are required and verified as a pair (the yarn tables
    are keyed by both; a branch of company A must not report under company
    B's header). month is 'YYYY-MM'.
    """
    co_id = _require_int(request, "co_id")
    branch_id = _require_int(request, "branch_id")
    month = request.query_params.get("month")
    if not month:
        raise HTTPException(status_code=400, detail="month is required (YYYY-MM)")
    try:
        from_date, to_date = month_window(month)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid month format, expected YYYY-MM")
    try:
        pair_ok = db.execute(
            text("SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"),
            {"branch_id": branch_id, "co_id": co_id},
        ).first()
        if not pair_ok:
            raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")

        scope = {"co_id": co_id, "branch_id": branch_id}
        window = {**scope, "date_from": from_date, "date_to": to_date}
        before_month = {**scope, "before": from_date}
        # closing counts: IN the month — "on or before to_date" == "strictly
        # before the next day", and never before from_date
        in_month = {**scope, "date_from": from_date, "before": to_date + timedelta(days=1)}

        purchase_rows = _rows(db, get_yarn_ledger_purchase_rows_query(), window)
        book_rows = _rows(db, get_yarn_ledger_book_opening_query(), before_month)
        movement_rows = _rows(db, get_yarn_ledger_movement_rows_query(), window)
        godown_opening_rows = _rows(db, get_yarn_ledger_godown_count_query(), before_month)
        godown_closing_rows = _rows(db, get_yarn_ledger_godown_closing_count_query(), in_month)
        floor_opening_rows = _rows(db, get_yarn_ledger_floor_count_query(), before_month)
        floor_closing_rows = _rows(db, get_yarn_ledger_floor_closing_count_query(), in_month)

        # Legacy Stores receipts of yarn-type items (the pre-phase-5 purchase
        # source), converted per UOM, never guessed — transition guard only.
        # Unit names the conversion refused travel with the figure: a receipt
        # booked in MTR converts to 0 MT and would otherwise pass the guard.
        legacy = db.execute(get_yarn_purchase_qty_query(), window).fetchone()
        legacy_mt, legacy_unknown = 0.0, []
        if legacy is not None and _f(legacy.purchase_qty) != 0:
            legacy_mt, legacy_unknown = purchase_qty_to_mt(legacy.purchase_qty, legacy.uoms)

        built = build_ledger(
            from_date=from_date,
            to_date=to_date,
            purchase_rows=purchase_rows,
            book_rows=book_rows,
            movement_rows=movement_rows,
            godown_opening_rows=godown_opening_rows,
            godown_closing_rows=godown_closing_rows,
            floor_opening_rows=floor_opening_rows,
            floor_closing_rows=floor_closing_rows,
            legacy_stores_kg=legacy_mt * 1000.0,
            legacy_unknown_uoms=legacy_unknown,
            today=date.today(),
        )
        return {"data": {"month": month, **built}}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
