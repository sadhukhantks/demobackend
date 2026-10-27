"""Hands Report — actual worker hands against the manning norms, per designation.

Port of the legacy MIS "Daily Hands Comparison", which only ever *read* a
precomputed ``EMPMILL12.tbl_daily_hand_comp_data``. Nothing is precomputed here:
actual hands come straight from ``daily_attendance`` and the targets from the
effective-dated ``jute_prod_hands_norms_mst``, so the report can never drift from either.

Conventions worth knowing before reading the code:

* **hands = (working_hours - idle_hours) / 8** for every shift, C included. Flat
  /8 is what the DPR hands section and the HRMS attendance summary already use;
  the old printed report's 7.5h C-shift norm over-counted C-shift workers, whose
  rows carry working_hours = 8 anyway.
* **Shift letter** is the first character of the spell code (A1 -> A); anything
  outside A/B/C is the general shift 'G'.
* **Norm kinds** (Hands Norms v2, ``norm_kind``): **F** fixed — the norm's own
  shift_a..shift_g; **M** machine-based — Σ units of the norm's machines that
  WORKED that date+shift ÷ ``units_per_hand`` (legacy rows without
  units_per_hand: machines × ``hands_per_machine``); **P** production — kg or
  cuts produced by the norm's machines that shift ÷ ``qty_per_hand``, summed
  over every machine that carries production whether it counted as worked or
  not (kg produced is kg produced); **N** no norm — target blank, excluded
  from excess/short; **A** as actual — target = hands present.
  The machines of an M/P norm are its *scope*: a machine group (members with
  per-machine units), else a machine type, else a department (every machine
  of that department, units 1). A department-scoped P norm in kg also counts
  the person-keyed winding doffs of that department's winders, machine or not.
  A row without ``norm_kind`` (pre-migration tenant, test fixture) is read by
  ``_kind``: 'V' or a machine link → M, a per-machine rate with no shift
  counts → M, else F. An M/P norm with no scope or no rate cannot be
  computed: its target is blank (None) and noted, never a 0 read off empty
  shift columns (TKT-000165); a complete one whose machines never worked /
  produced is a real 0.
* Rows are **attendance-driven**: a designation with a norm but no attendance in
  the window does not produce a row (mirrors the legacy source table).
* excess/short are computed **per designation per date** and then SUMMED into the
  rollups — never recomputed from rolled-up totals, which would net a short shed
  against an over-staffed one.
* **MTD** (``mtd_actual`` / ``mtd_target`` / ``mtd_excess`` / ``mtd_short``,
  TKT-000154) rides on every manning row and on ``meta``. It is the month of the
  To Date, from the 1st up to and including the To Date, summed with the same
  per-day rule; on the per-date views (detailed, by_date) each row carries the
  running figure up to ITS date instead, so a day-by-day sheet reads like the
  printed one. The actuals and machine runs are therefore fetched from the 1st
  of the From Date's month, not from the From Date.
* **Machines Worked** (``view=machines_run``, the "Machines Worked" tab) is the
  machine-wise sheet behind the variable targets: one row per (date, shift,
  machine) from every machine-keyed entry (production, waste, attendance
  marks, stoppage, breakdown), deduplicated in SQL so a loom with three
  weaving lines and an attendance mark is one machine-shift.
  **A machine WORKED in a shift only when attendance says so** (owner,
  2026-10-01): at least one worker is marked on it (``daily_ebmc_attendance``
  through a live, non-rejected header) AND it was not stopped for the whole
  shift (the higher of the two stoppage books and the breakdown minutes >=
  the shift's hours). A production, waste, stoppage or breakdown entry alone
  no longer makes a machine count — such rows are listed with
  ``status = NO_WORKER_MARKED`` and noted by machine type for the mill to fix
  the marking; a marked machine stopped all shift is ``STOPPED_ALL_SHIFT``.
  The WORKED machine-shifts are exactly the multiplier of an M norm. Each row
  also carries the production the entries booked on that machine-shift
  (``prod_kg`` / ``prod_cuts``, one leg per quantity — see the query module),
  the numerator of a P norm. Like every other view it covers the requested
  window only, although the rows are fetched from the 1st of the month for
  the MTD targets (TKT-000167).
* **Storage** — the machine rows are read from ``jute_prod_machine_shift_worked``,
  a real table every request REBUILDS for its own window (1st of the From
  Date's month .. To Date) before reading: ``rebuild_machine_shift_worked``,
  one transaction of DELETE + INSERT ... SELECT from the sources. The table
  is as fresh as the last open (``meta.synced_at``); ``build_hands_report``
  itself stays pure (rows in, dict out).
* **Manual overrides** (owner, 2026-10-01, spec §12) — attendance may miss a
  machine or mark a wrong one, so a role with EDIT access to the Hands Report
  menu can ADD a machine-shift (it counts even with no entry anywhere and
  even when the stoppage books say stopped all shift) or EXCLUDE one (it does
  not count though a worker is marked), each with a reason:
  ``worked = ADD override OR (worker marked AND not stopped all shift AND no
  EXCLUDE override)``. The overrides live in ``jute_prod_machine_shift_override``
  (book of record: who, when, why; undo = ``active = 0``) and the rebuild
  applies the ones in force in its window in the same transaction, stamping
  ``manual_action`` on the row. Row ``status`` precedence: ``ADDED_MANUALLY``
  > ``EXCLUDED_MANUALLY`` > ``STOPPED_ALL_SHIFT`` > ``WORKED`` >
  ``NO_WORKER_MARKED``; a row is ``suggested`` (for the Add dialog) when it
  has a production entry, no worker marked and no override. Endpoints:
  ``POST hands_report_machine_override`` / ``..._override_undo`` and
  ``GET hands_report_machines`` (the picker).
* **Detailed** rows are ordered date -> department -> occupation and come with
  ``dept_totals`` (one per date x department) and ``day_totals`` (one per date)
  beside them, rolled up server-side like every other total (TKT-000167); the
  frontend places them under each department and each date.
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import now_ist, parse_json_body
from src.config.db import get_tenant_db
from src.juteProduction.hands_norms import _check_members_in_branch
from src.juteProduction.hands_report_query import (
    OVERRIDE_ACTIONS,
    OVERRIDE_ADD,
    OVERRIDE_EXCLUDE,
    PRODUCTION_SOURCES,
    RUN_SOURCES,
    SHIFT_HOURS_FALLBACK,
    STOP_SOURCES,
    get_hands_actuals_query,
    get_hands_dept_production_query,
    get_hands_group_members_query,
    get_hands_norms_query,
    get_hands_report_machines_query,
    get_machine_shift_overrides_to_undo_query,
    get_machine_shift_worked_apply_add_query,
    get_machine_shift_worked_apply_exclude_query,
    get_machine_shift_worked_delete_query,
    get_machine_shift_worked_insert_query,
    get_machine_shift_worked_rows_query,
    insert_machine_shift_override_query,
    retire_machine_shift_overrides_query,
    undo_machine_shift_overrides_query,
)
from src.juteProduction.spinning_lock import user_menu_access_level


hands_report_router = APIRouter()

# Attendance is the heaviest table in the tenant (~1.2M rows on sls); the window
# is capped the same way the wage checklists cap theirs.
HANDS_REPORT_MAX_DAYS = 62

SHIFTS = ("a", "b", "c", "g")
VIEWS = ("detailed", "by_date", "by_occupation", "by_department", "machines_run")

# Norm kinds (jute_prod_hands_norms_mst.norm_kind): F fixed, M machine-based,
# P production-based, N no norm, A as actual.
NORM_KINDS = ("F", "M", "P", "N", "A")
# Kinds whose target is a computation over the machines that ran.
_MACHINE_KINDS = ("M", "P")
# Production units a P norm can be expressed in -> the ``ran`` entry key.
PROD_UOMS = ("kg", "cuts")

# Display order of the shift letters on the Machines Worked sheet.
_SHIFT_ORDER = {"A": 0, "B": 1, "C": 2, "G": 3}
_SOURCE_ORDER = {name: i for i, name in enumerate(RUN_SOURCES + STOP_SOURCES)}

# ``status`` of a Machines Worked row. WORKED and ADDED_MANUALLY count for a
# machine-based norm; the other three partition the rows that do not. A manual
# override outranks what the sources say (spec §12.2): ADDED_MANUALLY >
# EXCLUDED_MANUALLY > STOPPED_ALL_SHIFT (marked and stopped) > WORKED >
# NO_WORKER_MARKED.
STATUS_WORKED = "WORKED"
STATUS_STOPPED_ALL_SHIFT = "STOPPED_ALL_SHIFT"
STATUS_NO_WORKER_MARKED = "NO_WORKER_MARKED"
STATUS_ADDED_MANUALLY = "ADDED_MANUALLY"
STATUS_EXCLUDED_MANUALLY = "EXCLUDED_MANUALLY"

# Manual overrides of the machines worked (spec §12.3). The two POSTs need
# Edit (4) on the report's OWN menu row — menu_mst.menu_path, the same row on
# dev3 (menu_id 997) and sls (1015), seeded by 20260825_add_mis_report_menus.sql
# — and the frontend gates the buttons on the same level
# (hasMenuAccess(pathname, "edit")). The GETs stay at view level.
HANDS_REPORT_MENU_PATH = "juteProduction/report/handsReport"
OVERRIDE_EDIT_LEVEL = 4
OVERRIDE_VIEW_ONLY_MSG = "This role can view the hands report but not change the machines worked"
OVERRIDE_SHIFTS = ("A", "B", "C", "G")
OVERRIDE_REASON_MIN = 3
OVERRIDE_REASON_MAX = 255  # jute_prod_machine_shift_override.reason VARCHAR(255)
OVERRIDE_MAX_MACHINES = 500

# MySQL errors two simultaneous rebuilds of the same window can raise on each
# other: deadlock found (1213) and lock wait timeout exceeded (1205).
_REBUILD_RETRY_ERRNOS = (1213, 1205)

# Every numeric column a rollup sums. Targets are None-tolerant: an unresolvable
# norm contributes nothing rather than poisoning the whole department's total.
_SUM_FIELDS = (
    [f"shift_{s}" for s in SHIFTS]
    + [f"target_{s}" for s in SHIFTS]
    + ["total_actual", "total_target", "excess", "short"]
)

_DI_BUCKETS = ("direct", "indirect", "unclassified")

# MTD column -> the per-day detail column it accumulates. Kept OUT of
# _SUM_FIELDS: a rollup over the requested window must not add these up
# again, they are stamped on afterwards by _attach_mtd.
_MTD_FIELDS = {
    "mtd_actual": "total_actual",
    "mtd_target": "total_target",
    "mtd_excess": "excess",
    "mtd_short": "short",
}

# How many names a "no norm configured" note is allowed to list before it just
# reports the count — these notes render in a toast on the frontend.
_NOTE_SAMPLE = 15


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _scope(request: Request) -> tuple:
    """co_id / branch_id / date-range validation. Both ids are REQUIRED: the
    attendance and doff tables are branch-keyed with no co_id of their own."""
    raw_co = request.query_params.get("co_id")
    raw_branch = request.query_params.get("branch_id")
    if not raw_co:
        raise HTTPException(status_code=400, detail="co_id is required")
    if not raw_branch:
        raise HTTPException(status_code=400, detail="branch_id is required")
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    if not date_from or not date_to:
        raise HTTPException(status_code=400, detail="date_from and date_to are required")
    try:
        co_id = int(raw_co)
        branch_id = int(raw_branch)
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id, branch_id or date")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    if (d_to - d_from).days >= HANDS_REPORT_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large — at most {HANDS_REPORT_MAX_DAYS} days",
        )
    return co_id, branch_id, d_from, d_to


def _require_view(request: Request) -> str:
    view = (request.query_params.get("view") or "detailed").strip().lower()
    if view not in VIEWS:
        raise HTTPException(
            status_code=400, detail=f"Invalid view — one of {', '.join(VIEWS)}"
        )
    return view


def _optional_int(request: Request, key: str) -> Optional[int]:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


# ---------------------------------------------------------------------------
# Norm resolution
# ---------------------------------------------------------------------------


def _norm_sort_key(row: dict) -> tuple:
    """Ascending order in which the LAST in-force row is the winning one:
    latest effective_date, then a branch-specific row over the NULL-branch
    fallback, then the newest id."""
    return (
        row["effective_date"],
        0 if row.get("branch_id") is None else 1,
        int(row.get("hands_norm_id") or 0),
    )


def _norms_by_designation(norm_rows: list) -> dict:
    grouped: dict = {}
    for r in norm_rows:
        grouped.setdefault(int(r["designation_id"]), []).append(r)
    for rows in grouped.values():
        rows.sort(key=_norm_sort_key)
    return grouped


def _norm_for(norm_rows: list, d: date) -> Optional[dict]:
    """The norm in force on ``d``: MAX(effective_date) <= d, branch-specific
    beating the NULL-branch fallback. ``norm_rows`` is pre-sorted on exactly
    that key, so the last still-in-force row is the answer."""
    chosen = None
    for r in norm_rows:
        if r["effective_date"] > d:
            break
        chosen = r
    return chosen


def _num(v) -> Optional[float]:
    """Decimal / str / None -> float or None (DECIMAL columns arrive as Decimal)."""
    return None if v is None else float(v)


def _kind(norm: dict) -> str:
    """The norm's kind (F/M/P/N/A). ``norm_kind`` when the row carries one;
    otherwise (pre-migration tenant, test fixture) the legacy reading: 'V' or
    a machine link (type or group) → M, a per-machine rate with no explicit
    shift counts → M (TKT-000165: every per-machine norm seeded on sls is
    flagged 'F' with zero shift columns — the migration maps them to M the
    same way), else F."""
    k = (norm.get("norm_kind") or "").strip().upper()[:1]
    if k in NORM_KINDS:
        return k
    if (norm.get("variable_fixed") or "").strip().upper().startswith("V"):
        return "M"
    if norm.get("machine_type_id") is not None or norm.get("machine_group_id") is not None:
        return "M"
    if norm.get("hands_per_machine") is not None and not any(
        float(norm.get(f"shift_{s}") or 0) for s in SHIFTS
    ):
        return "M"
    return "F"


def _norm_scope(norm: dict) -> Optional[tuple]:
    """``(kind, id)`` of an M/P norm's machine set: ``("group", id)`` when
    machine_group_id is set, else ``("type", id)``, else ``("dept", id)``;
    None when the norm names none of the three (incomplete)."""
    if norm.get("machine_group_id") is not None:
        return ("group", int(norm["machine_group_id"]))
    if norm.get("machine_type_id") is not None:
        return ("type", int(norm["machine_type_id"]))
    if norm.get("scope_dept_id") is not None:
        return ("dept", int(norm["scope_dept_id"]))
    return None


def _scoped_units(scope: tuple, d: date, shift: str, runs: dict, ran: dict, members: dict) -> float:
    """Σ units of the scope's machines that WORKED on (d, shift) — a worker
    marked on the machine and not stopped all shift: group members weigh
    their configured units, machines of a type or of a department weigh 1
    each (machine_mst has no units)."""
    kind, sid = scope
    if kind == "type":
        # Same index the legacy variable target reads — one count per type.
        return float(runs.get((d, shift, sid), 0))
    entries = ran.get((d, shift), {})
    if kind == "group":
        units = members.get(sid, {})
        return sum(float(units[mid]) for mid in entries if mid in units)
    return float(sum(1 for e in entries.values() if e.get("dept_id") == sid))


def _scoped_qty(scope: tuple, uom: str, d: date, shift: str, produced: dict, members: dict,
                dept_kg: dict) -> float:
    """Σ production (``uom`` = kg | cuts) of the scope's machines on (d, shift).
    ``produced`` holds EVERY machine-shift that carries production, worked or
    not (owner, 2026-10-01: kg produced is kg produced — a missing attendance
    mark must not shrink a production target). A department scope in kg adds
    the winding doffs of that department's winders (person-keyed, from
    ``get_hands_dept_production_query``) and leaves the Winding leg's
    machine-attributed kg out of the machine sum, so a winder's doffs count
    once whichever department the machine sits in."""
    kind, sid = scope
    entries = produced.get((d, shift), {})
    if kind == "group":
        units = members.get(sid, {})
        picked = [e for mid, e in entries.items() if mid in units]
    elif kind == "type":
        picked = [e for e in entries.values() if e.get("machine_type_id") == sid]
    else:
        picked = [e for e in entries.values() if e.get("dept_id") == sid]
    qty = sum(float(e.get(uom) or 0) for e in picked)
    if kind == "dept" and uom == "kg":
        qty -= sum(float(e.get("winding_kg") or 0) for e in picked)
        qty += float(dept_kg.get((d, shift, sid), 0))
    return qty


def _targets(
    norm: Optional[dict],
    d: date,
    runs: dict,
    ran: Optional[dict] = None,
    members: Optional[dict] = None,
    dept_kg: Optional[dict] = None,
    actual: Optional[dict] = None,
    produced: Optional[dict] = None,
) -> dict:
    """Per-shift target hands for one designation on one date, per kind:

    * **F** — the norm's own shift columns.
    * **M** — Σ units of the scope's machines that WORKED that shift (a worker
      marked in attendance, not stopped all shift) ÷ ``units_per_hand``; a
      legacy row without units_per_hand multiplies the units by
      ``hands_per_machine`` instead.
    * **P** — Σ production of the scope's machines that shift in
      ``qty_uom`` (kg default) ÷ ``qty_per_hand``, over every machine that
      carries production, worked or not; group units are ignored.
    * **N** — None everywhere (blank, excluded from excess/short).
    * **A** — the hands actually present (``actual``), so excess = short = 0.

    ``runs[(date, shift, machine_type_id)]`` is the per-type count of worked
    machines and ``ran[(date, shift)] -> {machine_id: {kg, cuts, winding_kg,
    machine_type_id, dept_id}}`` the per-machine index of them;
    ``produced`` is the same shape over every machine-shift with production
    (when not given, ``ran`` is read instead — callers predating the split);
    all three come from ``_machine_runs``. ``members[machine_group_id] ->
    {machine_id: units}``; ``dept_kg[(date, shift, dept_id)]`` the winding kg
    per winder department.

    An M/P norm with no scope (group / type / department) or no rate cannot be
    computed at all: its targets stay None and it is noted, never a 0 read
    off empty shift columns (TKT-000165). A complete M/P norm whose machines
    never worked / produced is a real 0."""
    blank = {s: None for s in SHIFTS}
    if norm is None:
        return blank
    kind = _kind(norm)
    if kind == "N":
        return blank
    if kind == "A":
        return {s: float((actual or {}).get(s) or 0) for s in SHIFTS}
    if kind == "F":
        return {s: float(norm.get(f"shift_{s}") or 0) for s in SHIFTS}

    scope = _norm_scope(norm)
    if scope is None:
        return blank
    ran = ran or {}
    members = members or {}
    if kind == "M":
        uph = _num(norm.get("units_per_hand"))
        hpm = _num(norm.get("hands_per_machine"))
        if uph:
            return {
                s: _scoped_units(scope, d, s.upper(), runs, ran, members) / uph for s in SHIFTS
            }
        if hpm is None:
            return blank
        return {
            s: _scoped_units(scope, d, s.upper(), runs, ran, members) * hpm for s in SHIFTS
        }
    # P
    qph = _num(norm.get("qty_per_hand"))
    if not qph:
        return blank
    uom = (norm.get("qty_uom") or "kg").strip().lower()
    if uom not in PROD_UOMS:
        uom = "kg"
    dept_kg = dept_kg or {}
    if produced is None:
        produced = ran
    return {
        s: _scoped_qty(scope, uom, d, s.upper(), produced, members, dept_kg) / qph
        for s in SHIFTS
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _r(v) -> Optional[float]:
    return None if v is None else round(float(v), 2)


def _rollup(rows: list, keys: list) -> list:
    """Sum every numeric column of ``rows`` grouped by ``keys``.

    None is "not known", not zero: an unresolvable target adds nothing to a
    group that has other targets, but a group where NOTHING resolved stays None
    rather than collapsing to a 0 that reads as a real, met target."""
    out: dict = {}
    for r in rows:
        k = tuple(r.get(f) for f in keys)
        agg = out.get(k)
        if agg is None:
            agg = {f: r.get(f) for f in keys}
            agg.update({f: None for f in _SUM_FIELDS})
            out[k] = agg
        for f in _SUM_FIELDS:
            v = r.get(f)
            if v is None:
                continue
            agg[f] = float(v) if agg[f] is None else agg[f] + float(v)
    return list(out.values())


def _round_rows(rows: list) -> list:
    for r in rows:
        for f in _SUM_FIELDS:
            r[f] = _r(r.get(f))
    return rows


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _mtd_sums(detail: list, upto: str, keys: list) -> dict:
    """Month-to-date sums of the per-day detail, grouped by ``keys``: every
    detail row dated from the 1st of ``upto``'s month up to and including
    ``upto`` (ISO strings compare in date order). Same None-is-unknown rule as
    ``_rollup`` — a target that never resolved stays None, never 0."""
    start = upto[:8] + "01"
    out: dict = {}
    for r in detail:
        d = r["date"]
        if d < start or d > upto:
            continue
        k = tuple(r.get(f) for f in keys)
        agg = out.get(k)
        if agg is None:
            agg = {f: None for f in _MTD_FIELDS}
            out[k] = agg
        for mtd_field, day_field in _MTD_FIELDS.items():
            v = r.get(day_field)
            if v is None:
                continue
            agg[mtd_field] = float(v) if agg[mtd_field] is None else agg[mtd_field] + float(v)
    return out


def _attach_mtd(rows: list, detail: list, keys: list, upto: Optional[str] = None) -> list:
    """Stamp the four mtd_* columns on ``rows``. With ``upto`` (rollup views)
    every row gets the MTD as at that date; without it (per-date views) each
    row gets the running MTD up to its own ``date``. ``keys`` are the row's
    identity fields (designation / department / nothing for a grand total)."""
    cache: dict = {}
    for r in rows:
        at = upto or r["date"]
        sums = cache.get(at)
        if sums is None:
            sums = _mtd_sums(detail, at, keys)
            cache[at] = sums
        agg = sums.get(tuple(r.get(f) for f in keys), {})
        for f in _MTD_FIELDS:
            r[f] = _r(agg.get(f))
    return rows


def _di_bucket(value) -> str:
    di = (value or "").strip().upper()[:1]
    return {"D": "direct", "I": "indirect"}.get(di, "unclassified")


def _shift_bucket(value) -> str:
    shift = (value or "G").strip().upper()[:1]
    return shift if shift in ("A", "B", "C") else "G"


def _split_sources(value) -> list:
    """``'Attendance,Stoppage,Weaving'`` -> ``['Weaving', 'Attendance', 'Stoppage']``
    (display order = the RUN legs first, then the STOP legs)."""
    names = {n.strip() for n in (value or "").split(",") if n.strip()}
    return sorted(names, key=lambda n: (_SOURCE_ORDER.get(n, len(_SOURCE_ORDER)), n))


def _manual_action(r: dict) -> Optional[str]:
    """The row's manual override in force — ``'ADD'`` / ``'EXCLUDE'`` — or
    None (no override, or a row / fixture without the column)."""
    action = str(r.get("manual_action") or "").strip().upper()
    return action if action in OVERRIDE_ACTIONS else None


def _row_flags(r: dict, sources: list, stoppage: float) -> tuple:
    """``(workers, marked, shift_hours, stopped, worked)`` of one machine-shift
    row.

    A row of ``jute_prod_machine_shift_worked`` carries ``workers_marked``,
    ``shift_hours``, ``stopped_all_shift`` and ``worked`` computed in SQL and
    they are taken as they are. A row without them (a fixture or caller that
    predates the table) is read by the same rule: marked = ``Attendance`` among
    its sources, stopped all shift = the higher of the stoppage hours and the
    breakdown minutes >= the shift's hours (8 when unknown), worked = marked
    and not stopped all shift.

    A manual override has the last word on ``worked`` (spec §12.2), whatever
    the row's own flag says: ADD -> worked, EXCLUDE -> not worked. ``marked``
    and ``stopped`` stay what the sources say."""
    if r.get("workers_marked") is not None:
        workers = int(r["workers_marked"])
        marked = workers > 0
    else:
        workers = int(r.get("hands") or 0)
        marked = workers > 0 or "Attendance" in sources
    shift_hours = _num(r.get("shift_hours"))
    if r.get("stopped_all_shift") is not None:
        stopped = bool(int(r["stopped_all_shift"]))
    else:
        longest = max(stoppage, float(r.get("breakdown_min") or 0) / 60)
        stopped = longest >= (shift_hours or SHIFT_HOURS_FALLBACK)
    if r.get("worked") is not None:
        worked = bool(int(r["worked"]))
    else:
        worked = marked and not stopped
    manual = _manual_action(r)
    if manual == OVERRIDE_ADD:
        worked = True
    elif manual == OVERRIDE_EXCLUDE:
        worked = False
    return workers, marked, shift_hours, stopped, worked


def _machine_runs(run_rows: list, machine_groups: Optional[dict] = None) -> tuple:
    """One row per (date, shift, machine) plus the machine indexes.

    Returns ``(machine_rows, runs, ran, produced)``:

    * ``runs[(date, shift, machine_type_id)]`` — the number of DISTINCT
      machines of that type that WORKED, the multiplier of a type-scoped
      machine norm;
    * ``ran[(date, shift)] -> {machine_id: {"kg", "cuts", "winding_kg",
      "machine_type_id", "dept_id"}}`` — every WORKED machine-shift, what a
      group- or department-scoped machine norm reads;
    * ``produced[(date, shift)]`` — the same shape over every machine-shift
      that carries production (kg or cuts), worked or not: what every
      production norm sums.

    A machine-shift is **counted** iff it WORKED (owner, 2026-10-01): at
    least one worker is marked on the machine in attendance and the machine
    was not stopped for the whole shift — see ``_row_flags``. A production,
    waste, stoppage or breakdown entry alone does not count; those rows are
    listed with ``status`` NO_WORKER_MARKED (STOPPED_ALL_SHIFT for a marked
    machine stopped all shift) so the mill can see what to fix. The table has
    one row per machine-shift; the ``seen`` sets make the indexes immune to a
    duplicate row anyway (the first row that qualifies is kept).

    A manual override outranks all of that (spec §12.2): a row with
    ``manual_action = 'ADD'`` is counted (status ADDED_MANUALLY) even with no
    entry anywhere and even when stopped all shift; one with ``'EXCLUDE'`` is
    not counted (EXCLUDED_MANUALLY) though a worker is marked. Each row
    carries the override (``manual_action``, ``override_id``,
    ``manual_reason``, ``manual_by``, ``manual_at``) and ``suggested``: a
    production entry, no worker marked and no override — what the Add dialog
    proposes. Production norms are untouched: an excluded machine's kg / cuts
    still count, an added one with no entry has none.

    ``machine_groups[machine_id] -> [(group_id, group_name), ...]`` stamps
    ``group_ids`` / ``groups`` on each row (see ``_index_machine_groups``).
    """
    machine_groups = machine_groups or {}
    rows: list = []
    runs: dict = {}
    ran: dict = {}
    produced: dict = {}
    seen: set = set()
    seen_produced: set = set()
    for r in run_rows:
        mid = r.get("machine_id")
        if mid is None:
            continue
        d = r["tran_date"]
        shift = _shift_bucket(r.get("shift"))
        sources = _split_sources(r.get("sources"))
        # Two books record a stoppage (the stoppage-hours entry and the
        # attendance mark's machine stoppage); show the higher, never the sum.
        stoppage = max(
            float(r.get("stoppage_hours") or 0), float(r.get("att_stoppage_hours") or 0)
        )
        workers, marked, shift_hours, stopped, counted = _row_flags(r, sources, stoppage)
        manual = _manual_action(r)
        if manual == OVERRIDE_ADD:
            status = STATUS_ADDED_MANUALLY
        elif manual == OVERRIDE_EXCLUDE:
            status = STATUS_EXCLUDED_MANUALLY
        elif counted:
            status = STATUS_WORKED
        elif marked and stopped:
            status = STATUS_STOPPED_ALL_SHIFT
        else:
            status = STATUS_NO_WORKER_MARKED
        manual_at = r.get("manual_at") if manual else None
        mt = r.get("machine_type_id")
        key = (d, shift, int(mid))
        kg = _num(r.get("prod_kg"))
        cuts = _num(r.get("prod_cuts"))
        entry = {
            "kg": kg,
            "cuts": cuts,
            "winding_kg": _num(r.get("winding_kg")),
            "machine_type_id": None if mt is None else int(mt),
            "dept_id": None if r.get("dept_id") is None else int(r["dept_id"]),
        }
        if counted and key not in seen:
            seen.add(key)
            if mt is not None:
                runs[(d, shift, int(mt))] = runs.get((d, shift, int(mt)), 0) + 1
            ran.setdefault((d, shift), {})[int(mid)] = entry
        if (kg is not None or cuts is not None) and key not in seen_produced:
            seen_produced.add(key)
            produced.setdefault((d, shift), {})[int(mid)] = entry
        groups = machine_groups.get(int(mid), [])
        rows.append(
            {
                "date": d.isoformat(),
                "shift": shift,
                "dept_id": r.get("dept_id"),
                "dept": r.get("dept"),
                "machine_type_id": mt,
                "machine_type": r.get("machine_type_name"),
                "machine_id": int(mid),
                "machine": r.get("machine_name") or r.get("mech_code") or f"MC{mid}",
                "mech_code": r.get("mech_code"),
                "sources": ", ".join(sources),
                "hands": None if r.get("hands") is None else int(r["hands"]),
                "workers": workers,
                "stoppage_hours": _r(stoppage),
                "breakdown_min": _r(r.get("breakdown_min") or 0),
                "shift_hours": _r(shift_hours),
                "prod_kg": _r(kg),
                "prod_cuts": _r(cuts),
                "counted": counted,
                "status": status,
                "group_ids": [gid for gid, _name in groups],
                "groups": ", ".join(name for _gid, name in groups),
                # Manual override in force (None x5 when there is none).
                "manual_action": manual,
                "override_id": (
                    int(r["override_id"]) if manual and r.get("override_id") is not None else None
                ),
                "manual_reason": r.get("manual_reason") if manual else None,
                "manual_by": r.get("manual_by") if manual else None,
                "manual_at": (
                    manual_at.isoformat() if hasattr(manual_at, "isoformat") else manual_at
                ),
                # Proposed by the Add dialog: production booked on the
                # machine-shift, nobody marked on it, nothing decided yet.
                "suggested": (
                    manual is None
                    and not marked
                    and any(name in PRODUCTION_SOURCES for name in sources)
                ),
            }
        )
    rows.sort(
        key=lambda x: (
            x["date"],
            _SHIFT_ORDER.get(x["shift"], 9),
            x["machine_type"] or "",
            x["machine"] or "",
            x["machine_id"],
        )
    )
    return rows, runs, ran, produced


def _has_production_entry(machine_row: dict) -> bool:
    """True when the machine-shift was seen in a PRODUCTION entry (weaving,
    doff, spinning, beaming, finishing, spreader, drawing, winding, bale
    press) — not merely in attendance, waste, stoppage or breakdown."""
    names = {n.strip() for n in (machine_row.get("sources") or "").split(",")}
    return any(n in PRODUCTION_SOURCES for n in names)


def _machine_type_summary(machine_rows: list) -> list:
    """Machines per (date, machine type) by shift — the sheet's header block
    and, read across, the very numbers a type-scoped machine norm multiplies.

    ``run_a`` .. ``run_g`` / ``machine_shifts`` count the WORKED machine-shifts
    (the manually added ones included) and ``machines`` the distinct machines
    behind them (a loom in A and B is two machine-shifts but one machine).
    ``stopped_all_shift``, ``no_worker_marked`` and ``excluded_manually``
    count the listed machine-shifts that did NOT count, so a type whose
    machines produced with nobody marked on them still shows up — with zero
    run and the number to fix; ``added_manually`` says how many of the
    counted ones are manual additions. ``prod_kg`` / ``prod_cuts`` add
    up the production of EVERY listed row of the type, worked or not (what a
    production norm sums; None when no row of the type carried any)."""
    out: dict = {}
    for r in machine_rows:
        k = (r["date"], r.get("machine_type_id"))
        agg = out.get(k)
        if agg is None:
            agg = {
                "date": r["date"],
                "machine_type_id": r.get("machine_type_id"),
                "machine_type": r.get("machine_type"),
                "run_a": 0,
                "run_b": 0,
                "run_c": 0,
                "run_g": 0,
                "machine_shifts": 0,
                "stopped_all_shift": 0,
                "no_worker_marked": 0,
                "added_manually": 0,
                "excluded_manually": 0,
                "prod_kg": None,
                "prod_cuts": None,
                "_machines": set(),
            }
            out[k] = agg
        if r["counted"]:
            agg[f"run_{r['shift'].lower()}"] += 1
            agg["machine_shifts"] += 1
            agg["_machines"].add(r["machine_id"])
            if r.get("status") == STATUS_ADDED_MANUALLY:
                agg["added_manually"] += 1
        elif r.get("status") == STATUS_EXCLUDED_MANUALLY:
            agg["excluded_manually"] += 1
        elif r.get("status") == STATUS_STOPPED_ALL_SHIFT:
            agg["stopped_all_shift"] += 1
        else:
            agg["no_worker_marked"] += 1
        for f in ("prod_kg", "prod_cuts"):
            v = r.get(f)
            if v is not None:
                agg[f] = float(v) if agg[f] is None else agg[f] + float(v)
    rows = []
    for agg in out.values():
        agg["machines"] = len(agg.pop("_machines"))
        agg["prod_kg"] = _r(agg["prod_kg"])
        agg["prod_cuts"] = _r(agg["prod_cuts"])
        rows.append(agg)
    rows.sort(key=lambda x: (x["date"], x["machine_type"] or "", x["machine_type_id"] or 0))
    return rows


def _index_members(member_rows: list) -> dict:
    """``get_hands_group_members_query`` rows -> ``{group_id: {machine_id: units}}``."""
    members: dict = {}
    for r in member_rows:
        gid, mid = r.get("machine_group_id"), r.get("machine_id")
        if gid is None or mid is None:
            continue
        members.setdefault(int(gid), {})[int(mid)] = float(r.get("units") or 0)
    return members


def _index_machine_groups(member_rows: list, norm_rows: list) -> dict:
    """``{machine_id: [(group_id, group_name), ...]}`` — the active machine
    groups of the company / branch each machine belongs to, ordered by name,
    for the ``group_ids`` / ``groups`` of a Machines Worked row. The name
    comes from the member row (``get_hands_group_members_query``), else from a
    norm scoped to the group, else ``#id``."""
    names: dict = {}
    for n in norm_rows:
        if n.get("machine_group_id") is not None and n.get("machine_group"):
            names[int(n["machine_group_id"])] = n["machine_group"]
    for r in member_rows:
        if r.get("machine_group_id") is not None and r.get("group_name"):
            names[int(r["machine_group_id"])] = r["group_name"]
    out: dict = {}
    for r in member_rows:
        gid, mid = r.get("machine_group_id"), r.get("machine_id")
        if gid is None or mid is None:
            continue
        entry = (int(gid), names.get(int(gid)) or f"#{int(gid)}")
        if entry not in out.setdefault(int(mid), []):
            out[int(mid)].append(entry)
    for groups in out.values():
        groups.sort(key=lambda g: (g[1], g[0]))
    return out


