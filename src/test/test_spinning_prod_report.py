"""Spinning Production Report — src/juteProduction/spinning_prod_report.py (pure pivot)."""

from src.juteProduction.spinning_prod_report import (
    EFF, FRAMES, PPF, PROD, build_spinning_pivot, frame_side,
)

D1, D2 = "2026-10-01", "2026-10-02"


def _r(d, mc, kg, eff, qtype="SKWP", quality="16-SKWP", eb="E1"):
    return {
        "doff_date": d, "machine_id": mc, "frame": f"F{mc}", "net_wt": kg, "eff_pct": eff,
        "quality_type": qtype, "quality_name": quality, "eb_label": eb, "worker_name": eb,
        "side": "Fine",
    }


ROWS = [
    _r(D1, 1, 100.0, 80.0),
    _r(D1, 1, 60.0, 0.0),          # same frame, no standard -> out of the eff mean
    _r(D1, 2, 140.0, 90.0),
    _r(D2, 1, 200.0, 70.0),
    _r(D1, 3, 50.0, 60.0, qtype="HSWP", quality="7.5-HSWP", eb="E2"),
]


def _pick(rows, label1, label2, line):
    return next(r for r in rows if (r["label1"], r["label2"], r["line"]) == (label1, label2, line))


def test_quality_lines_subtotals_and_grand_total():
    out = build_spinning_pivot(ROWS, "quality")
    assert [(r["label1"], r["label2"]) for r in out[::3]] == [
        ("HSWP", "7.5-HSWP"), ("HSWP", "Total"),
        ("SKWP", "16-SKWP"), ("SKWP", "Total"),
        ("Grand Total", None),
    ]
    prod = _pick(out, "SKWP", "16-SKWP", PROD)
    assert prod["days"] == {D1: 300.0, D2: 200.0}
    assert prod["total"] == 500.0 and prod["average"] == 250.0
    eff = _pick(out, "SKWP", "16-SKWP", EFF)
    assert eff["days"] == {D1: 85.0, D2: 70.0}       # zero eff excluded from D1's mean
    assert eff["total"] is None and eff["average"] == 80.0
    ppf = _pick(out, "SKWP", "16-SKWP", PPF)
    assert ppf["days"] == {D1: 150.0, D2: 200.0}     # 2 distinct frames on D1, 1 on D2
    assert ppf["average"] == round(500 / 3, 2)       # 500 kg over 3 frame-days
    grand = _pick(out, "Grand Total", None, PROD)
    assert grand["days"] == {D1: 350.0, D2: 200.0} and grand["total"] == 550.0


def test_total_report_is_one_group_with_frames_run_and_no_grand_total():
    out = build_spinning_pivot(ROWS, "total")
    assert [r["line"] for r in out] == [PROD, EFF, FRAMES, PPF]
    assert out[2]["days"] == {D1: 3, D2: 1} and out[2]["total"] == 4
    assert out[2]["average"] == 2.0


def test_worker_and_machine_show_prod_and_eff_only():
    worker = build_spinning_pivot(ROWS, "worker")
    assert {r["line"] for r in worker} == {PROD, EFF}
    assert _pick(worker, "E2", "E2", PROD)["total"] == 50.0
    machine = build_spinning_pivot(ROWS, "machine")
    assert _pick(machine, "F1", "Total", PROD)["total"] == 360.0


def test_frame_side_from_name():
    assert frame_side("SPG 4 1/4 - 12") == "Fine"
    assert frame_side("SPG 4 3/4 - 66") == "Coarse"
    assert frame_side("FRAME -3") == "Unassigned" and frame_side(None) == "Unassigned"
