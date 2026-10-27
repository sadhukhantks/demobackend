"""Bale Stock & Inspection query builders (/api/baleStock + Finishing Stock §5).

Every builder is arg-less and returns a text() statement; parameters go in the
db.execute(stmt, {...}) dict at the call site. Each named bind below MUST be
present as a key even when its value is None. ``:cancelled_status``,
``:bale_entry_ids``, ``:invoice_types`` and ``:invoice_ids`` are EXPANDING binds
(bindparam(..., expanding=True)) — the caller passes a list, never a literal
``IN (4,)`` (a one-element Python tuple is a MySQL syntax error).

Bale stock is DERIVED (spec D1): one jute_prod_bale_entry row is one bale, and
its state as on a date is computed by ONE statement — the inner select built by
_bale_status_select() — which the grid (rows), the per-quality summary and
Finishing Stock all share, so a bale can never be "in stock" on one page and
"despatched" on another.

Grouped: status derivation (rows, per-quality summary, per-series summary),
setup lookups, invoice pickers, write builders (inspection, despatch incl. the
§7b link-later UPDATE).
"""

from sqlalchemy import bindparam, text

from src.juteProduction.constants import (
    BALE_ENTRY_TYPE_REPACK,
    BALE_STOCK_STATUS_DESPATCHED,
    BALE_STOCK_STATUS_IN_STOCK,
    BALE_STOCK_STATUS_INSPECTED,
    BALE_STOCK_STATUS_REPACKED,
)

# =============================================================================
# Status derivation
# =============================================================================

# The optional grid filters (spec §3 + the §7e invoice filter). Only the rows
# builder carries them; the summary is always the whole branch as on the date.
# ``:invoice_id`` matches the bale's ACTIVE despatch row (the dp LEFT JOIN, as
# on the date) — live or void, so the invoice page lists both; a bale with no
# despatch row has dp.invoice_id NULL and never matches a given id.
_OPTIONAL_FILTERS_SQL = """
          AND (:quality_type IS NULL OR q.quality_type = :quality_type)
          AND (:finishing_quality_id IS NULL OR be.finishing_quality_id = :finishing_quality_id)
          AND (:bale_series_id IS NULL OR be.bale_series_id = :bale_series_id)
          AND (:bale_no_from IS NULL OR be.bale_no >= :bale_no_from)
          AND (:bale_no_to IS NULL OR be.bale_no <= :bale_no_to)
          AND (:date_from IS NULL OR be.tran_date >= :date_from)
          AND (:date_to IS NULL OR be.tran_date <= :date_to)
          AND (:bale_entry_ids_set = 0 OR be.bale_entry_id IN :bale_entry_ids)
          AND (:invoice_id IS NULL OR dp.invoice_id = :invoice_id)
"""


