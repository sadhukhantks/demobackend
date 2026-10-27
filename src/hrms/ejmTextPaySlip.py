"""EJM Pay Slip dot-matrix text prints (legacy ``Data_entry::payslipprint``).

Fixed-width ``.txt`` for continuous stationery, one layout per legacy ``holget``
code, reproduced byte-for-byte from the PHP ``$logMsg`` string builder:

* 3       — 83-col two-up slip (two slips per printed row, 5 rows per page,
            form feed on dept change, net ``*``-padded to 6).
* 4, 5    — "Voucher Payslip for FNE …" (79-col ruler; 5 slips per page — the
            legacy ``noslp>=6`` test starts at 1 — Dept Total on dept change).
* 6       — 136-col Pay Ticket with a stub past the ruler, 5 per page.
* 1, 2, 7 — 136-col main pay slip, 6 per page, form feed on dept change;
            code 2 rates from ``basic_rate``, the others ``prod_basic/working_hours``.

``render(code, rows, ctx)`` — rows ordered by dept_code, eb_no; numerics are
floats, texts are str. Control chars kept verbatim: ``\\x0c`` form feed,
``\\x0e`` double-width on, ``\\x0f`` condensed on, ``\\x12`` condensed off.

Substitutions / gaps vs the legacy source
=========================================

======================================  ==========================================================
Legacy                                  Here
======================================  ==========================================================
``$company_name`` (session)             ``ctx["company_name"]`` — accepted, never printed: every
                                        legacy ``$company_name`` line is commented out.
``$payschemeName`` GET param; code 6    ``ctx["scheme_name"]`` everywhere the name was printed
overrode it with att_payschm 159/160/   (4/5 heading, 6 double-width banner).
158 → " (1)" / " (2)" / " (3)"
``$periodtodate``                       ``ctx["date_to"]`` — raw ISO in the 4/5 heading,
                                        dd/mm/yyyy (3), dd-mm-yyyy (6), dd-mm-yy (1/2/7).
``$periodfromdate``                     ``ctx["date_from"]`` — accepted, never printed (query only).
code 3 ``gross_deduction``              not in the pivot → ``row.get(...)`` → "0.00".
code 6 ``ptax``                         not in the pivot → ``row.get(...)`` → "0.00".
code 3 ``net100``                       a model-side column the layout never prints; not computed.
``OCC`` / ``shrcode`` / ``occu_desc``   external DB gone; the layouts only ever print ``desig``.
4/5 ``$$periodtodate`` on detail line   undefined variable-variable → ''.
4/5 ``$Blnk`` on the Dept Total line    undefined → ''.
1/2/7 ``$miscded``                      literal 0 in legacy → "0".
code 3 shift column                     legacy hard-codes " A " — kept literal.
======================================  ==========================================================

Deliberately dropped (never changes output): the 4/5 ``lnn>58`` page test
(dead — lnn tops out at 38 with 5 slips per page), the code 6 running totals
(accumulated, never printed; the grand total is commented out in legacy), and
the last dept's Dept Total in 4/5 (legacy only prints it on dept *change*).
"""
from decimal import Decimal, ROUND_HALF_UP

FF = "\x0c"      # Chr(12) form feed
DW = "\x0e"      # chr(14) double-width on
CON = "\x0f"     # chr(15) condensed on
COFF = "\x12"    # chr(18) condensed off


# ─── PHP formatting helpers ──────────────────────────────────────────
def _q(v, d):
    """PHP round(): half away from zero on the decimal repr, d places."""
    # ponytail: NULL DB column -> 0, as PHP number_format(null)/(float)null did
    return Decimal(repr(float(v or 0))).quantize(Decimal(1).scaleb(-d), ROUND_HALF_UP)


def nf(v, d=2, commas=True):
    """PHP number_format(v, d); commas=False for the str_replace(',', '') wrap."""
    return f"{_q(v, d):,.{d}f}" if commas else f"{_q(v, d):.{d}f}"