def _norm_scopes(norm_rows: list, d_from: date, d_to: date, names: dict) -> list:
    """The ``scopes`` of the Machines Worked view: one entry per machine (M) /
    production (P) norm in force on any date of the window that names a
    scope, so the tab can filter the machines by designation — a ``group``
    scope matches the rows whose ``group_ids`` hold ``scope_id``, a ``type``
    scope those of that ``machine_type_id``, a ``dept`` scope those of that
    ``dept_id``. An incomplete M/P norm (no scope) has no machines to filter
    and is left out. Each entry carries the norm's rate: ``units_per_hand``
    (M; a legacy row's 1 / hands_per_machine) or ``qty_per_hand`` +
    ``qty_uom`` (P; 'kg' unless the norm says 'cuts', as ``_targets`` reads
    it), and ``unit_label`` — what a GROUP's units count (machine / point /
    spindle ...), None for a type or department scope. ``names`` are the
    fallbacks (``designation`` / ``group`` / ``type`` / ``dept`` /
    ``unit_label`` -> ``{id: value}``) for norm rows that do not carry them."""
    out: dict = {}
    for did, rows in _norms_by_designation(norm_rows).items():
        # The norm in force on the first day, plus every revision that comes
        # into force inside the window.
        days = {d_from} | {r["effective_date"] for r in rows if d_from < r["effective_date"] <= d_to}
        for d in sorted(days):
            norm = _norm_for(rows, d)
            if norm is None:
                continue
            kind = _kind(norm)
            scope = _norm_scope(norm)
            if kind not in _MACHINE_KINDS or scope is None:
                continue
            scope_kind, sid = scope
            unit_label = None
            if scope_kind == "group":
                scope_name = norm.get("machine_group") or names.get("group", {}).get(sid)
                unit_label = norm.get("unit_label") or names.get("unit_label", {}).get(sid)
            elif scope_kind == "type":
                scope_name = norm.get("machine_type") or names.get("type", {}).get(sid)
            else:
                scope_name = norm.get("scope_dept") or names.get("dept", {}).get(sid)
            uph = _num(norm.get("units_per_hand"))
            hpm = _num(norm.get("hands_per_machine"))
            if kind == "M" and not uph and hpm and hpm > 0:
                # Legacy row: the rate is stored as hands per machine only.
                uph = 1 / hpm
            uom = None
            if kind == "P":
                uom = (norm.get("qty_uom") or "kg").strip().lower()
                if uom not in PROD_UOMS:
                    uom = "kg"
            # Later revisions overwrite earlier ones of the same scope.
            out[(did, kind, scope_kind, sid)] = {
                "designation_id": did,
                "designation": (
                    norm.get("designation") or names.get("designation", {}).get(did) or str(did)
                ),
                "norm_kind": kind,
                "scope": scope_kind,
                "scope_id": sid,
                "scope_name": scope_name or f"#{sid}",
                "units_per_hand": None if kind != "M" or not uph else round(uph, 3),
                "qty_per_hand": (
                    None
                    if kind != "P" or norm.get("qty_per_hand") is None
                    else round(float(norm["qty_per_hand"]), 3)
                ),
                "qty_uom": uom,
                "unit_label": unit_label,
            }
    scopes = list(out.values())
    scopes.sort(
        key=lambda x: (x["designation"], x["designation_id"], x["norm_kind"], x["scope"], x["scope_id"])
    )
    return scopes


