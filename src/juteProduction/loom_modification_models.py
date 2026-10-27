"""SQLAlchemy ORM models for the Loom Modification register (Jute Production).

Spec: docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md §4.1-§4.4,
§4.6. Four tenant-DB tables, all NEW, loom-only by design (§2.1):

  jute_prod_loom_group          -- the owner's "loom group": a named label over a set
                                   of looms (a weaver's looms, a study set, a saved
                                   CONTROL set). Holds the CURRENT membership only.
  jute_prod_loom_group_dtl      -- current members of a group (soft-delete + reinsert
                                   on edit, weaving_masters.py:400-425 pattern). Not
                                   dated: history lives on the event snapshot.
  jute_prod_loom_modification   -- the dated event register. One row per stage of a
                                   modification chain (INITIAL / FOLLOWUP / RECTIFY /
                                   REVERSAL, MOD_KINDS); parent_modification_id is a
                                   self-FK making the chain a linear linked list and
                                   root_modification_id (stamped at save) fetches a
                                   whole chain in one WHERE.
  jute_prod_loom_modification_mc -- the membership FROZEN at each event: one row per
                                   (event, loom) among active = 1, with optional
                                   per-loom done_date / done_to_date for a change
                                   rolled across a group over several days. The
                                   report reads only this snapshot (§2.3).

Conventions follow bale_models.py: shared Base from src/models/mst.py, SQLAlchemy 2.0
Mapped / mapped_column declarations, soft delete (active default 1), audit columns
updated_by + updated_date_time only on the parent tables (NOT created_*, which DB
triggers handle); child tables carry own PK + parent FK + active only. total_cost is
Double (house rule for amounts, same as jute_mech_overhaul.total_cost).

machine_mst has no co_id / branch_id: a loom's branch is dept_mst.branch_id and the
company is branch_mst.co_id, both derived in code at save (§4.1, §4.3). No
standard-speed columns anywhere: the register reads Weaving Standards, never writes
them (D6), and the report resolves the standard in force from
jute_prod_weaving_target_map when it runs.

Schema source of truth: dbqueries/migrations/20260928_loom_modification_tables.sql.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    TIMESTAMP,
    Date,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteProdLoomGroup(Base):
    """A named set of looms (§4.1) — current membership in JuteProdLoomGroupDtl.

    ``branch_id`` is derived from the members' dept_mst.branch_id (all members must
    agree and that branch must belong to ``co_id``). ``group_purpose`` is STUDY (looms
    under study) or CONTROL (a saved control set for the impact report),
    LOOM_GROUP_PURPOSES. Duplicate guard on (co_id, group_code) among active = 1 lives
    in code (soft-delete pattern — no DB unique key).
    """

    __tablename__ = "jute_prod_loom_group"
    __table_args__ = (
        Index("idx_lg_co", "co_id", "active"),
        Index("idx_lg_co_branch", "co_id", "branch_id"),
    )

    loom_group_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    group_code: Mapped[str] = mapped_column(String(30), nullable=False)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False)
    group_purpose: Mapped[str] = mapped_column(
        String(10), nullable=False, default="STUDY", server_default="STUDY"
    )
    remarks: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdLoomGroupDtl(Base):
    """Current members of a loom group (§4.2).

    ``machine_id`` must be a loom (machine_type_mst name WEAVING_MACHINE_TYPE_NAME,
    compared inside SQL) and must be active when NEWLY added; a member retired later
    is kept and labelled "inactive" in the UI. Replaced by soft-delete + reinsert on
    edit. Not dated — history lives on the event snapshot (JuteProdLoomModificationMc).
    """

    __tablename__ = "jute_prod_loom_group_dtl"
    __table_args__ = (
        Index("idx_lgd_group", "loom_group_id", "active"),
        Index("idx_lgd_machine", "machine_id"),
    )

    loom_group_dtl_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loom_group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jute_prod_loom_group.loom_group_id"), nullable=False
    )
    machine_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("machine_mst.machine_id"), nullable=False
    )
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class JuteProdLoomModification(Base):
    """One stage of a modification chain (§4.3, §5).

    ``mod_kind`` INITIAL (parent NULL, root = self) is "the modification"; FOLLOWUP /
    RECTIFY rows follow the chain's latest non-REVERSAL stage; a REVERSAL undoes its
    parent (and every later stage) for the looms in ITS OWN snapshot. Chains are
    linear. ``root_modification_id`` is stamped at save (= parent's root, else own
    id) and rewritten only by "Insert earlier stage". ``branch_id`` is derived from
    the first snapshot loom (machine_mst -> dept_mst.branch_id). ``loom_group_id``
    set = the looms were taken from a group; NULL = a hand-picked list. ``settle_days``
    are excluded from the report's analysis after the work. ``party_id`` is required
    when ``done_by`` = VENDOR. Phase 3 (D17) adds last_verdict* columns by a separate
    ALTER — not here.
    """

    __tablename__ = "jute_prod_loom_modification"
    __table_args__ = (
        Index("idx_lm_co_branch_date", "co_id", "branch_id", "mod_date"),
        Index("idx_lm_group_date", "loom_group_id", "mod_date"),
        Index("idx_lm_root", "root_modification_id", "mod_date"),
        Index("idx_lm_parent", "parent_modification_id"),
    )

    modification_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    loom_group_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("jute_prod_loom_group.loom_group_id"), nullable=True
    )
    mod_date: Mapped[date] = mapped_column(Date, nullable=False)  # day the change was done / started
    mod_to_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # NULL = same day
    settle_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    mod_kind: Mapped[str] = mapped_column(
        String(10), nullable=False, default="INITIAL", server_default="INITIAL"
    )
    parent_modification_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("jute_prod_loom_modification.modification_id"), nullable=True
    )
    root_modification_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mod_category: Mapped[str] = mapped_column(String(20), nullable=False)  # MOD_CATEGORIES
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expected_effect: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    done_by: Mapped[str] = mapped_column(
        String(10), nullable=False, default="INHOUSE", server_default="INHOUSE"
    )
    party_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_cost: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdLoomModificationMc(Base):
    """Frozen membership of one event (§4.4): one row per (event, loom) among active = 1.

    Per-loom dates: D_m = COALESCE(done_date, mod_date), E_m = COALESCE(done_to_date,
    done_date, mod_to_date, mod_date); both must lie inside the header's
    [mod_date, COALESCE(mod_to_date, mod_date)], and a loom's [D_m, E_m] may not overlap
    the same loom's span on any other active event in ANY chain (checked in code).
    Replaced by soft-delete + reinsert on edit.
    """

    __tablename__ = "jute_prod_loom_modification_mc"
    __table_args__ = (
        Index("idx_lmm_mod", "modification_id", "active"),
        Index("idx_lmm_machine_date", "machine_id", "done_date"),
    )

    modification_mc_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    modification_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jute_prod_loom_modification.modification_id"), nullable=False
    )
    machine_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("machine_mst.machine_id"), nullable=False
    )
    done_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # NULL = header mod_date
    done_to_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
