"""Spreader Production Report — src/juteProduction/reports.py (pure pivot)."""

from datetime import date

from src.juteProduction.reports import build_spreader_datewise_rows

D1, D2, D3 = date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)


def _r(cal_date, early, rolls, kg, machine_id=1, item_id=10):
    return {
        "cal_date": cal_date, "early": early, "machine_id": machine_id,
        "machine_name": f"SP-{machine_id}", "item_id": item_id, "item_id_2": None,
        "ratio_1": 1, "ratio_2": None, "quality_label": f"Q{item_id}",
        "rolls": rolls, "kg": kg,
    }


def test_early_hours_book_to_previous_day_and_average_skips_idle_days():
    rows = build_spreader_datewise_rows(
        [
            _r(D1, 0, 10, 600.0),
            _r(D2, 1, 2, 120.0),   # 00-05 of D2 -> D1's C shift
            _r(D3, 0, 6, 360.0),
            _r(D1, 1, 99, 999.0),  # belongs to 31 Aug -> outside the period
            _r(D1, 0, 4, 200.0, machine_id=2, item_id=11),
            _r(D2, 0, 5, 250.0, item_id=12),  # second quality on SP-1
        ],
        "Production", D1, D3,
    )
    assert [(r["machine_name"], r["quality_label"], r["unit"]) for r in rows] == [
        ("SP-1", "Q10", "Rolls"), ("SP-1", "Q10", "Kg"),
        ("SP-1", "Q12", "Rolls"), ("SP-1", "Q12", "Kg"),
        ("SP-1", "Total", "Rolls"), ("SP-1", "Total", "Kg"),
        ("SP-2", "Q11", "Rolls"), ("SP-2", "Q11", "Kg"),
        ("SP-2", "Total", "Rolls"), ("SP-2", "Total", "Kg"),
        (None, "Grand Total", "Rolls"), (None, "Grand Total", "Kg"),
    ]
    rolls = rows[0]
    assert rolls["days"] == {"2026-09-01": 12, "2026-09-03": 6}
    assert rolls["total"] == 18 and rolls["average"] == 9  # 2 worked days, not 3
    assert rows[1]["total"] == 1080.0 and rows[1]["average"] == 540.0
    mc_total = rows[4]  # SP-1 across both qualities
    assert mc_total["days"] == {"2026-09-01": 12, "2026-09-02": 5, "2026-09-03": 6}
    assert mc_total["total"] == 23
    grand = rows[10]
    assert grand["days"] == {"2026-09-01": 16, "2026-09-02": 5, "2026-09-03": 6}
    assert grand["total"] == 27


def test_issue_has_no_machine_subtotal():
    row = {**_r(D1, 0, 3, 150.0), "machine_id": None, "machine_name": None}
    rows = build_spreader_datewise_rows([row], "Issue", D1, D3)
    assert [r["quality_label"] for r in rows] == ["Q10", "Q10", "Grand Total", "Grand Total"]


def test_no_rows_no_total():
    assert build_spreader_datewise_rows([], "Issue", D1, D3) == []
