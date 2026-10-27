"""Raw SQL builders for the Jute Mechanical & Maintenance MASTERS endpoints.

Backs ``src/juteMechanical/masters.py`` (prefix ``/api/mechMaintMasters``) — the
four maintenance masters plus the shared dropdown payload:

    jute_mech_component_mst   failure component (cross-section Pareto key)
    jute_mech_reason_mst      breakdown reason codes, scoped by machine type
    jute_mech_machine_attr    maintenance satellite on machine_mst
    jute_mech_pm_schedule     preventive-maintenance checklist template

Conventions mirror ``src/juteProduction/stoppage_query.py``: ``sqlalchemy.text()``
with named binds and the ``(:x IS NULL OR ...)`` optional-filter idiom.

REUSED masters — this module creates none of them:

    machine_mst       has NO co_id and NO branch_id; branch is reachable ONLY via
                      machine_mst m INNER JOIN dept_mst d ON d.dept_id = m.dept_id
                      -> d.branch_id  (the canonical derivation used mill-wide)
    machine_type_mst  active = 1
    dept_mst          NO active column — never filter one
    sub_dept_mst      NO active column — never filter one
    spell_mst         keys activity by status = 1, NOT active = 1

Uniqueness notes that shape the dup-check builders: the DB unique keys
``uq_mech_reason_co_type_code`` / ``uq_mech_component_co_name`` /
``uq_mech_attr_co_machine`` apply regardless of ``active``, so every dup check
here deliberately scans soft-deleted rows too — filtering them out would turn a
clean 400 into a duplicate-key 500.

Schema source of truth:
dbqueries/migrations/create_jute_mech_maintenance_tables.sql
ORM: src/juteMechanical/models.py
"""

from sqlalchemy import text


# =============================================================================
# SETUP / LOOKUP BUILDERS  (/masters_setup and the table-screen filters)
# =============================================================================


def get_machines_query():
    """All active machines with type + dept + DERIVED branch.

    machine_mst carries no branch_id, so ``d.branch_id`` (nullable) is the only
    branch available. Optional filters: branch_id, machine_type_id.
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
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
        ORDER BY d.dept_desc, m.machine_name
        """
    )


def get_machine_types_query():
    """Active machine types (== the Excel "section")."""
    return text(
        """
        SELECT mt.machine_type_id, mt.machine_type_name
        FROM machine_type_mst mt
        WHERE mt.active = 1
        ORDER BY mt.machine_type_name
        """
    )


def get_departments_query():
    """Departments (dept_mst has NO active column — do not filter one)."""
    return text(
        """
        SELECT d.dept_id, d.dept_desc AS dept_name, d.dept_code, d.branch_id
        FROM dept_mst d
        WHERE (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY d.dept_desc
        """
    )


def get_sub_departments_query():
    """Attending crews (MECH FACTORY SIDE / MOTOR / FOUNDRY ...).

    sub_dept_mst has NO active column; branch is resolved through the parent
    department.
    """
    return text(
        """
        SELECT
            sd.sub_dept_id,
            sd.sub_dept_code,
            sd.sub_dept_desc AS sub_dept_name,
            sd.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id
        FROM sub_dept_mst sd
        LEFT JOIN dept_mst d ON d.dept_id = sd.dept_id
        WHERE (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY d.dept_desc, sd.sub_dept_desc
        """
    )


