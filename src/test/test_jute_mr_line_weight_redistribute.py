"""jute_mr_li.actual_weight must follow the header net once gate-out weighs the
lorry — QC saved before gate-out leaves every line at 0 kg (sls br29, 2026-09-05)."""
from unittest.mock import MagicMock

from src.juteProcurement.totals import plan_mr_line_weights, redistribute_mr_line_weights


def _line(li_id, qty, allowable=None, moisture=None):
    return {"jute_mr_li_id": li_id, "actual_qty": qty,
            "allowable_moisture": allowable, "actual_moisture": moisture}


def test_split_is_proportional_and_sums_to_header():
    plan = plan_mr_line_weights(11130, [_line(1, 20), _line(2, 55)])
    assert [p["actual_weight"] for p in plan] == [2968, 8162]
    assert sum(p["actual_weight"] for p in plan) == 11130


def test_remainder_kg_lands_on_highest_fraction_never_lost():
    plan = plan_mr_line_weights(100, [_line(1, 1), _line(2, 1), _line(3, 1)])
    assert sorted(p["actual_weight"] for p in plan) == [33, 33, 34]


def test_zero_qty_line_gets_nothing():
    plan = plan_mr_line_weights(100, [_line(1, 0), _line(2, 4)])
    assert [p["actual_weight"] for p in plan] == [0, 100]


def test_moisture_over_allowable_deducts_like_mr_save():
    (p,) = plan_mr_line_weights(8162, [_line(1, 55, allowable=12, moisture="14")])
    assert p["shortage_kgs"] == 163          # round(8162 * 2%)
    assert p["accepted_weight"] == 7999      # 8162 - 163


def test_redistribute_writes_one_update_per_line_and_skips_zero_header():
    db = MagicMock()
    header = MagicMock(); header._mapping = {"actual_weight": 11130}
    l1 = MagicMock(); l1._mapping = _line(45581, 20)
    l2 = MagicMock(); l2._mapping = _line(45582, 55)
    db.execute.return_value.fetchone.return_value = header
    db.execute.return_value.fetchall.return_value = [l1, l2]

    n = redistribute_mr_line_weights(db, 28173)

    assert n == 2
    updates = [c.args[1] for c in db.execute.call_args_list if "UPDATE jute_mr_li" in str(c.args[0])]
    assert [(u["jute_mr_li_id"], u["actual_weight"]) for u in updates] == [(45581, 2968), (45582, 8162)]

    db.reset_mock()
    header._mapping = {"actual_weight": 0}
    assert redistribute_mr_line_weights(db, 28173) == 0
    assert not [c for c in db.execute.call_args_list if "UPDATE" in str(c.args[0])]
