"""Dot-matrix Pay Register text prints — port of the legacy Data-Portal-Php
``Data_entry::payregisterprint()`` (lines 5036-6176), the ``$logMsg`` string
builders only. Stdlib, no DB, no FastAPI: ``render(code, rows, ctx) -> str``.

Layouts (legacy ``$holget``):

=====  ====  =========================================================
code   cols  layout
=====  ====  =========================================================
1/2/7  133   Pay Register, 3 lines/employee, new page per department, no totals
3      79    Voucher Register (Mill Retired) + Voucher Summary Register
4/5    79    Voucher Register (Card Holder bank/cash) + Voucher Summary Register
6      136   Pay Register (Trainee), 2 lines/employee, Grand Total
8      85    PAYIMAGE COMPARATIVE STATEMENT, Dept Total + Grand Total
=====  ====  =========================================================

Substitutions / simplifications vs the PHP:

======================================  ==========================================================
legacy                                  here
======================================  ==========================================================
``$this->session->userdata(companyname)``  ``ctx["company_name"]``
``'THE EMPIRE JUTE CO. LTD'`` (code 8)  ``ctx["company_name"]``
``$payschemeName`` / hard-coded          ``ctx["scheme_name"]`` everywhere
``'(1)'/'(2)'/'(3)'/' FORTNIGHTLY …'``
``$periodtodate`` / ``$periodtdate``     ``ctx["date_to"]`` (YYYY-MM-DD / dd-mm-yyyy as legacy)
``$periodfrmdate``                       ``ctx["date_from"]`` as dd-mm-yyyy
``$$periodtodate`` (undefined var-var)   '' (legacy printed nothing)
OCC column (external DB, gone)           ``desig[:4]`` — exactly what legacy printed anyway
``LNBL`` / CO-LNBAL header column        never printed by legacy either; ``co_loan_bal`` unused
ADC-INC column (1/2/7)                   9 blanks, as legacy
code 6 ``$record->ptax``                 not in the legacy SELECT → "0.00", total "0" (as legacy)
code 3 ``WORKING_HOURS/HOLIDAY_HR/…``    legacy SELECT used other aliases so every code-3 value
                                        printed as 0; here mapped to ``working_hrs``, ``hol_hrs``,
                                        ``rate_per_day``, ``arrear_plus/minus``, ``exadvance``,
                                        ``netpay``; OT hours → 0 (no ``ot_hours`` on code-3 rows)
``getpayregistersummdata``               summary grouped in Python per ``dept_code``
code 8 leading "Total Rec 0" block + FF  skipped (legacy emitted an empty dept total before
                                        the first department because ``$dpc`` started as '')
PHP ``"1,234.50" + 0 == 1``             totals summed from floats, not from the formatted strings
``str_repeat(' ', n<0)``                 clamped to 0 (PHP returns '')
``number_format`` / ``round``            half-away-from-zero via ``Decimal``
PHP float → string                      ``%.14G`` (``pf``)
======================================  ==========================================================

Row contract: ordered by dept_code, eb_no; numeric values are floats (never
None), text values are str. No grand-total row — totals are computed here.
"""
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby

FF = "\x0c"


# ─── PHP formatting helpers ──────────────────────────────────────────
def php_round(v: float, d: int = 0) -> float:
    return float(Decimal(str(v)).quantize(Decimal(1).scaleb(-d), ROUND_HALF_UP))


def nf(v: float, d: int = 2) -> str:
    """PHP number_format($v, $d): rounded, thousands commas."""
    return f"{php_round(v, d):,.{d}f}"


def dec(v: float) -> str:
    """A bare MySQL DECIMAL(…,2) column as PHP printed it."""
    return f"{v:.2f}"


def pf(v: float) -> str:
    """PHP float → string (precision=14): 1234.0 → '1234', 12.5 → '12.5'."""
    # ponytail: PHP prints 1.0E+15 where %G prints 1E+15; payroll never gets there.
    return f"{v + 0.0:.14G}"  # + 0.0 folds -0.0 into 0 (legacy never printed '-0')