def _index_dept_production(dept_rows: list) -> tuple:
    """``get_hands_dept_production_query`` rows -> ``({(date, shift, dept_id):
    kg}, {date: unattributed kg})``; a row with no department is winding kg
    nobody can be given, reported per date in a note."""
    dept_kg: dict = {}
    unattributed: dict = {}
    for r in dept_rows:
        d = r["tran_date"]
        kg = float(r.get("prod_kg") or 0)
        if r.get("dept_id") is None:
            unattributed[d] = unattributed.get(d, 0.0) + kg
            continue
        k = (d, _shift_bucket(r.get("shift")), int(r["dept_id"]))
        dept_kg[k] = dept_kg.get(k, 0.0) + kg
    return dept_kg, unattributed


def _detail_rows(
    actual_rows: list,
    norm_rows: list,
    runs: dict,
    ran: Optional[dict] = None,
    members: Optional[dict] = None,
    dept_kg: Optional[dict] = None,
    produced: Optional[dict] = None,
) -> tuple:
    """Build the per (date x designation) grid — the grain every view rolls up
    from. ``runs`` / ``ran`` are the worked-machines indexes and ``produced``
    the production index from ``_machine_runs``, ``members`` the group members
    and ``dept_kg`` the winding kg per winder department (see ``_targets``).
    Returns ``(rows, missing_norm_designations, variable_types_used)``."""
    norms = _norms_by_designation(norm_rows)
    ran = ran or {}
    members = members or {}
    dept_kg = dept_kg or {}
    if produced is None:
        produced = ran

    cells: dict = {}
    for r in actual_rows:
        key = (r["tran_date"], r.get("designation_id"))
        cell = cells.get(key)
        if cell is None:
            cell = {
                "date": r["tran_date"],
                "designation_id": r.get("designation_id"),
                "designation": r.get("designation"),
                "dept_id": r.get("dept_id"),
                "dept": r.get("dept"),
                "desig_direct_indirect": r.get("desig_direct_indirect"),
                "hands": {s: 0.0 for s in SHIFTS},
            }
            cells[key] = cell
        cell["hands"][_shift_bucket(r.get("shift")).lower()] += float(r.get("hands") or 0)

    rows: list = []
    missing_norm: dict = {}
    variable_types: set = set()
    for cell in cells.values():
        did = cell["designation_id"]
        norm = _norm_for(norms.get(int(did), []), cell["date"]) if did is not None else None
        if norm is None:
            missing_norm[did] = cell.get("designation")
        elif norm.get("machine_type_id") is not None:
            variable_types.add(int(norm["machine_type_id"]))

        targets = _targets(
            norm, cell["date"], runs, ran, members, dept_kg, actual=cell["hands"],
            produced=produced,
        )
        actual_vals = [cell["hands"][s] for s in SHIFTS]
        target_vals = [targets[s] for s in SHIFTS]
        total_actual = sum(actual_vals)
        known = [t for t in target_vals if t is not None]
        total_target = sum(known) if known else None

        row = {
            "date": cell["date"].isoformat(),
            "dept_id": cell.get("dept_id"),
            "dept": cell.get("dept"),
            "designation_id": did,
            "designation": cell.get("designation"),
            "direct_indirect": (
                (norm or {}).get("direct_indirect") or cell.get("desig_direct_indirect")
            ),
            "variable_fixed": (norm or {}).get("variable_fixed"),
            "norm_kind": None if norm is None else _kind(norm),
            "norms_text": (norm or {}).get("norms_text"),
            "machine_type_id": (norm or {}).get("machine_type_id"),
            "machine_group_id": (norm or {}).get("machine_group_id"),
            "machine_group": (norm or {}).get("machine_group"),
            "scope_dept_id": (norm or {}).get("scope_dept_id"),
            # 4 dp, as stored: '36 MC = 1 HAND' is 0.0278, which _r would blur.
            "hands_per_machine": (
                None
                if (norm or {}).get("hands_per_machine") is None
                else round(float(norm["hands_per_machine"]), 4)
            ),
            # 3 dp, as stored (DECIMAL(9,3) / DECIMAL(12,3)).
            "units_per_hand": (
                None
                if (norm or {}).get("units_per_hand") is None
                else round(float(norm["units_per_hand"]), 3)
            ),
            "qty_per_hand": (
                None
                if (norm or {}).get("qty_per_hand") is None
                else round(float(norm["qty_per_hand"]), 3)
            ),
            "qty_uom": (norm or {}).get("qty_uom"),
            "total_actual": total_actual,
            "total_target": total_target,
            "excess": None if total_target is None else max(total_actual - total_target, 0),
            "short": None if total_target is None else max(total_target - total_actual, 0),
        }
        for s in SHIFTS:
            row[f"shift_{s}"] = cell["hands"][s]
            row[f"target_{s}"] = targets[s]
        rows.append(row)

    # Department, then occupation, within each date (TKT-000167) — the order the
    # Detailed view's department subtotals are placed in on the frontend.
    rows.sort(
        key=lambda r: (
            r["date"],
            (r["dept"] or ""),
            r["dept_id"] or 0,
            (r["designation"] or ""),
            r["designation_id"] or 0,
        )
    )
    return rows, missing_norm, variable_types


