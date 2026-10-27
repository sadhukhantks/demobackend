"""Jute Production Planning — plan document + planning-norms endpoints (SPEC §3).

One APIRouter, registered in src/main.py under prefix ``/api/juteProdPlanning``.

A plan is a DAILY document: one active header per (co_id, branch_id, plan_date) with
three line sections (loom / yarn / dept) that are replaced WHOLESALE on every save,
in one transaction. There is NO approval workflow (jute-production convention):
direct CRUD, soft delete, status_id = 1 (Open).

Portal persona throughout: ``Depends(get_tenant_db)`` + ``Depends(get_current_user_with_refresh)``,
plain ``def`` handlers (Sync Handler Policy — JSON bodies via ``parse_json_body``),
every response wrapped as ``{"data": ...}``, co_id validated first (400), every param
type-cast, and ``None`` (never the string "null") for SQL NULL.

Helper style (``_require_co_id`` / ``_require_date`` / enum validation) and the
norms-resolution semantics (MAX(effective_date) <= on_date among active rows) mirror
``spng_target_map.py`` — planning norms are the same EAV contract as every
``*_target_map``, with id_type 'qid' | 'yid' | 'stage'.
"""

import math
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    PLANNING_CLOTH_ITEM_TYPE_ID,
    PLANNING_ID_TYPE_QLTY,
    PLANNING_ID_TYPE_STAGE,
    PLANNING_ID_TYPE_YARN,
    PLANNING_NORM_PARAMS,
    PLANNING_STAGES,
    PLANNING_STATUS_OPEN,
    PLANNING_VALUE_ROLE,
    PLANNING_YARN_ITEM_TYPE_ID,
)
from src.juteProduction.planning_query import (
    delete_planning_dept_lines_query,
    delete_planning_loom_lines_query,
    delete_planning_yarn_lines_query,
    find_duplicate_plan_query,
    get_planning_cloth_qualities_query,
    get_planning_dept_lines_query,
    get_planning_header_query,
    get_planning_latest_header_query,
    get_planning_loom_lines_query,
    get_planning_machine_counts_query,
    get_planning_norms_history_query,
    get_planning_table_query,
    get_planning_yarn_items_query,
    get_planning_yarn_lines_query,
    get_resolved_planning_norms_query,
    insert_planning_dept_line_query,
    insert_planning_header_query,
    insert_planning_loom_line_query,
    insert_planning_norm_query,
    insert_planning_yarn_line_query,
    resolve_planning_norm_query,
    soft_delete_planning_norm_query,
    soft_delete_planning_query,
    stamp_plan_no_query,
    update_planning_header_query,
)

router = APIRouter()

ID_TYPES = (PLANNING_ID_TYPE_QLTY, PLANNING_ID_TYPE_YARN, PLANNING_ID_TYPE_STAGE)

PLAN_NOT_FOUND_MSG = "Production plan not found"
DUPLICATE_PLAN_MSG = "A plan already exists for this date"

# Norm values are stored DECIMAL(14,4); compare at that precision so a re-save with
# an unchanged value never appends a duplicate norm row.
_NORM_DECIMALS = 4

# Stage lookups derived from the PLANNING_STAGES constant (SPEC §3).
_STAGE_BY_CODE = {code: (seq, code, label, mtype) for seq, code, label, mtype in PLANNING_STAGES}
_STAGE_BY_SEQ = {seq: (seq, code, label, mtype) for seq, code, label, mtype in PLANNING_STAGES}


# =============================================================================
# Helpers — query params
# =============================================================================


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _optional_branch_id(request: Request) -> Optional[int]:
    raw = request.query_params.get("branch_id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branch_id")


def _optional_int_param(request: Request, key: str) -> Optional[int]:
    raw = request.query_params.get(key)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _parse_date(raw: str, key: str) -> date:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key} (expected YYYY-MM-DD)")


def _require_date(request: Request, key: str) -> date:
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return _parse_date(raw, key)


def _optional_date(request: Request, key: str) -> Optional[date]:
    raw = request.query_params.get(key)
    if not raw:
        return None
    return _parse_date(raw, key)


def _require_enum(request: Request, key: str, allowed) -> str:
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    if raw not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid {key} '{raw}'")
    return raw


# =============================================================================
# Helpers — value casting
# =============================================================================


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _fo(v) -> Optional[float]:
    """Cast to float, preserving None."""
    return None if v is None else float(v)


def _i(v, default: Optional[int] = None) -> Optional[int]:
    return default if v is None else int(v)


def _d(v) -> Optional[str]:
    """Stringify a date-ish value for the JSON response, preserving None."""
    return None if v is None else str(v)


def _num(value, field: str, default: float = 0.0) -> float:
    """Body number -> float, rejecting NaN/inf and non-numeric input (400)."""
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid number for '{field}'")
    if not math.isfinite(out):
        raise HTTPException(status_code=400, detail=f"Value must be a finite number for '{field}'")
    return out


def _num_opt(value, field: str) -> Optional[float]:
    """Optional body number -> float or None (NULL-able columns)."""
    if value is None or value == "":
        return None
    return _num(value, field)


