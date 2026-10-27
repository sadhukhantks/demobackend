"""Finishing stock — the daily physical count of the finishing department.

One tenant-DB table, jute_prod_finishing_stock, holding two kinds of line
distinguished by ``bucket``:

  BALE   pressed bales waiting to be despatched. ``owner_co_id`` names the group
         company the bales belong to (NULL = own company) — the mill presses for
         more than one company and the report prints one block per owner.
  LOOSE  finishing WIP: cuts / bundles / pieces sitting at a stage (hemming,
         herackle, branded pressing …), counted AND weighed.

The second counted-snapshot table in this ERP, after jute_prod_winding_stock;
every other stock figure in the system is derived from movements.

total_kg is NEVER stored: it is wt_kg when not NULL else qty * wt_per_unit_kg,
computed at read, so correcting a weight fixes history. MT = kg / 1000 at 3 dp.

Duplicate (stage, quality) lines are legitimate — they are separate lots, which
is why there is no UNIQUE key and no duplicate probe (unlike winding stock).

Schema source of truth:
dbqueries/migrations/create_jute_prod_finishing_stock.sql
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    DECIMAL,
    TIMESTAMP,
    Date,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteProdFinishingStock(Base):
    """One counted lot for a date — a stack of bales or a pile of loose cuts."""

    __tablename__ = "jute_prod_finishing_stock"
    __table_args__ = (
        Index("idx_finstock_co_branch_date", "co_id", "branch_id", "tran_date"),
        Index("idx_finstock_quality", "finishing_quality_id"),
        Index("idx_finstock_owner", "owner_co_id"),
    )

    finishing_stock_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    # one of constants.FINISHING_STOCK_BUCKETS
    bucket: Mapped[str] = mapped_column(String(10), nullable=False)
    # jute_prod_finishing_quality — the item IS the quality identity, and its
    # quality_type (1 Hessian / 2 Sacking) drives the report's two sections.
    finishing_quality_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # LOOSE only; a constants.FINISHING_STOCK_STAGE_CODES value, or NULL when
    # the counter did not record where the pile was standing.
    stage: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # LOOSE only; 0 = Dept, 1 = Contract — the sheet blocks loose stock by who
    # is doing the work.
    work_by: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # BALE only; co_mst.co_id. NULL means the own company — normalised that way
    # on write so "own" is one value, never two.
    owner_co_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    qty: Mapped[float] = mapped_column(DECIMAL(12, 3), nullable=False)
    # 'BALE' for bales; CUTS / BDL / PCS for loose.
    qty_uom: Mapped[str] = mapped_column(String(10), nullable=False)
    # BALE: kg per bale, defaulted from the quality's std_bale_weight but
    # editable per line — bales of one quality genuinely differ by lot.
    wt_per_unit_kg: Mapped[Optional[float]] = mapped_column(
        DECIMAL(10, 3), nullable=True
    )
    # LOOSE: the weighed total for the lot.
    wt_kg: Mapped[Optional[float]] = mapped_column(DECIMAL(14, 3), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