def get_spells_query():
    """Active spells with working hours. spell_mst keys activity by status = 1.

    Callers de-duplicate by spell_code (some tenants carry a duplicate code under
    a second branch's shift).
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


def get_components_lookup_query():
    """Active components for a company (dropdown payload)."""
    return text(
        """
        SELECT c.component_id, c.component_name, c.component_group
        FROM jute_mech_component_mst c
        WHERE c.co_id = :co_id AND c.active = 1
        ORDER BY c.component_name
        """
    )


# =============================================================================
# REASON MASTER  (jute_mech_reason_mst)
# =============================================================================


def get_reason_list_query():
    """Reason table with machine-type and component display names.

    LEFT JOINs on both lookups: machine_type_id NULL means a GLOBAL code and
    component_id is optional, so an INNER JOIN would silently hide rows.
    """
    return text(
        """
        SELECT
            r.reason_id,
            r.co_id,
            r.machine_type_id,
            mt.machine_type_name,
            r.reason_code,
            r.reason_desc,
            r.discipline,
            r.component_id,
            c.component_name,
            c.component_group,
            r.remarks,
            r.active,
            r.updated_by,
            r.updated_date_time
        FROM jute_mech_reason_mst r
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = r.machine_type_id
        LEFT JOIN jute_mech_component_mst c ON c.component_id = r.component_id
        WHERE r.co_id = :co_id
          AND r.active = 1
          AND (:machine_type_id IS NULL OR r.machine_type_id = :machine_type_id)
          AND (:discipline IS NULL OR r.discipline = :discipline)
          AND (:search IS NULL
               OR r.reason_code LIKE :search
               OR r.reason_desc LIKE :search)
        ORDER BY mt.machine_type_name, r.reason_code
        """
    )


def get_reason_by_id_query():
    """One reason row (any active state) with display names."""
    return text(
        """
        SELECT
            r.reason_id,
            r.co_id,
            r.machine_type_id,
            mt.machine_type_name,
            r.reason_code,
            r.reason_desc,
            r.discipline,
            r.component_id,
            c.component_name,
            r.remarks,
            r.active,
            r.updated_by,
            r.updated_date_time
        FROM jute_mech_reason_mst r
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = r.machine_type_id
        LEFT JOIN jute_mech_component_mst c ON c.component_id = r.component_id
        WHERE r.reason_id = :reason_id AND r.co_id = :co_id
        """
    )


def check_reason_duplicate_query():
    """Dup check against uq_mech_reason_co_type_code (co_id, machine_type_id,
    reason_code).

    Scans soft-deleted rows too (the unique key does), and matches NULL
    machine_type_id explicitly — ``= NULL`` is never true in SQL. Pass
    :reason_id = NULL on create, the row's own id on edit.
    """
    return text(
        """
        SELECT reason_id, active
        FROM jute_mech_reason_mst
        WHERE co_id = :co_id
          AND reason_code = :reason_code
          AND ((:machine_type_id IS NULL AND machine_type_id IS NULL)
               OR machine_type_id = :machine_type_id)
          AND (:reason_id IS NULL OR reason_id <> :reason_id)
        LIMIT 1
        """
    )


def insert_reason_query():
    return text(
        """
        INSERT INTO jute_mech_reason_mst
            (co_id, machine_type_id, reason_code, reason_desc, discipline,
             component_id, remarks, active, updated_by, updated_date_time)
        VALUES
            (:co_id, :machine_type_id, :reason_code, :reason_desc, :discipline,
             :component_id, :remarks, :active, :updated_by, :updated_date_time)
        """
    )


def update_reason_query():
    return text(
        """
        UPDATE jute_mech_reason_mst
        SET machine_type_id   = :machine_type_id,
            reason_code       = :reason_code,
            reason_desc       = :reason_desc,
            discipline        = :discipline,
            component_id      = :component_id,
            remarks           = :remarks,
            active            = :active,
            updated_by        = :updated_by,
            updated_date_time = :updated_date_time
        WHERE reason_id = :reason_id AND co_id = :co_id
        """
    )


def soft_delete_reason_query():
    return text(
        """
        UPDATE jute_mech_reason_mst
        SET active            = 0,
            updated_by        = :updated_by,
            updated_date_time = :updated_date_time
        WHERE reason_id = :reason_id AND co_id = :co_id
        """
    )


def count_breakdowns_for_reason_query():
    """FK-usage guard for reason delete."""
    return text(
        """
        SELECT COUNT(*) AS cnt
        FROM jute_mech_breakdown
        WHERE reason_id = :reason_id AND active = 1
        """
    )


# =============================================================================
# COMPONENT MASTER  (jute_mech_component_mst)
# =============================================================================


def get_component_list_query():
    return text(
        """
        SELECT
            c.component_id,
            c.co_id,
            c.component_name,
            c.component_group,
            c.remarks,
            c.active,
            c.updated_by,
            c.updated_date_time
        FROM jute_mech_component_mst c
        WHERE c.co_id = :co_id
          AND c.active = 1
          AND (:component_group IS NULL OR c.component_group = :component_group)
          AND (:search IS NULL
               OR c.component_name LIKE :search
               OR c.component_group LIKE :search)
        ORDER BY c.component_group, c.component_name
        """
    )


def get_component_by_id_query():
    return text(
        """
        SELECT
            c.component_id,
            c.co_id,
            c.component_name,
            c.component_group,
            c.remarks,
            c.active,
            c.updated_by,
            c.updated_date_time
        FROM jute_mech_component_mst c
        WHERE c.component_id = :component_id AND c.co_id = :co_id
        """
    )


def check_component_duplicate_query():
    """Dup check against uq_mech_component_co_name (co_id, component_name).

    Scans soft-deleted rows too. Pass :component_id = NULL on create.
    """
    return text(
        """
        SELECT component_id, active
        FROM jute_mech_component_mst
        WHERE co_id = :co_id
          AND component_name = :component_name
          AND (:component_id IS NULL OR component_id <> :component_id)
        LIMIT 1
        """
    )


def insert_component_query():
    return text(
        """
        INSERT INTO jute_mech_component_mst
            (co_id, component_name, component_group, remarks,
             active, updated_by, updated_date_time)
        VALUES
            (:co_id, :component_name, :component_group, :remarks,
             :active, :updated_by, :updated_date_time)
        """
    )


def update_component_query():
    return text(
        """
        UPDATE jute_mech_component_mst
        SET component_name    = :component_name,
            component_group   = :component_group,
            remarks           = :remarks,
            active            = :active,
            updated_by        = :updated_by,
            updated_date_time = :updated_date_time
        WHERE component_id = :component_id AND co_id = :co_id
        """
    )


def soft_delete_component_query():
    return text(
        """
        UPDATE jute_mech_component_mst
        SET active            = 0,
            updated_by        = :updated_by,
            updated_date_time = :updated_date_time
        WHERE component_id = :component_id AND co_id = :co_id
        """
    )


def count_reasons_for_component_query():
    """FK-usage guard #1 for component delete (reason tagged with it)."""
    return text(
        """
        SELECT COUNT(*) AS cnt
        FROM jute_mech_reason_mst
        WHERE component_id = :component_id AND active = 1
        """
    )


