"""Jute Gain/Loss Sheet — monthly manual statement replacing the mill's Excel.

Six tenant-DB tables:

  jute_gl_row_mst              -- figure-row master (Jute Issue, Yarn Purchase,
                                  Sale Yarn, ...). Which statements include a
                                  row (in_fng/in_wvg/in_wdg), its section and
                                  its +/- sign are DATA, so tenants add or
                                  re-scope rows without a release.
  jute_gl_dept_mst             -- process-stock departments. FNG spans all
                                  active rows; in_wvg marks Batching→Weaving,
                                  in_wdg Winding→Weaving. "Singur Shed" is a
                                  plain row here, not a constant.
  jute_gain_loss_hdr           -- one sheet per (co, branch, month). Draft→Lock.
  jute_gain_loss_dtl           -- Hess/Sack MT per figure row.
  jute_gain_loss_prod_dtl      -- quality-wise production lines: statement
                                  'FNG' (packed) or 'WVG' (weaving).
  jute_gain_loss_process_stock -- month-end opening/closing Hess/Sack per dept.
                                  Opening is stored (prefilled from the prior
                                  month's closing) so a locked sheet is
                                  self-contained.

All quantities are MT. Everything is manual entry by design (user decision
2026-08-25): the sheet replaces the Excel one-for-one; deriving figures from
production entries is a later, separate step.

Schema source of truth: dbqueries/migrations/20260825_jute_gain_loss_tables.sql
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import TIMESTAMP, Date, Double, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteGlRowMst(Base):
    """One named line of the statement (e.g. 'Yarn Purchase')."""

    __tablename__ = "jute_gl_row_mst"
    __table_args__ = (Index("idx_gl_row_co", "co_id"),)

    gl_row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    row_code: Mapped[str] = mapped_column(String(40), nullable=False)
    row_name: Mapped[str] = mapped_column(String(150), nullable=False)
    section: Mapped[str] = mapped_column(String(10), nullable=False)  # INPUT | OUTPUT
    calc_sign: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # +1 Add, -1 Less
    in_fng: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    in_wvg: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    in_wdg: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteGlDeptMst(Base):
    """A process-stock department row of the statement."""

    __tablename__ = "jute_gl_dept_mst"
    __table_args__ = (Index("idx_gl_dept_co", "co_id"),)

    gl_dept_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dept_code: Mapped[str] = mapped_column(String(40), nullable=False)
    dept_name: Mapped[str] = mapped_column(String(150), nullable=False)
    in_wvg: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    in_wdg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteGainLossHdr(Base):
    """One monthly sheet per (co, branch, month). month_date = 1st of month."""

    __tablename__ = "jute_gain_loss_hdr"
    __table_args__ = (Index("idx_gl_hdr_unit", "co_id", "branch_id", "month_date"),)

    gain_loss_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    month_date: Mapped[date] = mapped_column(Date, nullable=False)
    remarks: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    is_locked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    locked_date_time: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteGainLossDtl(Base):
    """Hess/Sack MT captured for one figure row of one sheet."""

    __tablename__ = "jute_gain_loss_dtl"
    __table_args__ = (Index("idx_gl_dtl_hdr", "gain_loss_id"),)

    gain_loss_dtl_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gain_loss_id: Mapped[int] = mapped_column(Integer, nullable=False)
    gl_row_id: Mapped[int] = mapped_column(Integer, nullable=False)
    hess_qty: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    sack_qty: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteGainLossProdDtl(Base):
    """One quality-wise production line.

    statement 'FNG' = packed production (the FNG output block), 'WVG' = weaving
    production (the WVG output block; WDG reuses the WVG totals). quality_name
    is free text so a quality without a finishing-master row can still be
    reported; the FE offers finishing-quality names as suggestions.
    """

    __tablename__ = "jute_gain_loss_prod_dtl"
    __table_args__ = (Index("idx_gl_prod_hdr", "gain_loss_id"),)

    gain_loss_prod_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gain_loss_id: Mapped[int] = mapped_column(Integer, nullable=False)
    statement: Mapped[str] = mapped_column(String(3), nullable=False)  # FNG | WVG
    quality_type: Mapped[int] = mapped_column(Integer, nullable=False)  # 1=Hessian, 2=Sacking
    quality_name: Mapped[str] = mapped_column(String(255), nullable=False)
    qty: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteGainLossProcessStock(Base):
    """Month-end process stock of one department on one sheet."""

    __tablename__ = "jute_gain_loss_process_stock"
    __table_args__ = (Index("idx_gl_ps_hdr", "gain_loss_id"),)

    gain_loss_ps_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gain_loss_id: Mapped[int] = mapped_column(Integer, nullable=False)
    gl_dept_id: Mapped[int] = mapped_column(Integer, nullable=False)
    opening_hess: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    opening_sack: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    closing_hess: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    closing_sack: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
