"""Hands Norms master — maintains ``jute_prod_hands_norms_mst`` and the machine
groups it scopes on (TKT-000202, Hands Norms v2 of 2026-09-30).

The Hands Report (``hands_report.py``) takes its target hands from this table,
but until this router nothing in the app could write it: sls's rows were
bulk-loaded from the legacy MIS with no machine type on any of them, so every
per-machine norm ('2 MC = 1 HAND') had no machines to count and no target.

One row = one designation's manning norm from ``effective_date`` onwards, and
``norm_kind`` says how the report targets it (spec
docs/superpowers/specs/2026-09-30-hands-norms-v2-machine-groups.md §4):

* **F fixed** — the target is the row's own ``shift_a`` / ``shift_b`` /
  ``shift_c`` / ``shift_g``.
* **M machine-based** (was 'V') — Σ units of the scoped machines that ran in
  the shift ÷ ``units_per_hand``. The scope is exactly one of a machine group
  (members carry units: loom 1, drawing frame 10 points), a machine type, or
  a whole department (units 1 each).
* **P production** — production qty of the scoped machines / department that
  shift ÷ ``qty_per_hand`` (``qty_uom`` kg or cuts).
* **N no norm** — blank target, excluded from excess/short.
* **A as actual** — target = hands present.

``variable_fixed`` ('V' for M/P, 'F' for F/N/A) and ``hands_per_machine``
(``1 / units_per_hand`` for M) are kept as mirrors for pre-v2 readers and
rewritten on every save.

Saving normalises a row to exactly what the report will read: a fixed row
drops any scope and rate, an M/P row keeps exactly one scope, and an ACTIVE
M/P row must be complete (scope + rate) or it is refused — otherwise its
target is blank on the report. Retired rows skip that so a legacy row can be
switched off as it is.

Changing a norm going forward = a new row with a later effective_date (the
page's "New revision"), so past reports keep the norm that was in force then.
Rows are never deleted; ``active = 0`` retires one.

``in_force`` on the list marks the row the Hands Report would use TODAY. The
rule mirrors ``hands_report._norm_sort_key`` / ``_norm_for`` — keep them in
step: per designation, among active rows with effective_date <= today, the
latest effective_date wins, a branch-specific row beats the co-wide
(NULL-branch) one on the same date, then the newest id.

Machine groups (``jute_prod_machine_group_mst`` / ``_dtl``) are a tab on the
same page and served by the ``/groups`` endpoints below: a group belongs to one
branch, its members are active machines of that branch's departments, each
with units > 0; a group in use by an active norm cannot be retired.

Access (§6.3): writes need the user's level on the page's own menu row
(``juteProduction/masters/handsNorms``) — create ≥ 3 Write, edit ≥ 4 Edit —
else 403; reads stay at the page's view level (the sidebar gates the page).

Portal persona: get_tenant_db + get_current_user_with_refresh, ``{"data": ...}``
responses. The norms table has no audit columns (DB triggers), so nothing
stamps updated_by there; the group header carries updated_by like the other
jute_prod masters.
"""

import math
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.spinning_lock import user_menu_access_level


router = APIRouter()

NORM_NOT_FOUND_MSG = "Hands norm not found"
GROUP_NOT_FOUND_MSG = "Machine group not found"
NORM_KINDS = ("F", "M", "P", "N", "A")
LEGACY_NORM_TYPES = ("F", "V")  # variable_fixed, accepted as an input alias
SCOPED_KINDS = ("M", "P")
PROD_UOMS = ("kg", "cuts")
DIRECT_INDIRECT = ("D", "I")
SHIFT_FIELDS = ("shift_a", "shift_b", "shift_c", "shift_g")
SCOPE_FIELDS = ("machine_group_id", "machine_type_id", "scope_dept_id")
NORMS_TEXT_MAX = 80
# Column limits: shift_* DECIMAL(7,2), hands_per_machine DECIMAL(9,4),
# units_per_hand DECIMAL(9,3), qty_per_hand DECIMAL(12,3), dtl.units DECIMAL(9,3).
SHIFT_MAX = 99999.99
HANDS_PER_MACHINE_MAX = 99999.9999
UNITS_PER_HAND_MAX = 999999.999
QTY_PER_HAND_MAX = 999999999.999
MEMBER_UNITS_MAX = 999999.999
GROUP_CODE_MAX = 30
GROUP_NAME_MAX = 100
UNIT_LABEL_MAX = 20
GROUP_REMARKS_MAX = 255
DEFAULT_UNIT_LABEL = "machine"

# Access (§6.3): the page's own menu row; thresholds mirror the FE action map
# (sidebarContext.tsx: create needs Write 3, edit needs Edit 4).
HANDS_NORMS_MENU_PATH = "juteProduction/masters/handsNorms"
WRITE_LEVEL = 3
EDIT_LEVEL = 4
VIEW_ONLY_MSG = "This role can view hands norms but not change them"


# =============================================================================
# SQL — norms
# =============================================================================

_CO_BRANCHES = "(SELECT b.branch_id FROM branch_mst b WHERE b.co_id = :co_id)"
_BRANCH_DEPTS = "(SELECT dp.dept_id FROM dept_mst dp WHERE dp.branch_id = :branch_id)"


def get_hands_norms_list_query():
    """Every norm row visible from one branch: its own rows plus the co-wide
    (NULL-branch) fallbacks — the same scope the Hands Report reads.

    Binds: :co_id, :branch_id, :include_inactive (0/1)."""
    return text(
        """
        SELECT n.hands_norm_id, n.co_id, n.branch_id, n.designation_id,
               d.desig AS designation, d.dept_id, dp.dept_desc AS dept,
               n.direct_indirect, n.variable_fixed, n.norm_kind,
               n.shift_a, n.shift_b, n.shift_c, n.shift_g,
               n.machine_type_id, mt.machine_type_name,
               n.machine_group_id, g.group_name AS machine_group, g.unit_label,
               n.scope_dept_id, sd.dept_desc AS scope_dept,
               n.hands_per_machine, n.units_per_hand, n.qty_per_hand, n.qty_uom,
               n.norms_text, n.effective_date, n.active
        FROM jute_prod_hands_norms_mst n
        LEFT JOIN designation_mst d ON d.designation_id = n.designation_id
        LEFT JOIN dept_mst dp ON dp.dept_id = d.dept_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = n.machine_type_id
        LEFT JOIN jute_prod_machine_group_mst g ON g.machine_group_id = n.machine_group_id
        LEFT JOIN dept_mst sd ON sd.dept_id = n.scope_dept_id
        WHERE n.co_id = :co_id
          AND (n.branch_id = :branch_id OR n.branch_id IS NULL)
          AND (:include_inactive = 1 OR n.active = 1)
        ORDER BY (dp.order_id IS NULL), dp.order_id, dp.dept_desc, d.desig,
                 n.effective_date DESC, n.hands_norm_id DESC
        """
    )


