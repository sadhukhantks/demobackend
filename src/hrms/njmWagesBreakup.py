"""All Wages Break Up Summary (legacy hol_get = 11).

Ports `Njmwagesprocess::njmwgsbrksummexceldownload` + the nine
`*wgsbrksummary` model functions from the Data-Portal-Php app.

Legacy had nine near-identical SQL functions that differed only in which pay
component feeds which output column and which pay schemes they covered. That
difference is the entirety of the variation, so here it is data (PIVOTS +
SHEETS) over one query builder.

The workbook keeps legacy's shape: the operator uploads the maintained
template, we refill the nine generated sheets and relink their totals into
`Final Sheet`. The hand-typed sheets (ALMA, Fng & Twisting Contractor,
Sweeper, PENDING) are never touched, which is the only reason the upload
exists — everything else in the file is regenerated.
"""

import io
import logging
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms.njmWagesData import MAX_UPLOAD_BYTES, _validate_period

logger = logging.getLogger(__name__)

router = APIRouter()

XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# The 18 money columns, in the order they sit on every sheet (C..T) and on
# Final Sheet (B..S). Sheet column C == index 0 here.
BREAKUP_COLUMNS = (
    "gross_earn", "other_earn", "pf", "esi", "p_tax", "gwf", "rent",
    "land_rent", "advance", "rsd", "canteen", "other_ded", "roff", "net",
    "death", "net_pay", "employer_pf", "employer_esi",
)

# Headers legacy's template carries on row 1 of every generated sheet. Written
# back so a fresh-but-empty template still reads correctly; the operator's own
# header row is overwritten with the identical text.
SHEET_HEADERS = (
    "EB.No.", "Employee Name", "Gross Earings", "Other Earnings", "PF", "ESI",
    "PTAX", "GWF", "Rent", "Land Rent", "Advance", "RSD", "Canteen",
    "Other deduction", "Roff", "Net Payment", "Death Fund", "Net Payable",
    "Employer PF", "Employer Esi", "Bank Account No.", "IFSC code",
    "PayScheme", "Dept",
)

FIRST_DATA_ROW = 2
# C..T — the money block. Column 1 is A.
FIRST_MONEY_COL = 3


def _c(component_id: int) -> str:
    """One pay component's amount for the employee, as legacy pivoted it."""
    return f"MAX(CASE WHEN k.COMPONENT_ID = {int(component_id)} THEN k.AMOUNT ELSE 0 END)"


# Employer shares are derived from the employee deduction, never stored:
# PF at 11/10 of the employee's 10%, ESI at 3.25/0.75 of the employee's 0.75%.
_EMPLOYER_PF = (
    "MAX(CASE WHEN k.COMPONENT_ID = 18 THEN ROUND(k.AMOUNT / 10 * 11, 0) ELSE 0 END)"
)
_EMPLOYER_ESI = (
    "MAX(CASE WHEN k.COMPONENT_ID = 19 THEN ROUND(k.AMOUNT / .75 * 3.25, 0) ELSE 0 END)"
)

