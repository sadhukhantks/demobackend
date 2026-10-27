"""Wastage Report mapping master.

One tenant-DB table, jute_waste_report_map: which (waste item, department)
lands in which report group, and which production figure each group's
percentage divides by — day and monthly-total denominators are SEPARATE
columns because the mill's format divides them by different bases for some
groups (kept as-is by user decision 2026-08-25).

dept_id NULL = the item maps to this group from any department; an exact
(item, dept) row beats the NULL-dept row. Waste entries with no mapping land
in the report's OTHER bucket — the mapping can never silently hide waste.

Schema source of truth: dbqueries/migrations/20260825_jute_waste_report_map.sql
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import TIMESTAMP, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteWasteReportMap(Base):
    """One mapping row: (item [, dept]) -> report group + % denominators."""

    __tablename__ = "jute_waste_report_map"
    __table_args__ = (Index("idx_waste_rpt_map_co", "co_id"),)

    waste_report_map_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    group_code: Mapped[str] = mapped_column(String(40), nullable=False)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # one of constants.WASTE_REPORT_DENOMS
    day_denom: Mapped[str] = mapped_column(String(20), nullable=False, default="NONE")
    total_denom: Mapped[str] = mapped_column(String(20), nullable=False, default="NONE")
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dept_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    col_label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
