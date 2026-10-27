"""SQLAlchemy ORM models for the Jute Production — Waste Entry sub-module.

Three tenant-DB tables:

  wastage_area_mst      -- the physical areas waste is collected from, each one
                           owned by a single department (and thus one branch)
  jute_prod_waste_entry -- item-tagged waste quantity (kg) per department /
                           area / date / spell, optionally attributed to a machine
  jute_waste_rate_mst   -- effective-dated valuation rate per waste item, used
                           to price a waste line on the Jute Issue page

The table is an **append + soft-delete event log with NO approval workflow**
(consistent with every other jute-production daily entry page). Rows are never
hard-deleted: `/entry_delete` sets `active = 0`, and every read filters
`active = 1`. There is no `status_id`, no approval level, and no header/detail
split — each row stands alone.

`(dept_id, tran_date, spell_id, item_id)` is intentionally NON-unique:
multiple waste rows per department/spell/day are expected (e.g. waste booked
separately per machine inside the same department and spell), so the supporting
index is a plain KEY, not a unique constraint.

Unlike Stoppage Hours, `dept_id` IS stored (NOT NULL) — waste is fundamentally
a departmental measure and is frequently collected in bulk with no single
machine responsible; `machine_id` is therefore NULL-able optional attribution
(guarded on write: machine_mst.dept_id must equal the submitted dept_id).
`branch_id` is nullable and derived from dept_mst.branch_id on insert when the
client omits it; reads use COALESCE(w.branch_id, d.branch_id) to tolerate
legacy NULLs. The waste type IS an item: `item_id` references item_mst and is
restricted on write to items whose group carries item_grp_mst.item_type_id = 3
('jute wastage', JUTE_WASTAGE_ITEM_TYPE_ID in constants.py).

Conventions follow stoppage_models.py (shared Base from src/models/mst.py so it
participates in existing metadata) but use SQLAlchemy 2.0 Mapped /
mapped_column declarations: co_id NOT NULL, nullable derived branch_id,
tran_date, soft delete (active default 1), audit cols updated_by +
updated_date_time (NOT created_*, which are handled by DB triggers).

`wastage_area_id` points at wastage_area_mst. The column is NULL-able in the DB
purely so the waste rows booked before the master existed remain valid — the API
requires an area on every create and edit, and guards that the area's dept_id
matches the row's dept_id (same shape as the machine guard).

Schema source of truth:
dbqueries/migrations/create_jute_prod_waste_entry.sql
dbqueries/migrations/alter_jute_prod_waste_entry_item_id.sql (waste_type_code -> item_id)
dbqueries/migrations/create_wastage_area_mst.sql
dbqueries/migrations/alter_jute_prod_waste_entry_wastage_area.sql
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    DECIMAL,
    TIMESTAMP,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class WastageAreaMst(Base):
    """A physical waste-collection area, owned by exactly one department.

    branch_id is stored (not derived) because the master page is branch-scoped:
    the operator picks a branch, then a department inside it. dept_mst.branch_id
    is nullable, so relying on the department alone would strand areas whose
    department has no branch.

    (branch_id, dept_id, area_name) is UNIQUE — two areas of the same name in
    the same department are a data-entry mistake, not a legitimate pair.
    `active = 0` disables an area (it stops appearing in the waste-entry picker)
    but keeps historical waste rows resolvable.
    """

    __tablename__ = "wastage_area_mst"
    __table_args__ = (
        UniqueConstraint("branch_id", "dept_id", "area_name", name="uk_wastage_area_name"),
        Index("idx_wastage_area_co_branch", "co_id", "branch_id"),
        Index("idx_wastage_area_dept", "dept_id"),
    )

    wastage_area_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dept_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("dept_mst.dept_id"), nullable=False
    )
    area_name: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteWasteRateMst(Base):
    """Valuation rate (per QUINTAL) for a waste item, effective-dated.

    Waste is a production byproduct and so has no purchase rate of its own — an
    MR line carries ``actual_rate`` from the purchase, a waste entry carries
    nothing. This master supplies the rate the Jute Issue page prices a waste
    line with (``issue_value = weight / 100 * rate``), matching the per-quintal
    convention already used for MR stock.

    ``branch_id`` NULL means "every branch of the company"; a branch-specific
    row wins over a company-wide one for the same item and date (see
    ``get_jute_waste_rate_expression`` in juteProcurement/query.py). Overlapping
    windows are NOT rejected — the resolver is deterministic (branch-specific
    first, then the latest ``valid_from``), so an overlap simply means the newer
    rate supersedes the older one from its start date.

    Deliberately NOT unique on (co_id, branch_id, item_id, valid_from): a rate
    can be superseded and re-superseded on the same day during data entry, and
    the resolver's ORDER BY already picks one row.

    Schema source of truth: dbqueries/migrations/add_jute_waste_stock.sql
    """

    __tablename__ = "jute_waste_rate_mst"
    __table_args__ = (
        Index("idx_waste_rate_lookup", "co_id", "item_id", "valid_from"),
        Index("idx_waste_rate_branch", "branch_id"),
    )

    waste_rate_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL = applies to every branch of the company
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("item_mst.item_id"), nullable=False
    )
    rate: Mapped[float] = mapped_column(DECIMAL(12, 2), nullable=False, server_default="0")
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL = open-ended (still current)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdWasteEntry(Base):
    """Process-waste event log (one row per waste booking).

    Soft-delete only (`active = 0`), no approval workflow. Department is stored
    and mandatory; machine is optional attribution. The waste type is an item
    (item_mst) of item type 3 ('jute wastage'), validated on write.
    """

    __tablename__ = "jute_prod_waste_entry"
    __table_args__ = (
        Index("idx_waste_co_branch_date", "co_id", "branch_id", "tran_date"),
        # non-unique (event log): many waste rows per dept/date/spell are allowed
        Index("idx_waste_dept_date_spell", "dept_id", "tran_date", "spell_id"),
        Index("idx_waste_machine", "machine_id"),
        Index("idx_waste_spell", "spell_id"),
        Index("idx_waste_area", "wastage_area_id"),
    )

    waste_entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # derived from dept_mst.branch_id on insert when the client omits it
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    spell_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("spell_mst.spell_id"), nullable=False
    )
    dept_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("dept_mst.dept_id"), nullable=False
    )
    # where the waste was collected; NULL only on rows booked before the master
    # existed — the API requires it and checks wastage_area_mst.dept_id = dept_id
    wastage_area_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("wastage_area_mst.wastage_area_id"), nullable=True
    )
    # optional attribution; when set, machine_mst.dept_id must equal dept_id
    machine_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("machine_mst.machine_id"), nullable=True
    )
    # the waste type itself: an item whose group has item_type_id = 3 ('jute wastage')
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("item_mst.item_id"), nullable=False
    )
    waste_qty: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=False)  # kilograms, > 0
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
