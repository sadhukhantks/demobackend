"""NJM pay slips as dot-matrix text (legacy ``Njmwagesprocess::njmcntwagespayslip``).

Two layouts, picked by ``kind``:

* ``main`` — legacy ``njmwagespayslip1641`` (scheme 164 njm_main_payroll; sls
  2015): a 12-line condensed slip per employee built from the period's pay
  components by CODE, 5 per page, department totals and a form feed on every
  department change. Reproduced from the PHP ``$logMsg`` builder, control chars
  verbatim (``\\x0f`` condensed on, ``\\x0e`` double-width, ``\\x12`` condensed
  off, ``\\x0c`` form feed).
* ``contractor`` — legacy ``njmwagespayslip163`` (scheme 163 NJM_CONT_WAGES_NEW;
  sls 3163) was an FPDF two-up slip; the same content here as 79-col text, two
  employees per row, their piece production from
  tbl_njm_cnt_wages_data_collection, 5 rows per page, a new page per department.
* ``contractor`` + ``fmt=pdf`` — that FPDF two-up itself (A4, Helvetica, the
  legacy cell geometry to the millimetre), drawn with reportlab. This is what
  the NJM Wages screen downloads for Contractor Wages Pay Slip.

Substitutions vs the legacy source
==================================
* Contractor PDF: a slip pair that would cross FPDF's 20 mm bottom margin moves
  whole to the next page (legacy let FPDF break it mid-slip and drew the right
  column at the stale y). The blank pages legacy's AddPage left after a 5th
  pair that was also the last of its department, and at the very end, are
  dropped.
* Hard-coded mill names ('LIMELIGHT COMM PVT LTD', 'NJM CO LTD/NELLIMARLA') ->
  the tenant's company name.
* Main: every amount prints as the DECIMAL(…,2) it is; legacy echoed a bare
  ``0`` for a component the employee had no row for.
* Main: the copy of the slip legacy printed for the LAST employee put
  ``$C_sundayadv_`` under DJ.A and a constant 0.00 under S.ADV; every other
  employee got ``$C_mveminus_`` / ``$C_sundayadv_``. The in-loop version is
  used for all.
* ``retired`` — legacy 167 (njm_retired_payroll; sls 3167) ran the same
  ``njmwagespayslip1641`` builder with two tweaks: ``$PFNO=''`` and the DJ.A box
  relabelled ADJ.A. Ported.
* ``newbudli`` — legacy 169 (njm_newbudli_payroll; sls 2017) and 174
  (njm_newbudli_rtd; sls 3174). Legacy's two blocks are byte-identical, so one
  kind prints both: the main layout with ADJ.A for DJ.A (PFN kept, unlike 167),
  O.A/``C_OTHER_ALLOW_`` in place of INC/``C_inc_``, FWI -> FWOA, SWI -> SWOA,
  and the P.F.G box printed blank — label and value both ($bln).
* Main: ``$RATE`` was the literal 5.54211; SLD/LODAY/ILT&PW/CDN/ILIN/ILDG/SWI
  were literal 0.00; ``$C_extrahoursincamt_`` (OTI) was never assigned.
* Contractor: "Quality" is the wages quality CODE, as legacy printed prod_id.
* Contractor PDF: the centred name sits ~1 pt off legacy's, because legacy's
  CONCAT(first,' ',middle,' ',last) left the blank middle name's spaces in and
  CONCAT_WS here drops them. Every other run matches to 0.1 pt.
* Contractor: a page break landing on a department change no longer prints a
  blank page (legacy AddPage'd twice).
"""
import io
from collections import defaultdict
from datetime import date
from itertools import groupby

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms import ejmPayRegister as reg
from src.hrms.ejmTextPaySlip import COFF, CON, DW, FF, dec, lj, nf, num, rj
from src.hrms.query import get_company_name

router = APIRouter()

SLIPS_PER_PAGE = 5