def get_hands_norm_row_query():
    return text(
        """
        SELECT hands_norm_id, co_id, branch_id, designation_id, direct_indirect,
               variable_fixed, norm_kind, shift_a, shift_b, shift_c, shift_g,
               machine_type_id, machine_group_id, scope_dept_id,
               hands_per_machine, units_per_hand, qty_per_hand, qty_uom,
               norms_text, effective_date, active
        FROM jute_prod_hands_norms_mst
        WHERE hands_norm_id = :id
        """
    )


def get_norm_designations_query():
    """Designations of the WHOLE company, not just the branch: attendance's
    worked_designation_id can point at another branch's designation (dev3 does
    exactly that), and those need norms too. The page's branch sorts first.

    Binds: :co_id, :branch_id."""
    return text(
        f"""
        SELECT d.designation_id, d.desig, d.dept_id, dp.dept_desc AS dept,
               d.direct_indirect, d.branch_id, b.branch_name
        FROM designation_mst d
        LEFT JOIN dept_mst dp ON dp.dept_id = d.dept_id
        LEFT JOIN branch_mst b ON b.branch_id = d.branch_id
        WHERE d.active = 1
          AND d.branch_id IN {_CO_BRANCHES}
        ORDER BY (d.branch_id <> :branch_id), dp.dept_desc, d.desig
        """
    )


def get_norm_machine_types_query():
    """Active machine types, each with how many active machines the branch has
    of it — sls carries 60 types, most with no machine, and a norm linked to an
    empty type always targets 0.

    Binds: :branch_id."""
    return text(
        f"""
        SELECT mt.machine_type_id, mt.machine_type_name,
               COUNT(m.machine_id) AS machine_count
        FROM machine_type_mst mt
        LEFT JOIN machine_mst m
               ON m.machine_type_id = mt.machine_type_id
              AND m.active = 1
              AND m.dept_id IN {_BRANCH_DEPTS}
        WHERE mt.active = 1
        GROUP BY mt.machine_type_id, mt.machine_type_name
        ORDER BY (COUNT(m.machine_id) = 0), mt.machine_type_name
        """
    )


def get_norm_machines_query():
    """Every active machine of the branch's departments — the group dialog's
    member picker (~800 rows on sls, filtered client-side).

    Binds: :branch_id."""
    return text(
        f"""
        SELECT m.machine_id, m.machine_name, m.mech_code, m.machine_type_id,
               mt.machine_type_name, m.dept_id, dp.dept_desc AS dept
        FROM machine_mst m
        JOIN dept_mst dp ON dp.dept_id = m.dept_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        WHERE m.active = 1
          AND m.dept_id IN {_BRANCH_DEPTS}
        ORDER BY dp.order_id, dp.dept_desc, mt.machine_type_name, m.machine_name
        """
    )


def get_norm_departments_query():
    """Departments of the branch — the department scope of an M/P norm
    (dept_mst has no active flag).

    Binds: :branch_id."""
    return text(
        """
        SELECT dp.dept_id, dp.dept_desc
        FROM dept_mst dp
        WHERE dp.branch_id = :branch_id
        ORDER BY (dp.order_id IS NULL), dp.order_id, dp.dept_desc
        """
    )


def get_designation_in_company_query():
    return text(
        f"""
        SELECT d.designation_id, d.direct_indirect
        FROM designation_mst d
        WHERE d.designation_id = :designation_id
          AND d.branch_id IN {_CO_BRANCHES}
        """
    )


def get_machine_type_exists_query():
    return text(
        "SELECT machine_type_id FROM machine_type_mst WHERE machine_type_id = :machine_type_id"
    )


def get_group_for_norm_query():
    """An ACTIVE group of the company, with its branch (the caller checks it
    against the norm's branch). Binds: :co_id, :machine_group_id."""
    return text(
        """
        SELECT machine_group_id, branch_id
        FROM jute_prod_machine_group_mst
        WHERE machine_group_id = :machine_group_id AND co_id = :co_id AND active = 1
        """
    )


def get_dept_for_norm_query():
    """A department of the norm's branch (NULL branch = any branch of the co).
    dept_mst carries no active flag. Binds: :co_id, :branch_id, :dept_id."""
    return text(
        f"""
        SELECT dp.dept_id
        FROM dept_mst dp
        WHERE dp.dept_id = :dept_id
          AND (:branch_id IS NULL OR dp.branch_id = :branch_id)
          AND dp.branch_id IN {_CO_BRANCHES}
        """
    )


def get_duplicate_norm_query():
    """An ACTIVE row for the same designation, branch scope and effective date.
    Two such rows make the report pick one silently (newest id), so saving a
    second one is refused. ``<=>`` so a NULL (co-wide) branch matches NULL."""
    return text(
        """
        SELECT hands_norm_id
        FROM jute_prod_hands_norms_mst
        WHERE co_id = :co_id
          AND designation_id = :designation_id
          AND effective_date = :effective_date
          AND branch_id <=> :branch_id
          AND active = 1
          AND (:exclude_id IS NULL OR hands_norm_id <> :exclude_id)
        LIMIT 1
        """
    )


def insert_hands_norm_query():
    return text(
        """
        INSERT INTO jute_prod_hands_norms_mst
            (co_id, branch_id, designation_id, direct_indirect, variable_fixed, norm_kind,
             shift_a, shift_b, shift_c, shift_g,
             machine_type_id, machine_group_id, scope_dept_id,
             hands_per_machine, units_per_hand, qty_per_hand, qty_uom,
             norms_text, effective_date, active)
        VALUES
            (:co_id, :branch_id, :designation_id, :direct_indirect, :variable_fixed, :norm_kind,
             :shift_a, :shift_b, :shift_c, :shift_g,
             :machine_type_id, :machine_group_id, :scope_dept_id,
             :hands_per_machine, :units_per_hand, :qty_per_hand, :qty_uom,
             :norms_text, :effective_date, 1)
        """
    )


