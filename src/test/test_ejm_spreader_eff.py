from src.hrms.ejmWagesData import _act_eff, _target_qty, _overall_eff
from src.hrms.ejmWagesData import (
    _beaming_summary, _doff_mazdoor_summary, _drawing_summary, _finishing_summary,
    _finishing_worker_summary, _line_crew_summary,
    _loom_summary, _spinning_summary, _spreader_summary, _winding_summary,
)


def test_winding_summary():
    def row(d, item, qc, prod, target, rate, hrs=8):
        return {"eb_id": 1, "d": d, "shift": "A", "dept_id": 19, "dept_code": "05", "occu_code": "01",
                "name": "W", "hrs": hrs, "item_id": item, "prod": prod, "wages_qc_id": qc,
                "quality_rate": rate, "target": target}
    rows = [
        row(1, 10, 7, 300, 375, 0.01),     # day 1: two items, two qualities, hours once
        row(1, 11, 8, 230, 460, 0.02),
        row(2, 10, 7, 375, 375, 0.01),
        row(2, 99, None, 50, 0, 0),        # unlinked yarn: skipped
    ]
    lines, unlinked = _winding_summary(rows)
    assert unlinked == 1
    by = {t["quality_id"]: t for t in lines}
    # 800 * (675/375 + 230/460) / 16 = 800 * 2.3 / 16
    assert by[7]["eff"] == by[8]["eff"] == 115.0
    assert (by[7]["prod"], by[7]["prod_amount"], by[8]["prod_amount"]) == (675.0, 6.75, 4.6)


def test_finishing_summary():
    # Machine processes: Spec Sheet standard over spell hours.
    rows = [{"process": "lapping", "eb_id": None, "shift": "A", "prod": 50, "std_shift": 100, "hrs": 8},
            {"process": "cutting", "eb_id": None, "shift": "A", "prod": 800, "std_shift": 1000, "hrs": 8}]
    codes, cutting = _finishing_summary(rows)
    assert codes == {32: [("A", 50.0, 50.0)]} and cutting == (80.0, 800.0)


def test_finishing_worker_summary():
    def row(eb, d, proc, pieces, target, rate=0.06, qc=290, hrs=8):
        return {"eb_id": eb, "d": d, "shift": "A", "process": proc, "dept_id": 75, "dept_code": "10",
                "occu_code": "02", "name": "W", "hrs": hrs, "prod": pieces, "wages_qc_id": qc,
                "quality_rate": rate, "target": target}
    rows = [
        # targets are pieces per 8 h for every worker process (user 2026-10-07)
        row(1, 1, "hemming", 2125, 2125),          # = target -> 100%
        row(2, 1, "herackle", 1062.5, 2125),       # -> 50%
        row(3, 1, "hemming", 100, 2125, qc=None),  # unlinked: skipped
    ]
    lines, codes, unlinked = _finishing_worker_summary(rows)
    by = {t["eb_id"]: t for t in lines}
    assert unlinked == 1 and (by[1]["eff"], by[2]["eff"]) == (100.0, 50.0)
    assert (by[1]["prod"], by[1]["prod_amount"]) == (2125.0, 127.5)      # pieces x rate
    assert codes[18] == [("A", 100.0, 2125.0)] and codes[33] == [("A", 50.0, 1062.5)]
    assert codes[19] == [(None, 75.0, 3187.5)]                            # 800*(1+0.5)/16
    # sack sewing likewise
    lines, codes, _ = _finishing_worker_summary([row(4, 1, "sacksewing", 600, 800, qc=291)])
    assert (lines[0]["eff"], lines[0]["prod"]) == (75.0, 600.0) and codes[20] == [("A", 75.0, 600.0)]


