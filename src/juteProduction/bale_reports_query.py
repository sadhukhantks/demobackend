"""SQL builders for the two Bale Production reports
(/api/juteProductionReports/bale_daily_sheet, /bale_heavy_light).

Arg-less functions returning sqlalchemy.text() with named binds — the
arithmetic (F1–F8 of docs/bale-production-spec-sheet.md §5) lives in the pure
builders of bale_reports.py, so every query here is a flat SELECT / GROUP BY.

Sources:
  * bales             — jute_prod_bale_entry (one row per bale; PROD | REPACK,
                        snapshots of trolly_wt / packsheet_wt / std_bale_wt at
                        save), joined to jute_prod_finishing_quality for item,
                        type and the current standards
  * MR% readings      — jute_sqc_packing_mr (SQC Packing-MR page). item-level
                        rows (item_id set) first, quality_group HESSIAN|SACKING
                        rows as the fallback; branch filter NULL-tolerant like
                        juteSQC.query.get_packing_mr_by_date_query
  * weft count        — jute_prod_finishing_quality.weft_yarn_item_id →
                        jute_yarn_mst.item_id → jute_yarn_count (scalar
                        subquery so a duplicated yarn row can never fan out
                        the bale aggregate)
"""

from sqlalchemy import text


def get_branch_name_query():
    """The (branch, co) pair must exist and be active; returns its name for the
    daily-sheet header (doubles as the scope guard)."""
    return text(
        """
        SELECT branch_id, branch_name
        FROM branch_mst
        WHERE branch_id = :branch_id AND co_id = :co_id AND active = 1
        LIMIT 1
        """
    )


def get_bale_daily_sheet_rows_query():
    """Every active bale of one (co, branch, date), optionally narrowed to a
    spell and/or an entry type, with the labels the printed sheet shows.

    Ordered quality-first (type, item, quality id) then by weigh_time so the
    builder can group into per-quality blocks in one pass. Both the row
    snapshots (std_bale_wt, packsheet_wt) and the quality master's current
    values are returned; the builder prefers the snapshot.
    """
    return text(
        """
        SELECT
            be.bale_entry_id,
            be.entry_type,
            be.finishing_quality_id,
            fq.item_id,
            im.item_name,
            fq.quality_type,
            be.std_bale_wt,
            fq.std_bale_weight AS quality_std_bale_wt,
            be.packsheet_wt,
            fq.packsheet_wt AS quality_packsheet_wt,
            be.bale_series_id,
            bs.series_name,
            be.bale_no,
            be.original_bale_no,
            be.spell_id,
            sp.spell_code,
            be.machine_id,
            be.gross_wt,
            be.trolly_id,
            t.trolly_name,
            be.trolly_wt,
            be.net_wt,
            be.weigh_time,
            be.remarks
        FROM jute_prod_bale_entry be
        INNER JOIN jute_prod_finishing_quality fq
                ON fq.finishing_quality_id = be.finishing_quality_id
        INNER JOIN item_mst im ON im.item_id = fq.item_id
        LEFT JOIN jute_prod_bale_series bs ON bs.bale_series_id = be.bale_series_id
        LEFT JOIN spell_mst sp ON sp.spell_id = be.spell_id
        LEFT JOIN trolly_mst t ON t.trolly_id = be.trolly_id
        WHERE be.co_id = :co_id
          AND be.branch_id = :branch_id
          AND be.tran_date = :tran_date
          AND be.active = 1
          AND (:spell_id IS NULL OR be.spell_id = :spell_id)
          AND (:entry_type IS NULL OR be.entry_type = :entry_type)
        ORDER BY fq.quality_type, im.item_name, be.finishing_quality_id,
                 be.weigh_time, be.bale_entry_id
        """
    )


def get_bale_heavy_light_day_rows_query():
    """PROD bales aggregated per (finishing quality, day) over a date range.

    bales / total_net feed F1 and F5; std_bale_wt is the row snapshot (F8 —
    a later standard edit must not rewrite history), falling back to the
    quality master only for legacy rows saved without one. std_mr_pct and the
    weft count come from the quality master. :entry_type carries 'PROD' so
    REPACK rows never enter HY/LT (spec D2 / Q4).
    """
    return text(
        """
        SELECT
            be.finishing_quality_id,
            be.tran_date,
            fq.item_id,
            im.item_name,
            fq.quality_type,
            COUNT(*) AS bales,
            SUM(be.net_wt) AS total_net,
            MAX(COALESCE(be.std_bale_wt, fq.std_bale_weight)) AS std_bale_wt,
            fq.std_mr_pct,
            (SELECT MAX(ym.jute_yarn_count)
               FROM jute_yarn_mst ym
              WHERE ym.item_id = fq.weft_yarn_item_id) AS weft_count
        FROM jute_prod_bale_entry be
        INNER JOIN jute_prod_finishing_quality fq
                ON fq.finishing_quality_id = be.finishing_quality_id
        INNER JOIN item_mst im ON im.item_id = fq.item_id
        WHERE be.co_id = :co_id
          AND be.branch_id = :branch_id
          AND be.active = 1
          AND be.entry_type = :entry_type
          AND be.tran_date BETWEEN :date_from AND :date_to
        GROUP BY be.finishing_quality_id, be.tran_date, fq.item_id, im.item_name,
                 fq.quality_type, fq.std_mr_pct, fq.weft_yarn_item_id
        ORDER BY fq.quality_type, im.item_name, be.finishing_quality_id, be.tran_date
        """
    )


def get_packing_mr_item_daily_query():
    """Packing-MR daily average per (entry_date, item) — the item-level MR
    source of F7. Only rows that name an item; mean of the stored 10-reading
    averages (every row carries exactly 10 readings, so this equals the
    reading-weighted mean the SQC page shows)."""
    return text(
        """
        SELECT pm.entry_date, pm.item_id, AVG(pm.calc_avg_mr) AS avg_mr
        FROM jute_sqc_packing_mr pm
        WHERE pm.co_id = :co_id
          AND pm.active = 1
          AND pm.item_id IS NOT NULL
          AND pm.calc_avg_mr IS NOT NULL
          AND pm.entry_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR pm.branch_id = :branch_id OR pm.branch_id IS NULL)
        GROUP BY pm.entry_date, pm.item_id
        """
    )


def get_packing_mr_group_daily_query():
    """Packing-MR daily average per (entry_date, quality_group HESSIAN|SACKING)
    — the type-level fallback of F7, over every row of the group (item-tagged
    or not), as the sheet's MR% tab does."""
    return text(
        """
        SELECT pm.entry_date, pm.quality_group, AVG(pm.calc_avg_mr) AS avg_mr
        FROM jute_sqc_packing_mr pm
        WHERE pm.co_id = :co_id
          AND pm.active = 1
          AND pm.calc_avg_mr IS NOT NULL
          AND pm.entry_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR pm.branch_id = :branch_id OR pm.branch_id IS NULL)
        GROUP BY pm.entry_date, pm.quality_group
        """
    )