def update_hands_norm_query():
    return text(
        """
        UPDATE jute_prod_hands_norms_mst
        SET direct_indirect = :direct_indirect,
            variable_fixed = :variable_fixed,
            norm_kind = :norm_kind,
            shift_a = :shift_a, shift_b = :shift_b,
            shift_c = :shift_c, shift_g = :shift_g,
            machine_type_id = :machine_type_id,
            machine_group_id = :machine_group_id,
            scope_dept_id = :scope_dept_id,
            hands_per_machine = :hands_per_machine,
            units_per_hand = :units_per_hand,
            qty_per_hand = :qty_per_hand,
            qty_uom = :qty_uom,
            norms_text = :norms_text,
            effective_date = :effective_date,
            active = :active
        WHERE hands_norm_id = :id AND co_id = :co_id
        """
    )


def update_hands_norm_active_query():
    return text(
        """
        UPDATE jute_prod_hands_norms_mst
        SET active = :active
        WHERE hands_norm_id = :id AND co_id = :co_id
        """
    )


# =============================================================================
# SQL — machine groups
# =============================================================================


def get_machine_groups_query():
    """Groups of the co/branch with member_count, total_units (active members)
    and norms_using (active norms scoped on the group).

    Binds: :co_id, :branch_id, :include_inactive (0/1)."""
    return text(
        """
        SELECT g.machine_group_id, g.co_id, g.branch_id, g.group_code, g.group_name,
               g.unit_label, g.remarks, g.active,
               (SELECT COUNT(*) FROM jute_prod_machine_group_dtl d
                 WHERE d.machine_group_id = g.machine_group_id AND d.active = 1) AS member_count,
               (SELECT COALESCE(SUM(d.units), 0) FROM jute_prod_machine_group_dtl d
                 WHERE d.machine_group_id = g.machine_group_id AND d.active = 1) AS total_units,
               (SELECT COUNT(*) FROM jute_prod_hands_norms_mst n
                 WHERE n.machine_group_id = g.machine_group_id AND n.active = 1) AS norms_using
        FROM jute_prod_machine_group_mst g
        WHERE g.co_id = :co_id
          AND g.branch_id = :branch_id
          AND (:include_inactive = 1 OR g.active = 1)
        ORDER BY (g.active = 0), g.group_name, g.machine_group_id
        """
    )


def get_machine_group_row_query():
    return text(
        """
        SELECT machine_group_id, co_id, branch_id, group_code, group_name,
               unit_label, remarks, active
        FROM jute_prod_machine_group_mst
        WHERE machine_group_id = :id
        """
    )


def get_machine_group_members_query():
    """Active members of a group with the machine's type and department.
    Binds: :id."""
    return text(
        """
        SELECT d.machine_group_dtl_id, d.machine_id, m.machine_name, m.mech_code,
               m.machine_type_id, mt.machine_type_name, m.dept_id, dp.dept_desc AS dept,
               d.units
        FROM jute_prod_machine_group_dtl d
        LEFT JOIN machine_mst m ON m.machine_id = d.machine_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst dp ON dp.dept_id = m.dept_id
        WHERE d.machine_group_id = :id AND d.active = 1
        ORDER BY dp.order_id, dp.dept_desc, mt.machine_type_name, m.machine_name, d.machine_id
        """
    )


def get_duplicate_group_code_query():
    """An ACTIVE group with the same code in the co/branch (case-insensitive
    collation). Binds: :co_id, :branch_id, :group_code, :exclude_id."""
    return text(
        """
        SELECT machine_group_id
        FROM jute_prod_machine_group_mst
        WHERE co_id = :co_id AND branch_id = :branch_id
          AND group_code = :group_code AND active = 1
          AND (:exclude_id IS NULL OR machine_group_id <> :exclude_id)
        LIMIT 1
        """
    )


def get_group_norms_using_query():
    """Active norms scoped on the group — names for the refusal message.
    Binds: :machine_group_id."""
    return text(
        """
        SELECT n.hands_norm_id, d.desig
        FROM jute_prod_hands_norms_mst n
        LEFT JOIN designation_mst d ON d.designation_id = n.designation_id
        WHERE n.machine_group_id = :machine_group_id AND n.active = 1
        ORDER BY d.desig, n.hands_norm_id
        """
    )