def dec(v):
    """A bare DECIMAL(…,2) column as MySQL/PHP printed it."""
    return f"{float(v or 0):.2f}"


def num(v):
    """A PHP float echoed: 8.0 → "8", 8.5 → "8.5" (precision 14)."""
    return f"{float(v or 0):.14g}"


def rnd(v, d):
    """PHP round(v, d) then echoed."""
    return num(_q(v, d))


def rj(s, w):
    s = str(s)
    return " " * (w - len(s)) + s     # " " * negative == "" — PHP str_repeat clamp


def lj(s, w):
    s = str(s)
    return s + " " * (w - len(s))


def dmy(iso, sep):
    return f"{iso[8:10]}{sep}{iso[5:7]}{sep}{iso[0:4]}"


# ─── holget 3: two-up slip ───────────────────────────────────────────
def two_up(rows, ctx):
    hd1 = "DP S SRL T/No. Name            F/E Date | DP S SRL T/No. Name            F/E Date |"
    hd2 = "ESI NO     TOT-HRS MISC-EARN   TOT-EARN | ESI NO     TOT-HRS MISC-EARN   TOT-EARN |"
    hd3 = "ESIC  ADVANCE  MISC_DED  TOT-DED    NET | ESIC  ADVANCE  MISC_DED  TOT-DED    NET |"
    bkline = "                                        |                                         |"
    bklin = "----------------------------------------|-----------------------------------------|"
    enddate = dmy(ctx["date_to"], "/")
    out = []

    def flush(l1, l2, l3):
        out.extend([bkline, hd1, l1, bkline, hd2, l2, bkline, hd3, l3, bkline, bklin])

    pg, rnop, dpc = 1, 0, ""
    l1 = l2 = l3 = ""
    for r in rows:
        if dpc != r["dept_code"]:
            if dpc:
                if rnop % 2 == 1:
                    flush(l1, l2, l3)
                out.append(FF)
                pg, rnop = 1, 0
            dpc = r["dept_code"]
        if pg > 5:
            out.append(FF)
            pg = 1
        rnop += 1
        whrs = num(r["working_hrs"] + r["ns_hrs"] + r["hol_hrs"])
        np_ = nf(r["netpay"], 0)
        c1 = f"{r['dept_code']} A {rj(rnop, 3)} {lj(r['eb_no'], 5)} {lj(r['wname'][:13], 14)}{enddate} | "
        c2 = (rj(r["esi_no"], 10) + rj(whrs, 8) + rj(dec(r["arrear_plus"]), 10)
              + rj(dec(r["total_earn"]), 11) + " | ")
        c3 = (rj(nf(r["esi"], 0), 4) + rj(dec(r["exadvance"]), 9) + rj(dec(r["arrear_minus"]), 10)
              + rj(dec(r.get("gross_deduction", 0.0)), 9) + rj("*" * (6 - len(np_)) + np_, 7) + " | ")
        if rnop % 2 == 1:
            l1, l2, l3 = c1, c2, c3
        else:
            flush(l1 + c1, l2 + c2, l3 + c3)
            pg += 1
    if rnop % 2 == 1:
        flush(l1, l2, l3)
    out.append(FF)
    return "\n".join(out) + "\n"


