"""Finishing stock query builders.

Every builder is arg-less and returns a text() statement; parameters go in the
db.execute(stmt, {...}) dict at the call site. Each named bind below MUST be
present as a key even when its value is None.

Grouped: CRUD, day reads, setup lookups, report lookups.
"""

from sqlalchemy import bindparam, text

from src.sales.constants import SALES_STATUS_IDS

# Invoice header states that mean "this despatch did not happen". Passed as a
# BIND (not interpolated): a one-element tuple renders as `(4,)` in an f-string,
# which is valid Python and a MySQL syntax error, so dropping a status from this
# list would 500 every report. The router passes it.
DESPATCH_EXCLUDED_STATUS_IDS = (
    SALES_STATUS_IDS["REJECTED"],
    SALES_STATUS_IDS["CANCELLED"],
)


# =============================================================================
# CRUD builders
# =============================================================================


def insert_finishing_stock_row_query():
    """Insert one counted lot (active = 1).

    No duplicate probe, unlike winding stock: several lines for the same
    (stage, quality) are separate LOTS, which is how the shop-floor sheet
    records them (19, 45, 27, 2 …). The report sums them.
    """
    return text(
        """
        INSERT INTO jute_prod_finishing_stock
            (co_id, branch_id, tran_date, bucket, finishing_quality_id, stage,
             work_by, owner_co_id, qty, qty_uom, wt_per_unit_kg, wt_kg, remarks,
             active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :bucket, :finishing_quality_id, :stage,
             :work_by, :owner_co_id, :qty, :qty_uom, :wt_per_unit_kg, :wt_kg, :remarks,
             1, :updated_by)
        """
    )


