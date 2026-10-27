"""Raw SQL builders for the Jute Mechanical & Maintenance — Preventive Maintenance.

A PM execution (``jute_mech_pm_execution`` + ``jute_mech_pm_execution_dtl``) is one
PM visit on one machine plus the per-task result of the checklist worked through on
the day. Header + detail, NO approval workflow — direct save.

House conventions (mirroring ``src/juteProduction/stoppage_query.py``): named binds
only, the ``:x IS NULL OR ...`` optional-filter idiom, branch reached exclusively
via ``machine_mst -> dept_mst.branch_id``, ``COALESCE(e.branch_id, d.branch_id)`` on
read for legacy NULLs, and soft delete (``active = 0``).

THE CHECKLIST
    ``get_pm_checklist_query`` returns template tasks that target the machine
    directly OR its machine type — the two ways ``jute_mech_pm_schedule`` scopes a
    task. It seeds the entry grid; the chosen tasks are then SNAPSHOTTED (task_desc
    copied) into the execution detail so later template edits never rewrite history.

FEEDBACK INTO ``jute_mech_machine_attr``
    ``upsert_machine_attr_pm_query`` maintains ``last_pm_date`` / ``next_pm_due`` on
    the machine's attribute satellite — the row the ``/maintenance-due`` report
    reads. It rides ``uq_mech_attr_co_machine (co_id, machine_id)`` so the attr row
    is created for a machine that has never had one.

Schema source of truth:
dbqueries/migrations/create_jute_mech_maintenance_tables.sql
"""

from sqlalchemy import text


# =============================================================================
# Setup / lookup builders
# =============================================================================


def get_pm_machines_query():
    """All active machines with machine type, department and derived branch.

    Canonical machine join (mirrors stoppage_query.get_stoppage_machines_query):
    branch_id belongs to the DEPARTMENT — machine_mst has none. Unfiltered by
    machine type, because every machine in the mill can be on a PM round.
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.active = 1
          AND mt.active = 1
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY d.dept_desc, m.machine_name
        """
    )


def get_machine_context_query():
    """One machine's identity + machine type + derived branch.

    Serves three jobs on every write: existence/active validation (400 when it
    returns nothing), branch derivation when the body omits branch_id, and the
    machine_type_id the checklist query needs. dept_mst has no active column, so
    only the machine is gated.
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            m.dept_id,
            d.branch_id
        FROM machine_mst m
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.machine_id = :machine_id
          AND m.active = 1
        """
    )


def get_pm_checklist_query():
    """Template tasks for one machine: machine-specific OR machine-type-wide.

    ``jute_mech_pm_schedule`` scopes a task either to a single machine
    (``machine_id``) or to every machine of a type (``machine_type_id``); both
    populations are wanted, hence the OR. ``frequency`` is an optional filter so a
    weekly round shows only the weekly tasks. seq_no is nullable and MySQL sorts
    NULLs first, so COALESCE pushes unsequenced tasks to the end instead of the top.
    """
    return text(
        """
        SELECT
            s.pm_schedule_id,
            s.co_id,
            s.machine_type_id,
            mt.machine_type_name,
            s.machine_id,
            s.task_desc,
            s.frequency,
            s.freq_days,
            s.seq_no,
            s.remarks
        FROM jute_mech_pm_schedule s
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = s.machine_type_id
        WHERE s.active = 1
          AND s.co_id = :co_id
          AND (s.machine_id = :machine_id OR s.machine_type_id = :machine_type_id)
          AND (:frequency IS NULL OR s.frequency = :frequency)
        ORDER BY COALESCE(s.seq_no, 999999), s.pm_schedule_id
        """
    )


def get_pm_schedule_freq_days_query():
    """freq_days configured on the template for one machine + frequency.

    Fallback chain for next_pm_due when the machine's attr row carries no
    pm_freq_days: the template's own resolved day count, then the app-level
    PM_FREQUENCY_DAYS map. MIN() collapses several matching template rows to the
    tightest interval.
    """
    return text(
        """
        SELECT MIN(s.freq_days) AS freq_days
        FROM jute_mech_pm_schedule s
        WHERE s.active = 1
          AND s.co_id = :co_id
          AND (s.machine_id = :machine_id OR s.machine_type_id = :machine_type_id)
          AND (:frequency IS NULL OR s.frequency = :frequency)
          AND s.freq_days IS NOT NULL
        """
    )


# =============================================================================
# Machine attribute feedback (the due-report feed)
# =============================================================================