def count_breakdowns_for_component_query():
    """FK-usage guard #2 for component delete (breakdown tagged with it)."""
    return text(
        """
        SELECT COUNT(*) AS cnt
        FROM jute_mech_breakdown
        WHERE component_id = :component_id AND active = 1
        """
    )


# =============================================================================
# MACHINE MAINTENANCE ATTRIBUTES  (jute_mech_machine_attr)
# =============================================================================


def get_machine_attr_list_query():
    """Machine-centric LEFT JOIN: EVERY active machine, attributes when present.

    Driven from machine_mst, not from the attr table — the mill has 600+ machines
    and only some will ever have maintenance attributes filled in, and the screen
    exists precisely to fill the blanks. Rows without an attr row come back with
    machine_attr_id and all attr columns NULL.

    The attr side is joined on co_id + active INSIDE the ON clause; moving either
    into WHERE would convert the LEFT JOIN back into an inner join.
    """
    return text(
        """
        SELECT
            a.machine_attr_id,
            m.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id,
            a.co_id,
            a.make,
            a.model,
            a.serial_no,
            a.commissioning_date,
            a.criticality,
            a.last_overhaul_date,
            a.overhaul_freq_months,
            a.next_overhaul_due,
            a.last_pm_date,
            a.pm_freq_days,
            a.next_pm_due,
            a.remarks,
            a.active,
            a.updated_by,
            a.updated_date_time
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN jute_mech_machine_attr a
               ON a.machine_id = m.machine_id
              AND a.co_id = :co_id
              AND a.active = 1
        WHERE m.active = 1
          AND mt.active = 1
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
          AND (:search IS NULL
               OR m.machine_name LIKE :search
               OR m.mech_code LIKE :search
               OR a.make LIKE :search
               OR a.model LIKE :search
               OR a.serial_no LIKE :search)
        ORDER BY mt.machine_type_name, m.machine_name
        """
    )


