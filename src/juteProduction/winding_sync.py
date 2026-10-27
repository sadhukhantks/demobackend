"""Winding "Sync from attendance" (TKT-000171). Prefix /api/windingProd.

Why this is NOT a Process/Reprocess
-----------------------------------
Spinning and weaving freeze a day-slice into a log (jute_prod_spinning_daily /
jute_prod_weaving_log) that goes stale when attendance, standards or SQC
change, hence their Process / Re-process / Sync buttons. Winding freezes
nothing: the Winder Wise tab, the quality-wise reports, the DPR block and the
Low Producer report all compute production at read time from the doff + jugar
rows (winding_models.py), and attendance hours are read live where they are
used at all. There is no winding log to recompute and this module does not
invent one.

What CAN go stale is the person -> quality map (jute_prod_winding_daily_qlty):
"Get Quality" seeds it by carrying the previous spell's winders forward, so
whoever attendance says moved machine, came in, or stayed home is only
reflected if somebody edits the map by hand. That map feeds the doff form's
prefill + machine shortcut, the Machine column on the Doff grid and the Winder
Wise tab (TKT-000169), and the reconciliation's yarn fallback. Sync reconciles
it against the unit's attendance — the winding analogue of spinning's
attendance fill-sync (spec 5.4), never a freeze.

POST /quality_sync  body {co_id, branch_id, tran_date, spell_id | spell,
                          mode: "fill" | "force", remove_absent: bool}

mode "fill" (default; spinning's fill semantics — never overwrites what is set):
  * ADD a map row for every winder the unit's attendance marks on a Winding-type
    machine of the branch, or who already has doffs in the unit, and who is not
    on the map (machine from attendance, yarn from their doffs else their most
    recent earlier map row, spindles likewise);
  * FILL a blank machine on an existing map row from attendance;
  * REPORT: map machine <> attendance machine (what "force" would change),
    map winders with no attendance in the unit (with "has doffs" and the other
    spell they were marked in), winders marked on two machines.
mode "force" (Edit permission, like spinning's force sync): fill + overwrite
  the map's machine with the attendance machine where they differ.
remove_absent (Edit permission): soft-delete map rows with NEITHER attendance
  NOR doffs in the unit. Refused outright when the unit has no attendance at
  all — attendance is habitually late, and "nobody is marked yet" must never
  wipe the carried-forward map.

The reconciliation itself is a pure function (plan_winding_sync) so the rules
are unit-testable without a database; the endpoint only fetches, plans,
executes the plan and labels the result. Everything runs in ONE transaction.
"""

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import WINDING_MACHINE_TYPE_NAME
# Shared spell-resolution contract (spell_id preferred, branch-scoped code
# fallback) — import, do NOT copy. Same for the menu access-level probe.
from src.juteProduction.spinning_entry import _resolve_spell
from src.juteProduction.spinning_lock import EDIT_LEVEL, user_menu_access_level
from src.juteProduction.winding_query import (
    insert_quality_row_query,
    soft_delete_quality_row_query,
)
from src.juteProduction.winding_sync_query import (
    get_winding_sync_attendance_machines_query,
    get_winding_sync_attendance_query,
    get_winding_sync_doff_winders_query,
    get_winding_sync_last_map_query,
    get_winding_sync_map_query,
    get_winding_sync_other_spell_attendance_query,
    get_winding_sync_worker_labels_query,
    update_winding_sync_map_machine_query,
)

router = APIRouter()

WINDING_MENU_PATH = "juteProduction/winding"
SYNC_MODES = ("fill", "force")

BRANCH_REQUIRED_MSG = "branch_id is required — winding is branch-keyed"
MODE_MSG = "mode must be 'fill' or 'force'"
SYNC_EDIT_MSG = (
    "Applying attendance machines or removing absent winders requires Edit "
    "permission for the Winding Production menu."
)
REMOVE_NO_ATTENDANCE_MSG = (
    "No attendance found for this date and spell, so nothing was removed — "
    "enter attendance first, then sync again."
)