def get_branch_machines_query():
    """Which of the given machine ids are ACTIVE machines of the branch's
    departments. Binds: :branch_id, :machine_ids (expanding list)."""
    return text(
        f"""
        SELECT m.machine_id
        FROM machine_mst m
        WHERE m.active = 1
          AND m.dept_id IN {_BRANCH_DEPTS}
          AND m.machine_id IN :machine_ids
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


def insert_machine_group_query():
    return text(
        """
        INSERT INTO jute_prod_machine_group_mst
            (co_id, branch_id, group_code, group_name, unit_label, remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :group_code, :group_name, :unit_label, :remarks, 1, :updated_by)
        """
    )


def update_machine_group_query():
    return text(
        """
        UPDATE jute_prod_machine_group_mst
        SET group_code = :group_code, group_name = :group_name, unit_label = :unit_label,
            remarks = :remarks, active = :active, updated_by = :updated_by
        WHERE machine_group_id = :id AND co_id = :co_id
        """
    )


def get_machine_group_member_rows_query():
    """Current ACTIVE member rows (id, machine, units) for the edit diff.
    Binds: :id."""
    return text(
        """
        SELECT machine_group_dtl_id, machine_id, units
        FROM jute_prod_machine_group_dtl
        WHERE machine_group_id = :id AND active = 1
        """
    )


def insert_machine_group_dtl_query():
    return text(
        """
        INSERT INTO jute_prod_machine_group_dtl (machine_group_id, machine_id, units, active)
        VALUES (:machine_group_id, :machine_id, :units, 1)
        """
    )


def update_machine_group_dtl_units_query():
    return text(
        """
        UPDATE jute_prod_machine_group_dtl
        SET units = :units
        WHERE machine_group_dtl_id = :machine_group_dtl_id
        """
    )


def retire_machine_group_dtl_query():
    return text(
        """
        UPDATE jute_prod_machine_group_dtl
        SET active = 0
        WHERE machine_group_dtl_id IN :ids
        """
    ).bindparams(bindparam("ids", expanding=True))


# =============================================================================
# Pydantic models
# =============================================================================
#
# Numbers are validated in the handler (finite, >= 0), not with Field(ge=0): a
# NaN failing a Pydantic constraint makes FastAPI build a 422 body containing
# NaN, which then breaks the JSON encoder (same reasoning as
# winding_target_map.py).


class HandsNormCreate(BaseModel):
    co_id: int
    branch_id: int
    designation_id: int
    effective_date: date
    norm_kind: Optional[str] = None
    variable_fixed: Optional[str] = None  # legacy alias: V -> M, F -> F
    direct_indirect: Optional[str] = None
    shift_a: Optional[float] = 0
    shift_b: Optional[float] = 0
    shift_c: Optional[float] = 0
    shift_g: Optional[float] = 0
    machine_type_id: Optional[int] = None
    machine_group_id: Optional[int] = None
    scope_dept_id: Optional[int] = None
    units_per_hand: Optional[float] = None
    hands_per_unit: Optional[float] = None  # input-only alias: units_per_hand = 1 / hands_per_unit
    hands_per_machine: Optional[float] = None  # legacy alias of hands_per_unit
    qty_per_hand: Optional[float] = None
    qty_uom: Optional[str] = None
    norms_text: Optional[str] = None


class HandsNormUpdate(BaseModel):
    """Only the fields sent are changed (exclude_unset). The designation and the
    branch scope of a row are fixed — a different designation is a new row."""

    effective_date: Optional[date] = None
    norm_kind: Optional[str] = None
    variable_fixed: Optional[str] = None
    direct_indirect: Optional[str] = None
    shift_a: Optional[float] = None
    shift_b: Optional[float] = None
    shift_c: Optional[float] = None
    shift_g: Optional[float] = None
    machine_type_id: Optional[int] = None
    machine_group_id: Optional[int] = None
    scope_dept_id: Optional[int] = None
    units_per_hand: Optional[float] = None
    hands_per_unit: Optional[float] = None
    hands_per_machine: Optional[float] = None
    qty_per_hand: Optional[float] = None
    qty_uom: Optional[str] = None
    norms_text: Optional[str] = None
    active: Optional[int] = None


class MachineGroupMember(BaseModel):
    machine_id: int
    units: Optional[float] = 1


class MachineGroupCreate(BaseModel):
    co_id: int
    branch_id: int
    group_code: str
    group_name: str
    unit_label: Optional[str] = None
    remarks: Optional[str] = None
    members: list[MachineGroupMember] = []


class MachineGroupUpdate(BaseModel):
    """Only the fields sent are changed; ``members`` is a FULL replacement of
    the active member list. co/branch are fixed."""

    group_code: Optional[str] = None
    group_name: Optional[str] = None
    unit_label: Optional[str] = None
    remarks: Optional[str] = None
    active: Optional[int] = None
    members: Optional[list[MachineGroupMember]] = None


# =============================================================================
# Helpers
# =============================================================================


def _require_int(request: Request, key: str) -> int:
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _optional_int(request: Request, key: str) -> Optional[int]:
    raw = request.query_params.get(key)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _num(value, label: str, maximum: float) -> Optional[float]:
    """None stays None; anything else must be a finite number in [0, maximum]."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} must be a number")
    if not math.isfinite(v):
        raise HTTPException(status_code=400, detail=f"{label} must be a finite number")
    if v < 0:
        raise HTTPException(status_code=400, detail=f"{label} cannot be negative")
    if v > maximum:
        raise HTTPException(status_code=400, detail=f"{label} cannot exceed {maximum}")
    return v


def _flag(value, allowed: tuple, label: str) -> Optional[str]:
    """'v' / ' V ' / 'Variable' -> 'V'; '' / None -> None; anything else 400."""
    if value is None:
        return None
    s = str(value).strip().upper()[:1]
    if not s:
        return None
    if s not in allowed:
        raise HTTPException(
            status_code=400, detail=f"{label} must be one of {', '.join(allowed)}"
        )
    return s


def _text(value, label: str, maximum: int, required: bool = False) -> Optional[str]:
    s = (value or "").strip() or None
    if s is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{label} is required")
        return None
    if len(s) > maximum:
        raise HTTPException(
            status_code=400, detail=f"{label} cannot exceed {maximum} characters"
        )
    return s


def _positive(value) -> bool:
    return value is not None and float(value) > 0


def legacy_kind(variable_fixed) -> Optional[str]:
    """The pre-v2 flag as a kind: V -> M, F -> F, blank -> None."""
    vf = _flag(variable_fixed, LEGACY_NORM_TYPES, "Norm type")
    return None if vf is None else ("M" if vf == "V" else "F")


def _kind_of_input(row: dict) -> str:
    """The kind a save asks for: ``norm_kind`` when sent, else the legacy
    ``variable_fixed`` alias; neither -> 400."""
    kind = _flag(row.get("norm_kind"), NORM_KINDS, "Norm type")
    if kind is None:
        kind = legacy_kind(row.get("variable_fixed"))
    if kind is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Norm type is required (F = fixed, M = machine, P = production, "
                "N = no norm, A = as actual)"
            ),
        )
    return kind


def _scope_of(row: dict) -> dict:
    """The scope columns present on a row (ints), unset ones None."""
    out = {}
    for f in SCOPE_FIELDS:
        v = row.get(f)
        out[f] = None if v in (None, "") else int(v)
    return out