_EMP_IDENTITY = """
    k.EMPLOYEEID AS eb_id,
    IFNULL(od.emp_code, '') AS eb_no,
    CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
              NULLIF(TRIM(pd.last_name), '')) AS emp_name,
    IFNULL(sdm.sub_dept_code, '') AS dept_code,
    IFNULL(sdm.sub_dept_desc, '') AS department
"""
# Legacy department_master is this tenant's sub_dept_mst.
_EMP_JOINS = """
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = k.EMPLOYEEID AND od.active = 1
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = k.EMPLOYEEID
    LEFT JOIN sub_dept_mst AS sdm ON sdm.sub_dept_id = od.sub_dept_id
"""


# ─── main payroll (legacy njmwagespayslip1641) ───────────────────────
LEGACY_RATE = "5.5421"   # number_format('5.54211', 4) — a literal in the PHP
Z4 = "   0.00"           # str_repeat(' ', 4 - strlen(0.00)) . number_format(0.00, 2)
Z5 = "    0.00"

MAIN_QUERY = text(f"""
    SELECT {_EMP_IDENTITY},
           IFNULL(dsg.desig, '') AS designation,
           IFNULL(esi.esi_no, '') AS esi_no,
           COALESCE(NULLIF(pf.pf_no, ''), pf.pf_uan_no, '') AS pf_no,
           c.CODE AS code, k.AMOUNT AS amount
    FROM pay_employee_payroll AS k
    JOIN pay_components AS c ON c.ID = k.COMPONENT_ID
    {_EMP_JOINS}
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = od.designation_id
    LEFT JOIN hrms_ed_esi AS esi ON esi.eb_id = k.EMPLOYEEID AND esi.active = 1
    LEFT JOIN hrms_ed_pf AS pf ON pf.eb_id = k.EMPLOYEEID AND pf.active = 1
    WHERE k.PAYPERIOD_ID = :payperiod_id AND k.STATUS = 1
    ORDER BY dept_code, eb_no, k.EMPLOYEEID, c.CODE
""")

_IDENTITY_KEYS = ("eb_id", "eb_no", "emp_name", "dept_code", "department",
                  "designation", "esi_no", "pf_no")


def load_main(db, pid, p):
    """One dict per employee, in print order: identity + amounts {CODE: Decimal}."""
    emps, by_id = [], {}
    for r in db.execute(MAIN_QUERY, {"payperiod_id": pid}).mappings():
        e = by_id.get(r["eb_id"])
        if e is None:
            e = by_id[r["eb_id"]] = {**{k: r[k] for k in _IDENTITY_KEYS}, "amounts": {}}
            emps.append(e)
        e["amounts"][r["code"]] = r["amount"] or 0
    return emps