def _by_date(detail: list) -> list:
    rows = _rollup(detail, ["date"])
    index = {r["date"]: r for r in rows}
    for r in rows:
        for b in _DI_BUCKETS:
            r[f"{b}_actual"] = 0.0
            r[f"{b}_target"] = None
    for d in detail:
        agg = index[d["date"]]
        b = _di_bucket(d.get("direct_indirect"))
        agg[f"{b}_actual"] += float(d.get("total_actual") or 0)
        tgt = d.get("total_target")
        if tgt is not None:
            agg[f"{b}_target"] = float(tgt) + float(agg[f"{b}_target"] or 0)
    _round_rows(rows)
    for r in rows:
        for b in _DI_BUCKETS:
            r[f"{b}_actual"] = _r(r[f"{b}_actual"])
            r[f"{b}_target"] = _r(r[f"{b}_target"])
    rows.sort(key=lambda r: r["date"])
    return rows


def _detailed_totals(detail: list, detail_all: list) -> tuple:
    """Department subtotals and day totals of the Detailed view (TKT-000167).

    Rolled up from the same per-designation-day grid as the rows, with the same
    summed-never-netted excess/short and None-is-unknown targets, and carried
    BESIDE ``rows`` (like ``grand_total``) so ``rows`` stays one row per
    designation per date. Each total carries the running MTD up to its own
    date — per department for the subtotals, for the whole selection for the
    day totals — exactly as the rows and the By Date view do.
    Returns ``(dept_totals, day_totals)``."""
    dept_totals = _round_rows(_rollup(detail, ["date", "dept_id", "dept"]))
    dept_totals.sort(key=lambda r: (r["date"], (r.get("dept") or ""), r.get("dept_id") or 0))
    _attach_mtd(dept_totals, detail_all, ["dept_id"])
    day_totals = _round_rows(_rollup(detail, ["date"]))
    day_totals.sort(key=lambda r: r["date"])
    _attach_mtd(day_totals, detail_all, [])
    return dept_totals, day_totals


