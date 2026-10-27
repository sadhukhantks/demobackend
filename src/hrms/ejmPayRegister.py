"""EJM Pay Register / Pay Slip and OT Register — the two report selects on the
EJM Wages Data Preparation screen.

Ports the "Register" grids of the legacy Data-Portal-Php screens
``pay_register_payslip.php`` (Data_entry::getpayregisterdata) and
``ot_register_payslip.php`` (Data_entry::getotregisterdata). Every legacy
report is the same shape — pay_employee_payroll pivoted by component id for
one pay period (or summed over the periods in a date range), one row per
employee, plus a grand total — so one pivot builder serves them all and each
report is just its column list and its row filter.

Legacy "Print" was a fixed-width dot-matrix .txt of the same rows; the screen
exports the grid to Excel instead. Pay slips and the bank sheet reuse the
existing /hrms/pay_register_payslips and /pay_register_bank_sheet endpoints,
keyed by the pay period this module resolves.
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db

router = APIRouter()

# ─── Column specs: (field, header, component_id) ────────────────────
# Component ids are the legacy tbl_pay_components ids, identical in migrated
# tenants (see reportQueries.get_pay_register_print_query).

# Legacy hol_get 1/2/7 — the full Main Pay Roll register.
MAIN_PAYROLL = [
    ("basic_rate", "Basic Rate", 238), ("working_hours", "Work Hrs", 178),
    ("ns_hrs", "NS Hrs", 179), ("hl_hrs", "Hol Hrs", 180), ("ls_hrs", "Lost Hrs", 182),
    ("stl_d", "STL Days", 183), ("wrk_days", "Work Days", 198),
    ("prod_basic", "Prod Basic", 251), ("fix_basic", "Fix Basic", 216),
    ("time_rated_basic", "Time Basic", 189), ("ns_amount", "NS Amt", 171),
    ("hol_amt", "Hol Amt", 109), ("increment_amt", "Increment", 217),
    ("layoff_wgs", "Layoff Wgs", 221), ("incentive_amount", "Incentive", 219),
    ("da", "DA", 212), ("stl_wgs", "STL Wgs", 112), ("pf_gross", "PF Gross", 134),
    ("gross_pay", "Gross Pay", 20), ("hra", "HRA", 8), ("misc_earn", "Misc Earn", 184),
    ("total_earn", "Total Earn", 224), ("c_f", "C/F", 236), ("epf", "EPF", 18),
    ("esi_gross", "ESI Gross", 149), ("esic", "ESIC", 19), ("lwf", "LWF", 235),
    ("p_tax", "P.Tax", 16), ("puja_advance", "Puja Adv", 186),
    ("stl_advance", "STL Adv", 187), ("total_deduction", "Total Ded", 25),
    ("b_f", "B/F", 223), ("net_pay", "Net Pay", 21), ("c_work_day", "C Work Day", 206),
    ("c_bon_ern", "C Bonus Earn", 207),
]

# Legacy hol_get 8 — Pay Image Comparative Statement (100% vs actual).
COMPARATIVE = [
    ("pf_gross100", "PF Gross 100%", 239), ("pf_gross", "PF Gross", 134),
    ("gross_pay100", "Gross 100%", 243), ("gross_pay", "Gross Pay", 20),
    ("net100", "Net 100%", 247), ("net_pay", "Net Pay", 21),
]

# Legacy hol_get 3 — Mill Retired.
MILL_RETIRED = [
    ("working_hrs", "Work Hrs", 178), ("ns_hrs", "NS Hrs", 179), ("hol_hrs", "Hol Hrs", 102),
    ("rate_per_day", "Rate/Day", 70), ("basic", "Basic", 7), ("hol_wgs", "Hol Wgs", 109),
    ("arrear_plus", "Arrear +", 268), ("arrear_minus", "Arrear -", 269),
    ("total_earn", "Total Earn", 224), ("esi", "ESI", 19), ("exadvance", "Advance", 166),
    ("netpay", "Net Pay", 21),
]

# Legacy hol_get 4/5 — Card Holder Bank / Cash.
CARD_HOLDER = [
    ("working_hours", "Work Hrs", 178), ("holiday_hr", "Fest Hrs", 102),
    ("ot_hours", "OT Hrs", 135), ("rate_per_day", "Rate/Day", 70), ("basic", "Basic", 7),
    ("festival_wage", "Fest Wage", 109), ("overtime_pay", "OT Pay", 237),
    ("advance", "Advance", 166), ("arrear_plus", "Arrear +", 268),
    ("arrear_minus", "Arrear -", 269), ("net_payable", "Net Pay", 21),
]

# Legacy hol_get 6 (one period) and 16 (summed over a date range) — Trainee.
TRAINEE = [
    ("working_hours", "Work Hrs", 178), ("night_shift_hr", "NS Hrs", 179),
    ("holiday_hr", "Fest Hrs", 102), ("ot_hours", "OT Hrs", 135),
    ("rate_per_day", "Rate/Day", 70), ("basic", "Basic", 7),
    ("tiffin_amount", "Tiffin", 270), ("washing_allowance", "Washing", 133),
    ("other_allowance", "Other", 9), ("conv_allowance", "Conveyance", 72),
    ("festival_wage", "Fest Wage", 109), ("gross2", "Gross2", 66), ("epf", "EPF", 18),
    ("esi", "ESI", 19), ("advance", "Advance", 166), ("arrear_plus", "Arrear +", 268),
    ("arrear_minus", "Arrear -", 269), ("total_earn", "Total Earn", 224),
    ("gross_ded", "Gross Ded", 25), ("b_f", "R/Off", 223), ("net_payable", "Net Pay", 21),
    ("empl_epf", "Employer EPF", 22), ("empl_esi", "Employer ESI", 23),
]

# Legacy hol_get 30 — Periodical Main Payroll & 18-PF (Loan_adv_model2
# getmainvcpayrollexceldrg), summed over a date range.
MAIN_PAYROLL_RANGE = [
    ("working_hours", "Work Hrs", 178), ("hl_hrs", "Hol Hrs", 180), ("ns_hrs", "NS Hrs", 179),
    ("stl_d", "STL Days", 183), ("time_rated_basic", "Time Basic", 189),
    ("prod_basic", "Prod Basic", 251), ("da", "DA", 212), ("hol_amt", "Hol Amt", 109),
    ("ns_amount", "NS Amt", 171), ("hra", "HRA", 8), ("stl_wgs", "STL Wgs", 112),
    ("pf_gross", "PF Gross", 134), ("epf", "EPF", 18), ("esi_gross", "ESI Gross", 149),
    ("esic", "ESIC", 19), ("p_tax", "P.Tax", 16), ("advance", "Advance", 166),
    ("total_earning", "Total Earn", 224), ("gross_ded", "Gross Ded", 25),
    ("net_pay", "Net Pay", 21), ("ot_hours", "OT Hrs", 135), ("ot_advance", "OT Adv", 284),
    ("overtime_pay", "OT Pay", 237), ("incentive_amount", "Att Incentive", 248),
]

# Legacy Loan_adv_model::otregisterprint — one row per employee with OT.
OT_REGISTER = [
    ("rate", "Rate", 70), ("ot_hours", "OT Hrs", 135), ("overtime_pay", "OT Amount", 237),
    ("ot_advance", "OT Advance", 284), ("misc_ot_earn", "Misc OT Earn", 285),
    ("ot_net_pay", "OT Net Pay", 286),
]

# Columns only the dot-matrix prints (ejmTextPrint) need, on top of the
# grid's list for the same report code.
PRINT_EXTRA: dict[int, list] = {
    **dict.fromkeys((1, 2, 7), [
        ("co_loan", "CO Loan", 185), ("co_loan_bal", "CO Loan Bal", 222),
        ("c_pf_cont", "C PF Cont", 231), ("c_fpf_cont", "C FPF Cont", 232),
        ("co_cont", "CO Cont", 233), ("attincn", "Att Incentive", 248),
        ("otpay", "OT Pay", 237), ("otadv", "OT Adv", 284), ("otnet", "OT Net", 286),
        ("basic_amount", "Basic", 7), ("exadvance", "Advance", 166),
        ("arrear_plus", "Arrear +", 268), ("arrear_minus", "Arrear -", 269),
    ]),
    6: [("overtime_pay", "OT Pay", 237)],
    8: [("pf100", "PF 100%", 245), ("epf", "EPF", 18),
        ("total100", "Total 100%", 244), ("total_deduction", "Total Ded", 25)],
    3: [("miss_ot_earning", "Misc OT Earn", 285)],
}

IDENTITY = [
    ("eb_no", "EB No"), ("wname", "Employee"), ("dept_code", "Dept"),
    ("dept_desc", "Department"), ("desig", "Designation"), ("shift", "Shift"),
    ("esi_no", "ESI No"), ("pf_uan_no", "PF UAN"),
]

# report code -> (title, component columns, HAVING filter, summed over range?)
PAY_REPORTS: dict[int, tuple[str, list, str, bool]] = {
    1: ("Main Pay Roll", MAIN_PAYROLL, "total_earn > 0", False),
    2: ("Main Pay Roll Vouchers", MAIN_PAYROLL, "total_earn > 0 AND epf > 0", False),
    7: ("Main Pay Roll Vouchers (ESI)", MAIN_PAYROLL, "total_earn > 0 AND epf = 0", False),
    8: ("Pay Image Comparative Statement", COMPARATIVE, "gross_pay > 0", False),
    3: ("Mill Retired", MILL_RETIRED, "total_earn > 0", False),
    4: ("Card Holder Bank", CARD_HOLDER,
        "bank_detail_id IS NOT NULL AND working_hours + holiday_hr + ot_hours > 0", False),
    5: ("Card Holder Cash", CARD_HOLDER,
        "bank_detail_id IS NULL AND working_hours + holiday_hr + ot_hours + advance + net_payable > 0", False),
    6: ("Trainee", TRAINEE, "total_earn > 0", False),
    16: ("Periodical Pay Register For Trainee Wages", TRAINEE, "total_earn > 0", True),
    30: ("Periodical Pay Register For Main Payroll & 18-PF", MAIN_PAYROLL_RANGE,
         "total_earning > 0", True),
}

# Not ported, and why — surfaced to the operator instead of a bare 400.
PAY_UNAVAILABLE = {
    11: "NJM Main Pay Roll pay slips print from the NJM Wages Data Preparation screen",
    12: "NJM OFB Clerk pay slips print from the NJM Wages Data Preparation screen",
    13: "C.E Deduction Abstract Sheet is an NJM report; run it from NJM Wages Data Preparation",
    14: "Bank Sheet downloads directly from the Pay Slip button",
    29: "The legacy screen never had a working handler for this report (Data_entry/getdranglcpayreg does not exist)",
    31: "Pay Roll Posting runs from the Pay Register screen",
}


def _pivot_query(cols: list, having: str, date_range: bool):
    """One employee per row, the report's components pivoted into columns.

    Single-period reports read one resolved pay period. Date-range reports
    first sum each employee's components over every non-cancelled period
    starting inside the range (legacy hol_get 16 / 30), then pivot the same way.
    Component ids are module constants, never user input, so they are safe to
    inline.
    """
    pivots = ",\n        ".join(
        f"MAX(CASE WHEN k.COMPONENT_ID = {cid} THEN k.AMOUNT ELSE 0 END) AS {field}"
        for field, _, cid in cols
    )
    if date_range:
        source = """(
            SELECT k.EMPLOYEEID, k.COMPONENT_ID, SUM(k.AMOUNT) AS AMOUNT
            FROM pay_employee_payroll AS k
            JOIN pay_period AS p ON p.ID = k.PAYPERIOD_ID
            WHERE p.FROM_DATE BETWEEN :date_from AND :date_to
              AND (p.STATUS IS NULL OR p.STATUS <> 4)
              AND k.STATUS <> 4
              AND k.PAYSCHEME_ID = :pay_scheme_id
            GROUP BY k.EMPLOYEEID, k.COMPONENT_ID
        ) AS k"""
        period_filter = ""
    else:
        source = "pay_employee_payroll AS k"
        period_filter = "WHERE k.PAYPERIOD_ID = :payperiod_id AND k.STATUS = 1"

    return text(f"""
    SELECT
        od.emp_code AS eb_no,
        CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ', TRIM(IFNULL(pd.middle_name, '')),
               ' ', TRIM(IFNULL(pd.last_name, ''))) AS wname,
        IFNULL(dm.dept_code, '') AS dept_code,
        IFNULL(dm.dept_desc, '') AS dept_desc,
        IFNULL(dsg.desig, '') AS desig,
        IFNULL(esi.esi_no, '') AS esi_no,
        IFNULL(pf.pf_no, '') AS pf_no,
        IFNULL(pf.pf_uan_no, '') AS pf_uan_no,
        IFNULL(dsg.time_piece, '') AS time_piece,
        MAX(bd.tbl_hrms_ed_bank_detail_id) AS bank_detail_id,
        CASE MAX(CASE WHEN k.COMPONENT_ID = 175 THEN k.AMOUNT ELSE 0 END)
            WHEN 1 THEN 'A' WHEN 2 THEN 'B' WHEN 3 THEN 'C' ELSE 'G' END AS shift,
        {pivots}
    FROM {source}
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = k.EMPLOYEEID AND od.active = 1
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = k.EMPLOYEEID
    LEFT JOIN sub_dept_mst AS sdm ON sdm.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = sdm.dept_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = od.designation_id
    LEFT JOIN hrms_ed_esi AS esi ON esi.eb_id = k.EMPLOYEEID AND esi.active = 1
    LEFT JOIN hrms_ed_pf AS pf ON pf.eb_id = k.EMPLOYEEID AND pf.active = 1
    LEFT JOIN hrms_ed_bank_details AS bd ON bd.eb_id = k.EMPLOYEEID AND bd.active = 1
    {period_filter}
    GROUP BY k.EMPLOYEEID, od.emp_code, wname, dm.dept_code, dm.dept_desc,
             dsg.desig, dsg.time_piece, esi.esi_no, pf.pf_no, pf.pf_uan_no
    HAVING {having}
    ORDER BY dept_code, eb_no
    """)


# Legacy Loan_adv_model::getptaxsummary (hol_get 15): employees banded by
# P.Tax earnings over the approved periods in the range. The bands and rates
# are the legacy ones verbatim (West Bengal slabs at the time).
PTAX_QUERY = text("""
    SELECT
        CASE
            WHEN ptax_earn BETWEEN 1 AND 10000.99 THEN '0 TO 10000'
            WHEN ptax_earn BETWEEN 10001 AND 15000.99 THEN '10001 TO 15000'
            WHEN ptax_earn BETWEEN 15001 AND 25000.99 THEN '15001 TO 25000'
            WHEN ptax_earn BETWEEN 25001 AND 40000.99 THEN '25001 TO 40000'
            WHEN ptax_earn > 40001 THEN '40001 + ABOVE'
            ELSE 'Unknown'
        END AS earning_group,
        CASE
            WHEN ptax_earn BETWEEN 10001 AND 15000.99 THEN 110
            WHEN ptax_earn BETWEEN 15001 AND 25000.99 THEN 130
            WHEN ptax_earn BETWEEN 25001 AND 40000.99 THEN 150
            ELSE 0
        END AS rate,
        COUNT(*) AS hands,
        SUM(ptax_earn) AS total_earnings,
        SUM(p_tax) AS p_tax
    FROM (
        SELECT k.EMPLOYEEID,
               MAX(CASE WHEN k.COMPONENT_ID = 16 THEN k.AMOUNT ELSE 0 END) AS p_tax,
               MAX(CASE WHEN k.COMPONENT_ID = 234 THEN k.AMOUNT ELSE 0 END) AS ptax_earn
        FROM pay_employee_payroll AS k
        JOIN pay_period AS p ON p.ID = k.PAYPERIOD_ID
        WHERE p.FROM_DATE BETWEEN :date_from AND :date_to
          AND p.PAYSCHEME_ID = :pay_scheme_id
          AND p.STATUS = 3
        GROUP BY k.EMPLOYEEID
    ) AS g
    WHERE ptax_earn > 0
    GROUP BY earning_group, rate
    ORDER BY earning_group, rate