def _bale_status_select(with_filters: bool) -> str:
    """The one SQL that says what state a bale is in as on :as_on (spec §3).

    Status CASE, in priority order:
      REPACKED    an active REPACK row in the same series, dated <= :as_on,
                  carries this bale's number as original_bale_no. A REPACK
                  consumes the MOST RECENT earlier active bale with that number
                  (MAX(bale_entry_id) below it), so a series reset that re-uses
                  old numbers never reaches back into the previous cycle.
      DESPATCHED  an active despatch row dated <= :as_on that either carries
                  NO invoice (dp.invoice_id IS NULL — challan / DO / transfer /
                  sample, §7b, never void) or whose invoice header is not in
                  :cancelled_status (NULL status = legacy header, kept as
                  live). A link to a cancelled invoice is VOID: the bale falls
                  through to the next arm and reads as back in stock, but the
                  dp.* / si.* columns still come back so the UI can show
                  "linked to cancelled invoice" and offer undo / re-link (D4).
      INSPECTED   an active marking dated <= :as_on.
      IN_STOCK    none of the above.

    Every join is LEFT so a retired quality / series / party never hides a bale.
    party_mst.supp_name is the display name for customers too (bale_query note).
    """
    filters = _OPTIONAL_FILTERS_SQL if with_filters else ""
    return f"""
        SELECT be.bale_entry_id,
               be.tran_date,
               be.entry_type,
               be.bale_series_id,
               bs.series_name,
               be.bale_no,
               be.original_bale_no,
               be.finishing_quality_id,
               q.item_id,
               im.item_name,
               q.quality_type,
               be.gross_wt,
               be.net_wt,
               be.weigh_time,
               rp.bale_entry_id   AS repacked_by_id,
               rp.bale_no         AS repacked_by_no,
               rp.tran_date       AS repacked_on,
               ins.bale_inspection_id,
               ins.inspection_date,
               ins.inspection_ref,
               ins.inspector,
               ins.remarks        AS inspection_remarks,
               dp.bale_despatch_id,
               dp.invoice_id,
               dp.despatch_date,
               dp.despatch_reason,
               dp.despatch_ref,
               dp.remarks         AS despatch_remarks,
               si.invoice_no,
               si.invoice_date,
               si.invoice_type,
               si.status_id       AS invoice_status_id,
               p.supp_name        AS party_name,
               CASE WHEN rp.bale_entry_id IS NOT NULL THEN '{BALE_STOCK_STATUS_REPACKED}'
                    WHEN dp.bale_despatch_id IS NOT NULL
                         AND (dp.invoice_id IS NULL
                              OR si.status_id IS NULL
                              OR si.status_id NOT IN :cancelled_status)
                         THEN '{BALE_STOCK_STATUS_DESPATCHED}'
                    WHEN ins.bale_inspection_id IS NOT NULL THEN '{BALE_STOCK_STATUS_INSPECTED}'
                    ELSE '{BALE_STOCK_STATUS_IN_STOCK}' END AS status
        FROM jute_prod_bale_entry be
        LEFT JOIN jute_prod_finishing_quality q ON q.finishing_quality_id = be.finishing_quality_id
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        LEFT JOIN jute_prod_bale_series bs ON bs.bale_series_id = be.bale_series_id
        -- a REPACK consumes the most recent earlier active bale carrying its
        -- original number in the series
        LEFT JOIN (
            SELECT r.bale_entry_id, r.bale_no, r.tran_date,
                   (SELECT MAX(b.bale_entry_id) FROM jute_prod_bale_entry b
                     WHERE b.bale_series_id = r.bale_series_id
                       AND b.bale_no = r.original_bale_no
                       AND b.active = 1
                       AND b.bale_entry_id < r.bale_entry_id) AS consumed_id
            FROM jute_prod_bale_entry r
            WHERE r.entry_type = '{BALE_ENTRY_TYPE_REPACK}'
              AND r.active = 1
              AND r.original_bale_no IS NOT NULL
              AND r.co_id = :co_id
              AND r.branch_id = :branch_id
              AND r.tran_date <= :as_on
        ) rp ON rp.consumed_id = be.bale_entry_id
        LEFT JOIN jute_prod_bale_inspection ins
               ON ins.bale_entry_id = be.bale_entry_id
              AND ins.active = 1
              AND ins.inspection_date <= :as_on
        LEFT JOIN jute_prod_bale_despatch dp
               ON dp.bale_entry_id = be.bale_entry_id
              AND dp.active = 1
              AND dp.despatch_date <= :as_on
        LEFT JOIN sales_invoice si ON si.invoice_id = dp.invoice_id
        LEFT JOIN party_mst p ON p.party_id = si.party_id
        WHERE be.active = 1
          AND be.co_id = :co_id
          AND be.branch_id = :branch_id
          AND be.tran_date <= :as_on
          {filters}
    """


