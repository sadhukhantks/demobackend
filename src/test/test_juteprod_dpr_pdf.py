"""Tests for the server-side Daily Production Report PDF (juteProduction/dpr_pdf.py).

No DB: build_dpr_data and the company/branch name lookups are monkeypatched.
"""

from datetime import date

import pytest

from src.juteProduction import dpr_pdf
from src.juteProduction.dpr_reports import UNAVAILABLE_SECTIONS


def _power_keyed() -> dict:
    """build_power_section output for a keyed day (ITM-004)."""
    return {
        "keyed": True,
        "day": {
            "kwh": 18420.0,
            "grid_kwh": 17520.0,
            "dg_kwh": 900.0,
            "dg_hours": 2.5,
            "dg_litres": 310.0,
            "kwh_per_t_cloth": 622.3,
            "kwh_per_t_yarn": 608.44,
            "dg_share_pct": 4.89,
        },
        "mtd": {
            "days": 22,
            "kwh": 400000.0,
            "grid_kwh": 388000.0,
            "dg_kwh": 12000.0,
            "dg_hours": 40.0,
            "dg_litres": 5000.0,
            "kwh_per_t_cloth": 623.57,
            "kwh_per_t_yarn": 619.23,
            "dg_share_pct": 3.0,
        },
    }


def _power_not_keyed(mtd_days: int) -> dict:
    none_block = {k: None for k in _power_keyed()["day"]}
    mtd = dict(_power_keyed()["mtd"]) if mtd_days else {**none_block}
    mtd["days"] = mtd_days
    return {"keyed": False, "day": dict(none_block), "mtd": mtd}


