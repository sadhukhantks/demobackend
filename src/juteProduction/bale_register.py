"""Bale Register report (GET /api/juteProductionReports/bale_register) — spec
docs/superpowers/specs/2026-10-12-bale-stock-inspection-design.md §7c.

> "need to make an inspection and dispatch report. this can actually be
> something like a register, which has opening, production, despatch,
> closing" … "inspection not required in this as already covered in the
> finishing stock report" (owner, 2026-10-12).

A stock register for a date range, day by day, per finishing quality
(default) or per bale series, built from the same three events the Bale
Stock page derives its status from (bale_stock_query, spec §3):

  pressed(d)              bale entries with tran_date = d — PROD AND REPACK (a
                          REPACK row is a new bale)
  repacked_out(d)         bales consumed by a REPACK dated d (the §3 MAX() rule)
  despatched_invoice(d) / live despatch links dated d, split by whether they
  despatched_no_invoice(d) carry an invoice (§7b); despatched = the sum
  opening(d)              bales pressed before d − out before d
  closing(d)              opening + pressed − repacked_out − despatched = opening(d+1)

NO inspection figures — the Finishing Stock report already carries them.

Shape follows bale_reports.py: sync ``def`` handler, ``get_tenant_db`` +
``get_current_user_with_refresh``, ``{"data": ...}`` response, every
parameter validated (400) before the try/except, arg-less query builders,
and a DB-free pure builder (``build_bale_register``) that the tests hit
directly with stub rows. The handler issues exactly four statements, in this
order: bales, repacks, despatches, unit names.
"""

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.bale_register_query import (
    get_finishing_stock_unit_names_query,
    get_register_bales_query,
    get_register_despatches_query,
    get_register_repacks_query,
)
from src.juteProduction.constants import (
    BALE_DESPATCH_REASON_LABELS,
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
)
from src.sales.constants import SALES_STATUS_IDS

router = APIRouter()
bale_register_router = router

GROUP_BY_QUALITY = "quality"
GROUP_BY_SERIES = "series"
GROUP_BY_VALUES = (GROUP_BY_QUALITY, GROUP_BY_SERIES)

# A register wider than a quarter is a different report (and ~3 statements
# over the whole bale table); the FE default is the last 7 days.
MAX_RANGE_DAYS = 92

# A despatch link to an invoice in one of these states is VOID — the bale is
# back in stock (spec D2 / D4). Defined here from the sales constants rather
# than imported from bale_stock.py so the two routers stay independent.
CANCELLED_INVOICE_STATUS_IDS = (
    SALES_STATUS_IDS["REJECTED"],
    SALES_STATUS_IDS["CANCELLED"],
)

RANGE_TOO_LONG_MSG = f"Date range too large — at most {MAX_RANGE_DAYS} days"
DATE_ORDER_MSG = "date_to is before date_from"
FUTURE_DATE_MSG = "date_to cannot be in the future"
BAD_GROUP_BY_MSG = f"group_by must be one of {', '.join(GROUP_BY_VALUES)}"
BAD_DATE_MSG = "Invalid {field} format — expected YYYY-MM-DD"

# Internal "how the bale left stock" markers (never serialised).
_OUT_REPACK = "REPACK"
_OUT_DESPATCH = "DESPATCH"

# Period / totals field order (the FE prints them in this order).
PERIOD_COUNT_FIELDS = (
    "opening",
    "pressed",
    "repacked_out",
    "despatched_invoice",
    "despatched_no_invoice",
    "despatched",
    "closing",
)


# =============================================================================
# Small helpers
# =============================================================================


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


def _i(v) -> Optional[int]:
    return None if v is None else int(v)


def _s(v) -> Optional[str]:
    return None if v is None else str(v)


def _plain(v) -> Any:
    """JSON-safe scalar: Decimal -> float, date/datetime -> ISO string."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(sep="T", timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _mt(kg: float) -> float:
    """kg -> MT at 3 dp, half-up on whole grams (the finishing-stock rule,
    kept byte-identical with bale_stock._mt without importing it)."""
    grams = Decimal(str(_f(kg))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(grams) / 1000


def _to_date(v) -> Optional[date]:
    """date / datetime / 'YYYY-MM-DD…' -> date (None stays None)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _mapping(row) -> dict:
    """SQLAlchemy Row or plain dict -> dict."""
    m = getattr(row, "_mapping", None)
    return dict(m) if m is not None else dict(row)


