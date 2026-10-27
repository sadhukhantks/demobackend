"""Unit tests for the pure planning helpers of the STOCKFIX-20260831 repair script.

No DB: only allocate_cuts / dust_cut are imported (the script's DB layer is not
touched at import time).
"""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[2]
           / "dbqueries" / "repairs" / "stock_negative_dust_fix_20260831.py")
_spec = importlib.util.spec_from_file_location("stock_negative_dust_fix", _SCRIPT)
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)


# ---------------------------------------------------------------- allocate_cuts
def test_cut_single_line_partial():
    assert fix.allocate_cuts(20.0, [(1, 24.5)]) == [(1, 4.5, -20.0)]


def test_cut_spans_multiple_lines_newest_first():
    cuts = fix.allocate_cuts(15.0, [(3, 5.0), (2, 5.0), (1, 100.0)])
    assert cuts == [(3, 0.0, -5.0), (2, 0.0, -5.0), (1, 95.0, -5.0)]


def test_cut_stops_once_satisfied():
    cuts = fix.allocate_cuts(2.0, [(3, 10.0), (2, 10.0)])
    assert cuts == [(3, 8.0, -2.0)]           # older line untouched


def test_line_can_land_exactly_on_zero():
    cuts = fix.allocate_cuts(8.0, [(9, 8.0)])
    assert cuts == [(9, 0.0, -8.0)]
    assert cuts[0][1] == 0.0                  # row kept at zero, never negative


def test_zero_qty_lines_are_skipped():
    cuts = fix.allocate_cuts(3.0, [(5, 0.0), (4, 3.0)])
    assert cuts == [(4, 0.0, -3.0)]


def test_shortfall_exceeding_all_lines_raises():
    with pytest.raises(ValueError, match="exceeds consumable"):
        fix.allocate_cuts(30.0, [(1, 10.0), (2, 5.0)])


def test_non_positive_shortfall_raises():
    with pytest.raises(ValueError):
        fix.allocate_cuts(0.0, [(1, 10.0)])


def test_new_qty_never_negative():
    for cut in fix.allocate_cuts(12.34, [(1, 5.0), (2, 5.0), (3, 5.0)]):
        assert cut[1] >= 0.0


# -------------------------------------------------------------------- dust_cut
def test_dust_negative_balance_trims_newest_big_enough_line():
    li_id, new_qty, delta = fix.dust_cut(-0.0001953125, [(7, 0.0), (6, 5364.9501953125)])
    assert li_id == 6
    assert new_qty == pytest.approx(5364.95, abs=1e-12)
    assert delta == -0.0001953125


def test_dust_positive_balance_bumps_qty_up():
    li_id, new_qty, delta = fix.dust_cut(0.0002, [(4, 10.0)])
    assert (li_id, delta) == (4, 0.0002)
    assert new_qty == pytest.approx(10.0002, abs=1e-12)


def test_dust_skips_lines_smaller_than_the_gap():
    assert fix.dust_cut(-0.0004, [(3, 0.0001), (2, 1.0)]) == (2, 0.9996, -0.0004)


def test_dust_returns_none_when_no_line_is_big_enough():
    assert fix.dust_cut(-0.0004, [(3, 0.0001)]) is None
    assert fix.dust_cut(-0.0004, []) is None
