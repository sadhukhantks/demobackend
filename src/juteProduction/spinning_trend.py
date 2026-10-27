"""Spinning Production Trend report
(GET /api/juteProductionReports/spinning_production_trend).

Day-wise and quality-wise spinning production over a date range, with the
"average kg per frame" the mill asks for expressed as **kg per frame-shift**,
and a loss analysis that says WHERE the gap to standard sits:

    production kg = frame-shifts run  x  kg per frame-shift

A frame-shift is one frame doffing in one shift letter on one day (A1 / A2
fold into A), the DPR's frame-run convention. Normalising per shift is what
makes a one-shift Sunday comparable with a three-shift weekday, and splitting
the two factors is what tells a manager whether a dip is "fewer frames ran" or
"the frames that ran produced less".

Every actual is a roll-up of ONE aggregating scan of daily_doff_tbl
(``get_spinning_trend_rows_query``). Targets are an overlay: the Spinning
Production grid's *Std Prod* per (date, spell, frame, yarn) unit, resolved
with the Doff Analysis rules (``unit_target``), summed per cut over the units
that HAVE a target and compared with the actual kg of those same units — so
achievement never overstates and a long range is never blanked by a single
unit without standards. The share of kg that had a standard is reported as
``std_coverage_pct`` beside every figure. Where a TARGET efficiency has been
entered for a yarn, the planning grid's target production (p100 x target eff)
is carried alongside as ``tgt_*`` — standard and target are shown as lines
next to the actual, which is how the mill reads them. The standard is also
restated at the yarn's STANDARD count (``stdc_*``, ``unit_stdc``): the planning
formula takes the day's SQC count, so a yarn spun heavier than its nominal
count inflates the standard along with the actual; the std-count line is what
the standard would be at the count the yarn is supposed to be spun at.

The loss analysis (``build_loss``) decomposes the period:

    capacity at standard (installed frames x days x shifts x std per frame-shift)
      - idle frames   (installed frames that never doffed)
      - idle shifts   (frames that ran, shifts they did not run)
      = standard of the frame-shifts that ran
      - efficiency gap (standard - actual of those frame-shifts)
      = actual

and then cuts the efficiency gap by shift, weekday, quality, frame (Pareto),
spinner and regular-vs-contract hands, lists the worst frame-shifts, flags
"short" frame-shifts (far fewer doffs than the frame's norm — the only proxy
for time lost while spinning has no stoppage entries) and gives a frame x day
achievement grid. Capacity figures are estimates (idle frame-shifts are costed
at the period's average standard per frame-shift) and include planned idle
time — the report cannot tell a holiday from a breakdown.

The arithmetic lives in ``build_trend`` (pure: rows in, response dict out) so
it is testable without a database; the router only validates, queries and
wires. Design note: docs/superpowers/specs/2026-10-07-spinning-production-trend-design.md
"""

import statistics
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import SPINNING_MACHINE_TYPE_NAME
from src.juteProduction.doff_analysis import (
    CONTRACT_LABEL,
    UNMAPPED_QUALITY,
    _eb_label,
    _frame_key,
    _iso,
    _optional_int,
    _pos,
    _quality,
    _require_date,
    _require_int,
    _shift,
    _spell_minutes,
    _verify_pair,
    target_notes,
    unit_target,
)
from src.juteProduction.services.spinning_rules import p100_prod_spell
from src.juteProduction.spinning_trend_query import (
    get_installed_frames_query,
    get_spinning_trend_rows_query,
    get_trend_target_inputs_query,
)

spinning_trend_router = APIRouter()

# A quarter. The scan is one aggregated row per (date, shift, frame, yarn,
# spinner) — ~35 frames x 3 shifts x 93 days is a few thousand rows twice over
# (the previous period rides the same query), so the cap is about screen
# width, not the database.
MAX_RANGE_DAYS = 93

# Frame status against its own quality mix (see _frame_expected): below LOW_PCT
# of the expected kg per frame-shift is LOW, below WATCH_PCT is WATCH.
LOW_PCT = 90.0
WATCH_PCT = 97.0

STATUS_LOW = "low"
STATUS_WATCH = "watch"
STATUS_OK = "ok"

SHIFT_LETTERS = ("A", "B", "C")
MAX_SHIFTS_PER_DAY = 3

# A frame-shift is "short" when it has at most this share of the frame's median
# doffs per frame-shift (and the frame normally doffs at least MIN_MEDIAN_DOFFS):
# far fewer doffs than the frame's own norm means the frame stood for part of
# the shift. Spinning has no stoppage entries, so this is the time-lost proxy.
SHORT_DOFF_RATIO = 0.6
MIN_MEDIAN_DOFFS = 4

WORST_UNITS = 25
TOP_SPINNERS = 10

# "Needs attention" rules (any one flags the frame; see build_attention):
ATTN_BELOW_MEDIAN_PTS = 10.0   # mean achievement this far under the fleet median
ATTN_ERRATIC_SD = 15.0         # achievement swings this much across its frame-shifts
ATTN_SHORT_MIN = 2             # short frame-shifts in the period
ATTN_WORST_MIN = 2             # appearances in the worst-frame-shift list
ACH_BANDS = ["<30"] + [f"{lo}-{lo + 10}" for lo in range(30, 110, 10)] + [">=110"]


def _f(v) -> float:
    """DECIMAL/None -> float, 2 dp (every kg figure in this report)."""
    return round(float(v or 0), 2)


def _ratio(num, den, dp=1) -> Optional[float]:
    """num / den rounded, None when the denominator is zero — a blank, never a
    fake 0 that would drag a chart to the floor."""
    if not den:
        return None
    return round(float(num) / float(den), dp)


def _pct(num, den) -> Optional[float]:
    r = _ratio(num, den, 3)
    return None if r is None else round(r * 100, 1)


def _delta_pct(now, before) -> Optional[float]:
    """% change now vs before; None when either side is missing/zero."""
    if now is None or not before:
        return None
    return round((float(now) - float(before)) / float(before) * 100, 1)


def _weekday(d: str) -> str:
    try:
        return date.fromisoformat(d).strftime("%a")
    except ValueError:
        return ""