def normalise_norm(row: dict, strict: bool = True) -> dict:
    """Validate a merged norm row and return the column values to store.

    Per kind (spec §6.1): F keeps its shifts and drops every scope / rate;
    M keeps exactly one scope and ``units_per_hand`` (``hands_per_unit`` /
    legacy ``hands_per_machine`` inputs are converted), shifts 0; P keeps one
    scope, ``qty_per_hand`` and ``qty_uom``, shifts 0; N / A clear everything.
    The legacy mirrors are rewritten: ``variable_fixed`` V for M/P else F,
    ``hands_per_machine`` = ROUND(1 / units_per_hand, 4) for M else NULL.

    ``strict`` (an active row) enforces completeness — the rule that decides
    whether the report can compute the target at all: an M row needs a scope
    and units per hand > 0, a P row a scope, qty per hand > 0 and a uom.
    Retired (inactive) rows skip it so a legacy unlinked row can still be
    switched off after other edits. More than one scope is refused always."""
    kind = _kind_of_input(row)
    if row.get("effective_date") is None:
        raise HTTPException(status_code=400, detail="Effective date is required")

    out = {
        "norm_kind": kind,
        "variable_fixed": "V" if kind in SCOPED_KINDS else "F",
        "direct_indirect": _flag(row.get("direct_indirect"), DIRECT_INDIRECT, "Direct/Indirect"),
        "norms_text": _text(row.get("norms_text"), "Norms text", NORMS_TEXT_MAX),
        "effective_date": row["effective_date"],
        "machine_type_id": None,
        "machine_group_id": None,
        "scope_dept_id": None,
        "hands_per_machine": None,
        "units_per_hand": None,
        "qty_per_hand": None,
        "qty_uom": None,
    }
    for f in SHIFT_FIELDS:
        out[f] = _num(row.get(f), f"Shift {f[-1].upper()} hands", SHIFT_MAX) or 0.0

    if kind == "F":
        # The report computes ANY row with a scope + rate as machine-based, so
        # a fixed row must not keep them.
        return out

    for f in SHIFT_FIELDS:
        # Only a fixed norm reads shift counts; a stale count on a machine /
        # production row would just mislead the page.
        out[f] = 0.0

    if kind not in SCOPED_KINDS:
        return out  # N / A: nothing else to store

    scope = _scope_of(row)
    chosen = [f for f in SCOPE_FIELDS if scope[f] is not None]
    if len(chosen) > 1:
        raise HTTPException(
            status_code=400,
            detail="Pick one scope only: a machine group, a machine type or a department",
        )
    what = "machine" if kind == "M" else "production"
    if strict and not chosen:
        raise HTTPException(
            status_code=400,
            detail=f"A {what} norm needs a scope: a machine group, a machine type or a department",
        )
    out.update(scope)

    if kind == "M":
        units = _num(row.get("units_per_hand"), "Units per hand", UNITS_PER_HAND_MAX)
        hpu = None
        if units is None:
            # Said the other way round: "hands per unit" (input-only alias) or
            # the legacy "hands per machine" — stored as units = 1 / hands.
            hpu = _num(row.get("hands_per_unit"), "Hands per unit", HANDS_PER_MACHINE_MAX)
            if hpu is None:
                hpu = _num(row.get("hands_per_machine"), "Hands per machine", HANDS_PER_MACHINE_MAX)
            if hpu is not None:
                units = round(1 / hpu, 3) if hpu > 0 else 0.0
        if strict and not _positive(units):
            raise HTTPException(
                status_code=400,
                detail="A machine norm needs units per hand (or hands per machine) greater than 0",
            )
        out["units_per_hand"] = units
        # Legacy mirror: the hands-per-unit figure as typed when that is what
        # came in (no 1.5 -> 0.667 -> 1.4993 round trip), else 1 / units.
        if _positive(hpu):
            out["hands_per_machine"] = hpu
        elif _positive(units):
            out["hands_per_machine"] = round(1 / units, 4)
        return out

    qty = _num(row.get("qty_per_hand"), "Qty per hand", QTY_PER_HAND_MAX)
    uom = (row.get("qty_uom") or "").strip().lower() or None
    if uom is not None and uom not in PROD_UOMS:
        raise HTTPException(
            status_code=400, detail=f"Unit must be one of {', '.join(PROD_UOMS)}"
        )
    if strict:
        if not _positive(qty):
            raise HTTPException(
                status_code=400,
                detail="A production norm needs qty per hand greater than 0",
            )
        if uom is None:
            raise HTTPException(
                status_code=400,
                detail=f"A production norm needs a unit ({' or '.join(PROD_UOMS)})",
            )
    out["qty_per_hand"] = qty
    out["qty_uom"] = uom
    return out


def mark_in_force(rows: list, on: date) -> None:
    """Stamp ``in_force`` on the row the Hands Report would use on ``on``
    (see the module docstring — mirrors hands_report._norm_sort_key), and
    ``duplicate`` on active rows sharing designation + branch + date with
    another active row."""
    best: dict = {}
    seen: dict = {}
    for r in rows:
        r["in_force"] = False
        r["duplicate"] = False
        if not r.get("active"):
            continue
        dup_key = (r["designation_id"], r.get("branch_id"), r["effective_date"])
        seen.setdefault(dup_key, []).append(r)
        if r["effective_date"] > on:
            continue
        key = (
            r["effective_date"],
            0 if r.get("branch_id") is None else 1,
            int(r["hands_norm_id"]),
        )
        cur = best.get(r["designation_id"])
        if cur is None or key > cur[0]:
            best[r["designation_id"]] = (key, r)
    for _key, r in best.values():
        r["in_force"] = True
    for group in seen.values():
        if len(group) > 1:
            for r in group:
                r["duplicate"] = True


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


def norm_kind_of(m: dict) -> str:
    """The kind of a stored row. ``norm_kind`` when the row carries one;
    otherwise (row dicts from before the v2 migration, tests) it is derived the
    way the migration maps them: 'V' or a per-machine rate -> M, else F —
    except a rate WITH shift counts, which the pre-v2 page read as fixed."""
    kind = (m.get("norm_kind") or "").strip().upper()[:1]
    if kind in NORM_KINDS:
        return kind
    if (m.get("variable_fixed") or "").strip().upper()[:1] == "V":
        return "M"
    if _positive(m.get("hands_per_machine")):
        if m.get("machine_type_id") is not None or not any(
            float(m.get(f) or 0) for f in SHIFT_FIELDS
        ):
            return "M"
    return "F"


def has_scope(m: dict) -> bool:
    return any(m.get(f) is not None for f in SCOPE_FIELDS)


def is_computable(m: dict) -> bool:
    """Whether the Hands Report can produce a target from this row (same rule
    as hands_report._targets): F / N / A always; M needs a scope (group, type
    or department) and units per hand > 0 (or the legacy hands per machine >
    0); P needs a scope and qty per hand > 0. sls's legacy seed left 47 'F'
    rows with a rate and no scope ('2 MC = 1 HAND' -> 0.5) — those are M rows
    without a scope now, and the page lists them for completion."""
    kind = norm_kind_of(m)
    if kind == "M":
        return has_scope(m) and (
            _positive(m.get("units_per_hand")) or _positive(m.get("hands_per_machine"))
        )
    if kind == "P":
        return has_scope(m) and _positive(m.get("qty_per_hand"))
    return True