def _full_data() -> dict:
    weights = {
        "opening_weight": 99848.0,
        "receipt_weight": 19910.0,
        "issue_weight": 9380.0,
        "sold_weight": 0.0,
        "closing_weight": 110378.0,
        "mtd_receipt_weight": 407700.0,
        "mtd_issue_weight": 323372.0,
    }
    return {
        "report_date": "2026-09-30",
        "date_from": "2026-09-01",
        "kpi": {
            "spinning_mt": 30.274,
            "spinning_mtd_mt": 645.966,
            "frame_utilization_pct": 50.0,
            "spinning_eff_pct": 74.37,
            "weaving_mt": 29.598,
            "weaving_mtd_mt": 641.47,
            "loom_utilization_pct": 48.8,
            "weaving_eff_pct": 61.24,
            "winding_kg": 28292.0,
            "winding_mtd_kg": 625839.6,
            "twisting_kg": 667.0,
            "jbo_pct": 15.0,
            "total_hands": 850.44,
            "shifts_worked": 3,
            "shifts_worked_mtd": 83,
            "hands_per_mt": 28.09,
            "hands_per_mt_yarn": 11.7,
            "hands_per_mt_cloth": 16.39,
            "hands_per_mt_mtd": 28.3,
            "hands_stage_basis": True,
            "hands_per_mt_spinning_basis": 28.09,
            "jute_closing_weight": 247469.0,
            "jute_days_stock": 8.2,
            "jute_cover_basis_kg": 30273.7,
            "jute_cover_basis_date": "2026-09-30",
        },
        "spinning": {
            "shifts": [
                {
                    "shift": s,
                    "production_kg": 11026.6,
                    "production_mt": 11.027,
                    "frame_runs": 31,
                    "kg_per_frame": 355.7,
                    "utilization_pct": 53.45,
                    "eff_pct": 76.2,
                }
                for s in ("A", "B", "C")
            ],
            "total": {
                "production_kg": 30273.7,
                "production_mt": 30.274,
                "frame_runs": 87,
                "kg_per_frame": 348.0,
                "utilization_pct": 50.0,
                "eff_pct": 74.37,
            },
            "mtd": {
                "production_kg": 645966.24,
                "production_mt": 645.966,
                "frame_runs": 1935,
                "kg_per_frame": 333.8,
            },
            "total_frames": 58,
            "quality": [
                {
                    "item_id": 1,
                    "item_code": "13-SKWP",
                    "item_name": "13-SKWP <&> ↔ test",
                    "act_count": 13.03,
                    "std_count": 13.0,
                    "production_kg": 12875.4,
                    "frame_shifts": 35,
                    "kg_per_frame_shift": 367.9,
                    "target_per_frame_shift": 0.0,
                    "winding_kg": 11861.999,
                    "eff_pct": None,
                }
            ],
            "standards_missing": ["13-SKWP"],
            "avg_count": {
                "by_frames": 13.64,
                "frame_shifts": 87,
                "by_production": 14.17,
                "production_kg": 30273.7,
                "by_std_production": None,
                "std_production_kg": 0.0,
                "std_qualities": 0,
                "qualities": 1,
                "count_missing": ["X"],
            },
        },
        "winding": {
            "qualities": [
                {
                    "item_id": 1,
                    "item_code": "13-SKWP",
                    "item_name": "13-SKWP",
                    "production_kg": 11862.0,
                    "winders": 36,
                    "kg_per_winder": 329.5,
                    "mtd_production_kg": 257398.0,
                    "mtd_kg_per_winder_shift": 339.1,
                    "is_twisted": 0,
                },
                {
                    "item_id": None,
                    "item_code": None,
                    "item_name": None,
                    "production_kg": 667.0,
                    "winders": 1,
                    "kg_per_winder": 667.0,
                    "mtd_production_kg": 17877.0,
                    "mtd_kg_per_winder_shift": 638.5,
                    "is_twisted": 1,
                },
            ],
            "total": {
                "production_kg": 28292.0,
                "winders": 87,
                "kg_per_winder": 325.2,
                "mtd_production_kg": 625839.6,
                "twisting_kg": 667.0,
                "twisting": {
                    "production_kg": 667.0,
                    "winders": 1,
                    "kg_per_winder": 667.0,
                    "mtd_production_kg": 17877.0,
                },
                "gross": {
                    "production_kg": 28959.0,
                    "winders": 88,
                    "kg_per_winder": 329.1,
                    "mtd_production_kg": 643716.6,
                },
            },
            "twisting_available": True,
        },
        "weaving": {
            "qualities": [
                {
                    "weaving_quality_id": 145,
                    "quality_code": "1445",
                    "quality_name": "HESSIAN 38\" 11x 10 7.714 Ozs",
                    "shift_A_mt": 0.176,
                    "shift_B_mt": 0.142,
                    "shift_C_mt": 0.0,
                    "production_mt": 0.318,
                    "looms_run": 8,
                    "kg_per_loom": 39.7,
                    "eff_pct": 43.3,
                    "mtd_production_mt": 11.399,
                }
            ],
            "shift_totals": {"A": 10.724, "B": 10.441, "C": 8.432},
            "total": {
                "production_mt": 29.598,
                "loom_runs": 163,
                "kg_per_loom": 181.6,
                "eff_pct": 61.24,
                "utilization_pct": 48.8,
                "mtd_production_mt": 641.47,
            },
            "total_looms": 334,
            "standards_missing": [],
        },
        "finishing": {
            "processes": [
                {
                    "process": "balepress",
                    "uom": "bale",
                    "day_qty": 56.0,
                    "day_wt_kg": 15536.0,
                    "mtd_qty": 1430.0,
                    "mtd_wt_kg": 404897.0,
                }
            ]
        },
        "emulsion": {
            "day": {
                "jbo_ltr": 150.0,
                "eco_fin_ltr": 0.0,
                "oil_used_ltr": 150.0,
                "tank_capacity_ltr": 1000.0,
                "oil_pct": 15.0,
                "std_low": 16.0,
                "std_high": 17.0,
                "status": "LOW",
            },
            "mtd": {"jbo_ltr": 3030.0, "eco_fin_ltr": 0.0, "avg_oil_pct": 14.29, "days": 21},
        },
        "yarn_heavy_light": [
            {
                "item_id": 1,
                "item_code": "13-SKWP",
                "item_name": "13-SKWP",
                "std_count": 13.0,
                "obs_count": 13.03,
                "corr_count": 13.22,
                "day_tests": 15,
                "hy_lt_pct": 1.69,
                "remark": "HEAVY",
                "mtd_corr_count": 13.41,
            },
            {
                "item_id": 2,
                "item_code": "16-HSWP",
                "item_name": None,
                "std_count": 16.0,
                "obs_count": None,
                "corr_count": None,
                "day_tests": 0,
                "hy_lt_pct": None,
                "remark": None,
                "mtd_corr_count": 14.15,
            },
        ],
        "jute": {
            "groups": [
                {"item_group_name": "JUTE > DESI", "is_waste": False, **weights},
                {"item_group_name": "Jute Waste", "is_waste": True, **weights},
            ],
            "totals": weights,
            "jute_totals": weights,
            "waste_totals": weights,
            "days_stock": 8.2,
            "days_stock_raw_jute": 5.0,
            "days_stock_waste": 3.2,
            "raw_jute_closing_weight": 151583.0,
            "waste_closing_weight": 95886.0,
            "cover_basis": {
                "mode": "average",
                "date": "2026-09-30",
                "date_from": "2026-09-23",
                "date_to": "2026-09-30",
                "days": 7,
                "production_kg": 30273.7,
                "is_report_date": False,
            },
        },
        "hands": {
            "shifts": [
                {
                    "shift": "A",
                    "day_hours": 3464.5,
                    "day_hands": 433.06,
                    "mtd_hours": 86572.0,
                    "mtd_hands": 10821.5,
                }
            ],
            "total": {
                "day_hours": 6803.5,
                "day_hands": 850.44,
                "mtd_hours": 154223.33,
                "mtd_hands": 19277.92,
            },
            "hands_per_mt": 28.09,
            "mtd_hands_per_mt": 28.3,
            "basis_label": "per MT of cloth: yarn-stage hands ÷ spinning MT + "
            "cloth-stage hands ÷ weaving MT",
            "stages": {
                "available": True,
                "yarn": {
                    "label": "Yarn stage (jute → spinning)",
                    "basis_label": "spinning output",
                    "depts": ["BATCHING(EJM)", "JUTE(EJM)", "PREPARING(EJM)", "SPINNING(EJM)"],
                    "direct_day_hands": 296.43, "direct_mtd_hands": 6500.0,
                    "common_day_hands": 57.8, "common_mtd_hands": 1300.0,
                    "day_hands": 354.23, "mtd_hands": 7800.0,
                    "day_mt": 30.274, "mtd_mt": 645.966,
                    "hands_per_mt": 11.7, "mtd_hands_per_mt": 12.07,
                },
                "cloth": {
                    "label": "Cloth stage (winding → finishing)",
                    "basis_label": "weaving output",
                    "depts": ["BEAMING(EJM)", "FINISHING(EJM)", "SPOOL WINDING(EJM)", "WEAVING(EJM)"],
                    "direct_day_hands": 414.0, "direct_mtd_hands": 9000.0,
                    "common_day_hands": 82.21, "common_mtd_hands": 1977.92,
                    "day_hands": 496.21, "mtd_hands": 10977.92,
                    "day_mt": 29.598, "mtd_mt": 641.47,
                    "hands_per_mt": 16.39, "mtd_hands_per_mt": 16.23,
                },
                "common": {
                    "label": "Common (spread pro-rata)",
                    "depts": ["ELECTRICAL(EJM)", "MILL MECHANICAL(EJM)"],
                    "day_hands": 140.01, "mtd_hands": 3277.92,
                    "yarn_share_pct": 41.3, "mtd_yarn_share_pct": 41.9,
                },
                "total": {
                    "day_hands": 850.44, "mtd_hands": 19277.92,
                    "hands_per_mt": 28.09, "mtd_hands_per_mt": 28.3,
                },
            },
            "spinning_basis": {
                "label": "All hands ÷ spinning output (previous basis)",
                "day_hands": 850.44, "mtd_hands": 19277.92,
                "day_mt": 30.274, "mtd_mt": 645.966,
                "hands_per_mt": 28.09, "mtd_hands_per_mt": 29.84,
            },
            "yarn_balance": {
                "purchased_available": True,
                "day": {"spun_mt": 30.274, "purchased_issued_mt": 2.864,
                        "woven_mt": 29.598, "surplus_mt": 3.54},
                "mtd": {"spun_mt": 645.966, "purchased_issued_mt": 42.71,
                        "woven_mt": 641.47, "surplus_mt": 47.206},
            },
        },
        "shifts_worked": {
            "day": 3,
            "mtd": 83,
            "day_shifts": ["A", "B", "C"],
            "sections": [
                {"section": "Spinning", "day": 3, "mtd": 82, "day_shifts": ["A", "B", "C"]},
                {"section": "Finishing", "day": 0, "mtd": 49, "day_shifts": []},
            ],
        },
        "machine_run": [
            {
                "section": "Spinning",
                "basis": "target",
                "avg_per_machine": 0.0,
                "avg_act_per_machine": 348.0,
                "total_prod_kg": 30273.7,
                "should_run": 0.0,
                "act_run": 87,
                "excess_machines": None,
            }
        ],
        "power": _power_keyed(),
        "unavailable": UNAVAILABLE_SECTIONS,
    }