def _int_opt(value, field: str) -> Optional[int]:
    """Optional body integer -> int or None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid integer for '{field}'")


def _str_opt(value, limit: int) -> Optional[str]:
    """Optional body string, trimmed and truncated to the column width."""
    if value is None:
        return None
    out = str(value).strip()
    if not out:
        return None
    return out[:limit]


def _token(value, default: str, limit: int = 20) -> str:
    """A NOT NULL discriminator token (product_group / section / line_kind).

    Trimmed + upper-cased; falls back to `default` when the caller omits it, so a
    partially-filled FE row can never violate the NOT NULL constraint.
    """
    if value is None:
        return default
    out = str(value).strip().upper()
    return out[:limit] if out else default


def _body_date(body: Dict[str, Any], key: str, required: bool = False) -> Optional[date]:
    raw = body.get(key)
    if raw is None or raw == "":
        if required:
            raise HTTPException(status_code=400, detail=f"{key} is required")
        return None
    return _parse_date(raw, key)


# =============================================================================
# Helpers — response mappers
# =============================================================================


def _header_out(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "planning_id": m["planning_id"],
        "co_id": m.get("co_id"),
        "branch_id": m.get("branch_id"),
        "branch_name": m.get("branch_name"),
        "plan_date": _d(m.get("plan_date")),
        "plan_no": m.get("plan_no"),
        "plan_name": m.get("plan_name"),
        "remarks": m.get("remarks"),
        "status_id": _i(m.get("status_id")),
        "updated_by": _i(m.get("updated_by")),
        "updated_by_name": m.get("updated_by_name"),
        "updated_date_time": _d(m.get("updated_date_time")),
    }


def _loom_line_out(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "planning_loom_id": m["planning_loom_id"],
        "product_group": m.get("product_group"),
        "quality_item_id": _i(m.get("quality_item_id")),
        "quality_desc": m.get("quality_desc"),
        "quality_item_code": m.get("quality_item_code"),
        "quality_item_name": m.get("quality_item_name"),
        "looms_a": _f(m.get("looms_a")),
        "looms_b": _f(m.get("looms_b")),
        "looms_c": _f(m.get("looms_c")),
        "kg_per_loom_100": _f(m.get("kg_per_loom_100")),
        "eff_pct": _f(m.get("eff_pct")),
        "kg_per_loom": _f(m.get("kg_per_loom")),
        "prod_a": _f(m.get("prod_a")),
        "prod_b": _f(m.get("prod_b")),
        "prod_c": _f(m.get("prod_c")),
        "warp_yarn_item_id": _i(m.get("warp_yarn_item_id")),
        "weft_yarn_item_id": _i(m.get("weft_yarn_item_id")),
        "warp_pct": _fo(m.get("warp_pct")),
        "weft_pct": _fo(m.get("weft_pct")),
        "line_order": _i(m.get("line_order"), 0),
        "remarks": m.get("remarks"),
    }


def _yarn_line_out(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "planning_yarn_id": m["planning_yarn_id"],
        "line_kind": m.get("line_kind"),
        "section": m.get("section"),
        "yarn_item_id": _i(m.get("yarn_item_id")),
        "yarn_desc": m.get("yarn_desc"),
        "yarn_item_code": m.get("yarn_item_code"),
        "yarn_item_name": m.get("yarn_item_name"),
        "warp_weft": m.get("warp_weft"),
        "pct_of_prod": _fo(m.get("pct_of_prod")),
        "req_kg_a": _f(m.get("req_kg_a")),
        "req_kg_b": _f(m.get("req_kg_b")),
        "req_kg_c": _f(m.get("req_kg_c")),
        "req_kg_day": _f(m.get("req_kg_day")),
        "kg_per_frame_100": _f(m.get("kg_per_frame_100")),
        "eff_pct": _f(m.get("eff_pct")),
        "kg_per_frame": _f(m.get("kg_per_frame")),
        "frames_required": _f(m.get("frames_required")),
        "frames_a": _f(m.get("frames_a")),
        "frames_b": _f(m.get("frames_b")),
        "frames_c": _f(m.get("frames_c")),
        "priority": _i(m.get("priority")),
        "order_mt": _fo(m.get("order_mt")),
        "days_needed": _fo(m.get("days_needed")),
        "delivery_date": _d(m.get("delivery_date")),
        "line_order": _i(m.get("line_order"), 0),
        "remarks": m.get("remarks"),
    }


def _dept_line_out(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "planning_dept_id": m["planning_dept_id"],
        "stage_seq": _i(m.get("stage_seq")),
        "stage_code": m.get("stage_code"),
        "stage_label": m.get("stage_label"),
        "output_kg_day": _f(m.get("output_kg_day")),
        "waste_pct": _f(m.get("waste_pct")),
        "input_kg_day": _f(m.get("input_kg_day")),
        "kg_per_machine": _f(m.get("kg_per_machine")),
        "machines_required": _f(m.get("machines_required")),
        "machines_a": _f(m.get("machines_a")),
        "machines_b": _f(m.get("machines_b")),
        "machines_c": _f(m.get("machines_c")),
        "machines_installed": _i(m.get("machines_installed")),
        "machines_maint": _i(m.get("machines_maint")),
        "machines_ready": _i(m.get("machines_ready")),
        "line_order": _i(m.get("line_order"), 0),
        "remarks": m.get("remarks"),
    }


def _load_document(db: Session, planning_id: int, header: Dict[str, Any]) -> Dict[str, Any]:
    """Header + the three active line sections (line_order ASC) as one document."""
    loom_rows = db.execute(
        get_planning_loom_lines_query(), {"planning_id": planning_id}
    ).fetchall()
    yarn_rows = db.execute(
        get_planning_yarn_lines_query(), {"planning_id": planning_id}
    ).fetchall()
    dept_rows = db.execute(
        get_planning_dept_lines_query(), {"planning_id": planning_id}
    ).fetchall()

    return {
        "header": _header_out(header),
        "loom_lines": [_loom_line_out(dict(r._mapping)) for r in loom_rows],
        "yarn_lines": [_yarn_line_out(dict(r._mapping)) for r in yarn_rows],
        "dept_lines": [_dept_line_out(dict(r._mapping)) for r in dept_rows],
    }


# =============================================================================
# Helpers — norms
# =============================================================================


def norm_params_for(id_type: str) -> List[str]:
    """Ordered valid params for a norms dimension (SPEC §2.5).

    Single source of truth for the norms grid columns and for entry validation.
    Returns [] for an unknown id_type; callers validate the id_type enum separately.
    """
    return list(PLANNING_NORM_PARAMS.get(id_type, ()))


def _validate_norm_entry(id_type: str, param: str) -> None:
    if id_type not in ID_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid id_type '{id_type}'")
    if param not in norm_params_for(id_type):
        raise HTTPException(
            status_code=400, detail=f"Invalid param '{param}' for id_type '{id_type}'"
        )


def _resolved_norm_map(
    db: Session, co_id: int, on_date: date, id_type: Optional[str] = None
) -> Dict[Tuple[str, int, str], Dict[str, Any]]:
    """{(id_type, ref_id, param): {value, effective_date}} effective on ``on_date``.

    Rows arrive ordered ascending by planning_norm_id within a key, so the LAST one
    written into the dict wins — i.e. the newest row at MAX(effective_date) <=
    on_date, exactly the ``ORDER BY effective_date DESC, id DESC LIMIT 1`` rule.
    """
    rows = db.execute(
        get_resolved_planning_norms_query(),
        {"co_id": co_id, "on_date": on_date, "id_type": id_type},
    ).fetchall()
    out: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for r in rows:
        m = dict(r._mapping)
        key = (m["id_type"], int(m["ref_id"]), m["param"])
        out[key] = {
            "value": _f(m.get("value")),
            "effective_date": _d(m.get("effective_date")),
        }
    return out


def _upsert_norm_entries(
    db: Session,
    co_id: int,
    branch_id: Optional[int],
    effective_date: date,
    entries: List[Dict[str, Any]],
    user_id: Optional[int],
) -> Tuple[int, int]:
    """Insert a norm row per entry, SKIPPING entries equal to the resolved value.

    Mirrors ``target_map_bulk_save``: resolve the key as production reads it
    (LAST-DATE, branch-agnostic), then insert a new effective-dated row only when the
    value actually differs. Returns (inserted, skipped). The caller owns the commit.
    """
    resolve_q = resolve_planning_norm_query()
    insert_q = insert_planning_norm_query()
    inserted = 0
    skipped = 0

    for entry in entries:
        id_type = entry["id_type"]
        ref_id = int(entry["ref_id"])
        param = entry["param"]
        value = float(entry["value"])

        current = db.execute(
            resolve_q,
            {
                "co_id": co_id,
                "id_type": id_type,
                "ref_id": ref_id,
                "param": param,
                "on_date": effective_date,
            },
        ).fetchone()

        if (
            current is not None
            and current.value is not None
            and round(float(current.value), _NORM_DECIMALS) == round(value, _NORM_DECIMALS)
        ):
            skipped += 1
            continue

        db.execute(
            insert_q,
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "effective_date": effective_date,
                "ref_id": ref_id,
                "id_type": id_type,
                "value_role": PLANNING_VALUE_ROLE,
                "param": param,
                "value": value,
                "updated_by": user_id,
            },
        )
        inserted += 1

    return inserted, skipped


def _norm_entries_from_lines(
    loom_lines: List[Dict[str, Any]],
    yarn_lines: List[Dict[str, Any]],
    dept_lines: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Norm entries implied by a saved plan (planning_save's ``update_norms``).

    qid   (ref = quality_item_id): kg_per_loom_100, warp_pct, weft_pct,
          warp_yarn_item / weft_yarn_item (value = the yarn item_id), std_eff (= eff_pct)
    yid   (ref = yarn_item_id)   : kg_per_frame_100, std_eff (= eff_pct)
    stage (ref = stage_seq)      : kg_per_machine, waste_pct
    Entries with no ref or a NULL value are dropped (nothing to learn from them).
    """
    entries: List[Dict[str, Any]] = []

    def push(id_type: str, ref_id: Optional[int], param: str, value) -> None:
        if ref_id is None:
            return
        # Same coercion as the line writers: None/"" mean "nothing to learn",
        # genuinely non-numeric input is a 400 — never a mid-transaction 500.
        num = _num_opt(value, param)
        if num is None:
            return
        entries.append(
            {"id_type": id_type, "ref_id": int(ref_id), "param": param, "value": float(num)}
        )

    for line in loom_lines:
        ref = line.get("quality_item_id")
        push(PLANNING_ID_TYPE_QLTY, ref, "kg_per_loom_100", line.get("kg_per_loom_100"))
        push(PLANNING_ID_TYPE_QLTY, ref, "warp_pct", line.get("warp_pct"))
        push(PLANNING_ID_TYPE_QLTY, ref, "weft_pct", line.get("weft_pct"))
        push(PLANNING_ID_TYPE_QLTY, ref, "warp_yarn_item", line.get("warp_yarn_item_id"))
        push(PLANNING_ID_TYPE_QLTY, ref, "weft_yarn_item", line.get("weft_yarn_item_id"))
        push(PLANNING_ID_TYPE_QLTY, ref, "std_eff", line.get("eff_pct"))

    for line in yarn_lines:
        ref = line.get("yarn_item_id")
        push(PLANNING_ID_TYPE_YARN, ref, "kg_per_frame_100", line.get("kg_per_frame_100"))
        push(PLANNING_ID_TYPE_YARN, ref, "std_eff", line.get("eff_pct"))

    for line in dept_lines:
        # Resolve the stage the same way _dept_line_params does — a row sent
        # with only stage_code would otherwise silently record no stage norms.
        ref = _int_opt(line.get("stage_seq"), "stage_seq")
        if ref is None:
            raw_code = line.get("stage_code")
            stage = _STAGE_BY_CODE.get(str(raw_code).upper()) if raw_code else None
            ref = stage[0] if stage else None
        push(PLANNING_ID_TYPE_STAGE, ref, "kg_per_machine", line.get("kg_per_machine"))
        push(PLANNING_ID_TYPE_STAGE, ref, "waste_pct", line.get("waste_pct"))

    return entries