def test_beaming_summary():
    def row(mc, name, shift_id, shift, qc, prod, target, rate, hrs):
        return {"machine_id": mc, "machine_name": name, "mech_code": f"3900{mc}", "shift_id": shift_id,
                "shift": shift, "quality_id": qc, "prod": prod, "target": target, "rate": rate, "hrs": hrs}
    rows = [
        row(1, "BEAMING HESSIAN MC NO 1", 7, "A", 88, 50, 100, 0.1, 8),    # 800*0.5/8 = 50
        row(1, "BEAMING HESSIAN MC NO 1", 7, "A", 92, 30, 60, 0.2, 8),     # same mc-shift: +0.5 -> 100
        row(2, "BEAMING SACKING MC 2", 7, "A", 88, 80, 100, 0.1, 16),      # 800*0.8/16 = 40
        row(1, "BEAMING HESSIAN MC NO 1", 8, "B", 88, 40, 0, 0.1, 8),      # no target: not in eff
    ]
    lines, codes = _beaming_summary(rows)
    by = {(t["machine_id"], t["quality_id"], t["shift"]): t for t in lines}
    # mc 1 A: 80 / 8 h x 8 = 80 vs weighted target (50x100 + 30x60) / 80 = 85
    assert by[(1, 88, "A")]["eff"] == by[(1, 92, "A")]["eff"] == 94.12
    assert (by[(1, 92, "A")]["prod_amount"], by[(2, 88, "A")]["eff"]) == (6.0, 40.0)
    assert codes[10] == [("A", 94.12, 80.0)]            # hessian machine only
    assert codes[11] == [("A", 57.66, 160.0)]           # 160 / 24 x 8 vs 92.5
    assert codes[9] == [(None, 57.66, 200.0)]           # B has no target: production only
    # user 2026-10-06: MC 2 = 72 (no target) + 16 + 37 beams in 15 h vs 67 -> 99.50
    mc2 = [row(2, "BEAMING HESSIAN MC NO 2", 7, "A", q, p, t, 0.1, 15)
           for q, p, t in ((327, 72, None), (87, 16, 67), (89, 37, 67))]
    assert _beaming_summary(mc2)[0][0]["eff"] == 99.5


def test_line_crew_summary():
    looms = [  # line 3 exists on both sides; only the hessian one is the sardar's
        {"tran_date": 1, "shift": "A", "side": "H", "line": "3", "working_hours": 8,
         "efficiency": 50, "prod": 1, "tg": 60},
        {"tran_date": 1, "shift": "A", "side": "H", "line": "3", "working_hours": 4,
         "efficiency": 80, "prod": 1, "tg": 60},
        {"tran_date": 1, "shift": "A", "side": "S", "line": "3", "working_hours": 8,
         "efficiency": 99, "prod": 1, "tg": 85},
    ]
    crew = [{"eb_id": 5, "attendance_date": 1, "shift": "A", "line": "3", "name": "S",
             "machine_name": "HESS SARDAR LINE NO 3"},
            {"eb_id": 6, "attendance_date": 1, "shift": "A", "line": "3", "name": "H",
             "machine_name": "S4 LOOM HELPER LINE NO 3"}]
    by = {t["eb_id"]: t for t in _line_crew_summary(looms, crew)}
    assert (by[5]["occu_code"], by[5]["mcno"], by[5]["eff"], by[5]["tareff"]) == ("02", "S00003", 60.0, 60.0)
    assert (by[6]["occu_code"], by[6]["mcno"], by[6]["eff"], by[6]["tareff"]) == ("06", "H00003", 99.0, 85.0)
    assert (by[5]["quantity"], by[6]["quantity"]) == (2.0, 1.0)   # production of the line's looms


