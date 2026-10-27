"""Raw SQL builders for the Jute Mechanical & Maintenance — Overhaul register.

The overhaul register (``jute_mech_overhaul`` + ``jute_mech_overhaul_spare``) is a
header + detail pair with NO approval workflow — direct save. Every builder here is
a ``sqlalchemy.text()`` object with named binds, matching the house conventions in
``src/juteProduction/stoppage_query.py``:

  * the ``:x IS NULL OR ...`` optional-filter idiom (never string-built WHERE),
  * ``machine_mst -> dept_mst.branch_id`` as the ONLY way to reach a branch
    (machine_mst carries no co_id and no branch_id of its own),
  * ``COALESCE(o.branch_id, d.branch_id)`` on read so a legacy NULL branch on the
    stored row still resolves through the machine's department,
  * soft delete (``active = 0``) — rows are never physically removed.

FEEDBACK INTO ``jute_mech_machine_attr``
    ``upsert_machine_attr_overhaul_query`` is the reason this module matters beyond
    its own register: it maintains ``last_overhaul_date`` / ``next_overhaul_due`` on
    the machine's maintenance-attribute satellite, which is what the
    ``/maintenance-due`` report reads. The upsert rides the
    ``uq_mech_attr_co_machine (co_id, machine_id)`` unique key, so it creates the
    attr row when the machine has never had one — the common case, since most
    machines carry no attr row until their first overhaul or PM.

Schema source of truth:
dbqueries/migrations/create_jute_mech_maintenance_tables.sql
"""

from sqlalchemy import text


# =============================================================================
# Setup / lookup builders
# =============================================================================


def get_overhaul_machines_query():
    """All active machines with machine type, department and derived branch.

    Canonical machine join (mirrors stoppage_query.get_stoppage_machines_query):
    branch_id is the DEPARTMENT's branch — machine_mst has none of its own. Not
    filtered by machine_type_name, because any machine in the mill can be
    overhauled (utilities and services included).
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


def get_overhaul_parties_query():
    """Active parties for the overhaul vendor dropdown (done_by = VENDOR).

    party_mst.co_id is nullable in several tenants, so rows with a NULL company are
    treated as shared across companies rather than hidden.
    """
    return text(
        """
        SELECT
            p.party_id,
            p.supp_name  AS party_name,
            p.supp_code  AS party_code
        FROM party_mst p
        WHERE p.active = 1
          AND (p.co_id = :co_id OR p.co_id IS NULL)
        ORDER BY p.supp_name
        """
    )


def get_machine_context_query():
    """One machine's identity + derived branch — validation AND branch derivation.

    Used on every write: a 400 when this returns nothing (unknown or inactive
    machine), and ``branch_id`` from it when the body omits one. dept_mst has no
    active column, so only the machine is gated on ``active = 1``.
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


def get_party_active_query():
    """Existence guard for a vendor party (done_by = VENDOR)."""
    return text(
        """
        SELECT p.party_id
        FROM party_mst p
        WHERE p.party_id = :party_id
          AND p.active = 1
          AND (p.co_id = :co_id OR p.co_id IS NULL)
        """
    )


# =============================================================================
# Machine attribute feedback (the due-report feed)
# =============================================================================


