"""Raw SQL builders for the Jute Mechanical & Maintenance — Breakdown Entry module.

Backs ``src/juteMechanical/breakdown_entry.py`` (prefix ``/api/mechBreakdown``), the
incident event log that replaces the mill's "MILL SIDE MECHANICAL BREAK DOWN SUMMARY"
workbook. One row of ``jute_mech_breakdown`` per incident; spares consumed hang off it
in ``jute_mech_breakdown_spare``.

Conventions mirror ``src/juteProduction/stoppage_query.py``:

* ``sqlalchemy.text()`` with named binds — bind names match the SQL exactly.
* The ``:x IS NULL OR ...`` optional-filter idiom (callers pass Python ``None``,
  never the string ``"null"``).
* ``machine_mst`` has NO co_id and NO branch_id — branch is reachable only via
  ``machine_mst.dept_id -> dept_mst.branch_id``, so every read COALESCEs the stored
  ``branch_id`` with the department's (legacy-NULL safety net) and every insert
  derives it.
* ``spell_mst`` keys activity by ``status = 1``, NOT ``active`` — and so does its
  parent ``shift_mst``.
* ``dept_mst`` and ``sub_dept_mst`` have NO ``active`` column.
* Soft delete everywhere: ``active = 1`` on reads, ``active = 0`` on delete.

Derived columns (``tran_date``, ``duration_min``, ``response_min``, ``status``) are
computed in Python on save — see the helpers in ``breakdown_entry.py``. The only
place SQL computes a duration is ``get_open_breakdowns_query`` (elapsed-so-far on the
live board), which is a read-time value that has no row to live in.
"""

from sqlalchemy import text


# =============================================================================
# Setup / lookup builders
# =============================================================================


def get_breakdown_machines_query():
    """ALL active machines with type + dept + derived branch (canonical join).

    Not filtered by machine_type_name: a breakdown can be logged against any
    machine in the mill, including utilities and services. branch_id is the
    department's branch — machine_mst has none of its own.
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            m.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id
        FROM machine_mst m
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.active = 1
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY d.dept_desc, m.machine_name
        """
    )


def get_breakdown_spells_query():
    """Active spells with working hours (spell_mst keys on status, NOT active).

    Callers de-duplicate by spell_code — some tenants carry a duplicate code under
    a second branch's shift.
    """
    return text(
        """
        SELECT sp.spell_id, sp.spell_code, sp.spell_name, sp.working_hours
        FROM spell_mst sp
        INNER JOIN shift_mst sh ON sh.shift_id = sp.shift_id
        WHERE sp.status = 1
          AND sh.status = 1
          AND (:branch_id IS NULL OR sh.branch_id = :branch_id)
        ORDER BY sp.starting_time
        """
    )


def get_breakdown_components_query():
    """Active failure components for the company (entry-screen override dropdown)."""
    return text(
        """
        SELECT component_id, component_name, component_group
        FROM jute_mech_component_mst
        WHERE active = 1
          AND co_id = :co_id
        ORDER BY component_name
        """
    )


def get_breakdown_sub_departments_query():
    """Attending crews (MECH FACTORY SIDE / MOTOR / FOUNDRY ...).

    sub_dept_mst has NO active column; branch is reached through its department.
    """
    return text(
        """
        SELECT
            s.sub_dept_id,
            s.sub_dept_code,
            s.sub_dept_desc,
            s.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id
        FROM sub_dept_mst s
        LEFT JOIN dept_mst d ON d.dept_id = s.dept_id
        WHERE (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY s.sub_dept_desc
        """
    )


# =============================================================================
# Machine + reason resolution (validation and derivation)
# =============================================================================


def get_machine_for_validation_query():
    """One active machine: existence check, machine_type_id, and branch derivation.

    Serves three jobs in one round trip — "does this machine exist and is it
    active?", "which reason codes may be logged against it?" (machine_type_id) and
    "which branch does the incident belong to?" (dept_mst.branch_id).
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            m.dept_id,
            d.branch_id
        FROM machine_mst m
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.machine_id = :machine_id
          AND m.active = 1
        """
    )


