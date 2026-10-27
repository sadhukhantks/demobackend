"""Winding stock — the daily counted yarn snapshot and its per-day lock.

Two tenant-DB tables:

  jute_prod_winding_stock       -- one counted line per (date, bucket, yarn item,
                                   package label, area). The FIRST counted-snapshot
                                   table in this ERP; every other stock figure in
                                   the system is derived from movements.
  jute_prod_winding_stock_lock  -- one lock header per (co, branch, date); once
                                   locked, mutating the day needs Edit permission.

MT is NEVER stored: it is no_of_samples * wt_per_sample / 1000, computed at read,
so correcting a weight fixes history instead of stranding a derived value.

There is no weight-per-sample master. The default offered on a new line is the
most recent saved wt_per_sample for the same (co, branch, item, package) key —
one lookup against this same table.

Schema source of truth:
dbqueries/migrations/create_jute_prod_winding_stock.sql
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


class JuteProdWindingStock(Base):
    """One counted stock line for a date.

    ``item_id`` is nullable on purpose: lines such as "BEAM ON FLOOR-HESS",
    "PURCHASE PW FOR S4" and the four waste lines (THREAD WASTE, DIRTY BOBBIN,
    DAMAGE SPOOL, ENTANGLE COPS) have no jute_yarn_mst row. ``package_label``
    carries them, and is therefore mandatory on every row.

    No spell: the sheet is an as-on-date count, not a shift entry.
    """

    __tablename__ = "jute_prod_winding_stock"
    __table_args__ = (
        Index("idx_winding_stock_co_branch_date", "co_id", "branch_id", "tran_date"),
        Index("idx_winding_stock_item", "item_id"),
        Index("idx_winding_stock_area", "wastage_area_id"),
        Index("idx_winding_stock_loc_dept", "stock_loc", "dept_id"),
    )

    winding_stock_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    # one of constants.WINDING_STOCK_BUCKETS
    bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    # yarn item; NULL where no jute_yarn_mst row exists for the line
    item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    package_label: Mapped[str] = mapped_column(String(60), nullable=False)
    wastage_area_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # one of constants.WINDING_STOCK_LOCATIONS (2026-09-25); existing rows are FLOOR
    stock_loc: Mapped[str] = mapped_column(
        String(10), nullable=False, default="FLOOR", server_default="FLOOR"
    )
    # department the FLOOR line belongs to; NULL for GODOWN lines and legacy rows
    dept_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    no_of_samples: Mapped[float] = mapped_column(DECIMAL(12, 3), nullable=False)
    wt_per_sample: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=False)  # kg/piece
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdWindingStockLock(Base):
    """One lock header per (co, branch, tran_date).

    Mirrors jute_prod_weaving_process_lock minus spell_id — the stock count has
    no spell. Locking is an explicit action, never a side effect of running the
    report: a GET must not mutate.
    """

    __tablename__ = "jute_prod_winding_stock_lock"
    __table_args__ = (Index("idx_wstocklock_unit", "co_id", "tran_date"),)

    winding_stock_lock_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_locked: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    processed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    processed_date_time: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
