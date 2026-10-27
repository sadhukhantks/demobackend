"""Loom Modification register — GET/POST/PUT/DELETE under /api/loomModification.

Spec: docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md (§4-§6, §8).
Phase 1: Loom Groups master + Loom Modification event register (loom-only, D1).

Persona: Portal — ``get_tenant_db`` + ``get_current_user_with_refresh``, every response
``{"data": ...}`` (the register list adds total / page / page_size), Pydantic bodies,
400 with a plain message for every validation, soft delete via ``active = 0``, no
approval workflow (none exists in jute production), trigger-based audit (no
created_*). Queries live in loom_modification_query.py (arg-less text() builders).
Enum labels come from src/juteProduction/constants.py (MOD_KINDS, MOD_CATEGORIES,
MOD_DONE_BY, LOOM_GROUP_PURPOSES, MOD_DEFAULT_SETTLE_DAYS). The register NEVER writes
jute_prod_weaving_target_map (D6): a speed change is only recorded as
mod_category = SPEED_CHANGE.

Endpoints (every write commits once in the endpoint and rolls back on error):
  GET    /setup                                 looms, lines, groups, enums, parties, open_events, all_stages
  GET    /loom_group_list                       groups with member_count
  POST   /loom_group_create                     {loom_group_id}
  PUT    /loom_group_edit/{id}?co_id=           COALESCE patch + optional machine_ids replace
  GET    /loom_group_detail/{id}?co_id=         header + members + event_count
  DELETE /loom_group_delete/{id}?co_id=         400 while an active event references it
  GET    /modification_table                    paginated register, stage k of n, loom_count, data_check
  GET    /modification_by_id/{id}?co_id=        header + members + parent + children
  GET    /modification_chain/{root_id}?co_id=   every event of the chain with snapshots
  GET    /machine_timeline?co_id&machine_id     all snapshot rows of one loom in date order
  POST   /modification_create                   {modification_id, root_modification_id, data_check, message}
  PUT    /modification_edit/{id}                same body (co_id in body), snapshot replaced
  POST   /modification_add_members/{id}         append snapshot rows ("Add looms to this stage";
                                                same REVERSAL-subset and reverted-loom rules)
  POST   /modification_insert_earlier/{root_id} new INITIAL before an existing root, re-parents it
  DELETE /modification_delete/{id}?co_id=&chain=0   400 while children exist unless chain=1 on a root
(the optional /loom_group_suggest of the spec is NOT built — owner decision D2 (a).)

EXECUTE ORDER OF _save_modification (tests feed execute.side_effect in exactly this
order; every step marked [cond] runs only under its condition):

  edit only:
    E1  get_modification_by_id_query          existing header      (404 when missing)
    E2  count_modification_children_query     cnt, min_mod_date    (parent / kind immutable
                                                                     while cnt > 0; new end
                                                                     must precede min_mod_date)
    E3  get_snapshot_machine_ids_query        old snapshot ids     (newly-ticked diff)
    E4  [cnt > 0] get_child_reversal_snapshot_query   looms carried by child REVERSALs
                                                      must stay in the member list
  chain rules, kind != INITIAL:
    C1  get_modification_by_id_query          parent (400 unknown / inactive / other co / REVERSAL)
    C2  [FOLLOWUP, RECTIFY, and NOT an edit of a stage with children]
                            get_chain_latest_stage_query   must return the parent
                            (a middle stage's parent is frozen by E2, so its place in
                            the chain was proven at creation — free edit, D9)
        [REVERSAL]          get_snapshot_machine_ids_query  parent snapshot (subset rule)
    C3  [FOLLOWUP, RECTIFY] get_chain_reverted_looms_query   none of the members may be
                            carried by a REVERSAL of the chain dated before mod_date
                            (a REVERSAL closes the timeline of its looms, §5)
  P1  [done_by = VENDOR] get_party_active_query           (400 unknown party)
  G1  [loom_group_id]    get_loom_group_row_query          (400 unknown group)
  L1  get_loom_context_query                 is-a-loom, active (newly ticked), one branch,
                                             branch of co_id; branch_id derived here
  O1  get_loom_spans_query                   per-loom overlap across ALL chains (Python check)
  W1  insert_modification_query (create, lastrowid) | update_modification_query (edit)
  W2  [create and INITIAL] stamp_root_modification_query   root = own id
  W3  [edit] soft_delete_snapshot_query
  W4  insert_snapshot_member_query x N       one per member, body order
  D1  get_data_check_query                   before_days / after_days

Pure (no DB) validations run BEFORE any execute and never commit: enums, blank title,
settle_days >= 0, mod_to_date >= mod_date, dates <= today, INITIAL <-> parent
presence, VENDOR needs party_id, >= 1 distinct member, per-loom done_date /
done_to_date inside the event's dates.

modification_create with ``save_as_group`` runs the group create first (its own
execute order, see _create_loom_group) so the new loom_group_id rides in the header
insert. modification_insert_earlier runs: old root fetch -> _save_modification (create,
forced INITIAL, branch pinned to the old root's branch) -> rewrite_chain_root_query ->
reparent_old_root_query.
"""

from datetime import date
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    LOOM_GROUP_PURPOSES,
    MOD_CATEGORIES,
    MOD_CATEGORY_LABELS,
    MOD_DEFAULT_SETTLE_DAYS,
    MOD_DONE_BY,
    MOD_DONE_BY_LABELS,
    MOD_KIND_LABELS,
    MOD_KINDS,
    WEAVING_MACHINE_TYPE_NAME,
)
from src.juteProduction.loom_modification_query import (
    check_loom_group_duplicate_query,
    count_loom_group_events_query,
    count_modification_children_query,
    get_chain_latest_stage_query,
    get_chain_reverted_looms_query,
    get_chain_snapshot_members_query,
    get_child_reversal_snapshot_query,
    get_data_check_for_modifications_query,
    get_data_check_query,
    get_loom_context_query,
    get_loom_group_list_query,
    get_loom_group_members_query,
    get_loom_group_row_query,
    get_loom_spans_query,
    get_looms_query,
    get_machine_timeline_query,
    get_modification_by_id_query,
    get_modification_chain_query,
    get_modification_children_query,
    get_modification_table_count_query,
    get_modification_table_query,
    get_open_stages_query,
    get_parties_query,
    get_party_active_query,
    get_snapshot_machine_ids_query,
    get_snapshot_members_query,
    insert_loom_group_member_query,
    insert_loom_group_query,
    insert_modification_query,
    insert_snapshot_member_query,
    reparent_old_root_query,
    rewrite_chain_root_query,
    soft_delete_chain_query,
    soft_delete_chain_snapshots_query,
    soft_delete_loom_group_members_query,
    soft_delete_loom_group_query,
    soft_delete_modification_query,
    soft_delete_snapshot_query,
    stamp_root_modification_query,
    update_loom_group_query,
    update_modification_query,
)


