"""Outstanding PO Position (/api/juteReports/po-outstanding-position).

The raw-jute purchase position as on a date, for the jute purchase desk and
the production planner alike: which
POs are still live, what has arrived against each quality, and what is still
to come — with the balance value so the planner can see both the tonnage in
the pipeline and the money committed to it.

* Scope: live POs (not draft / rejected / cancelled / closed) of the branch,
  dated on or before ``as_on``; receipts = stock-eligible MR lines matched by
  item that ARRIVED on or before ``as_on`` (gate-entry date first). PO status
  has no history, so a PO closed after ``as_on`` is not resurrected — the
  report is exact for today and approximate for back-dates.
* Weight stays in the unit the PO was entered in (kg on every current tenant,
  quintals on legacy sls rows) — nothing is divided by 100. An item's ordered
  weight is its share of the PO header weight (percentage, else quantity
  share); balance value is the unreceived fraction of the line's own stored
  value, so no rate unit is assumed either.
* Sections: ``detail`` (one row per PO item with a balance), ``by_quality``,
  ``by_supplier`` and ``totals``. Fully-received items are dropped; a PO with
  nothing outstanding therefore disappears from every section.
* Lorry-and-weight view (§11.2): each row carries the PO-level
  ``returned_lorries`` / ``returned_weight`` (MR status 48 with a gate entry by
  ``as_on``, never counted as received); every roll-up carries
  ``order_lorries``, ``received_lorries``, ``pending_lorries``,
  ``returned_lorries`` and the matching ``*_weight`` sums over its distinct POs.
* Settlement flags (po-settlement-spec.md §6): each detail row carries
  ``days_since_receipt``, ``stale`` (overdue and no receipt for
  ``rule.stale_days``) and ``settle_suggest`` (the receipts already satisfy the
  settlement rule but the PO is still live — the engine has not run since a
  backend change; "Re-evaluate" fixes it). The response carries the ``rule``
  (loaded through ``load_rule``, which tolerates a tenant without the
  migration) and ``totals.stale_pos`` / ``totals.settle_suggest_pos``. The
  SQL never references the new jute_po columns.

Shape follows the jute-production report routers (juteProduction/selection_reports.py): sync ``def``
handler, ``get_tenant_db`` + ``get_current_user_with_refresh``, ``{"data": ...}``
response, parameters validated before the try/except so a bad input is a 400
and never a 500, and a DB-free pure builder (``build_po_position``) that the
tests hit directly with stub rows.
"""

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.po_settlement import PoReceipts, SettleRule, is_settled, load_rule
from src.juteProcurement.reportQueries import get_jute_po_outstanding_position_query
from src.juteProduction.bale_reports_query import get_branch_name_query

router = APIRouter()
po_position_router = router

BAD_BRANCH_MSG = "branch_id must be an active branch of this company"

# =============================================================================
# Small helpers
# =============================================================================


def _f(v, default: Optional[float] = 0.0) -> Optional[float]:
    return default if v is None else float(v)


def _i(v, default: int = 0) -> int:
    return default if v is None else int(v)


def _r3(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 3)