# =============================================================================
# Helpers — line normalisation (body -> INSERT bind params)
# =============================================================================


def _loom_line_params(line: Dict[str, Any], planning_id: int, order: int, user_id) -> Dict[str, Any]:
    return {
        "planning_id": planning_id,
        "product_group": _token(line.get("product_group"), "OTHER"),
        "quality_item_id": _int_opt(line.get("quality_item_id"), "quality_item_id"),
        "quality_desc": _str_opt(line.get("quality_desc"), 120),
        "looms_a": _num(line.get("looms_a"), "looms_a"),
        "looms_b": _num(line.get("looms_b"), "looms_b"),
        "looms_c": _num(line.get("looms_c"), "looms_c"),
        "kg_per_loom_100": _num(line.get("kg_per_loom_100"), "kg_per_loom_100"),
        "eff_pct": _num(line.get("eff_pct"), "eff_pct"),
        "kg_per_loom": _num(line.get("kg_per_loom"), "kg_per_loom"),
        "prod_a": _num(line.get("prod_a"), "prod_a"),
        "prod_b": _num(line.get("prod_b"), "prod_b"),
        "prod_c": _num(line.get("prod_c"), "prod_c"),
        "warp_yarn_item_id": _int_opt(line.get("warp_yarn_item_id"), "warp_yarn_item_id"),
        "weft_yarn_item_id": _int_opt(line.get("weft_yarn_item_id"), "weft_yarn_item_id"),
        "warp_pct": _num_opt(line.get("warp_pct"), "warp_pct"),
        "weft_pct": _num_opt(line.get("weft_pct"), "weft_pct"),
        "line_order": order,
        "remarks": _str_opt(line.get("remarks"), 255),
        "updated_by": user_id,
    }


