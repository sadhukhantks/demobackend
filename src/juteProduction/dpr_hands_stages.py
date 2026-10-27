"""Stage-wise hands per tonne for the Daily Production Report (TKT-000185).

Hands/MT used to divide EVERY hand in the mill by spinning output alone
(TKT-000117). That ignored two things the mill lives with daily:

* purchased yarn — bought yarn is issued straight to beaming, so weaving
  hands do process it while no spinning hand ever made it;
* surplus yarn — the mill spins (and buys) more yarn than it weaves on most
  days (sls branch 29, 20 Sep – 10 Oct 2026: spun 526 MT + bought 43 MT,
  wove 510 MT); the balance goes to yarn stock or sale.

Owner ruling 2026-10-11 (ticket comment + chat): split the hands by the
production stage their department belongs to and divide each stage by the
tonnage THAT stage produced —

    yarn stage  (jute, batching, preparing, spinning)       ÷ spinning MT
    cloth stage (winding, beaming, weaving, finishing, ...)  ÷ weaving MT
    common      (mechanical, electrical, general, ...)       spread pro-rata
                                                            over the two

The headline is the SUM: hands per MT of cloth, counting each stage once.
Purchased yarn then lands only in the weaving denominator, where hands did
work on it, and surplus yarn only in the spinning denominator, where hands
did make it. Beside it the sheet prints the same ratio month-to-date (day
timing — yarn wound today goes on beams tomorrow — washes out) and the day's
yarn balance (spun + purchased − woven) so an odd day explains itself.

Department → stage is decided on the department NAME (``dept_mst.dept_desc``
reached through the worked designation; ``daily_attendance.worked_department_id``
is a legacy id space that does not join to dept_mst). The sheet lists which
departments fell where, so a tenant whose names do not fit shows up at once
rather than silently landing in "common".

Pure functions — no DB access; dpr_reports.py wires them.
"""

from decimal import Decimal
from typing import Optional

STAGE_YARN = "yarn"
STAGE_CLOTH = "cloth"
STAGE_COMMON = "common"

# Substrings of the UPPER-CASED department name. Cloth is checked first so
# "COP WINDING" / "SPOOL WINDING" can never fall to a yarn pattern. "PRESS" is
# deliberately absent (AIR COMPRESSOR); bale press is caught by "BALE".
CLOTH_PATTERNS = (
    "WIND", "SPOOL", "TWIST", "BEAM", "WEAV", "LOOM",
    "FINISH", "CALENDER", "SEW", "SACK", "BALE", "HEMM", "LAMINAT",
)
YARN_PATTERNS = (
    "JUTE", "BATCH", "SPREADER", "CARD", "PREPAR", "DRAWING",
    "SPINN", "SPNG", "ROVING",
)

STAGE_BASIS_LABEL = (
    "per MT of cloth: yarn-stage hands ÷ spinning MT + cloth-stage hands ÷ weaving MT"
)
FALLBACK_BASIS_LABEL = "spinning output (departments not mapped to stages)"

YARN_STAGE_LABEL = "Yarn stage (jute → spinning)"
CLOTH_STAGE_LABEL = "Cloth stage (winding → finishing)"
COMMON_LABEL = "Common (spread pro-rata)"


