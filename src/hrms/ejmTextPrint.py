"""EJM Wages Data Preparation — dot-matrix text prints.

The legacy Data-Portal-Php screens printed every register / pay slip as a
fixed-width ``.txt`` for continuous stationery (``Data_entry::hlincprint``,
``otregisterprint``, ``otpayslipprint``, ``payregisterprint``,
``payslipprint``). This router serves the same text for the three selects on
the EJM Wages Data Preparation screen:

* ``list=action``  — the "Report" (legacy Data For) codes that had a print:
  2 (FN attendance incentive: two-up slips + register), 3/7 and 4/8
  (fortnightly / monthly incentive register), 21 (dept-wise incentive summary).
* ``list=ot``      — OT Register (codes 1–4) and OT pay slips (1, 2).
* ``list=payreg``  — Pay Register (1/2/7, 3, 4/5, 6, 8) and Pay Slips
  (1/2/7, 3, 4/5, 6); those layouts live in ejmTextPayReg / ejmTextPaySlip.

Rows come from the same pivot queries the on-screen grids use
(ejmPayRegister) and the attendance-incentive query (ejmWagesData); the
legacy session company / hard-coded mill names become the tenant's company
name and the chosen pay scheme's name.
"""
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms import ejmPayRegister as reg
from src.hrms.ejmWagesData import (
    INCENTIVE_FULL_DAY_HOURS, INCENTIVE_MIN_DAYS, LEAVE_APPROVED,
    _attendance_incentive_query,
)
from src.hrms.query import get_company_name

router = APIRouter()

FF = "\x0c"

# Legacy getMNattincentiveData: a monthly day counts at 6 hours, and an
# employee is paid only from 24 worked days.
MONTHLY_FULL_DAY_HOURS = 6
MONTHLY_MIN_PAID_DAYS = 24


# ─── PHP formatting helpers ──────────────────────────────────────────
def nf(v, d=2) -> str:
    """PHP number_format: thousands commas, d decimals."""
    return f"{float(v or 0):,.{d}f}"


def dec(v) -> str:
    """A bare DECIMAL(…,2) column as MySQL/PHP printed it."""
    return f"{float(v or 0):.2f}"


def rj(s, w) -> str:
    s = str(s)
    return " " * max(0, w - len(s)) + s


def lj(s, w) -> str:
    s = str(s)[:w]
    return s + " " * (w - len(s))


def dmy(iso: str, sep="-") -> str:
    return f"{iso[8:10]}{sep}{iso[5:7]}{sep}{iso[0:4]}"


def stars(v) -> str:
    """Net pay `*`-padded to 6 (legacy cheque-style fill)."""
    s = nf(v, 0)
    return "*" * max(0, 6 - len(s)) + s


# ─── OT register (legacy otregisterprint) ────────────────────────────
def ot_sheet(rows, ctx) -> str:
    """Codes 1 and 4: 82-col OT Sheet, one line per employee, grand total."""
    line = "-" * 82
    hd1 = "Dept  EB No     Name                Hours   Amount   Advance  Misc Earn   Net Pay"
    title = f"{ctx['scheme_name']} OT Sheet for the period From {ctx['date_from']} To {ctx['date_to']}       Page No "
    out = [ctx["company_name"], title + "1", line, hd1, line, " ", " "]
    pg, lnn = 1, 8
    tot = {"h": 0.0, "a": 0.0, "adv": 0.0, "m": 0.0, "n": 0.0}
    for r in rows:
        if lnn > 58:
            pg += 1
            out += [FF + ctx["scheme_name"], f"Main Pay Roll OT Sheet for the period From {ctx['date_from']} To {ctx['date_to']}       Page No {pg}", line, hd1, line]
            lnn = 5
        # Legacy substr($wname, 1, 20) dropped the first letter of every name;
        # that is a bug, not a format, so the name starts at 0 here.
        out.append(" " + r["dept_code"] + "  " + lj(r["eb_no"], 7) + lj(r["wname"], 20)
                   + rj(nf(r["ot_hours"]), 9) + rj(dec(r["overtime_pay"]), 9)
                   + rj(dec(r["ot_advance"]), 10) + rj(dec(r["misc_ot_earn"]), 11)
                   + rj(nf(r["ot_net_pay"], 0), 10))
        out.append(line)
        tot["h"] += r["ot_hours"]; tot["a"] += r["overtime_pay"]; tot["adv"] += r["ot_advance"]
        tot["m"] += r["misc_ot_earn"]; tot["n"] += r["ot_net_pay"]
        lnn += 2
    out.append(lj("Grand Total", 11) + " " * 21 + rj(dec(tot["h"]), 9) + rj(dec(tot["a"]), 9)
               + rj(dec(tot["adv"]), 10) + rj(dec(tot["m"]), 11) + rj(dec(tot["n"]), 10))
    out += [line, FF]
    return "\n".join(out) + "\n"