def _r2(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _iso(v: Optional[date]) -> Optional[str]:
    return v.isoformat() if v else None


def _pct(part: float, whole: float) -> Optional[float]:
    """part / whole × 100 at 2 dp; None when there is no whole."""
    if not whole:
        return None
    return round(part / whole * 100.0, 2)


def _name_key(name) -> Tuple[int, str]:
    return (name is None, str(name or "").lower())


# =============================================================================
# Pure builder (no DB) — tested directly
# =============================================================================


def _detail_row(r: dict, as_on: date, rule: SettleRule) -> Optional[dict]:
    """One PO-item row with its balance, or None when nothing is outstanding.

    ordered_wt = po_weight × weight_share (None when either is missing);
    balance_wt = max(ordered_wt − received_wt, 0). When the ordered weight is
    unknown the item is judged on quantity instead, so a PO whose lines never
    got a weight share still shows up if drums/bales are pending.

    stale / settle_suggest are PO-level flags (header receipts) repeated on
    every item row of the PO, like received_lorries.
    """
    po_weight = _f(r.get("po_weight"), None)
    share = _f(r.get("weight_share"), None)
    ordered_wt = po_weight * share if po_weight is not None and share is not None else None
    received_wt = _f(r.get("received_wt")) or 0.0
    ordered_qty = _f(r.get("ordered_qty")) or 0.0
    received_qty = _f(r.get("received_qty")) or 0.0
    balance_qty = max(ordered_qty - received_qty, 0.0)

    if ordered_wt is not None and ordered_wt > 0:
        balance_wt: Optional[float] = max(ordered_wt - received_wt, 0.0)
        outstanding = balance_wt > 0
        balance_fraction: Optional[float] = min(balance_wt / ordered_wt, 1.0)
    else:
        balance_wt = None
        outstanding = balance_qty > 0
        balance_fraction = (
            min(balance_qty / ordered_qty, 1.0) if ordered_qty > 0 else None
        )
    if not outstanding:
        return None

    ordered_value = _f(r.get("ordered_value"), None)
    balance_value = (
        ordered_value * balance_fraction
        if ordered_value is not None and balance_fraction is not None
        else None
    )

    po_date = _as_date(r.get("po_date"))
    delivery_days = r.get("delivery_days")
    due_date = (
        po_date + timedelta(days=int(delivery_days))
        if po_date is not None and delivery_days is not None
        else None
    )
    vehicles = r.get("vehicle_quantity")
    received_lorries = _i(r.get("received_lorries"))
    po_received_weight = _f(r.get("po_received_weight")) or 0.0
    returned_lorries = _i(r.get("returned_lorries"))
    returned_weight = _f(r.get("returned_weight")) or 0.0
    overdue = bool(due_date and due_date < as_on)
    last_receipt = _as_date(r.get("last_receipt_date"))
    days_since_receipt = (as_on - last_receipt).days if last_receipt else None
    stale = overdue and (last_receipt is None or days_since_receipt >= rule.stale_days)
    settle_suggest = is_settled(
        PoReceipts(
            vehicle_quantity=None if vehicles is None else _i(vehicles),
            po_weight=po_weight,
            received_lorries=received_lorries,
            received_weight=po_received_weight,
            returned_lorries=returned_lorries,
            returned_weight=returned_weight,
        ),
        rule,
    )

    return {
        "jute_po_id": _i(r.get("jute_po_id")),
        "po_label": r.get("po_label") or f"PO-{r.get('jute_po_id')}",
        "po_date": _iso(po_date),
        "age_days": (as_on - po_date).days if po_date else None,
        "due_date": _iso(due_date),
        "overdue": overdue,
        "po_status": r.get("po_status"),
        "party_name": r.get("party_name"),
        "mukam": r.get("mukam"),
        "vehicle_quantity": None if vehicles is None else _i(vehicles),
        "received_lorries": received_lorries,
        "pending_lorries": max(_i(vehicles) - received_lorries, 0),
        "po_weight": _r3(po_weight),
        "po_received_weight": _r3(po_received_weight),
        "po_pending_weight": _r3(max((po_weight or 0.0) - po_received_weight, 0.0)),
        # Returned lorries (MR status 48, gate entry by as_on): shown, never counted as received.
        "returned_lorries": returned_lorries,
        "returned_weight": _r3(returned_weight),
        "item_id": None if r.get("item_id") is None else _i(r.get("item_id")),
        "item_code": r.get("item_code"),
        "item_name": r.get("item_name"),
        "po_uom": r.get("po_uom"),
        "po_rate": _r2(_f(r.get("po_rate"), None)),
        # Lowest line rate of the item (= po_rate unless the same quality sits on
        # several PO lines at different rates); the PO Detail grid shows the
        # range. Display only: avg_rate keeps using po_rate.
        "po_rate_min": _r2(_f(r.get("po_rate_min"), None)),
        "ordered_qty": _r3(ordered_qty),
        "received_qty": _r3(received_qty),
        "balance_qty": _r3(balance_qty),
        "ordered_wt": _r3(ordered_wt),
        "received_wt": _r3(received_wt),
        "balance_wt": _r3(balance_wt),
        "balance_pct": (
            _pct(balance_wt, ordered_wt)
            if balance_wt is not None and ordered_wt
            else _pct(balance_qty, ordered_qty)
        ),
        "ordered_value": _r2(ordered_value),
        "balance_value": _r2(balance_value),
        "mr_count": _i(r.get("mr_count")),
        "last_receipt_date": _iso(last_receipt),
        "days_since_receipt": days_since_receipt,
        "stale": stale,
        "settle_suggest": settle_suggest,
    }


_SUM_FIELDS = (
    "ordered_qty",
    "received_qty",
    "balance_qty",
    "ordered_wt",
    "received_wt",
    "balance_wt",
    "ordered_value",
    "balance_value",
)


def _new_bucket() -> dict:
    b = {f: 0.0 for f in _SUM_FIELDS}
    b["_pos"] = set()
    b["_overdue_pos"] = set()
    b["_stale_pos"] = set()
    b["_suggest_pos"] = set()
    # PO-level lorry/weight figures, keyed by PO so item rows never double count.
    b["_po_lorries"] = {}
    b["_rate_wt"] = 0.0  # Σ balance_wt × rate, for the balance-weighted rate
    b["_rate_base"] = 0.0
    return b


def _accumulate(b: dict, d: dict) -> None:
    for f in _SUM_FIELDS:
        v = d.get(f)
        if v is not None:
            b[f] += v
    b["_pos"].add(d["jute_po_id"])
    if d["overdue"]:
        b["_overdue_pos"].add(d["jute_po_id"])
    if d["stale"]:
        b["_stale_pos"].add(d["jute_po_id"])
    if d["settle_suggest"]:
        b["_suggest_pos"].add(d["jute_po_id"])
    b["_po_lorries"][d["jute_po_id"]] = (
        d["vehicle_quantity"] or 0, d["received_lorries"], d["pending_lorries"],
        d["returned_lorries"], d["po_weight"] or 0.0, d["po_received_weight"] or 0.0,
        d["po_pending_weight"] or 0.0, d["returned_weight"] or 0.0,
    )
    if d.get("balance_wt") and d.get("po_rate") is not None:
        b["_rate_wt"] += d["balance_wt"] * d["po_rate"]
        b["_rate_base"] += d["balance_wt"]


def _finish(b: dict) -> dict:
    out = {f: _r3(b[f]) if f.endswith(("qty", "wt")) else _r2(b[f]) for f in _SUM_FIELDS}
    out["po_count"] = len(b["_pos"])
    out["overdue_pos"] = len(b["_overdue_pos"])
    out["stale_pos"] = len(b["_stale_pos"])
    out["settle_suggest_pos"] = len(b["_suggest_pos"])
    # Lorry-and-weight view (spec §11.2 / §11.4): sums over the bucket's distinct POs.
    lorries = list(b["_po_lorries"].values())
    out["order_lorries"] = sum(x[0] for x in lorries)
    out["received_lorries"] = sum(x[1] for x in lorries)
    out["pending_lorries"] = sum(x[2] for x in lorries)
    out["returned_lorries"] = sum(x[3] for x in lorries)
    out["order_weight"] = _r3(sum(x[4] for x in lorries))
    out["received_weight"] = _r3(sum(x[5] for x in lorries))
    out["pending_weight"] = _r3(sum(x[6] for x in lorries))
    out["returned_weight"] = _r3(sum(x[7] for x in lorries))
    out["balance_pct"] = _pct(b["balance_wt"], b["ordered_wt"])
    out["avg_rate"] = _r2(b["_rate_wt"] / b["_rate_base"]) if b["_rate_base"] else None
    return out


def build_po_position(rows: List[dict], as_on: date, rule: Optional[SettleRule] = None) -> dict:
    """Turn the query's PO-item rows into the report sections.

    rows: dicts shaped like get_jute_po_outstanding_position_query()'s SELECT.
    rule: the company's settlement rule (defaults when None) for the stale /
    settle_suggest flags.
    Returns {"detail", "by_quality", "by_supplier", "totals"}; detail keeps
    the query order (po_date, po, item), by_quality is ordered by item_name,
    by_supplier by balance_wt desc.
    """
    if rule is None:
        rule = SettleRule()
    detail: List[dict] = []
    by_quality: Dict[Tuple[int, str], dict] = {}
    by_supplier: Dict[str, dict] = {}
    totals = _new_bucket()

    for r in rows:
        d = _detail_row(r, as_on, rule)
        if d is None:
            continue
        detail.append(d)
        _accumulate(totals, d)

        q_key = (d["item_id"] if d["item_id"] is not None else -1, str(d["item_name"] or ""))
        q = by_quality.get(q_key)
        if q is None:
            q = by_quality[q_key] = _new_bucket()
            q["item_id"] = d["item_id"]
            q["item_code"] = d["item_code"]
            q["item_name"] = d["item_name"]
        _accumulate(q, d)

        s_key = d["party_name"] or ""
        s = by_supplier.get(s_key)
        if s is None:
            s = by_supplier[s_key] = _new_bucket()
            s["party_name"] = d["party_name"]
            s["oldest_po_date"] = d["po_date"]
        elif d["po_date"] and (s["oldest_po_date"] is None or d["po_date"] < s["oldest_po_date"]):
            s["oldest_po_date"] = d["po_date"]
        _accumulate(s, d)

    quality_rows = [
        {"item_id": q["item_id"], "item_code": q["item_code"], "item_name": q["item_name"], **_finish(q)}
        for q in sorted(by_quality.values(), key=lambda q: (_name_key(q["item_name"]), q["item_id"] or 0))
    ]
    supplier_rows = sorted(
        (
            {"party_name": s["party_name"], "oldest_po_date": s["oldest_po_date"], **_finish(s)}
            for s in by_supplier.values()
        ),
        key=lambda s: (-(s["balance_wt"] or 0.0), _name_key(s["party_name"])),
    )
    return {
        "detail": detail,
        "by_quality": quality_rows,
        "by_supplier": supplier_rows,
        "totals": _finish(totals),
    }


# =============================================================================
# Parameter validation
# =============================================================================


def _require_int_param(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _as_on_param(request: Request) -> date:
    raw = request.query_params.get("as_on")
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid as_on format — expected YYYY-MM-DD")


# =============================================================================
# Endpoint
# =============================================================================


@router.get("/po-outstanding-position")
def po_outstanding_position(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Outstanding PO Position — ?co_id&branch_id[&as_on=YYYY-MM-DD] (as_on defaults to today)."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    as_on = _as_on_param(request)
    try:
        branch = db.execute(
            get_branch_name_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchone()
        if not branch:
            raise HTTPException(status_code=400, detail=BAD_BRANCH_MSG)

        # Before the position query on purpose: the last statement executed
        # must stay the report query (tests assert its binds), and load_rule
        # falls back to defaults on a tenant without the settlement migration.
        rule = load_rule(db, co_id)
        rows = [
            dict(r._mapping)
            for r in db.execute(
                get_jute_po_outstanding_position_query(),
                {"co_id": co_id, "branch_id": branch_id, "as_on": as_on},
            ).fetchall()
        ]
        return {
            "data": {
                "as_on": as_on.isoformat(),
                "branch_id": branch_id,
                "branch_name": dict(branch._mapping).get("branch_name"),
                "rule": rule.as_dict(),
                **build_po_position(rows, as_on, rule),
            }
        }
    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=f"Error building PO position: {e}")
