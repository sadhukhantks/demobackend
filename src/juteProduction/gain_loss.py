"""Jute Gain/Loss Sheet — monthly entry + statement report (/api/juteGainLoss).

Replaces the mill's "Jute Gain / Loss" Excel workbook. One manually-entered
sheet per (co, branch, month) renders THREE statements — FNG (packed
production), WVG (weaving production) and WDG (winding-to-weaving difference)
— which differ only in which figure rows and which process-stock departments
they include. Both scopes are data (jute_gl_row_mst / jute_gl_dept_mst), not
code.

Everything the Excel hardcoded is dynamic here:
  * To-Date = sum of saved sheets from the FY start (April) to the month —
    the Excel hardcoded every prior month as literals inside formulas.
  * Departments (incl. Singur Shed) and figure rows come from masters.
  * Opening process stock is prefilled from the previous month's closing and
    the report flags any mismatch instead of silently trusting a copy.

Lifecycle: Draft -> Lock (POST /lock). A locked month rejects saves until
POST /unlock — covering the mill's "revised" months without duplicate sheets.

Shape follows winding_stock.py: literal {"data": ...} returns, Pydantic body
params, arg-less query builders, soft delete, sync handlers, and pure
statement builders kept free of DB access so tests exercise the arithmetic
directly.
"""

import math
from datetime import date
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
    GL_FY_START_MONTH,
    GL_PROD_STATEMENTS,
    GL_QTY_ABS_MAX,
    GL_STATEMENT_FNG,
    GL_STATEMENT_TITLES,
    GL_STATEMENT_WDG,
    GL_STATEMENT_WVG,
    GL_STATEMENTS,
)
from src.juteProduction.gain_loss_query import (
    get_dtl_in_range_query,
    get_dtl_query,
    get_gl_depts_query,
    get_gl_rows_query,
    get_hdr_query,
    get_hdrs_in_range_query,
    get_prod_in_range_query,
    get_prod_query,
    get_ps_in_range_query,
    get_ps_query,
    get_quality_suggestions_query,
    get_used_quality_names_query,
    insert_dtl_query,
    insert_hdr_query,
    insert_prod_query,
    insert_ps_query,
    set_hdr_lock_query,
    soft_delete_dtl_query,
    soft_delete_prod_query,
    soft_delete_ps_query,
    update_hdr_query,
    verify_branch_co_query,
    verify_gl_dept_ids_query,
    verify_gl_row_ids_query,
)

router = APIRouter()

LOCKED_MSG = "This month is locked. Unlock it before saving changes."
BAD_MONTH_MSG = "Invalid month format (expected YYYY-MM or YYYY-MM-DD)"
FUTURE_MONTH_MSG = "month cannot be in the future"
BAD_BRANCH_MSG = "branch_id must be an active branch of this company"
BAD_ROW_IDS_MSG = "figures reference rows that are not active rows of this company"
BAD_DEPT_IDS_MSG = "process_stock references departments that are not active for this company"
BAD_STATEMENT_MSG = f"statement must be one of {', '.join(GL_PROD_STATEMENTS)}"
BAD_QUALITY_TYPE_MSG = "quality_type must be 1 (Hessian) or 2 (Sacking)"
QUALITY_NAME_REQUIRED_MSG = "quality_name is required on every production line"
QUALITY_NAME_TOO_LONG_MSG = "quality_name cannot exceed 255 characters"
REMARKS_TOO_LONG_MSG = "remarks cannot exceed 1000 characters"
BAD_QTY_MSG = f"quantities must be finite numbers within +/-{GL_QTY_ABS_MAX:g} MT"
SHEET_NOT_FOUND_MSG = "No sheet is saved for this month"

QT_HESS = FINISHING_QUALITY_TYPE_HESSIAN
QT_SACK = FINISHING_QUALITY_TYPE_SACKING

# jute_gl_row_mst flag column per statement — scoping is data, this just names
# which flag applies.
_STMT_FLAG = {
    GL_STATEMENT_FNG: "in_fng",
    GL_STATEMENT_WVG: "in_wvg",
    GL_STATEMENT_WDG: "in_wdg",
}
# Which captured production block each statement renders as its output.
_STMT_PROD_SOURCE = {
    GL_STATEMENT_FNG: GL_STATEMENT_FNG,
    GL_STATEMENT_WVG: GL_STATEMENT_WVG,
    GL_STATEMENT_WDG: GL_STATEMENT_WVG,
}


