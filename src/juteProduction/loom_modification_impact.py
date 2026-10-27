"""Loom Modification Impact report — GET /api/juteProductionReports/loom_modification_impact
and /loom_modification_impact_options.

Spec: docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md §5, §7, §8,
§9 (phase 2), trimmed on 2026-09-29 by owner instruction to an EFFICIENCY-ONLY
comparison (before vs after each stage, vs control) plus a breakdown / mechanical
stoppage context section. Gone: the metric registry and Auto fallback (picks / yds /
cuts / kg), quality mix, same-quality filter, waste + hands legs, extra output, cost,
the secondary figure, the stoppage tile and the Q flag. Portal persona: get_tenant_db
+ get_current_user_with_refresh, every response {"data": ...}, 400 with a plain message
for validation and the §8 caps.

Shape of the module
-------------------
``load_report_inputs(db, params)`` is the ONLY function that touches the database
(execute order below, so an ``execute.side_effect`` test can feed it); everything
after it is pure: ``build_report(inputs, params)`` returns the payload of the contract.
The endpoint test patches ``load_report_inputs``.

Execute order of load_report_inputs (one fetch each, in this order):
  R1  root of the requested stage id (get_modification_root_query)
  R2  chain events (get_chain_events_query)
  R3  chain snapshot rows (get_chain_snapshot_query)
  R4  last data date of the branch (get_last_data_date_query)
  R5  spans of the study looms across ALL chains (get_all_loom_spans_query)
  R6  branch row dates for the planning range (get_branch_row_dates_query)
  R7  producing loom-days of the study looms, planning range (get_producing_loomdays_query)
  R8  target-map mcid/speed rows, both roles (get_target_map_speed_rows_query)
  R9  control candidates: speed_band / same_dept -> get_eligible_control_looms_query;
      control_group -> get_control_group_query then get_control_group_members_query;
      none -> no read
  R10 spans of the control candidates (get_all_loom_spans_query), skipped when none
  R11 SHOW INDEX for the range-read hint (weaving_range_index_hint)
  R12 exact-day SQC pick rows of the span (get_pick_rows_query)
  R13 the range read (weaving_range_loomday_sql) — study + control ids in ONE statement
  R14 mechanical-reason stoppage hours of the study looms (get_mechanical_stoppage_query)
  R15 breakdowns of the study looms, failures only (get_breakdowns_query)

Method (§7.3) in one paragraph: per loom and stage, D_m = COALESCE(done_date,
mod_date), E_m = COALESCE(done_to_date, done_date, mod_to_date, mod_date); excluded =
[D_m, E_m + S]; the before window (INITIAL — or the loom's first stage in the chain
when it joined later) is [D_m - W, D_m - 1] extended backwards up to
IMPACT_MAX_WINDOW_DAYS until it holds the producing minima, never across a data hole;
after_k = [E_m,k + S + 1, min(E_m,k + S + W, next event of the loom in ANY chain - 1,
last data date)]. Loom-days inside another chain's excluded span are dropped.
Efficiency is a ratio of sums (Σ actual_measured x 100 / Σ capacity_measured, None
when the capacity sum is 0); the constant-yardstick efficiency multiplies each loom's
cap_per_rpm by the baseline speed in force on the first day of the before window
(actual role if > 0 else standard; last date, newest id) and is the verdict basis
when any study loom of the stage carries the S flag. Controls are un-modified
same-dept looms in the study loom's speed band (+/- CONTROL_SPEED_BAND_PCT), DiD per
band weighted by study loom-days. Verdict: producing minima, standard + hours
coverage >= COVERAGE_MIN_PCT and >= MIN_EFF_DAYS measurable days on both sides
(else NOT_ENOUGH_DATA with the cause named — no fallback metric), +/-
VERDICT_THRESHOLD_PCT relative, and |delta| >= SE_MULTIPLIER x SE of the day-level
series (pooled SD). The strip shows the latest stage that meets the minima.

Interpretations taken where the spec leaves room (also listed in meta.notes):
  * the §8 span cap is checked on the NOMINAL span (W before + excluded + W after
    over all stages, clipped to the last data date) before the producing-day
    extension, which is bounded by IMPACT_MAX_WINDOW_DAYS on its own;
  * the control set uses the UNION of the study looms' calendar windows per side
    (identical to the per-D_m pairing in the common single-date case);
  * a loom that joins the chain at a later stage ("not in previous stage") gets its
    before window from its own first stage;
  * per_loom rows compare the baseline with the verdict stage; their delta is on the
    verdict basis (baseline-standard efficiency when the stage carries flag S);
  * verdict.comparison (owner, 2026-09-29: "a comparison of the change in this loom
    set vs others where this was not done") is the judged stage's study and control
    Before / After figures side by side, on the verdict basis, from the same
    aggregates as the ladder rows of that stage — nothing is recomputed.
"""

import math
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    CONTROL_MIN_LOOMS,
    CONTROL_SPEED_BAND_PCT,
    COVERAGE_MIN_PCT,
    IMPACT_DATA_HOLE_DAYS,
    IMPACT_DEFAULT_WINDOW_DAYS,
    IMPACT_MAX_LOOMS,
    IMPACT_MAX_SPAN_DAYS,
    IMPACT_MAX_STUDY_LOOMS,
    IMPACT_MAX_WINDOW_DAYS,
    IMPACT_WINDOW_PRESETS,
    MIN_DAYS_PER_SIDE_GROUP,
    MIN_DAYS_PER_SIDE_LOOM,
    MIN_EFF_DAYS,
    MIN_LOOM_DAYS_GROUP,
    MIN_LOOM_DAYS_LOOM,
    MOD_CATEGORY_LABELS,
    MOD_DONE_BY_LABELS,
    MOD_KIND_LABELS,
    SE_MULTIPLIER,
    VERDICT_THRESHOLD_PCT,
    WEAVING_MACHINE_TYPE_NAME,
)
from src.juteProduction.loom_modification_impact_query import (
    get_all_loom_spans_query,
    get_branch_row_dates_query,
    get_breakdowns_query,
    get_chain_events_query,
    get_chain_snapshot_query,
    get_control_group_members_query,
    get_control_group_query,
    get_control_groups_query,
    get_eligible_control_looms_query,
    get_last_data_date_query,
    get_mechanical_stoppage_query,
    get_modification_root_query,
    get_pick_rows_query,
    get_producing_loomdays_query,
    get_root_modifications_query,
    get_target_map_speed_rows_query,
    pick_range_index_hint,
    weaving_range_index_hint,
    weaving_range_loomday_sql,
)

router = APIRouter()

CONTROL_MODES = ("speed_band", "same_dept", "control_group", "none")

FLAGS_LEGEND = {
    "S": "Effective standard speed (actual or standard role) changed inside a window",
    "H": "Hours coverage moved >= 20 pts between the windows",
    "W": "Dominant weaver share moved >= 20 pts or the weaver sets are disjoint",
    "D": "Coverage < 50 % (measurable efficiency on fewer than half the producing days)",
    "N": "No standard speed in force for the loom",
}
FLAG_SHIFT_PTS = 20.0
FLAG_D_COVERAGE_PCT = 50.0
SOURCE_NOTE = (
    "Recomputed live from jute_prod_weaving_daily through the range read; may differ "
    "from the locked DPR (frozen weaving log)."
)
KG_PER_OZ_YD = 28.35 / 1000.0


# =============================================================================
# Small helpers
# =============================================================================


def _f(v, default=0.0) -> float:
    if v is None:
        return default
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def _r(v, nd=2):
    return None if v is None else round(float(v) + 0.0, nd)


def _d(v) -> Optional[date]:
    """date / datetime / ISO string -> date."""
    if v is None:
        return None
    if isinstance(v, date):
        return v if type(v) is date else v.date()
    return date.fromisoformat(str(v)[:10])


def _iso(v) -> Optional[str]:
    return None if v is None else _d(v).isoformat()


def _pct(num, den, nd=2):
    return _r(num * 100.0 / den, nd) if den else None


def _delta_pct(before, after):
    if before is None or after is None or not before:
        return None
    return _r((after - before) * 100.0 / before, 2)


def _delta_pts(before, after):
    return None if before is None or after is None else _r(after - before, 3)


def _mapping(row) -> dict:
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


def _sd(values: list) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


# =============================================================================
# Params
# =============================================================================


def _int_param(request: Request, key: str, required=False, default=None):
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        if required:
            raise HTTPException(status_code=400, detail=f"{key} is required")
        return default
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def parse_report_params(request: Request) -> dict:
    """co_id, branch_id, modification_id, window_days, settle_days, control,
    control_group_id. The removed params (metric, same_quality_only,
    weaving_quality_id) are ignored when sent."""
    co_id = _int_param(request, "co_id", required=True)
    branch_id = _int_param(request, "branch_id", required=True)
    modification_id = _int_param(request, "modification_id", required=True)
    window_days = _int_param(request, "window_days", default=IMPACT_DEFAULT_WINDOW_DAYS)
    if window_days < 1:
        raise HTTPException(status_code=400, detail="window_days must be at least 1")
    if window_days > IMPACT_MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"window_days above the cap — at most {IMPACT_MAX_WINDOW_DAYS} days per side",
        )
    settle_days = _int_param(request, "settle_days", default=None)
    if settle_days is not None and settle_days < 0:
        raise HTTPException(status_code=400, detail="settle_days cannot be negative")
    control = (request.query_params.get("control") or "speed_band").strip().lower()
    if control not in CONTROL_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid control — one of {', '.join(CONTROL_MODES)}")
    control_group_id = _int_param(request, "control_group_id", default=None)
    if control == "control_group" and control_group_id is None:
        raise HTTPException(status_code=400, detail="control_group_id is required for control=control_group")
    return {
        "co_id": co_id,
        "branch_id": branch_id,
        "modification_id": modification_id,
        "window_days": window_days,
        "settle_days": settle_days,
        "control": control,
        "control_group_id": control_group_id,
    }


# =============================================================================
# Standards: speed index (target map, both roles) and exact-day picks
# =============================================================================


def build_speed_index(rows) -> dict:
    """{machine_id: {role: [(effective_date, id, value), ...] sorted}} from the
    mcid/speed rows of get_target_map_speed_rows_query (any row shape with
    machine_id / value_role / value / effective_date / weaving_target_map_id)."""
    idx: dict = defaultdict(lambda: {"actual": [], "standard": []})
    for r in rows:
        m = _mapping(r)
        role = str(m.get("value_role") or "").lower()
        if role not in ("actual", "standard"):
            continue
        idx[int(m["machine_id"])][role].append(
            (_d(m["effective_date"]), int(m.get("weaving_target_map_id") or 0), _f(m.get("value")))
        )
    for per_role in idx.values():
        for role in per_role:
            per_role[role].sort(key=lambda t: (t[0], t[1]))
    return dict(idx)


def _latest_row(rows: list, on_date: date):
    """Row with the last effective_date <= on_date, newest id on ties (the slice's
    ROW_NUMBER() ... ORDER BY effective_date DESC, weaving_target_map_id DESC)."""
    if not rows:
        return None
    keys = [t[0] for t in rows]
    i = bisect_right(keys, on_date)
    return rows[i - 1] if i else None


def resolve_speed(index: dict, machine_id: int, on_date: date):
    """The slice's eff_speed rule: the actual role if its resolved value > 0, else
    the standard role. Returns (value or None, role or None, effective_date or None)."""
    per_role = index.get(int(machine_id))
    if not per_role:
        return None, None, None
    act = _latest_row(per_role["actual"], on_date)
    if act is not None and act[2] > 0:
        return act[2], "actual", act[0]
    std = _latest_row(per_role["standard"], on_date)
    if std is not None:
        return (std[2] if std[2] > 0 else None), "standard", std[0]
    return None, None, None