def test_loom_summary():
    def row(eb, shift, side, qgrp, q, prod, std, eff, tg, hrs):
        return {"eb_id": eb, "shift": shift, "side": side, "qgrp": qgrp, "quality": q,
                "production_yds": prod, "std_prod_yds": std, "efficiency": eff,
                "target_eff": tg, "working_hours": hrs}
    rows = [
        row(1, "A", "S", "2", 10, 60, 100, 60, 65, 8),   # sacking, quality target 65
        row(1, "A", "S", "2", 11, 35, 50, 70, 0, 4),     # no quality target -> code 7
        row(2, "A", "H", "1", 20, 50, 100, 50, 0, 8),    # hessian -> code 6
    ]
    per_eb, codes, side_shift = _loom_summary(rows, {6: 55.0, 7: 70.0})
    # loom mistry: (eff, tareff, production yds of the loom type that shift)
    assert side_shift == {("S", "A"): (63.33, 66.67, 95.0), ("H", "A"): (50.0, 55.0, 50.0)}
    w1 = [t for t in per_eb if t["eb_id"] == 1]
    assert {t["eff"] for t in w1} == {63.33}                      # (60*8 + 70*4) / 12
    assert {t["tareff"] for t in w1} == {66.67}                   # (65*8 + 70*4) / 12
    assert {t["quality"]: t["quantity"] for t in w1} == {10: 4.06, 11: 2.19}  # 60/60*65/16, 35/70*70/16
    assert codes[13] == [(None, 58.0, 62.0, 145.0)]               # 145/250; (65+35+55)/250
    assert codes[21][0][1] == 63.33 and codes[30] == [("A", 50.0, 55.0, 50.0)]


def test_doff_mazdoor_summary():
    units = [{"tran_date": 1, "machine_id": m, "item_id": 9, "shift": "A", "grp": "SKWP", "p100": 100,
              "mech_code": "34001"}
             for m in (1, 2, 3)]
    frame_wt = [  # frame 3 had a learner spinner, so it is absent here
        {"doff_date": 1, "mc_id": 1, "item_id": 9, "shift": "A", "wt": 60},
        {"doff_date": 1, "mc_id": 2, "item_id": 9, "shift": "A", "wt": 80},
    ]
    hands = [{"eb_id": 7, "attendance_date": 1, "mc_id": m, "shift": "A", "occu_code": "09", "name": "Z"}
             for m in (1, 2, 3)]
    (t,) = _doff_mazdoor_summary(hands, units, frame_wt, {4: 61.0})
    assert (t["occu_code"], t["wt"], t["twt"], t["eff"], t["tareff"]) == ("09", 140.0, 200.0, 70.0, 61.0)


def test_spinning_summary():
    units = [  # one sacking-warp frame and one hessian-weft frame, A shift
        {"tran_date": 1, "machine_id": 1, "item_id": 9, "shift": "A", "grp": "SKWP", "act": 80, "p100": 100,
         "mech_code": "34005"},   # 34-frame: coarse side
        {"tran_date": 1, "machine_id": 2, "item_id": 8, "shift": "A", "grp": "HSWT", "act": 60, "p100": 100,
         "mech_code": "33001"},   # 33-frame: fine side
    ]
    doffs = [  # frame 1 shared by two spinners by weight, frame 2 by one
        {"eb_id": 1, "doff_date": 1, "mc_id": 1, "item_id": 9, "shift": "A", "wt": 60, "name": "X"},
        {"eb_id": 2, "doff_date": 1, "mc_id": 1, "item_id": 9, "shift": "A", "wt": 20, "name": "Y"},
        {"eb_id": 2, "doff_date": 1, "mc_id": 2, "item_id": 8, "shift": "A", "wt": 60, "name": "Y"},
    ]
    per_eb, codes = _spinning_summary(units, doffs, {2: 75.0, 4: 80.0})
    by = {(t["eb_id"], t["side"]): t for t in per_eb}
    assert (by[(1, "C")]["eff"], by[(1, "C")]["tareff"]) == (80.0, 80.0)  # 60 / (100*60/80)
    assert by[(2, "C")]["eff"] == 80.0 and (by[(2, "F")]["eff"], by[(2, "F")]["tareff"]) == (60.0, 75.0)
    assert codes[2] == [(None, 70.0, 77.5, 140.0)]
    assert codes[3][0][1] == 80.0 and codes[4][0][1] == 60.0
    assert codes[7] == [("A", 80.0, 80.0, 80.0)] and codes[8] == [("A", 60.0, 75.0, 60.0)]


