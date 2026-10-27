"""Tests for the NJM "Updates" sheet parsing and guards (src/hrms/njmWagesData.py).

The SQL itself is exercised against a live tenant; what is worth pinning here is
the sheet handling, because every one of these rules exists to stop a bad
workbook from half-loading a wage period.
"""
import io

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook

from src.hrms.njmWagesData import (
    CNT_COLUMNS,
    MAIN_WAGES_ARMS,
    MAIN_WAGES_SHEET_COLUMNS,
    OTH_COLUMNS,
    _as_date,
    _main_wages_steps,
    _num,
    _sheet_sql,
    _read_sheet,
    _resolve_or_fail,
    _rows_by_header,
    _validate_period,
    _OTH_INSERT,
)


def _upload(rows, filename="up.xlsx"):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return UploadFile(filename=filename, file=buf)


def _csv_upload(text_body, filename="up.csv"):
    return UploadFile(filename=filename, file=io.BytesIO(text_body.encode()))


class TestReadSheet:
    def test_reads_xlsx(self):
        sheet = _read_sheet(_upload([["A", "B"], [1, 2]]))
        assert sheet[0] == ["A", "B"]
        assert sheet[1] == [1, 2]

    def test_reads_csv(self):
        sheet = _read_sheet(_csv_upload("A,B\n1,2\n"))
        assert sheet == [["A", "B"], ["1", "2"]]

    def test_rejects_other_extensions(self):
        with pytest.raises(HTTPException) as exc:
            _read_sheet(_upload([["A"]], filename="up.txt"))
        assert exc.value.status_code == 400

    def test_rejects_empty_file(self):
        with pytest.raises(HTTPException) as exc:
            _read_sheet(UploadFile(filename="up.csv", file=io.BytesIO(b"")))
        assert exc.value.status_code == 400

    def test_rejects_unreadable_workbook(self):
        junk = UploadFile(filename="up.xlsx", file=io.BytesIO(b"not a workbook"))
        with pytest.raises(HTTPException) as exc:
            _read_sheet(junk)
        assert exc.value.status_code == 400


class TestRowsByHeader:
    def test_keys_rows_by_column_name(self):
        rows = _rows_by_header([["TICKET_NO", "AMOUNT"], ["A1", 250]], "TICKET_NO")
        assert rows == [{"TICKET_NO": "A1", "AMOUNT": 250}]

    def test_drops_rows_without_a_key(self):
        rows = _rows_by_header(
            [["TICKET_NO", "AMOUNT"], ["A1", 1], [None, 9], ["  ", 9]], "TICKET_NO"
        )
        assert [r["TICKET_NO"] for r in rows] == ["A1"]

    def test_missing_key_column_names_what_it_found(self):
        with pytest.raises(HTTPException) as exc:
            _rows_by_header([["WRONG", "AMOUNT"], ["x", 1]], "TICKET_NO")
        assert exc.value.status_code == 400
        assert "WRONG" in exc.value.detail

    def test_header_only_sheet_rejected(self):
        with pytest.raises(HTTPException):
            _rows_by_header([["TICKET_NO"]], "TICKET_NO")

    def test_all_keys_blank_rejected(self):
        with pytest.raises(HTTPException):
            _rows_by_header([["TICKET_NO"], [None], [""]], "TICKET_NO")


class TestNum:
    @pytest.mark.parametrize("raw,expected", [
        (5, 5), ("5", 5), (2.5, 2.5), ("2.5", 2.5),
        (None, 0), ("", 0), ("abc", 0), ([], 0),
    ])
    def test_coerces_or_defaults(self, raw, expected):
        assert _num(raw) == expected


class TestAsDate:
    @pytest.mark.parametrize("raw", ["2026-07-01", "01-07-2026", "01/07/2026"])
    def test_parses_common_formats(self, raw):
        assert _as_date(raw).isoformat() == "2026-07-01"

    def test_returns_none_for_junk(self):
        assert _as_date("not a date") is None
        assert _as_date(None) is None


class TestValidatePeriod:
    def test_accepts_a_valid_period(self):
        _validate_period("2026-07-01", "2026-07-31")

    def test_rejects_unparseable_dates(self):
        with pytest.raises(HTTPException) as exc:
            _validate_period("garbage", "2026-07-31")
        assert exc.value.status_code == 400

    def test_rejects_reversed_period(self):
        with pytest.raises(HTTPException) as exc:
            _validate_period("2026-07-31", "2026-07-01")
        assert "earlier" in exc.value.detail

    @pytest.mark.parametrize("bad", ["0006-09-10", "0002-09-10", "0202-09-10"])
    def test_rejects_half_typed_year(self, bad):
        # What a native date input emits mid-keystroke. Parsed fine before and
        # made the ATT arm collect a worker's whole history as one month.
        with pytest.raises(HTTPException) as exc:
            _validate_period(bad, "2026-08-31")
        assert "out of range" in exc.value.detail


class TestResolveOrFail:
    def test_pairs_rows_with_eb_ids(self):
        rows = [{"EB_NO": "A1"}, {"EB_NO": "A2"}]
        assert _resolve_or_fail(rows, "EB_NO", {"A1": 10, "A2": 20}) == [
            (10, rows[0]), (20, rows[1])
        ]

    def test_trims_the_key_before_matching(self):
        # emp_code is stored padded in places, and the map TRIMs on the way out.
        assert _resolve_or_fail([{"EB_NO": " A1 "}], "EB_NO", {"A1": 10})[0][0] == 10

    def test_one_unknown_code_rejects_the_whole_file(self):
        # All-or-nothing: a half-loaded wage period hides the missing people.
        with pytest.raises(HTTPException) as exc:
            _resolve_or_fail(
                [{"EB_NO": "A1"}, {"EB_NO": "GHOST"}], "EB_NO", {"A1": 10}
            )
        assert exc.value.status_code == 400
        assert "GHOST" in exc.value.detail
        assert "Nothing was saved" in exc.value.detail

    def test_long_missing_lists_are_truncated(self):
        rows = [{"EB_NO": f"X{i}"} for i in range(40)]
        with pytest.raises(HTTPException) as exc:
            _resolve_or_fail(rows, "EB_NO", {})
        assert "+20 more" in exc.value.detail