def first_speed_in_window(index: dict, machine_id: int, d_from: date, d_to: date):
    """Earliest resolved speed effective inside [d_from, d_to] (the fallback when
    none is in force on d_from). Returns (value, effective_date) or (None, None)."""
    per_role = index.get(int(machine_id))
    if not per_role:
        return None, None
    dates = sorted({t[0] for role in per_role.values() for t in role if d_from <= t[0] <= d_to})
    for d in dates:
        v, _, _ = resolve_speed(index, machine_id, d)
        if v:
            return v, d
    return None, None


def speed_change_events(index: dict, machine_id: int, d_from: date, d_to: date) -> list:
    """Dates inside (d_from, d_to] where the EFFECTIVE speed changes, with old / new
    and the role that caused it: [(date, old, new, role), ...]."""
    per_role = index.get(int(machine_id))
    if not per_role:
        return []
    out = []
    prev, _, _ = resolve_speed(index, machine_id, d_from)
    cand = sorted({(t[0], role) for role, rows in per_role.items() for t in rows if d_from < t[0] <= d_to})
    seen = set()
    for d, role in cand:
        if d in seen:
            continue
        seen.add(d)
        cur, cur_role, _ = resolve_speed(index, machine_id, d)
        if cur != prev:
            out.append((d, prev, cur, cur_role or role))
            prev = cur
    return out


def build_picks_index(rows) -> dict:
    """{(weaving_quality_id, entry_date): AVG(picks)} over the exact-day readings."""
    acc: dict = defaultdict(lambda: [0.0, 0])
    for r in rows:
        m = _mapping(r)
        if m.get("picks") is None:
            continue
        k = (int(m["weaving_quality_id"]), _d(m["entry_date"]))
        acc[k][0] += _f(m["picks"])
        acc[k][1] += 1
    return {k: v[0] / v[1] for k, v in acc.items() if v[1]}


# =============================================================================
# Loom-days (stage 2 + 3 of the range read)
# =============================================================================


def quality_row_metrics(m: dict, eff_speed: Optional[float], std_picks: float) -> dict:
    """Stage 2: the slice's formulas on one loom-quality-day row.

    ``yds_manned`` is the yds of the manned spell rows only (total_jugar_manned) —
    the slice counts a spell row in the efficiency numerator only when that row has
    std_prod_yds > 0, i.e. working_hours > 0 (§7.3, dpr_reports "output that has a
    standard behind it"). eff_speed and std_picks are constant within the group, so
    std_prod_yds > 0 here <=> some row is manned with a speed and picks on the day.
    yds / kg are internal only (the producing-day rule and the parity harness); the
    report does not return them.
    """
    fl = _f(m.get("finished_length"))
    ozs = _f(m.get("ozs_yds"))
    njpc = _f(m.get("no_of_jugar_per_cut"))
    jugar = _f(m.get("total_jugar"))
    # manned jugar (spell rows with working_hours > 0); absent key (older callers /
    # tests) -> the whole row when the row itself is manned, else 0
    jugar_manned = m.get("total_jugar_manned")
    wh = _f(m.get("working_hours"))
    jugar_manned = _f(jugar_manned) if jugar_manned is not None else (jugar if wh > 0 else 0.0)
    yds = jugar * fl / njpc if njpc > 0 else 0.0
    yds_manned = jugar_manned * fl / njpc if njpc > 0 else 0.0
    kg = yds * ozs * KG_PER_OZ_YD
    speed = eff_speed or 0.0
    if std_picks > 0:
        cap_per_rpm = wh * 60.0 / (36.0 * std_picks)
        std_prod_yds = speed * cap_per_rpm
    else:
        cap_per_rpm = std_prod_yds = 0.0
    return {
        "yds": yds,
        "yds_manned": yds_manned,
        "kg": kg,
        "std_prod_yds": std_prod_yds,
        "cap_per_rpm": cap_per_rpm,
        "std_picks_present": std_picks > 0,
    }


def build_loom_days(quality_rows, speed_index: dict, picks_index: dict) -> list:
    """Stage 3: loom-quality-day rows (range SQL) -> loom-day dicts (§7.3 columns)."""
    by_key: dict = {}
    for r in quality_rows:
        m = _mapping(r)
        mid = int(m["machine_id"])
        qid = m.get("weaving_quality_id")
        qid = int(qid) if qid is not None else None
        d = _d(m["tran_date"])
        speed, _, _ = resolve_speed(speed_index, mid, d)
        std_picks = picks_index.get((qid, d), 0.0) if qid is not None else 0.0
        q = quality_row_metrics(m, speed, std_picks)
        ld = by_key.get((mid, d))
        if ld is None:
            ld = by_key[(mid, d)] = {
                "machine_id": mid, "tran_date": d,
                "is_control": bool(int(m.get("is_control") or 0)),
                "cuts": 0.0, "yds": 0.0, "kg": 0.0, "hours_run": 0.0,
                "actual_measured": 0.0, "capacity_measured": 0.0, "cap_per_rpm": 0.0,
                "actual_baseline": 0.0,
                "has_standard": False, "std_picks_present": False,
                "std_speed": speed, "rows_n": 0, "eb_ids": set(),
            }
        ld["cuts"] += _f(m.get("cuts"))
        ld["yds"] += q["yds"]
        ld["kg"] += q["kg"]
        ld["hours_run"] += _f(m.get("working_hours"))
        if q["std_prod_yds"] > 0:
            ld["actual_measured"] += q["yds_manned"]
            ld["capacity_measured"] += q["std_prod_yds"]
        if q["cap_per_rpm"] > 0:  # constant-yardstick numerator: manned rows with picks
            ld["actual_baseline"] += q["yds_manned"]
        ld["cap_per_rpm"] += q["cap_per_rpm"]
        ld["has_standard"] = ld["has_standard"] or bool(speed)
        ld["std_picks_present"] = ld["std_picks_present"] or q["std_picks_present"]
        ld["rows_n"] += int(m.get("rows_n") or 1)
        for eb in str(m.get("eb_ids") or "").split(","):
            eb = eb.strip()
            if eb:
                ld["eb_ids"].add(eb)
    out = []
    for ld in by_key.values():
        ld["days_run"] = 1 if ld["kg"] > 0 else 0
        out.append(ld)
    out.sort(key=lambda x: (x["machine_id"], x["tran_date"]))
    return out


# =============================================================================
# Windows (§5 segments per loom, §7.3 windows)
# =============================================================================


def stage_label(stage: dict, parent_kind: Optional[str] = None) -> str:
    kind = stage["mod_kind"]
    if kind == "REVERSAL":
        return "Reverted to original" if parent_kind in (None, "INITIAL") else "Tweak undone"
    return f"After-{stage['seq']} ({MOD_KIND_LABELS.get(kind, kind)})"


def build_stages(events, snapshot_rows, settle_override: Optional[int] = None) -> list:
    """Chain events (date order) + snapshot rows -> stage dicts with per-loom (D, E)."""
    looms_by_event: dict = defaultdict(dict)
    loom_info: dict = {}
    for r in snapshot_rows:
        m = _mapping(r)
        mid = int(m["machine_id"])
        looms_by_event[int(m["modification_id"])][mid] = (_d(m["d_m"]), _d(m["e_m"]))
        loom_info[mid] = {
            "machine_id": mid,
            "loom": m.get("machine_name"),
            "line_no": m.get("line_no"),
            "dept_id": m.get("dept_id"),
            "machine_active": int(m.get("machine_active") if m.get("machine_active") is not None else 1),
        }
    stages = []
    by_id = {}
    for seq, r in enumerate(events, start=1):
        m = _mapping(r)
        mid_ev = int(m["modification_id"])
        st = {
            "seq": seq,
            "modification_id": mid_ev,
            "mod_kind": m.get("mod_kind") or "INITIAL",
            "mod_kind_label": MOD_KIND_LABELS.get(m.get("mod_kind") or "INITIAL", m.get("mod_kind")),
            "mod_date": _d(m["mod_date"]),
            "mod_to_date": _d(m.get("mod_to_date")),
            "settle_days": int(settle_override if settle_override is not None else (m.get("settle_days") or 0)),
            "event_settle_days": int(m.get("settle_days") or 0),
            "title": m.get("title"),
            "mod_category": m.get("mod_category"),
            "mod_category_label": MOD_CATEGORY_LABELS.get(m.get("mod_category"), m.get("mod_category")),
            "done_by": m.get("done_by"),
            "done_by_label": MOD_DONE_BY_LABELS.get(m.get("done_by"), m.get("done_by")),
            "parent_modification_id": m.get("parent_modification_id"),
            "loom_group_id": m.get("loom_group_id"),
            "group_code": m.get("group_code"),
            "group_name": m.get("group_name"),
            "looms": looms_by_event.get(mid_ev, {}),
        }
        by_id[mid_ev] = st
        stages.append(st)
    for st in stages:
        parent = by_id.get(st["parent_modification_id"]) if st["parent_modification_id"] else None
        st["parent_seq"] = parent["seq"] if parent else None
        st["parent_kind"] = parent["mod_kind"] if parent else None
        st["label"] = stage_label(st, st["parent_kind"])
    return stages, loom_info


def compute_loom_windows(stages: list, all_spans: dict, window_days: int,
                         last_data_date: Optional[date], root_id: int,
                         extension_days: int = 0) -> dict:
    """Per loom: before window, per-stage excluded / after windows, other-chain drops.

    ``all_spans`` = {machine_id: [span dict(d_m, e_m, settle_days, root_modification_id,
    modification_id, title, window_days?)]} across ALL chains (R5). ``extension_days``
    is the producing-day extension already decided (0 first pass).
    Returns {machine_id: {"before": (from, to) | None, "stages": {seq: {...}},
                          "drops": [(from, to)], "other_after": [(from, to, title)]}}.
    """
    W = window_days
    out = {}
    loom_stage_seqs: dict = defaultdict(list)
    for st in stages:
        for mid in st["looms"]:
            loom_stage_seqs[mid].append(st["seq"])
    for mid, seqs in loom_stage_seqs.items():
        seqs.sort()
        first = stages[seqs[0] - 1]
        d_first, _ = first["looms"][mid]
        before = (d_first - timedelta(days=W + extension_days), d_first - timedelta(days=1))
        spans = sorted(all_spans.get(mid, []), key=lambda s: s["d_m"])
        drops, other_after = [], []
        for s in spans:
            if s.get("root_modification_id") == root_id:
                continue
            S = int(s.get("settle_days") or 0)
            drops.append((s["d_m"], s["e_m"] + timedelta(days=S)))
            other_after.append((s["e_m"] + timedelta(days=S + 1), s["e_m"] + timedelta(days=S + W), s.get("title")))
        per_stage = {}
        for seq in seqs:
            st = stages[seq - 1]
            d_k, e_k = st["looms"][mid]
            S = st["settle_days"]
            excl = (d_k, e_k + timedelta(days=S))
            after_from = e_k + timedelta(days=S + 1)
            after_to = e_k + timedelta(days=S + W)
            cut_reason = "window"
            nxt = [s["d_m"] for s in spans if s["d_m"] > e_k and s["modification_id"] != st["modification_id"]]
            if nxt:
                cand = min(nxt) - timedelta(days=1)
                if cand < after_to:
                    after_to, cut_reason = cand, "next_event"
            if last_data_date is not None and last_data_date < after_to:
                after_to, cut_reason = last_data_date, "last_data_date"
            per_stage[seq] = {
                "excluded": excl,
                "after": (after_from, after_to) if after_to >= after_from else None,
                "cut_reason": cut_reason,
            }
        out[mid] = {"before": before, "stages": per_stage, "drops": drops, "other_after": other_after}
    return out


def _in(d: date, win) -> bool:
    return win is not None and win[0] <= d <= win[1]


def phase_of(loom_win: dict, d: date):
    """('BEFORE', None) | ('EXCLUDED', seq) | ('AFTER', seq) | ('DROPPED', None) | None."""
    if loom_win is None:
        return None
    for lo, hi in loom_win["drops"]:
        if lo <= d <= hi:
            return ("DROPPED", None)
    for seq, w in loom_win["stages"].items():
        if _in(d, w["excluded"]):
            return ("EXCLUDED", seq)
    for seq, w in loom_win["stages"].items():
        if _in(d, w["after"]):
            return ("AFTER", seq)
    if _in(d, loom_win["before"]):
        return ("BEFORE", None)
    return None