def whole(v: float) -> str:
    """PHP round($v, 0) printed: '1235'."""
    return pf(php_round(v))


def rj(s: str, n: int) -> str:
    return " " * max(0, n - len(s)) + s


def lj(s: str, n: int) -> str:
    return s + " " * max(0, n - len(s))


def _dmy(iso: str) -> str:
    return f"{iso[8:10]}-{iso[5:7]}-{iso[0:4]}"


# ─── entry point ─────────────────────────────────────────────────────
def render(code: int, rows: list[dict], ctx: dict) -> str:
    if code in (1, 2, 7):
        return _pay_register(rows, ctx)
    if code in (3, 4, 5):
        return _voucher_register(code, rows, ctx)
    if code == 6:
        return _trainee_register(rows, ctx)
    if code == 8:
        return _comparative(rows, ctx)
    raise ValueError(f"no text pay-register layout for code {code}")


# ─── 1 / 2 / 7 : 133-col Pay Register ────────────────────────────────
def _pay_register(rows: list[dict], ctx: dict) -> str:
    line = "=" * 133 + "\n"
    ln1 = f"{ctx['company_name']}   {ctx['scheme_name']}                F/E:{ctx['date_to']}                  PAGE  "
    ln4 = "Dpt S SRLNO EB-NO T/P <-----NAME-----> OCC BAS-RATE  WRKHRS  NSHRS  HLHRS  LOFHR STLD WRKDAY C-WRKDAY C-BON-ERN  PF-GROSS  CO-LNBAL  \n"
    ln6 = "..BASIC..FIX-BASIC ...DA... ..NS-AMT  .HOL-AMT INCREMENT ADC-INC LOFF-WG INCENTIVE MISC-ERN .STL-WG. GROSS-PAY ..HRA...B/F. TO-EARN \n"
    ln7 = "..ESI-NO..PF-NO C-PF-CONT C-FPF-CONT ESIGROSS PF-CONT FPF-CONT CO-CONT .ESI. PTAX LWF CO-LN  PUJA-ADV  STLADV T-DEDN .C/F.  NET-PAY\n"
    out = []
    lnn, pg, dpc = 1, 1, None
    for r in rows:
        if lnn > 58:
            out.append(FF + "\n")
            lnn = 1
            pg += 1
        if dpc != r["dept_code"]:
            out.append(FF + "\n")
            lnn, pg, dpc = 1, 1, r["dept_code"]
        if lnn == 1:
            out += [ln1 + str(pg) + "\n", ln4, ln6, ln7, line]
            lnn += 6

        total_earn = dec(r["total_earn"])
        out.append(
            lj(r["dept_code"], 10) + rj(r["eb_no"], 6) + rj(r["time_piece"], 3) + "  "
            + lj(r["wname"][:15], 20) + r["desig"][:4]
            + rj(dec(r["basic_rate"]), 6) + rj(dec(r["working_hours"]), 8)
            + rj(dec(r["ns_hrs"]), 7) + rj(dec(r["hl_hrs"]), 7) + rj(dec(r["ls_hrs"]), 7)
            + rj(dec(r["stl_d"]), 6) + rj(dec(r["wrk_days"]), 7) + rj(dec(r["c_work_day"]), 7)
            + rj(dec(r["c_bon_ern"]), 11) + rj(dec(r["pf_gross"]), 12) + "\n"
        )
        out.append(
            rj(dec(r["prod_basic"]), 9) + rj(pf(r["fix_basic"] + r["time_rated_basic"]), 9)
            + rj(dec(r["da"]), 9) + rj(dec(r["ns_amount"]), 7) + rj(dec(r["hol_amt"]), 9)
            + rj(dec(r["increment_amt"]), 9) + " " * 9 + rj(dec(r["layoff_wgs"]), 9)
            + rj(dec(r["incentive_amount"]), 9) + rj(dec(r["misc_earn"]), 9)
            + rj(dec(r["stl_wgs"]), 9) + rj(dec(r["gross_pay"]), 10) + rj(dec(r["hra"]), 8)
            + rj(dec(r["b_f"]), 6) + rj(total_earn, 10) + "\n"
        )
        out.append(
            r["esi_no"][:10] + " " + r["pf_no"] + "  " + dec(r["c_pf_cont"]) + "     " + dec(r["c_fpf_cont"])
            + rj(total_earn, 10) + rj(dec(r["epf"]), 8) + rj(whole(r["epf_833"]), 7)
            + rj(whole(r["epf_167"]), 7) + rj(whole(r["esic"]), 6) + rj(whole(r["p_tax"]), 6)
            + rj(whole(r["lwf"]), 6) + rj(whole(r["co_loan"]), 6) + rj(dec(r["puja_advance"]), 6)
            + rj(dec(r["stl_advance"]), 9) + rj(dec(r["total_deduction"]), 9)
            + rj(dec(r["c_f"]), 6) + rj(dec(r["net_pay"]), 7) + "\n"
        )
        out.append(line)
        lnn += 4
    return "".join(out)