def main_slip(e, ctx):
    """The 12 lines of one employee's slip, concatenated exactly as the PHP did."""
    a = e["amounts"]

    def v(code):
        return dec(a.get(code, 0))

    def r8(code):
        return rj(v(code), 8)

    dept, eb, name = e["department"], e["eb_no"], e["emp_name"]
    # Legacy njm_retired_payroll (167) is this layout with $PFNO forced blank
    # and the DJ.A box relabelled ADJ.A — the value column stays 17 wide.
    # njm_newbudli (169/174) relabels DJ.A too, but keeps the PFN.
    kind = ctx.get("kind", "main")
    nb = kind == "newbudli"
    pfno = "" if kind == "retired" else e["pf_no"][:5]
    adj = ("   DJ.A:" + rj(v("C_mveminusamt_"), 9) if kind == "main"
           else "   ADJ.A:" + r8("C_mveminusamt_"))
    days = nf((float(a.get("C_wrkhrs1_", 0)) + float(a.get("C_PHours_", 0))
               + float(a.get("C_wrkhours2_", 0))) / 8)
    return [
        CON + "  " + lj(ctx["company_name"][:22], 22) + "  " + LEGACY_RATE
        + "  PAY SLIP FOR:" + rj(ctx["payslip_for"], 9) + "  DEPT: "
        + " " * max(0, 8 - len(dept)) + dept[:12] + "    # PFN:" + "  " * (5 - len(pfno)) + pfno
        + "   DAYS:" + days + " " * 17 + DW + ctx["print_date"],
        "   NAME  :" + name[:15] + "       DESG:" + e["designation"][:6] + "      ESINO: " + e["esi_no"]
        + "  TKT NO: " + eb + "  CODE:" + rj(eb, 6) + " " * 23 + "PAY SLIP FOR:" + ctx["payslip_for"],
        "   THR1  :" + r8("C_wrkhrs1_") + "   THR2 :" + r8("C_wrkhours2_") + "   PHRS :" + r8("C_PHours_")
        + "   CPN  :" + r8("C_nightdays_") + "   FHRS :" + r8("C_FHours_") + "   LHRS :" + r8("C_LayOffHrs_")
        + " " * 12 + "DEPT:" + dept,
        "   SLD   :" + Z4 + "    ELD  :" + r8("C_eldays_") + "   LODAY:" + Z5 + "   OTTHR:" + r8("C_extrahrstime_")
        + "   OTPHR:" + r8("C_extrahourspiece_") + "   ESID :" + r8("C_esi_days_") + " " * 12 + name,
        "   TWAGE :" + r8("C_timewages_") + adj + "   LOWI :" + r8("C_loffwagesincamt_")
        + "  ILT&PW:" + Z5 + "   S.ADV:" + r8("C_sundayadv_") + "   RSD  :" + r8("C_rsdamt_") + " " * 12 + "CODE: " + eb,
        "   P.WAGE:" + r8("C_pwage_") + "   DA   :" + r8("C_DAAMOUNT_") + "   LODG :" + r8("C_loffdagenincamt_")
        + "  INCENT:" + r8("C_pwagesinc_") + "   CDN  :" + Z5 + "   GDED :" + r8("C_TOTAL_DEDUCTION_") + " " * 12 + "TKT:" + eb,
        "   NA    :" + r8("C_na_") + "   EWI  :" + r8("C_eldwagesincamt_") + "   OTW  :" + r8("C_extrahourswages_")
        + "   ILIN :" + Z5 + "   PF   :" + r8("C_EPF_") + "   NPAY :" + r8("C_netpayamount_") + " " * 12 + DW + "NET:" + v("C_finalnetpay_"),
        "   GI    :" + r8("C_genincamt_") + "   EDG  :" + r8("C_elddagenincamt_") + "   OTI  :" + " " * 8
        + "   ILDG :" + Z5 + "   RENT :" + r8("C_rentdedamt_") + "   RNPAY:" + r8("C_NET"),
        ("   O.A   :" + r8("C_OTHER_ALLOW_") if nb else "   INC   :" + r8("C_inc_"))
        + "   FDG  :" + r8("C_festdagenincamt_") + "   ODG  :" + r8("C_exthrsdagincamt_")
        + "   GRS2 :" + r8("C_gross2amt_") + "   ESI  :" + r8("C_ESI_") + "   VARD1:" + r8("C_iftudedamt_"),
        ("   FWOA  :" if nb else "   FWI   :") + r8("C_festwagesincamt_")
        + "   SDG  :" + r8("C_sldwagesincamt_") + "   HRA  :" + r8("C_HRA_")
        + "  ADVDED:" + r8("C_exadvance_") + "   G.W.F:" + r8("C_govtwelfare_") + "   NET  :" + r8("C_finalnetpay_"),
        ("   SWOA  :" if nb else "   SWI   :") + Z5 + "   TOTAL:" + r8("C_totalwagesamt_")
        + "   GRS1 :" + r8("C_gross1amt_")
        + "   PTAX :" + r8("C_PTAX_") + "   LR   :" + r8("C_landrentamt_"),
        # 169/174 print the P.F.G box empty: legacy's label is 8 blanks and its
        # value $bln, so both keep their widths.
        "   PVE   :" + r8("C_mveplus_") + ("        :" + " " * 8 if nb else "   P.F.G:" + r8("C_PFG_"))
        + "   OP   :" + r8("C_otherpay_")
        + "   CANT :" + r8("C_canteen_") + "   PROD :" + r8("C_loomproducion_"),
        "",
    ]


def _dept_totals(pg, tot, vard, dept):
    return [f"    TOTAL  NO. OF PAYSLIPS :{pg}",
            f"    TOTAL NET AMOUNT:  {num(round(tot, 2))}           LOAN DED: {num(round(vard, 2))}"
            f"              DEPARTMENT :{dept}"]