# component_id -> output column per scheme family, taken from the legacy pivots
# verbatim. A column absent from a family is a literal 0 there, exactly as
# legacy wrote `0 gwf`.
#
# "A" covers main / retired / new budli / new budli rtd: legacy's four copies of
# this pivot were byte-identical apart from the scheme id.
PIVOTS: dict[str, dict[str, str]] = {
    "A": {
        "gross_earn": _c(349), "other_earn": "0", "pf": _c(18), "esi": _c(19),
        "p_tax": _c(16), "gwf": "0", "rent": _c(343), "land_rent": _c(345),
        "advance": f"{_c(166)} + {_c(292)}", "rsd": _c(354), "canteen": _c(291),
        "other_ded": "0", "roff": "0", "net": _c(21), "death": _c(351),
        "net_pay": _c(357),
        "employer_pf": _EMPLOYER_PF, "employer_esi": _EMPLOYER_ESI,
    },
    # Clerical staff & office boys/drivers. Net is the 100% net plus the IFTU
    # deduction added back, and gross comes off gross1 rather than gross2.
    "B": {
        "gross_earn": _c(350), "other_earn": _c(286), "pf": _c(18), "esi": _c(19),
        "p_tax": _c(16), "gwf": "0", "rent": _c(343), "land_rent": _c(320),
        "advance": _c(166), "rsd": _c(354), "canteen": "0",
        "other_ded": "0", "roff": "0", "net": f"{_c(252)} + {_c(351)}",
        "death": _c(351), "net_pay": _c(252),
        "employer_pf": _EMPLOYER_PF, "employer_esi": _EMPLOYER_ESI,
    },
    # Blueforce security. No employer PF: they are not on the PF roll.
    "C": {
        "gross_earn": _c(20), "other_earn": _c(62), "pf": _c(18), "esi": _c(19),
        "p_tax": _c(16), "gwf": "0", "rent": "0", "land_rent": "0",
        "advance": _c(54), "rsd": "0", "canteen": "0", "other_ded": "0",
        "roff": "0", "net": _c(21), "death": "0", "net_pay": _c(21),
        "employer_pf": "0", "employer_esi": _EMPLOYER_ESI,
    },
    # Staffs. Gross is shown net of OT because OT is reported beside it.
    "D": {
        "gross_earn": f"{_c(20)} - {_c(62)}", "other_earn": _c(62),
        "pf": _c(18), "esi": _c(19), "p_tax": _c(16), "gwf": "0",
        "rent": "0", "land_rent": "0", "advance": _c(54), "rsd": "0",
        "canteen": "0", "other_ded": "0", "roff": "0", "net": _c(21),
        "death": "0", "net_pay": _c(21),
        "employer_pf": _EMPLOYER_PF, "employer_esi": _EMPLOYER_ESI,
    },
    # Contractors. Canteen is recovered out of the advance, so it is subtracted
    # there and shown in its own column.
    "E": {
        "gross_earn": _c(66), "other_earn": f"{_c(268)} + {_c(76)}",
        "pf": _c(18), "esi": _c(19), "p_tax": _c(16), "gwf": "0",
        "rent": "0", "land_rent": "0",
        "advance": f"{_c(166)} - {_c(291)}", "rsd": "0", "canteen": _c(291),
        "other_ded": "0", "roff": "0", "net": _c(21), "death": "0",
        "net_pay": _c(21),
        "employer_pf": "0", "employer_esi": _EMPLOYER_ESI,
    },
}


class SheetSpec(NamedTuple):
    """One generated sheet: where it writes and what feeds it."""

    sheet: str
    #: Final Sheet row its totals link into.
    final_row: int
    #: PIVOTS key, or None for Cash Hand Payments (not a payroll scheme).
    pivot: str | None
    scheme_ids: tuple[int, ...]
    #: Legacy dropped zero-gross rows from the four "A" sheets only.
    drop_zero_gross: bool = False


# Legacy hardcoded its scheme ids; so do we, because these are one mill's
# schemes and nothing in the schema groups them. They are the NJM (branch 87)
# mirrors of the legacy ids the PHP listed: 164, 167, 169, 174, 166,
# (1, 90, 24, 23, 21), (94, 96) and 163. Every query also filters
# pay_scheme_master.branch_id, so a caller on another branch gets nothing
# rather than NJM's payroll.
SHEETS: tuple[SheetSpec, ...] = (
    SheetSpec("Main Payroll Wages", 4, "A", (2015,), drop_zero_gross=True),
    SheetSpec("Retired", 5, "A", (3167,), drop_zero_gross=True),
    SheetSpec("New Budli", 6, "A", (2017,), drop_zero_gross=True),
    SheetSpec("New Budli Rtd", 7, "A", (3174,), drop_zero_gross=True),
    SheetSpec("Clerical Staff & Office Boys,Dr", 8, "B", (3166,)),
    SheetSpec("Staffs", 9, "D", (3001, 3090, 3024, 3023, 3021)),
    SheetSpec("Blueforce", 10, "C", (3094, 3096)),
    SheetSpec("Contractors", 11, "E", (3163,)),
    # Cash hands are paid off attendance, never through a pay scheme.
    SheetSpec("Cash Hand Payments", 13, None, ()),
)

#: Final Sheet rows the operator maintains by hand — listed so callers can say
#: so, never written to.
MANUAL_SHEETS = ("Fng & Twisting Contractor", "ALMA", "Sweeper", "PENDING")

FINAL_SHEET = "Final Sheet"
#: Row 2 of Final Sheet, rewritten with the period each run (legacy did this).
FINAL_TITLE_CELL = "A2"