def _row_date(x) -> Optional[date]:
    """tran_date of one branch-row-dates row: a plain date, a dict (``_rows``), or a Row."""
    if isinstance(x, dict):
        return _d(x.get("tran_date"))
    if hasattr(x, "_mapping"):
        return _d(x._mapping["tran_date"])
    return _d(x)


def find_data_holes(row_dates, d_from: date, d_to: date, min_len: int = IMPACT_DATA_HOLE_DAYS) -> list:
    """Runs of >= min_len consecutive dates in [d_from, d_to] with no branch row."""
    present = {_row_date(x) for x in row_dates}
    holes, run_start, d = [], None, d_from
    while d <= d_to:
        if d in present:
            if run_start is not None and (d - run_start).days >= min_len:
                holes.append((run_start, d - timedelta(days=1)))
            run_start = None
        elif run_start is None:
            run_start = d
        d += timedelta(days=1)
    if run_start is not None and (d - run_start).days >= min_len:
        holes.append((run_start, d - timedelta(days=1)))
    return holes


def producing_minima(n_looms: int):
    if n_looms <= 1:
        return MIN_LOOM_DAYS_LOOM, MIN_DAYS_PER_SIDE_LOOM
    return MIN_LOOM_DAYS_GROUP, MIN_DAYS_PER_SIDE_GROUP


def count_before(loom_windows: dict, producing: set):
    """(producing loom-days, producing days) inside the before windows minus drops."""
    loom_days, days = 0, set()
    for mid, w in loom_windows.items():
        if w["before"] is None:
            continue
        lo, hi = w["before"]
        for d in _dates_between(lo, hi):
            if (mid, d) in producing and not any(a <= d <= b for a, b in w["drops"]):
                loom_days += 1
                days.add(d)
    return loom_days, len(days)


def _dates_between(lo: date, hi: date):
    d = lo
    while d <= hi:
        yield d
        d += timedelta(days=1)


def decide_extension(stages: list, all_spans: dict, window_days: int, last_data_date,
                     root_id: int, producing: set, holes: list) -> dict:
    """Producing-day rule: smallest extension (days) of the before window that meets
    the group minima, bounded by IMPACT_MAX_WINDOW_DAYS and stopped at a data hole.
    Returns {"extension_days", "met", "loom_days", "days", "stopped_by_hole",
    "capped"}.

    Counted incrementally: the nominal windows are counted once (count_before), then
    each 1-day step only tests the single new date per loom (nominal start - ext), so
    a step is O(looms) instead of O(looms x window) — the thin-data case is exactly
    where the loop runs long.
    """
    n_looms = len({m for st in stages for m in st["looms"]})
    min_ld, min_days = producing_minima(n_looms)
    base = compute_loom_windows(stages, all_spans, window_days, last_data_date, root_id, 0)
    nominal_start = min((w["before"][0] for w in base.values() if w["before"]), default=None)
    max_ext = max(0, IMPACT_MAX_WINDOW_DAYS - window_days)
    hole_limit = None
    if nominal_start is not None:
        for lo, hi in holes:
            if hi < nominal_start:
                lim = (nominal_start - (hi + timedelta(days=1))).days
                hole_limit = lim if hole_limit is None else min(hole_limit, lim)
    ld, _ = count_before(base, producing)
    day_hits: dict = defaultdict(int)  # date -> producing looms on it (inside windows)
    for mid, w in base.items():
        if w["before"] is None:
            continue
        lo, hi = w["before"]
        for d in _dates_between(lo, hi):
            if (mid, d) in producing and not any(a <= d <= b for a, b in w["drops"]):
                day_hits[d] += 1
    days = len(day_hits)
    ext, stopped_by_hole, capped = 0, False, False
    while (ld < min_ld or days < min_days) and ext < max_ext:
        if hole_limit is not None and ext >= hole_limit:
            stopped_by_hole = True
            break
        ext += 1
        for mid, w in base.items():  # only the newly uncovered date per loom
            if w["before"] is None:
                continue
            d = w["before"][0] - timedelta(days=ext)
            if (mid, d) in producing and not any(a <= d <= b for a, b in w["drops"]):
                ld += 1
                day_hits[d] += 1
        days = len(day_hits)
    if (ld < min_ld or days < min_days) and ext >= max_ext and not stopped_by_hole:
        capped = True
    return {"extension_days": ext, "met": ld >= min_ld and days >= min_days,
            "loom_days": ld, "days": days, "stopped_by_hole": stopped_by_hole, "capped": capped,
            "min_loom_days": min_ld, "min_days": min_days}


# =============================================================================
# Aggregates (§7.3) — efficiency only
# =============================================================================


def aggregate(loom_days: list, baseline_speeds: dict) -> dict:
    """Ratio-of-sums over a set of loom-days. ``baseline_speeds`` {machine_id: rpm}
    feeds the constant-yardstick efficiency. yds / kg are carried internally
    (producing-day rule) and never returned."""
    a = {
        "looms_n": len({ld["machine_id"] for ld in loom_days}),
        "loom_days": len(loom_days),
        "days_run": 0, "measured_days": 0, "hours_days": 0,
        "unmanned_days": 0, "no_pick_days": 0, "no_standard_days": 0,
        "yds": 0.0, "kg": 0.0, "hours_run": 0.0,
        "actual_measured": 0.0, "capacity_measured": 0.0, "baseline_capacity": 0.0,
        "actual_baseline": 0.0,
    }
    days_run_dates, eff_dates = set(), defaultdict(float)
    for ld in loom_days:
        a["yds"] += ld["yds"]
        a["kg"] += ld["kg"]
        a["hours_run"] += ld["hours_run"]
        a["actual_measured"] += ld["actual_measured"]
        a["capacity_measured"] += ld["capacity_measured"]
        bs = baseline_speeds.get(ld["machine_id"])
        if bs and ld["cap_per_rpm"] > 0:
            a["baseline_capacity"] += bs * ld["cap_per_rpm"]
            a["actual_baseline"] += ld.get("actual_baseline", ld["yds"])
        if ld["days_run"]:
            a["days_run"] += 1
            days_run_dates.add(ld["tran_date"])
            if ld["capacity_measured"] > 0:
                a["measured_days"] += 1
            if ld["hours_run"] > 0:
                a["hours_days"] += 1
            else:
                a["unmanned_days"] += 1
            if not ld["std_picks_present"]:
                a["no_pick_days"] += 1
            if not ld["has_standard"]:
                a["no_standard_days"] += 1
        eff_dates[ld["tran_date"]] += ld["capacity_measured"]
    a["producing_days"] = len(days_run_dates)
    a["eff_days"] = sum(1 for v in eff_dates.values() if v > 0)
    a["efficiency_pct"] = _pct(a["actual_measured"], a["capacity_measured"])
    a["efficiency_at_baseline_std_pct"] = _pct(a["actual_baseline"], a["baseline_capacity"])
    a["std_coverage_pct"] = _pct(a["measured_days"], a["days_run"])
    a["hours_coverage_pct"] = _pct(a["hours_days"], a["days_run"])
    return a


def eff_value(agg: Optional[dict], use_baseline_std: bool = False):
    """The verdict basis of an aggregate: efficiency, or the constant-yardstick
    efficiency when the stage carries flag S."""
    if agg is None:
        return None
    return agg.get("efficiency_at_baseline_std_pct") if use_baseline_std else agg.get("efficiency_pct")


def day_series(loom_days: list, baseline_speeds: dict, use_baseline_std=False) -> list:
    """Day-level efficiency series (ratio of sums per date) — the unit of the SE
    rule. Only dates with a defined value are returned: [(date, value), ...]."""
    by_date: dict = defaultdict(list)
    for ld in loom_days:
        by_date[ld["tran_date"]].append(ld)
    out = []
    for d in sorted(by_date):
        v = eff_value(aggregate(by_date[d], baseline_speeds), use_baseline_std)
        if v is not None:
            out.append((d, v))
    return out


def pooled_se(s1_vals: list, s2_vals: list):
    """(n1, n2, s_p, SE) — t-like pooled SD over the two day-level series."""
    n1, n2 = len(s1_vals), len(s2_vals)
    s1, s2 = _sd(s1_vals), _sd(s2_vals)
    if n1 + n2 - 2 <= 0 or s1 is None and s2 is None:
        return n1, n2, None, None
    num = ((n1 - 1) * (s1 or 0.0) ** 2 if n1 > 1 else 0.0) + ((n2 - 1) * (s2 or 0.0) ** 2 if n2 > 1 else 0.0)
    s_p = math.sqrt(num / (n1 + n2 - 2))
    se = s_p * math.sqrt((1.0 / n1 if n1 else 0.0) + (1.0 / n2 if n2 else 0.0)) if n1 and n2 else None
    return n1, n2, s_p, se


def efficiency_cause(agg: dict) -> str:
    """Names why efficiency is unmeasured on producing loom-days of one side —
    'no weaver marked on N loom-days, no SQC pick reading on N, no standard speed on
    N' — for the NOT_ENOUGH_DATA reason / note."""
    parts = []
    if agg.get("unmanned_days"):
        parts.append(f"no weaver marked on {agg['unmanned_days']} loom-days")
    if agg.get("no_pick_days"):
        parts.append(f"no SQC pick reading on {agg['no_pick_days']} loom-days")
    if agg.get("no_standard_days"):
        parts.append(f"no standard speed on {agg['no_standard_days']} loom-days")
    return ", ".join(parts)


def efficiency_data_reasons(before: dict, after: dict) -> list:
    """Efficiency-only minima (no fallback metric): standard and hours coverage >=
    COVERAGE_MIN_PCT, >= MIN_EFF_DAYS measurable days and a defined value on both
    sides. Each reason names the side and the cause."""
    reasons = []
    for side, agg in (("before", before), ("after", after)):
        cause = efficiency_cause(agg)
        tail = f" ({cause})" if cause else ""
        sc, hc = agg.get("std_coverage_pct"), agg.get("hours_coverage_pct")
        if sc is None or sc < COVERAGE_MIN_PCT:
            reasons.append(f"{side}: standard coverage {sc or 0:g} % < {COVERAGE_MIN_PCT:g} %{tail}")
        elif hc is None or hc < COVERAGE_MIN_PCT:
            reasons.append(f"{side}: hours coverage {hc or 0:g} % < {COVERAGE_MIN_PCT:g} %{tail}")
        if agg.get("eff_days", 0) < MIN_EFF_DAYS:
            reasons.append(f"{side}: {agg.get('eff_days', 0)} days with measurable efficiency < {MIN_EFF_DAYS}")
    return reasons