def render_main(emps, ctx):
    out = []
    dpc, dept_prev = None, ""
    pg = pgn = 0            # slips in the department / on the page
    tot = vard = 0.0
    join_next = False       # legacy ended the dept-total line with a form feed and NO newline

    def emit(lines):
        nonlocal join_next
        if join_next:
            out[-1] += lines[0]
            lines = lines[1:]
            join_next = False
        out.extend(lines)

    for e in emps:
        if e["dept_code"] != dpc:
            if dpc is not None:
                head, tail = _dept_totals(pg, tot, vard, dept_prev)
                emit(["", "", "", head, tail + FF])
                join_next = True
                pg, tot, vard = 0, 0.0, 0.0
            dpc, pgn = e["dept_code"], 1
        dept_prev = e["department"]
        if float(e["amounts"].get("C_gross2amt_", 0)) <= 0:
            continue
        tot += float(e["amounts"].get("C_NET", 0))
        vard += float(e["amounts"].get("C_iftudedamt_", 0))
        pg += 1
        emit(main_slip(e, ctx))
        pgn += 1
        if pgn > SLIPS_PER_PAGE:
            out.append(COFF + FF)
            pgn = 1
    head, tail = _dept_totals(pg, tot, vard, dept_prev)
    emit(["", "", head, tail + COFF + FF, COFF + FF])
    return "\n".join(out) + "\n"


# ─── contractor (legacy njmwagespayslip163 / getcntwagespayslip) ─────
# pay_components ids, legacy verbatim (they match on sls; 76 is C_LTAs_ there).
CNT_COMPONENTS = {"otday": 280, "regday": 282, "trate": 70, "advance": 166, "ta": 76,
                  "esi": 19, "pf": 18, "plus_balance": 268, "net": 21}

CNT_QUERY = text(f"""
    SELECT {_EMP_IDENTITY},
           {", ".join(f"MAX(CASE WHEN k.COMPONENT_ID = {cid} THEN k.AMOUNT ELSE 0 END) AS {f}"
                      for f, cid in CNT_COMPONENTS.items())}
    FROM pay_employee_payroll AS k
    {_EMP_JOINS}
    WHERE k.PAYPERIOD_ID = :payperiod_id AND k.STATUS = 1
    GROUP BY k.EMPLOYEEID, eb_no, emp_name, dept_code, department
    HAVING net > 0
    ORDER BY dept_code, eb_no
""")

CNT_PROD_QUERY = text("""
    SELECT c.eb_id, c.prod_id AS quality, c.production AS qty,
           c.rates AS prate, c.prod_amount AS amount
    FROM tbl_njm_cnt_wages_data_collection AS c
    WHERE c.start_date = :date_from AND c.end_date = :date_to
      AND c.is_active = 1 AND c.production > 0
    ORDER BY c.eb_id, c.prod_id
""")


def load_contractor(db, pid, p):
    emps = [dict(r) for r in db.execute(CNT_QUERY, {"payperiod_id": pid}).mappings()]
    prods = defaultdict(list)
    for r in db.execute(CNT_PROD_QUERY, p).mappings():
        prods[r["eb_id"]].append(dict(r))
    for e in emps:
        e["prods"] = prods.get(e["eb_id"], [])
    return emps


HALF = 39   # one slip column incl. its right border; two per 79-col row
BLANK_CELL = " " * (HALF - 1) + "|"


def _cell(l1, v1, l2, v2):
    return f"{l1:<12}{v1:>9} {l2:<7}{v2:>9}|"


def contractor_half(e, month, nprod):
    """One slip column: the fixed rows, nprod production rows (blank-padded), NET."""
    if e is None:
        return [" " * HALF] * (nprod + 10)
    pday = num(float(e["regday"]) + float(e["otday"]))
    ta = dec(e["ta"]) if float(e["ta"]) > 0 else ""
    lines = [
        f"{e['emp_name'][:HALF - 1]:^{HALF - 1}} ",
        _cell("TKT NO", e["eb_no"], "Month", month),
        _cell("PDays", "", "TDays", pday),
        _cell("Total Days:", pday, "TRate:", dec(e["trate"])),
        _cell("Advance", dec(e["advance"]), "TA", ta),
        _cell("Plus Balance", dec(e["plus_balance"]), "", ""),
        _cell("ESI", dec(e["esi"]), "PF", dec(e["pf"])),
        f"{'S.no':<4}{'Quality':<9}{'Qty':>7}{'PRate':>8}{'Amount':>10}|",
    ]
    for i, pr in enumerate(e["prods"], 1):
        lines.append(f"{i:<4}{str(pr['quality'])[:9]:<9}{num(pr['qty']):>7}"
                     f"{dec(pr['prate']):>8}{dec(pr['amount']):>10}|")
    lines += [BLANK_CELL] * (nprod - len(e["prods"]) + 1)
    lines.append(_cell("NET", dec(e["net"]), "", ""))
    return lines