def get_bale_stock_rows_query():
    """The bale grid (GET /bales), the §7e suggested pick (GET /suggest) AND the
    id-list fetch behind every write gate.

    Every filter is optional (``:x IS NULL OR …``). When no id list is given the
    caller passes ``bale_entry_ids_set = 0`` and ``bale_entry_ids = [0]`` (the
    expanding bind must not be empty). ``:status_filter`` is a grid code
    (STOCK / NOT_INSPECTED / INSPECTED / DESPATCHED / REPACKED) or NULL for ALL;
    it is applied OUTSIDE the derivation so it filters on the computed status.
    ``:invoice_id`` (§7e) keeps only bales whose active despatch row carries
    that invoice — live or void (combine with status ALL for the invoice
    page's read-only list); NULL = no invoice filter.

    Capped by ``LIMIT :row_cap`` — the router asks for cap + 1 and reports
    ``truncated`` when it gets them, rather than paging a stock register.

    Binds: co_id, branch_id, as_on, cancelled_status (expanding), quality_type,
    finishing_quality_id, bale_series_id, bale_no_from, bale_no_to, date_from,
    date_to, bale_entry_ids_set, bale_entry_ids (expanding), invoice_id,
    status_filter, row_cap.
    """
    return text(
        f"""
        SELECT * FROM (
        {_bale_status_select(with_filters=True)}
        ) x
        WHERE (:status_filter IS NULL
               OR (:status_filter = 'STOCK'
                   AND x.status IN ('{BALE_STOCK_STATUS_IN_STOCK}', '{BALE_STOCK_STATUS_INSPECTED}'))
               OR (:status_filter = 'NOT_INSPECTED' AND x.status = '{BALE_STOCK_STATUS_IN_STOCK}')
               OR (:status_filter IN ('{BALE_STOCK_STATUS_INSPECTED}',
                                      '{BALE_STOCK_STATUS_DESPATCHED}',
                                      '{BALE_STOCK_STATUS_REPACKED}')
                   AND x.status = :status_filter))
        ORDER BY x.quality_type, x.series_name, x.bale_no, x.bale_entry_id
        LIMIT :row_cap
        """
    ).bindparams(
        bindparam("cancelled_status", expanding=True),
        bindparam("bale_entry_ids", expanding=True),
    )


def get_bale_stock_summary_query():
    """Per-quality stock figures as on :as_on (GET /summary, Finishing Stock §5).

    The same derivation, grouped by quality: ``bales`` / ``total_kg`` count only
    the in-stock pair (IN_STOCK + INSPECTED), ``inspected_bales`` /
    ``inspected_kg`` the INSPECTED ones, and ``despatched_bales`` is the
    to-date despatch count for information — it reads the derived status
    column, so it counts no-invoice despatches (§7b) and skips void links
    exactly as the grid does. total_kg is the ACTUAL weighed net (Σ net_wt,
    D6) — never count x standard.

    Ordered quality_type, item_name — hessian before sacking, as every
    finishing report prints them.

    Binds: co_id, branch_id, as_on, cancelled_status (expanding).
    """
    in_stock = f"('{BALE_STOCK_STATUS_IN_STOCK}', '{BALE_STOCK_STATUS_INSPECTED}')"
    return text(
        f"""
        SELECT x.finishing_quality_id,
               x.item_id,
               x.item_name,
               x.quality_type,
               SUM(CASE WHEN x.status IN {in_stock} THEN 1 ELSE 0 END) AS bales,
               SUM(CASE WHEN x.status = '{BALE_STOCK_STATUS_INSPECTED}' THEN 1 ELSE 0 END)
                   AS inspected_bales,
               SUM(CASE WHEN x.status IN {in_stock} THEN x.net_wt ELSE 0 END) AS total_kg,
               SUM(CASE WHEN x.status = '{BALE_STOCK_STATUS_INSPECTED}' THEN x.net_wt ELSE 0 END)
                   AS inspected_kg,
               SUM(CASE WHEN x.status = '{BALE_STOCK_STATUS_DESPATCHED}' THEN 1 ELSE 0 END)
                   AS despatched_bales
        FROM (
        {_bale_status_select(with_filters=False)}
        ) x
        GROUP BY x.finishing_quality_id, x.item_id, x.item_name, x.quality_type
        ORDER BY x.quality_type, x.item_name
        """
    ).bindparams(bindparam("cancelled_status", expanding=True))


