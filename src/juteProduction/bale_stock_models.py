"""Bale Stock & Inspection — the two event tables behind the DERIVED bale stock.

There is no bale-stock table. One ``jute_prod_bale_entry`` row is one physical
bale (Bale Production, bale_models.JuteProdBaleEntry), so "bales in stock" is a
query, not a count somebody keys in: every active bale pressed on or before the
as-on date that has not since (a) been opened for a REPACK (an active REPACK row
in the same series carries its number as ``original_bale_no``), (b) been
despatched — linked to a live sales invoice, or sent out without one on a
challan / delivery order / transfer / sample (§7b) — or (c) been soft-deleted
(spec D1 / D2). The two tables here record the two events that are NOT already
on the bale row:

  jute_prod_bale_inspection  the bale was inspected (sacking / govt supply only,
                             D3): date, certificate / note ref, agency, remarks.
                             Un-marking is a soft delete. A REPACK of an inspected
                             bale starts uninspected — the seal is broken.
  jute_prod_bale_despatch    the bale left the mill (D4 / §7b): against a sales
                             invoice (``invoice_id``), or WITHOUT one — ``invoice_id``
                             NULL, ``despatch_reason`` says why (CHALLAN, DELIVERY_ORDER,
                             TRANSFER, SAMPLE, OTHER) and ``despatch_ref`` carries the
                             challan / DO / gate-pass number; "Link to invoice" later
                             fills ``invoice_id`` in on the SAME row, the despatch date
                             staying the day it left. A link whose invoice is later
                             Rejected / Cancelled is VOID at read time (the bale reads
                             as back in stock) and is never deleted by the system; the
                             user undoes or re-links it. A no-invoice despatch is never
                             void.

Every figure is as on a date D (D5): bale ``tran_date <= D``, repacked only by a
REPACK dated ``<= D``, despatched only by a link with ``despatch_date <= D``,
inspected only by a marking with ``inspection_date <= D``. The derivation lives
in ONE statement, bale_stock_query.get_bale_stock_rows_query(), which the summary
builder and Finishing Stock reuse rather than re-deriving.

One ACTIVE row per bale in each table is an app-level rule (soft-delete pattern,
no UNIQUE key — exactly as bale_models does for (series, bale_no)). The write
endpoints evaluate the bale's CURRENT state before inserting, so a second live
marking or link can never be created, and the LEFT JOINs in the rows query can
never fan a bale out into two rows.

Conventions follow bale_models.py: shared Base from src/models/mst.py, SQLAlchemy
2.0 Mapped / mapped_column, co_id + branch_id NOT NULL, soft delete via
``active`` (default 1), audit columns updated_by + updated_date_time only — DB
triggers handle the rest, so NO created_* columns.

Schema source of truth:
dbqueries/migrations/20261012_jute_prod_bale_inspection_despatch.sql
dbqueries/migrations/20261012b_jute_prod_bale_despatch_no_invoice.sql
  (invoice_id NULLable, + despatch_reason, despatch_ref)
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

# Registers jute_prod_bale_entry on the shared metadata so the FKs below resolve
# even when this module is the first jute-production model imported.
from src.juteProduction import bale_models  # noqa: F401
from src.models.mst import Base


class JuteProdBaleInspection(Base):
    """One inspection marking of one sacking bale (the inspected-goods register).

    ``inspection_date`` is the date the bale counts as inspected from (D5);
    ``inspection_ref`` is the certificate / inspection-note number and
    ``inspector`` the agency or person. Soft delete = un-mark.
    """

    __tablename__ = "jute_prod_bale_inspection"
    __table_args__ = (
        Index("idx_bi_bale", "bale_entry_id", "active"),
        Index("idx_bi_co_branch_date", "co_id", "branch_id", "inspection_date"),
    )

    bale_inspection_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # the inspected bale — a jute_prod_bale_entry row with quality_type 2 (sacking)
    bale_entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jute_prod_bale_entry.bale_entry_id"), nullable=False
    )
    inspection_date: Mapped[date] = mapped_column(Date, nullable=False)
    # certificate / inspection-note no
    inspection_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # agency / inspector name
    inspector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdBaleDespatch(Base):
    """One despatch of one bale: it left the mill against a sales invoice, or
    without one (§7b).

    ``despatch_date`` is the date the bale stops counting as stock (D5) — the
    body date, defaulting to the invoice date when an invoice is given.
    ``invoice_id`` is NULL for a despatch without invoice; ``despatch_reason``
    (constants.BALE_DESPATCH_REASONS code) is then required and ``despatch_ref``
    holds the challan / DO / gate-pass number. Linking later UPDATEs
    ``invoice_id`` on this row and leaves ``despatch_date`` alone.
    ``invoice_line_item_id`` is reserved for a future per-line pick and is NULL
    today. A link is void — not deleted — while its invoice header is Rejected
    (4) / Cancelled (6); a no-invoice despatch is never void.
    """

    __tablename__ = "jute_prod_bale_despatch"
    __table_args__ = (
        Index("idx_bd_bale", "bale_entry_id", "active"),
        Index("idx_bd_invoice", "invoice_id"),
        Index("idx_bd_co_branch_date", "co_id", "branch_id", "despatch_date"),
    )

    bale_despatch_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bale_entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jute_prod_bale_entry.bale_entry_id"), nullable=False
    )
    # sales_invoice.invoice_id — no FK: sales_invoice is a legacy table outside
    # this module's migrations (the despatch report joins it the same way).
    # NULL = despatched without an invoice (see despatch_reason), §7b.
    invoice_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # sales_invoice_dtl.invoice_line_item_id, optional (future per-line pick)
    invoice_line_item_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    despatch_date: Mapped[date] = mapped_column(Date, nullable=False)
    # constants.BALE_DESPATCH_REASONS code — required when invoice_id is NULL
    despatch_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # challan / delivery-order / gate-pass number, free text
    despatch_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