# ─── 3 / 4 / 5 : 79-col Voucher Register + Summary ───────────────────
def _voucher_fields(code: int, r: dict) -> tuple[float, float, float, float, float]:
    """(hours, rate, arrear, advance, net) — legacy adds ARR_PLUS + ARR_MINUS."""
    if code == 3:
        return (r["working_hrs"] + r["hol_hrs"], r["rate_per_day"],
                r["arrear_plus"] + r["arrear_minus"], r["exadvance"], r["netpay"])
    return (r["working_hours"] + r["holiday_hr"] + r["ot_hours"], r["rate_per_day"],
            r["arrear_plus"] + r["arrear_minus"], r["advance"], r["net_payable"])


def _voucher_total(label: str, hrs: float, amt: float) -> str:
    return " " * 10 + lj(label[:20], 20) + rj(pf(hrs), 9) + " " * 8 + rj(pf(amt), 10) + "\n"


def _voucher_register(code: int, rows: list[dict], ctx: dict) -> str:
    line = "-" * 79 + "\n"
    scheme, to = ctx["scheme_name"], ctx["date_to"]
    ln2 = f"Voucher Register for FNE To {to}           Dept Code "
    header = [
        "  EBNO  | NAME                | TOTAL | RATE  | ADJ AMT | AMOUNT   | SIGNATURE \n",
        "        |                     | HOURS |       | ADV DED |          | \n",
        "        |                     |       |       | ADH AMT |          |\n",
    ]
    out = []
    lnn, dpc = 1, None
    tothrs = totamt = gtothrs = gtotamt = 0.0
    for r in rows:
        if dpc != r["dept_code"]:
            if dpc is not None:
                out += [_voucher_total(f"Dept Total {dpc}", tothrs, totamt), line, " \n", " \n"]
                tothrs = totamt = 0.0
                lnn += 2
            dpc = r["dept_code"]
        if lnn > 58:
            out.append(FF + "\n")
            lnn = 1
        if lnn == 1:
            out += [scheme + "\n", ln2 + dpc + "\n", scheme + "\n", line, *header, line]
            lnn = 8
        hrs, rate, arr, adv, net = _voucher_fields(code, r)
        whrs = nf(hrs)
        out.append(
            lj(r["eb_no"], 10) + lj(r["wname"][:20], 20) + rj(whrs, 9) + rj(dec(rate), 8)
            + rj(nf(arr), 10) + rj(nf(net, 0), 10) + "\n"
        )
        out.append(" " * 10 + lj(f"Dept Code {dpc}"[:20], 20) + " " * 17 + rj(nf(adv), 10) + "\n")
        out += [" \n", " \n", " \n", line]
        hrs = php_round(hrs, 2)  # legacy summed the number_format'd string
        tothrs += hrs
        totamt += net
        gtothrs += hrs
        gtotamt += net
        lnn += 6
    out += [_voucher_total(f"Dept Total {dpc}", tothrs, totamt), line, line,
            _voucher_total("Grand Total", gtothrs, gtotamt), line, FF + "\n"]

    # Voucher Summary Register — legacy getpayregistersummdata, grouped here.
    out += [scheme + "\n", f"Voucher Summary Register for FNE To {to}           Dept Code \n",
            scheme + "\n", line,
            "  DEPT CODE  | TOTAL HOURS            | TOTAL AMOUNT    |  DEPARTMENT.\n", line]
    stothrs = stotamt = 0.0
    for dept, grp in groupby(rows, key=lambda r: r["dept_code"]):
        grp = list(grp)
        hrs = sum(_voucher_fields(code, r)[0] for r in grp)
        amt = sum(_voucher_fields(code, r)[4] for r in grp)
        out.append("    " + lj(dept, 15) + rj(nf(hrs), 14) + rj(nf(amt, 0), 18)
                   + "        " + grp[0]["dept_desc"] + "\n")
        out.append(line)
        stothrs += php_round(hrs, 2)
        stotamt += amt
    out.append("    " + lj("Grand Total", 15) + rj(pf(stothrs), 14) + rj(pf(stotamt), 18) + "\n")
    out.append(FF + "\n")
    return "".join(out)