def decide_verdict(before: dict, after: dict, se: Optional[float],
                   n_looms_total: int, looms_meeting: int,
                   net_delta_pct: Optional[float] = None, se_net: Optional[float] = None,
                   net_delta_units: Optional[float] = None,
                   use_baseline_std: bool = False) -> dict:
    """§7.3 verdict for one stage on efficiency alone. Returns dict(verdict, reason,
    before, after, delta_pts, delta_pct, within_noise, se)."""
    min_ld, min_days = producing_minima(n_looms_total)
    b_val = eff_value(before, use_baseline_std)
    a_val = eff_value(after, use_baseline_std)
    reasons = []
    for side, agg in (("before", before), ("after", after)):
        if agg["days_run"] < min_ld:
            reasons.append(f"{side}: {agg['days_run']} producing loom-days < {min_ld}")
        if agg["producing_days"] < min_days:
            reasons.append(f"{side}: {agg['producing_days']} producing days < {min_days}")
    reasons.extend(efficiency_data_reasons(before, after))
    if n_looms_total > 1 and looms_meeting * 2 < n_looms_total:
        reasons.append(f"only {looms_meeting} of {n_looms_total} looms meet the per-loom minimum")
    if b_val is None or a_val is None:
        reasons.append("efficiency not measurable on both sides")
    delta = _delta_pts(b_val, a_val)
    delta_pct = _delta_pct(b_val, a_val)
    thr = VERDICT_THRESHOLD_PCT
    if reasons:
        return {"verdict": "NOT_ENOUGH_DATA", "reason": "; ".join(reasons), "before": b_val, "after": a_val,
                "delta_pts": delta, "delta_pct": delta_pct, "within_noise": None, "se": se}
    within_noise = se is not None and abs(delta) < SE_MULTIPLIER * se
    if se is None:
        within_noise = False
    sign_ok = None
    if net_delta_pct is not None and se_net is not None and net_delta_units is not None:
        sign_ok = abs(net_delta_units) >= SE_MULTIPLIER * se_net and (net_delta_units > 0) == (delta > 0)
    if delta_pct is not None and delta_pct >= thr and not within_noise and (sign_ok is None or sign_ok):
        v, reason = "IMPROVED", f"+{delta_pct} % >= {thr:g} % and beyond {SE_MULTIPLIER:g} x SE"
    elif delta_pct is not None and delta_pct <= -thr and not within_noise and (sign_ok is None or sign_ok):
        v, reason = "WORSE", f"{delta_pct} % <= -{thr:g} % and beyond {SE_MULTIPLIER:g} x SE"
    else:
        if within_noise:
            reason = f"|delta| {abs(delta)} within {SE_MULTIPLIER:g} x SE ({_r(se, 3)}) — day-to-day variation"
        elif sign_ok is False:
            reason = "net change vs control not beyond noise or of the opposite sign"
        else:
            reason = f"|delta %| below the {thr:g} % threshold"
        v = "NO_CLEAR_CHANGE"
    if net_delta_pct is not None:
        reason += f"; net vs control {net_delta_pct:+} %"
    return {"verdict": v, "reason": reason, "before": b_val, "after": a_val, "delta_pts": delta,
            "delta_pct": delta_pct, "within_noise": within_noise, "se": se}


# =============================================================================
# Controls (§7.3 D5)
# =============================================================================


def band_key(speed: Optional[float], loom_name: Optional[str]):
    """Band of a loom: its baseline speed (rounded rpm) or, without one, the letters
    before the digit run of its name."""
    if speed:
        return ("rpm", int(round(speed)))
    name = str(loom_name or "")
    prefix = ""
    for ch in name:
        if ch.isdigit():
            break
        prefix += ch
    return ("prefix", prefix.strip().upper())


def band_matches(study_band, ctrl_speed: Optional[float], ctrl_name: Optional[str]) -> Optional[float]:
    """Distance (relative, 0 = exact) when the control fits the study band, else None."""
    kind, val = study_band
    if kind == "rpm":
        if not ctrl_speed or not val:
            return None
        rel = abs(ctrl_speed - val) / float(val)
        return rel if rel * 100.0 <= CONTROL_SPEED_BAND_PCT else None
    if ctrl_speed:
        return None
    return 0.0 if band_key(None, ctrl_name) == study_band else None


def select_controls(candidates: list, study_bands: dict, speed_index: dict, on_date: date,
                    control_spans: dict, span: tuple, window_days: int, study_ids: set,
                    max_controls: int, require_band: bool = True) -> dict:
    """Eligible controls in band, trimmed to ``max_controls`` by closest band.

    ``candidates`` = [{machine_id, machine_name, ...}], ``study_bands`` = {study
    machine_id: band}, ``control_spans`` = {machine_id: [span dicts]} across all
    chains. Returns {"selected": [candidate dict + speed/band/distance],
    "dropped": [(name, reason)], "trimmed": n}.
    """
    span_from, span_to = span
    selected, dropped = [], []
    bands = set(study_bands.values())
    for c in candidates:
        mid = int(c["machine_id"])
        if mid in study_ids:
            dropped.append((c.get("machine_name"), "in the study"))
            continue
        conflict = None
        for s in control_spans.get(mid, []):
            S = int(s.get("settle_days") or 0)
            lo = s["d_m"] - timedelta(days=window_days)
            hi = s["e_m"] + timedelta(days=S + window_days)
            if lo <= span_to and hi >= span_from:
                conflict = s
                break
        if conflict is not None:
            dropped.append((c.get("machine_name"), f"modified {conflict['d_m'].strftime('%d-%b')} ({conflict.get('title')})"))
            continue
        speed, _, _ = resolve_speed(speed_index, mid, on_date)
        dist = None
        if require_band:
            for b in bands:
                dd = band_matches(b, speed, c.get("machine_name"))
                if dd is not None and (dist is None or dd < dist):
                    dist = dd
            if dist is None:
                dropped.append((c.get("machine_name"), "outside every study speed band"))
                continue
        else:
            dist = 0.0
        selected.append({**c, "machine_id": mid, "speed": speed, "distance": dist,
                         "band": band_key(speed, c.get("machine_name"))})
    trimmed = 0
    if len(selected) > max_controls:
        selected.sort(key=lambda c: (c["distance"], str(c.get("machine_name"))))
        trimmed = len(selected) - max_controls
        selected = selected[:max_controls]
    return {"selected": selected, "dropped": dropped, "trimmed": trimmed}


def did_by_band(study_looms: dict, control_looms: dict, before_days: dict, after_days: dict,
                baseline_speeds: dict, use_baseline_std=False) -> dict:
    """Difference-in-differences of efficiency per band, combined weighted by study
    loom-days.

    ``study_looms`` / ``control_looms`` = {machine_id: band}; ``before_days`` /
    ``after_days`` = {machine_id: [loom-day dicts]}. Returns {"net_delta_pct",
    "bands": [...]}."""
    by_band: dict = defaultdict(lambda: {"study": [], "control": []})
    for mid, b in study_looms.items():
        by_band[b]["study"].append(mid)
    for mid, b in control_looms.items():
        for sb in by_band:
            if sb == b or (sb[0] == "rpm" and b[0] == "rpm" and band_matches(sb, float(b[1]), None) is not None):
                by_band[sb]["control"].append(mid)
    bands_out, wsum, wtotal = [], 0.0, 0.0
    for b, members in by_band.items():
        s_b = [ld for mid in members["study"] for ld in before_days.get(mid, [])]
        s_a = [ld for mid in members["study"] for ld in after_days.get(mid, [])]
        c_b = [ld for mid in members["control"] for ld in before_days.get(mid, [])]
        c_a = [ld for mid in members["control"] for ld in after_days.get(mid, [])]
        sd = _delta_pct(eff_value(aggregate(s_b, baseline_speeds), use_baseline_std),
                        eff_value(aggregate(s_a, baseline_speeds), use_baseline_std))
        cd = _delta_pct(eff_value(aggregate(c_b, baseline_speeds), use_baseline_std),
                        eff_value(aggregate(c_a, baseline_speeds), use_baseline_std))
        w = float(len(s_b) + len(s_a))
        row = {"band": f"{b[1]} rpm" if b[0] == "rpm" else f"prefix {b[1] or '-'}",
               "study_looms": len(members["study"]), "control_looms": len(members["control"]),
               "study_delta_pct": sd, "control_delta_pct": cd,
               "did_pct": None if sd is None or cd is None else _r(sd - cd, 2), "weight": w}
        bands_out.append(row)
        if row["did_pct"] is not None and w > 0:
            wsum += row["did_pct"] * w
            wtotal += w
    return {"net_delta_pct": _r(wsum / wtotal, 2) if wtotal else None, "bands": bands_out}


# =============================================================================
# Flags (§7.2 C) — S / H / W / D / N
# =============================================================================


def _dominant_share(counter: dict):
    total = sum(counter.values())
    if not total:
        return None, 0.0
    k, v = max(counter.items(), key=lambda kv: kv[1])
    return k, v * 100.0 / total


def loom_flags(before_days: list, after_days: list, speed_changed: bool,
               has_standard: bool) -> list:
    flags = []
    if speed_changed:
        flags.append("S")
    wb, wa = defaultdict(int), defaultdict(int)
    for ld in before_days:
        for eb in ld["eb_ids"]:
            wb[eb] += 1
    for ld in after_days:
        for eb in ld["eb_ids"]:
            wa[eb] += 1
    ab, aa = aggregate(before_days, {}), aggregate(after_days, {})
    hb, ha = ab.get("hours_coverage_pct"), aa.get("hours_coverage_pct")
    if hb is not None and ha is not None and abs(hb - ha) >= FLAG_SHIFT_PTS:
        flags.append("H")
    if wb and wa:
        kb, sb = _dominant_share(wb)
        sa_same = wa.get(kb, 0) * 100.0 / sum(wa.values())
        if not (set(wb) & set(wa)) or abs(sb - sa_same) >= FLAG_SHIFT_PTS:
            flags.append("W")
    covs = [c for c in (ab.get("std_coverage_pct"), aa.get("std_coverage_pct")) if c is not None]
    if covs and min(covs) < FLAG_D_COVERAGE_PCT:
        flags.append("D")
    if not has_standard:
        flags.append("N")
    return flags


# =============================================================================
# Mechanical context (breakdowns + mechanical-reason stoppage), per study loom
# =============================================================================


def build_mechanical(mech_stoppage, breakdowns, windows: dict, study_ids, loom_info: dict) -> list:
    """Per study loom, BEFORE | AFTER: mechanical-reason stoppage hours
    (jute_prod_stoppage_hours, reason_code 'mechanical'), breakdown count and
    minutes (jute_mech_breakdown, failures only); then one 'total' row per side.

    A loom-day counts on the side of its phase (before window / any after window of
    the chain — the section is context, not the verdict); excluded, dropped and
    out-of-window dates are ignored, as are looms outside the study."""
    def side_of(mid, d):
        ph = phase_of(windows.get(mid), _d(d))
        if ph is None or ph[0] in ("DROPPED", "EXCLUDED"):
            return None
        return "BEFORE" if ph[0] == "BEFORE" else "AFTER"

    acc: dict = {}
    for mid in study_ids:
        for side in ("BEFORE", "AFTER"):
            acc[(mid, side)] = {"mech_stoppage_hours": 0.0, "breakdown_count": 0, "breakdown_minutes": 0}
    for r in mech_stoppage or []:
        m = _mapping(r)
        mid = int(m["machine_id"])
        s = side_of(mid, m["tran_date"])
        if s and (mid, s) in acc:
            acc[(mid, s)]["mech_stoppage_hours"] += _f(m.get("stoppage_hours"))
    for r in breakdowns or []:
        m = _mapping(r)
        mid = int(m["machine_id"])
        s = side_of(mid, m["tran_date"])
        if s and (mid, s) in acc:
            acc[(mid, s)]["breakdown_count"] += int(m.get("breakdown_count") or 0)
            acc[(mid, s)]["breakdown_minutes"] += int(_f(m.get("breakdown_minutes")))
    out = []
    totals = {"BEFORE": {"mech_stoppage_hours": 0.0, "breakdown_count": 0, "breakdown_minutes": 0},
              "AFTER": {"mech_stoppage_hours": 0.0, "breakdown_count": 0, "breakdown_minutes": 0}}
    for mid in study_ids:
        for side in ("BEFORE", "AFTER"):
            a = acc[(mid, side)]
            out.append({"machine_id": mid, "loom": loom_info[mid]["loom"], "side": side,
                        "mech_stoppage_hours": _r(a["mech_stoppage_hours"], 2),
                        "breakdown_count": a["breakdown_count"], "breakdown_minutes": a["breakdown_minutes"],
                        "row_kind": "loom"})
            for k in totals[side]:
                totals[side][k] += a[k]
    for side in ("BEFORE", "AFTER"):
        t = totals[side]
        out.append({"machine_id": None, "loom": "TOTAL", "side": side,
                    "mech_stoppage_hours": _r(t["mech_stoppage_hours"], 2),
                    "breakdown_count": t["breakdown_count"], "breakdown_minutes": t["breakdown_minutes"],
                    "row_kind": "total"})
    return out