def get_machine_attr_query():
    """The machine's maintenance attributes, if a row exists at all.

    Read before an overhaul is saved to pick up ``overhaul_freq_months``, the
    interval that turns this overhaul's end date into the next due date. Not gated
    on ``active`` — the unique key spans every row for (co_id, machine_id), so a
    soft-deleted row must still be found rather than duplicated.
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


def get_latest_overhaul_date_query():
    """The machine's most recent overhaul date across all its live overhaul rows.

    Used to recompute ``last_overhaul_date`` after every write, rather than taking
    the date off the row just saved. Editing a back-dated overhaul (say a January
    one, after a June one already exists) would otherwise rewind the machine's
    ``last_overhaul_date`` to January and make it wrongly reappear as due; deleting
    the latest overhaul would leave the date pointing at a removed record.

    ``COALESCE(to_date, from_date)`` because an overhaul still in progress has no
    end date yet but has certainly started.
    """
    return text(
        """
        SELECT MAX(COALESCE(to_date, from_date)) AS last_overhaul_date
        FROM jute_mech_overhaul
        WHERE co_id = :co_id
          AND machine_id = :machine_id
          AND active = 1
        """
    )


def clear_machine_attr_overhaul_query():
    """Blank the overhaul feedback when a machine has no live overhaul rows left.

    Only reached when the last remaining overhaul for the machine is deleted. The
    attr row itself is kept — make/model/criticality/PM fields are unrelated to
    overhaul history and must survive.
    """
    return text(
        """
        UPDATE jute_mech_machine_attr
           SET last_overhaul_date = NULL,
               next_overhaul_due  = NULL,
               updated_by         = :updated_by
         WHERE co_id = :co_id
           AND machine_id = :machine_id
        """
    )


def upsert_machine_attr_overhaul_query():
    """Write overhaul feedback onto the machine's attribute satellite.

    Rides ``uq_mech_attr_co_machine (co_id, machine_id)``: inserts the attr row for
    a machine that has never had one, otherwise updates only the two overhaul
    columns and leaves make/model/criticality/PM fields untouched. ``active = 1``
    is re-asserted so a previously soft-deleted attr row comes back into the
    due-report population — an overhaul was actually performed on this machine, so
    its maintenance record must be live.
    """
    return text(
        """
        INSERT INTO jute_mech_machine_attr
            (co_id, machine_id, last_overhaul_date, next_overhaul_due, active, updated_by)
        VALUES
            (:co_id, :machine_id, :last_overhaul_date, :next_overhaul_due, 1, :updated_by)
        ON DUPLICATE KEY UPDATE
            last_overhaul_date = VALUES(last_overhaul_date),
            next_overhaul_due  = VALUES(next_overhaul_due),
            active             = 1,
            updated_by         = VALUES(updated_by)
        """
    )


# =============================================================================
# Overhaul header — reads
# =============================================================================


def get_overhaul_table_query():
    """Paginated overhaul register with machine / department / vendor names resolved.

    Every filter is optional except co_id and uses the ``:x IS NULL OR`` idiom, so
    one builder serves all filter combinations. The date range is applied to
    ``from_date`` (when the overhaul started) — that is what the register is
    ordered and reported by. work_done is TEXT and deliberately omitted from the
    list payload; it is returned by /overhaul_by_id.
    """
    return text(
        """
        SELECT
            o.overhaul_id,
            o.co_id,
            COALESCE(o.branch_id, d.branch_id) AS branch_id,
            o.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            o.overhaul_type,
            o.from_date,
            o.to_date,
            o.downtime_days,
            o.done_by,
            o.party_id,
            p.supp_name AS party_name,
            o.labour_cost,
            o.spare_cost,
            o.total_cost,
            o.next_due_date,
            o.remarks
        FROM jute_mech_overhaul o
        INNER JOIN machine_mst m ON m.machine_id = o.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN party_mst p ON p.party_id = o.party_id
        WHERE o.active = 1
          AND o.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(o.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR o.from_date >= :date_from)
          AND (:date_to IS NULL OR o.from_date <= :date_to)
          AND (:machine_id IS NULL OR o.machine_id = :machine_id)
          AND (:overhaul_type IS NULL OR o.overhaul_type = :overhaul_type)
        ORDER BY o.from_date DESC, o.overhaul_id DESC
        LIMIT :limit OFFSET :offset
        """
    )


def get_overhaul_table_count_query():
    """Row count for /overhaul_table — identical filters, no LIMIT/ORDER BY."""
    return text(
        """
        SELECT COUNT(*) AS total
        FROM jute_mech_overhaul o
        INNER JOIN machine_mst m ON m.machine_id = o.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE o.active = 1
          AND o.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(o.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR o.from_date >= :date_from)
          AND (:date_to IS NULL OR o.from_date <= :date_to)
          AND (:machine_id IS NULL OR o.machine_id = :machine_id)
          AND (:overhaul_type IS NULL OR o.overhaul_type = :overhaul_type)
        """
    )


def get_overhaul_by_id_query():
    """Full header for one overhaul (includes work_done), display names resolved."""
    return text(
        """
        SELECT
            o.overhaul_id,
            o.co_id,
            COALESCE(o.branch_id, d.branch_id) AS branch_id,
            o.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            o.overhaul_type,
            o.from_date,
            o.to_date,
            o.downtime_days,
            o.work_done,
            o.done_by,
            o.party_id,
            p.supp_name AS party_name,
            o.labour_cost,
            o.spare_cost,
            o.total_cost,
            o.next_due_date,
            o.remarks
        FROM jute_mech_overhaul o
        INNER JOIN machine_mst m ON m.machine_id = o.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN party_mst p ON p.party_id = o.party_id
        WHERE o.overhaul_id = :overhaul_id
          AND o.co_id = :co_id
          AND o.active = 1
        """
    )


def get_overhaul_active_by_id_query():
    """Lean active-row guard for edit / delete (no joins)."""
    return text(
        """
        SELECT
            overhaul_id,
            co_id,
            branch_id,
            machine_id,
            overhaul_type,
            from_date,
            to_date,
            done_by,
            party_id,
            labour_cost
        FROM jute_mech_overhaul
        WHERE overhaul_id = :overhaul_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def get_overhaul_spares_query():
    """Active spare lines for one overhaul, with item / UOM names for display."""
    return text(
        """
        SELECT
            s.overhaul_spare_id,
            s.overhaul_id,
            s.item_id,
            i.item_code,
            i.item_name,
            s.qty,
            s.uom_id,
            u.uom_name,
            s.rate,
            s.amount,
            s.remarks
        FROM jute_mech_overhaul_spare s
        LEFT JOIN item_mst i ON i.item_id = s.item_id
        LEFT JOIN uom_mst u ON u.uom_id = s.uom_id
        WHERE s.overhaul_id = :overhaul_id
          AND s.active = 1
        ORDER BY s.overhaul_spare_id
        """
    )