def get_reason_for_validation_query():
    """One active reason code scoped to the company.

    Returns machine_type_id (NULL = global code), plus the component_id and
    discipline the breakdown row defaults to when the body does not override them.
    The caller enforces "reason belongs to this machine's type OR is global" —
    that gate is what stops a Spreader code being logged against a Draw Head.
    """
    return text(
        """
        SELECT
            reason_id,
            reason_code,
            reason_desc,
            machine_type_id,
            component_id,
            discipline
        FROM jute_mech_reason_mst
        WHERE reason_id = :reason_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def get_reasons_by_machine_query():
    """Reason codes selectable for one machine: its machine type PLUS all globals.

    ``machine_type_id IS NULL`` codes (NR, MT, PP, the electrical/utility set) are
    always offered; type-scoped codes only when they match the machine's type.
    Ordered by reason_code so the shift staff see the list they already know.
    """
    return text(
        """
        SELECT
            r.reason_id,
            r.reason_code,
            r.reason_desc,
            r.discipline,
            r.machine_type_id,
            mt.machine_type_name,
            r.component_id,
            c.component_name,
            c.component_group,
            CASE WHEN r.machine_type_id IS NULL THEN 1 ELSE 0 END AS is_global
        FROM jute_mech_reason_mst r
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = r.machine_type_id
        LEFT JOIN jute_mech_component_mst c ON c.component_id = r.component_id
        WHERE r.active = 1
          AND r.co_id = :co_id
          AND (r.machine_type_id IS NULL OR r.machine_type_id = :machine_type_id)
        ORDER BY r.reason_code
        """
    )


# =============================================================================
# Breakdown reads
# =============================================================================


# Who reported / who fixed / what production said about the fix. Shared by every
# breakdown read so the grid, the edit form and the live board can never disagree.
_TRACK_COLUMNS = """
            b.reported_by_user_id,
            ru.name AS reported_by_user_name,
            b.reported_date_time,
            b.fixed_by_user_id,
            fu.name AS fixed_by_user_name,
            b.fixed_date_time,
            b.prod_confirm_status,
            b.prod_confirm_by,
            pu.name AS prod_confirm_by_name,
            b.prod_confirm_date_time,
            b.prod_restart_datetime,
            b.prod_confirm_remarks"""

_TRACK_JOINS = """
        LEFT JOIN user_mst ru ON ru.user_id = b.reported_by_user_id
        LEFT JOIN user_mst fu ON fu.user_id = b.fixed_by_user_id
        LEFT JOIN user_mst pu ON pu.user_id = b.prod_confirm_by"""

# "The recorded fix is unchanged by this UPDATE" — evaluated against the STORED
# row, so every SET that uses it must come BEFORE status / restart_datetime /
# duration_min are assigned (MySQL applies SET clauses left to right).
_FIX_UNCHANGED = """status = 'CLOSED'
                     AND :status = 'CLOSED'
                     AND restart_datetime <=> :restart_datetime
                     AND duration_min <=> :duration_min"""


_ENTRY_FROM_AND_FILTERS = """
        FROM jute_mech_breakdown b
        INNER JOIN machine_mst m ON m.machine_id = b.machine_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN jute_mech_reason_mst r ON r.reason_id = b.reason_id
        LEFT JOIN jute_mech_component_mst c ON c.component_id = b.component_id
        LEFT JOIN spell_mst sp ON sp.spell_id = b.spell_id
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = b.attended_dept_id""" + _TRACK_JOINS + """
        WHERE b.active = 1
          AND b.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(b.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR b.tran_date >= :date_from)
          AND (:date_to IS NULL OR b.tran_date <= :date_to)
          AND (:machine_id IS NULL OR b.machine_id = :machine_id)
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
          AND (:discipline IS NULL OR b.discipline = :discipline)
          AND (:status IS NULL OR b.status = :status)
"""


def get_breakdown_entries_query():
    """Filtered incident grid, newest stop first, with display names resolved.

    Same FROM/WHERE block as get_breakdown_entries_count_query so the page total
    can never disagree with the page rows.
    """
    return text(
        """
        SELECT
            b.breakdown_id,
            b.co_id,
            COALESCE(b.branch_id, d.branch_id) AS branch_id,
            b.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            m.dept_id,
            d.dept_desc AS dept_name,
            b.tran_date,
            b.spell_id,
            sp.spell_code,
            b.stop_datetime,
            b.attended_datetime,
            b.restart_datetime,
            b.duration_min,
            b.response_min,
            b.reason_id,
            r.reason_code,
            r.reason_desc,
            b.component_id,
            c.component_name,
            c.component_group,
            b.discipline,
            b.downtime_type,
            b.attended_dept_id,
            sd.sub_dept_desc AS attended_dept_name,
            b.attended_by,
            b.reported_by,
            b.root_cause,
            b.action_taken,
            b.preventive_action,
            b.production_loss_qty,
            b.status,
            b.remarks,""" + _TRACK_COLUMNS + """
        """
        + _ENTRY_FROM_AND_FILTERS
        + """
        ORDER BY b.stop_datetime DESC, b.breakdown_id DESC
        LIMIT :limit OFFSET :offset
        """
    )


def get_breakdown_entries_count_query():
    """Total row count for the same filter set (drives {"total": n})."""
    return text("SELECT COUNT(*) AS total" + _ENTRY_FROM_AND_FILTERS)


def get_breakdown_by_id_query():
    """One incident with every display name resolved (header of entry_by_id)."""
    return text(
        """
        SELECT
            b.breakdown_id,
            b.co_id,
            COALESCE(b.branch_id, d.branch_id) AS branch_id,
            b.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            m.dept_id,
            d.dept_desc AS dept_name,
            b.tran_date,
            b.spell_id,
            sp.spell_code,
            b.stop_datetime,
            b.attended_datetime,
            b.restart_datetime,
            b.duration_min,
            b.response_min,
            b.reason_id,
            r.reason_code,
            r.reason_desc,
            b.component_id,
            c.component_name,
            c.component_group,
            b.discipline,
            b.downtime_type,
            b.attended_dept_id,
            sd.sub_dept_desc AS attended_dept_name,
            b.attended_by,
            b.reported_by,
            b.root_cause,
            b.action_taken,
            b.preventive_action,
            b.production_loss_qty,
            b.status,
            b.remarks,""" + _TRACK_COLUMNS + """
        FROM jute_mech_breakdown b
        INNER JOIN machine_mst m ON m.machine_id = b.machine_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN jute_mech_reason_mst r ON r.reason_id = b.reason_id
        LEFT JOIN jute_mech_component_mst c ON c.component_id = b.component_id
        LEFT JOIN spell_mst sp ON sp.spell_id = b.spell_id
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = b.attended_dept_id""" + _TRACK_JOINS + """
        WHERE b.active = 1
          AND b.co_id = :co_id
          AND b.breakdown_id = :breakdown_id
        """
    )


def get_breakdown_active_by_id_query():
    """Raw active header by id + co_id — the edit / delete / close guard.

    Deliberately unjoined: it answers "does this incident exist for this company?"
    and hands back the stored datetimes an edit merges its partial body onto.
    """
    return text(
        """
        SELECT
            breakdown_id, co_id, branch_id, machine_id, tran_date, spell_id,
            stop_datetime, attended_datetime, restart_datetime,
            duration_min, response_min, reason_id, component_id, discipline,
            downtime_type, attended_dept_id, attended_by, reported_by,
            root_cause, action_taken, preventive_action, production_loss_qty,
            status, remarks, prod_confirm_status
        FROM jute_mech_breakdown
        WHERE breakdown_id = :breakdown_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def get_breakdown_spares_query():
    """Active spare lines for one incident, with item / uom names for display."""
    return text(
        """
        SELECT
            s.breakdown_spare_id,
            s.breakdown_id,
            s.item_id,
            i.item_code,
            i.item_name,
            s.qty,
            s.uom_id,
            u.uom_name,
            s.rate,
            s.amount,
            s.remarks
        FROM jute_mech_breakdown_spare s
        LEFT JOIN item_mst i ON i.item_id = s.item_id
        LEFT JOIN uom_mst u ON u.uom_id = s.uom_id
        WHERE s.active = 1
          AND s.breakdown_id = :breakdown_id
        ORDER BY s.breakdown_spare_id
        """
    )


def get_open_breakdowns_query():
    """Live board: incidents never restarted AND carrying no duration, longest first.

    "Open" is both columns being NULL: an incident closed by a hand-entered
    duration_min (no restart time keyed) is a finished repair and must leave the
    board. ``elapsed_min`` is computed at read time against the server clock — an
    open incident has no restart to store a duration against, so this value cannot
    live on the row. Ordered longest-first: the machine that has been down all
    shift is the one the board exists to surface.
    """
    return text(
        """
        SELECT
            b.breakdown_id,
            b.co_id,
            COALESCE(b.branch_id, d.branch_id) AS branch_id,
            b.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            m.dept_id,
            d.dept_desc AS dept_name,
            b.tran_date,
            b.spell_id,
            sp.spell_code,
            b.stop_datetime,
            b.attended_datetime,
            b.response_min,
            b.reason_id,
            r.reason_code,
            r.reason_desc,
            b.component_id,
            c.component_name,
            b.discipline,
            b.downtime_type,
            b.attended_dept_id,
            sd.sub_dept_desc AS attended_dept_name,
            b.attended_by,
            b.reported_by,
            b.status,
            b.remarks,
            b.reported_by_user_id,
            ru.name AS reported_by_user_name,
            b.reported_date_time,
            TIMESTAMPDIFF(MINUTE, b.stop_datetime, NOW()) AS elapsed_min
        FROM jute_mech_breakdown b
        INNER JOIN machine_mst m ON m.machine_id = b.machine_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN jute_mech_reason_mst r ON r.reason_id = b.reason_id
        LEFT JOIN jute_mech_component_mst c ON c.component_id = b.component_id
        LEFT JOIN spell_mst sp ON sp.spell_id = b.spell_id
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = b.attended_dept_id""" + _TRACK_JOINS + """
        WHERE b.active = 1
          AND b.co_id = :co_id
          AND b.restart_datetime IS NULL
          AND b.duration_min IS NULL
          AND (:branch_id IS NULL OR COALESCE(b.branch_id, d.branch_id) = :branch_id)
          AND (:machine_id IS NULL OR b.machine_id = :machine_id)
        ORDER BY elapsed_min DESC, b.stop_datetime ASC
        """
    )


# =============================================================================
# Breakdown writes
# =============================================================================


def insert_breakdown_query():
    """Insert one incident (active = 1). Derived columns arrive already computed.

    The saving user is stamped as the reporter, and also as the fixer when the
    incident is keyed already closed.
    """
    return text(
        """
        INSERT INTO jute_mech_breakdown
            (co_id, branch_id, machine_id, tran_date, spell_id,
             stop_datetime, attended_datetime, restart_datetime,
             duration_min, response_min, reason_id, component_id,
             discipline, downtime_type, attended_dept_id, attended_by,
             reported_by, root_cause, action_taken, preventive_action,
             production_loss_qty, status, remarks, active, updated_by,
             reported_by_user_id, reported_date_time,
             fixed_by_user_id, fixed_date_time)
        VALUES
            (:co_id, :branch_id, :machine_id, :tran_date, :spell_id,
             :stop_datetime, :attended_datetime, :restart_datetime,
             :duration_min, :response_min, :reason_id, :component_id,
             :discipline, :downtime_type, :attended_dept_id, :attended_by,
             :reported_by, :root_cause, :action_taken, :preventive_action,
             :production_loss_qty, :status, :remarks, 1, :updated_by,
             :updated_by, NOW(),
             CASE WHEN :status = 'CLOSED' THEN :updated_by END,
             CASE WHEN :status = 'CLOSED' THEN NOW() END)
        """
    )


def update_breakdown_query():
    """Full-row edit. Every derived column is rewritten from the merged body.

    The reporter stamp is never touched. The fixer stamp follows the fix: set to
    the editing user when this edit closes the incident or changes its restart /
    duration, cleared when the edit reopens it, kept otherwise. Production's
    reconfirmation refers to one specific fix, so it is cleared whenever that fix
    changes or the incident reopens.
    """
    return text(
        """
        UPDATE jute_mech_breakdown
        SET fixed_by_user_id = CASE
                WHEN :status <> 'CLOSED' THEN NULL
                WHEN """ + _FIX_UNCHANGED + """ THEN fixed_by_user_id
                ELSE :updated_by END,
            fixed_date_time = CASE
                WHEN :status <> 'CLOSED' THEN NULL
                WHEN """ + _FIX_UNCHANGED + """ THEN fixed_date_time
                ELSE NOW() END,
            prod_confirm_status = CASE
                WHEN """ + _FIX_UNCHANGED + """ THEN prod_confirm_status END,
            prod_confirm_by = CASE
                WHEN """ + _FIX_UNCHANGED + """ THEN prod_confirm_by END,
            prod_confirm_date_time = CASE
                WHEN """ + _FIX_UNCHANGED + """ THEN prod_confirm_date_time END,
            prod_restart_datetime = CASE
                WHEN """ + _FIX_UNCHANGED + """ THEN prod_restart_datetime END,
            prod_confirm_remarks = CASE
                WHEN """ + _FIX_UNCHANGED + """ THEN prod_confirm_remarks END,
            branch_id           = :branch_id,
            machine_id          = :machine_id,
            tran_date           = :tran_date,
            spell_id            = :spell_id,
            stop_datetime       = :stop_datetime,
            attended_datetime   = :attended_datetime,
            restart_datetime    = :restart_datetime,
            duration_min        = :duration_min,
            response_min        = :response_min,
            reason_id           = :reason_id,
            component_id        = :component_id,
            discipline          = :discipline,
            downtime_type       = :downtime_type,
            attended_dept_id    = :attended_dept_id,
            attended_by         = :attended_by,
            reported_by         = :reported_by,
            root_cause          = :root_cause,
            action_taken        = :action_taken,
            preventive_action   = :preventive_action,
            production_loss_qty = :production_loss_qty,
            status              = :status,
            remarks             = :remarks,
            updated_by          = :updated_by
        WHERE breakdown_id = :breakdown_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def close_breakdown_query():
    """Quick close from the live board: restart + duration + CLOSED.

    ``action_taken`` is COALESCEd so a close that carries no note leaves whatever
    the fitter already recorded intact.
    """
    return text(
        """
        UPDATE jute_mech_breakdown
        SET fixed_by_user_id       = :updated_by,
            fixed_date_time        = NOW(),
            prod_confirm_status    = NULL,
            prod_confirm_by        = NULL,
            prod_confirm_date_time = NULL,
            prod_restart_datetime  = NULL,
            prod_confirm_remarks   = NULL,
            restart_datetime = :restart_datetime,
            duration_min     = :duration_min,
            action_taken     = COALESCE(:action_taken, action_taken),
            status           = :status,
            updated_by       = :updated_by
        WHERE breakdown_id = :breakdown_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def confirm_breakdown_query():
    """Production's reconfirmation of a closed incident (CONFIRMED / DISPUTED).

    Touches only the prod_* columns — mechanical's restart, duration and status
    are left exactly as they were entered. ``status = 'CLOSED'`` in the WHERE is
    the last-line guard against confirming an incident reopened in between.
    """
    return text(
        """
        UPDATE jute_mech_breakdown
        SET prod_confirm_status    = :prod_confirm_status,
            prod_confirm_by        = :updated_by,
            prod_confirm_date_time = NOW(),
            prod_restart_datetime  = :prod_restart_datetime,
            prod_confirm_remarks   = :prod_confirm_remarks
        WHERE breakdown_id = :breakdown_id
          AND co_id = :co_id
          AND active = 1
          AND status = 'CLOSED'
        """
    )


def soft_delete_breakdown_query():
    """Soft delete the header (active = 0)."""
    return text(
        """
        UPDATE jute_mech_breakdown
        SET active = 0, updated_by = :updated_by
        WHERE breakdown_id = :breakdown_id
          AND co_id = :co_id
          AND active = 1
        """
    )


# =============================================================================
# Spare-line writes
# =============================================================================


def insert_breakdown_spare_query():
    """Insert one spare line under an incident (active = 1)."""
    return text(
        """
        INSERT INTO jute_mech_breakdown_spare
            (breakdown_id, item_id, qty, uom_id, rate, amount,
             remarks, active, updated_by)
        VALUES
            (:breakdown_id, :item_id, :qty, :uom_id, :rate, :amount,
             :remarks, 1, :updated_by)
        """
    )


def soft_delete_breakdown_spares_query():
    """Soft delete every spare line of an incident.

    Used by both edit (replace-the-set) and delete (cascade). Soft, not physical,
    so a deleted incident's spare consumption stays auditable.
    """
    return text(
        """
        UPDATE jute_mech_breakdown_spare
        SET active = 0, updated_by = :updated_by
        WHERE breakdown_id = :breakdown_id
          AND active = 1
        """
    )
