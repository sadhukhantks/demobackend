"""SQLAlchemy ORM models for the Low Performer Follow-up (Jute Production).

Two tenant-DB tables (schema source of truth:
dbqueries/migrations/20261007_jute_prod_low_performer_followup.sql):

  jute_prod_followup_person -- one follow-up of one worker (eb_id) in one department
                               (WEAVING / SPINNING / WINDING). status 1 = open,
                               0 = closed. At most one OPEN active row per
                               (co_id, branch_id, eb_id, dept_code): guarded in code
                               AND by the functional unique key uq_jpfp_open
                               (20261007b_jute_prod_followup_open_unique.sql; DDL-only,
                               not declared here).
  jute_prod_followup_event  -- the dated timeline of a follow-up: REMARK, NOTICE
                               (fixed notice types, scanned letter in file_attachment
                               under module 'jute_prod_followup' / file_type 'notice')
                               and HOURS_CUT (spell + hours entered by the supervisor,
                               NOT read from attendance or payroll).

Production / efficiency is never stored: the router reads it live from the Low
Producer computations (lowprod_weaving / lowprod_spinning / lowprod_winding).

Conventions follow loom_modification_models.py: shared Base from src/models/mst.py,
SQLAlchemy 2.0 Mapped / mapped_column, soft delete (active), updated_by +
updated_date_time (no created_* — DB triggers).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import TIMESTAMP, Date, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteProdFollowupPerson(Base):
    """One follow-up of one worker in one department."""

    __tablename__ = "jute_prod_followup_person"
    __table_args__ = (
        Index("idx_jpfp_scope", "co_id", "branch_id", "dept_code", "status", "active"),
        Index("idx_jpfp_eb", "eb_id"),
    )

    followup_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    eb_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dept_code: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    review_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    closed_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    close_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    close_remarks: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdFollowupEvent(Base):
    """One dated timeline row of a follow-up (REMARK / NOTICE / HOURS_CUT)."""

    __tablename__ = "jute_prod_followup_event"
    __table_args__ = (Index("idx_jpfe_followup", "followup_id", "active", "event_date"),)

    followup_event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    followup_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jute_prod_followup_person.followup_id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    notice_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    issued_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    spell_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hours_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