def get_machine_attr_by_id_query():
    """One attr row by PK, with the machine it hangs off resolved for display."""
    return text(
        """
        SELECT
            a.machine_attr_id,
            a.co_id,
            a.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id,
            a.make,
            a.model,
            a.serial_no,
            a.commissioning_date,
            a.criticality,
            a.last_overhaul_date,
            a.overhaul_freq_months,
            a.next_overhaul_due,
            a.last_pm_date,
            a.pm_freq_days,
            a.next_pm_due,
            a.remarks,
            a.active,
            a.updated_by,
            a.updated_date_time
        FROM jute_mech_machine_attr a
        INNER JOIN machine_mst m ON m.machine_id = a.machine_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE a.machine_attr_id = :machine_attr_id AND a.co_id = :co_id
        """
    )


def get_machine_attr_by_machine_query():
    """Upsert probe for uq_mech_attr_co_machine (co_id, machine_id).

    Ignores ``active`` on purpose: the unique key does, so a soft-deleted row must
    be found and revived rather than re-inserted into a duplicate-key error.
    """
    return text(
        """
        SELECT machine_attr_id, active
        FROM jute_mech_machine_attr
        WHERE co_id = :co_id AND machine_id = :machine_id
        LIMIT 1
        """
    )


def machine_exists_query():
    """Machine existence + derived branch (FK sanity before writing an attr row)."""
    return text(
        """
        SELECT m.machine_id, m.machine_name, d.branch_id
        FROM machine_mst m
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.machine_id = :machine_id
        """
    )


def insert_machine_attr_query():
    return text(
        """
        INSERT INTO jute_mech_machine_attr
            (co_id, machine_id, make, model, serial_no, commissioning_date,
             criticality, last_overhaul_date, overhaul_freq_months, next_overhaul_due,
             last_pm_date, pm_freq_days, next_pm_due, remarks,
             active, updated_by, updated_date_time)
        VALUES
            (:co_id, :machine_id, :make, :model, :serial_no, :commissioning_date,
             :criticality, :last_overhaul_date, :overhaul_freq_months, :next_overhaul_due,
             :last_pm_date, :pm_freq_days, :next_pm_due, :remarks,
             :active, :updated_by, :updated_date_time)
        """
    )


def update_machine_attr_query():
    """Full-row update by PK (also used as the UPDATE half of the upsert)."""
    return text(
        """
        UPDATE jute_mech_machine_attr
        SET machine_id           = :machine_id,
            make                 = :make,
            model                = :model,
            serial_no            = :serial_no,
            commissioning_date   = :commissioning_date,
            criticality          = :criticality,
            last_overhaul_date   = :last_overhaul_date,
            overhaul_freq_months = :overhaul_freq_months,
            next_overhaul_due    = :next_overhaul_due,
            last_pm_date         = :last_pm_date,
            pm_freq_days         = :pm_freq_days,
            next_pm_due          = :next_pm_due,
            remarks              = :remarks,
            active               = :active,
            updated_by           = :updated_by,
            updated_date_time    = :updated_date_time
        WHERE machine_attr_id = :machine_attr_id AND co_id = :co_id
        """
    )


