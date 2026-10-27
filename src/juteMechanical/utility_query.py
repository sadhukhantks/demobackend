"""SQL for the Jute Mechanical utilities router (``/api/mechUtility``) — ITM-004
daily power readings.

Every function returns a ``sqlalchemy.text()`` with NAMED binds; callers pass
``None`` for SQL NULL. Tables: ``jute_power_meter_mst`` (meters per branch) and
``jute_power_reading`` (one row per meter per day / spell). Schema:
``dbqueries/migrations/20261018_jute_power_reading.sql``; ORM in
``src/juteMechanical/models.py``.

Conventions shared with ``breakdown_query.py``: soft delete through ``active``,
``updated_by`` stamped by the app, ``FIELD(meter_type, 'GRID', 'DG', 'SOLAR')``
as the display order everywhere a meter list is returned.

``spell_id`` is INT NOT NULL with 0 = whole day (``WHOLE_DAY_SPELL_ID``) so the
UNIQUE key ``uq_jpr_scope`` holds; the router maps null <-> 0 at the edge. A
date is keyed EITHER as a whole day OR per spell, never both (the sums over
spell_id in the history / report / DPR queries would double-count otherwise):
``get_power_spell_conflict_query`` is the guard the save runs first.

The two per-day production feeds for kWh/t come from the DPR:
``dprQueries.get_dpr_spinning_daily_production_query`` (yarn kg per doff date,
reused as-is) and ``get_power_report_cloth_daily_query`` below, which groups
``dprQueries.WEAVING_STORED_JUGAR_MT_SQL`` -- the one shared stored-jugar
expression -- by ``wd.tran_date`` instead of by quality.
"""

from sqlalchemy import text

from src.juteProduction.dprQueries import WEAVING_STORED_JUGAR_MT_SQL


# =============================================================================
# Meter master
# =============================================================================


def get_power_meters_query():
    """Meters of one branch, GRID -> DG -> SOLAR then code.

    Binds: :co_id, :branch_id, :include_inactive (1 = also inactive meters).
    """
    return text(
        """
        SELECT power_meter_id, co_id, branch_id, meter_code, meter_name, meter_type, active
        FROM jute_power_meter_mst
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND (:include_inactive = 1 OR active = 1)
        ORDER BY FIELD(meter_type, 'GRID', 'DG', 'SOLAR'), meter_code
        """
    )


def check_power_meter_duplicate_query():
    """Another meter (active or not) already holding this code on the branch.

    Binds: :co_id, :branch_id, :meter_code, :power_meter_id (None on create;
    the row being edited is excluded on update).
    """
    return text(
        """
        SELECT power_meter_id, active
        FROM jute_power_meter_mst
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND meter_code = :meter_code
          AND (:power_meter_id IS NULL OR power_meter_id <> :power_meter_id)
        LIMIT 1
        """
    )


def get_power_meter_by_id_query():
    """Binds: :power_meter_id, :co_id."""
    return text(
        """
        SELECT power_meter_id, co_id, branch_id, meter_code, meter_name, meter_type, active
        FROM jute_power_meter_mst
        WHERE power_meter_id = :power_meter_id
          AND co_id = :co_id
        LIMIT 1
        """
    )


def insert_power_meter_query():
    """Binds: :co_id, :branch_id, :meter_code, :meter_name, :meter_type, :active, :updated_by."""
    return text(
        """
        INSERT INTO jute_power_meter_mst
            (co_id, branch_id, meter_code, meter_name, meter_type, active, updated_by)
        VALUES
            (:co_id, :branch_id, :meter_code, :meter_name, :meter_type, :active, :updated_by)
        """
    )


def update_power_meter_query():
    """Binds: :power_meter_id, :co_id, :meter_code, :meter_name, :meter_type, :active, :updated_by."""
    return text(
        """
        UPDATE jute_power_meter_mst
        SET meter_code = :meter_code,
            meter_name = :meter_name,
            meter_type = :meter_type,
            active = :active,
            updated_by = :updated_by
        WHERE power_meter_id = :power_meter_id
          AND co_id = :co_id
        """
    )


# =============================================================================
# Readings — entry screen
# =============================================================================


