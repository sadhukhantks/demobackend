"""Tests for the dot-matrix Pay Register text layouts (legacy payregisterprint)."""

import pytest

from src.hrms import ejmTextPayReg as m

CTX = {"company_name": "DEV3 JUTE MILLS", "scheme_name": "MAIN PAYROLL",
       "date_from": "2026-08-01", "date_to": "2026-08-15"}

NUMERIC = (
    # 1/2/7
    "basic_rate working_hours ns_hrs hl_hrs ls_hrs stl_d wrk_days c_work_day prod_basic "
    "time_rated_basic fix_basic c_bon_ern pf_gross da ns_amount hol_amt increment_amt layoff_wgs "
    "incentive_amount misc_earn stl_wgs gross_pay hra b_f total_earn epf c_pf_cont c_fpf_cont "
    "co_cont epf_833 epf_167 esi_gross esic p_tax lwf co_loan co_loan_bal puja_advance stl_advance "
    "total_deduction c_f net_pay attincn otpay otadv otnet "
    # 8
    "pf_gross100 gross_pay100 pf100 total100 net100 diff_gross diff_net "
    # 3
    "working_hrs hol_hrs rate_per_day basic hol_wgs arrear_plus arrear_minus esi exadvance netpay "
    # 4/5/6
    "holiday_hr ot_hours festival_wage overtime_pay advance net_payable night_shift_hr "
    "tiffin_amount washing_allowance other_allowance conv_allowance gross2 gross_ded empl_epf empl_esi"
).split()

RULER = {1: 133, 2: 133, 7: 133, 3: 79, 4: 79, 5: 79, 6: 136, 8: 88}


def _row(dept: str, i: int) -> dict:
    r = {k: round(100.0 + 7.5 * ((i + n) % 9), 2) for n, k in enumerate(NUMERIC)}
    r.update(diff_gross=-12.5 * (i % 2), diff_net=-13.0 * (i % 2), wrk_days=12.0, stl_d=1.0)
    r.update(eb_no=f"E{i:03d}", wname=f"WORKER NUMBER {i} WITH A LONG NAME", dept_code=dept,
             dept_desc=f"DEPT {dept}", desig="SPINNER", time_piece="T", shift="A",
             esi_no="4112345678", pf_no="WB/12345", pf_uan_no="100200300400",
             bank_detail_id=None if i % 2 else i)
    return r


def rows(n_per_dept: int = 3) -> list[dict]:
    return [_row(d, i) for d in ("SP", "WV") for i in range(n_per_dept)]


def _lines(out: str) -> list[str]:
    return out.split("\n")


@pytest.mark.parametrize("code", sorted(RULER))
def test_layout_shape(code):
    out = m.render(code, rows(), CTX)
    assert out
    assert CTX["scheme_name"] in out
    assert "\x0c" in out
    assert max(len(l.rstrip()) for l in _lines(out)) <= RULER[code], code
    assert out.endswith("\n")


def test_pay_register_banner_per_dept_no_totals():
    out = m.render(1, rows(), CTX)
    banner = f"{CTX['company_name']}   {CTX['scheme_name']}                F/E:{CTX['date_to']}"
    assert out.count(banner) == 2  # one page per department
    assert out.count("\x0c") == 2
    assert "Total" not in out
    assert "SPIN" in out  # desig[:4] in the OCC column
    assert out.count("=" * 133) == 2 + 6  # ruler under header + one per employee


def test_pay_register_page_break_after_13_employees():
    out = m.render(2, [_row("SP", i) for i in range(14)], CTX)
    assert out.count("\x0c") == 2
    assert out.count("PAGE  1") == 1 and out.count("PAGE  2") == 1


@pytest.mark.parametrize("code", [3, 4, 5])
def test_voucher_register_totals_and_summary(code):
    out = m.render(code, rows(), CTX)
    assert f"Voucher Register for FNE To {CTX['date_to']}           Dept Code SP" in out
    assert out.count("Dept Total SP") == 1 and out.count("Dept Total WV") == 1
    assert out.count("Grand Total") == 2  # register + summary
    assert "Voucher Summary Register for FNE To" in out
    assert "DEPT SP" in out and "DEPT WV" in out
    assert out.count("\x0c") == 2
    reg, summ = out.split("Voucher Summary Register")
    assert "Grand Total" in reg and "Grand Total" in summ


def test_voucher_amounts_are_php_number_format():
    r = _row("SP", 0)
    r.update(working_hours=1000.0, holiday_hr=0.0, ot_hours=0.5, net_payable=12345.5)
    out = m.render(4, [r], CTX)
    assert " 1,000.50" in out
    assert "12,346" in out  # number_format(…, 0) rounds half away from zero


def test_trainee_register_grand_total():
    out = m.render(6, rows(), CTX)
    assert "Pay Register for the period from  01-08-2026    FNE To15-08-2026" in out
    assert out.count("Grand Total") == 1
    assert out.count("\x0c") == 2
    assert "Dept Total" not in out


def test_comparative_statement_totals():
    out = m.render(8, rows(), CTX)
    assert f"{CTX['company_name']}   PAYIMAGE COMPARATIVE STATEMENT F/E {CTX['date_to']}  Dept SP" in out
    assert out.count("Total Rec") == 3  # SP, WV, grand
    assert out.count("\x0c") == 2
    grand = [l for l in _lines(out) if "Total Rec" in l][-1]
    assert "Total Rec        6" in grand
    assert grand.rstrip().endswith("-25       -26")  # 2 odd rows x (-12.5, -13)
    assert not out.startswith("=")  # no bogus empty dept-total block up front


def test_unknown_code_raises():
    with pytest.raises(ValueError):
        m.render(14, rows(), CTX)


def test_php_helpers():
    assert m.nf(1234.5, 0) == "1,235"
    assert m.nf(1234567.891) == "1,234,567.89"
    assert m.pf(1234.0) == "1234" and m.pf(12.5) == "12.5" and m.pf(0.1 + 0.2) == "0.3"
    assert m.whole(2.5) == "3"
    assert m.rj("x", -3) == "x" and m.lj("abc", 2) == "abc"