def _serialise(m: dict) -> dict:
    vf = (m.get("variable_fixed") or "").strip().upper()[:1] or None
    return {
        "hands_norm_id": int(m["hands_norm_id"]),
        "co_id": m.get("co_id"),
        "branch_id": m.get("branch_id"),
        "designation_id": int(m["designation_id"]),
        "designation": m.get("designation"),
        "dept_id": m.get("dept_id"),
        "dept": m.get("dept"),
        "direct_indirect": m.get("direct_indirect"),
        "variable_fixed": vf,
        "norm_kind": norm_kind_of(m),
        "shift_a": _f(m.get("shift_a")),
        "shift_b": _f(m.get("shift_b")),
        "shift_c": _f(m.get("shift_c")),
        "shift_g": _f(m.get("shift_g")),
        "machine_type_id": m.get("machine_type_id"),
        "machine_type_name": m.get("machine_type_name"),
        "machine_group_id": m.get("machine_group_id"),
        "machine_group": m.get("machine_group"),
        "unit_label": m.get("unit_label"),
        "scope_dept_id": m.get("scope_dept_id"),
        "scope_dept": m.get("scope_dept"),
        "hands_per_machine": _f(m.get("hands_per_machine")),
        "units_per_hand": _f(m.get("units_per_hand")),
        "qty_per_hand": _f(m.get("qty_per_hand")),
        "qty_uom": m.get("qty_uom"),
        "norms_text": m.get("norms_text"),
        "effective_date": m["effective_date"],
        "active": int(m.get("active") or 0),
        # False = the report leaves this designation's target blank until the
        # row gets a scope and a rate (or becomes a fixed / N / A norm).
        "computable": is_computable(m),
    }


def _serialise_group(m: dict) -> dict:
    return {
        "machine_group_id": int(m["machine_group_id"]),
        "co_id": m.get("co_id"),
        "branch_id": m.get("branch_id"),
        "group_code": m.get("group_code"),
        "group_name": m.get("group_name"),
        "unit_label": m.get("unit_label"),
        "remarks": m.get("remarks"),
        "active": int(m.get("active") or 0),
        "member_count": int(m.get("member_count") or 0),
        "total_units": float(m.get("total_units") or 0),
        "norms_using": int(m.get("norms_using") or 0),
    }


def _serialise_member(m: dict) -> dict:
    return {
        "machine_group_dtl_id": m.get("machine_group_dtl_id"),
        "machine_id": int(m["machine_id"]),
        "machine_name": m.get("machine_name"),
        "mech_code": m.get("mech_code"),
        "machine_type_id": m.get("machine_type_id"),
        "machine_type_name": m.get("machine_type_name"),
        "dept_id": m.get("dept_id"),
        "dept": m.get("dept"),
        "units": float(m.get("units") or 0),
    }


def _require_level(db: Session, token_data: dict, co_id: int, branch_id: Optional[int], level: int) -> None:
    """403 unless the user holds ``level`` on the Hands Norms menu row for the
    co (and branch, when known) — §6.3."""
    have = user_menu_access_level(
        db, token_data.get("user_id"), co_id, branch_id, HANDS_NORMS_MENU_PATH
    )
    if have < level:
        raise HTTPException(status_code=403, detail=VIEW_ONLY_MSG)


def _check_designation(db: Session, co_id: int, designation_id: int) -> dict:
    row = db.execute(
        get_designation_in_company_query(),
        {"co_id": co_id, "designation_id": designation_id},
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=400, detail="Designation not found in this company"
        )
    return dict(row._mapping)


def _check_machine_type(db: Session, machine_type_id: Optional[int]) -> None:
    if machine_type_id is None:
        return
    if not db.execute(
        get_machine_type_exists_query(), {"machine_type_id": machine_type_id}
    ).fetchone():
        raise HTTPException(status_code=400, detail="Machine type not found")


