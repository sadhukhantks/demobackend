"""Tests for the stage-wise hands/MT of the Daily Production Report (TKT-000185).

src/juteProduction/dpr_hands_stages.py — pure functions, no DB.

Covered:
  * classify_dept — cloth / yarn / common by department name; blank and
    unknown names are common; AIR COMPRESSOR is not "press"; cloth wins for
    "COP WINDING" / "SPOOL WINDING"
  * build_hands_stages — sls br 29 golden case (9 Oct 2026): direct hands per
    stage, common spread pro-rata, each stage over its own tonnage, headline
    = sum; departments listed per stage
  * purchased / surplus yarn land in the right denominator only
  * zero tonnage gives None, not 0.0; no direct hands -> unavailable; a day
    with only common hands borrows the MTD share
  * build_yarn_balance — spun + purchased − woven, day and MTD; tenant
    without yarn tables
"""

from src.juteProduction.dpr_hands_stages import (
    STAGE_CLOTH,
    STAGE_COMMON,
    STAGE_YARN,
    build_hands_stages,
    build_yarn_balance,
    classify_dept,
    ratio_or_none,
)


class TestClassifyDept:
    def test_sls_branch_29_names(self):
        yarn = ["JUTE(EJM)", "BATCHING(EJM)", "PREPARING(EJM)", "SPINNING(EJM)",
                "SPREADER MACHINE NO-8"]
        cloth = ["SPOOL WINDING(EJM)", "COP WINDING(EJM)", "WINDING(EJM)",
                 "TWISTING(EJM)", "BEAMING(EJM)", "WEAVING(EJM)", "FINISHING(EJM)",
                 "SACK SEWING(EJM)", "BALE PRESS"]
        common = ["MILL MECHANICAL(EJM)", "FACTORY MECHANICAL(EJM)", "GENERAL(EJM)",
                  "OTHERS GENERAL(EJM)", "GENERAL MECHANICAL(EJM)", "ELECTRICAL(EJM)",
                  "STAFF", "S Q C(EJM)", "BOILER(EJM)", "CARPET(EJM)", "FELT(EJM)",
                  "BLA GODOWN(EJM)", "ACCOUNTS"]
        assert all(classify_dept(n) == STAGE_YARN for n in yarn), [
            (n, classify_dept(n)) for n in yarn
        ]
        assert all(classify_dept(n) == STAGE_CLOTH for n in cloth), [
            (n, classify_dept(n)) for n in cloth
        ]
        assert all(classify_dept(n) == STAGE_COMMON for n in common), [
            (n, classify_dept(n)) for n in common
        ]

    def test_air_compressor_is_not_a_press(self):
        assert classify_dept("AIR COMPRESSOR") == STAGE_COMMON
        assert classify_dept("AIR COMPRESSOR(EJM)") == STAGE_COMMON

    def test_blank_and_unknown_are_common(self):
        assert classify_dept(None) == STAGE_COMMON
        assert classify_dept("") == STAGE_COMMON
        assert classify_dept("   ") == STAGE_COMMON
        assert classify_dept("DISPENSARY") == STAGE_COMMON

    def test_case_insensitive(self):
        assert classify_dept("spinning") == STAGE_YARN
        assert classify_dept("Weaving Shed") == STAGE_CLOTH


class TestRatioOrNone:
    def test_none_on_zero_denominator(self):
        assert ratio_or_none(10.0, 0.0) is None
        assert ratio_or_none(10.0, 4.0) == 2.5


# sls co 2 / br 29, 9 Oct 2026 — hands by department of the worked designation
# (approved attendance), the day the owner reviewed the options on.
SLS_9_OCT = [
    {"dept_desc": "WEAVING(EJM)", "day_hands": 159.63, "mtd_hands": 1596.3},
    {"dept_desc": "SPINNING(EJM)", "day_hands": 158.06, "mtd_hands": 1580.6},
    {"dept_desc": "SPOOL WINDING(EJM)", "day_hands": 101.13, "mtd_hands": 1011.3},
    {"dept_desc": "PREPARING(EJM)", "day_hands": 89.31, "mtd_hands": 893.1},
    {"dept_desc": "SACK SEWING(EJM)", "day_hands": 61.63, "mtd_hands": 616.3},
    {"dept_desc": "MILL MECHANICAL(EJM)", "day_hands": 47.0, "mtd_hands": 470.0},
    {"dept_desc": "FINISHING(EJM)", "day_hands": 46.63, "mtd_hands": 466.3},
    {"dept_desc": "BATCHING(EJM)", "day_hands": 39.06, "mtd_hands": 390.6},
    {"dept_desc": "FACTORY MECHANICAL(EJM)", "day_hands": 34.0, "mtd_hands": 340.0},
    {"dept_desc": "BEAMING(EJM)", "day_hands": 29.0, "mtd_hands": 290.0},
    {"dept_desc": "GENERAL(EJM)", "day_hands": 21.38, "mtd_hands": 213.8},
    {"dept_desc": "OTHERS GENERAL(EJM)", "day_hands": 17.38, "mtd_hands": 173.8},
    {"dept_desc": "GENERAL MECHANICAL(EJM)", "day_hands": 13.13, "mtd_hands": 131.3},
    {"dept_desc": "JUTE(EJM)", "day_hands": 10.0, "mtd_hands": 100.0},
    {"dept_desc": "ELECTRICAL(EJM)", "day_hands": 6.88, "mtd_hands": 68.8},
    {"dept_desc": "COP WINDING(EJM)", "day_hands": 6.0, "mtd_hands": 60.0},
]
YARN_DIRECT = 158.06 + 89.31 + 39.06 + 10.0            # 296.43
CLOTH_DIRECT = 159.63 + 101.13 + 61.63 + 46.63 + 29.0 + 6.0  # 404.02
COMMON = 47.0 + 34.0 + 21.38 + 17.38 + 13.13 + 6.88    # 139.77


