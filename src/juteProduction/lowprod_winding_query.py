"""Raw SQL for the Winding Low Producer report.

Everything this report needs about PRODUCTION and TARGETS already exists:

* ``winding_query.get_winding_reconciliation_query()`` — reconciled KG per
  winder + spell + quality for ONE date (doff sum - opening jugar + closing
  jugar). ``get_winding_reconciliation_range_query()`` below runs that same SQL
  over the requested range in one round trip.
* ``winding_query.resolve_winding_targets_query()`` — KG per winder per
  8-hour shift, latest effective row on or before a date.

The one thing that did not exist is the ATTENDANCE link — the missing piece the
older winding reports documented as a limitation ("targets are not pro-rated by
hours actually worked"). That link is this module's whole reason to exist.
"""

from sqlalchemy import bindparam, text

from src.juteProduction.winding_query import get_winding_reconciliation_query

_ONE_DATE = "AND tran_date = :tran_date"
_DATE_RANGE = "AND tran_date BETWEEN :date_from AND :date_to"
_ONE_DATE_ORDER = "ORDER BY sp.spell_code, emp_code"
_DATE_RANGE_ORDER = "ORDER BY doff.tran_date, sp.spell_code, emp_code"


def get_winding_reconciliation_range_query():
    """get_winding_reconciliation_query over :date_from .. :date_to, ONE round trip.

    Built from that query's own SQL text, not a copy, so the reconciliation
    (doff sum - opening jugar + closing jugar, quality fallback, worker and spell
    joins) stays in one place. Only two things change: each of the three
    subqueries' ``tran_date = :tran_date`` becomes a BETWEEN, and the rows are
    ordered by date first. Every subquery groups by tran_date and every join
    matches on it, and the tran_date columns are DATE, so the result is exactly
    the per-date results one after another (the loop this replaces).

    Raises RuntimeError if the single-date SQL no longer has the expected shape
    (a unit test pins this), rather than quietly returning a wrong query.

    Binds: :branch_id (required), :date_from, :date_to, :spell_id, :item_id.
    """
    sql = get_winding_reconciliation_query().text
    if (
        sql.count(_ONE_DATE) != 3
        or sql.count(":tran_date") != 3
        or sql.count(_ONE_DATE_ORDER) != 1
    ):
        raise RuntimeError(
            "get_winding_reconciliation_query changed shape; update "
            "get_winding_reconciliation_range_query to match"
        )
    return text(
        sql.replace(_ONE_DATE, _DATE_RANGE).replace(_ONE_DATE_ORDER, _DATE_RANGE_ORDER)
    )


def get_spell_hours_query():
    """spell_id -> configured hours, from spell_mst (status=1). The report's
    only source for "how long is a full shift" — never hardcode shift lengths."""
    return text(
        """
        SELECT spell_id, spell_code, working_hours
        FROM spell_mst
        WHERE status = 1
        """
    )


def get_winding_attendance_hours_query():
    """Net worked hours per (date, spell, winder) for the requested branch.

    ``att_hrs = SUM(working_hours - idle_hours)``, the same expression every
    other jute-production checklist uses (``reportQueries._ATTENDANCE_AGG``),
    matched on **spell_id** — never spell_code, which repeats once per shift
    generation (sls 'A1' = 91/97/102) and would rake in another generation's
    rows.

    Scope is ``branch_id`` alone: ``daily_attendance`` has no co_id, and the
    endpoint has already verified that this branch belongs to the requested
    company, so a second ``branch_id IN (branches of :co_id)`` subquery would
    re-check what is already proven.

    Row filter matches the existing attendance reads: ``is_active = 1`` and not
    REJECTED (status_id 4). Deliberately NO designation gate — winding
    designations share no prefix or substring ('WINDER SPOOL', 'P.W. WINDING',
    'RE WINDER', ...; see winding_query.get_winding_workers_query), so any gate
    would silently drop real winders' hours and read as a production shortfall.

    ``att_rows`` comes back so the router can disclose duplicate attendance keys
    rather than silently halving somebody's efficiency.
    ponytail: duplicates are SUMmed (~0.1% of rows on sls, same as the sibling
    checklists). Collapse to one row per key here only if a tenant starts
    producing them in bulk.

    Binds: :branch_id, :date_from, :date_to, :eb_ids (expanding, non-empty).
    """
    return text(
        """
        SELECT da.attendance_date AS tran_date,
               da.spell_id,
               da.eb_id,
               SUM(COALESCE(da.working_hours, 0) - COALESCE(da.idle_hours, 0)) AS att_hrs,
               COUNT(*) AS att_rows
        FROM daily_attendance da
        WHERE da.attendance_date BETWEEN :date_from AND :date_to
          AND da.is_active = 1
          AND da.branch_id = :branch_id
          AND da.eb_id IN :eb_ids
          AND (da.status_id IS NULL OR CAST(da.status_id AS UNSIGNED) <> 4)
        GROUP BY da.attendance_date, da.spell_id, da.eb_id
        """
    ).bindparams(bindparam("eb_ids", expanding=True))