def _ach_band(pct: float) -> str:
    if pct < 30:
        return ACH_BANDS[0]
    if pct >= 110:
        return ACH_BANDS[-1]
    lo = int(pct // 10) * 10
    return f"{lo}-{lo + 10}"


class _Agg:
    """Running totals for one cut (a day, a quality, a frame, ...)."""

    __slots__ = (
        "kg", "doffs", "frames", "frame_shifts", "days", "shifts",
        "target", "covered", "covered_fs", "tgt", "tgt_covered", "tgt_fs",
        "stdc", "stdc_fs",
    )

    def __init__(self):
        self.kg = 0.0
        self.doffs = 0
        self.frames: set = set()
        self.frame_shifts: set = set()
        self.days: set = set()
        self.shifts: set = set()
        # Standard overlay: sum of unit standards, the actual kg of those
        # units and the frame-shifts they cover (for standard per frame-shift).
        self.target = 0.0
        self.covered = 0.0
        self.covered_fs: set = set()
        # Target overlay (only where a target efficiency has been entered).
        self.tgt = 0.0
        self.tgt_covered = 0.0
        self.tgt_fs: set = set()
        # The standard restated at the yarn's standard count (only where the
        # unit has a standard and both counts are known).
        self.stdc = 0.0
        self.stdc_fs: set = set()

    def add(self, d: str, sh: str, frame_id, net: float, doffs: int) -> None:
        self.kg += net
        self.doffs += doffs
        self.frames.add(frame_id)
        self.frame_shifts.add((d, frame_id, sh))
        self.days.add(d)
        self.shifts.add(sh)

    def add_target(
        self,
        std: float,
        actual: float,
        fs_key=None,
        tgt: Optional[float] = None,
        stdc: Optional[float] = None,
    ) -> None:
        self.target += std
        self.covered += actual
        if fs_key is not None:
            self.covered_fs.add(fs_key)
        if tgt:
            self.tgt += tgt
            self.tgt_covered += actual
            if fs_key is not None:
                self.tgt_fs.add(fs_key)
        if stdc:
            self.stdc += stdc
            if fs_key is not None:
                self.stdc_fs.add(fs_key)

    @property
    def gap(self) -> float:
        return round(self.target - self.covered, 2) if self.target > 0 else 0.0

    def target_fields(self) -> dict:
        """Standard (``std_*``), where entered target (``tgt_*``) and the
        standard at the yarn's standard count (``stdc_*``) figures.
        Per-frame-shift figures divide by the frame-shifts the overlay covers,
        so a half-covered day is not understated."""
        has = self.target > 0
        has_tgt = self.tgt > 0
        has_stdc = self.stdc > 0
        return {
            "std_kg": round(self.target, 2) if has else None,
            "std_kg_per_frame_shift": _ratio(self.target, len(self.covered_fs)) if has else None,
            "std_frame_shifts": len(self.covered_fs) if has else 0,
            "std_ach_pct": _pct(self.covered, self.target) if has else None,
            "std_coverage_pct": _pct(self.covered, self.kg) if has else None,
            "tgt_kg": round(self.tgt, 2) if has_tgt else None,
            "tgt_kg_per_frame_shift": _ratio(self.tgt, len(self.tgt_fs)) if has_tgt else None,
            "tgt_frame_shifts": len(self.tgt_fs) if has_tgt else 0,
            "stdc_kg": round(self.stdc, 2) if has_stdc else None,
            "stdc_kg_per_frame_shift": _ratio(self.stdc, len(self.stdc_fs)) if has_stdc else None,
            "stdc_frame_shifts": len(self.stdc_fs) if has_stdc else 0,
        }

    def gap_fields(self, gap_total: float) -> dict:
        has = self.target > 0
        return {
            "gap_kg": self.gap if has else None,
            "gap_share_pct": _pct(self.gap, gap_total) if (has and gap_total > 0) else None,
        }


# ---------------------------------------------------------------------------
# Targets (pure)
# ---------------------------------------------------------------------------


def unit_tgt(r: dict) -> Optional[float]:
    """The unit's TARGET production (planning grid: p100prod x target_eff /
    100), or None when no target efficiency has been entered for the yarn.
    A Processed unit's frozen non-zero target_prod wins, like the standard."""
    frozen = _pos(r.get("frozen_tgt_prod"))
    if frozen:
        return round(frozen, 2)
    tgt_eff = _pos(r.get("target_eff"))
    if not tgt_eff:
        return None
    inputs = (r.get("std_speed"), r.get("spindles"), r.get("std_tpi"), r.get("act_count"))
    if any(not _pos(v) for v in inputs):
        return None
    minutes = _spell_minutes(r.get("spell_code"), r.get("working_hours"))
    if not minutes:
        return None
    frame_net = _pos(r.get("frame_net"))
    share = _pos(r.get("item_net")) / frame_net if frame_net else 1.0
    p100 = p100_prod_spell(
        _pos(r.get("std_speed")),
        round(minutes * share),
        _pos(r.get("act_count")),
        _pos(r.get("spindles")),
        _pos(r.get("std_tpi")),
    )
    tgt = round(p100 * tgt_eff / 100, 2)
    return tgt or None


def unit_stdc(r: dict, target: Optional[float]) -> Optional[float]:
    """The unit's standard production at the yarn's STANDARD count (Yarn
    master ``jute_yarn_count``) instead of the day's SQC count — what the
    standard would be if the yarn were spun at its nominal count.

    p100 is linear in count, so a frozen (Processed) standard is scaled by
    ``std_count / act_count`` using the counts the planning grid stored on
    that row (the live SQC average may have drifted since); a live-recomputed
    standard is recomputed with the standard count. None when the unit has no
    standard or either count is unknown — a blank, never a copy of the
    standard, so the line is only drawn where it means something."""
    if not target:
        return None
    frozen = _pos(r.get("frozen_target"))
    if frozen:
        std_count = _pos(r.get("frozen_std_count")) or _pos(r.get("std_count"))
        act_count = _pos(r.get("frozen_act_count")) or _pos(r.get("act_count"))
        if not std_count or not act_count:
            return None
        return round(frozen * std_count / act_count, 2) or None
    std_count = _pos(r.get("std_count"))
    if not std_count:
        return None
    # ``target`` exists, so every other input is present (see unit_target).
    minutes = _spell_minutes(r.get("spell_code"), r.get("working_hours"))
    frame_net = _pos(r.get("frame_net"))
    share = _pos(r.get("item_net")) / frame_net if frame_net else 1.0
    p100 = p100_prod_spell(
        _pos(r.get("std_speed")),
        round(minutes * share),
        std_count,
        _pos(r.get("spindles")),
        _pos(r.get("std_tpi")),
    )
    return round(p100 * _pos(r.get("std_eff")) / 100, 2) or None


def build_target_units(target_rows: list) -> list:
    """Target-input rows -> one dict per (date, spell, frame, yarn) unit, with
    the unit's ACTUAL kg kept alongside its standard (``target``), where a
    target efficiency is entered its target (``tgt``), and the standard at
    the yarn's standard count (``stdc``), so all can be compared with the
    actual of the same units. ``missing`` feeds ``target_notes``."""
    units = []
    for r in target_rows or []:
        target, missing = unit_target(r)
        units.append(
            {
                "date": _iso(r.get("doff_date")),
                "shift": _shift(r.get("spell_code")),
                "machine_id": r.get("machine_id"),
                "frame": r.get("frame"),
                "item_id": r.get("item_id"),
                "actual": _f(r.get("item_net")),
                "target": target,
                "tgt": unit_tgt(r) if target else None,
                "stdc": unit_stdc(r, target),
                "missing": missing,
            }
        )
    return units


# ---------------------------------------------------------------------------
# Builder (pure — rows in, response dict out)
# ---------------------------------------------------------------------------


def _frame_expected(frame_fs_by_item: dict, quality_kpfs: dict) -> Optional[float]:
    """Expected kg per frame-shift for a frame given ITS quality mix: each
    quality's period average weighted by the frame-shifts the frame ran on it.
    A frame on a coarse count is judged against the coarse count's average,
    not against the fleet's."""
    num = 0.0
    den = 0
    for item_id, fs in frame_fs_by_item.items():
        avg = quality_kpfs.get(item_id)
        if avg is None:
            continue
        num += avg * fs
        den += fs
    return round(num / den, 1) if den else None


def _status(vs_pct: Optional[float]) -> Optional[str]:
    if vs_pct is None:
        return None
    if vs_pct < LOW_PCT:
        return STATUS_LOW
    if vs_pct < WATCH_PCT:
        return STATUS_WATCH
    return STATUS_OK


def _period(rows: list, d_from: date, d_to: date) -> tuple:
    """Whole-period totals + the per-day cuts of one window."""
    total = _Agg()
    by_day: dict = {}
    for r in rows:
        d = _iso(r.get("doff_date"))
        sh = _shift(r.get("shift"))
        fid = r.get("machine_id")
        net = _f(r.get("net_wt"))
        n = int(r.get("num_doffs") or 0)
        total.add(d, sh, fid, net, n)
        by_day.setdefault(d, _Agg()).add(d, sh, fid, net, n)
    producing = len(by_day)
    frames_per_day = sum(len(a.frames) for a in by_day.values())
    return total, by_day, {
        "date_from": _iso(d_from),
        "date_to": _iso(d_to),
        "calendar_days": (d_to - d_from).days + 1,
        "producing_days": producing,
        "production_kg": round(total.kg, 2),
        "avg_kg_per_day": _ratio(total.kg, producing, 2),
        "frame_shifts": len(total.frame_shifts),
        "kg_per_frame_shift": _ratio(total.kg, len(total.frame_shifts)),
        "frames_avg_per_day": _ratio(frames_per_day, producing),
        "doffs": total.doffs,
        "avg_doff_kg": _ratio(total.kg, total.doffs, 2),
    }


def _allocate_targets(rows: list, units: Optional[list], by_day: dict) -> dict:
    """Unit targets -> per (date, shift, frame, item) group, then spread over
    that group's rows (spinners) by kg share, so every row carries the slice
    of standard it was responsible for. Returns {id(row): (standard, actual,
    target | None, standard at std count | None)}; rows of groups without a
    standard are absent."""
    group_target: dict = {}
    group_tgt: dict = {}
    group_stdc: dict = {}
    for u in units or []:
        t = u.get("target")
        if not t or u["date"] not in by_day:
            continue
        key = (u["date"], u["shift"], u["machine_id"], u["item_id"])
        group_target[key] = group_target.get(key, 0.0) + t
        if u.get("tgt"):
            group_tgt[key] = group_tgt.get(key, 0.0) + u["tgt"]
        if u.get("stdc"):
            group_stdc[key] = group_stdc.get(key, 0.0) + u["stdc"]

    group_kg: dict = {}
    for r in rows:
        key = (_iso(r.get("doff_date")), _shift(r.get("shift")), r.get("machine_id"), r.get("item_id"))
        group_kg[key] = group_kg.get(key, 0.0) + _f(r.get("net_wt"))

    alloc: dict = {}
    for r in rows:
        key = (_iso(r.get("doff_date")), _shift(r.get("shift")), r.get("machine_id"), r.get("item_id"))
        t = group_target.get(key)
        if not t:
            continue
        kg = _f(r.get("net_wt"))
        total_kg = group_kg.get(key) or 0.0
        share = kg / total_kg if total_kg else 0.0
        g = group_tgt.get(key)
        c = group_stdc.get(key)
        alloc[id(r)] = (t * share, kg, g * share if g else None, c * share if c else None)
    return alloc


def build_loss(
    rows: list,
    alloc: dict,
    by_frame: dict,
    frame_label: dict,
    quality_label: dict,
    total: "_Agg",
    producing_days: int,
    installed_frames: Optional[int],
    shift_filter: Optional[str],
    item_filter: Optional[int],
    frame_filter: Optional[str],
) -> dict:
    """The "where do we lose" block — every figure a roll-up of the same rows
    and the same allocated targets the rest of the report uses."""
    # --- frame-shift level (all yarns of a frame in a shift together) -------
    fs_kg: dict = {}
    fs_doffs: dict = {}
    fs_target: dict = {}
    fs_covered: dict = {}
    fs_tgt: dict = {}
    fs_spinner_kg: dict = {}
    fs_items: dict = {}
    for r in rows:
        key = (_iso(r.get("doff_date")), _shift(r.get("shift")), r.get("machine_id"))
        kg = _f(r.get("net_wt"))
        fs_kg[key] = fs_kg.get(key, 0.0) + kg
        fs_doffs[key] = fs_doffs.get(key, 0) + int(r.get("num_doffs") or 0)
        fs_items.setdefault(key, set()).add(quality_label.get(r.get("item_id"), UNMAPPED_QUALITY))
        label = _eb_label(r.get("eb_id"), r.get("emp_code"))
        sp = fs_spinner_kg.setdefault(key, {})
        sp[label] = sp.get(label, 0.0) + kg
        a = alloc.get(id(r))
        if a:
            fs_target[key] = fs_target.get(key, 0.0) + a[0]
            fs_covered[key] = fs_covered.get(key, 0.0) + a[1]
            if a[2]:
                fs_tgt[key] = fs_tgt.get(key, 0.0) + a[2]

    def fs_ach(key) -> Optional[float]:
        t = fs_target.get(key)
        return _pct(fs_covered.get(key, 0.0), t) if t else None

    # --- by shift / weekday ------------------------------------------------
    def cut(keyfn) -> list:
        aggs: dict = {}
        for r in rows:
            d = _iso(r.get("doff_date"))
            sh = _shift(r.get("shift"))
            fid = r.get("machine_id")
            a = aggs.setdefault(keyfn(d, sh), _Agg())
            a.add(d, sh, fid, _f(r.get("net_wt")), int(r.get("num_doffs") or 0))
            al = alloc.get(id(r))
            if al:
                a.add_target(al[0], al[1], (d, fid, sh), al[2], al[3])
        out = []
        for k, a in aggs.items():
            fs = len(a.frame_shifts)
            out.append(
                {
                    "key": k,
                    "days": len(a.days),
                    "frames": len(a.frames),
                    "frame_shifts": fs,
                    "kg": round(a.kg, 2),
                    "kg_per_frame_shift": _ratio(a.kg, fs),
                    **a.target_fields(),
                    **a.gap_fields(total.gap),
                }
            )
        return out

    by_shift = cut(lambda d, sh: sh)
    by_shift.sort(key=lambda x: x["key"])
    for row in by_shift:
        row["shift"] = row.pop("key")

    weekday_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_weekday = cut(lambda d, sh: _weekday(d))
    by_weekday.sort(key=lambda x: weekday_order.index(x["key"]) if x["key"] in weekday_order else 9)
    for row in by_weekday:
        row["weekday"] = row.pop("key")

    # --- hands: regular vs contract, and spinners --------------------------
    hands: dict = {}
    spinners: dict = {}
    for r in rows:
        d = _iso(r.get("doff_date"))
        sh = _shift(r.get("shift"))
        fid = r.get("machine_id")
        kg = _f(r.get("net_wt"))
        n = int(r.get("num_doffs") or 0)
        kind = "contract" if r.get("eb_id") is None else "regular"
        hands.setdefault(kind, _Agg()).add(d, sh, fid, kg, n)
        label = _eb_label(r.get("eb_id"), r.get("emp_code"))
        spinners.setdefault(label, _Agg()).add(d, sh, fid, kg, n)
        al = alloc.get(id(r))
        if al:
            hands[kind].add_target(al[0], al[1], (d, fid, sh), al[2], al[3])
            spinners[label].add_target(al[0], al[1], (d, fid, sh), al[2], al[3])

    def agg_row(a: "_Agg") -> dict:
        fs = len(a.frame_shifts)
        return {
            "frame_shifts": fs,
            "frames": len(a.frames),
            "days": len(a.days),
            "kg": round(a.kg, 2),
            "kg_per_frame_shift": _ratio(a.kg, fs),
            **a.target_fields(),
            **a.gap_fields(total.gap),
        }

    hands_rows = [
        {"kind": kind, **agg_row(hands[kind])}
        for kind in ("regular", "contract")
        if kind in hands
    ]
    spinner_rows = [
        {"spinner": label, **agg_row(a)}
        for label, a in spinners.items()
        if a.target > 0 and label != CONTRACT_LABEL
    ]
    spinner_rows.sort(key=lambda x: -(x["gap_kg"] or 0))
    top_spinners = spinner_rows[:TOP_SPINNERS]

    # --- achievement distribution + frame consistency ----------------------
    hist = {b: {"band": b, "frame_shifts": 0, "kg": 0.0} for b in ACH_BANDS}
    frame_achs: dict = {}
    for key, t in fs_target.items():
        a = fs_ach(key)
        if a is None:
            continue
        hist[_ach_band(a)]["frame_shifts"] += 1
        hist[_ach_band(a)]["kg"] = round(hist[_ach_band(a)]["kg"] + fs_kg.get(key, 0.0), 2)
        frame_achs.setdefault(key[2], []).append(a)

    # --- short frame-shifts (time-lost proxy) ------------------------------
    frame_doffs: dict = {}
    for (d, sh, fid), n in fs_doffs.items():
        frame_doffs.setdefault(fid, []).append(n)
    frame_median = {fid: statistics.median(v) for fid, v in frame_doffs.items()}
    short = []
    for key, n in fs_doffs.items():
        med = frame_median.get(key[2], 0)
        if med >= MIN_MEDIAN_DOFFS and n <= SHORT_DOFF_RATIO * med:
            t = fs_target.get(key)
            short.append(
                {
                    "date": key[0],
                    "shift": key[1],
                    "frame": frame_label.get(key[2]),
                    "doffs": n,
                    "median_doffs": med,
                    "kg": round(fs_kg.get(key, 0.0), 2),
                    "std_kg": round(t, 2) if t else None,
                    "tgt_kg": round(fs_tgt[key], 2) if key in fs_tgt else None,
                    "ach_pct": fs_ach(key),
                    "gap_kg": round(t - fs_covered.get(key, 0.0), 2) if t else None,
                }
            )
    short.sort(key=lambda x: (-(x["gap_kg"] or 0), x["date"]))
    short_gap = round(sum(s["gap_kg"] or 0 for s in short), 2)
    short_by_frame: dict = {}
    for s_row in short:
        short_by_frame[s_row["frame"]] = short_by_frame.get(s_row["frame"], 0) + 1

    # --- worst frame-shifts ------------------------------------------------
    worst = []
    for key, t in fs_target.items():
        if not t:
            continue
        spinner_kg = fs_spinner_kg.get(key, {})
        spinner = max(spinner_kg.items(), key=lambda kv: kv[1])[0] if spinner_kg else ""
        if len(spinner_kg) > 1:
            spinner = f"{spinner} +{len(spinner_kg) - 1}"
        worst.append(
            {
                "date": key[0],
                "shift": key[1],
                "frame": frame_label.get(key[2]),
                "qualities": ", ".join(sorted(fs_items.get(key, set()))),
                "spinner": spinner,
                "doffs": fs_doffs.get(key, 0),
                "kg": round(fs_kg.get(key, 0.0), 2),
                "std_kg": round(t, 2),
                "tgt_kg": round(fs_tgt[key], 2) if key in fs_tgt else None,
                "ach_pct": fs_ach(key),
                "gap_kg": round(t - fs_covered.get(key, 0.0), 2),
            }
        )
    worst.sort(key=lambda x: -x["gap_kg"])
    worst = worst[:WORST_UNITS]
    worst_by_frame: dict = {}
    for w in worst:
        worst_by_frame[w["frame"]] = worst_by_frame.get(w["frame"], 0) + 1

    # --- frame x day achievement grid (worst frame first) -------------------
    fd_target: dict = {}
    fd_covered: dict = {}
    for key, t in fs_target.items():
        fd = (key[0], key[2])
        fd_target[fd] = fd_target.get(fd, 0.0) + t
        fd_covered[fd] = fd_covered.get(fd, 0.0) + fs_covered.get(key, 0.0)
    heat_frames: dict = {}
    heat_gaps: dict = {}
    for (d, fid), t in fd_target.items():
        heat_frames.setdefault(fid, {})[d] = _pct(fd_covered.get((d, fid), 0.0), t)
        # actual - standard: negative = below standard
        heat_gaps.setdefault(fid, {})[d] = round(fd_covered.get((d, fid), 0.0) - t, 1)
    heat_rows = []
    for fid, cells in heat_frames.items():
        vals = [v for v in cells.values() if v is not None]
        gaps = heat_gaps.get(fid, {})
        heat_rows.append(
            {
                "frame": frame_label.get(fid),
                "mean_ach_pct": round(sum(vals) / len(vals), 1) if vals else None,
                "gap_kg": round(sum(gaps.values()), 1),
                "cells": dict(sorted(cells.items())),
                "gap_cells": dict(sorted(gaps.items())),
            }
        )
    heat_rows.sort(key=lambda x: (x["gap_kg"], _frame_key(x["frame"])))
    heat_dates = sorted({d for (d, _fid) in fd_target})

    # Every frame-shift with a standard as a point: (standard kg, actual kg)
    # — the actual-vs-standard scatter, which needs no percentage.
    points = [
        {
            "date": key[0],
            "shift": key[1],
            "frame": frame_label.get(key[2]),
            "kg": round(fs_kg.get(key, 0.0), 1),
            "std_kg": round(t, 1),
            "tgt_kg": round(fs_tgt[key], 1) if key in fs_tgt else None,
        }
        for key, t in sorted(fs_target.items())
        if t
    ]

    # --- capacity bridge ---------------------------------------------------
    run_fs = len(total.frame_shifts)
    covered_fs = len(fs_target)
    std_per_fs = _ratio(total.target, covered_fs) if covered_fs else None
    letters = {sh for sh in total.shifts if sh in SHIFT_LETTERS}
    shifts_per_day = 1 if shift_filter else max(1, min(MAX_SHIFTS_PER_DAY, len(letters)))
    frames_ran = len(total.frames)
    installed = 1 if frame_filter else installed_frames
    capacity_applicable = (
        installed is not None and item_filter is None and std_per_fs is not None and producing_days > 0
    )
    bridge = None
    if capacity_applicable:
        installed = max(int(installed), frames_ran)
        capacity_fs = installed * producing_days * shifts_per_day
        idle_frames_fs = (installed - frames_ran) * producing_days * shifts_per_day
        idle_shifts_fs = max(0, frames_ran * producing_days * shifts_per_day - run_fs)
        run_std_kg = round(run_fs * std_per_fs, 2)
        bridge = {
            "installed_frames": installed,
            "frames_ran": frames_ran,
            "producing_days": producing_days,
            "shifts_per_day": shifts_per_day,
            "std_per_frame_shift": std_per_fs,
            "capacity_frame_shifts": capacity_fs,
            "idle_frame_frame_shifts": idle_frames_fs,
            "idle_shift_frame_shifts": idle_shifts_fs,
            "run_frame_shifts": run_fs,
            "utilization_pct": _pct(run_fs, capacity_fs),
            "steps": [
                {"step": "capacity", "label": "Capacity at standard", "kg": round(capacity_fs * std_per_fs, 2), "kind": "total"},
                {"step": "idle_frames", "label": "Frames that never ran", "kg": round(idle_frames_fs * std_per_fs, 2), "kind": "loss"},
                {"step": "idle_shifts", "label": "Shifts not run", "kg": round(idle_shifts_fs * std_per_fs, 2), "kind": "loss"},
                {"step": "run_std", "label": "Standard of frame-shifts run", "kg": run_std_kg, "kind": "total"},
                {"step": "efficiency", "label": "Below standard while running", "kg": round(run_std_kg - total.kg, 2), "kind": "loss"},
                {"step": "actual", "label": "Actual", "kg": round(total.kg, 2), "kind": "total"},
            ],
        }

    return {
        "gap_kg": total.gap if total.target > 0 else None,
        "bridge": bridge,
        "ach_histogram": [hist[b] for b in ACH_BANDS],
        "median_ach_pct": (
            round(statistics.median([fs_ach(k) for k in fs_target if fs_ach(k) is not None]), 1)
            if fs_target
            else None
        ),
        "by_shift": by_shift,
        "by_weekday": by_weekday,
        "hands": hands_rows,
        "spinners": top_spinners,
        "spinner_count": len(spinner_rows),
        "short_frame_shifts": {
            "count": len(short),
            "share_pct": _pct(len(short), run_fs),
            "gap_kg": short_gap,
            "rule": (
                f"doffs at or below {int(SHORT_DOFF_RATIO * 100)}% of the frame's median "
                f"doffs per frame-shift (frames with a median of at least {MIN_MEDIAN_DOFFS})"
            ),
            "rows": short[:WORST_UNITS],
        },
        "worst_frame_shifts": worst,
        "heat": {"dates": heat_dates, "frames": heat_rows},
        "frame_shift_points": points,
        "frame_consistency": {
            fid: {
                "mean_ach_pct": round(sum(v) / len(v), 1),
                "ach_sd": round(statistics.pstdev(v), 1) if len(v) > 1 else 0.0,
            }
            for fid, v in frame_achs.items()
        },
        "_short_by_frame": short_by_frame,
        "_worst_by_frame": worst_by_frame,
    }


def build_attention(frames: list, median_ach: Optional[float], short_by_frame: dict,
                    worst_by_frame: dict) -> list:
    """The frames that need a look, with the reason(s), biggest gap first.

    A frame is listed when ANY rule fires: below its quality peers (LOW /
    WATCH), mean achievement well under the fleet median, erratic across its
    frame-shifts, repeatedly short, or repeatedly in the worst-frame-shift
    list. The rules are deliberately simple and printed with the row — the
    supervisor decides, the report only points."""
    out = []
    for f in frames:
        reasons = []
        if f.get("status") == STATUS_LOW:
            reasons.append(f"LOW vs peers ({f['vs_peers_pct']}% of expected for its qualities)")
        elif f.get("status") == STATUS_WATCH:
            reasons.append(f"WATCH vs peers ({f['vs_peers_pct']}% of expected)")
        mean = f.get("mean_ach_pct")
        if mean is not None and median_ach is not None and mean < median_ach - ATTN_BELOW_MEDIAN_PTS:
            reasons.append(
                f"mean {mean}% of standard, {round(median_ach - mean, 1)} points under the fleet median"
            )
        sd = f.get("ach_sd")
        if sd is not None and sd >= ATTN_ERRATIC_SD and (f.get("frame_shifts") or 0) >= 3:
            reasons.append(f"erratic: achievement swings ±{sd} points between frame-shifts")
        n_short = short_by_frame.get(f.get("frame"), 0)
        if n_short >= ATTN_SHORT_MIN:
            reasons.append(f"{n_short} short frame-shifts (stood for part of the shift)")
        n_worst = worst_by_frame.get(f.get("frame"), 0)
        if n_worst >= ATTN_WORST_MIN:
            reasons.append(f"{n_worst} of the {WORST_UNITS} worst frame-shifts")
        if not reasons:
            continue
        out.append(
            {
                "frame": f.get("frame"),
                "machine_id": f.get("machine_id"),
                "status": f.get("status"),
                "qualities": f.get("qualities"),
                "frame_shifts": f.get("frame_shifts"),
                "kg": f.get("kg"),
                "kg_per_frame_shift": f.get("kg_per_frame_shift"),
                "expected_kg_per_frame_shift": f.get("expected_kg_per_frame_shift"),
                "vs_peers_pct": f.get("vs_peers_pct"),
                "std_kg": f.get("std_kg"),
                "std_kg_per_frame_shift": f.get("std_kg_per_frame_shift"),
                "std_ach_pct": f.get("std_ach_pct"),
                "mean_ach_pct": mean,
                "ach_sd": sd,
                "gap_kg": f.get("gap_kg"),
                "gap_share_pct": f.get("gap_share_pct"),
                "short_count": n_short,
                "worst_count": n_worst,
                "reasons": reasons,
            }
        )
    out.sort(key=lambda x: (-(x["gap_kg"] or 0), _frame_key(x["frame"])))
    return out


def build_trend(
    rows: list,
    d_from: date,
    d_to: date,
    prev_rows: Optional[list] = None,
    prev_from: Optional[date] = None,
    prev_to: Optional[date] = None,
    units: Optional[list] = None,
    installed_frames: Optional[int] = None,
    shift_filter: Optional[str] = None,
    item_filter: Optional[int] = None,
    frame_filter: Optional[str] = None,
) -> dict:
    """Every cut of the report from the (date, shift, frame, yarn, spinner)
    aggregate.

    ``rows`` is the report window, ``prev_rows`` the same-length window before
    it (comparison only — production, frame-shifts, frames). ``units`` (from
    ``build_target_units``) overlays targets on every cut. ``installed_frames``
    and the active filters feed the capacity bridge of the loss block.
    """
    total, by_day, summary = _period(rows, d_from, d_to)
    alloc = _allocate_targets(rows, units, by_day)

    by_day_item: dict = {}
    by_day_shift: dict = {}
    by_item: dict = {}
    by_frame: dict = {}
    frame_label: dict = {}
    frame_items: dict = {}  # machine_id -> {item_id: frame-shift set}
    quality_label: dict = {}

    for r in rows:
        d = _iso(r.get("doff_date"))
        sh = _shift(r.get("shift"))
        fid = r.get("machine_id")
        item = r.get("item_id")
        net = _f(r.get("net_wt"))
        n = int(r.get("num_doffs") or 0)
        quality_label[item] = _quality(r.get("quality_name"))
        frame_label[fid] = r.get("frame")

        cuts = (
            by_day[d],
            by_day_item.setdefault((d, item), _Agg()),
            by_day_shift.setdefault((d, sh), _Agg()),
            by_item.setdefault(item, _Agg()),
            by_frame.setdefault(fid, _Agg()),
        )
        for c in cuts[1:]:
            c.add(d, sh, fid, net, n)
        frame_items.setdefault(fid, {}).setdefault(item, set()).add((d, sh))

        al = alloc.get(id(r))
        if al:
            total.add_target(al[0], al[1], (d, fid, sh), al[2], al[3])
            for c in cuts:
                c.add_target(al[0], al[1], (d, fid, sh), al[2], al[3])

    gap_total = total.gap if total.target > 0 else 0.0

    days = []
    for d in sorted(by_day):
        a = by_day[d]
        fs = len(a.frame_shifts)
        days.append(
            {
                "date": d,
                "weekday": _weekday(d),
                "shifts": sorted(a.shifts),
                "frames": len(a.frames),
                "frame_shifts": fs,
                "doffs": a.doffs,
                "kg": round(a.kg, 2),
                "kg_per_frame_shift": _ratio(a.kg, fs),
                "kg_per_frame": _ratio(a.kg, len(a.frames)),
                "avg_doff_kg": _ratio(a.kg, a.doffs, 2),
                **a.target_fields(),
                **a.gap_fields(gap_total),
            }
        )

    day_quality = []
    for (d, item), a in sorted(by_day_item.items(), key=lambda kv: (kv[0][0], -kv[1].kg)):
        fs = len(a.frame_shifts)
        tf = a.target_fields()
        day_quality.append(
            {
                "date": d,
                "item_id": item,
                "quality": quality_label[item],
                "kg": round(a.kg, 2),
                "frames": len(a.frames),
                "frame_shifts": fs,
                "doffs": a.doffs,
                "kg_per_frame_shift": _ratio(a.kg, fs),
                "std_kg": tf["std_kg"],
                "std_kg_per_frame_shift": tf["std_kg_per_frame_shift"],
                "std_ach_pct": tf["std_ach_pct"],
                "tgt_kg": tf["tgt_kg"],
                "tgt_kg_per_frame_shift": tf["tgt_kg_per_frame_shift"],
                "stdc_kg": tf["stdc_kg"],
                "stdc_kg_per_frame_shift": tf["stdc_kg_per_frame_shift"],
            }
        )

    day_shift = []
    for (d, sh), a in sorted(by_day_shift.items()):
        tf = a.target_fields()
        day_shift.append(
            {
                "date": d,
                "shift": sh,
                "kg": round(a.kg, 2),
                "frames": len(a.frames),
                "doffs": a.doffs,
                "frame_shifts": len(a.frame_shifts),
                "kg_per_frame_shift": _ratio(a.kg, len(a.frame_shifts)),
                "std_kg": tf["std_kg"],
                "std_kg_per_frame_shift": tf["std_kg_per_frame_shift"],
                "std_ach_pct": tf["std_ach_pct"],
                "tgt_kg": tf["tgt_kg"],
                "tgt_kg_per_frame_shift": tf["tgt_kg_per_frame_shift"],
                "stdc_kg": tf["stdc_kg"],
                "stdc_kg_per_frame_shift": tf["stdc_kg_per_frame_shift"],
            }
        )

    qualities = []
    quality_kpfs: dict = {}
    for item, a in sorted(by_item.items(), key=lambda kv: -kv[1].kg):
        fs = len(a.frame_shifts)
        kpfs = _ratio(a.kg, fs)
        quality_kpfs[item] = kpfs
        qualities.append(
            {
                "item_id": item,
                "quality": quality_label[item],
                "kg": round(a.kg, 2),
                "share_pct": _pct(a.kg, total.kg),
                "days": len(a.days),
                "frames": len(a.frames),
                "frame_shifts": fs,
                "doffs": a.doffs,
                "kg_per_frame_shift": kpfs,
                "avg_doff_kg": _ratio(a.kg, a.doffs, 2),
                **a.target_fields(),
                **a.gap_fields(gap_total),
            }
        )

    loss = build_loss(
        rows,
        alloc,
        by_frame,
        frame_label,
        quality_label,
        total,
        len(by_day),
        installed_frames,
        shift_filter,
        item_filter,
        frame_filter,
    )
    consistency = loss.pop("frame_consistency")
    short_by_frame = loss.pop("_short_by_frame")
    worst_by_frame = loss.pop("_worst_by_frame")

    frames = []
    for fid, a in by_frame.items():
        fs = len(a.frame_shifts)
        kpfs = _ratio(a.kg, fs)
        expected = _frame_expected(
            {item: len(s) for item, s in frame_items.get(fid, {}).items()},
            quality_kpfs,
        )
        vs = _pct(kpfs, expected) if (kpfs is not None and expected) else None
        tf = a.target_fields()
        cons = consistency.get(fid, {})
        frames.append(
            {
                "machine_id": fid,
                "frame": frame_label.get(fid),
                "kg": round(a.kg, 2),
                "days": len(a.days),
                "frame_shifts": fs,
                "doffs": a.doffs,
                "kg_per_frame_shift": kpfs,
                "expected_kg_per_frame_shift": expected,
                "vs_peers_pct": vs,
                "status": _status(vs),
                "qualities": sorted({quality_label[i] for i in frame_items.get(fid, {})}),
                "std_kg": tf["std_kg"],
                "std_kg_per_frame_shift": tf["std_kg_per_frame_shift"],
                "std_ach_pct": tf["std_ach_pct"],
                "tgt_kg": tf["tgt_kg"],
                "tgt_kg_per_frame_shift": tf["tgt_kg_per_frame_shift"],
                "stdc_kg": tf["stdc_kg"],
                "stdc_kg_per_frame_shift": tf["stdc_kg_per_frame_shift"],
                **a.gap_fields(gap_total),
                "mean_ach_pct": cons.get("mean_ach_pct"),
                "ach_sd": cons.get("ach_sd"),
            }
        )
    frames.sort(key=lambda x: _frame_key(x["frame"]))
    loss["attention"] = build_attention(
        frames, loss.get("median_ach_pct"), short_by_frame, worst_by_frame
    )

    producing = [d for d in days if d["kg"] > 0]
    summary.update(total.target_fields())
    summary["gap_kg"] = total.gap if total.target > 0 else None
    if producing:
        best = max(producing, key=lambda x: x["kg"])
        worst = min(producing, key=lambda x: x["kg"])
        summary["best_day"] = {"date": best["date"], "kg": best["kg"]}
        summary["worst_day"] = {"date": worst["date"], "kg": worst["kg"]}
    else:
        summary["best_day"] = None
        summary["worst_day"] = None

    if prev_from is not None and prev_to is not None:
        _t, _b, prev = _period(prev_rows or [], prev_from, prev_to)
        prev.pop("calendar_days", None)
        prev.pop("doffs", None)
        prev.pop("avg_doff_kg", None)
        summary["previous"] = prev
        summary["delta"] = {
            "production_kg": _delta_pct(summary["production_kg"], prev["production_kg"]),
            "avg_kg_per_day": _delta_pct(summary["avg_kg_per_day"], prev["avg_kg_per_day"]),
            "kg_per_frame_shift": _delta_pct(
                summary["kg_per_frame_shift"], prev["kg_per_frame_shift"]
            ),
            "frames_avg_per_day": _delta_pct(
                summary["frames_avg_per_day"], prev["frames_avg_per_day"]
            ),
        }
    else:
        summary["previous"] = None
        summary["delta"] = None

    return {
        "summary": summary,
        "days": days,
        "day_quality": day_quality,
        "day_shift": day_shift,
        "qualities": qualities,
        "frames": frames,
        "loss": loss,
        "options": {
            "shifts": sorted(total.shifts),
            "frames": [f["frame"] for f in frames],
        },
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _load_target_units(db: Session, binds: dict) -> tuple:
    """(units, notes). Targets are an overlay on a report whose actuals must
    keep working on a tenant without the spinning-standards tables (schema
    drift) — a failing target query blanks the targets and says so."""
    try:
        target_rows = [
            dict(r._mapping)
            for r in db.execute(get_trend_target_inputs_query(), binds).fetchall()
        ]
    except Exception:
        return [], ["Targets could not be loaded — target figures are blank."]
    units = build_target_units(target_rows)
    return units, target_notes(units)


def _load_installed_frames(db: Session, branch_id: int) -> Optional[int]:
    try:
        row = db.execute(
            get_installed_frames_query(),
            {"branch_id": branch_id, "spinning_type": SPINNING_MACHINE_TYPE_NAME},
        ).first()
    except Exception:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError):
        return None


def _optional_shift(request: Request) -> Optional[str]:
    raw = (request.query_params.get("shift") or "").strip().upper()
    if not raw:
        return None
    if raw not in SHIFT_LETTERS and raw != "G":
        raise HTTPException(status_code=400, detail="shift must be A, B, C or G")
    return raw


@spinning_trend_router.get("/spinning_production_trend")
def spinning_production_trend(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    # Every parameter is validated BEFORE the session is touched.
    co_id = _require_int(request, "co_id")
    branch_id = _require_int(request, "branch_id")
    item_id = _optional_int(request, "item_id")
    shift = _optional_shift(request)
    frame = (request.query_params.get("frame") or "").strip() or None
    d_from = _require_date(request, "date_from")
    d_to = _require_date(request, "date_to")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    n_days = (d_to - d_from).days + 1
    if n_days > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large — at most {MAX_RANGE_DAYS} days",
        )
    prev_to = d_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=n_days - 1)

    try:
        _verify_pair(db, co_id, branch_id)
        # One scan covers the previous period too; split by date here.
        all_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_spinning_trend_rows_query(),
                {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "date_from": prev_from,
                    "date_to": d_to,
                    "item_id": item_id,
                    "shift": shift,
                    "frame": frame,
                },
            ).fetchall()
        ]
        rows, prev_rows = [], []
        for r in all_rows:
            dd = r.get("doff_date")
            dd = dd if isinstance(dd, date) else date.fromisoformat(str(dd))
            (rows if dd >= d_from else prev_rows).append(r)

        units, notes_ = ([], [])
        installed = None
        if rows:
            units, notes_ = _load_target_units(
                db,
                {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "date_from": d_from,
                    "date_to": d_to,
                    "spell_id": None,
                    "item_id": item_id,
                    "frame": frame,
                },
            )
            if shift:
                # The target query has no shift-letter bind; the units carry it.
                units = [u for u in units if u["shift"] == shift]
            installed = _load_installed_frames(db, branch_id)

        data = build_trend(
            rows,
            d_from,
            d_to,
            prev_rows,
            prev_from,
            prev_to,
            units,
            installed,
            shift,
            item_id,
            frame,
        )
        notes = [
            "A frame-shift is one frame doffing in one shift letter on one day "
            "(A1/A2 count as A); kg per frame-shift = production / frame-shifts. "
            "Day averages use producing days only.",
        ]
        if not rows:
            notes.append("No spinning doffs in this range.")
        if any(q["quality"] == UNMAPPED_QUALITY for q in data["qualities"]):
            notes.append("Doffs with no yarn stamped are grouped as 'Unmapped'.")
        if any(len(items) > 1 for items in _frames_multi_quality(rows)):
            notes.append(
                "A frame that changed yarn within a shift counts one frame-shift "
                "under each yarn, so quality-wise frame-shifts can add up to more "
                "than the day's."
            )
        if rows and data["summary"].get("std_kg") is not None:
            if data["summary"].get("tgt_kg") is None:
                notes.append(
                    "No target efficiency is entered for these yarns, so only the standard "
                    "is shown; enter a target efficiency in Spinning Standards to add the "
                    "target line."
                )
            elif (data["summary"].get("tgt_frame_shifts") or 0) < (data["summary"].get("std_frame_shifts") or 0):
                notes.append(
                    f"Target is entered for {data['summary']['tgt_frame_shifts']} of "
                    f"{data['summary']['std_frame_shifts']} frame-shifts with a standard; "
                    "target figures cover those only."
                )
        if rows and data["summary"].get("stdc_kg") is not None:
            notes.append(
                "'Std at std count' is the standard recomputed with the yarn's standard "
                "count (Yarn master) in place of the day's SQC count; the standard line "
                "itself follows the count actually spun. It covers "
                f"{data['summary'].get('stdc_frame_shifts') or 0} of "
                f"{data['summary'].get('std_frame_shifts') or 0} frame-shifts with a "
                "standard (the rest have no standard count or no SQC count)."
            )
        if data["loss"]["bridge"]:
            notes.append(
                "Capacity figures are estimates: idle frame-shifts are costed at the "
                "period's average standard per frame-shift, and idle time includes "
                "planned stops (holidays, frames not commissioned) — the report cannot "
                "tell those from breakdowns."
            )
        notes.extend(notes_)
        data["meta"] = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": _iso(d_from),
            "date_to": _iso(d_to),
            "item_id": item_id,
            "shift": shift,
            "frame": frame,
            "notes": notes,
        }
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _frames_multi_quality(rows: list):
    """Yarn sets per (date, shift, frame) — more than one = a mid-shift change."""
    seen: dict = {}
    for r in rows:
        key = (_iso(r.get("doff_date")), _shift(r.get("shift")), r.get("machine_id"))
        seen.setdefault(key, set()).add(r.get("item_id"))
    return seen.values()
