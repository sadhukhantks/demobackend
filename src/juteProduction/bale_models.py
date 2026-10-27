"""SQLAlchemy ORM models for the Jute Production — Bale Production (Bale Press) sub-module.

Two tenant-DB tables (docs/bale-production-spec-sheet.md §4.1 / §4.2):

  jute_prod_bale_series -- running bale-number counters: one row per sheet "vendor"
                           (sacking: a state agency / FCI, shared across the bag
                           qualities; hessian: one per quality). The "Last Bale No" tab.
  jute_prod_bale_entry  -- one row per bale weighed at the bale press, PROD or REPACK
                           in the same table (D2), with tare SNAPSHOTS so
                           net_wt = gross_wt − trolly_wt − packsheet_wt (F0) is
                           reproducible after later master edits.

Both are **append + soft-delete logs with NO approval workflow**, consistent with every
other jute-production daily entry page: `/entry_delete` sets `active = 0`, every read
filters `active = 1`, there is no status_id and no header/detail split.

Uniqueness is APP-level throughout (soft-delete pattern — no DB unique keys):
* series: (co_id, branch_id, series_name) among active = 1 (the master's duplicate guard);
* entry:  (bale_series_id, bale_no) among active = 1 answers **409** on the API, which the
  operator may override with `allow_duplicate` (Q5) — REPACK re-uses numbers and a series
  reset legitimately re-uses old numbers, so the supporting index is a plain KEY.

Standards are also snapshotted on the entry (`std_bale_wt`), and the Finishing Quality
Master (finishing_models.JuteProdFinishingQuality) gained the two nullable columns
`std_mr_pct` / `weft_yarn_item_id` that HY/LT reads — those live in finishing_models.py
(legacy Column style, kept consistent there).

Conventions follow waste_models.py: shared Base from src/models/mst.py (so the tables
participate in existing metadata) with SQLAlchemy 2.0 Mapped / mapped_column
declarations; co_id + branch_id NOT NULL, tran_date, soft delete (active default 1),
audit cols updated_by + updated_date_time (NOT created_*, which DB triggers handle).

Schema source of truth: dbqueries/migrations/create_jute_prod_bale_tables.sql
(seed: dbqueries/migrations/seed_bale_series_dev3.sql).
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    DECIMAL,
    TIMESTAMP,
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


class JuteProdBaleSeries(Base):
    """One running bale-number counter (the sheet's "Last Bale No" tab as data).

    ``item_id`` set -> the series is bound to ONE quality (hessian pattern); NULL ->
    any quality of that ``quality_type`` (sacking / state pattern). ``state_id`` /
    ``party_id`` are optional matches to existing masters (never forced).
    ``last_bale_no`` is bumped by every save whose bale_no is higher (F9) and is
    editable in the master = a manual reset (D3); the master also stores that value in
    ``opening_bale_no``. Deleting / renumbering the top bale winds the counter back to
    GREATEST(opening_bale_no, highest remaining active bale_no). quality_type shares the codes of
    jute_prod_finishing_quality (1 = Hessian, 2 = Sacking).
    """

    __tablename__ = "jute_prod_bale_series"
    __table_args__ = (
        Index("idx_bs_co_branch", "co_id", "branch_id"),
        Index("idx_bs_item", "item_id"),
    )

    bale_series_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)  # counters are per mill
    series_name: Mapped[str] = mapped_column(String(60), nullable=False)
    quality_type: Mapped[int] = mapped_column(Integer, nullable=False)  # 1=Hessian, 2=Sacking
    # bound quality item (hessian); NULL = any quality of the type (sacking / state)
    item_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("item_mst.item_id"), nullable=True
    )
    # optional masters links, blank for now — FKs dropped 2026-09-21
    state_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    party_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_bale_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # counter value last set in the master — the floor a top-bale delete winds back to
    opening_bale_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdBaleEntry(Base):
    """One bale weighed at the bale press (PROD) or re-pressed (REPACK).

    Soft-delete only (`active = 0`), no approval workflow. ``entry_type`` flags the
    row (D2): a REPACK row takes the next series number as its own ``bale_no`` and
    stores the number of the bale that was opened in ``original_bale_no`` (NULL on
    PROD rows). ``trolly_wt`` / ``packsheet_wt`` / ``std_bale_wt`` are snapshots taken
    at save; ``net_wt`` is server-computed (F0). ``weigh_time`` is the form's
    Timestamp — a business field ordering the Daily Bale Sheet within a spell, not an
    audit column. Every create / edit / delete re-syncs the matching
    jute_prod_finishing_daily balepress row (F10 / D4).
    """

    __tablename__ = "jute_prod_bale_entry"
    __table_args__ = (
        Index("idx_be_co_branch_date", "co_id", "branch_id", "tran_date"),
        # NON-unique by design: REPACK re-uses numbers and a series reset re-uses old ones
        Index("idx_be_series_no", "bale_series_id", "bale_no"),
        Index("idx_be_quality_date", "finishing_quality_id", "tran_date"),
        Index("idx_be_spell", "spell_id"),
    )

    bale_entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)  # production date, never blank
    spell_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("spell_mst.spell_id"), nullable=False
    )
    # Bale Press machine; NULL allowed for branches without a machine row
    machine_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("machine_mst.machine_id"), nullable=True
    )
    entry_type: Mapped[str] = mapped_column(
        String(8), nullable=False, default="PROD", server_default="PROD"
    )  # 'PROD' | 'REPACK'
    finishing_quality_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("jute_prod_finishing_quality.finishing_quality_id"),
        nullable=False,
    )
    bale_series_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jute_prod_bale_series.bale_series_id"), nullable=False
    )
    bale_no: Mapped[int] = mapped_column(Integer, nullable=False)  # this row's own number
    # REPACK only: the number of the bale that was opened and re-pressed
    original_bale_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trolly_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("trolly_mst.trolly_id"), nullable=True
    )
    # snapshot of trolly_mst.trolly_weight at save (kg)
    trolly_wt: Mapped[float] = mapped_column(
        DECIMAL(10, 3), nullable=False, default=0, server_default="0"
    )
    # snapshot of jute_prod_finishing_quality.packsheet_wt at save (kg)
    packsheet_wt: Mapped[float] = mapped_column(
        DECIMAL(10, 3), nullable=False, default=0, server_default="0"
    )
    gross_wt: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=False)  # scale reading, kg, > 0
    net_wt: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=False)  # gross − trolly − packsheet (F0)
    std_bale_wt: Mapped[Optional[float]] = mapped_column(DECIMAL(10, 3), nullable=True)  # snapshot
    weigh_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