def get_machine_attr_query():
    """The machine's maintenance attributes, if any row exists.

    Read before a PM execution is saved to pick up ``pm_freq_days``, the interval
    that turns pm_date into the next due date. Not gated on ``active``: the unique
    key spans all rows for (co_id, machine_id), so a soft-deleted row must be found
    rather than duplicated.
    """
    return text(
        """
        SELECT
            machine_attr_id,
            co_id,
            machine_id,
            criticality,
            last_overhaul_date,
            overhaul_freq_months,
            next_overhaul_due,
            last_pm_date,
            pm_freq_days,
            next_pm_due,
            active
        FROM jute_mech_machine_attr
        WHERE co_id = :co_id
          AND machine_id = :machine_id
        """
    )


def get_latest_pm_date_query():
    """The machine's most recent PM date across all its live PM execution rows.

    Used to recompute ``last_pm_date`` after every write, rather than taking the
    date off the row just saved. Editing a back-dated PM (a January visit corrected
    after a June one exists) would otherwise rewind the machine to January and make
    it wrongly reappear as due; deleting the latest PM would leave the date pointing
    at a removed record.
    """
    return text(
        """
        SELECT MAX(pm_date) AS last_pm_date
        FROM jute_mech_pm_execution
        WHERE co_id = :co_id
          AND machine_id = :machine_id
          AND active = 1
        """
    )


def clear_machine_attr_pm_query():
    """Blank the PM feedback when a machine has no live PM execution rows left.

    Only reached when the last remaining PM for the machine is deleted. The attr row
    itself is kept — make/model/criticality and the overhaul fields are unrelated to
    PM history.
    """
    return text(
        """
        UPDATE jute_mech_machine_attr
           SET last_pm_date = NULL,
               next_pm_due  = NULL,
               updated_by   = :updated_by
         WHERE co_id = :co_id
           AND machine_id = :machine_id
        """
    )


def upsert_machine_attr_pm_query():
    """Write PM feedback onto the machine's attribute satellite.

    Rides ``uq_mech_attr_co_machine (co_id, machine_id)``: inserts the attr row for
    a machine that has never had one, otherwise updates only the two PM columns and
    leaves make/model/criticality/overhaul fields untouched. ``active = 1`` is
    re-asserted so a previously soft-deleted attr row rejoins the due-report
    population — PM was actually performed on this machine.
    """
    return text(
        """
        INSERT INTO jute_mech_machine_attr
            (co_id, machine_id, last_pm_date, next_pm_due, active, updated_by)
        VALUES
            (:co_id, :machine_id, :last_pm_date, :next_pm_due, 1, :updated_by)
        ON DUPLICATE KEY UPDATE
            last_pm_date = VALUES(last_pm_date),
            next_pm_due  = VALUES(next_pm_due),
            active       = 1,
            updated_by   = VALUES(updated_by)
        """
    )


# =============================================================================
# PM execution header — reads
# =============================================================================


def get_pm_execution_table_query():
    """Paginated PM execution list with machine / department names resolved.

    Optional filters (branch_id, date range on pm_date, machine_id) all use the
    ``:x IS NULL OR`` idiom so one builder serves every combination.
    """
    return text(
        """
        SELECT
            e.pm_exec_id,
            e.co_id,
            COALESCE(e.branch_id, d.branch_id) AS branch_id,
            e.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            e.pm_date,
            e.frequency,
            e.done_by,
            e.downtime_min,
            e.next_due_date,
            e.remarks,
            (SELECT COUNT(*)
               FROM jute_mech_pm_execution_dtl t
              WHERE t.pm_exec_id = e.pm_exec_id AND t.active = 1) AS task_count,
            (SELECT COUNT(*)
               FROM jute_mech_pm_execution_dtl t
              WHERE t.pm_exec_id = e.pm_exec_id AND t.active = 1
                AND t.result = 'NOT_OK') AS not_ok_count
        FROM jute_mech_pm_execution e
        INNER JOIN machine_mst m ON m.machine_id = e.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        WHERE e.active = 1
          AND e.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(e.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR e.pm_date >= :date_from)
          AND (:date_to IS NULL OR e.pm_date <= :date_to)
          AND (:machine_id IS NULL OR e.machine_id = :machine_id)
        ORDER BY e.pm_date DESC, e.pm_exec_id DESC
        LIMIT :limit OFFSET :offset
        """
    )


def get_pm_execution_table_count_query():
    """Row count for /pm_execution_table — identical filters, no LIMIT/ORDER BY."""
    return text(
        """
        SELECT COUNT(*) AS total
        FROM jute_mech_pm_execution e
        INNER JOIN machine_mst m ON m.machine_id = e.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE e.active = 1
          AND e.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(e.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR e.pm_date >= :date_from)
          AND (:date_to IS NULL OR e.pm_date <= :date_to)
          AND (:machine_id IS NULL OR e.machine_id = :machine_id)
        """
    )