router = APIRouter()

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

KIND_INITIAL = "INITIAL"
KIND_REVERSAL = "REVERSAL"
DONE_BY_VENDOR = "VENDOR"

# ----------------------------------------------------------------------------- messages
GROUP_NOT_FOUND_MSG = "Loom group not found"
GROUP_DUP_MSG = "A loom group with this code already exists"
GROUP_CODE_BLANK_MSG = "group_code and group_name are required"
GROUP_NO_MEMBERS_MSG = "at least one loom is required"
GROUP_BAD_PURPOSE_MSG = "group_purpose must be one of: " + ", ".join(LOOM_GROUP_PURPOSES)
GROUP_IN_USE_MSG = "this group is used by {n} modification event(s) — remove them first"
UNKNOWN_GROUP_MSG = "Unknown or inactive loom_group_id"

NOT_FOUND_MSG = "Modification event not found"
BAD_KIND_MSG = "mod_kind must be one of: " + ", ".join(MOD_KINDS)
BAD_CATEGORY_MSG = "mod_category must be one of: " + ", ".join(MOD_CATEGORIES)
BAD_DONE_BY_MSG = "done_by must be one of: " + ", ".join(MOD_DONE_BY)
TITLE_BLANK_MSG = "title is required"
SETTLE_MSG = "settle_days cannot be negative"
NO_MEMBERS_MSG = "tick at least one loom"
DUP_MEMBER_MSG = "loom {id} is listed twice"
DATE_ORDER_MSG = "'Completed on' cannot be earlier than 'Date done'"
FUTURE_DATE_MSG = "date done cannot be in the future"
INITIAL_HAS_PARENT_MSG = "an Initial stage cannot follow another stage — choose Follow-up, Rectification or Reversal"
PARENT_REQUIRED_MSG = "'Follows' is required for a Follow-up, Rectification or Reversal"
PARENT_NOT_FOUND_MSG = "the stage this one follows was not found or is inactive"
PARENT_IS_REVERSAL_MSG = "a stage cannot follow a Reversal — follow the stage that was undone"
NOT_LATEST_MSG = "the latest stage of this chain is {title}, {mod_date} — follow that one"
SAME_DAY_MSG = "date done must be after the previous stage ended ({end}) — no same-day or overlapping stages"
REVERSAL_SUBSET_MSG = "loom {name} is not in the stage being reversed — a reversal may only untick looms"
DONE_DATE_RANGE_MSG = "done date of loom {name} is outside the event's dates — extend 'Completed on'"
DONE_TO_ORDER_MSG = "done-to date of loom {name} cannot be earlier than its done date"
NOT_A_LOOM_MSG = "machine {id} is not a loom"
INACTIVE_LOOM_MSG = "loom {name} is inactive"
MIXED_BRANCH_MSG = "all looms must be in one branch"
BRANCH_MISMATCH_MSG = "the looms belong to another branch than the one selected"
BRANCH_NOT_OF_CO_MSG = "the looms' branch does not belong to this company"
OVERLAP_MSG = "loom {name} already has '{title}' on {d} to {e} — the dates overlap"
VENDOR_PARTY_MSG = "party_id is required when done_by is VENDOR"
UNKNOWN_PARTY_MSG = "Unknown or inactive party_id"
IMMUTABLE_MSG = "this stage has follow-ups — delete the follow-ups first"
REVERTED_LOOM_MSG = "loom {name} was reverted on {d} ('{title}') — it accepts no later stage in this chain"
UNTICK_REVERSED_MSG = "loom {name} is carried by the reversal '{title}' — it cannot be unticked from this stage"
GROUP_BRANCH_MSG = "a group's branch follows its looms — replace the members to move it to another branch"
INSERT_EARLIER_BRANCH_MSG = "the earlier stage must be in the same branch as the chain ({branch})"
CHILD_STARTS_MSG = "the next stage starts on {d} — this stage must end before it"
HAS_CHILDREN_MSG = "has {n} follow-up(s) — delete the follow-ups first"
CHAIN_ON_ROOT_ONLY_MSG = "Delete chain is available on the first stage of a chain only"
INSERT_ROOT_ONLY_MSG = "Insert earlier stage is available on the first stage of a chain only"
INSERT_EARLIER_END_MSG = "the earlier stage must end before {d}"
BAD_OLD_ROOT_KIND_MSG = "old_root_kind must be FOLLOWUP or RECTIFY"
ALREADY_MEMBER_MSG = "loom {name} is already in this stage"


# =============================================================================
# Pydantic bodies
# =============================================================================


class LoomGroupCreate(BaseModel):
    co_id: int
    branch_id: Optional[int] = None
    group_code: str
    group_name: str
    group_purpose: str = "STUDY"
    remarks: Optional[str] = None
    machine_ids: List[int] = Field(default_factory=list)


class LoomGroupUpdate(BaseModel):
    """COALESCE patch; ``machine_ids`` given = the member set is replaced."""

    branch_id: Optional[int] = None
    group_code: Optional[str] = None
    group_name: Optional[str] = None
    group_purpose: Optional[str] = None
    remarks: Optional[str] = None
    machine_ids: Optional[List[int]] = None


class ModificationMember(BaseModel):
    """One snapshot row: a loom plus its optional own dates (§4.4)."""

    machine_id: int
    done_date: Optional[date] = None
    done_to_date: Optional[date] = None
    remarks: Optional[str] = None


class SaveAsGroup(BaseModel):
    group_code: str
    group_name: str
    group_purpose: str = "STUDY"


class ModificationCreate(BaseModel):
    co_id: int
    branch_id: Optional[int] = None
    loom_group_id: Optional[int] = None
    members: List[ModificationMember] = Field(default_factory=list)
    mod_date: date
    mod_to_date: Optional[date] = None
    settle_days: int = MOD_DEFAULT_SETTLE_DAYS
    mod_kind: str = KIND_INITIAL
    parent_modification_id: Optional[int] = None
    mod_category: str
    title: str
    description: Optional[str] = None
    expected_effect: Optional[str] = None
    done_by: str = "INHOUSE"
    party_id: Optional[int] = None
    total_cost: Optional[float] = None
    remarks: Optional[str] = None
    save_as_group: Optional[SaveAsGroup] = None


class ModificationUpdate(ModificationCreate):
    """Edit is a full replace (co_id in the body, overhaul contract); snapshot replaced."""


class AddMembersBody(BaseModel):
    co_id: int
    members: List[ModificationMember] = Field(default_factory=list)


class InsertEarlierBody(ModificationCreate):
    """Create body of the new INITIAL + the kind the old root takes (FOLLOWUP / RECTIFY)."""

    old_root_kind: str = "FOLLOWUP"


# =============================================================================
# Helpers — params, casts, labels
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
    return _optional_int(request, "branch_id")


