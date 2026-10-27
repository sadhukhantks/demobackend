"""Raw SQL for the Hands Report (``GET /api/juteProductionReports/hands_report``).

Three cheap aggregates; every comparison is done in Python over them:

* **actuals**  — ``daily_attendance`` hands per (date, designation, shift letter)
* **norms**    — every ``jute_prod_hands_norms_mst`` row that could be in force anywhere in
  the window. The table is small (one row per designation per revision), so the
  effective-date pick runs in Python and the whole range costs ONE query instead
  of one per day.
* **machine runs** — one row per (date, shift letter, machine) that shows up in ANY
  machine-keyed entry, with the list of sources it came from and the production
  quantity (kg / cuts) those entries carry. Feeds both the "Machines Worked"
  sheet and the multiplier of a *machine* (M) or *production* (P) norm. Since
  2026-10-01 the report does not run this SELECT directly: every request first
  REBUILDS its window into ``jute_prod_machine_shift_worked``
  (``get_machine_shift_worked_delete_query`` + ``..._insert_query``, the same
  legs plus ``workers_marked`` / ``shift_hours`` / ``stopped_all_shift`` /
  ``worked`` / ``synced_at``) and then reads the table
  (``get_machine_shift_worked_rows_query``). The same transaction then applies
  the **manual overrides** of the window (``jute_prod_machine_shift_override``,
  spec §12: ``..._apply_add_query`` upserts the ADD rows, ``..._apply_exclude_query``
  updates the EXCLUDE rows), so ``worked`` reads ``ADD override OR (worker
  marked AND not stopped all shift AND no EXCLUDE override)``.
* **group members** — the active members (machine + units) of every active
  machine group of the branch, the machine set of a group-scoped norm.
* **department production** — person-keyed winding kg per (date, shift,
  department of the winder), the winding half of a department-scoped P norm.

``daily_attendance`` and ``daily_doff_tbl`` carry ``branch_id`` but NO ``co_id``,
so the company scope rides ``branch_id IN (branches of :co_id)``.
"""

from sqlalchemy import bindparam, text

from src.juteProduction.spinning_query import SPINNING_WEIGHT_TYPES_SQL


# A rejected attendance row is a dead record. NULL status (legacy / bio-imported
# rows — a whole branch of them on sls) still counts, exactly as
# ``src/hrms/attendance.py`` treats it; filtering on status_id = 3 instead would
# silently report zero hands on any tenant whose attendance came in by import.
ATTENDANCE_STATUS_REJECTED = 4

# Company scope for the attendance tables, which have no co_id of their own.
_CO_BRANCHES = "(SELECT b.branch_id FROM branch_mst b WHERE b.co_id = :co_id)"


def _shift_letter(expr: str) -> str:
    """Shift bucket for display/grouping: first char of the spell code, with
    anything outside A/B/C landing in the general shift 'G'."""
    return (
        f"CASE WHEN LEFT(COALESCE({expr}, ''), 1) IN ('A', 'B', 'C') "
        f"THEN LEFT(COALESCE({expr}, ''), 1) ELSE 'G' END"
    )