def _check_group(db: Session, co_id: int, branch_id: Optional[int], group_id: Optional[int]) -> None:
    """An active group of the same co and branch (a co-wide NULL-branch norm
    may use a group of any branch of the co)."""
    if group_id is None:
        return
    row = db.execute(
        get_group_for_norm_query(), {"co_id": co_id, "machine_group_id": group_id}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Machine group not found in this company")
    if branch_id is not None and int(row._mapping["branch_id"]) != int(branch_id):
        raise HTTPException(
            status_code=400, detail="Machine group belongs to another branch"
        )


def _check_dept(db: Session, co_id: int, branch_id: Optional[int], dept_id: Optional[int]) -> None:
    if dept_id is None:
        return
    if not db.execute(
        get_dept_for_norm_query(),
        {"co_id": co_id, "branch_id": branch_id, "dept_id": dept_id},
    ).fetchone():
        raise HTTPException(status_code=400, detail="Department not found in this branch")


def _check_scope(db: Session, co_id: int, branch_id: Optional[int], values: dict) -> None:
    _check_machine_type(db, values["machine_type_id"])
    _check_group(db, co_id, branch_id, values["machine_group_id"])
    _check_dept(db, co_id, branch_id, values["scope_dept_id"])


def _check_duplicate(
    db: Session,
    co_id: int,
    branch_id: Optional[int],
    designation_id: int,
    effective_date: date,
    exclude_id: Optional[int],
) -> None:
    clash = db.execute(
        get_duplicate_norm_query(),
        {
            "co_id": co_id,
            "branch_id": branch_id,
            "designation_id": designation_id,
            "effective_date": effective_date,
            "exclude_id": exclude_id,
        },
    ).fetchone()
    if clash:
        raise HTTPException(
            status_code=400,
            detail=(
                "An active norm for this designation already starts on "
                f"{effective_date.isoformat()} — edit that row or use another "
                "effective date"
            ),
        )


def _alias_legacy_changes(changes: dict) -> dict:
    """Edit bodies from the pre-v2 dialog: ``variable_fixed`` stands for
    ``norm_kind`` and ``hands_per_machine`` for ``hands_per_unit`` when the
    v2 fields are not sent (otherwise the stored v2 columns would win the
    merge and the edit would be ignored)."""
    if "variable_fixed" in changes and "norm_kind" not in changes:
        kind = legacy_kind(changes.pop("variable_fixed"))
        if kind is not None:
            changes["norm_kind"] = kind
    if (
        "hands_per_machine" in changes
        and "units_per_hand" not in changes
        and "hands_per_unit" not in changes
    ):
        changes["hands_per_unit"] = changes.pop("hands_per_machine")
    return changes


# =============================================================================
# Setup
# =============================================================================


@router.get("/setup")
def hands_norms_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int(request, "co_id")
    branch_id = _require_int(request, "branch_id")
    try:
        designations = [
            dict(r._mapping)
            for r in db.execute(
                get_norm_designations_query(), {"co_id": co_id, "branch_id": branch_id}
            ).fetchall()
        ]
        machine_types = [
            {**dict(r._mapping), "machine_count": int(r._mapping["machine_count"] or 0)}
            for r in db.execute(
                get_norm_machine_types_query(), {"branch_id": branch_id}
            ).fetchall()
        ]
        machine_groups = [
            _serialise_group(dict(r._mapping))
            for r in db.execute(
                get_machine_groups_query(),
                {"co_id": co_id, "branch_id": branch_id, "include_inactive": 0},
            ).fetchall()
        ]
        machines = [
            dict(r._mapping)
            for r in db.execute(get_norm_machines_query(), {"branch_id": branch_id}).fetchall()
        ]
        departments = [
            dict(r._mapping)
            for r in db.execute(get_norm_departments_query(), {"branch_id": branch_id}).fetchall()
        ]
        return {
            "data": {
                "designations": designations,
                "machine_types": machine_types,
                "machine_groups": machine_groups,
                "machines": machines,
                "departments": departments,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# List
# =============================================================================


@router.get("/list")
def hands_norms_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int(request, "co_id")
    branch_id = _require_int(request, "branch_id")
    include_inactive = 1 if request.query_params.get("include_inactive") in ("1", "true") else 0
    try:
        rows = [
            _serialise(dict(r._mapping))
            for r in db.execute(
                get_hands_norms_list_query(),
                {"co_id": co_id, "branch_id": branch_id, "include_inactive": include_inactive},
            ).fetchall()
        ]
        mark_in_force(rows, date.today())
        for r in rows:
            r["effective_date"] = r["effective_date"].isoformat()
        return {"data": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Create
# =============================================================================


@router.post("/create")
def hands_norms_create(
    body: HandsNormCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = int(body.co_id)
    branch_id = int(body.branch_id)
    _require_level(db, token_data, co_id, branch_id, WRITE_LEVEL)
    try:
        desig = _check_designation(db, co_id, int(body.designation_id))
        row = body.model_dump()
        if row.get("direct_indirect") in (None, ""):
            # Default the D/I class from the designation master, as the report
            # does; a value there that is not D/I is simply left blank.
            di = (desig.get("direct_indirect") or "").strip().upper()[:1]
            row["direct_indirect"] = di if di in DIRECT_INDIRECT else None
        values = normalise_norm(row, strict=True)
        _check_scope(db, co_id, branch_id, values)
        _check_duplicate(
            db, co_id, branch_id, int(body.designation_id), values["effective_date"], None
        )
        result = db.execute(
            insert_hands_norm_query(),
            {
                **values,
                "co_id": co_id,
                "branch_id": branch_id,
                "designation_id": int(body.designation_id),
            },
        )
        db.commit()
        return {"data": {"hands_norm_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Edit
# =============================================================================


@router.put("/edit/{hands_norm_id}")
def hands_norms_edit(
    hands_norm_id: int,
    body: HandsNormUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int(request, "co_id")
    # The row's branch is not known before the read; the page may pass its
    # branch_id to scope the grant, else the user's best grant in the co counts.
    _require_level(db, token_data, co_id, _optional_int(request, "branch_id"), EDIT_LEVEL)
    try:
        existing = db.execute(get_hands_norm_row_query(), {"id": hands_norm_id}).fetchone()
        if not existing or int(existing._mapping["co_id"]) != co_id:
            raise HTTPException(status_code=404, detail=NORM_NOT_FOUND_MSG)
        current = dict(existing._mapping)
        changes = _alias_legacy_changes(body.model_dump(exclude_unset=True))

        if "active" in changes and changes["active"] not in (0, 1):
            raise HTTPException(status_code=400, detail="active must be 0 or 1")
        active = int(changes.get("active", current.get("active") or 0))

        if set(changes) <= {"active"}:
            # A bare retire / restore touches nothing else, so legacy rows keep
            # their imported values exactly.
            if active == 1:
                _check_duplicate(
                    db, co_id, current.get("branch_id"), int(current["designation_id"]),
                    current["effective_date"], hands_norm_id,
                )
            db.execute(
                update_hands_norm_active_query(),
                {"id": hands_norm_id, "co_id": co_id, "active": active},
            )
            db.commit()
            return {"data": {"hands_norm_id": hands_norm_id}}

        merged = {**current, **changes}
        if any(f in changes for f in SCOPE_FIELDS):
            # The dialog's scope radio sends the one it picked; the other two
            # come from the stored row unless the body clears them itself.
            for f in SCOPE_FIELDS:
                if f not in changes:
                    merged[f] = None
        if "hands_per_unit" in changes:
            merged["units_per_hand"] = None  # the alias, not the stored value, wins
        values = normalise_norm(merged, strict=active == 1)
        _check_scope(db, co_id, current.get("branch_id"), values)
        if active == 1:
            _check_duplicate(
                db, co_id, current.get("branch_id"), int(current["designation_id"]),
                values["effective_date"], hands_norm_id,
            )
        db.execute(
            update_hands_norm_query(),
            {**values, "active": active, "id": hands_norm_id, "co_id": co_id},
        )
        db.commit()
        return {"data": {"hands_norm_id": hands_norm_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Machine groups
# =============================================================================


def _normalise_members(members: Optional[list]) -> list:
    """[{machine_id, units}] -> [(machine_id, units)] with units > 0 and no
    repeated machine."""
    out = []
    seen = set()
    for i, mbr in enumerate(members or []):
        m = mbr if isinstance(mbr, dict) else mbr.model_dump()
        try:
            machine_id = int(m.get("machine_id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Member {i + 1}: machine is required")
        if machine_id in seen:
            raise HTTPException(
                status_code=400, detail=f"Machine {machine_id} is listed more than once"
            )
        seen.add(machine_id)
        units = _num(m.get("units", 1), f"Units of machine {machine_id}", MEMBER_UNITS_MAX)
        if units is None:
            units = 1.0
        if units <= 0:
            raise HTTPException(
                status_code=400, detail=f"Units of machine {machine_id} must be greater than 0"
            )
        out.append((machine_id, units))
    return out


def _check_members_in_branch(db: Session, branch_id: int, members: list) -> None:
    """Every member must be an ACTIVE machine of the branch's departments."""
    ids = [mid for mid, _u in members]
    if not ids:
        return
    found = {
        int(r._mapping["machine_id"])
        for r in db.execute(
            get_branch_machines_query(), {"branch_id": branch_id, "machine_ids": ids}
        ).fetchall()
    }
    missing = [mid for mid in ids if mid not in found]
    if missing:
        shown = ", ".join(str(m) for m in missing[:10])
        more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(missing)} machine(s) are not active machines of this branch: "
                f"{shown}{more}"
            ),
        )


def _check_group_code(db: Session, co_id: int, branch_id: int, code: str, exclude_id: Optional[int]) -> None:
    if db.execute(
        get_duplicate_group_code_query(),
        {"co_id": co_id, "branch_id": branch_id, "group_code": code, "exclude_id": exclude_id},
    ).fetchone():
        raise HTTPException(
            status_code=400, detail=f"An active machine group with code '{code}' already exists"
        )


def _group_header_values(row: dict) -> dict:
    return {
        "group_code": _text(row.get("group_code"), "Group code", GROUP_CODE_MAX, required=True),
        "group_name": _text(row.get("group_name"), "Group name", GROUP_NAME_MAX, required=True),
        "unit_label": _text(row.get("unit_label"), "Unit label", UNIT_LABEL_MAX) or DEFAULT_UNIT_LABEL,
        "remarks": _text(row.get("remarks"), "Remarks", GROUP_REMARKS_MAX),
    }


@router.get("/groups")
def machine_groups_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int(request, "co_id")
    branch_id = _require_int(request, "branch_id")
    include_inactive = 1 if request.query_params.get("include_inactive") in ("1", "true") else 0
    try:
        rows = [
            _serialise_group(dict(r._mapping))
            for r in db.execute(
                get_machine_groups_query(),
                {"co_id": co_id, "branch_id": branch_id, "include_inactive": include_inactive},
            ).fetchall()
        ]
        return {"data": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/groups/{machine_group_id}")
def machine_group_get(
    machine_group_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int(request, "co_id")
    try:
        row = db.execute(get_machine_group_row_query(), {"id": machine_group_id}).fetchone()
        if not row or int(row._mapping["co_id"]) != co_id:
            raise HTTPException(status_code=404, detail=GROUP_NOT_FOUND_MSG)
        members = [
            _serialise_member(dict(r._mapping))
            for r in db.execute(
                get_machine_group_members_query(), {"id": machine_group_id}
            ).fetchall()
        ]
        header = _serialise_group(
            {
                **dict(row._mapping),
                "member_count": len(members),
                "total_units": sum(m["units"] for m in members),
                "norms_using": len(
                    db.execute(
                        get_group_norms_using_query(), {"machine_group_id": machine_group_id}
                    ).fetchall()
                ),
            }
        )
        return {"data": {**header, "members": members}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/groups/create")
def machine_group_create(
    body: MachineGroupCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = int(body.co_id)
    branch_id = int(body.branch_id)
    _require_level(db, token_data, co_id, branch_id, WRITE_LEVEL)
    try:
        header = _group_header_values(body.model_dump())
        members = _normalise_members(body.members)
        _check_group_code(db, co_id, branch_id, header["group_code"], None)
        _check_members_in_branch(db, branch_id, members)
        result = db.execute(
            insert_machine_group_query(),
            {
                **header,
                "co_id": co_id,
                "branch_id": branch_id,
                "updated_by": token_data.get("user_id"),
            },
        )
        group_id = result.lastrowid
        for machine_id, units in members:
            db.execute(
                insert_machine_group_dtl_query(),
                {"machine_group_id": group_id, "machine_id": machine_id, "units": units},
            )
        db.commit()
        return {"data": {"machine_group_id": group_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/groups/edit/{machine_group_id}")
def machine_group_edit(
    machine_group_id: int,
    body: MachineGroupUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int(request, "co_id")
    _require_level(db, token_data, co_id, _optional_int(request, "branch_id"), EDIT_LEVEL)
    try:
        existing = db.execute(get_machine_group_row_query(), {"id": machine_group_id}).fetchone()
        if not existing or int(existing._mapping["co_id"]) != co_id:
            raise HTTPException(status_code=404, detail=GROUP_NOT_FOUND_MSG)
        current = dict(existing._mapping)
        branch_id = int(current["branch_id"])
        changes = body.model_dump(exclude_unset=True)

        if "active" in changes and changes["active"] not in (0, 1):
            raise HTTPException(status_code=400, detail="active must be 0 or 1")
        active = int(changes.get("active", current.get("active") or 0))
        if active == 0 and int(current.get("active") or 0) == 1:
            using = [
                dict(r._mapping)
                for r in db.execute(
                    get_group_norms_using_query(), {"machine_group_id": machine_group_id}
                ).fetchall()
            ]
            if using:
                names = ", ".join(str(u.get("desig") or u["hands_norm_id"]) for u in using)
                raise HTTPException(
                    status_code=400,
                    detail=f"{len(using)} active norm(s) use this group: {names}",
                )

        header = _group_header_values({**current, **changes})
        code_changed = header["group_code"] != current.get("group_code")
        restoring = active == 1 and int(current.get("active") or 0) == 0
        if active == 1 and (code_changed or restoring):
            _check_group_code(db, co_id, branch_id, header["group_code"], machine_group_id)
        db.execute(
            update_machine_group_query(),
            {
                **header,
                "active": active,
                "updated_by": token_data.get("user_id"),
                "id": machine_group_id,
                "co_id": co_id,
            },
        )

        if changes.get("members") is not None:
            members = _normalise_members(changes["members"])
            _check_members_in_branch(db, branch_id, members)
            current_rows = {
                int(r._mapping["machine_id"]): dict(r._mapping)
                for r in db.execute(
                    get_machine_group_member_rows_query(), {"id": machine_group_id}
                ).fetchall()
            }
            wanted = dict(members)
            gone = [
                int(r["machine_group_dtl_id"])
                for mid, r in current_rows.items()
                if mid not in wanted
            ]
            if gone:
                db.execute(retire_machine_group_dtl_query(), {"ids": gone})
            for machine_id, units in members:
                cur = current_rows.get(machine_id)
                if cur is None:
                    db.execute(
                        insert_machine_group_dtl_query(),
                        {
                            "machine_group_id": machine_group_id,
                            "machine_id": machine_id,
                            "units": units,
                        },
                    )
                elif float(cur.get("units") or 0) != units:
                    db.execute(
                        update_machine_group_dtl_units_query(),
                        {"machine_group_dtl_id": int(cur["machine_group_dtl_id"]), "units": units},
                    )
        db.commit()
        return {"data": {"machine_group_id": machine_group_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
