"""Jute Production reports: Maturity Time + Spreader Production summary +
Loom Checklist + the wage-reconciliation checklists (winding / beaming /
finishing / attendance)."""

from datetime import date, datetime, timedelta
from fnmatch import fnmatchcase
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import A_SHIFT, B_SHIFT, SHIFT_BUCKETS
from src.juteProduction.reportQueries import (
    get_attendance_checklist_query,
    get_beaming_checklist_query,
    get_checklist_map_query,
    get_deactivate_rejected_attendance_query,
    get_finishing_checklist_query,
    get_maturity_time_report_query,
    get_spreader_datewise_issue_query,
    get_spreader_datewise_production_query,
    get_spreader_production_report_query,
    get_winding_attendance_checklist_query,
    get_winding_production_checklist_query,
)
from src.juteProduction.services.shift import shift_for_report
from src.juteProduction.weaving_lock import fetch_weaving_day_rows, header_covers_scope
from src.juteProduction.weaving_query import (
    get_weaving_day_headers_query,
    get_weaving_day_pick_count_query,
    get_weaving_quality_map_pairs_query,
)


router = APIRouter()


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _checklist_scope(request: Request, max_days: int) -> tuple:
    """Shared co_id / branch_id / date-range validation for every checklist.

    Returns ``(co_id, branch_id, d_from, d_to)``. ``max_days`` caps the window
    inclusively — these grids scan attendance tables that run to ~1.2M rows."""
    co_id = _require_co_id(request)
    branch_raw = request.query_params.get("branch_id")
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    if not date_from or not date_to:
        raise HTTPException(status_code=400, detail="date_from and date_to are required")
    try:
        branch_id = int(branch_raw) if branch_raw else None
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date or branch_id")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    if (d_to - d_from).days >= max_days:
        raise HTTPException(
            status_code=400, detail=f"Date range too large — at most {max_days} days"
        )
    return co_id, branch_id, d_from, d_to


def _parse_multi(request: Request, key: str) -> Optional[list]:
    """Parse repeated query params (?machines=1&machines=2) into a list of ints."""
    vals = request.query_params.getlist(key)
    if not vals:
        return None
    out = []
    for v in vals:
        for tok in str(v).split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.append(int(tok))
            except ValueError:
                pass
    return out or None


def _parse_str_multi(request: Request, key: str) -> Optional[list]:
    vals = request.query_params.getlist(key)
    if not vals:
        return None
    out: list = []
    for v in vals:
        for tok in str(v).split(","):
            tok = tok.strip()
            if tok:
                out.append(tok)
    return out or None


@router.get("/maturity_time_report")
def maturity_time_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    d = request.query_params.get("issue_date")
    if not d:
        raise HTTPException(status_code=400, detail="issue_date is required")
    try:
        rows = db.execute(
            get_maturity_time_report_query(), {"co_id": co_id, "d": date.fromisoformat(d)}
        ).fetchall()
        data = []
        for r in rows:
            m = dict(r._mapping)
            for k in ("wt_per_roll",):
                if m.get(k) is not None:
                    m[k] = float(m[k])
            for k in ("issue_dt", "prod_entry_dt"):
                if m.get(k) and isinstance(m[k], datetime):
                    m[k] = m[k].isoformat()
            data.append(m)
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _build_pivot(rows, entity_key: str, entity_label_key: str) -> list:
    """Build a list of pivot rows like {entity_id, entity_name, A1, B1, A2, B2, C, A, B, Total}."""
    bucket_init = {b: 0 for b in SHIFT_BUCKETS}
    grouped: dict = {}
    for r in rows:
        ent_id = r.get(entity_key)
        if ent_id is None:
            continue
        bucket = r.get("__shift")
        if not bucket:
            continue
        entry = grouped.setdefault(
            ent_id,
            {
                "entity_id": ent_id,
                "entity_name": r.get(entity_label_key) or str(ent_id),
                **bucket_init.copy(),
            },
        )
        entry[bucket] = entry.get(bucket, 0) + int(r.get("no_of_rolls", 0) or 0)
    out = []
    for entry in grouped.values():
        entry["A"] = sum(entry.get(s, 0) for s in A_SHIFT)
        entry["B"] = sum(entry.get(s, 0) for s in B_SHIFT)
        entry["Total"] = sum(entry.get(s, 0) for s in SHIFT_BUCKETS)
        out.append(entry)
    out.sort(key=lambda x: x["entity_name"])
    return out


