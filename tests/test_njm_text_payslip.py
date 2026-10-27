"""NJM text pay slips (src/hrms/njmTextPaySlip.py) — the layout rules that would
silently break a dot-matrix run: 5 slips per page, department totals on change,
unpaid employees skipped, contractor pairs padded evenly."""
from decimal import Decimal

import re

from src.hrms.njmTextPaySlip import (COFF, FF, contractor_slip_rows, render_contractor,
                                     render_contractor_pdf, render_main)

CTX = {"company_name": "LIMELIGHT COMM PVT LTD", "payslip_for": "Aug'2026",
       "print_date": "10,Sep'2026", "month": "Aug 2026", "month_long": "August 2026"}


def _emp(i, dept="01", gross2="100.00", net="90.00"):
    return {"eb_id": i, "eb_no": f"N{i}", "emp_name": f"WORKER {i}", "dept_code": dept,
            "department": "JUTE" if dept == "01" else "BATCHING", "designation": "WEAVER",
            "esi_no": "1234567890", "pf_no": "WB/12345",
            "amounts": {"C_gross2amt_": Decimal(gross2), "C_NET": Decimal(net),
                        "C_finalnetpay_": Decimal(net), "C_iftudedamt_": Decimal("5.00"),
                        "C_wrkhrs1_": Decimal("208.00")}}


def test_main_slip_pages_totals_and_skips_unpaid():
    emps = [_emp(i) for i in range(1, 7)] + [_emp(7, gross2="0.00"), _emp(8, dept="02")]
    body = render_main(emps, CTX)
    assert body.count("\x0f  LIMELIGHT") == 7           # 6 paid in dept 01 + 1 in 02; unpaid skipped
    assert body.count(COFF + FF + "\n") == 3            # page after 5th slip, final totals, trailer
    assert "    TOTAL  NO. OF PAYSLIPS :6\n" in body
    assert "TOTAL NET AMOUNT:  540           LOAN DED: 30              DEPARTMENT :JUTE" + FF + "\x0f" in body
    assert "    TOTAL  NO. OF PAYSLIPS :1\n" in body
    assert body.startswith("\x0f  LIMELIGHT COMM PVT LTD  5.5421  PAY SLIP FOR: Aug'2026  DEPT:     JUTE"
                           "    # PFN:WB/12   DAYS:26.00" + " " * 17 + "\x0e10,Sep'2026\n"
                           "   NAME  :WORKER 1       DESG:WEAVER      ESINO: 1234567890  TKT NO: N1  CODE:    N1")
    assert " " * 12 + "\x0eNET:90.00\n" in body


def test_newbudli_relabels_four_boxes_and_keeps_the_pfn():
    """Legacy 169/174 (sls 2017/3174): the main layout with ADJ.A, O.A for INC,
    FWOA/SWOA and a blank P.F.G. Amounts are one slip from a legacy run."""
    amounts = {"C_gross2amt_": Decimal("6776.61"), "C_NET": Decimal("6148.00"),
               "C_finalnetpay_": Decimal("6088.00"), "C_iftudedamt_": Decimal("60.00"),
               "C_wrkhrs1_": Decimal("96.00"), "C_timewages_": Decimal("553.85"),
               "C_rsdamt_": Decimal("1.00"), "C_OTHER_ALLOW_": Decimal("1006.20"),
               "C_inc_": Decimal("999.99"), "C_PFG_": Decimal("888.88"),
               "C_HRA_": Decimal("288.08"), "C_ESI_": Decimal("51.00"),
               "C_totalwagesamt_": Decimal("5761.53"), "C_gross1amt_": Decimal("6049.61"),
               "C_otherpay_": Decimal("727.00")}
    e = dict(_emp(1), pf_no="10242", amounts=amounts)
    body = render_main([e], {**CTX, "kind": "newbudli"})

    assert "# PFN:10242   DAYS:12.00" in body          # 167 blanks the PFN; 169/174 keep it
    assert "   TWAGE :  553.85   ADJ.A:    0.00   LOWI :" in body
    for line in (
        "   O.A   : 1006.20   FDG  :    0.00   ODG  :    0.00   GRS2 : 6776.61   ESI  :   51.00   VARD1:   60.00",
        "   FWOA  :    0.00   SDG  :    0.00   HRA  :  288.08  ADVDED:    0.00   G.W.F:    0.00   NET  : 6088.00",
        "   SWOA  :    0.00   TOTAL: 5761.53   GRS1 : 6049.61   PTAX :    0.00   LR   :    0.00",
        "   PVE   :    0.00        :           OP   :  727.00   CANT :    0.00   PROD :    0.00",
    ):
        assert line + "\n" in body, line
    assert "INC   :" not in body and "P.F.G" not in body   # and neither value leaks

    # main keeps its own boxes — DJ.A is 9 wide where ADJ.A is 8
    plain = render_main([e], {**CTX, "kind": "main"})
    assert "   TWAGE :  553.85   DJ.A:     0.00   LOWI :" in plain
    assert "   INC   :  999.99   FDG  :" in plain
    assert "   PVE   :    0.00   P.F.G:  888.88   OP   :" in plain


