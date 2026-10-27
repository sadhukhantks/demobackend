"""SQL builders for the daily Wastage Report (/api/juteProductionReports/wastage_report).

Arg-less functions returning sqlalchemy.text() with named binds. All queries
cover a whole month in one round trip (:date_from / :date_to), grouped per day.

Sources and scoping precedents:
  * waste sums        — jute_prod_waste_entry, dept-join branch fallback
                        (COALESCE(w.branch_id, d.branch_id)) as in waste_query.py
  * weaving kg/looms  — jute_prod_weaving_daily, stored-jugar kg formula copied
                        from dprQueries.get_dpr_weaving_mtd_query, split per day
                        and per machine so looms-worked can be counted; quality
                        type via the finishing-quality map (winding_stock_query)
  * spinning kg/frames— daily_doff_tbl over the machine→dept→branch spine, as
                        dprQueries.get_dpr_spinning_mtd_query, grouped per day
  * breaker feed kg   — spreader_roll_issue rolls × weight (the kg fed to the
                        breaker; breaker production itself is not tracked)
  * jute selection    — jute_issue MR-sourced legs (waste re-issues excluded so
                        waste never counts in its own denominator), ledger
                        status convention (status_id <> 4)
  * winding kg        — jute_prod_winding_doff reconciled with opening/closing
                        jugar per (branch, date, spell, winder), the model of
                        dprQueries.get_dpr_winding_mtd_query, grouped per day
                        (TKT-000183)
"""

from sqlalchemy import text

from src.juteProcurement.constants import (
    JUTE_ISSUE_EXCLUDED_STATUS,
    JUTE_WASTE_ITEM_TYPE_ID,
)


def get_waste_daily_sums_query():
    """SUM(waste_qty) kg per (day, waste item, department) over the month."""
    return text(
        """
        SELECT w.tran_date, w.item_id, im.item_name,
               w.dept_id, d.dept_desc AS dept_name,
               ROUND(SUM(w.waste_qty), 3) AS waste_kg
        FROM jute_prod_waste_entry w
        INNER JOIN dept_mst d ON d.dept_id = w.dept_id
        INNER JOIN item_mst im ON im.item_id = w.item_id
        WHERE w.co_id = :co_id
          AND w.active = 1
          AND w.tran_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR COALESCE(w.branch_id, d.branch_id) = :branch_id)
        GROUP BY w.tran_date, w.item_id, im.item_name, w.dept_id, d.dept_desc
        """
    )


def get_waste_report_map_query():
    """Active mapping rows for a company, exact-dept rows first."""
    return text(
        """
        SELECT m.waste_report_map_id, m.group_code, m.group_name,
               m.day_denom, m.total_denom, m.item_id, m.dept_id,
               m.col_label, m.display_order
        FROM jute_waste_report_map m
        WHERE m.co_id = :co_id AND m.active = 1
        ORDER BY m.display_order, m.waste_report_map_id
        """
    )


def get_weaving_daily_kg_query():
    """Weaving kg per (day, loom, quality type) — one query feeds both the
    weaving-production denominator and the hessian/sacking looms-worked counts.

    kg formula is the stored-jugar expression of get_dpr_weaving_mtd_query
    (cuts*njpc + close - open - less, scaled by finished_length and ozs_yds).
    quality_type: 1 = Hessian, 2 = Sacking, NULL = unmapped (counts in kg,
    in neither loom bucket).
    """
    return text(
        """
        SELECT
            wd.tran_date,
            wd.machine_id,
            fq.quality_type,
            ROUND(SUM(
                CASE WHEN COALESCE(q.no_of_jugar_per_cut, 0) > 0
                     THEN (wd.cuts * q.no_of_jugar_per_cut
                           + COALESCE(wd.close_jugar, 0)
                           - COALESCE(wd.open_jugar, 0)
                           - COALESCE(wd.less_production, 0))
                          * COALESCE(q.finished_length, 0) / q.no_of_jugar_per_cut
                     ELSE 0 END
                * COALESCE(q.ozs_yds, 0) * 28.35 / 1000
            ), 3) AS production_kg
        FROM jute_prod_weaving_daily wd
        INNER JOIN jute_prod_weaving_quality q
                ON q.weaving_quality_id = wd.weaving_quality_id
        LEFT JOIN (
            SELECT DISTINCT item_id, quality_type
            FROM jute_prod_finishing_quality
            WHERE active = 1 AND co_id = :co_id
        ) fq ON fq.item_id = q.item_id
        WHERE wd.active = 1
          AND wd.co_id = :co_id
          AND wd.tran_date BETWEEN :date_from AND :date_to
          -- branch NULL-tolerant by design (parity with weaving_day_slice_sql)
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
        GROUP BY wd.tran_date, wd.machine_id, fq.quality_type
        """
    )


def get_spinning_daily_kg_query():
    """Spinning kg + frame runs per day from the raw doff log.

    frame_runs mirrors the DPR convention: COUNT(DISTINCT mc, shift-letter)
    per day — a frame doffing in A1 and A2 counts once for shift A.
    """
    return text(
        """
        SELECT
            dd.doff_date AS tran_date,
            ROUND(SUM(dd.net_weight), 3) AS production_kg,
            COUNT(DISTINCT dd.mc_id,
                  COALESCE(LEFT(sp.spell_code, 1), '?')) AS frame_runs
        FROM daily_doff_tbl dd
        INNER JOIN machine_mst m ON m.machine_id = dd.mc_id
        INNER JOIN machine_type_mst mt
                ON mt.machine_type_id = m.machine_type_id
               AND mt.active = 1
               AND mt.machine_type_name = :spinning_type
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
        LEFT JOIN spell_mst sp ON sp.spell_id = dd.spell
        WHERE dd.doff_date BETWEEN :date_from AND :date_to
          AND (dd.active = 1 OR dd.active IS NULL)
          AND dd.item_id IS NOT NULL
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        GROUP BY dd.doff_date
        """
    )