class QualitySyncRequest(BaseModel):
    co_id: int
    branch_id: Optional[int] = None
    tran_date: date
    spell: Optional[str] = None  # deprecated — send spell_id
    spell_id: Optional[int] = None  # preferred; exactly one of spell/spell_id
    mode: str = "fill"
    remove_absent: bool = False


# =============================================================================
# Pure planner
# =============================================================================


def _first_machine(machines: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """First-marked machine wins (rows arrive ordered by dtl_rec_id)."""
    return machines[0] if machines else None


def plan_winding_sync(
    map_rows: List[Dict[str, Any]],
    att_by_eb: Dict[int, Dict[str, Any]],
    att_mc_by_eb: Dict[int, List[Dict[str, Any]]],
    doff_by_eb: Dict[int, Dict[str, Any]],
    *,
    mode: str = "fill",
    remove_absent: bool = False,
) -> Dict[str, Any]:
    """Reconcile the unit's map against its attendance. Pure — no I/O.

    map_rows     active map rows: {winding_daily_qlty_id, eb_id, machine_id, ...}
    att_by_eb    winders with attendance in the unit: eb_id -> {daily_atten_id, att_rows}
    att_mc_by_eb eb_id -> [ {machine_id, mech_code, machine_name, dtl_rec_id}, ...]
                 (Winding-type machines of the branch only, first-marked first)
    doff_by_eb   eb_id -> {item_id, doffs} for winders with doffs in the unit

    Returns a plan the endpoint executes verbatim:
      attendance_missing  no attendance in the unit at all -> absent/remove suppressed
      add                 [{eb_id, machine, item_id, source}]  source 'attendance'|'doff'
      machine_fill        [{row, machine}]  blank map machine, attendance has one
      machine_update      [{row, machine}]  force only: differing machine overwritten
      machine_differs     [{row, machine}]  fill only: the same rows, report-only
      absent              [{row, has_doffs}] map winders without attendance
      remove              [row]  remove_absent: absent rows without doffs
      machine_ambiguous   [{eb_id, machine_ids, used_machine_id}]
    """
    on_map = {int(r["eb_id"]) for r in map_rows if r.get("eb_id") is not None}
    attendance_missing = len(att_by_eb) == 0

    machine_ambiguous = []
    for eb_id, machines in att_mc_by_eb.items():
        ids = sorted({int(m["machine_id"]) for m in machines})
        if len(ids) > 1:
            machine_ambiguous.append({
                "eb_id": int(eb_id),
                "machine_ids": ids,
                "used_machine_id": int(machines[0]["machine_id"]),
            })

    # ADD: certain winders only — marked on a winding machine, or already doffing.
    add = []
    for eb_id in sorted(set(att_mc_by_eb) | set(doff_by_eb)):
        if eb_id in on_map:
            continue
        machine = _first_machine(att_mc_by_eb.get(eb_id, []))
        doff = doff_by_eb.get(eb_id)
        add.append({
            "eb_id": int(eb_id),
            "machine": machine,
            "item_id": doff["item_id"] if doff else None,
            "source": "attendance" if machine is not None else "doff",
        })

    machine_fill, machine_update, machine_differs, absent, remove = [], [], [], [], []
    for row in map_rows:
        if row.get("eb_id") is None:
            continue
        eb_id = int(row["eb_id"])
        machine = _first_machine(att_mc_by_eb.get(eb_id, []))
        if machine is not None:
            current = row.get("machine_id")
            if current is None:
                machine_fill.append({"row": row, "machine": machine})
            elif int(current) != int(machine["machine_id"]):
                (machine_update if mode == "force" else machine_differs).append(
                    {"row": row, "machine": machine})
        if not attendance_missing and eb_id not in att_by_eb:
            has_doffs = eb_id in doff_by_eb
            absent.append({"row": row, "has_doffs": has_doffs})
            if remove_absent and not has_doffs:
                remove.append(row)

    return {
        "attendance_missing": attendance_missing,
        "add": add,
        "machine_fill": machine_fill,
        "machine_update": machine_update,
        "machine_differs": machine_differs,
        "absent": absent,
        "remove": remove,
        "machine_ambiguous": machine_ambiguous,
    }


# =============================================================================
# Result shaping
# =============================================================================


def _row_ref(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "winding_daily_qlty_id": row.get("winding_daily_qlty_id"),
        "eb_id": row.get("eb_id"),
        "emp_code": row.get("emp_code"),
        "worker_name": row.get("worker_name"),
    }


def _machine_change(row: Dict[str, Any], machine: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **_row_ref(row),
        "old_machine_id": row.get("machine_id"),
        "old_mech_code": row.get("mech_code"),
        "old_machine_name": row.get("machine_name"),
        "machine_id": machine.get("machine_id"),
        "mech_code": machine.get("mech_code"),
        "machine_name": machine.get("machine_name"),
    }


def _i(v) -> Optional[int]:
    return None if v is None else int(v)


def _rows(res) -> List[Dict[str, Any]]:
    return [dict(r._mapping) for r in res.fetchall()]


# =============================================================================
# Endpoint
# =============================================================================


@router.post("/quality_sync")
def quality_sync(
    body: QualitySyncRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        if body.mode not in SYNC_MODES:
            raise HTTPException(status_code=400, detail=MODE_MSG)
        if body.branch_id is None:
            raise HTTPException(status_code=400, detail=BRANCH_REQUIRED_MSG)
        branch_id = int(body.branch_id)
        spell_id = int(_resolve_spell(db, body.spell_id, body.spell, branch_id))
        user_id = token_data.get("user_id")

        # Overwriting the map's machine or dropping rows = Edit, like spinning's
        # force sync. Plain fill is open to anyone who can use the page.
        if body.mode == "force" or body.remove_absent:
            level = user_menu_access_level(
                db, user_id, body.co_id, branch_id, WINDING_MENU_PATH)
            if level < EDIT_LEVEL:
                raise HTTPException(status_code=403, detail=SYNC_EDIT_MSG)

        binds = {"branch_id": branch_id, "tran_date": body.tran_date, "spell_id": spell_id}

        map_rows = _rows(db.execute(get_winding_sync_map_query(), binds))
        att_by_eb = {
            int(r.eb_id): {"daily_atten_id": _i(r.daily_atten_id), "att_rows": int(r.att_rows or 0)}
            for r in db.execute(get_winding_sync_attendance_query(), binds).fetchall()
        }
        att_mc_by_eb: Dict[int, List[Dict[str, Any]]] = {}
        for r in db.execute(
            get_winding_sync_attendance_machines_query(),
            {**binds, "machine_type_name": WINDING_MACHINE_TYPE_NAME},
        ).fetchall():
            att_mc_by_eb.setdefault(int(r.eb_id), []).append({
                "machine_id": int(r.machine_id),
                "mech_code": r.mech_code,
                "machine_name": r.machine_name,
                "dtl_rec_id": _i(r.dtl_rec_id),
            })
        doff_by_eb = {
            int(r.eb_id): {"item_id": _i(r.item_id), "doffs": int(r.doffs or 0)}
            for r in db.execute(get_winding_sync_doff_winders_query(), binds).fetchall()
        }

        plan = plan_winding_sync(
            map_rows, att_by_eb, att_mc_by_eb, doff_by_eb,
            mode=body.mode, remove_absent=body.remove_absent,
        )
        if body.remove_absent and plan["attendance_missing"]:
            raise HTTPException(status_code=400, detail=REMOVE_NO_ATTENDANCE_MSG)

        # --- ADD ----------------------------------------------------------
        added = []
        add_ebs = [a["eb_id"] for a in plan["add"]]
        labels: Dict[int, Dict[str, Any]] = {}
        carry: Dict[int, Dict[str, Any]] = {}
        if add_ebs:
            labels = {
                int(r.eb_id): {"emp_code": r.emp_code, "worker_name": r.worker_name}
                for r in db.execute(
                    get_winding_sync_worker_labels_query(), {"eb_ids": add_ebs}
                ).fetchall()
            }
            carry = {
                int(r.eb_id): {"item_id": _i(r.item_id), "no_of_spindle": int(r.no_of_spindle or 0)}
                for r in db.execute(
                    get_winding_sync_last_map_query(), {**binds, "eb_ids": add_ebs}
                ).fetchall()
            }
        for a in plan["add"]:
            eb_id = a["eb_id"]
            prior = carry.get(eb_id, {})
            item_id = a["item_id"] if a["item_id"] is not None else prior.get("item_id")
            machine = a["machine"] or {}
            res = db.execute(
                insert_quality_row_query(),
                {
                    "co_id": int(body.co_id),
                    "branch_id": branch_id,
                    "tran_date": body.tran_date,
                    "spell_id": spell_id,
                    "eb_id": eb_id,
                    "item_id": item_id,
                    "machine_id": machine.get("machine_id"),
                    "no_of_spindle": int(prior.get("no_of_spindle", 0) or 0),
                    "updated_by": user_id,
                },
            )
            added.append({
                "winding_daily_qlty_id": getattr(res, "lastrowid", None),
                "eb_id": eb_id,
                **labels.get(eb_id, {"emp_code": None, "worker_name": None}),
                "machine_id": machine.get("machine_id"),
                "mech_code": machine.get("mech_code"),
                "machine_name": machine.get("machine_name"),
                "item_id": item_id,
                "source": a["source"],
            })

        # --- MACHINE fill / force ------------------------------------------
        machine_filled, machine_updated = [], []
        for bucket, out in ((plan["machine_fill"], machine_filled),
                            (plan["machine_update"], machine_updated)):
            for change in bucket:
                row, machine = change["row"], change["machine"]
                db.execute(
                    update_winding_sync_map_machine_query(),
                    {"id": row["winding_daily_qlty_id"],
                     "machine_id": machine["machine_id"],
                     "updated_by": user_id},
                )
                out.append(_machine_change(row, machine))

        # --- REMOVE absentees (explicit, Edit) ------------------------------
        removed = []
        removed_ids = set()
        for row in plan["remove"]:
            db.execute(
                soft_delete_quality_row_query(),
                {"id": row["winding_daily_qlty_id"], "updated_by": user_id},
            )
            removed.append(_row_ref(row))
            removed_ids.add(row["winding_daily_qlty_id"])

        # --- REPORT ---------------------------------------------------------
        absent = [a for a in plan["absent"]
                  if a["row"]["winding_daily_qlty_id"] not in removed_ids]
        other_spells: Dict[int, List[str]] = {}
        if absent:
            for r in db.execute(
                get_winding_sync_other_spell_attendance_query(),
                {**binds, "eb_ids": [int(a["row"]["eb_id"]) for a in absent]},
            ).fetchall():
                other_spells.setdefault(int(r.eb_id), []).append(r.spell_code)
        exceptions = {
            "machine_differs": [_machine_change(c["row"], c["machine"])
                                for c in plan["machine_differs"]],
            "absent_on_map": [
                {**_row_ref(a["row"]),
                 "has_doffs": bool(a["has_doffs"]),
                 "other_spells": other_spells.get(int(a["row"]["eb_id"]), [])}
                for a in absent
            ],
            "machine_ambiguous": plan["machine_ambiguous"],
        }

        db.commit()
        return {"data": {
            "mode": body.mode,
            "attendance_missing": plan["attendance_missing"],
            "counts": {
                "attendance_winders": len(att_by_eb),
                "map_rows": len(map_rows),
                "doff_winders": len(doff_by_eb),
            },
            "added": added,
            "machine_filled": machine_filled,
            "machine_updated": machine_updated,
            "removed": removed,
            "exceptions": exceptions,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