def get_power_readings_by_date_query():
    """Active readings of one date + spell, joined to the meter for name / type.

    LEFT JOIN so a reading whose meter was deactivated (or whose code no longer
    resolves) still comes back and can be deleted from the screen.

    Binds: :co_id, :branch_id, :tran_date, :spell_id (0 = whole day).
    """
    return text(
        """
        SELECT r.power_reading_id, r.co_id, r.branch_id, r.tran_date, r.spell_id,
               r.meter_code, m.meter_name, m.meter_type, m.active AS meter_active,
               r.opening_kwh, r.closing_kwh, r.kwh, r.dg_hours, r.dg_litres,
               r.max_demand_kva, r.remarks, sp.spell_code
        FROM jute_power_reading r
        LEFT JOIN jute_power_meter_mst m
               ON m.co_id = r.co_id
              AND m.branch_id = r.branch_id
              AND m.meter_code = r.meter_code
        LEFT JOIN spell_mst sp ON sp.spell_id = r.spell_id
        WHERE r.active = 1
          AND r.co_id = :co_id
          AND r.branch_id = :branch_id
          AND r.tran_date = :tran_date
          AND r.spell_id = :spell_id
        ORDER BY FIELD(m.meter_type, 'GRID', 'DG', 'SOLAR'), r.meter_code
        """
    )


def get_power_prev_closing_query():
    """The closing_kwh to offer as the opening of the (date, spell) being keyed,
    per meter: the latest closing before it in reading order.

    Reading order is the date, then the spell's ``spell_mst.starting_time``
    (so the previous day's last spell -- C, which starts latest -- wins over
    its A), then the row id. When a SPELL is being keyed, the same day's
    earlier spells (starting_time below the keyed spell's) qualify first, so
    spell B opens on spell A's closing; a whole-day row of the same date never
    qualifies (that shape is a conflict anyway) and a whole-day save looks at
    previous days only. Falls back to the previous day's last closing when no
    earlier spell exists.

    Binds: :co_id, :branch_id, :tran_date, :spell_id (0 = whole day),
    :whole_day (the sentinel, WHOLE_DAY_SPELL_ID).
    """
    return text(
        """
        SELECT x.meter_code, x.closing_kwh, x.tran_date
        FROM (
            SELECT r.meter_code, r.closing_kwh, r.tran_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.meter_code
                       ORDER BY r.tran_date DESC,
                                COALESCE(sp.starting_time, '00:00:00') DESC,
                                r.spell_id DESC,
                                r.power_reading_id DESC
                   ) AS rn
            FROM jute_power_reading r
            LEFT JOIN spell_mst sp ON sp.spell_id = r.spell_id
            LEFT JOIN spell_mst cur ON cur.spell_id = :spell_id
            WHERE r.active = 1
              AND r.co_id = :co_id
              AND r.branch_id = :branch_id
              AND r.closing_kwh IS NOT NULL
              AND (
                    r.tran_date < :tran_date
                 OR (r.tran_date = :tran_date
                     AND :spell_id <> :whole_day
                     AND r.spell_id <> :whole_day
                     AND r.spell_id <> :spell_id
                     AND sp.starting_time < cur.starting_time)
              )
        ) x
        WHERE x.rn = 1
        """
    )


def get_power_kwh_history_query():
    """Daily kWh per meter over a window (spells summed per day) — the 30-day
    median basis for the spike warning and the entry screen hint.

    Binds: :co_id, :branch_id, :meter_code (None = every meter), :date_from, :date_to.
    """
    return text(
        """
        SELECT r.meter_code, r.tran_date, ROUND(SUM(r.kwh), 2) AS kwh
        FROM jute_power_reading r
        WHERE r.active = 1
          AND r.co_id = :co_id
          AND r.branch_id = :branch_id
          AND (:meter_code IS NULL OR r.meter_code = :meter_code)
          AND r.tran_date BETWEEN :date_from AND :date_to
        GROUP BY r.meter_code, r.tran_date
        ORDER BY r.meter_code, r.tran_date
        """
    )


def get_power_reading_by_key_query():
    """The row (active OR soft-deleted — same scan as the unique key) holding
    this (co, branch, date, spell, meter) key, so a save can UPDATE it in place.

    Binds: :co_id, :branch_id, :tran_date, :spell_id, :meter_code.
    """
    return text(
        """
        SELECT power_reading_id, active
        FROM jute_power_reading
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND meter_code = :meter_code
        LIMIT 1
        """
    )


def get_power_spell_conflict_query():
    """1 row when the date is already keyed in the OTHER shape: per spell while
    a whole day (:spell_id = :whole_day) is being saved, or as a whole day while
    a spell is being saved. The save refuses then (400) -- every reader SUMs
    across spell_id, so both shapes on one date would double-count the day.

    Binds: :co_id, :branch_id, :tran_date, :spell_id, :whole_day.
    """
    return text(
        """
        SELECT 1
        FROM jute_power_reading r
        WHERE r.active = 1
          AND r.co_id = :co_id
          AND r.branch_id = :branch_id
          AND r.tran_date = :tran_date
          AND (
                (:spell_id = :whole_day AND r.spell_id <> :whole_day)
             OR (:spell_id <> :whole_day AND r.spell_id = :whole_day)
          )
        LIMIT 1
        """
    )