def get_bale_stock_series_summary_query():
    """Per-SERIES stock figures as on :as_on (spec §7a) — the series breakup
    shown next to the per-quality summary.

    Sacking bales are numbered per consignee series (PUNJAB, BIHAR, FCI …) and
    that is how the mill despatches and gets them inspected, so the same
    derivation is grouped by (series, quality) with the SAME measures as
    get_bale_stock_summary_query: ``bales`` / ``total_kg`` for the in-stock
    pair, ``inspected_bales`` / ``inspected_kg`` for INSPECTED, and
    ``despatched_bales`` to date. Every quality type comes back — the callers
    filter to sacking where the view is sacking-only. A bale whose series row
    is gone still counts (LEFT JOIN: NULL series_name).

    Ordered quality_type, series_name, item_name.

    Binds: co_id, branch_id, as_on, cancelled_status (expanding) — identical
    to the quality summary, so one params dict serves both statements.
    """
    in_stock = f"('{BALE_STOCK_STATUS_IN_STOCK}', '{BALE_STOCK_STATUS_INSPECTED}')"
    return text(
        f"""
        SELECT x.bale_series_id,
               x.series_name,
               x.finishing_quality_id,
               x.item_id,
               x.item_name,
               x.quality_type,
               SUM(CASE WHEN x.status IN {in_stock} THEN 1 ELSE 0 END) AS bales,
               SUM(CASE WHEN x.status = '{BALE_STOCK_STATUS_INSPECTED}' THEN 1 ELSE 0 END)
                   AS inspected_bales,
               SUM(CASE WHEN x.status IN {in_stock} THEN x.net_wt ELSE 0 END) AS total_kg,
               SUM(CASE WHEN x.status = '{BALE_STOCK_STATUS_INSPECTED}' THEN x.net_wt ELSE 0 END)
                   AS inspected_kg,
               SUM(CASE WHEN x.status = '{BALE_STOCK_STATUS_DESPATCHED}' THEN 1 ELSE 0 END)
                   AS despatched_bales
        FROM (
        {_bale_status_select(with_filters=False)}
        ) x
        GROUP BY x.bale_series_id, x.series_name, x.finishing_quality_id,
                 x.item_id, x.item_name, x.quality_type
        ORDER BY x.quality_type, x.series_name, x.item_name
        """
    ).bindparams(bindparam("cancelled_status", expanding=True))


# =============================================================================
# Setup lookups
# =============================================================================


def get_bale_stock_series_query():
    """Active bale series of the company (optionally one branch) — the grid's
    series filter and the bale-number-range selector. Slimmer than
    bale_query.get_bale_series_list_query (no joins, no bale counts): the picker
    needs a name, a type and the bound item only.
    """
    return text(
        """
        SELECT bale_series_id, series_name, quality_type, item_id
        FROM jute_prod_bale_series
        WHERE co_id = :co_id
          AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        ORDER BY quality_type, series_name
        """
    )


def get_bale_stock_source_query():
    """Does the unit have ANY active bale entry dated <= :as_on? One row = yes.

    Decides ``bale_source`` (D7): BALE_PROD when a row comes back — own-company
    bales are then derived and Finishing Stock's manual own-company BALE lines
    are superseded — else MANUAL and nothing changes. Branch-tolerant
    (``:branch_id`` NULL = any branch of the company) for the setup calls that
    have no branch yet. LIMIT 1: existence, not a count.
    """
    return text(
        """
        SELECT 1 AS found
        FROM jute_prod_bale_entry
        WHERE co_id = :co_id
          AND (:branch_id IS NULL OR branch_id = :branch_id)
          AND active = 1
          AND tran_date <= :as_on
        LIMIT 1
        """
    )


# =============================================================================
# Invoice pickers (GET /invoices) and the despatch gate
# =============================================================================