def trainee_ot_sheet(rows, ctx) -> str:
    """Code 2: 72-col Trainee OT Sheet (also the trainee OT pay slip)."""
    line = "-" * 72
    hd = "Department   EB No     Name                    Hours    Rate     Amount"

    def header(pg):
        return [ctx["scheme_name"],
                f"Trainee OT Sheet for the period From {ctx['date_from']} To {ctx['date_to']}       Page No {pg}",
                line, hd, line, " ", " "]

    out = header(1)
    pg, lnn, tothrs, totamt = 1, 8, 0.0, 0.0
    for r in rows:
        if lnn > 58:
            pg += 1
            out += [FF] + header(pg)
            lnn = 8
        out.append("   " + r["dept_code"] + "        " + lj(r["eb_no"], 10) + lj(r["wname"], 20)
                   + rj(nf(r["ot_hours"]), 9) + rj(dec(r["rate"]), 8) + rj(dec(r["overtime_pay"]), 10))
        out += [" ", " ", line]
        tothrs += r["ot_hours"]; totamt += r["overtime_pay"]
        lnn += 4
    out.append(" " * 13 + " " * 10 + lj("Grand Total", 20) + rj(dec(tothrs), 9) + " " * 8 + rj(dec(totamt), 10))
    out += [line, FF]
    return "\n".join(out) + "\n"


def ot_dept_summary(rows, ctx) -> str:
    """Code 3: 79-col OT Summary Statement, one line per department."""
    line = "-" * 79
    header = ["", ctx["company_name"],
              f"OT Summary Statement for the  period from  {dmy(ctx['date_from'])} To {dmy(ctx['date_to'])}",
              line, "Dept Code   Department     Ot-Hours    Ot-Amount   Ot-Advance   Net Payment", line]
    out, lnn = [], 1
    g = {"h": 0.0, "p": 0.0, "adv": 0.0, "n": 0.0}
    for r in rows:
        if lnn > 58:
            out.append(FF)
            lnn = 1
        if lnn == 1:
            out += header
            lnn = 6
        out.append(" " + r["dept_code"] + "        " + lj(r["dept_desc"], 15) + rj(dec(r["ot_hours"]), 8)
                   + rj(nf(r["overtime_pay"], 0), 12) + rj(nf(r["ot_advance"], 0), 12) + rj(nf(r["ot_net_pay"], 0), 12))
        out.append(line)
        g["h"] += r["ot_hours"]; g["p"] += r["overtime_pay"]; g["adv"] += r["ot_advance"]; g["n"] += r["ot_net_pay"]
        lnn += 2
    if not out:
        out += header
    out += [line, " " * 5 + lj("Grand Total", 20) + rj(dec(g["h"]), 8) + rj(dec(g["p"]), 12)
            + rj(dec(g["adv"]), 13) + rj(dec(g["n"]), 13), line, line, FF]
    return "\n".join(out) + "\n"


# ─── Two-up slips (legacy otpayslipprint / hlincprint holget 2) ──────
BKLINE = "                                        |                                         |"
BKLIN = "----------------------------------------|-----------------------------------------|"


def two_up_slips(rows, ctx, heads, cells) -> str:
    """72-col two-slips-per-row layout shared by the OT pay slip and the
    attendance-incentive slip. ``heads`` are the three column-header lines,
    ``cells(r)`` returns the three detail fragments (each ends with ' | ').
    Form feed on every department change and every 5 printed rows."""
    out, pend, dpc, pg, n = [], None, None, 1, 0

    def flush(l1, l2, l3):
        out.extend([BKLINE, heads[0], l1, BKLINE, heads[1], l2, BKLINE, heads[2], l3, BKLIN])

    for r in rows:
        if dpc is not None and r["dept_code"] != dpc:
            if pend:
                flush(*pend); pend = None
            out.append(FF); pg, n = 1, 0
        dpc = r["dept_code"]
        if pg > 5:
            out.append(FF); pg = 1
        n += 1
        c1, c2, c3 = cells(r, n)
        if n % 2 == 1:
            pend = (c1, c2, c3)
        else:
            flush(pend[0] + c1, pend[1] + c2, pend[2] + c3)
            pend, pg = None, pg + 1
    if pend:
        flush(*pend)
    out.append(FF)
    return "\n".join(out) + "\n"