def _sparse_data() -> dict:
    """Company-wide / holiday shape: branch-keyed sections None, lists empty."""
    return {
        "report_date": "2026-09-30",
        "date_from": "2026-09-01",
        "kpi": {
            "spinning_mt": 0.0,
            "winding_kg": None,
            "winding_mtd_kg": None,
            "twisting_kg": None,
            "jbo_pct": None,
            "jute_closing_weight": None,
            "jute_days_stock": None,
            "frame_utilization_pct": None,
        },
        "spinning": {
            "shifts": [],
            "total": {},
            "mtd": {},
            "total_frames": 0,
            "quality": [],
            "standards_missing": [],
            "avg_count": None,
        },
        "winding": None,
        "weaving": {"qualities": [], "shift_totals": {}, "total": {}, "standards_missing": []},
        "finishing": {"processes": []},
        "emulsion": None,
        "yarn_heavy_light": [],
        "jute": None,
        "hands": {"shifts": [], "total": {}, "hands_per_mt": None, "mtd_hands_per_mt": None,
                  "stages": None, "spinning_basis": None, "yarn_balance": None},
        "shifts_worked": {"day": 0, "mtd": 0, "day_shifts": [], "sections": []},
        "machine_run": [],
        "power": None,
        "unavailable": [],
    }


