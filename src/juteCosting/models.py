"""SQLAlchemy ORM model for the costing parameters master (Jute Costing, ITM-019).

One tenant-DB table (schema source of truth:
dbqueries/migrations/20261018_jute_cost_param.sql):

  jute_cost_param -- effective-dated EAV of the rates and norms the Daily P&L
                     prices with, one row per (co_id, branch_id, effective_date,
                     param_code, product). branch_id NULL = every branch of the
                     company, product NULL = every product. Resolution
                     (params.resolve_params): the active row with
                     MAX(effective_date) <= the report date, a branch-specific
                     row over an all-branch row, product-specific over
                     all-product.

The UNIQUE key uq_jute_cost_param (co_id, branch_id, effective_date, param_code,
product) is DDL-only and deliberately not declared here: MySQL treats NULL as
distinct inside a UNIQUE key, so the all-branch / all-product duplicate guard
lives in code (params_query.check_cost_param_duplicate_query).

Conventions follow low_performer_followup_models.py: shared Base from
src/models/mst.py, SQLAlchemy 2.0 Mapped / mapped_column, soft delete (active),
updated_by + updated_date_time (no created_* — DB triggers).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import TIMESTAMP, Date, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteCostParam(Base):
    """One effective-dated costing parameter value."""

    __tablename__ = "jute_cost_param"
    __table_args__ = (
        Index("idx_jcp_lookup", "co_id", "param_code", "effective_date"),
        Index("idx_jcp_scope", "co_id", "branch_id", "active"),
    )

    cost_param_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    param_code: Mapped[str] = mapped_column(String(40), nullable=False)
    param_value: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    uom: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    product: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