def ot_payslip(rows, ctx) -> str:
    end = dmy(ctx["date_to"], "/")
    heads = ("DP S SRL T/No. Name SUPL SLIP) F/E Date | DP S SRL T/No. Name SUPL SLIP) F/E Date |",
             "           WRK-HRS ATTN-INC    TOT-EARN |            WRK-HRS ATTN-INC    TOT-EARN |",
             "     ADVANCE     MISC               NET |      ADVANCE      MISC              NET |")

    def cells(r, n):
        return (f"{r['dept_code']} A {rj(n, 3)} {lj(r['eb_no'], 5)} {lj(r['wname'][:13], 14)}{end} | ",
                rj(nf(r["ot_hours"]), 18) + rj(0, 10) + rj(dec(r["overtime_pay"]), 11) + " | ",
                rj(nf(r["ot_advance"], 0), 11) + rj(nf(r["misc_ot_earn"]), 11) + rj(stars(r["ot_net_pay"]), 17) + " | ")
    return two_up_slips(rows, ctx, heads, cells)


def incentive_payslip(rows, ctx) -> str:
    end = dmy(ctx["date_to"], "/")
    heads = ("DP S SRL T/No. Name ATTN INCN) F/E Date | DP S SRL T/No. Name ATTN INCN) F/E Date |",
             "           TOT-DAY MISC-EARN   TOT-EARN |            TOT-DAY MISC-EARN   TOT-EARN |",
             "     ADVANCE                        NET |      ADVANCE                        NET |")

    def cells(r, n):
        return (f"{r['dept_code']} A {rj(n, 3)} {lj(r['emp_code'], 5)} {lj(r['empname'][:13], 14)}{end} | ",
                rj(nf(r["incdays"]), 18) + rj(0, 10) + rj(int(r["incamt"]), 11) + " | ",
                rj(0, 11) + rj(stars(r["incamt"]), 28) + " | ")
    return two_up_slips(rows, ctx, heads, cells)


# ─── Attendance incentive registers (legacy hlincprint) ──────────────
def incentive_register_by_dept(rows, ctx) -> str:
    """Code 2, second file (increg.txt): 81-col register with a total per
    department and a grand total."""
    line = "-" * 81
    shd1 = f"Attendance Incentive Register for the period From {dmy(ctx['date_from'])} To {dmy(ctx['date_to'])}"
    shd2 = "DP S SRL T/No. Name                TOT-DAY MISC-EARN   TOT-EARN  ADVANCE      NET"
    out, dpc, dtot, gtot, n = [], None, 0.0, 0.0, 0
    for r in rows:
        if r["dept_code"] != dpc:
            if dpc is not None:
                out += [line, rj(int(dtot), 80), line]
            out += [shd1, line, shd2, line]
            dpc, dtot, n = r["dept_code"], 0.0, 0
        n += 1
        out.append(f"{r['dept_code']} A {rj(n, 3)} {lj(r['emp_code'], 5)} {lj(r['empname'][:13], 14)}"
                   + rj(nf(r["incdays"]), 13) + rj(0, 10) + rj(int(r["incamt"]), 11) + rj(0, 9) + rj(nf(r["incamt"], 0), 8))
        gtot += r["incamt"]; dtot += r["incamt"]
    if dpc is not None:
        # Legacy never printed the last department's total — the loop ended
        # first. Printing it is the obvious intent.
        out += [line, rj(int(dtot), 80), line]
    out += [line, "Grand Total" + rj(int(gtot), 69), line]
    return "\n".join(out) + "\n"


def incentive_register(rows, ctx) -> str:
    """Codes 3/7 (fortnightly) and 4/8 (monthly): 81-col register, 58-line
    pages, grand total, then a one-line Summary page."""
    line = "-" * 81
    hdm1 = f"Attendance Incentive Register for the period From {dmy(ctx['date_from'])} To {dmy(ctx['date_to'])}"
    hdm2 = "EB No    Name                 Dept Name        W.Days   Inc Amt   Signature "
    header = [ctx["scheme_name"], hdm1, line, hdm2, line]
    out, lnn, tdays, tamt = list(header), 5, 0.0, 0.0
    for r in rows:
        if lnn > 58:
            out += [FF] + header
            lnn = 5
        out.append(lj(r["emp_code"], 8) + lj(r["empname"][:20], 21) + lj(r["dept_desc"][:15], 15)
                   + rj(int(r["incdays"]), 9) + rj(int(r["incamt"]), 10))
        out += [" ", " ", line]
        tdays += r["incdays"]; tamt += r["incamt"]
        lnn += 4

    def total(label):
        return lj(" ", 8) + lj(label, 21) + lj(" ", 15) + rj(int(tdays), 9) + rj(int(tamt), 10)
    out += [total("Grand Total"), line, FF, line] + header + [total("Summary"), line, FF]
    return "\n".join(out) + "\n"


