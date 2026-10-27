"""NJM wages data preparation — the five "Updates" actions on the NJM Wages
Process screen.

Ports the Cad/Vard/GWF update and the four wage-period file uploads from the
legacy Data-Portal-Php app (`controllers/Njmwagesprocess.php`,
`models/Njmallwagesprocess.php`), which wrote to EMPMILL12. Everything lands in
the same three tables, created by
`dbqueries/migrations/njm_wages_data_tables.sql`:

    tbl_njm_wages_data_collection      Cad/Vard/GWF ('V'), FA ('FA'), Other ('OTH')
    tbl_njm_cnt_wages_data_collection  contractor sheet
    tbl_run_loom_line_hours            line hours sheet

Rows are keyed by (date_from, date_to, eb_id, payscheme_id) with no branch
column — same convention as the sibling tbl_ejm_wages_data_collection. branch_id
only scopes the SOURCE rows (attendance, leave, holidays, the employee lookup).

Every action rebuilds its own arm: deactivate that update_for for the period and
pay scheme, then insert. Other arms in the same period are never touched.
"""
import csv
import io
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.hrms.canteenDetails import STATUS_APPROVED as CANTEEN_APPROVED
from src.hrms.canteenDetails import STATUS_CANCELLED as CANTEEN_CANCELLED
CANTEEN_REJECTED = 4
from src.hrms.prodWagesQualLink import QUALITY_TYPES
from src.hrms.query import get_company_name, get_pay_roll_custom_components

logger = logging.getLogger(__name__)

router = APIRouter()

# Legacy hardcoded every one of these.
# Categories are matched by NAME, not by the ids legacy hardcoded. Every company
# in this tenant has its own copy of the same category list under different ids
# -- CONTRACT is 14 on Empire but 84 on Limelight, PERMENANT 15 vs 85, and so on.
# Hardcoding the ids silently returns zero rows for every company but the first.
CONTRACT_CATEGORY_NAMES = ("CONTRACT",)
EL_LEAVE_TYPE = 2           # hrms_leave_types_mst 'L'
LEAVE_APPROVED = 3
FULL_DAY_HOURS = 8          # leave and holidays credit a flat 8 hours
CONTRACTOR_UPDT_FROM = "M"  # the contractor arm's marker in updt_from

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # legacy $config['max_size'] = 2048 (KB)


# ─── sheet helpers ──────────────────────────────────────────────────

def _read_sheet(upload: UploadFile) -> list[list]:
    """Read an uploaded .xlsx/.csv into a list of rows (row 0 = header).

    openpyxl only speaks xlsx, so csv goes through the stdlib reader — legacy
    accepted both and switched on the extension the same way.
    """
    name = (upload.filename or "").lower()
    if not name.endswith((".xlsx", ".csv")):
        raise HTTPException(status_code=400, detail="Upload an .xlsx or .csv file")

    raw = upload.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds the 2 MB limit")

    if name.endswith(".csv"):
        decoded = raw.decode("utf-8-sig", errors="replace")
        return [list(r) for r in csv.reader(io.StringIO(decoded))]

    from openpyxl import load_workbook
    try:
        # read_only + data_only: we want computed values, not formulas, and the
        # sheets are throwaway inputs, never edited in place.
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read the workbook")
    try:
        return [list(r) for r in wb.active.iter_rows(values_only=True)]
    finally:
        wb.close()


def _rows_by_header(sheet: list[list], key_column: str) -> list[dict]:
    """Header-keyed rows, dropping any whose key cell is blank.

    The FA and Other/Adjs sheets are addressed by column NAME (legacy fed them
    through JSON_TABLE on the header row), unlike the contractor and line-hours
    sheets which are addressed by position.
    """
    if len(sheet) < 2:
        raise HTTPException(status_code=400, detail="The sheet has no data rows")
    header = [str(c).strip() if c is not None else "" for c in sheet[0]]
    if key_column not in header:
        found = ", ".join(h for h in header if h)
        raise HTTPException(
            status_code=400,
            detail=f"The sheet needs a '{key_column}' column. Found: {found}",
        )
    out = []
    for raw in sheet[1:]:
        row = {h: (raw[i] if i < len(raw) else None) for i, h in enumerate(header) if h}
        if str(row.get(key_column) or "").strip():
            out.append(row)
    if not out:
        raise HTTPException(status_code=400, detail=f"No rows carried a {key_column}")
    return out


def _num(value, default=0):
    """Sheet cell -> number. Blanks, text and junk all fall back to `default`,
    which is what legacy's `!empty($x) ? $x : 0` did for every numeric cell."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_date(value):
    """Sheet cell -> date. xlsx gives a datetime, csv gives a string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except (TypeError, ValueError):
            continue
    return None


# ─── request helpers ────────────────────────────────────────────────

# A native <input type="date"> fires on every keystroke, so typing a year emits
# valid-but-absurd partials ('0002', '0020', '0202', '0006') on the way to
# '2026'. One of those reaching a wage period is silently catastrophic: the ATT
# arm then scans from year 6 and collects a worker's entire history as one
# month. No NJM wage period predates the ERP, so refuse the whole class here —
# every wages endpoint routes through this function.
_MIN_WAGE_YEAR = 2000


def _validate_period(date_from: str, date_to: str) -> None:
    parsed = {}
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        parsed[label] = _as_date(value)
        if not parsed[label]:
            raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD")
        if not _MIN_WAGE_YEAR <= parsed[label].year <= date.today().year + 1:
            raise HTTPException(
                status_code=400,
                detail=f"{label} year {parsed[label].year} is out of range",
            )
    if parsed["date_to"] < parsed["date_from"]:
        raise HTTPException(
            status_code=400, detail="date_to cannot be earlier than date_from"
        )


def _emp_code_map(db, branch_id, pay_scheme_id=None, category_names=None):
    """emp_code -> eb_id for one branch, optionally narrowed to a pay scheme or
    a set of category NAMES. One query instead of legacy's lookup-per-row.

    Names rather than ids: see CONTRACT_CATEGORY_NAMES — each company numbers
    the same categories differently.
    """
    scheme_join = ""
    if pay_scheme_id is not None:
        scheme_join = (
            "JOIN pay_employee_payscheme t ON t.EMPLOYEEID = od.eb_id "
            "AND t.STATUS = 1 AND t.PAY_SCHEME_ID = :pay_scheme_id"
        )
    category_filter = ""
    if category_names:
        placeholders = ", ".join(f":cat_{i}" for i in range(len(category_names)))
        category_filter = (f"AND od.catagory_id IN (SELECT cata_id FROM category_mst "
                           f"WHERE cata_desc IN ({placeholders}))")
    sql = f"""
        SELECT TRIM(od.emp_code) AS emp_code, od.eb_id
        FROM hrms_ed_official_details od
        JOIN hrms_ed_personal_details pd ON pd.eb_id = od.eb_id AND pd.active = 1
        {scheme_join}
        WHERE od.active = 1 AND pd.branch_id = :branch_id
          {category_filter}
    """
    params = {"branch_id": branch_id}
    if pay_scheme_id is not None:
        params["pay_scheme_id"] = pay_scheme_id
    for i, name in enumerate(category_names or ()):
        params[f"cat_{i}"] = name
    return {
        r._mapping["emp_code"]: r._mapping["eb_id"]
        for r in db.execute(text(sql), params)
    }


def _resolve_or_fail(rows, key_column, codes):
    """Pair every sheet row with its eb_id, or reject the whole upload.

    All-or-nothing on purpose, and legacy did the same for FA and Other/Adjs:
    a half-loaded wage period is worse than a rejected one, because the missing
    people are invisible once the run starts.
    """
    missing, resolved = [], []
    for row in rows:
        code = str(row.get(key_column) or "").strip()
        eb_id = codes.get(code)
        if eb_id is None:
            missing.append(code)
        else:
            resolved.append((eb_id, row))
    if missing:
        uniq = sorted(set(missing))
        shown = ", ".join(uniq[:20])
        more = "" if len(uniq) <= 20 else f" (+{len(uniq) - 20} more)"
        raise HTTPException(
            status_code=400,
            detail=(f"No employee on this branch/pay scheme for: {shown}{more}. "
                    "Nothing was saved."),
        )
    return resolved

def _deactivate(db, update_for, params):
    """Retire the previous run of one arm so the insert below replaces it."""
    db.execute(text("""
        UPDATE tbl_njm_wages_data_collection SET is_active = 0
        WHERE date_from = :date_from AND date_to = :date_to
          AND payscheme_id = :pay_scheme_id
          AND update_for = :update_for AND is_active = 1
    """), dict(params, update_for=update_for))


# ─── 1. Cad/Vard/GWF ────────────────────────────────────────────────

_VARD_INSERT = text("""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, is_active, update_for, payscheme_id,
         cad_amount, iftu_amount, gwf_amount)
    SELECT k.eb_id, :date_from, :date_to, 1, 'V', :pay_scheme_id,
           :cad_ded, :vard_ded, :gwf_ded
    FROM (
        SELECT u.eb_id, SUM(u.whrs) AS whrs FROM (
            SELECT da.eb_id, (da.working_hours - COALESCE(da.idle_hours, 0)) AS whrs
            FROM daily_attendance da
            WHERE da.attendance_type IN ('R', 'O')
              AND da.is_active = 1 AND da.status_id = '3'
              AND da.branch_id = :branch_id
              AND da.attendance_date BETWEEN :date_from AND :date_to
            UNION ALL
            -- vw_leave_dates, not leave_tran_details: the view expands the
            -- transaction's from/to range itself and already filters
            -- status = 3, so a request whose detail rows were never written
            -- (or were written stale) still credits its days. DISTINCT
            -- because the view LEFT JOINs hrms_ed_official_details, which
            -- has duplicate eb_id rows — without it a day pays 8 hours twice.
            SELECT v.eb_id, :full_day_hours
            FROM (
                SELECT DISTINCT eb_id, leave_date
                FROM vw_leave_dates
                WHERE leave_type_id = :el_leave_type
                  AND branch_id = :branch_id
                  AND leave_date BETWEEN :date_from AND :date_to
            ) v
            UNION ALL
            SELECT h.eb_id, :full_day_hours
            FROM hrms_holiday_transactions h
            JOIN holiday_master hm ON hm.holiday_id = h.holiday_id
            WHERE h.is_active = 1
              AND hm.branch_id = :branch_id
              AND hm.holiday_date BETWEEN :date_from AND :date_to
        ) u
        GROUP BY u.eb_id
    ) k
    JOIN (SELECT DISTINCT EMPLOYEEID FROM pay_employee_payscheme
          WHERE STATUS = 1 AND PAY_SCHEME_ID = :pay_scheme_id) t
      ON t.EMPLOYEEID = k.eb_id
    WHERE k.whrs > 0
""")


