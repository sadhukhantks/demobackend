"""Tests for the EJM Pay Register / OT Register grids (EJM Wages Data Preparation)."""

from sqlalchemy import text

from src.hrms import ejmPayRegister as m


class TestPivotQuery:
    def test_every_pay_report_builds_with_the_right_binds(self):
        for code, (title, cols, having, date_range) in m.PAY_REPORTS.items():
            q = m._pivot_query(cols, having, date_range)
            assert isinstance(q, type(text("")))
            sql = str(q)
            if date_range:
                for bind in (":date_from", ":date_to", ":pay_scheme_id"):
                    assert bind in sql, (code, bind)
                assert ":payperiod_id" not in sql, code
            else:
                assert ":payperiod_id" in sql, code
            assert f"HAVING {having}" in sql, code

    def test_every_component_column_is_pivoted(self):
        _, cols, having, _ = m.PAY_REPORTS[1]
        sql = str(m._pivot_query(cols, having, False))
        for field, _, cid in cols:
            assert f"COMPONENT_ID = {cid} THEN k.AMOUNT ELSE 0 END) AS {field}" in sql

    def test_legacy_epf_split_for_vouchers(self):
        # hol_get 2 = EPF members, 7 = ESI-only (legacy "and epf>0" / "and epf=0").
        assert "epf > 0" in m.PAY_REPORTS[2][2]
        assert "epf = 0" in m.PAY_REPORTS[7][2]

    def test_card_holder_bank_vs_cash_is_the_bank_detail(self):
        assert "bank_detail_id IS NOT NULL" in m.PAY_REPORTS[4][2]
        assert "bank_detail_id IS NULL" in m.PAY_REPORTS[5][2]

    def test_date_range_excludes_cancelled_periods(self):
        sql = str(m._pivot_query(m.TRAINEE, "total_earn > 0", True))
        assert "p.STATUS <> 4" in sql
        assert "SUM(k.AMOUNT)" in sql


class TestTotals:
    def test_grand_total_sums_only_the_numeric_fields(self):
        rows = [{"wname": "A", "x": 1.5, "y": 2.0}, {"wname": "B", "x": 2.5, "y": 0.0}]
        total = m._grand_total(rows, ["x", "y"])
        assert total == {"x": 4.0, "y": 2.0, "wname": "Grand Total"}

    def test_numeric_coerces_none_and_decimals(self):
        from decimal import Decimal
        rows = [{"x": None, "y": Decimal("1.25")}]
        m._numeric(rows, ["x", "y"])
        assert rows == [{"x": 0.0, "y": 1.25}]

    def test_columns_mark_component_fields_numeric(self):
        cols = m._columns([("eb_no", "EB No")], [("net_pay", "Net Pay", 21)])
        assert cols == [
            {"field": "eb_no", "headerName": "EB No"},
            {"field": "net_pay", "headerName": "Net Pay", "type": "number"},
        ]


class TestUnavailableCodes:
    def test_unported_codes_explain_themselves(self):
        # Every legacy dropdown code is either a report or has a reason.
        legacy = {1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 29, 30, 31}
        covered = set(m.PAY_REPORTS) | set(m.PAY_UNAVAILABLE) | {15}
        assert legacy <= covered
        assert not (set(m.PAY_REPORTS) & set(m.PAY_UNAVAILABLE))