def _yarn_line_params(line: Dict[str, Any], planning_id: int, order: int, user_id) -> Dict[str, Any]:
    warp_weft = _str_opt(line.get("warp_weft"), 2)
    return {
        "planning_id": planning_id,
        "line_kind": _token(line.get("line_kind"), "CLOTH", 10),
        "section": _token(line.get("section"), "OTHER"),
        "yarn_item_id": _int_opt(line.get("yarn_item_id"), "yarn_item_id"),
        "yarn_desc": _str_opt(line.get("yarn_desc"), 120),
        "warp_weft": warp_weft.upper() if warp_weft else None,
        "pct_of_prod": _num_opt(line.get("pct_of_prod"), "pct_of_prod"),
        "req_kg_a": _num(line.get("req_kg_a"), "req_kg_a"),
        "req_kg_b": _num(line.get("req_kg_b"), "req_kg_b"),
        "req_kg_c": _num(line.get("req_kg_c"), "req_kg_c"),
        "req_kg_day": _num(line.get("req_kg_day"), "req_kg_day"),
        "kg_per_frame_100": _num(line.get("kg_per_frame_100"), "kg_per_frame_100"),
        "eff_pct": _num(line.get("eff_pct"), "eff_pct"),
        "kg_per_frame": _num(line.get("kg_per_frame"), "kg_per_frame"),
        "frames_required": _num(line.get("frames_required"), "frames_required"),
        "frames_a": _num(line.get("frames_a"), "frames_a"),
        "frames_b": _num(line.get("frames_b"), "frames_b"),
        "frames_c": _num(line.get("frames_c"), "frames_c"),
        "priority": _int_opt(line.get("priority"), "priority"),
        "order_mt": _num_opt(line.get("order_mt"), "order_mt"),
        "days_needed": _num_opt(line.get("days_needed"), "days_needed"),
        "delivery_date": _body_date(line, "delivery_date"),
        "line_order": order,
        "remarks": _str_opt(line.get("remarks"), 255),
        "updated_by": user_id,
    }