def render_contractor(emps, ctx):
    out = []

    def page_break():
        if out and out[-1] != FF:
            out.append(FF)

    for _, grp in groupby(emps, key=lambda e: e["dept_code"]):
        grp = list(grp)
        page_break()
        out += [f"{ctx['company_name']}   PAY SLIP FOR : {ctx['month_long']}    DEPT : {grp[0]['department']}"
                .center(2 * HALF + 1).rstrip(), ""]
        for kn, i in enumerate(range(0, len(grp), 2), 1):
            a, b = grp[i], grp[i + 1] if i + 1 < len(grp) else None
            n = max(len(a["prods"]), len(b["prods"]) if b else 0)
            for left, right in zip(contractor_half(a, ctx["month"], n), contractor_half(b, ctx["month"], n)):
                out.append((left + " " + right).rstrip())
            out += ["", ""]
            if kn % SLIPS_PER_PAGE == 0:
                out.append(FF)
    page_break()
    return "\n".join(out) + "\n"


# ─── contractor as PDF (legacy njmwagespayslip163, FPDF geometry verbatim) ──
# FPDF units: mm from the top-left of an A4 portrait page, 1 mm cell padding,
# text baseline at y + h/2 + 0.3 * font size. Two slip columns at x = 10 and
# 105, each 39|16|19|16 mm wide (or 10|29|16|19|16 for the production table),
# every row 4 mm tall. Fonts: Arial in FPDF is the core Helvetica.
PAGE_H = 297
PAGE_BREAK_AT = 277          # FPDF default auto page break: 20 mm bottom margin
SLIP_X = (10, 105)
ROW_H = 4
SLIP_GAP = 6                 # legacy Ln(), Ln(), Ln(2) after the NET row, minus the row itself
NET_FILL = (211 / 255,) * 3  # SetFillColor(211, 211, 211)
HELV, HELV_B = "Helvetica", "Helvetica-Bold"