def get_bale_stock_invoices_query():
    """Candidate invoices for a despatch link (spec §4 /invoices).

    sales_invoice has NO co_id — a branch belongs to one company, so branch_id IS
    the scope (same note as finishing_stock_query.get_finishing_stock_despatch_query).
    status_id is NULL on older imported headers, so the cancelled filter is
    NULL-safe, and active is COALESCEd (NULL = never cancelled). Newest first.

    Binds: branch_id, invoice_types (expanding), cancelled_status (expanding),
    date_from, as_on.
    """
    return text(
        """
        SELECT si.invoice_id,
               si.invoice_no,
               si.invoice_date,
               si.invoice_type,
               it.invoice_type_name,
               si.party_id,
               p.supp_name AS party_name,
               si.status_id
        FROM sales_invoice si
        LEFT JOIN invoice_type_mst it ON it.invoice_type_id = si.invoice_type
        LEFT JOIN party_mst p ON p.party_id = si.party_id
        WHERE si.branch_id = :branch_id
          AND si.invoice_type IN :invoice_types
          AND COALESCE(si.active, 1) = 1
          AND (si.status_id IS NULL OR si.status_id NOT IN :cancelled_status)
          AND si.invoice_date >= :date_from
          AND si.invoice_date <= :as_on
        ORDER BY si.invoice_date DESC, si.invoice_id DESC
        """
    ).bindparams(
        bindparam("invoice_types", expanding=True),
        bindparam("cancelled_status", expanding=True),
    )


def get_bale_stock_invoice_lines_query():
    """Cloth lines of a set of invoices, with the bale count the invoice carried.

    Only cloth items (``item_grp_mst.item_type_id = :cloth_item_type_id``) — a
    freight or packing line is not something a bale is linked to. qty_bales comes
    from sales_invoice_hessian_dtl, which exists only for bale-billed lines
    (LEFT JOIN, NULL when absent; 1:1 so the quantity is not fanned out).

    Binds: invoice_ids (expanding), cloth_item_type_id.
    """
    return text(
        """
        SELECT d.invoice_id,
               d.invoice_line_item_id,
               d.item_id,
               im.item_name,
               d.quantity,
               um.uom_name,
               h.qty_bales
        FROM sales_invoice_dtl d
        INNER JOIN item_mst im ON im.item_id = d.item_id
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN sales_invoice_hessian_dtl h
               ON h.invoice_line_item_id = d.invoice_line_item_id
        LEFT JOIN uom_mst um ON um.uom_id = d.uom_id
        WHERE d.invoice_id IN :invoice_ids
          AND ig.item_type_id = :cloth_item_type_id
        ORDER BY d.invoice_id, d.invoice_line_item_id
        """
    ).bindparams(bindparam("invoice_ids", expanding=True))


def get_bale_stock_invoice_linked_counts_query():
    """How many bales are already linked (active rows) to each of a set of
    invoices — the picker shows it so a second partial link is deliberate.

    Binds: invoice_ids (expanding).
    """
    return text(
        """
        SELECT invoice_id, COUNT(*) AS linked_bales
        FROM jute_prod_bale_despatch
        WHERE active = 1
          AND invoice_id IN :invoice_ids
        GROUP BY invoice_id
        """
    ).bindparams(bindparam("invoice_ids", expanding=True))


def get_bale_stock_invoice_row_query():
    """One invoice header for the POST /despatch gates: exists, right branch,
    allowed type, not Rejected / Cancelled, and its date (the default
    despatch_date). active is COALESCEd so the router can treat a soft-deleted
    header as not found.
    """
    return text(
        """
        SELECT si.invoice_id,
               si.branch_id,
               si.invoice_type,
               si.status_id,
               si.invoice_date,
               COALESCE(si.active, 1) AS active
        FROM sales_invoice si
        WHERE si.invoice_id = :invoice_id
        """
    )


# =============================================================================
# Write builders — inspection
# =============================================================================


def insert_bale_inspection_query():
    """Mark one bale inspected (active = 1). One statement per bale; the router
    has already proved every bale is a sacking bale currently in stock."""
    return text(
        """
        INSERT INTO jute_prod_bale_inspection
            (co_id, branch_id, bale_entry_id, inspection_date, inspection_ref,
             inspector, remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :bale_entry_id, :inspection_date, :inspection_ref,
             :inspector, :remarks, 1, :updated_by)
        """
    )