""")

PTAX_COLUMNS = [
    ("earning_group", "Earning Group"), ("rate", "Rate of Deduction"),
    ("hands", "No. of Hands"), ("total_earnings", "Total Earnings"), ("p_tax", "P.Tax"),
]


def _resolve_period(db: Session, date_from: str, date_to: str,
                    pay_scheme_id: int, branch_id: int | None) -> int:
    """The screen's period + scheme → pay_period id (same rule as
    njmWagesData.njm_pay_period; cancelled periods excluded)."""
    row = db.execute(text("""
        SELECT p.ID FROM pay_period p
        WHERE p.FROM_DATE = :date_from AND p.TO_DATE = :date_to
          AND p.PAYSCHEME_ID = :pay_scheme_id
          AND (p.STATUS IS NULL OR p.STATUS <> 4)
          AND (:branch_id IS NULL OR p.branch_id = :branch_id OR p.branch_id IS NULL)
        ORDER BY p.ID DESC LIMIT 1
    """), {"date_from": date_from, "date_to": date_to,
           "pay_scheme_id": pay_scheme_id, "branch_id": branch_id}).fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=(f"No pay period covers this pay scheme for {date_from} to "
                    f"{date_to}. Create the period in Pay Register first."),
        )
    return int(row[0])


def _numeric(rows: list[dict], fields: list[str]) -> None:
    for r in rows:
        for f in fields:
            r[f] = float(r.get(f) or 0)


def _grand_total(rows: list[dict], fields: list[str], label_field: str = "wname") -> dict:
    total = {f: round(sum(r[f] for r in rows), 2) for f in fields}
    total[label_field] = "Grand Total"
    return total


def _columns(identity: list, cols: list) -> list[dict]:
    return ([{"field": f, "headerName": h} for f, h in identity]
            + [{"field": f, "headerName": h, "type": "number"} for f, h, *_ in cols])


def _parse(request: Request) -> dict:
    body = parse_json_body(request)
    if not body.get("date_from") or not body.get("date_to") or not body.get("pay_scheme_id"):
        raise HTTPException(status_code=400, detail="date_from, date_to and pay_scheme_id are required")
    try:
        date_from = date.fromisoformat(str(body["date_from"]))
        date_to = date.fromisoformat(str(body["date_to"]))
    except ValueError:
        raise HTTPException(status_code=400, detail="date_from and date_to must be YYYY-MM-DD dates")
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="date_to cannot be earlier than date_from")
    try:
        report = int(body.get("report") or 0)
        pay_scheme_id = int(body["pay_scheme_id"])
        branch_id = int(body["branch_id"]) if body.get("branch_id") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="report, pay_scheme_id and branch_id must be numbers")
    return {
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
        "pay_scheme_id": pay_scheme_id, "branch_id": branch_id, "report": report,
    }


@router.post("/ejm_pay_register_report")
def ejm_pay_register_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Pay Register grid for one legacy "Pay Register / Pay Slip Type" code.

    Body: date_from, date_to, pay_scheme_id, report (legacy hol_get), branch_id.
    Returns the rows, the columns to show, the title and — for single-period
    reports — the resolved payperiod_id the pay-slip / bank-sheet downloads key on.
    """
    p = _parse(request)
    report = p["report"]
    if report in PAY_UNAVAILABLE:
        raise HTTPException(status_code=400, detail=PAY_UNAVAILABLE[report])

    if report == 15:
        rows = [dict(r._mapping) for r in db.execute(PTAX_QUERY, p).fetchall()]
        fields = ["rate", "hands", "total_earnings", "p_tax"]
        _numeric(rows, fields)
        if rows:
            total = _grand_total(rows, ["hands", "total_earnings", "p_tax"], "earning_group")
            total["rate"] = None
            rows.append(total)
        return {"title": "P.Tax Report", "payperiod_id": None,
                "columns": [{"field": f, "headerName": h, "type": "number" if f != "earning_group" else None}
                            for f, h in PTAX_COLUMNS],
                "data": rows}

    if report not in PAY_REPORTS:
        raise HTTPException(status_code=400, detail=f"Unknown pay register report {report}")
    title, cols, having, date_range = PAY_REPORTS[report]

    params: dict = {"date_from": p["date_from"], "date_to": p["date_to"],
                    "pay_scheme_id": p["pay_scheme_id"]}
    payperiod_id = None
    if not date_range:
        payperiod_id = _resolve_period(db, p["date_from"], p["date_to"],
                                       p["pay_scheme_id"], p["branch_id"])
        params = {"payperiod_id": payperiod_id}

    rows = [dict(r._mapping) for r in db.execute(_pivot_query(cols, having, date_range), params).fetchall()]
    fields = [f for f, *_ in cols]
    _numeric(rows, fields)

    if report == 8:
        # Legacy: actual minus 100%, gains clamped to 0 (shortfalls print
        # negative); gross to paise, net to rupees.
        for r in rows:
            r["diff_gross"] = min(0.0, round(r["gross_pay"] - r["gross_pay100"], 2))
            r["diff_net"] = min(0.0, round(r["net_pay"] - r["net100"]))
        cols = cols + [("diff_gross", "Diff Gross", None), ("diff_net", "Diff Net", None)]
        fields += ["diff_gross", "diff_net"]

    if rows:
        rows.append(_grand_total(rows, fields))
    return {"title": title, "payperiod_id": payperiod_id,
            "columns": _columns(IDENTITY, cols), "data": rows}