# =============================================================================
# Overhaul header + spares — writes
# =============================================================================


def insert_overhaul_query():
    """Insert one overhaul header (active = 1).

    downtime_days, spare_cost, total_cost and next_due_date are computed in Python
    before this runs — the DB derives nothing.
    """
    return text(
        """
        INSERT INTO jute_mech_overhaul
            (co_id, branch_id, machine_id, overhaul_type, from_date, to_date,
             downtime_days, work_done, done_by, party_id, labour_cost, spare_cost,
             total_cost, next_due_date, remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :machine_id, :overhaul_type, :from_date, :to_date,
             :downtime_days, :work_done, :done_by, :party_id, :labour_cost, :spare_cost,
             :total_cost, :next_due_date, :remarks, 1, :updated_by)
        """
    )


def update_overhaul_query():
    """Full header replace on edit, with the derived fields recomputed."""
    return text(
        """
        UPDATE jute_mech_overhaul
        SET branch_id     = :branch_id,
            machine_id    = :machine_id,
            overhaul_type = :overhaul_type,
            from_date     = :from_date,
            to_date       = :to_date,
            downtime_days = :downtime_days,
            work_done     = :work_done,
            done_by       = :done_by,
            party_id      = :party_id,
            labour_cost   = :labour_cost,
            spare_cost    = :spare_cost,
            total_cost    = :total_cost,
            next_due_date = :next_due_date,
            remarks       = :remarks,
            updated_by    = :updated_by
        WHERE overhaul_id = :overhaul_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def soft_delete_overhaul_query():
    """Soft delete the header (active = 0)."""
    return text(
        """
        UPDATE jute_mech_overhaul
        SET active = 0, updated_by = :updated_by
        WHERE overhaul_id = :overhaul_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def insert_overhaul_spare_query():
    """Insert one spare line under an overhaul."""
    return text(
        """
        INSERT INTO jute_mech_overhaul_spare
            (overhaul_id, item_id, qty, uom_id, rate, amount, remarks, active, updated_by)
        VALUES
            (:overhaul_id, :item_id, :qty, :uom_id, :rate, :amount, :remarks, 1, :updated_by)
        """
    )


def soft_delete_overhaul_spares_query():
    """Soft delete every live spare line of an overhaul.

    Used both by /overhaul_delete (cascade) and by /overhaul_edit, which replaces
    the spare set: old lines are retired here, the new set is inserted after.
    """
    return text(
        """
        UPDATE jute_mech_overhaul_spare
        SET active = 0, updated_by = :updated_by
        WHERE overhaul_id = :overhaul_id
          AND active = 1
        """
    )