def soft_delete_machine_attr_query():
    return text(
        """
        UPDATE jute_mech_machine_attr
        SET active            = 0,
            updated_by        = :updated_by,
            updated_date_time = :updated_date_time
        WHERE machine_attr_id = :machine_attr_id AND co_id = :co_id
        """
    )


# =============================================================================
# PM SCHEDULE  (jute_mech_pm_schedule)
# =============================================================================


def get_pm_schedule_list_query():
    """Checklist template rows with machine-type / machine display names.

    Both scoping columns are nullable (type-wide, machine-specific, or global), so
    both lookups are LEFT JOINs.
    """
    return text(
        """
        SELECT
            s.pm_schedule_id,
            s.co_id,
            s.machine_type_id,
            mt.machine_type_name,
            s.machine_id,
            m.machine_name,
            m.mech_code,
            s.task_desc,
            s.frequency,
            s.freq_days,
            s.seq_no,
            s.remarks,
            s.active,
            s.updated_by,
            s.updated_date_time
        FROM jute_mech_pm_schedule s
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = s.machine_type_id
        LEFT JOIN machine_mst m ON m.machine_id = s.machine_id
        WHERE s.co_id = :co_id
          AND s.active = 1
          AND (:machine_type_id IS NULL OR s.machine_type_id = :machine_type_id)
          AND (:machine_id IS NULL OR s.machine_id = :machine_id)
          AND (:frequency IS NULL OR s.frequency = :frequency)
          AND (:search IS NULL
               OR s.task_desc LIKE :search
               OR s.remarks LIKE :search)
        ORDER BY mt.machine_type_name, m.machine_name, s.seq_no, s.pm_schedule_id
        """
    )


def get_pm_schedule_by_id_query():
    return text(
        """
        SELECT
            s.pm_schedule_id,
            s.co_id,
            s.machine_type_id,
            mt.machine_type_name,
            s.machine_id,
            m.machine_name,
            m.mech_code,
            s.task_desc,
            s.frequency,
            s.freq_days,
            s.seq_no,
            s.remarks,
            s.active,
            s.updated_by,
            s.updated_date_time
        FROM jute_mech_pm_schedule s
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = s.machine_type_id
        LEFT JOIN machine_mst m ON m.machine_id = s.machine_id
        WHERE s.pm_schedule_id = :pm_schedule_id AND s.co_id = :co_id
        """
    )


def insert_pm_schedule_query():
    return text(
        """
        INSERT INTO jute_mech_pm_schedule
            (co_id, machine_type_id, machine_id, task_desc, frequency, freq_days,
             seq_no, remarks, active, updated_by, updated_date_time)
        VALUES
            (:co_id, :machine_type_id, :machine_id, :task_desc, :frequency, :freq_days,
             :seq_no, :remarks, :active, :updated_by, :updated_date_time)
        """
    )


def update_pm_schedule_query():
    return text(
        """
        UPDATE jute_mech_pm_schedule
        SET machine_type_id   = :machine_type_id,
            machine_id        = :machine_id,
            task_desc         = :task_desc,
            frequency         = :frequency,
            freq_days         = :freq_days,
            seq_no            = :seq_no,
            remarks           = :remarks,
            active            = :active,
            updated_by        = :updated_by,
            updated_date_time = :updated_date_time
        WHERE pm_schedule_id = :pm_schedule_id AND co_id = :co_id
        """
    )


def soft_delete_pm_schedule_query():
    return text(
        """
        UPDATE jute_mech_pm_schedule
        SET active            = 0,
            updated_by        = :updated_by,
            updated_date_time = :updated_date_time
        WHERE pm_schedule_id = :pm_schedule_id AND co_id = :co_id
        """
    )