def insert_power_reading_query():
    """Binds: :co_id, :branch_id, :tran_date, :spell_id, :meter_code, :opening_kwh,
    :closing_kwh, :kwh, :dg_hours, :dg_litres, :max_demand_kva, :remarks, :updated_by."""
    return text(
        """
        INSERT INTO jute_power_reading
            (co_id, branch_id, tran_date, spell_id, meter_code,
             opening_kwh, closing_kwh, kwh, dg_hours, dg_litres, max_demand_kva,
             remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, :meter_code,
             :opening_kwh, :closing_kwh, :kwh, :dg_hours, :dg_litres, :max_demand_kva,
             :remarks, 1, :updated_by)
        """
    )


def update_power_reading_query():
    """Overwrite every value column and re-activate the row.

    Binds: :power_reading_id, :opening_kwh, :closing_kwh, :kwh, :dg_hours,
    :dg_litres, :max_demand_kva, :remarks, :updated_by.
    """
    return text(
        """
        UPDATE jute_power_reading
        SET opening_kwh = :opening_kwh,
            closing_kwh = :closing_kwh,
            kwh = :kwh,
            dg_hours = :dg_hours,
            dg_litres = :dg_litres,
            max_demand_kva = :max_demand_kva,
            remarks = :remarks,
            active = 1,
            updated_by = :updated_by
        WHERE power_reading_id = :power_reading_id
        """
    )


def get_power_reading_active_by_id_query():
    """Binds: :power_reading_id, :co_id."""
    return text(
        """
        SELECT power_reading_id, co_id, branch_id, tran_date, meter_code
        FROM jute_power_reading
        WHERE power_reading_id = :power_reading_id
          AND co_id = :co_id
          AND active = 1
        LIMIT 1
        """
    )


def soft_delete_power_reading_query():
    """Binds: :power_reading_id, :updated_by."""
    return text(
        """
        UPDATE jute_power_reading
        SET active = 0,
            updated_by = :updated_by
        WHERE power_reading_id = :power_reading_id
          AND active = 1
        """
    )


# =============================================================================
# Power & DG report
# =============================================================================


def get_power_report_readings_query():
    """kWh / DG hours / DG litres / max demand per (date, meter) over the report
    window, spells summed per day. Meter name and type resolved through the
    master; a reading whose meter is gone falls back to its code as the name
    and GRID as the type.

    Binds: :co_id, :branch_id, :window_from, :date_to.
    """
    return text(
        """
        SELECT r.tran_date,
               r.meter_code,
               COALESCE(m.meter_name, r.meter_code) AS meter_name,
               COALESCE(m.meter_type, 'GRID') AS meter_type,
               ROUND(SUM(r.kwh), 2) AS kwh,
               ROUND(SUM(COALESCE(r.dg_hours, 0)), 2) AS dg_hours,
               ROUND(SUM(COALESCE(r.dg_litres, 0)), 2) AS dg_litres,
               MAX(r.max_demand_kva) AS max_demand_kva
        FROM jute_power_reading r
        LEFT JOIN jute_power_meter_mst m
               ON m.co_id = r.co_id
              AND m.branch_id = r.branch_id
              AND m.meter_code = r.meter_code
        WHERE r.active = 1
          AND r.co_id = :co_id
          AND r.branch_id = :branch_id
          AND r.tran_date BETWEEN :window_from AND :date_to
        GROUP BY r.tran_date, r.meter_code, m.meter_name, m.meter_type
        ORDER BY r.tran_date, FIELD(m.meter_type, 'GRID', 'DG', 'SOLAR'), r.meter_code
        """
    )


def get_power_report_cloth_daily_query():
    """Weaving production in MT per day — the cloth tonnes under kWh/t.

    ``dprQueries.get_dpr_weaving_mtd_query`` with ``wd.tran_date`` in the
    SELECT and the GROUP BY on the date instead of the quality, so one row per
    day equals the DPR's weaving day total for that day. The MT expression is
    the shared ``WEAVING_STORED_JUGAR_MT_SQL`` (never a copy), so the DPR, its
    Power line and this report divide by one definition of cloth tonnes.
    Branch predicate is NULL-tolerant like the source (parity with
    weaving_day_slice_sql).

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date.
    """
    return text(
        f"""
        SELECT
            wd.tran_date,
            {WEAVING_STORED_JUGAR_MT_SQL} AS production_mt
        FROM jute_prod_weaving_daily wd
        INNER JOIN jute_prod_weaving_quality q
                ON q.weaving_quality_id = wd.weaving_quality_id
        WHERE wd.active = 1
          AND wd.co_id = :co_id
          AND wd.tran_date BETWEEN :date_from AND :report_date
          -- branch NULL-tolerant by design (parity with weaving_day_slice_sql)
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
        GROUP BY wd.tran_date
        ORDER BY wd.tran_date
        """
    )