def get_hands_actuals_query():
    """Actual hands per (date, worked designation, shift letter).

    hands = (working_hours - idle_hours) / 8 for EVERY shift, C included — the
    same flat /8 the DPR hands section and the HRMS attendance summary use.

    ``da.spell`` is the legacy text fallback for rows whose spell_id was never
    stamped (same COALESCE the DPR uses).

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(
        f"""
        SELECT x.tran_date,
               x.designation_id,
               x.shift,
               dg.desig AS designation,
               dg.direct_indirect AS desig_direct_indirect,
               dg.dept_id,
               dp.dept_desc AS dept,
               SUM(x.hands) AS hands
        FROM (
            SELECT da.attendance_date AS tran_date,
                   da.worked_designation_id AS designation_id,
                   {_shift_letter("COALESCE(sp.spell_code, da.spell)")} AS shift,
                   (COALESCE(da.working_hours, 0) - COALESCE(da.idle_hours, 0)) / 8
                       AS hands
            FROM daily_attendance da
            LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
            WHERE da.attendance_date BETWEEN :date_from AND :date_to
              AND da.is_active = 1
              AND COALESCE(CAST(da.status_id AS UNSIGNED), 0)
                  <> {ATTENDANCE_STATUS_REJECTED}
              AND da.branch_id = :branch_id
              AND da.branch_id IN {_CO_BRANCHES}
        ) x
        LEFT JOIN designation_mst dg ON dg.designation_id = x.designation_id
        LEFT JOIN dept_mst dp ON dp.dept_id = dg.dept_id
        GROUP BY x.tran_date, x.designation_id, x.shift,
                 dg.desig, dg.direct_indirect, dg.dept_id, dp.dept_desc
        ORDER BY x.tran_date, dg.desig
        """
    )


def get_hands_norms_query():
    """Every norm row that could be in force on any date of the window.

    The MAX(effective_date) <= report-date pick and the branch preference
    (an exact-branch row beats the NULL-branch fallback) happen in Python —
    see ``hands_report._norm_for``.

    Hands Norms v2 columns (``norm_kind`` F/M/P/N/A, the group / type /
    department scope, ``units_per_hand`` for M, ``qty_per_hand`` + ``qty_uom``
    for P) ride along; ``variable_fixed`` and ``hands_per_machine`` stay for the
    legacy fallback in ``hands_report._kind`` / ``_targets``. ``designation``,
    ``machine_type`` and ``scope_dept`` are the display names of the
    designation and of a type / department scope, and ``unit_label`` what a
    group scope's units count, for the ``scopes`` list of the Machines Worked
    view.

    Binds: :co_id, :branch_id, :date_to.
    """
    return text(
        """
        SELECT n.hands_norm_id,
               n.designation_id,
               dg.desig AS designation,
               n.branch_id,
               n.direct_indirect,
               n.variable_fixed,
               n.norm_kind,
               n.shift_a, n.shift_b, n.shift_c, n.shift_g,
               n.machine_type_id,
               mt.machine_type_name AS machine_type,
               n.machine_group_id,
               g.group_name AS machine_group,
               g.unit_label,
               n.scope_dept_id,
               sd.dept_desc AS scope_dept,
               n.hands_per_machine,
               n.units_per_hand,
               n.qty_per_hand,
               n.qty_uom,
               n.norms_text,
               n.effective_date
        FROM jute_prod_hands_norms_mst n
        LEFT JOIN jute_prod_machine_group_mst g ON g.machine_group_id = n.machine_group_id
        LEFT JOIN designation_mst dg ON dg.designation_id = n.designation_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = n.machine_type_id
        LEFT JOIN dept_mst sd ON sd.dept_id = n.scope_dept_id
        WHERE n.co_id = :co_id
          AND n.active = 1
          AND n.effective_date <= :date_to
          AND (n.branch_id = :branch_id OR n.branch_id IS NULL)
        ORDER BY n.designation_id, n.effective_date, n.hands_norm_id
        """
    )


def get_hands_group_members_query():
    """Active members of every active machine group of the company / branch —
    ``members[machine_group_id] -> {machine_id: units}`` once indexed by
    ``hands_report``. A machine may sit in several groups. ``group_name`` and
    ``unit_label`` ride along for the Machines Worked rows (``group_ids`` /
    ``groups``) and ``scopes``.

    Binds: :co_id, :branch_id.
    """
    return text(
        """
        SELECT g.machine_group_id,
               g.group_name,
               g.unit_label,
               d.machine_id,
               d.units
        FROM jute_prod_machine_group_mst g
        INNER JOIN jute_prod_machine_group_dtl d
                ON d.machine_group_id = g.machine_group_id AND d.active = 1
        WHERE g.co_id = :co_id
          AND g.branch_id = :branch_id
          AND g.active = 1
        ORDER BY g.machine_group_id, d.machine_id
        """
    )


# Labels of the UNION legs below. The order is the display order of the
# "Sources" cell. Since 2026-10-01 only the Attendance leg makes a machine-shift
# COUNT (a worker marked, and not stopped all shift — see
# hands_report._machine_runs); the RUN / STOP split is kept for the display
# order and for reading a row that predates the ``worked`` flag.
RUN_SOURCES = (
    "Weaving",
    "Doff",
    "Spinning",
    "Beaming",
    "Finishing",
    "Spreader",
    "Drawing",
    "Winding",
    "Bale Press",
    "Waste",
    "Attendance",
)
STOP_SOURCES = ("Stoppage", "Breakdown")
# The RUN legs that are a PRODUCTION entry: everything but the attendance mark
# itself and waste booked against a machine. A machine-shift seen in one of
# these with no worker marked is what the report asks the mill to fix.
PRODUCTION_SOURCES = tuple(s for s in RUN_SOURCES if s not in ("Attendance", "Waste"))


def _leg(select_shift: str, table_sql: str, date_col: str, machine_col: str, src: str,
         where: str, hands: str = "NULL", stoppage: str = "0", breakdown: str = "0",
         prod_kg: str = "NULL", prod_cuts: str = "NULL") -> str:
    """One UNION leg, pre-collapsed to (date, shift letter, machine) so the
    outer GROUP BY works over at most one row per source per machine-shift.
    ``select_shift`` is the already-bucketed shift expression for that table.
    ``prod_kg`` / ``prod_cuts`` are the leg's production quantity (NULL where
    the leg has none) — each quantity rides exactly ONE leg, see
    ``get_hands_machine_runs_query``."""
    return f"""
            SELECT {date_col} AS tran_date,
                   {select_shift} AS shift,
                   {machine_col} AS machine_id,
                   '{src}' AS src,
                   {hands} AS hands,
                   {stoppage} AS stoppage_hours,
                   {breakdown} AS breakdown_min,
                   {prod_kg} AS prod_kg,
                   {prod_cuts} AS prod_cuts
            FROM {table_sql}
            WHERE {where}
            GROUP BY {date_col}, shift, {machine_col}"""


# Winding production is person-keyed (``jute_prod_winding_doff.eb_id``; its
# machine_id column is dead). The two derived tables below attach a winder's
# doff kg of one shift to a machine through the winder's quality row
# (``jute_prod_winding_daily_qlty.machine_id`` is live):
#
# * ``_WINDING_FIRST_QLTY_SQL`` — per (co, date, spell, winder) the EARLIEST
#   active quality row that names a machine, so a winder on two qualities is
#   counted once (and a first row without a machine does not swallow the kg).
# * ``_WINDING_DOFF_KG_SQL`` — per (co, date, spell, winder) the doff kg.
#
# Both are scoped by the same binds as the legs.
_WINDING_FIRST_QLTY_SQL = """(SELECT q.co_id, q.tran_date, q.spell_id, q.eb_id,
                                     MIN(q.winding_daily_qlty_id) AS first_qlty_id
                              FROM jute_prod_winding_daily_qlty q
                              WHERE q.active = 1 AND q.machine_id IS NOT NULL
                                AND q.co_id = :co_id AND q.branch_id = :branch_id
                                AND q.tran_date BETWEEN :date_from AND :date_to
                              GROUP BY q.co_id, q.tran_date, q.spell_id, q.eb_id)"""

_WINDING_DOFF_KG_SQL = """(SELECT d.co_id, d.tran_date, d.spell_id, d.eb_id,
                                  SUM(d.production_qty) AS kg
                           FROM jute_prod_winding_doff d
                           WHERE d.active = 1
                             AND d.co_id = :co_id AND d.branch_id = :branch_id
                             AND d.tran_date BETWEEN :date_from AND :date_to
                           GROUP BY d.co_id, d.tran_date, d.spell_id, d.eb_id)"""


def _machine_run_legs_sql() -> str:
    """The UNION ALL of every machine-keyed leg (see
    ``get_hands_machine_runs_query`` for what each one is), each pre-grouped
    to (date, shift letter, machine). Shared by the live SELECT and by the
    INSERT that rebuilds ``jute_prod_machine_shift_worked`` so the table can
    never drift from the legs.

    Binds: :co_id, :branch_id, :date_from, :date_to."""
    by_id = _shift_letter("sp.spell_code")
    co_branch = "co_id = :co_id AND branch_id = :branch_id"
    window = "BETWEEN :date_from AND :date_to"
    legs = [
        _leg(by_id, "jute_prod_weaving_daily t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id",
             "t.tran_date", "t.machine_id", "Weaving",
             f"t.active = 1 AND t.{co_branch} AND t.tran_date {window}",
             prod_cuts="SUM(t.cuts)"),
        _leg(by_id, "daily_doff_tbl t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell",
             "t.doff_date", "t.mc_id", "Doff",
             f"(t.active = 1 OR t.active IS NULL) AND t.weight_type IN {SPINNING_WEIGHT_TYPES_SQL}"
             f" AND t.branch_id = :branch_id AND t.branch_id IN {_CO_BRANCHES}"
             f" AND t.doff_date {window}",
             prod_kg="SUM(t.net_weight)"),
        _leg(by_id, "jute_prod_spinning_daily t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id",
             "t.tran_date", "t.machine_id", "Spinning",
             f"t.active = 1 AND t.{co_branch} AND t.tran_date {window}"),
        _leg(by_id, "jute_prod_beaming_daily t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id",
             "t.tran_date", "t.machine_id", "Beaming",
             f"t.active = 1 AND t.{co_branch} AND t.tran_date {window}",
             prod_kg="SUM(t.act_prod_kg)"),
        _leg(by_id, "jute_prod_finishing_daily t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id",
             "t.tran_date", "t.machine_id", "Finishing",
             f"t.active = 1 AND t.machine_id IS NOT NULL AND t.{co_branch} AND t.tran_date {window}",
             prod_kg="SUM(CASE WHEN t.process IS NULL OR t.process <> 'balepress'"
                     " THEN t.prod_wt_kg END)"),
        _leg(_shift_letter("t.spell"), "spreader_prod_entry t",
             "t.entry_date", "t.machine_id", "Spreader",
             f"t.active = 1 AND t.machine_id IS NOT NULL AND t.{co_branch} AND t.entry_date {window}",
             prod_kg="SUM(t.no_of_rolls * t.wt_per_roll)"),
        _leg(_shift_letter("t.spell"), "jute_prod_drawing_entry t",
             "t.tran_date", "t.machine_id", "Drawing",
             f"t.active = 1 AND t.machine_id IS NOT NULL AND t.{co_branch} AND t.tran_date {window}"),
        _leg(by_id,
             "jute_prod_winding_daily_qlty t"
             " LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id"
             f" LEFT JOIN {_WINDING_FIRST_QLTY_SQL} fq"
             "        ON fq.first_qlty_id = t.winding_daily_qlty_id"
             f" LEFT JOIN {_WINDING_DOFF_KG_SQL} wd"
             "        ON wd.co_id = fq.co_id AND wd.tran_date = fq.tran_date"
             "       AND wd.spell_id = fq.spell_id AND wd.eb_id = fq.eb_id",
             "t.tran_date", "t.machine_id", "Winding",
             f"t.active = 1 AND t.machine_id IS NOT NULL AND t.{co_branch} AND t.tran_date {window}",
             prod_kg="SUM(wd.kg)"),
        _leg(by_id, "jute_prod_bale_entry t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id",
             "t.tran_date", "t.machine_id", "Bale Press",
             f"t.active = 1 AND t.machine_id IS NOT NULL AND t.{co_branch} AND t.tran_date {window}",
             prod_kg="SUM(CASE WHEN t.entry_type = 'PROD' THEN t.net_wt END)"),
        _leg(by_id, "jute_prod_waste_entry t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id",
             "t.tran_date", "t.machine_id", "Waste",
             f"t.active = 1 AND t.machine_id IS NOT NULL AND t.{co_branch} AND t.tran_date {window}"),
        _leg(_shift_letter("COALESCE(sp.spell_code, t.spell, da.spell)"),
             # Header first, then the marks: without the hint MySQL scans the
             # whole 1.1M-row marks table (4 s on sls). Same shape as the weaving
             # sync queries; the 2026-09-16 attendance index migration makes the
             # second hop a seek.
             "daily_attendance da"
             " STRAIGHT_JOIN daily_ebmc_attendance t ON t.daily_atten_id = da.daily_atten_id"
             " LEFT JOIN spell_mst sp ON sp.spell_id = COALESCE(t.spell_id, da.spell_id)",
             "da.attendance_date", "t.mc_id", "Attendance",
             f"t.is_active = 1 AND t.mc_id IS NOT NULL"
             f" AND da.is_active = 1"
             f" AND COALESCE(CAST(da.status_id AS UNSIGNED), 0) <> {ATTENDANCE_STATUS_REJECTED}"
             f" AND da.branch_id = :branch_id AND da.branch_id IN {_CO_BRANCHES}"
             f" AND da.attendance_date {window}",
             hands="COUNT(DISTINCT t.eb_id)",
             # One stoppage figure per machine-shift, not one per worker on it.
             stoppage="MAX(COALESCE(t.mc_stoppage_hours, 0))"),
        _leg(by_id, "jute_prod_stoppage_hours t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id",
             "t.tran_date", "t.machine_id", "Stoppage",
             f"t.active = 1 AND t.{co_branch} AND t.tran_date {window}",
             stoppage="SUM(COALESCE(t.stoppage_hours, 0))"),
        _leg(by_id, "jute_mech_breakdown t LEFT JOIN spell_mst sp ON sp.spell_id = t.spell_id",
             "t.tran_date", "t.machine_id", "Breakdown",
             f"t.active = 1 AND t.{co_branch} AND t.tran_date {window}",
             breakdown="SUM(COALESCE(t.duration_min, 0))"),
    ]
    return "\n            UNION ALL".join(legs)


def get_hands_machine_runs_query():
    """Every machine that appears in ANY machine-keyed entry, per date x shift
    letter, with the sources it came from — one row per (date, shift, machine),
    which is exactly what stops a machine from being counted twice: a loom with
    three weaving lines, an attendance mark and a stoppage entry in shift A is
    ONE row whose ``sources`` reads ``Attendance,Stoppage,Weaving``.

    Legs (each pre-grouped to the same grain, then UNION ALL + outer GROUP BY):

    RUN — a production / waste entry or the attendance mark. Since 2026-10-01
    only the ATTENDANCE leg makes the machine count as worked; the production
    legs list the machine and carry its kg / cuts:
      Weaving   ``jute_prod_weaving_daily.machine_id`` (spell_id)
      Doff      ``daily_doff_tbl.mc_id`` — ``spell`` holds the spell_id, NOT a code
      Spinning  ``jute_prod_spinning_daily.machine_id`` (derived from doffs; harmless
                under the dedupe, and the only spinning source on tenants with no doffs)
      Beaming   ``jute_prod_beaming_daily.machine_id``
      Finishing ``jute_prod_finishing_daily.machine_id`` (NULL for labour-only processes)
      Spreader  ``spreader_prod_entry.machine_id`` — ``spell`` is a CODE, ``entry_date``
      Drawing   ``jute_prod_drawing_entry.machine_id`` — ``spell`` is a CODE
      Winding   ``jute_prod_winding_daily_qlty.machine_id`` (winding doffs are
                person-keyed; their machine_id column is dead)
      Bale Press ``jute_prod_bale_entry.machine_id``
      Waste     ``jute_prod_waste_entry.machine_id`` (waste booked against a machine)
      Attendance ``daily_ebmc_attendance.mc_id`` — the man-machine deployment mark.
                Scoped through its ``daily_attendance`` header: ``dea.branch_id``
                and ``dea.attendace_date`` are NULL for mobile marks, and a
                rejected/inactive header must not count. ``hands`` = distinct
                workers marked on the machine.

    STOP — negative evidence, listed but never counted on its own; the hours
    also decide ``stopped_all_shift`` in the table:
      Stoppage  ``jute_prod_stoppage_hours`` (hours, summed over reasons)
      Breakdown ``jute_mech_breakdown`` (duration_min; spell may be NULL -> 'G')

    Shift letters are normalised INSIDE each leg (spell_id tables via spell_mst,
    code tables from the code itself), so A1 and A2 collapse into A before the
    dedupe and never produce two rows for one machine.

    Production quantity (Hands Norms v2, the numerator of a P norm) — each
    quantity rides exactly ONE leg so the outer SUM cannot double count:
      Weaving    ``prod_cuts = SUM(cuts)``; no kg (that exists only in the
                 weaving read-time day slice)
      Doff       ``SUM(net_weight)`` — the spinning kg
      Spinning   none: ``act_prod_doff`` is derived from the same doffs
      Beaming    ``SUM(act_prod_kg)``
      Finishing  ``SUM(prod_wt_kg)`` excluding ``process = 'balepress'`` (a
                 synced copy of the bale entries; ``prod_qty`` is mixed UoM)
      Spreader   ``SUM(no_of_rolls * wt_per_roll)``
      Drawing    none (``diff_meter`` is metres)
      Winding    ``SUM(kg)`` of the per-winder doff sums, attached to the
                 winder's EARLIEST quality row of the shift that names a machine
                 (see ``_WINDING_FIRST_QLTY_SQL``); doffs of winders with no
                 such row are picked up per department by
                 ``get_hands_dept_production_query`` instead
      Bale Press ``SUM(net_wt) WHERE entry_type = 'PROD'`` (REPACK re-presses)
      Waste / Attendance / Stoppage / Breakdown  none
    The outer query also returns ``winding_kg`` (the Winding leg's share of
    ``prod_kg``) so a department-scoped P norm, which takes its winding kg
    from the per-department query, can leave that share out of the machine sum.

    This is the LIVE read of the sources. The Hands Report itself reads the
    same rows from ``jute_prod_machine_shift_worked`` after rebuilding its
    window (``get_machine_shift_worked_insert_query`` is built from the very
    same legs), so this query is kept as the reference / ad-hoc check.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    union = _machine_run_legs_sql()
    return text(
        f"""
        SELECT k.tran_date,
               k.shift,
               k.machine_id,
               m.machine_name,
               m.mech_code,
               m.machine_type_id,
               mt.machine_type_name,
               m.dept_id,
               dp.dept_desc AS dept,
               GROUP_CONCAT(DISTINCT k.src ORDER BY k.src SEPARATOR ',') AS sources,
               MAX(k.hands) AS hands,
               SUM(CASE WHEN k.src = 'Attendance' THEN k.stoppage_hours ELSE 0 END)
                   AS att_stoppage_hours,
               SUM(CASE WHEN k.src = 'Stoppage' THEN k.stoppage_hours ELSE 0 END)
                   AS stoppage_hours,
               SUM(k.breakdown_min) AS breakdown_min,
               SUM(k.prod_kg) AS prod_kg,
               SUM(k.prod_cuts) AS prod_cuts,
               SUM(CASE WHEN k.src = 'Winding' THEN k.prod_kg END) AS winding_kg
        FROM ({union}
        ) k
        INNER JOIN machine_mst m ON m.machine_id = k.machine_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst dp ON dp.dept_id = m.dept_id
        GROUP BY k.tran_date, k.shift, k.machine_id, m.machine_name, m.mech_code,
                 m.machine_type_id, mt.machine_type_name, m.dept_id, dp.dept_desc
        ORDER BY k.tran_date, k.shift, mt.machine_type_name, m.machine_name
        """
    )