def build_hands_report(
    actual_rows: list,
    norm_rows: list,
    run_rows: list,
    *,
    co_id: int,
    branch_id: int,
    d_from: date,
    d_to: date,
    view: str,
    dept_id: Optional[int] = None,
    direct_indirect: Optional[str] = None,
    members: Optional[list] = None,
    dept_production: Optional[list] = None,
    can_edit: bool = False,
) -> dict:
    """Pure assembly of the whole report from the result sets — kept free of
    the Session so it can be unit-tested without a database. ``members`` are
    the ``get_hands_group_members_query`` rows and ``dept_production`` the
    ``get_hands_dept_production_query`` rows (both optional: a tenant without
    groups / winding, or a caller predating Hands Norms v2). ``run_rows`` are
    the ``get_machine_shift_worked_rows_query`` rows — one per machine-shift
    with its ``worked`` flag and its manual override, if any; rows without the
    flag are read by the same rule (``_row_flags``). ``can_edit`` is the
    caller's right to add / exclude machines (the endpoint resolves it), put
    on ``meta`` as it is."""
    members_idx = _index_members(members or [])
    machine_rows, runs, ran, produced = _machine_runs(
        run_rows, _index_machine_groups(members or [], norm_rows)
    )
    dept_kg, unattributed_kg = _index_dept_production(dept_production or [])
    # ``detail_all`` may start before d_from (the endpoint fetches from the 1st
    # of the From Date's month so the MTD columns can be built); ``detail`` is
    # the requested window and is what every view and total is made of.
    # (the variable machine types are re-derived below from the window's rows)
    detail_all, missing_norm, _ = _detail_rows(
        actual_rows, norm_rows, runs, ran, members_idx, dept_kg, produced
    )

    # Filters run AFTER norm resolution: the norm's direct_indirect wins over
    # the designation master's, so filtering earlier would use the wrong value.
    if dept_id is not None:
        detail_all = [r for r in detail_all if r.get("dept_id") == dept_id]
    if direct_indirect:
        want = direct_indirect.strip().upper()[:1]
        detail_all = [
            r for r in detail_all if (r.get("direct_indirect") or "").strip().upper()[:1] == want
        ]
    from_iso, to_iso = d_from.isoformat(), d_to.isoformat()
    detail = [r for r in detail_all if from_iso <= r["date"] <= to_iso]
    # Notes speak about the requested window only.
    in_window = {r["designation_id"] for r in detail}
    missing_norm = {k: v for k, v in missing_norm.items() if k in in_window}
    # Machine rows are fetched from the 1st of the month too: ``runs`` / ``ran``
    # / ``produced`` keep them all (the MTD variable targets need the earlier
    # days), but the Machines Worked sheet, its summary, the meta counts and
    # the notes cover the requested window only (TKT-000167).
    machine_rows = [r for r in machine_rows if from_iso <= r["date"] <= to_iso]
    worked_types = {mt for (d, _s, mt) in runs if d_from <= d <= d_to}
    worked_machines: set = set()
    for (d, _s), entries in ran.items():
        if d_from <= d <= d_to:
            worked_machines.update(entries)
    produced_types: set = set()
    produced_machines: set = set()
    for (d, _s), entries in produced.items():
        if d_from <= d <= d_to:
            produced_machines.update(entries)
            produced_types.update(
                e["machine_type_id"] for e in entries.values() if e["machine_type_id"] is not None
            )
    # The scopes of the window's machine / production norms, to say which of
    # them saw no machine worked (M) / no production (P): their targets are 0,
    # not blank. ``(kind, id)`` so an M and a P norm on one type are judged
    # each by its own rule.
    scoped = [r for r in detail if r.get("norm_kind") in _MACHINE_KINDS]
    variable_types = {
        (r["norm_kind"], int(r["machine_type_id"]))
        for r in scoped
        if r.get("machine_type_id") is not None and r.get("machine_group_id") is None
    }
    variable_groups = {
        (
            r["norm_kind"],
            int(r["machine_group_id"]),
            r.get("machine_group") or f"#{r['machine_group_id']}",
        )
        for r in scoped
        if r.get("machine_group_id") is not None
    }
    # Machine-shifts that did not count, for the notes and the two meta
    # figures ``stopped_all_shift`` / ``no_worker_marked``: counted over the
    # rows the Machines Worked view returns, so they follow its department
    # filter exactly like the rows do (the manning views list no machine rows
    # and count the whole window).
    shown_machine_rows = machine_rows
    if view == "machines_run" and dept_id is not None:
        shown_machine_rows = [r for r in machine_rows if r.get("dept_id") == dept_id]
    stopped_rows = [r for r in shown_machine_rows if r["status"] == STATUS_STOPPED_ALL_SHIFT]
    no_worker_rows = [r for r in shown_machine_rows if r["status"] == STATUS_NO_WORKER_MARKED]
    # Manual overrides in force (spec §12), over the same rows.
    added_rows = [r for r in shown_machine_rows if r["status"] == STATUS_ADDED_MANUALLY]
    excluded_rows = [r for r in shown_machine_rows if r["status"] == STATUS_EXCLUDED_MANUALLY]
    if view == "machines_run":
        notes = [
            "One row per date, shift and machine from every machine-keyed entry "
            f"({', '.join(RUN_SOURCES + STOP_SOURCES)}); a machine present in several "
            "is listed once.",
            "A machine counts as WORKED in a shift only when at least one worker is "
            "marked on it in attendance and it was not stopped for the whole shift — "
            "worked machine-shifts are the multiplier of the machine-based (M) hands "
            "norms. A production, waste, stoppage or breakdown entry alone does not "
            "make a machine count (status No worker marked).",
            "Stopped all shift = the higher of the stoppage-hours entry, the "
            "attendance-marked machine stoppage and the breakdown minutes covers the "
            "shift's hours (the full-shift spell of the shift letter, "
            f"{SHIFT_HOURS_FALLBACK} when the branch has none).",
            "A machine attendance missed can be added manually, and one marked "
            "wrongly excluded, by a role with edit access to this report — each with "
            "a reason (status Added manually / Excluded manually). An added machine "
            "counts even with no entry and even when the books say stopped all shift.",
            "Workers marked = distinct workers marked on the machine in attendance.",
            "Stoppage hours = the higher of the stoppage-hours entry and the "
            "attendance-marked machine stoppage.",
            "Prod kg / cuts = the production the entries booked on that machine-shift "
            "(spinning doffs, beaming, finishing, spreader rolls, bale press, winding "
            "doffs by the winder's quality-row machine; weaving in cuts) — the "
            "numerator of a production-based (P) hands norm, counted whether or not "
            "a worker was marked on the machine.",
        ]
    else:
        notes = [
            "hands = (working_hours - idle_hours) / 8 for every shift, C included.",
            "Rejected attendance rows are excluded; rows with no status (legacy or "
            "bio-imported) are counted.",
            "Rows are attendance-driven: a designation with a norm but no attendance "
            "in the window is not listed.",
            "Machine (M) targets = units of the norm's machines (group / type / "
            "department) that worked that date and shift ÷ units per hand. A machine "
            "counts as worked only when a worker is marked on it in attendance and "
            "it was not stopped all shift, or when it was added manually (see the "
            "Machines Worked tab). Production "
            "(P) targets = kg or cuts produced by the norm's machines that shift ÷ "
            "qty per hand, whether or not a worker was marked.",
            f"MTD columns = 1st of the To Date's month ({_month_start(d_to).isoformat()}) "
            "up to the To Date; on the Detailed and By Date views each row's MTD runs "
            "up to that row's own date.",
        ]
    # Production booked on a machine nobody was marked on: the machine does not
    # count for a machine target until the marking is fixed — said by machine
    # type so the mill knows where to look.
    unmarked = [r for r in no_worker_rows if _has_production_entry(r)]
    if unmarked:
        by_type: dict = {}
        for r in unmarked:
            name = r.get("machine_type") or (
                "(no machine type)"
                if r.get("machine_type_id") is None
                else f"type {r['machine_type_id']}"
            )
            by_type[name] = by_type.get(name, 0) + 1
        ranked = sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))
        more = "" if len(ranked) <= _NOTE_SAMPLE else f" (+{len(ranked) - _NOTE_SAMPLE} more types)"
        notes.append(
            f"{len(unmarked)} machine-shift(s) have production entered but no worker "
            "marked in attendance — not counted for machine targets: "
            f"{', '.join(f'{name} {n}' for name, n in ranked[:_NOTE_SAMPLE])}{more}"
        )
    if stopped_rows:
        notes.append(
            f"{len(stopped_rows)} machine-shift(s) had a worker marked but were stopped "
            "the whole shift — not counted."
        )
    if added_rows or excluded_rows:
        notes.append(
            f"{len(added_rows)} machine-shift(s) were added manually and "
            f"{len(excluded_rows)} excluded manually — see the Machines Worked tab."
        )
    if missing_norm:
        names = [n for n in missing_norm.values() if n]
        sample = ", ".join(sorted(names)[:_NOTE_SAMPLE]) or "(unnamed designations)"
        more = "" if len(missing_norm) <= _NOTE_SAMPLE else f" (+{len(missing_norm) - _NOTE_SAMPLE} more)"
        notes.append(
            f"{len(missing_norm)} designation(s) in attendance have no jute_prod_hands_norms_mst "
            f"row — target left blank: {sample}{more}"
        )
    # An M scope is idle when none of its machines worked, a P scope when none
    # of them carries production.
    unlinked = sorted(
        {
            t
            for kind, t in variable_types
            if t not in (produced_types if kind == "P" else worked_types)
        }
    )
    idle_groups = sorted(
        {
            name
            for kind, gid, name in variable_groups
            if not (
                set(members_idx.get(gid, {}))
                & (produced_machines if kind == "P" else worked_machines)
            )
        }
    )
    if unlinked or idle_groups:
        parts = []
        if unlinked:
            parts.append(f"type(s) {', '.join(str(t) for t in unlinked)}")
        if idle_groups:
            parts.append(f"group(s) {', '.join(idle_groups)}")
        notes.append(
            f"No machine of {' / '.join(parts)} worked (machine norms) or carried "
            "production (production norms) in the window — their machine / "
            "production targets are 0."
        )
    # TKT-000165: a per-machine / production rate with no machine scope to
    # count machines of. Named apart from the generic note below because the
    # fix is one specific field (link the norm to a group, type or department).
    rate_unlinked = sorted(
        {
            r["designation"] or str(r["designation_id"])
            for r in detail
            if r["total_target"] is None
            and r.get("norm_kind") in _MACHINE_KINDS
            and (
                r.get("hands_per_machine") is not None
                or r.get("units_per_hand") is not None
                or r.get("qty_per_hand") is not None
            )
            and r.get("machine_type_id") is None
            and r.get("machine_group_id") is None
            and r.get("scope_dept_id") is None
        }
    )
    if rate_unlinked:
        more = (
            ""
            if len(rate_unlinked) <= _NOTE_SAMPLE
            else f" (+{len(rate_unlinked) - _NOTE_SAMPLE} more)"
        )
        notes.append(
            f"{len(rate_unlinked)} designation(s) have a per-machine / production norm "
            "(units or qty per hand) that is not linked to a machine type, group or "
            "department, so the machines run cannot be counted — target left blank "
            f"until the norm is linked: {', '.join(rate_unlinked[:_NOTE_SAMPLE])}{more}"
        )
    unresolvable = sorted(
        {
            r["designation"] or str(r["designation_id"])
            for r in detail
            if r["total_target"] is None
            and r.get("norm_kind") in _MACHINE_KINDS
            and (r["designation"] or str(r["designation_id"])) not in rate_unlinked
        }
    )
    if unresolvable:
        notes.append(
            "Variable norm (kind M / P) with no machine link (group / type / "
            "department) or rate (units / qty per hand) — target cannot be computed "
            f"for: {', '.join(unresolvable[:_NOTE_SAMPLE])}"
        )
    # Kinds N and A are blank / actual BY DESIGN — named so nobody hunts for a
    # missing configuration behind them.
    no_norm = sorted(
        {r["designation"] or str(r["designation_id"]) for r in detail if r.get("norm_kind") == "N"}
    )
    if no_norm:
        more = "" if len(no_norm) <= _NOTE_SAMPLE else f" (+{len(no_norm) - _NOTE_SAMPLE} more)"
        notes.append(
            f"{len(no_norm)} designation(s) have no norm by design (kind N) — target "
            f"blank, excluded from excess/short: {', '.join(no_norm[:_NOTE_SAMPLE])}{more}"
        )
    as_actual = sorted(
        {r["designation"] or str(r["designation_id"]) for r in detail if r.get("norm_kind") == "A"}
    )
    if as_actual:
        more = "" if len(as_actual) <= _NOTE_SAMPLE else f" (+{len(as_actual) - _NOTE_SAMPLE} more)"
        notes.append(
            f"{len(as_actual)} designation(s) are targeted as actual (kind A): "
            f"{', '.join(as_actual[:_NOTE_SAMPLE])}{more}"
        )
    lost_kg = sum(kg for d, kg in unattributed_kg.items() if d_from <= d <= d_to)
    if lost_kg > 0:
        notes.append(
            f"{_r(lost_kg)} kg of winding doffs had no machine attribution (and no "
            "attendance row to place the winder in a department) and were left out "
            "of production targets"
        )

    grand_total = None
    type_summary = None
    scopes = None
    dept_totals = day_totals = None
    if view == "machines_run":
        rows = shown_machine_rows
        type_summary = _machine_type_summary(rows)
        # Display-name fallbacks for norm rows that do not carry the names.
        scope_names = {
            "designation": {
                int(r["designation_id"]): r.get("designation")
                for r in actual_rows
                if r.get("designation_id") is not None and r.get("designation")
            },
            "type": {
                int(r["machine_type_id"]): r.get("machine_type_name")
                for r in run_rows
                if r.get("machine_type_id") is not None and r.get("machine_type_name")
            },
            "dept": {
                int(r["dept_id"]): r.get("dept")
                for r in list(actual_rows) + list(run_rows)
                if r.get("dept_id") is not None and r.get("dept")
            },
            "group": {
                int(r["machine_group_id"]): r.get("group_name")
                for r in (members or [])
                if r.get("machine_group_id") is not None and r.get("group_name")
            },
            "unit_label": {
                int(r["machine_group_id"]): r.get("unit_label")
                for r in (members or [])
                if r.get("machine_group_id") is not None and r.get("unit_label")
            },
        }
        scopes = _norm_scopes(norm_rows, d_from, d_to, scope_names)
    elif view == "detailed":
        rows = _attach_mtd(_round_rows(detail), detail_all, ["designation_id"])
        # Totals of the rounded rows, so each subtotal is the sum of what is
        # printed above it — the same basis as this view's meta totals.
        dept_totals, day_totals = _detailed_totals(detail, detail_all)
    elif view == "by_date":
        rows = _attach_mtd(_by_date(detail), detail_all, [])
    elif view == "by_occupation":
        rows = _round_rows(
            _rollup(
                detail,
                [
                    "designation_id",
                    "designation",
                    "dept_id",
                    "dept",
                    "direct_indirect",
                    "variable_fixed",
                    "norm_kind",
                    "machine_group",
                    "norms_text",
                ],
            )
        )
        rows.sort(key=lambda r: ((r.get("dept") or ""), (r.get("designation") or "")))
        _attach_mtd(rows, detail_all, ["designation_id"], upto=to_iso)
    else:  # by_department
        rows = _round_rows(_rollup(detail, ["dept_id", "dept"]))
        rows.sort(key=lambda r: (r.get("dept") or ""))
        _attach_mtd(rows, detail_all, ["dept_id"], upto=to_iso)
        grand_total = _round_rows(_rollup(detail, []))
        grand_total = grand_total[0] if grand_total else {f: 0.0 for f in _SUM_FIELDS}
        grand_total["dept"] = "GRAND TOTAL"
        _attach_mtd([grand_total], detail_all, [], upto=to_iso)

    totals = _rollup(detail, [])
    totals = totals[0] if totals else {f: 0.0 for f in _SUM_FIELDS}
    mtd_totals = _mtd_sums(detail_all, to_iso, []).get((), {})
    counted_rows = [r for r in machine_rows if r["counted"]]
    # When the rows were last rebuilt from the sources (the table is as fresh
    # as the last open); None for rows that carry no stamp.
    stamps = [r["synced_at"] for r in run_rows if r.get("synced_at") is not None]
    synced_at = max(stamps) if stamps else None

    data = {
        "view": view,
        "rows": rows,
        "meta": {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "view": view,
            "row_count": len(rows),
            "total_actual": _r(totals["total_actual"]),
            "total_target": _r(totals["total_target"]),
            "total_excess": _r(totals["excess"]),
            "total_short": _r(totals["short"]),
            # Month-to-date as at the To Date (TKT-000154), same per-day rule.
            "mtd_from": _month_start(d_to).isoformat(),
            "mtd_actual": _r(mtd_totals.get("mtd_actual")),
            "mtd_target": _r(mtd_totals.get("mtd_target")),
            "mtd_excess": _r(mtd_totals.get("mtd_excess")),
            "mtd_short": _r(mtd_totals.get("mtd_short")),
            # Not applicable to a manning report — kept for contract uniformity
            # with the sibling low-producer reports.
            "filtered_avg_eff": None,
            "overall_avg_eff": None,
            # Machines Worked figures. Worked machine-shifts (the manually
            # added ones included, the excluded ones not), the distinct
            # machines behind them and the rows listed but not counted are
            # for the whole window (unfiltered by dept, as before);
            # ``stopped_all_shift`` / ``no_worker_marked`` /
            # ``excluded_manually`` split the not-counted rows by status and
            # ``added_manually`` counts the manual additions, all four over
            # the rows the machines view returns (so they follow its dept
            # filter).
            "machine_shifts_run": len(counted_rows),
            "machines_run": len({r["machine_id"] for r in counted_rows}),
            "stopped_only_rows": len(machine_rows) - len(counted_rows),
            "stopped_all_shift": len(stopped_rows),
            "no_worker_marked": len(no_worker_rows),
            "added_manually": len(added_rows),
            "excluded_manually": len(excluded_rows),
            # May the caller add / exclude machines (Edit on the report's menu).
            "can_edit": bool(can_edit),
            "synced_at": (
                synced_at.isoformat() if hasattr(synced_at, "isoformat") else synced_at
            ),
            "notes": notes,
        },
    }
    if grand_total is not None:
        data["grand_total"] = grand_total
    if type_summary is not None:
        data["type_summary"] = type_summary
    if scopes is not None:
        # Machines Worked view only: the M / P norms' machine scopes, for the
        # tab's Designation filter.
        data["scopes"] = scopes
    if dept_totals is not None:
        # Detailed view only (TKT-000167).
        data["dept_totals"] = dept_totals
        data["day_totals"] = day_totals
    return data