def get_finishing_stock_active_by_id_query():
    """One active line by id, scoped to the caller's company.

    The co_id predicate is load-bearing: a tenant DB holds every company's rows,
    so matching on the primary key alone would let a user scoped to company A
    read — and through the edit/delete guards, mutate — company B's row.
    """
    return text(
        """
        SELECT finishing_stock_id, co_id, branch_id, tran_date, bucket,
               finishing_quality_id, stage, work_by, owner_co_id, qty, qty_uom,
               wt_per_unit_kg, wt_kg, remarks
        FROM jute_prod_finishing_stock
        WHERE finishing_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


def update_finishing_stock_row_query():
    """Edit a line. co_id / branch_id / tran_date are immutable — the identity of
    a physical count is (unit, date); delete and re-add to move it."""
    return text(
        """
        UPDATE jute_prod_finishing_stock
        SET bucket               = :bucket,
            finishing_quality_id = :finishing_quality_id,
            stage                = :stage,
            work_by              = :work_by,
            owner_co_id          = :owner_co_id,
            qty                  = :qty,
            qty_uom              = :qty_uom,
            wt_per_unit_kg       = :wt_per_unit_kg,
            wt_kg                = :wt_kg,
            remarks              = :remarks,
            updated_by           = :updated_by
        WHERE finishing_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


def soft_delete_finishing_stock_row_query():
    return text(
        """
        UPDATE jute_prod_finishing_stock
        SET active = 0, updated_by = :updated_by
        WHERE finishing_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


# =============================================================================
# Day reads
# =============================================================================


def get_finishing_stock_by_date_query():
    """Every counted lot of a day, with the labels the grid and report print.

    Also feeds GET /report — the report needs exactly these rows and the same
    row_to_dict() shape, so it reuses this builder rather than carrying a
    byte-identical twin that would drift.

    The quality / item joins are LEFT so a quality later deactivated on the
    master cannot make a counted row vanish from its own day.

    owner_co_name is NULL here for own-company lines; the router fills in the
    company's own name (it has to fetch it anyway for the report header).

    Ordering is the sheet's: bales first, then Hessian before Sacking, item,
    stage, and finally id so repeated lots of one (stage, quality) keep their
    entry order.
    """
    return text(
        """
        SELECT s.finishing_stock_id,
               s.co_id,
               s.branch_id,
               s.tran_date,
               s.bucket,
               s.finishing_quality_id,
               q.item_id,
               im.item_code,
               im.item_name,
               q.quality_type,
               s.stage,
               s.work_by,
               s.owner_co_id,
               oc.co_name AS owner_co_name,
               s.qty,
               s.qty_uom,
               s.wt_per_unit_kg,
               s.wt_kg,
               s.remarks
        FROM jute_prod_finishing_stock s
        LEFT JOIN jute_prod_finishing_quality q
               ON q.finishing_quality_id = s.finishing_quality_id
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        LEFT JOIN co_mst oc ON oc.co_id = s.owner_co_id
        WHERE s.co_id = :co_id
          AND s.branch_id = :branch_id
          AND s.tran_date = :tran_date
          AND s.active = 1
        ORDER BY CASE s.bucket WHEN 'BALE' THEN 0 ELSE 1 END,
                 q.quality_type, im.item_name, s.stage, s.finishing_stock_id
        """
    )


def get_finishing_stock_prev_entry_date_query():
    """The most recent date before :tran_date that has any counted lot.

    Returns NULL when nothing earlier exists. Drives the copy-previous-day hint
    and the copy itself, so the operator never retypes ~30 near-identical rows.
    """
    return text(
        """
        SELECT MAX(tran_date) AS prev_entry_date
        FROM jute_prod_finishing_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date < :tran_date
          AND active = 1
        """
    )


def copy_finishing_stock_previous_day_query():
    """Clone every lot of :from_date into :tran_date, counts included.

    The operator then overtypes what changed. Guarded by the caller, which
    refuses when :tran_date already has rows — so it can never silently double
    a count.
    """
    return text(
        """
        INSERT INTO jute_prod_finishing_stock
            (co_id, branch_id, tran_date, bucket, finishing_quality_id, stage,
             work_by, owner_co_id, qty, qty_uom, wt_per_unit_kg, wt_kg, remarks,
             active, updated_by)
        SELECT co_id, branch_id, :tran_date, bucket, finishing_quality_id, stage,
               work_by, owner_co_id, qty, qty_uom, wt_per_unit_kg, wt_kg, remarks,
               1, :updated_by
        FROM jute_prod_finishing_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date = :from_date
          AND active = 1
        """
    )


# =============================================================================
# Setup / lookup builders
# =============================================================================


def get_finishing_stock_qualities_query():
    """Active Finishing-Quality rows of a company — the entry picker AND the
    report's zero-fill list.

    Branch-tolerant the same way get_finishing_qualities_query is: quality is
    effectively company-level, and rows carrying a NULL branch_id belong to
    every branch.

    std_bale_weight is the per-bale default a BALE line starts from; it is NULL
    for the misc loose categories (joints, short bags …), which are never
    pressed.
    """
    return text(
        """
        SELECT q.finishing_quality_id,
               q.item_id,
               im.item_code,
               im.item_name,
               q.quality_type,
               q.std_bale_weight,
               q.packsheet_wt,
               q.no_of_bags
        FROM jute_prod_finishing_quality q
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        WHERE q.co_id = :co_id
          AND q.active = 1
          AND (:branch_id IS NULL OR q.branch_id = :branch_id OR q.branch_id IS NULL)
        ORDER BY q.quality_type, im.item_name
        """
    )


def get_finishing_stock_companies_query():
    """Every company in the tenant — the BALE owner picker.

    Deliberately unfiltered: the mill presses bales FOR its group companies, so
    the list is exactly the point. co_mst has no active column.
    """
    return text(
        """
        SELECT co_id, co_name
        FROM co_mst
        ORDER BY co_name
        """
    )


def verify_finishing_stock_owner_co_query():
    """Single-row check that an owner company exists.

    Separate from the picker query so create/edit costs one indexed lookup
    rather than fetching and scanning the whole company list.
    """
    return text(
        """
        SELECT co_id
        FROM co_mst
        WHERE co_id = :owner_co_id
        LIMIT 1
        """
    )


def get_finishing_stock_unit_names_query():
    """Own company name + branch name for the report header.

    Scalar subqueries rather than a join so the statement always returns exactly
    one row — a missing branch yields NULL branch_name instead of no header at
    all. The co_name doubles as owner_co_name for own-company lines.
    """
    return text(
        """
        SELECT (SELECT co_name FROM co_mst WHERE co_id = :co_id) AS co_name,
               (SELECT branch_name FROM branch_mst WHERE branch_id = :branch_id)
                   AS branch_name
        """
    )


def get_finishing_stock_cut_weights_query():
    """Kilograms in ONE full cut, per cloth item, from the Weaving Quality Master.

    ``finished_length`` (yds per cut) x ``ozs_yds`` (oz per yd) x 28.35 / 1000 —
    the same oz->kg arithmetic weaving's production_kg uses, so a cut weighs the
    same number here and on the weaving report.

    One row per item_id: several active weaving qualities can share a cloth item,
    and the newest (highest weaving_quality_id) is the construction the floor is
    running, so the MAX() subquery picks it rather than fanning the item out into
    two conflicting cut weights.

    Rows with a zero / NULL length or oz-per-yd are excluded rather than
    zero-filled: a 0.000 kg prefill reads as an answer, a missing row reads as
    "weigh it yourself", which is the truth.
    """
    return text(
        """
        SELECT wq.item_id,
               ROUND(wq.finished_length * wq.ozs_yds * 28.35 / 1000, 3) AS cut_wt_kg
        FROM jute_prod_weaving_quality wq
        INNER JOIN (
            SELECT item_id, MAX(weaving_quality_id) AS weaving_quality_id
            FROM jute_prod_weaving_quality
            WHERE co_id = :co_id
              AND active = 1
              AND finished_length > 0
              AND ozs_yds > 0
            GROUP BY item_id
        ) newest ON newest.weaving_quality_id = wq.weaving_quality_id
        WHERE wq.co_id = :co_id
        """
    )


def get_finishing_stock_pcs_per_bundle_query():
    """Pieces in one bundle, per finishing quality, from the finishing spec sheet.

    The spec sheet is effective-dated EAV, so the row that applies on :as_of is
    the latest effective_date <= :as_of for that ref_id — hence the MAX()
    subquery. process / id_type / value_role / param arrive as BINDS (values from
    src/juteProduction/constants.py) so this stays a plain text() like every other
    builder here.

    Ordered oldest-first: a caller building {ref_id: value} then keeps the LAST
    row when two spec rows share one effective_date, which is the same
    "newest wins" answer the spec-sheet grid gives.
    """
    return text(
        """
        SELECT tm.ref_id AS finishing_quality_id,
               tm.value AS pcs_per_bundle
        FROM jute_prod_finishing_target_map tm
        INNER JOIN (
            SELECT ref_id, MAX(effective_date) AS effective_date
            FROM jute_prod_finishing_target_map
            WHERE co_id = :co_id
              AND process = :process
              AND id_type = :id_type
              AND value_role = :value_role
              AND param = :param
              AND active = 1
              AND effective_date <= :as_of
            GROUP BY ref_id
        ) latest ON latest.ref_id = tm.ref_id
                AND latest.effective_date = tm.effective_date
        WHERE tm.co_id = :co_id
          AND tm.process = :process
          AND tm.id_type = :id_type
          AND tm.value_role = :value_role
          AND tm.param = :param
          AND tm.active = 1
        ORDER BY tm.finishing_target_map_id
        """
    )


# =============================================================================
# Report lookups
# =============================================================================


def get_finishing_stock_despatch_query():
    """Cloth despatched on the date, per item — the report's despatch block.

    Derived from sales invoices rather than counted: what left the gate is
    already recorded once, and re-keying it on the stock sheet would let the two
    disagree.

    sales_invoice has NO co_id column — a branch belongs to exactly one company,
    so branch_id IS the scope. status_id is NULL on older imported headers, so
    the cancelled/rejected filter is written NULL-safe; active is COALESCEd for
    the same reason (the column is nullable and NULL means "never cancelled").

    bales come from sales_invoice_hessian_dtl, which exists only for bale-billed
    lines — hence the LEFT JOIN and a NULL (not 0) bales figure when no line of
    the item carried one. invoice_line_item_id is 1:1 there, so the join does not
    fan the quantity out.

    :excluded_status is an EXPANDING bind and :cloth_item_type_id an ordinary
    one — the values still come from the constants, but through the params dict,
    so the statement stays a plain text() like every other builder here.
    """
    return text(
        """
        SELECT d.item_id,
               im.item_name,
               SUM(h.qty_bales) AS bales,
               SUM(d.quantity) AS qty,
               MIN(um.uom_name) AS uom_name
        FROM sales_invoice si
        INNER JOIN sales_invoice_dtl d ON d.invoice_id = si.invoice_id
        INNER JOIN item_mst im ON im.item_id = d.item_id
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN sales_invoice_hessian_dtl h
               ON h.invoice_line_item_id = d.invoice_line_item_id
        LEFT JOIN uom_mst um ON um.uom_id = d.uom_id
        WHERE si.invoice_date = :tran_date
          AND si.branch_id = :branch_id
          AND COALESCE(si.active, 1) = 1
          AND (si.status_id IS NULL
               OR si.status_id NOT IN :excluded_status)
          AND ig.item_type_id = :cloth_item_type_id
        GROUP BY d.item_id, im.item_name
        ORDER BY im.item_name
        """
    ).bindparams(bindparam("excluded_status", expanding=True))