@pytest.fixture
def patched(monkeypatch):
    calls = {}

    def fake_build(db, co_id, branch_id, report_d):
        calls["args"] = (co_id, branch_id, report_d)
        return calls["data"]

    monkeypatch.setattr(dpr_pdf, "build_dpr_data", fake_build)
    monkeypatch.setattr(dpr_pdf, "_fetch_company_name", lambda db, co_id: "Test Jute Co. Ltd.")
    monkeypatch.setattr(dpr_pdf, "_fetch_branch_name", lambda db, co_id, branch_id: "Main Factory / Unit-1")
    return calls


class TestBuildDprPdf:
    def test_full_report_is_pdf_with_filename(self, patched):
        patched["data"] = _full_data()
        pdf, filename = dpr_pdf.build_dpr_pdf(object(), 2, 29, date(2026, 9, 30))
        assert isinstance(pdf, bytes)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 2000
        assert filename == "DPR_Main_Factory_Unit_1_2026-09-30.pdf"
        assert patched["args"] == (2, 29, date(2026, 9, 30))

    def test_mostly_empty_sections_do_not_crash(self, patched):
        patched["data"] = _sparse_data()
        pdf, filename = dpr_pdf.build_dpr_pdf(object(), 2, 29, date(2026, 9, 30))
        assert pdf.startswith(b"%PDF")
        assert filename.startswith("DPR_") and filename.endswith("_2026-09-30.pdf")

    def test_missing_branch_name_falls_back_to_id(self, patched, monkeypatch):
        patched["data"] = _sparse_data()
        monkeypatch.setattr(dpr_pdf, "_fetch_branch_name", lambda db, co_id, branch_id: None)
        monkeypatch.setattr(dpr_pdf, "_fetch_company_name", lambda db, co_id: None)
        pdf, filename = dpr_pdf.build_dpr_pdf(object(), 2, 29, date(2026, 9, 30))
        assert pdf.startswith(b"%PDF")
        assert filename == "DPR_branch29_2026-09-30.pdf"

    def test_render_tolerates_empty_and_none_payload(self):
        assert dpr_pdf.render_dpr_pdf({}, None, None).startswith(b"%PDF")
        data = {k: None for k in _full_data()}
        assert dpr_pdf.render_dpr_pdf(data, "Co", "Br").startswith(b"%PDF")


class TestFmt:
    def test_indian_grouping_and_digits(self):
        assert dpr_pdf.fmt(247469, 0) == "2,47,469"
        assert dpr_pdf.fmt(12345678.5, 1) == "1,23,45,678.5"
        assert dpr_pdf.fmt(30.274, 3) == "30.274"
        assert dpr_pdf.fmt(999, 0) == "999"
        assert dpr_pdf.fmt(-7.19) == "-7.19"
        assert dpr_pdf.fmt(-0.001) == "0.00"

    def test_missing_values_render_dash(self):
        assert dpr_pdf.fmt(None) == dpr_pdf.DASH
        assert dpr_pdf.fmt(float("nan")) == dpr_pdf.DASH
        assert dpr_pdf.fmt("abc") == dpr_pdf.DASH