def _category_sql(spec: SheetSpec) -> str:
    """Per-employee breakup for one sheet's schemes.

    `spec.pivot` selects a hardcoded expression set; nothing from the request
    reaches the SQL text.
    """
    pivot = PIVOTS[spec.pivot]
    inner = ",\n               ".join(
        f"{pivot[col]} AS {col}" for col in BREAKUP_COLUMNS
    )
    having = "WHERE g.gross_earn > 0" if spec.drop_zero_gross else ""
    return f"""
        SELECT * FROM (
            SELECT od.emp_code AS ticket_no,
                   CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ',
                          TRIM(IFNULL(pd.middle_name, '')), ' ',
                          TRIM(IFNULL(pd.last_name, ''))) AS emp_name,
                   p.PAYSCHEME_ID AS payscheme_id,
                   psm.payscheme_name,
                   od.sub_dept_id,
                   bd.bank_acc_no, bd.ifsc_code,
                   {inner}
            FROM pay_employee_payroll k
            JOIN pay_period p ON p.ID = k.PAYPERIOD_ID
                             AND (p.STATUS IS NULL OR p.STATUS NOT IN (4))
            JOIN pay_scheme_master psm ON psm.payscheme_id = p.PAYSCHEME_ID
                                      AND psm.branch_id = :branch_id
            LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = k.EMPLOYEEID
            LEFT JOIN hrms_ed_official_details od
                   ON od.eb_id = k.EMPLOYEEID AND od.active = 1
            LEFT JOIN hrms_ed_bank_details bd
                   ON bd.eb_id = k.EMPLOYEEID AND bd.active = 1
            WHERE k.STATUS = 1
              AND p.FROM_DATE = :date_from AND p.TO_DATE = :date_to
              AND p.PAYSCHEME_ID IN :scheme_ids
            GROUP BY k.EMPLOYEEID, od.emp_code, emp_name, p.PAYSCHEME_ID,
                     psm.payscheme_name, od.sub_dept_id,
                     bd.bank_acc_no, bd.ifsc_code
        ) g
        {having}
        ORDER BY g.payscheme_id, g.ticket_no
    """


# Cash hands never go through payroll: the daily process writes each payment to
# daily_cash_outsider_payment, and the period's payments per employee are the
# whole of their breakup — gross, net payment and net payable are one number and
# there are no deductions.
_CASH_ROWS = text("""
    SELECT od.emp_code AS ticket_no,
           CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ',
                  TRIM(IFNULL(pd.middle_name, '')), ' ',
                  TRIM(IFNULL(pd.last_name, ''))) AS emp_name,
           od.sub_dept_id,
           bd.bank_acc_no, bd.ifsc_code,
           ROUND(SUM(cp.amount), 2) AS amount
    FROM daily_cash_outsider_payment cp
    LEFT JOIN hrms_ed_official_details od
           ON od.eb_id = cp.eb_id AND od.active = 1
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = cp.eb_id
    LEFT JOIN hrms_ed_bank_details bd ON bd.eb_id = cp.eb_id AND bd.active = 1
    WHERE COALESCE(cp.is_active, 1) = 1
      AND cp.branch_id = :branch_id
      AND cp.pay_date BETWEEN :date_from AND :date_to
    GROUP BY cp.eb_id, od.emp_code, emp_name, od.sub_dept_id,
             bd.bank_acc_no, bd.ifsc_code
    HAVING SUM(cp.amount) <> 0
    ORDER BY od.emp_code
""")