def _fpdf_cell(c, x, y, w, txt, border="", align="L", font=HELV, size=7, fill=False, h=ROW_H):
    """FPDF Cell(w, h, txt, border, 0, align, fill) at (x, y) mm on a reportlab canvas."""
    X, Y, W, H = x * mm, (PAGE_H - y - h) * mm, w * mm, h * mm
    if fill:
        c.setFillColorRGB(*NET_FILL)
        c.rect(X, Y, W, H, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
    for side, seg in (("L", (X, Y, X, Y + H)), ("T", (X, Y + H, X + W, Y + H)),
                      ("B", (X, Y, X + W, Y)), ("R", (X + W, Y, X + W, Y + H))):
        if side in border:
            c.line(*seg)
    txt = str(txt)
    if txt:
        c.setFont(font, size)
        tw = c.stringWidth(txt, font, size)
        tx = X + mm if align == "L" else X + W - mm - tw if align == "R" else X + (W - tw) / 2
        c.drawString(tx, (PAGE_H - y - h / 2) * mm - 0.3 * size, txt)


def contractor_slip_rows(e, month, nprod):
    """One slip column as legacy cell rows: [(font, [(w, text, border, align, fill), ...]), ...].

    ``e`` None is the empty right column of an odd pair — legacy still drew its
    labels, with 0 days (null + null) and blank amounts.
    """
    v = (lambda k: dec(e[k])) if e else (lambda k: "")
    pday = num(float(e["regday"]) + float(e["otday"])) if e else "0"
    ta = v("ta") if e and float(e["ta"]) > 0 else ""
    prods = e["prods"] if e else []
    rows = [
        (HELV, 8, [(90, e["emp_name"] if e else "", "", "C", False)]),
        (HELV_B, 7, [(39, "TKT NO", "LTB", "L", False), (16, e["eb_no"] if e else "", "LTBR", "L", False),
                     (19, "Month", "LTB", "L", False), (16, month if e else "", "LTBR", "L", False)]),
        (HELV, 7, [(39, "PDays", "LB", "L", False), (16, "", "LB", "R", False),
                   (19, "TDays", "LB", "L", False), (16, pday, "LBR", "R", False)]),
        (HELV, 7, [(39, "Total Days:", "LB", "L", False), (16, pday, "LB", "R", False),
                   (19, "TRate: ", "LB", "L", False), (16, v("trate"), "LBR", "R", False)]),
        (HELV, 7, [(39, "Advance", "LB", "L", False), (16, v("advance"), "LB", "R", False),
                   (19, "TA", "LB", "L", False), (16, ta, "LBR", "R", False)]),
        (HELV, 7, [(39, "Plus Balance", "LB", "L", False), (16, v("plus_balance"), "LB", "R", False),
                   (19, "", "LB", "L", False), (16, "", "LBR", "R", False)]),
        (HELV, 7, [(39, "ESI", "LB", "L", False), (16, v("esi"), "LB", "R", False),
                   (19, "PF", "LB", "L", False), (16, v("pf"), "LBR", "R", False)]),
        (HELV_B, 7, [(10, "S.no", "LB", "C", False), (29, "Quality", "LB", "C", False), (16, "Qty", "LB", "C", False),
                     (19, "PRate", "LB", "C", False), (16, "Amount", "LBR", "C", False)]),
    ]
    for i in range(nprod + 1):                       # padded to the pair's max, plus legacy's blank row
        pr = prods[i] if i < len(prods) else None
        rows.append((HELV, 7, [
            (10, i + 1 if pr else "", "L", "L", False),
            (29, pr["quality"] if pr else "", "L", "C", False),
            (16, num(pr["qty"]) if pr else "", "L", "C", False),
            (19, dec(pr["prate"]) if pr else "", "L", "R", False),
            (16, dec(pr["amount"]) if pr else "", "LR", "R", False),
        ]))
    rows.append((HELV, 7, [(39, "NET", "LTB", "L", True), (16, v("net"), "LTB", "R", True),
                           (19, "", "LTB", "L", False), (16, "", "LTBR", "R", False)]))
    return rows


def render_contractor_pdf(emps, ctx):
    bio = io.BytesIO()
    c = Canvas(bio, pagesize=A4)
    c.setTitle(f"NJM payslip {ctx['month']}")
    c.setLineWidth(0.2 * mm)                         # FPDF default
    y = 25                                           # first page: $y = 15 + 10; later departments start at 15

    for di, (_, grp) in enumerate(groupby(emps, key=lambda e: e["dept_code"])):
        grp = list(grp)
        if di:
            c.showPage()
            y = 15
        _fpdf_cell(c, 10, y, 190, f"{ctx['company_name']}   PAY SLIP FOR : {ctx['month_long']}"
                   f"    DEPT : {grp[0]['department']}", "", "C", HELV_B, 12, h=10)
        y += 10
        pairs = [(grp[i], grp[i + 1] if i + 1 < len(grp) else None) for i in range(0, len(grp), 2)]
        for kn, (a, b) in enumerate(pairs, 1):
            n = max(len(a["prods"]), len(b["prods"]) if b else 0)
            height = ROW_H * (10 + n)                # name … S.no header (8), n + 1 production rows, NET
            if y + height > PAGE_BREAK_AT:           # ponytail: whole-slip break where FPDF broke mid-slip
                c.showPage()
                y = 10
            for x, e in zip(SLIP_X, (a, b)):
                for r, (font, size, cells) in enumerate(contractor_slip_rows(e, ctx["month"], n)):
                    cx = x
                    for w, txt, border, align, fill in cells:
                        _fpdf_cell(c, cx, y + ROW_H * r, w, txt, border, align, font, size, fill)
                        cx += w
            y += height + SLIP_GAP
            if kn % SLIPS_PER_PAGE == 0 and kn < len(pairs):
                c.showPage()
                y = 10
    c.save()
    return bio.getvalue()


LAYOUTS = {("main", "txt"): (load_main, render_main),
           ("retired", "txt"): (load_main, render_main),
           ("newbudli", "txt"): (load_main, render_main),
           ("contractor", "txt"): (load_contractor, render_contractor),
           ("contractor", "pdf"): (load_contractor, render_contractor_pdf)}

# Every pay scheme has its own slip: legacy dispatched on the scheme id
# (163 -> the FPDF two-up, 164 -> this text layout). Run a layout against
# another scheme and it silently prints zeros, because the components it reads
# by CODE do not exist in that scheme's payroll. Matched on payscheme_code
# rather than the id so no tenant row id lives in the source.
# One kind may serve more than one scheme only where legacy printed them from
# the same block — 169 and 174 are byte-identical there.
SCHEME_CODE = {"main": ("njm_main_payroll",), "retired": ("njm_retired_payroll",),
               "newbudli": ("njm_newbudli_payroll", "njm_newbudli_rtd"),
               "contractor": ("NJM_CONT_WAGES_NEW",)}

SCHEME_QUERY = text("SELECT payscheme_code FROM pay_scheme_master WHERE payscheme_id = :id")


@router.get("/njm_text_payslip")
def njm_text_payslip(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Pay slips for one period + pay scheme — see module docstring.

    Query: kind=main|retired|newbudli|contractor, fmt=txt|pdf (pdf: contractor
    only), date_from, date_to, pay_scheme_id, branch_id, co_id.
    """
    q = request.query_params
    kind, fmt = q.get("kind", ""), q.get("fmt", "txt")
    layout = LAYOUTS.get((kind, fmt))
    if not layout:
        raise HTTPException(
            status_code=400,
            detail="kind must be main, retired, newbudli or contractor; "
                   "fmt txt, or pdf for contractor")
    try:
        d0, d1 = date.fromisoformat(q.get("date_from", "")), date.fromisoformat(q.get("date_to", ""))
        scheme_id = int(q.get("pay_scheme_id", ""))
        branch_id = int(q["branch_id"]) if q.get("branch_id") else None
        co_id = int(q["co_id"]) if q.get("co_id") else None
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="date_from, date_to (YYYY-MM-DD) and pay_scheme_id are required")
    if d1 < d0:
        raise HTTPException(status_code=400, detail="date_to cannot be earlier than date_from")

    want = SCHEME_CODE[kind]
    got = db.execute(SCHEME_QUERY, {"id": scheme_id}).scalar()
    if got not in want:
        raise HTTPException(
            status_code=400,
            detail=(f"This pay slip is only for the {' / '.join(want)} pay scheme; "
                    f"{got or 'the selected scheme'} prints its own pay slip."))

    p = {"date_from": d0.isoformat(), "date_to": d1.isoformat()}
    pid = reg._resolve_period(db, p["date_from"], p["date_to"], scheme_id, branch_id)
    load, render = layout
    rows = load(db, pid, p)
    if not rows:
        raise HTTPException(status_code=404, detail="No pay slips to print for the selected period and pay scheme")

    co = db.execute(get_company_name(), {"co_id": co_id}).fetchone() if co_id else None
    today = date.today()
    ctx = {
        "company_name": (co._mapping["co_name"] if co else "") or "Company",
        "payslip_for": f"{d1:%b}'{d1:%Y}",             # PHP date("M'Y", periodtodate)
        "print_date": f"10,{today:%b}'{today:%Y}",     # PHP date("10,M'Y") — the 10th, verbatim
        "month": f"{d0:%b} {d1:%Y}",                   # contractor "Month" cell
        "month_long": f"{d1:%B %Y}",                   # contractor page heading
        "kind": kind,                                  # legacy's per-scheme tweaks
    }
    body = render(rows, ctx)
    return StreamingResponse(
        iter([body if isinstance(body, bytes) else body.encode("utf-8")]),
        media_type="application/pdf" if fmt == "pdf" else "text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="payslip.{fmt}"'},
    )