# ---------------------------------------------------------------------------
# jute_prod_machine_shift_worked — "machines worked from attendance"
# (spec 2026-09-30 hands norms v2, §11; migration 20261001_machine_shift_worked_table.sql)
# ---------------------------------------------------------------------------

MACHINE_SHIFT_WORKED_TABLE = "jute_prod_machine_shift_worked"

# Hours of a shift when the branch has no full-shift spell for the letter.
SHIFT_HOURS_FALLBACK = 8

# One row: the branch's full-shift hours per shift letter. A branch owns its
# spells through ``spell_mst.shift_id -> shift_mst.branch_id``. Always exactly
# one row (an aggregate without GROUP BY), all NULL when the branch has no
# spell rows at all.
_SHIFT_HOURS_SQL = """(SELECT
                MAX(CASE WHEN UPPER(TRIM(sp.spell_code)) = 'A' AND sp.working_hours > 0
                         THEN sp.working_hours END) AS hrs_a,
                MAX(CASE WHEN UPPER(TRIM(sp.spell_code)) = 'B' AND sp.working_hours > 0
                         THEN sp.working_hours END) AS hrs_b,
                MAX(CASE WHEN UPPER(TRIM(sp.spell_code)) = 'C' AND sp.working_hours > 0
                         THEN sp.working_hours END) AS hrs_c,
                MAX(CASE WHEN UPPER(TRIM(sp.spell_code)) = 'G' AND sp.working_hours > 0
                         THEN sp.working_hours END) AS hrs_g
            FROM spell_mst sp
            INNER JOIN shift_mst sm ON sm.shift_id = sp.shift_id
            WHERE sm.branch_id = :branch_id)"""


