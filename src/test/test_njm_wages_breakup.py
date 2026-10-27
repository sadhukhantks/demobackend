"""Wages Break Up Summary workbook layout (src/hrms/njmWagesBreakup.py).

No DB: these cover the parts that decide where numbers land — the pivot SQL
shape, the sheet writer and the Final Sheet links. Getting any of those wrong
misfiles money on a payment sheet, and that is not visible in a row count.
"""

import openpyxl
import pytest

from src.hrms.njmWagesBreakup import (
    BREAKUP_COLUMNS,
    PIVOTS,
    SHEETS,
    SHEET_HEADERS,
    _category_sql,
    _link_final,
    _totals,
    _write_sheet,
)


def _row(**over):
    base = {
        "ticket_no": "650003", "emp_name": "A B", "payscheme_id": 2017,
        "sub_dept_id": 21, "bank_acc_no": "0031845924793",
        "ifsc_code": "SBIN0002768",
        **{c: 0.0 for c in BREAKUP_COLUMNS},
    }
    base.update(over)
    return base


def test_every_pivot_defines_every_column():
    # A missing key would raise only when that sheet is generated, i.e. in
    # front of the operator on payment day.
    for key, pivot in PIVOTS.items():
        assert set(pivot) == set(BREAKUP_COLUMNS), key


def test_every_sheet_names_a_known_pivot_and_a_distinct_final_row():
    rows = [s.final_row for s in SHEETS]
    assert len(rows) == len(set(rows))
    for spec in SHEETS:
        assert spec.pivot is None or spec.pivot in PIVOTS
        # Cash is the only sheet not backed by a pay scheme.
        assert bool(spec.scheme_ids) == (spec.pivot is not None)


def test_category_sql_binds_and_only_drops_zero_gross_where_legacy_did():
    main = next(s for s in SHEETS if s.sheet == "Main Payroll Wages")
    staffs = next(s for s in SHEETS if s.sheet == "Staffs")

    sql = _category_sql(main)
    # Scheme ids, branch and period all arrive as binds, never interpolated.
    for bind in (":branch_id", ":date_from", ":date_to", ":scheme_ids"):
        assert bind in sql
    assert "2015" not in sql
    assert "WHERE g.gross_earn > 0" in sql
    assert "WHERE g.gross_earn > 0" not in _category_sql(staffs)


def test_write_sheet_lays_out_columns_and_sums_them():
    ws = openpyxl.Workbook().active
    ws["A1"] = "stale header"
    ws["A2"] = "last month"          # must not survive a regeneration
    ws["A3"] = "also last month"

    rows = [_row(gross_earn=100.5, net_pay=90.0),
            _row(ticket_no="650007", gross_earn=200.0, net_pay=180.0)]
    total_row = _write_sheet(ws, rows)

    assert total_row == 4
    assert [ws.cell(1, c).value for c in range(1, 25)] == list(SHEET_HEADERS)
    # EB no and bank account stay text: leading zeros are part of the value.
    assert ws.cell(2, 1).value == "650003"
    assert isinstance(ws.cell(2, 21).value, str)
    assert ws.cell(2, 21).value == "0031845924793"
    # C is gross, R is net payable — the two columns the mill reconciles on.
    assert ws.cell(2, 3).value == 100.5
    assert ws.cell(2, 18).value == 90.0
    assert ws.cell(3, 1).value == "650007"
    assert ws.cell(total_row, 3).value == "=SUM(C2:C3)"
    assert ws.cell(total_row, 20).value == "=SUM(T2:T3)"
    # The old third row is gone, not left below the total.
    assert ws.cell(5, 1).value is None


def test_write_sheet_with_no_rows_totals_zero_not_a_backwards_range():
    ws = openpyxl.Workbook().active
    total_row = _write_sheet(ws, [])
    assert total_row == 2
    # =SUM(C2:C1) is what a naive last-row calculation produces; it is not a
    # number Final Sheet can add.
    assert ws.cell(total_row, 3).value == 0
    assert ws.cell(total_row, 20).value == 0


def test_link_final_writes_one_column_left_of_the_data_sheet():
    final = openpyxl.Workbook().active
    spec = next(s for s in SHEETS if s.sheet == "Retired")
    _link_final(final, spec, 434)

    # Data sheet C (gross) -> Final Sheet B; data sheet T -> Final Sheet S.
    assert final.cell(spec.final_row, 2).value == "='Retired'!C434"
    assert final.cell(spec.final_row, 19).value == "='Retired'!T434"
    assert final.cell(spec.final_row, 20).value is None


def test_link_final_never_writes_the_hand_maintained_rows():
    # Rows 12 (Fng & Twisting) and 14 (ALMA) are typed by the operator; a
    # generated link there would silently overwrite their numbers.
    assert {12, 14}.isdisjoint({s.final_row for s in SHEETS})


def test_totals_sum_each_money_column():
    t = _totals([_row(gross_earn=1.257, pf=2.0), _row(gross_earn=2.0, pf=3.0)])
    assert t["gross_earn"] == pytest.approx(3.26)
    assert t["pf"] == 5.0
    assert t["gwf"] == 0.0
    assert set(t) == set(BREAKUP_COLUMNS)