class TestColumnMaps:
    def test_oth_insert_binds_every_mapped_column(self):
        sql = str(_OTH_INSERT)
        for column in OTH_COLUMNS.values():
            assert f":{column}" in sql

    def test_oth_map_has_no_duplicate_targets(self):
        assert len(set(OTH_COLUMNS.values())) == len(OTH_COLUMNS)

    def test_oth_map_matches_legacy_column_count(self):
        # Legacy's INSERT listed 27 value columns after payscheme_id. Two are
        # deliberately gone: only payable leave transfers now, as el_days, so
        # nothing writes sl_days or ul_days any more.
        assert len(OTH_COLUMNS) == 25
        assert "C_sld_" not in OTH_COLUMNS
        assert "C_uldays_" not in OTH_COLUMNS

    def test_contractor_layout_is_positional_and_unique(self):
        assert len(set(CNT_COLUMNS.values())) == len(CNT_COLUMNS)
        assert CNT_COLUMNS["emp_code"] == 2


class TestMainWagesSteps:
    PARAMS = {"date_from": "2026-08-01", "date_to": "2026-08-31",
              "branch_id": 87, "pay_scheme_id": 1}

    def test_every_bind_is_supplied(self):
        # A bind added to the SQL but not to the step's params only fails at
        # execute time against a live tenant; catch it here.
        for label, stmt, binds in _main_wages_steps(self.PARAMS):
            missing = set(stmt._bindparams) - set(binds)
            assert not missing, f"{label}: unbound {missing}"

    # The 32 data_column_no values legacy EMPMILL12 mapped for every NJM main
    # scheme (164/167/169). The sheet must expose each, or the export and the
    # posting silently drop that component.
    LEGACY_MAP_COLUMNS = (
        "hours_wkd_1", "hours_wkd_2", "esi_days", "el_days", "sl_days",
        "lay_off_hrs", "piece_hours", "canteen", "sunday_adv", "other_adv",
        "el_advance", "installment_advance", "extra_hours_t", "extra_hours_p",
        "festival_hours", "arrear_plus", "arrear_minus", "piece_wages",
        "piece_wages_inc", "other_pay", "cl_days", "ul_days", "advance",
        "loom_production", "c_shift_days", "iltime_hrs", "ilpiece_hrs",
        "sardhelp_rate", "cadeligibility", "gwf_eleg", "iftu_eleg", "minus_bal",
    )

    def test_sheet_exposes_every_legacy_map_column(self):
        assert len(self.LEGACY_MAP_COLUMNS) == 32
        missing = set(self.LEGACY_MAP_COLUMNS) - set(MAIN_WAGES_SHEET_COLUMNS)
        assert not missing, missing
        sql = _sheet_sql()
        for col in MAIN_WAGES_SHEET_COLUMNS:
            assert f"w.{col}" in sql

    def test_night_shift_reads_spell_master_and_excludes_by_name(self):
        # daily_attendance.spell is NULL on ~90% of this tenant's rows, and the
        # legacy designation ids belong to another branch — both made the night
        # arm count almost nothing. Keyed off spell_mst and designation names.
        steps = {label: (str(stmt), binds)
                 for label, stmt, binds in _main_wages_steps(self.PARAMS)}
        sql, binds = steps["attendance"]
        assert "COALESCE(sp.spell_code, da.spell) = 'C'" in sql
        assert "d.desig NOT IN" in sql
        assert all(isinstance(x, str) for x in binds["night_excluded"])

    def test_attendance_sums_sub_departments_not_max(self):
        # worked_department_id is a sub-department here, so one dept_code gets
        # several rows per employee (WEAVING A 1 / B 1 / C 1). MAX would keep
        # only the biggest and short-pay every worker who rotates lines.
        steps = {label: str(stmt) for label, stmt, _ in _main_wages_steps(self.PARAMS)}
        sql = steps["attendance"]
        assert "MAX(CASE WHEN k." not in sql
        assert sql.count("SUM(CASE WHEN k.") == 7

    def test_winding_divides_bundle_unit_cards_by_15(self):
        # The /15 keys on the rate card's unit (BDL), not on a q_code list, so
        # a new cop card needs no code change. Winding only.
        steps = {label: (str(stmt), binds)
                 for label, stmt, binds in _main_wages_steps(self.PARAMS)}
        sql, binds = steps["winding"]
        assert "lk.unit = :bundle_unit" in sql
        assert "cop_qcodes" not in sql
        assert binds["bundle_unit"] == "BDL"
        assert binds["cop_kg_per_bundle"] == 15
        for label in ("beaming", "finishing", "weaving"):
            assert ":bundle_unit" not in steps[label][0], label

    def test_every_insert_writes_an_owned_arm(self):
        # Each INSERT names exactly one update_for the process will retire on
        # the next run; otherwise reruns pile up copies of that arm.
        inserts = [str(stmt) for _, stmt, _ in _main_wages_steps(self.PARAMS)
                   if str(stmt).lstrip().startswith("INSERT")]
        assert len(inserts) == len(MAIN_WAGES_ARMS)
        for sql in inserts:
            owned = [arm for arm in MAIN_WAGES_ARMS if f"'{arm}'" in sql]
            assert len(owned) == 1, owned