def compress_bale_nos(nos: Iterable[Any]) -> str:
    """Sorted, de-duplicated bale numbers as a compact run list:
    ``[601, 604, 603] -> "601, 603-604"``; a single number prints alone,
    two consecutive numbers print as a range, an empty input gives ``""``.
    """
    uniq = sorted({int(n) for n in nos if n is not None})
    if not uniq:
        return ""

    def _run(a: int, b: int) -> str:
        return str(a) if a == b else f"{a}-{b}"

    parts: List[str] = []
    start = prev = uniq[0]
    for n in uniq[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(_run(start, prev))
        start = prev = n
    parts.append(_run(start, prev))
    return ", ".join(parts)


def _despatch_is_live(m: dict) -> bool:
    """The §3 DESPATCHED test on one despatch row: the SQL ``is_live`` column
    when present, else no invoice / NULL status / status not cancelled."""
    if m.get("is_live") is not None:
        return bool(int(m["is_live"]))
    if m.get("invoice_id") is None:
        return True
    status = m.get("invoice_status_id", m.get("status_id"))
    return status is None or int(status) not in CANCELLED_INVOICE_STATUS_IDS


# =============================================================================
# Pure builder (no DB) — tested directly
# =============================================================================


def _index_bales(
    bales: List[Any],
    date_to: date,
    quality_type: Optional[int],
    finishing_quality_id: Optional[int],
    bale_series_id: Optional[int],
) -> Dict[int, dict]:
    """bale_entry_id -> normalised bale with an empty "out" slot. Applies the
    optional filters and drops anything pressed after date_to (the SQL
    already does; the builder is defensive about its inputs)."""
    index: Dict[int, dict] = {}
    for r in bales:
        m = _mapping(r)
        qt = _i(m.get("quality_type"))
        qid = _i(m.get("finishing_quality_id"))
        sid = _i(m.get("bale_series_id"))
        if quality_type is not None and qt != quality_type:
            continue
        if finishing_quality_id is not None and qid != finishing_quality_id:
            continue
        if bale_series_id is not None and sid != bale_series_id:
            continue
        tran = _to_date(m.get("tran_date"))
        if tran is None or tran > date_to:
            continue
        index[int(m["bale_entry_id"])] = {
            "tran_date": tran,
            "entry_type": _s(m.get("entry_type")),
            "bale_series_id": sid,
            "series_name": m.get("series_name"),
            "finishing_quality_id": qid,
            "item_id": _i(m.get("item_id")),
            "item_name": m.get("item_name"),
            "quality_type": qt,
            "bale_no": _i(m.get("bale_no")),
            "net_wt": _f(m.get("net_wt")),
            "out_kind": None,
            "out_date": None,
            "despatch": None,
        }
    return index


def _apply_outs(index: Dict[int, dict], repacks: List[Any], despatches: List[Any], date_to: date) -> None:
    """Stamp each bale's single exit event. A REPACK consumption wins over a
    despatch link (the §3 status priority: REPACKED before DESPATCHED), a void
    link (cancelled invoice) is ignored, and when a bale carries more than one
    event of a kind the earliest dated one is taken so a bale never leaves
    twice. Events after date_to are dropped (the SQL already caps them)."""
    for r in repacks:
        m = _mapping(r)
        bale = index.get(_i(m.get("consumed_id")) or -1)
        if bale is None:
            continue
        d = _to_date(m.get("tran_date"))
        if d is None or d > date_to:
            continue
        if bale["out_kind"] is None or d < bale["out_date"]:
            bale["out_kind"], bale["out_date"], bale["despatch"] = _OUT_REPACK, d, None

    for r in despatches:
        m = _mapping(r)
        if not _despatch_is_live(m):
            continue
        bale = index.get(_i(m.get("bale_entry_id")) or -1)
        if bale is None or bale["out_kind"] == _OUT_REPACK:
            continue
        d = _to_date(m.get("despatch_date"))
        if d is None or d > date_to:
            continue
        if bale["out_kind"] is None or d < bale["out_date"]:
            bale["out_kind"], bale["out_date"], bale["despatch"] = _OUT_DESPATCH, d, m


def _group_key(bale: dict, group_by: str) -> Tuple[str, dict]:
    qid = bale["finishing_quality_id"]
    meta = {
        "finishing_quality_id": qid,
        "item_name": bale["item_name"],
        "quality_type": bale["quality_type"],
        "bale_series_id": None,
        "series_name": None,
    }
    if group_by == GROUP_BY_SERIES:
        sid = bale["bale_series_id"]
        meta["bale_series_id"] = sid
        meta["series_name"] = bale["series_name"]
        return f"s:{sid if sid is not None else 0}:{qid}", meta
    return f"q:{qid}", meta


def _empty_period() -> dict:
    p: Dict[str, Any] = {k: 0 for k in PERIOD_COUNT_FIELDS}
    p["opening_kg"] = 0.0
    p["closing_kg"] = 0.0
    return p


def _finish_period(p: dict) -> dict:
    """Round the kg sums, derive MT, and emit the fields in spec order."""
    return {
        "opening": p["opening"],
        "opening_kg": round(p["opening_kg"], 3),
        "opening_mt": _mt(p["opening_kg"]),
        "pressed": p["pressed"],
        "repacked_out": p["repacked_out"],
        "despatched_invoice": p["despatched_invoice"],
        "despatched_no_invoice": p["despatched_no_invoice"],
        "despatched": p["despatched"],
        "closing": p["closing"],
        "closing_kg": round(p["closing_kg"], 3),
        "closing_mt": _mt(p["closing_kg"]),
    }


def _sort_key(g: dict) -> tuple:
    qt = g.get("quality_type")
    return (
        qt is None,
        qt or 0,
        str(g.get("item_name") or ""),
        g.get("series_name") is None,
        str(g.get("series_name") or ""),
        str(g.get("key") or ""),
    )


def _build_despatch_detail(index: Dict[int, dict], date_from: date, date_to: date) -> List[dict]:
    """One row per (day, series, quality, invoice | reason+ref) with the bale
    numbers compressed. remarks = the first non-empty remark in the group."""
    rows: Dict[tuple, dict] = {}
    for bale in index.values():
        if bale["out_kind"] != _OUT_DESPATCH:
            continue
        d = bale["out_date"]
        if not (date_from <= d <= date_to):
            continue
        dp = bale["despatch"] or {}
        invoice_id = _i(dp.get("invoice_id"))
        has_invoice = invoice_id is not None
        reason = _s(dp.get("despatch_reason")) or None
        ref = _s(dp.get("despatch_ref")) or None
        ref_key = ("I", invoice_id) if has_invoice else ("R", reason or "", ref or "")
        key = (d, bale["bale_series_id"], bale["finishing_quality_id"], ref_key)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "date": d.isoformat(),
                "series_name": bale["series_name"],
                "item_name": bale["item_name"],
                "quality_type": bale["quality_type"],
                "bale_nos": "",
                "count": 0,
                "invoice_id": invoice_id,
                "invoice_no": _plain(dp.get("invoice_no")) if has_invoice else None,
                "invoice_date": _s(_to_date(dp.get("invoice_date"))) if has_invoice else None,
                "party_name": dp.get("party_name") if has_invoice else None,
                "despatch_reason_label": (
                    BALE_DESPATCH_REASON_LABELS.get(reason, reason) if reason else None
                ),
                "despatch_ref": ref,
                "remarks": None,
                "_nos": [],
            }
        row["_nos"].append(bale["bale_no"])
        if row["remarks"] is None and dp.get("remarks"):
            row["remarks"] = str(dp["remarks"])

    def _detail_sort(item: tuple) -> tuple:
        (d, _sid, _qid, ref_key), row = item
        qt = row.get("quality_type")
        return (
            d,
            qt is None,
            qt or 0,
            str(row.get("item_name") or ""),
            str(row.get("series_name") or ""),
            0 if ref_key[0] == "I" else 1,
            str(row.get("invoice_no") or ""),
            str(row.get("despatch_reason_label") or ""),
            str(row.get("despatch_ref") or ""),
        )

    out: List[dict] = []
    for _key, row in sorted(rows.items(), key=_detail_sort):
        nos = row.pop("_nos")
        row["bale_nos"] = compress_bale_nos(nos)
        row["count"] = len(nos)
        out.append(row)
    return out