# ─── 6 : 136-col Trainee Pay Register ────────────────────────────────
def _trainee_register(rows: list[dict], ctx: dict) -> str:
    line = "-" * 136 + "\n"
    scheme = ctx["scheme_name"]
    ln2 = (f"Pay Register for the period from  {_dmy(ctx['date_from'])}    FNE To{_dmy(ctx['date_to'])}"
           "           Dept Code ")
    header = [
        "Dept Code    EB No             W.Hours    BASIC        (12%)    Tiffin    Washing Allow     P.Tax     Total Earn   R/off        (13.00%)\n",
        "Dept Name    Name                Rate     Other        (0.75%)    Conv       Gross2         Advance   Total Ded   Net Pay        (3.25%)\n",
        "                                                                         Adjust  Amt                                                  \n",
    ]
    out = []
    lnn, dpc = 1, ""
    g = dict.fromkeys(("hrs", "bas", "epf", "esi", "tiffin", "wash", "gross", "roff",
                       "emppf", "other", "conv", "gros2", "adv", "ded", "net"), 0.0)
    for r in rows:
        # ponytail: legacy's per-dept reset block is dead code ($pg never increments) — dropped.
        dpc = r["dept_code"]
        if lnn > 58:
            out.append(FF + "\n")
            lnn = 1
        if lnn == 1:
            out += [scheme + "\n", ln2 + dpc + "\n", line, *header, line]
            lnn = 8
        whrs = nf(r["working_hours"] + r["night_shift_hr"] + r["holiday_hr"])
        gross2 = nf(r["gross2"])
        esi = nf(r["esi"])
        b_f = nf(r["b_f"])
        out.append(
            " " * 13 + lj(r["eb_no"], 10) + rj(whrs, 16) + rj(dec(r["basic"]), 11)
            + rj(dec(r["epf"]), 8) + rj(dec(r["tiffin_amount"]), 14)
            + rj(dec(r["washing_allowance"]), 15) + rj("0.00", 11) + rj(gross2, 14)
            + rj(b_f, 11) + rj(dec(r["empl_epf"]), 13) + "\n"
        )
        out.append(
            " " + dpc + " " * 10 + lj(r["wname"][:20], 20) + rj(nf(r["rate_per_day"]), 6)
            + rj(nf(r["other_allowance"]), 11) + rj(esi, 8) + rj(nf(r["conv_allowance"]), 14)
            + rj(gross2, 15) + rj(nf(r["advance"]), 11) + rj(nf(r["gross_ded"]), 14)
            + rj(nf(r["net_payable"]), 11) + rj(nf(r["empl_esi"]), 13) + " " * 8 + "\n"
        )
        out += [" \n", " \n", line]
        lnn += 6
        g["hrs"] += php_round(r["working_hours"] + r["night_shift_hr"] + r["holiday_hr"], 2)
        g["bas"] += r["basic"]
        g["epf"] += r["epf"]
        g["esi"] += php_round(r["esi"], 2)
        g["tiffin"] += r["tiffin_amount"]
        g["wash"] += r["washing_allowance"]
        g["gross"] += r["total_earn"]
        g["roff"] += php_round(r["b_f"], 2)
        g["emppf"] += r["empl_epf"]
        g["other"] += r["other_allowance"]
        g["conv"] += r["conv_allowance"]
        g["gros2"] += r["gross2"]
        g["adv"] += r["advance"]
        g["ded"] += r["gross_ded"]
        g["net"] += r["net_payable"]
    out += [FF + "\n", line]
    out.append(
        " " * 8 + lj("Grand Total", 15) + rj(pf(g["hrs"]), 16) + rj(pf(g["bas"]), 11)
        + rj(pf(g["epf"]), 8) + rj(pf(g["tiffin"]), 14) + rj(pf(g["wash"]), 15) + rj("0", 11)
        + rj(pf(g["gross"]), 14) + rj(pf(g["roff"]), 11) + rj(pf(g["emppf"]), 13) + "\n"
    )
    out.append(  # legacy repeats gesi in the (3.25%) column, not the employer ESI sum
        " " + dpc + " " * 36 + rj(pf(g["other"]), 11) + rj(pf(g["esi"]), 8)
        + rj(pf(g["conv"]), 14) + rj(pf(g["gros2"]), 15) + rj(pf(g["adv"]), 11)
        + rj(pf(g["ded"]), 14) + rj(pf(g["net"]), 11) + rj(pf(g["esi"]), 13) + "\n"
    )
    out += [line, FF + "\n"]
    return "".join(out)


