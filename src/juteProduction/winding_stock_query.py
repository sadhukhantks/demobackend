"""Winding stock query builders.

Every builder is arg-less and returns a text() statement; parameters go in the
db.execute(stmt, {...}) dict at the call site. Each named bind below MUST be
present as a key even when its value is None.

Grouped: CRUD, day reads, lock, report lookups.
"""

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from src.juteProcurement.constants import YARN_DOC_STATUS_APPROVED
from src.juteProcurement.reportQueries import _issue_cte
from src.juteProduction.constants import (
    PLANNING_YARN_ITEM_TYPE_ID,
)
from src.sales.constants import SALES_STATUS_IDS


# =============================================================================
# CRUD builders
# =============================================================================


def insert_winding_stock_row_query():
    """Insert one counted line (active = 1)."""
    return text(
        """
        INSERT INTO jute_prod_winding_stock
            (co_id, branch_id, tran_date, bucket, item_id, package_label,
             wastage_area_id, stock_loc, dept_id,
             no_of_samples, wt_per_sample, remarks,
             active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :bucket, :item_id, :package_label,
             :wastage_area_id, :stock_loc, :dept_id,
             :no_of_samples, :wt_per_sample, :remarks,
             1, :updated_by)
        """
    )


def get_winding_stock_active_by_id_query():
    """One active line by id, scoped to the caller's company.

    The co_id predicate is load-bearing: a tenant DB holds every company's rows,
    so matching on the primary key alone would let a user scoped to company A
    read — and through the edit/delete guards, mutate — company B's row.
    """
    return text(
        """
        SELECT winding_stock_id, co_id, branch_id, tran_date, bucket, item_id,
               package_label, wastage_area_id, stock_loc, dept_id,
               no_of_samples, wt_per_sample, remarks
        FROM jute_prod_winding_stock
        WHERE winding_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


def update_winding_stock_row_query():
    """Edit a line. tran_date and branch_id are immutable — delete and re-add to
    move a count to another day, mirroring the waste-entry precedent."""
    return text(
        """
        UPDATE jute_prod_winding_stock
        SET bucket          = :bucket,
            item_id         = :item_id,
            package_label   = :package_label,
            wastage_area_id = :wastage_area_id,
            stock_loc       = :stock_loc,
            dept_id         = :dept_id,
            no_of_samples   = :no_of_samples,
            wt_per_sample   = :wt_per_sample,
            remarks         = :remarks,
            updated_by      = :updated_by
        WHERE winding_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


def soft_delete_winding_stock_row_query():
    return text(
        """
        UPDATE jute_prod_winding_stock
        SET active = 0, updated_by = :updated_by
        WHERE winding_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


def get_winding_stock_duplicate_query():
    """Probe for an existing line with the same identity on the same day.

    NULL-safe on item_id / wastage_area_id / dept_id via the <=> operator, which
    a UNIQUE key cannot express — MySQL treats NULLs as distinct, so the
    constraint would let exactly the duplicates that matter through.

    stock_loc and dept_id are part of the identity (2026-09-25): the same
    package counted in the godown and on a department floor is two legitimate
    lines, not a duplicate.
    """
    return text(
        """
        SELECT winding_stock_id
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date = :tran_date
          AND bucket = :bucket
          AND item_id <=> :item_id
          AND package_label = :package_label
          AND wastage_area_id <=> :wastage_area_id
          AND stock_loc = :stock_loc
          AND dept_id <=> :dept_id
          AND active = 1
          AND (:exclude_id IS NULL OR winding_stock_id <> :exclude_id)
        LIMIT 1
        """
    )


# =============================================================================
# Day reads
# =============================================================================


def get_winding_stock_by_date_query():
    """All counted lines for a day, with the labels the grid and report print.

    yarn_count comes from jute_yarn_mst, whose item_id is NON-UNIQUE — the
    scalar subquery is deliberate: a JOIN here fans the row set out.

    stock_loc / dept_id / dept_desc (2026-09-25) are labels only: the report
    still sums every location, so adding them changes no total.
    """
    return text(
        """
        SELECT ws.winding_stock_id,
               ws.co_id,
               ws.branch_id,
               ws.tran_date,
               ws.bucket,
               ws.item_id,
               im.item_code,
               im.item_name,
               ig.item_grp_name AS quality_name,
               (SELECT ym.jute_yarn_count
                  FROM jute_yarn_mst ym
                 WHERE ym.item_id = ws.item_id
                 LIMIT 1) AS yarn_count,
               ws.package_label,
               ws.wastage_area_id,
               wa.area_name,
               ws.stock_loc,
               ws.dept_id,
               dm.dept_desc,
               ws.no_of_samples,
               ws.wt_per_sample,
               ws.remarks
        FROM jute_prod_winding_stock ws
        LEFT JOIN item_mst im ON im.item_id = ws.item_id
        LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN wastage_area_mst wa ON wa.wastage_area_id = ws.wastage_area_id
        LEFT JOIN dept_mst dm ON dm.dept_id = ws.dept_id
        WHERE ws.co_id = :co_id
          AND ws.branch_id = :branch_id
          AND ws.tran_date = :tran_date
          AND ws.active = 1
        ORDER BY ws.bucket, im.item_code, ws.package_label
        """
    )


def get_winding_stock_prev_entry_date_query():
    """The most recent date before :tran_date that has any counted line.

    Returns NULL when the chain has no earlier entry. The report compares this
    against the calendar previous day to decide whether the opening balance can
    be trusted, rather than silently reconciling against a stale count.
    """
    return text(
        """
        SELECT MAX(tran_date) AS prev_entry_date
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date < :tran_date
          AND active = 1
        """
    )


def get_winding_stock_wt_default_query():
    """Most recent saved kg-per-piece for a (co, branch, item, package) key.

    This IS the weight-per-sample "master": there is no separate table, so
    overtyping a row makes that value the next default.
    """
    return text(
        """
        SELECT wt_per_sample
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND item_id <=> :item_id
          AND package_label = :package_label
          AND active = 1
        ORDER BY tran_date DESC, winding_stock_id DESC
        LIMIT 1
        """
    )


def copy_winding_stock_previous_day_query():
    """Clone every line of :from_date into :tran_date, counts included.

    The operator then overtypes the numbers that changed. Guarded by the caller,
    which refuses when :tran_date already has rows.
    """
    return text(
        """
        INSERT INTO jute_prod_winding_stock
            (co_id, branch_id, tran_date, bucket, item_id, package_label,
             wastage_area_id, stock_loc, dept_id,
             no_of_samples, wt_per_sample, remarks,
             active, updated_by)
        SELECT co_id, branch_id, :tran_date, bucket, item_id, package_label,
               wastage_area_id, stock_loc, dept_id,
               no_of_samples, wt_per_sample, remarks,
               1, :updated_by
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date = :from_date
          AND active = 1
        """
    )


# =============================================================================
# Setup / lookup builders
# =============================================================================


def get_winding_stock_yarn_items_query():
    """Active yarn items for the picker.

    Company scope lives on item_grp_mst.co_id — item_mst has no co_id by design.
    jute_yarn_mst.item_id is non-unique, so the count is read as a scalar
    subquery; joining the table would duplicate items.
    """
    return text(
        f"""
        SELECT im.item_id,
               im.item_code,
               im.item_name,
               ig.item_grp_name AS quality_name,
               (SELECT ym.jute_yarn_count
                  FROM jute_yarn_mst ym
                 WHERE ym.item_id = im.item_id
                 LIMIT 1) AS yarn_count
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE im.active = 1
          AND ig.co_id = :co_id
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
        ORDER BY im.item_name
        """
    )


def get_winding_stock_areas_query():
    """Areas for the PLACE column — the existing wastage_area_mst master."""
    return text(
        """
        SELECT a.wastage_area_id, a.area_name, a.dept_id, a.branch_id
        FROM wastage_area_mst a
        WHERE a.active = 1
          AND a.co_id = :co_id
          AND (:branch_id IS NULL OR a.branch_id = :branch_id)
        ORDER BY a.area_name
        """
    )


def verify_winding_stock_item_query():
    """Single-row check that item_id is an active yarn item of this company.

    Separate from the picker query so create/edit costs one indexed lookup
    rather than fetching and scanning the whole item list.
    """
    return text(
        f"""
        SELECT im.item_id
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE im.item_id = :item_id
          AND im.active = 1
          AND ig.co_id = :co_id
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
        LIMIT 1
        """
    )


def verify_winding_stock_area_query():
    """Single-row check that the area belongs to this company and branch."""
    return text(
        """
        SELECT wastage_area_id
        FROM wastage_area_mst
        WHERE wastage_area_id = :wastage_area_id
          AND co_id = :co_id
          AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        LIMIT 1
        """
    )


def get_winding_stock_departments_query():
    """Departments for the FLOOR location picker.

    Owner ruling 2 (2026-09-25 spec §5): any dept_mst row of the branch, not a
    fixed list. dept_mst has no co_id and no active column — branch is the only
    scope it carries, so the caller must supply :branch_id (no NULL fallback,
    otherwise every branch's departments would be offered).
    """
    return text(
        """
        SELECT dept_id, dept_desc, dept_code
        FROM dept_mst
        WHERE branch_id = :branch_id
        ORDER BY order_id, dept_desc
        """
    )


def verify_winding_stock_dept_query():
    """Single-row check that dept_id is a department of the row's branch."""
    return text(
        """
        SELECT dept_id
        FROM dept_mst
        WHERE dept_id = :dept_id
          AND branch_id = :branch_id
        LIMIT 1
        """
    )


# =============================================================================
# Lock builders
# =============================================================================


def get_winding_stock_lock_row_query():
    return text(
        """
        SELECT winding_stock_lock_id, is_locked
        FROM jute_prod_winding_stock_lock
        WHERE co_id = :co_id AND tran_date = :tran_date
          AND (:branch_id IS NULL OR branch_id = :branch_id OR branch_id IS NULL)
          AND active = 1
        ORDER BY winding_stock_lock_id DESC
        LIMIT 1
        """
    )


def insert_winding_stock_lock_query():
    return text(
        """
        INSERT INTO jute_prod_winding_stock_lock
            (co_id, branch_id, tran_date, is_locked, processed_by,
             processed_date_time, active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, 1, :processed_by,
             CURRENT_TIMESTAMP, 1, :processed_by)
        """
    )


def update_winding_stock_lock_query():
    """Set the lock. The co_id predicate is load-bearing, same as
    get_winding_stock_active_by_id_query: a tenant DB holds every company's
    rows, and winding_stock_lock_id is a guessable sequential integer, so
    matching on it alone would let a user scoped to company A lock/unlock
    company B's day."""
    return text(
        """
        UPDATE jute_prod_winding_stock_lock
        SET is_locked = 1, processed_by = :processed_by,
            processed_date_time = CURRENT_TIMESTAMP,
            updated_by = :processed_by, updated_date_time = CURRENT_TIMESTAMP
        WHERE winding_stock_lock_id = :id AND co_id = :co_id
        """
    )


def clear_winding_stock_lock_query():
    """Clear the lock. The co_id predicate is load-bearing, same as
    get_winding_stock_active_by_id_query: a tenant DB holds every company's
    rows, and winding_stock_lock_id is a guessable sequential integer, so
    matching on it alone would let a user scoped to company A lock/unlock
    company B's day."""
    return text(
        """
        UPDATE jute_prod_winding_stock_lock
        SET is_locked = 0, updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE winding_stock_lock_id = :id AND co_id = :co_id
        """
    )


# =============================================================================
# Report lookups
# =============================================================================


def get_weaving_quality_type_map_query():
    """weaving_quality_id -> hessian/sacking, resolved through the cloth item.

    The classification lives on jute_prod_finishing_quality.quality_type
    (1 = Hessian, 2 = Sacking), keyed by item_id, and is joined here rather than
    duplicated onto the weaving master. quality_type is NULL for any quality
    whose cloth item has no finishing row; those group as OTHER, which keeps the
    three buckets summing to total weaving production.

    The DISTINCT subquery matters: one item can carry several finishing rows and
    a plain join would multiply the weaving qualities.
    """
    return text(
        """
        SELECT wq.weaving_quality_id, fq.quality_type
        FROM jute_prod_weaving_quality wq
        LEFT JOIN (
            SELECT DISTINCT item_id, quality_type
            FROM jute_prod_finishing_quality
            WHERE active = 1 AND co_id = :co_id
        ) fq ON fq.item_id = wq.item_id
        WHERE wq.co_id = :co_id
          AND wq.active = 1
        """
    )


def get_weaving_unresolved_quality_count_query():
    """Count weaving-daily rows the quality join would silently drop.

    Both weaving builders (get_weaving_entries_by_date_query / weaving_day_slice_sql
    for the day view, get_dpr_weaving_mtd_query for MTD) resolve a row's quality by
    joining jute_prod_weaving_daily to jute_prod_weaving_quality on
    weaving_quality_id. A row whose quality does not resolve contributes 0 to
    weaving production rather than raising anything — this counts those rows so
    the report can disclose it instead of presenting a silently-low weaving figure.

    Two failure modes, both caught by one NOT EXISTS: weaving_quality_id IS NULL
    (no join key at all — NULL never equals the correlated column, so the
    correlated subquery finds nothing and NOT EXISTS is true), and a non-NULL id
    pointing at a jute_prod_weaving_quality row that is missing or inactive
    (q.active = 1 in the subquery excludes it even though the id "resolves").

    Only rows with cuts > 0 count — a zero-cut row contributes nothing to
    production either way, so an unresolved quality on it is not disclosure-worthy.
    active = 1 excludes soft-deleted entry lines, which were never going to count.

    Verified on the sls production tenant (2026-08-19): branch 87 has 2,189 such
    rows across 2026, and on 2026-08-11 ALL 269 weaving rows with cuts > 0 had a
    NULL weaving_quality_id — weaving production for that day computed as 0.000
    though the mill demonstrably wove. Branch 29 is clean (0 of 5,096). This feeds
    the weaving_unresolved_day / weaving_unresolved_mtd counts in the
    /winding_weaving_difference response's warnings object (winding_reports.py) —
    see DIFFERENCE_NOTE there.

    Binds: :co_id, :branch_id (nullable, NULL-tolerant the same way the sibling
    weaving builders are), :date_from, :date_to.
    """
    return text(
        """
        SELECT COUNT(*) AS unresolved_count
        FROM jute_prod_weaving_daily wd
        WHERE wd.active = 1
          AND wd.cuts > 0
          AND wd.co_id = :co_id
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
          AND wd.tran_date BETWEEN :date_from AND :date_to
          AND NOT EXISTS (
              SELECT 1
              FROM jute_prod_weaving_quality q
              WHERE q.weaving_quality_id = wd.weaving_quality_id
                AND q.active = 1
          )
        """
    )


def get_sale_yarn_mt_query():
    """Yarn actually SOLD in a period, in kg, for one branch.

    Yarn is identified by the LINE's item type (item_grp_mst.item_type_id = 4),
    which catches yarn sold on any invoice type. Deliberately NOT filtered on
    sales_invoice.invoice_type: INVOICE_TYPE_IDS['JUTE_YARN'] is also 4, a
    numeric coincidence, and matching it would pull in non-yarn lines that ride
    a yarn invoice.

    sales_invoice_dtl has no active column — cancellation is excluded by the
    header status alone.

    TWISTED (ply) items are excluded. Under the twisting-netting model the
    report already subtracts twisting output as a proxy for the single yarn it
    consumed, so a ply SALE would subtract the same tonnage a second time —
    ply yarn is item_type_id 4 like every other yarn, so without this predicate
    it lands in sale_mt too.
    """
    return text(
        f"""
        SELECT ROUND(
                 SUM(COALESCE(NULLIF(sid.sales_weight, 0), sid.quantity, 0)), 3
               ) AS sale_kg
        FROM sales_invoice sinv
        INNER JOIN sales_invoice_dtl sid ON sid.invoice_id = sinv.invoice_id
        INNER JOIN item_mst im ON im.item_id = sid.item_id
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE sinv.branch_id = :branch_id
          AND sinv.status_id = {SALES_STATUS_IDS["APPROVED"]}
          AND COALESCE(sinv.active, 1) = 1
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
          AND sinv.invoice_date BETWEEN :date_from AND :date_to
          AND NOT EXISTS (
              SELECT 1
              FROM jute_yarn_mst ym
              WHERE ym.item_id = sid.item_id
                AND ym.is_twisted = 1
          )
        """
    )


def get_twisted_yarn_item_ids_query():
    """Items flagged as TWISTED (ply) output on the yarn master.

    Twisting has no module of its own: the mill keys ply output into the winding
    doff entry, so those doffs inflate book stock while the yarn they consumed is
    never subtracted. The flag is the only thing that tells the two apart.

    DISTINCT is load-bearing — jute_yarn_mst.item_id is NON-UNIQUE. No co_id
    predicate: item_id is unique tenant-wide, and jute_yarn_mst.co_id is the
    deprecated column (scoping lives on item_grp_mst), so filtering on it would
    drop legitimately-flagged items rather than protect anything.
    """
    return text(
        """
        SELECT DISTINCT item_id
        FROM jute_yarn_mst
        WHERE is_twisted = 1
          AND item_id IS NOT NULL
        """
    )


def fetch_twisted_yarn_item_ids(db):
    """Twisted (ply) item ids, or None on a tenant that has no is_twisted flag.

    None and an empty set mean different things and callers must not conflate
    them: None = "this tenant cannot tell twisting apart at all", empty set =
    "it can, and nothing is flagged". Reporting the first as the second would
    claim a clean split that never happened.

    is_twisted was added by TKT-000127's migration
    (dbqueries/migrations/add_is_twisted_to_jute_yarn_mst.sql) and is live on
    dev3 only — production tenants that have not run it yet (sls at the time of
    writing) have no such column, and referencing it raises 1054 Unknown column.
    Callers use this to net twisting out of winding; a report that cannot get
    the flag must degrade to "no twisting known", never to a 500.

    ponytail: swallow-and-degrade instead of a schema probe per call. Delete the
    fallback once every tenant has run the migration.
    """
    try:
        return {r.item_id for r in db.execute(get_twisted_yarn_item_ids_query()).fetchall()}
    except (OperationalError, ProgrammingError):
        # The failed statement poisons the session's transaction — MySQL will
        # reject every later query with "current transaction is aborted"
        # otherwise.
        db.rollback()
        return None


def get_jute_issue_weight_query():
    """Total jute issued for a branch over a date range, in KG.

    Reuses _issue_cte from the jute procurement reports rather than rewriting the
    eligibility rules: it already unions the waste-issue leg and applies the
    settled attribution rule (jute_mr_li.actual_item_id, not jute_issue.item_id)
    plus the MR-stock-eligible status filter.

    jute_issue.weight is in KG — the caller divides by 1000 for MT. The alias
    used to be `issue_qtl` and the docstring used to claim QUINTAL, which is what
    led the winding report to divide by 10 and print jute issue 100x too large.
    Live sls branch 29: weight/quantity is ~146.9 for BALE rows (a jute bale is
    ~150 KG; at quintal it would be a 14.7-tonne bale), and a second packaging
    cluster sits at exactly 40.0 per unit. src/juteProcurement/issuePlanner.py
    and jutePO.py both already treat this column as KG.
    """
    return text(
        f"""
        SELECT COALESCE(SUM(x.wt), 0) AS issue_kg
        FROM ({_issue_cte("{d} BETWEEN :date_from AND :date_to")}) x
        """
    )


def get_yarn_purchase_qty_query():
    """LEGACY Stores figure — kept as the transition cross-check only.

    Since phase 5 of the purchased-yarn design (2026-09-25, §3.4) purchase_mt
    reads the Yarn Purchase document (get_yarn_purchase_document_qty_query).
    This builder's figure now lands ONLY in warnings.legacy_stores_yarn_mt and
    never enters should_be_closing_mt; it exists so the report can say
    "Stores receipts of yarn exist but no Yarn Purchase does" instead of
    silently reading 0 during the sls backfill.

    'Purchase yarn' = approved store-receipt lines (proc_inward + proc_inward_dtl,
    sr_status = 3, line active = 1) whose item resolves to a yarn item group.
    Receipt date is proc_inward.inward_date — the column every stores report uses
    (src/inventory/reportQueries.py); sr_date diverges on about half the rows and
    has no documented rule.

    proc_inward_dtl has NO weight column, and approved_qty is in whatever uom the
    line carries. The distinct uom names are returned so the caller converts
    explicitly and flags anything it does not recognise, rather than assuming.

    KNOWN STATE: this returns 0 on sls today. Purchased yarn is really booked
    against item 334018 'spools/yarn', which sits in a MISC EXPENSES group with
    item_type_id = 1, so the yarn filter matches nothing. The filter is
    deliberately left honest — reclassifying that item into a yarn group makes
    this query start working with no code change.

    Company scope lives on item_grp_mst.co_id — item_mst has no co_id column.
    """
    return text(
        f"""
        SELECT COALESCE(SUM(pid.approved_qty), 0) AS purchase_qty,
               GROUP_CONCAT(DISTINCT um.uom_name) AS uoms
        FROM proc_inward_dtl pid
        INNER JOIN proc_inward pi ON pi.inward_id = pid.inward_id
        INNER JOIN item_mst im ON im.item_id = pid.item_id
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN uom_mst um ON um.uom_id = pid.uom_id
        WHERE pid.active = 1
          AND pi.sr_status = 3
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
          AND ig.co_id = :co_id
          AND pi.branch_id = :branch_id
          AND pi.inward_date BETWEEN :date_from AND :date_to
        """
    )


def get_yarn_purchase_document_qty_query():
    """Purchased yarn received for a co/branch over a date range, in KG, from
    the Yarn Purchase document (jute_yarn_purchase / _li).

    Phase 5 of docs/superpowers/specs/2026-09-25-yarn-purchase-issue-
    consumption-design.md §3.4: this replaces the Stores receipt figure as
    purchase_mt's source. Approved headers only (YARN_DOC_STATUS_APPROVED —
    only approved documents count as stock, §3.1), header AND line active,
    header tran_date in the window. weight_kg is the authoritative unit on the
    line, so there is no UOM to convert and nothing to guess; the caller
    divides by 1000 for MT (purchase_kg_to_mt).

    Binds: :co_id, :branch_id, :date_from, :date_to — the same four the legacy
    Stores builder takes, so one dict serves both.
    """
    return text(
        f"""
        SELECT COALESCE(SUM(li.weight_kg), 0) AS purchase_kg
        FROM jute_yarn_purchase_li li
        INNER JOIN jute_yarn_purchase h ON h.yarn_purchase_id = li.yarn_purchase_id
        WHERE h.active = 1
          AND li.active = 1
          AND h.status_id = {YARN_DOC_STATUS_APPROVED}
          AND h.co_id = :co_id
          AND h.branch_id = :branch_id
          AND h.tran_date BETWEEN :date_from AND :date_to
        """
    )


def get_yarn_purchase_document_detail_query():
    """Drill-down behind purchase_mt: one row per (Yarn Purchase document,
    yarn quality) in the window with the document number, so the figure is
    auditable against the paper (§3.4 "purchase by quality with document
    numbers").

    Same eligibility as get_yarn_purchase_document_qty_query, so the rows
    always sum to it. yarn_count is a scalar subquery because
    jute_yarn_mst.item_id is NON-UNIQUE (a JOIN would fan the lines out).
    Ordered by date then purchase number, the order the register is read in.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(
        f"""
        SELECT h.yarn_purchase_id,
               h.purchase_no,
               h.tran_date,
               li.item_id,
               im.item_name,
               (SELECT ym.jute_yarn_count
                  FROM jute_yarn_mst ym
                 WHERE ym.item_id = li.item_id
                 LIMIT 1) AS yarn_count,
               SUM(li.weight_kg) AS kg
        FROM jute_yarn_purchase_li li
        INNER JOIN jute_yarn_purchase h ON h.yarn_purchase_id = li.yarn_purchase_id
        LEFT JOIN item_mst im ON im.item_id = li.item_id
        WHERE h.active = 1
          AND li.active = 1
          AND h.status_id = {YARN_DOC_STATUS_APPROVED}
          AND h.co_id = :co_id
          AND h.branch_id = :branch_id
          AND h.tran_date BETWEEN :date_from AND :date_to
        GROUP BY h.yarn_purchase_id, h.purchase_no, h.tran_date, li.item_id, im.item_name
        ORDER BY h.tran_date, h.purchase_no, h.yarn_purchase_id, im.item_name
        """
    )