def _cnt(i, prods=()):
    return {"eb_id": i, "eb_no": f"N{i}", "emp_name": f"HAND {i}", "dept_code": "01", "department": "JUTE",
            "otday": Decimal("1.00"), "regday": Decimal("25.50"), "trate": Decimal("270.00"),
            "advance": Decimal("0.00"), "ta": Decimal("0.00"), "esi": Decimal("10.00"), "pf": Decimal("0.00"),
            "plus_balance": Decimal("0.00"), "net": Decimal("7000.00"),
            "prods": [{"quality": 7801, "qty": q, "prate": Decimal("3.50"), "amount": Decimal("3.50") * q}
                      for q in prods]}


def test_contractor_pairs_and_pads_production_rows():
    body = render_contractor([_cnt(1, prods=(100, 200)), _cnt(2), _cnt(3)], CTX)
    lines = body.split("\n")
    assert lines[0].strip() == "LIMELIGHT COMM PVT LTD   PAY SLIP FOR : August 2026    DEPT : JUTE"
    assert {len(l) for l in lines if l.endswith("|")} <= {39, 79}
    assert "TKT NO             N1 Month   Aug 2026| TKT NO             N2 Month   Aug 2026|" in lines
    assert "TKT NO             N3 Month   Aug 2026|" in lines             # odd one out, right half blank
    assert "1   7801         100    3.50    350.00| " + " " * 38 + "|" in lines
    assert "NET           7000.00                 | NET           7000.00                 |" in lines
    assert "TDays       26.5|" in body
    assert body.count(FF) == 1

    six_rows = render_contractor([_cnt(i) for i in range(1, 13)], CTX)
    assert six_rows.count(FF) == 2                       # after the 5th row and at the end


def test_contractor_null_rate_or_amount_prints_zero_not_crash():
    # a production row with NULL rates/prod_amount (real sls data) must render
    # 0.00, as legacy PHP number_format(null) did — not raise float(None).
    e = {**_cnt(1), "prods": [{"quality": 7801, "qty": Decimal("100"),
                               "prate": None, "amount": None}]}
    cells = [[c[:2] for c in r[2]] for r in contractor_slip_rows(e, "Aug 2026", 1)]
    assert cells[8] == [(10, 1), (29, 7801), (16, "100"), (19, "0.00"), (16, "0.00")]
    assert "0.00" in render_contractor([e], CTX)


def test_contractor_pdf_rows_follow_legacy_fpdf_cells():
    rows = contractor_slip_rows(_cnt(1, prods=(100,)), "Aug 2026", 2)
    cells = [[c[:2] for c in r[2]] for r in rows]
    assert cells[1] == [(39, "TKT NO"), (16, "N1"), (19, "Month"), (16, "Aug 2026")]
    assert cells[3] == [(39, "Total Days:"), (16, "26.5"), (19, "TRate: "), (16, "270.00")]
    assert cells[4][3] == (16, "")                       # TA prints blank when 0, as legacy did
    assert cells[8] == [(10, 1), (29, 7801), (16, "100"), (19, "3.50"), (16, "350.00")]
    assert cells[9] == cells[10] == [(10, ""), (29, ""), (16, ""), (19, ""), (16, "")]   # pad + legacy blank row
    assert rows[-1][2][:2] == [(39, "NET", "LTB", "L", True), (16, "7000.00", "LTB", "R", True)]
    assert [r[0] for r in rows[:2]] == ["Helvetica", "Helvetica-Bold"]
    assert len(rows) == 12
    # the empty right column of an odd pair: labels kept, 0 days, blank amounts
    empty = [[c[1] for c in r[2]] for r in contractor_slip_rows(None, "Aug 2026", 0)]
    assert empty[2] == ["PDays", "", "TDays", "0"] and empty[3] == ["Total Days:", "0", "TRate: ", ""]
    assert empty[-1] == ["NET", "", "", ""]


def test_contractor_pdf_pages_five_pairs_and_per_department():
    pages = lambda pdf: len(re.findall(rb"/Type\s*/Page\b(?!s)", pdf))  # noqa: E731
    pdf = render_contractor_pdf([_cnt(1, prods=(100, 200)), _cnt(2), _cnt(3)], CTX)
    assert pdf.startswith(b"%PDF") and pages(pdf) == 1
    assert pages(render_contractor_pdf([_cnt(i) for i in range(1, 13)], CTX)) == 2   # 6 pairs: 5 + 1
    assert pages(render_contractor_pdf([_cnt(i) for i in range(1, 11)], CTX)) == 1   # exactly 5: no blank trailer
    two_depts = [_cnt(i) for i in range(1, 11)] + [dict(_cnt(11), dept_code="02", department="BATCHING")]
    assert pages(render_contractor_pdf(two_depts, CTX)) == 2
    # a pair too tall for the page moves whole to the next page (FPDF broke it mid-slip)
    tall = [_cnt(i, prods=tuple(range(1, 6))) for i in range(1, 11)]
    assert pages(render_contractor_pdf(tall, CTX)) == 2