def build_bale_register(
    bales: List[Any],
    repacks: List[Any],
    despatches: List[Any],
    *,
    date_from,
    date_to,
    group_by: str = GROUP_BY_QUALITY,
    quality_type: Optional[int] = None,
    finishing_quality_id: Optional[int] = None,
    bale_series_id: Optional[int] = None,
) -> dict:
    """The §7c register from the three flat row sets (SQLAlchemy rows or
    plain dicts).

    Returns ``{"groups": [...], "totals": {...}, "despatch_detail": [...]}``:

    * ``groups`` — one per finishing quality (``key "q:<qid>"``) or per
      (series, quality) (``key "s:<series_id>:<qid>"``), each with ``days``
      for EVERY calendar day of the range (zero rows included — it is a
      register) and a ``period`` block (opening / opening_kg / opening_mt,
      the four movement sums, despatched, closing / closing_kg / closing_mt).
      Groups with zero opening and no movement are omitted; ordering is
      quality_type, item_name, series_name.
    * ``totals`` — ``hessian`` / ``sacking`` / ``all`` period blocks summed
      over the groups (a quality with an unknown type counts in ``all`` only).
    * ``despatch_detail`` — the live despatches inside the range grouped by
      (date, series, quality, invoice | reason + ref), bale numbers
      compressed ("601, 603-604"), with invoice no / date / party or the
      reason label / reference.

    Rules: a REPACK row counts as pressed on its own date and its consumed
    bale leaves as repacked_out on that same date; a despatch counts only
    when LIVE (no invoice, or the invoice not Rejected / Cancelled); a bale
    consumed by a REPACK is never also counted as despatched. The optional
    filters narrow the bales before anything is counted, so the totals are
    totals of what is shown.
    """
    if group_by not in GROUP_BY_VALUES:
        raise ValueError(BAD_GROUP_BY_MSG)
    d_from = _to_date(date_from)
    d_to = _to_date(date_to)
    if d_from is None or d_to is None or d_to < d_from:
        raise ValueError(DATE_ORDER_MSG)

    index = _index_bales(bales, d_to, quality_type, finishing_quality_id, bale_series_id)
    _apply_outs(index, repacks, despatches, d_to)

    days = [d_from + timedelta(days=n) for n in range((d_to - d_from).days + 1)]

    # per group: meta + opening counters + per-day movement counters
    # (movement slots: 0 pressed, 1 repacked_out, 2 despatched_invoice,
    # 3 despatched_no_invoice)
    groups: Dict[str, dict] = {}
    for bale in index.values():
        key, meta = _group_key(bale, group_by)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "key": key,
                **meta,
                "_opening": 0,
                "_opening_kg": 0.0,
                "_closing_kg": 0.0,
                "_moves": {d: [0, 0, 0, 0] for d in days},
            }
        tran = bale["tran_date"]
        out_kind, out_date = bale["out_kind"], bale["out_date"]

        if tran < d_from and (out_kind is None or out_date >= d_from):
            g["_opening"] += 1
            g["_opening_kg"] += bale["net_wt"]
        if d_from <= tran <= d_to:
            g["_moves"][tran][0] += 1
        if out_kind is not None and d_from <= out_date <= d_to:
            if out_kind == _OUT_REPACK:
                g["_moves"][out_date][1] += 1
            elif (bale["despatch"] or {}).get("invoice_id") is not None:
                g["_moves"][out_date][2] += 1
            else:
                g["_moves"][out_date][3] += 1
        if out_kind is None:  # still in stock at the end of date_to
            g["_closing_kg"] += bale["net_wt"]

    out_groups: List[dict] = []
    totals_acc = {
        "hessian": _empty_period(),
        "sacking": _empty_period(),
        "all": _empty_period(),
    }
    type_bucket = {
        FINISHING_QUALITY_TYPE_HESSIAN: "hessian",
        FINISHING_QUALITY_TYPE_SACKING: "sacking",
    }
    for g in sorted(groups.values(), key=_sort_key):
        running = g["_opening"]
        period = _empty_period()
        period["opening"] = g["_opening"]
        period["opening_kg"] = g["_opening_kg"]
        day_rows: List[dict] = []
        for d in days:
            pressed, repacked_out, desp_inv, desp_no_inv = g["_moves"][d]
            despatched = desp_inv + desp_no_inv
            closing = running + pressed - repacked_out - despatched
            day_rows.append(
                {
                    "date": d.isoformat(),
                    "opening": running,
                    "pressed": pressed,
                    "repacked_out": repacked_out,
                    "despatched_invoice": desp_inv,
                    "despatched_no_invoice": desp_no_inv,
                    "despatched": despatched,
                    "closing": closing,
                }
            )
            period["pressed"] += pressed
            period["repacked_out"] += repacked_out
            period["despatched_invoice"] += desp_inv
            period["despatched_no_invoice"] += desp_no_inv
            period["despatched"] += despatched
            running = closing
        period["closing"] = running
        period["closing_kg"] = g["_closing_kg"]

        has_activity = any(
            period[k] for k in ("opening", "pressed", "repacked_out", "despatched")
        )
        if not has_activity:
            continue

        out_groups.append(
            {
                "key": g["key"],
                "finishing_quality_id": g["finishing_quality_id"],
                "item_name": g["item_name"],
                "quality_type": g["quality_type"],
                "bale_series_id": g["bale_series_id"],
                "series_name": g["series_name"],
                "days": day_rows,
                "period": _finish_period(period),
            }
        )
        buckets = ["all"]
        bucket = type_bucket.get(g["quality_type"])
        if bucket is not None:
            buckets.append(bucket)
        for name in buckets:
            acc = totals_acc[name]
            for k in PERIOD_COUNT_FIELDS:
                acc[k] += period[k]
            acc["opening_kg"] += period["opening_kg"]
            acc["closing_kg"] += period["closing_kg"]

    return {
        "groups": out_groups,
        "totals": {name: _finish_period(acc) for name, acc in totals_acc.items()},
        "despatch_detail": _build_despatch_detail(index, d_from, d_to),
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


def _optional_int_param(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _require_date_param(request: Request, name: str) -> date:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=BAD_DATE_MSG.format(field=name))


def _date_range(request: Request) -> Tuple[date, date]:
    """date_from <= date_to <= today, at most MAX_RANGE_DAYS calendar days."""
    d_from = _require_date_param(request, "date_from")
    d_to = _require_date_param(request, "date_to")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail=DATE_ORDER_MSG)
    if d_to > date.today():
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=RANGE_TOO_LONG_MSG)
    return d_from, d_to