def incentive_dept_summary(rows, ctx) -> str:
    """Code 21: 79-col department-wise attendance incentive summary."""
    line = "-" * 79
    out = [ctx["company_name"], "Departmnt Wise Attendance Incentive Summary for ",
           f"Over All   for period from  {dmy(ctx['date_from'])} To {dmy(ctx['date_to'])}",
           "Dept Code   Department     Incentive-Amount      Net-Amount", line]
    g = 0.0
    for r in rows:
        a = nf(r["attincn"], 0)
        out += ["    " + r["dept_code"] + "        " + lj(r["dept_desc"], 15) + rj(a, 8) + rj(a, 20), line]
        g += r["attincn"]
    out += [line, " " * 5 + lj("Grand Total", 20) + rj(dec(g), 8) + rj(dec(g), 22), line, FF]
    return "\n".join(out) + "\n"


# ─── Data ────────────────────────────────────────────────────────────
TEXT_FIELDS = {"eb_no", "wname", "dept_code", "dept_desc", "desig", "shift", "esi_no",
               "pf_no", "pf_uan_no", "time_piece", "emp_code", "empname"}


def _num(rows, skip=("bank_detail_id", "eb_id")):
    """DB Decimals -> float, NULL text -> '', NULL numbers -> 0.0."""
    for r in rows:
        for k, v in r.items():
            if k in skip:
                continue
            if k in TEXT_FIELDS:
                r[k] = "" if v is None else str(v)
            elif isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                r[k] = float(v)
            elif v is None:
                r[k] = 0.0
    return rows


def _ot_rows(db, p):
    pid = reg._resolve_period(db, p["date_from"], p["date_to"], p["pay_scheme_id"], p["branch_id"])
    rows = [dict(r._mapping) for r in db.execute(
        reg._pivot_query(reg.OT_REGISTER, "overtime_pay > 0", False), {"payperiod_id": pid}).fetchall()]
    return _num(rows)


def _dept_rollup(rows, fields):
    depts = {}
    for r in rows:
        d = depts.setdefault((r["dept_code"], r["dept_desc"]),
                             {"dept_code": r["dept_code"], "dept_desc": r["dept_desc"], **{f: 0.0 for f in fields}})
        for f in fields:
            d[f] += r[f]
    return list(depts.values())


def _monthly_eligible_days(date_from: str, date_to: str) -> int:
    """Legacy: days in the month minus EMPMILL12.tbl_non_working_days.
    # ponytail: tenant DBs have no non-working-days master, so Sundays stand
    # in for it (the bulk of that table). Swap in the master when it lands."""
    d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
    days = (d1 - d0).days + 1
    sundays = sum(1 for i in range(days) if (d0 + timedelta(days=i)).weekday() == 6)
    return days - sundays


def _incentive_rows(db, p, code):
    if not p["branch_id"]:
        raise HTTPException(status_code=400, detail="Select a branch in the sidebar before printing this report")
    monthly = code in (4, 8)
    params = {
        "date_from": p["date_from"], "date_to": p["date_to"], "branch_id": p["branch_id"],
        "pay_scheme_id": p["pay_scheme_id"], "leave_approved": LEAVE_APPROVED,
        "full_day_hours": MONTHLY_FULL_DAY_HOURS if monthly else INCENTIVE_FULL_DAY_HOURS,
        "min_days": _monthly_eligible_days(p["date_from"], p["date_to"]) if monthly else INCENTIVE_MIN_DAYS,
        "min_paid_days": MONTHLY_MIN_PAID_DAYS,
    }
    scheme_bound = code in (2, 3, 4)
    if not scheme_bound:
        params.pop("pay_scheme_id")
    q = _attendance_incentive_query(monthly=monthly, scheme_bound=scheme_bound)
    return _num([dict(r._mapping) for r in db.execute(q, params).fetchall()])


def _payroll_incentive_rows(db, p):
    """Code 2 prints what payroll paid (component 248), never a recomputation.

    Legacy getmainFNattincentiveData built the attendance query and then
    overwrote ``$sql`` with this payroll pivot, so the register only ever
    showed employees with a posted incentive. Days are back-derived from the
    amount exactly as legacy did.
    """
    pid = reg._resolve_period(db, p["date_from"], p["date_to"], p["pay_scheme_id"], p["branch_id"])
    rows = _num([dict(r._mapping) for r in db.execute(
        reg._pivot_query([("incamt", "Att Incentive", 248)], "incamt > 0", False),
        {"payperiod_id": pid}).fetchall()])
    for r in rows:
        r["emp_code"], r["empname"] = r["eb_no"], r["wname"]
        r["incdays"] = _incentive_days(r["incamt"])
    return rows


# ponytail: legacy hardcoded 20/day; read tbl_holiday_att_inc_eligibility.fn_att_inc_rate if rates ever differ.
INCENTIVE_RATE_PER_DAY = 20.0


def _incentive_days(incamt: float) -> float:
    return incamt if incamt < INCENTIVE_RATE_PER_DAY else incamt / INCENTIVE_RATE_PER_DAY


