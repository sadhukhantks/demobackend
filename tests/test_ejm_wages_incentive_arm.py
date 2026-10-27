"""The 'INC' arm must land the Main Payroll FN Attendance Incentive (legacy
holget = 2) in atten_incentive — not misc_earn, where manual entries live.
"""
import re

from src.hrms.ejmWagesData import BUILD_ARMS, EDITABLE_FIELDS, _year_starts


def test_year_starts_calendar_and_april_financial_year():
    assert _year_starts("2026-08-16") == ("2026-01-01", "2026-04-01")
    assert _year_starts("2026-02-01") == ("2026-01-01", "2025-04-01")
    assert _year_starts("2026-04-01") == ("2026-01-01", "2026-04-01")


def test_cum_arm_fills_every_prev_cuml_column_and_stops_before_the_period():
    build = {code: q for code, _, q in BUILD_ARMS}["CUM"]
    sql = str(build())
    cols = re.search(r"INSERT INTO tbl_ejm_wages_data_collection\s*\((.*?)\)", sql, re.S).group(1)
    for col in ("prev_cuml_w_days", "prev_cuml_pf_gross", "prev_cuml_pf_contribution",
                "prev_cuml_bonus_gross", "prev_cuml_fpf_contribution"):
        assert col in cols, col
    assert "c.date_from < :date_from" in sql
    assert ":cy_start" in sql and ":fy_start" in sql


def test_nsh_arms_post_regular_to_ns_hours_and_overtime_to_ot_hours():
    nsh = {label: str(q()) for code, label, q in BUILD_ARMS if code == "NSH"}
    col = lambda sql: re.search(r"INSERT INTO tbl_ejm_wages_data_collection\s*\((.*?)\)", sql, re.S).group(1)
    assert "ns_hours" in col(nsh["night hours"]) and "attendance_type = 'R'" in nsh["night hours"]
    assert "ot_hours" in col(nsh["night OT hours"]) and "attendance_type = 'O'" in nsh["night OT hours"]
    for sql in nsh.values():
        assert "working_hours," not in col(sql) and "whrs = 7.5" in sql


def test_every_attendance_read_takes_only_live_approved_rows():
    from src.hrms import ejmWagesData as m
    from src.hrms.holidayMaster import _insert_transactions_query
    reads = {c: str(q()) for c, _, q in BUILD_ARMS if c in ("ATT", "NSH", "INC")}
    reads["cash_att"] = m._cash_att_sql("1=1", "")
    reads["cash_pay"] = m._CASH_PAYMENT_INSERT
    reads["holiday"] = str(_insert_transactions_query())
    live = "da.is_active = 1 AND da.status_id = '3'"
    for name, sql in reads.items():
        assert sql.count("FROM daily_attendance da") >= 1, name
        # every attendance read carries the approved filter, never a bare is_active
        assert sql.count("da.is_active = 1") == sql.count(live) >= 1, name


def test_shift_letter_comes_from_spell_mst_everywhere():
    # daily_attendance.spell is NULL on nearly every row; only spell_id is set.
    for code in ("ATT", "NSH", "INC"):
        sql = str({c: q for c, _, q in BUILD_ARMS}[code]())
        assert "spell_mst sp ON sp.spell_id = da.spell_id" in sql, code
        assert "da.spell = 'C'" not in sql and "SUBSTR(da.spell" not in sql, code


def test_payroll_excel_rows_follow_the_upload_template_and_zero_fill():
    from decimal import Decimal
    from src.hrms.ejmWagesData import PAYROLL_EXCEL_MAP, _payroll_excel_rows
    codes = ["C_SHIFT_", "C_WORKING_HR_", "C_NSHR_", "C_FNE_"]
    hands = [(1, "18188", " UMESH YADAV-I "), (2, "18189", "HARENDRA RAI")]
    sums = {1: {"working_hours": Decimal("7.50"), "ns_hours": Decimal("0.50")}}
    header, rows = _payroll_excel_rows(codes, "EJM VOUCHER WORKER", hands, sums)
    assert header == ["Employee Code", "Employee Name", "PayScheme Name", *codes]
    # C_SHIFT_ is never fed from wages: always 0
    assert rows[0] == ["18188", "UMESH YADAV-I", "EJM VOUCHER WORKER", 0.0, 7.5, 0.5, 0.0]
    # a hand with no wages rows is still listed, all zeros
    assert rows[1] == ["18189", "HARENDRA RAI", "EJM VOUCHER WORKER", 0.0, 0.0, 0.0, 0.0]
    assert len(set(PAYROLL_EXCEL_MAP.values())) == len(PAYROLL_EXCEL_MAP)


def test_every_arm_stamps_both_pay_scheme_columns():
    # The table carries pay_scheme_id and the sls-era payschme_id; every arm
    # must fill both from the same bind so no row is ever half-keyed.
    for code, _, build in BUILD_ARMS:
        sql = str(build())
        cols = re.search(r"INSERT INTO tbl_ejm_wages_data_collection\s*\((.*?)\)", sql, re.S).group(1)
        assert "pay_scheme_id" in cols and "payschme_id" in cols, code
        assert sql.count(":pay_scheme_id, :pay_scheme_id, '") == 1, code


def test_incentive_arm_targets_atten_incentive():
    build = {code: q for code, _, q in BUILD_ARMS}["INC"]
    cols = re.search(r"INSERT INTO tbl_ejm_wages_data_collection\s*\((.*?)\)", str(build()), re.S).group(1)
    assert "atten_incentive" in cols
    assert "misc_earn" not in cols
    assert "atten_incentive" in EDITABLE_FIELDS