# =============================================================================
# DB loader (the only function that touches the session)
# =============================================================================


def _rows(res) -> list:
    return [_mapping(r) for r in res.fetchall()]


def load_report_inputs(db: Session, p: dict) -> dict:
    """R1..R15 of the module docstring. Raises 400 on unknown ids / caps that can be
    decided before the range read."""
    co_id, branch_id = p["co_id"], p["branch_id"]
    W = p["window_days"]
    root_row = db.execute(get_modification_root_query(), {"modification_id": p["modification_id"], "co_id": co_id}).fetchone()
    if root_row is None:
        raise HTTPException(status_code=400, detail="modification not found for this company")
    root_m = _mapping(root_row)
    root_id = int(root_m.get("root_modification_id") or root_m["modification_id"])
    events = _rows(db.execute(get_chain_events_query(), {"root_modification_id": root_id, "co_id": co_id}))
    if not events:
        raise HTTPException(status_code=400, detail="modification chain has no active stages")
    snapshot = _rows(db.execute(get_chain_snapshot_query(), {"root_modification_id": root_id, "co_id": co_id}))
    study_ids = sorted({int(r["machine_id"]) for r in snapshot})
    if not study_ids:
        raise HTTPException(status_code=400, detail="the chain has no looms in its snapshot")
    if len(study_ids) > IMPACT_MAX_STUDY_LOOMS:
        raise HTTPException(status_code=400, detail=f"study looms above the cap — at most {IMPACT_MAX_STUDY_LOOMS} looms")
    ldd = db.execute(get_last_data_date_query(), {"co_id": co_id, "branch_id": branch_id}).fetchone()
    last_data_date = _d(_mapping(ldd).get("last_data_date")) if ldd is not None else None
    stages, loom_info = build_stages(events, snapshot, p["settle_days"])
    study_spans = _rows(db.execute(get_all_loom_spans_query(), {"co_id": co_id, "machine_ids": study_ids}))
    d_first = min(st["mod_date"] for st in stages)
    plan_from = d_first - timedelta(days=IMPACT_MAX_WINDOW_DAYS)
    plan_to = last_data_date or date.today()
    row_dates = _rows(db.execute(get_branch_row_dates_query(), {"co_id": co_id, "branch_id": branch_id, "date_from": plan_from, "date_to": plan_to}))
    producing = _rows(db.execute(get_producing_loomdays_query(), {"co_id": co_id, "branch_id": branch_id, "machine_ids": study_ids, "date_from": plan_from, "date_to": plan_to}))
    speed_rows = _rows(db.execute(get_target_map_speed_rows_query(), {"co_id": co_id}))

    control_candidates, control_group = [], None
    if p["control"] in ("speed_band", "same_dept"):
        dept_ids = sorted({int(v["dept_id"]) for v in loom_info.values() if v.get("dept_id") is not None})
        if dept_ids:
            control_candidates = _rows(db.execute(get_eligible_control_looms_query(), {
                "co_id": co_id, "loom_type": WEAVING_MACHINE_TYPE_NAME, "dept_ids": dept_ids, "exclude_ids": study_ids}))
    elif p["control"] == "control_group":
        g = db.execute(get_control_group_query(), {"loom_group_id": p["control_group_id"], "co_id": co_id}).fetchone()
        if g is None:
            raise HTTPException(status_code=400, detail="control group not found for this company")
        control_group = _mapping(g)
        control_candidates = _rows(db.execute(get_control_group_members_query(), {"loom_group_id": p["control_group_id"]}))
    control_ids = sorted({int(c["machine_id"]) for c in control_candidates})
    control_spans = _rows(db.execute(get_all_loom_spans_query(), {"co_id": co_id, "machine_ids": control_ids})) if control_ids else []

    # Windows decide the span of the range read (nominal, then extension).
    all_spans = _spans_by_loom(study_spans + control_spans)
    holes = find_data_holes(row_dates, plan_from, plan_to)
    producing_set = {(int(r["machine_id"]), _d(r["tran_date"])) for r in producing}
    ext = decide_extension(stages, all_spans, W, last_data_date, root_id, producing_set, holes)
    nominal = compute_loom_windows(stages, all_spans, W, last_data_date, root_id, 0)
    span_from_nominal = min(w["before"][0] for w in nominal.values() if w["before"])
    span_to = max((w["after"][1] if w["after"] else w["excluded"][1])
                  for lw in nominal.values() for w in lw["stages"].values())
    nominal_days = (span_to - span_from_nominal).days + 1
    if nominal_days > IMPACT_MAX_SPAN_DAYS:
        raise HTTPException(status_code=400, detail=(
            f"total span above the cap — {nominal_days} days (before + excluded + after over all stages), "
            f"at most {IMPACT_MAX_SPAN_DAYS}; reduce window_days"))
    windows = compute_loom_windows(stages, all_spans, W, last_data_date, root_id, ext["extension_days"])
    span_from = min(w["before"][0] for w in windows.values() if w["before"])

    hint = pick_range_index_hint([_mapping(r).get("Key_name") for r in db.execute(weaving_range_index_hint()).fetchall()])
    machines = sorted(set(study_ids) | set(control_ids))
    binds = {"co_id": co_id, "branch_id": branch_id, "date_from": span_from, "date_to": span_to}
    picks = _rows(db.execute(get_pick_rows_query(), binds))
    t0 = time.perf_counter()
    quality_rows = _rows(db.execute(weaving_range_loomday_sql(hint), {**binds, "machines": machines, "control_machines": control_ids}))
    range_ms = int((time.perf_counter() - t0) * 1000)
    legs = {"machine_ids": study_ids, **binds}
    mech_stoppage = _rows(db.execute(get_mechanical_stoppage_query(), legs))
    breakdowns = _rows(db.execute(get_breakdowns_query(), legs))
    return {
        "root_id": root_id, "events": events, "snapshot": snapshot, "stages": stages, "loom_info": loom_info,
        "study_ids": study_ids, "last_data_date": last_data_date, "all_spans": all_spans,
        "row_dates": row_dates, "holes": holes, "producing": producing_set, "extension": ext,
        "windows": windows, "span": (span_from, span_to), "nominal_span": (span_from_nominal, span_to),
        "speed_rows": speed_rows, "control_candidates": control_candidates, "control_group": control_group,
        "control_ids": control_ids, "picks": picks, "quality_rows": quality_rows, "range_ms": range_ms,
        "index_hint": hint, "mech_stoppage": mech_stoppage, "breakdowns": breakdowns,
    }


def _spans_by_loom(rows) -> dict:
    out: dict = defaultdict(list)
    for r in rows:
        m = _mapping(r)
        out[int(m["machine_id"])].append({
            "machine_id": int(m["machine_id"]), "modification_id": int(m["modification_id"]),
            "root_modification_id": int(m["root_modification_id"]) if m.get("root_modification_id") is not None else None,
            "mod_kind": m.get("mod_kind"), "title": m.get("title"),
            "settle_days": int(m.get("settle_days") or 0), "d_m": _d(m["d_m"]), "e_m": _d(m["e_m"]),
        })
    return dict(out)


# =============================================================================
# Report builder (pure)
# =============================================================================