def _payreg_rows(db, p, code):
    if code not in reg.PAY_REPORTS:
        raise HTTPException(status_code=400, detail=f"No legacy print for Pay Register type {code}")
    _, cols, having, date_range = reg.PAY_REPORTS[code]
    cols = cols + reg.PRINT_EXTRA.get(code, [])
    pid = None
    if date_range:
        params = {"date_from": p["date_from"], "date_to": p["date_to"], "pay_scheme_id": p["pay_scheme_id"]}
    else:
        pid = reg._resolve_period(db, p["date_from"], p["date_to"], p["pay_scheme_id"], p["branch_id"])
        params = {"payperiod_id": pid}
    rows = _num([dict(r._mapping) for r in db.execute(reg._pivot_query(cols, having, date_range), params).fetchall()])
    for r in rows:
        if code == 8:
            r["diff_gross"] = min(0.0, round(r["gross_pay"] - r["gross_pay100"], 2))
            r["diff_net"] = min(0.0, round(r["net_pay"] - r["net100"]))
        if code in (1, 2, 7):
            r["epf_833"] = round(r["pf_gross"] * 8.33 / 100)
            r["epf_167"] = round(r["pf_gross"] * 1.67 / 100)
    return rows


# (list, kind) -> {code: (renderer, rows loader, filename stem)}
PRINTS = {
    ("ot", "register"): {
        1: (ot_sheet, _ot_rows, "otdata"),
        4: (ot_sheet, _ot_rows, "otdata"),
        2: (trainee_ot_sheet, _ot_rows, "otdata"),
        3: (ot_dept_summary,
            lambda db, p: _dept_rollup([r for r in _ot_rows(db, p) if r["ot_net_pay"] > 0],
                                       ["ot_hours", "overtime_pay", "ot_advance", "ot_net_pay"]),
            "summreg"),
    },
    ("ot", "payslip"): {
        1: (ot_payslip, _ot_rows, "otpayslip"),
        2: (trainee_ot_sheet, _ot_rows, "otdata"),
    },
    ("action", "register"): {
        # Legacy zipped two files for code 2; one download, a form feed apart.
        2: (lambda rows, ctx: incentive_payslip(rows, ctx) + incentive_register_by_dept(rows, ctx),
            _payroll_incentive_rows, "incdata"),
        3: (incentive_register, lambda db, p: _incentive_rows(db, p, 3), "increg"),
        7: (incentive_register, lambda db, p: _incentive_rows(db, p, 7), "increg"),
        4: (incentive_register, lambda db, p: _incentive_rows(db, p, 4), "increg"),
        8: (incentive_register, lambda db, p: _incentive_rows(db, p, 8), "increg"),
        21: (incentive_dept_summary,
             lambda db, p: _dept_rollup([r for r in _num([dict(r._mapping) for r in db.execute(
                 reg._pivot_query([("attincn", "Att Incentive", 248)], "attincn > 0", False),
                 {"payperiod_id": reg._resolve_period(db, p["date_from"], p["date_to"], p["pay_scheme_id"], p["branch_id"])},
             ).fetchall()])], ["attincn"]),
             "summreg"),
    },
}

NO_PRINT = {
    "action": "The legacy screen only printed Main Payroll FN Attendance Incentive (2), the fortnightly / monthly incentive registers (3, 4, 7, 8) and the dept-wise incentive summary (21)",
    "ot": "OT pay slips exist for Main Pay Roll (1) and Trainee (2) only",
    "payreg": "No legacy print for this Pay Register type — Bank Sheet downloads from the Pay Slip button, 11–13 are NJM reports, 29/31 never had one",
    "statement": "Pay Statement exists for Main Pay Roll (1/2/7), Trainee (6) and P.Tax Report (15) only",
}


# ─── Pay Statement (legacy paystatement) ────────────────────────────
def phpf(v) -> str:
    """PHP's float-to-string for round()/floatval() results: no trailing zeros."""
    s = f"{float(v or 0):.2f}".rstrip("0").rstrip(".")
    return s or "0"