# =============================================================================
# Request models
# =============================================================================


class GlFigureIn(BaseModel):
    gl_row_id: int
    hess_qty: float = 0
    sack_qty: float = 0


class GlProdIn(BaseModel):
    statement: str
    quality_type: int
    quality_name: str
    qty: float = 0
    display_order: int = 0


class GlProcessStockIn(BaseModel):
    gl_dept_id: int
    opening_hess: float = 0
    opening_sack: float = 0
    closing_hess: float = 0
    closing_sack: float = 0


class GlSheetSave(BaseModel):
    co_id: int
    branch_id: int
    month: str  # YYYY-MM or YYYY-MM-DD
    remarks: Optional[str] = None
    figures: List[GlFigureIn] = []
    productions: List[GlProdIn] = []
    process_stock: List[GlProcessStockIn] = []


class GlMonthRef(BaseModel):
    co_id: int
    branch_id: int
    month: str


# =============================================================================
# Param + value helpers
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
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def parse_month(raw: str) -> date:
    """'YYYY-MM' or any day of the month -> the 1st of that month."""
    value = (raw or "").strip()
    try:
        if len(value) == 7:
            d = date.fromisoformat(value + "-01")
        else:
            d = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=BAD_MONTH_MSG)
    return d.replace(day=1)


def _require_month_param(request: Request) -> date:
    raw = request.query_params.get("month")
    if not raw:
        raise HTTPException(status_code=400, detail="month is required")
    return parse_month(raw)


def fy_start_of(month: date) -> date:
    """First month of the financial year containing ``month`` (April FY)."""
    year = month.year if month.month >= GL_FY_START_MONTH else month.year - 1
    return date(year, GL_FY_START_MONTH, 1)


def prev_month_of(month: date) -> date:
    return (
        date(month.year - 1, 12, 1)
        if month.month == 1
        else date(month.year, month.month - 1, 1)
    )


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


def _validate_qty(value, msg: str = BAD_QTY_MSG) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=msg)
    if not math.isfinite(n) or abs(n) > GL_QTY_ABS_MAX:
        raise HTTPException(status_code=400, detail=msg)
    return n