# ---------------------------------------------------------------------------
# Rebuild of jute_prod_machine_shift_worked
# ---------------------------------------------------------------------------


def _mysql_errno(exc: Exception) -> Optional[int]:
    """The MySQL error number behind a SQLAlchemy / PyMySQL exception
    (``exc.orig.args[0]``), or None when there is none to read."""
    orig = getattr(exc, "orig", None) or exc
    args = getattr(orig, "args", None) or ()
    try:
        return int(args[0])
    except (IndexError, TypeError, ValueError):
        return None


def rebuild_machine_shift_worked(
    db: Session, co_id: int, branch_id: int, d_from: date, d_to: date
) -> bool:
    """Rebuild the rows of ``jute_prod_machine_shift_worked`` for one company /
    branch and the window ``d_from .. d_to`` (both inclusive) from the sources.

    ONE transaction: DELETE the window, INSERT ... SELECT it again from every
    machine-keyed leg (``get_machine_shift_worked_insert_query``), then apply
    the manual overrides in force in the window (spec §12.2) — the ADD rows
    are upserted (``get_machine_shift_worked_apply_add_query``: a machine-shift
    no entry names gets a row of its own), the EXCLUDE rows updated
    (``..._apply_exclude_query``), ``manual_action`` stamped on both — and
    COMMIT. Rows of machines whose override was retired simply come back as
    the sources say. Both INSERTs carry ON DUPLICATE KEY UPDATE, so two
    simultaneous opens of the report cannot fail each other on the unique
    key. A deadlock or lock-wait timeout (MySQL 1213 / 1205) against such a
    concurrent rebuild — on any of the statements — is retried ONCE; if the
    second attempt hits one too the function gives up and returns False — the
    caller then simply reads what the other open wrote. Any other error (e.g.
    a table or the ``manual_action`` column is missing: migration not
    applied) is rolled back and raised.

    The transaction runs at READ COMMITTED: under the server default
    (REPEATABLE READ) an INSERT ... SELECT takes shared next-key locks on
    every source row it reads — a month of attendance marks and production
    entries — and would block the mill's data entry for as long as the
    report builds. ``SET TRANSACTION`` (no SESSION keyword) covers only the
    next transaction, so the session's later reads are back on the default.

    The session's current transaction is ENDED first (``db.rollback()``):
    ``SET TRANSACTION`` is refused inside one. Call this with no pending
    writes on ``db`` — the report endpoint has only read by then.

    Returns True when this call rebuilt the window, False when it gave way to
    a concurrent rebuild.
    """
    binds = {"co_id": co_id, "branch_id": branch_id, "date_from": d_from, "date_to": d_to}
    for _attempt in range(2):
        try:
            db.rollback()
            db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            db.execute(get_machine_shift_worked_delete_query(), binds)
            db.execute(get_machine_shift_worked_insert_query(), binds)
            # Manual overrides, AFTER the source INSERT: ADD first, EXCLUDE
            # second (a machine-shift has one override in force, so the order
            # only fixes what the log reads).
            db.execute(get_machine_shift_worked_apply_add_query(), binds)
            db.execute(get_machine_shift_worked_apply_exclude_query(), binds)
            db.commit()
            return True
        except Exception as e:
            db.rollback()
            if _mysql_errno(e) not in _REBUILD_RETRY_ERRNOS:
                raise
    return False