# Legacy getpaystatementsumm column -> how it is derived from one register row.
# Anything legacy hard-wired to 0 is simply absent here.
DEPT_SUMS = {
    "WORKING_HOURS": lambda r: r["working_hours"], "NS_HRS": lambda r: r["ns_hrs"],
    "HL_HRS": lambda r: r["hl_hrs"], "LS_HRS": lambda r: r["ls_hrs"],
    "STL_HRS": lambda r: r["stl_d"] * 8, "STL_D": lambda r: r["stl_d"],
    "VFIX_BASIC": lambda r: r["fix_basic"] + r["prod_basic"],
    "FXBAS": lambda r: r["time_rated_basic"] + r["basic_amount"], "FDA": lambda r: r["da"],
    "NS_AMOUNT": lambda r: r["ns_amount"], "HOL_AMT": lambda r: r["hol_amt"],
    "STL_WGS": lambda r: r["stl_wgs"], "INCREMENTA": lambda r: r["increment_amt"],
    "PFGROSS": lambda r: r["gross_pay"], "GROSS": lambda r: r["gross_pay"], "HRA": lambda r: r["hra"],
    "INCENTIVE_AMOUNT": lambda r: r["incentive_amount"], "MISS_EARN": lambda r: r["misc_earn"],
    "ESI_GROSS": lambda r: r["esi_gross"], "EPF": lambda r: r["epf"], "ESIC": lambda r: r["esic"],
    "LWF": lambda r: r["lwf"], "STL_ADVANCE": lambda r: r["stl_advance"],
    "PUJA_ADVANCE": lambda r: r["puja_advance"], "CO_LOAN": lambda r: r["co_loan"],
    "TOTAL_EARN": lambda r: r["total_earn"], "TOTAL_DEDUCTION": lambda r: r["total_deduction"],
    "NETPAY": lambda r: r["total_earn"] - r["total_deduction"], "B_F": lambda r: r["b_f"],
    "C_F": lambda r: r["c_f"], "NETPAYABLE": lambda r: r["net_pay"], "P_TAX": lambda r: r["p_tax"],
    "exadvance": lambda r: r["exadvance"], "arrear_plus": lambda r: r["arrear_plus"],
}


def _sum_block(rows) -> dict:
    return {k: sum(f(r) for r in rows) for k, f in DEPT_SUMS.items()}


def _dept_summary_block(hrem, dept, shift, t, grand) -> list:
    """The 14-line label/value block legacy printed per department (and once
    more, with the round()/floatval quirks, for the grand total)."""
    n2 = lambda k: nf(t.get(k, 0))
    z = nf(0)
    wrk = phpf(t["WORKING_HOURS"]) if grand else n2("WORKING_HOURS")
    fda = phpf(t["FDA"]) if grand else n2("FDA")
    netpay = phpf(t["NETPAY"]) if grand else n2("NETPAY")
    tearn = phpf(t["TOTAL_EARN"]) if grand else n2("TOTAL_EARN")
    miss = nf(t["MISS_EARN"] + t["arrear_plus"]) if grand else n2("MISS_EARN")
    othadv = nf(t["exadvance"]) if grand else z
    netpayable = phpf(round(t["NETPAYABLE"])) if grand else n2("NETPAYABLE")
    L = lambda label, val, w=10: f"{label:<12}: {rj(val, w)}  "
    B = L("", " ")
    return [
        f"{hrem}{' ' * 25}DEPT {dept}{' ' * 25} Shift {shift}",
        L("WRK HRS", wrk) + L("Vr Basic", n2("VFIX_BASIC")) + L("H.R.A", n2("HRA")) + L("PF CONT", n2("EPF")) + L("PEN CONT", z).rstrip(),
        L("NS HRS", n2("NS_HRS")) + L("Fx Basic", n2("FXBAS")) + L("Adj N PF", z) + L("ESI", n2("ESIC")) + L("P TAX", n2("P_TAX")),
        L("HOL HRS", n2("HL_HRS")) + L("F.D.A", fda) + L("INCENTIVE", n2("INCENTIVE_AMOUNT")) + L("L.W.F", n2("LWF")) + L("H-RENT", z),
        L("LOST HRS", n2("LS_HRS")) + L("NS ALLOW", n2("NS_AMOUNT")) + L("MISC. EARN", miss) + L("PF LOAN", z) + L("PFLN INTL", z),
        L("STL HRS", n2("STL_HRS")) + L("HOL WAGES", n2("HOL_AMT")) + L("ADHOC INC", z) + L("STL ADV", n2("STL_ADVANCE")) + L("ADVANCE", z),
        L("LOFF HRS", z) + L("STL WAGES", n2("STL_WGS")) + L("TOTAL EARN", tearn, 12) + L("CO LOAN", n2("CO_LOAN")) + L("OTHER ADV", othadv),
        " " * 27 + L("ADJ PF", z),
        " " * 27 + L("INCREMENT", n2("INCREMENTA")),
        " " * 27 + f"ARREAR   : {rj(z, 10)}  ",
        L("TOT PROD", z) + L("PF GROSSS", n2("PFGROSS"), 12) + B + B + L("TOTAL DEDN", n2("TOTAL_DEDUCTION")),
        B + L("GROSSS", n2("GROSS"), 12) + B + L("NET PAY", netpay, 12) + L("", " ", 50),
        B + B + L("P/BF", n2("B_F")) + B + L("", " ", 50),
        B + B + L("P/CF", n2("C_F")) + B + L("", " ", 50),
        B + B + L("NET PAYABLE", netpayable, 12) + B + L("", " ", 50),
    ]