# TKT-000201: the Average Count block shows the SQC actual count beside the std count.
_ACT_AVG = {
    "by_frames": 13.64, "frame_shifts": 87, "by_production": 14.12,
    "production_kg": 30160.35, "by_std_production": 13.95, "std_production_kg": 41144.0,
    "std_qualities": 6, "qualities": 6, "count_missing": [],
    "act_by_frames": 13.33, "act_by_production": 13.69, "act_by_std_production": 13.56,
    "act_measured_kg": 30160.35, "act_fallback": [],
}


class TestAvgCountSqcActual:
    def test_kpi_headlines_sqc_actual_with_std_in_caption(self):
        value, sub = dpr_pdf.avg_count_kpi(_ACT_AVG)
        assert value == "13.33 / 13.69 / 13.56"
        assert sub == "SQC actual (frames / prod / std) · std count 13.64 / 14.12 / 13.95"

    def test_kpi_counts_qualities_without_sqc(self):
        avg = {**_ACT_AVG, "act_measured_kg": 29000.0, "act_fallback": ["9-HSWP", "X"]}
        assert dpr_pdf.avg_count_kpi(avg)[1].endswith(" · 2 w/o SQC")

    def test_kpi_never_labels_std_as_actual_when_no_sqc_entered(self):
        avg = {**_ACT_AVG, "act_measured_kg": 0.0, "act_fallback": ["A", "B"]}
        value, sub = dpr_pdf.avg_count_kpi(avg)
        assert value == "13.64 / 14.12 / 13.95"
        assert sub == "std count · frames / prod / std · no SQC count entered"

    def test_kpi_old_payload_unchanged(self):
        old = {k: v for k, v in _ACT_AVG.items() if not k.startswith("act_")}
        assert dpr_pdf.avg_count_kpi(old) == ("13.64 / 14.12 / 13.95", "frames / production / std")
        old["by_std_production"] = None
        assert dpr_pdf.avg_count_kpi(old)[1] == "frames / production / std (std not entered)"

    def test_note_names_sources_and_fallback(self):
        note = dpr_pdf.avg_count_note(
            {**_ACT_AVG, "act_measured_kg": 29000.0, "act_fallback": ["9-HSWP"]}
        )
        assert "count on the yarn master" in note
        assert "entered in Spinning SQC" in note
        assert "SQC count covers 29,000 of 30,160 kg." in note
        assert "std count stands in the actual column: 9-HSWP." in note

    def test_note_when_no_sqc_entered(self):
        note = dpr_pdf.avg_count_note({**_ACT_AVG, "act_measured_kg": 0.0, "act_fallback": ["A"]})
        assert "No SQC count was entered for the day" in note
        assert "stands in the actual column" not in note

    def test_note_says_moisture_corrected_on_the_corrected_basis(self):
        # TKT-000201 owner ruling: corrected count from Oct-2026, observed before.
        note = dpr_pdf.avg_count_note({**_ACT_AVG, "count_basis": "corrected"})
        assert "day's average moisture-corrected count entered in Spinning SQC" in note
        assert "moisture-corrected" not in dpr_pdf.avg_count_note(_ACT_AVG)

    def test_std_basis_blank_reason(self):
        avg = {**_ACT_AVG, "by_std_production": None, "act_by_std_production": None}
        assert dpr_pdf.avg_count_std_basis(avg) == (
            "no std production (std TPI / efficiency or SQC count missing)"
        )

    def test_report_renders_with_actual_count(self, patched):
        data = _full_data()
        data["spinning"]["avg_count"] = dict(_ACT_AVG)
        patched["data"] = data
        pdf, _ = dpr_pdf.build_dpr_pdf(object(), 2, 29, date(2026, 10, 8))
        assert pdf.startswith(b"%PDF")


def _texts(flowables) -> str:
    """Every Paragraph's text plus every table cell, flattened."""
    from reportlab.platypus import Paragraph, Table

    out = []
    for f in flowables:
        if isinstance(f, Paragraph):
            out.append(f.text)
        elif isinstance(f, Table):
            for row in f._cellvalues:
                for cell in row:
                    out.append(cell.text if isinstance(cell, Paragraph) else str(cell))
    return " | ".join(out)


