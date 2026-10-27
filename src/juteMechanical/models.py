"""SQLAlchemy ORM models for the Jute Mechanical & Maintenance module.

Ten tenant-DB tables replace the mill-side mechanical breakdown Excel workbooks:

  Masters
    jute_mech_component_mst    -- cross-section failure component (Gear, Chain, Bearing...)
    jute_mech_reason_mst       -- breakdown reason codes, scoped by machine_type_id
    jute_mech_machine_attr     -- maintenance attributes satellite on machine_mst
    jute_mech_pm_schedule      -- preventive-maintenance checklist template

  Transactions
    jute_mech_breakdown        -- incident event log (stop/restart datetime)
    jute_mech_breakdown_spare  -- spares consumed per breakdown
    jute_mech_overhaul         -- overhaul register
    jute_mech_overhaul_spare   -- spares consumed per overhaul
    jute_mech_pm_execution     -- one PM visit on one machine
    jute_mech_pm_execution_dtl -- per-checklist-task result

Conventions mirror src/juteProduction/stoppage_models.py (Column style, shared Base
from src.models.mst so the tables participate in existing metadata): co_id NOT NULL,
nullable derived branch_id, soft delete (active TINYINT default 1), audit cols
updated_by + updated_date_time (NOT created_*, which is trigger-based).

REUSED masters -- this module creates NO new machine, department, shift or spell
master. It joins the existing ones:

    machine_mst       machine_id, machine_name, mech_code, machine_type_id, dept_id
    machine_type_mst  machine_type_id, machine_type_name  -- == the Excel "section"
    dept_mst          dept_id, dept_desc, branch_id
    sub_dept_mst      attending crew (MECH FACTORY SIDE / MOTOR / FOUNDRY)
    spell_mst         spell_id, spell_code, working_hours   (NOTE: status = 1, not active = 1)
    shift_mst         shift_id, shift_name, is_overnight
    item_mst          spare parts        party_mst  overhaul vendor

machine_mst carries NO co_id and NO branch_id, so branch_id on every transaction
table is DERIVED on insert via machine_mst -> dept_mst.branch_id, exactly as
jute_prod_stoppage_hours does.

This module is deliberately INDEPENDENT of jute_prod_stoppage_hours: that stays the
production hours-accounting feed (all causes, incl. labour/no-material), this is the
engineering log. Neither writes to the other.

Schema source of truth:
dbqueries/migrations/create_jute_mech_maintenance_tables.sql

ITM-004 (2026-10-18) adds two utility tables, mapped_column style, same Base
(schema: dbqueries/migrations/20261018_jute_power_reading.sql):

    jute_power_meter_mst  -- energy meters per (co_id, branch_id): meter_code
                             MAIN / DG1 / ..., meter_type GRID | DG | SOLAR
    jute_power_reading    -- one row per meter per day (optionally per spell):
                             opening / closing / kWh, DG hours + litres, max
                             demand kVA. spell_id is INT NOT NULL with the
                             sentinel 0 = whole day (WHOLE_DAY_SPELL_ID) so the
                             UNIQUE key uq_jpr_scope holds -- MySQL treats NULLs
                             as distinct in unique keys. The API exposes null.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Date,
    DateTime,
    DECIMAL,
    Float,
    Index,
    Numeric,
    TIMESTAMP,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


# =============================================================================
# MASTERS
# =============================================================================


class JuteMechComponentMst(Base):
    """Failure component master (Gear, Chain, Bearing, Roller, Motor, Pump...).

    Reason codes are machine-type-specific -- DHGT exists only for Draw Head, RF
    only for Spreader -- so the reason code alone cannot answer "how much downtime
    did gears cost across the mill?". Tagging each reason with a component makes
    that cross-section Pareto possible without changing what the shift staff type.
    """

    __tablename__ = "jute_mech_component_mst"

    component_id = Column(Integer, primary_key=True, autoincrement=True)
    co_id = Column(Integer, nullable=False, index=True)
    component_name = Column(String(100), nullable=False)
    component_group = Column(String(50), nullable=True)  # Transmission|Bearing|Roller|Electrical|...
    remarks = Column(String(255), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


class JuteMechReasonMst(Base):
    """Breakdown reason code master, scoped by machine type.

    machine_type_id NULL means the code is global (NR, MT, PP, MW and the whole
    electrical/utility/operational set). A non-NULL machine_type_id restricts the
    code to that section, so the entry screen shows a Spreader operator only the
    Spreader codes they already know.

    Seeded verbatim from the mill's REASON sheet by
    dbqueries/migrations/seed_jute_mech_masters.sql.
    """

    __tablename__ = "jute_mech_reason_mst"

    reason_id = Column(Integer, primary_key=True, autoincrement=True)
    co_id = Column(Integer, nullable=False, index=True)
    machine_type_id = Column(Integer, nullable=True, index=True)  # NULL = global code
    reason_code = Column(String(20), nullable=False)
    reason_desc = Column(String(255), nullable=False)
    discipline = Column(String(20), nullable=False, default="MECHANICAL", server_default="MECHANICAL")
    component_id = Column(Integer, nullable=True, index=True)
    remarks = Column(String(255), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


class JuteMechMachineAttr(Base):
    """Maintenance attributes for a machine -- satellite on machine_mst.

    Follows the spreader_machine_attr / jute_prod_drawing_machine_attr pattern:
    one row per (co_id, machine_id). machine_mst is shared with every production
    module, so maintenance-only fields (overhaul dates, PM interval, criticality)
    live here rather than widening it.

    next_overhaul_due / next_pm_due are maintained by the app when an overhaul or
    PM execution is saved, and drive the "due / overdue" reports.
    """

    __tablename__ = "jute_mech_machine_attr"

    machine_attr_id = Column(Integer, primary_key=True, autoincrement=True)
    co_id = Column(Integer, nullable=False, index=True)
    machine_id = Column(Integer, nullable=False, index=True)  # FK machine_mst.machine_id
    make = Column(String(100), nullable=True)
    model = Column(String(100), nullable=True)
    serial_no = Column(String(100), nullable=True)
    commissioning_date = Column(Date, nullable=True)
    criticality = Column(String(1), nullable=True)  # A critical | B important | C normal
    last_overhaul_date = Column(Date, nullable=True)
    overhaul_freq_months = Column(Integer, nullable=True)
    next_overhaul_due = Column(Date, nullable=True)
    last_pm_date = Column(Date, nullable=True)
    pm_freq_days = Column(Integer, nullable=True)
    next_pm_due = Column(Date, nullable=True)
    remarks = Column(String(255), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


class JuteMechPmSchedule(Base):
    """Preventive-maintenance checklist template.

    machine_type_id set -> the task applies to every machine of that type.
    machine_id set      -> the task is specific to one machine.
    """

    __tablename__ = "jute_mech_pm_schedule"

    pm_schedule_id = Column(Integer, primary_key=True, autoincrement=True)
    co_id = Column(Integer, nullable=False, index=True)
    machine_type_id = Column(Integer, nullable=True, index=True)
    machine_id = Column(Integer, nullable=True, index=True)
    task_desc = Column(String(500), nullable=False)
    frequency = Column(String(20), nullable=False)  # DAILY|WEEKLY|MONTHLY|QUARTERLY|HALF_YEARLY|ANNUAL
    freq_days = Column(Integer, nullable=True)  # resolved day count, drives next_pm_due
    seq_no = Column(Integer, nullable=True)
    remarks = Column(String(255), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


# =============================================================================
# TRANSACTIONS
# =============================================================================


class JuteMechBreakdown(Base):
    """Machine breakdown incident log (one row per incident).

    (machine_id, tran_date) is intentionally NON-unique -- many incidents per
    machine per day are normal (the workbook shows up to three, e.g. 'RF/DRJ' and
    'DRJ/PB/BJ' crammed into one Excel cell; each becomes its own row here).

    duration_min and response_min are set by the app on save, not by the DB:
        duration_min = the entered minutes, else restart - stop   (NULL while OPEN)
        response_min = attended_datetime - stop_datetime     (NULL if not recorded)
    Storing them avoids recomputing across large report ranges and lets an
    open breakdown be found with duration_min IS NULL.

    branch_id is derived on insert via machine_mst -> dept_mst.branch_id.
    """

    __tablename__ = "jute_mech_breakdown"

    breakdown_id = Column(Integer, primary_key=True, autoincrement=True)
    co_id = Column(Integer, nullable=False, index=True)
    branch_id = Column(Integer, nullable=True, index=True)  # derived machine->dept->branch
    machine_id = Column(Integer, nullable=False, index=True)  # FK machine_mst.machine_id
    tran_date = Column(Date, nullable=False, index=True)  # date part of stop_datetime
    spell_id = Column(Integer, nullable=True, index=True)  # FK spell_mst.spell_id
    stop_datetime = Column(DateTime, nullable=False)
    attended_datetime = Column(DateTime, nullable=True)  # fitter reached machine
    restart_datetime = Column(DateTime, nullable=True)  # NULL while open
    duration_min = Column(Integer, nullable=True)  # entered, else restart - stop
    response_min = Column(Integer, nullable=True)  # computed: attended - stop
    reason_id = Column(Integer, nullable=False, index=True)
    component_id = Column(Integer, nullable=True, index=True)  # defaults from reason
    discipline = Column(String(20), nullable=False, default="MECHANICAL", server_default="MECHANICAL")
    downtime_type = Column(String(20), nullable=False, default="BREAKDOWN", server_default="BREAKDOWN")
    attended_dept_id = Column(Integer, nullable=True)  # sub_dept_mst.sub_dept_id
    attended_by = Column(String(255), nullable=True)
    reported_by = Column(String(255), nullable=True)
    reported_by_user_id = Column(Integer, nullable=True)  # user_mst: who logged it
    reported_date_time = Column(DateTime, nullable=True)
    fixed_by_user_id = Column(Integer, nullable=True)  # user_mst: who entered the fix
    fixed_date_time = Column(DateTime, nullable=True)
    root_cause = Column(Text, nullable=True)
    action_taken = Column(Text, nullable=True)
    preventive_action = Column(Text, nullable=True)
    production_loss_qty = Column(DECIMAL(12, 3), nullable=True)
    status = Column(String(20), nullable=False, default="OPEN", server_default="OPEN")
    # Production's optional reconfirmation of the fix: NULL | CONFIRMED | DISPUTED.
    prod_confirm_status = Column(String(20), nullable=True)
    prod_confirm_by = Column(Integer, nullable=True)  # user_mst
    prod_confirm_date_time = Column(DateTime, nullable=True)
    prod_restart_datetime = Column(DateTime, nullable=True)  # production's own time
    prod_confirm_remarks = Column(String(500), nullable=True)
    remarks = Column(String(500), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


class JuteMechBreakdownSpare(Base):
    """Spare parts consumed on a breakdown.

    item_id references item_mst.item_id but carries no FK constraint -- the item
    master differs slightly across tenants and this table must migrate cleanly
    into all of them.
    """

    __tablename__ = "jute_mech_breakdown_spare"

    breakdown_spare_id = Column(Integer, primary_key=True, autoincrement=True)
    breakdown_id = Column(Integer, nullable=False, index=True)
    item_id = Column(Integer, nullable=False, index=True)  # item_mst.item_id
    qty = Column(Float, nullable=True)
    uom_id = Column(Integer, nullable=True)
    rate = Column(Float, nullable=True)
    amount = Column(Float, nullable=True)
    remarks = Column(String(255), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


class JuteMechOverhaul(Base):
    """Overhaul register.

    On save the app updates jute_mech_machine_attr.last_overhaul_date and
    next_overhaul_due (from overhaul_freq_months), which drives the overhaul-due
    report.
    """

    __tablename__ = "jute_mech_overhaul"

    overhaul_id = Column(Integer, primary_key=True, autoincrement=True)
    co_id = Column(Integer, nullable=False, index=True)
    branch_id = Column(Integer, nullable=True, index=True)  # derived machine->dept->branch
    machine_id = Column(Integer, nullable=False, index=True)
    overhaul_type = Column(String(10), nullable=False, default="MINOR", server_default="MINOR")
    from_date = Column(Date, nullable=False)
    to_date = Column(Date, nullable=True)
    downtime_days = Column(DECIMAL(6, 2), nullable=True)  # computed: to - from + 1
    work_done = Column(Text, nullable=True)
    done_by = Column(String(10), nullable=False, default="INHOUSE", server_default="INHOUSE")
    party_id = Column(Integer, nullable=True)  # party_mst.party_id when done_by = VENDOR
    labour_cost = Column(Float, nullable=True)
    spare_cost = Column(Float, nullable=True)  # rolled up from jute_mech_overhaul_spare
    total_cost = Column(Float, nullable=True)
    next_due_date = Column(Date, nullable=True)
    remarks = Column(String(500), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


class JuteMechOverhaulSpare(Base):
    """Spare parts consumed during an overhaul."""

    __tablename__ = "jute_mech_overhaul_spare"

    overhaul_spare_id = Column(Integer, primary_key=True, autoincrement=True)
    overhaul_id = Column(Integer, nullable=False, index=True)
    item_id = Column(Integer, nullable=False, index=True)  # item_mst.item_id
    qty = Column(Float, nullable=True)
    uom_id = Column(Integer, nullable=True)
    rate = Column(Float, nullable=True)
    amount = Column(Float, nullable=True)
    remarks = Column(String(255), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


class JuteMechPmExecution(Base):
    """One preventive-maintenance visit on one machine.

    On save the app updates jute_mech_machine_attr.last_pm_date and next_pm_due
    (from pm_freq_days), which drives the PM-due report.
    """

    __tablename__ = "jute_mech_pm_execution"

    pm_exec_id = Column(Integer, primary_key=True, autoincrement=True)
    co_id = Column(Integer, nullable=False, index=True)
    branch_id = Column(Integer, nullable=True, index=True)  # derived machine->dept->branch
    machine_id = Column(Integer, nullable=False, index=True)
    pm_date = Column(Date, nullable=False, index=True)
    frequency = Column(String(20), nullable=True)  # which cycle this visit covers
    done_by = Column(String(255), nullable=True)
    downtime_min = Column(Integer, nullable=True)
    next_due_date = Column(Date, nullable=True)
    remarks = Column(String(500), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


class JuteMechPmExecutionDtl(Base):
    """Per-checklist-task result for one PM visit.

    task_desc is snapshotted from the schedule row so later template edits do not
    rewrite history; pm_schedule_id may be NULL for an ad-hoc task done on the day.
    """

    __tablename__ = "jute_mech_pm_execution_dtl"

    pm_exec_dtl_id = Column(Integer, primary_key=True, autoincrement=True)
    pm_exec_id = Column(Integer, nullable=False, index=True)
    pm_schedule_id = Column(Integer, nullable=True, index=True)
    task_desc = Column(String(500), nullable=True)  # snapshot at execution time
    result = Column(String(10), nullable=False, default="OK", server_default="OK")
    observation = Column(String(500), nullable=True)
    active = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, nullable=True)
    updated_date_time = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())


# =============================================================================
# POWER / UTILITIES (ITM-004)
# =============================================================================


class JutePowerMeterMst(Base):
    """Energy meter master per branch (MAIN grid meter, DG sets, solar).

    meter_code is what jute_power_reading rows carry (no FK by id), so a meter
    is never deleted -- deactivate it (active = 0) and its readings keep their
    code. UNIQUE on (co_id, branch_id, meter_code).
    """

    __tablename__ = "jute_power_meter_mst"
    __table_args__ = (
        UniqueConstraint("co_id", "branch_id", "meter_code", name="uq_jpm_scope"),
        Index("idx_jpm_branch", "branch_id", "active"),
    )

    power_meter_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    meter_code: Mapped[str] = mapped_column(String(20), nullable=False)
    meter_name: Mapped[str] = mapped_column(String(100), nullable=False)
    meter_type: Mapped[str] = mapped_column(String(10), nullable=False)  # GRID | DG | SOLAR
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JutePowerReading(Base):
    """One meter reading per day (optionally per spell).

    kwh is always stored: closing - opening when both bounds are keyed, else the
    kWh typed directly (meter reset). spell_id 0 = whole day (see module doc).
    DG hours / litres are meaningful on DG-type meters only; max_demand_kva on
    the grid meter. Soft delete via active = 0; the unique key still covers
    inactive rows, so a re-save of the same key re-activates the row in place.
    """

    __tablename__ = "jute_power_reading"
    __table_args__ = (
        UniqueConstraint(
            "co_id", "branch_id", "tran_date", "spell_id", "meter_code", name="uq_jpr_scope"
        ),
        Index("idx_jpr_branch_date", "branch_id", "tran_date"),
        Index("idx_jpr_meter_date", "co_id", "branch_id", "meter_code", "tran_date"),
    )

    power_reading_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    spell_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    meter_code: Mapped[str] = mapped_column(String(20), nullable=False)
    opening_kwh: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    closing_kwh: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    kwh: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    dg_hours: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    dg_litres: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    max_demand_kva: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
