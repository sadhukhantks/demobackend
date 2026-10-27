"""Bale Register report query builders
(GET /api/juteProductionReports/bale_register — spec
docs/superpowers/specs/2026-10-12-bale-stock-inspection-design.md §7c).

Every builder is arg-less and returns a text() statement; parameters go in
the db.execute(stmt, {...}) dict at the call site, and every named bind MUST
be a key there even when its value is None. ``:cancelled_status`` is an
EXPANDING bind (the caller passes a list, never a literal ``IN (4,)``).

The register is built from the SAME three event sources the Bale Stock page
derives its status from (bale_stock_query._bale_status_select, spec §3):

  * bales       — one jute_prod_bale_entry row is one physical bale (PROD or
                  REPACK; a REPACK row is a NEW bale, pressed on its date)
  * repacks     — each active REPACK and the bale it consumed (the §3 rule:
                  the most recent earlier active bale carrying its
                  original_bale_no in the same series = MAX(bale_entry_id))
  * despatches  — active jute_prod_bale_despatch rows joined LEFT to the
                  sales_invoice header; a link is LIVE when it has no invoice
                  (§7b challan / DO / transfer / sample) or the invoice is not
                  Rejected / Cancelled — a void link leaves the bale in stock

No status CASE, no grouping and no date arithmetic here: everything up to
``:date_to`` comes back flat and bale_register.build_bale_register (pure,
unit-tested) walks the calendar. Inspection is deliberately NOT read — the
owner ruled it out of this register (Finishing Stock report covers it).
"""

from sqlalchemy import bindparam, text

from src.juteProduction.constants import BALE_ENTRY_TYPE_REPACK
from src.juteProduction.finishing_stock_query import get_finishing_stock_unit_names_query

__all__ = [
    "get_finishing_stock_unit_names_query",
    "get_register_bales_query",
    "get_register_despatches_query",
    "get_register_repacks_query",
]


def get_register_bales_query():
    """Every active bale of the unit pressed on or before :date_to, with its
    quality / item / series labels.

    Both PROD and REPACK rows come back (entry_type tells them apart) because
    both are bales in stock from their tran_date on. Every label join is LEFT
    so a retired quality, item or series never drops a bale from the
    register. Ordered the way the report prints: quality type, item, series,
    bale number.

    Binds: co_id, branch_id, date_to.
    """
    return text(
        """
        SELECT be.bale_entry_id,
               be.tran_date,
               be.entry_type,
               be.bale_series_id,
               bs.series_name,
               be.finishing_quality_id,
               q.item_id,
               im.item_name,
               q.quality_type,
               be.bale_no,
               be.net_wt
        FROM jute_prod_bale_entry be
        LEFT JOIN jute_prod_finishing_quality q ON q.finishing_quality_id = be.finishing_quality_id
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        LEFT JOIN jute_prod_bale_series bs ON bs.bale_series_id = be.bale_series_id
        WHERE be.active = 1
          AND be.co_id = :co_id
          AND be.branch_id = :branch_id
          AND be.tran_date <= :date_to
        ORDER BY q.quality_type, im.item_name, bs.series_name, be.bale_no, be.bale_entry_id
        """
    )


def get_register_repacks_query():
    """Every active REPACK of the unit dated <= :date_to with the id of the
    bale it consumed (``consumed_id``, NULL when no earlier active bale of
    that number exists in the series).

    The consumption rule is byte-for-byte the §3 one used by the stock page:
    the MOST RECENT earlier active bale (MAX(bale_entry_id) below the REPACK)
    carrying the REPACK's original_bale_no in the same series — so a series
    reset that re-uses old numbers never reaches back into a previous cycle.
    The consumed bale leaves stock on the REPACK's tran_date.

    Binds: co_id, branch_id, date_to.
    """
    return text(
        f"""
        SELECT r.bale_entry_id,
               r.tran_date,
               r.bale_series_id,
               r.bale_no,
               r.original_bale_no,
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
          AND r.tran_date <= :date_to
        ORDER BY r.tran_date, r.bale_entry_id
        """
    )


def get_register_despatches_query():
    """Every active despatch link of the unit dated <= :date_to, with the
    invoice header (LEFT — NULL for a §7b no-invoice despatch) and the
    customer name (party_mst.supp_name is the display name for customers too).

    ``is_live`` is 1 when the link counts as a despatch: no invoice at all, or
    an invoice whose status is NULL (legacy header) or not in
    :cancelled_status (Rejected 4 / Cancelled 6). A 0 is a VOID link — the
    bale is back in stock and the register ignores the row — the same test
    the stock page's DESPATCHED arm applies.

    Binds: co_id, branch_id, date_to, cancelled_status (expanding).
    """
    return text(
        """
        SELECT dp.bale_despatch_id,
               dp.bale_entry_id,
               dp.despatch_date,
               dp.invoice_id,
               dp.despatch_reason,
               dp.despatch_ref,
               dp.remarks,
               si.invoice_no,
               si.invoice_date,
               si.status_id       AS invoice_status_id,
               p.supp_name        AS party_name,
               CASE WHEN dp.invoice_id IS NULL
                      OR si.status_id IS NULL
                      OR si.status_id NOT IN :cancelled_status
                    THEN 1 ELSE 0 END AS is_live
        FROM jute_prod_bale_despatch dp
        LEFT JOIN sales_invoice si ON si.invoice_id = dp.invoice_id
        LEFT JOIN party_mst p ON p.party_id = si.party_id
        WHERE dp.active = 1
          AND dp.co_id = :co_id
          AND dp.branch_id = :branch_id
          AND dp.despatch_date <= :date_to
        ORDER BY dp.despatch_date, dp.bale_despatch_id
        """
    ).bindparams(bindparam("cancelled_status", expanding=True))