class TestPowerSection:
    """ITM-004 — the DPR PDF's Power line."""

    def test_none_payload_says_not_keyed(self):
        flow = dpr_pdf._power_section(None, 400.0)
        text = _texts(flow)
        assert "Power" in text
        assert "not keyed" in text
        assert "MTD figures" not in text
        from reportlab.platypus import Table

        assert not any(isinstance(f, Table) for f in flow)

    def test_not_keyed_without_mtd_days(self):
        text = _texts(dpr_pdf._power_section(_power_not_keyed(0), 400.0))
        assert "Power readings not keyed for this date" in text
        assert "MTD figures" not in text

    def test_not_keyed_with_mtd_days_shows_mtd_row(self):
        from reportlab.platypus import Table

        flow = dpr_pdf._power_section(_power_not_keyed(22), 400.0)
        text = _texts(flow)
        assert "not keyed" in text and "MTD figures shown from the days that were keyed" in text
        assert any(isinstance(f, Table) for f in flow)
        assert "4,00,000" in text  # MTD kWh, Indian grouping
        assert "22" in text

    def test_keyed_renders_day_and_mtd_table(self):
        from reportlab.platypus import Table

        flow = dpr_pdf._power_section(_power_keyed(), 400.0)
        text = _texts(flow)
        assert any(isinstance(f, Table) for f in flow)
        assert "not keyed" not in text
        for label in ("Total kWh", "Grid kWh", "DG kWh", "DG hours", "DG litres",
                      "kWh per t cloth", "kWh per t yarn", "DG share %"):
            assert label in text
        assert "18,420" in text and "17,520" in text and "622" in text
        assert "MTD over 22 keyed day(s)" in text

    def test_render_with_power_none_is_still_pdf(self):
        data = _full_data()
        data["power"] = None
        assert dpr_pdf.render_dpr_pdf(data, "Co", "Br").startswith(b"%PDF")

    def test_render_with_power_not_keyed_is_still_pdf(self):
        data = _full_data()
        data["power"] = _power_not_keyed(0)
        assert dpr_pdf.render_dpr_pdf(data, "Co", "Br").startswith(b"%PDF")

    def test_render_with_garbage_power_is_still_pdf(self):
        data = _full_data()
        data["power"] = "not a dict"
        assert dpr_pdf.render_dpr_pdf(data, "Co", "Br").startswith(b"%PDF")


class TestSpinningMissingInputs:
    """TKT-000234: blank Target/Frame-Shift + the reason, on the PDF the
    WhatsApp DPR sender attaches."""

    def _spinning(self) -> dict:
        sp = _full_data()["spinning"]
        sp["quality"][0]["target_per_frame_shift"] = None
        sp["targets_missing"] = ["13-SKWP"]
        sp["missing_summary"] = [
            {"code": "target_eff", "label": "Quality target Eff % not set",
             "where": "Spinning Target Map > Quality > Target", "qualities": ["13-SKWP"]},
            {"code": "sqc_count", "label": "No Spinning SQC count entered for the day",
             "where": "Spinning SQC (count entry)", "qualities": ["13-SKWP"]},
        ]
        return sp

    def test_lines_headline_and_one_per_reason(self):
        lines = dpr_pdf.spinning_missing_lines(self._spinning())
        assert lines[0] == "Target not shown for 1 quality; Eff % not shown for 1 quality."
        assert lines[1].startswith("Quality target Eff % not set — 1: 13-SKWP.")
        assert "Spinning Target Map > Quality > Target" in lines[1]
        assert "Spinning SQC" in lines[2]

    def test_section_renders_dash_and_reasons(self):
        text = _texts(dpr_pdf._spinning_quality_section(self._spinning(), 500))
        assert dpr_pdf.DASH in text
        assert "Quality target Eff % not set" in text
        assert "Spinning Target Map &gt; Quality &gt; Target" in text

    def test_fallback_line(self):
        sp = self._spinning()
        sp["missing_summary"] = [
            {"code": "target_eff_fallback",
             "label": "Target uses Standard Eff % (Target Eff % not set)",
             "where": "Spinning Target Map > Quality > Target",
             "qualities": ["10.5-SKWP"]},
        ]
        sp["targets_missing"] = []
        sp["standards_missing"] = []
        assert dpr_pdf.spinning_missing_lines(sp) == [
            "Target uses Standard Eff % for 1 quality (Target Eff % not set): 10.5-SKWP "
            "— set in Spinning Target Map > Quality > Target."
        ]

    def test_old_payload_keeps_standards_note(self):
        text = _texts(dpr_pdf._spinning_quality_section(_full_data()["spinning"], 500))
        assert "Eff % is blank for 1 quality" in text

    def test_report_renders_with_missing_summary(self, patched):
        data = _full_data()
        data["spinning"] = self._spinning()
        patched["data"] = data
        pdf, _ = dpr_pdf.build_dpr_pdf(object(), 106, 87, date(2026, 10, 16))
        assert pdf.startswith(b"%PDF")
