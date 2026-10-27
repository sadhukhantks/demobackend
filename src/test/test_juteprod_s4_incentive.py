"""S4 Incentive Report — src/juteProduction/s4_incentive.py (pure aggregation)."""

from src.juteProduction.s4_incentive import (
    _round0,
    build_incentive_rows,
    helper_entries,
    weaver_entries,
)

SLAB = {60: 10.0, 70: 40.0, 75: 70.0}
NAMES = {1: {"eb_no": "A1", "worker_name": "Ram"}, 2: {"eb_no": "B2", "worker_name": "Shyam"}}


def _r(eb, d, prod, std, machine=10, spell=1):
    return {"eb_id": eb, "tran_date": d, "production_yds": prod, "std_prod_yds": std,
            "machine_id": machine, "spell_id": spell}


def test_round_half_up():
    assert _round0(69.5) == 70 and _round0(70.5) == 71 and _round0(60.49) == 60


def test_weaver_day_eff_is_production_over_standard_and_slab_exact_match():
    rows = build_incentive_rows(
        weaver_entries([
            # day 1: (80 + 60) / 200 = 70 -> 40
            _r(1, "2026-09-01", 80, 100), _r(1, "2026-09-01", 60, 100),
            # day 2: 75 -> 70
            _r(1, "2026-09-02", 150, 200),
            # worked, nothing produced: counted as a day at efficiency 0
            _r(1, "2026-09-03", 0, 100),
            # worker 2: 101 is outside the slab -> no incentive
            _r(2, "2026-09-01", 101, 100),
            # no worker -> dropped
            _r(None, "2026-09-01", 75, 100),
            # production without a standard -> ignored
            _r(1, "2026-09-02", 900, 0),
        ]),
        SLAB,
        NAMES,
    )
    assert [w["eb_no"] for w in rows] == ["A1", "B2"]  # worst avg eff first
    a = rows[0]
    assert a["days"] == {"2026-09-01": 70, "2026-09-02": 75, "2026-09-03": 0}
    assert a["total_days"] == 3 and a["inc_days"] == 2
    assert a["total_inc_amt"] == 110.0
    assert a["avg_eff"] == 58  # 290 / 500
    b = rows[1]
    assert b["total_inc_amt"] == 0.0 and b["inc_days"] == 0 and b["avg_eff"] == 101


def test_helper_gets_the_whole_line_for_the_spell_attended():
    loom_line = {10: 900, 11: 900, 12: 901}
    slice_rows = [
        _r(1, "2026-09-01", 60, 100, machine=10),
        _r(2, "2026-09-01", 80, 100, machine=11),
        _r(1, "2026-09-01", 10, 100, machine=12),           # other line
        _r(1, "2026-09-01", 10, 100, machine=10, spell=2),  # other spell
        _r(3, "2026-09-01", 500, 0, machine=11),            # no standard -> ignored
    ]
    att = [
        {"eb_id": 7, "attendance_date": "2026-09-01", "spell_id": 1, "mc_id": 900},
        {"eb_id": 7, "attendance_date": "2026-09-02", "spell_id": 1, "mc_id": 900},  # no production
    ]
    rows = build_incentive_rows(helper_entries(slice_rows, att, loom_line), SLAB, {})
    assert rows[0]["days"] == {"2026-09-01": 70, "2026-09-02": 0}
    assert rows[0]["total_days"] == 2 and rows[0]["total_inc_amt"] == 40.0