def payroll_dept_statement(rows, ctx) -> str:
    """Codes 1/2/7: Worker PayRoll Department Summary — one block per
    department, three per page, then the grand block on its own page."""
    line = "-" * 136
    code = ctx["report"]
    hrem = {1: "[MAIN Payroll]", 2: "[18 PF]", 7: "[VCH NPF]"}[code]
    if "RETIRED" in ctx["scheme_name"].upper():
        hrem = "[Retd]"
    out = [ctx["company_name"] + " " * 50 + "Worker PayRoll Department Summary F/E   " + dmy(ctx["date_to"]), line]
    depts = {}
    for r in rows:
        depts.setdefault(r["dept_code"], []).append(r)
    pg = 1
    for dept, drows in depts.items():
        out += _dept_summary_block(hrem, dept, "A", _sum_block(drows), False) + [line, " ", " "]
        pg += 1
        if pg > 3:
            out.append(FF); pg = 1
    out += [line + "\x12", FF]
    out += _dept_summary_block(hrem, "***", "***", _sum_block(rows), True) + [line, " ", " ", line + "\x12", FF]
    return "\n".join(out) + "\n"


def trainee_statement(rows, ctx) -> str:
    """Code 6: Summary Statement per employee, then the department-wise one."""
    line = "-" * 79
    fr, to = dmy(ctx["date_from"]), dmy(ctx["date_to"])
    hdr = [ctx["scheme_name"], f"Summary Statement for the period from  {fr}    To {to}", line,
           "Dept Code  EB No     Name                         Amount   OT Amt   TOT Amount", line]
    out, lnn, dpc = [], 1, None
    g = {"net": 0.0, "ot": 0.0, "tot": 0.0}
    for r in rows:
        tot = r["net_payable"] + r["overtime_pay"]
        if dpc is not None and r["dept_code"] != dpc:
            out.append(line)
        dpc = r["dept_code"]
        if lnn > 58:
            out.append(FF); lnn = 1
        if lnn == 1:
            out += hdr; lnn = 5
        out += [dpc + " " * 9 + lj(r["eb_no"], 10) + lj(r["wname"][:20], 20)
                + rj(nf(r["net_payable"], 0), 15) + rj(nf(r["overtime_pay"], 0), 9) + rj(nf(tot, 0), 13), line]
        g["net"] += r["net_payable"]; g["ot"] += r["overtime_pay"]; g["tot"] += tot
        lnn += 2
    out += [line, " " * 21 + lj("Grand Total", 20) + rj(phpf(g["net"]), 15) + rj(phpf(g["ot"]), 9) + rj(phpf(g["tot"]), 13), line, FF]

    out += [ctx["scheme_name"], f"Departmentwise Summary Statement for the period from  {fr}    To {to}", line,
            "Dept Code    Department                W.hours       Amount   OT Amt   TOT Amount", line]
    depts = _dept_rollup(rows, ["working_hours", "net_payable", "overtime_pay"])
    th = 0.0
    for d in depts:
        tot = d["net_payable"] + d["overtime_pay"]
        out += [d["dept_code"] + " " * 9 + " " * 10 + lj(d["dept_desc"][:15], 15) + rj(nf(d["working_hours"]), 11)
                + rj(nf(d["net_payable"], 0), 11) + rj(nf(d["overtime_pay"], 0), 9) + rj(nf(tot, 0), 13), line]
        th += d["working_hours"]
    out += [line, " " * 21 + lj("Grand Total", 15) + rj(phpf(th), 11) + rj(phpf(g["net"]), 11)
            + rj(phpf(g["ot"]), 9) + rj(phpf(g["tot"]), 13), line, FF]
    return "\n".join(out) + "\n"


def ptax_statement(rows, ctx) -> str:
    """Code 15: fortnightly workers P-Tax summary (120-col)."""
    line = "=" * 120
    out = [line, ctx["company_name"],
           f"FORTNIGHTLY WORKERS P-TAX SUMMARY FOR THE PERIOD OF {dmy(ctx['date_from'])} TO {dmy(ctx['date_to'])}", line,
           "EARNING GROUP     RATE OF      NO. OF         TOTAL          P.TAX DEDUCTION    P.TAX DEDUCTION            TOTAL",
           "                 DEDUCTION      HANDS         EARNINGS        CURRENT                 ARREAR             DEDUCTION", line]
    pad = lambda w, s: " " * max(1, w - len(s)) + s
    hands, earn, tax = 0, 0.0, 0.0
    for r in rows:
        rate, cnt, te, tp = nf(r["rate"]), str(int(r["hands"])), nf(r["total_earnings"]), nf(r["p_tax"])
        # Legacy printed the current P.Tax in the arrear column too.
        out += [" " + r["earning_group"] + pad(10, rate) + pad(12, cnt) + pad(20, te) + pad(17, tp) + pad(42, tp), line]
        hands += int(r["hands"]); earn += r["total_earnings"]; tax += r["p_tax"]
    out += [" ***Grand Total***" + pad(12, str(hands)) + pad(17, nf(earn)) + pad(13, nf(tax)), line]
    return "\n".join(out) + "\n"


