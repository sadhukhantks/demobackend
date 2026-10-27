"""Tests for the EJM Pay Slip dot-matrix text prints (legacy payslipprint)."""

import re

import pytest

from src.hrms import ejmTextPaySlip as m

FF = "\x0c"
CTRL = re.compile("[\x0c\x0e\x0f\x12]")
CTX = {"company_name": "Test Mills", "scheme_name": "Main Pay Roll",
       "date_from": "2026-08-01", "date_to": "2026-08-15"}

# Every numeric field any layout reads, at realistic magnitudes.
NUMS = {
    # identity-ish hours / days
    "working_hours": 96.0, "working_hrs": 96.0, "ns_hrs": 8.0, "hl_hrs": 0.0, "ls_hrs": 0.0,
    "hol_hrs": 0.0, "holiday_hr": 8.0, "ot_hours": 4.0, "night_shift_hr": 8.0,
    "stl_d": 0.0, "wrk_days": 12.0, "c_work_day": 12.0,
    # codes 1/2/7
    "basic_rate": 13.25, "prod_basic": 1200.0, "time_rated_basic": 1200.0, "fix_basic": 0.0,
    "c_bon_ern": 0.0, "pf_gross": 1200.0, "da": 100.0, "ns_amount": 40.0, "hol_amt": 0.0,
    "increment_amt": 0.0, "layoff_wgs": 0.0, "incentive_amount": 0.0, "misc_earn": 10.0,
    "stl_wgs": 0.0, "gross_pay": 1500.0, "hra": 50.0, "b_f": 0.5, "total_earn": 1500.0,
    "epf": 144.0, "c_pf_cont": 144.0, "c_fpf_cont": 100.0, "co_cont": 0.0, "epf_833": 100.0,
    "epf_167": 20.0, "esi_gross": 1500.0, "esic": 11.0, "p_tax": 0.0, "lwf": 0.0,
    "co_loan": 100.0, "co_loan_bal": 500.0, "puja_advance": 100.0, "stl_advance": 0.0,
    "total_deduction": 200.0, "c_f": 0.25, "net_pay": 1300.0, "attincn": 0.0,
    "otpay": 0.0, "otadv": 0.0, "otnet": 0.0,
    # code 3
    "rate_per_day": 100.0, "basic": 1200.0, "hol_wgs": 0.0, "arrear_plus": 10.0,
    "arrear_minus": 5.0, "esi": 11.0, "exadvance": 100.0, "netpay": 1300.0, "miss_ot_earning": 0.0,
    # codes 4/5/6
    "festival_wage": 0.0, "overtime_pay": 60.0, "advance": 100.0, "net_payable": 1300.0,
    "tiffin_amount": 50.0, "washing_allowance": 20.0, "other_allowance": 30.0,
    "conv_allowance": 40.0, "gross2": 1400.0, "gross_ded": 200.0, "empl_epf": 144.0, "empl_esi": 11.0,
}


def row(dept, i, **over):
    r = dict(NUMS)
    r.update(eb_no=f"{dept}{i:03d}", wname=f"WORKER {i} DEPT {dept}", dept_code=dept, dept_desc=f"DEPT {dept}",
             desig="FITTER", time_piece="T", shift="A", esi_no="1234567890", pf_no=f"PF{i}",
             pf_uan_no="UAN", bank_detail_id=None)
    r.update(over)
    return r


ROWS = [row(d, i) for d in ("12", "34") for i in range(1, 4)]   # 2 depts x 3 employees
ALL_CODES = (1, 2, 3, 4, 5, 6, 7)
RULER = {3: 84, 4: 82, 5: 82, 6: 136, 1: 136, 2: 136, 7: 136}


def widths(text, code):
    lines = [CTRL.sub("", l) for l in text.split("\n")]
    if code == 6:   # the pay-ticket stub sits past the 136-col ruler
        lines = [l.split("|", 1)[0] for l in lines]
    return max(len(l) for l in lines)


class TestPhpHelpers:
    def test_number_format_rounds_half_up_with_commas(self):
        assert m.nf(0.125) == "0.13"
        assert m.nf(1234.5, 0) == "1,235"
        assert m.nf(1234.567, 2, commas=False) == "1234.57"

    def test_bare_decimal_and_echoed_float(self):
        assert m.dec(8) == "8.00"
        assert m.num(8.0) == "8" and m.num(8.5) == "8.5"
        assert m.rnd(26.05, 1) == "26.1"

    def test_negative_pad_is_clamped_like_php(self):
        assert m.rj("toolong", 3) == "toolong"
        assert m.lj("toolong", 3) == "toolong"