def build_report(inp: dict, p: dict, t_start: Optional[float] = None) -> dict:
    t_start = t_start or time.perf_counter()
    W = p["window_days"]
    stages, loom_info, windows = inp["stages"], inp["loom_info"], inp["windows"]
    study_ids = list(inp["study_ids"])
    study_set = set(study_ids)
    root = stages[0]
    span_from, span_to = inp["span"]
    last_data_date = inp["last_data_date"]
    notes: list = [SOURCE_NOTE]
    speed_index = build_speed_index(inp["speed_rows"])
    picks_index = build_picks_index(inp["picks"])

    # --- baseline speed per study loom (first day of the extended before window)
    baseline_speeds, baseline_role, baseline_fallback, no_standard = {}, {}, [], []
    for mid in study_ids:
        w = windows.get(mid)
        if not w or not w["before"]:
            continue
        v, role, _ = resolve_speed(speed_index, mid, w["before"][0])
        if not v:
            v, eff = first_speed_in_window(speed_index, mid, w["before"][0], w["before"][1])
            if v:
                baseline_fallback.append((loom_info[mid]["loom"], eff))
                role = "first in window"
        if v:
            baseline_speeds[mid] = v
            baseline_role[mid] = role
        else:
            no_standard.append(loom_info[mid]["loom"])
    if baseline_fallback:
        notes.append("No standard in force on the first day of the before window for "
                     + ", ".join(f"{n} (uses the standard effective {d.strftime('%d-%b-%y')})" for n, d in baseline_fallback) + ".")
    if no_standard:
        notes.append(f"{len(no_standard)} looms with no standard speed (flag N): " + ", ".join(no_standard) + " — efficiency blank for them.")

    # --- controls
    control_mode = p["control"]
    control_sel = {"selected": [], "dropped": [], "trimmed": 0}
    control_desc = "none"
    control_spans = {mid: s for mid, s in inp["all_spans"].items() if mid not in study_set}
    study_bands = {mid: band_key(baseline_speeds.get(mid), loom_info[mid]["loom"]) for mid in study_ids}
    on_date = min(w["before"][0] for w in windows.values() if w["before"])
    max_controls = max(0, IMPACT_MAX_LOOMS - len(study_ids))
    if control_mode != "none" and inp["control_candidates"]:
        control_sel = select_controls(inp["control_candidates"], study_bands, speed_index, on_date, control_spans,
                                      (span_from, span_to), W, study_set, max_controls,
                                      require_band=(control_mode == "speed_band"))
        if control_mode == "control_group":
            g = inp["control_group"] or {}
            control_desc = f"control group {g.get('group_name') or g.get('group_code') or p['control_group_id']} ({len(control_sel['selected'])} looms)"
            if control_sel["dropped"]:
                notes.append(f"control group {g.get('group_name')}: " + "; ".join(f"{n} dropped — {r}" for n, r in control_sel["dropped"]) + ".")
        else:
            dept_names = ", ".join(sorted({str(loom_info[m].get('dept_id')) for m in study_ids}))
            control_desc = (f"{len(control_sel['selected'])} un-modified looms of the same dept"
                            + (" in the study speed bands" if control_mode == "speed_band" else "") + f" (dept {dept_names})")
            dropped_mod = [(n, r) for n, r in control_sel["dropped"] if r.startswith("modified")]
            if dropped_mod:
                notes.append(f"{len(dropped_mod)} same-dept looms excluded as controls (modified inside the span): "
                             + "; ".join(f"{n} — {r}" for n, r in dropped_mod[:10]) + ("…" if len(dropped_mod) > 10 else "") + ".")
            if not control_sel["selected"]:
                n_cand = len(inp["control_candidates"])
                notes.append(
                    f"No eligible controls: none of the {n_cand} other looms of the dept "
                    + ("falls in the study looms' speed bands (try Control = same dept, any speed)."
                       if control_mode == "speed_band" else "is un-modified inside the span.")
                )
        if control_sel["trimmed"]:
            notes.append(f"control set trimmed by {control_sel['trimmed']} looms to the {IMPACT_MAX_LOOMS}-loom cap (closest speed band kept).")
    elif control_mode != "none":
        notes.append("No eligible controls (no other active looms in the study looms' departments).")
    control_ids = [c["machine_id"] for c in control_sel["selected"]]
    control_set = set(control_ids)
    control_bands = {c["machine_id"]: c["band"] for c in control_sel["selected"]}

    # Baseline speed per CONTROL loom too (same calendar before-window start as the
    # study), so the constant-yardstick basis (efficiency at the baseline standard)
    # has a control figure: a control whose standard changes inside the span has its
    # later loom-days dropped below, so its baseline standard is its only standard.
    before_starts = [w["before"][0] for w in windows.values() if w and w["before"]]
    if before_starts and control_ids:
        ctrl_from = min(before_starts)
        ctrl_to = max(w["before"][1] for w in windows.values() if w and w["before"])
        for mid in control_ids:
            v, _role, _ = resolve_speed(speed_index, mid, ctrl_from)
            if not v:
                v, _eff = first_speed_in_window(speed_index, mid, ctrl_from, ctrl_to)
            if v:
                baseline_speeds[mid] = v

    # --- loom-days
    loom_days = build_loom_days(inp["quality_rows"], speed_index, picks_index)

    # control S-drop: loom-days after a control's own effective-speed change inside the span
    control_change_first = {}
    for mid in control_ids:
        ch = speed_change_events(speed_index, mid, span_from, span_to)
        if ch:
            control_change_first[mid] = ch[0][0]
    if control_change_first:
        notes.append(f"control standard changed on {len(control_change_first)} looms inside the span — their loom-days after the change are dropped.")

    # --- assign loom-days to phases
    study_by_phase: dict = defaultdict(list)   # ("BEFORE",None) / ("AFTER",seq) -> [ld]
    per_loom_days: dict = defaultdict(lambda: defaultdict(list))  # mid -> phase -> [ld]
    control_before, control_after = [], defaultdict(list)
    control_phase_windows = _control_windows(windows, stages)
    other_after_hits = defaultdict(int)
    no_rows_looms = set(study_ids)
    for ld in loom_days:
        mid = ld["machine_id"]
        if mid in study_set:
            no_rows_looms.discard(mid)
            ph = phase_of(windows.get(mid), ld["tran_date"])
            if ph is None or ph[0] in ("DROPPED", "EXCLUDED"):
                if ph is not None and ph[0] == "DROPPED":
                    per_loom_days[mid][("DROPPED", None)].append(ld)
                elif ph is not None:
                    per_loom_days[mid][ph].append(ld)
                continue
            study_by_phase[ph].append(ld)
            per_loom_days[mid][ph].append(ld)
            if ph[0] == "BEFORE":
                for lo, hi, title in windows[mid]["other_after"]:
                    if lo <= ld["tran_date"] <= hi:
                        other_after_hits[title] += 1
        elif mid in control_set:
            cut = control_change_first.get(mid)
            if cut is not None and ld["tran_date"] >= cut:
                continue
            if _in(ld["tran_date"], control_phase_windows["before"]):
                control_before.append(ld)
                per_loom_days[mid][("BEFORE", None)].append(ld)
            for seq, win in control_phase_windows["after"].items():
                if _in(ld["tran_date"], win):
                    control_after[seq].append(ld)
                    per_loom_days[mid][("AFTER", seq)].append(ld)
                    break
    for title, n in other_after_hits.items():
        notes.append(f"baseline includes {n} loom-days after another chain's event ({title}).")
    if no_rows_looms:
        notes.append(f"{len(no_rows_looms)} looms returned no weaving rows in the span: " + ", ".join(loom_info[m]["loom"] for m in sorted(no_rows_looms)) + ".")

    # --- extension / holes / coverage notes
    ext = inp["extension"]
    if ext["extension_days"]:
        notes.append(f"before window extended by {ext['extension_days']} days (to {W + ext['extension_days']} days) to reach "
                     f"{ext['min_loom_days']} producing loom-days and {ext['min_days']} producing days.")
    if ext["stopped_by_hole"]:
        notes.append("before-window extension stopped at a data hole (>= 7 consecutive days with no weaving rows for the branch).")
    span_holes = [(lo, hi) for lo, hi in inp["holes"] if hi >= span_from and lo <= span_to]
    for lo, hi in span_holes:
        notes.append(f"data hole inside the span: {lo.strftime('%d-%b')}..{hi.strftime('%d-%b')} has no weaving rows for the branch.")

    # --- segments
    before_all = study_by_phase.get(("BEFORE", None), [])
    agg_before_all = aggregate(before_all, baseline_speeds)
    use_bstd_any = False
    speed_changed = {}
    for mid in study_ids:
        ch = speed_change_events(speed_index, mid, span_from, span_to)
        speed_changed[mid] = bool(ch)
        use_bstd_any = use_bstd_any or bool(ch)
    if use_bstd_any:
        notes.append(f"standard changed inside a window on {sum(1 for v in speed_changed.values() if v)} study looms (flag S) — "
                     "the verdict uses efficiency at the baseline standard.")
    segments, stage_results = [], {}
    before_dates = [w["before"] for w in windows.values() if w["before"]]
    seg0 = _segment_row(0, "Before", None, "BEFORE", agg_before_all,
                        min(b[0] for b in before_dates), max(b[1] for b in before_dates),
                        len({m for m in study_ids}), False)
    segments.append(seg0)
    if control_ids:
        agg_cb = aggregate(control_before, baseline_speeds)
        segments.append(_segment_row(0, "Before (control)", None, "BEFORE", agg_cb,
                                     control_phase_windows["before"][0], control_phase_windows["before"][1], len(control_ids), True))
    for st in stages:
        seq = st["seq"]
        looms_k = set(st["looms"])
        after_k = study_by_phase.get(("AFTER", seq), [])
        before_k = [ld for ld in before_all if ld["machine_id"] in looms_k]
        agg_a, agg_b = aggregate(after_k, baseline_speeds), aggregate(before_k, baseline_speeds)
        use_bstd = any(speed_changed.get(m) for m in looms_k)
        # per-loom minimum for the "k of m looms" rule
        looms_meeting, looms_improved = 0, 0
        for mid in looms_k:
            b_ld = per_loom_days[mid].get(("BEFORE", None), [])
            a_ld = per_loom_days[mid].get(("AFTER", seq), [])
            ab_, aa_ = aggregate(b_ld, baseline_speeds), aggregate(a_ld, baseline_speeds)
            if ab_["days_run"] >= MIN_LOOM_DAYS_LOOM and aa_["days_run"] >= MIN_LOOM_DAYS_LOOM:
                looms_meeting += 1
                bv, av = eff_value(ab_, use_bstd), eff_value(aa_, use_bstd)
                if bv is not None and av is not None and av > bv:
                    looms_improved += 1
        s1 = [v for _, v in day_series(before_k, baseline_speeds, use_bstd)]
        s2 = [v for _, v in day_series(after_k, baseline_speeds, use_bstd)]
        n1, n2, s_p, se = pooled_se(s1, s2)
        # control
        net, se_net, net_units, ctrl_agg_a, ctrl_agg_b, did = None, None, None, None, None, None
        ctrl_after_k = control_after.get(seq, [])
        if control_ids:
            ctrl_agg_b, ctrl_agg_a = aggregate(control_before, baseline_speeds), aggregate(ctrl_after_k, baseline_speeds)
            ctrl_ok = (len({ld["machine_id"] for ld in control_before}) >= CONTROL_MIN_LOOMS
                       and ctrl_agg_b["days_run"] >= MIN_LOOM_DAYS_GROUP and ctrl_agg_a["days_run"] >= MIN_LOOM_DAYS_GROUP
                       and ctrl_agg_b["producing_days"] >= MIN_DAYS_PER_SIDE_GROUP and ctrl_agg_a["producing_days"] >= MIN_DAYS_PER_SIDE_GROUP)
            if ctrl_ok:
                bd = {mid: per_loom_days[mid].get(("BEFORE", None), []) for mid in list(looms_k) + control_ids}
                ad = {mid: per_loom_days[mid].get(("AFTER", seq), []) for mid in list(looms_k) + control_ids}
                if control_mode == "speed_band":
                    did = did_by_band({m: study_bands[m] for m in looms_k}, control_bands, bd, ad, baseline_speeds, use_bstd)
                    net = did["net_delta_pct"]
                else:
                    sd_ = _delta_pct(eff_value(agg_b, use_bstd), eff_value(agg_a, use_bstd))
                    cd_ = _delta_pct(eff_value(ctrl_agg_b, use_bstd), eff_value(ctrl_agg_a, use_bstd))
                    net = None if sd_ is None or cd_ is None else _r(sd_ - cd_, 2)
                c1 = [v for _, v in day_series(control_before, baseline_speeds, use_bstd)]
                c2 = [v for _, v in day_series(ctrl_after_k, baseline_speeds, use_bstd)]
                _, _, _, se_c = pooled_se(c1, c2)
                if se is not None and se_c is not None:
                    se_net = math.sqrt(se ** 2 + se_c ** 2)
                bv, av = eff_value(agg_b, use_bstd), eff_value(agg_a, use_bstd)
                cb, ca = eff_value(ctrl_agg_b, use_bstd), eff_value(ctrl_agg_a, use_bstd)
                if None not in (bv, av, cb, ca):
                    net_units = (av - bv) - (ca - cb)
            elif seq == 1:
                notes.append(f"control set too thin (needs >= {CONTROL_MIN_LOOMS} looms with >= {MIN_LOOM_DAYS_GROUP} producing loom-days "
                             f"and >= {MIN_DAYS_PER_SIDE_GROUP} producing days per window) — verdict uses the plain change with a caveat.")
        verdict = decide_verdict(agg_b, agg_a, se, len(looms_k), looms_meeting, net, se_net, net_units, use_bstd)
        # delta vs previous: parent stage's after (reversal of a tweak: the segment before that parent)
        prev_seq = st["parent_seq"]
        if st["mod_kind"] == "REVERSAL" and st["parent_kind"] not in (None, "INITIAL"):
            prev_seq = stages[st["parent_seq"] - 1]["parent_seq"] if st["parent_seq"] else None
        prev_after = [ld for ld in study_by_phase.get(("AFTER", prev_seq), []) if ld["machine_id"] in looms_k] if prev_seq else None
        d_prev = d_prev_pct = None
        if prev_after:
            agg_p = aggregate(prev_after, baseline_speeds)
            pv, av = eff_value(agg_p, use_bstd), eff_value(agg_a, use_bstd)
            if pv is not None and av is not None and agg_p["days_run"] >= producing_minima(len(looms_k))[0]:
                d_prev, d_prev_pct = _r(av - pv, 3), _delta_pct(pv, av)
        after_wins = [windows[m]["stages"][seq]["after"] for m in looms_k if windows.get(m) and windows[m]["stages"].get(seq) and windows[m]["stages"][seq]["after"]]
        a_from = min(w[0] for w in after_wins) if after_wins else None
        a_to = max(w[1] for w in after_wins) if after_wins else None
        row = _segment_row(seq, st["label"], seq, st["mod_kind"], agg_a, a_from, a_to, len(looms_k), False)
        row.update({
            "delta_vs_baseline_pts": verdict["delta_pts"], "delta_vs_baseline_pct": verdict["delta_pct"],
            "delta_vs_previous_pts": d_prev, "delta_vs_previous_pct": d_prev_pct,
            "net_vs_control_pct": net, "se": _r(se, 3), "verdict": verdict["verdict"],
        })
        segments.append(row)
        if control_ids:
            cwin = control_phase_windows["after"].get(seq)
            crow = _segment_row(seq, f"{st['label']} (control)", seq, st["mod_kind"], ctrl_agg_a,
                                cwin[0] if cwin else None, cwin[1] if cwin else None, len(control_ids), True)
            cb, ca = eff_value(ctrl_agg_b, use_bstd), eff_value(ctrl_agg_a, use_bstd)
            crow.update({"delta_vs_baseline_pts": _delta_pts(cb, ca), "delta_vs_baseline_pct": _delta_pct(cb, ca)})
            segments.append(crow)
        stage_results[seq] = {
            "verdict": verdict, "agg_before": agg_b, "agg_after": agg_a, "n1": n1, "n2": n2, "s_p": s_p, "se": se,
            "net": net, "se_net": se_net, "did": did, "looms_meeting": looms_meeting, "looms_improved": looms_improved,
            "looms_total": len(looms_k), "use_bstd": use_bstd, "ctrl_after": ctrl_agg_a, "ctrl_before": ctrl_agg_b,
            "after_from": a_from, "after_to": a_to,
        }

    # --- verdict strip: latest stage that meets the minima
    strip_seq = None
    for seq in sorted(stage_results, reverse=True):
        if stage_results[seq]["verdict"]["verdict"] != "NOT_ENOUGH_DATA":
            strip_seq = seq
            break
    other_stage_note = None
    latest_seq = max(stage_results)
    if strip_seq is None:
        strip_seq = latest_seq
    elif strip_seq != latest_seq:
        lr = stage_results[latest_seq]
        other_stage_note = (f"latest stage ({stages[latest_seq - 1]['mod_date'].strftime('%d-%b')}) has "
                            f"{lr['agg_after']['producing_days']} producing days — verdict shown for stage {strip_seq}")
    sr = stage_results[strip_seq]
    st_strip = stages[strip_seq - 1]
    v = sr["verdict"]
    rest = [m for m in study_ids if m not in st_strip["looms"] and windows.get(m)]
    if rest and strip_seq > 1:
        prev_note = f"{len(rest)} looms not in stage {strip_seq}"
        other_stage_note = (other_stage_note + "; " if other_stage_note else "") + prev_note
    reverted = sorted({loom_info[m]["loom"] for s in stages if s["mod_kind"] == "REVERSAL" for m in s["looms"]})
    if reverted:
        notes.append("reverted looms: " + ", ".join(reverted) + ".")
    if v["verdict"] == "NOT_ENOUGH_DATA":
        notes.append(f"verdict NOT_ENOUGH_DATA for stage {strip_seq} — {v['reason']}. Efficiency is the only yardstick "
                     "(no fallback to picks / yds / cuts): fix the cause (weaver marks, SQC picks, standard speed) or widen the window.")
    since = min(st_strip["looms"][m][1] for m in st_strip["looms"]) + timedelta(days=st_strip["settle_days"] + 1) if st_strip["looms"] else None
    # verdict.efficiency is always the plain efficiency; the constant-yardstick pair is
    # added when any study loom carries flag S (it is then the verdict basis for the
    # stages whose looms changed standard — see evidence "Yardstick" and the reason).
    eb, ea = sr["agg_before"]["efficiency_pct"], sr["agg_after"]["efficiency_pct"]
    eff_bstd = None
    if use_bstd_any:
        bb, ba = sr["agg_before"]["efficiency_at_baseline_std_pct"], sr["agg_after"]["efficiency_at_baseline_std_pct"]
        eff_bstd = {"before": bb, "after": ba, "delta_pts": _delta_pts(bb, ba), "delta_pct": _delta_pct(bb, ba)}
    # --- comparison block: this loom set vs the un-modified looms of the judged
    # stage, on the verdict basis (efficiency, or at the baseline standard when a
    # study loom of the stage carries flag S — control figures are then those of the
    # ladder's control rows on that basis). Reuses the stage aggregates; control is
    # None when no control set was selected or eligible; net_delta_pct is the
    # existing net figure (DiD per band in speed_band mode), net_delta_pts the plain
    # difference of the two point changes.
    study_cmp = {"looms_n": sr["looms_total"], "before": v["before"], "after": v["after"],
                 "delta_pts": v["delta_pts"], "delta_pct": v["delta_pct"]}
    control_cmp = None
    if control_ids and sr["ctrl_before"] is not None:
        cb, ca = eff_value(sr["ctrl_before"], sr["use_bstd"]), eff_value(sr["ctrl_after"], sr["use_bstd"])
        control_cmp = {"looms_n": len(control_ids), "before": cb, "after": ca,
                       "delta_pts": _delta_pts(cb, ca), "delta_pct": _delta_pct(cb, ca)}
    net_pts = None
    if control_cmp is not None and study_cmp["delta_pts"] is not None and control_cmp["delta_pts"] is not None:
        net_pts = _r(study_cmp["delta_pts"] - control_cmp["delta_pts"], 3)
    comparison = {
        "basis": "efficiency_at_baseline_std" if sr["use_bstd"] else "efficiency",
        "stage_seq": strip_seq, "description": control_desc,
        "study": study_cmp, "control": control_cmp,
        "net_delta_pts": net_pts, "net_delta_pct": sr["net"],
    }
    verdict_out = {
        "verdict": v["verdict"], "stage_seq": strip_seq, "since_date": _iso(since),
        "stage_label": f"{st_strip['mod_kind_label']}: {st_strip['title']} (stage {strip_seq} of {len(stages)}, "
                       f"{len(st_strip['looms'])} of {len(study_ids)} looms)",
        "efficiency": {"before": eb, "after": ea, "delta_pts": _delta_pts(eb, ea), "delta_pct": _delta_pct(eb, ea),
                       "net_delta_pct": sr["net"], "se": _r(sr["se"], 3), "within_noise": bool(v["within_noise"])},
        "efficiency_at_baseline_std": eff_bstd,
        "looms_improved": sr["looms_improved"], "looms_total": sr["looms_total"],
        "eff_days_before": sr["agg_before"]["eff_days"], "eff_days_after": sr["agg_after"]["eff_days"],
        "coverage_before_pct": sr["agg_before"]["std_coverage_pct"], "coverage_after_pct": sr["agg_after"]["std_coverage_pct"],
        "reason": v["reason"], "other_stage_note": other_stage_note,
        "comparison": comparison,
    }

    # --- per loom (baseline vs the strip stage)
    per_loom = []
    w_changed = 0
    for mid in study_ids:
        info = loom_info[mid]
        in_stage = mid in st_strip["looms"]
        b_ld = per_loom_days[mid].get(("BEFORE", None), [])
        a_ld = per_loom_days[mid].get(("AFTER", strip_seq), []) if in_stage else []
        ab_, aa_ = aggregate(b_ld, baseline_speeds), aggregate(a_ld, baseline_speeds)
        flags = loom_flags(b_ld, a_ld, speed_changed.get(mid, False), mid in baseline_speeds)
        w_changed += "W" in flags
        hb, ha = eff_value(ab_, sr["use_bstd"]), eff_value(aa_, sr["use_bstd"])
        latest_speed, _, _ = resolve_speed(speed_index, mid, span_to)
        since_l = None
        if in_stage:
            since_l = st_strip["looms"][mid][1] + timedelta(days=st_strip["settle_days"] + 1)
        per_loom.append({
            "machine_id": mid, "loom": info["loom"], "line_no": info.get("line_no"),
            "speed_band": (f"{study_bands[mid][1]} rpm" if study_bands[mid][0] == "rpm" else f"prefix {study_bands[mid][1] or '-'}"),
            "since_date": _iso(since_l),
            "producing_days_before": ab_["days_run"], "producing_days_after": aa_["days_run"],
            "eff_days_before": ab_["eff_days"], "eff_days_after": aa_["eff_days"],
            "eff_before": ab_["efficiency_pct"], "eff_after": aa_["efficiency_pct"],
            "delta_pts": _delta_pts(hb, ha), "delta_pct": _delta_pct(hb, ha),
            "eff_baseline_std_before": ab_["efficiency_at_baseline_std_pct"], "eff_baseline_std_after": aa_["efficiency_at_baseline_std_pct"],
            "std_speed_baseline": baseline_speeds.get(mid), "std_speed_latest": latest_speed,
            "flags": flags, "in_stage": in_stage, "row_kind": "loom",
        })
    per_loom.sort(key=lambda r: (r["delta_pct"] is None, r["delta_pct"] if r["delta_pct"] is not None else 0))
    per_loom.append(_total_row("study_total", "GROUP TOTAL", sr["agg_before"], sr["agg_after"], sr["use_bstd"]))
    if control_ids and sr["ctrl_before"] is not None:
        per_loom.append(_total_row("control_total", "CONTROL TOTAL", sr["ctrl_before"], sr["ctrl_after"], sr["use_bstd"]))
    if w_changed:
        notes.append(f"weaver changed on {w_changed} looms (flag W).")

    # --- daywise
    daywise = _daywise(loom_days, windows, stages, study_set, control_set, control_change_first,
                       baseline_speeds, sr["use_bstd"], span_from, span_to, speed_index, study_ids, control_ids)

    # --- mechanical context
    mechanical = build_mechanical(inp.get("mech_stoppage"), inp.get("breakdowns"), windows, study_ids, loom_info)

    # --- coverage notes (§7.2 G)
    unmanned = sum(1 for ld in loom_days if ld["machine_id"] in study_set and ld["kg"] > 0 and ld["hours_run"] <= 0)
    if unmanned:
        notes.append(f"{unmanned} loom-days produced with no weaver marked (0 loom hours — count as 0 % efficiency here, unlike the Low Producer shed average).")
    no_pick_days = len({ld["tran_date"] for ld in loom_days if ld["machine_id"] in study_set and ld["kg"] > 0 and not ld["std_picks_present"]})
    if no_pick_days:
        notes.append(f"{no_pick_days} days with no SQC pick reading for a running quality (std_picks 0 — efficiency unmeasured on them).")
    if sr["agg_after"]["producing_days"] < producing_minima(sr["looms_total"])[1]:
        notes.append(f"short after window: {sr['agg_after']['producing_days']} producing days.")
    if any(s["mod_category"] == "SPEED_CHANGE" for s in stages) and not use_bstd_any:
        notes.append("category SPEED_CHANGE but no standard change found in Weaving Standards inside the span — update the standard (D6).")
    notes.append("a manned loom with no output counts as 0 % efficiency here (unlike the Low Producer shed average).")

    thresholds = {
        "MIN_LOOM_DAYS_GROUP": MIN_LOOM_DAYS_GROUP, "MIN_LOOM_DAYS_LOOM": MIN_LOOM_DAYS_LOOM,
        "MIN_DAYS_PER_SIDE_GROUP": MIN_DAYS_PER_SIDE_GROUP, "MIN_DAYS_PER_SIDE_LOOM": MIN_DAYS_PER_SIDE_LOOM,
        "MIN_EFF_DAYS": MIN_EFF_DAYS, "VERDICT_THRESHOLD_PCT": VERDICT_THRESHOLD_PCT,
        "SE_MULTIPLIER": SE_MULTIPLIER, "COVERAGE_MIN_PCT": COVERAGE_MIN_PCT,
        "CONTROL_SPEED_BAND_PCT": CONTROL_SPEED_BAND_PCT, "CONTROL_MIN_LOOMS": CONTROL_MIN_LOOMS,
        "IMPACT_MAX_WINDOW_DAYS": IMPACT_MAX_WINDOW_DAYS, "IMPACT_MAX_SPAN_DAYS": IMPACT_MAX_SPAN_DAYS,
        "IMPACT_MAX_STUDY_LOOMS": IMPACT_MAX_STUDY_LOOMS, "IMPACT_MAX_LOOMS": IMPACT_MAX_LOOMS,
        "IMPACT_DATA_HOLE_DAYS": IMPACT_DATA_HOLE_DAYS,
        "n1": sr["n1"], "n2": sr["n2"], "s_p": _r(sr["s_p"], 4), "se": _r(sr["se"], 4), "se_net": _r(sr["se_net"], 4),
    }
    evidence = [
        {"label": "Yardstick", "value": "Efficiency % at the baseline standard" if sr["use_bstd"] else "Efficiency %"},
        {"label": "Producing loom-days before / after", "value": f"{sr['agg_before']['days_run']} / {sr['agg_after']['days_run']}"},
        {"label": "Days with measurable efficiency before / after", "value": f"{sr['agg_before']['eff_days']} / {sr['agg_after']['eff_days']}"},
        {"label": "Standard coverage before / after (%)", "value": f"{sr['agg_before']['std_coverage_pct']} / {sr['agg_after']['std_coverage_pct']}"},
        {"label": "Producing days before / after (n1 / n2)", "value": f"{sr['n1']} / {sr['n2']}"},
        {"label": "Pooled day-level SD (s_p)", "value": _r(sr["s_p"], 4)},
        {"label": "SE of the difference", "value": _r(sr["se"], 4)},
        {"label": "Noise band (SE_MULTIPLIER x SE)", "value": _r(SE_MULTIPLIER * sr["se"], 4) if sr["se"] is not None else None},
        {"label": "Threshold (relative %)", "value": VERDICT_THRESHOLD_PCT},
        {"label": "Control", "value": control_desc},
        {"label": "Net vs control (%)", "value": sr["net"]},
        {"label": "Looms meeting the per-loom minimum", "value": f"{sr['looms_meeting']} of {sr['looms_total']}"},
        {"label": "Before window extension (days)", "value": ext["extension_days"]},
        {"label": "Range read (ms)", "value": inp.get("range_ms")},
    ]
    if sr["did"]:
        for b in sr["did"]["bands"]:
            evidence.append({"label": f"Band {b['band']}: study Δ% / control Δ% / DiD", "value": f"{b['study_delta_pct']} / {b['control_delta_pct']} / {b['did_pct']} (w {int(b['weight'])})"})

    chain = {
        "root_modification_id": inp["root_id"], "title": root["title"],
        "loom_group": ({"loom_group_id": root["loom_group_id"], "group_code": root["group_code"], "group_name": root["group_name"]}
                       if root.get("loom_group_id") else None),
        "stages": [{
            "modification_id": s["modification_id"], "seq": s["seq"], "mod_kind": s["mod_kind"], "mod_kind_label": s["mod_kind_label"],
            "mod_date": _iso(s["mod_date"]), "mod_to_date": _iso(s["mod_to_date"]), "settle_days": s["settle_days"],
            "title": s["title"], "mod_category": s["mod_category"], "mod_category_label": s["mod_category_label"],
            "done_by": s["done_by"], "done_by_label": s["done_by_label"],
            "loom_count": len(s["looms"]), "root_loom_count": len(study_ids),
            "control_description": control_desc,
        } for s in stages],
    }
    excluded_rows, after_rows = [], []
    for st in stages:
        ex = [windows[m]["stages"][st["seq"]]["excluded"] for m in st["looms"] if windows.get(m)]
        if ex:
            excluded_rows.append({"stage_seq": st["seq"], "date_from": _iso(min(e[0] for e in ex)), "date_to": _iso(max(e[1] for e in ex))})
        res_k = stage_results[st["seq"]]
        # the reason reported is the one of the loom(s) whose after window ends on the date shown
        reasons = [windows[m]["stages"][st["seq"]]["cut_reason"] for m in st["looms"]
                   if windows.get(m) and windows[m]["stages"][st["seq"]]["after"]
                   and windows[m]["stages"][st["seq"]]["after"][1] == res_k["after_to"]]
        after_rows.append({"stage_seq": st["seq"], "date_from": _iso(res_k["after_from"]), "date_to": _iso(res_k["after_to"]),
                           "days": ((res_k["after_to"] - res_k["after_from"]).days + 1) if res_k["after_from"] and res_k["after_to"] else 0,
                           "cut_reason": max(set(reasons), key=reasons.count) if reasons else "window"})
    b_from, b_to = seg0["date_from"], seg0["date_to"]
    windows_out = {
        "window_days": W, "settle_days": p["settle_days"] if p["settle_days"] is not None else root["event_settle_days"],
        "last_data_date": _iso(last_data_date),
        "before": {"date_from": b_from, "date_to": b_to, "days": seg0["days"], "extended": bool(ext["extension_days"])},
        "excluded": excluded_rows, "after": after_rows,
    }
    meta = {
        "source": SOURCE_NOTE, "thresholds": thresholds,
        "notes": notes, "server_ms": int((time.perf_counter() - t_start) * 1000),
        "control": {"mode": control_mode, "looms_n": len(control_ids), "description": control_desc},
        "flags_legend": FLAGS_LEGEND,
    }
    return {"chain": chain, "windows": windows_out, "verdict": verdict_out, "segments": segments, "per_loom": per_loom,
            "daywise": daywise, "mechanical": mechanical, "evidence": evidence, "meta": meta}