def _ptax_rows(db, p):
    rows = [dict(r._mapping) for r in db.execute(reg.PTAX_QUERY, {
        "date_from": p["date_from"], "date_to": p["date_to"], "pay_scheme_id": p["pay_scheme_id"]}).fetchall()]
    return _num(rows, skip=("earning_group",))


STATEMENTS = {
    1: (payroll_dept_statement, lambda db, p: _payreg_rows(db, p, 1), "summreg"),
    2: (payroll_dept_statement, lambda db, p: _payreg_rows(db, p, 2), "summreg"),
    7: (payroll_dept_statement, lambda db, p: _payreg_rows(db, p, 7), "summreg"),
    6: (trainee_statement, lambda db, p: _payreg_rows(db, p, 6), "summreg"),
    15: (ptax_statement, _ptax_rows, "ptaxreg"),
}


def _payreg_print(kind, code):
    """Pay Register / Pay Slip layouts are ported in their own modules."""
    if kind == "register":
        from src.hrms.ejmTextPayReg import render
    else:
        from src.hrms.ejmTextPaySlip import render
    return (lambda rows, ctx: render(code, rows, ctx),
            lambda db, p: _payreg_rows(db, p, code),
            "Payregdata" if kind == "register" else "payslip")


@router.get("/ejm_text_print")
def ejm_text_print(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Fixed-width .txt for one (list, kind, report) — see module docstring.

    Query: list=action|ot|payreg, kind=register|payslip, report, date_from,
    date_to, pay_scheme_id, branch_id, co_id.
    """
    q = request.query_params
    lst, kind = q.get("list", ""), q.get("kind", "register")
    if lst not in ("action", "ot", "payreg") or kind not in ("register", "payslip", "statement"):
        raise HTTPException(status_code=400, detail="list must be action|ot|payreg and kind register|payslip|statement")
    try:
        p = {
            "date_from": date.fromisoformat(q.get("date_from", "")).isoformat(),
            "date_to": date.fromisoformat(q.get("date_to", "")).isoformat(),
            "pay_scheme_id": int(q.get("pay_scheme_id", "")),
            "branch_id": int(q["branch_id"]) if q.get("branch_id") else None,
            "report": int(q.get("report", "")),
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="date_from, date_to (YYYY-MM-DD), pay_scheme_id and report are required")
    if p["date_to"] < p["date_from"]:
        raise HTTPException(status_code=400, detail="date_to cannot be earlier than date_from")

    if lst == "payreg" and kind == "statement":
        entry = STATEMENTS.get(p["report"])
        if not entry:
            raise HTTPException(status_code=400,
                                detail="Pay Statement exists for Main Pay Roll (1/2/7), Trainee (6) and P.Tax Report (15) only")
        render, load, stem = entry
    elif lst == "payreg":
        if p["report"] not in reg.PAY_REPORTS or (kind == "payslip" and p["report"] in (8, 16, 30)) \
                or (kind == "register" and p["report"] in (16, 30)):
            raise HTTPException(status_code=400, detail=NO_PRINT["payreg"])
        render, load, stem = _payreg_print(kind, p["report"])
    else:
        entry = PRINTS.get((lst, kind), {}).get(p["report"])
        if not entry:
            raise HTTPException(status_code=400, detail=NO_PRINT[lst])
        render, load, stem = entry

    rows = load(db, p)
    if not rows:
        raise HTTPException(status_code=404, detail="No data to print for the selected period and pay scheme")

    co = db.execute(get_company_name(), {"co_id": int(q["co_id"])}).fetchone() if q.get("co_id") else None
    scheme = db.execute(text("SELECT payscheme_name FROM pay_scheme_master WHERE payscheme_id = :id"),
                        {"id": p["pay_scheme_id"]}).fetchone()
    ctx = {
        "company_name": (co._mapping["co_name"] if co else "") or "Company",
        "scheme_name": (scheme[0] if scheme else "") or str(p["pay_scheme_id"]),
        "date_from": p["date_from"], "date_to": p["date_to"], "report": p["report"],
    }
    body = render(rows, ctx)
    name = f"{stem}_{p['report']}_{p['date_from']}_to_{p['date_to']}.txt"
    return StreamingResponse(
        iter([body.encode("utf-8")]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