class TestEveryLayout:
    @pytest.mark.parametrize("code", ALL_CODES)
    def test_renders_within_ruler(self, code):
        text = m.render(code, ROWS, CTX)
        assert text and text.endswith("\n")
        assert widths(text, code) <= RULER[code], code

    @pytest.mark.parametrize("code", (11, 12, 13, 99))
    def test_other_codes_raise(self, code):
        with pytest.raises(ValueError):
            m.render(code, ROWS, CTX)

    def test_long_name_does_not_raise(self):
        rows = [row("12", 1, wname="A" * 60)]
        for code in ALL_CODES:
            assert m.render(code, rows, CTX)


class TestTwoUp:   # code 3
    def test_heading_and_star_padded_net(self):
        text = m.render(3, ROWS, CTX)
        assert "DP S SRL T/No. Name            F/E Date | DP S SRL" in text
        assert "12 A   1 12001 WORKER 1 DEPT 15/08/2026 | 12 A   2 12002" in text
        assert "*1,300 | " in text
        assert "     104" in text     # 96 + 8 + 0 hours, echoed as a PHP float

    def test_form_feed_on_dept_change_and_at_end(self):
        text = m.render(3, ROWS, CTX)
        assert text.count(FF) == 2
        # odd slip of dept 12 flushed alone before the dept break
        assert "12 A   3 12003 WORKER 3 DEPT 15/08/2026 | \n" in text

    def test_form_feed_every_five_rows(self):
        text = m.render(3, [row("12", i) for i in range(1, 12)], CTX)
        assert text.count(FF) == 2      # after 5 pairs + final


class TestVoucher:   # codes 4 / 5
    def test_heading_uses_ctx(self):
        text = m.render(4, ROWS, CTX)
        assert text.startswith("Main Pay Roll\nVoucher Payslip for FNE 2026-08-15              Dept Code 12\n1" + "-" * 79)
        assert "Dept Code 34" in text
        assert "12001     WORKER 1 DEPT 12       108.00  100.00     15.00     1,300" in text

    def test_dept_total_only_on_dept_change(self):
        text = m.render(5, ROWS, CTX)
        assert " " * 10 + "Dept Total 12" + " " * 7 + "      324" + " " * 16 + "      3900" in text
        assert "Dept Total 34" not in text
        assert text.count(FF) == 2 and text.count("pgb" + "-" * 79) == 1

    def test_five_slips_per_page(self):
        text = m.render(4, [row("12", i) for i in range(1, 7)], CTX)
        assert text.count("pgb") == 1 and text.count(FF) == 2


class TestPayTicket:   # code 6
    def test_banner_and_control_chars(self):
        text = m.render(6, ROWS, CTX)
        assert text.startswith("\x0eMain Pay Roll\nPay Ticket for FNE 15-08-2026\n\x0f" + "-" * 136)
        assert "|  PAY TICKET " in text
        assert "|  EB NO : 12001" in text and "|  NAME  :WORKER 1 DEPT 12" in text
        assert " " * 136 + "|  NET PAY 1,300.00" in text

    def test_five_per_page_with_condensed_off(self):
        text = m.render(6, ROWS, CTX)          # 6 rows -> break before the 6th, then trailer
        assert text.count(FF + "\x12") == 1 and text.count(FF) == 3


class TestMainSlip:   # codes 1 / 2 / 7
    def test_slip_lines(self):
        text = m.render(1, ROWS, CTX)
        assert " 12/A/001    12001    T 12/A/001    12001    FITT 4567890  12.500  96.00  8.0 0.0   0" in text
        assert " 15-08-26 PF1     1300   1300   200.00  1500.00  0.25     0" in text
        assert text.count(" " * 28 + "Total   1500.00" + " " * 15) == 6
        assert "WORKER 1 DEPT 12" + " " * 23 + " P.F. No -    PF1" in text

    def test_code_2_uses_basic_rate(self):
        assert "  13.250" in m.render(2, ROWS, CTX)
        assert "  13.250" not in m.render(7, ROWS, CTX)

    def test_form_feed_glued_to_next_slip(self):
        text = m.render(1, ROWS, CTX)
        assert text.count(FF) == 2
        assert FF + " 34/A/001" in text

    def test_six_per_page(self):
        text = m.render(7, [row("12", i) for i in range(1, 8)], CTX)
        assert text.count(FF) == 2 and FF + " 12/A/007" in text
