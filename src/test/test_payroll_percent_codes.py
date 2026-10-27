"""Component CODEs containing characters outside [A-Za-z0-9_] (legacy codes
like 'C_HRA%_') must tokenize as one identifier in pay formulas — previously
the '%' split the token and the formula silently evaluated to 0."""
from src.hrms.payRegister import _evaluate_employee_formulas


META = {
    20: {"code": "C_GROSS_", "name": "Gross Earnings"},
    191: {"code": "C_HRA%_", "name": "HRA%"},
    8: {"code": "C_HRA_", "name": "HRA"},
    243: {"code": "C_GROSSPAY_100%_", "name": "GROSS_PAY_100%"},
    242: {"code": "C_HRA100%_", "name": "HRA100%"},
}


def test_hra_formula_with_percent_code():
    # Real EJM VOUCHER WORKER scheme shape: HRA = C_GROSS_*(C_HRA%_/100)
    formulas = {20: "1000", 191: "2.5", 8: "C_GROSS_*(C_HRA%_/100)"}
    cooked, errors, warnings = _evaluate_employee_formulas(formulas, META)
    assert cooked[8] == 25.0
    assert not errors
    assert not warnings


def test_chained_percent_codes():
    # C_HRA100%_ = C_GROSSPAY_100%_*C_HRA%_/100 — both deps have % in CODE
    formulas = {243: "2000", 191: "2.5", 242: "C_GROSSPAY_100%_*C_HRA%_/100"}
    cooked, errors, warnings = _evaluate_employee_formulas(formulas, META)
    assert cooked[242] == 50.0
    assert not errors
    assert not warnings


def test_plain_codes_unaffected():
    formulas = {20: "1000", 8: "C_GROSS_*10/100"}
    cooked, errors, warnings = _evaluate_employee_formulas(formulas, META)
    assert cooked[8] == 100.0
    assert not errors
    assert not warnings


def test_math_floor_and_ceil_formulas():
    # sls 3023 NJM 12 ELEMENTS C_1_GROSS_ docks a day per three lates. The 'floor' / 'ceil'
    # tail was zeroed as an unknown identifier ('math.0(...)'), so gross, net and C_banks_
    # failed for every employee.
    meta = {1: {"code": "C_ELEMENTS_"}, 2: {"code": "C_PACKAGE_"}, 6: {"code": "C_PAYABLE_DAYS_"},
            50: {"code": "C_LATES_"}, 65: {"code": "C_CALENDAR_DAYS_"}, 66: {"code": "C_1_GROSS_"}}
    day = "((C_PACKAGE_/C_ELEMENTS_)/C_CALENDAR_DAYS_)"
    base = {1: "12", 2: "600000", 6: "25", 50: "4", 65: "31"}
    for fn, docked in (("Math.floor", 1), ("Math.ceil", 2)):
        formulas = {**base, 66: f"{day}*C_PAYABLE_DAYS_-{day}*({fn}(C_LATES_/3))"}
        cooked, errors, warnings = _evaluate_employee_formulas(formulas, meta)
        assert not errors and not warnings, (fn, errors, warnings)
        assert abs(cooked[66] - 50000 / 31 * (25 - docked)) < 0.01