def _segment_row(seq, label, stage_seq, kind, agg, d_from, d_to, looms_total, is_control) -> dict:
    return {
        "seq": seq, "label": label, "stage_seq": stage_seq, "kind": kind,
        "date_from": _iso(d_from), "date_to": _iso(d_to),
        "days": ((d_to - d_from).days + 1) if d_from and d_to else 0,
        "looms_n": agg["looms_n"], "looms_total": looms_total, "loom_days": agg["loom_days"],
        "days_run": agg["days_run"], "producing_days": agg["producing_days"], "eff_days": agg["eff_days"],
        "hours_run": _r(agg["hours_run"], 2),
        "std_coverage_pct": agg["std_coverage_pct"], "hours_coverage_pct": agg["hours_coverage_pct"],
        "efficiency_pct": agg["efficiency_pct"], "efficiency_at_baseline_std_pct": agg["efficiency_at_baseline_std_pct"],
        "delta_vs_baseline_pts": None, "delta_vs_baseline_pct": None,
        "delta_vs_previous_pts": None, "delta_vs_previous_pct": None, "net_vs_control_pct": None,
        "se": None, "verdict": None, "is_control": is_control,
    }


def _total_row(kind, label, ab_, aa_, use_b) -> dict:
    hb, ha = eff_value(ab_, use_b), eff_value(aa_, use_b)
    return {
        "machine_id": None, "loom": label, "line_no": None, "speed_band": None, "since_date": None,
        "producing_days_before": ab_["days_run"], "producing_days_after": aa_["days_run"],
        "eff_days_before": ab_["eff_days"], "eff_days_after": aa_["eff_days"],
        "eff_before": ab_["efficiency_pct"], "eff_after": aa_["efficiency_pct"],
        "delta_pts": _delta_pts(hb, ha), "delta_pct": _delta_pct(hb, ha),
        "eff_baseline_std_before": ab_["efficiency_at_baseline_std_pct"], "eff_baseline_std_after": aa_["efficiency_at_baseline_std_pct"],
        "std_speed_baseline": None, "std_speed_latest": None,
        "flags": [], "in_stage": True, "row_kind": kind,
    }


