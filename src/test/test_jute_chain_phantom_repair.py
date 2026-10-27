"""plan_child pairs root/child lines positionally and refuses anything unsafe."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "phantom", Path(__file__).resolve().parents[2] / "dbqueries" / "repairs" / "jute_chain_phantom_lines_20260915.py")
phantom = importlib.util.module_from_spec(spec); spec.loader.exec_module(phantom)


def _li(i, active=1, wt=0, amt=0, issues=0, lots=0, inv=0):
    return {"jute_mr_li_id": i, "active": active, "accepted_weight": wt, "total_price": amt,
            "issues": issues, "lots": lots, "inv_lines": inv}


def test_pairs_by_position_and_skips_live_lines():
    root = [_li(1, 1, 100, 5000), _li(2, 0), _li(3, 0)]
    child = [_li(11, 1, 100, 4900), _li(12), _li(13)]
    rows, why = phantom.plan_child(99, 9, root, child)
    assert why is None and [r["child_li_id"] for r in rows] == [12, 13]


def test_refuses_count_mismatch_and_referenced_or_weighted_clones():
    assert phantom.plan_child(1, 1, [_li(1, 0)], [])[1].startswith("line count")
    assert "weight" in phantom.plan_child(1, 1, [_li(1, 0)], [_li(2, 1, 5)])[1]
    assert "referenced" in phantom.plan_child(1, 1, [_li(1, 0)], [_li(2, issues=1)])[1]


def test_rerun_safe_when_clone_already_inactive():
    assert phantom.plan_child(1, 1, [_li(1, 0)], [_li(2, 0)]) == ([], None)