def get_pm_execution_by_id_query():
    """Full header for one PM execution, display names resolved."""
    return text(
        """
        SELECT
            e.pm_exec_id,
            e.co_id,
            COALESCE(e.branch_id, d.branch_id) AS branch_id,
            e.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            e.pm_date,
            e.frequency,
            e.done_by,
            e.downtime_min,
            e.next_due_date,
            e.remarks
        FROM jute_mech_pm_execution e
        INNER JOIN machine_mst m ON m.machine_id = e.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        WHERE e.pm_exec_id = :pm_exec_id
          AND e.co_id = :co_id
          AND e.active = 1
        """
    )


def get_pm_execution_active_by_id_query():
    """Lean active-row guard for edit / delete (no joins)."""
    return text(
        """
        SELECT
            pm_exec_id,
            co_id,
            branch_id,
            machine_id,
            pm_date,
            frequency,
            done_by,
            downtime_min,
            next_due_date
        FROM jute_mech_pm_execution
        WHERE pm_exec_id = :pm_exec_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def get_pm_execution_tasks_query():
    """Active task rows of one PM execution.

    task_desc is the snapshot stored at execution time; the template's current
    wording is exposed alongside as ``schedule_task_desc`` for reference only —
    reports and history read the snapshot.
    """
    return text(
        """
        SELECT
            t.pm_exec_dtl_id,
            t.pm_exec_id,
            t.pm_schedule_id,
            t.task_desc,
            t.result,
            t.observation,
            s.task_desc AS schedule_task_desc,
            s.frequency AS schedule_frequency,
            s.seq_no
        FROM jute_mech_pm_execution_dtl t
        LEFT JOIN jute_mech_pm_schedule s ON s.pm_schedule_id = t.pm_schedule_id
        WHERE t.pm_exec_id = :pm_exec_id
          AND t.active = 1
        ORDER BY COALESCE(s.seq_no, 999999), t.pm_exec_dtl_id
        """
    )


# =============================================================================
# PM execution header + detail — writes
# =============================================================================


def insert_pm_execution_query():
    """Insert one PM execution header (active = 1).

    next_due_date is computed in Python before this runs — the DB derives nothing.
    """
    return text(
        """
        INSERT INTO jute_mech_pm_execution
            (co_id, branch_id, machine_id, pm_date, frequency, done_by,
             downtime_min, next_due_date, remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :machine_id, :pm_date, :frequency, :done_by,
             :downtime_min, :next_due_date, :remarks, 1, :updated_by)
        """
    )


def update_pm_execution_query():
    """Full header replace on edit, with next_due_date recomputed."""
    return text(
        """
        UPDATE jute_mech_pm_execution
        SET branch_id     = :branch_id,
            machine_id    = :machine_id,
            pm_date       = :pm_date,
            frequency     = :frequency,
            done_by       = :done_by,
            downtime_min  = :downtime_min,
            next_due_date = :next_due_date,
            remarks       = :remarks,
            updated_by    = :updated_by
        WHERE pm_exec_id = :pm_exec_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def soft_delete_pm_execution_query():
    """Soft delete the header (active = 0)."""
    return text(
        """
        UPDATE jute_mech_pm_execution
        SET active = 0, updated_by = :updated_by
        WHERE pm_exec_id = :pm_exec_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def insert_pm_execution_dtl_query():
    """Insert one task result, snapshotting task_desc at execution time.

    pm_schedule_id is nullable: an ad-hoc task done on the day that is not in the
    template is still recorded, carrying only its typed description.
    """
    return text(
        """
        INSERT INTO jute_mech_pm_execution_dtl
            (pm_exec_id, pm_schedule_id, task_desc, result, observation, active, updated_by)
        VALUES
            (:pm_exec_id, :pm_schedule_id, :task_desc, :result, :observation, 1, :updated_by)
        """
    )


def soft_delete_pm_execution_dtl_query():
    """Soft delete every live task row of a PM execution.

    Used by /pm_execution_delete (cascade) and by /pm_execution_edit, which replaces
    the task set: the old rows are retired here, the new set inserted after.
    """
    return text(
        """
        UPDATE jute_mech_pm_execution_dtl
        SET active = 0, updated_by = :updated_by
        WHERE pm_exec_id = :pm_exec_id
          AND active = 1
        """
    )


def get_pm_schedule_task_desc_query():
    """Template wording for a pm_schedule_id, for snapshotting when none is posted.

    Scoped by co_id so one company cannot snapshot another's template text.
    """
    return text(
        """
        SELECT s.pm_schedule_id, s.task_desc, s.frequency, s.freq_days
        FROM jute_mech_pm_schedule s
        WHERE s.pm_schedule_id = :pm_schedule_id
          AND s.co_id = :co_id
        """
    )