def soft_delete_bale_inspections_query():
    """Un-mark: soft-delete the active markings of a set of bales.

    Joined to jute_prod_bale_entry so ``be.co_id = :co_id`` scopes the update —
    the marking table carries co_id too, but the BALE's company is the
    authority, and a caller cannot un-mark another company's bale by id.
    rowcount 0 -> the router answers 404 (nothing was marked).

    Binds: updated_by, bale_entry_ids (expanding), co_id.
    """
    return text(
        """
        UPDATE jute_prod_bale_inspection x
        JOIN jute_prod_bale_entry be ON be.bale_entry_id = x.bale_entry_id
        SET x.active = 0,
            x.updated_by = :updated_by
        WHERE x.bale_entry_id IN :bale_entry_ids
          AND be.co_id = :co_id
          AND x.active = 1
        """
    ).bindparams(bindparam("bale_entry_ids", expanding=True))


# =============================================================================
# Write builders — despatch link
# =============================================================================


def insert_bale_despatch_query():
    """Despatch one bale (active = 1): against an invoice (:invoice_id set) or
    without one (:invoice_id NULL, :despatch_reason a BALE_DESPATCH_REASONS
    code, :despatch_ref the challan / DO no — §7b). invoice_line_item_id is
    bound NULL today (reserved for a per-line pick); despatch_date is the
    router's resolved date (body value, or the invoice date when an invoice is
    given).

    Binds: co_id, branch_id, bale_entry_id, invoice_id, invoice_line_item_id,
    despatch_date, despatch_reason, despatch_ref, remarks, updated_by.
    """
    return text(
        """
        INSERT INTO jute_prod_bale_despatch
            (co_id, branch_id, bale_entry_id, invoice_id, invoice_line_item_id,
             despatch_date, despatch_reason, despatch_ref, remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :bale_entry_id, :invoice_id, :invoice_line_item_id,
             :despatch_date, :despatch_reason, :despatch_ref, :remarks, 1, :updated_by)
        """
    )


def update_bale_despatch_invoice_query():
    """"Link to invoice" later (§7b): fill invoice_id in on an EXISTING
    no-invoice despatch row. One statement per bale, keyed by the row's
    bale_despatch_id. Reason / ref / remarks are only overwritten when the
    body carries a value (COALESCE keeps what was recorded on the day the bale
    left); despatch_date is deliberately NOT touched — the bale left on that
    day, the invoice merely followed.

    Joined to jute_prod_bale_entry so ``be.co_id = :co_id`` scopes the update
    (same authority rule as the soft-delete builders), and guarded by
    ``dp.active = 1 AND dp.invoice_id IS NULL`` so a row that is already linked
    or retired is never rewritten — rowcount 0 tells the router the bale's
    state moved under it.

    Binds: invoice_id, despatch_reason, despatch_ref, remarks, updated_by,
    bale_despatch_id, co_id.
    """
    return text(
        """
        UPDATE jute_prod_bale_despatch dp
        JOIN jute_prod_bale_entry be ON be.bale_entry_id = dp.bale_entry_id
        SET dp.invoice_id = :invoice_id,
            dp.despatch_reason = COALESCE(:despatch_reason, dp.despatch_reason),
            dp.despatch_ref = COALESCE(:despatch_ref, dp.despatch_ref),
            dp.remarks = COALESCE(:remarks, dp.remarks),
            dp.updated_by = :updated_by
        WHERE dp.bale_despatch_id = :bale_despatch_id
          AND be.co_id = :co_id
          AND dp.active = 1
          AND dp.invoice_id IS NULL
        """
    )


def soft_delete_bale_despatches_query():
    """Undo despatch: soft-delete the active despatch rows of a set of bales —
    with or without an invoice (same shape and scoping as
    soft_delete_bale_inspections_query). Also used by POST /despatch to retire
    a VOID link (cancelled invoice) before re-linking, so a bale never carries
    two active rows.

    Binds: updated_by, bale_entry_ids (expanding), co_id.
    """
    return text(
        """
        UPDATE jute_prod_bale_despatch x
        JOIN jute_prod_bale_entry be ON be.bale_entry_id = x.bale_entry_id
        SET x.active = 0,
            x.updated_by = :updated_by
        WHERE x.bale_entry_id IN :bale_entry_ids
          AND be.co_id = :co_id
          AND x.active = 1
        """
    ).bindparams(bindparam("bale_entry_ids", expanding=True))