# ---------------------------------------------------------------------------
# Access to the manual overrides
# ---------------------------------------------------------------------------


def _can_edit(db: Session, token_data: Optional[dict], co_id: int, branch_id: int) -> bool:
    """May this user add / exclude machines: Edit (>= 4) on the Hands Report
    menu row for the company and branch — ``user_menu_access_level``, the same
    resolution as the sidebar (roles of the user, control_co_module cap).
    Never raises: no user in the token, or a lookup that fails, is simply
    "no" — the report itself must still open."""
    user_id = (token_data or {}).get("user_id")
    if user_id is None:
        return False
    try:
        level = user_menu_access_level(db, user_id, co_id, branch_id, HANDS_REPORT_MENU_PATH)
    except Exception:
        return False
    return level >= OVERRIDE_EDIT_LEVEL


def _require_edit(db: Session, token_data: Optional[dict], co_id: int, branch_id: int) -> None:
    """403 unless the user holds Edit on the Hands Report menu row (§12.3)."""
    user_id = (token_data or {}).get("user_id")
    level = (
        0
        if user_id is None
        else user_menu_access_level(db, user_id, co_id, branch_id, HANDS_REPORT_MENU_PATH)
    )
    if level < OVERRIDE_EDIT_LEVEL:
        raise HTTPException(status_code=403, detail=OVERRIDE_VIEW_ONLY_MSG)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@hands_report_router.get("/hands_report")
def hands_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id, branch_id, d_from, d_to = _scope(request)
    view = _require_view(request)
    dept_id = _optional_int(request, "dept_id")
    direct_indirect = request.query_params.get("direct_indirect") or None

    try:
        # daily_attendance / daily_doff_tbl are branch-keyed with no co_id, so a
        # branch of another company of the same tenant would silently report that
        # company's manning under this co_id's header.
        _require_branch_of_co(db, co_id, branch_id)

        # Actuals and machine rows are pulled from the 1st of the From Date's
        # month so the MTD columns have the days before the window; the
        # builder slices the requested window back out. At most 31 extra days
        # on top of the 62-day cap.
        binds = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": _month_start(d_from),
            "date_to": d_to,
        }
        # Machines worked from attendance (owner, 2026-10-01): refresh this
        # request's window of jute_prod_machine_shift_worked from the sources
        # before anything is read — for every view, the manning targets read
        # the same rows as the Machines Worked tab.
        rebuild_machine_shift_worked(db, co_id, branch_id, binds["date_from"], d_to)
        actual_rows = [
            dict(r._mapping) for r in db.execute(get_hands_actuals_query(), binds).fetchall()
        ]
        norm_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_hands_norms_query(),
                {"co_id": co_id, "branch_id": branch_id, "date_to": d_to},
            ).fetchall()
        ]
        run_rows = [
            dict(r._mapping)
            for r in db.execute(get_machine_shift_worked_rows_query(), binds).fetchall()
        ]
        # Hands Norms v2: the machine sets of group-scoped norms and the
        # person-keyed winding kg per winder department (department-scoped
        # production norms), over the same month-start window as the runs.
        member_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_hands_group_members_query(), {"co_id": co_id, "branch_id": branch_id}
            ).fetchall()
        ]
        dept_rows = [
            dict(r._mapping)
            for r in db.execute(get_hands_dept_production_query(), binds).fetchall()
        ]

        return {
            "data": build_hands_report(
                actual_rows,
                norm_rows,
                run_rows,
                co_id=co_id,
                branch_id=branch_id,
                d_from=d_from,
                d_to=d_to,
                view=view,
                dept_id=dept_id,
                direct_indirect=direct_indirect,
                members=member_rows,
                dept_production=dept_rows,
                # The caller's right to add / exclude machines, for the tab's
                # buttons (the POSTs check it again).
                can_edit=_can_edit(db, token_data, co_id, branch_id),
            )
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Manual additions / exclusions of worked machines (spec §12.3)
# ---------------------------------------------------------------------------


def _json_object(request: Request) -> dict:
    """The request's JSON body, which must be an object."""
    try:
        body = parse_json_body(request)
    except Exception:
        raise HTTPException(status_code=400, detail="The request body is not valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="The request body must be a JSON object")
    return body


def _body_int(body: dict, key: str, label: str) -> int:
    value = body.get(key)
    if value is None or value == "" or isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{label} is required")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} is not valid")
    if isinstance(value, float) and value != number:
        raise HTTPException(status_code=400, detail=f"{label} is not valid")
    return number


def _body_id_list(body: dict, key: str, what: str, maximum: int) -> list:
    """A non-empty list of whole-number ids, de-duplicated in the order given;
    ``what`` names one of them in the messages ("machine", "manual change")."""
    raw = body.get(key)
    if not isinstance(raw, (list, tuple)) or not raw:
        raise HTTPException(status_code=400, detail=f"Select at least one {what}")
    ids: list = []
    seen: set = set()
    for value in raw:
        if isinstance(value, bool) or (isinstance(value, float) and value != int(value)):
            raise HTTPException(status_code=400, detail=f"'{value}' is not a valid {what}")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"'{value}' is not a valid {what}")
        if number not in seen:
            seen.add(number)
            ids.append(number)
    if len(ids) > maximum:
        raise HTTPException(
            status_code=400,
            detail=f"At most {maximum} {what}s can be changed at a time — {len(ids)} were sent",
        )
    return ids