def _f(value) -> float:
    """Decimal/None out of MySQL -> a float Excel and JSON both accept."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _cash_rows(db: Session, branch_id: int, date_from: str, date_to: str) -> list[dict]:
    rows = []
    for r in db.execute(_CASH_ROWS, {
        "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
    }).fetchall():
        m = dict(r._mapping)
        amount = _f(m.pop("amount"))
        rows.append({
            **m,
            "payscheme_id": None, "payscheme_name": "Cash Hand Payments",
            **{col: 0.0 for col in BREAKUP_COLUMNS},
            # The payment is simultaneously the gross, the net and the payable.
            "gross_earn": amount, "net": amount, "net_pay": amount,
        })
    return rows


def sheet_rows(db: Session, spec: SheetSpec, branch_id: int,
               date_from: str, date_to: str) -> list[dict]:
    """Every employee row for one sheet, money columns as floats."""
    if spec.pivot is None:
        return _cash_rows(db, branch_id, date_from, date_to)
    stmt = text(_category_sql(spec)).bindparams(
        bindparam("scheme_ids", expanding=True)
    )
    rows = []
    for r in db.execute(stmt, {
        "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
        "scheme_ids": list(spec.scheme_ids),
    }).fetchall():
        m = dict(r._mapping)
        rows.append({**m, **{col: _f(m.get(col)) for col in BREAKUP_COLUMNS}})
    return rows


def _totals(rows: list[dict]) -> dict[str, float]:
    return {col: round(sum(r[col] for r in rows), 2) for col in BREAKUP_COLUMNS}


# ─── Workbook ───────────────────────────────────────────────────────

def _col_letter(idx: int) -> str:
    """1 -> A. Only ever called with the fixed column numbers above."""
    from openpyxl.utils import get_column_letter
    return get_column_letter(idx)


def _write_sheet(ws, rows: list[dict]) -> int:
    """Refill one generated sheet and return the row its SUM row landed on.

    Clears every existing data row first: the sheet is regenerated, never
    merged into, so a shorter month must not leave last month's tail behind.
    """
    if ws.max_row >= FIRST_DATA_ROW:
        ws.delete_rows(FIRST_DATA_ROW, ws.max_row - FIRST_DATA_ROW + 1)

    for col, header in enumerate(SHEET_HEADERS, start=1):
        ws.cell(row=1, column=col, value=header)

    for i, r in enumerate(rows):
        row = FIRST_DATA_ROW + i
        # EB no and bank account are identifiers, not quantities: leading zeros
        # and 15-digit account numbers both survive only as text.
        ws.cell(row=row, column=1, value=str(r.get("ticket_no") or ""))
        ws.cell(row=row, column=2, value=r.get("emp_name") or "")
        for j, col in enumerate(BREAKUP_COLUMNS):
            ws.cell(row=row, column=FIRST_MONEY_COL + j, value=r[col])
        ws.cell(row=row, column=21, value=str(r.get("bank_acc_no") or ""))
        ws.cell(row=row, column=22, value=r.get("ifsc_code") or "")
        ws.cell(row=row, column=23, value=r.get("payscheme_id"))
        ws.cell(row=row, column=24, value=r.get("sub_dept_id"))

    total_row = FIRST_DATA_ROW + len(rows)
    for j in range(len(BREAKUP_COLUMNS)):
        col = FIRST_MONEY_COL + j
        letter = _col_letter(col)
        # An empty sheet has no range to sum; a literal 0 keeps Final Sheet
        # arithmetic working where =SUM(C2:C1) would not.
        ws.cell(row=total_row, column=col, value=(
            f"=SUM({letter}{FIRST_DATA_ROW}:{letter}{total_row - 1})"
            if rows else 0
        ))
    return total_row


def _link_final(final_ws, spec: SheetSpec, total_row: int) -> None:
    """Point Final Sheet's row for this category at the sheet's SUM row.

    Final Sheet column B holds what the data sheets hold in C, so the money
    block is written one column to the left.
    """
    for j in range(len(BREAKUP_COLUMNS)):
        src = _col_letter(FIRST_MONEY_COL + j)
        final_ws.cell(
            row=spec.final_row,
            column=FIRST_MONEY_COL - 1 + j,
            value=f"='{spec.sheet}'!{src}{total_row}",
        )


def _build_workbook(raw: bytes, db: Session, branch_id: int,
                    date_from: str, date_to: str) -> tuple[bytes, dict]:
    from openpyxl import load_workbook

    try:
        # Not read_only (we write) and not data_only (the template's own
        # formulas, including the hand-maintained sheets', must survive).
        wb = load_workbook(io.BytesIO(raw))
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read the workbook")

    required = [FINAL_SHEET] + [s.sheet for s in SHEETS]
    missing = [name for name in required if name not in wb.sheetnames]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=("This is not the Wages Break Up template — it has no sheet "
                    "named: " + ", ".join(missing)),
        )

    final_ws = wb[FINAL_SHEET]
    period = datetime.strptime(date_from, "%Y-%m-%d").strftime("%b %Y")
    final_ws[FINAL_TITLE_CELL] = (
        f"ALL MANPOWER PAYMENT SHEET FOR THE MONTH OF {period}"
    )

    summary = {}
    for spec in SHEETS:
        rows = sheet_rows(db, spec, branch_id, date_from, date_to)
        total_row = _write_sheet(wb[spec.sheet], rows)
        _link_final(final_ws, spec, total_row)
        summary[spec.sheet] = {"rows": len(rows), **_totals(rows)}

    out = io.BytesIO()
    wb.save(out)
    wb.close()
    return out.getvalue(), summary


# ─── Endpoints ──────────────────────────────────────────────────────

@router.get("/njm_wages_breakup_summary")
def njm_wages_breakup_summary(
    request: Request,
    branch_id: int,
    date_from: str,
    date_to: str,
    pay_scheme_id: int | None = None,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Per-employee breakup rows.

    With `pay_scheme_id` it answers for that scheme alone, using whichever
    pivot the scheme's sheet uses. Without one it returns every generated
    sheet's rows, each tagged with `sheet`.
    """
    try:
        _validate_period(date_from, date_to)
        specs = SHEETS
        if pay_scheme_id:
            specs = tuple(s for s in SHEETS if pay_scheme_id in s.scheme_ids)
            if not specs:
                raise HTTPException(
                    status_code=400,
                    detail=(f"Pay scheme {pay_scheme_id} is not one of the wages "
                            "break up schemes"),
                )
            # One scheme of a multi-scheme sheet must not drag in its siblings.
            specs = (specs[0]._replace(scheme_ids=(pay_scheme_id,)),)

        data = []
        for spec in specs:
            for r in sheet_rows(db, spec, branch_id, date_from, date_to):
                data.append({"id": len(data), "sheet": spec.sheet, **r})
        return {"data": data, "total": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_wages_breakup_summary failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/njm_wages_breakup_totals")
def njm_wages_breakup_totals(
    request: Request,
    branch_id: int,
    date_from: str,
    date_to: str,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Final Sheet as rows: one per generated category plus a grand total.

    The hand-maintained sheets are not included — nothing here knows what the
    operator has typed into them — so this total is the generated part only.
    """
    try:
        _validate_period(date_from, date_to)
        data, grand = [], {col: 0.0 for col in BREAKUP_COLUMNS}
        for spec in SHEETS:
            rows = sheet_rows(db, spec, branch_id, date_from, date_to)
            totals = _totals(rows)
            for col in BREAKUP_COLUMNS:
                grand[col] += totals[col]
            data.append({
                "id": len(data), "payment_for": spec.sheet,
                "employees": len(rows), **totals,
            })
        data.append({
            "id": len(data), "payment_for": "Grand Total",
            "employees": sum(r["employees"] for r in data),
            **{col: round(grand[col], 2) for col in BREAKUP_COLUMNS},
        })
        return {"data": data, "total": len(data), "manual_sheets": list(MANUAL_SHEETS)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_wages_breakup_totals failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/njm_wages_breakup_workbook")
def njm_wages_breakup_workbook(
    request: Request,
    file: UploadFile = File(...),
    branch_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The Wages Break Up Summary workbook.

    Takes the maintained template, refills the nine generated sheets from this
    period's payroll, and relinks their totals into Final Sheet. ALMA,
    Fng & Twisting Contractor, Sweeper and PENDING come back exactly as they
    were uploaded — they are typed by hand and this run knows nothing about
    them.
    """
    try:
        _validate_period(date_from, date_to)
        name = (file.filename or "").lower()
        if not name.endswith(".xlsx"):
            raise HTTPException(status_code=400,
                                detail="Upload the template as an .xlsx file")
        raw = file.file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="The uploaded file is empty")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="File exceeds the 2 MB limit")

        xlsx, summary = _build_workbook(raw, db, branch_id, date_from, date_to)
        logger.info("njm_wages_breakup_workbook %s..%s branch %s: %s",
                    date_from, date_to, branch_id,
                    {k: v["rows"] for k, v in summary.items()})

        period = datetime.strptime(date_from, "%Y-%m-%d").strftime("%b %Y")
        filename = f"Wages_break_up_Summary {period}.xlsx"
        return StreamingResponse(
            iter([xlsx]),
            media_type=XLSX_MEDIA_TYPE,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_wages_breakup_workbook failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
