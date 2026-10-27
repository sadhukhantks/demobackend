"""
Raw SQL builders for the Jute Issue Planner report (/juteReports/issue-planner).

The planner combines four sources, so the builders here span both modules'
tables: previous-day spinning production (daily_doff_tbl), the batch plan
window (jute_batch_daily_assign + jute_batch_plan_li), the spreader roll
stock as of the 06:00 snapshot (spreader_prod_entry / spreader_roll_issue),
and the maturity master (item_maturity_mst). Godown stock reuses
get_jute_stock_report_query() from reportQueries.py.
"""

from sqlalchemy import text

from src.juteProduction.spinning_query import SPINNING_WEIGHT_TYPES_SQL
from src.juteProcurement.constants import ISSUE_PLANNER_ASSIGN_STATUSES

_ASSIGN_STATUSES_SQL = ", ".join(str(s) for s in ISSUE_PLANNER_ASSIGN_STATUSES)


def get_branch_co_query():
    """Company owning a branch (spreader tables and item_maturity_mst are co-scoped)."""
    return text("SELECT co_id FROM branch_mst WHERE branch_id = :branch_id")


def get_reference_production_query():
    """Previous-day spinning production per yarn item for one branch.

    daily_doff_tbl has no co_id and branch_id is nullable on legacy rows, so
    branch scope rides COALESCE(row branch, machine -> dept -> branch spine).
    The weight_type discriminator keeps mobile-app non-spinning weighings out,
    and item_id IS NOT NULL mirrors every other per-yarn aggregate (unsynced
    doffs have no yarn stamped yet). jute_yarn_mst is pre-collapsed to one
    yarn per item so the join can never fan out doff rows.

    Binds: :branch_id (int), :production_date (YYYY-MM-DD)
    """
    return text(
        f"""
        SELECT
            dd.item_id,
            im.item_name AS yarn_name,
            jy.jute_yarn_id,
            ROUND(SUM(dd.net_weight), 3) AS production_kg
        FROM daily_doff_tbl dd
        INNER JOIN machine_mst m ON m.machine_id = dd.mc_id
        INNER JOIN dept_mst dm ON dm.dept_id = m.dept_id
        LEFT JOIN item_mst im ON im.item_id = dd.item_id
        LEFT JOIN (
            SELECT item_id, MAX(jute_yarn_id) AS jute_yarn_id
            FROM jute_yarn_mst
            GROUP BY item_id
        ) jy ON jy.item_id = dd.item_id
        WHERE dd.doff_date = :production_date
          AND (dd.active = 1 OR dd.active IS NULL)
          AND dd.item_id IS NOT NULL
          AND dd.weight_type IN {SPINNING_WEIGHT_TYPES_SQL}
          AND COALESCE(dd.branch_id, dm.branch_id) = :branch_id
        GROUP BY dd.item_id, im.item_name, jy.jute_yarn_id
        ORDER BY im.item_name
        """
    )


def get_plan_window_query():
    """Batch daily assigns joined to their plan composition for a date window.

    One row per (assign_date, yarn, plan quality line); assigns whose plan has
    no lines still appear (quality columns NULL) so the plan section can show
    them. Draft assigns are included — see ISSUE_PLANNER_ASSIGN_STATUSES.

    Binds: :branch_id (int), :date_from, :date_to (YYYY-MM-DD)
    """
    return text(
        f"""
        SELECT
            bda.assign_date,
            bda.jute_yarn_id,
            jym.item_id AS yarn_item_id,
            COALESCE(yim.item_name, jym.jute_yarn_name) AS yarn_name,
            bda.batch_plan_id,
            bp.plan_name,
            bda.status_id,
            bpl.jute_quality_id AS item_id,
            qim.item_name AS quality_name,
            bpl.percentage
        FROM jute_batch_daily_assign bda
        INNER JOIN jute_batch_plan bp ON bp.batch_plan_id = bda.batch_plan_id
        LEFT JOIN jute_batch_plan_li bpl ON bpl.batch_plan_id = bda.batch_plan_id
        LEFT JOIN jute_yarn_mst jym ON jym.jute_yarn_id = bda.jute_yarn_id
        LEFT JOIN item_mst yim ON yim.item_id = jym.item_id
        LEFT JOIN item_mst qim ON qim.item_id = bpl.jute_quality_id
        WHERE bda.branch_id = :branch_id
          AND bda.assign_date BETWEEN :date_from AND :date_to
          AND bda.status_id IN ({_ASSIGN_STATUSES_SQL})
        ORDER BY bda.assign_date, yarn_name, quality_name
        """
    )


def get_roll_stock_snapshot_query():
    """Spreader roll stock per open group as of a cutoff datetime.

    Same shape as get_bins_with_stock_query (src/juteProduction/query.py) but
    windowed on the persisted entry_dt/issue_dt instead of "as of now": closing
    = production before cutoff minus issues before cutoff. Blend columns
    (item_id_2 / ratios) are returned raw; the caller splits kg per component
    quality. Branch fallback mirrors the live roll-stock query (row branch,
    else the machine's dept branch).

    Unlike the live roll-stock query (current stock > 0), groups netting
    NEGATIVE at the cutoff are kept (<> 0): an issue saved against rolls whose
    claimed production hour falls after the snapshot must subtract from the
    quality total, matching the MIS snapshot's HAVING closstock <> 0.

    Binds: :co_id (int), :branch_id (int or None), :cutoff_dt (datetime)
    """
    return text(
        """
        SELECT
            p.entry_id_grp,
            p.item_id,
            i.item_name,
            p.item_id_2,
            i2.item_name AS item_name_2,
            p.ratio_1,
            p.ratio_2,
            p.produced_kg,
            COALESCE(iss.issued_kg, 0) AS issued_kg,
            (p.produced_kg - COALESCE(iss.issued_kg, 0)) AS current_kg
        FROM (
            SELECT
                pe.entry_id_grp,
                pe.item_id,
                pe.item_id_2,
                pe.ratio_1,
                pe.ratio_2,
                SUM(pe.no_of_rolls * pe.wt_per_roll) AS produced_kg,
                MAX(COALESCE(pe.branch_id, d.branch_id)) AS branch_id
            FROM spreader_prod_entry pe
            LEFT JOIN machine_mst m ON m.machine_id = pe.machine_id
            LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
            WHERE pe.co_id = :co_id
              AND pe.active = 1
              AND pe.entry_dt < :cutoff_dt
            GROUP BY pe.entry_id_grp, pe.item_id, pe.item_id_2, pe.ratio_1, pe.ratio_2
        ) p
        LEFT JOIN item_mst i ON i.item_id = p.item_id
        LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2
        LEFT JOIN (
            SELECT entry_id_grp,
                   SUM(no_of_rolls * wt_per_roll) AS issued_kg
            FROM spreader_roll_issue
            WHERE co_id = :co_id
              AND active = 1
              AND issue_dt < :cutoff_dt
            GROUP BY entry_id_grp
        ) iss ON iss.entry_id_grp = p.entry_id_grp
        WHERE (p.produced_kg - COALESCE(iss.issued_kg, 0)) <> 0
          AND (:branch_id IS NULL OR p.branch_id = :branch_id)
        """
    )


def get_maturity_map_query():
    """Active maturity hours per jute quality for a company.

    Binds: :co_id (int)
    """
    return text(
        """
        SELECT imm.item_id, imm.maturity_hours
        FROM item_maturity_mst imm
        WHERE imm.co_id = :co_id
          AND imm.active = 1
        """
    )