def _shift_hours_case(shift_expr: str) -> str:
    """Hours of the shift letter ``shift_expr`` off the one-row ``sh``
    (``_SHIFT_HOURS_SQL``): the branch's full-shift spell of the letter,
    ``SHIFT_HOURS_FALLBACK`` when it has none."""
    fallback = SHIFT_HOURS_FALLBACK
    return (
        f"CASE UPPER({shift_expr})"
        f" WHEN 'A' THEN COALESCE(sh.hrs_a, {fallback})"
        f" WHEN 'B' THEN COALESCE(sh.hrs_b, {fallback})"
        f" WHEN 'C' THEN COALESCE(sh.hrs_c, {fallback})"
        f" ELSE COALESCE(sh.hrs_g, {fallback}) END"
    )


def _machine_shift_worked_select_sql() -> str:
    """The SELECT half of the rebuild: one row per (date, shift letter,
    machine) of the window, in the column order of the INSERT.

    Built from the SAME legs as ``get_hands_machine_runs_query`` (so the
    production kg / cuts and both stoppage books land in the row), plus:

    ``workers_marked``
        distinct workers marked on the machine in attendance — the Attendance
        leg's ``hands``, 0 when the machine-shift has no mark.

    ``shift_hours`` — the length of the shift, per shift LETTER
        ``spell_mst.working_hours`` of the branch's **full-shift spell** of the
        letter: the spell whose code is exactly the letter (``A`` / ``B`` /
        ``C`` / ``G``), among the spells of the branch's own shifts
        (``spell_mst.shift_id -> shift_mst.branch_id = :branch_id``); the
        longest when the branch has several rows with that code; rows with
        NULL / 0 hours are ignored. The part-shift spells (``A1``, ``A2``,
        ``B1``, ``B2``) are deliberately NOT used: they are the two halves of
        a split shift (5 h + 3 h) and neither is the shift. When the branch
        has no full-shift spell for the letter the fallback is
        ``SHIFT_HOURS_FALLBACK`` (8).
        Real rows it rests on (2026-10-01): sls branch 29 — A = 8.00 (spell
        109), B = 8.00 (110), C = 7.50 (101), next to A1 5 / A2 3 / B1 3 /
        B2 5; sls branch 87 — C = 8.00 (108), no A / B row (only A1 8, A2 3,
        B1 8, B2 5) so A and B fall back to 8; dev3 branch 12 — A = 8 (6, 9),
        B = 8 (7; spell 10 has NULL hours and is ignored), C = 8 (8).
        Shift ``G`` (anything outside A / B / C) has no spell on either tenant
        and is 8.

    ``stopped_all_shift``
        1 when ``GREATEST(att_stoppage_hours, stoppage_hours,
        breakdown_min / 60) >= shift_hours`` — the higher of the three books,
        never their sum. Independent of ``workers_marked``.

    ``worked``
        1 when ``workers_marked > 0 AND stopped_all_shift = 0`` — what the
        SOURCES say. The manual overrides of the window are applied on top by
        the next two statements of the rebuild
        (``get_machine_shift_worked_apply_add_query`` /
        ``..._apply_exclude_query``); the final ``worked`` is the only thing
        that makes a machine count for a machine-based (M) norm.

    ``synced_at`` = ``NOW()`` (the session time zone, +05:30).

    The stoppage / breakdown / production sums are clamped to the column
    ranges of the table, so one absurd entry cannot fail the INSERT (and with
    it the report) under STRICT_TRANS_TABLES.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    union = _machine_run_legs_sql()
    shift_hours = _shift_hours_case("x.shift")
    stopped = (
        "CASE WHEN GREATEST(x.att_stoppage_hours, x.stoppage_hours, x.breakdown_min / 60)"
        f" >= {shift_hours} THEN 1 ELSE 0 END"
    )
    return f"""
        SELECT :co_id AS co_id,
               :branch_id AS branch_id,
               x.tran_date,
               UPPER(x.shift) AS shift,
               x.machine_id,
               x.workers_marked,
               x.att_stoppage_hours,
               x.stoppage_hours,
               x.breakdown_min,
               {shift_hours} AS shift_hours,
               x.prod_kg,
               x.prod_cuts,
               x.winding_kg,
               x.sources,
               {stopped} AS stopped_all_shift,
               CASE WHEN x.workers_marked > 0 AND {stopped} = 0 THEN 1 ELSE 0 END AS worked,
               NOW() AS synced_at
        FROM (
            SELECT k.tran_date,
                   k.shift,
                   k.machine_id,
                   COALESCE(MAX(k.hands), 0) AS workers_marked,
                   LEAST(GREATEST(ROUND(SUM(CASE WHEN k.src = 'Attendance'
                                                 THEN k.stoppage_hours ELSE 0 END), 2), 0),
                         9999.99) AS att_stoppage_hours,
                   LEAST(GREATEST(ROUND(SUM(CASE WHEN k.src = 'Stoppage'
                                                 THEN k.stoppage_hours ELSE 0 END), 2), 0),
                         9999.99) AS stoppage_hours,
                   LEAST(GREATEST(ROUND(SUM(k.breakdown_min), 2), 0), 999999.99)
                       AS breakdown_min,
                   LEAST(GREATEST(ROUND(SUM(k.prod_kg), 3), -99999999999.999),
                         99999999999.999) AS prod_kg,
                   LEAST(GREATEST(ROUND(SUM(k.prod_cuts), 3), -999999999.999),
                         999999999.999) AS prod_cuts,
                   LEAST(GREATEST(ROUND(SUM(CASE WHEN k.src = 'Winding' THEN k.prod_kg END), 3),
                                  -99999999999.999),
                         99999999999.999) AS winding_kg,
                   GROUP_CONCAT(DISTINCT k.src ORDER BY k.src SEPARATOR ',') AS sources
            FROM ({union}
            ) k
            INNER JOIN machine_mst m ON m.machine_id = k.machine_id
            GROUP BY k.tran_date, k.shift, k.machine_id
        ) x
        CROSS JOIN {_SHIFT_HOURS_SQL} sh"""


def get_machine_shift_worked_source_query():
    """The rebuild's SELECT on its own — what the INSERT would write, without
    writing it. Read-only: for validating the SQL and sanity-checking a
    window against a tenant before / without a rebuild.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(_machine_shift_worked_select_sql())


