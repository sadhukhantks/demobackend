"""SQLAlchemy ORM models for Jute Production Planning (SPEC §2).

Five tables: one plan header + three line sections + the norms master.

    jute_prod_planning        header — one active row per (co_id, branch_id, plan_date)
    jute_prod_planning_loom   Section 1 — loom / weaving plan lines
    jute_prod_planning_yarn   Section 2 — yarn requirement + frame allocation
    jute_prod_planning_dept   Section 3 — dept-wise breakup / machines required
    jute_prod_planning_norm   norms master (EAV, LAST-DATE resolution)

These reuse the same declarative Base as src/models/mst.py (like
src/juteProduction/spinning_models.py) so they participate in the existing metadata
if anyone runs create_all(). The endpoints in planning.py use raw SQL
(planning_query.py) — these models are the authoritative schema reference.

UNITS (SPEC §1): every machine rate is PER MACHINE PER SHIFT, so `*_required`
columns are machine-shifts per day and the A/B/C columns are machines running in that
shift. Norm rows store the 100%-eff rate; the plan line stores eff_pct plus the
effective rate, which the user may override.
"""

from datetime import date, datetime
from decimal import Decimal
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


class JuteProdPlanning(Base):
    """Production-plan header — one document per (co_id, branch_id, plan_date).

    Uniqueness among active=1 headers is enforced by planning_save (not by a unique
    index): branch_id is NULL-able and soft-deleted rows must be free to repeat a
    date. plan_no is generated after insert as CONCAT('JPP-', LPAD(planning_id,5,'0')).
    No approval workflow — status_id stays 1 (Open) in v1.
    """

    __tablename__ = "jute_prod_planning"

    planning_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    plan_date: Mapped[date] = mapped_column(Date, nullable=False)
    plan_no: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    plan_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_jpp_co_date", "co_id", "plan_date"),
        Index("idx_jpp_branch", "branch_id"),
    )


class JuteProdPlanningLoom(Base):
    """Section 1 — loom / weaving plan line (SPEC §2.2).

    kg_per_loom_100 is the 100%-eff norm for the cloth quality;
    kg_per_loom = kg_per_loom_100 * eff_pct/100 (per loom PER SHIFT) and
    prod_x = looms_x * kg_per_loom for x in {a,b,c}. quality_item_id NULL marks a
    group-level row (the sacking style, where yarn is planned as a % of the group's
    production instead of per quality).
    """

    __tablename__ = "jute_prod_planning_loom"

    planning_loom_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    planning_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    product_group: Mapped[str] = mapped_column(String(20), nullable=False)  # SACKING|HESSIAN|PACKSHEET|OTHER
    quality_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # item_mst.item_id (item_type_id=5)
    quality_desc: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    looms_a: Mapped[Decimal] = mapped_column(DECIMAL(8, 2), nullable=False, default=0, server_default="0.00")
    looms_b: Mapped[Decimal] = mapped_column(DECIMAL(8, 2), nullable=False, default=0, server_default="0.00")
    looms_c: Mapped[Decimal] = mapped_column(DECIMAL(8, 2), nullable=False, default=0, server_default="0.00")
    kg_per_loom_100: Mapped[Decimal] = mapped_column(
        DECIMAL(10, 3), nullable=False, default=0, server_default="0.000"
    )
    eff_pct: Mapped[Decimal] = mapped_column(DECIMAL(5, 2), nullable=False, default=0, server_default="0.00")
    kg_per_loom: Mapped[Decimal] = mapped_column(DECIMAL(10, 3), nullable=False, default=0, server_default="0.000")
    prod_a: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False, default=0, server_default="0.000")
    prod_b: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False, default=0, server_default="0.000")
    prod_c: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False, default=0, server_default="0.000")
    warp_yarn_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # item_mst.item_id (item_type_id=4)
    weft_yarn_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # item_mst.item_id (item_type_id=4)
    warp_pct: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(5, 2), nullable=True)
    weft_pct: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(5, 2), nullable=True)
    line_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_jppl_plan", "planning_id"),
        Index("idx_jppl_quality", "quality_item_id"),
    )