@router.post("/njm_wrk_vard_update")
def njm_wrk_vard_update(
    request: Request,
    branch_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    pay_scheme_id: int = Form(...),
    cad_ded: float = Form(0),
    vard_ded: float = Form(0),
    gwf_ded: float = Form(0),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Cad/Vard/GWF deduction row per employee credited with any hours.

    Credited hours = worked hours net of idle, plus 8 for each approved 'L'
    leave day and each holiday in the period — legacy's three-way UNION.

    DIFFERS FROM LEGACY, deliberately: legacy's AJAX never sent the three
    amounts on screen and the SQL hardcoded iftu_amount = 60. Here the typed
    values are what gets written (cad -> cad_amount, vard -> iftu_amount,
    gwf -> gwf_amount), because a form whose inputs are ignored is a trap.
    Operators reproducing the old behaviour should type 60 into Vard Ded.
    """
    try:
        _validate_period(date_from, date_to)
        for label, value in (("cad_ded", cad_ded), ("vard_ded", vard_ded),
                             ("gwf_ded", gwf_ded)):
            if value < 0:
                raise HTTPException(
                    status_code=400, detail=f"{label} cannot be negative"
                )

        params = {"date_from": date_from, "date_to": date_to,
                  "pay_scheme_id": pay_scheme_id}
        _deactivate(db, "V", params)
        result = db.execute(_VARD_INSERT, dict(
            params,
            branch_id=branch_id,
            cad_ded=cad_ded, vard_ded=vard_ded, gwf_ded=gwf_ded,
            full_day_hours=FULL_DAY_HOURS,
            el_leave_type=EL_LEAVE_TYPE,
        ))
        db.commit()
        written = result.rowcount or 0
        return {"data": {
            "message": f"Cad/Vard/GWF updated for {written} employees",
            "rows": written,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"njm_wrk_vard_update failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── 2. Contractor upload ───────────────────────────────────────────

# Positional layout, taken from legacy's $sheetData[$i][N] indexes rather than
# its header comment — the two disagree, and the indexes are what ran.
CNT_COLUMNS = {
    "emp_code": 2, "wrk_hours_adj": 3, "rates": 4, "prod_id": 5, "production": 6,
    "canteen": 7, "advance": 8, "plus_amount": 9, "minus_amount": 10,
    "travel_allowance": 11, "updt_from": 12,
}

_CNT_INSERT = text("""
    INSERT INTO tbl_njm_cnt_wages_data_collection
        (start_date, end_date, eb_id, wrk_hours_reg, wrk_hours_ot, wrk_hours_adj,
         rates, prod_id, production, amount_reg, amount_ot, amount_adj,
         canteen, advance, plus_amount, minus_amount, travel_allowance,
         updt_from, reg_amount, prod_amount, esi_days, is_active)
    VALUES (:date_from, :date_to, :eb_id, 0, 0, :wrk_hours_adj,
            :rates, :prod_id, :production, 0, 0, 0,
            :canteen, :advance, :plus_amount, :minus_amount, :travel_allowance,
            :updt_from, 0, :prod_amount, 0, 1)
""")


@router.post("/njm_cnt_wages_upload")
def njm_cnt_wages_upload(
    request: Request,
    file: UploadFile = File(...),
    branch_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    pay_scheme_id: int = Form(...),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Contractor wages sheet -> tbl_njm_cnt_wages_data_collection.

    Columns by position: 2 EB No, 3 hours, 4 rate, 5 prod id, 6 production,
    7 canteen, 8 advance, 9 plus, 10 minus, 11 travel allowance, 12 source.
    prod_amount is derived as production x rate, as legacy did.

    Only employees in the CONTRACT category (14) are eligible, matching legacy.
    """
    try:
        _validate_period(date_from, date_to)
        sheet = _read_sheet(file)
        if len(sheet) < 2:
            raise HTTPException(status_code=400, detail="The sheet has no data rows")

        code_ix = CNT_COLUMNS["emp_code"]
        rows = []
        for raw in sheet[1:]:
            if not str((raw[code_ix] if len(raw) > code_ix else None) or "").strip():
                continue
            rows.append({
                key: (raw[i] if len(raw) > i else None)
                for key, i in CNT_COLUMNS.items()
            })
        if not rows:
            raise HTTPException(status_code=400, detail="No rows carried an EB No")

        codes = _emp_code_map(db, branch_id, category_names=CONTRACT_CATEGORY_NAMES)
        resolved = _resolve_or_fail(rows, "emp_code", codes)

        db.execute(text("""
            UPDATE tbl_njm_cnt_wages_data_collection SET is_active = 0
            WHERE start_date = :date_from AND end_date = :date_to
              AND updt_from = :updt_from AND is_active = 1
        """), {"date_from": date_from, "date_to": date_to,
               "updt_from": CONTRACTOR_UPDT_FROM})

        payload = []
        for eb_id, row in resolved:
            production = _num(row["production"])
            rates = _num(row["rates"])
            payload.append({
                "date_from": date_from, "date_to": date_to, "eb_id": eb_id,
                "wrk_hours_adj": _num(row["wrk_hours_adj"]),
                "rates": rates,
                "prod_id": int(_num(row["prod_id"])) or None,
                "production": production,
                "canteen": _num(row["canteen"]),
                "advance": _num(row["advance"]),
                "plus_amount": _num(row["plus_amount"]),
                "minus_amount": _num(row["minus_amount"]),
                "travel_allowance": _num(row["travel_allowance"]),
                "updt_from": str(row["updt_from"] or CONTRACTOR_UPDT_FROM)[:1],
                "prod_amount": production * rates,
            })
        db.execute(_CNT_INSERT, payload)
        db.commit()
        return {"data": {
            "message": f"{len(payload)} contractor rows uploaded",
            "rows": len(payload),
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"njm_cnt_wages_upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── 3. Food allowance (FA) upload ──────────────────────────────────

@router.post("/njm_wrk_fa_upload")
def njm_wrk_fa_upload(
    request: Request,
    file: UploadFile = File(...),
    branch_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    pay_scheme_id: int = Form(...),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Food-allowance sheet (TICKET_NO, AMOUNT) -> other_pay on the 'FA' arm."""
    try:
        _validate_period(date_from, date_to)
        rows = _rows_by_header(_read_sheet(file), "TICKET_NO")
        codes = _emp_code_map(db, branch_id, pay_scheme_id=pay_scheme_id)
        resolved = _resolve_or_fail(rows, "TICKET_NO", codes)

        params = {"date_from": date_from, "date_to": date_to,
                  "pay_scheme_id": pay_scheme_id}
        _deactivate(db, "FA", params)
        payload = [
            dict(params, eb_id=eb_id, other_pay=_num(row.get("AMOUNT")))
            for eb_id, row in resolved
        ]
        db.execute(text("""
            INSERT INTO tbl_njm_wages_data_collection
                (eb_id, date_from, date_to, is_active, update_for,
                 payscheme_id, other_pay)
            VALUES (:eb_id, :date_from, :date_to, 1, 'FA',
                    :pay_scheme_id, :other_pay)
        """), payload)
        db.commit()
        total = sum(p["other_pay"] for p in payload)
        return {"data": {
            "message": f"{len(payload)} food-allowance rows uploaded, total {total:g}",
            "rows": len(payload),
            "total_amount": total,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"njm_wrk_fa_upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── 4. Other / Adjustments upload ──────────────────────────────────

# Sheet column -> table column, from legacy's JSON_TABLE paths paired with its
# INSERT list. C_pratesardhelp_ is parsed by legacy but never inserted, so it is
# absent here too.
OTH_COLUMNS = {
    "C_wrkhrs1_": "hours_wkd_1", "C_wrkhours2_": "hours_wkd_2",
    "C_esi_days_": "esi_days", "C_eldays_": "el_days",
    "C_LayOffHrs_": "lay_off_hrs", "C_PHours_": "piece_hours",
    "C_canteen_": "canteen", "C_sundayadv_": "sunday_adv",
    "C_otheradv1_": "other_adv", "C_otherpay_": "other_pay",
    "C_eladvance_": "el_advance", "C_instadvded_": "installment_advance",
    "C_extrahrstime_": "extra_hours_t", "C_extrahourspiece_": "extra_hours_p",
    "C_nightdays_": "c_shift_days", "C_FHours_": "festival_hours",
    "C_mveplus_": "arrear_plus", "C_mveminus_": "arrear_minus",
    "C_iltime_": "iltime_hrs", "C_ilpiece_": "ilpiece_hrs",
    "C_pwage_": "act_prod_amount", "C_pwagesinc_": "piece_wages_inc",
    "C_cldays_": "cl_days", "C_minusbalance_": "minus_bal",
    "C_exadvance_": "advance",
}
# Dropped with the SL/UL leave arms: C_sld_ -> sl_days and C_uldays_ ->
# ul_days. Only payable leave transfers now, as el_days, so nothing should
# write those two columns any more — a legacy sheet still carrying the
# headers has them ignored rather than inserted.

_OTH_INSERT = text(
    "INSERT INTO tbl_njm_wages_data_collection "
    "(eb_id, date_from, date_to, is_active, update_for, payscheme_id, "
    + ", ".join(OTH_COLUMNS.values())
    + ") VALUES (:eb_id, :date_from, :date_to, 1, 'OTH', :pay_scheme_id, "
    + ", ".join(":" + c for c in OTH_COLUMNS.values())
    + ")"
)


@router.post("/njm_wrk_oth_adjs_upload")
def njm_wrk_oth_adjs_upload(
    request: Request,
    file: UploadFile = File(...),
    branch_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    pay_scheme_id: int = Form(...),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Other/Adjustments sheet (EB_NO + 27 C_* columns) -> the 'OTH' arm.

    Missing C_* columns are written as 0 rather than rejected: the sheet is
    hand-assembled per period and rarely carries every column.
    """
    try:
        _validate_period(date_from, date_to)
        rows = _rows_by_header(_read_sheet(file), "EB_NO")
        codes = _emp_code_map(db, branch_id, pay_scheme_id=pay_scheme_id)
        resolved = _resolve_or_fail(rows, "EB_NO", codes)

        params = {"date_from": date_from, "date_to": date_to,
                  "pay_scheme_id": pay_scheme_id}
        _deactivate(db, "OTH", params)
        payload = []
        for eb_id, row in resolved:
            values = {col: _num(row.get(sheet_col))
                      for sheet_col, col in OTH_COLUMNS.items()}
            payload.append(dict(params, eb_id=eb_id, **values))
        db.execute(_OTH_INSERT, payload)
        db.commit()
        return {"data": {
            "message": f"{len(payload)} other/adjustment rows uploaded",
            "rows": len(payload),
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"njm_wrk_oth_adjs_upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── 5. Line hours upload ───────────────────────────────────────────

@router.post("/njm_line_hours_upload")
def njm_line_hours_upload(
    request: Request,
    file: UploadFile = File(...),
    branch_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    pay_scheme_id: int = Form(...),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Loom line-hours sheet -> tbl_run_loom_line_hours, keyed by
    (period, line_no, sard_helper, wgroup) so a re-upload corrects in place.

    Columns by position: 0 start date, 1 end date, 2 line no, 3 sardar/helper,
    4 running hours, 5 lost hours, 6 wage group, 7 no of looms.

    The sheet carries its own period and legacy rejected the whole file when the
    first data row disagreed with the screen — kept, because a line-hours sheet
    loaded against the wrong fortnight silently misprices every loom.
    """
    try:
        _validate_period(date_from, date_to)
        sheet = _read_sheet(file)
        if len(sheet) < 2:
            raise HTTPException(status_code=400, detail="The sheet has no data rows")

        first = sheet[1]
        sheet_from = _as_date(first[0] if len(first) > 0 else None)
        sheet_to = _as_date(first[1] if len(first) > 1 else None)
        if str(sheet_from) != date_from or str(sheet_to) != date_to:
            raise HTTPException(
                status_code=400,
                detail=(f"The sheet covers {sheet_from} to {sheet_to}, but the "
                        f"screen is set to {date_from} to {date_to}. "
                        "Nothing was saved."),
            )

        payload = []
        for raw in sheet[1:]:
            cell = lambda i: (raw[i] if len(raw) > i else None)  # noqa: E731
            line_no = str(cell(2) or "").strip()
            if not line_no:
                continue
            payload.append({
                "date_from": date_from, "date_to": date_to, "line_no": line_no,
                "sard_helper": str(cell(3) or "")[:1],
                "running_hours": _num(cell(4)),
                "lost_hours": _num(cell(5)),
                "wgroup": str(cell(6) or "").strip()[:10],
                "no_of_looms": int(_num(cell(7))),
            })
        if not payload:
            raise HTTPException(status_code=400,
                                detail="No rows carried a line number")

        updated = inserted = 0
        for row in payload:
            result = db.execute(text("""
                UPDATE tbl_run_loom_line_hours
                SET running_hours = :running_hours, lost_hours = :lost_hours,
                    no_of_looms = :no_of_looms
                WHERE line_no = :line_no AND date_from = :date_from
                  AND date_to = :date_to AND sard_helper = :sard_helper
                  AND wgroup = :wgroup
            """), row)
            if result.rowcount:
                updated += 1
            else:
                db.execute(text("""
                    INSERT INTO tbl_run_loom_line_hours
                        (date_from, date_to, line_no, sard_helper,
                         running_hours, lost_hours, wgroup, no_of_looms)
                    VALUES (:date_from, :date_to, :line_no, :sard_helper,
                            :running_hours, :lost_hours, :wgroup, :no_of_looms)
                """), row)
                inserted += 1
        db.commit()
        return {"data": {
            "message": f"Line hours saved — {inserted} added, {updated} updated",
            "rows": inserted + updated,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"njm_line_hours_upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Checklists and exports ─────────────────────────────────────────
#
# Read-only counterparts to the arms above. Each is a straight port of the
# matching Njmallwagesprocess model function, with the legacy table names
# swapped for the vowerp3 ones:
#   worker_master      -> hrms_ed_personal_details + hrms_ed_official_details
#   master_department  -> dept_mst        (rec_id and dept_id are the same ids)
#   category_master    -> category_mst
#   tbl_pay_scheme     -> pay_scheme_master (pay_scheme is empty in migrated
#                                            tenants; payscheme_id is the key)
# Legacy scoped by company_id; these scope by the sidebar branch instead.


def _rows(db, sql, params):
    """Run a report query and return plain dicts with a grid id."""
    out = []
    for i, row in enumerate(db.execute(text(sql), params)):
        m = dict(row._mapping)
        for k, v in m.items():
            if hasattr(v, "isoformat"):
                m[k] = v.isoformat()
            elif isinstance(v, Decimal):
                m[k] = float(v)
        out.append({"id": i, **m})
    return out


_LINE_HOURS_CHECKLIST = """
    SELECT line_no, sard_helper, running_hours, lost_hours, wgroup,
           no_of_looms, line_no_id,
           IFNULL(quantity, 0) AS quantity,
           IFNULL(pamount, 0) AS pamount
    FROM tbl_run_loom_line_hours
    WHERE date_from = :date_from AND date_to = :date_to
    ORDER BY sard_helper, CAST(line_no AS UNSIGNED), wgroup
"""


def line_hours_rate(pamount, running_hours, lost_hours, no_of_looms, sard_helper) -> float:
    """Per-loom-hour rate as legacy Njmwagesprocess::njmlinehrschecklist printed
    it: amount over half the net loom-hours, sardars at 1.3x. Rounded to 6
    places at each step, exactly as legacy did.

    Legacy also demanded lost_hours > 0 (a line with no lost hours prints
    rate 0); kept so the statement matches the old PDF row for row.
    """
    am, rh, lh, nl = (float(v or 0) for v in (pamount, running_hours, lost_hours, no_of_looms))
    if not (am > 0 and rh > 0 and lh > 0 and nl > 0):
        return 0.0
    rate = round(am / (((rh - lh) * nl) / 2), 6)
    return round(rate * 1.3, 6) if sard_helper == "S" else rate


def _line_hours_pdf(rows: list[dict], company: str, date_from: str, date_to: str) -> io.BytesIO:
    """LINE HOURS STATEMENT — the legacy FPDF layout: company, period line,
    then one ruled table with the header repeated on every page."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def dmy(iso: str) -> str:
        y, m, d = iso.split("-")
        return f"{d}-{m}-{y}"

    head = ParagraphStyle("LhHead", fontName="Helvetica-Bold", fontSize=8, leading=10)
    # Legacy echoed the columns raw: decimal(…,2) hours/amount as "151.01",
    # int looms/production as "30", and the PHP float rate as "6.65427" / "0".
    d2 = lambda v: f"{float(v or 0):.2f}"  # noqa: E731
    i0 = lambda v: str(int(v or 0))  # noqa: E731
    php = lambda v: (f"{v:.6f}".rstrip("0").rstrip(".") if v else "0")  # noqa: E731
    data = [["LINE NO/GROUP", "SARD HELP", "RUNNING HRS", "LOST HOURS",
             "NO OF LOOMS", "PRODUCTION", "AMOUNT", "RATE"]]
    for r in rows:
        data.append([
            f"{r['line_no']}  |  {r['wgroup'] or ''}", r["sard_helper"] or "",
            d2(r["running_hours"]), d2(r["lost_hours"]), i0(r["no_of_looms"]),
            i0(r["quantity"]), d2(r["pamount"]), php(r["rate"]),
        ])
    table = Table(data, repeatRows=1,
                  colWidths=[34 * mm, 18 * mm, 22 * mm, 22 * mm, 22 * mm, 24 * mm, 24 * mm, 26 * mm])
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica-Bold", 8),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.black),
        ("LINEABOVE", (0, 0), (-1, 0), 0.4, colors.black),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    bio = io.BytesIO()
    SimpleDocTemplate(bio, pagesize=A4, leftMargin=5 * mm, rightMargin=5 * mm,
                      topMargin=6 * mm, bottomMargin=8 * mm,
                      title="Line Hours Statement").build([
        Paragraph(company, head),
        Paragraph(f"LINE HOURS STATEMENT FOR THE PERIOD FROM :{dmy(date_from)} TO {dmy(date_to)}", head),
        Spacer(1, 3 * mm),
        table,
    ])
    bio.seek(0)
    return bio


@router.get("/njm_line_hours_checklist")
def njm_line_hours_checklist(
    request: Request,
    date_from: str,
    date_to: str,
    co_id: int | None = None,
    fmt: str = "json",
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Line Hours Checklist (legacy hol_get1 = 3 / njmlinehrschecklist): what
    the line-hours upload holds for the period, with the per-loom-hour rate
    legacy derived on the statement. `fmt=pdf` returns the legacy LINE HOURS
    STATEMENT instead of the grid rows."""
    try:
        _validate_period(date_from, date_to)
        data = _rows(db, _LINE_HOURS_CHECKLIST,
                     {"date_from": date_from, "date_to": date_to})
        for r in data:
            r["rate"] = line_hours_rate(r["pamount"], r["running_hours"], r["lost_hours"],
                                        r["no_of_looms"], r["sard_helper"])
        if fmt == "pdf":
            co = db.execute(get_company_name(), {"co_id": co_id}).fetchone() if co_id else None
            company = (co._mapping["co_name"] if co else "") or "Company"
            bio = _line_hours_pdf(data, company, date_from, date_to)
            return StreamingResponse(
                iter([bio.getvalue()]), media_type="application/pdf",
                headers={"Content-Disposition":
                         f'attachment; filename="linehours_{date_from}_{date_to}.pdf"'},
            )
        return {"data": data, "total": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_line_hours_checklist failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


_PRODUCTION_CHECKLIST = """
    SELECT od.emp_code,
           dm.dept_desc            AS master_department,
           w.qualcode,
           q.quality,
           w.qual_rate,
           w.production,
           w.act_prod_amount
    FROM tbl_njm_wages_data_collection w
    LEFT JOIN hrms_ed_official_details od
           ON od.eb_id = w.eb_id AND od.active = 1
    LEFT JOIN tbl_wages_quality_master q ON q.q_code = w.qualcode
    LEFT JOIN dept_mst dm ON dm.dept_id = q.master_dept_id
    WHERE w.date_from = :date_from AND w.date_to = :date_to
      AND w.is_active = 1
      AND w.payscheme_id = :pay_scheme_id
      AND (w.production > 0 OR w.piece_wages > 0)
    ORDER BY w.qualcode, od.emp_code
"""


@router.get("/njm_production_checklist")
def njm_production_checklist(
    request: Request,
    date_from: str,
    date_to: str,
    pay_scheme_id: int,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Production Checklist (legacy hol_get1 = 4): piece production and its
    value per employee and quality for the period.

    Narrowed by pay scheme, which legacy did not do — every other arm of
    tbl_njm_wages_data_collection is keyed by it, and without the filter the
    checklist mixes schemes that are paid separately.
    """
    try:
        _validate_period(date_from, date_to)
        data = _rows(db, _PRODUCTION_CHECKLIST, {
            "date_from": date_from, "date_to": date_to,
            "pay_scheme_id": pay_scheme_id,
        })
        return {"data": data, "total": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_production_checklist failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Food allowance credits attendance type 'C' as well as R/O -- legacy's FA
# query is the one place that does, so a canteen day still earns the allowance.
_FOOD_ALLOWANCE_DETAILS = """
    SELECT fa.eb_no                                   AS ticket_no,
           att.emp_name,
           fa.da_rate,
           fa.fa_rate,
           ROUND(att.whrs / 8, 3)                     AS fa_days,
           ROUND(ROUND(att.whrs / 8, 3) * fa.fa_rate, 0) AS amount
    FROM tbl_njm_food_allowance fa
    JOIN (
        SELECT od.emp_code,
               CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ',
                      TRIM(IFNULL(pd.middle_name, '')), ' ',
                      TRIM(IFNULL(pd.last_name, ''))) AS emp_name,
               SUM(da.working_hours - COALESCE(da.idle_hours, 0)) AS whrs
        FROM daily_attendance da
        LEFT JOIN hrms_ed_official_details od
               ON od.eb_id = da.eb_id AND od.active = 1
        LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = da.eb_id
        WHERE da.is_active = 1
          AND da.branch_id = :branch_id
          AND da.attendance_type IN ('R', 'O', 'C')
          AND da.attendance_date BETWEEN :date_from AND :date_to
        GROUP BY od.emp_code, emp_name
    ) att ON att.emp_code = fa.eb_no
    WHERE att.whrs > 0
    ORDER BY fa.eb_no
"""


@router.get("/njm_food_allowance_details")
def njm_food_allowance_details(
    request: Request,
    branch_id: int,
    date_from: str,
    date_to: str,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Food Allowance Details (legacy hol_get1 = 7): FA days and amount per
    employee, from the rate card in tbl_njm_food_allowance."""
    try:
        _validate_period(date_from, date_to)
        data = _rows(db, _FOOD_ALLOWANCE_DETAILS, {
            "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
        })
        return {"data": data, "total": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_food_allowance_details failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


_ADVANCE_DETAILS = """
    SELECT esi_no, emp_code, empname, eb_id, advance_date, advance_amount,
           installment_amount, adj_amount, date_from, ded_amount,
           cumulative_ded, balamt
    FROM vw_company_advance_balance
    ORDER BY esi_no, date_from
"""


@router.get("/njm_advance_details")
def njm_advance_details(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Advance Details (legacy hol_get1 = 8): outstanding company advances and
    what each wage period has recovered.

    Deliberately unfiltered, as legacy was: an advance spans wage periods, so
    narrowing to the selected one would hide the balance still to recover.
    """
    try:
        data = _rows(db, _ADVANCE_DETAILS, {})
        return {"data": data, "total": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_advance_details failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Canteen and STL statements ─────────────────────────────────────
# Legacy served both as Excel downloads off their own modals
# (Data_entry_2::canteendetdownloaddata / ::stldownloaddata); here they are
# checklist entries like the rest, and the screen exports the grid.

# The canteen sheet carries one column per day, so the period that builds the
# pivot is capped at a wage month.
_STATEMENT_MAX_DAYS = 31


def _period_days(date_from: str, date_to: str) -> list[date]:
    _validate_period(date_from, date_to)
    start, end = _as_date(date_from), _as_date(date_to)
    span = (end - start).days + 1
    if span > _STATEMENT_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Period is {span} days; a day-wise statement covers at most "
                   f"{_STATEMENT_MAX_DAYS}",
        )
    return [start + timedelta(days=i) for i in range(span)]


def _day_label(day: date) -> str:
    """Column name for one day of the pivot.

    The SQL alias and the `columns` list the screen renders BOTH come from
    here: when they drift the sheet still draws its day columns but reads keys
    that no longer exist, and every day cell comes out blank while the totals
    look fine.
    """
    return f"{day:%d}"


def _canteen_statement_sql(days: list[date]) -> str:
    """Meals per employee per day, valued at the rate on each entry.

    Two deviations from legacy, both forced by the tenant schema: entries key
    on eb_id rather than matching a ticket number to emp_code, and the amount
    uses canteen_details.rate_of_meals instead of the 40 legacy hardcoded.

    Columns are aliased by day of month, as legacy's sheet was. The 31-day cap
    is what keeps that unambiguous — a period that straddles two months still
    cannot repeat a day number within it.

    Amounts are whole rupees: meals are counted, not measured, and the sheet is
    read against cash.
    """
    pivot = ",\n           ".join(
        f"CAST(SUM(IF(c.tran_date = '{d.isoformat()}', "
        f"c.no_of_meals * IFNULL(c.rate_of_meals, 0), 0)) AS SIGNED) "
        f"AS `{_day_label(d)}`"
        for d in days
    )
    return f"""
    SELECT od.emp_code,
           CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ',
                  TRIM(IFNULL(pd.middle_name, '')), ' ',
                  TRIM(IFNULL(pd.last_name, ''))) AS emp_name,
           dm.dept_desc,
           {pivot},
           CAST(SUM(c.no_of_meals) AS SIGNED)       AS meals,
           CAST(SUM(c.no_of_meals * IFNULL(c.rate_of_meals, 0)) AS SIGNED)
                                                    AS total_amount
    FROM canteen_details c
    LEFT JOIN hrms_ed_official_details od ON od.eb_id = c.eb_id AND od.active = 1
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = c.eb_id
    LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst dm ON dm.dept_id = sd.dept_id
    WHERE c.branch_id = :branch_id
      AND c.tran_date BETWEEN :date_from AND :date_to
      AND c.status_id <> :cancelled
    GROUP BY od.emp_code, emp_name, dm.dept_desc
    ORDER BY dm.dept_desc, od.emp_code
"""


@router.get("/njm_canteen_statement")
def njm_canteen_statement(
    request: Request,
    branch_id: int,
    date_from: str,
    date_to: str,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Canteen Statement (checklist 9): meals and their value per employee,
    one column per day of the period.

    Counts every entry the Canteen Details screen still lists — draft as well
    as approved — so what an operator can see there is what the statement
    charges; only cancelled entries drop out.
    """
    try:
        days = _period_days(date_from, date_to)
        data = _rows(db, _canteen_statement_sql(days), {
            "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
            "cancelled": CANTEEN_CANCELLED,
        })
        return {
            "data": data,
            "columns": [
                "dept_desc", *(_day_label(d) for d in days), "meals", "total_amount",
            ],
            "total": len(data),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_canteen_statement failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _stl_statement_sql(days: list[date]) -> str:
    """Payable leave per employee per day — the canteen statement's shape: one
    row an employee, a column a day, then the total days.

    Which leave counts is the master's call (payable = 'Y'), not a hardcoded
    type id. A day is marked from the request's leave_tran_details when it has
    them — they are the authoritative dates, off-days already excluded — and
    from the request's own range when it has none, because details stop being
    written well before the latest approved requests and that leave is still
    payable.
    """
    calendar = " UNION ALL ".join(
        f"SELECT DATE('{d.isoformat()}') AS d" for d in days
    )
    pivot = ",\n           ".join(
        f"MAX(IF(cal.d = '{d.isoformat()}', 1, 0)) AS `{_day_label(d)}`"
        for d in days
    )
    return f"""
    SELECT od.emp_code,
           CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ',
                  TRIM(IFNULL(pd.middle_name, '')), ' ',
                  TRIM(IFNULL(pd.last_name, ''))) AS emp_name,
           dm.dept_desc,
           {pivot},
           COUNT(DISTINCT cal.d) AS total_days
    FROM leave_transactions lt
    -- Not narrowed by is_active: a type since retired still had payable leave
    -- granted under it, and that leave is still owed.
    JOIN hrms_leave_types_mst ltp
      ON ltp.leave_type_id = lt.leave_type_id
     AND UPPER(TRIM(ltp.payable)) = 'Y'
    JOIN ({calendar}) cal
      ON cal.d BETWEEN lt.leave_from_date
                   AND COALESCE(lt.leave_to_date, lt.leave_from_date)
    LEFT JOIN leave_tran_details ltd
      ON ltd.ltran_id = lt.leave_transaction_id
     AND ltd.is_active = 1
     AND ltd.leave_date = cal.d
    LEFT JOIN (
        SELECT ltran_id, COUNT(*) AS n FROM leave_tran_details
        WHERE is_active = 1 GROUP BY ltran_id
    ) det ON det.ltran_id = lt.leave_transaction_id
    LEFT JOIN hrms_ed_official_details od ON od.eb_id = lt.eb_id AND od.active = 1
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = lt.eb_id
    LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst dm ON dm.dept_id = sd.dept_id
    WHERE lt.branch_id = :branch_id
      AND lt.status = :leave_approved
      -- A request with details is marked only on the days it actually lists.
      AND (det.n IS NULL OR ltd.lvd_tran_id IS NOT NULL)
    GROUP BY od.emp_code, emp_name, dm.dept_desc
    ORDER BY dm.dept_desc, od.emp_code
"""


@router.get("/njm_stl_statement")
def njm_stl_statement(
    request: Request,
    branch_id: int,
    date_from: str,
    date_to: str,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """STL Statement (checklist 10): approved PAYABLE leave, day-wise per
    employee — the canteen statement's layout.

    Which leave counts comes from hrms_leave_types_mst.payable = 'Y' rather
    than legacy's hardcoded type 24 — STL is one payable type among several,
    and a tenant that adds another gets it here without a code change.

    Legacy listed every unpaid STL request ever, ignoring the period, because
    it could exclude what EMPMILL12.tbl_stl_days_payment had already paid. That
    table has no tenant equivalent, so an unbounded list would only grow; the
    period on screen is the filter instead.
    """
    try:
        days = _period_days(date_from, date_to)
        data = _rows(db, _stl_statement_sql(days), {
            "branch_id": branch_id, "leave_approved": LEAVE_APPROVED,
        })
        return {
            "data": data,
            "columns": [
                "dept_desc", *(_day_label(d) for d in days), "total_days",
            ],
            "total": len(data),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_stl_statement failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


_CNT_WAGES_EXPORT = """
    SELECT od.emp_code,
           CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ',
                  TRIM(IFNULL(pd.middle_name, '')), ' ',
                  TRIM(IFNULL(pd.last_name, ''))) AS emp_name,
           psm.payscheme_name                     AS pay_scheme,
           IFNULL(w.working_days, 0)              AS c_working_days,
           IFNULL(w.pwage_amt, 0)                 AS c_pwage_amt,
           IFNULL(w.exadvance, 0)                 AS c_exadvance,
           IFNULL(w.arr_plus, 0)                  AS c_arramtplus,
           IFNULL(w.arr_minus, 0)                 AS c_arramtminus,
           IFNULL(w.ot_days, 0)                   AS c_rate1otdays,
           0                                      AS c_rate2otdays,
           IFNULL(w.reg_days, 0)                  AS c_rate1regdays,
           0                                      AS c_rate2regdays,
           IFNULL(w.canteen, 0)                   AS c_canteen,
           IFNULL(esi.esi_days, 0)                AS c_esi_days
    FROM pay_employee_payscheme pep
    LEFT JOIN pay_scheme_master psm ON psm.payscheme_id = pep.PAY_SCHEME_ID
    LEFT JOIN hrms_ed_official_details od
           ON od.eb_id = pep.EMPLOYEEID AND od.active = 1
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = pep.EMPLOYEEID
    LEFT JOIN (
        SELECT c.eb_id,
               DATEDIFF(:date_to, :date_from) + 1              AS working_days,
               IFNULL(SUM(c.prod_amount), 0)                   AS pwage_amt,
               IFNULL(SUM(c.advance + c.canteen), 0)           AS exadvance,
               IFNULL(SUM(c.plus_amount), 0)                   AS arr_plus,
               IFNULL(SUM(c.minus_amount), 0)                  AS arr_minus,
               IFNULL(ROUND(SUM(c.wrk_hours_ot) / 8, 2), 0)    AS ot_days,
               IFNULL(ROUND(SUM(c.wrk_hours_reg + c.wrk_hours_adj) / 8, 2), 0)
                                                               AS reg_days,
               IFNULL(SUM(c.canteen), 0)                       AS canteen
        FROM tbl_njm_cnt_wages_data_collection c
        WHERE c.start_date = :date_from AND c.end_date = :date_to
          AND c.is_active = 1
        GROUP BY c.eb_id
    ) w ON w.eb_id = pep.EMPLOYEEID
    LEFT JOIN (
        SELECT da.eb_id, COUNT(DISTINCT da.attendance_date) AS esi_days
        FROM daily_attendance da
        JOIN hrms_ed_official_details o2
          ON o2.eb_id = da.eb_id AND o2.active = 1
         AND o2.catagory_id IN (SELECT cata_id FROM category_mst
                                WHERE cata_desc IN :contract_categories)
        WHERE da.attendance_date BETWEEN :date_from AND :date_to
          AND da.attendance_type IN ('R', 'O')
          AND da.is_active = 1
          AND da.branch_id = :branch_id
        GROUP BY da.eb_id
    ) esi ON esi.eb_id = pep.EMPLOYEEID
    WHERE pep.PAY_SCHEME_ID = :pay_scheme_id AND pep.STATUS = 1
    ORDER BY od.emp_code
"""

# Export column -> pay_components.CODE. The sheet's headers are these codes,
# which is how the payroll upload resolves them; the contractor posting uses
# the same map so posting == uploading the downloaded sheet.
CNT_EXPORT_COMPONENT_CODES = {
    "c_working_days": "C_WORKING_DAYS_",
    "c_pwage_amt": "C_Pwage_amt_",
    "c_exadvance": "C_exadvance_",
    "c_arramtplus": "C_ARRAMTPLUS_",
    "c_arramtminus": "C_ARRAMTMINUS_",
    "c_rate1otdays": "C_RATE1OTDAYS_",
    "c_rate2otdays": "C_RATE2OTDAYS_",
    "c_rate1regdays": "C_RATE1REGDAYS_",
    "c_rate2regdays": "C_RATE2REGDAYS_",
    "c_canteen": "C_canteen_",
    "c_esi_days": "C_esi_days_",
}


@router.get("/njm_cnt_wages_export")
def njm_cnt_wages_export(
    request: Request,
    branch_id: int,
    date_from: str,
    date_to: str,
    pay_scheme_id: int,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Contractor Wages Excel for PayRoll (legacy hol_get = 3): the payroll
    upload columns, per employee on the selected scheme.

    Legacy hardcoded pay scheme 163 in three places; here it is the scheme the
    screen has selected.
    """
    try:
        _validate_period(date_from, date_to)
        data = _rows(db, _CNT_WAGES_EXPORT, {
            "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
            "pay_scheme_id": pay_scheme_id,
            "contract_categories": list(CONTRACT_CATEGORY_NAMES),
        })
        return {"data": data, "total": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_cnt_wages_export failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/njm_pay_period")
def njm_pay_period(
    request: Request,
    date_from: str,
    date_to: str,
    pay_scheme_id: int,
    branch_id: int | None = None,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Resolve the screen's period + pay scheme to a pay_period id.

    Pay Register, Pay Slip Printing and Bank Statements (legacy hol_get 4, 8, 9
    and 10) already exist in this backend as /hrmsMasters/pay_register_salary,
    /pay_register_payslips and /pay_register_bank_sheet — all keyed by
    payperiod_id. This is the bridge, so those four screens reuse the existing
    reports instead of growing a second NJM-only copy of each.

    Cancelled periods (status 4) are excluded, matching the period picker.
    """
    try:
        _validate_period(date_from, date_to)
        row = db.execute(text("""
            SELECT p.ID AS payperiod_id, p.STATUS AS status_id, p.branch_id,
                   psm.payscheme_name
            FROM pay_period p
            LEFT JOIN pay_scheme_master psm ON psm.payscheme_id = p.PAYSCHEME_ID
            WHERE p.FROM_DATE = :date_from AND p.TO_DATE = :date_to
              AND p.PAYSCHEME_ID = :pay_scheme_id
              AND (p.STATUS IS NULL OR p.STATUS <> 4)
              AND (:branch_id IS NULL OR p.branch_id = :branch_id
                   OR p.branch_id IS NULL)
            ORDER BY p.ID DESC
            LIMIT 1
        """), {
            "date_from": date_from, "date_to": date_to,
            "pay_scheme_id": pay_scheme_id, "branch_id": branch_id,
        }).fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail=("No pay period covers this pay scheme for "
                        f"{date_from} to {date_to}. Create the period in Pay "
                        "Register first."),
            )
        return {"data": dict(row._mapping)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_pay_period failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Contractor Wages Process (legacy hol_get = 1) ──────────────────

# The finishing piece-rate roles. Legacy `NOT IN (1098,1100,1104,1106)` kept
# them out of the hourly arm and priced their production instead — resolved to
# the NAMES those ids carry, because designation ids are per-branch (sls
# branch 87 numbers the same four roles 1864/1866/1870/1872, so the ids
# skipped nobody there).
CNT_PIECE_DESIGNATION_NAMES = ("HAND SEWER", "HERAKALE OPERATOR",
                               "J BAG SEWER", "MOUTH HEMMING OPERATOR")

_CNT_PROCESS_INSERT = text("""
    INSERT INTO tbl_njm_cnt_wages_data_collection
        (start_date, end_date, eb_id, dept_id, desig_id,
         wrk_hours_reg, wrk_hours_ot, wrk_hours_adj, rates, prod_id, production,
         amount_reg, amount_ot, amount_adj, canteen, advance, plus_amount,
         minus_amount, travel_allowance, updt_from, loom_type, time_piece,
         reg_amount, prod_amount, esi_days, is_active)
    SELECT :date_from, :date_to, da.eb_id, od.sub_dept_id, da.worked_designation_id,
           SUM(da.rwhrs), SUM(da.owhrs), 0, 0, 0, 0,
           0, 0, 0, 0, 0, 0,
           0, 0, 'A', NULL, dsg.time_piece,
           0, 0, SUM(esi.esi_days), 1
    FROM (
        SELECT d.eb_id, d.worked_designation_id,
               SUM(CASE WHEN d.attendance_type = 'R'
                        THEN d.working_hours - COALESCE(d.idle_hours, 0) ELSE 0 END) AS rwhrs,
               SUM(CASE WHEN d.attendance_type = 'O'
                        THEN d.working_hours - COALESCE(d.idle_hours, 0) ELSE 0 END) AS owhrs
        FROM daily_attendance d
        WHERE d.attendance_date BETWEEN :date_from AND :date_to
          AND d.attendance_type IN ('R', 'O')
          AND d.is_active = 1 AND d.status_id = '3'
          AND d.branch_id = :branch_id
        GROUP BY d.eb_id, d.worked_designation_id
    ) da
    JOIN hrms_ed_official_details od
      ON od.eb_id = da.eb_id AND od.active = 1
     AND od.catagory_id IN (SELECT cata_id FROM category_mst
                            WHERE cata_desc IN :contract_categories)
    LEFT JOIN designation_mst dsg ON dsg.designation_id = da.worked_designation_id
    LEFT JOIN (
        SELECT d.eb_id, COUNT(DISTINCT d.attendance_date) AS esi_days
        FROM daily_attendance d
        WHERE d.attendance_date BETWEEN :date_from AND :date_to
          AND d.attendance_type IN ('R', 'O')
          AND d.is_active = 1 AND d.status_id = '3'
          AND d.branch_id = :branch_id
        GROUP BY d.eb_id
    ) esi ON esi.eb_id = da.eb_id
    WHERE COALESCE(dsg.desig, '') NOT IN :piece_designations
    GROUP BY da.eb_id, od.sub_dept_id, da.worked_designation_id, dsg.time_piece
""").bindparams(bindparam("contract_categories", expanding=True),
                bindparam("piece_designations", expanding=True))


# Canteen recovery for contractor staff (legacy's 'C' arm). Its own updt_from
# so a rebuild of the attendance arm never disturbs it and vice versa.
CONTRACTOR_CANTEEN_UPDT_FROM = "C"

_CNT_CANTEEN_INSERT = text("""
    INSERT INTO tbl_njm_cnt_wages_data_collection
        (start_date, end_date, eb_id, dept_id, desig_id,
         wrk_hours_reg, wrk_hours_ot, wrk_hours_adj, rates, prod_id, production,
         amount_reg, amount_ot, amount_adj, canteen, advance, plus_amount,
         minus_amount, travel_allowance, updt_from, loom_type, time_piece,
         reg_amount, prod_amount, esi_days, is_active)
    SELECT :date_from, :date_to, cd.eb_id, NULL, NULL,
           NULL, NULL, NULL, 0, NULL, NULL,
           0, 0, 0,
           SUM(cd.no_of_meals * COALESCE(cd.rate_of_meals, :canteen_rate)),
           0, 0,
           0, 0, :updt_from, NULL, 'T',
           0, 0, 0, 1
    FROM canteen_details cd
    JOIN hrms_ed_official_details od
      ON od.eb_id = cd.eb_id AND od.active = 1
     AND od.catagory_id IN (SELECT cata_id FROM category_mst
                            WHERE cata_desc IN :contract_categories)
    WHERE cd.tran_date BETWEEN :date_from AND :date_to
      AND cd.branch_id = :branch_id
      AND cd.status_id = :canteen_approved
    GROUP BY cd.eb_id
""").bindparams(bindparam("contract_categories", expanding=True))


# Piece production for contractor staff (legacy's 'P' arm). Legacy priced
# vowsls.finishing_entries x process_type_master.process_code against the
# cnt_rate card; this tenant captures the same work per employee in
# jute_prod_finishing_daily under a process token, so the token is mapped to
# the card's q_code here as the FALLBACK. The branch's production-quality ->
# wages-quality link decides first: one token covers several cards (Aug-2026
# `hemming` rows were HEMMING 26.5" = 7801 at 3.50 AND HEMMING JOINTS = 7808
# at 12.75), and only the link knows which. Same order as the main FNG arm.
CONTRACTOR_PIECE_UPDT_FROM = "P"
CNT_PIECE_PROCESS_QCODE = {"hemming": "7801", "herackle": "7803", "sacksewing": "7805"}

# One process per run. Legacy only priced production on days the employee was
# marked on a piece designation for that spell — same gate here. Binds
# :dept_needle (LINK_DEPT_NEEDLE["finishing"]) for the link's department.
_CNT_PIECE_INSERT = text("""
    INSERT INTO tbl_njm_cnt_wages_data_collection
        (start_date, end_date, eb_id, dept_id, desig_id,
         wrk_hours_reg, wrk_hours_ot, wrk_hours_adj, rates, prod_id, production,
         amount_reg, amount_ot, amount_adj, canteen, advance, plus_amount,
         minus_amount, travel_allowance, updt_from, loom_type, time_piece,
         reg_amount, prod_amount, esi_days, is_active)
    SELECT :date_from, :date_to, fd.eb_id, od.sub_dept_id, NULL,
           NULL, NULL, NULL, q.cnt_rate, q.q_code, SUM(fd.prod_qty),
           0, 0, 0, 0, 0, 0,
           0, 0, :updt_from, NULL, 'P',
           0, ROUND(q.cnt_rate * SUM(fd.prod_qty), 2), esi.esi_days, 1
    FROM jute_prod_finishing_daily fd
    JOIN daily_attendance da
      ON da.eb_id = fd.eb_id AND da.attendance_date = fd.tran_date
     AND da.spell_id = fd.spell_id AND da.is_active = 1 AND da.status_id = '3'
    JOIN designation_mst dsg
      ON dsg.designation_id = da.worked_designation_id
     AND dsg.desig IN :piece_designations
    JOIN hrms_ed_official_details od
      ON od.eb_id = fd.eb_id AND od.active = 1
     AND od.catagory_id IN (SELECT cata_id FROM category_mst
                            WHERE cata_desc IN :contract_categories)
    LEFT JOIN (
        SELECT l.prod_quality_id, lq.q_code
        FROM link_prodquality_wagequality_mst l
        JOIN dept_mst ld ON ld.dept_id = l.dept_id
                        AND UPPER(ld.dept_desc) REGEXP :dept_needle
        JOIN tbl_wages_quality_master lq
          ON lq.q_code_id = l.wages_quality_id AND lq.is_active = 1
        WHERE l.active = 1 AND l.branch_id = :branch_id
    ) lk ON lk.prod_quality_id = fd.finishing_quality_id
    JOIN tbl_wages_quality_master q
      ON q.q_code = COALESCE(lk.q_code, :q_code)
     AND q.branch_id = :branch_id AND q.is_active = 1
    LEFT JOIN (
        SELECT eb_id, COUNT(DISTINCT tran_date) AS esi_days
        FROM jute_prod_finishing_daily
        WHERE tran_date BETWEEN :date_from AND :date_to
          AND active = 1 AND branch_id = :branch_id
        GROUP BY eb_id
    ) esi ON esi.eb_id = fd.eb_id
    WHERE fd.tran_date BETWEEN :date_from AND :date_to
      AND fd.active = 1 AND fd.branch_id = :branch_id
      AND fd.process = :process
    GROUP BY fd.eb_id, od.sub_dept_id, q.cnt_rate, q.q_code, esi.esi_days
""").bindparams(bindparam("contract_categories", expanding=True),
                bindparam("piece_designations", expanding=True))


@router.post("/njm_cnt_wages_process")
def njm_cnt_wages_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Contractor Wages Process (legacy hol_get = 1).

    Builds the contractor wage base for the period:
      'A'  regular and overtime hours net of idle, plus ESI days, for
           CONTRACT-category employees, skipping days worked on the piece
           designations (those are paid by production instead)
      'P'  piece production x cnt_rate per employee and process, for days the
           employee was on a piece designation; ESI days = distinct
           production dates
      'C'  canteen recovery, meals x rate per employee

    DEFAULT_CANTEEN_RATE only applies where canteen_details carries no rate of
    its own; legacy always used the hardcoded 40.

    Rebuilds only what it owns — rows marked 'M' (the contractor upload) survive,
    exactly as legacy's `updt_from NOT IN ('M')` did.

    Body: { branch_id, date_from, date_to }
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")
        _validate_period(date_from, date_to)

        # Hard delete rather than legacy's is_active = 0: each rebuild would
        # otherwise leave another full copy of the period behind.
        db.execute(text("""
            DELETE FROM tbl_njm_cnt_wages_data_collection
            WHERE start_date = :date_from AND end_date = :date_to
              AND COALESCE(updt_from, '') <> :manual
        """), {"date_from": date_from, "date_to": date_to,
               "manual": CONTRACTOR_UPDT_FROM})

        base = {"date_from": date_from, "date_to": date_to, "branch_id": int(branch_id),
                "contract_categories": list(CONTRACT_CATEGORY_NAMES),
                "piece_designations": list(CNT_PIECE_DESIGNATION_NAMES)}
        attendance = db.execute(_CNT_PROCESS_INSERT, base).rowcount or 0
        piece = sum(
            db.execute(_CNT_PIECE_INSERT, {
                **base, "process": process, "q_code": q_code,
                "dept_needle": LINK_DEPT_NEEDLE["finishing"],
                "updt_from": CONTRACTOR_PIECE_UPDT_FROM,
            }).rowcount or 0
            for process, q_code in CNT_PIECE_PROCESS_QCODE.items()
        )
        canteen = db.execute(_CNT_CANTEEN_INSERT, {
            **base, "canteen_rate": DEFAULT_CANTEEN_RATE,
            "canteen_approved": CANTEEN_APPROVED,
            "updt_from": CONTRACTOR_CANTEEN_UPDT_FROM,
        }).rowcount or 0
        db.commit()
        return {"data": {
            "message": (f"Contractor wages built — attendance {attendance}, "
                        f"piece production {piece}, canteen {canteen}"),
            "rows": attendance + piece + canteen,
            "attendance": attendance, "piece": piece, "canteen": canteen,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"njm_cnt_wages_process failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Payroll Posting (legacy hol_get = 2 and 6) ─────────────────────
#
# Legacy left both "Under Development" — the posting step never existed there.
# It does here. Contractor posting replays the Contractor Wages Excel into the
# scheme's components by CODE (what uploading that sheet does); main posting
# replays the Main Wages sheet below through tbl_wages_data_payschm_map_table,
# which says which sheet column feeds which pay component. Both write
# pay_components_custom, the same table /hrms/pay_roll_save writes, so the
# payroll engine picks the values up with no further plumbing.

# ─── Main wages sheet (legacy njmwagesexceldownload) ────────────────
#
# The per-employee sheet the Main Wages Excel exports and the main posting
# reads. The map's data_column_no names address THIS result, not the table:
# legacy derived three of them at export time and the map still names them.
#   piece_wages      production money capped at PIECE_BASIC_MONTHLY /
#                    PIECE_BASIC_HOURS per hour of piece time (piece_hours +
#                    extra_hours_p), plus the sardar/helper amount
#   piece_wages_inc  whatever production money exceeded that cap
#   sardhelp_rate    the sardar/helper amount on its own
# cadeligibility and gwf_eleg are forced to 0; iftu_eleg is 1 when any IFTU
# amount was collected. The cap runs per (employee, upd_type) first and the
# groups are then summed — legacy's two-level GROUP BY, kept as it was. Every
# scheme member is listed, with blanks where nothing was collected, exactly as
# the legacy sheet did (it started from the scheme membership).
EMP_STATUS_JOINED = 35      # status_mst 'JOINED' on hrms_ed_personal_details
PIECE_BASIC_MONTHLY = 1200
PIECE_BASIC_HOURS = 208

_SHEET_SUM_COLUMNS = (
    "hours_wkd_1", "hours_wkd_2", "esi_days", "el_days", "sl_days",
    "lay_off_hrs", "piece_hours", "canteen", "sunday_adv", "other_adv",
    "el_advance", "adv_ded", "installment_advance", "extra_hours_t",
    "extra_hours_p", "c_shift_days", "festival_hours", "arrear_plus",
    "arrear_minus", "other_pay", "cl_days", "prod_balance", "ul_days",
    "advance", "act_prod_amount", "loom_production", "iltime_hrs",
    "ilpiece_hrs", "minus_bal",
)
_SHEET_DERIVED_COLUMNS = ("sardhelp_rate", "cadeligibility", "gwf_eleg",
                          "iftu_eleg", "piece_wages", "piece_wages_inc")
MAIN_WAGES_SHEET_COLUMNS = _SHEET_SUM_COLUMNS + _SHEET_DERIVED_COLUMNS


def _sheet_sql() -> str:
    """Binds :date_from, :date_to, :pay_scheme_id, :piece_basic_monthly,
    :piece_basic_hours. Column names come from the tuples above, never from a
    request."""
    inner = ", ".join(f"SUM(COALESCE({c}, 0)) AS {c}" for c in _SHEET_SUM_COLUMNS)
    outer = ", ".join(f"SUM(g.{c}) AS {c}" for c in _SHEET_SUM_COLUMNS)
    pick = ", ".join(f"w.{c}" for c in MAIN_WAGES_SHEET_COLUMNS)
    piece_hours = "SUM(COALESCE(piece_hours, 0) + COALESCE(extra_hours_p, 0))"
    prod_amount = "SUM(COALESCE(act_prod_amount, 0))"
    basic = "(:piece_basic_monthly / :piece_basic_hours)"
    # NULLIF makes the no-piece-hours case explicit: legacy's bare division
    # went NULL and fell through to ELSE the same way.
    under_cap = f"{prod_amount} / NULLIF({piece_hours}, 0) <= {basic}"
    return f"""
        SELECT od.emp_code,
               CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ',
                      TRIM(IFNULL(pd.middle_name, '')), ' ',
                      TRIM(IFNULL(pd.last_name, ''))) AS emp_name,
               psm.payscheme_name AS pay_scheme,
               t.EMPLOYEEID AS eb_id,
               {pick}
        FROM pay_employee_payscheme t
        JOIN hrms_ed_official_details od ON od.eb_id = t.EMPLOYEEID AND od.active = 1
        -- Only JOINED employees go on the sheet (and so into payroll posting).
        JOIN hrms_ed_personal_details pd
          ON pd.eb_id = t.EMPLOYEEID AND pd.status_id = {EMP_STATUS_JOINED}
        LEFT JOIN pay_scheme_master psm ON psm.payscheme_id = t.PAY_SCHEME_ID
        LEFT JOIN (
            SELECT g.eb_id, {outer},
                   SUM(g.sardhelp_amt) AS sardhelp_rate,
                   0 AS cadeligibility,
                   0 AS gwf_eleg,
                   CASE WHEN SUM(g.iftu_amount) > 0 THEN 1 ELSE 0 END AS iftu_eleg,
                   SUM(g.piece_wages + g.sardhelp_amt) AS piece_wages,
                   SUM(g.piece_wages_inc) AS piece_wages_inc
            FROM (
                SELECT eb_id, upd_type, {inner},
                       SUM(COALESCE(sardhelp_amt, 0)) AS sardhelp_amt,
                       SUM(COALESCE(iftu_amount, 0)) AS iftu_amount,
                       CASE WHEN {under_cap}
                            THEN ROUND({prod_amount}, 2)
                            ELSE ROUND({piece_hours} * {basic}, 2)
                       END AS piece_wages,
                       CASE WHEN {under_cap}
                            THEN 0
                            ELSE ROUND({prod_amount} - {piece_hours} * {basic}, 2)
                       END AS piece_wages_inc
                FROM tbl_njm_wages_data_collection
                WHERE date_from = :date_from AND date_to = :date_to
                  AND is_active = 1 AND payscheme_id = :pay_scheme_id
                GROUP BY eb_id, upd_type
            ) g
            GROUP BY g.eb_id
        ) w ON w.eb_id = t.EMPLOYEEID
        WHERE t.STATUS = 1 AND t.PAY_SCHEME_ID = :pay_scheme_id
        ORDER BY od.emp_code
    """


def _main_wages_sheet(db, date_from: str, date_to: str, pay_scheme_id: int) -> list[dict]:
    return _rows(db, _sheet_sql(), {
        "date_from": date_from, "date_to": date_to, "pay_scheme_id": pay_scheme_id,
        "piece_basic_monthly": PIECE_BASIC_MONTHLY,
        "piece_basic_hours": PIECE_BASIC_HOURS,
    })


def _posting_map(db, pay_scheme_id: int) -> list[dict]:
    """Sheet column -> pay component (id and CODE), for one scheme, in the
    order the map was entered — the sheet's column order."""
    rows = db.execute(text("""
        SELECT m.data_column_no, m.payscheme_component_id, pc.CODE AS code
        FROM tbl_wages_data_payschm_map_table m
        LEFT JOIN pay_components pc ON pc.ID = m.payscheme_component_id
        WHERE m.payscheme_id = :pay_scheme_id
          AND m.data_column_no IS NOT NULL
          AND m.payscheme_component_id IS NOT NULL
        ORDER BY m.wages_data_payschm_map_id
    """), {"pay_scheme_id": pay_scheme_id}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/njm_payroll_posting")
def njm_payroll_posting(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Post collected wages data into payroll (legacy hol_get = 2 / 6).

    Writes per-employee values to pay_components_custom for the period's pay
    period, replacing anything a previous posting wrote for the same period,
    employees and components.

    contractor: the Contractor Wages Excel (njm_cnt_wages_export), column by
    column, to the scheme's components with those CODEs — identical to
    downloading that sheet and uploading it through Pay Roll. Components the
    scheme does not carry are reported in skipped_columns.

    main: the Main Wages sheet (_sheet_sql, the same rows the Excel exports),
    column by column, to the components tbl_wages_data_payschm_map_table maps
    them to. With no mapping configured the endpoint refuses rather than
    silently posting nothing — an empty payroll run is far more expensive to
    discover later.

    Body: { branch_id (contractor), date_from, date_to, pay_scheme_id,
            source: contractor|main }
    """
    try:
        body = parse_json_body(request)
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        pay_scheme_id = body.get("pay_scheme_id")
        source_key = (body.get("source") or "main").lower()
        _validate_period(date_from, date_to)
        if not pay_scheme_id:
            raise HTTPException(status_code=400, detail="pay_scheme_id is required")
        if source_key not in ("contractor", "main"):
            raise HTTPException(
                status_code=400,
                detail="source must be 'contractor' or 'main'",
            )

        # Both paths end with `rows` (mappings keyed by eb_id + value columns)
        # and `usable`: [{col, payscheme_component_id}] saying which row column
        # feeds which component.
        if source_key == "contractor":
            # Contractor posting == uploading the Contractor Wages Excel: the
            # export's columns go to the scheme's components with those CODEs.
            branch_id = body.get("branch_id")
            if not branch_id:
                raise HTTPException(status_code=400, detail="branch_id is required")
            code_to_id = {
                r._mapping["code"]: int(r._mapping["id"])
                for r in db.execute(get_pay_roll_custom_components(),
                                    {"pay_scheme_id": int(pay_scheme_id)}).fetchall()
            }
            usable = [{"col": col, "payscheme_component_id": code_to_id[code]}
                      for col, code in CNT_EXPORT_COMPONENT_CODES.items()
                      if code in code_to_id]
            skipped = [code for code in CNT_EXPORT_COMPONENT_CODES.values()
                       if code not in code_to_id]
            if not usable:
                raise HTTPException(
                    status_code=400,
                    detail=("This pay scheme has none of the contractor wages "
                            "components (" + ", ".join(skipped) + ")"),
                )
            rows = [r for r in db.execute(text(_CNT_WAGES_EXPORT).bindparams(
                        bindparam("contract_categories", expanding=True)), {
                        "branch_id": int(branch_id), "date_from": date_from,
                        "date_to": date_to, "pay_scheme_id": int(pay_scheme_id),
                        "contract_categories": list(CONTRACT_CATEGORY_NAMES),
                    }).fetchall()]
            # eb_id is what the tail keys on; the export carries emp_code.
            eb_by_code = {
                r._mapping["emp_code"]: int(r._mapping["eb_id"])
                for r in db.execute(text("""
                    SELECT od.emp_code, od.eb_id
                    FROM pay_employee_payscheme pep
                    JOIN hrms_ed_official_details od
                      ON od.eb_id = pep.EMPLOYEEID AND od.active = 1
                    WHERE pep.PAY_SCHEME_ID = :pay_scheme_id AND pep.STATUS = 1
                """), {"pay_scheme_id": int(pay_scheme_id)}).fetchall()
            }
            rows = [{**dict(r._mapping), "eb_id": eb_by_code.get(r._mapping["emp_code"])}
                    for r in rows]
        else:
            mapping = _posting_map(db, int(pay_scheme_id))
            if not mapping:
                raise HTTPException(
                    status_code=400,
                    detail=("No wages-to-payroll mapping is configured for this pay "
                            "scheme. Add rows to tbl_wages_data_payschm_map_table "
                            "(data_column_no -> payscheme_component_id) first."),
                )

            # A map row can only name a sheet column; nothing from the map ever
            # reaches SQL text.
            usable = [{"col": m["data_column_no"],
                       "payscheme_component_id": m["payscheme_component_id"]}
                      for m in mapping if m["data_column_no"] in MAIN_WAGES_SHEET_COLUMNS]
            skipped = [m["data_column_no"] for m in mapping
                       if m["data_column_no"] not in MAIN_WAGES_SHEET_COLUMNS]
            if not usable:
                raise HTTPException(
                    status_code=400,
                    detail=("None of the mapped columns exist on the main wages sheet: "
                            + ", ".join(sorted(set(skipped)))),
                )
            rows = _main_wages_sheet(db, date_from, date_to, int(pay_scheme_id))

        rows = [r for r in rows if r.get("eb_id")]
        if not rows:
            raise HTTPException(
                status_code=404,
                detail="No collected wages data for this period and pay scheme",
            )

        user_id = token_data.get("user_id", 0)
        period = db.execute(text("""
            SELECT ID FROM pay_period
            WHERE FROM_DATE = :date_from AND TO_DATE = :date_to
              AND PAYSCHEME_ID = :pay_scheme_id
              AND (STATUS IS NULL OR STATUS <> 4)
            ORDER BY ID DESC LIMIT 1
        """), {"date_from": date_from, "date_to": date_to,
               "pay_scheme_id": int(pay_scheme_id)}).fetchone()
        if not period:
            raise HTTPException(
                status_code=404,
                detail=("No pay period covers this pay scheme for "
                        f"{date_from} to {date_to}. Create the period first."),
            )
        pay_period_id = int(period._mapping["ID"])

        eb_ids = [int(r["eb_id"]) for r in rows]
        component_ids = [int(m["payscheme_component_id"]) for m in usable]

        # Retire this posting's own previous run only: same period, same
        # employees, same components. Anything else on the period is untouched.
        db.execute(text("""
            UPDATE pay_components_custom SET STATUS = 0, updated_by = :user_id
            WHERE PAY_PERIOD_ID = :pay_period_id
              AND STATUS = 1
              AND EMPLOYEEID IN :eb_ids
              AND COMPONENT_ID IN :component_ids
        """).bindparams(
            bindparam("eb_ids", expanding=True),
            bindparam("component_ids", expanding=True),
        ), {"user_id": user_id, "pay_period_id": pay_period_id,
            "eb_ids": eb_ids, "component_ids": component_ids})

        payload = []
        for m in rows:
            for mp in usable:
                value = m[mp["col"]]
                if value is None:
                    continue
                payload.append({
                    "component_id": int(mp["payscheme_component_id"]),
                    "value": str(float(value)),
                    "eb_id": int(m["eb_id"]),
                    "pay_period_id": pay_period_id,
                    "date_from": date_from,
                    "date_to": date_to,
                    "user_id": user_id,
                })
        if payload:
            db.execute(text("""
                INSERT INTO pay_components_custom
                    (COMPONENT_ID, VALUE, EMPLOYEEID, STATUS, PAY_PERIOD_ID,
                     FROM_DATE, TO_DATE, updated_by)
                VALUES (:component_id, :value, :eb_id, 1, :pay_period_id,
                        :date_from, :date_to, :user_id)
            """), payload)
        db.commit()

        return {"data": {
            "message": (f"Posted {len(payload)} component values for "
                        f"{len(eb_ids)} employees"),
            "rows": len(payload),
            "employees": len(eb_ids),
            "pay_period_id": pay_period_id,
            "skipped_columns": sorted(set(skipped)),
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"njm_payroll_posting failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Main Wages Excel for PayRoll (legacy hol_get = 7) ──────────────

@router.get("/njm_wages_export")
def njm_wages_export(
    request: Request,
    date_from: str,
    date_to: str,
    pay_scheme_id: int,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Main Wages Excel for PayRoll: the main wages sheet (_sheet_sql) per
    employee, laid out as the payroll upload expects.

    Columns come from the same tbl_wages_data_payschm_map_table the posting
    uses, so the sheet and the posting can never drift apart. Headers are the
    pay component CODEs, as legacy wrote them — the payroll upload matches on
    those, not on the wages column names.
    """
    try:
        _validate_period(date_from, date_to)
        mapping = _posting_map(db, pay_scheme_id)
        if not mapping:
            raise HTTPException(
                status_code=400,
                detail=("No wages-to-payroll mapping is configured for this pay "
                        "scheme, so there are no columns to export."),
            )
        usable = [m for m in mapping if m["data_column_no"] in MAIN_WAGES_SHEET_COLUMNS]
        if not usable:
            raise HTTPException(status_code=400,
                                detail="No mapped column exists on the main wages sheet")

        # A component with no CODE (deleted master) falls back to the wages
        # column name so the value still shows; the upload will report it.
        header = [(m["data_column_no"], m["code"] or m["data_column_no"]) for m in usable]
        # Every mapped column is a number; a member with nothing collected
        # gets 0 on the sheet, not a blank, so the payroll upload reads it.
        data = [
            {"id": r["id"], "emp_code": r["emp_code"], "emp_name": r["emp_name"],
             "pay_scheme": r["pay_scheme"],
             **{code: 0 if r.get(col) is None else r.get(col) for col, code in header}}
            for r in _main_wages_sheet(db, date_from, date_to, pay_scheme_id)
        ]
        return {
            "data": data,
            "total": len(data),
            "columns": ["pay_scheme"] + [code for _, code in header],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_wages_export failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# All Wages Break Up Summary (legacy hol_get = 11) lives in
# src/hrms/njmWagesBreakup.py — it grew a second pivot per scheme family
# and a workbook writer, which is more than this module should carry.


# ─── Contractor ESI File Generation (legacy hol_get = 12) ───────────

@router.post("/njm_contractor_esi")
def njm_contractor_esi(
    request: Request,
    file: UploadFile = File(...),
    branch_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Contractor ESI return file.

    Takes the ESI portal's own template (IP Number, IP Name, and optionally the
    columns it wants back) and fills in this period's contractor ESI days and
    gross from the collected contractor wages, matched on ESI number.

    Reason code follows legacy: 0 where there is contributable wage, 11 (leave
    without pay) where there is none.

    Rows whose IP Number matches no employee come back in `unmatched` rather
    than being dropped, so the operator can see what the portal will reject.
    """
    try:
        _validate_period(date_from, date_to)
        rows = _rows_by_header(_read_sheet(file), "IP Number")

        computed = {
            str(r._mapping["esi_no"] or "").strip(): dict(r._mapping)
            for r in db.execute(text("""
                SELECT esi.esi_no,
                       od.emp_code,
                       COUNT(DISTINCT da.attendance_date) AS esi_days,
                       ROUND(SUM(c.gross), 0) AS esi_gross
                FROM hrms_ed_esi esi
                JOIN hrms_ed_official_details od
                  ON od.eb_id = esi.eb_id AND od.active = 1
                 AND od.catagory_id IN (SELECT cata_id FROM category_mst
                            WHERE cata_desc IN :contract_categories)
                LEFT JOIN daily_attendance da
                  ON da.eb_id = esi.eb_id
                 AND da.attendance_date BETWEEN :date_from AND :date_to
                 AND da.attendance_type IN ('R', 'O')
                 AND da.is_active = 1
                 AND da.branch_id = :branch_id
                LEFT JOIN (
                    SELECT eb_id,
                           SUM(COALESCE(prod_amount, 0) + COALESCE(reg_amount, 0)
                               + COALESCE(plus_amount, 0) - COALESCE(minus_amount, 0)) AS gross
                    FROM tbl_njm_cnt_wages_data_collection
                    WHERE start_date = :date_from AND end_date = :date_to
                      AND is_active = 1
                    GROUP BY eb_id
                ) c ON c.eb_id = esi.eb_id
                WHERE esi.active = 1 AND esi.esi_no IS NOT NULL
                GROUP BY esi.esi_no, od.emp_code
            """), {"date_from": date_from, "date_to": date_to,
                   "branch_id": branch_id,
                   "contract_categories": list(CONTRACT_CATEGORY_NAMES)}).fetchall()
        }

        data, unmatched = [], []
        for i, row in enumerate(rows):
            ip = str(row.get("IP Number") or "").strip()
            hit = computed.get(ip)
            if not hit:
                unmatched.append(ip)
                continue
            days = int(hit["esi_days"] or 0)
            gross = float(hit["esi_gross"] or 0)
            data.append({
                "id": i,
                "ip_number": ip,
                "ip_name": str(row.get("IP Name") or "").strip(),
                "emp_code": hit["emp_code"],
                "no_of_days": days,
                "total_wages": gross,
                # Legacy: 0 when there is contributable wage, else 11.
                "reason_code": 0 if gross > 0 else 11,
                "last_working_day": "",
            })
        return {
            "data": data,
            "total": len(data),
            "unmatched": sorted(set(unmatched)),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"njm_contractor_esi failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Main Wages Process (legacy hol_get = 5) ────────────────────────
#
# Ports every arm legacy njmwagesprocessdata actually executed, in its order:
# attendance, advance, leave, then the piece arms — beaming (BEAM), winding
# (WIND), finishing (FNG), weaving (WEAV) — then the loom line wage pool and the
# sardar/helper arm (SH) paid out of it. Legacy's standalone HELP/SARD inserts
# were commented out there and are not ported.
#
# Piece production comes from the jute production tables this tenant keeps
# (jute_prod_*_daily, the frozen jute_prod_weaving_log) and is priced through
# link_prodquality_wagequality_mst -> tbl_wages_quality_master.rate1, which is
# what replaced legacy's hardcoded quality codes. A production quality with no
# link row earns nothing, exactly like legacy's LEFT JOIN to a missing rate.
#
# Legacy scoped everything by company_id = 1 and cleared every arm except
# V/FA/OTH. This clears only the arms it writes (MAIN_WAGES_ARMS), so an arm
# added later cannot be wiped by a run that does not know how to rebuild it.

# Legacy `wm.cata_id in (15,16,17,20,21)` — resolved to the names those ids
# carry, so the same set is found whatever a company numbered them.
#
# 'NEW BUDLI RTD' is not in legacy's list but belongs here: a retired new-budli
# worker is paid exactly as a new-budli one (legacy's nbdlrtdwgsbrksummary is
# byte-identical to nbdlwgsbrksummary apart from the scheme id). Without it they
# are dropped from the run — including the ones sitting on njm_retired_payroll,
# which is a main-wages scheme by any reading.
#
# CONTRACT / OUTSIDER(CONT) / SWEEPER are deliberately absent: those sit on the
# contractor schemes and are paid by the Contractor Wages Process instead.
MAIN_WAGES_CATEGORY_NAMES = ("PERMENANT", "BUDLI", "NEW BUDLI", "NEW BUDLI RTD",
                             "RETIRED", "APPRENTICE")
# Excluded from the night-shift arm. Legacy's `NOT IN (1005,1114,1115)` were
# EXTRA SARDAR / LINE HELPER / LINE SARDAR — by NAME, like the sardar/helper
# arm below, because every branch numbers them differently (branch 4 has
# 1005/1114/1115, branch 87 has 1771/1880/1881) and legacy's raw ids excluded
# nobody at all on NJM.
NIGHT_EXCLUDED_DESIGNATIONS = ("EXTRA SARDAR", "LINE HELPER", "LINE SARDAR")
# Legacy $canteenrate, used only where canteen_details carries no rate.
DEFAULT_CANTEEN_RATE = 40
# Legacy grouped leave into SL ('S','C'), EL ('L') and UL ('A','U') code
# buckets. Gone: only PAYABLE leave transfers now, and it all books as
# el_days, so hrms_leave_types_mst.payable is the whole rule — see
# _MAIN_LEAVE_INSERT.

_MAIN_ATTENDANCE_INSERT = text("""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, hours_wkd_1, hours_wkd_2, piece_hours,
         extra_hours_t, extra_hours_p, c_shift_days, esi_days,
         is_active, dept_code, update_for, payscheme_id)
    SELECT g.eb_id, :date_from, :date_to, g.t1, g.t2, g.ph,
           g.ott, g.otp, g.ns, g.ed,
           1, g.dept_code, 'ATT', :pay_scheme_id
    FROM (
        -- SUM, not legacy's MAX. Legacy's worked_department_id WAS the
        -- department, so one row per (dept, time_piece, rate type, R/O) and
        -- MAX == SUM. Here worked_department_id is a SUB-department, so a
        -- weaver who ran WEAVING A 1 / B 1 / C 1 in the period contributes
        -- three rows to one dept_code and MAX kept only the biggest —
        -- silently dropping the rest of their piece hours, ESI days and
        -- night shifts. Anyone who rotates lines (weaving, winding, beaming)
        -- was short-paid.
        SELECT k.eb_id, k.dept_code,
               SUM(CASE WHEN k.time_piece = 'T' AND k.regular_ot = 'R'
                         AND k.piece_rate_type <> '2' THEN k.wkhrs ELSE 0 END) AS t1,
               SUM(CASE WHEN k.time_piece = 'T' AND k.regular_ot = 'R'
                         AND k.piece_rate_type = '2' THEN k.wkhrs ELSE 0 END) AS t2,
               SUM(CASE WHEN k.time_piece = 'P' AND k.regular_ot = 'R'
                        THEN k.wkhrs ELSE 0 END) AS ph,
               SUM(CASE WHEN k.time_piece = 'T' AND k.regular_ot = 'O'
                        THEN k.wkhrs ELSE 0 END) AS ott,
               SUM(CASE WHEN k.time_piece = 'P' AND k.regular_ot = 'O'
                        THEN k.wkhrs ELSE 0 END) AS otp,
               SUM(CASE WHEN k.time_piece = 'N' THEN k.wkhrs ELSE 0 END) AS ns,
               SUM(CASE WHEN k.time_piece = 'E' THEN k.wkhrs ELSE 0 END) AS ed
        FROM (
            SELECT v.eb_id, dm.dept_code, v.time_piece, v.piece_rate_type,
                   v.wkhrs, v.regular_ot
            FROM (
                -- Night-shift days: a 'C' spell on a piece designation, counted
                -- in shifts (hours / 8) rather than hours.
                -- The spell comes from spell_mst via spell_id, NOT from the
                -- legacy `daily_attendance.spell` name: that varchar is NULL on
                -- ~90% of this tenant's rows (16,077 of 17,879 in Aug-26), so
                -- matching it counted almost no night shifts at all. The rest
                -- of this module already joins on spell_id; `spell` is only a
                -- fallback for rows written before the spell_id backfill.
                SELECT n.worked_department_id, n.eb_id, 'N' AS time_piece,
                       '0' AS piece_rate_type, SUM(n.nshft) AS wkhrs, 'E' AS regular_ot
                FROM (
                    SELECT da.worked_department_id, da.eb_id, da.attendance_type,
                           da.attendance_date,
                           SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8 AS nshft
                    FROM daily_attendance da
                    LEFT JOIN designation_mst d
                           ON d.designation_id = da.worked_designation_id
                    LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
                    WHERE COALESCE(sp.spell_code, da.spell) = 'C'
                      AND da.attendance_date BETWEEN :date_from AND :date_to
                      AND d.time_piece = 'P'
                      AND da.is_active = 1 AND da.status_id = '3'
                      AND da.branch_id = :branch_id
                      AND d.desig NOT IN :night_excluded
                    GROUP BY da.worked_department_id, da.eb_id,
                             da.attendance_date, da.attendance_type
                ) n
                GROUP BY n.worked_department_id, n.eb_id, n.attendance_type
                UNION ALL
                -- ESI days: distinct dates actually worked on regular attendance.
                SELECT a.worked_department_id, a.eb_id, 'E', '0', COUNT(*), 'E'
                FROM (
                    SELECT DISTINCT da.attendance_date, da.eb_id,
                           da.worked_department_id
                    FROM daily_attendance da
                    WHERE da.attendance_date BETWEEN :date_from AND :date_to
                      AND da.branch_id = :branch_id
                      AND da.is_active = 1 AND da.status_id = '3'
                      AND da.attendance_type = 'R'
                ) a
                GROUP BY a.eb_id, a.worked_department_id
                UNION ALL
                -- Worked hours, split by the designation's time/piece flag.
                -- Legacy also subtracted daily_layoff_deptwise.layoff_hours here;
                -- that table does not exist in this tenant, so layoff is 0 and
                -- the hours are gross of any layoff adjustment.
                SELECT da.worked_department_id, da.eb_id,
                       -- This tenant stores 'Time'/'Piece' as well as 'T'/'P';
                       -- legacy compared to 'T' exactly and silently dropped the
                       -- long forms, losing those designations' hours.
                       LEFT(TRIM(COALESCE(d.time_piece, '')), 1),
                       COALESCE(d.piece_rate_type, '0'),
                       SUM(da.working_hours - COALESCE(da.idle_hours, 0)),
                       da.attendance_type
                FROM daily_attendance da
                JOIN designation_mst d
                     ON d.designation_id = da.worked_designation_id
                WHERE da.attendance_type IN ('R', 'O')
                  AND da.attendance_date BETWEEN :date_from AND :date_to
                  AND da.branch_id = :branch_id
                  AND da.is_active = 1 AND da.status_id = '3'
                GROUP BY da.worked_department_id, da.eb_id, d.time_piece,
                         d.piece_rate_type, da.attendance_type
            ) v
            JOIN hrms_ed_official_details od
              ON od.eb_id = v.eb_id AND od.active = 1
             AND od.catagory_id IN (SELECT cata_id FROM category_mst
                                    WHERE cata_desc IN :main_categories)
            LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = v.worked_department_id
            LEFT JOIN dept_mst dm ON dm.dept_id = sd.dept_id
        ) k
        GROUP BY k.eb_id, k.dept_code
    ) g
    -- DISTINCT, not a plain join: pay_employee_payscheme has only a PK, so an
    -- employee can carry two identical active rows for one scheme (the branch-87
    -- migration could insert the same pair twice). A plain join then fanned the
    -- aggregate out and wrote the SAME attendance row twice — eb 115157 in
    -- Aug-26. Every arm below joins the same way for the same reason.
    JOIN (SELECT DISTINCT EMPLOYEEID FROM pay_employee_payscheme
          WHERE STATUS = 1 AND PAY_SCHEME_ID = :pay_scheme_id) t
      ON t.EMPLOYEEID = g.eb_id
""").bindparams(
    bindparam("night_excluded", expanding=True),
    bindparam("main_categories", expanding=True),
)

# Advance instalments still outstanding. Legacy ran two variants and only the
# second survived (the first was overwritten before execute) — this is that one:
# an advance is recovered until what previous periods deducted, plus any
# adjustment, covers it.
_MAIN_ADVANCE_INSERT = text("""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, advance, is_active, update_for,
         payscheme_id, advance_date)
    SELECT a.eb_id, :date_from, :date_to, a.installment_amount, 1, 'IADV',
           :pay_scheme_id, a.advance_date
    FROM (
        SELECT tca.eb_id, tca.advance_amount, tca.advance_date,
               tca.installment_amount,
               tca.advance_amount
                 - COALESCE(d.advded, 0)
                 - COALESCE(tca.adj_trans_amount, 0) AS balamt
        FROM tbl_company_advance tca
        LEFT JOIN (
            SELECT w.eb_id, w.advance_date, SUM(w.advance) AS advded
            FROM tbl_njm_wages_data_collection w
            WHERE w.update_for = 'IADV'
              AND w.date_from < :date_from
              AND w.is_active = 1
              AND w.payscheme_id = :pay_scheme_id
            GROUP BY w.eb_id, w.advance_date
        ) d ON d.eb_id = tca.eb_id AND d.advance_date = tca.advance_date
        WHERE tca.advance_amount > 0
          AND tca.advance_amount - COALESCE(d.advded, 0) > 0
    ) a
    -- Only recover from someone who actually worked in the period.
    JOIN (
        SELECT da.eb_id
        FROM daily_attendance da
        WHERE da.attendance_date BETWEEN :date_from AND :date_to
          AND da.is_active = 1 AND da.status_id = '3'
          AND da.branch_id = :branch_id
        GROUP BY da.eb_id
    ) w ON w.eb_id = a.eb_id
    JOIN (SELECT DISTINCT EMPLOYEEID FROM pay_employee_payscheme
          WHERE STATUS = 1 AND PAY_SCHEME_ID = :pay_scheme_id) t
      ON t.EMPLOYEEID = a.eb_id
    WHERE a.balamt > 0
""")

# Holidays, leave and canteen, pivoted per employee. esi_days follows legacy:
# festival hours / 8 plus earned-leave days.
_MAIN_LEAVE_INSERT = text("""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, festival_hours, el_days,
         canteen, is_active, dept_code, update_for, payscheme_id, esi_days)
    SELECT g.eb_id, :date_from, :date_to, g.fl, g.el,
           g.ml, 1, dm.dept_code, 'LEAVE', :pay_scheme_id,
           g.fl / 8 + g.el
    FROM (
        SELECT v.eb_id,
               MAX(CASE WHEN v.lv_type = 'FL' THEN v.mhrs ELSE 0 END) AS fl,
               MAX(CASE WHEN v.lv_type = 'EL' THEN v.msld ELSE 0 END) AS el,
               MAX(CASE WHEN v.lv_type = 'ML' THEN v.msld ELSE 0 END) AS ml
        FROM (
            SELECT h.eb_id, 'FL' AS lv_type,
                   COALESCE(SUM(h.holiday_hours), 0) AS mhrs, 0 AS msld
            FROM (
                SELECT t.eb_id, hm.holiday_date, t.holiday_hours
                FROM hrms_holiday_transactions t
                JOIN holiday_master hm ON hm.holiday_id = t.holiday_id
                WHERE hm.holiday_date BETWEEN :date_from AND :date_to
                  AND hm.branch_id = :branch_id
                  AND t.is_active = 1
                GROUP BY t.eb_id, hm.holiday_date, t.holiday_hours
            ) h
            GROUP BY h.eb_id
            UNION ALL
            -- The ONLY leave arm: payable days, booked as el_days. Leave that
            -- is not payable — sick, casual, unpaid, anything else — is not
            -- transferred at all, so sl_days / cl_days / ul_days are left
            -- untouched by this run, exactly as asked. EL is defined by
            -- hrms_leave_types_mst.payable, not by a code list, the way EJM's
            -- STL arm does it: a new paid type pays without an edit here.
            --
            -- vw_leave_dates, like the Cad/Vard arm: it expands each
            -- transaction's from/to range, carries payable and branch_id
            -- itself, and already filters status = 3 — so a request whose
            -- leave_tran_details rows were never written (or written stale)
            -- still pays. DISTINCT on (eb_id, leave_date) because the view
            -- LEFT JOINs hrms_ed_official_details, which has duplicate eb_id
            -- rows: a plain COUNT(*) would bill the same day twice.
            SELECT v.eb_id, 'EL', 0, COUNT(*)
            FROM (SELECT DISTINCT eb_id, leave_date FROM vw_leave_dates
                  WHERE leave_date BETWEEN :date_from AND :date_to
                    AND branch_id = :branch_id
                    AND payable = 'Y') v
            GROUP BY v.eb_id
            UNION ALL
            -- Canteen recovery. Legacy multiplied by a hardcoded 40; this
            -- tenant stores the rate per row, so use it and fall back to 40.
            SELECT cd.eb_id, 'ML', 0,
                   SUM(cd.no_of_meals * COALESCE(cd.rate_of_meals, :canteen_rate))
            FROM canteen_details cd
            WHERE cd.tran_date BETWEEN :date_from AND :date_to
              AND cd.branch_id = :branch_id
              AND cd.status_id = :canteen_approved
            GROUP BY cd.eb_id
        ) v
        GROUP BY v.eb_id
    ) g
    JOIN hrms_ed_official_details od
      ON od.eb_id = g.eb_id AND od.active = 1
     AND od.catagory_id IN (SELECT cata_id FROM category_mst
                            WHERE cata_desc IN :main_categories)
    LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst dm ON dm.dept_id = sd.dept_id
    JOIN (SELECT DISTINCT EMPLOYEEID FROM pay_employee_payscheme
          WHERE STATUS = 1 AND PAY_SCHEME_ID = :pay_scheme_id) t
      ON t.EMPLOYEEID = g.eb_id
""").bindparams(
    bindparam("main_categories", expanding=True),
)

# ─── piece arms ─────────────────────────────────────────────────────

# Production quality -> rate card, per department type. The department's type
# is read off its name with the same needles the link master uses. A type can
# carry several needles (finishing = FINISH + SACK), so the value is a REGEXP
# alternation matched with REGEXP, not LIKE — a dict-comp keyed by qtype used to
# keep only the LAST needle ("%SACK%"), which mispriced every FINISHING(LC)
# department at the fallback rate.
_NEEDLES_BY_TYPE: dict[str, list[str]] = {}
for _needle, _qtype in QUALITY_TYPES:
    _NEEDLES_BY_TYPE.setdefault(_qtype, []).append(_needle)
LINK_DEPT_NEEDLE = {qt: "|".join(ns) for qt, ns in _NEEDLES_BY_TYPE.items()}

# Legacy priced every cop item on q_code 6501 at net_weight / 15 (a bundle of
# cops is 15 kg; the card's unit is BDL). The link decides which cop card
# applies; a winding card whose unit is BDL decides the /15 (6501-6503 today).
# Spool and everything else is priced as entered: amount = production /
# curr_rate_pu x rate1 (any other `unit` is a label, not a divisor — 5401 says
# '0.1' and still pays 2802 kg x 0.1264). Winding only: the finishing 78xx
# cards also say BDL and are priced as entered.
BUNDLE_UNIT = "BDL"
COP_KG_PER_BUNDLE = 15
# rate1 is per this many production units; NULL/0 on a card means 1.
_RATE_PU = "COALESCE(NULLIF(q.curr_rate_pu, 0), 1)"

# Loom line wage pool (legacy's line quantity/amount update): every priced
# loom-spell adds production x BASE_WAGE / (2 x 8-hour target at TARGET_EFF).
# The target is speed x 480 min / (36 x picks) — the log's std_prod_yds formula
# for an 8-hour spell. Both numbers were hardcoded in legacy (46.15, .65).
LINE_BASE_WAGE = 46.15
LINE_TARGET_EFF = 0.65
# A sardar draws 1.3x the helper's share of the pool (legacy *1.3 / *1).
SARDAR_RATE_FACTOR = 1.3
HELPER_RATE_FACTOR = 1.0
# Legacy designation ids 1114 (helper) and 1115/1005 (sardar), by NAME — sls
# branch 87 numbers them 1880/1881. Spinning's 'LINE SARDAR (F)' / '(C)' are
# excluded by the exact match and by the weaving-department gate below.
LINE_HELPER_DESIGNATIONS = ("LINE HELPER",)
LINE_SARDAR_DESIGNATIONS = ("LINE SARDAR",)

# Rate card joined through the link master for one department type. Binds
# :branch_id and :dept_needle.
_LINK_RATES = f"""(
    SELECT l.prod_quality_id, q.q_code, q.rate1, {_RATE_PU} AS rate_pu,
           UPPER(TRIM(q.unit)) AS unit
    FROM link_prodquality_wagequality_mst l
    JOIN dept_mst ld ON ld.dept_id = l.dept_id
                    AND UPPER(ld.dept_desc) REGEXP :dept_needle
    JOIN tbl_wages_quality_master q
      ON q.q_code_id = l.wages_quality_id AND q.is_active = 1
    WHERE l.active = 1 AND l.branch_id = :branch_id
)"""

# Every piece arm ends the same way: only people on the selected scheme.
_SCHEME_JOIN = """
    JOIN (SELECT DISTINCT EMPLOYEEID FROM pay_employee_payscheme
          WHERE STATUS = 1 AND PAY_SCHEME_ID = :pay_scheme_id) t
      ON t.EMPLOYEEID = g.eb_id
"""

# Present on the day and spell, on regular or overtime attendance — legacy's
# gate on every piece arm. DISTINCT so an R and an O row for the same spell
# cannot double a production row.
_PRESENT = """(
    SELECT DISTINCT da.eb_id, da.attendance_date, da.spell_id
    FROM daily_attendance da
    WHERE da.is_active = 1 AND da.status_id = '3' AND da.branch_id = :branch_id
      AND da.attendance_date BETWEEN :date_from AND :date_to
      AND da.attendance_type IN ('R', 'O')
)"""

# Crew letter of a weaving sub-department: legacy's
# replace(right(dept_desc, 3), ' ', '') -> 'A1' -> 'A'. The tenant kept the
# same names ('WEAVING A 1' ... 'WEAVING C 5'), and the line-hours sheet's
# wgroup is this letter.
_CREW = "LEFT(REPLACE(RIGHT(sd.sub_dept_desc, 3), ' ', ''), 1)"

# Beaming: the production row names the machine, not the beamer, so everyone
# marked on that machine for the spell is paid the machine's cuts — legacy's
# join, and how a beaming crew is paid.
_MAIN_BEAM_INSERT = text(f"""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, act_prod_amount, is_active, update_for,
         payscheme_id, prod_balance, qualcode, qual_rate, production)
    SELECT g.eb_id, :date_from, :date_to, ROUND(g.cuts / g.rate_pu * g.rate1, 2), 1, 'BEAM',
           :pay_scheme_id, g.cuts, g.q_code, g.rate1, g.cuts
    FROM (
        SELECT k.eb_id, lk.q_code, lk.rate1, lk.rate_pu, SUM(bm.cuts) AS cuts
        FROM (
            SELECT bd.tran_date, bd.spell_id, bd.machine_id, bd.bm_quality_id,
                   SUM(bd.act_cuts) AS cuts
            FROM jute_prod_beaming_daily bd
            WHERE bd.active = 1 AND bd.branch_id = :branch_id
              AND bd.tran_date BETWEEN :date_from AND :date_to
            GROUP BY bd.tran_date, bd.spell_id, bd.machine_id, bd.bm_quality_id
        ) bm
        JOIN (
            SELECT DISTINCT dea.mc_id, da.attendance_date, da.spell_id, da.eb_id
            FROM daily_ebmc_attendance dea
            JOIN daily_attendance da ON da.daily_atten_id = dea.daily_atten_id
            WHERE dea.is_active = 1 AND da.is_active = 1 AND da.status_id = '3'
              AND da.branch_id = :branch_id
              AND da.attendance_date BETWEEN :date_from AND :date_to
              AND da.attendance_type IN ('R', 'O')
        ) k ON k.mc_id = bm.machine_id AND k.attendance_date = bm.tran_date
           AND k.spell_id = bm.spell_id
        JOIN {_LINK_RATES} lk ON lk.prod_quality_id = bm.bm_quality_id
        GROUP BY k.eb_id, lk.q_code, lk.rate1, lk.rate_pu
    ) g
    {_SCHEME_JOIN}
""")

# Winding: the doff names the winder; the yarn item is the production quality.
_MAIN_WIND_INSERT = text(f"""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, act_prod_amount, is_active, update_for,
         payscheme_id, prod_balance, qualcode, qual_rate, production)
    SELECT g.eb_id, :date_from, :date_to, ROUND(g.prod / g.rate_pu * g.rate1, 2), 1, 'WIND',
           :pay_scheme_id, g.prod, g.q_code, g.rate1, g.prod
    FROM (
        SELECT wd.eb_id, lk.q_code, lk.rate1, lk.rate_pu,
               ROUND(SUM(CASE WHEN lk.unit = :bundle_unit
                              THEN wd.production_qty / :cop_kg_per_bundle
                              ELSE wd.production_qty END), 2) AS prod
        FROM jute_prod_winding_doff wd
        JOIN {_PRESENT} p ON p.eb_id = wd.eb_id AND p.attendance_date = wd.tran_date
                          AND p.spell_id = wd.spell_id
        JOIN {_LINK_RATES} lk ON lk.prod_quality_id = wd.item_id
        WHERE wd.active = 1 AND wd.branch_id = :branch_id
          AND wd.tran_date BETWEEN :date_from AND :date_to
        GROUP BY wd.eb_id, lk.q_code, lk.rate1, lk.rate_pu
    ) g
    {_SCHEME_JOIN}
""")

# Finishing: priced by the finishing quality's link where one exists, else by
# the process token — the same map the contractor arm prices on.
_FNG_PROCESS_CASE = " ".join(
    f"WHEN '{process}' THEN '{q_code}'"
    for process, q_code in CNT_PIECE_PROCESS_QCODE.items()
)
_MAIN_FNG_INSERT = text(f"""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, act_prod_amount, is_active, update_for,
         payscheme_id, prod_balance, qualcode, qual_rate, production)
    SELECT g.eb_id, :date_from, :date_to, ROUND(g.prod / g.rate_pu * g.rate1, 2), 1, 'FNG',
           :pay_scheme_id, g.prod, g.q_code, g.rate1, g.prod
    FROM (
        SELECT fd.eb_id, q.q_code, q.rate1, {_RATE_PU} AS rate_pu,
               SUM(fd.prod_qty) AS prod
        FROM jute_prod_finishing_daily fd
        JOIN {_PRESENT} p ON p.eb_id = fd.eb_id AND p.attendance_date = fd.tran_date
                          AND p.spell_id = fd.spell_id
        LEFT JOIN {_LINK_RATES} lk ON lk.prod_quality_id = fd.finishing_quality_id
        JOIN tbl_wages_quality_master q
          ON q.q_code = COALESCE(lk.q_code, CASE fd.process {_FNG_PROCESS_CASE} END)
         AND q.branch_id = :branch_id AND q.is_active = 1
        WHERE fd.active = 1 AND fd.branch_id = :branch_id
          AND fd.tran_date BETWEEN :date_from AND :date_to
        GROUP BY fd.eb_id, q.q_code, q.rate1, q.curr_rate_pu
    ) g
    {_SCHEME_JOIN}
""")

# Weaving: the frozen log is what wages are paid on (Process stamps the weaver
# and freezes the yards). An unprocessed spell has no log row and earns nothing
# until it is processed — the same gate the loom checklist applies.
_MAIN_WEAV_INSERT = text(f"""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, act_prod_amount, is_active, loom_production,
         update_for, payscheme_id, qualcode, qual_rate, production)
    SELECT g.eb_id, :date_from, :date_to, ROUND(g.prod / g.rate_pu * g.rate1, 2), 1, ROUND(g.prod),
           'WEAV', :pay_scheme_id, g.q_code, g.rate1, g.prod
    FROM (
        SELECT wl.eb_id, lk.q_code, lk.rate1, lk.rate_pu, SUM(wl.production_yds) AS prod
        FROM jute_prod_weaving_log wl
        JOIN {_LINK_RATES} lk ON lk.prod_quality_id = wl.weaving_quality_id
        WHERE wl.active = 1 AND wl.branch_id = :branch_id
          AND wl.tran_date BETWEEN :date_from AND :date_to
          AND wl.eb_id IS NOT NULL
        GROUP BY wl.eb_id, lk.q_code, lk.rate1, lk.rate_pu
    ) g
    {_SCHEME_JOIN}
""")

# Loom line wage pool -> tbl_run_loom_line_hours.quantity / pamount, per line
# and crew. Quantity counts every loom-spell; the amount only those with a
# speed and a pick standard (legacy's NULL target dropped out of its SUM the
# same way). The crew is the weaver's worked sub-department that spell.
# Cleared first so a line that lost its production on a rerun cannot keep a
# stale pool — legacy left the old numbers standing.
_LINE_POOL_RESET = text("""
    UPDATE tbl_run_loom_line_hours
    SET quantity = NULL, pamount = NULL
    WHERE date_from = :date_from AND date_to = :date_to
""")
_LINE_POOL_UPDATE = text(f"""
    UPDATE tbl_run_loom_line_hours lh
    JOIN (
        SELECT wl.line_no, {_CREW} AS crew,
               SUM(wl.production_yds) AS qty,
               SUM(CASE WHEN wl.eff_speed > 0 AND wl.std_picks > 0
                        THEN wl.production_yds * :base_wage
                             / (2 * :target_eff * wl.eff_speed * 480 / (36 * wl.std_picks))
                        END) AS pamount
        FROM jute_prod_weaving_log wl
        JOIN (
            SELECT da.eb_id, da.attendance_date, da.spell_id,
                   MAX(da.worked_department_id) AS worked_department_id
            FROM daily_attendance da
            WHERE da.is_active = 1 AND da.status_id = '3' AND da.branch_id = :branch_id
              AND da.attendance_date BETWEEN :date_from AND :date_to
              AND da.attendance_type IN ('R', 'O')
            GROUP BY da.eb_id, da.attendance_date, da.spell_id
        ) da ON da.eb_id = wl.eb_id AND da.attendance_date = wl.tran_date
            AND da.spell_id = wl.spell_id
        JOIN sub_dept_mst sd ON sd.sub_dept_id = da.worked_department_id
        WHERE wl.active = 1 AND wl.branch_id = :branch_id
          AND wl.tran_date BETWEEN :date_from AND :date_to
        GROUP BY wl.line_no, crew
    ) p ON p.line_no = lh.line_no AND p.crew = lh.wgroup
    SET lh.quantity = ROUND(p.qty), lh.pamount = ROUND(p.pamount, 2)
    WHERE lh.date_from = :date_from AND lh.date_to = :date_to
""")

# Sardar/helper: hours on the line x the line's pool rate. The line is the
# mech_shr_code of the pseudo-machine they are marked on ('SARDARLINE16',
# 'HELPER LINE 16' -> '16'), legacy's mach_shr_code. Rate = pool /
# (net running hours x looms) x 2, x1.3 for a sardar. A line with no priced
# pool leaves the amount NULL, as legacy's LEFT JOIN did — the hours still
# show on the checklist. Legacy divided by a hardcoded 30 looms; the sheet's
# no_of_looms is used instead (30 on every row uploaded so far).
_MAIN_SH_INSERT = text(f"""
    INSERT INTO tbl_njm_wages_data_collection
        (eb_id, date_from, date_to, sardhelp_amt, is_active, update_for,
         payscheme_id, lineno, qual_rate, linehrs)
    SELECT g.eb_id, :date_from, :date_to, ROUND(g.rate * g.wrkhrs, 6), 1, 'SH',
           :pay_scheme_id, CONCAT(g.sard_help, g.line_no), g.rate, g.wrkhrs
    FROM (
        SELECT a.eb_id, a.line_no, a.sard_help, a.wrkhrs,
               r.base_rate * CASE WHEN a.sard_help = 'S'
                                  THEN :sardar_factor ELSE :helper_factor END AS rate
        FROM (
            SELECT da.eb_id, m.mech_shr_code AS line_no,
                   CASE WHEN dg.desig IN :helper_designations THEN 'H' ELSE 'S' END AS sard_help,
                   {_CREW} AS crew,
                   SUM(da.working_hours - COALESCE(da.idle_hours, 0)) AS wrkhrs
            FROM daily_ebmc_attendance dea
            JOIN daily_attendance da ON da.daily_atten_id = dea.daily_atten_id
            JOIN designation_mst dg ON dg.designation_id = da.worked_designation_id
                                   AND dg.desig IN :line_designations
            JOIN machine_mst m ON m.machine_id = dea.mc_id
            JOIN sub_dept_mst sd ON sd.sub_dept_id = da.worked_department_id
            JOIN dept_mst dm ON dm.dept_id = sd.dept_id
                            AND UPPER(dm.dept_desc) REGEXP :weaving_needle
            WHERE dea.is_active = 1 AND da.is_active = 1 AND da.status_id = '3'
              AND da.branch_id = :branch_id
              AND da.attendance_date BETWEEN :date_from AND :date_to
              AND da.attendance_type IN ('R', 'O')
            GROUP BY da.eb_id, m.mech_shr_code, sard_help, crew
        ) a
        LEFT JOIN (
            SELECT lh.line_no, lh.wgroup, lh.sard_helper,
                   lh.pamount
                     / NULLIF((lh.running_hours - lh.lost_hours) * lh.no_of_looms, 0)
                     * 2 AS base_rate
            FROM tbl_run_loom_line_hours lh
            WHERE lh.date_from = :date_from AND lh.date_to = :date_to
        ) r ON r.line_no = a.line_no AND r.wgroup = a.crew
           AND r.sard_helper = a.sard_help
    ) g
    {_SCHEME_JOIN}
""").bindparams(
    bindparam("helper_designations", expanding=True),
    bindparam("line_designations", expanding=True),
)

# The arms this process owns. A run rebuilds exactly these and nothing else.
MAIN_WAGES_ARMS = ("ATT", "IADV", "LEAVE", "BEAM", "WIND", "FNG", "WEAV", "SH")


def _main_wages_steps(params: dict) -> list[tuple[str, object, dict]]:
    """(label, statement, binds) in legacy's order. One list so the endpoint
    and the test that proves every bind is supplied read the same thing."""
    return [
        ("attendance", _MAIN_ATTENDANCE_INSERT, {
            **params,
            "night_excluded": list(NIGHT_EXCLUDED_DESIGNATIONS),
            "main_categories": list(MAIN_WAGES_CATEGORY_NAMES),
        }),
        ("advance", _MAIN_ADVANCE_INSERT, params),
        ("leave", _MAIN_LEAVE_INSERT, {
            **params,
            "canteen_rate": DEFAULT_CANTEEN_RATE,
            "canteen_approved": CANTEEN_APPROVED,
            "main_categories": list(MAIN_WAGES_CATEGORY_NAMES),
        }),
        ("beaming", _MAIN_BEAM_INSERT,
         {**params, "dept_needle": LINK_DEPT_NEEDLE["beaming"]}),
        ("winding", _MAIN_WIND_INSERT, {
            **params, "dept_needle": LINK_DEPT_NEEDLE["winding"],
            "bundle_unit": BUNDLE_UNIT,
            "cop_kg_per_bundle": COP_KG_PER_BUNDLE,
        }),
        ("finishing", _MAIN_FNG_INSERT,
         {**params, "dept_needle": LINK_DEPT_NEEDLE["finishing"]}),
        ("weaving", _MAIN_WEAV_INSERT,
         {**params, "dept_needle": LINK_DEPT_NEEDLE["weaving"]}),
        ("line pool reset", _LINE_POOL_RESET, params),
        ("line pools", _LINE_POOL_UPDATE,
         {**params, "base_wage": LINE_BASE_WAGE, "target_eff": LINE_TARGET_EFF}),
        ("sardar/helper", _MAIN_SH_INSERT, {
            **params,
            "line_designations": list(LINE_SARDAR_DESIGNATIONS + LINE_HELPER_DESIGNATIONS),
            "helper_designations": list(LINE_HELPER_DESIGNATIONS),
            "weaving_needle": LINK_DEPT_NEEDLE["weaving"],
            "sardar_factor": SARDAR_RATE_FACTOR,
            "helper_factor": HELPER_RATE_FACTOR,
        }),
    ]


_UNAPPROVED_SOURCES_SQL = text("""
    SELECT
      (SELECT COUNT(*) FROM daily_attendance da
       WHERE da.attendance_date BETWEEN :date_from AND :date_to
         AND da.branch_id = :branch_id AND da.is_active = 1
         AND COALESCE(CAST(da.status_id AS UNSIGNED), 0) <> :approved) AS attendance,
      (SELECT COUNT(DISTINCT lt.leave_transaction_id)
       FROM leave_tran_details ltd
       JOIN leave_transactions lt ON lt.leave_transaction_id = ltd.ltran_id
       WHERE ltd.leave_date BETWEEN :date_from AND :date_to
         AND lt.branch_id = :branch_id AND ltd.is_active = 1
         AND COALESCE(CAST(lt.status AS UNSIGNED), 0) <> :approved) AS leave_count,
      -- canteen_details has no active flag; rejected/cancelled are simply skipped.
      (SELECT COUNT(*) FROM canteen_details cd
       WHERE cd.tran_date BETWEEN :date_from AND :date_to
         AND cd.branch_id = :branch_id
         AND COALESCE(cd.status_id, 0) NOT IN (:approved, :rejected, :cancelled)) AS canteen
""")


def _check_sources_approved(db, params: dict) -> None:
    """Refuse to process while any active attendance, leave or canteen entry in
    the period is not yet approved (status 3)."""
    r = db.execute(_UNAPPROVED_SOURCES_SQL, {
        **params, "approved": LEAVE_APPROVED,
        "rejected": CANTEEN_REJECTED, "cancelled": CANTEEN_CANCELLED,
    }).one()
    pending = [f"{label} {n}" for label, n in
               (("attendance", r.attendance), ("leave", r.leave_count), ("canteen", r.canteen))
               if n]
    if pending:
        raise HTTPException(status_code=400, detail=(
            "Unapproved entries in the period — " + ", ".join(pending)
            + ". Approve them before running the wages process."))


@router.post("/njm_wages_process")
def njm_wages_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Main Wages Process (legacy hol_get = 5).

    Rebuilds the arms of tbl_njm_wages_data_collection for the period and pay
    scheme, in legacy njmwagesprocessdata's order:
      ATT    worked hours split time/piece and regular/OT, night shifts, ESI days
      IADV   the next instalment of every advance still outstanding
      LEAVE  holiday hours, sick/earned/unpaid leave days, canteen recovery
      BEAM   cuts x rate for everyone marked on the beaming machine that spell
      WIND   doffed kg (cops as 15 kg bundles) x rate per winder and yarn item
      FNG    finishing pieces x rate per worker and quality/process
      WEAV   frozen loom yards x rate per weaver and weaving quality
      then the loom line wage pools (tbl_run_loom_line_hours quantity/pamount)
      SH     sardar/helper hours on a line x that line's pool rate

    Manual uploads (V, FA, OTH) are never touched.

    Body: { branch_id, date_from, date_to, pay_scheme_id }
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        pay_scheme_id = body.get("pay_scheme_id")
        if not branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")
        if not pay_scheme_id:
            raise HTTPException(status_code=400, detail="pay_scheme_id is required")
        _validate_period(date_from, date_to)

        params = {
            "date_from": date_from, "date_to": date_to,
            "branch_id": int(branch_id), "pay_scheme_id": int(pay_scheme_id),
        }
        _check_sources_approved(db, params)

        # Retire only this process's own arms.
        db.execute(text("""
            UPDATE tbl_njm_wages_data_collection SET is_active = 0
            WHERE date_from = :date_from AND date_to = :date_to
              AND payscheme_id = :pay_scheme_id AND is_active = 1
              AND update_for IN :arms
        """).bindparams(bindparam("arms", expanding=True)),
            {**params, "arms": list(MAIN_WAGES_ARMS)})

        counts = {
            label: db.execute(stmt, binds).rowcount or 0
            for label, stmt, binds in _main_wages_steps(params)
        }
        db.commit()

        # The reset is housekeeping, not a result.
        counts.pop("line pool reset", None)
        return {"data": {
            "message": "Main wages built — " + ", ".join(
                f"{label} {n}" for label, n in counts.items()),
            "rows": sum(n for label, n in counts.items() if label != "line pools"),
            **counts,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"njm_wages_process failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
