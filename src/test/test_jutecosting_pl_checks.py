"""Daily P&L lazy breakdowns and the jute-issue data check
(src/juteCosting/daily_pl_breakdown.py — ITM-027 §1.5 / §1.6).

The flag rules (flag_issue_rows / build_issue_checks) and the breakdown
builders are pure and exercised on stub rows; validation and the query wiring
go through TestClient on a throwaway app with a mocked session, as
test_jutecosting_daily_pl.py does.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pymysql.err
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from src.juteCosting.daily_pl_breakdown import (
    BREAKDOWN_HEADS,
    DUPLICATE_GROUP_MIN,
    DUPLICATE_WINDOW_SECONDS,
    FLAG_LABELS,
    INVALID_HEAD_MSG,
    LINE_FLAGS,
    MR_OPEN_STATUSES,
    OVER_ISSUE_TOLERANCE_KG,
    RATE_OUTLIER_MIN_LINES,
    build_issue_checks,
    build_power_breakdown,
    build_raw_material_breakdown,
    build_stores_breakdown,
    build_wages_breakdown,
    compute_rate_medians,
    duplicate_context,
    flag_issue_rows,
    get_current_user_with_refresh,
    get_tenant_db,
    router,
)
from src.juteCosting.daily_pl_breakdown_query import (
    get_hands_by_department_query,
    get_jute_issue_by_mr_query,
    get_jute_issue_by_quality_query,
    get_jute_issue_check_rows_query,
    get_jute_issue_waste_ledger_query,
    get_stores_by_category_query,
    get_stores_by_dept_query,
    get_stores_by_expense_type_query,
    get_stores_by_group_query,
    get_stores_top_items_query,
)

app = FastAPI()
app.include_router(router, prefix="/api/juteCostReports")
client = TestClient(app)

BREAKDOWN_URL = "/api/juteCostReports/daily_pl/breakdown"
CHECKS_URL = "/api/juteCostReports/daily_pl/jute_issue_checks"
TODAY = date(2026, 10, 20)
KEYED = datetime(2026, 10, 5, 10, 0, 0)
RANGE = {"co_id": 2, "branch_id": 29, "date_from": "2026-10-01", "date_to": "2026-10-19"}


def _line(jute_issue_id, item_id=57, weight=1000, value=None, mr_rate=12000, **over):
    """One get_jute_issue_check_rows_query row; value defaults to weight x rate."""
    row = {
        "jute_issue_id": jute_issue_id,
        "issue_date": date(2026, 10, 5),
        "issue_status_id": 21,
        "jute_mr_id": 100,
        "jute_mr_li_id": 500,
        "branch_mr_no": 107,
        "jute_gate_entry_no": 76,
        "mr_status_id": 3,
        "mr_status": "APPROVED",
        "item_id": item_id,
        "item_name": "D TD-6",
        "issue_item_id": item_id,
        "mr_item_id": item_id,
        "weight_kg": weight,
        "issue_value": (weight / 100 * mr_rate) if value is None else value,
        "mr_rate_qtl": mr_rate,
        "mr_line_balance_kg": 10.0,
        "keyed_at": KEYED,
    }
    row.update(over)
    return row


def _flags(rows):
    return {r["jute_issue_id"]: r["flags"] for r in flag_issue_rows(rows, compute_rate_medians(rows), TODAY)}


# ---------------------------------------------------------------------------
# Flag rules
# ---------------------------------------------------------------------------


class TestFlagRules:
    def test_clean_line_has_no_flags(self):
        rows = [_line(1)]
        out = flag_issue_rows(rows, compute_rate_medians(rows), TODAY)
        assert len(out) == 1 and out[0]["flags"] == []
        assert out[0]["implied_rate_qtl"] == 12000.0 and out[0]["suggested_value"] == 120000.0
        assert out[0]["median_rate_qtl"] is None  # one line is not a benchmark
        assert out[0]["issue_date"] == "2026-10-05" and out[0]["mr_line_balance_kg"] == 10.0
        assert out[0]["dup_group_size"] == 1 and out[0]["dup_seconds"] is None

    def test_zero_weight(self):
        assert _flags([_line(1, weight=0, value=0)])[1] == ["ZERO_WEIGHT"]
        assert _flags([_line(2, weight=None, value=0)])[2] == ["ZERO_WEIGHT"]

    def test_no_value(self):
        out = flag_issue_rows([_line(1, value=0)], {}, TODAY)[0]
        assert out["flags"] == ["NO_VALUE"] and out["implied_rate_qtl"] is None
        assert out["suggested_value"] == 120000.0
        null_value = {**_line(2), "issue_value": None}
        assert flag_issue_rows([null_value], {}, TODAY)[0]["flags"] == ["NO_VALUE"]

    def test_no_mr_rate(self):
        out = flag_issue_rows([_line(1, value=0, mr_rate=0)], {}, TODAY)[0]
        assert out["flags"] == ["NO_VALUE", "NO_MR_RATE"]
        assert out["mr_rate_qtl"] is None and out["suggested_value"] is None and out["suggested_basis"] is None
        assert flag_issue_rows([_line(2, value=1, mr_rate=None)], {}, TODAY)[0]["flags"] == ["NO_MR_RATE"]

    def test_value_rate_mismatch_tolerance(self):
        # 5 % band: 12,600 is exactly 5 % over -> not flagged; 12,601 is.
        assert _flags([_line(1, value=126000)])[1] == []
        assert _flags([_line(2, value=126010)])[2] == ["VALUE_RATE_MISMATCH"]
        assert _flags([_line(3, value=100000)])[3] == ["VALUE_RATE_MISMATCH"]

    def test_rate_outlier_needs_three_rated_lines(self):
        assert RATE_OUTLIER_MIN_LINES == 3
        # Distinct MR lines (jute_mr_li_id) so the identical-line rule stays out of the way.
        two = [_line(1, mr_rate=12000, jute_mr_li_id=501), _line(2, mr_rate=20000, jute_mr_li_id=502)]
        assert all(f == [] for f in _flags(two).values())
        three = [_line(1, mr_rate=12000, jute_mr_li_id=501), _line(2, mr_rate=12100, jute_mr_li_id=502),
                 _line(3, mr_rate=20000, jute_mr_li_id=503)]
        flags = _flags(three)
        assert flags[1] == [] and flags[2] == [] and flags[3] == ["RATE_OUTLIER"]
        out = flag_issue_rows(three, compute_rate_medians(three), TODAY)
        assert out[2]["median_rate_qtl"] == 12100.0
        # A different item has its own median; unrated lines do not count.
        mixed = three + [_line(4, item_id=58, mr_rate=20000, jute_mr_li_id=504),
                         _line(5, item_id=57, mr_rate=0, value=0, jute_mr_li_id=505)]
        assert _flags(mixed)[4] == [] and "RATE_OUTLIER" not in _flags(mixed)[5]
        assert compute_rate_medians(mixed) == {57: {"median": 12100.0, "n": 3}, 58: {"median": 20000.0, "n": 1}}

    def test_suggested_value_falls_back_to_the_quality_median(self):
        rows = [_line(1, mr_rate=12000), _line(2, mr_rate=12100), _line(3, mr_rate=13000),
                _line(4, weight=500, value=0, mr_rate=0)]
        out = {r["jute_issue_id"]: r for r in flag_issue_rows(rows, compute_rate_medians(rows), TODAY)}
        assert out[1]["suggested_basis"] == "mr_rate" and out[1]["suggested_value"] == 120000.0
        assert out[4]["flags"] == ["NO_VALUE", "NO_MR_RATE"]
        assert out[4]["median_rate_qtl"] == 12100.0
        assert out[4]["suggested_value"] == 500 / 100 * 12100 and out[4]["suggested_basis"] == "median"

    def test_item_mismatch(self):
        assert _flags([_line(1, mr_item_id=58)])[1] == ["ITEM_MISMATCH"]
        assert _flags([_line(2, issue_item_id=None)])[2] == []

    def test_mr_status_split(self):
        assert _flags([_line(0, mr_status_id=3)])[0] == []
        assert _flags([_line(1, mr_status_id=13)])[1] == []  # Pending chain root is normal
        # 17 PENDING APPROVAL LEVEL 2 and 24 TRANSFER PENDING are in flight too.
        assert MR_OPEN_STATUSES == (1, 17, 20, 21, 24)
        for open_status in (1, 17, 20, 21, 24):
            assert _flags([_line(2, mr_status_id=open_status)])[2] == ["MR_OPEN"], open_status
        for bad in (4, 6, 48):
            assert _flags([_line(3, mr_status_id=bad)])[3] == ["MR_STATUS"], bad
        assert _flags([_line(5, mr_status_id=None)])[5] == ["MR_STATUS"]
        assert FLAG_LABELS["MR_OPEN"] == "MR not yet approved — rate may change"
        assert FLAG_LABELS["MR_STATUS"] == "MR rejected / cancelled / returned"
        assert list(FLAG_LABELS).index("MR_OPEN") < list(FLAG_LABELS).index("MR_STATUS")

    def test_identical_pair_is_not_flagged_but_measured(self):
        assert DUPLICATE_WINDOW_SECONDS == 120 and DUPLICATE_GROUP_MIN == 3
        assert FLAG_LABELS["DUPLICATE"] == "Possible double entry (3+ identical lines)"
        pair = [_line(1), _line(2, keyed_at=KEYED + timedelta(seconds=90))]
        out = {r["jute_issue_id"]: r for r in flag_issue_rows(pair, {}, TODAY)}
        # Keyed 90 s apart with the same value: still a normal repeated trolley weight.
        assert out[1]["flags"] == [] and out[2]["flags"] == []
        assert out[1]["dup_group_size"] == 2 and out[1]["dup_seconds"] == 90 and out[2]["dup_seconds"] == 90
        assert _flags([_line(1), _line(2, keyed_at=KEYED)])[2] == []  # same second, still a pair

    def test_dup_seconds_is_informational(self):
        # Same weight, different booked value: no same-value partner -> dup_seconds None.
        diff_value = [_line(1), _line(2, value=100000, keyed_at=KEYED + timedelta(seconds=10))]
        out = {r["jute_issue_id"]: r for r in flag_issue_rows(diff_value, {}, TODAY)}
        assert out[1]["dup_seconds"] is None and out[1]["dup_group_size"] == 2 and out[1]["flags"] == []
        # Two zero-value lines (COALESCE(issue_value, 0) equal) 10 s apart: gap measured, not flagged.
        zeros = [_line(1, value=0), {**_line(2, keyed_at=KEYED + timedelta(seconds=10)), "issue_value": None}]
        out = {r["jute_issue_id"]: r for r in flag_issue_rows(zeros, {}, TODAY)}
        assert out[1]["dup_seconds"] == 10 and "DUPLICATE" not in out[1]["flags"]
        # A missing timestamp on either side: no gap.
        no_ts = [_line(1, keyed_at=None), _line(2)]
        out = {r["jute_issue_id"]: r for r in flag_issue_rows(no_ts, {}, TODAY)}
        assert out[1]["dup_seconds"] is None and out[2]["dup_seconds"] is None
        # Different date, MR line or weight: different groups.
        others = [_line(1), _line(2, jute_mr_li_id=501, keyed_at=KEYED), _line(3, weight=999, keyed_at=KEYED),
                  _line(4, issue_date=date(2026, 10, 6), keyed_at=KEYED)]
        assert all(f == [] for f in _flags(others).values())
        assert duplicate_context(others) == [(1, None)] * 4

    def test_three_identical_lines_flag_regardless_of_time(self):
        trio = [_line(1), _line(2, keyed_at=KEYED + timedelta(hours=1)), _line(3, keyed_at=KEYED + timedelta(hours=2))]
        out = {r["jute_issue_id"]: r for r in flag_issue_rows(trio, {}, TODAY)}
        assert all(out[i]["flags"] == ["DUPLICATE"] and out[i]["dup_group_size"] == 3 for i in (1, 2, 3))
        assert out[1]["dup_seconds"] == 3600 and out[2]["dup_seconds"] == 3600
        # Differing values and no timestamps still count (size rule only).
        trio_nt = [_line(i, value=v, keyed_at=None) for i, v in ((1, 1.0), (2, 2.0), (3, 3.0))]
        assert all("DUPLICATE" in f for f in _flags(trio_nt).values())
        # ISO-string timestamps (as a JSON fixture would carry) are accepted for the gap.
        iso = [_line(1, keyed_at="2026-10-05T10:00:00"), _line(2, keyed_at="2026-10-05 10:01:00")]
        assert flag_issue_rows(iso, {}, TODAY)[0]["dup_seconds"] == 60

    def test_several_flags_in_rule_order(self):
        row = _line(1, value=0, mr_rate=0, mr_item_id=58, mr_status_id=6, mr_line_balance_kg=-5)
        twins = [_line(2, value=0, mr_rate=0), _line(3, value=0, mr_rate=0)]
        assert _flags([row] + twins)[1] == ["NO_VALUE", "NO_MR_RATE", "ITEM_MISMATCH", "MR_STATUS", "DUPLICATE", "OVER_ISSUE"]
        opened = _line(9, value=0, mr_rate=0, mr_status_id=1)
        assert _flags([opened])[9] == ["NO_VALUE", "NO_MR_RATE", "MR_OPEN"]

    def test_decimals_are_accepted(self):
        row = _line(1, weight=Decimal("1470.000"), value=Decimal("183750.00"), mr_rate=Decimal("12500"))
        out = flag_issue_rows([row], {}, TODAY)[0]
        assert out["flags"] == [] and out["weight_kg"] == 1470.0 and out["implied_rate_qtl"] == 12500.0

    def test_future_date(self):
        assert _flags([_line(1, issue_date=TODAY + timedelta(days=1))])[1] == ["FUTURE_DATE"]
        assert _flags([_line(2, issue_date=TODAY)])[2] == []
        assert _flags([_line(3, issue_date="2026-10-21")])[3] == ["FUTURE_DATE"]  # ISO string accepted

    def test_over_issue_boundary(self):
        assert OVER_ISSUE_TOLERANCE_KG == 1.0
        assert _flags([_line(1, mr_line_balance_kg=-1.0)])[1] == []
        assert _flags([_line(2, mr_line_balance_kg=Decimal("-1.001"))])[2] == ["OVER_ISSUE"]
        out = flag_issue_rows([_line(3, mr_line_balance_kg=None)], {}, TODAY)[0]
        assert out["flags"] == [] and out["mr_line_balance_kg"] is None  # the view no longer lists the line


class TestBuildIssueChecks:
    def _cloth(self, *days):
        return [{"tran_date": d, "weaving_quality_name": "SACK A", "production_mt": t} for d, t in days]

    def test_summary_rows_and_day_flags(self):
        d1, d2, d3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
        rows = [
            _line(1, issue_date=d1, weight=10600),
            _line(2, issue_date=d1, weight=500, value=0),
            _line(3, issue_date=d3, weight=1000, value=0, mr_rate=0),
            # an identical pair (counted, not flagged) on another MR line
            _line(4, issue_date=d1, weight=600, jute_mr_li_id=777),
            _line(5, issue_date=d1, weight=600, jute_mr_li_id=777),
        ]
        cloth = self._cloth((d1, 10), (d2, 5), (d3, 0))
        data = build_issue_checks(rows, cloth, d1, d3, 1.06, TODAY)
        s = data["summary"]
        assert s["lines"] == 5 and s["flagged"] == 2 and s["identical_pairs"] == 1
        assert s["by_flag"]["DUPLICATE"] == 0
        assert s["by_flag"]["NO_VALUE"] == 2 and s["by_flag"]["NO_MR_RATE"] == 1
        assert s["by_flag"]["ZERO_WEIGHT"] == 0 and s["by_flag"]["NO_ISSUE_DAY"] == 1
        assert set(s["by_flag"]) == set(LINE_FLAGS) | {"NO_ISSUE_DAY"}
        assert s["issued_kg"] == 13300.0 and s["cloth_t"] == 15.0
        assert s["jute_kg_per_kg_cloth"] == round(13300 / 15000, 3) and s["std"] == 1.06
        assert s["ratio_flag"] == "LOW"
        assert [r["jute_issue_id"] for r in data["rows"]] == [2, 3]  # flagged only, date then id
        assert data["day_flags"] == [{"tran_date": "2026-10-02", "cloth_t": 5.0, "issued_kg": 0.0, "flag": "NO_ISSUE_DAY"}]
        assert data["flag_labels"] == FLAG_LABELS and data["flag_labels"]["NO_MR_RATE"] == "MR line has no rate"

    def test_ratio_flags(self):
        d = date(2026, 10, 1)
        cloth = self._cloth((d, 10))
        assert build_issue_checks([_line(1, issue_date=d, weight=10600)], cloth, d, d, 1.06, TODAY)["summary"]["ratio_flag"] is None
        assert build_issue_checks([_line(1, issue_date=d, weight=12600)], cloth, d, d, 1.06, TODAY)["summary"]["ratio_flag"] == "HIGH"
        assert build_issue_checks([_line(1, issue_date=d, weight=8900)], cloth, d, d, 1.06, TODAY)["summary"]["ratio_flag"] == "LOW"
        empty = build_issue_checks([], [], d, d, 1.06, TODAY)
        assert empty["summary"]["jute_kg_per_kg_cloth"] is None and empty["summary"]["ratio_flag"] is None
        assert empty["rows"] == [] and empty["day_flags"] == [] and empty["summary"]["lines"] == 0
        assert empty["summary"]["identical_pairs"] == 0
        assert set(empty["summary"]["by_flag"]) == set(FLAG_LABELS)  # stable shape, NO_ISSUE_DAY included

    def test_cloth_outside_the_range_is_ignored(self):
        d = date(2026, 10, 5)
        cloth = self._cloth((date(2026, 9, 30), 10), (d, 10))
        data = build_issue_checks([_line(1, issue_date=d, weight=10600)], cloth, d, d, 1.06, TODAY)
        assert data["summary"]["cloth_t"] == 10.0 and data["day_flags"] == []

    def test_waste_ledger_lines_are_counted_outside_the_check(self):
        d = date(2026, 10, 5)
        ledger = [{"lines": 85, "kg": Decimal("11829.000"), "value": Decimal("0.00")}]
        data = build_issue_checks([_line(1, issue_date=d)], [], d, d, 1.06, TODAY, ledger)
        s = data["summary"]
        assert s["waste_ledger_lines"] == 85 and s["waste_ledger_kg"] == 11829.0 and s["waste_ledger_value"] == 0.0
        assert s["lines"] == 1  # not part of the checked lines
        assert data["notes"] == ["85 waste-ledger lines (11.8 t, booked ₹0 by design) are outside this check"]
        none = build_issue_checks([], [], d, d, 1.06, TODAY, [{"lines": 0, "kg": None, "value": None}])
        assert none["summary"]["waste_ledger_lines"] == 0 and none["notes"] == []
        assert build_issue_checks([], [], d, d, 1.06, TODAY)["summary"]["waste_ledger_kg"] == 0.0


# ---------------------------------------------------------------------------
# Breakdown builders
# ---------------------------------------------------------------------------


class TestBreakdownBuilders:
    def test_raw_material(self):
        by_quality = [
            {"item_id": 57, "item_name": "D TD-6", "lines": 92, "kg": Decimal("125162.000"), "value": Decimal("15000000.00"),
             "valued_kg": Decimal("100000.000"), "zero_value_lines": Decimal("28"), "mr_rated_value": Decimal("3000000.00")},
            {"item_id": 59, "item_name": "D TD-7", "lines": 31, "kg": 19885, "value": 0, "valued_kg": 0, "zero_value_lines": 31, "mr_rated_value": 0},
        ]
        by_mr = [{"jute_mr_id": 1, "branch_mr_no": 107, "jute_gate_entry_no": 76, "mr_date": date(2026, 9, 28),
                  "item_name": "D TD-6, D TD-7", "items": 2, "mr_rate_qtl": Decimal("12875.40"), "kg": 5000, "value": 643750, "lines": 3},
                 {"jute_mr_id": 2, "branch_mr_no": 108, "jute_gate_entry_no": 77, "mr_date": date(2026, 9, 29),
                  "item_name": "D TD-6", "items": 1, "mr_rate_qtl": None, "kg": 900, "value": 0, "lines": 1}]
        by_day = [{"tran_date": date(2026, 10, 1), "kg": 17807, "value": 2235409, "lines": 18, "zero_value_lines": 0}]
        waste = [{"item_name": "Caddis", "kg": 100, "value": 1500}]
        data = build_raw_material_breakdown(by_quality, by_mr, by_day, waste)
        q = data["by_quality"][0]
        assert q["avg_rate_qtl"] == 15000.0 and q["zero_value_lines"] == 28 and q["lines"] == 92
        assert data["by_quality"][1]["avg_rate_qtl"] is None
        assert data["by_mr"][0] == {
            "jute_mr_id": 1, "branch_mr_no": 107, "jute_gate_entry_no": 76, "mr_date": "2026-09-28",
            "item_name": "D TD-6, D TD-7", "items": 2, "mr_rate_qtl": 12875.4, "kg": 5000.0, "value": 643750.0, "lines": 3,
        }
        assert data["by_mr"][1]["mr_rate_qtl"] is None and data["by_mr"][1]["items"] == 1  # no rated line
        assert data["by_day"][0]["tran_date"] == "2026-10-01" and data["by_day"][0]["lines"] == 18
        assert data["waste_by_item"] == [{"item_name": "Caddis", "kg": 100.0, "value": 1500.0}]
        assert data["summary"] == {"lines": 123, "kg": 145047.0, "value": 15000000.0, "avg_rate_qtl": 15000.0, "zero_value_lines": 59, "mr_rated_value": 3000000.0}

    def test_stores(self):
        data = build_stores_breakdown(
            [{"category": "SPARES", "value": Decimal("1000.50"), "lines": 10}, {"category": "GENERAL", "value": 200, "lines": 2}],
            [{"item_grp_name": "LOOM CAMBS", "category": "SPARES", "value": 500, "lines": 3}],
            [{"dept_id": 15, "dept_desc": "BATCHING(EJM)", "value": 400, "lines": 4}],
            [{"expense_type_id": 7, "expense_type_name": "Capital", "value": 100, "lines": 1}],
            [{"item_name": "Ecofin", "item_grp_name": "OIL/GREASE/LUBRICANTS", "qty": Decimal("12.500"), "value": 900}],
        )
        assert data["by_category"][0] == {"category": "SPARES", "value": 1000.5, "lines": 10, "label": "Machine parts & spares"}
        assert data["summary"] == {"value": 1200.5, "lines": 12}
        assert data["top_items"][0]["qty"] == 12.5 and data["by_dept"][0]["dept_desc"] == "BATCHING(EJM)"
        assert data["by_expense_type"][0]["expense_type_name"] == "Capital" and "Capital" in data["notes"][0]

    def test_stores_total_is_the_unrounded_sum_rounded_once(self):
        # Each category rounds DOWN by 0.004; the three together are 0.012 over.
        by_category = [
            {"category": "SPARES", "value": Decimal("100.004"), "lines": 1},
            {"category": "CONSUMABLES", "value": 200.004, "lines": 1},
            {"category": "GENERAL", "value": 300.004, "lines": 1},
        ]
        data = build_stores_breakdown(by_category, [], [], [], [])
        assert [c["value"] for c in data["by_category"]] == [100.0, 200.0, 300.0]  # rows: 2 dp each
        assert data["summary"]["value"] == round(100.004 + 200.004 + 300.004, 2) == 600.01  # not 600.00

    def test_power(self):
        missing = build_power_breakdown(None, None)
        assert missing["available"] is False and missing["by_meter"] == [] and missing["summary"]["kwh"] is None
        data = build_power_breakdown(
            [{"meter_code": "MAIN", "meter_name": "Main grid meter", "meter_type": "GRID", "kwh": Decimal("150000.00"),
              "dg_hours": 0, "dg_litres": 0, "max_demand_kva": None, "days": 1},
             {"meter_code": "DG1", "meter_name": "DG set 1", "meter_type": "DG", "kwh": 500, "dg_hours": 4, "dg_litres": 60, "max_demand_kva": Decimal("100.5"), "days": 2}],
            [{"tran_date": date(2026, 10, 6), "meter_type": "GRID", "kwh": 150000}],
        )
        assert data["available"] is True and data["by_meter"][0]["meter_type_label"] == "Grid"
        assert data["by_meter"][1]["max_demand_kva"] == 100.5 and data["by_meter"][1]["days"] == 2
        assert data["summary"] == {"kwh": 150500.0, "dg_hours": 4.0, "dg_litres": 60.0}
        assert data["by_day_type"] == [{"tran_date": "2026-10-06", "meter_type": "GRID", "kwh": 150000.0}]

    def test_wages(self):
        rate = {"value": 400.0, "source": "proxy", "effective_date": date(2026, 10, 1), "branch_id": 29}
        data = build_wages_breakdown(
            [{"spell_id": 109, "spell_name": "A", "rows": 6782, "hours": Decimal("54000.00"), "hands": Decimal("6750.00")},
             {"spell_id": 110, "spell_name": "B", "rows": 4142, "hours": 32000, "hands": 4000}],
            [{"designation": "WINDER > 12 LBS", "rows": 1143, "hours": 9000, "hands": 1125}],
            [{"department": "Not assigned", "rows": 10000, "hours": 80000, "hands": 10000},
             {"department": "BATCHING(EJM)", "rows": 924, "hours": 6000, "hands": 750}],
            [{"working_hours": Decimal("8.00"), "rows": 10533}],
            rate,
        )
        assert data["summary"] == {"rows": 10924, "hours": 86000.0, "hands": 10750.0, "wages": 4300000.0}
        assert data["rate"] == {"value": 400.0, "source": "proxy", "effective_date": "2026-10-01", "scope": "branch"}
        assert data["notes"] == [
            "92 % of the attendance rows carry no resolvable department (worked designation → "
            "designation_mst.dept_id) — shown as 'Not assigned'."
        ]
        assert data["hours_profile"] == [{"working_hours": 8.0, "rows": 10533}]
        no_rate = build_wages_breakdown([], [], [], [], None)
        assert no_rate["rate"] is None and no_rate["summary"]["wages"] is None and no_rate["notes"] == []


class TestQueries:
    def test_check_rows_query_scope(self):
        sql = str(get_jute_issue_check_rows_query())
        assert "ji.status_id NOT IN (4, 6)" in sql and "vw_jute_stock_outstanding vs" in sql
        assert "ji.update_date_time AS keyed_at" in sql and "status_mst sm" in sql
        assert "dup" not in sql and "SELECT COUNT(*)" not in sql  # grouping is done in Python
        assert sql.count("vw_jute_stock_outstanding") == 1
        assert "item_type_id" not in sql  # waste lines included in the check
        assert "COALESCE(ji.item_id, mrli.actual_item_id)" in sql

    def test_breakdown_queries_keep_the_scope_and_caps(self):
        q = str(get_jute_issue_by_quality_query())
        assert "COALESCE(ig.item_type_id, 0) <> 3" in q and "LEFT JOIN item_mst iq" in q and "AS `lines`" in q
        assert q.index("LEFT JOIN item_mst iq") < q.index("WHERE b.co_id")
        mr = str(get_jute_issue_by_mr_query())
        assert "LIMIT 30" in mr and "LEFT JOIN jute_mr jm" in mr
        assert "COALESCE(jm.jute_mr_date, jm.jute_gate_entry_date) AS mr_date" in mr
        dept = str(get_stores_by_dept_query())
        assert "LEFT JOIN dept_mst d" in dept and "LIMIT 15" in dept and dept.index("dept_mst") < dept.index("WHERE (ih.active")
        assert "expense_type_mst et" in str(get_stores_by_expense_type_query())

    def test_by_mr_groups_on_the_mr_only(self):
        mr = " ".join(str(get_jute_issue_by_mr_query()).split())
        # One row per MR: the GROUP BY is the MR header, nothing from the line / item.
        start = mr.index("GROUP BY")
        group_by = mr[start : mr.index("ORDER BY", start)]  # (GROUP_CONCAT has an ORDER BY of its own)
        assert group_by == (
            "GROUP BY jm.jute_mr_id, jm.branch_mr_no, jm.jute_gate_entry_no, "
            "COALESCE(jm.jute_mr_date, jm.jute_gate_entry_date) "
        )
        assert "COUNT(*) AS `lines`" in mr
        assert "COUNT(DISTINCT COALESCE(ji.item_id, mrli.actual_item_id)) AS items" in mr
        assert "GROUP_CONCAT(DISTINCT iq.item_name ORDER BY iq.item_name SEPARATOR ', ') AS item_name" in mr
        # kg-weighted MR rate over the rated lines (₹/qtl), NULL when none is rated.
        assert "COALESCE(ji.weight, 0) * mrli.rate" in mr and "NULLIF(" in mr and "AS mr_rate_qtl" in mr
        assert "mrli.rate AS mr_rate_qtl" not in mr
        assert "COALESCE(ig.item_type_id, 0) <> 3" in mr

    def test_department_routes_through_the_designation(self):
        sql = " ".join(str(get_hands_by_department_query()).split())
        assert "LEFT JOIN designation_mst dg ON dg.designation_id = da.worked_designation_id" in sql
        assert "LEFT JOIN dept_mst dp ON dp.dept_id = dg.dept_id" in sql
        assert "COALESCE(dp.dept_desc, 'Not assigned') AS department" in sql
        assert "worked_department_id" not in sql

    def test_stores_category_is_unrounded_display_tables_round(self):
        assert "ROUND(SUM(il.issue_qty" not in str(get_stores_by_category_query())
        for q in (get_stores_by_group_query, get_stores_by_dept_query, get_stores_by_expense_type_query, get_stores_top_items_query):
            assert "ROUND(SUM(il.issue_qty" in str(q()), q.__name__

    def test_waste_ledger_query(self):
        sql = str(get_jute_issue_waste_ledger_query())
        assert "ji.jute_mr_li_id IS NULL" in sql and "ji.status_id NOT IN (4, 6)" in sql
        assert "jute_mr_li mrli" not in sql and "AS `lines`" in sql and "AS kg" in sql


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _rows_result(mappings):
    res = MagicMock()
    res.fetchall.return_value = [SimpleNamespace(_mapping=m) for m in mappings]
    return res


def _scope_ok():
    res = MagicMock()
    res.first.return_value = (1,)
    return res


def _missing_table():
    return ProgrammingError("SELECT 1", {}, pymysql.err.ProgrammingError(1146, "Table 'dev3.jute_power_reading' doesn't exist"))


class TestEndpoints:
    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_head_validation_before_any_query(self):
        assert BREAKDOWN_HEADS == ("raw_material", "stores", "power", "wages")
        r = client.get(BREAKDOWN_URL, params={**RANGE, "head": "capital"})
        assert r.status_code == 400 and r.json()["detail"] == INVALID_HEAD_MSG
        r = client.get(BREAKDOWN_URL, params=RANGE)
        assert r.status_code == 400 and r.json()["detail"] == INVALID_HEAD_MSG
        r = client.get(BREAKDOWN_URL, params={"head": "stores"})
        assert r.status_code == 400 and r.json()["detail"] == "co_id is required"
        r = client.get(BREAKDOWN_URL, params={**RANGE, "date_from": "2026-10-19", "date_to": "2026-10-01", "head": "stores"})
        assert r.status_code == 400 and r.json()["detail"] == "date_to is before date_from"
        r = client.get(CHECKS_URL, params={**RANGE, "date_from": "2026-01-01"})
        assert r.status_code == 400 and "at most 62 days" in r.json()["detail"]
        self.db.execute.assert_not_called()

    def test_raw_material_wiring(self):
        self.db.execute.side_effect = [
            _scope_ok(),
            _rows_result([{"item_id": 57, "item_name": "D TD-6", "lines": 2, "kg": 2000, "value": 240000, "valued_kg": 2000, "zero_value_lines": 0, "mr_rated_value": 0}]),
            _rows_result([]),
            _rows_result([{"tran_date": date(2026, 10, 1), "kg": 2000, "value": 240000, "lines": 2, "zero_value_lines": 0}]),
            _rows_result([]),
        ]
        r = client.get(BREAKDOWN_URL, params={**RANGE, "head": "raw_material"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["summary"]["kg"] == 2000.0 and data["by_quality"][0]["avg_rate_qtl"] == 12000.0
        assert data["meta"] == {"co_id": 2, "branch_id": 29, "date_from": "2026-10-01", "date_to": "2026-10-19", "head": "raw_material"}
        calls = self.db.execute.call_args_list
        assert len(calls) == 5
        assert calls[1][0][1] == {"co_id": 2, "branch_id": 29, "date_from": date(2026, 10, 1), "date_to": date(2026, 10, 19)}
        assert "LIMIT 30" in str(calls[2][0][0]) and "ig.item_type_id = 3" in str(calls[4][0][0])

    def test_stores_wiring(self):
        self.db.execute.side_effect = [_scope_ok()] + [_rows_result([])] * 5
        r = client.get(BREAKDOWN_URL, params={**RANGE, "head": "stores"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["summary"] == {"value": 0.0, "lines": 0}
        assert self.db.execute.call_count == 6

    def test_power_table_missing_is_unavailable_not_500(self):
        self.db.execute.side_effect = [_scope_ok(), _missing_table()]
        r = client.get(BREAKDOWN_URL, params={**RANGE, "head": "power"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["available"] is False and data["by_meter"] == []
        assert self.db.execute.call_count == 2  # by_day_type never asked for
        self.db.rollback.assert_called_once()

    def test_wages_wiring_resolves_the_rate_on_date_to(self):
        params = [{"cost_param_id": 4, "co_id": 2, "branch_id": 29, "effective_date": date(2026, 10, 1), "param_code": "WAGE_PER_HAND_DAY",
                   "param_value": 400, "uom": "₹/hand-day", "product": None, "source": "proxy", "remarks": None, "active": 1}]
        self.db.execute.side_effect = [
            _scope_ok(),
            _rows_result(params),
            _rows_result([{"spell_id": 109, "spell_name": "A", "rows": 10, "hours": 80, "hands": 10}]),
            _rows_result([]),
            _rows_result([]),
            _rows_result([]),
        ]
        r = client.get(BREAKDOWN_URL, params={**RANGE, "head": "wages"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["rate"]["value"] == 400.0 and data["summary"]["wages"] == 4000.0
        assert self.db.execute.call_args_list[1][0][1] == {"co_id": 2, "branch_id": 29, "on_date": date(2026, 10, 19)}

    def test_no_branch_skips_scope_check(self):
        self.db.execute.side_effect = [_rows_result([])] * 5
        r = client.get(BREAKDOWN_URL, params={"co_id": 2, "date_from": "2026-10-01", "date_to": "2026-10-19", "head": "stores"})
        assert r.status_code == 200, r.text
        assert self.db.execute.call_args_list[0][0][1]["branch_id"] is None

    def test_checks_wiring(self):
        std = [{"cost_param_id": 3, "co_id": 2, "branch_id": 29, "effective_date": date(2026, 10, 1), "param_code": "JUTE_PER_KG_CLOTH",
                "param_value": 1.1, "uom": "kg/kg", "product": None, "source": None, "remarks": None, "active": 1}]
        self.db.execute.side_effect = [
            _scope_ok(),
            _rows_result(std),
            _rows_result([_line(1, issue_date=date(2026, 10, 16), weight=1000, value=0, mr_rate=0)]),
            _rows_result([{"tran_date": date(2026, 10, 16), "weaving_quality_name": "SACK A", "production_mt": 1}]),
            _rows_result([{"lines": 2, "kg": 300, "value": 0}]),
        ]
        r = client.get(CHECKS_URL, params=RANGE)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["summary"]["std"] == 1.1 and data["summary"]["by_flag"]["NO_MR_RATE"] == 1
        assert data["rows"][0]["flags"] == ["NO_VALUE", "NO_MR_RATE"] and data["rows"][0]["issue_date"] == "2026-10-16"
        assert data["meta"]["thresholds"]["rate_outlier_min_lines"] == 3
        assert data["meta"]["thresholds"]["duplicate_window_seconds"] == 120
        assert data["summary"]["waste_ledger_lines"] == 2 and data["summary"]["waste_ledger_kg"] == 300.0
        assert data["notes"] == ["2 waste-ledger lines (0.3 t, booked ₹0 by design) are outside this check"]
        calls = self.db.execute.call_args_list
        assert "vw_jute_stock_outstanding" in str(calls[2][0][0]) and "jute_prod_weaving_daily" in str(calls[3][0][0])
        assert calls[3][0][1]["date_from"] == date(2026, 10, 1)
        assert "ji.jute_mr_li_id IS NULL" in str(calls[4][0][0]) and calls[4][0][1] == calls[2][0][1]

    def test_generic_error_is_500(self):
        self.db.execute.side_effect = [_scope_ok(), Exception("boom")]
        r = client.get(CHECKS_URL, params=RANGE)
        assert r.status_code == 500 and r.json()["detail"] == "boom"