class JuteProdPlanningYarn(Base):
    """Section 2 — yarn requirement + spinning-frame allocation line (SPEC §2.3).

    CLOTH lines are derived from the loom plan (pct_of_prod for sacking-style groups,
    warp_pct/weft_pct for quality rows); SALE lines are order-driven and carry
    order_mt / days_needed / delivery_date. frames_required is FRAME-SHIFTS per day
    (req_kg_day / kg_per_frame); frames_a/b/c are frames running in that shift.
    """

    __tablename__ = "jute_prod_planning_yarn"

    planning_yarn_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    planning_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    line_kind: Mapped[str] = mapped_column(String(10), nullable=False)  # CLOTH | SALE
    section: Mapped[str] = mapped_column(String(20), nullable=False)  # SACKING|HESSIAN|PACKSHEET|TWINE|SALE|OTHER
    yarn_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # item_mst.item_id (item_type_id=4)
    yarn_desc: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    warp_weft: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)  # WP | WT | NULL
    pct_of_prod: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(5, 2), nullable=True)
    req_kg_a: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False, default=0, server_default="0.000")
    req_kg_b: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False, default=0, server_default="0.000")
    req_kg_c: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False, default=0, server_default="0.000")
    req_kg_day: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False, default=0, server_default="0.000")
    kg_per_frame_100: Mapped[Decimal] = mapped_column(
        DECIMAL(10, 3), nullable=False, default=0, server_default="0.000"
    )
    eff_pct: Mapped[Decimal] = mapped_column(DECIMAL(5, 2), nullable=False, default=0, server_default="0.00")
    kg_per_frame: Mapped[Decimal] = mapped_column(DECIMAL(10, 3), nullable=False, default=0, server_default="0.000")
    frames_required: Mapped[Decimal] = mapped_column(DECIMAL(8, 2), nullable=False, default=0, server_default="0.00")
    frames_a: Mapped[Decimal] = mapped_column(DECIMAL(6, 1), nullable=False, default=0, server_default="0.0")
    frames_b: Mapped[Decimal] = mapped_column(DECIMAL(6, 1), nullable=False, default=0, server_default="0.0")
    frames_c: Mapped[Decimal] = mapped_column(DECIMAL(6, 1), nullable=False, default=0, server_default="0.0")
    priority: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    order_mt: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(10, 3), nullable=True)
    days_needed: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(8, 2), nullable=True)
    delivery_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    line_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_jppy_plan", "planning_id"),
        Index("idx_jppy_yarn", "yarn_item_id"),
    )


class JuteProdPlanningDept(Base):
    """Section 3 — dept-wise breakup / machines required, one row per stage (SPEC §2.4).

    Backward explosion in flow order stage_seq 1..7 (BATCHING -> CARDING -> DRAWING ->
    SPINNING -> WINDING -> BEAMING -> WEAVING): every stage has
    input_kg_day = output_kg_day * (1 + waste_pct/100), and the upstream stage's
    output defaults to the downstream stage's input. BATCHING.input_kg_day is the
    headline raw-jute requirement per day. For WINDING the "machines" are winders
    (winding is person-keyed in this ERP), labelled "Machines / Workers" in the FE.
    """

    __tablename__ = "jute_prod_planning_dept"

    planning_dept_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    planning_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    stage_seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..7
    stage_code: Mapped[str] = mapped_column(String(20), nullable=False)
    stage_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    output_kg_day: Mapped[Decimal] = mapped_column(DECIMAL(14, 3), nullable=False, default=0, server_default="0.000")
    waste_pct: Mapped[Decimal] = mapped_column(DECIMAL(5, 2), nullable=False, default=0, server_default="0.00")
    input_kg_day: Mapped[Decimal] = mapped_column(DECIMAL(14, 3), nullable=False, default=0, server_default="0.000")
    kg_per_machine: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False, default=0, server_default="0.000")
    machines_required: Mapped[Decimal] = mapped_column(DECIMAL(8, 2), nullable=False, default=0, server_default="0.00")
    machines_a: Mapped[Decimal] = mapped_column(DECIMAL(6, 1), nullable=False, default=0, server_default="0.0")
    machines_b: Mapped[Decimal] = mapped_column(DECIMAL(6, 1), nullable=False, default=0, server_default="0.0")
    machines_c: Mapped[Decimal] = mapped_column(DECIMAL(6, 1), nullable=False, default=0, server_default="0.0")
    machines_installed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    machines_maint: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    machines_ready: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    line_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_jppd_plan", "planning_id"),
        Index("idx_jppd_stage", "planning_id", "stage_seq"),
    )


class JuteProdPlanningNorm(Base):
    """Planning norms master — effective-dated EAV, same shape as the *_target_map tables.

    Resolution (SPEC §2.5): among active=1 rows matching
    (co_id, id_type, ref_id, param), take the row with MAX(effective_date) <= on_date.
    value_role is fixed 'standard' in v1 (shape parity with *_target_map only).

    Params by dimension —
      id_type='qid'   (ref_id = cloth item_id): kg_per_loom_100, warp_pct, weft_pct,
                       warp_yarn_item (value = yarn item_id), weft_yarn_item, std_eff
      id_type='yid'   (ref_id = yarn item_id) : kg_per_frame_100, std_eff
      id_type='stage' (ref_id = stage_seq 1..7): kg_per_machine, waste_pct
    """

    __tablename__ = "jute_prod_planning_norm"

    planning_norm_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    id_type: Mapped[str] = mapped_column(String(8), nullable=False)  # 'qid' | 'yid' | 'stage'
    value_role: Mapped[str] = mapped_column(
        String(10), nullable=False, default="standard", server_default="standard"
    )
    param: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[Decimal] = mapped_column(DECIMAL(14, 4), nullable=False, default=0, server_default="0.0000")
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_jppn", "co_id", "id_type", "ref_id", "param", "effective_date"),
    )