def get_breaker_feed_daily_kg_query():
    """Spreader rolls issued to the breaker, kg per day.

    Branch rides the issue row when set, else the spreader entry group's
    dept branch — same fallback as get_issues_by_date_query.
    """
    return text(
        """
        SELECT r.issue_date AS tran_date,
               ROUND(SUM(r.no_of_rolls * r.wt_per_roll), 3) AS feed_kg
        FROM spreader_roll_issue r
        LEFT JOIN (
            SELECT p.entry_id_grp,
                   MIN(COALESCE(p.branch_id, d.branch_id)) AS branch_id
            FROM spreader_prod_entry p
            LEFT JOIN machine_mst m ON m.machine_id = p.machine_id
            LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
            WHERE p.co_id = :co_id AND p.active = 1
            GROUP BY p.entry_id_grp
        ) pg ON pg.entry_id_grp = r.entry_id_grp
        WHERE r.co_id = :co_id
          AND r.active = 1
          AND r.issue_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR COALESCE(r.branch_id, pg.branch_id) = :branch_id)
        GROUP BY r.issue_date
        """
    )


def get_selection_daily_weight_query():
    """Raw jute issued per day — the 'Jute Selection' denominator.

    MR-sourced legs only (jute_mr_li_id set) AND only lines of a RAW JUTE
    item: waste re-issues must not count in the denominator of a wastage
    percentage. Since purchased waste is bought through the MR chain (owner
    ruling 2026-09-25) an MR-keyed issue can be waste too, so the line's item
    group is checked and type-3 (jute waste) lines are left out. Ledger status
    convention (everything except rejected), matching the stock report's issue
    CTE. Unit follows jute_issue.weight (kg on dev3; tenant-dependent).
    """
    return text(
        f"""
        SELECT ji.issue_date AS tran_date,
               ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS selection_wt
        FROM jute_issue ji
        INNER JOIN branch_mst b ON b.branch_id = ji.branch_id
        INNER JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
        LEFT JOIN item_mst im ON im.item_id = mrli.actual_item_id
        LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE b.co_id = :co_id
          AND (:branch_id IS NULL OR ji.branch_id = :branch_id)
          AND ji.issue_date BETWEEN :date_from AND :date_to
          AND ji.status_id <> {int(JUTE_ISSUE_EXCLUDED_STATUS)}
          AND ji.jute_mr_li_id IS NOT NULL
          AND COALESCE(ig.item_type_id, 0) <> {int(JUTE_WASTE_ITEM_TYPE_ID)}
        GROUP BY ji.issue_date
        """
    )


def get_winding_daily_kg_query():
    """Reconciled winding production kg per day — the WINDING denominator
    (TKT-000183, owner 2026-10-05: line waste divides by winding production).

    Same reconciliation as dprQueries.get_dpr_winding_mtd_query: per
    (branch, tran_date, spell, winder) SUM(doff production_qty) − opening
    jugar + closing jugar, jugar taken once per slot (MAX weight), same
    active filters — only rolled up by day instead of by yarn quality.
    Winding is branch-keyed, so :branch_id is required (no co_id filter,
    exactly like the DPR). Binds: :branch_id, :date_from, :date_to.
    """
    return text(
        """
        SELECT
            wdr.tran_date,
            ROUND(SUM(wdr.reconciled_qty), 3) AS production_kg
        FROM (
            SELECT wd.tran_date, wd.branch_id, wd.spell_id, wd.eb_id,
                   SUM(wd.production_qty)
                   - COALESCE(MAX(jo.open_w), 0)
                   + COALESCE(MAX(jc.close_w), 0) AS reconciled_qty
            FROM jute_prod_winding_doff wd
            LEFT JOIN (
                SELECT branch_id, tran_date, spell_id, eb_id, MAX(weight) AS open_w
                FROM jute_prod_winding_jugar
                WHERE tran_date BETWEEN :date_from AND :date_to
                  AND open_close = 'O'
                  AND (active = 1 OR active IS NULL)
                  AND branch_id = :branch_id
                GROUP BY branch_id, tran_date, spell_id, eb_id
            ) jo ON jo.branch_id = wd.branch_id
                AND jo.tran_date = wd.tran_date
                AND jo.spell_id = wd.spell_id
                AND jo.eb_id = wd.eb_id
            LEFT JOIN (
                SELECT branch_id, tran_date, spell_id, eb_id, MAX(weight) AS close_w
                FROM jute_prod_winding_jugar
                WHERE tran_date BETWEEN :date_from AND :date_to
                  AND open_close = 'C'
                  AND (active = 1 OR active IS NULL)
                  AND branch_id = :branch_id
                GROUP BY branch_id, tran_date, spell_id, eb_id
            ) jc ON jc.branch_id = wd.branch_id
                AND jc.tran_date = wd.tran_date
                AND jc.spell_id = wd.spell_id
                AND jc.eb_id = wd.eb_id
            WHERE wd.branch_id = :branch_id
              AND wd.tran_date BETWEEN :date_from AND :date_to
              AND (wd.active = 1 OR wd.active IS NULL)
            GROUP BY wd.tran_date, wd.branch_id, wd.spell_id, wd.eb_id
        ) wdr
        GROUP BY wdr.tran_date
        """
    )