def _require_branch_of_co(db: Session, co_id: int, branch_id: int) -> None:
    pair_ok = db.execute(
        text("SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"),
        {"branch_id": branch_id, "co_id": co_id},
    ).first()
    if not pair_ok:
        raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")


def _override_values(body: dict, today: date) -> dict:
    """Validated ``tran_date`` / ``shift`` / ``machine_ids`` / ``action`` /
    ``reason`` of an override save (everything but the machines' branch
    check, which needs the database). ``today`` is the mill's date (IST)."""
    raw_date = str(body.get("tran_date") or "").strip()
    if not raw_date:
        raise HTTPException(status_code=400, detail="Date is required")
    try:
        tran_date = date.fromisoformat(raw_date[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail="Date is not valid — use YYYY-MM-DD")
    if tran_date > today:
        raise HTTPException(
            status_code=400, detail="Date is in the future — machines can only be changed up to today"
        )
    shift = str(body.get("shift") or "").strip().upper()
    if shift not in OVERRIDE_SHIFTS:
        raise HTTPException(
            status_code=400, detail=f"Shift must be one of {', '.join(OVERRIDE_SHIFTS)}"
        )
    action = str(body.get("action") or "").strip().upper()
    if action not in OVERRIDE_ACTIONS:
        raise HTTPException(
            status_code=400, detail=f"Action must be {' or '.join(OVERRIDE_ACTIONS)}"
        )
    machine_ids = _body_id_list(body, "machine_ids", "machine", OVERRIDE_MAX_MACHINES)
    reason = body.get("reason")
    reason = " ".join(reason.split()) if isinstance(reason, str) else ""
    if len(reason) < OVERRIDE_REASON_MIN:
        raise HTTPException(
            status_code=400,
            detail=f"Reason is required — at least {OVERRIDE_REASON_MIN} characters",
        )
    if len(reason) > OVERRIDE_REASON_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Reason is too long — at most {OVERRIDE_REASON_MAX} characters",
        )
    return {
        "tran_date": tran_date,
        "shift": shift,
        "machine_ids": machine_ids,
        "action": action,
        "reason": reason,
    }


def _date_runs(dates) -> list:
    """Distinct dates -> ``[(first, last), ...]`` runs of consecutive days, so
    an undo spanning several days rebuilds each stretch once and nothing in
    between."""
    runs: list = []
    for d in sorted(set(dates)):
        if runs and d - runs[-1][1] == timedelta(days=1):
            runs[-1] = (runs[-1][0], d)
        else:
            runs.append((d, d))
    return runs


@hands_report_router.post("/hands_report_machine_override")
def hands_report_machine_override(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Add machines to, or exclude them from, the machines WORKED of one date
    and shift, with a reason. Body: ``{co_id, branch_id, tran_date, shift,
    machine_ids, action: "ADD" | "EXCLUDE", reason}``.

    Needs Edit on the Hands Report menu (403 otherwise). For each machine the
    active override of that machine-shift (if any) is retired and the new one
    inserted — one transaction — and then that date is rebuilt, so the next
    read of either tab already counts the change. Returns ``{"saved": n}``.

    The save runs at READ COMMITTED, like the rebuild: under the server
    default (REPEATABLE READ) the retiring UPDATE takes gap locks on the
    override index — on the newest dates the gap runs to the end of the index
    — and two supervisors saving at the same moment, even for different
    dates, would deadlock on each other's INSERT. Without gap locks two saves
    of the very same machine-shift can both land an active row; the readers
    take the newest (``_override_in_force``), so the later save wins.
    """
    body = _json_object(request)
    co_id = _body_int(body, "co_id", "co_id")
    branch_id = _body_int(body, "branch_id", "branch_id")
    _require_edit(db, token_data, co_id, branch_id)
    values = _override_values(body, now_ist().date())
    try:
        _require_branch_of_co(db, co_id, branch_id)
        # The same check the machine-group members go through: every machine
        # must be an ACTIVE machine of this branch's departments.
        _check_members_in_branch(db, branch_id, [(mid, 1) for mid in values["machine_ids"]])
        scope = {
            "co_id": co_id,
            "branch_id": branch_id,
            "tran_date": values["tran_date"],
            "shift": values["shift"],
        }
        # End the read transaction of the checks above (SET TRANSACTION is
        # refused inside one), then retire + insert in ONE transaction.
        db.rollback()
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        db.execute(
            retire_machine_shift_overrides_query(),
            {**scope, "machine_ids": values["machine_ids"]},
        )
        db.execute(
            insert_machine_shift_override_query(),
            [
                {
                    **scope,
                    "machine_id": machine_id,
                    "action": values["action"],
                    "reason": values["reason"],
                    "updated_by": token_data.get("user_id"),
                }
                for machine_id in values["machine_ids"]
            ],
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    try:
        # The overrides are saved; bring that date's rows in line with them.
        # (A rebuild that gives way to a concurrent one applied them too.)
        rebuild_machine_shift_worked(db, co_id, branch_id, values["tran_date"], values["tran_date"])
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"The change was saved but the machines worked could not be refreshed: {e}",
        )
    return {"data": {"saved": len(values["machine_ids"])}}


@hands_report_router.post("/hands_report_machine_override_undo")
def hands_report_machine_override_undo(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Undo manual additions / exclusions: ``active = 0`` on the given
    overrides — only active rows of that company / branch; ids that are not
    (already undone, another branch) are ignored — then rebuild the dates
    they were on, so those machine-shifts read what the sources say again.
    Body: ``{co_id, branch_id, override_ids}``. Needs Edit on the Hands
    Report menu. Returns ``{"undone": n}``.
    """
    body = _json_object(request)
    co_id = _body_int(body, "co_id", "co_id")
    branch_id = _body_int(body, "branch_id", "branch_id")
    _require_edit(db, token_data, co_id, branch_id)
    override_ids = _body_id_list(body, "override_ids", "manual change", OVERRIDE_MAX_MACHINES)
    try:
        scope = {"co_id": co_id, "branch_id": branch_id}
        found = [
            dict(r._mapping)
            for r in db.execute(
                get_machine_shift_overrides_to_undo_query(),
                {**scope, "override_ids": override_ids},
            ).fetchall()
        ]
        if not found:
            return {"data": {"undone": 0}}
        db.execute(
            undo_machine_shift_overrides_query(),
            {
                **scope,
                "updated_by": token_data.get("user_id"),
                "override_ids": [int(r["machine_shift_override_id"]) for r in found],
            },
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    try:
        for first, last in _date_runs(r["tran_date"] for r in found):
            rebuild_machine_shift_worked(db, co_id, branch_id, first, last)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"The undo was saved but the machines worked could not be refreshed: {e}",
        )
    return {"data": {"undone": len(found)}}


@hands_report_router.get("/hands_report_machines")
def hands_report_machines(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The branch's active machines for the "Add machines" picker of the
    Machines Worked tab, each with the machine groups it belongs to
    (``?co_id&branch_id``; view level)."""
    raw_co = request.query_params.get("co_id")
    raw_branch = request.query_params.get("branch_id")
    if not raw_co:
        raise HTTPException(status_code=400, detail="co_id is required")
    if not raw_branch:
        raise HTTPException(status_code=400, detail="branch_id is required")
    try:
        co_id = int(raw_co)
        branch_id = int(raw_branch)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id or branch_id")
    try:
        scope = {"co_id": co_id, "branch_id": branch_id}
        machine_rows = [
            dict(r._mapping)
            for r in db.execute(get_hands_report_machines_query(), scope).fetchall()
        ]
        member_rows = [
            dict(r._mapping)
            for r in db.execute(get_hands_group_members_query(), scope).fetchall()
        ]
        machine_groups = _index_machine_groups(member_rows, [])
        data = []
        for m in machine_rows:
            machine_id = int(m["machine_id"])
            groups = machine_groups.get(machine_id, [])
            data.append(
                {
                    "machine_id": machine_id,
                    "machine": m.get("machine_name") or m.get("mech_code") or f"MC{machine_id}",
                    "mech_code": m.get("mech_code"),
                    "machine_type_id": m.get("machine_type_id"),
                    "machine_type": m.get("machine_type_name"),
                    "dept_id": m.get("dept_id"),
                    "dept": m.get("dept"),
                    "group_ids": [gid for gid, _name in groups],
                    "groups": ", ".join(name for _gid, name in groups),
                }
            )
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