def _control_windows(windows: dict, stages: list) -> dict:
    """Union calendar windows of the study looms per side (control assignment)."""
    befores = [w["before"] for w in windows.values() if w["before"]]
    before = (min(b[0] for b in befores), max(b[1] for b in befores)) if befores else None
    after = {}
    for st in stages:
        wins = [windows[m]["stages"][st["seq"]]["after"] for m in st["looms"]
                if windows.get(m) and windows[m]["stages"].get(st["seq"]) and windows[m]["stages"][st["seq"]]["after"]]
        if wins:
            after[st["seq"]] = (min(w[0] for w in wins), max(w[1] for w in wins))
    return {"before": before, "after": after}


def _daywise(loom_days, windows, stages, study_set, control_set, control_change_first,
             baseline_speeds, use_b, span_from, span_to, speed_index, study_ids, control_ids) -> list:
    by_date_s, by_date_c = defaultdict(list), defaultdict(list)
    for ld in loom_days:
        mid = ld["machine_id"]
        if mid in study_set:
            ph = phase_of(windows.get(mid), ld["tran_date"])
            if ph and ph[0] in ("BEFORE", "AFTER", "EXCLUDED"):
                by_date_s[ld["tran_date"]].append(ld)
        elif mid in control_set:
            cut = control_change_first.get(mid)
            if cut is not None and ld["tran_date"] >= cut:
                continue
            by_date_c[ld["tran_date"]].append(ld)
    # markers: event dates and standard changes of either role for member looms
    markers = defaultdict(list)
    for st in stages:
        ds = sorted({d for d, _ in st["looms"].values()})
        for d in ds:
            n = sum(1 for dd, _ in st["looms"].values() if dd == d)
            markers[d].append(f"Stage {st['seq']} {st['mod_kind_label']}: {st['title']} ({n} looms)")
    change_acc = defaultdict(lambda: defaultdict(int))
    for mid in list(study_ids) + list(control_ids):
        for d, old, new, role in speed_change_events(speed_index, mid, span_from, span_to):
            tag = "STD SPEED" if role == "standard" else "ACT SPEED"
            who = "study" if mid in study_set else "control"
            change_acc[d][(tag, old, new, who)] += 1
    for d, acc in change_acc.items():
        for (tag, old, new, who), n in acc.items():
            markers[d].append(f"{tag} {_fmt_num(old)} → {_fmt_num(new)} on {n} {who} looms")
    rows, hist_s, hist_c = [], [], []
    d = span_from
    while d <= span_to:
        ph_label = _group_phase(d, windows, stages, study_ids)
        s_agg = aggregate(by_date_s.get(d, []), baseline_speeds) if by_date_s.get(d) else None
        c_agg = aggregate(by_date_c.get(d, []), baseline_speeds) if by_date_c.get(d) else None
        sv = eff_value(s_agg, use_b)
        cv = eff_value(c_agg, use_b)
        hist_s.append((d, by_date_s.get(d, [])))
        hist_c.append((d, by_date_c.get(d, [])))
        roll_s = _rolling(hist_s, d, baseline_speeds, use_b)
        roll_c = _rolling(hist_c, d, baseline_speeds, use_b)
        rows.append({
            "tran_date": d.isoformat(), "phase": ph_label,
            "study_looms_run": s_agg["days_run"] if s_agg else 0,
            "study_eff": sv, "study_rolling7": roll_s, "control_eff": cv, "control_rolling7": roll_c,
            "gap": None if sv is None or cv is None else _r(sv - cv, 3),
            "markers": markers.get(d, []),
        })
        d += timedelta(days=1)
    return rows


def _fmt_num(v) -> str:
    if v is None or v == 0:
        return "-"
    return str(int(v)) if float(v).is_integer() else str(round(float(v), 1))


def _rolling(hist, d, baseline_speeds, use_b):
    lo = d - timedelta(days=6)
    lds = [ld for dd, lst in hist if dd >= lo for ld in lst]
    return eff_value(aggregate(lds, baseline_speeds), use_b) if lds else None


def _group_phase(d: date, windows: dict, stages: list, study_ids) -> str:
    kinds = defaultdict(int)
    for mid in study_ids:
        ph = phase_of(windows.get(mid), d)
        if ph and ph[0] != "DROPPED":
            kinds[ph] += 1
    if not kinds:
        return ""
    ph = max(kinds.items(), key=lambda kv: kv[1])[0]
    if ph[0] == "AFTER":
        return f"AFTER-{ph[1]}"
    return ph[0]


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/loom_modification_impact")
def loom_modification_impact(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    t0 = time.perf_counter()
    p = parse_report_params(request)
    try:
        inputs = load_report_inputs(db, p)
        data = build_report(inputs, p, t0)
        return {"data": data}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/loom_modification_impact_options")
def loom_modification_impact_options(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _int_param(request, "co_id", required=True)
    branch_id = _int_param(request, "branch_id", default=None)
    try:
        mods = []
        for r in _rows(db.execute(get_root_modifications_query(), {"co_id": co_id, "branch_id": branch_id})):
            d = _d(r["mod_date"])
            grp = r.get("loom_group_name")
            mods.append({
                "modification_id": int(r["modification_id"]),
                "label": f"{d.strftime('%d-%m-%y')} · {r.get('title')} · {grp + ' / ' if grp else ''}{int(r.get('loom_count') or 0)} looms",
                "mod_date": d.isoformat(), "title": r.get("title"), "loom_group_name": grp,
                "loom_count": int(r.get("loom_count") or 0), "stage_count": int(r.get("stage_count") or 0),
            })
        groups = [{"loom_group_id": int(r["loom_group_id"]), "group_name": r.get("group_name")}
                  for r in _rows(db.execute(get_control_groups_query(), {"co_id": co_id, "branch_id": branch_id}))]
        return {"data": {"modifications": mods, "control_groups": groups,
                         "window_presets": list(IMPACT_WINDOW_PRESETS), "default_window_days": IMPACT_DEFAULT_WINDOW_DAYS}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