def _optional_int(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_date(request: Request, name: str) -> Optional[date]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format")


def _optional_str(request: Request, name: str) -> Optional[str]:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _pagination(request: Request):
    """page / limit -> (page, limit, offset), limit capped at MAX_PAGE_LIMIT (200)."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
        limit = max(
            1,
            min(MAX_PAGE_LIMIT, int(request.query_params.get("limit", str(DEFAULT_PAGE_LIMIT)))),
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pagination parameter")
    return page, limit, (page - 1) * limit


def _fo(v) -> Optional[float]:
    return None if v is None else float(v)


def _io(v) -> Optional[int]:
    return None if v is None else int(v)


_DATE_KEYS = ("mod_date", "mod_to_date", "done_date", "done_to_date", "d_m", "e_m", "min_mod_date")


def _serialize(row: dict) -> dict:
    """Dates -> ISO strings, Decimals -> float, enum labels added; in place."""
    for k in _DATE_KEYS:
        if row.get(k) is not None:
            row[k] = str(row[k])
    if "total_cost" in row:
        row["total_cost"] = _fo(row.get("total_cost"))
    for key, labels in (
        ("mod_kind", MOD_KIND_LABELS),
        ("mod_category", MOD_CATEGORY_LABELS),
        ("done_by", MOD_DONE_BY_LABELS),
    ):
        if row.get(key) is not None:
            row[key + "_label"] = labels.get(row[key], row[key])
    for key in ("loom_count", "children_count", "stage_count", "stage_no", "member_count"):
        if key in row and row[key] is not None:
            row[key] = int(row[key])
    if row.get("stage_no") is not None and row.get("stage_count") is not None:
        row["stage_label"] = f"{row['stage_no']} of {row['stage_count']}"
    return row


def _loom_label(row: dict) -> str:
    code = row.get("mech_code")
    name = row.get("machine_name")
    return f"{code} — {name}" if code and name and code != name else (name or code or "?")


def _codes(codes, labels) -> List[dict]:
    return [{"code": c, "label": labels.get(c, c)} for c in codes]


# =============================================================================
# Helpers — loom context (is-a-loom, active, one branch, branch of co)
# =============================================================================


def _loom_context(
    db: Session,
    co_id: int,
    machine_ids: List[int],
    body_branch_id: Optional[int],
    existing_ids=(),
) -> Dict[int, dict]:
    """One execute: validate every machine id and return {machine_id: row}.

    Rules (§4, §5): every id is a loom (400 "machine X is not a loom"); a loom must
    be active only when NEWLY ticked (not in ``existing_ids``); all looms share one
    branch; that branch belongs to ``co_id``; a body branch, when given, must match.
    """
    rows = db.execute(
        get_loom_context_query(),
        {"machine_ids": [int(m) for m in machine_ids], "loom_type": WEAVING_MACHINE_TYPE_NAME},
    ).fetchall()
    found = {int(r._mapping["machine_id"]): dict(r._mapping) for r in rows}
    existing = {int(x) for x in existing_ids}

    branches = set()
    for mid in machine_ids:
        row = found.get(int(mid))
        if not row or not int(row.get("is_loom") or 0):
            raise HTTPException(status_code=400, detail=NOT_A_LOOM_MSG.format(id=mid))
        if int(mid) not in existing and not int(row.get("active") or 0):
            raise HTTPException(
                status_code=400, detail=INACTIVE_LOOM_MSG.format(name=_loom_label(row))
            )
        branches.add(row.get("branch_id"))

    if len(branches) != 1:
        raise HTTPException(status_code=400, detail=MIXED_BRANCH_MSG)
    branch_id = branches.pop()
    first = found[int(machine_ids[0])]
    if branch_id is None or first.get("branch_co_id") is None or int(first["branch_co_id"]) != int(co_id):
        raise HTTPException(status_code=400, detail=BRANCH_NOT_OF_CO_MSG)
    if body_branch_id is not None and int(body_branch_id) != int(branch_id):
        raise HTTPException(status_code=400, detail=BRANCH_MISMATCH_MSG)
    return found


def _distinct_ids(ids: List[int], dup_msg: str) -> List[int]:
    seen = set()
    out = []
    for i in ids:
        i = int(i)
        if i in seen:
            raise HTTPException(status_code=400, detail=dup_msg.format(id=i))
        seen.add(i)
        out.append(i)
    return out


# =============================================================================
# Loom groups — shared create
# =============================================================================


def _validate_group_header(group_code, group_name, group_purpose) -> str:
    if not (group_code or "").strip() or not (group_name or "").strip():
        raise HTTPException(status_code=400, detail=GROUP_CODE_BLANK_MSG)
    purpose = (group_purpose or "STUDY").strip().upper()
    if purpose not in LOOM_GROUP_PURPOSES:
        raise HTTPException(status_code=400, detail=GROUP_BAD_PURPOSE_MSG)
    return purpose


def _create_loom_group(
    db: Session,
    co_id: int,
    branch_id: Optional[int],
    group_code: str,
    group_name: str,
    group_purpose: str,
    remarks: Optional[str],
    machine_ids: List[int],
    user_id,
) -> int:
    """Execute order: dup check -> loom context -> insert header -> insert member x N."""
    purpose = _validate_group_header(group_code, group_name, group_purpose)
    ids = _distinct_ids(machine_ids or [], DUP_MEMBER_MSG)
    if not ids:
        raise HTTPException(status_code=400, detail=GROUP_NO_MEMBERS_MSG)

    dup = db.execute(
        check_loom_group_duplicate_query(),
        {"co_id": int(co_id), "group_code": group_code.strip(), "exclude_id": None},
    ).fetchone()
    if dup:
        raise HTTPException(status_code=400, detail=GROUP_DUP_MSG)

    looms = _loom_context(db, int(co_id), ids, branch_id)
    derived_branch = looms[ids[0]]["branch_id"]

    result = db.execute(
        insert_loom_group_query(),
        {
            "co_id": int(co_id),
            "branch_id": int(derived_branch),
            "group_code": group_code.strip(),
            "group_name": group_name.strip(),
            "group_purpose": purpose,
            "remarks": remarks,
            "updated_by": user_id,
        },
    )
    loom_group_id = int(result.lastrowid)
    for mid in ids:
        db.execute(
            insert_loom_group_member_query(), {"loom_group_id": loom_group_id, "machine_id": mid}
        )
    return loom_group_id


# =============================================================================
# Endpoints — setup
# =============================================================================


@router.get("/setup")
def setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Dropdown payload for both pages: looms of the branch (with line_no), distinct
    lines, groups, the fixed enums as {code,label}, parties, and the "Follows" feeds:
    ``open_events`` (latest non-REVERSAL stage per chain with an un-reverted loom) and
    ``all_stages`` (every non-REVERSAL stage — a REVERSAL may name any of them).

    Execute order: looms, groups, parties, open stages.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        looms = [
            dict(r._mapping)
            for r in db.execute(
                get_looms_query(),
                {"co_id": co_id, "branch_id": branch_id, "loom_type": WEAVING_MACHINE_TYPE_NAME},
            ).fetchall()
        ]
        lines = sorted(
            {str(l["line_no"]) for l in looms if l.get("line_no") not in (None, "")},
            key=lambda s: (len(s), s),
        )
        groups = [
            _serialize(dict(r._mapping))
            for r in db.execute(
                get_loom_group_list_query(),
                {"co_id": co_id, "branch_id": branch_id, "group_purpose": None, "search": None},
            ).fetchall()
        ]
        parties = [
            dict(r._mapping)
            for r in db.execute(get_parties_query(), {"co_id": co_id}).fetchall()
        ]
        stages = [
            _serialize(dict(r._mapping))
            for r in db.execute(
                get_open_stages_query(), {"co_id": co_id, "branch_id": branch_id}
            ).fetchall()
        ]
        latest_by_root: Dict[int, dict] = {}
        for s in stages:  # ordered by root, mod_date, id -> the last seen wins
            latest_by_root[s.get("root_modification_id")] = s
        open_events = [
            s for s in latest_by_root.values() if int(s.get("unreverted_count") or 0) > 0
        ]
        for s in stages:
            s["snapshot_count"] = int(s.get("snapshot_count") or 0)
            s["unreverted_count"] = int(s.get("unreverted_count") or 0)
            s["label"] = f"{s.get('title')} — {s.get('mod_date')}" + (
                f" ({s['group_code']})" if s.get("group_code") else ""
            )

        return {
            "data": {
                "looms": looms,
                "lines": lines,
                "groups": groups,
                "mod_kinds": _codes(MOD_KINDS, MOD_KIND_LABELS),
                "mod_categories": _codes(MOD_CATEGORIES, MOD_CATEGORY_LABELS),
                "done_by_options": _codes(MOD_DONE_BY, MOD_DONE_BY_LABELS),
                "group_purposes": _codes(
                    LOOM_GROUP_PURPOSES, {"STUDY": "Study", "CONTROL": "Control"}
                ),
                "parties": parties,
                "open_events": open_events,
                "all_stages": stages,
                "default_settle_days": MOD_DEFAULT_SETTLE_DAYS,
                "today": str(date.today()),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — loom groups
# =============================================================================


@router.get("/loom_group_list")
def loom_group_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    purpose = _optional_str(request, "group_purpose")
    search = _optional_str(request, "search")
    try:
        rows = db.execute(
            get_loom_group_list_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "group_purpose": purpose.upper() if purpose else None,
                "search": f"%{search}%" if search else None,
            },
        ).fetchall()
        return {"data": [_serialize(dict(r._mapping)) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/loom_group_create")
def loom_group_create(
    body: LoomGroupCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        loom_group_id = _create_loom_group(
            db,
            body.co_id,
            body.branch_id,
            body.group_code,
            body.group_name,
            body.group_purpose,
            body.remarks,
            body.machine_ids,
            token_data.get("user_id"),
        )
        db.commit()
        return {"data": {"loom_group_id": loom_group_id, "message": "Loom group created"}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/loom_group_edit/{loom_group_id}")
def loom_group_edit(
    loom_group_id: int,
    body: LoomGroupUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Execute order: existing -> [dup check when group_code] -> [old members, loom
    context when machine_ids] -> update header -> [soft-delete members, insert x N].
    branch_id is derived from the members; a body.branch_id without machine_ids that
    differs from the current branch is a 400 (never written)."""
    co_id = _require_co_id(request)
    user_id = token_data.get("user_id")
    try:
        existing = db.execute(
            get_loom_group_row_query(), {"loom_group_id": loom_group_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=GROUP_NOT_FOUND_MSG)
        old = dict(existing._mapping)

        purpose = None
        if body.group_purpose is not None:
            purpose = (body.group_purpose or "").strip().upper()
            if purpose not in LOOM_GROUP_PURPOSES:
                raise HTTPException(status_code=400, detail=GROUP_BAD_PURPOSE_MSG)
        code = body.group_code.strip() if body.group_code is not None else None
        name = body.group_name.strip() if body.group_name is not None else None
        if code == "" or name == "":
            raise HTTPException(status_code=400, detail=GROUP_CODE_BLANK_MSG)
        ids = None
        if body.machine_ids is not None:
            ids = _distinct_ids(body.machine_ids, DUP_MEMBER_MSG)
            if not ids:
                raise HTTPException(status_code=400, detail=GROUP_NO_MEMBERS_MSG)

        if code is not None:
            dup = db.execute(
                check_loom_group_duplicate_query(),
                {"co_id": co_id, "group_code": code, "exclude_id": loom_group_id},
            ).fetchone()
            if dup:
                raise HTTPException(status_code=400, detail=GROUP_DUP_MSG)

        # The header branch is derived from the members (§4.1): without a member
        # replace a differing body.branch_id is refused, never written.
        branch_id = None
        if ids is None and body.branch_id is not None and old.get("branch_id") is not None:
            if int(body.branch_id) != int(old["branch_id"]):
                raise HTTPException(status_code=400, detail=GROUP_BRANCH_MSG)
        if ids is not None:
            old_ids = [
                int(r._mapping["machine_id"])
                for r in db.execute(
                    get_loom_group_members_query(), {"loom_group_id": loom_group_id}
                ).fetchall()
            ]
            looms = _loom_context(db, co_id, ids, body.branch_id, existing_ids=old_ids)
            branch_id = looms[ids[0]]["branch_id"]

        db.execute(
            update_loom_group_query(),
            {
                "loom_group_id": loom_group_id,
                "branch_id": _io(branch_id),
                "group_code": code,
                "group_name": name,
                "group_purpose": purpose,
                "remarks": body.remarks,
                "updated_by": user_id,
            },
        )
        if ids is not None:
            db.execute(soft_delete_loom_group_members_query(), {"loom_group_id": loom_group_id})
            for mid in ids:
                db.execute(
                    insert_loom_group_member_query(),
                    {"loom_group_id": loom_group_id, "machine_id": mid},
                )
        db.commit()
        return {"data": {"message": "Loom group updated", "loom_group_id": old["loom_group_id"]}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/loom_group_detail/{loom_group_id}")
def loom_group_detail(
    loom_group_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Header + members (with machine_active for the "inactive" label) + event_count."""
    co_id = _require_co_id(request)
    try:
        row = db.execute(
            get_loom_group_row_query(), {"loom_group_id": loom_group_id, "co_id": co_id}
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=GROUP_NOT_FOUND_MSG)
        header = _serialize(dict(row._mapping))
        members = [
            dict(r._mapping)
            for r in db.execute(
                get_loom_group_members_query(), {"loom_group_id": loom_group_id}
            ).fetchall()
        ]
        for m in members:
            m["machine_active"] = int(m.get("machine_active") or 0)
            m["label"] = _loom_label(m)
        cnt = db.execute(
            count_loom_group_events_query(), {"loom_group_id": loom_group_id}
        ).fetchone()
        header["members"] = members
        header["member_count"] = len(members)
        header["event_count"] = int(cnt._mapping["cnt"]) if cnt else 0
        return {"data": header}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/loom_group_delete/{loom_group_id}")
def loom_group_delete(
    loom_group_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete, refused while an active modification event references the group."""
    co_id = _require_co_id(request)
    user_id = token_data.get("user_id")
    try:
        row = db.execute(
            get_loom_group_row_query(), {"loom_group_id": loom_group_id, "co_id": co_id}
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=GROUP_NOT_FOUND_MSG)
        cnt = db.execute(
            count_loom_group_events_query(), {"loom_group_id": loom_group_id}
        ).fetchone()
        n = int(cnt._mapping["cnt"]) if cnt else 0
        if n > 0:
            raise HTTPException(status_code=400, detail=GROUP_IN_USE_MSG.format(n=n))
        db.execute(
            soft_delete_loom_group_query(), {"loom_group_id": loom_group_id, "updated_by": user_id}
        )
        db.execute(soft_delete_loom_group_members_query(), {"loom_group_id": loom_group_id})
        db.commit()
        return {"data": {"message": "Loom group deleted"}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Modification events — pure validation + shared save
# =============================================================================


def _event_end(mod_date, mod_to_date) -> date:
    return mod_to_date or mod_date


def _member_span(m: ModificationMember, mod_date: date, mod_to_date: Optional[date]):
    """(D_m, E_m) per §5."""
    d = m.done_date or mod_date
    e = m.done_to_date or m.done_date or mod_to_date or mod_date
    return d, e


def _validate_body(body: ModificationCreate, today: Optional[date] = None) -> dict:
    """Pure checks (no DB). Returns the normalised enum values."""
    today = today or date.today()
    kind = (body.mod_kind or "").strip().upper()
    if kind not in MOD_KINDS:
        raise HTTPException(status_code=400, detail=BAD_KIND_MSG)
    category = (body.mod_category or "").strip().upper()
    if category not in MOD_CATEGORIES:
        raise HTTPException(status_code=400, detail=BAD_CATEGORY_MSG)
    done_by = (body.done_by or "").strip().upper()
    if done_by not in MOD_DONE_BY:
        raise HTTPException(status_code=400, detail=BAD_DONE_BY_MSG)
    if not (body.title or "").strip():
        raise HTTPException(status_code=400, detail=TITLE_BLANK_MSG)
    if int(body.settle_days) < 0:
        raise HTTPException(status_code=400, detail=SETTLE_MSG)
    if body.mod_to_date is not None and body.mod_to_date < body.mod_date:
        raise HTTPException(status_code=400, detail=DATE_ORDER_MSG)
    if body.mod_date > today:
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)
    if kind == KIND_INITIAL and body.parent_modification_id is not None:
        raise HTTPException(status_code=400, detail=INITIAL_HAS_PARENT_MSG)
    if kind != KIND_INITIAL and body.parent_modification_id is None:
        raise HTTPException(status_code=400, detail=PARENT_REQUIRED_MSG)
    if done_by == DONE_BY_VENDOR and body.party_id is None:
        raise HTTPException(status_code=400, detail=VENDOR_PARTY_MSG)
    if not body.members:
        raise HTTPException(status_code=400, detail=NO_MEMBERS_MSG)
    _distinct_ids([m.machine_id for m in body.members], DUP_MEMBER_MSG)
    _validate_member_dates(body.members, body.mod_date, body.mod_to_date, today)
    return {"mod_kind": kind, "mod_category": category, "done_by": done_by}


def _validate_member_dates(members, mod_date: date, mod_to_date: Optional[date], today: date):
    end = _event_end(mod_date, mod_to_date)
    for m in members:
        name = str(m.machine_id)
        if m.done_date is not None:
            if m.done_date > today:
                raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)
            if not (mod_date <= m.done_date <= end):
                raise HTTPException(status_code=400, detail=DONE_DATE_RANGE_MSG.format(name=name))
        if m.done_to_date is not None:
            start = m.done_date or mod_date
            if m.done_to_date < start:
                raise HTTPException(status_code=400, detail=DONE_TO_ORDER_MSG.format(name=name))
            if m.done_to_date > end:
                raise HTTPException(status_code=400, detail=DONE_DATE_RANGE_MSG.format(name=name))


def _fetch_event(db: Session, modification_id: int, co_id: int) -> Optional[dict]:
    row = db.execute(
        get_modification_by_id_query(), {"modification_id": int(modification_id), "co_id": int(co_id)}
    ).fetchone()
    return dict(row._mapping) if row else None


def _snapshot_ids(db: Session, modification_id: int) -> List[int]:
    return [
        int(r._mapping["machine_id"])
        for r in db.execute(
            get_snapshot_machine_ids_query(), {"modification_id": int(modification_id)}
        ).fetchall()
    ]


def _check_reverted(db: Session, root_id: int, machine_ids: List[int], mod_date) -> None:
    """C3 (§5): a REVERSAL closes the chain timeline for the looms in its snapshot —
    none of ``machine_ids`` may be carried by a REVERSAL of the chain dated before
    ``mod_date``."""
    rows = db.execute(
        get_chain_reverted_looms_query(),
        {
            "root_modification_id": int(root_id),
            "mod_date": mod_date,
            "machine_ids": [int(m) for m in machine_ids],
        },
    ).fetchall()
    if rows:
        rr = rows[0]._mapping
        raise HTTPException(
            status_code=400,
            detail=REVERTED_LOOM_MSG.format(
                name=_loom_label(rr), d=rr.get("reverted_on"), title=rr.get("title")
            ),
        )


def _check_overlaps(
    db: Session, co_id: int, members, mod_date, mod_to_date, looms: Dict[int, dict], exclude_id
):
    """O1: every member's [D_m, E_m] against the same loom's spans on other events."""
    rows = db.execute(
        get_loom_spans_query(),
        {
            "co_id": int(co_id),
            "machine_ids": [int(m.machine_id) for m in members],
            "exclude_id": _io(exclude_id),
        },
    ).fetchall()
    spans: Dict[int, list] = {}
    for r in rows:
        spans.setdefault(int(r._mapping["machine_id"]), []).append(dict(r._mapping))
    for m in members:
        d, e = _member_span(m, mod_date, mod_to_date)
        for s in spans.get(int(m.machine_id), []):
            if d <= s["e_m"] and s["d_m"] <= e:
                raise HTTPException(
                    status_code=400,
                    detail=OVERLAP_MSG.format(
                        name=_loom_label(looms.get(int(m.machine_id), s)),
                        title=s.get("title"),
                        d=s["d_m"],
                        e=s["e_m"],
                    ),
                )


def _data_check(db: Session, co_id: int, machine_ids: List[int], mod_date) -> dict:
    row = db.execute(
        get_data_check_query(),
        {"co_id": int(co_id), "machine_ids": [int(m) for m in machine_ids], "mod_date": mod_date},
    ).fetchone()
    before = int(row._mapping.get("before_days") or 0) if row else 0
    after = int(row._mapping.get("after_days") or 0) if row else 0
    return {"before_days": before, "after_days": after}


def _save_modification(
    db: Session,
    body: ModificationCreate,
    user_id,
    modification_id: Optional[int] = None,
) -> dict:
    """Shared create / edit body (execute order in the module docstring).

    ``modification_id`` None inserts; otherwise the header is replaced and the snapshot
    soft-deleted and reinserted. Returns {modification_id, root_modification_id,
    branch_id, data_check}. The caller owns the transaction.
    """
    norm = _validate_body(body)
    kind, category, done_by = norm["mod_kind"], norm["mod_category"], norm["done_by"]
    co_id = int(body.co_id)
    member_ids = [int(m.machine_id) for m in body.members]
    end = _event_end(body.mod_date, body.mod_to_date)

    existing = None
    old_ids: List[int] = []
    n_kids = 0
    if modification_id is not None:
        existing = _fetch_event(db, modification_id, co_id)  # E1
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        kids = db.execute(  # E2
            count_modification_children_query(), {"modification_id": int(modification_id)}
        ).fetchone()
        n_kids = int(kids._mapping["cnt"]) if kids else 0
        if n_kids > 0:
            if (
                _io(body.parent_modification_id) != _io(existing.get("parent_modification_id"))
                or kind != existing.get("mod_kind")
            ):
                raise HTTPException(status_code=400, detail=IMMUTABLE_MSG)
            min_child = kids._mapping.get("min_mod_date")
            if min_child is not None and end >= min_child:
                raise HTTPException(status_code=400, detail=CHILD_STARTS_MSG.format(d=min_child))
        old_ids = _snapshot_ids(db, modification_id)  # E3
        if n_kids > 0:  # E4: a child REVERSAL's snapshot must stay a subset of ours
            keep = set(member_ids)
            for r in db.execute(
                get_child_reversal_snapshot_query(), {"modification_id": int(modification_id)}
            ).fetchall():
                rr = r._mapping
                if int(rr["machine_id"]) not in keep:
                    raise HTTPException(
                        status_code=400,
                        detail=UNTICK_REVERSED_MSG.format(name=_loom_label(rr), title=rr.get("title")),
                    )

    # ---- chain rules (C1, C2)
    root_id = None
    if kind != KIND_INITIAL:
        parent = _fetch_event(db, int(body.parent_modification_id), co_id)  # C1
        if not parent or int(parent.get("co_id")) != co_id:
            raise HTTPException(status_code=400, detail=PARENT_NOT_FOUND_MSG)
        if parent.get("mod_kind") == KIND_REVERSAL:
            raise HTTPException(status_code=400, detail=PARENT_IS_REVERSAL_MSG)
        if modification_id is not None and int(parent["modification_id"]) == int(modification_id):
            raise HTTPException(status_code=400, detail=PARENT_NOT_FOUND_MSG)
        root_id = int(parent.get("root_modification_id") or parent["modification_id"])
        if kind == KIND_REVERSAL:
            parent_ids = set(_snapshot_ids(db, int(parent["modification_id"])))  # C2
            for m in body.members:
                if int(m.machine_id) not in parent_ids:
                    raise HTTPException(
                        status_code=400, detail=REVERSAL_SUBSET_MSG.format(name=m.machine_id)
                    )
        elif n_kids == 0:
            # C2 — only for a new or terminal stage: a middle stage keeps its (frozen)
            # parent, and "latest other than self" would be its own child (D9 free edit).
            latest = db.execute(
                get_chain_latest_stage_query(),
                {"root_modification_id": root_id, "exclude_id": _io(modification_id)},
            ).fetchone()
            if not latest or int(latest._mapping["modification_id"]) != int(parent["modification_id"]):
                ref = latest._mapping if latest else parent
                raise HTTPException(
                    status_code=400,
                    detail=NOT_LATEST_MSG.format(title=ref.get("title"), mod_date=ref.get("mod_date")),
                )
        parent_end = _event_end(parent["mod_date"], parent.get("mod_to_date"))
        if body.mod_date <= parent_end:
            raise HTTPException(status_code=400, detail=SAME_DAY_MSG.format(end=parent_end))
        if kind != KIND_REVERSAL:
            _check_reverted(db, root_id, member_ids, body.mod_date)  # C3

    # ---- party (P1), group (G1)
    party_id = None
    if done_by == DONE_BY_VENDOR:
        party = db.execute(
            get_party_active_query(), {"party_id": int(body.party_id), "co_id": co_id}
        ).fetchone()
        if not party:
            raise HTTPException(status_code=400, detail=UNKNOWN_PARTY_MSG)
        party_id = int(body.party_id)
    group_branch = None
    if body.loom_group_id is not None:
        grp = db.execute(
            get_loom_group_row_query(), {"loom_group_id": int(body.loom_group_id), "co_id": co_id}
        ).fetchone()
        if not grp:
            raise HTTPException(status_code=400, detail=UNKNOWN_GROUP_MSG)
        group_branch = grp._mapping.get("branch_id")

    # ---- loom context (L1) + overlap (O1)
    looms = _loom_context(db, co_id, member_ids, body.branch_id, existing_ids=old_ids)
    branch_id = int(looms[member_ids[0]]["branch_id"])
    if group_branch is not None and int(group_branch) != branch_id:
        raise HTTPException(status_code=400, detail=BRANCH_MISMATCH_MSG)
    _check_overlaps(db, co_id, body.members, body.mod_date, body.mod_to_date, looms, modification_id)

    # ---- header (W1, W2)
    params = {
        "co_id": co_id,
        "branch_id": branch_id,
        "loom_group_id": _io(body.loom_group_id),
        "mod_date": body.mod_date,
        "mod_to_date": body.mod_to_date,
        "settle_days": int(body.settle_days),
        "mod_kind": kind,
        "parent_modification_id": _io(body.parent_modification_id),
        "root_modification_id": root_id,
        "mod_category": category,
        "title": body.title.strip(),
        "description": body.description,
        "expected_effect": body.expected_effect,
        "done_by": done_by,
        "party_id": party_id,
        "total_cost": _fo(body.total_cost),
        "remarks": body.remarks,
        "updated_by": user_id,
    }
    if modification_id is None:
        result = db.execute(insert_modification_query(), params)
        saved_id = int(result.lastrowid)
        if root_id is None:
            root_id = saved_id
            db.execute(stamp_root_modification_query(), {"modification_id": saved_id})
    else:
        saved_id = int(modification_id)
        if root_id is None:
            root_id = saved_id
        params["modification_id"] = saved_id
        params["root_modification_id"] = root_id
        db.execute(update_modification_query(), params)
        db.execute(soft_delete_snapshot_query(), {"modification_id": saved_id})  # W3

    # ---- snapshot (W4)
    for m in body.members:
        db.execute(
            insert_snapshot_member_query(),
            {
                "modification_id": saved_id,
                "machine_id": int(m.machine_id),
                "done_date": m.done_date,
                "done_to_date": m.done_to_date,
                "remarks": m.remarks,
            },
        )

    # ---- data check (D1)
    data_check = _data_check(db, co_id, member_ids, body.mod_date)
    return {
        "modification_id": saved_id,
        "root_modification_id": root_id,
        "branch_id": branch_id,
        "data_check": data_check,
    }


def _data_check_message(dc: dict) -> str:
    msg = (
        f"saved — {dc['before_days']} producing loom-days in the 90 days before, "
        f"{dc['after_days']} after"
    )
    if dc["before_days"] == 0 or dc["after_days"] == 0:
        msg += ": the report will say NOT ENOUGH DATA"
    return msg


# =============================================================================
# Endpoints — modification reads
# =============================================================================


@router.get("/modification_table")
def modification_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated register. Filters: branch_id, loom_group_id, machine_id (joins the
    snapshot), date_from / date_to (on mod_date), mod_kind; page / limit (<= 200).
    Execute order: count, rows, data check for the page's ids (skipped when empty)."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        mod_kind = _optional_str(request, "mod_kind")
        if mod_kind is not None:
            mod_kind = mod_kind.upper()
            if mod_kind not in MOD_KINDS:
                raise HTTPException(status_code=400, detail=BAD_KIND_MSG)
        page, limit, offset = _pagination(request)
        filters = {
            "co_id": co_id,
            "branch_id": branch_id,
            "loom_group_id": _optional_int(request, "loom_group_id"),
            "machine_id": _optional_int(request, "machine_id"),
            "date_from": _optional_date(request, "date_from"),
            "date_to": _optional_date(request, "date_to"),
            "mod_kind": mod_kind,
        }
        total_row = db.execute(get_modification_table_count_query(), filters).fetchone()
        total = int(total_row._mapping["total"]) if total_row else 0

        params = dict(filters)
        params["limit"] = limit
        params["offset"] = offset
        rows = [
            _serialize(dict(r._mapping))
            for r in db.execute(get_modification_table_query(), params).fetchall()
        ]
        checks: Dict[int, dict] = {}
        if rows:
            for r in db.execute(
                get_data_check_for_modifications_query(),
                {"modification_ids": [int(x["modification_id"]) for x in rows]},
            ).fetchall():
                checks[int(r._mapping["modification_id"])] = {
                    "before_days": int(r._mapping.get("before_days") or 0),
                    "after_days": int(r._mapping.get("after_days") or 0),
                }
        for r in rows:
            r["data_check"] = checks.get(int(r["modification_id"]), {"before_days": 0, "after_days": 0})
            r["scope_label"] = (
                f"Group {r['group_code']} — {r['loom_count']} looms"
                if r.get("group_code")
                else f"{r['loom_count']} loom{'s' if r['loom_count'] != 1 else ''} picked"
            )
        return {"data": rows, "total": total, "page": page, "page_size": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modification_by_id/{modification_id}")
def modification_by_id(
    modification_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Header + members (labels, dates) + parent summary (when any) + children summaries.
    Execute order: header, members, [parent], children."""
    co_id = _require_co_id(request)
    try:
        header = _fetch_event(db, modification_id, co_id)
        if not header:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        _serialize(header)
        members = [
            _serialize(dict(r._mapping))
            for r in db.execute(
                get_snapshot_members_query(), {"modification_id": modification_id}
            ).fetchall()
        ]
        for m in members:
            m["machine_active"] = int(m.get("machine_active") or 0)
            m["label"] = _loom_label(m)
        parent = None
        if header.get("parent_modification_id") is not None:
            parent = _fetch_event(db, int(header["parent_modification_id"]), co_id)
            if parent:
                parent.pop("description", None)
                _serialize(parent)
        children = [
            _serialize(dict(r._mapping))
            for r in db.execute(
                get_modification_children_query(), {"modification_id": modification_id}
            ).fetchall()
        ]
        header["members"] = members
        header["parent"] = parent
        header["children"] = children
        return {"data": header}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modification_chain/{root_id}")
def modification_chain(
    root_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Every active event of the chain in date order, each with its snapshot.
    Execute order: events, chain snapshots."""
    co_id = _require_co_id(request)
    try:
        events = [
            _serialize(dict(r._mapping))
            for r in db.execute(
                get_modification_chain_query(), {"root_modification_id": root_id, "co_id": co_id}
            ).fetchall()
        ]
        if not events:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        by_id = {int(e["modification_id"]): e for e in events}
        for e in events:
            e["members"] = []
        for r in db.execute(
            get_chain_snapshot_members_query(), {"root_modification_id": root_id}
        ).fetchall():
            m = _serialize(dict(r._mapping))
            m["machine_active"] = int(m.get("machine_active") or 0)
            m["label"] = _loom_label(m)
            ev = by_id.get(int(m["modification_id"]))
            if ev is not None:
                ev["members"].append(m)
        return {"data": {"root_modification_id": root_id, "events": events}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/machine_timeline")
def machine_timeline(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """All snapshot rows of one loom across every chain, in date order."""
    co_id = _require_co_id(request)
    machine_id = _optional_int(request, "machine_id")
    if machine_id is None:
        raise HTTPException(status_code=400, detail="machine_id is required")
    try:
        rows = [
            _serialize(dict(r._mapping))
            for r in db.execute(
                get_machine_timeline_query(), {"co_id": co_id, "machine_id": machine_id}
            ).fetchall()
        ]
        return {"data": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — modification writes
# =============================================================================


@router.post("/modification_create")
def modification_create(
    body: ModificationCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create one stage. With ``save_as_group`` the hand-picked looms are first saved
    as a STUDY group (execute order of _create_loom_group) and the new loom_group_id
    rides in the header insert."""
    user_id = token_data.get("user_id")
    try:
        if body.save_as_group is not None:
            _validate_body(body)  # pure, so no group is created for a body that would 400
            body.loom_group_id = _create_loom_group(
                db,
                body.co_id,
                body.branch_id,
                body.save_as_group.group_code,
                body.save_as_group.group_name,
                body.save_as_group.group_purpose,
                None,
                [m.machine_id for m in body.members],
                user_id,
            )
        saved = _save_modification(db, body, user_id)
        db.commit()
        return {
            "data": {
                "modification_id": saved["modification_id"],
                "root_modification_id": saved["root_modification_id"],
                "loom_group_id": _io(body.loom_group_id),
                "data_check": saved["data_check"],
                "message": _data_check_message(saved["data_check"]),
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/modification_edit/{modification_id}")
def modification_edit(
    modification_id: int,
    body: ModificationUpdate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Full replace; snapshot soft-deleted + reinserted; parent / kind immutable while
    the stage has active children (save_as_group is ignored on edit)."""
    user_id = token_data.get("user_id")
    try:
        saved = _save_modification(db, body, user_id, modification_id=modification_id)
        db.commit()
        return {
            "data": {
                "message": _data_check_message(saved["data_check"]),
                "modification_id": saved["modification_id"],
                "root_modification_id": saved["root_modification_id"],
                "data_check": saved["data_check"],
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/modification_add_members/{modification_id}")
def modification_add_members(
    modification_id: int,
    body: AddMembersBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """"Add looms to this stage (done on ...)": append snapshot rows with their own
    done_date, validated inside the event's dates. Execute order: header (404),
    current snapshot ids (dupe guard), [REVERSAL: parent snapshot ids] |
    [FOLLOWUP / RECTIFY: chain reverted looms (C3 rule)], loom context, overlap
    spans, insert x N."""
    co_id = int(body.co_id)
    try:
        header = _fetch_event(db, modification_id, co_id)
        if not header:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        if not body.members:
            raise HTTPException(status_code=400, detail=NO_MEMBERS_MSG)
        ids = _distinct_ids([m.machine_id for m in body.members], DUP_MEMBER_MSG)
        _validate_member_dates(body.members, header["mod_date"], header.get("mod_to_date"), date.today())

        current = set(_snapshot_ids(db, modification_id))
        for mid in ids:
            if mid in current:
                raise HTTPException(status_code=400, detail=ALREADY_MEMBER_MSG.format(name=mid))
        if header.get("mod_kind") == KIND_REVERSAL and header.get("parent_modification_id") is not None:
            parent_ids = set(_snapshot_ids(db, int(header["parent_modification_id"])))
            for mid in ids:
                if mid not in parent_ids:
                    raise HTTPException(status_code=400, detail=REVERSAL_SUBSET_MSG.format(name=mid))
        elif header.get("mod_kind") in ("FOLLOWUP", "RECTIFY"):
            _check_reverted(
                db,
                int(header.get("root_modification_id") or modification_id),
                ids,
                header["mod_date"],
            )

        looms = _loom_context(db, co_id, ids, header.get("branch_id"))
        _check_overlaps(
            db, co_id, body.members, header["mod_date"], header.get("mod_to_date"), looms, modification_id
        )
        for m in body.members:
            db.execute(
                insert_snapshot_member_query(),
                {
                    "modification_id": modification_id,
                    "machine_id": int(m.machine_id),
                    "done_date": m.done_date,
                    "done_to_date": m.done_to_date,
                    "remarks": m.remarks,
                },
            )
        db.commit()
        return {"data": {"message": f"{len(ids)} loom(s) added to this stage", "added": len(ids)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/modification_insert_earlier/{root_id}")
def modification_insert_earlier(
    root_id: int,
    body: InsertEarlierBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """A change done earlier that was never entered: save the body as a new INITIAL,
    move the old chain under it and re-parent the old root as FOLLOWUP / RECTIFY (§5).
    Execute order: old root (404 / must be a root), _save_modification (create, with
    the old root's branch pinned so the chain stays in one branch), rewrite chain
    root, re-parent old root."""
    user_id = token_data.get("user_id")
    try:
        old_kind = (body.old_root_kind or "").strip().upper()
        if old_kind not in ("FOLLOWUP", "RECTIFY"):
            raise HTTPException(status_code=400, detail=BAD_OLD_ROOT_KIND_MSG)
        old_root = _fetch_event(db, root_id, int(body.co_id))
        if not old_root:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        if old_root.get("parent_modification_id") is not None:
            raise HTTPException(status_code=400, detail=INSERT_ROOT_ONLY_MSG)
        if _event_end(body.mod_date, body.mod_to_date) >= old_root["mod_date"]:
            raise HTTPException(
                status_code=400, detail=INSERT_EARLIER_END_MSG.format(d=old_root["mod_date"])
            )
        old_branch = old_root.get("branch_id")
        if old_branch is not None:
            if body.branch_id is not None and int(body.branch_id) != int(old_branch):
                raise HTTPException(
                    status_code=400, detail=INSERT_EARLIER_BRANCH_MSG.format(branch=old_branch)
                )
            body.branch_id = int(old_branch)  # L1 then enforces BRANCH_MISMATCH_MSG on the looms
        body.mod_kind = KIND_INITIAL
        body.parent_modification_id = None
        body.save_as_group = None
        saved = _save_modification(db, body, user_id)
        new_id = saved["modification_id"]
        db.execute(rewrite_chain_root_query(), {"new_root": new_id, "old_root": root_id})
        db.execute(
            reparent_old_root_query(),
            {"new_root": new_id, "old_root": root_id, "mod_kind": old_kind, "updated_by": user_id},
        )
        db.commit()
        return {
            "data": {
                "modification_id": new_id,
                "root_modification_id": new_id,
                "data_check": saved["data_check"],
                "message": _data_check_message(saved["data_check"]),
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/modification_delete/{modification_id}")
def modification_delete(
    modification_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete one stage (refused while children exist), or with ``chain=1`` on a
    root row the whole chain (snapshots + events, one transaction).
    Execute order: header (404), children count, then either
    [chain snapshots, chain events] or [snapshot, event]."""
    co_id = _require_co_id(request)
    chain = (request.query_params.get("chain") or "0").strip() in ("1", "true")
    user_id = token_data.get("user_id")
    try:
        header = _fetch_event(db, modification_id, co_id)
        if not header:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        kids = db.execute(
            count_modification_children_query(), {"modification_id": modification_id}
        ).fetchone()
        n_kids = int(kids._mapping["cnt"]) if kids else 0

        if chain:
            if header.get("parent_modification_id") is not None:
                raise HTTPException(status_code=400, detail=CHAIN_ON_ROOT_ONLY_MSG)
            root = int(header.get("root_modification_id") or modification_id)
            db.execute(soft_delete_chain_snapshots_query(), {"root_modification_id": root})
            db.execute(
                soft_delete_chain_query(), {"root_modification_id": root, "updated_by": user_id}
            )
            db.commit()
            return {
                "data": {
                    "message": "Chain deleted",
                    "deleted_stages": int(header.get("stage_count") or (n_kids + 1)),
                }
            }

        if n_kids > 0:
            raise HTTPException(status_code=400, detail=HAS_CHILDREN_MSG.format(n=n_kids))
        db.execute(soft_delete_snapshot_query(), {"modification_id": modification_id})
        db.execute(
            soft_delete_modification_query(),
            {"modification_id": modification_id, "updated_by": user_id},
        )
        db.commit()
        return {"data": {"message": "Modification deleted"}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