def test_drawing_summary():
    targets = {8: 70.0, 12: 80.0, 13: 60.0}
    ops = [  # hand 1 on a fine (21) and a coarse-side (25) machine, same shift
        {"eb_id": 1, "shift": "A", "mc_group": "21", "name": "X", "diffm": 700, "cmeter": 1000},
        {"eb_id": 1, "shift": "A", "mc_group": "25", "name": "X", "diffm": 400, "cmeter": 500},
    ]
    groups = [{"mc_group": "25", "diffm": 400, "cmeter": 500},
              {"mc_group": "29", "diffm": 300, "cmeter": 500}]
    per_eb, codes = _drawing_summary(ops, groups, targets)
    by = {t["side"]: t for t in per_eb}
    assert (by["F"]["eff"], by["F"]["tareff"]) == (70.0, 70.0)
    assert (by["C"]["eff"], by["C"]["tareff"]) == (80.0, 80.0)
    assert codes[43][:2] == (80.0, 80.0) and codes[44][:2] == (80.0, 80.0)
    assert codes[5][:2] == (70.0, 70.0)  # (400+300) / 1000; target (400+300) / 1000


def test_spreader_summary():
    # Hand 1 minds two spreaders in one A-shift attendance row (hours once),
    # and works a B shift; hand 2 works one A shift.
    rows = [
        {"daily_atten_id": 1, "eb_id": 1, "shift": "A", "name": "X", "hrs": 8, "rolls": 30, "wt": 2530},
        {"daily_atten_id": 2, "eb_id": 1, "shift": "B", "name": "X", "hrs": 4, "rolls": 10, "wt": 1265},
        {"daily_atten_id": 3, "eb_id": 2, "shift": "A", "name": "Y", "hrs": 8, "rolls": 60, "wt": 5060},
    ]
    per_eb, shifts, overall = _spreader_summary(rows, 5060)
    by = {(t["eb_id"], t["shift"]): t for t in per_eb}
    assert by[(1, "A")]["eff"] == 50.0      # 2530 / (5060/8*8)
    assert by[(1, "B")]["eff"] == 50.0      # 1265 / (5060/8*4)
    assert by[(2, "A")]["eff"] == 100.0
    assert shifts["A"]["eff"] == 75.0       # 7590 / (5060/8*16)
    assert overall["hours"] == 20 and overall["eff"] == 70.0  # 8855 / 12650


def test_target_qty():
    assert _target_qty({"quantity": 2530, "eff": 50}) == 5060.0          # spreader: 2530 kg at 50%
    assert _target_qty({"quantity": 10, "eff": 0}) is None               # no efficiency
    assert _target_qty({"quantity": None, "eff": 60}) is None            # code rows without quantity
    assert _target_qty({"quantity": 1_227_007, "eff": 61.11}) == 2007866.143  # group totals fit DECIMAL(14,3)
    # an EB's overall eff from many rows = Σ quantity / Σ target_qty
    rows = [{"quantity": 60, "eff": 60}, {"quantity": 35, "eff": 70}]
    assert round(sum(r["quantity"] for r in rows) / sum(_target_qty(r) for r in rows) * 100, 2) == 63.33