def get_machine_shift_worked_delete_query():
    """Rebuild step 1: drop the window's rows of the company / branch.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(
        f"""
        DELETE FROM {MACHINE_SHIFT_WORKED_TABLE}
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date BETWEEN :date_from AND :date_to
        """
    )


def get_machine_shift_worked_insert_query():
    """Rebuild step 2: ``INSERT ... SELECT`` of the window from the sources
    (``_machine_shift_worked_select_sql`` — the legs, ``shift_hours``,
    ``stopped_all_shift``, ``worked``, ``synced_at``).

    ``ON DUPLICATE KEY UPDATE`` on ``uq_msw (branch_id, tran_date, shift,
    machine_id)``: two simultaneous opens of the report rebuild the same
    window, and the second INSERT must overwrite the first one's rows rather
    than fail on the unique key.

    The SELECT is wrapped in a derived table (``src``) on purpose: it ends in
    ``CROSS JOIN (...) sh``, and MySQL would read a directly following
    ``ON DUPLICATE ...`` as that join's ``ON`` condition (error 1064).

    ``manual_action`` is not in the column list (a fresh row takes the
    column's NULL default); the upsert resets it to NULL so a row a concurrent
    rebuild already stamped is rebuilt from the sources too — the override
    statements that follow set it again from the overrides in force.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(
        f"""
        INSERT INTO {MACHINE_SHIFT_WORKED_TABLE}
            (co_id, branch_id, tran_date, shift, machine_id,
             workers_marked, att_stoppage_hours, stoppage_hours, breakdown_min,
             shift_hours, prod_kg, prod_cuts, winding_kg, sources,
             stopped_all_shift, worked, synced_at)
        SELECT src.co_id, src.branch_id, src.tran_date, src.shift, src.machine_id,
               src.workers_marked, src.att_stoppage_hours, src.stoppage_hours,
               src.breakdown_min, src.shift_hours, src.prod_kg, src.prod_cuts,
               src.winding_kg, src.sources, src.stopped_all_shift, src.worked,
               src.synced_at
        FROM ({_machine_shift_worked_select_sql()}
        ) src
        ON DUPLICATE KEY UPDATE
            co_id = VALUES(co_id),
            workers_marked = VALUES(workers_marked),
            att_stoppage_hours = VALUES(att_stoppage_hours),
            stoppage_hours = VALUES(stoppage_hours),
            breakdown_min = VALUES(breakdown_min),
            shift_hours = VALUES(shift_hours),
            prod_kg = VALUES(prod_kg),
            prod_cuts = VALUES(prod_cuts),
            winding_kg = VALUES(winding_kg),
            sources = VALUES(sources),
            stopped_all_shift = VALUES(stopped_all_shift),
            worked = VALUES(worked),
            manual_action = NULL,
            synced_at = VALUES(synced_at)
        """
    )


# ---------------------------------------------------------------------------
# jute_prod_machine_shift_override — manual additions / exclusions of worked
# machines (spec §12; migration 20261001_machine_shift_override.sql)
# ---------------------------------------------------------------------------

MACHINE_SHIFT_OVERRIDE_TABLE = "jute_prod_machine_shift_override"

OVERRIDE_ADD = "ADD"
OVERRIDE_EXCLUDE = "EXCLUDE"
OVERRIDE_ACTIONS = (OVERRIDE_ADD, OVERRIDE_EXCLUDE)


def _override_in_force(alias: str) -> str:
    """Predicate: the override row ``alias`` is THE override in force of its
    machine-shift — active, and the newest active one. The save keeps one
    active row per machine-shift (it retires the previous one in the same
    transaction), so the ``NOT EXISTS`` only matters if two saves of the same
    machine-shift ever race: the later row wins everywhere (rebuild and read)
    instead of an ADD and an EXCLUDE both applying, or the read doubling the
    row. A seek on ``idx_mso_machine (machine_id, tran_date)``."""
    return (
        f"{alias}.active = 1"
        f" AND NOT EXISTS (SELECT 1 FROM {MACHINE_SHIFT_OVERRIDE_TABLE} newer"
        f" WHERE newer.machine_id = {alias}.machine_id"
        f" AND newer.tran_date = {alias}.tran_date"
        f" AND newer.branch_id = {alias}.branch_id"
        f" AND newer.shift = {alias}.shift"
        f" AND newer.active = 1"
        f" AND newer.machine_shift_override_id > {alias}.machine_shift_override_id)"
    )


def get_machine_shift_worked_apply_add_query():
    """Rebuild step 3: the ADD overrides in force in the window, upserted
    AFTER the source INSERT, in the same transaction.

    A machine-shift no entry names gets a row of its own — ``sources`` NULL,
    no workers, no stoppage, no production, ``shift_hours`` of its letter —
    with ``worked = 1`` and ``manual_action = 'ADD'``. One the sources already
    wrote keeps everything it carries (workers, stoppage, production,
    ``stopped_all_shift``) and only gets ``worked = 1`` and the stamp: an ADD
    counts even when the stoppage books say stopped all shift (the supervisor
    said it worked).

    ``INNER JOIN machine_mst`` because the read joins it too: an override of
    a machine that no longer exists must not leave a row nobody can see.
    Wrapped in ``src`` for the same reason as the source INSERT (the SELECT
    would otherwise end where ``ON DUPLICATE`` can be misread).

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(
        f"""
        INSERT INTO {MACHINE_SHIFT_WORKED_TABLE}
            (co_id, branch_id, tran_date, shift, machine_id,
             workers_marked, att_stoppage_hours, stoppage_hours, breakdown_min,
             shift_hours, sources, stopped_all_shift, worked, manual_action,
             synced_at)
        SELECT src.co_id, src.branch_id, src.tran_date, src.shift, src.machine_id,
               0, 0, 0, 0,
               src.shift_hours, NULL, 0, 1, '{OVERRIDE_ADD}',
               src.synced_at
        FROM (
            SELECT o.co_id,
                   o.branch_id,
                   o.tran_date,
                   UPPER(o.shift) AS shift,
                   o.machine_id,
                   {_shift_hours_case("o.shift")} AS shift_hours,
                   NOW() AS synced_at
            FROM {MACHINE_SHIFT_OVERRIDE_TABLE} o
            INNER JOIN machine_mst m ON m.machine_id = o.machine_id
            CROSS JOIN {_SHIFT_HOURS_SQL} sh
            WHERE o.co_id = :co_id
              AND o.branch_id = :branch_id
              AND o.tran_date BETWEEN :date_from AND :date_to
              AND o.action = '{OVERRIDE_ADD}'
              AND {_override_in_force("o")}
        ) src
        ON DUPLICATE KEY UPDATE
            worked = 1,
            manual_action = '{OVERRIDE_ADD}',
            synced_at = VALUES(synced_at)
        """
    )


def get_machine_shift_worked_apply_exclude_query():
    """Rebuild step 4: the EXCLUDE overrides in force in the window — the
    machine-shift's row (if the sources wrote one) gets ``worked = 0`` and
    ``manual_action = 'EXCLUDE'``, whatever attendance says. An EXCLUDE of a
    machine-shift no entry names has nothing to update and shows nowhere
    until an entry names it.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(
        f"""
        UPDATE {MACHINE_SHIFT_WORKED_TABLE} w
        INNER JOIN {MACHINE_SHIFT_OVERRIDE_TABLE} o
                ON o.branch_id = w.branch_id
               AND o.tran_date = w.tran_date
               AND o.shift = w.shift
               AND o.machine_id = w.machine_id
        SET w.worked = 0,
            w.manual_action = '{OVERRIDE_EXCLUDE}'
        WHERE w.co_id = :co_id
          AND w.branch_id = :branch_id
          AND w.tran_date BETWEEN :date_from AND :date_to
          AND o.co_id = :co_id
          AND o.action = '{OVERRIDE_EXCLUDE}'
          AND {_override_in_force("o")}
        """
    )


def retire_machine_shift_overrides_query():
    """Save, step 1: retire the active override(s) of the machine-shifts about
    to get a new one. The retired row is FROZEN as it was — ``updated_by`` and
    ``updated_date_time`` keep saying who made that change and when (the
    explicit ``updated_date_time = updated_date_time`` stops the column's ON
    UPDATE) — because the row that supersedes it carries the new who / when.

    Binds: :co_id, :branch_id, :tran_date, :shift, :machine_ids (expanding).
    """
    return text(
        f"""
        UPDATE {MACHINE_SHIFT_OVERRIDE_TABLE}
        SET active = 0,
            updated_date_time = updated_date_time
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date = :tran_date
          AND shift = :shift
          AND active = 1
          AND machine_id IN :machine_ids
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


def insert_machine_shift_override_query():
    """Save, step 2: the new override of one machine-shift (executed with a
    list of binds, one per machine). ``updated_date_time`` takes the column
    default — the moment of the change.

    Binds: :co_id, :branch_id, :tran_date, :shift, :machine_id, :action,
    :reason, :updated_by.
    """
    return text(
        f"""
        INSERT INTO {MACHINE_SHIFT_OVERRIDE_TABLE}
            (co_id, branch_id, tran_date, shift, machine_id, action, reason,
             active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :shift, :machine_id, :action, :reason,
             1, :updated_by)
        """
    )


def get_machine_shift_overrides_to_undo_query():
    """Undo, step 1: which of the given override ids are ACTIVE overrides of
    the company / branch, with their dates (the windows to rebuild).

    Binds: :co_id, :branch_id, :override_ids (expanding).
    """
    return text(
        f"""
        SELECT machine_shift_override_id, tran_date
        FROM {MACHINE_SHIFT_OVERRIDE_TABLE}
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND active = 1
          AND machine_shift_override_id IN :override_ids
        ORDER BY tran_date, machine_shift_override_id
        """
    ).bindparams(bindparam("override_ids", expanding=True))


def undo_machine_shift_overrides_query():
    """Undo, step 2: ``active = 0`` on those rows — only rows of that company
    / branch, only active ones. The row then reads who undid it and when
    (``updated_by`` = the user, ``updated_date_time`` moves by its ON UPDATE);
    action and reason stay.

    Binds: :co_id, :branch_id, :updated_by, :override_ids (expanding).
    """
    return text(
        f"""
        UPDATE {MACHINE_SHIFT_OVERRIDE_TABLE}
        SET active = 0,
            updated_by = :updated_by
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND active = 1
          AND machine_shift_override_id IN :override_ids
        """
    ).bindparams(bindparam("override_ids", expanding=True))


def get_hands_report_machines_query():
    """Every ACTIVE machine of the branch's departments, for the "Add
    machines" picker of the Machines Worked tab (~800 rows on sls, filtered
    client-side) — the same machine set the override save accepts
    (``hands_norms.get_branch_machines_query``), scoped to the company
    through the branch.

    Binds: :co_id, :branch_id.
    """
    return text(
        """
        SELECT m.machine_id,
               m.machine_name,
               m.mech_code,
               m.machine_type_id,
               mt.machine_type_name,
               m.dept_id,
               dp.dept_desc AS dept
        FROM machine_mst m
        INNER JOIN dept_mst dp ON dp.dept_id = m.dept_id
        INNER JOIN branch_mst b ON b.branch_id = dp.branch_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        WHERE m.active = 1
          AND dp.branch_id = :branch_id
          AND b.co_id = :co_id
        ORDER BY dp.order_id, dp.dept_desc, mt.machine_type_name, m.machine_name, m.machine_id
        """
    )


def get_machine_shift_worked_rows_query():
    """The report's read of ``jute_prod_machine_shift_worked``: one row per
    (date, shift letter, machine) of the window with the machine / type /
    department names — the columns ``get_hands_machine_runs_query`` returns
    (``hands`` = the workers marked, NULL when none, as before) plus
    ``workers_marked``, ``shift_hours``, ``stopped_all_shift``, ``worked`` and
    ``synced_at``.

    Manual overrides (spec §12): ``manual_action`` is the row's own stamp
    (``ADD`` / ``EXCLUDE`` / NULL, set by the rebuild); ``override_id``,
    ``manual_reason``, ``manual_by`` (the user's name, else the e-mail) and
    ``manual_at`` come from the override in force of that machine-shift with
    that action — at most one row (``_override_in_force``), so the join can
    never double a machine-shift.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(
        f"""
        SELECT w.tran_date,
               w.shift,
               w.machine_id,
               m.machine_name,
               m.mech_code,
               m.machine_type_id,
               mt.machine_type_name,
               m.dept_id,
               dp.dept_desc AS dept,
               w.sources,
               NULLIF(w.workers_marked, 0) AS hands,
               w.att_stoppage_hours,
               w.stoppage_hours,
               w.breakdown_min,
               w.prod_kg,
               w.prod_cuts,
               w.winding_kg,
               w.workers_marked,
               w.shift_hours,
               w.stopped_all_shift,
               w.worked,
               w.synced_at,
               w.manual_action,
               o.machine_shift_override_id AS override_id,
               o.reason AS manual_reason,
               COALESCE(NULLIF(TRIM(u.name), ''), u.email_id) AS manual_by,
               o.updated_date_time AS manual_at
        FROM {MACHINE_SHIFT_WORKED_TABLE} w
        INNER JOIN machine_mst m ON m.machine_id = w.machine_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst dp ON dp.dept_id = m.dept_id
        LEFT JOIN {MACHINE_SHIFT_OVERRIDE_TABLE} o
               ON o.branch_id = w.branch_id
              AND o.tran_date = w.tran_date
              AND o.shift = w.shift
              AND o.machine_id = w.machine_id
              AND o.co_id = w.co_id
              AND o.action = w.manual_action
              AND {_override_in_force("o")}
        LEFT JOIN user_mst u ON u.user_id = o.updated_by
        WHERE w.co_id = :co_id
          AND w.branch_id = :branch_id
          AND w.tran_date BETWEEN :date_from AND :date_to
        ORDER BY w.tran_date, w.shift, mt.machine_type_name, m.machine_name
        """
    )


def get_hands_dept_production_query():
    """Person-keyed winding kg per (date, shift letter, department of the
    winder) — the winding half of a DEPARTMENT-scoped production norm, which
    cannot be read off the machine legs because ~13 % of the doffs on sls have
    no machine (the winder has no quality row naming one) and a whole branch
    (87) has none at all.

    The winder's department is, in order:
      1. the department of the designation the winder was marked in
         ``daily_attendance`` that date — same shift letter first, then any
         attendance row of the date (the same rows, filters and shift bucket
         as ``get_hands_actuals_query``);
      2. else the department of the machine on the winder's earliest quality
         row of that shift (``_WINDING_FIRST_QLTY_SQL``);
      3. else NULL — returned as a ``dept_id IS NULL`` row so the report can
         say how many kg were left out.

    Every active doff of the branch is included (machine-attributed or not);
    the report leaves the Winding leg's ``winding_kg`` out of the machine sum
    of a department scope so nothing is counted twice.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    att_shift = _shift_letter("COALESCE(sp.spell_code, da.spell)")
    doff_shift = _shift_letter("sp.spell_code")
    attendance = f"""(SELECT da.eb_id,
                             da.attendance_date,
                             {att_shift} AS shift,
                             dg.dept_id
                      FROM daily_attendance da
                      LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
                      INNER JOIN designation_mst dg
                              ON dg.designation_id = da.worked_designation_id
                      WHERE da.attendance_date BETWEEN :date_from AND :date_to
                        AND da.is_active = 1
                        AND COALESCE(CAST(da.status_id AS UNSIGNED), 0)
                            <> {ATTENDANCE_STATUS_REJECTED}
                        AND da.branch_id = :branch_id
                        AND da.branch_id IN {_CO_BRANCHES}
                        AND dg.dept_id IS NOT NULL)"""
    return text(
        f"""
        SELECT x.tran_date,
               x.shift,
               x.dept_id,
               SUM(x.kg) AS prod_kg
        FROM (
            SELECT d.tran_date,
                   {doff_shift} AS shift,
                   COALESCE(a1.dept_id, a2.dept_id, m.dept_id) AS dept_id,
                   d.production_qty AS kg
            FROM jute_prod_winding_doff d
            LEFT JOIN spell_mst sp ON sp.spell_id = d.spell_id
            LEFT JOIN (SELECT a.eb_id, a.attendance_date, a.shift, MIN(a.dept_id) AS dept_id
                       FROM {attendance} a
                       GROUP BY a.eb_id, a.attendance_date, a.shift) a1
                   ON a1.eb_id = d.eb_id AND a1.attendance_date = d.tran_date
                  AND a1.shift = {doff_shift}
            LEFT JOIN (SELECT a.eb_id, a.attendance_date, MIN(a.dept_id) AS dept_id
                       FROM {attendance} a
                       GROUP BY a.eb_id, a.attendance_date) a2
                   ON a2.eb_id = d.eb_id AND a2.attendance_date = d.tran_date
            LEFT JOIN {_WINDING_FIRST_QLTY_SQL} fq
                   ON fq.co_id = d.co_id AND fq.tran_date = d.tran_date
                  AND fq.spell_id = d.spell_id AND fq.eb_id = d.eb_id
            LEFT JOIN jute_prod_winding_daily_qlty q
                   ON q.winding_daily_qlty_id = fq.first_qlty_id
            LEFT JOIN machine_mst m ON m.machine_id = q.machine_id
            WHERE d.active = 1
              AND d.co_id = :co_id AND d.branch_id = :branch_id
              AND d.tran_date BETWEEN :date_from AND :date_to
        ) x
        GROUP BY x.tran_date, x.shift, x.dept_id
        ORDER BY x.tran_date, x.shift, x.dept_id
        """
    )
