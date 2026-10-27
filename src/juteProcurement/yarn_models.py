"""Purchased yarn ledger — Yarn Purchase, Yarn Issue / Return and the godown view.

Design: docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-design.md
Schema source of truth: dbqueries/migrations/20260925_yarn_purchase_issue_tables.sql

Four tenant-DB tables, all keyed by the mill's own co/branch (the scope the
Winding & Weaving Difference report reads), all in KILOGRAMS:

  jute_yarn_purchase / _li    -- receipt of purchased yarn: supplier -> GODOWN
  jute_yarn_issue / _li       -- ISSUE: GODOWN -> department floor;
                                 RETURN: department floor -> GODOWN.
                                 The lot (yarn_purchase_li_id) is mandatory.
  vw_yarn_godown_outstanding  -- per purchase lot: received - issued + returned

Header statuses follow the standard 21 draft -> 1 open -> 20 pending ->
3 approved / 4 rejected / 6 cancelled lifecycle (src/procurement/constants.py).
No FKs, as on every jute production table. MT is never stored.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import DECIMAL, TIMESTAMP, Date, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteYarnPurchase(Base):
    """Receipt header: one per lorry / challan of purchased yarn."""

    __tablename__ = "jute_yarn_purchase"
    __table_args__ = (
        Index("idx_yarn_purchase_co_branch_date", "co_id", "branch_id", "tran_date"),
        Index("idx_yarn_purchase_status", "status_id"),
        Index("idx_yarn_purchase_inward", "inward_id"),
    )

    yarn_purchase_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    # per branch per financial year, assigned on /open
    purchase_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    party_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    challan_no: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    challan_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    vehicle_no: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # optional link to the Stores receipt (proc_inward) that carries the money
    inward_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status_id: Mapped[int] = mapped_column(Integer, nullable=False, default=21, server_default="21")
    approval_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteYarnPurchaseLi(Base):
    """One yarn quality on a receipt. ``weight_kg`` is authoritative; packages
    and weight per package are for counting and the default on the next line."""

    __tablename__ = "jute_yarn_purchase_li"
    __table_args__ = (
        Index("idx_yarn_purchase_li_hdr", "yarn_purchase_id"),
        Index("idx_yarn_purchase_li_item", "item_id"),
    )

    yarn_purchase_li_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    yarn_purchase_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # item_mst row that has a jute_yarn_mst entry (item_grp_mst.item_type_id = 4)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    package_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    no_of_packages: Mapped[Optional[float]] = mapped_column(DECIMAL(12, 3), nullable=True)
    wt_per_package: Mapped[Optional[float]] = mapped_column(DECIMAL(10, 3), nullable=True)
    weight_kg: Mapped[float] = mapped_column(DECIMAL(12, 3), nullable=False)
    rate_per_kg: Mapped[Optional[float]] = mapped_column(DECIMAL(12, 2), nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(DECIMAL(14, 2), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteYarnIssue(Base):
    """Issue (godown -> department) or Return (department -> godown) header."""

    __tablename__ = "jute_yarn_issue"
    __table_args__ = (
        Index("idx_yarn_issue_co_branch_date", "co_id", "branch_id", "tran_date"),
        Index("idx_yarn_issue_dept", "dept_id"),
        Index("idx_yarn_issue_status", "status_id"),
    )

    yarn_issue_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    issue_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # constants.YARN_ISSUE_TYPES: 'ISSUE' | 'RETURN'
    issue_type: Mapped[str] = mapped_column(String(10), nullable=False, default="ISSUE", server_default="ISSUE")
    # any active dept_mst row of the branch (owner ruling 2)
    dept_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status_id: Mapped[int] = mapped_column(Integer, nullable=False, default=21, server_default="21")
    approval_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteYarnIssueLi(Base):
    """One lot drawn (or returned). The lot is mandatory (owner ruling 5)."""

    __tablename__ = "jute_yarn_issue_li"
    __table_args__ = (
        Index("idx_yarn_issue_li_hdr", "yarn_issue_id"),
        Index("idx_yarn_issue_li_lot", "yarn_purchase_li_id"),
        Index("idx_yarn_issue_li_item", "item_id"),
    )

    yarn_issue_li_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    yarn_issue_id: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    yarn_purchase_li_id: Mapped[int] = mapped_column(Integer, nullable=False)
    no_of_packages: Mapped[Optional[float]] = mapped_column(DECIMAL(12, 3), nullable=True)
    weight_kg: Mapped[float] = mapped_column(DECIMAL(12, 3), nullable=False)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class VwYarnGodownOutstanding(Base):
    """Read-only view: godown balance per purchase lot.

    Wide on purpose (open / pending / approved purchases; issues that are not
    rejected, cancelled or draft). ``purchase_status_id`` lets the issue picker
    and the save-time guard narrow to constants.YARN_PURCHASE_ISSUABLE_STATUSES.
    """

    __tablename__ = "vw_yarn_godown_outstanding"

    yarn_purchase_li_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    yarn_purchase_id: Mapped[int] = mapped_column(Integer)
    co_id: Mapped[int] = mapped_column(Integer)
    branch_id: Mapped[int] = mapped_column(Integer)
    tran_date: Mapped[date] = mapped_column(Date)
    purchase_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    party_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    purchase_status_id: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[int] = mapped_column(Integer)
    package_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    no_of_packages: Mapped[Optional[float]] = mapped_column(DECIMAL(12, 3), nullable=True)
    wt_per_package: Mapped[Optional[float]] = mapped_column(DECIMAL(10, 3), nullable=True)
    received_kg: Mapped[float] = mapped_column(DECIMAL(12, 3))
    issued_kg: Mapped[float] = mapped_column(DECIMAL(12, 3))
    returned_kg: Mapped[float] = mapped_column(DECIMAL(12, 3))
    bal_kg: Mapped[float] = mapped_column(DECIMAL(12, 3))
    issued_pkgs: Mapped[float] = mapped_column(DECIMAL(12, 3))
    returned_pkgs: Mapped[float] = mapped_column(DECIMAL(12, 3))
    bal_pkgs: Mapped[float] = mapped_column(DECIMAL(12, 3))