class TestBuildHandsStages:
    def test_sls_golden_case_9_oct(self):
        out = build_hands_stages(SLS_9_OCT, 30.031, 300.31, 29.073, 290.73)
        assert out["available"] is True
        yarn, cloth, common = out["yarn"], out["cloth"], out["common"]
        assert yarn["direct_day_hands"] == round(YARN_DIRECT, 2)
        assert cloth["direct_day_hands"] == round(CLOTH_DIRECT, 2)
        assert common["day_hands"] == round(COMMON, 2)
        # Common spread by direct hands: 296.43 / 700.45 = 42.3 % to yarn.
        share = YARN_DIRECT / (YARN_DIRECT + CLOTH_DIRECT)
        assert common["yarn_share_pct"] == round(share * 100, 1)
        assert yarn["common_day_hands"] == round(COMMON * share, 2)
        assert cloth["common_day_hands"] == round(COMMON * (1 - share), 2)
        # The three blocks still add up to every hand in the mill.
        assert round(yarn["day_hands"] + cloth["day_hands"], 1) == round(
            YARN_DIRECT + CLOTH_DIRECT + COMMON, 1
        )
        assert out["total"]["day_hands"] == 840.22
        # Each stage over ITS OWN tonnage.
        yarn_hands = YARN_DIRECT + COMMON * share
        cloth_hands = CLOTH_DIRECT + COMMON * (1 - share)
        assert yarn["day_mt"] == 30.031 and yarn["basis_label"] == "spinning output"
        assert cloth["day_mt"] == 29.073 and cloth["basis_label"] == "weaving output"
        assert yarn["hands_per_mt"] == round(yarn_hands / 30.031, 2)   # ≈ 11.84
        assert cloth["hands_per_mt"] == round(cloth_hands / 29.073, 2)  # ≈ 16.67
        # Headline = the sum (per MT of cloth), ≈ 28.5 — not 840 / 30 = 28.0.
        assert out["total"]["hands_per_mt"] == round(
            yarn["hands_per_mt"] + cloth["hands_per_mt"], 2
        )
        assert 28.4 <= out["total"]["hands_per_mt"] <= 28.6
        # MTD rides the same way on the MTD columns.
        assert yarn["mtd_hands_per_mt"] == round(yarn["mtd_hands"] / 300.31, 2)
        assert out["total"]["mtd_hands_per_mt"] == round(
            yarn["mtd_hands_per_mt"] + cloth["mtd_hands_per_mt"], 2
        )

    def test_departments_listed_per_stage(self):
        out = build_hands_stages(SLS_9_OCT, 30.0, 300.0, 29.0, 290.0)
        assert out["yarn"]["depts"] == [
            "BATCHING(EJM)", "JUTE(EJM)", "PREPARING(EJM)", "SPINNING(EJM)"
        ]
        assert "COP WINDING(EJM)" in out["cloth"]["depts"]
        assert "WEAVING(EJM)" in out["cloth"]["depts"]
        assert out["common"]["depts"] == [
            "ELECTRICAL(EJM)", "FACTORY MECHANICAL(EJM)", "GENERAL MECHANICAL(EJM)",
            "GENERAL(EJM)", "MILL MECHANICAL(EJM)", "OTHERS GENERAL(EJM)",
        ]

    def test_no_department_rows_land_in_common_as_named(self):
        rows = [
            {"dept_desc": None, "day_hands": 5.0, "mtd_hands": 50.0},
            {"dept_desc": "SPINNING", "day_hands": 10.0, "mtd_hands": 100.0},
        ]
        out = build_hands_stages(rows, 1.0, 10.0, 1.0, 10.0)
        assert out["common"]["depts"] == ["(no department)"]
        assert out["common"]["day_hands"] == 5.0

    def test_purchased_yarn_only_touches_the_cloth_denominator(self):
        """7 Oct 2026 on sls: 6.25 MT bought yarn went to beaming. Weaving MT
        carries it (cloth hands did process it); spinning MT does not (no
        yarn hand made it) — so the yarn-stage figure is identical with or
        without the purchase and only the cloth stage moves."""
        rows = [
            {"dept_desc": "SPINNING", "day_hands": 300.0, "mtd_hands": 3000.0},
            {"dept_desc": "WEAVING", "day_hands": 400.0, "mtd_hands": 4000.0},
        ]
        without = build_hands_stages(rows, 30.0, 300.0, 21.0, 210.0)
        with_purchase = build_hands_stages(rows, 30.0, 300.0, 27.25, 272.5)
        assert without["yarn"]["hands_per_mt"] == with_purchase["yarn"]["hands_per_mt"] == 10.0
        assert without["cloth"]["hands_per_mt"] == round(400 / 21.0, 2)
        assert with_purchase["cloth"]["hands_per_mt"] == round(400 / 27.25, 2)

    def test_surplus_yarn_only_touches_the_yarn_denominator(self):
        """Yarn spun but not woven stays in the spinning MT (the yarn hands did
        make it) and leaves the cloth stage alone."""
        rows = [
            {"dept_desc": "SPINNING", "day_hands": 300.0, "mtd_hands": 3000.0},
            {"dept_desc": "WEAVING", "day_hands": 400.0, "mtd_hands": 4000.0},
        ]
        balanced = build_hands_stages(rows, 27.0, 270.0, 27.0, 270.0)
        surplus = build_hands_stages(rows, 30.0, 300.0, 27.0, 270.0)
        assert balanced["cloth"]["hands_per_mt"] == surplus["cloth"]["hands_per_mt"]
        assert surplus["yarn"]["hands_per_mt"] == 10.0
        assert balanced["yarn"]["hands_per_mt"] == round(300 / 27.0, 2)

    def test_zero_tonnage_is_none_not_zero(self):
        rows = [
            {"dept_desc": "SPINNING", "day_hands": 10.0, "mtd_hands": 100.0},
            {"dept_desc": "WEAVING", "day_hands": 10.0, "mtd_hands": 100.0},
        ]
        out = build_hands_stages(rows, 0.0, 100.0, 5.0, 50.0)
        assert out["yarn"]["hands_per_mt"] is None
        assert out["cloth"]["hands_per_mt"] == 2.0
        # Half a sum is not a hands/MT — the headline is blank, MTD is fine.
        assert out["total"]["hands_per_mt"] is None
        assert out["total"]["mtd_hands_per_mt"] == round(100 / 100 + 100 / 50, 2)

    def test_no_direct_hands_at_all_is_unavailable(self):
        rows = [{"dept_desc": "GENERAL", "day_hands": 40.0, "mtd_hands": 400.0}]
        out = build_hands_stages(rows, 10.0, 100.0, 10.0, 100.0)
        assert out["available"] is False
        assert out["common"]["day_hands"] == 40.0
        assert build_hands_stages([], 10.0, 100.0, 10.0, 100.0)["available"] is False

    def test_holiday_with_only_common_hands_borrows_the_mtd_share(self):
        rows = [
            {"dept_desc": "SPINNING", "day_hands": 0.0, "mtd_hands": 300.0},
            {"dept_desc": "WEAVING", "day_hands": 0.0, "mtd_hands": 100.0},
            {"dept_desc": "MECHANICAL", "day_hands": 40.0, "mtd_hands": 400.0},
        ]
        out = build_hands_stages(rows, 0.0, 300.0, 0.0, 100.0)
        assert out["available"] is True
        assert out["common"]["yarn_share_pct"] == 75.0  # from MTD 300 / 400
        assert out["yarn"]["day_hands"] == 30.0
        assert out["cloth"]["day_hands"] == 10.0
        assert out["total"]["hands_per_mt"] is None  # nothing produced


class TestBuildYarnBalance:
    def test_sls_7_oct(self):
        out = build_yarn_balance(30.005, 300.05, 27.226, 272.26, 6253.0, 42710.0, True)
        assert out["purchased_available"] is True
        assert out["day"] == {
            "spun_mt": 30.005,
            "purchased_issued_mt": 6.253,
            "woven_mt": 27.226,
            "surplus_mt": round(30.005 + 6.253 - 27.226, 3),  # +9.032
        }
        assert out["mtd"]["purchased_issued_mt"] == 42.71
        assert out["mtd"]["surplus_mt"] == round(300.05 + 42.71 - 272.26, 3)

    def test_deficit_is_negative(self):
        out = build_yarn_balance(20.0, 200.0, 25.0, 250.0, 0.0, 0.0, True)
        assert out["day"]["surplus_mt"] == -5.0

    def test_tenant_without_yarn_tables(self):
        out = build_yarn_balance(30.0, 300.0, 27.0, 270.0, None, None, False)
        assert out["purchased_available"] is False
        assert out["day"]["purchased_issued_mt"] is None
        assert out["day"]["surplus_mt"] == 3.0  # own yarn only
