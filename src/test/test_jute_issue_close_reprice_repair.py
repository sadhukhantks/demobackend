"""close_line() / reprice() of dbqueries/repairs/jute_issue_close_reprice_20260905.py — no DB."""
import datetime
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "dbqueries" / "repairs" / "jute_issue_close_reprice_20260905.py"
_spec = importlib.util.spec_from_file_location("jute_issue_close_reprice", _SCRIPT)
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)


def _i(jid, qty, w, v=0, d=datetime.date(2026, 4, 1)):
    return {"jute_issue_id": jid, "quantity": qty, "weight": w, "issue_value": v, "issue_date": d}


def test_bale_and_kg_over_issue_cut_from_last_row():
    # sls line 45030: 71 bales / 9685 kg, 77 bales / 10511 kg drawn
    issues = [_i(1, 8, 1089), _i(2, 14, 1904), _i(3, 14, 1930), _i(4, 14, 1905),
              _i(5, 13, 1773), _i(6, 14, 1910)]
    assert fix.close_line(71, 9685, issues) == (6, 8.0, 1084.0)


def test_kg_only_over_issue():
    # sls line 45012: bales exact, 70 kg over
    issues = [_i(1, 70, 9980), _i(2, 0, 70), _i(3, 20, 2920)]   # 12970 kg vs 12900
    assert fix.close_line(90, 12900, issues) == (3, 20.0, 2850.0)


def test_not_over_issued_is_none():
    assert fix.close_line(10, 1000, [_i(1, 10, 1000)]) is None
    assert fix.close_line(10, 1000, [_i(1, 4, 400)]) is None
    assert fix.close_line(10, 1000, []) is None


def test_cut_larger_than_last_row_fails_loudly():
    with pytest.raises(ValueError):
        fix.close_line(10, 1000, [_i(1, 10, 1000), _i(2, 5, 500), _i(3, 1, 100)]) if False else \
            fix.close_line(1, 100, [_i(1, 10, 1000), _i(2, 1, 100)])


def test_reprice_at_production_rate():
    assert fix.reprice(320, 4159.98, 13000) == 41600.0      # stale /10 value
    assert fix.reprice(1773, 0, 12900) == 228717.0          # zero-valued row
    assert fix.reprice(582, 75078.0, 12900) is None          # already right
    assert fix.reprice(1910, 0, None) is None                # no production rate: leave