@router.get("/spreader_production_summary")
def spreader_production_summary(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    d = request.query_params.get("report_date")
    if not d:
        raise HTTPException(status_code=400, detail="report_date is required")

    try:
        report_d = date.fromisoformat(d)
        sel_machines = _parse_multi(request, "spreaders")
        sel_items = _parse_multi(request, "items")
        sel_shifts = _parse_str_multi(request, "shifts")

        raw = db.execute(
            get_spreader_production_report_query(), {"co_id": co_id, "d": report_d}
        ).fetchall()

        rows = []
        for r in raw:
            m = dict(r._mapping)
            shift = shift_for_report(m["entry_date"], m["entry_time"], report_d)
            if shift is None:
                continue
            if sel_shifts and shift not in sel_shifts:
                continue
            if sel_machines and int(m["machine_id"]) not in sel_machines:
                continue
            if sel_items:
                comp_ids = {int(m["item_id"])}
                if m.get("item_id_2") is not None:
                    comp_ids.add(int(m["item_id_2"]))
                if not comp_ids & set(sel_items):
                    continue
            m["__shift"] = shift
            # Pivot on the blend tuple, not the label: the label can be NULL (orphaned
            # item row) and two distinct items can share a name — either would silently
            # drop or merge rows in the pivot.
            m["blend_key"] = (
                f"{m['item_id']}-{m.get('item_id_2') or 0}-{m.get('ratio_1')}-{m.get('ratio_2') or 0}"
            )
            if isinstance(m.get("entry_dt"), datetime):
                m["entry_dt"] = m["entry_dt"].isoformat()
            if m.get("wt_per_roll") is not None:
                m["wt_per_roll"] = float(m["wt_per_roll"])
            if m.get("weight_kg") is not None:
                m["weight_kg"] = float(m["weight_kg"])
            rows.append(m)

        # Aggregate pivots
        spreader_pivot = _build_pivot(rows, entity_key="machine_id", entity_label_key="machine_name")
        quality_pivot = _build_pivot(rows, entity_key="blend_key", entity_label_key="quality_label")

        total_rolls = sum(int(r.get("no_of_rolls", 0) or 0) for r in rows)
        total_weight_kg = round(sum(float(r.get("weight_kg", 0) or 0) for r in rows), 2)

        # Strip the internal __shift before returning
        for r in rows:
            r["shift"] = r.pop("__shift")

        return {
            "data": {
                "rows": rows,
                "spreader_pivot": spreader_pivot,
                "quality_pivot": quality_pivot,
                "totals": {
                    "rolls": total_rolls,
                    "weight_kg": total_weight_kg,
                    "weight_mt": round(total_weight_kg / 1000.0, 3),
                    "unique_spreaders": len({r["machine_id"] for r in rows if r.get("machine_id")}),
                    "unique_items": len({r["blend_key"] for r in rows}),
                },
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Spreader Production Report — date-wise pivot over a period: production per
# machine × quality and issue per quality, each as a Rolls row and a Kg row.
# ---------------------------------------------------------------------------

SPREADER_DATEWISE_MAX_DAYS = 31


def build_spreader_datewise_rows(raw: list, kind: str, d_from: date, d_to: date) -> list:
    """Grouped SQL rows -> pivot rows ``{kind, machine_id, machine_name,
    quality_label, unit, days: {iso: value}, total, average}``, two per
    machine × blend (Rolls, Kg), a "Total" pair after each machine and the
    block's "Grand Total" pair at the end.

    Hours 00-05 (``early``) belong to the previous day's C shift. ``average`` is
    over the days that have a value, not over the whole period."""
    groups: dict = {}
    for r in raw:
        day = r["cal_date"] - timedelta(days=1) if r.get("early") else r["cal_date"]
        if not d_from <= day <= d_to:
            continue
        # Pivot on the blend tuple, not the label — see spreader_production_summary.
        key = (r.get("machine_id"), r.get("item_id"), r.get("item_id_2"), r.get("ratio_1"), r.get("ratio_2"))
        g = groups.setdefault(
            key,
            {
                "machine_id": r.get("machine_id"),
                "machine_name": r.get("machine_name"),
                "quality_label": r.get("quality_label"),
                "Rolls": {},
                "Kg": {},
            },
        )
        iso = day.isoformat()
        g["Rolls"][iso] = g["Rolls"].get(iso, 0) + float(r.get("rolls") or 0)
        g["Kg"][iso] = g["Kg"].get(iso, 0) + float(r.get("kg") or 0)

    def _row(machine_id, machine_name, quality_label, unit, days):
        days = {d: round(v, 2) for d, v in days.items() if v}
        total = round(sum(days.values()), 2)
        return {
            "kind": kind,
            "machine_id": machine_id,
            "machine_name": machine_name,
            "quality_label": quality_label,
            "unit": unit,
            "days": days,
            "total": total,
            "average": round(total / len(days), 2) if days else 0,
        }

    out = []
    grand: dict = {"Rolls": {}, "Kg": {}}
    mc: dict = {"Rolls": {}, "Kg": {}}
    ordered = sorted(
        groups.values(), key=lambda x: (str(x["machine_name"] or ""), str(x["quality_label"] or ""))
    )
    for n, g in enumerate(ordered):
        for unit in ("Rolls", "Kg"):
            out.append(_row(g["machine_id"], g["machine_name"], g["quality_label"], unit, g[unit]))
            for d, v in g[unit].items():
                grand[unit][d] = grand[unit].get(d, 0) + v
                mc[unit][d] = mc[unit].get(d, 0) + v
        # Machine subtotal once its last quality is out (Issue rows have no machine).
        last_of_mc = n + 1 == len(ordered) or ordered[n + 1]["machine_id"] != g["machine_id"]
        if g["machine_id"] is not None and last_of_mc:
            for unit in ("Rolls", "Kg"):
                out.append(_row(g["machine_id"], g["machine_name"], "Total", unit, mc[unit]))
            mc = {"Rolls": {}, "Kg": {}}
    if out:
        for unit in ("Rolls", "Kg"):
            out.append(_row(None, None, "Grand Total", unit, grand[unit]))
    return out


@router.get("/spreader_production_datewise")
def spreader_production_datewise(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id, branch_id, d_from, d_to = _checklist_scope(request, SPREADER_DATEWISE_MAX_DAYS)
    machine_raw = request.query_params.get("machine_id")
    try:
        machine_id = int(machine_raw) if machine_raw else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid machine_id")

    try:
        binds = {"co_id": co_id, "branch_id": branch_id, "d_from": d_from, "d_to": d_to}
        prod = db.execute(
            get_spreader_datewise_production_query(), {**binds, "machine_id": machine_id}
        ).fetchall()
        issue = db.execute(get_spreader_datewise_issue_query(), binds).fetchall()

        dates = []
        d = d_from
        while d <= d_to:
            dates.append(d.isoformat())
            d += timedelta(days=1)
        return {
            "data": {
                "dates": dates,
                "rows": build_spreader_datewise_rows(
                    [dict(r._mapping) for r in prod], "Production", d_from, d_to
                )
                + build_spreader_datewise_rows(
                    [dict(r._mapping) for r in issue], "Issue", d_from, d_to
                ),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Loom Checklist — the pre-wages audit gate over weaving production. Rows come
# from fetch_weaving_day_rows, so a Processed+fresh unit is audited on the
# FROZEN jute_prod_weaving_log numbers wages are actually paid on (stale /
# never-synced spells fall back to the live day-slice, and say so in a remark).
# ---------------------------------------------------------------------------

LOOM_CHECKLIST_MAX_DAYS = 62
LOOM_EFF_HIGH = 85
LOOM_EFF_LOW = 10
LOOM_EFF_STD_MARGIN = 1.15
# Spell order for the jugar chain — same map as _spell_rank_case() in
# weaving_query.py (which the SQL chain probes use). Must stay in lockstep.
LOOM_SPELL_RANK = {"A1": 1, "A": 1, "B1": 2, "B": 2, "A2": 3, "B2": 4, "C": 5}


def loom_sync_states(headers: list, request_branch_id) -> dict:
    """spell_id -> 'not_synced' | 'stale' | 'locked' | 'synced'.

    ``headers`` = all active sync/lock headers of ONE date
    (get_weaving_day_headers_query rows as dicts). Freshness mirrors
    fetch_weaving_day_rows: a spell is fresh only when some header covers the
    read scope AND every header of that spell has reprocess_needed = 0.
    Spells with no header at all are simply absent (callers default to
    'not_synced')."""
    by_spell: dict = {}
    for h in headers:
        by_spell.setdefault(int(h["spell_id"]), []).append(h)
    out = {}
    for sid, hs in by_spell.items():
        covering = [h for h in hs if header_covers_scope(h["branch_id"], request_branch_id)]
        if not covering:
            out[sid] = "not_synced"
        elif any(h["reprocess_needed"] for h in hs):
            out[sid] = "stale"
        elif any(h["is_locked"] for h in covering):
            out[sid] = "locked"
        else:
            out[sid] = "synced"
    return out


def _zero_eff_cause(row) -> str:
    """Why is efficiency 0 on a row that produced? Split the cause so the audit
    reads as an actionable gap rather than 'something is wrong'."""
    if float(row.get("working_hours") or 0) == 0:
        return "Full-spell stoppage + Prod"
    if row.get("std_picks") in (None, 0):
        return "Eff 0, No Picks"
    if (row.get("eff_speed") or row.get("std_speed")) in (None, 0):
        return "Eff 0, No Speed Std"
    return "Eff 0"


def loom_check_remarks(
    row,
    sync_state: str = "synced",
    request_branch_id=None,
    map_exists: bool = True,
    day_has_picks: bool = True,
) -> list:
    """Per-row anomaly rules. ``row`` is a weaving day-slice / log row mapping.

    An unmapped quality makes production AND efficiency uncomputable, so its
    zeros are artefacts — the derived-figure rules are suppressed for that row
    and instead the remark names the exact missing input: the §6.6 quality map
    (``map_exists``), the Sync run that stamps quality onto the daily row, and
    the day's SQC pick reading (``day_has_picks`` — width-only readings carry no
    picks and don't count). EB rules need a synced unit (eb_id is stamped by
    Sync/Process)."""
    prod = float(row.get("production_yds") or 0)
    eff = float(row.get("efficiency") or 0)
    cuts = float(row.get("cuts") or 0)
    close_jugar = float(row.get("close_jugar") or 0)
    std_eff = float(row.get("std_eff") or 0)
    quality_missing = row.get("weaving_quality_id") is None

    remarks = []
    if quality_missing:
        if sync_state == "not_synced" and not map_exists:
            remarks.append("No quality map — map + Sync")
        elif sync_state == "not_synced":
            remarks.append("Not synced — run Sync")
        else:
            remarks.append("No quality map")
        if not day_has_picks:
            remarks.append("No Picks")
    if sync_state != "not_synced":
        if row.get("eb_id") is not None and prod == 0:
            remarks.append("EB-No Prod Entry")
        if row.get("eb_id") is None and prod > 0:
            remarks.append("No EB")
    if not quality_missing:
        if prod > 0 and eff == 0:
            remarks.append(_zero_eff_cause(row))
        if (cuts + close_jugar) > 0 and prod == 0:
            remarks.append("Cuts/Jugar, No Prod")
    if prod < 0:
        remarks.append("Neg Prod")
    if not quality_missing:
        limit = std_eff * LOOM_EFF_STD_MARGIN if std_eff > 0 else LOOM_EFF_HIGH
        if eff > limit:
            remarks.append(f"Eff > {limit:.0f}%")
        if 0 < eff < LOOM_EFF_LOW:
            remarks.append("Eff < 10%")
    if sync_state == "stale":
        remarks.append("Stale — re-sync")
    if request_branch_id is not None and row.get("branch_id") is None:
        remarks.append("No branch on row")
    return remarks


LOOM_DEFAULT_EB_MACHINES = 2


def parse_machine_limit(raw):
    """``designation_mst.no_of_machines`` is a VARCHAR holding '', NULL or digits.
    Returns the positive int, or None when there is nothing usable to honour (the
    caller then falls back to LOOM_DEFAULT_EB_MACHINES)."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def loom_eb_overload_remarks(rows: list, eb_limits: dict) -> dict:
    """Cross-row: one EB credited on more looms than the designation allows —
    working two looms is normal, more than the allowance is a wage claim to check.
    ``eb_limits`` = {eb_id: no_of_machines} from designation_mst; anything absent
    falls back to LOOM_DEFAULT_EB_MACHINES. Returns {row index: remark}. Grouped
    on spell_id, never spell_code (codes repeat across companies/branches)."""
    groups: dict = {}
    for i, r in enumerate(rows):
        eb = r.get("eb_id")
        if eb is None:
            continue
        groups.setdefault((r.get("tran_date"), r.get("spell_id"), eb), []).append(i)
    out = {}
    for (_d, _s, eb), idxs in groups.items():
        n = len({rows[i].get("machine_id") for i in idxs})
        limit = eb_limits.get(eb, LOOM_DEFAULT_EB_MACHINES)
        if n <= limit:
            continue
        for i in idxs:
            out[i] = f"EB on {n} looms (limit {limit})"
    return out


def loom_jugar_chain_remarks(rows: list) -> dict:
    """Cross-row: opening jugar must equal the previous entry's closing jugar in
    the loom chain (per machine in spell order — quality is not a criterion).
    Returns {row index: remark}. The first row of a chain has no in-range
    predecessor and is never flagged."""
    chains: dict = {}
    for i, r in enumerate(rows):
        chains.setdefault(r.get("machine_id"), []).append(i)
    out = {}
    for idxs in chains.values():
        idxs.sort(
            key=lambda i: (
                rows[i].get("tran_date"),
                LOOM_SPELL_RANK.get(rows[i].get("spell_code"), 99),
                rows[i].get("weaving_daily_id") or 0,
            )
        )
        for prev, cur in zip(idxs, idxs[1:]):
            gap = float(rows[cur].get("open_jugar") or 0) - float(
                rows[prev].get("close_jugar") or 0
            )
            if abs(gap) > 0.001:
                out[cur] = "Jugar chain break"
    return out


def loom_row_is_idle(row) -> bool:
    """Checklist ('chk') filter: an idle loom row is dropped whatever remarks it
    carries (TKT-000122: carry-forward jugar alone kept flagging idle looms).
    Idle = nothing entered (zero cuts AND the closing jugar equals the opening —
    a carried-forward reading, not an entry) with no EB and no production. The
    EB / production guards keep wage-risk rows visible: an EB credited on a loom
    that did nothing is exactly what the pre-wages audit exists to show."""
    return (
        float(row.get("cuts") or 0) == 0
        and abs(
            float(row.get("close_jugar") or 0) - float(row.get("open_jugar") or 0)
        ) < 0.001
        and row.get("eb_id") is None
        and float(row.get("production_yds") or 0) == 0
    )


def loom_row_is_empty(row, remarks: list) -> bool:
    """'All' filter: hide a row that carries nothing at all. Subordinate to the
    remarks — anything flagged is always shown."""
    if remarks or row.get("eb_id") is not None:
        return False
    return (
        float(row.get("cuts") or 0)
        + float(row.get("close_jugar") or 0)
        + float(row.get("less_production") or 0)
        + abs(float(row.get("production_yds") or 0))
    ) < 1e-9


@router.get("/loom_checklist")
def loom_checklist(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Weaving rows for a date range with anomaly remarks — the pre-wages audit.

    ``rtype=chk`` returns only remarked rows (the checklist proper); ``rtype=all``
    returns every entry that carries something (a remarked row is never hidden).
    branch_id optional (NULL-tolerant, same semantics as the entry grid).
    """
    co_id, branch_id, d_from, d_to = _checklist_scope(request, LOOM_CHECKLIST_MAX_DAYS)
    rtype = (request.query_params.get("rtype") or "chk").lower()

    try:
        # Pass 1 — collect the whole range first: the EB-overload and jugar-chain
        # rules are cross-row, so nothing can be filtered out before they run.
        rows = []
        d = d_from
        # ponytail: the day-slice is single-day scoped by design; loop the
        # (capped) range instead of authoring a second range-scoped slice.
        while d <= d_to:
            # ponytail: re-reads the day's headers that fetch_weaving_day_rows
            # already read (small indexed read) rather than widening its contract.
            states = loom_sync_states(
                [
                    dict(h._mapping)
                    for h in db.execute(
                        get_weaving_day_headers_query(),
                        {
                            "co_id": co_id,
                            "tran_date": d,
                            "spell_id": None,
                            "branch_id": branch_id,
                        },
                    ).fetchall()
                ],
                branch_id,
            )
            day_binds = {"co_id": co_id, "tran_date": d}
            mapped_pairs = {
                (int(r.spell_id), int(r.machine_id))
                for r in db.execute(
                    get_weaving_quality_map_pairs_query(), day_binds
                ).fetchall()
            }
            day_has_picks = bool(
                db.execute(get_weaving_day_pick_count_query(), day_binds).scalar()
            )
            for m in fetch_weaving_day_rows(db, co_id, d, branch_id):
                sid = m.get("spell_id")
                m["__sync_state"] = (
                    states.get(int(sid), "not_synced") if sid is not None else "not_synced"
                )
                m["__map_exists"] = (
                    sid is not None
                    and (int(sid), int(m["machine_id"])) in mapped_pairs
                )
                m["__day_has_picks"] = day_has_picks
                rows.append(m)
            d += timedelta(days=1)

        # Per-EB loom allowance for the overload rule. Read over ALL rows (the
        # rule is cross-row and runs pre-filter, unlike the emp_code label lookup
        # below). An EB can carry more than one active official-details row; ORDER
        # BY the PK ascending so the dict build keeps the MAX-PK (latest) row's
        # designation deterministically instead of whatever came back first.
        eb_limits = {}
        all_eb_ids = sorted({m["eb_id"] for m in rows if m.get("eb_id") is not None})
        if all_eb_ids:
            for eb, raw in db.execute(
                text(
                    "SELECT o.eb_id, d.no_of_machines "
                    "FROM hrms_ed_official_details o "
                    "JOIN designation_mst d ON d.designation_id = o.designation_id "
                    "WHERE o.active = 1 AND o.eb_id IN :ids "
                    "ORDER BY o.tbl_hrms_ed_official_detail_id"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": all_eb_ids},
            ).fetchall():
                limit = parse_machine_limit(raw)
                # unset / blank / non-numeric designations keep the default
                if limit is not None:
                    eb_limits[eb] = limit
                else:
                    eb_limits.pop(eb, None)

        # Pass 2 — remarks (per-row, then cross-row), THEN filtering.
        remarks_by_idx = {
            i: loom_check_remarks(
                m,
                m["__sync_state"],
                branch_id,
                map_exists=m["__map_exists"],
                day_has_picks=m["__day_has_picks"],
            )
            for i, m in enumerate(rows)
        }
        for extra in (
            loom_eb_overload_remarks(rows, eb_limits),
            loom_jugar_chain_remarks(rows),
        ):
            for i, msg in extra.items():
                remarks_by_idx[i].append(msg)

        out = []
        for i, m in enumerate(rows):
            remarks = remarks_by_idx[i]
            if rtype == "chk" and (not remarks or loom_row_is_idle(m)):
                continue
            if rtype != "chk" and loom_row_is_empty(m, remarks):
                continue
            out.append(
                {
                    "weaving_daily_id": m["weaving_daily_id"],
                    "tran_date": m["tran_date"].isoformat(),
                    "spell_id": m["spell_id"],
                    "spell_code": m["spell_code"],
                    "branch_id": m["branch_id"],
                    "mech_code": m["mech_code"],
                    "machine_name": m["machine_name"],
                    "weaving_quality_id": m["weaving_quality_id"],
                    "weaving_quality_code": m["weaving_quality_code"],
                    "weaving_quality_name": m["weaving_quality_name"],
                    "eb_id": m["eb_id"],
                    "eb_no": None,
                    "open_jugar": float(m["open_jugar"] or 0),
                    "cuts": float(m["cuts"] or 0),
                    "close_jugar": float(m["close_jugar"] or 0),
                    "less_production": float(m["less_production"] or 0),
                    "jugar": float(m["jugar"] or 0),
                    "production_yds": float(m["production_yds"] or 0),
                    "production_kg": float(m["production_kg"] or 0),
                    "efficiency": float(m["efficiency"] or 0),
                    "std_eff": float(m["std_eff"] or 0),
                    "working_hours": float(m["working_hours"] or 0),
                    "sync_state": m["__sync_state"],
                    "remarks": "; ".join(remarks),
                }
            )

        # Best-effort EB label (emp_code) — one lookup for all distinct eb_ids.
        # An EB can carry more than one active official-details row; ORDER BY the
        # PK ascending so the dict build keeps the MAX-PK (latest) row's code
        # deterministically instead of whatever the server returned first.
        eb_ids = sorted({r["eb_id"] for r in out if r["eb_id"] is not None})
        if eb_ids:
            eb_rows = db.execute(
                text(
                    "SELECT eb_id, emp_code FROM hrms_ed_official_details "
                    "WHERE active = 1 AND eb_id IN :ids "
                    "ORDER BY tbl_hrms_ed_official_detail_id"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": eb_ids},
            ).fetchall()
            emp_code = {row[0]: row[1] for row in eb_rows}
            for r in out:
                if r["eb_id"] is not None:
                    r["eb_no"] = emp_code.get(r["eb_id"])

        out.sort(key=lambda r: (r["tran_date"], r["mech_code"] or "", r["spell_code"] or ""))
        return {"data": out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Wage-reconciliation checklists (ports of the old PHP ones). Plain exception
# grids — no sync_state / rule-engine machinery: every rule is a pure function
# over one already-aggregated SQL row, and `remarks` is the "; "-joined result.
# ---------------------------------------------------------------------------

CHECKLIST_MAX_DAYS = 62
ATTENDANCE_CHECKLIST_MAX_DAYS = 31
WINDING_MAX_KG_PER_SHIFT = 410
# Fallback name matching, used only where `jute_prod_checklist_map` has no rows
# for the scope: designations / machine types are then matched by NAME, never by
# id (ids differ per tenant). Patterns are reused in Python for the reverse
# (production -> attendance) check.
WINDER_DESIG_PATTERN = "winder%"
BEAMING_MACHINE_TYPE = "beaming"


def _desig_matches(desig, pattern: str) -> bool:
    """Python mirror of the SQL ``LOWER(desig) LIKE pattern``."""
    return bool(desig) and fnmatchcase(desig.lower(), pattern.replace("%", "*"))


def _map_ref_ids(db, co_id: int, branch_id, checklist: str, ref_type: str) -> list:
    """Configured ref_ids for a checklist, or [] when the scope has none.

    DB-only config (`jute_prod_checklist_map`, no UI): finishing is map-driven
    only, winding/beaming fall back to name matching when this is empty."""
    rows = db.execute(
        get_checklist_map_query(),
        {
            "co_id": co_id,
            "branch_id": branch_id,
            "checklist": checklist,
            "ref_type": ref_type,
        },
    ).fetchall()
    return [r._mapping["ref_id"] for r in rows]


def _desig_ok(row, desig_set: set, pattern: str) -> bool:
    """Was the worker present under a designation this checklist counts?
    Map membership when the scope is configured, else the legacy name pattern."""
    if desig_set:
        return row.get("worked_designation_id") in desig_set
    return _desig_matches(row.get("designation"), pattern)


def _f(row, key) -> float:
    return float(row.get(key) or 0)


def _over_shift_cap_remark(qty: float) -> Optional[str]:
    if qty > WINDING_MAX_KG_PER_SHIFT:
        return (
            f"Production {qty:g} kg exceeds {WINDING_MAX_KG_PER_SHIFT} kg/shift"
            " — probable typo"
        )
    return None


def winding_attendance_remarks(row) -> list:
    """Attendance-first: a winder was marked present — did the shift pay off?"""
    remarks = []
    qty = _f(row, "production_qty")
    if _f(row, "net_hours") <= 0:
        remarks.append("Present but no working hours")
    if int(row.get("doff_count") or 0) == 0 or qty == 0:
        remarks.append("Present, no winding production recorded")
    over = _over_shift_cap_remark(qty)
    if over:
        remarks.append(over)
    return remarks


def winding_production_remarks(row, desig_set: set = frozenset()) -> list:
    """Production-first: doffs are credited to an eb — is the shift payable?"""
    remarks = []
    qty = _f(row, "production_qty")
    if int(row.get("atten_rows") or 0) == 0:
        remarks.append("No attendance for this shift — wages at risk")
    else:
        if not _desig_ok(row, desig_set, WINDER_DESIG_PATTERN):
            remarks.append(
                f"Present under '{row.get('designation')}', not a winding designation"
            )
        if _f(row, "net_hours") <= 0:
            remarks.append("Present but no working hours")
    if qty == 0:
        remarks.append("Blank/zero doff entry")
    over = _over_shift_cap_remark(qty)
    if over:
        remarks.append(over)
    return remarks


def beaming_remarks(row) -> list:
    """Machine hours booked vs cuts produced, per machine x date x spell."""
    hours = _f(row, "net_hours")
    cuts = _f(row, "cuts")
    remarks = []
    if hours <= 0 and cuts > 0:
        remarks.append("Machine ran, no worker hours booked")
    if hours > 0 and cuts <= 0:
        remarks.append("Hours booked, no cuts produced")
    return remarks


def finishing_remarks(row, desig_set: set = frozenset()) -> list:
    """Two-way match: finishing attendance vs finishing production."""
    has_prod = int(row.get("prod_rows") or 0) > 0
    has_atten = int(row.get("atten_rows") or 0) > 0
    if has_prod and row.get("eb_id") is None:
        # Nothing to match against — an eb-less production row can never pair
        # with attendance, so the other rules would only add noise.
        return ["No EB on the production entry — cannot match attendance"]
    if not has_prod:
        # The key can only have come from the finishing-attendance side.
        return ["Present in finishing, no work entered"]
    if not has_atten:
        return ["No attendance for this shift — wages at risk"]
    # Map-only, no name fallback: the configured designation set IS the
    # definition of "finishing" for this branch.
    if row.get("worked_designation_id") not in desig_set:
        return [f"Present under '{row.get('designation')}', not finishing"]
    return []


def _retire_rejected_attendance(db, co_id: int, branch_id, d_from, d_to) -> None:
    """Search on the Attendance Checklist also retires rejected attendance
    (status_id 4 -> is_active 0) in the same scope. Only that checklist runs
    it: loom, winding, beaming and finishing must not write anything."""
    db.execute(
        get_deactivate_rejected_attendance_query(),
        {"co_id": co_id, "branch_id": branch_id, "d_from": d_from, "d_to": d_to},
    )
    db.commit()


def _checklist_rows(db, query, binds, shape, remark_fn, rtype: str) -> list:
    """Run one checklist query, remark every row, drop the clean ones on
    ``rtype=chk``. ``shape`` maps the response row from the SQL row mapping."""
    out = []
    for r in db.execute(query, binds).fetchall():
        m = dict(r._mapping)
        remarks = remark_fn(m)
        if rtype == "chk" and not remarks:
            continue
        row = shape(m)
        row["remarks"] = "; ".join(remarks)
        out.append(row)
    return out


def _sort_checklist(rows: list, tie_key: str) -> list:
    rows.sort(key=lambda r: (r["tran_date"], r["spell_code"] or "", r[tie_key] or ""))
    return rows


@router.get("/winding_checklist")
def winding_checklist(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Winding wage checklist, read from either end.

    ``direction=attendance`` (default) starts from winder-designation attendance
    and asks what they produced; ``direction=production`` starts from the doff
    entries and asks whether the shift is payable at all.
    """
    co_id, branch_id, d_from, d_to = _checklist_scope(request, CHECKLIST_MAX_DAYS)
    rtype = (request.query_params.get("rtype") or "chk").lower()
    direction = (request.query_params.get("direction") or "attendance").lower()
    if direction not in ("attendance", "production"):
        raise HTTPException(
            status_code=400, detail="direction must be 'attendance' or 'production'"
        )

    binds = {
        "co_id": co_id,
        "branch_id": branch_id,
        "d_from": d_from,
        "d_to": d_to,
    }

    def shape(m):
        return {
            # daily_atten_id keeps the rare duplicate (date, spell, eb)
            # attendance rows distinct in the grid.
            "row_id": (
                f"{m['tran_date']}|{m['spell_id']}|{m['eb_id']}|{m['daily_atten_id']}"
                if direction == "attendance"
                else f"{m['tran_date']}|{m['spell_id']}|{m['eb_id']}"
            ),
            "tran_date": m["tran_date"].isoformat(),
            "spell_id": m["spell_id"],
            "spell_code": m["spell_code"],
            "eb_id": m["eb_id"],
            "eb_no": m["eb_no"],
            "worker_name": m["worker_name"] or m["eb_no"],
            "designation": m["designation"],
            "quality": m["quality"],
            "net_hours": _f(m, "net_hours"),
            "doff_count": int(m["doff_count"] or 0),
            "production_qty": _f(m, "production_qty"),
        }

    try:
        desig_ids = _map_ref_ids(db, co_id, branch_id, "winding", "designation")
        if direction == "attendance":
            if desig_ids:
                binds["desig_ids"] = desig_ids
            else:
                binds["desig_pattern"] = WINDER_DESIG_PATTERN
            rows = _checklist_rows(
                db,
                get_winding_attendance_checklist_query(bool(desig_ids)),
                binds,
                shape,
                winding_attendance_remarks,
                rtype,
            )
        else:
            desig_set = set(desig_ids)
            rows = _checklist_rows(
                db,
                get_winding_production_checklist_query(),
                binds,
                shape,
                lambda m: winding_production_remarks(m, desig_set),
                rtype,
            )
        return {"data": _sort_checklist(rows, "eb_no")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/beaming_checklist")
def beaming_checklist(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Beaming machine hours (from machine attendance) vs cuts produced."""
    co_id, branch_id, d_from, d_to = _checklist_scope(request, CHECKLIST_MAX_DAYS)
    rtype = (request.query_params.get("rtype") or "chk").lower()

    def shape(m):
        return {
            "row_id": f"{m['tran_date']}|{m['spell_id']}|{m['machine_id']}",
            "tran_date": m["tran_date"].isoformat(),
            "spell_id": m["spell_id"],
            "spell_code": m["spell_code"],
            "machine_id": m["machine_id"],
            "mech_code": m["mech_code"],
            "machine_name": m["machine_name"],
            "workers": m["workers"],
            "quality_code": m["quality_code"],
            "quality": m["quality"],
            "net_hours": _f(m, "net_hours"),
            "cuts": _f(m, "cuts"),
            "beams": int(m["beams"] or 0),
        }

    try:
        mt_ids = _map_ref_ids(db, co_id, branch_id, "beaming", "machine_type")
        binds = {
            "co_id": co_id,
            "branch_id": branch_id,
            "d_from": d_from,
            "d_to": d_to,
        }
        binds["mt_ids" if mt_ids else "beaming_type"] = mt_ids or BEAMING_MACHINE_TYPE
        rows = _checklist_rows(
            db,
            get_beaming_checklist_query(bool(mt_ids)),
            binds,
            shape,
            beaming_remarks,
            rtype,
        )
        return {"data": _sort_checklist(rows, "mech_code")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/finishing_checklist")
def finishing_checklist(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Finishing attendance vs finishing production, matched both ways.

    The finishing designations come from ``jute_prod_checklist_map`` (DB-only
    config, no UI). An unconfigured branch returns an empty grid."""
    co_id, branch_id, d_from, d_to = _checklist_scope(request, CHECKLIST_MAX_DAYS)
    rtype = (request.query_params.get("rtype") or "chk").lower()

    def shape(m):
        return {
            "row_id": f"{m['tran_date']}|{m['spell_id']}|{m['eb_id'] if m['eb_id'] is not None else '-'}",
            "tran_date": m["tran_date"].isoformat(),
            "spell_id": m["spell_id"],
            "spell_code": m["spell_code"],
            "eb_id": m["eb_id"],
            "eb_no": m["eb_no"],
            "worker_name": m["worker_name"] or m["eb_no"],
            "designation": m["designation"],
            "quality": m["quality"],
            "net_hours": _f(m, "net_hours"),
            "prod_qty": _f(m, "prod_qty"),
            "prod_wt_kg": _f(m, "prod_wt_kg"),
        }

    try:
        desig_ids = _map_ref_ids(db, co_id, branch_id, "finishing", "designation")
        if not desig_ids:
            # Map-driven only — an unconfigured branch gets an empty grid rather
            # than the old '%finish%' false positives.
            return {"data": []}
        desig_set = set(desig_ids)
        rows = _checklist_rows(
            db,
            get_finishing_checklist_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "d_from": d_from,
                "d_to": d_to,
                "desig_ids": desig_ids,
            },
            shape,
            lambda m: finishing_remarks(m, desig_set),
            rtype,
        )
        return {"data": _sort_checklist(rows, "eb_no")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/attendance_checklist")
def attendance_checklist(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Plain attendance listing with the machines each worker was booked on.

    No remarks and no ``rtype`` — this one is the reference sheet the other
    checklists are argued against. Range capped tighter (31 days) because it
    returns every row instead of only the exceptions.
    """
    co_id, branch_id, d_from, d_to = _checklist_scope(
        request, ATTENDANCE_CHECKLIST_MAX_DAYS
    )
    try:
        _retire_rejected_attendance(db, co_id, branch_id, d_from, d_to)
        rows = []
        for r in db.execute(
            get_attendance_checklist_query(),
            {"co_id": co_id, "branch_id": branch_id, "d_from": d_from, "d_to": d_to},
        ).fetchall():
            m = dict(r._mapping)
            rows.append(
                {
                    "row_id": str(m["daily_atten_id"]),
                    "tran_date": m["tran_date"].isoformat(),
                    "spell_id": m["spell_id"],
                    "spell_code": m["spell_code"],
                    "eb_id": m["eb_id"],
                    "eb_no": m["eb_no"],
                    "worker_name": m["worker_name"] or m["eb_no"],
                    "department": m["department"],
                    "designation": m["designation"],
                    "attendance_type": m["attendance_type"],
                    "net_hours": _f(m, "net_hours"),
                    "machines": m["machines"],
                }
            )
        return {"data": _sort_checklist(rows, "eb_no")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
