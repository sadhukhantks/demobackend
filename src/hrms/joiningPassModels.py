"""SQLAlchemy ORM models for the Joining Pass (HRMS, owner request 2026-10-24).

Schema source of truth: dbqueries/migrations/20261024_hrms_joining_pass.sql.

  hrms_joining_pass       one issued pass per worker absence. The letter figures
                          (absent from, fortnight presence / working days /
                          efficiency) are frozen at issue so a reprint never
                          changes. active 0 = cancelled.
  hrms_joining_pass_rule  per company (PK co_id): the "more than N working days"
                          threshold, the absentee-list look-back and the fortnight
                          length. Code defaults (joiningPass.RULE_DEFAULTS) apply
                          when no row exists.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import TIMESTAMP, BigInteger, Date, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class HrmsJoiningPass(Base):
    __tablename__ = "hrms_joining_pass"

    joining_pass_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pass_no: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    eb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emp_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    emp_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    dept_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dept_desc: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    designation: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_seen_date: Mapped[date] = mapped_column(Date, nullable=False)
    absent_from: Mapped[date] = mapped_column(Date, nullable=False)
    absent_working_days: Mapped[int] = mapped_column(Integer, nullable=False)
    absent_calendar_days: Mapped[int] = mapped_column(Integer, nullable=False)
    join_date: Mapped[date] = mapped_column(Date, nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    spell_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    spell_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    authority: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    fn_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    fn_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    fn_days_present: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fn_working_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fn_efficiency: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 2), nullable=True)
    fn_eff_dept: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    la_case_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    cancel_reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    cancelled_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cancelled_date_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    issued_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class HrmsJoiningPassRule(Base):
    __tablename__ = "hrms_joining_pass_rule"

    co_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    min_absent_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    fortnight_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14, server_default="14")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