# ─── holget 4 / 5: voucher payslip ───────────────────────────────────
def voucher(rows, ctx):
    line = "-" * 79
    ln2 = f"Voucher Payslip for FNE {ctx['date_to']}              Dept Code "
    ln4 = "  EBNO  | NAME                | TOTAL | RATE  | ADJ AMT | AMOUNT   | SIGNATURE "
    ln6 = "        |                     | HOURS |       | ADV DED |          | "
    ln7 = "        |                     |       |       | ADH AMT |          |"
    out = []
    pg, noslp, dpc, tothrs, totamt = 1, 1, "", 0.0, 0.0
    for r in rows:
        if dpc != r["dept_code"]:
            if pg > 1:
                out.append(" " * 10 + lj(f"Dept Total {dpc}"[:20], 20) + rj(num(tothrs), 9)
                           + " " * 16 + rj(num(totamt), 10))
                tothrs = totamt = 0.0
                out.extend(["pgb" + line, FF])
                noslp = 1
            dpc = r["dept_code"]
            pg += 1
        if noslp >= 6:
            out.extend(["pgb" + line, FF])
            noslp = 1
        out.extend([ctx["scheme_name"], ln2 + dpc, f"{noslp}{line}", ln4, ln6, ln7, line])
        whrs = nf(r["working_hours"] + r["holiday_hr"] + r["ot_hours"])
        out.append(lj(r["eb_no"], 10) + lj(r["wname"][:20], 20) + rj(whrs, 9) + rj(dec(r["rate_per_day"]), 8)
                   + rj(nf(r["arrear_plus"] + r["arrear_minus"]), 10) + rj(nf(r["net_payable"], 0), 10))
        out.append(" " * 47 + rj(nf(r["advance"]), 10))
        out.extend([" ", " ", " ", line])
        totamt += r["net_payable"]
        # ponytail: legacy summed the comma-formatted string (PHP reads "1,008.00" as 1);
        # sum the printed number instead — only differs above 999 hours a period.
        tothrs += float(whrs.replace(",", ""))
        noslp += 1
    out.extend([FF, line])
    return "\n".join(out) + "\n"


# ─── holget 6: pay ticket ────────────────────────────────────────────
def pay_ticket(rows, ctx):
    line = "-" * 136
    head = [
        DW + ctx["scheme_name"],
        "Pay Ticket for FNE " + dmy(ctx["date_to"], "-"),
        CON + line,
        "Dept Code    EB No             W.Hours    BASIC        (12%)    Tiffin    Washing Allow     P.Tax     Total Earn   R/off        (13.00%)|  PAY TICKET ",
        "Dept Name    Name                Rate     Other        (0.75%)    Conv       Gross2         Advance   Total Ded   Net Pay        (3.25%)|  ---------- ",
        "                                                                         Adjust  Amt                                                    |",
        line,
    ]
    out = []
    pg = 1
    for r in rows:
        if pg > 5:
            out.append(FF + COFF)
            pg = 1
        out.extend(head)
        whrs = nf(r["working_hours"] + r["night_shift_hr"] + r["holiday_hr"])
        net = nf(r["net_payable"])
        out.append(" " * 13 + lj(r["eb_no"], 10) + rj(whrs, 16) + rj(dec(r["basic"]), 11) + rj(dec(r["epf"]), 8)
                   + rj(dec(r["tiffin_amount"]), 14) + rj(dec(r["washing_allowance"]), 15)
                   + rj(nf(r.get("ptax", 0.0)), 11) + rj(nf(r["gross2"]), 14) + rj(nf(r["b_f"]), 11)
                   + rj(dec(r["empl_epf"]), 13) + "|  EB NO : " + r["eb_no"])
        out.append(" " + r["dept_code"] + " " * 10 + lj(r["wname"][:20], 20) + rj(nf(r["rate_per_day"]), 6)
                   + rj(nf(r["other_allowance"]), 11) + rj(nf(r["esi"]), 8) + rj(nf(r["conv_allowance"]), 14)
                   + rj(nf(r["gross2"]), 15) + rj(nf(r["advance"]), 11) + rj(nf(r["gross_ded"]), 14)
                   + rj(net, 11) + rj(nf(r["empl_esi"]), 13) + "|  NAME  :" + r["wname"])
        out.extend([" " * 136 + "|  NET PAY " + net, " ", line, line])
        pg += 1
    out.extend([FF, line, line, FF])
    return "\n".join(out) + "\n"