def _dept_line_params(line: Dict[str, Any], planning_id: int, order: int, user_id) -> Dict[str, Any]:
    """Normalise a Section-3 row, resolving stage_seq/code/label from PLANNING_STAGES.

    Either stage_code or stage_seq identifies the stage; the missing members are
    filled from the constant so the NOT NULL columns are always populated. A row
    carrying neither (or an unknown stage) is a 400.
    """
    raw_code = _str_opt(line.get("stage_code"), 20)
    raw_seq = _int_opt(line.get("stage_seq"), "stage_seq")
    stage = None
    if raw_code:
        stage = _STAGE_BY_CODE.get(raw_code.upper())
    if stage is None and raw_seq is not None:
        stage = _STAGE_BY_SEQ.get(raw_seq)
    if stage is None:
        raise HTTPException(
            status_code=400, detail="Each dept line needs a valid stage_code or stage_seq"
        )
    seq, code, label, _machine_type = stage

    return {
        "planning_id": planning_id,
        "stage_seq": seq,
        "stage_code": code,
        "stage_label": _str_opt(line.get("stage_label"), 50) or label,
        "output_kg_day": _num(line.get("output_kg_day"), "output_kg_day"),
        "waste_pct": _num(line.get("waste_pct"), "waste_pct"),
        "input_kg_day": _num(line.get("input_kg_day"), "input_kg_day"),
        "kg_per_machine": _num(line.get("kg_per_machine"), "kg_per_machine"),
        "machines_required": _num(line.get("machines_required"), "machines_required"),
        "machines_a": _num(line.get("machines_a"), "machines_a"),
        "machines_b": _num(line.get("machines_b"), "machines_b"),
        "machines_c": _num(line.get("machines_c"), "machines_c"),
        "machines_installed": _int_opt(line.get("machines_installed"), "machines_installed"),
        "machines_maint": _int_opt(line.get("machines_maint"), "machines_maint"),
        "machines_ready": _int_opt(line.get("machines_ready"), "machines_ready"),
        "line_order": order,
        "remarks": _str_opt(line.get("remarks"), 255),
        "updated_by": user_id,
    }


def _lines(body: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    raw = body.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail=f"{key} must be a list")
    return [x for x in raw if isinstance(x, dict)]


# =============================================================================
# Plan list
# =============================================================================


@router.get("/planning_table")
def planning_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Plan headers (newest first) with total cloth kg/day + raw jute kg/day."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    from_date = _optional_date(request, "from_date")
    to_date = _optional_date(request, "to_date")
    try:
        rows = db.execute(
            get_planning_table_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "from_date": from_date,
                "to_date": to_date,
            },
        ).fetchall()

        data = []
        for r in rows:
            m = dict(r._mapping)
            item = _header_out(m)
            item["total_cloth_kg"] = _f(m.get("total_cloth_kg"))
            item["raw_jute_kg"] = _f(m.get("raw_jute_kg"))
            data.append(item)
        return {"data": data}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Setup
# =============================================================================