def _validate_remarks(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if len(value) > 1000:
        raise HTTPException(status_code=400, detail=REMARKS_TOO_LONG_MSG)
    return value


def _validate_branch(db: Session, co_id: int, branch_id: int) -> None:
    row = db.execute(
        verify_branch_co_query(), {"co_id": co_id, "branch_id": branch_id}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=BAD_BRANCH_MSG)


def _validate_id_set(db: Session, query, co_id: int, ids: List[int], msg: str) -> None:
    distinct = sorted(set(int(i) for i in ids))
    if not distinct:
        return
    row = db.execute(
        query, {"co_id": co_id, "ids": ",".join(str(i) for i in distinct)}
    ).fetchone()
    if not row or int(row.n) != len(distinct):
        raise HTTPException(status_code=400, detail=msg)


def _get_hdr(db: Session, co_id: int, branch_id: int, month: date):
    return db.execute(
        get_hdr_query(),
        {"co_id": co_id, "branch_id": branch_id, "month_date": month},
    ).fetchone()


# =============================================================================
# Pure builders (no DB) — tested directly
# =============================================================================


def figures_map(dtl_rows: List[dict]) -> Dict[int, Dict[str, float]]:
    """dtl rows -> {gl_row_id: {'hess','sack'}} (summed if a month range)."""
    out: Dict[int, Dict[str, float]] = {}
    for r in dtl_rows:
        cell = out.setdefault(int(r["gl_row_id"]), {"hess": 0.0, "sack": 0.0})
        cell["hess"] += _f(r.get("hess_qty"))
        cell["sack"] += _f(r.get("sack_qty"))
    return out


def prod_key(row: dict) -> Tuple[str, int, str]:
    return (
        str(row.get("statement")),
        int(row.get("quality_type")),
        str(row.get("quality_name") or "").strip().upper(),
    )


def prod_totals_map(prod_rows: List[dict]) -> Dict[Tuple[str, int, str], float]:
    """Production rows -> {(statement, quality_type, NAME): qty} (summed)."""
    out: Dict[Tuple[str, int, str], float] = {}
    for r in prod_rows:
        k = prod_key(r)
        out[k] = out.get(k, 0.0) + _f(r.get("qty"))
    return out


def ps_scope_ids(depts: List[dict], statement: str) -> List[int]:
    """Departments a statement spans. FNG = every active department."""
    if statement == GL_STATEMENT_FNG:
        return [int(d["gl_dept_id"]) for d in depts]
    flag = "in_wvg" if statement == GL_STATEMENT_WVG else "in_wdg"
    return [int(d["gl_dept_id"]) for d in depts if int(d.get(flag) or 0) == 1]


def ps_totals(ps_rows: List[dict], scope: List[int]) -> Dict[str, Dict[str, float]]:
    """Opening/closing Hess+Sack summed over the departments in scope."""
    scope_set = set(scope)
    out = {
        "opening": {"hess": 0.0, "sack": 0.0},
        "closing": {"hess": 0.0, "sack": 0.0},
    }
    for r in ps_rows:
        if int(r["gl_dept_id"]) not in scope_set:
            continue
        out["opening"]["hess"] += _f(r.get("opening_hess"))
        out["opening"]["sack"] += _f(r.get("opening_sack"))
        out["closing"]["hess"] += _f(r.get("closing_hess"))
        out["closing"]["sack"] += _f(r.get("closing_sack"))
    return out


def _cells(hess: float, sack: float) -> Dict[str, float]:
    return {"hess": hess, "sack": sack, "total": hess + sack}


def _row(
    label: str,
    cur: Dict[str, float],
    to_date: Optional[float],
    prev: Optional[Dict[str, float]],
    **extra,
) -> dict:
    prev = prev or {"hess": 0.0, "sack": 0.0, "total": 0.0}
    out = {
        "label": label,
        "hess": cur["hess"],
        "sack": cur["sack"],
        "total": cur["total"],
        "to_date": to_date,
        "prev_hess": prev["hess"],
        "prev_sack": prev["sack"],
        "prev_total": prev["total"],
    }
    out.update(extra)
    return out


def _pct(gain: float, consumed: float) -> Optional[float]:
    return None if consumed == 0 else gain / consumed * 100.0


def build_statement(
    statement: str,
    rows_master: List[dict],
    depts_master: List[dict],
    cur_figures: Dict[int, Dict[str, float]],
    cur_prod: List[dict],
    cur_ps: List[dict],
    prev_figures: Dict[int, Dict[str, float]],
    prev_prod: List[dict],
    prev_ps: List[dict],
    td_figures: Dict[int, Dict[str, float]],
    td_prod_map: Dict[Tuple[str, int, str], float],
    fy_first_ps: List[dict],
) -> dict:
    """Render one statement from the shared monthly dataset.

    ``fy_first_ps`` is the process stock of the EARLIEST saved sheet of the
    financial year: its OPENING is the To-Date opening (the Excel keeps
    April's opening in the To-Date column all year), while the To-Date
    closing is the current month's own closing.
    """
    if statement not in GL_STATEMENTS:
        raise ValueError(f"unknown statement {statement}")

    flag = _STMT_FLAG[statement]
    prod_source = _STMT_PROD_SOURCE[statement]
    scope = ps_scope_ids(depts_master, statement)

    cur_tot = ps_totals(cur_ps, scope)
    prev_tot = ps_totals(prev_ps, scope)
    fy_tot = ps_totals(fy_first_ps, scope)

    # ---- INPUT ----
    def fig(m: Dict[int, Dict[str, float]], row_id: int) -> Dict[str, float]:
        cell = m.get(row_id, {"hess": 0.0, "sack": 0.0})
        return _cells(cell["hess"], cell["sack"])

    input_rows = []
    net = {"hess": 0.0, "sack": 0.0}
    td_net = 0.0
    for r in rows_master:
        if r["section"] != "INPUT" or int(r.get(flag) or 0) != 1:
            continue
        row_id = int(r["gl_row_id"])
        sign = int(r.get("calc_sign") or 1)
        cur = fig(cur_figures, row_id)
        prev = fig(prev_figures, row_id)
        td = fig(td_figures, row_id)["total"]
        input_rows.append(_row(r["row_name"], cur, td, prev, sign=sign))
        net["hess"] += sign * cur["hess"]
        net["sack"] += sign * cur["sack"]
        td_net += sign * td

    opening = _cells(cur_tot["opening"]["hess"], cur_tot["opening"]["sack"])
    closing = _cells(cur_tot["closing"]["hess"], cur_tot["closing"]["sack"])
    prev_opening = _cells(prev_tot["opening"]["hess"], prev_tot["opening"]["sack"])
    prev_closing = _cells(prev_tot["closing"]["hess"], prev_tot["closing"]["sack"])
    td_opening = fy_tot["opening"]["hess"] + fy_tot["opening"]["sack"]
    td_closing = closing["total"]

    net["hess"] += opening["hess"] - closing["hess"]
    net["sack"] += opening["sack"] - closing["sack"]
    td_net += td_opening - td_closing
    net_row = _row(
        "NET CONSUMED (A)", _cells(net["hess"], net["sack"]), td_net,
        _cells(
            prev_opening["hess"] - prev_closing["hess"]
            + sum(int(r.get("calc_sign") or 1) * fig(prev_figures, int(r["gl_row_id"]))["hess"]
                  for r in rows_master
                  if r["section"] == "INPUT" and int(r.get(flag) or 0) == 1),
            prev_opening["sack"] - prev_closing["sack"]
            + sum(int(r.get("calc_sign") or 1) * fig(prev_figures, int(r["gl_row_id"]))["sack"]
                  for r in rows_master
                  if r["section"] == "INPUT" and int(r.get(flag) or 0) == 1),
        ),
    )

    # ---- OUTPUT: quality-wise production ----
    prev_prod_map = prod_totals_map(prev_prod)

    def quality_rows(quality_type: int) -> Tuple[List[dict], Dict[str, float]]:
        rows = []
        total = 0.0
        prev_total = 0.0
        seen = set()
        for p in cur_prod:
            if p["statement"] != prod_source or int(p["quality_type"]) != quality_type:
                continue
            qty = _f(p.get("qty"))
            k = prod_key(p)
            seen.add(k)
            cur = _cells(qty, 0.0) if quality_type == QT_HESS else _cells(0.0, qty)
            prev_qty = prev_prod_map.get(k, 0.0)
            prev = (
                _cells(prev_qty, 0.0)
                if quality_type == QT_HESS
                else _cells(0.0, prev_qty)
            )
            rows.append(_row(p["quality_name"], cur, td_prod_map.get(k, qty), prev))
            total += qty
            prev_total += prev_qty
        # prev-month qualities absent this month still print (with zero now)
        for k, prev_qty in prev_prod_map.items():
            if k[0] != prod_source or k[1] != quality_type or k in seen:
                continue
            cur = _cells(0.0, 0.0)
            prev = (
                _cells(prev_qty, 0.0)
                if quality_type == QT_HESS
                else _cells(0.0, prev_qty)
            )
            rows.append(_row(k[2], cur, td_prod_map.get(k, 0.0), prev))
            prev_total += prev_qty
        totals = {"cur": total, "prev": prev_total}
        return rows, totals

    hess_rows, hess_tot = quality_rows(QT_HESS)
    sack_rows, sack_tot = quality_rows(QT_SACK)

    def td_type_total(quality_type: int) -> float:
        return sum(
            v for k, v in td_prod_map.items()
            if k[0] == prod_source and k[1] == quality_type
        )

    hess_total_row = _row(
        "HESSIAN TOTAL", _cells(hess_tot["cur"], 0.0), td_type_total(QT_HESS),
        _cells(hess_tot["prev"], 0.0),
    )
    sack_total_row = _row(
        "SACKING TOTAL", _cells(0.0, sack_tot["cur"]), td_type_total(QT_SACK),
        _cells(0.0, sack_tot["prev"]),
    )
    sub_total_row = _row(
        "SUB TOTAL", _cells(hess_tot["cur"], sack_tot["cur"]),
        td_type_total(QT_HESS) + td_type_total(QT_SACK),
        _cells(hess_tot["prev"], sack_tot["prev"]),
    )

    # ---- OUTPUT: misc figure rows ----
    misc_rows = []
    out_tot = {"hess": hess_tot["cur"], "sack": sack_tot["cur"]}
    prev_out_tot = {"hess": hess_tot["prev"], "sack": sack_tot["prev"]}
    td_out = sub_total_row["to_date"] or 0.0
    for r in rows_master:
        if r["section"] != "OUTPUT" or int(r.get(flag) or 0) != 1:
            continue
        row_id = int(r["gl_row_id"])
        cur = fig(cur_figures, row_id)
        prev = fig(prev_figures, row_id)
        td = fig(td_figures, row_id)["total"]
        misc_rows.append(_row(r["row_name"], cur, td, prev))
        out_tot["hess"] += cur["hess"]
        out_tot["sack"] += cur["sack"]
        prev_out_tot["hess"] += prev["hess"]
        prev_out_tot["sack"] += prev["sack"]
        td_out += td

    total_output_row = _row(
        "TOTAL OUTPUT (B)", _cells(out_tot["hess"], out_tot["sack"]), td_out,
        _cells(prev_out_tot["hess"], prev_out_tot["sack"]),
    )

    # ---- GAIN / LOSS ----
    gain = _cells(
        out_tot["hess"] - net["hess"], out_tot["sack"] - net["sack"]
    )
    prev_gain = _cells(
        total_output_row["prev_hess"] - net_row["prev_hess"],
        total_output_row["prev_sack"] - net_row["prev_sack"],
    )
    td_gain = td_out - td_net
    gain_row = _row("JUTE GAIN / LOSS ( B - A )", gain, td_gain, prev_gain)
    pct_row = {
        "label": "JUTE GAIN / LOSS IN %",
        "hess": _pct(gain["hess"], net["hess"]),
        "sack": _pct(gain["sack"], net["sack"]),
        "total": _pct(gain["total"], net_row["total"]),
        "to_date": _pct(td_gain, td_net),
        "prev_hess": _pct(prev_gain["hess"], net_row["prev_hess"]),
        "prev_sack": _pct(prev_gain["sack"], net_row["prev_sack"]),
        "prev_total": _pct(prev_gain["total"], net_row["prev_total"]),
    }

    # ---- PROCESS STOCK section (dept-wise closing vs previous month) ----
    cur_by_dept = {int(r["gl_dept_id"]): r for r in cur_ps}
    prev_by_dept = {int(r["gl_dept_id"]): r for r in prev_ps}
    scope_set = set(scope)
    ps_rows_out = []
    for d in depts_master:
        dept_id = int(d["gl_dept_id"])
        if dept_id not in scope_set:
            continue
        c = cur_by_dept.get(dept_id, {})
        p = prev_by_dept.get(dept_id, {})
        closing_cells = _cells(_f(c.get("closing_hess")), _f(c.get("closing_sack")))
        opening_cells = _cells(_f(c.get("opening_hess")), _f(c.get("opening_sack")))
        prev_cells = _cells(_f(p.get("closing_hess")), _f(p.get("closing_sack")))
        ps_rows_out.append(
            {
                "dept_code": d["dept_code"],
                "label": d["dept_name"],
                "hess": closing_cells["hess"],
                "sack": closing_cells["sack"],
                "total": closing_cells["total"],
                "prev_hess": prev_cells["hess"],
                "prev_sack": prev_cells["sack"],
                "prev_total": prev_cells["total"],
                "diff": closing_cells["total"] - prev_cells["total"],
                # opening should equal the previous month's closing; a mismatch
                # means the operator overrode the prefill — surfaced, not hidden
                "opening_mismatch": abs(opening_cells["total"] - prev_cells["total"]) > 0.0005
                and bool(prev_by_dept),
            }
        )
    ps_total = {
        "label": "TOTAL",
        "hess": closing["hess"],
        "sack": closing["sack"],
        "total": closing["total"],
        "prev_hess": prev_closing["hess"],
        "prev_sack": prev_closing["sack"],
        "prev_total": prev_closing["total"],
        "diff": closing["total"] - prev_closing["total"],
    }

    return {
        "code": statement,
        "title": GL_STATEMENT_TITLES[statement],
        "input": {
            "rows": input_rows,
            "opening_ps": _row(
                "Add :- Opening Process Stock", opening, td_opening, prev_opening
            ),
            "closing_ps": _row(
                "Less :- Closing Process Stock", closing, td_closing, prev_closing
            ),
            "net_consumed": net_row,
        },
        "output": {
            "hessian_rows": hess_rows,
            "hessian_total": hess_total_row,
            "sacking_rows": sack_rows,
            "sacking_total": sack_total_row,
            "sub_total": sub_total_row,
            "misc_rows": misc_rows,
            "total_output": total_output_row,
        },
        "gain_loss": gain_row,
        "gain_loss_pct": pct_row,
        "process_stock": {"rows": ps_rows_out, "total": ps_total},
    }


# =============================================================================
# Setup
# =============================================================================


@router.get("/setup")
def setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Masters the entry page needs: figure rows, departments and quality-name
    suggestions (finishing-quality items + names used on earlier sheets)."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _optional_int_param(request, "branch_id")
    try:
        rows = [
            dict(r._mapping)
            for r in db.execute(get_gl_rows_query(), {"co_id": co_id}).fetchall()
        ]
        depts = [
            dict(r._mapping)
            for r in db.execute(get_gl_depts_query(), {"co_id": co_id}).fetchall()
        ]
        suggestions = [
            dict(r._mapping)
            for r in db.execute(
                get_quality_suggestions_query(), {"co_id": co_id}
            ).fetchall()
        ]
        if branch_id is not None:
            seen = {(None, s["quality_type"], s["quality_name"].strip().upper())
                    for s in suggestions}
            for r in db.execute(
                get_used_quality_names_query(),
                {"co_id": co_id, "branch_id": branch_id},
            ).fetchall():
                row = dict(r._mapping)
                key = (None, row["quality_type"], row["quality_name"].strip().upper())
                if key not in seen:
                    seen.add(key)
                    suggestions.append(
                        {
                            "quality_type": row["quality_type"],
                            "quality_name": row["quality_name"],
                        }
                    )
        return {
            "data": {
                "figure_rows": rows,
                "departments": depts,
                "quality_suggestions": suggestions,
                "statements": list(GL_STATEMENTS),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Sheet read
# =============================================================================


@router.get("/sheet")
def sheet(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The saved sheet for a month (or an empty shell), plus the previous
    month's closing process stock for the opening prefill."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    month = _require_month_param(request)
    try:
        hdr = _get_hdr(db, co_id, branch_id, month)

        figures, productions, process_stock = [], [], []
        if hdr:
            binds = {"gain_loss_id": hdr.gain_loss_id}
            figures = [dict(r._mapping) for r in db.execute(get_dtl_query(), binds)]
            productions = [
                dict(r._mapping) for r in db.execute(get_prod_query(), binds)
            ]
            process_stock = [
                dict(r._mapping) for r in db.execute(get_ps_query(), binds)
            ]
            for f_ in figures:
                f_["hess_qty"] = _f(f_["hess_qty"])
                f_["sack_qty"] = _f(f_["sack_qty"])
            for p in productions:
                p["qty"] = _f(p["qty"])
            for s in process_stock:
                for k in ("opening_hess", "opening_sack", "closing_hess", "closing_sack"):
                    s[k] = _f(s[k])

        prev = prev_month_of(month)
        prev_hdr = _get_hdr(db, co_id, branch_id, prev)
        prev_closing = []
        if prev_hdr:
            prev_closing = [
                {
                    "gl_dept_id": int(r._mapping["gl_dept_id"]),
                    "closing_hess": _f(r._mapping["closing_hess"]),
                    "closing_sack": _f(r._mapping["closing_sack"]),
                }
                for r in db.execute(
                    get_ps_query(), {"gain_loss_id": prev_hdr.gain_loss_id}
                )
            ]

        return {
            "data": {
                "header": (
                    None
                    if not hdr
                    else {
                        "gain_loss_id": hdr.gain_loss_id,
                        "month_date": str(hdr.month_date),
                        "remarks": hdr.remarks,
                        "is_locked": int(hdr.is_locked or 0),
                    }
                ),
                "figures": figures,
                "productions": productions,
                "process_stock": process_stock,
                "prev_month": str(prev),
                "prev_month_exists": prev_hdr is not None,
                "prev_closing": prev_closing,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Save
# =============================================================================


@router.post("/save")
def save(
    body: GlSheetSave,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Full-sheet upsert: header created or updated, children soft-deleted and
    re-inserted in one transaction. Rejected while the month is locked."""
    try:
        month = parse_month(body.month)
        if month > date.today().replace(day=1):
            raise HTTPException(status_code=400, detail=FUTURE_MONTH_MSG)
        remarks = _validate_remarks(body.remarks)
        _validate_branch(db, int(body.co_id), int(body.branch_id))

        for p in body.productions:
            if p.statement not in GL_PROD_STATEMENTS:
                raise HTTPException(status_code=400, detail=BAD_STATEMENT_MSG)
            if int(p.quality_type) not in (QT_HESS, QT_SACK):
                raise HTTPException(status_code=400, detail=BAD_QUALITY_TYPE_MSG)
            name = (p.quality_name or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail=QUALITY_NAME_REQUIRED_MSG)
            if len(name) > 255:
                raise HTTPException(status_code=400, detail=QUALITY_NAME_TOO_LONG_MSG)
            _validate_qty(p.qty)
        for f_ in body.figures:
            _validate_qty(f_.hess_qty)
            _validate_qty(f_.sack_qty)
        for s in body.process_stock:
            for v in (s.opening_hess, s.opening_sack, s.closing_hess, s.closing_sack):
                _validate_qty(v)

        _validate_id_set(
            db, verify_gl_row_ids_query(), int(body.co_id),
            [f_.gl_row_id for f_ in body.figures], BAD_ROW_IDS_MSG,
        )
        _validate_id_set(
            db, verify_gl_dept_ids_query(), int(body.co_id),
            [s.gl_dept_id for s in body.process_stock], BAD_DEPT_IDS_MSG,
        )

        user_id = token_data.get("user_id")
        hdr = _get_hdr(db, int(body.co_id), int(body.branch_id), month)
        if hdr and int(hdr.is_locked or 0) == 1:
            raise HTTPException(status_code=400, detail=LOCKED_MSG)

        if hdr:
            gain_loss_id = hdr.gain_loss_id
            db.execute(
                update_hdr_query(),
                {
                    "gain_loss_id": gain_loss_id,
                    "co_id": int(body.co_id),
                    "remarks": remarks,
                    "updated_by": user_id,
                },
            )
        else:
            result = db.execute(
                insert_hdr_query(),
                {
                    "co_id": int(body.co_id),
                    "branch_id": int(body.branch_id),
                    "month_date": month,
                    "remarks": remarks,
                    "updated_by": user_id,
                },
            )
            gain_loss_id = result.lastrowid

        wipe = {"gain_loss_id": gain_loss_id, "updated_by": user_id}
        db.execute(soft_delete_dtl_query(), wipe)
        db.execute(soft_delete_prod_query(), wipe)
        db.execute(soft_delete_ps_query(), wipe)

        for f_ in body.figures:
            # all-zero rows are simply not stored; absence reads as zero
            if f_.hess_qty == 0 and f_.sack_qty == 0:
                continue
            db.execute(
                insert_dtl_query(),
                {
                    "gain_loss_id": gain_loss_id,
                    "gl_row_id": int(f_.gl_row_id),
                    "hess_qty": float(f_.hess_qty),
                    "sack_qty": float(f_.sack_qty),
                    "updated_by": user_id,
                },
            )
        for order, p in enumerate(body.productions):
            db.execute(
                insert_prod_query(),
                {
                    "gain_loss_id": gain_loss_id,
                    "statement": p.statement,
                    "quality_type": int(p.quality_type),
                    "quality_name": p.quality_name.strip(),
                    "qty": float(p.qty),
                    "display_order": p.display_order or order,
                    "updated_by": user_id,
                },
            )
        for s in body.process_stock:
            db.execute(
                insert_ps_query(),
                {
                    "gain_loss_id": gain_loss_id,
                    "gl_dept_id": int(s.gl_dept_id),
                    "opening_hess": float(s.opening_hess),
                    "opening_sack": float(s.opening_sack),
                    "closing_hess": float(s.closing_hess),
                    "closing_sack": float(s.closing_sack),
                    "updated_by": user_id,
                },
            )

        db.commit()
        return {"data": {"gain_loss_id": gain_loss_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Lock / Unlock
# =============================================================================


def _set_lock(db: Session, body: GlMonthRef, token_data: dict, locked: bool) -> dict:
    month = parse_month(body.month)
    hdr = _get_hdr(db, int(body.co_id), int(body.branch_id), month)
    if not hdr:
        raise HTTPException(status_code=404, detail=SHEET_NOT_FOUND_MSG)
    db.execute(
        set_hdr_lock_query(),
        {
            "gain_loss_id": hdr.gain_loss_id,
            "co_id": int(body.co_id),
            "is_locked": 1 if locked else 0,
            "user_id": token_data.get("user_id"),
        },
    )
    db.commit()
    return {"data": {"gain_loss_id": hdr.gain_loss_id, "is_locked": locked}}


@router.post("/lock")
def lock(
    body: GlMonthRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Finalize the month. An explicit action, never a report side effect."""
    try:
        return _set_lock(db, body, token_data, True)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unlock")
def unlock(
    body: GlMonthRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reopen a locked month for correction — the mill's 'revised' sheets
    become an unlock + edit + re-lock instead of a duplicate sheet."""
    try:
        return _set_lock(db, body, token_data, False)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Report
# =============================================================================


@router.get("/report")
def report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """All three statements for a month.

    To-Date spans the financial year (April..month) by summing saved sheets;
    months with no saved sheet are listed in ``missing_months`` instead of
    silently reading as zero.
    """
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    month = _require_month_param(request)
    try:
        _validate_branch(db, co_id, branch_id)
        hdr = _get_hdr(db, co_id, branch_id, month)
        if not hdr:
            raise HTTPException(status_code=404, detail=SHEET_NOT_FOUND_MSG)

        rows_master = [
            dict(r._mapping)
            for r in db.execute(get_gl_rows_query(), {"co_id": co_id}).fetchall()
        ]
        depts_master = [
            dict(r._mapping)
            for r in db.execute(get_gl_depts_query(), {"co_id": co_id}).fetchall()
        ]

        fy_start = fy_start_of(month)
        prev = prev_month_of(month)
        # one range read covers current month, To-Date and (except when the
        # month is April) the previous month
        range_from = min(fy_start, prev)
        range_binds = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": range_from,
            "date_to": month,
        }
        hdrs = [
            dict(r._mapping)
            for r in db.execute(get_hdrs_in_range_query(), range_binds).fetchall()
        ]
        dtl_rows = [
            dict(r._mapping)
            for r in db.execute(get_dtl_in_range_query(), range_binds).fetchall()
        ]
        prod_rows = [
            dict(r._mapping)
            for r in db.execute(get_prod_in_range_query(), range_binds).fetchall()
        ]
        ps_rows = [
            dict(r._mapping)
            for r in db.execute(get_ps_in_range_query(), range_binds).fetchall()
        ]

        def of_month(rows: List[dict], m: date) -> List[dict]:
            return [r for r in rows if r["month_date"] == m]

        def td_rows(rows: List[dict]) -> List[dict]:
            return [r for r in rows if fy_start <= r["month_date"] <= month]

        saved_months = {h["month_date"] for h in hdrs if h["month_date"] >= fy_start}
        fy_first_month = min(saved_months) if saved_months else month

        cur_figures = figures_map(of_month(dtl_rows, month))
        prev_figures = figures_map(of_month(dtl_rows, prev))
        td_figures = figures_map(td_rows(dtl_rows))
        cur_prod = of_month(prod_rows, month)
        prev_prod = of_month(prod_rows, prev)
        td_prod = prod_totals_map(td_rows(prod_rows))
        cur_ps = of_month(ps_rows, month)
        prev_ps = of_month(ps_rows, prev)
        fy_first_ps = of_month(ps_rows, fy_first_month)

        statements = {
            code: build_statement(
                code, rows_master, depts_master,
                cur_figures, cur_prod, cur_ps,
                prev_figures, prev_prod, prev_ps,
                td_figures, td_prod, fy_first_ps,
            )
            for code in GL_STATEMENTS
        }

        expected, cursor = [], fy_start
        while cursor <= month:
            expected.append(cursor)
            cursor = (
                date(cursor.year + 1, 1, 1)
                if cursor.month == 12
                else date(cursor.year, cursor.month + 1, 1)
            )
        missing = [str(m) for m in expected if m not in saved_months]

        prev_hdr = next((h for h in hdrs if h["month_date"] == prev), None)

        return {
            "data": {
                "month": str(month),
                "prev_month": str(prev),
                "prev_month_exists": prev_hdr is not None,
                "fy_start": str(fy_start),
                "missing_months": missing,
                "remarks": hdr.remarks,
                "prev_remarks": prev_hdr["remarks"] if prev_hdr else None,
                "is_locked": int(hdr.is_locked or 0),
                "statements": statements,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
