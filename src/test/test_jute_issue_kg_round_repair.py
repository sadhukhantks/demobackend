"""plan_line() of dbqueries/repairs/jute_issue_kg_round_20260905.py — no DB."""
import datetime
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "dbqueries" / "repairs" / "jute_issue_kg_round_20260905.py"
_spec = importlib.util.spec_from_file_location("jute_issue_kg_round", _SCRIPT)
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)

D = datetime.date


def _i(jid, qty, w, v, date=D(2026, 8, 20), by="system"):
    return {"jute_issue_id": jid, "quantity": qty, "weight": w, "issue_value": v,
            "issue_date": date, "updated_by": by}


def test_fully_issued_balanced_line_rounds_and_closes_exactly():
    # 3 bales / 1000 kg, issued 333.33 x 3 (sum 999.99 ~ balanced)
    issues = [_i(1, 1, 333.33, 100.0), _i(2, 1, 333.33, 100.0), _i(3, 1, 333.34, 100.0)]
    got = fix.plan_line(3, 1000, issues)
    assert got == [(1, 333.0, 99.9), (2, 333.0, 99.9), (3, 334.0, 100.2)]
    assert sum(w for _, w, _ in got) == 1000


def test_partial_line_only_rounds_each_row():
    issues = [_i(1, 4, 581.68, 75036.72), _i(2, 5, 727.1, 93795.9)]
    got = fix.plan_line(31, 4508, issues)
    assert [(j, w) for j, w, _ in got] == [(1, 582.0), (2, 727.0)]
    assert got[0][2] == round(582 * 75036.72 / 581.68, 2)


def test_bale_over_issue_is_not_rebalanced():
    # 71 bales / 9685 kg but 77 bales issued: rows round, nothing absorbs the gap
    issues = [_i(1, 70, 9548.5, 0), _i(2, 7, 954.9, 0)]
    got = fix.plan_line(71, 9685, issues)
    assert got == [(1, 9549.0, 0.0), (2, 955.0, 0.0)]


def test_legacy_fractional_line_is_left_alone():
    assert fix.plan_line(54, 74.67, [_i(1, 54, 74.67, 0)]) == []


def test_stockadj_and_pre_cutoff_rows_are_never_touched():
    issues = [
        _i(1, 1, 333.33, 0, date=D(2026, 3, 30)),
        _i(2, 1, 333.33, 0, by="STOCKADJ-20260331"),
        _i(3, 1, 333.335, 0),  # line balanced within 0.01 -> this row closes it
    ]
    got = fix.plan_line(3, 1000, issues)
    assert got == [(3, 333.34, 0.0)]   # 1000 - 333.33 - 333.33, untouched rows kept


def test_whole_kg_rows_are_a_no_op():
    assert fix.plan_line(98, 14450, [_i(1, 98, 14450.0, 500.0)]) == []


def test_float_noise_snaps_to_the_integer():
    got = fix.plan_line(200, 30000, [_i(1, 13, 1971.9999389648438, 0)])
    assert got == [(1, 1972.0, 0.0)]