# ─── holget 1 / 2 / 7: main pay slip ─────────────────────────────────
def main_slip(rows, ctx, code):
    d = ctx["date_to"]
    fne = f"{d[8:10]}-{d[5:7]}-{d[2:4]}"
    out = []
    pg, rnop, dpc = 1, 0, ""
    pre = ""   # legacy Chr(12) had no "\n": the form feed opens the next slip's first line
    for r in rows:
        if dpc != r["dept_code"]:
            if dpc:
                pre += FF
            dpc, pg, rnop = r["dept_code"], 1, 0
        if pg > 6:
            pre += FF
            pg = 1
        rnop += 1

        def n(k, dd=2):
            return nf(r[k], dd, commas=False)

        wh = r["working_hours"]
        brate = "0"
        if wh > 0:
            brate = nf(r["basic_rate"] if code == 2 else r["prod_basic"] / wh, 3)
        dsg = lj(r["desig"][:4], 4)
        wnam = lj(r["wname"][:15], 15)
        tag = f"{r['dept_code']}/{r['shift']}/{rnop:03d}"
        npay, tern = n("net_pay", 0), n("total_earn")

        out.append(pre + f" {tag}    {lj(r['eb_no'], 9)}{r['time_piece']} {tag}    {lj(r['eb_no'], 9)}{dsg} "
                   + r["esi_no"][3:10] + rj(brate, 8) + rj(n("working_hours"), 7) + rj(n("ns_hrs", 1), 5)
                   + rj(n("ls_hrs", 1), 4) + rj(n("hl_hrs", 0), 4) + " " * 3 + rj(n("stl_d", 0), 4)
                   + rj(n("c_pf_cont", 0), 5) + rj(n("c_fpf_cont", 0), 5) + rj(n("gross_pay"), 8)
                   + rj(nf(r["wrk_days"], 0), 3) + rj(rnd(r["c_work_day"], 1), 6) + rj(n("c_bon_ern"), 9)
                   + rj(nf(r["b_f"]), 5))
        pre = ""
        out.extend(["", ""])
        out.append(f" {wnam}  {dsg}  {wnam}" + rj(n("prod_basic"), 8)
                   + rj(nf(r["time_rated_basic"] + r["fix_basic"], 2, False), 7) + " " + rj(rnd(r["da"], 1), 7)
                   + " " * 6 + rj(n("increment_amt"), 4) + rj(dec(r["ns_amount"]), 7) + rj(n("hol_amt"), 7)
                   + rj(n("layoff_wgs"), 6) + " " * 12 + rj(n("pf_gross"), 8) + rj(n("misc_earn"), 8)
                   + rj(n("stl_wgs"), 8) + rj(n("hra"), 8))
        out.extend(["", ""])
        out.append(f" {fne} {lj(r['pf_no'], 6)}" + rj(npay, 6) + rj(npay, 7) + rj(n("total_deduction"), 9)
                   + rj(tern, 9) + rj(nf(r["c_f"]), 6) + rj("0", 6) + " " * 13 + rj(n("co_loan", 0), 4)
                   + rj(nf(r["puja_advance"] + r["stl_advance"], 0, False), 6) + rj(n("p_tax", 0), 7)
                   + rj(n("esic", 0), 6) + rj(n("epf", 0), 6) + " " * 4 + rj(n("co_loan_bal"), 9)
                   + " " * 11 + fne)
        out.append("")
        out.append(" " * 28 + "Total" + rj(tern, 10) + " " * 15 + lj(r["wname"], 39) + " P.F. No - "
                   + rj(r["pf_no"], 6))
        out.extend(["", ""])
        if pg < 6:
            out.append("")
        pg += 1
    out.append(FF)
    return "\n".join(out) + "\n"


def render(code, rows, ctx):
    if code == 3:
        return two_up(rows, ctx)
    if code in (4, 5):
        return voucher(rows, ctx)
    if code == 6:
        return pay_ticket(rows, ctx)
    if code in (1, 2, 7):
        return main_slip(rows, ctx, code)
    raise ValueError(f"No legacy pay slip print for code {code} (11-13 are NJM reports)")