def _f(v, default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def _round(v: float, nd: int = 2) -> float:
    return round(v + 0.0, nd)


def ratio_or_none(num: float, den: float, nd: int = 2) -> Optional[float]:
    """num / den, or None when there is nothing to divide by.

    None, not 0.0: hands over zero tonnage is "no production to rate against",
    and a printed 0.0 would read as free labour.
    """
    return _round(num / den, nd) if den else None


def classify_dept(dept_desc: Optional[str]) -> str:
    """Stage of a department from its name; unknown / blank → common."""
    name = (dept_desc or "").strip().upper()
    if not name:
        return STAGE_COMMON
    for pat in CLOTH_PATTERNS:
        if pat in name:
            return STAGE_CLOTH
    for pat in YARN_PATTERNS:
        if pat in name:
            return STAGE_YARN
    return STAGE_COMMON


def _share(yarn_direct: float, cloth_direct: float, fallback: float) -> float:
    direct = yarn_direct + cloth_direct
    return yarn_direct / direct if direct else fallback


def build_hands_stages(
    dept_rows: list,
    spinning_day_mt: float,
    spinning_mtd_mt: float,
    weaving_day_mt: float,
    weaving_mtd_mt: float,
) -> dict:
    """Stage-wise hands and hands/MT from per-department hands rows.

    ``dept_rows``: dicts with ``dept_desc``, ``day_hands``, ``mtd_hands``
    (one per department, as get_dpr_hands_by_dept_query returns them).

    Common hands are spread over the two stages in proportion to their DIRECT
    hands, day and MTD each on its own period; a day with no direct hands at
    all (a holiday where only maintenance came in) borrows the MTD share.
    ``available`` is False when neither period has a single direct hand — the
    caller then falls back to the TKT-000117 spinning basis.
    """
    direct = {
        STAGE_YARN: {"day": 0.0, "mtd": 0.0, "depts": []},
        STAGE_CLOTH: {"day": 0.0, "mtd": 0.0, "depts": []},
        STAGE_COMMON: {"day": 0.0, "mtd": 0.0, "depts": []},
    }
    for r in dept_rows:
        stage = classify_dept(r.get("dept_desc"))
        bucket = direct[stage]
        bucket["day"] += _f(r.get("day_hands"))
        bucket["mtd"] += _f(r.get("mtd_hands"))
        name = (r.get("dept_desc") or "").strip() or "(no department)"
        if name not in bucket["depts"]:
            bucket["depts"].append(name)
    for bucket in direct.values():
        bucket["depts"].sort()

    yarn, cloth, common = direct[STAGE_YARN], direct[STAGE_CLOTH], direct[STAGE_COMMON]
    available = (yarn["day"] + cloth["day"] + yarn["mtd"] + cloth["mtd"]) > 0

    mtd_share = _share(yarn["mtd"], cloth["mtd"], 0.5)
    day_share = _share(yarn["day"], cloth["day"], mtd_share)

    def stage(bucket: dict, share_day: float, share_mtd: float, day_mt: float,
              mtd_mt: float, label: str, basis_label: str) -> dict:
        common_day = common["day"] * share_day
        common_mtd = common["mtd"] * share_mtd
        day_hands = bucket["day"] + common_day
        mtd_hands = bucket["mtd"] + common_mtd
        return {
            "label": label,
            "basis_label": basis_label,
            "depts": list(bucket["depts"]),
            "direct_day_hands": _round(bucket["day"]),
            "direct_mtd_hands": _round(bucket["mtd"]),
            "common_day_hands": _round(common_day),
            "common_mtd_hands": _round(common_mtd),
            "day_hands": _round(day_hands),
            "mtd_hands": _round(mtd_hands),
            "day_mt": _round(_f(day_mt), 3),
            "mtd_mt": _round(_f(mtd_mt), 3),
            "hands_per_mt": ratio_or_none(day_hands, _f(day_mt)),
            "mtd_hands_per_mt": ratio_or_none(mtd_hands, _f(mtd_mt)),
        }

    yarn_out = stage(yarn, day_share, mtd_share, spinning_day_mt, spinning_mtd_mt,
                     YARN_STAGE_LABEL, "spinning output")
    cloth_out = stage(cloth, 1.0 - day_share, 1.0 - mtd_share, weaving_day_mt,
                      weaving_mtd_mt, CLOTH_STAGE_LABEL, "weaving output")

    def combined(a: Optional[float], b: Optional[float]) -> Optional[float]:
        # Each MT of cloth needs an MT of yarn, so the per-MT figures add; if
        # either stage has no tonnage the sum is not a hands/MT at all.
        if a is None or b is None:
            return None
        return _round(a + b)

    return {
        "available": available,
        "yarn": yarn_out,
        "cloth": cloth_out,
        "common": {
            "label": COMMON_LABEL,
            "depts": list(common["depts"]),
            "day_hands": _round(common["day"]),
            "mtd_hands": _round(common["mtd"]),
            "yarn_share_pct": _round(day_share * 100.0, 1),
            "mtd_yarn_share_pct": _round(mtd_share * 100.0, 1),
        },
        "total": {
            "day_hands": _round(yarn["day"] + cloth["day"] + common["day"]),
            "mtd_hands": _round(yarn["mtd"] + cloth["mtd"] + common["mtd"]),
            "hands_per_mt": combined(yarn_out["hands_per_mt"], cloth_out["hands_per_mt"]),
            "mtd_hands_per_mt": combined(
                yarn_out["mtd_hands_per_mt"], cloth_out["mtd_hands_per_mt"]
            ),
        },
    }


def build_yarn_balance(
    spun_day_mt: float,
    spun_mtd_mt: float,
    woven_day_mt: float,
    woven_mtd_mt: float,
    purchased_day_kg: Optional[float],
    purchased_mtd_kg: Optional[float],
    purchased_available: bool,
) -> dict:
    """Spun + purchased yarn issued − woven = yarn to stock / sale (MT).

    Purchased yarn is the approved Yarn Issue tonnage (net of returns) — it
    reaches the floor at beaming, bypassing winding. ``purchased_available``
    False = this tenant has no yarn purchase tables, so the balance is own
    yarn only and says so.
    """

    def period(spun: float, woven: float, purchased_kg: Optional[float]) -> dict:
        purchased_mt = _f(purchased_kg) / 1000.0 if purchased_available else 0.0
        spun = _f(spun)
        woven = _f(woven)
        return {
            "spun_mt": _round(spun, 3),
            "purchased_issued_mt": _round(purchased_mt, 3) if purchased_available else None,
            "woven_mt": _round(woven, 3),
            "surplus_mt": _round(spun + purchased_mt - woven, 3),
        }

    return {
        "purchased_available": purchased_available,
        "day": period(spun_day_mt, woven_day_mt, purchased_day_kg),
        "mtd": period(spun_mtd_mt, woven_mtd_mt, purchased_mtd_kg),
    }