# Legacy ot_register_payslip.php "Data For" codes.
OT_DEPT_SUMMARY = 3


@router.post("/ejm_ot_register_report")
def ejm_ot_register_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """OT Register for one pay period.

    Codes 1 (Main Pay Roll), 2 (Trainee) and 4 (Department-wise OT details)
    all return the legacy employee rows — the legacy grid ignored the code and
    the scheme picker already decides main vs trainee. Code 3 rolls the same
    rows up per department (legacy getotsummary), counting only employees
    with a positive OT net pay.
    """
    p = _parse(request)
    if p["report"] not in (1, 2, OT_DEPT_SUMMARY, 4):
        raise HTTPException(status_code=400, detail=f"Unknown OT register report {p['report']}")
    payperiod_id = _resolve_period(db, p["date_from"], p["date_to"],
                                   p["pay_scheme_id"], p["branch_id"])
    rows = [dict(r._mapping) for r in db.execute(
        _pivot_query(OT_REGISTER, "overtime_pay > 0", False),
        {"payperiod_id": payperiod_id}).fetchall()]
    fields = [f for f, *_ in OT_REGISTER]
    _numeric(rows, fields)

    if p["report"] == OT_DEPT_SUMMARY:
        depts: dict[tuple, dict] = {}
        for r in rows:
            if r["ot_net_pay"] <= 0:
                continue
            d = depts.setdefault((r["dept_code"], r["dept_desc"]), {
                "dept_code": r["dept_code"], "dept_desc": r["dept_desc"],
                "ot_hours": 0.0, "overtime_pay": 0.0, "ot_advance": 0.0, "ot_net_pay": 0.0,
            })
            for f in ("ot_hours", "overtime_pay", "ot_advance", "ot_net_pay"):
                d[f] = round(d[f] + r[f], 2)
        rows = list(depts.values())
        cols = [("ot_hours", "OT Hrs", None), ("overtime_pay", "OT Amount", None),
                ("ot_advance", "OT Advance", None), ("ot_net_pay", "Net Payment", None)]
        if rows:
            rows.append(_grand_total(rows, [f for f, *_ in cols], "dept_desc"))
        return {"title": "EJM Department-wise OT Summary Register", "payperiod_id": payperiod_id,
                "columns": _columns([("dept_code", "Dept"), ("dept_desc", "Department")], cols),
                "data": rows}

    if rows:
        rows.append(_grand_total(rows, fields))
    identity = [("dept_code", "Dept"), ("dept_desc", "Department"),
                ("eb_no", "EB No"), ("wname", "Employee")]
    return {"title": "OT Register", "payperiod_id": payperiod_id,
            "columns": _columns(identity, OT_REGISTER), "data": rows}