@router.get("/planning_setup")
def planning_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Everything the create/edit page needs before the first keystroke.

    cloth_qualities (item_type_id=5) • yarn_items (item_type_id=4 + jute_yarn_count)
    • stages (the 7 PLANNING_STAGES with machines_installed resolved by machine-type
    NAME, case-insensitive — a type absent in the tenant yields 0) • norms resolved
    for plan_date • latest_planning_id (newest plan on/before plan_date, for
    "Copy latest plan"). plan_date is optional and defaults to today.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    plan_date = _optional_date(request, "plan_date") or date.today()
    try:
        cloth_qualities = []
        for r in db.execute(
            get_planning_cloth_qualities_query(),
            {"co_id": co_id, "item_type_id": PLANNING_CLOTH_ITEM_TYPE_ID},
        ).fetchall():
            m = dict(r._mapping)
            cloth_qualities.append(
                {
                    "item_id": m["item_id"],
                    "item_code": m.get("item_code"),
                    "item_name": m.get("item_name"),
                    "item_grp_id": _i(m.get("item_grp_id")),
                    "grp_name": m.get("grp_name"),
                }
            )

        yarn_items = []
        for r in db.execute(
            get_planning_yarn_items_query(),
            {"co_id": co_id, "item_type_id": PLANNING_YARN_ITEM_TYPE_ID},
        ).fetchall():
            m = dict(r._mapping)
            yarn_items.append(
                {
                    "item_id": m["item_id"],
                    "item_code": m.get("item_code"),
                    "item_name": m.get("item_name"),
                    "grp_name": m.get("grp_name"),
                    "yarn_count": _fo(m.get("yarn_count")),
                }
            )

        # Machine counts keyed by LOWER(machine_type_name) — stage resolution is by
        # NAME, case-insensitive; a missing type simply resolves to 0.
        counts: Dict[str, int] = {}
        for r in db.execute(
            get_planning_machine_counts_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchall():
            m = dict(r._mapping)
            name = m.get("machine_type_name")
            if name is not None:
                counts[str(name).lower()] = _i(m.get("machine_count"), 0)

        stages = [
            {
                "stage_seq": seq,
                "stage_code": code,
                "stage_label": label,
                "machine_type_name": machine_type,
                "machines_installed": counts.get(machine_type.lower(), 0),
            }
            for seq, code, label, machine_type in PLANNING_STAGES
        ]

        norms = [
            {"id_type": key[0], "ref_id": key[1], "param": key[2], "value": val["value"]}
            for key, val in _resolved_norm_map(db, co_id, plan_date).items()
        ]

        latest = db.execute(
            get_planning_latest_header_query(),
            {"co_id": co_id, "branch_id": branch_id, "before_date": plan_date},
        ).fetchone()

        return {
            "data": {
                "plan_date": str(plan_date),
                "cloth_qualities": cloth_qualities,
                "yarn_items": yarn_items,
                "stages": stages,
                "norms": norms,
                "latest_planning_id": _i(latest.planning_id) if latest else None,
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Read one document
# =============================================================================


@router.get("/planning_by_id/{planning_id}")
def planning_by_id(
    planning_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Full plan document: header + loom / yarn / dept lines (active, line_order ASC)."""
    co_id = _require_co_id(request)
    try:
        header = db.execute(
            get_planning_header_query(), {"planning_id": int(planning_id), "co_id": co_id}
        ).fetchone()
        if not header:
            raise HTTPException(status_code=404, detail=PLAN_NOT_FOUND_MSG)

        return {"data": _load_document(db, int(planning_id), dict(header._mapping))}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/planning_latest")
def planning_latest(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Newest active plan with plan_date <= before_date ("Copy latest plan").

    Same document shape as planning_by_id; ``{"data": null}`` when there is none.
    before_date is optional (unbounded = the newest plan there is).
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    before_date = _optional_date(request, "before_date")
    try:
        header = db.execute(
            get_planning_latest_header_query(),
            {"co_id": co_id, "branch_id": branch_id, "before_date": before_date},
        ).fetchone()
        if not header:
            return {"data": None}

        m = dict(header._mapping)
        return {"data": _load_document(db, int(m["planning_id"]), m)}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Save (create or update)
# =============================================================================


@router.post("/planning_save")
def planning_save(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create or update a plan document in ONE transaction (SPEC §3).

    Body: {co_id, branch_id, planning_id?|null, plan_date, plan_name?, remarks?,
           loom_lines:[], yarn_lines:[], dept_lines:[], update_norms?:bool}

    * Uniqueness — another ACTIVE header on the same (co_id, branch_id, plan_date)
      (excluding the row being updated) -> 400 "A plan already exists for this date".
    * Lines are replaced WHOLESALE: on update the children are deleted and re-inserted
      with a fresh line_order (a brand-new plan has none to delete).
    * On insert, plan_no is stamped CONCAT('JPP-', LPAD(planning_id,5,'0')).
    * update_norms=true additionally upserts jute_prod_planning_norm rows at
      effective_date = plan_date, inserting only where the value differs from the
      currently-resolved one.

    Returns {"data": {"planning_id", "plan_no", ...}}.
    """
    body = parse_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")

    raw_co_id = body.get("co_id")
    if raw_co_id is None or raw_co_id == "":
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        co_id = int(raw_co_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid co_id")

    branch_id = _int_opt(body.get("branch_id"), "branch_id")
    planning_id = _int_opt(body.get("planning_id"), "planning_id")
    if planning_id is not None and planning_id <= 0:
        planning_id = None
    plan_date = _body_date(body, "plan_date", required=True)
    plan_name = _str_opt(body.get("plan_name"), 100)
    remarks = _str_opt(body.get("remarks"), 500)
    update_norms = bool(body.get("update_norms"))

    loom_lines = _lines(body, "loom_lines")
    yarn_lines = _lines(body, "yarn_lines")
    dept_lines = _lines(body, "dept_lines")

    user_id = token_data.get("user_id") if isinstance(token_data, dict) else None
    is_update = planning_id is not None

    # Normalise (and therefore VALIDATE) every line BEFORE touching the DB, so a bad
    # row 400s without any partial write. planning_id is patched in below once the
    # header insert has produced it.
    loom_params = [
        _loom_line_params(line, 0, order, user_id) for order, line in enumerate(loom_lines)
    ]
    yarn_params = [
        _yarn_line_params(line, 0, order, user_id) for order, line in enumerate(yarn_lines)
    ]
    dept_params = [
        _dept_line_params(line, 0, order, user_id) for order, line in enumerate(dept_lines)
    ]

    try:
        existing_plan_no = None
        if is_update:
            existing = db.execute(
                get_planning_header_query(), {"planning_id": planning_id, "co_id": co_id}
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail=PLAN_NOT_FOUND_MSG)
            existing_plan_no = dict(existing._mapping).get("plan_no")

        duplicate = db.execute(
            find_duplicate_plan_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "plan_date": plan_date,
                "planning_id": planning_id,
            },
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=400, detail=DUPLICATE_PLAN_MSG)

        if is_update:
            db.execute(
                update_planning_header_query(),
                {
                    "planning_id": planning_id,
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "plan_date": plan_date,
                    "plan_name": plan_name,
                    "remarks": remarks,
                    "updated_by": user_id,
                },
            )
            # Wholesale child replace.
            db.execute(delete_planning_loom_lines_query(), {"planning_id": planning_id})
            db.execute(delete_planning_yarn_lines_query(), {"planning_id": planning_id})
            db.execute(delete_planning_dept_lines_query(), {"planning_id": planning_id})
            plan_no = existing_plan_no
        else:
            result = db.execute(
                insert_planning_header_query(),
                {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "plan_date": plan_date,
                    "plan_name": plan_name,
                    "remarks": remarks,
                    "status_id": PLANNING_STATUS_OPEN,
                    "updated_by": user_id,
                },
            )
            planning_id = int(result.lastrowid)
            db.execute(
                stamp_plan_no_query(), {"planning_id": planning_id, "updated_by": user_id}
            )
            plan_no = "JPP-{0:05d}".format(planning_id)

        loom_q = insert_planning_loom_line_query()
        for params in loom_params:
            params["planning_id"] = planning_id
            db.execute(loom_q, params)

        yarn_q = insert_planning_yarn_line_query()
        for params in yarn_params:
            params["planning_id"] = planning_id
            db.execute(yarn_q, params)

        dept_q = insert_planning_dept_line_query()
        for params in dept_params:
            params["planning_id"] = planning_id
            db.execute(dept_q, params)

        norms_inserted = 0
        if update_norms:
            entries = _norm_entries_from_lines(loom_lines, yarn_lines, dept_lines)
            norms_inserted, _skipped = _upsert_norm_entries(
                db, co_id, branch_id, plan_date, entries, user_id
            )

        db.commit()
        return {
            "data": {
                "planning_id": planning_id,
                "plan_no": plan_no,
                "norms_inserted": norms_inserted,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Delete (soft)
# =============================================================================


@router.post("/planning_delete")
def planning_delete(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft-delete a plan header (active = 0). Body {planning_id, co_id}."""
    body = parse_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")

    raw_co_id = body.get("co_id")
    if raw_co_id is None or raw_co_id == "":
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        co_id = int(raw_co_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid co_id")

    planning_id = _int_opt(body.get("planning_id"), "planning_id")
    if planning_id is None:
        raise HTTPException(status_code=400, detail="planning_id is required")

    user_id = token_data.get("user_id") if isinstance(token_data, dict) else None
    try:
        existing = db.execute(
            get_planning_header_query(), {"planning_id": planning_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=PLAN_NOT_FOUND_MSG)

        db.execute(
            soft_delete_planning_query(),
            {"planning_id": planning_id, "co_id": co_id, "updated_by": user_id},
        )
        db.commit()
        return {"data": {"planning_id": planning_id, "message": "Deleted"}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Norms — grid / history / bulk save / delete
# =============================================================================


@router.get("/norms_grid")
def norms_grid(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Resolved norms for the norms master grid: rows = refs, cols = params.

    Refs by dimension: qid -> cloth qualities (item_type_id=5), yid -> yarn items
    (item_type_id=4), stage -> the 7 PLANNING_STAGES. Each cell carries value +
    source_date + is_exact so the UI can mark inherited vs set-here values (same
    contract as target_map_grid). A param with no resolving row is omitted.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    id_type = _require_enum(request, "id_type", ID_TYPES)
    on_date = _optional_date(request, "on_date") or date.today()
    try:
        params = norm_params_for(id_type)

        refs: List[Dict[str, Any]] = []
        if id_type == PLANNING_ID_TYPE_QLTY:
            for r in db.execute(
                get_planning_cloth_qualities_query(),
                {"co_id": co_id, "item_type_id": PLANNING_CLOTH_ITEM_TYPE_ID},
            ).fetchall():
                m = dict(r._mapping)
                refs.append(
                    {
                        "ref_id": m["item_id"],
                        "ref_code": m.get("item_code"),
                        "ref_name": m.get("item_name"),
                        "ref_group": m.get("grp_name"),
                    }
                )
        elif id_type == PLANNING_ID_TYPE_YARN:
            for r in db.execute(
                get_planning_yarn_items_query(),
                {"co_id": co_id, "item_type_id": PLANNING_YARN_ITEM_TYPE_ID},
            ).fetchall():
                m = dict(r._mapping)
                refs.append(
                    {
                        "ref_id": m["item_id"],
                        "ref_code": m.get("item_code"),
                        "ref_name": m.get("item_name"),
                        "ref_group": m.get("grp_name"),
                    }
                )
        else:  # stage — refs come from the constant, not a table.
            for seq, code, label, machine_type in PLANNING_STAGES:
                refs.append(
                    {
                        "ref_id": seq,
                        "ref_code": code,
                        "ref_name": label,
                        "ref_group": machine_type,
                    }
                )

        resolved = _resolved_norm_map(db, co_id, on_date, id_type)

        rows = []
        for ref in refs:
            cells = {}
            for param in params:
                hit = resolved.get((id_type, int(ref["ref_id"]), param))
                if hit is None:
                    continue
                cells[param] = {
                    "value": hit["value"],
                    "source_date": hit["effective_date"],
                    "is_exact": hit["effective_date"] == str(on_date),
                }
            row = dict(ref)
            row["cells"] = cells
            rows.append(row)

        return {
            "data": {
                "id_type": id_type,
                "on_date": str(on_date),
                "branch_id": branch_id,
                "params": params,
                "rows": rows,
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/norms_list")
def norms_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Raw active norm history rows for a ref (audit / history view), newest first.

    id_type / ref_id / param are all optional filters.
    """
    co_id = _require_co_id(request)
    id_type = request.query_params.get("id_type") or None
    if id_type is not None and id_type not in ID_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid id_type '{id_type}'")
    ref_id = _optional_int_param(request, "ref_id")
    param = request.query_params.get("param") or None
    if param is not None and id_type is not None and param not in norm_params_for(id_type):
        raise HTTPException(
            status_code=400, detail=f"Invalid param '{param}' for id_type '{id_type}'"
        )
    try:
        rows = db.execute(
            get_planning_norms_history_query(),
            {"co_id": co_id, "id_type": id_type, "ref_id": ref_id, "param": param},
        ).fetchall()

        data = []
        for r in rows:
            m = dict(r._mapping)
            data.append(
                {
                    "planning_norm_id": m["planning_norm_id"],
                    "co_id": m.get("co_id"),
                    "branch_id": _i(m.get("branch_id")),
                    "effective_date": _d(m.get("effective_date")),
                    "ref_id": _i(m.get("ref_id")),
                    "id_type": m.get("id_type"),
                    "value_role": m.get("value_role"),
                    "param": m.get("param"),
                    "value": _f(m.get("value")),
                    "active": _i(m.get("active")),
                    "updated_by": _i(m.get("updated_by")),
                    "updated_date_time": _d(m.get("updated_date_time")),
                }
            )
        return {"data": data}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/norms_bulk_save")
def norms_bulk_save(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Insert norm rows at :effective_date in ONE transaction (mirrors target_map_bulk_save).

    Body {co_id, branch_id, effective_date, entries:[{id_type, ref_id, param, value}]}.
    An entry whose value equals the currently-resolved value is SKIPPED (no duplicate
    row); everything else inserts a new effective-dated row. Any invalid entry (bad
    id_type / param for that id_type, non-finite or negative value) 400s and rolls the
    whole batch back.
    """
    body = parse_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")

    raw_co_id = body.get("co_id")
    if raw_co_id is None or raw_co_id == "":
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        co_id = int(raw_co_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid co_id")

    branch_id = _int_opt(body.get("branch_id"), "branch_id")
    effective_date = _body_date(body, "effective_date", required=True)
    raw_entries = body.get("entries")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        raise HTTPException(status_code=400, detail="entries must be a list")

    user_id = token_data.get("user_id") if isinstance(token_data, dict) else None

    entries: List[Dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Each entry must be an object")
        id_type = str(raw.get("id_type") or "")
        param = str(raw.get("param") or "")
        _validate_norm_entry(id_type, param)
        ref_id = _int_opt(raw.get("ref_id"), "ref_id")
        if ref_id is None:
            raise HTTPException(status_code=400, detail="ref_id is required for every entry")
        value = _num(raw.get("value"), f"{id_type}.{param}")
        if value < 0:
            raise HTTPException(
                status_code=400, detail=f"Negative value not allowed for param '{param}'"
            )
        entries.append({"id_type": id_type, "ref_id": ref_id, "param": param, "value": value})

    try:
        inserted, skipped = _upsert_norm_entries(
            db, co_id, branch_id, effective_date, entries, user_id
        )
        db.commit()
        return {"data": {"inserted": inserted, "skipped": skipped}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/norms_delete")
def norms_delete(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft-delete one norm row (active = 0). Body {planning_norm_id, co_id}."""
    body = parse_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")

    raw_co_id = body.get("co_id")
    if raw_co_id is None or raw_co_id == "":
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        co_id = int(raw_co_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid co_id")

    planning_norm_id = _int_opt(body.get("planning_norm_id"), "planning_norm_id")
    if planning_norm_id is None:
        raise HTTPException(status_code=400, detail="planning_norm_id is required")

    user_id = token_data.get("user_id") if isinstance(token_data, dict) else None
    try:
        result = db.execute(
            soft_delete_planning_norm_query(),
            {"planning_norm_id": planning_norm_id, "co_id": co_id, "updated_by": user_id},
        )
        if getattr(result, "rowcount", 1) == 0:
            raise HTTPException(status_code=404, detail="Planning norm row not found")
        db.commit()
        return {"data": {"planning_norm_id": planning_norm_id, "message": "Deleted"}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