def _group_by_param(request: Request) -> str:
    raw = (request.query_params.get("group_by") or GROUP_BY_QUALITY).strip().lower()
    if raw not in GROUP_BY_VALUES:
        raise HTTPException(status_code=400, detail=BAD_GROUP_BY_MSG)
    return raw


def _rows(db: Session, query, binds: dict) -> List[dict]:
    return [dict(r._mapping) for r in db.execute(query, binds).fetchall()]


def _own_names(db: Session, co_id: int, branch_id: int) -> Tuple[Optional[str], Optional[str]]:
    row = db.execute(
        get_finishing_stock_unit_names_query(),
        {"co_id": co_id, "branch_id": branch_id},
    ).fetchone()
    if row is None:
        return None, None
    return _s(getattr(row, "co_name", None)), _s(getattr(row, "branch_name", None))


# =============================================================================
# Endpoint
# =============================================================================


@router.get("/bale_register")
def bale_register(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Bale Register —
    ?co_id&branch_id&date_from&date_to[&quality_type][&finishing_quality_id][&bale_series_id][&group_by=quality|series]."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    d_from, d_to = _date_range(request)
    quality_type = _optional_int_param(request, "quality_type")
    finishing_quality_id = _optional_int_param(request, "finishing_quality_id")
    bale_series_id = _optional_int_param(request, "bale_series_id")
    group_by = _group_by_param(request)
    try:
        binds = {"co_id": co_id, "branch_id": branch_id, "date_to": d_to}
        bales = _rows(db, get_register_bales_query(), binds)
        repacks = _rows(db, get_register_repacks_query(), binds)
        despatches = _rows(
            db,
            get_register_despatches_query(),
            {**binds, "cancelled_status": list(CANCELLED_INVOICE_STATUS_IDS)},
        )
        co_name, branch_name = _own_names(db, co_id, branch_id)

        report = build_bale_register(
            bales,
            repacks,
            despatches,
            date_from=d_from,
            date_to=d_to,
            group_by=group_by,
            quality_type=quality_type,
            finishing_quality_id=finishing_quality_id,
            bale_series_id=bale_series_id,
        )
        return {
            "data": {
                "date_from": str(d_from),
                "date_to": str(d_to),
                "group_by": group_by,
                "co_name": co_name,
                "branch_name": branch_name,
                **report,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
