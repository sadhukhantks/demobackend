"""SQLAlchemy ORM model for the Jute Production — Selection Trolley Weight sub-module.

One tenant-DB table (../vowerp3ui/docs/claude/modules/jute-production/
selection-trolley-weight-spec.md §2):

  jute_prod_selection_trolly_wt -- one row per loaded trolley weighed at the end of the
                                   manual jute-selection step. The operator picks the
                                   jute quality (a raw-jute item_mst row, the SAME
                                   identity the jute-issue side uses, D3), the trolley
                                   (tagged to the 'Selection' stage in Trolly Master,
                                   D1) and types the gross scale reading; the server
                                   snapshots the trolley tare and stores
                                   net_wt = gross_wt − trolly_wt (D2).

It is an **append + soft-delete log with NO approval workflow** (D6), consistent with
every other jute-production daily entry page: `/entry_delete` sets `active = 0`, every
read filters `active = 1`, there is no status_id and no header/detail split.

``trolly_wt`` is a SNAPSHOT of trolly_mst.trolly_weight taken at save (kept on edit
while the trolley is unchanged) so history stays reproducible after later master edits.
``spell_id`` (D5) and ``jute_mr_id`` (D7 — the gate entry lives on jute_mr, whose
jute_gate_entry_no is joined for display) are optional. ``weigh_time`` is the business
timestamp that orders the day grid, not an audit column.

Conventions follow bale_models.py: shared Base from src/models/mst.py (so the table
participates in existing metadata) with SQLAlchemy 2.0 Mapped / mapped_column
declarations; co_id + branch_id NOT NULL, tran_date, soft delete (active default 1),
audit cols updated_by + updated_date_time only (NOT created_*, which DB triggers handle).

Schema source of truth: dbqueries/migrations/create_jute_prod_selection_trolly_wt.sql.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    DECIMAL,
    TIMESTAMP,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteProdSelectionTrollyWt(Base):
    """One loaded trolley weighed after jute selection.

    Soft-delete only (`active = 0`), no approval workflow (D6). ``item_id`` is the
    raw-jute quality (item_mst) — the same key as jute_mr_li.actual_item_id on the
    issue side (D3). ``trolly_wt`` is the tare snapshot; ``net_wt`` is server-computed
    (gross − tare, D2). ``spell_id`` and ``jute_mr_id`` are optional (D5 / D7).
    """

    __tablename__ = "jute_prod_selection_trolly_wt"
    __table_args__ = (
        Index("idx_stw_co_branch_date", "co_id", "branch_id", "tran_date"),
        Index("idx_stw_item_date", "item_id", "tran_date"),
        Index("idx_stw_trolly", "trolly_id"),
        Index("idx_stw_spell", "spell_id"),
        Index("idx_stw_mr", "jute_mr_id"),
    )

    selection_wt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)  # selection date, never in the future
    spell_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("spell_mst.spell_id"), nullable=True
    )  # D5, optional
    # jute quality = raw-jute item (item_mst); same identity as jute_mr_li.actual_item_id
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("item_mst.item_id"), nullable=False)
    trolly_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trolly_mst.trolly_id"), nullable=False
    )
    # D7, optional gate entry (jute_mr.jute_gate_entry_no is joined for display).
    # BigInteger because jute_mr.jute_mr_id is BIGINT — an INT FK column is refused.
    jute_mr_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("jute_mr.jute_mr_id"), nullable=True
    )
    # snapshot of trolly_mst.trolly_weight (kg) at save
    trolly_wt: Mapped[float] = mapped_column(
        DECIMAL(10, 3), nullable=False, default=0, server_default="0"
    )
    gross_wt: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=False)  # scale reading, kg, > 0
    net_wt: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=False)  # gross − trolly (D2), > 0
    weigh_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )  # business timestamp, orders the day grid
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
