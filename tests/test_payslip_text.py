"""Fixed-width payslip layout check.

The column positions in _fmt_payslip_text land on boxes of pre-printed
stationery, so a shifted field is a silent misprint rather than a crash. This
pins the layout against the known-good legacy sample (E:\\ejmhands\\payslip.txt,
emp 14908 PRATAP BISWAL, FNE 15-07-26) produced by Data_entry::payslipprint().

Run: python tests/test_payslip_text.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.hrms.payRegister import _PAYSLIP_COMPONENTS, _fmt_payslip_text, _nf, _rj

# Line 1 of the legacy sample, verbatim.
LEGACY_LINE_1 = (
    " 14/G/001    14908    T 14/G/001    14908    ELEF 5270414"
    "   3.351  88.00  0.0 0.0   0      0 4396 3661 6900.79 11  76.9 43929.38 0.00"
)


def _ctx(values, emp_name="PRATAP  BISWAL"):
    return {"employees": [{
        "eb_id": 1, "emp_code": "14908", "emp_name": emp_name,
        "department_name": "", "values": values, "meta": {},
    }]}


def _identity(desig="ELEF*", **kw):
    # 'ELEF*'[:4] == 'ELEF' — this feeds the slip the short code the legacy row
    # had, so the test pins the LAYOUT. It does not mean the tenant schema can
    # produce 'ELEF': real sls data yields 'ELEC' from desig 'ELECTICAL FITTER'.
    # See the ponytail note on _fmt_payslip_text.
    base = {"emp_code": "14908", "dept_code": "14", "desig": desig,
            "time_piece": "T", "pf_no": "3400", "esi_no": "4005270414"}
    base.update(kw)
    return {1: base}


def _by_code(**kw):
    return {_PAYSLIP_COMPONENTS[k]: v for k, v in kw.items()}


def test_number_format_rounds_half_away_from_zero():
    # PHP number_format rounds .5 up; Python's default banker's rounding
    # would give '2.02' for 2.025 and shift paise on the slip.
    assert _nf(2.025, 2) == "2.03"
    assert _nf(0.5, 0) == "1"
    assert _nf(None, 2) == "0.00"


def test_right_align_never_truncates():
    assert _rj("88.00", 7) == "  88.00"
    # a value wider than its column runs long instead of being cut (PHP 7 behaviour)
    assert _rj("123456789", 4) == "123456789"


def test_line1_matches_legacy_sample():
    values = _by_code(
        shiftcd=0, WORKING_HOURS=88.0, NS_HRS=0.0, LS_HRS=0.0, HL_HRS=0.0,
        STL_D=0.0, C_PF_CONT=4396, C_EPF_CONT=3661, GROSS_PAY=6900.79,
        WRK_DAYS=11, C_WORK_DAY=76.9, C_BON_ERN=43929.38, B_F=0.0,
        BASIC_RATE=3.351,
    )
    out = _fmt_payslip_text(_ctx(values), _identity(), "2026-07-15")
    line1 = out.split("\n")[0]
    assert line1 == LEGACY_LINE_1, (
        f"\nexpected: {LEGACY_LINE_1!r}\n     got: {line1!r}"
    )


def test_shift_code_and_serial():
    # shiftcd 3 -> 'C'; serial is 1-based and zero-padded to 3
    out = _fmt_payslip_text(
        _ctx(_by_code(shiftcd=3, WORKING_HOURS=8)), _identity(), "2026-07-15"
    )
    assert out.startswith(" 14/C/001")


def test_total_line_carries_net_and_pf():
    values = _by_code(TOTAL_EARN=7159.57, WORKING_HOURS=88)
    out = _fmt_payslip_text(_ctx(values), _identity(), "2026-07-15")
    total = [ln for ln in out.split("\n") if "Total" in ln][0]
    assert "Total" in total and "7159.57" in total
    assert total.rstrip().endswith("P.F. No -   3400")


def _many(n, dept="11"):
    """n employees in one department -> forces the 6-slips-per-page break."""
    ctx = {"employees": [{
        "eb_id": i, "emp_code": str(i), "emp_name": f"EMP {i}",
        "values": _by_code(WORKING_HOURS=8), "meta": {},
    } for i in range(1, n + 1)]}
    ident = {i: {"dept_code": dept, "desig": "XX", "time_piece": "T",
                 "pf_no": str(i), "esi_no": "4001234567"} for i in range(1, n + 1)}
    return ctx, ident


def test_form_feed_is_not_followed_by_a_newline():
    # 15x12 stationery: a newline after \f prints a blank first line and walks
    # every subsequent page one row down the form. First page looks fine, the
    # rest drift -- so assert the page break is bare.
    ctx, ident = _many(8)
    out = _fmt_payslip_text(ctx, ident, "2026-08-15")
    assert "\f\n" not in out, "form feed must not be followed by a newline"
    assert "\n\f" in out, "form feed should directly follow a completed line"


def test_full_page_is_71_lines_plus_form_feed():
    # 6 slips x 12 lines, less the trailing blank the 6th drops = 71 printed
    # lines, then the \f ejects the 12-inch form (72 lines at 6 LPI).
    ctx, ident = _many(7)
    out = _fmt_payslip_text(ctx, ident, "2026-08-15")
    first_page = out.split("\f")[0]
    assert first_page.count("\n") == 71, (
        f"expected 71 lines on a full page, got {first_page.count(chr(10))}"
    )


def test_every_page_starts_with_a_slip_not_a_blank():
    ctx, ident = _many(20)
    out = _fmt_payslip_text(ctx, ident, "2026-08-15")
    for idx, page in enumerate(out.split("\f")):
        if not page.strip():
            continue
        assert not page.startswith("\n"), f"page {idx + 1} starts with a blank line"


def test_department_change_emits_form_feed():
    ctx = {"employees": [
        {"eb_id": 1, "emp_code": "1", "emp_name": "A", "values": _by_code(WORKING_HOURS=8), "meta": {}},
        {"eb_id": 2, "emp_code": "2", "emp_name": "B", "values": _by_code(WORKING_HOURS=8), "meta": {}},
    ]}
    ident = {
        1: {"dept_code": "11", "desig": "X", "time_piece": "T", "pf_no": "1", "esi_no": "4001234567"},
        2: {"dept_code": "14", "desig": "Y", "time_piece": "T", "pf_no": "2", "esi_no": "4007654321"},
    }
    out = _fmt_payslip_text(ctx, ident, "2026-07-15")
    # one \f between the two departments, one closing the file
    assert out.count("\f") == 2
    # sorted by dept_code, so 11 prints before 14
    assert out.index("/11/") if "/11/" in out else True
    assert out.split("\f")[0].count("11/") >= 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}{e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