def test_eff_apply():
    from src.hrms.ejmWagesData import _eff_apply

    def att(i, eb, dp, oc, shift, wh, ot=0, mc=None, upd="ATT", ns=0, tp="P"):
        return {"data_collection_id": i, "eb_id": eb, "update_for": upd, "dept_code": dp, "occu_code": oc,
                "shift": shift, "mc_id": mc, "working_hours": wh, "ot_hours": ot, "ns_hours": ns, "t_p": tp}

    def eff(eb, dp, oc, shift, e, te, tq=None, details="X", mc=None, amt=None):
        return {"eb_id": eb, "deptcode": dp, "occucode": oc, "shift": shift, "details": details, "mc_id": mc,
                "eff": e, "tareff": te, "quantity": None, "target_qty": tq, "prod_amount": amt}

    rows = [
        att(1, 7, "08", "01", "A", 80, ot=16),          # own rows: 60/75 -> 80%
        att(2, 8, "02", "06", "A", 64),                 # occupation shift row: 120/100 -> capped 100
        att(3, 9, "03", "28", "B", 40),                 # fortnight row only: 45/60 -> 75%
        att(4, 9, "07", "55", "A", 40),                 # occupation 55 -> 100
        att(5, 10, "07", "02", "A", 30, mc=1),          # beaming machine share 30/40 of 200
        att(6, 11, "07", "02", "A", 10, mc=1),          # 10/40 of 200
        att(7, 7, None, None, None, 0, upd="NSH", ns=3.5),
        att(8, 7, "08", "01", "A", 16, tp="T"),          # time-rated: no prod_basic even with production
    ]
    effs = [
        eff(7, "08", "01", "A", 60, 75, tq=100, amt=500),
        eff(None, "02", "06", "A", 120, 100),
        eff(None, "03", "28", None, 45, 60),
        eff(None, "07", None, "A", 50, 100, details="BEAMING", mc=1, amt=200),
    ]
    rates = {("08", "01"): 3.30306, ("07", "02"): 3.27038}
    out = {u["data_collection_id"]: u for u in _eff_apply(rows, effs, {(1, "A"): 40.0}, rates)}
    assert (out[1]["act_eff"], out[1]["working_hours_eff"], out[1]["ot_hours_eff"]) == (80.0, 64.0, 16.0)
    # piece-rated: prod_basic = own prod_amount; time_basic = eff hours x f_b_rate
    assert (out[1]["prod_basic"], out[1]["time_basic"]) == (500.0, round(64 * 3.30306, 2))
    assert (out[2]["act_eff"], out[2]["working_hours_eff"]) == (100.0, 64.0)
    # time-rated (no production, occupation not in the rate table): legacy puts
    # eff hours x 3.45192 into prod_basic, time_basic stays 0
    assert (out[2]["prod_basic"], out[2]["time_basic"]) == (round(64 * 3.45192, 2), 0.0)
    assert (out[3]["act_eff"], out[3]["working_hours_eff"]) == (75.0, 30.0)
    assert out[4]["act_eff"] == 100.0
    assert (out[5]["prod_basic"], out[5]["time_basic"]) == (150.0, round(30 * 3.27038, 2))
    assert (out[6]["prod_basic"], out[6]["time_basic"]) == (50.0, round(10 * 3.27038, 2))
    assert (out[7]["ns_hrs_eff"], out[7]["prod_basic"], out[7]["time_basic"]) == (3.5, 0.0, 0.0)
    assert (out[8]["prod_basic"], out[8]["time_basic"]) == (0.0, round(round(16 * 0.8, 2) * 3.30306, 2))


def test_overall_eff():
    # weaving 60/70 over 300 loom hrs, spinning 40/80 over 100 frame hrs
    assert _overall_eff((60, 70), (40, 80), 300, 100) == (55.0, 72.5)
    assert _overall_eff((60, 70), (40, 80), 0, 0) == (0.0, 0.0)


def test_act_eff():
    assert _act_eff({"eff": 54, "tareff": 60}) == 90.0
    assert _act_eff({"eff": 54, "tareff": 0}) is None
    assert _act_eff({"eff": None, "tareff": 60}) is None
    assert _act_eff({"eff": 40, "tareff": 60, "occucode": "55"}) == 100.0