# ─── 8 : 85-col PAYIMAGE COMPARATIVE STATEMENT ───────────────────────
def _rec_total(n: int, gp: float, np_: float) -> str:
    return " " * 10 + "Total Rec" + rj(str(n), 9) + " " * 9 + rj(pf(gp), 36) + rj(pf(np_), 10) + "\n"


def _comparative(rows: list[dict], ctx: dict) -> str:
    line = "=" * 85 + "\n"
    scheme = ctx["scheme_name"]
    ln2 = f"{ctx['company_name']}   PAYIMAGE COMPARATIVE STATEMENT F/E {ctx['date_to']}"
    ln4 = "DPT EMPNO <----NAME---->  O-PFGRS  N-PFGRS O-DED N-DED O-NET N-NET  DIF-PFGR  DIF-NET\n"
    out = []
    lnn, dpc = 1, None
    trec = grec = 0
    tgp = tnp = ggp = gnp = 0.0
    for r in rows:
        if dpc != r["dept_code"]:
            if dpc is not None:
                out += [line, _rec_total(trec, tgp, tnp), line, " \n", " \n", FF + "\n"]
                trec, tgp, tnp, lnn = 0, 0.0, 0.0, 1
            dpc = r["dept_code"]
        if lnn > 58:
            out.append(FF + "\n")
            lnn = 1
        if lnn == 1:
            out += [scheme + "\n", ln2 + "  Dept " + dpc + "\n", scheme + "\n", line, ln4, line]
            lnn = 8
        gpdif = php_round(r["diff_gross"], 2)
        npdif = php_round(r["diff_net"])
        gp, gp100 = dec(r["gross_pay"]), dec(r["gross_pay100"])
        out.append(  # legacy pads O-PFGRS by the width of GROSS_PAY, then prints GROSS_PAY100
            dpc + "  " + lj(r["eb_no"], 6) + lj(r["wname"][:15], 15)
            + " " * max(0, 9 - len(gp)) + gp100 + " " * max(0, 9 - len(gp100)) + gp + " " * 8
            # N-NET widened from legacy's 6 — today's nets are 7+ chars and glued.
            + rj(whole(r["net100"]), 9) + rj(dec(r["net_pay"]), 9)
            + rj(pf(gpdif), 9) + rj(pf(npdif), 9) + "\n"
        )
        lnn += 1
        trec += 1
        grec += 1
        tgp += gpdif
        tnp += npdif
        ggp += gpdif
        gnp += npdif
    out += [_rec_total(trec, tgp, tnp), line, " \n", " \n",
            _rec_total(grec, ggp, gnp), line, " \n", " \n", FF + "\n"]
    return "".join(out)
