# Winding Stock Entry + Winding & Weaving Difference — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily winding-stock count entry page and a Winding & Weaving Difference report that reconciles counted stock against book stock.

**Architecture:** One new counted-snapshot table (`jute_prod_winding_stock`) plus a per-day lock table, a new `/api/windingStock` router cloned from `waste_entry.py`, and one report endpoint added to the existing `winding_reports.py` that derives every production figure from builders already in the repo. Nothing is accumulated or persisted beyond the counted rows themselves.

**Tech Stack:** FastAPI (sync `def` handlers), SQLAlchemy 2.0 `Mapped`/`mapped_column` ORM + raw `text()` query builders, MySQL (multi-tenant, one DB per tenant), pytest + `TestClient` with `dependency_overrides`, Next.js 14 App Router + MUI DataGrid + react-hook-form/zod on the frontend.

**Spec:** `docs/superpowers/specs/2026-08-19-winding-stock-difference-design.md`

## Global Constraints

- **Handlers are `def`, never `async def`.** The whole API is sync SQLAlchemy/PyMySQL; an `async def` handler with sync DB calls blocks the event loop for every other request.
- **Every response is wrapped**: `return {"data": ...}`. Never return a bare list.
- **This module returns literal `{"data": ...}` dicts and takes Pydantic `body:` params.** `waste_entry.py` deliberately uses neither `create_response` nor `parse_json_body`; match it.
- **SQL NULL is `None`**, never the string `"null"`. Every named bind in a statement must be present as a key in the params dict, even when `None`.
- **`item_mst` has no `co_id`.** Co-scope every item query via `item_grp_mst.co_id`.
- **Query builders are arg-less** `def get_*_query():` / `verify_*_query():` returning `text(...)`, living in `*_query.py`. Params go in the `db.execute(q, {...})` dict.
- **Soft delete only**: `active = 0`, never `DELETE`.
- **Row reads**: `dict(r._mapping)`; cast Decimals with a local `_f()` helper; stringify dates before returning.
- **Co-scope every by-id lookup.** A tenant DB holds every company's rows, so `WHERE pk = :id` alone lets a user of company A read and mutate company B's row. Always `AND co_id = :co_id`.
- **Migration runner defect:** the `run-migration` skill splits on `;` and skips any chunk starting with `--`. Therefore **no SQL statement may be preceded by a comment block**. Put all commentary after the final `;` at the end of the file.
- **Target tenants:** dev3 first, then sls. Never assume; the `run-migration` skill asks.
- Units in play: winding = **kg**, weaving MTD = **MT**, weaving day slice = both `production_kg` and `production_mt` per row, `jute_issue.weight` = **quintal**. Convert in Python, never in new SQL.

---

## File Structure

**Backend (`c:\code\vowerp3be`)**

| File | Responsibility |
|---|---|
| `dbqueries/migrations/create_jute_prod_winding_stock.sql` | DDL for both new tables |
| `dbqueries/migrations/20260819_winding_stock_menus.sql` | menu_mst + role_menu_map rows for both pages |
| `src/juteProduction/winding_stock_models.py` | ORM for the two new tables |
| `src/juteProduction/winding_stock_query.py` | all `text()` builders for entry CRUD + the report's stock/lookup reads |
| `src/juteProduction/winding_stock_lock.py` | lock read + `require_edit_if_locked` gate (clone of `weaving_lock.py`) |
| `src/juteProduction/winding_stock.py` | `/api/windingStock` router — entry CRUD, setup, copy-previous-day, lock |
| `src/juteProduction/winding_reports.py` | **modify** — add `GET /winding_weaving_difference` + its pure section builders |
| `src/juteProduction/constants.py` | **modify** — bucket codes + cloth-split codes |
| `src/main.py` | **modify** — register the new router |
| `src/test/test_winding_stock_entry.py` | entry router tests |
| `src/test/test_winding_stock_report.py` | report builder tests incl. the golden case |

**Frontend (`c:\code\vowerp3ui`)** — detailed in Tasks 8-10.

---

## Task 1: Database tables + ORM models

**Files:**
- Create: `dbqueries/migrations/create_jute_prod_winding_stock.sql`
- Create: `src/juteProduction/winding_stock_models.py`
- Modify: `src/juteProduction/constants.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: tables `jute_prod_winding_stock`, `jute_prod_winding_stock_lock`; ORM classes `JuteProdWindingStock`, `JuteProdWindingStockLock`; constants `WINDING_STOCK_BUCKETS`, `WINDING_STOCK_BUCKET_*`, `CLOTH_SPLIT_*`, `WINDING_STOCK_MENU_PATH`, `FINISHING_QUALITY_TYPE_HESSIAN`, `FINISHING_QUALITY_TYPE_SACKING`.

- [ ] **Step 1: Write the migration SQL**

Create `dbqueries/migrations/create_jute_prod_winding_stock.sql`. Note there is **no leading comment** — the runner would swallow the first statement.

```sql
CREATE TABLE jute_prod_winding_stock (
  winding_stock_id  INT PRIMARY KEY AUTO_INCREMENT,
  co_id             INT NOT NULL,
  branch_id         INT NOT NULL,
  tran_date         DATE NOT NULL,
  bucket            VARCHAR(20) NOT NULL,
  item_id           INT NULL,
  package_label     VARCHAR(60) NOT NULL,
  wastage_area_id   INT NULL,
  no_of_samples     DECIMAL(12,3) NOT NULL,
  wt_per_sample     DECIMAL(10,3) NOT NULL,
  remarks           VARCHAR(255) NULL,
  active            INT NOT NULL DEFAULT 1,
  updated_by        INT NULL,
  updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_winding_stock_co_branch_date (co_id, branch_id, tran_date),
  INDEX idx_winding_stock_item (item_id),
  INDEX idx_winding_stock_area (wastage_area_id)
);

CREATE TABLE jute_prod_winding_stock_lock (
  winding_stock_lock_id INT PRIMARY KEY AUTO_INCREMENT,
  co_id               INT NOT NULL,
  branch_id           INT NULL,
  tran_date           DATE NOT NULL,
  is_locked           TINYINT NOT NULL DEFAULT 1,
  processed_by        INT NULL,
  processed_date_time TIMESTAMP NULL,
  active              TINYINT NOT NULL DEFAULT 1,
  updated_by          INT NULL,
  updated_date_time   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_wstocklock_unit (co_id, tran_date)
);

-- Rollback:
--   DROP TABLE jute_prod_winding_stock_lock;
--   DROP TABLE jute_prod_winding_stock;
--
-- No FKs and no UNIQUE key on jute_prod_winding_stock, both deliberate:
--   * The module carries no FKs on production tables (see jute_prod_waste_entry).
--   * MySQL treats NULLs as distinct, so a UNIQUE over the nullable item_id /
--     wastage_area_id would not catch the duplicate lines that matter. The
--     duplicate check lives in the create/edit handler instead.
-- stock_mt is intentionally absent: MT = no_of_samples * wt_per_sample / 1000,
-- computed at read so a corrected weight fixes history.
```

- [ ] **Step 2: Add the constants**

Append to `src/juteProduction/constants.py`:

```python
# =============================================================================
# Winding stock (daily counted snapshot) — see
# docs/superpowers/specs/2026-08-19-winding-stock-difference-design.md
# =============================================================================

WINDING_STOCK_BUCKET_MILL_USE = "MILL_USE"
WINDING_STOCK_BUCKET_PURCHASE = "PURCHASE"
WINDING_STOCK_BUCKET_SALE_YARN = "SALE_YARN"
WINDING_STOCK_BUCKET_BALL = "BALL"
WINDING_STOCK_BUCKET_WASTE = "WASTE"

# Report print order. PURCHASE here is COUNTED purchased yarn sitting in stock
# (the sheet's "OUT SIDE PURCHASE MACKROL FOR S4"), NOT the purchase-yarn inflow
# figure in the production block — those are unrelated numbers sharing a word.
WINDING_STOCK_BUCKETS = (
    WINDING_STOCK_BUCKET_MILL_USE,
    WINDING_STOCK_BUCKET_PURCHASE,
    WINDING_STOCK_BUCKET_SALE_YARN,
    WINDING_STOCK_BUCKET_BALL,
    WINDING_STOCK_BUCKET_WASTE,
)

WINDING_STOCK_MENU_PATH = "juteProduction/windingStock"

# jute_prod_finishing_quality.quality_type — the existing hessian/sacking split.
FINISHING_QUALITY_TYPE_HESSIAN = 1
FINISHING_QUALITY_TYPE_SACKING = 2

CLOTH_SPLIT_HESSIAN = "HESSIAN"
CLOTH_SPLIT_SACKING = "SACKING"
CLOTH_SPLIT_OTHER = "OTHER"
CLOTH_SPLIT_BUCKETS = (CLOTH_SPLIT_HESSIAN, CLOTH_SPLIT_SACKING, CLOTH_SPLIT_OTHER)

# Max kg per counted piece — a beam is the heaviest real package on the sheet
# at 420 kg; anything past this is a typo, not a package.
WINDING_STOCK_WT_MAX = 2000.0
# Max pieces on one counted line. The largest real line on the reference sheet
# is 1,563 spools; the ceiling only has to catch a slipped keystroke.
WINDING_STOCK_SAMPLES_MAX = 1000000.0
```

- [ ] **Step 3: Write the ORM models**

Create `src/juteProduction/winding_stock_models.py`:

```python
"""Winding stock — the daily counted yarn snapshot and its per-day lock.

Two tenant-DB tables:

  jute_prod_winding_stock       -- one counted line per (date, bucket, yarn item,
                                   package label, area). The FIRST counted-snapshot
                                   table in this ERP; every other stock figure in
                                   the system is derived from movements.
  jute_prod_winding_stock_lock  -- one lock header per (co, branch, date); once
                                   locked, mutating the day needs Edit permission.

MT is NEVER stored: it is no_of_samples * wt_per_sample / 1000, computed at read,
so correcting a weight fixes history instead of stranding a derived value.

There is no weight-per-sample master. The default offered on a new line is the
most recent saved wt_per_sample for the same (co, branch, item, package) key —
one lookup against this same table.

Schema source of truth:
dbqueries/migrations/create_jute_prod_winding_stock.sql
"""

from datetime import date, datetime
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


class JuteProdWindingStock(Base):
    """One counted stock line for a date.

    ``item_id`` is nullable on purpose: lines such as "BEAM ON FLOOR-HESS",
    "PURCHASE PW FOR S4" and the four waste lines (THREAD WASTE, DIRTY BOBBIN,
    DAMAGE SPOOL, ENTANGLE COPS) have no jute_yarn_mst row. ``package_label``
    carries them, and is therefore mandatory on every row.

    No spell: the sheet is an as-on-date count, not a shift entry.
    """

    __tablename__ = "jute_prod_winding_stock"
    __table_args__ = (
        Index("idx_winding_stock_co_branch_date", "co_id", "branch_id", "tran_date"),
        Index("idx_winding_stock_item", "item_id"),
        Index("idx_winding_stock_area", "wastage_area_id"),
    )

    winding_stock_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    # one of constants.WINDING_STOCK_BUCKETS
    bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    # yarn item; NULL where no jute_yarn_mst row exists for the line
    item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    package_label: Mapped[str] = mapped_column(String(60), nullable=False)
    wastage_area_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    no_of_samples: Mapped[float] = mapped_column(DECIMAL(12, 3), nullable=False)
    wt_per_sample: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=False)  # kg/piece
    remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdWindingStockLock(Base):
    """One lock header per (co, branch, tran_date).

    Mirrors jute_prod_weaving_process_lock minus spell_id — the stock count has
    no spell. Locking is an explicit action, never a side effect of running the
    report: a GET must not mutate.
    """

    __tablename__ = "jute_prod_winding_stock_lock"
    __table_args__ = (Index("idx_wstocklock_unit", "co_id", "tran_date"),)

    winding_stock_lock_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tran_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_locked: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    processed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    processed_date_time: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
```

- [ ] **Step 4: Verify the models import cleanly**

Run: `.venv/Scripts/python.exe -c "from src.juteProduction.winding_stock_models import JuteProdWindingStock, JuteProdWindingStockLock; print(JuteProdWindingStock.__tablename__, JuteProdWindingStockLock.__tablename__)"`
Expected: `jute_prod_winding_stock jute_prod_winding_stock_lock`

- [ ] **Step 5: Apply the migration to dev3**

Use the `run-migration` skill with `dbqueries/migrations/create_jute_prod_winding_stock.sql`, target **dev3**. Do not run it against sls yet — sls goes in Task 11 after the code is verified.

Verify: `SHOW CREATE TABLE jute_prod_winding_stock;` returns the 14 columns above and the 3 indexes.

- [ ] **Step 6: Commit**

```bash
git add dbqueries/migrations/create_jute_prod_winding_stock.sql src/juteProduction/winding_stock_models.py src/juteProduction/constants.py
git commit -m "feat(winding-stock): add counted-snapshot tables, ORM and constants"
```

---

## Task 2: Query builders

**Files:**
- Create: `src/juteProduction/winding_stock_query.py`
- Create: `src/test/test_winding_stock_query.py`

**Interfaces:**
- Consumes: tables from Task 1.
- Produces (all arg-less, all returning `text()`):
  `get_winding_stock_by_date_query`, `get_winding_stock_active_by_id_query`,
  `insert_winding_stock_row_query`, `update_winding_stock_row_query`,
  `soft_delete_winding_stock_row_query`, `get_winding_stock_duplicate_query`,
  `get_winding_stock_wt_default_query`, `get_winding_stock_prev_entry_date_query`,
  `get_winding_stock_totals_by_date_query`, `copy_winding_stock_previous_day_query`,
  `get_winding_stock_yarn_items_query`, `get_winding_stock_areas_query`,
  `verify_winding_stock_item_query`, `verify_winding_stock_area_query`,
  `get_winding_stock_lock_row_query`, `insert_winding_stock_lock_query`,
  `update_winding_stock_lock_query`, `clear_winding_stock_lock_query`,
  `get_weaving_quality_type_map_query`, `get_sale_yarn_mt_query`.

- [ ] **Step 1: Write the builder-smoke test first**

Create `src/test/test_winding_stock_query.py`:

```python
"""Winding stock query builders — construction smoke test + bind-name contract.

Cheap guard: every builder must construct, and the binds each statement declares
must be exactly the set the router promises to pass. A bind added to the SQL
without being added to the caller's dict is a runtime 500, not an import error,
so it is asserted here.
"""

import src.juteProduction.winding_stock_query as q

BUILDERS = [
    "get_winding_stock_by_date_query",
    "get_winding_stock_active_by_id_query",
    "insert_winding_stock_row_query",
    "update_winding_stock_row_query",
    "soft_delete_winding_stock_row_query",
    "get_winding_stock_duplicate_query",
    "get_winding_stock_wt_default_query",
    "get_winding_stock_prev_entry_date_query",
    "get_winding_stock_totals_by_date_query",
    "copy_winding_stock_previous_day_query",
    "get_winding_stock_yarn_items_query",
    "get_winding_stock_areas_query",
    "verify_winding_stock_item_query",
    "verify_winding_stock_area_query",
    "get_winding_stock_lock_row_query",
    "insert_winding_stock_lock_query",
    "update_winding_stock_lock_query",
    "clear_winding_stock_lock_query",
    "get_weaving_quality_type_map_query",
    "get_sale_yarn_mt_query",
]


def test_all_builders_construct():
    for name in BUILDERS:
        assert getattr(q, name)() is not None, name


def test_by_id_builders_are_co_scoped():
    """A tenant DB holds every company's rows: pk alone is a cross-company read."""
    for name in (
        "get_winding_stock_active_by_id_query",
        "update_winding_stock_row_query",
        "soft_delete_winding_stock_row_query",
    ):
        sql = str(getattr(q, name)())
        assert "co_id = :co_id" in sql, name


def test_soft_delete_never_deletes():
    sql = str(q.soft_delete_winding_stock_row_query())
    assert "active = 0" in sql
    assert "DELETE" not in sql.upper()


def test_sale_yarn_filters_on_item_type_not_invoice_type():
    """INVOICE_TYPE_IDS['JUTE_YARN'] == 4 collides numerically with
    item_grp_mst.item_type_id == 4. Yarn is identified by the LINE's item type,
    which catches yarn on any invoice type; filtering on invoice_type would
    instead pull non-yarn lines that ride a yarn invoice."""
    sql = str(q.get_sale_yarn_mt_query())
    assert "item_type_id" in sql
    assert "invoice_type" not in sql
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_query.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.juteProduction.winding_stock_query'`

- [ ] **Step 3: Write the builders**

Create `src/juteProduction/winding_stock_query.py`:

```python
"""Winding stock query builders.

Every builder is arg-less and returns a text() statement; parameters go in the
db.execute(stmt, {...}) dict at the call site. Each named bind below MUST be
present as a key even when its value is None.

Grouped: CRUD, day reads, lock, report lookups.
"""

from sqlalchemy import text

from src.juteProduction.constants import (
    PLANNING_YARN_ITEM_TYPE_ID,
)
from src.sales.constants import SALES_STATUS_IDS


# =============================================================================
# CRUD builders
# =============================================================================


def insert_winding_stock_row_query():
    """Insert one counted line (active = 1)."""
    return text(
        """
        INSERT INTO jute_prod_winding_stock
            (co_id, branch_id, tran_date, bucket, item_id, package_label,
             wastage_area_id, no_of_samples, wt_per_sample, remarks,
             active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :bucket, :item_id, :package_label,
             :wastage_area_id, :no_of_samples, :wt_per_sample, :remarks,
             1, :updated_by)
        """
    )


def get_winding_stock_active_by_id_query():
    """One active line by id, scoped to the caller's company.

    The co_id predicate is load-bearing: a tenant DB holds every company's rows,
    so matching on the primary key alone would let a user scoped to company A
    read — and through the edit/delete guards, mutate — company B's row.
    """
    return text(
        """
        SELECT winding_stock_id, co_id, branch_id, tran_date, bucket, item_id,
               package_label, wastage_area_id, no_of_samples, wt_per_sample,
               remarks
        FROM jute_prod_winding_stock
        WHERE winding_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


def update_winding_stock_row_query():
    """Edit a line. tran_date and branch_id are immutable — delete and re-add to
    move a count to another day, mirroring the waste-entry precedent."""
    return text(
        """
        UPDATE jute_prod_winding_stock
        SET bucket          = :bucket,
            item_id         = :item_id,
            package_label   = :package_label,
            wastage_area_id = :wastage_area_id,
            no_of_samples   = :no_of_samples,
            wt_per_sample   = :wt_per_sample,
            remarks         = :remarks,
            updated_by      = :updated_by
        WHERE winding_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


def soft_delete_winding_stock_row_query():
    return text(
        """
        UPDATE jute_prod_winding_stock
        SET active = 0, updated_by = :updated_by
        WHERE winding_stock_id = :id AND co_id = :co_id AND active = 1
        """
    )


def get_winding_stock_duplicate_query():
    """Probe for an existing line with the same identity on the same day.

    NULL-safe on item_id / wastage_area_id via the <=> operator, which a UNIQUE
    key cannot express — MySQL treats NULLs as distinct, so the constraint would
    let exactly the duplicates that matter through.
    """
    return text(
        """
        SELECT winding_stock_id
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date = :tran_date
          AND bucket = :bucket
          AND item_id <=> :item_id
          AND package_label = :package_label
          AND wastage_area_id <=> :wastage_area_id
          AND active = 1
          AND (:exclude_id IS NULL OR winding_stock_id <> :exclude_id)
        LIMIT 1
        """
    )


# =============================================================================
# Day reads
# =============================================================================


def get_winding_stock_by_date_query():
    """All counted lines for a day, with the labels the grid and report print.

    yarn_count comes from jute_yarn_mst, whose item_id is NON-UNIQUE — the
    scalar subquery is deliberate: a JOIN here fans the row set out.
    """
    return text(
        """
        SELECT ws.winding_stock_id,
               ws.co_id,
               ws.branch_id,
               ws.tran_date,
               ws.bucket,
               ws.item_id,
               im.item_code,
               im.item_name,
               ig.item_grp_name AS quality_name,
               (SELECT ym.jute_yarn_count
                  FROM jute_yarn_mst ym
                 WHERE ym.item_id = ws.item_id
                 LIMIT 1) AS yarn_count,
               ws.package_label,
               ws.wastage_area_id,
               wa.area_name,
               ws.no_of_samples,
               ws.wt_per_sample,
               ws.remarks
        FROM jute_prod_winding_stock ws
        LEFT JOIN item_mst im ON im.item_id = ws.item_id
        LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN wastage_area_mst wa ON wa.wastage_area_id = ws.wastage_area_id
        WHERE ws.co_id = :co_id
          AND ws.branch_id = :branch_id
          AND ws.tran_date = :tran_date
          AND ws.active = 1
        ORDER BY ws.bucket, im.item_code, ws.package_label
        """
    )


def get_winding_stock_prev_entry_date_query():
    """The most recent date before :tran_date that has any counted line.

    Returns NULL when the chain has no earlier entry. The report compares this
    against the calendar previous day to decide whether the opening balance can
    be trusted, rather than silently reconciling against a stale count.
    """
    return text(
        """
        SELECT MAX(tran_date) AS prev_entry_date
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date < :tran_date
          AND active = 1
        """
    )


def get_winding_stock_totals_by_date_query():
    """Per-bucket MT totals for a day, plus the grand total in the caller's sum.

    MT is computed here rather than stored so a corrected weight fixes history.
    """
    return text(
        """
        SELECT bucket,
               ROUND(SUM(no_of_samples * wt_per_sample) / 1000, 3) AS bucket_mt
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date = :tran_date
          AND active = 1
        GROUP BY bucket
        """
    )


def get_winding_stock_wt_default_query():
    """Most recent saved kg-per-piece for a (co, branch, item, package) key.

    This IS the weight-per-sample "master": there is no separate table, so
    overtyping a row makes that value the next default.
    """
    return text(
        """
        SELECT wt_per_sample
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND item_id <=> :item_id
          AND package_label = :package_label
          AND active = 1
        ORDER BY tran_date DESC, winding_stock_id DESC
        LIMIT 1
        """
    )


def copy_winding_stock_previous_day_query():
    """Clone every line of :from_date into :tran_date, counts included.

    The operator then overtypes the numbers that changed. Guarded by the caller,
    which refuses when :tran_date already has rows.
    """
    return text(
        """
        INSERT INTO jute_prod_winding_stock
            (co_id, branch_id, tran_date, bucket, item_id, package_label,
             wastage_area_id, no_of_samples, wt_per_sample, remarks,
             active, updated_by)
        SELECT co_id, branch_id, :tran_date, bucket, item_id, package_label,
               wastage_area_id, no_of_samples, wt_per_sample, remarks,
               1, :updated_by
        FROM jute_prod_winding_stock
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date = :from_date
          AND active = 1
        """
    )


# =============================================================================
# Setup / lookup builders
# =============================================================================


def get_winding_stock_yarn_items_query():
    """Active yarn items for the picker.

    Company scope lives on item_grp_mst.co_id — item_mst has no co_id by design.
    jute_yarn_mst.item_id is non-unique, so the count is read as a scalar
    subquery; joining the table would duplicate items.
    """
    return text(
        f"""
        SELECT im.item_id,
               im.item_code,
               im.item_name,
               ig.item_grp_name AS quality_name,
               (SELECT ym.jute_yarn_count
                  FROM jute_yarn_mst ym
                 WHERE ym.item_id = im.item_id
                 LIMIT 1) AS yarn_count
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE im.active = 1
          AND ig.co_id = :co_id
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
        ORDER BY im.item_name
        """
    )


def get_winding_stock_areas_query():
    """Areas for the PLACE column — the existing wastage_area_mst master."""
    return text(
        """
        SELECT a.wastage_area_id, a.area_name, a.dept_id, a.branch_id
        FROM wastage_area_mst a
        WHERE a.active = 1
          AND a.co_id = :co_id
          AND (:branch_id IS NULL OR a.branch_id = :branch_id)
        ORDER BY a.area_name
        """
    )


def verify_winding_stock_item_query():
    """Single-row check that item_id is an active yarn item of this company.

    Separate from the picker query so create/edit costs one indexed lookup
    rather than fetching and scanning the whole item list.
    """
    return text(
        f"""
        SELECT im.item_id
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE im.item_id = :item_id
          AND im.active = 1
          AND ig.co_id = :co_id
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
        LIMIT 1
        """
    )


def verify_winding_stock_area_query():
    """Single-row check that the area belongs to this company and branch."""
    return text(
        """
        SELECT wastage_area_id
        FROM wastage_area_mst
        WHERE wastage_area_id = :wastage_area_id
          AND co_id = :co_id
          AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        LIMIT 1
        """
    )


# =============================================================================
# Lock builders
# =============================================================================


def get_winding_stock_lock_row_query():
    return text(
        """
        SELECT winding_stock_lock_id, is_locked
        FROM jute_prod_winding_stock_lock
        WHERE co_id = :co_id AND tran_date = :tran_date
          AND (:branch_id IS NULL OR branch_id = :branch_id OR branch_id IS NULL)
          AND active = 1
        ORDER BY winding_stock_lock_id DESC
        LIMIT 1
        """
    )


def insert_winding_stock_lock_query():
    return text(
        """
        INSERT INTO jute_prod_winding_stock_lock
            (co_id, branch_id, tran_date, is_locked, processed_by,
             processed_date_time, active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, 1, :processed_by,
             CURRENT_TIMESTAMP, 1, :processed_by)
        """
    )


def update_winding_stock_lock_query():
    return text(
        """
        UPDATE jute_prod_winding_stock_lock
        SET is_locked = 1, processed_by = :processed_by,
            processed_date_time = CURRENT_TIMESTAMP,
            updated_by = :processed_by, updated_date_time = CURRENT_TIMESTAMP
        WHERE winding_stock_lock_id = :id
        """
    )


def clear_winding_stock_lock_query():
    return text(
        """
        UPDATE jute_prod_winding_stock_lock
        SET is_locked = 0, updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE winding_stock_lock_id = :id
        """
    )


# =============================================================================
# Report lookups
# =============================================================================


def get_weaving_quality_type_map_query():
    """weaving_quality_id -> hessian/sacking, resolved through the cloth item.

    The classification lives on jute_prod_finishing_quality.quality_type
    (1 = Hessian, 2 = Sacking), keyed by item_id, and is joined here rather than
    duplicated onto the weaving master. quality_type is NULL for any quality
    whose cloth item has no finishing row; those group as OTHER, which keeps the
    three buckets summing to total weaving production.

    The DISTINCT subquery matters: one item can carry several finishing rows and
    a plain join would multiply the weaving qualities.
    """
    return text(
        """
        SELECT wq.weaving_quality_id, fq.quality_type
        FROM jute_prod_weaving_quality wq
        LEFT JOIN (
            SELECT DISTINCT item_id, quality_type
            FROM jute_prod_finishing_quality
            WHERE active = 1 AND co_id = :co_id
        ) fq ON fq.item_id = wq.item_id
        WHERE wq.co_id = :co_id
          AND wq.active = 1
        """
    )


def get_sale_yarn_mt_query():
    """Yarn actually SOLD in a period, in kg, for one branch.

    Yarn is identified by the LINE's item type (item_grp_mst.item_type_id = 4),
    which catches yarn sold on any invoice type. Deliberately NOT filtered on
    sales_invoice.invoice_type: INVOICE_TYPE_IDS['JUTE_YARN'] is also 4, a
    numeric coincidence, and matching it would pull in non-yarn lines that ride
    a yarn invoice.

    sales_invoice_dtl has no active column — cancellation is excluded by the
    header status alone.
    """
    return text(
        f"""
        SELECT ROUND(
                 SUM(COALESCE(NULLIF(sid.sales_weight, 0), sid.quantity, 0)), 3
               ) AS sale_kg
        FROM sales_invoice sinv
        INNER JOIN sales_invoice_dtl sid ON sid.invoice_id = sinv.invoice_id
        INNER JOIN item_mst im ON im.item_id = sid.item_id
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE sinv.branch_id = :branch_id
          AND sinv.status_id = {SALES_STATUS_IDS["APPROVED"]}
          AND COALESCE(sinv.active, 1) = 1
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
          AND sinv.invoice_date BETWEEN :date_from AND :date_to
        """
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_query.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/juteProduction/winding_stock_query.py src/test/test_winding_stock_query.py
git commit -m "feat(winding-stock): query builders for entry CRUD, lock and report lookups"
```

---

## Task 3: Lock module

**Files:**
- Create: `src/juteProduction/winding_stock_lock.py`
- Create: `src/test/test_winding_stock_lock.py`

**Interfaces:**
- Consumes: `get_winding_stock_lock_row_query` (Task 2), `get_user_menu_access_level_query` from `src.common.portal.query`, `WINDING_STOCK_MENU_PATH` (Task 1).
- Produces: `get_stock_lock(db, co_id, branch_id, tran_date)`, `is_day_locked(db, co_id, branch_id, tran_date) -> bool`, `require_edit_if_locked(db, token_data, co_id, branch_id, tran_date) -> None`.

- [ ] **Step 1: Write the failing test**

Create `src/test/test_winding_stock_lock.py`:

```python
"""Winding stock lock gate.

Locked days are not frozen outright: mutating one requires Edit (access level
>= 4) on the Winding Stock menu, so a supervisor can still correct a bad count
while a Write-only operator cannot edit a day whose report has been signed.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.juteProduction.winding_stock_lock import (
    EDIT_LEVEL,
    is_day_locked,
    require_edit_if_locked,
)

D = date(2026, 1, 27)


def _row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


def _session(lock_row, access_level=None):
    session = MagicMock()
    lock_res = MagicMock()
    lock_res.fetchone.return_value = lock_row
    level_res = MagicMock()
    level_res.scalar.return_value = access_level
    session.execute.side_effect = [lock_res, level_res]
    return session


def test_unlocked_day_is_not_locked():
    assert is_day_locked(_session(None), 1, 2, D) is False


def test_locked_flag_false_is_not_locked():
    assert is_day_locked(_session(_row(winding_stock_lock_id=5, is_locked=0)), 1, 2, D) is False


def test_unlocked_day_never_checks_permission():
    """No lock means no permission query at all — the gate must short-circuit."""
    session = _session(None)
    require_edit_if_locked(session, {"user_id": 9}, 1, 2, D)
    assert session.execute.call_count == 1


def test_locked_day_rejects_write_only_user():
    session = _session(_row(winding_stock_lock_id=5, is_locked=1), access_level=3)
    with pytest.raises(HTTPException) as exc:
        require_edit_if_locked(session, {"user_id": 9}, 1, 2, D)
    assert exc.value.status_code == 403


def test_locked_day_allows_edit_user():
    session = _session(_row(winding_stock_lock_id=5, is_locked=1), access_level=EDIT_LEVEL)
    require_edit_if_locked(session, {"user_id": 9}, 1, 2, D)


def test_missing_access_level_is_treated_as_no_access():
    """scalar() returns None when the user has no mapping — must not pass the gate."""
    session = _session(_row(winding_stock_lock_id=5, is_locked=1), access_level=None)
    with pytest.raises(HTTPException) as exc:
        require_edit_if_locked(session, {"user_id": 9}, 1, 2, D)
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_lock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.juteProduction.winding_stock_lock'`

- [ ] **Step 3: Write the module**

Create `src/juteProduction/winding_stock_lock.py`:

```python
"""Winding stock lock lookups + the locked-day permission gate.

A (co, branch, tran_date) day is locked once the operator finalises the count.
While locked, mutations require Edit (access_type_id >= 4); Write-only (3) is
rejected 403. This mirrors weaving_lock.py, minus spell (a stock count has no
shift).

The gate is deliberately a permission check rather than a flat refusal: a signed
report must not be edited under by the operator, but a supervisor still has to
be able to correct a miscount without a DBA.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.common.portal.query import get_user_menu_access_level_query
from src.juteProduction.constants import WINDING_STOCK_MENU_PATH
from src.juteProduction.winding_stock_query import get_winding_stock_lock_row_query

EDIT_LEVEL = 4
LOCKED_EDIT_ONLY_MSG = (
    "This day is locked because its stock report has been taken. Editing a "
    "locked winding stock entry requires Edit permission for the Winding Stock "
    "menu."
)


def get_stock_lock(db: Session, co_id, branch_id, tran_date):
    """Active lock header for the day (or None)."""
    return db.execute(
        get_winding_stock_lock_row_query(),
        {
            "co_id": int(co_id),
            "tran_date": tran_date,
            "branch_id": None if branch_id is None else int(branch_id),
        },
    ).fetchone()


def is_day_locked(db: Session, co_id, branch_id, tran_date) -> bool:
    row = get_stock_lock(db, co_id, branch_id, tran_date)
    return bool(row and row.is_locked)


def user_menu_access_level(db: Session, user_id, co_id, branch_id, menu_path) -> int:
    lvl = db.execute(
        get_user_menu_access_level_query(),
        {
            "user_id": int(user_id),
            "co_id": int(co_id),
            "branch_id": None if branch_id is None else int(branch_id),
            "menu_path": menu_path,
        },
    ).scalar()
    return int(lvl) if lvl is not None else 0


def require_edit_if_locked(db: Session, token_data: dict, co_id, branch_id, tran_date) -> None:
    """Raise 403 when the day is locked and the user lacks Edit (>= 4)."""
    if not is_day_locked(db, co_id, branch_id, tran_date):
        return
    level = user_menu_access_level(
        db, token_data.get("user_id"), co_id, branch_id, WINDING_STOCK_MENU_PATH
    )
    if level < EDIT_LEVEL:
        raise HTTPException(status_code=403, detail=LOCKED_EDIT_ONLY_MSG)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_lock.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/juteProduction/winding_stock_lock.py src/test/test_winding_stock_lock.py
git commit -m "feat(winding-stock): per-day lock gate requiring Edit to mutate a locked count"
```

---

## Task 4: Entry router

**Files:**
- Create: `src/juteProduction/winding_stock.py`
- Modify: `src/main.py` (2 lines)
- Create: `src/test/test_winding_stock_entry.py`

**Interfaces:**
- Consumes: every builder from Task 2; `require_edit_if_locked`, `is_day_locked`, `get_stock_lock` from Task 3; bucket + limit constants from Task 1.
- Produces: the `/api/windingStock` endpoints the frontend calls (Task 8). Response contracts:
  - `GET /setup` → `{"data": {"buckets": [str], "yarn_items": [...], "areas": [...]}}`
  - `GET /rows_by_date` → `{"data": {"rows": [...], "bucket_totals": {bucket: mt}, "grand_total_mt": float, "locked": bool, "prev_entry_date": str|null}}`
  - `GET /wt_default` → `{"data": {"wt_per_sample": float|null}}`
  - `POST /row_create` → `{"data": {"winding_stock_id": int}}`
  - `PUT /row_edit/{id}` / `DELETE /row_delete/{id}` → `{"data": {"winding_stock_id": int}}`
  - `POST /copy_previous_day` → `{"data": {"copied": int, "from_date": str}}`
  - `GET /lock_state` → `{"data": {"locked": bool}}`; `POST /lock` / `POST /unlock` → `{"data": {"locked": bool}}`

- [ ] **Step 1: Write the failing tests**

Create `src/test/test_winding_stock_entry.py`:

```python
"""Winding stock entry router.

Mocked-session tests in the house style: the real app is imported and auth/DB
are replaced through dependency_overrides. execute() is driven with a
side_effect list, one entry per statement the handler runs in order.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    WINDING_STOCK_BUCKET_MILL_USE,
    WINDING_STOCK_SAMPLES_MAX,
    WINDING_STOCK_WT_MAX,
)
from src.main import app

client = TestClient(app)

PAST_DATE = "2026-01-27"
FUTURE_DATE = str(date.today() + timedelta(days=7))
_UNSET = object()


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET, rowcount=_UNSET):
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    if rowcount is not _UNSET:
        res.rowcount = rowcount
    return res


def _create_payload(**overrides):
    payload = {
        "co_id": 1,
        "branch_id": 2,
        "tran_date": PAST_DATE,
        "bucket": WINDING_STOCK_BUCKET_MILL_USE,
        "item_id": 335538,
        "package_label": "MACKROLL SPOOL",
        "wastage_area_id": 40,
        "no_of_samples": 954,
        "wt_per_sample": 5.68,
        "remarks": None,
    }
    payload.update(overrides)
    return payload


class TestWindingStockEntry:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # ---- create -----------------------------------------------------------

    def test_create_inserts_and_returns_id(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_attr_row(item_id=335538)),   # item verify
                _exec_result(fetchone=_attr_row(wastage_area_id=40)),  # area verify
                _exec_result(fetchone=None),                        # duplicate probe
                _exec_result(lastrowid=501),                        # insert
            ]
            response = client.post("/api/windingStock/row_create", json=_create_payload())

        assert response.status_code == 200
        assert response.json()["data"]["winding_stock_id"] == 501
        params = self._mock_session.execute.call_args_list[-1].args[1]
        assert params["bucket"] == WINDING_STOCK_BUCKET_MILL_USE
        assert params["no_of_samples"] == 954
        assert params["wt_per_sample"] == 5.68
        self._mock_session.commit.assert_called()

    def test_create_rejects_future_date(self):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(tran_date=FUTURE_DATE)
        )
        assert response.status_code == 400

    def test_create_rejects_unknown_bucket(self):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(bucket="NOT_A_BUCKET")
        )
        assert response.status_code == 400

    def test_create_rejects_blank_package_label(self):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(package_label="   ")
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("samples", [0, -1, WINDING_STOCK_SAMPLES_MAX + 1])
    def test_create_rejects_out_of_range_samples(self, samples):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(no_of_samples=samples)
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("wt", [0, -1, WINDING_STOCK_WT_MAX + 1])
    def test_create_rejects_out_of_range_weight(self, wt):
        response = client.post(
            "/api/windingStock/row_create", json=_create_payload(wt_per_sample=wt)
        )
        assert response.status_code == 400

    def test_create_allows_null_item_for_unmastered_lines(self):
        """BEAM ON FLOOR-HESS and the waste lines have no yarn master row."""
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_attr_row(wastage_area_id=40)),  # area only
                _exec_result(fetchone=None),                            # duplicate probe
                _exec_result(lastrowid=502),
            ]
            response = client.post(
                "/api/windingStock/row_create",
                json=_create_payload(item_id=None, package_label="THREAD WASTE"),
            )
        assert response.status_code == 200
        params = self._mock_session.execute.call_args_list[-1].args[1]
        assert params["item_id"] is None

    def test_create_rejects_duplicate_line(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_attr_row(item_id=335538)),
                _exec_result(fetchone=_attr_row(wastage_area_id=40)),
                _exec_result(fetchone=_attr_row(winding_stock_id=490)),  # duplicate hit
            ]
            response = client.post("/api/windingStock/row_create", json=_create_payload())
        assert response.status_code == 400
        assert "already" in response.json()["detail"].lower()

    # ---- read -------------------------------------------------------------

    def test_rows_by_date_totals_and_grand_total(self):
        self._mock_session.execute.side_effect = [
            _exec_result(
                fetchall=[
                    _mock_row({
                        "winding_stock_id": 1, "co_id": 1, "branch_id": 2,
                        "tran_date": date(2026, 1, 27),
                        "bucket": WINDING_STOCK_BUCKET_MILL_USE,
                        "item_id": 335538, "item_code": "X", "item_name": "Y",
                        "quality_name": "SKWP", "yarn_count": 9.5,
                        "package_label": "HESSIAN WEFT", "wastage_area_id": 40,
                        "area_name": "COP GODOWN",
                        "no_of_samples": 185, "wt_per_sample": 14, "remarks": None,
                    })
                ]
            ),
            _exec_result(fetchall=[_mock_row({"bucket": WINDING_STOCK_BUCKET_MILL_USE,
                                              "bucket_mt": 2.59})]),
            _exec_result(fetchone=_attr_row(prev_entry_date=date(2026, 1, 26))),
            _exec_result(fetchone=None),  # lock lookup
        ]
        response = client.get(
            "/api/windingStock/rows_by_date?co_id=1&branch_id=2&tran_date=2026-01-27"
        )
        assert response.status_code == 200
        body = response.json()["data"]
        # 185 pieces x 14 kg = 2590 kg = 2.590 MT (the reference sheet's first row)
        assert body["rows"][0]["stock_mt"] == 2.59
        assert body["bucket_totals"][WINDING_STOCK_BUCKET_MILL_USE] == 2.59
        assert body["grand_total_mt"] == 2.59
        assert body["prev_entry_date"] == "2026-01-26"
        assert body["locked"] is False

    def test_rows_by_date_requires_branch(self):
        response = client.get("/api/windingStock/rows_by_date?co_id=1&tran_date=2026-01-27")
        assert response.status_code == 400

    def test_wt_default_returns_last_saved_value(self):
        self._mock_session.execute.return_value.fetchone.return_value = _attr_row(
            wt_per_sample=5.68
        )
        response = client.get(
            "/api/windingStock/wt_default"
            "?co_id=1&branch_id=2&item_id=335538&package_label=MACKROLL%20SPOOL"
        )
        assert response.status_code == 200
        assert response.json()["data"]["wt_per_sample"] == 5.68

    def test_wt_default_returns_null_for_unseen_key(self):
        self._mock_session.execute.return_value.fetchone.return_value = None
        response = client.get(
            "/api/windingStock/wt_default?co_id=1&branch_id=2&package_label=NEW%20THING"
        )
        assert response.status_code == 200
        assert response.json()["data"]["wt_per_sample"] is None

    # ---- edit / delete ----------------------------------------------------

    def test_edit_requires_co_id(self):
        response = client.put("/api/windingStock/row_edit/501", json={"no_of_samples": 10})
        assert response.status_code == 400

    def test_edit_404s_for_foreign_company_row(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
            response = client.put(
                "/api/windingStock/row_edit/501?co_id=1", json={"no_of_samples": 10}
            )
        assert response.status_code == 404

    def test_delete_soft_deletes(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchone=_attr_row(
                    winding_stock_id=501, co_id=1, branch_id=2,
                    tran_date=date(2026, 1, 27),
                )),
                _exec_result(rowcount=1),
            ]
            response = client.delete("/api/windingStock/row_delete/501?co_id=1")
        assert response.status_code == 200
        stmt = str(self._mock_session.execute.call_args_list[-1].args[0])
        assert "active = 0" in stmt
        assert "DELETE" not in stmt.upper()

    # ---- copy previous day ------------------------------------------------

    def test_copy_previous_day_refuses_when_target_has_rows(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchall=[_mock_row({"winding_stock_id": 1})]),  # existing rows
            ]
            response = client.post(
                "/api/windingStock/copy_previous_day",
                json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
            )
        assert response.status_code == 400

    def test_copy_previous_day_404s_when_no_earlier_entry(self):
        with patch("src.juteProduction.winding_stock.require_edit_if_locked"):
            self._mock_session.execute.side_effect = [
                _exec_result(fetchall=[]),
                _exec_result(fetchone=_attr_row(prev_entry_date=None)),
            ]
            response = client.post(
                "/api/windingStock/copy_previous_day",
                json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
            )
        assert response.status_code == 404

    # ---- lock -------------------------------------------------------------

    def test_lock_inserts_when_no_header_exists(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # lock probe
            _exec_result(rowcount=1),     # insert
        ]
        response = client.post(
            "/api/windingStock/lock",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 200
        assert response.json()["data"]["locked"] is True

    def test_unlock_clears_existing_header(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(winding_stock_lock_id=7, is_locked=1)),
            _exec_result(rowcount=1),
        ]
        response = client.post(
            "/api/windingStock/unlock",
            json={"co_id": 1, "branch_id": 2, "tran_date": PAST_DATE},
        )
        assert response.status_code == 200
        assert response.json()["data"]["locked"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_entry.py -v`
Expected: FAIL — every request 404s because the router does not exist yet.

- [ ] **Step 3: Write the router**

Create `src/juteProduction/winding_stock.py`:

```python
"""Winding stock — daily counted yarn snapshot (/api/windingStock).

The mill counts every package of yarn on the floor each morning. This router
stores that count, one row per (bucket, yarn item, package label, area), and
the Winding & Weaving Difference report reconciles the total against book
stock.

Shape follows waste_entry.py: literal {"data": ...} returns, Pydantic body
params, arg-less query builders, soft delete, and validators that answer with a
400 rather than letting MySQL raise a 500.

MT is never stored — it is no_of_samples * wt_per_sample / 1000, computed on
read, so correcting a weight fixes history.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    WINDING_STOCK_BUCKETS,
    WINDING_STOCK_SAMPLES_MAX,
    WINDING_STOCK_WT_MAX,
)
from src.juteProduction.winding_stock_lock import (
    is_day_locked,
    require_edit_if_locked,
)
from src.juteProduction.winding_stock_query import (
    clear_winding_stock_lock_query,
    copy_winding_stock_previous_day_query,
    get_winding_stock_active_by_id_query,
    get_winding_stock_areas_query,
    get_winding_stock_by_date_query,
    get_winding_stock_duplicate_query,
    get_winding_stock_lock_row_query,
    get_winding_stock_prev_entry_date_query,
    get_winding_stock_totals_by_date_query,
    get_winding_stock_wt_default_query,
    get_winding_stock_yarn_items_query,
    insert_winding_stock_lock_query,
    insert_winding_stock_row_query,
    soft_delete_winding_stock_row_query,
    update_winding_stock_lock_query,
    update_winding_stock_row_query,
    verify_winding_stock_area_query,
    verify_winding_stock_item_query,
)

router = APIRouter()

NOT_FOUND_MSG = "Winding stock line not found"
DUPLICATE_MSG = (
    "A line for this bucket, yarn, package and area already exists on this date"
)
FUTURE_DATE_MSG = "tran_date cannot be in the future"
BAD_BUCKET_MSG = f"bucket must be one of {', '.join(WINDING_STOCK_BUCKETS)}"
BAD_ITEM_MSG = "item_id must be an active yarn item (item type 4) for this company"
BAD_AREA_MSG = "wastage_area_id must be an active area of this company and branch"
PACKAGE_REQUIRED_MSG = "package_label is required"
PACKAGE_TOO_LONG_MSG = "package_label cannot exceed 60 characters"
SAMPLES_GATE_MSG = (
    f"no_of_samples must be greater than 0 and at most {WINDING_STOCK_SAMPLES_MAX}"
)
WT_GATE_MSG = (
    f"wt_per_sample must be greater than 0 and at most {WINDING_STOCK_WT_MAX}"
)
COPY_TARGET_NOT_EMPTY_MSG = (
    "This date already has counted lines. Delete them before copying a previous day."
)
NO_PREVIOUS_ENTRY_MSG = "No earlier counted day exists to copy from"


# =============================================================================
# Request models
# =============================================================================


class WindingStockCreate(BaseModel):
    co_id: int
    branch_id: int
    tran_date: date
    bucket: str
    item_id: Optional[int] = None  # NULL for lines with no yarn-master row
    package_label: str
    wastage_area_id: Optional[int] = None
    no_of_samples: float
    wt_per_sample: float  # kg per piece
    remarks: Optional[str] = None


class WindingStockUpdate(BaseModel):
    """Editable fields. tran_date and branch_id are immutable — delete and
    re-add to move a count to another day (mirrors the waste-entry precedent)."""

    bucket: Optional[str] = None
    item_id: Optional[int] = None
    package_label: Optional[str] = None
    wastage_area_id: Optional[int] = None
    no_of_samples: Optional[float] = None
    wt_per_sample: Optional[float] = None
    remarks: Optional[str] = None


class WindingStockDayRef(BaseModel):
    """A (co, branch, date) unit — used by copy-previous-day and the lock."""

    co_id: int
    branch_id: int
    tran_date: date


# =============================================================================
# Param + value helpers
# =============================================================================


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _require_branch_id(request: Request) -> int:
    raw = request.query_params.get("branch_id")
    if not raw:
        raise HTTPException(status_code=400, detail="branch_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branch_id")


def _optional_int_param(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _require_tran_date(request: Request) -> date:
    raw = request.query_params.get("tran_date")
    if not raw:
        raise HTTPException(status_code=400, detail="tran_date is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tran_date format")


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _stock_mt(no_of_samples, wt_per_sample) -> float:
    """MT for one counted line. Rounded to the 3 decimals the sheet prints."""
    return round(_f(no_of_samples) * _f(wt_per_sample) / 1000.0, 3)


def _serialize_dates(row: dict, keys=("tran_date",)) -> dict:
    for k in keys:
        if row.get(k) is not None:
            row[k] = str(row[k])
    return row


def _validate_bucket(bucket: str) -> str:
    value = (bucket or "").strip().upper()
    if value not in WINDING_STOCK_BUCKETS:
        raise HTTPException(status_code=400, detail=BAD_BUCKET_MSG)
    return value


def _clean_package_label(label: str) -> str:
    value = (label or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=PACKAGE_REQUIRED_MSG)
    if len(value) > 60:
        raise HTTPException(status_code=400, detail=PACKAGE_TOO_LONG_MSG)
    return value


def _validate_samples(value) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=SAMPLES_GATE_MSG)
    if n <= 0 or n > WINDING_STOCK_SAMPLES_MAX:
        raise HTTPException(status_code=400, detail=SAMPLES_GATE_MSG)
    return n


def _validate_wt(value) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=WT_GATE_MSG)
    if n <= 0 or n > WINDING_STOCK_WT_MAX:
        raise HTTPException(status_code=400, detail=WT_GATE_MSG)
    return n


def _validate_tran_date(tran_date: date) -> date:
    if tran_date > date.today():
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)
    return tran_date


def _validate_item(db: Session, item_id: Optional[int], co_id: int) -> Optional[int]:
    """None is legitimate: BEAM ON FLOOR / waste lines carry no yarn item."""
    if item_id is None:
        return None
    row = db.execute(
        verify_winding_stock_item_query(),
        {"item_id": int(item_id), "co_id": int(co_id)},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=BAD_ITEM_MSG)
    return int(item_id)


def _validate_area(
    db: Session, wastage_area_id: Optional[int], co_id: int, branch_id: int
) -> Optional[int]:
    if wastage_area_id is None:
        return None
    row = db.execute(
        verify_winding_stock_area_query(),
        {
            "wastage_area_id": int(wastage_area_id),
            "co_id": int(co_id),
            "branch_id": int(branch_id),
        },
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=BAD_AREA_MSG)
    return int(wastage_area_id)


def _reject_duplicate(
    db: Session,
    co_id: int,
    branch_id: int,
    tran_date: date,
    bucket: str,
    item_id: Optional[int],
    package_label: str,
    wastage_area_id: Optional[int],
    exclude_id: Optional[int],
) -> None:
    """Answer what a UNIQUE key cannot: MySQL treats NULLs as distinct, so the
    constraint would let exactly the duplicates that matter through."""
    row = db.execute(
        get_winding_stock_duplicate_query(),
        {
            "co_id": co_id,
            "branch_id": branch_id,
            "tran_date": tran_date,
            "bucket": bucket,
            "item_id": item_id,
            "package_label": package_label,
            "wastage_area_id": wastage_area_id,
            "exclude_id": exclude_id,
        },
    ).fetchone()
    if row:
        raise HTTPException(status_code=400, detail=DUPLICATE_MSG)


# =============================================================================
# Setup
# =============================================================================


@router.get("/setup")
def setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_int_param(request, "branch_id")
    try:
        yarn_items = [
            dict(r._mapping)
            for r in db.execute(
                get_winding_stock_yarn_items_query(), {"co_id": co_id}
            ).fetchall()
        ]
        for item in yarn_items:
            item["yarn_count"] = (
                None if item.get("yarn_count") is None else _f(item["yarn_count"])
            )
        areas = [
            dict(r._mapping)
            for r in db.execute(
                get_winding_stock_areas_query(),
                {"co_id": co_id, "branch_id": branch_id},
            ).fetchall()
        ]
        return {
            "data": {
                "buckets": list(WINDING_STOCK_BUCKETS),
                "yarn_items": yarn_items,
                "areas": areas,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Day read
# =============================================================================


@router.get("/rows_by_date")
def rows_by_date(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    d_val = _require_tran_date(request)
    try:
        binds = {"co_id": co_id, "branch_id": branch_id, "tran_date": d_val}

        rows = []
        for r in db.execute(get_winding_stock_by_date_query(), binds).fetchall():
            row = dict(r._mapping)
            row["no_of_samples"] = _f(row.get("no_of_samples"))
            row["wt_per_sample"] = _f(row.get("wt_per_sample"))
            row["yarn_count"] = (
                None if row.get("yarn_count") is None else _f(row["yarn_count"])
            )
            row["stock_mt"] = _stock_mt(row["no_of_samples"], row["wt_per_sample"])
            _serialize_dates(row)
            rows.append(row)

        bucket_totals = {
            m["bucket"]: _f(m["bucket_mt"])
            for m in (
                dict(r._mapping)
                for r in db.execute(
                    get_winding_stock_totals_by_date_query(), binds
                ).fetchall()
            )
        }
        grand_total_mt = round(sum(bucket_totals.values()), 3)

        prev = db.execute(get_winding_stock_prev_entry_date_query(), binds).fetchone()
        prev_entry_date = (
            str(prev.prev_entry_date)
            if prev is not None and prev.prev_entry_date is not None
            else None
        )

        return {
            "data": {
                "rows": rows,
                "bucket_totals": bucket_totals,
                "grand_total_mt": grand_total_mt,
                "prev_entry_date": prev_entry_date,
                "locked": is_day_locked(db, co_id, branch_id, d_val),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/wt_default")
def wt_default(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The kg-per-piece last saved for this (item, package) key.

    This is the entire weight-per-sample "master" — no separate table exists,
    so overtyping a row makes that value the next default.
    """
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    package_label = _clean_package_label(request.query_params.get("package_label", ""))
    item_id = _optional_int_param(request, "item_id")
    try:
        row = db.execute(
            get_winding_stock_wt_default_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "item_id": item_id,
                "package_label": package_label,
            },
        ).fetchone()
        return {
            "data": {
                "wt_per_sample": None if row is None else _f(row.wt_per_sample)
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Mutations
# =============================================================================


@router.post("/row_create")
def row_create(
    body: WindingStockCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        tran_date = _validate_tran_date(body.tran_date)
        bucket = _validate_bucket(body.bucket)
        package_label = _clean_package_label(body.package_label)
        no_of_samples = _validate_samples(body.no_of_samples)
        wt_per_sample = _validate_wt(body.wt_per_sample)

        require_edit_if_locked(db, token_data, body.co_id, body.branch_id, tran_date)

        item_id = _validate_item(db, body.item_id, body.co_id)
        wastage_area_id = _validate_area(
            db, body.wastage_area_id, body.co_id, body.branch_id
        )
        _reject_duplicate(
            db, int(body.co_id), int(body.branch_id), tran_date, bucket,
            item_id, package_label, wastage_area_id, None,
        )

        result = db.execute(
            insert_winding_stock_row_query(),
            {
                "co_id": int(body.co_id),
                "branch_id": int(body.branch_id),
                "tran_date": tran_date,
                "bucket": bucket,
                "item_id": item_id,
                "package_label": package_label,
                "wastage_area_id": wastage_area_id,
                "no_of_samples": no_of_samples,
                "wt_per_sample": wt_per_sample,
                "remarks": body.remarks,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"winding_stock_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/row_edit/{winding_stock_id}")
def row_edit(
    winding_stock_id: int,
    body: WindingStockUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id = _require_co_id(request)
        existing = db.execute(
            get_winding_stock_active_by_id_query(),
            {"id": winding_stock_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        require_edit_if_locked(
            db, token_data, co_id, existing.branch_id, existing.tran_date
        )

        bucket = (
            _validate_bucket(body.bucket) if body.bucket is not None else existing.bucket
        )
        package_label = (
            _clean_package_label(body.package_label)
            if body.package_label is not None
            else existing.package_label
        )
        no_of_samples = (
            _validate_samples(body.no_of_samples)
            if body.no_of_samples is not None
            else _f(existing.no_of_samples)
        )
        wt_per_sample = (
            _validate_wt(body.wt_per_sample)
            if body.wt_per_sample is not None
            else _f(existing.wt_per_sample)
        )
        item_id = (
            _validate_item(db, body.item_id, co_id)
            if body.item_id is not None
            else existing.item_id
        )
        wastage_area_id = (
            _validate_area(db, body.wastage_area_id, co_id, existing.branch_id)
            if body.wastage_area_id is not None
            else existing.wastage_area_id
        )
        remarks = body.remarks if body.remarks is not None else existing.remarks

        _reject_duplicate(
            db, co_id, int(existing.branch_id), existing.tran_date, bucket,
            item_id, package_label, wastage_area_id, winding_stock_id,
        )

        db.execute(
            update_winding_stock_row_query(),
            {
                "id": winding_stock_id,
                "co_id": co_id,
                "bucket": bucket,
                "item_id": item_id,
                "package_label": package_label,
                "wastage_area_id": wastage_area_id,
                "no_of_samples": no_of_samples,
                "wt_per_sample": wt_per_sample,
                "remarks": remarks,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"winding_stock_id": winding_stock_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/row_delete/{winding_stock_id}")
def row_delete(
    winding_stock_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id = _require_co_id(request)
        existing = db.execute(
            get_winding_stock_active_by_id_query(),
            {"id": winding_stock_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        require_edit_if_locked(
            db, token_data, co_id, existing.branch_id, existing.tran_date
        )

        db.execute(
            soft_delete_winding_stock_row_query(),
            {
                "id": winding_stock_id,
                "co_id": co_id,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"winding_stock_id": winding_stock_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/copy_previous_day")
def copy_previous_day(
    body: WindingStockDayRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Clone the previous counted day's lines into tran_date.

    The operator adds lines manually by design, but the sheet repeats ~30
    near-identical rows daily; retyping them is the biggest source of error.
    Refuses when the target day already has rows, so it can never silently
    double a count.
    """
    try:
        tran_date = _validate_tran_date(body.tran_date)
        require_edit_if_locked(db, token_data, body.co_id, body.branch_id, tran_date)

        binds = {
            "co_id": int(body.co_id),
            "branch_id": int(body.branch_id),
            "tran_date": tran_date,
        }
        existing = db.execute(get_winding_stock_by_date_query(), binds).fetchall()
        if existing:
            raise HTTPException(status_code=400, detail=COPY_TARGET_NOT_EMPTY_MSG)

        prev = db.execute(get_winding_stock_prev_entry_date_query(), binds).fetchone()
        from_date = None if prev is None else prev.prev_entry_date
        if from_date is None:
            raise HTTPException(status_code=404, detail=NO_PREVIOUS_ENTRY_MSG)

        result = db.execute(
            copy_winding_stock_previous_day_query(),
            {**binds, "from_date": from_date, "updated_by": token_data.get("user_id")},
        )
        db.commit()
        return {"data": {"copied": result.rowcount, "from_date": str(from_date)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Lock
# =============================================================================


@router.get("/lock_state")
def lock_state(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    d_val = _require_tran_date(request)
    try:
        return {"data": {"locked": is_day_locked(db, co_id, branch_id, d_val)}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/lock")
def lock(
    body: WindingStockDayRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Freeze the day once its report has been taken.

    An explicit action, never a side effect of running the report: a GET must
    not mutate. A locked day is still editable by a user holding Edit on the
    Winding Stock menu (see winding_stock_lock.require_edit_if_locked).
    """
    try:
        user_id = token_data.get("user_id")
        binds = {
            "co_id": int(body.co_id),
            "branch_id": int(body.branch_id),
            "tran_date": body.tran_date,
        }
        existing = db.execute(get_winding_stock_lock_row_query(), binds).fetchone()
        if existing:
            db.execute(
                update_winding_stock_lock_query(),
                {"id": existing.winding_stock_lock_id, "processed_by": user_id},
            )
        else:
            db.execute(
                insert_winding_stock_lock_query(), {**binds, "processed_by": user_id}
            )
        db.commit()
        return {"data": {"locked": True}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unlock")
def unlock(
    body: WindingStockDayRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        binds = {
            "co_id": int(body.co_id),
            "branch_id": int(body.branch_id),
            "tran_date": body.tran_date,
        }
        existing = db.execute(get_winding_stock_lock_row_query(), binds).fetchone()
        if existing:
            db.execute(
                clear_winding_stock_lock_query(),
                {
                    "id": existing.winding_stock_lock_id,
                    "updated_by": token_data.get("user_id"),
                },
            )
            db.commit()
        return {"data": {"locked": False}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 4: Register the router in `src/main.py`**

Add the import next to the sibling winding import (near line 113):

```python
from src.juteProduction.winding_stock import router as winding_stock_router
```

Add the registration next to the sibling winding registration (near line 286):

```python
app.include_router(winding_stock_router, prefix="/api/windingStock", tags=["jute-winding"])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_entry.py -v`
Expected: PASS, all tests.

- [ ] **Step 6: Run the whole winding stock suite**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_query.py src/test/test_winding_stock_lock.py src/test/test_winding_stock_entry.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/juteProduction/winding_stock.py src/juteProduction/winding_stock_query.py src/main.py src/test/test_winding_stock_entry.py
git commit -m "feat(winding-stock): /api/windingStock entry router with lock and copy-previous-day"
```

---

## Task 5: Report endpoint

**Files:**
- Modify: `src/juteProduction/winding_stock_query.py` (append 2 builders)
- Modify: `src/juteProduction/constants.py` (append UOM sets)
- Modify: `src/juteProduction/winding_reports.py` (add pure builders + 1 endpoint)
- Modify: `src/test/test_winding_stock_query.py` (extend the builder list)
- Create: `src/test/test_winding_stock_report.py`

**Interfaces:**
- Consumes: `get_winding_reconciliation_query`, `get_dpr_winding_mtd_query`, `get_dpr_weaving_mtd_query`, `get_weaving_entries_by_date_query`, `_issue_cte`, and the Task 2 builders.
- Produces: `GET /api/juteProductionReports/winding_weaving_difference` and the pure helpers `stock_mt_of`, `build_stock_blocks`, `build_reconciliation`, `build_cloth_split`, `purchase_qty_to_mt`.

**Context an implementer needs before writing a line of this:**
- Winding builders are **branch-keyed** and carry no `co_id` predicate; weaving builders **require `co_id`** and tolerate a NULL branch. The endpoint therefore takes both and must verify the pair, or a branch of another company reports that company's weaving.
- Winding returns **kg**; weaving day slice and weaving MTD both return **`production_mt`**; `jute_issue.weight` is **quintal**; purchased-yarn `approved_qty` is in **whatever UOM the line carries**.
- Purchased yarn currently resolves to **zero rows on sls** because it is booked against a misc-expenses item. The query stays keyed on `item_type_id = 4` and starts working the day that item is reclassified — no code change. This is a known, accepted state, recorded in the spec.

- [ ] **Step 1: Append the two builders to `src/juteProduction/winding_stock_query.py`**

Add these imports at the top of the file:

```python
from src.juteProcurement.reportQueries import _issue_cte
```

Append:

```python
def get_jute_issue_weight_query():
    """Total jute issued for a branch over a date range, in QUINTAL.

    Reuses _issue_cte from the jute procurement reports rather than rewriting the
    eligibility rules: it already unions the waste-issue leg and applies the
    settled attribution rule (jute_mr_li.actual_item_id, not jute_issue.item_id)
    plus the MR-stock-eligible status filter.

    jute_issue.weight is in QUINTAL — the caller divides by 10 for MT.
    """
    return text(
        f"""
        SELECT COALESCE(SUM(x.wt), 0) AS issue_qtl
        FROM ({_issue_cte("{d} BETWEEN :date_from AND :date_to")}) x
        """
    )


def get_yarn_purchase_qty_query():
    """Purchased yarn received for a branch over a date range.

    'Purchase yarn' = approved store-receipt lines (proc_inward + proc_inward_dtl,
    sr_status = 3, line active = 1) whose item resolves to a yarn item group.
    Receipt date is proc_inward.inward_date — the column every stores report uses
    (src/inventory/reportQueries.py); sr_date diverges on about half the rows and
    has no documented rule.

    proc_inward_dtl has NO weight column, and approved_qty is in whatever uom the
    line carries. The distinct uom names are returned so the caller converts
    explicitly and flags anything it does not recognise, rather than assuming.

    KNOWN STATE: this returns 0 on sls today. Purchased yarn is really booked
    against item 334018 'spools/yarn', which sits in a MISC EXPENSES group with
    item_type_id = 1, so the yarn filter matches nothing. The filter is
    deliberately left honest — reclassifying that item into a yarn group makes
    this query start working with no code change.

    Company scope lives on item_grp_mst.co_id — item_mst has no co_id column.
    """
    return text(
        f"""
        SELECT COALESCE(SUM(pid.approved_qty), 0) AS purchase_qty,
               GROUP_CONCAT(DISTINCT um.uom_name) AS uoms
        FROM proc_inward_dtl pid
        INNER JOIN proc_inward pi ON pi.inward_id = pid.inward_id
        INNER JOIN item_mst im ON im.item_id = pid.item_id
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN uom_mst um ON um.uom_id = pid.uom_id
        WHERE pid.active = 1
          AND pi.sr_status = 3
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
          AND ig.co_id = :co_id
          AND pi.branch_id = :branch_id
          AND pi.inward_date BETWEEN :date_from AND :date_to
        """
    )
```

- [ ] **Step 2: Append the UOM sets to `src/juteProduction/constants.py`**

```python
# Purchased-yarn UOM handling. MTR is METRE and MT / MTS are METRIC TONNE —
# genuinely different units that look alike, and sls carries both labels on the
# same item. The report converts only what it recognises and reports the rest as
# an unknown-unit warning; silently dividing an unrecognised unit by 1000 is the
# bug this set exists to prevent.
PURCHASE_UOM_MT = ("MT", "MTS", "M.TON", "METRIC TON", "METRIC TONNE")
PURCHASE_UOM_KG = ("KG", "KGS", "KGM", "KILOGRAM")
```

- [ ] **Step 3: Write the failing report tests**

Create `src/test/test_winding_stock_report.py`. The golden case is transcribed from the mill's printed sheet of 27-01-2026:

```python
"""Winding & Weaving Difference report.

The headline test is the GOLDEN CASE — every figure below is transcribed from
the mill's own printed sheet for 27-01-2026, so a change that breaks the
reconciliation breaks against real, signed-off arithmetic rather than against
numbers invented here.
"""

from datetime import date

import pytest

from src.juteProduction.constants import (
    CLOTH_SPLIT_HESSIAN,
    CLOTH_SPLIT_OTHER,
    CLOTH_SPLIT_SACKING,
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
    WINDING_STOCK_BUCKET_BALL,
    WINDING_STOCK_BUCKET_MILL_USE,
    WINDING_STOCK_BUCKET_PURCHASE,
    WINDING_STOCK_BUCKET_SALE_YARN,
    WINDING_STOCK_BUCKET_WASTE,
)
from src.juteProduction.winding_reports import (
    build_cloth_split,
    build_reconciliation,
    build_stock_blocks,
    purchase_qty_to_mt,
    stock_mt_of,
)


def _row(bucket, samples, wt, **extra):
    row = {
        "bucket": bucket,
        "no_of_samples": samples,
        "wt_per_sample": wt,
        "item_id": extra.get("item_id"),
        "package_label": extra.get("package_label", "PKG"),
        "item_code": None,
        "item_name": None,
        "quality_name": None,
        "yarn_count": None,
        "wastage_area_id": None,
        "area_name": None,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------- line maths


def test_stock_mt_of_matches_the_sheets_first_row():
    """185 samples x 14 kg = 2,590 kg = 2.590 MT (HESSIAN WEFT, 9.50LBS)."""
    assert stock_mt_of(185, 14) == 2.59


def test_stock_mt_of_handles_none():
    assert stock_mt_of(None, 14) == 0.0
    assert stock_mt_of(185, None) == 0.0


# ------------------------------------------------------------- GOLDEN CASE


GOLDEN_ROWS = [
    # Mill use totals 22.706 on the sheet; two lines standing in for its 16.
    _row(WINDING_STOCK_BUCKET_MILL_USE, 185, 14),        #  2.590
    _row(WINDING_STOCK_BUCKET_MILL_USE, 20116, 1),       # 20.116
    _row(WINDING_STOCK_BUCKET_PURCHASE, 209, 28.89),     #  6.038  OUT SIDE PURCHASE MACKROL FOR S4
    _row(WINDING_STOCK_BUCKET_SALE_YARN, 230, 50),       # 11.500  28.LBS 3PLY HANK
    _row(WINDING_STOCK_BUCKET_SALE_YARN, 76, 56),        #  4.256  20.LBS COP (BAG)
    _row(WINDING_STOCK_BUCKET_SALE_YARN, 182, 50),       #  9.100  10.00LBS 3PLY HANK-GOLDEN
    _row(WINDING_STOCK_BUCKET_BALL, 2116, 1),            #  2.116
    _row(WINDING_STOCK_BUCKET_WASTE, 60, 1),             #  0.060  THREAD WASTE
    _row(WINDING_STOCK_BUCKET_WASTE, 33, 1),             #  0.033  DIRTY BOBBIN
    _row(WINDING_STOCK_BUCKET_WASTE, 20, 1),             #  0.020  DAMAGE SPOOL
    _row(WINDING_STOCK_BUCKET_WASTE, 21, 1),             #  0.021  ENTANGLE COPS
]


def test_golden_bucket_subtotals_match_the_printed_sheet():
    blocks = build_stock_blocks(GOLDEN_ROWS, [])
    assert blocks[WINDING_STOCK_BUCKET_MILL_USE]["subtotal_mt"] == 22.706
    assert blocks[WINDING_STOCK_BUCKET_PURCHASE]["subtotal_mt"] == 6.038
    assert blocks[WINDING_STOCK_BUCKET_SALE_YARN]["subtotal_mt"] == 24.856
    assert blocks[WINDING_STOCK_BUCKET_BALL]["subtotal_mt"] == 2.116
    assert blocks[WINDING_STOCK_BUCKET_WASTE]["subtotal_mt"] == 0.134


def test_golden_grand_total_matches_the_printed_sheet():
    """22.706 + 6.038 + 24.856 + 2.116 + 0.134 = 55.850 — STOCK GRAND TOTAL."""
    blocks = build_stock_blocks(GOLDEN_ROWS, [])
    assert blocks["grand_total_mt"] == 55.850


def test_golden_reconciliation_matches_the_printed_sheet():
    """opening 18.415 + winding 37.875 = should-be 56.290; actual 55.850 gives
    the sheet's WINDING WEAVING DIFFERENCE of -0.440, i.e. -3.47% of winding."""
    recon = build_reconciliation(
        opening_mt=18.415,
        winding_mt=37.875,
        weaving_mt=0.0,
        purchase_mt=0.0,
        sale_mt=0.0,
        actual_mt=55.850,
    )
    assert recon["should_be_closing_mt"] == 56.290
    assert recon["difference_mt"] == -0.440
    assert recon["difference_pct"] == -1.16  # -0.440 / 37.875 * 100


def test_golden_day_percentage_matches_the_printed_sheet():
    """The sheet's TODAY figure: -0.440 against the day's winding of 12.697,
    i.e. -3.47%. Opening is back-solved so should-be lands on the sheet's 56.290."""
    recon = build_reconciliation(
        opening_mt=43.593,
        winding_mt=12.697,
        weaving_mt=0.0,
        purchase_mt=0.0,
        sale_mt=0.0,
        actual_mt=55.850,
    )
    assert recon["difference_pct"] == -3.47


# ------------------------------------------------------- reconciliation rules


def test_weaving_and_sale_are_subtracted_purchase_is_added():
    recon = build_reconciliation(
        opening_mt=10.0, winding_mt=5.0, weaving_mt=3.0,
        purchase_mt=2.0, sale_mt=1.0, actual_mt=13.0,
    )
    assert recon["should_be_closing_mt"] == 13.0
    assert recon["difference_mt"] == 0.0


def test_zero_winding_does_not_divide_by_zero():
    recon = build_reconciliation(
        opening_mt=10.0, winding_mt=0.0, weaving_mt=0.0,
        purchase_mt=0.0, sale_mt=0.0, actual_mt=9.0,
    )
    assert recon["difference_mt"] == -1.0
    assert recon["difference_pct"] is None


# -------------------------------------------------------------- cloth split


def test_cloth_split_groups_by_quality_type():
    rows = [
        {"weaving_quality_id": 1, "production_mt": 2.0},
        {"weaving_quality_id": 2, "production_mt": 3.0},
        {"weaving_quality_id": 3, "production_mt": 1.5},
    ]
    qtype = {
        1: FINISHING_QUALITY_TYPE_HESSIAN,
        2: FINISHING_QUALITY_TYPE_SACKING,
        3: None,
    }
    split = build_cloth_split(rows, qtype)
    assert split[CLOTH_SPLIT_HESSIAN] == 2.0
    assert split[CLOTH_SPLIT_SACKING] == 3.0
    assert split[CLOTH_SPLIT_OTHER] == 1.5


def test_cloth_split_always_sums_to_total_weaving():
    """Unclassified qualities fall to OTHER so nothing can silently vanish —
    on sls today every quality is unclassified and OTHER carries the lot."""
    rows = [
        {"weaving_quality_id": 9, "production_mt": 4.25},
        {"weaving_quality_id": 8, "production_mt": 1.75},
    ]
    split = build_cloth_split(rows, {})
    assert split[CLOTH_SPLIT_OTHER] == 6.0
    assert round(sum(split.values()), 3) == 6.0


# ------------------------------------------------------------- purchase UOM


def test_purchase_mt_from_metric_tonne_is_unchanged():
    mt, unknown = purchase_qty_to_mt(654.608, "MT")
    assert mt == 654.608
    assert unknown == []


def test_purchase_mt_from_kg_is_divided():
    mt, unknown = purchase_qty_to_mt(2000, "KGS")
    assert mt == 2.0
    assert unknown == []


def test_purchase_metre_is_not_treated_as_tonnes():
    """MTR is METRE. Converting it to MT would invent tonnage out of length."""
    mt, unknown = purchase_qty_to_mt(654.608, "MTR")
    assert mt == 0.0
    assert unknown == ["MTR"]


def test_purchase_multiple_uoms_reports_every_unknown():
    mt, unknown = purchase_qty_to_mt(100, "MT,MTR,PCS")
    assert unknown == ["MTR", "PCS"]
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_report.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_reconciliation' from 'src.juteProduction.winding_reports'`

- [ ] **Step 5: Add the pure builders to `src/juteProduction/winding_reports.py`**

Append these above the endpoints. They take plain dicts and do no I/O, which is what makes the golden case testable without a database.

```python
def stock_mt_of(no_of_samples, wt_per_sample) -> float:
    """MT for one counted line, at the 3 decimals the sheet prints."""
    return round(_f(no_of_samples) * _f(wt_per_sample) / 1000.0, 3)


def _prev_key(row: dict):
    """Identity of a counted line across days, for the PRVS STOCK column."""
    return (
        row.get("bucket"),
        row.get("item_id"),
        row.get("package_label"),
        row.get("wastage_area_id"),
    )


def build_stock_blocks(rows, prev_rows) -> dict:
    """Group counted lines into the report's buckets, in print order.

    Returns {bucket: {"rows": [...], "subtotal_mt": float}, "grand_total_mt": float}.
    Every bucket in WINDING_STOCK_BUCKETS appears even when empty, so the printed
    report keeps a stable shape day to day.
    """
    prev_mt = {}
    for r in prev_rows:
        prev_mt[_prev_key(r)] = prev_mt.get(_prev_key(r), 0.0) + stock_mt_of(
            r.get("no_of_samples"), r.get("wt_per_sample")
        )

    blocks = {b: {"rows": [], "subtotal_mt": 0.0} for b in WINDING_STOCK_BUCKETS}
    for r in rows:
        bucket = r.get("bucket")
        if bucket not in blocks:
            # An unknown bucket must not silently disappear from the totals.
            blocks[bucket] = {"rows": [], "subtotal_mt": 0.0}
        out = dict(r)
        out["stock_mt"] = stock_mt_of(r.get("no_of_samples"), r.get("wt_per_sample"))
        out["prvs_stock_mt"] = round(prev_mt.get(_prev_key(r), 0.0), 3)
        blocks[bucket]["rows"].append(out)
        blocks[bucket]["subtotal_mt"] = round(
            blocks[bucket]["subtotal_mt"] + out["stock_mt"], 3
        )

    blocks["grand_total_mt"] = round(
        sum(b["subtotal_mt"] for b in blocks.values() if isinstance(b, dict)), 3
    )
    return blocks


def build_reconciliation(
    opening_mt, winding_mt, weaving_mt, purchase_mt, sale_mt, actual_mt
) -> dict:
    """Book stock vs counted stock.

        should_be = opening + winding + purchase - weaving - sale
        difference = actual - should_be
        pct        = difference / winding * 100

    JUTE ISSUE is deliberately absent: it feeds spinning, not winding stock, and
    the mill's own sheet closes without it (18.415 + 37.875 = 56.290).

    pct is None rather than 0 when winding is zero — a percentage of nothing is
    not a percentage, and printing 0.00% would read as "no variance".
    """
    should_be = round(
        _f(opening_mt) + _f(winding_mt) + _f(purchase_mt) - _f(weaving_mt) - _f(sale_mt),
        3,
    )
    difference = round(_f(actual_mt) - should_be, 3)
    pct = None if _f(winding_mt) == 0 else round(difference / _f(winding_mt) * 100, 2)
    return {
        "opening_mt": round(_f(opening_mt), 3),
        "winding_mt": round(_f(winding_mt), 3),
        "weaving_mt": round(_f(weaving_mt), 3),
        "purchase_mt": round(_f(purchase_mt), 3),
        "sale_mt": round(_f(sale_mt), 3),
        "should_be_closing_mt": should_be,
        "actual_closing_mt": round(_f(actual_mt), 3),
        "difference_mt": difference,
        "difference_pct": pct,
    }


def build_cloth_split(weaving_rows, qtype_by_qid) -> dict:
    """Weaving production split hessian / sacking / other.

    Classification comes from jute_prod_finishing_quality.quality_type resolved
    through the cloth item (1 = Hessian, 2 = Sacking). Anything unclassified
    lands in OTHER so the three buckets always sum to total weaving production.

    Known state: on sls every weaving quality points at the JCLOTH-MIG catch-all
    item, which has no finishing row, so OTHER carries 100% until those qualities
    are pointed at real cloth items in the masters.
    """
    split = {
        CLOTH_SPLIT_HESSIAN: 0.0,
        CLOTH_SPLIT_SACKING: 0.0,
        CLOTH_SPLIT_OTHER: 0.0,
    }
    for r in weaving_rows:
        qtype = qtype_by_qid.get(r.get("weaving_quality_id"))
        if qtype == FINISHING_QUALITY_TYPE_HESSIAN:
            key = CLOTH_SPLIT_HESSIAN
        elif qtype == FINISHING_QUALITY_TYPE_SACKING:
            key = CLOTH_SPLIT_SACKING
        else:
            key = CLOTH_SPLIT_OTHER
        split[key] = round(split[key] + _f(r.get("production_mt")), 3)
    return split


def purchase_qty_to_mt(qty, uoms):
    """Convert a purchased-yarn quantity to MT, refusing to guess.

    Returns (mt, unknown_uom_names). MTR is METRE and MT / MTS are METRIC TONNE —
    different units that read alike, and the same sls item carries both labels.
    An unrecognised unit contributes 0 and is reported, because inventing tonnage
    out of a length is worse than a visibly missing figure.

    Mixed units on one line set cannot be apportioned, so the whole quantity is
    converted with the first recognised unit and every other unit is flagged.
    """
    names = [u.strip().upper() for u in (uoms or "").split(",") if u.strip()]
    unknown = [
        n for n in names if n not in PURCHASE_UOM_MT and n not in PURCHASE_UOM_KG
    ]
    if any(n in PURCHASE_UOM_MT for n in names):
        return round(_f(qty), 3), unknown
    if any(n in PURCHASE_UOM_KG for n in names):
        return round(_f(qty) / 1000.0, 3), unknown
    return 0.0, unknown
```

Extend the import block at the top of `winding_reports.py`:

```python
from src.juteProduction.constants import (
    BUNDLE_KG,
    CLOTH_SPLIT_HESSIAN,
    CLOTH_SPLIT_OTHER,
    CLOTH_SPLIT_SACKING,
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
    PURCHASE_UOM_KG,
    PURCHASE_UOM_MT,
    WINDING_STOCK_BUCKETS,
)
```

- [ ] **Step 6: Run the pure-builder tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_report.py -v`
Expected: PASS, 15 tests — including the four golden-case assertions.

- [ ] **Step 7: Add the endpoint to `src/juteProduction/winding_reports.py`**

```python
DIFFERENCE_NOTE = (
    "JUTE ISSUE is shown for context only and is not part of the should-be "
    "closing stock — it feeds spinning, not winding. Purchase yarn reads 0 until "
    "purchased yarn is booked against an item in a yarn item group. Cloth split "
    "falls to OTHER for any weaving quality whose cloth item has no finishing "
    "quality_type."
)


def _sum_col(rows, col) -> float:
    return round(sum(_f(r.get(col)) for r in rows), 3)


def _stock_total_on(db, co_id, branch_id, d_val) -> float:
    """Counted grand total MT for one date (0.0 when that date has no count)."""
    rows = db.execute(
        get_winding_stock_totals_by_date_query(),
        {"co_id": co_id, "branch_id": branch_id, "tran_date": d_val},
    ).fetchall()
    return round(sum(_f(dict(r._mapping)["bucket_mt"]) for r in rows), 3)


def _prev_entry_date(db, co_id, branch_id, before):
    row = db.execute(
        get_winding_stock_prev_entry_date_query(),
        {"co_id": co_id, "branch_id": branch_id, "tran_date": before},
    ).fetchone()
    return None if row is None else row.prev_entry_date


@router.get("/winding_weaving_difference")
def winding_weaving_difference(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Counted winding stock reconciled against book stock, day and month-to-date.

    Both co_id and branch_id are required: the winding builders are branch-keyed
    with no company predicate, while the weaving builders are company-keyed and
    NULL-branch tolerant. The pair check below stops a branch of company A from
    reporting company B's weaving under this header.
    """
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    d = request.query_params.get("report_date") or request.query_params.get("tran_date")
    if not d:
        raise HTTPException(status_code=400, detail="report_date is required")
    try:
        report_d = date.fromisoformat(d)
        month_start = report_d.replace(day=1)

        pair_ok = db.execute(
            text("SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"),
            {"branch_id": branch_id, "co_id": co_id},
        ).first()
        if not pair_ok:
            raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")

        stock_binds = {"co_id": co_id, "branch_id": branch_id, "tran_date": report_d}

        # --- counted stock, this day and the previous counted day -------------
        rows = [
            dict(r._mapping)
            for r in db.execute(get_winding_stock_by_date_query(), stock_binds).fetchall()
        ]
        prev_day = _prev_entry_date(db, co_id, branch_id, report_d)
        prev_rows = (
            []
            if prev_day is None
            else [
                dict(r._mapping)
                for r in db.execute(
                    get_winding_stock_by_date_query(),
                    {**stock_binds, "tran_date": prev_day},
                ).fetchall()
            ]
        )
        stock = build_stock_blocks(rows, prev_rows)
        actual_mt = stock["grand_total_mt"]

        # --- openings ---------------------------------------------------------
        day_opening = 0.0 if prev_day is None else _stock_total_on(db, co_id, branch_id, prev_day)
        month_open_day = _prev_entry_date(db, co_id, branch_id, month_start)
        mtd_opening = (
            0.0
            if month_open_day is None
            else _stock_total_on(db, co_id, branch_id, month_open_day)
        )

        # --- winding: kg -> MT ------------------------------------------------
        wind_day_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_winding_reconciliation_query(),
                {"branch_id": branch_id, "tran_date": report_d,
                 "spell_id": None, "item_id": None},
            ).fetchall()
        ]
        wind_mtd_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_dpr_winding_mtd_query(),
                {"branch_id": branch_id, "date_from": month_start, "report_date": report_d},
            ).fetchall()
        ]
        wind_day_mt = round(_sum_col(wind_day_rows, "production_kg") / 1000.0, 3)
        wind_mtd_mt = round(_sum_col(wind_mtd_rows, "production_kg") / 1000.0, 3)

        # --- weaving: already MT ---------------------------------------------
        weav_day_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_weaving_entries_by_date_query(),
                {"co_id": co_id, "branch_id": branch_id, "tran_date": report_d,
                 "spell_id": None, "machine_id": None},
            ).fetchall()
        ]
        weav_mtd_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_dpr_weaving_mtd_query(),
                {"co_id": co_id, "branch_id": branch_id,
                 "date_from": month_start, "report_date": report_d},
            ).fetchall()
        ]
        weav_day_mt = _sum_col(weav_day_rows, "production_mt")
        weav_mtd_mt = _sum_col(weav_mtd_rows, "production_mt")

        # --- jute issue: quintal -> MT (context only) -------------------------
        def _issue_mt(date_from, date_to):
            row = db.execute(
                get_jute_issue_weight_query(),
                {"branch_id": branch_id, "date_from": date_from, "date_to": date_to},
            ).fetchone()
            return round(_f(None if row is None else row.issue_qtl) / 10.0, 3)

        # --- purchase yarn: converted per UOM, never guessed ------------------
        def _purchase_mt(date_from, date_to):
            row = db.execute(
                get_yarn_purchase_qty_query(),
                {"co_id": co_id, "branch_id": branch_id,
                 "date_from": date_from, "date_to": date_to},
            ).fetchone()
            if row is None:
                return 0.0, []
            return purchase_qty_to_mt(row.purchase_qty, row.uoms)

        # --- sale yarn: kg -> MT ---------------------------------------------
        def _sale_mt(date_from, date_to):
            row = db.execute(
                get_sale_yarn_mt_query(),
                {"branch_id": branch_id, "date_from": date_from, "date_to": date_to},
            ).fetchone()
            return round(_f(None if row is None else row.sale_kg) / 1000.0, 3)

        purch_day_mt, purch_day_unknown = _purchase_mt(report_d, report_d)
        purch_mtd_mt, purch_mtd_unknown = _purchase_mt(month_start, report_d)

        day = build_reconciliation(
            opening_mt=day_opening,
            winding_mt=wind_day_mt,
            weaving_mt=weav_day_mt,
            purchase_mt=purch_day_mt,
            sale_mt=_sale_mt(report_d, report_d),
            actual_mt=actual_mt,
        )
        day["jute_issue_mt"] = _issue_mt(report_d, report_d)

        mtd = build_reconciliation(
            opening_mt=mtd_opening,
            winding_mt=wind_mtd_mt,
            weaving_mt=weav_mtd_mt,
            purchase_mt=purch_mtd_mt,
            sale_mt=_sale_mt(month_start, report_d),
            actual_mt=actual_mt,
        )
        mtd["jute_issue_mt"] = _issue_mt(month_start, report_d)

        # --- cloth split ------------------------------------------------------
        qtype_by_qid = {
            m["weaving_quality_id"]: m["quality_type"]
            for m in (
                dict(r._mapping)
                for r in db.execute(
                    get_weaving_quality_type_map_query(), {"co_id": co_id}
                ).fetchall()
            )
        }

        # A chain break means the day opening came from an older count, so the
        # day figures are stated against that date rather than silently implying
        # yesterday.
        expected_prev = report_d - timedelta(days=1)
        chain_ok = prev_day == expected_prev

        return {
            "data": {
                "report_date": str(report_d),
                "date_from": str(month_start),
                "stock": stock,
                "production": {"day": day, "mtd": mtd},
                "cloth_split": {
                    "day": build_cloth_split(weav_day_rows, qtype_by_qid),
                    "mtd": build_cloth_split(weav_mtd_rows, qtype_by_qid),
                },
                "chain": {
                    "ok": chain_ok,
                    "prev_entry_date": None if prev_day is None else str(prev_day),
                    "month_opening_date": (
                        None if month_open_day is None else str(month_open_day)
                    ),
                },
                "warnings": {
                    "purchase_unknown_uoms": sorted(
                        set(purch_day_unknown) | set(purch_mtd_unknown)
                    )
                },
                "note": DIFFERENCE_NOTE,
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

Extend the imports at the top of `winding_reports.py`:

```python
from datetime import date, timedelta

from sqlalchemy import text

from src.juteProduction.dprQueries import (
    get_dpr_weaving_mtd_query,
    get_dpr_winding_mtd_query,
)
from src.juteProduction.weaving_query import get_weaving_entries_by_date_query
from src.juteProduction.winding_stock_query import (
    get_jute_issue_weight_query,
    get_sale_yarn_mt_query,
    get_weaving_quality_type_map_query,
    get_winding_stock_by_date_query,
    get_winding_stock_prev_entry_date_query,
    get_winding_stock_totals_by_date_query,
    get_yarn_purchase_qty_query,
)
```

Also add `_require_co_id` next to the existing `_require_branch_id` in that file (copy it verbatim from `winding_stock.py`).

- [ ] **Step 8: Extend the builder smoke list**

In `src/test/test_winding_stock_query.py`, add to `BUILDERS`:

```python
    "get_jute_issue_weight_query",
    "get_yarn_purchase_qty_query",
```

- [ ] **Step 9: Run the whole backend suite for this feature**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_winding_stock_query.py src/test/test_winding_stock_lock.py src/test/test_winding_stock_entry.py src/test/test_winding_stock_report.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/juteProduction/winding_reports.py src/juteProduction/winding_stock_query.py src/juteProduction/constants.py src/test/test_winding_stock_report.py src/test/test_winding_stock_query.py
git commit -m "feat(winding-stock): winding & weaving difference report endpoint"
```

---

## Task 6: Menus

**Files:**
- Create: `dbqueries/migrations/20260819_winding_stock_menus.sql`

**Interfaces:**
- Consumes: nothing in code — this is what makes both pages reachable.
- Produces: `menu_mst` rows `'Winding Stock'` (entry, sidebar) and `'Winding & Weaving Difference'` (report, hub dropdown), plus `role_menu_map` grants.

Two different shapes, and getting them the wrong way round is the classic mistake here:

| | Entry page | Report page |
|---|---|---|
| parent | the **'Jute Production'** row | the **'Production Reports' hub** |
| `report` column | not written (stays 0/NULL) | **`report = 1`** — the hub dropdown filters on it |
| `order_by` | hardcoded **166** (Winding Production is 165) | `MAX(order_by)+1` among hub children |

Note every jute-production entry page is a **flat sibling** under 'Jute Production' — nothing nests under 'Winding Production' today, and the sidebar has never rendered 3-level nesting. Do not be the first to try it here.

- [ ] **Step 1: Write the migration**

Create `dbqueries/migrations/20260819_winding_stock_menus.sql`. Every comment block ends with a lone `;` so the following statement starts a clean chunk — the runner splits on `;` and silently skips any chunk whose trimmed text starts with `--`.

```sql
-- =============================================================================
-- Migration: Winding Stock entry page + Winding & Weaving Difference report
-- =============================================================================
-- Date: 2026-08-19
-- TARGET: tenant DB only (dev3, then sls once BE+FE are deployed there).
-- vowconsole3.portal_menu_mst deliberately untouched — it has no juteProduction
-- hub template at all; every sibling report under the hub is a tenant-only row.
--
-- Nothing hardcodes a menu_id, module id or role_id: every id resolves at run
-- time off existing rows, so the same script promotes to other tenants
-- unmodified. If an anchor fails to resolve the derived table is empty and the
-- INSERT is a silent no-op — always run the VERIFICATION queries.
--
-- POST-APPLY: portal sidebars cache their menu tree in localStorage, so users
-- MUST log out and back in before either page appears.
;

-- A1. menu_mst — 'Winding Stock' entry page, flat sibling under Jute Production
-- (Winding Production is order_by 165, Doff Data Editor 171)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Winding Stock', CONCAT(p.menu_path, '/windingStock'), 1, p.menu_id, NULL, 'inventory_2',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       166
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Winding Stock');

-- A2. role_menu_map — mirror whoever can already reach Winding Production
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT rmm.role_id, m.menu_id, rmm.access_type_id, 1
FROM role_menu_map rmm
JOIN menu_mst wind ON wind.menu_id = rmm.menu_id AND wind.menu_name = 'Winding Production'
JOIN menu_mst m ON m.menu_name = 'Winding Stock'
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.menu_id = m.menu_id AND x.role_id = rmm.role_id AND x.access_type_id = rmm.access_type_id
);

-- B1. menu_mst — report row under the Production Reports hub. report = 1 is what
-- puts it in the hub dropdown (fetchReportMenuTree filters report = 1 children).
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Winding & Weaving Difference', CONCAT(p.menu_path, '/windingWeavingDifference'), p.menu_id, 'compare_arrows', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Winding & Weaving Difference');

-- B2. role_menu_map — mirror each role's access on the hub (or any hub child),
-- taking the MAX level held, so exactly today's Production Reports audience can
-- open the new report.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    JOIN (
        SELECT menu_id
        FROM menu_mst
        WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
        ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
        LIMIT 1
    ) hub ON existing.menu_id = hub.menu_id OR existing.menu_parent_id = hub.menu_id
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Winding & Weaving Difference') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
--     FROM menu_mst WHERE menu_name IN ('Winding Stock', 'Winding & Weaving Difference')
--
--   SELECT r.role_id, m.menu_name, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_name IN ('Winding Stock', 'Winding & Weaving Difference')
--    ORDER BY m.menu_name, r.role_id
--
-- Then re-login and confirm: 'Winding Stock' in the Jute Production sidebar just
-- below Winding Production, and 'Winding & Weaving Difference' in the Production
-- Reports dropdown.
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_name IN ('Winding Stock', 'Winding & Weaving Difference')
--   DELETE FROM menu_mst WHERE menu_name IN ('Winding Stock', 'Winding & Weaving Difference')
-- =============================================================================
```

- [ ] **Step 2: Apply to dev3**

Use the `run-migration` skill, target **dev3**.

- [ ] **Step 3: Verify both rows landed**

Run the VERIFICATION queries from the file. Expected: two `menu_mst` rows — `Winding Stock` with `report` 0/NULL under the Jute Production parent, and `Winding & Weaving Difference` with `report = 1` under the hub — plus at least one `role_menu_map` row each.

- [ ] **Step 4: Commit**

```bash
git add dbqueries/migrations/20260819_winding_stock_menus.sql
git commit -m "feat(winding-stock): tenant menu rows for the entry page and the difference report"
```

---

## Task 7: Frontend API routes + services

**Files:**
- Modify: `../vowerp3ui/src/utils/api.ts`
- Create: `../vowerp3ui/src/utils/windingStockService.ts`
- Modify: `../vowerp3ui/src/utils/juteProductionReportService.ts`

**Interfaces:**
- Consumes: the Task 4 and Task 5 endpoints.
- Produces: `apiRoutesPortalMasters.WINDING_STOCK_*`, the `windingStockService` functions, and `fetchWindingWeavingDifference`.

- [ ] **Step 1: Add the route constants**

In `src/utils/api.ts`, immediately after the existing winding block (the `WINDING_QUALITY_BY_DATE` line):

```ts
    // Winding stock — daily counted snapshot (router prefix /api/windingStock).
    // *_EDIT / *_DELETE are base paths; caller appends /${id}?co_id=...
    WINDING_STOCK_SETUP: `${API_URL}/windingStock/setup`,
    WINDING_STOCK_ROWS_BY_DATE: `${API_URL}/windingStock/rows_by_date`,
    WINDING_STOCK_WT_DEFAULT: `${API_URL}/windingStock/wt_default`,
    WINDING_STOCK_ROW_CREATE: `${API_URL}/windingStock/row_create`,
    WINDING_STOCK_ROW_EDIT: `${API_URL}/windingStock/row_edit`,
    WINDING_STOCK_ROW_DELETE: `${API_URL}/windingStock/row_delete`,
    WINDING_STOCK_COPY_PREVIOUS_DAY: `${API_URL}/windingStock/copy_previous_day`,
    WINDING_STOCK_LOCK_STATE: `${API_URL}/windingStock/lock_state`,
    WINDING_STOCK_LOCK: `${API_URL}/windingStock/lock`,
    WINDING_STOCK_UNLOCK: `${API_URL}/windingStock/unlock`,
```

And next to `JUTE_PROD_DPR`:

```ts
    WINDING_WEAVING_DIFFERENCE: `${API_URL}/juteProductionReports/winding_weaving_difference`,
```

- [ ] **Step 2: Write the entry service**

Create `src/utils/windingStockService.ts`:

```ts
/**
 * Service layer for the Winding Stock daily count.
 *
 * Backend: /api/windingStock (vowerp3be src/juteProduction/winding_stock.py).
 * MT is never stored — the backend returns stock_mt computed as
 * no_of_samples * wt_per_sample / 1000.
 */

import { apiRoutesPortalMasters } from "@/utils/api";
import { fetchWithCookie } from "@/utils/apiClient2";

export interface WindingStockYarnItem {
  item_id: number;
  item_code: string | null;
  item_name: string | null;
  quality_name: string | null;
  yarn_count: number | null;
}

export interface WindingStockArea {
  wastage_area_id: number;
  area_name: string;
  dept_id: number | null;
  branch_id: number | null;
}

export interface WindingStockSetup {
  buckets: string[];
  yarn_items: WindingStockYarnItem[];
  areas: WindingStockArea[];
}

export interface WindingStockRow {
  winding_stock_id: number;
  co_id: number;
  branch_id: number;
  tran_date: string;
  bucket: string;
  item_id: number | null;
  item_code: string | null;
  item_name: string | null;
  quality_name: string | null;
  yarn_count: number | null;
  package_label: string;
  wastage_area_id: number | null;
  area_name: string | null;
  no_of_samples: number;
  wt_per_sample: number;
  stock_mt: number;
  remarks: string | null;
}

export interface WindingStockDay {
  rows: WindingStockRow[];
  bucket_totals: Record<string, number>;
  grand_total_mt: number;
  prev_entry_date: string | null;
  locked: boolean;
}

export interface WindingStockCreatePayload {
  co_id: number;
  branch_id: number;
  tran_date: string;
  bucket: string;
  item_id: number | null;
  package_label: string;
  wastage_area_id: number | null;
  no_of_samples: number;
  wt_per_sample: number;
  remarks: string | null;
}

export type WindingStockEditPayload = Partial<
  Omit<WindingStockCreatePayload, "co_id" | "branch_id" | "tran_date">
>;

export async function fetchWindingStockSetup(params: {
  coId: string | number;
  branchId: number;
}): Promise<WindingStockSetup> {
  const qs = new URLSearchParams();
  qs.set("co_id", String(params.coId));
  qs.set("branch_id", String(params.branchId));
  const url = `${apiRoutesPortalMasters.WINDING_STOCK_SETUP}?${qs.toString()}`;
  const res = await fetchWithCookie<{ data: WindingStockSetup }>(url);
  if (res.error || !res.data) throw new Error(res.error ?? "Failed to load setup");
  return res.data.data;
}

export async function fetchWindingStockDay(params: {
  coId: string | number;
  branchId: number;
  tranDate: string;
}): Promise<WindingStockDay> {
  const qs = new URLSearchParams();
  qs.set("co_id", String(params.coId));
  qs.set("branch_id", String(params.branchId));
  qs.set("tran_date", params.tranDate);
  const url = `${apiRoutesPortalMasters.WINDING_STOCK_ROWS_BY_DATE}?${qs.toString()}`;
  const res = await fetchWithCookie<{ data: WindingStockDay }>(url);
  if (res.error || !res.data) throw new Error(res.error ?? "Failed to load stock rows");
  return res.data.data;
}

/**
 * The kg-per-piece last saved for this (item, package) key — the entire
 * weight-per-sample "master". Returns null for a key never counted before.
 */
export async function fetchWtDefault(params: {
  coId: string | number;
  branchId: number;
  itemId: number | null;
  packageLabel: string;
}): Promise<number | null> {
  const qs = new URLSearchParams();
  qs.set("co_id", String(params.coId));
  qs.set("branch_id", String(params.branchId));
  qs.set("package_label", params.packageLabel);
  if (params.itemId != null) qs.set("item_id", String(params.itemId));
  const url = `${apiRoutesPortalMasters.WINDING_STOCK_WT_DEFAULT}?${qs.toString()}`;
  const res = await fetchWithCookie<{ data: { wt_per_sample: number | null } }>(url);
  if (res.error || !res.data) return null;
  return res.data.data.wt_per_sample;
}

export async function createWindingStockRow(payload: WindingStockCreatePayload) {
  const res = await fetchWithCookie<{ data: { winding_stock_id: number } }>(
    apiRoutesPortalMasters.WINDING_STOCK_ROW_CREATE,
    "POST",
    payload,
  );
  return { ok: !res.error, error: res.error, id: res.data?.data?.winding_stock_id ?? null };
}

export async function editWindingStockRow(
  id: number,
  coId: number,
  payload: WindingStockEditPayload,
) {
  const url = `${apiRoutesPortalMasters.WINDING_STOCK_ROW_EDIT}/${id}?co_id=${coId}`;
  const res = await fetchWithCookie(url, "PUT", payload);
  return { ok: !res.error, error: res.error, id };
}

export async function deleteWindingStockRow(id: number, coId: number) {
  const url = `${apiRoutesPortalMasters.WINDING_STOCK_ROW_DELETE}/${id}?co_id=${coId}`;
  const res = await fetchWithCookie(url, "DELETE");
  return { ok: !res.error, error: res.error, id };
}

export async function copyPreviousDay(payload: {
  co_id: number;
  branch_id: number;
  tran_date: string;
}) {
  const res = await fetchWithCookie<{ data: { copied: number; from_date: string } }>(
    apiRoutesPortalMasters.WINDING_STOCK_COPY_PREVIOUS_DAY,
    "POST",
    payload,
  );
  return {
    ok: !res.error,
    error: res.error,
    copied: res.data?.data?.copied ?? 0,
    fromDate: res.data?.data?.from_date ?? null,
  };
}

export async function setWindingStockLock(
  payload: { co_id: number; branch_id: number; tran_date: string },
  locked: boolean,
) {
  const url = locked
    ? apiRoutesPortalMasters.WINDING_STOCK_LOCK
    : apiRoutesPortalMasters.WINDING_STOCK_UNLOCK;
  const res = await fetchWithCookie<{ data: { locked: boolean } }>(url, "POST", payload);
  return { ok: !res.error, error: res.error, locked: res.data?.data?.locked ?? locked };
}
```

- [ ] **Step 3: Add the report fetcher**

Append to `src/utils/juteProductionReportService.ts`:

```ts
export interface WwdReconciliation {
  opening_mt: number;
  winding_mt: number;
  weaving_mt: number;
  purchase_mt: number;
  sale_mt: number;
  should_be_closing_mt: number;
  actual_closing_mt: number;
  difference_mt: number;
  /** null when winding production is zero — a percentage of nothing. */
  difference_pct: number | null;
  jute_issue_mt: number;
}

export interface WwdStockRow {
  bucket: string;
  item_code: string | null;
  item_name: string | null;
  quality_name: string | null;
  yarn_count: number | null;
  package_label: string;
  area_name: string | null;
  no_of_samples: number;
  wt_per_sample: number;
  prvs_stock_mt: number;
  stock_mt: number;
}

export interface WindingWeavingDifference {
  report_date: string;
  date_from: string;
  stock: Record<string, { rows: WwdStockRow[]; subtotal_mt: number } | number> & {
    grand_total_mt: number;
  };
  production: { day: WwdReconciliation; mtd: WwdReconciliation };
  cloth_split: {
    day: Record<string, number>;
    mtd: Record<string, number>;
  };
  chain: {
    ok: boolean;
    prev_entry_date: string | null;
    month_opening_date: string | null;
  };
  warnings: { purchase_unknown_uoms: string[] };
  note: string;
}

/** Counted winding stock reconciled against book stock, day and month-to-date. */
export async function fetchWindingWeavingDifference(params: {
  coId: number;
  branchId: number;
  reportDate: string;
}): Promise<WindingWeavingDifference> {
  const qs = new URLSearchParams();
  qs.set("co_id", String(params.coId));
  qs.set("branch_id", String(params.branchId));
  qs.set("report_date", params.reportDate);
  const url = `${apiRoutesPortalMasters.WINDING_WEAVING_DIFFERENCE}?${qs.toString()}`;
  const res = await fetchWithCookie<{ data: WindingWeavingDifference }>(url);
  if (res.error || !res.data) {
    throw new Error(res.error ?? "Failed to fetch the winding & weaving difference report");
  }
  return res.data.data;
}
```

- [ ] **Step 4: Type-check**

Run: `cd ../vowerp3ui && npx tsc --noEmit`
Expected: no errors from the new files.

- [ ] **Step 5: Commit (frontend repo)**

```bash
cd ../vowerp3ui
git add src/utils/api.ts src/utils/windingStockService.ts src/utils/juteProductionReportService.ts
git commit -m "feat(winding-stock): api routes and services for stock entry and the difference report"
```

---

## Task 8: Frontend entry page

**Files:**
- Create: `../vowerp3ui/src/app/dashboardportal/juteProduction/windingStock/page.tsx`
- Create: `../vowerp3ui/src/app/dashboardportal/juteProduction/windingStock/_components/WindingStockForm.tsx`
- Create: `../vowerp3ui/src/app/dashboardportal/juteProduction/windingStock/_components/WindingStockGrid.tsx`
- Create: `../vowerp3ui/src/app/dashboardportal/juteProduction/windingStock/_components/StockTotalsFooter.tsx`
- Create: `../vowerp3ui/src/app/dashboardportal/juteProduction/windingStock/utils/stockCalc.ts`
- Create: `../vowerp3ui/src/app/dashboardportal/juteProduction/windingStock/utils/stockSchema.ts`
- Create: `../vowerp3ui/src/app/dashboardportal/juteProduction/windingStock/utils/stockCalc.test.ts`

**Interfaces:**
- Consumes: `windingStockService` (Task 7).
- Produces: the page at `/dashboardportal/juteProduction/windingStock` that the Task 6 menu row points at.

**Pattern to follow — read `juteProduction/wasteEntry/` first.** It is the same shape: `page.tsx` owns every piece of state and all API calls; `_components/` render from props only. Copy these specifics exactly:

- **Hydration gate.** `const [mounted, setMounted] = React.useState(false)` + `React.useEffect(() => setMounted(true), [])` + `if (!mounted) return null;`. The company/branch come from localStorage-backed context, so without the gate the server HTML and the client disagree.
- **Branch resolution.** One sidebar branch auto-selects; several render an Autocomplete; zero shows a warning Alert. Copy the `sidebarBranchIds` / `pageBranchId` block from `wasteEntry/page.tsx` verbatim.
- **`coId` is a string** (`useSelectedCompanyCoId`), `branchId` is `number | null`.
- **No dialog.** Edit is inline: the grid calls `onEdit(row)` → `setEditingEntry(row)`, and a persistent form above the grid lifts that row. A `formResetToken` counter clears the form after each save.
- **Data hook shape.** Version-counter + `cancelled` flag, exactly as in `hooks/useWasteEntriesByDate.ts`.
- **Zod fields are all `z.string()`**, converted to numbers only when building the payload.
- **`PickerField`** — copy the component out of `WasteEntryForm.tsx`; its `renderOption` sets an explicit `key={o.id}` because tenants carry duplicate display names.

- [ ] **Step 1: Write the failing calc test**

Create `utils/stockCalc.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { stockMt, bucketTotals, grandTotal, roundMt } from "./stockCalc";

const row = (bucket: string, samples: number, wt: number) =>
  ({ bucket, no_of_samples: samples, wt_per_sample: wt }) as never;

describe("stockMt", () => {
  it("matches the mill sheet's first row: 185 x 14 kg = 2.590 MT", () => {
    expect(stockMt(185, 14)).toBe(2.59);
  });

  it("treats missing numbers as zero rather than NaN", () => {
    expect(stockMt(null, 14)).toBe(0);
    expect(stockMt(185, null)).toBe(0);
  });
});

describe("bucketTotals / grandTotal", () => {
  const rows = [
    row("MILL_USE", 185, 14),
    row("MILL_USE", 20116, 1),
    row("SALE_YARN", 230, 50),
    row("WASTE", 60, 1),
  ];

  it("sums per bucket", () => {
    const totals = bucketTotals(rows);
    expect(totals.MILL_USE).toBe(22.706);
    expect(totals.SALE_YARN).toBe(11.5);
    expect(totals.WASTE).toBe(0.06);
  });

  it("grand total equals the sum of the buckets", () => {
    expect(grandTotal(rows)).toBe(34.266);
  });
});

describe("roundMt", () => {
  it("does not leak float noise into the printed total", () => {
    expect(roundMt(0.1 + 0.2)).toBe(0.3);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ../vowerp3ui && npx vitest run src/app/dashboardportal/juteProduction/windingStock/utils/stockCalc.test.ts`
Expected: FAIL — cannot resolve `./stockCalc`.

- [ ] **Step 3: Write `utils/stockCalc.ts`**

```ts
import type { WindingStockRow } from "@/utils/windingStockService";

/** The column scale the mill's sheet prints MT at. */
export const MT_SCALE = 3;

/** Round to the printed scale so float addition never leaks 0.30000000000000004. */
export function roundMt(value: number): number {
  const factor = 10 ** MT_SCALE;
  return Math.round(value * factor) / factor;
}

/**
 * MT for one counted line. Mirrors the server formula exactly
 * (no_of_samples * wt_per_sample / 1000) so the grid and the report agree.
 */
export function stockMt(
  samples: number | null | undefined,
  wt: number | null | undefined,
): number {
  const s = Number(samples);
  const w = Number(wt);
  if (!Number.isFinite(s) || !Number.isFinite(w)) return 0;
  return roundMt((s * w) / 1000);
}

export function bucketTotals(rows: ReadonlyArray<WindingStockRow>): Record<string, number> {
  const totals: Record<string, number> = {};
  rows.forEach((r) => {
    const mt = stockMt(r.no_of_samples, r.wt_per_sample);
    totals[r.bucket] = roundMt((totals[r.bucket] ?? 0) + mt);
  });
  return totals;
}

export function grandTotal(rows: ReadonlyArray<WindingStockRow>): number {
  return roundMt(
    Object.values(bucketTotals(rows)).reduce((sum, mt) => sum + mt, 0),
  );
}

export function formatMt(value: number | null | undefined): string {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(MT_SCALE) : "";
}

/** Local-time-safe today, matching wasteEntry's todayISO. */
export function todayISO(): string {
  const d = new Date();
  const tzOffset = d.getTimezoneOffset() * 60000;
  return new Date(d.getTime() - tzOffset).toISOString().slice(0, 10);
}

export const BUCKET_LABELS: Record<string, string> = {
  MILL_USE: "Yarn for Mill Use",
  PURCHASE: "Outside Purchase Yarn",
  SALE_YARN: "Sale Yarn Stock",
  BALL: "Ball Stock",
  WASTE: "Waste",
};
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ../vowerp3ui && npx vitest run src/app/dashboardportal/juteProduction/windingStock/utils/stockCalc.test.ts`
Expected: PASS, 5 tests.

- [ ] **Step 5: Write `utils/stockSchema.ts`**

```ts
import { z } from "zod";
import type {
  WindingStockCreatePayload,
  WindingStockEditPayload,
} from "@/utils/windingStockService";

export const MAX_SAMPLES = 1_000_000;
export const MAX_WT = 2000;

function isFutureISODate(value: string): boolean {
  return value > new Date().toISOString().slice(0, 10);
}

export const windingStockSchema = z.object({
  tran_date: z
    .string()
    .min(1, "Date is required")
    .refine((v) => !isFutureISODate(v), { message: "Date cannot be in the future" }),
  bucket: z.string().min(1, "Bucket is required"),
  // Blank is legitimate: BEAM ON FLOOR and the waste lines have no yarn master row.
  item_id: z.string(),
  package_label: z
    .string()
    .min(1, "Package / description is required")
    .max(60, "Cannot exceed 60 characters"),
  wastage_area_id: z.string(),
  no_of_samples: z
    .string()
    .min(1, "Number of samples is required")
    .refine((v) => Number.isFinite(Number(v)), { message: "Must be a number" })
    .refine((v) => Number(v) > 0, { message: "Must be greater than 0" })
    .refine((v) => Number(v) <= MAX_SAMPLES, { message: `Cannot exceed ${MAX_SAMPLES}` }),
  wt_per_sample: z
    .string()
    .min(1, "Weight per sample is required")
    .refine((v) => Number.isFinite(Number(v)), { message: "Must be a number" })
    .refine((v) => Number(v) > 0, { message: "Must be greater than 0" })
    .refine((v) => Number(v) <= MAX_WT, { message: `Cannot exceed ${MAX_WT} kg` }),
  remarks: z.string().max(255, "Remarks cannot exceed 255 characters"),
});

export type WindingStockFormValues = z.infer<typeof windingStockSchema>;

export function buildDefaultFormValues(tranDate: string): WindingStockFormValues {
  return {
    tran_date: tranDate,
    bucket: "MILL_USE",
    item_id: "",
    package_label: "",
    wastage_area_id: "",
    no_of_samples: "",
    wt_per_sample: "",
    remarks: "",
  };
}

export function buildCreatePayload(
  values: WindingStockFormValues,
  coId: number,
  branchId: number,
): WindingStockCreatePayload {
  return {
    co_id: coId,
    branch_id: branchId,
    tran_date: values.tran_date,
    bucket: values.bucket,
    item_id: values.item_id === "" ? null : Number(values.item_id),
    package_label: values.package_label.trim(),
    wastage_area_id: values.wastage_area_id === "" ? null : Number(values.wastage_area_id),
    no_of_samples: Number(values.no_of_samples),
    wt_per_sample: Number(values.wt_per_sample),
    remarks: values.remarks.trim() === "" ? null : values.remarks.trim(),
  };
}

export function buildEditPayload(values: WindingStockFormValues): WindingStockEditPayload {
  return {
    bucket: values.bucket,
    item_id: values.item_id === "" ? null : Number(values.item_id),
    package_label: values.package_label.trim(),
    wastage_area_id: values.wastage_area_id === "" ? null : Number(values.wastage_area_id),
    no_of_samples: Number(values.no_of_samples),
    wt_per_sample: Number(values.wt_per_sample),
    remarks: values.remarks.trim() === "" ? null : values.remarks.trim(),
  };
}
```

- [ ] **Step 6: Write the three components and the page**

Build `WindingStockForm.tsx`, `WindingStockGrid.tsx`, `StockTotalsFooter.tsx` and `page.tsx` following `wasteEntry` structurally. Requirements specific to this page:

1. **Weight prefill.** When bucket + item + package label are all set and `wt_per_sample` is still empty, call `fetchWtDefault` and fill it. Do not overwrite a value the operator has typed.
2. **Grid grouped by bucket.** Add a `bucket` column and set `initialState.rowGrouping` off, sorting instead by `bucket` then `package_label` — the backend already returns them in that order.
3. **Grid columns:** actions, Bucket, Count (lbs, from `yarn_count`), Quality (`quality_name`), Package (`package_label`), Place (`area_name`), Samples, Kg/Sample, **MT** (`stock_mt`, `formatMt`).
4. **Totals footer:** one chip per bucket using `BUCKET_LABELS`, plus the grand total in bold — the same shape as `WasteTotalsFooter`.
5. **Lock banner.** When `day.locked`, render a warning `Alert` reading *"This day is locked because its stock report has been taken. Editing needs Edit permission."* and pass `actionsDisabled` into the grid. Do **not** hide the form — a user with Edit is allowed through, and the backend is the authority; the UI only signals.
6. **Copy previous day.** A button, enabled only when the grid is empty, calling `copyPreviousDay` then `refresh`. On success show `Copied N lines from <date>`.
7. **Lock / Unlock button** calling `setWindingStockLock`, then `refresh`.

- [ ] **Step 7: Type-check and lint**

Run: `cd ../vowerp3ui && npx tsc --noEmit && npx next lint --dir src/app/dashboardportal/juteProduction/windingStock`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
cd ../vowerp3ui
git add src/app/dashboardportal/juteProduction/windingStock
git commit -m "feat(winding-stock): daily counted stock entry page"
```

---

## Task 9: Frontend report page

**Files:**
- Create: `../vowerp3ui/src/app/dashboardportal/juteProduction/report/windingWeavingDifference/page.tsx`

**Interfaces:**
- Consumes: `fetchWindingWeavingDifference` (Task 7).
- Produces: the page the Task 6 report menu row points at. No hub changes are needed — the menu row *is* the registration, and `report/layout.tsx` adds "Back to Reports" automatically.

**Pattern: follow `report/dailyProductionReport/page.tsx`, not `ReportPanel`.** `ReportPanel` gives branch + date + Excel for free but only renders a single flat DataGrid; this report is a sectioned one-pager like the DPR, so it hand-builds its tables and offers **Print**. Excel export for a sectioned report would need `buildReportSheet` per section — deliberately deferred, and recorded as such in the spec.

- [ ] **Step 1: Write the page**

Reuse the DPR's primitives — `SectionCard`, `DprTable`, `Tr`, `Td`, `NumTd`, `totalRowSx`, `fmt` from `../dailyProductionReport/_components/DprPieces`. Structure:

```tsx
"use client";

import React, { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Alert, Box, Button, CircularProgress, Stack, TextField, Typography, Autocomplete, GlobalStyles } from "@mui/material";
import { Printer } from "lucide-react";
import { useReportBranches } from "@/components/reports/useReportBranches";
import { yesterdayIso } from "@/components/reports/reportDates";
import type { BranchOption } from "@/components/reports/types";
import {
  fetchWindingWeavingDifference,
  type WindingWeavingDifference,
} from "@/utils/juteProductionReportService";
import {
  DprTable, NumTd, SectionCard, Td, Tr, fmt, totalRowSx,
} from "../dailyProductionReport/_components/DprPieces";

const BUCKET_LABELS: Record<string, string> = {
  MILL_USE: "Yarn for Mill Use",
  PURCHASE: "Outside Purchase Yarn",
  SALE_YARN: "Sale Yarn Stock",
  BALL: "Ball Stock",
  WASTE: "Waste",
};

function Content() {
  const { branches, coId, companyName, initialBranchId, dateParam } = useReportBranches();
  // Branch/date come from localStorage-backed context: hold the controls until
  // mount so the server HTML and the client agree.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const [branchId, setBranchId] = useState<number | null>(initialBranchId);
  const [reportDate, setReportDate] = useState<string>(dateParam || yesterdayIso());
  const [data, setData] = useState<WindingWeavingDifference | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fetchedOnce = useRef(false);
  const branchSeeded = useRef(false);
  const lastCoId = useRef<number | null>(null);

  // …the four effects and runFetch, copied verbatim from dailyProductionReport…
}

export default function WindingWeavingDifferencePage() {
  return (
    <Suspense
      fallback={
        <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
          <CircularProgress />
        </Box>
      }
    >
      <Content />
    </Suspense>
  );
}
```

Sections to render, in the printed sheet's order:

1. **Header strip** — report date, month-opening date, and the two difference percentages (TODAY = `production.day.difference_pct`, TODATE = `production.mtd.difference_pct`). Render `null` percentages as an em-dash, never `0.00%`.
2. **Chain warning** — when `!data.chain.ok`, a warning `Alert`: *"Opening stock is taken from `{chain.prev_entry_date}`, not the previous day. Counts are missing in between."* When `chain.prev_entry_date` is null, say no earlier count exists.
3. **Unknown-UOM warning** — when `warnings.purchase_unknown_uoms.length`, a warning `Alert` naming them: *"Purchased yarn booked in {list} was not converted — MTR is metres, not tonnes."*
4. **Production details** — a two-column table, TODAY vs TODATE, one row per member of `WwdReconciliation` in this order: Jute Issue (MT), Opening Stock, Winding Production, Weaving Production, Purchase Yarn, Sale Yarn, Should-Be Closing, Actual Closing, **Difference** (use `NumTd` with `tone="nonzero-bad"`).
5. **Cloth split** — Hessian / Sacking / Other, day and MTD.
6. **Five stock blocks** — one `SectionCard` per bucket in `["MILL_USE","PURCHASE","SALE_YARN","BALL","WASTE"]`, columns Count · Quality · Package · No. of Samples · Weight/Sample (kg) · Prvs Stock · Stock Position (MT) · Place, each ending in a subtotal row with `totalRowSx()`. Skip a bucket entirely when it has no rows.
7. **Stock grand total** — a final bold line, `stock.grand_total_mt`.
8. **Note** — render `data.note` as a caption at the foot.

Add the same print isolation `GlobalStyles` block as the DPR, with the class renamed to `wwd-print-root`.

- [ ] **Step 2: Type-check and lint**

Run: `cd ../vowerp3ui && npx tsc --noEmit && npx next lint --dir src/app/dashboardportal/juteProduction/report/windingWeavingDifference`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
cd ../vowerp3ui
git add src/app/dashboardportal/juteProduction/report/windingWeavingDifference
git commit -m "feat(winding-stock): winding & weaving difference report page"
```

---

## Task 10: dev3 browser QA

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything above, deployed against dev3.

**Read this before starting: dev3 has zero winding rows and zero yarn inward lines.** The entry page and its CRUD can be exercised fully; the report's *production* figures will legitimately be 0 there. Do not "fix" a zero that is really an empty table — verify the arithmetic on sls in Task 11 instead.

- [ ] **Step 1: Start both servers**

Backend on `:8000`, frontend on `:3000`, with a Chrome window open.

- [ ] **Step 2: Run the QA agent**

Use the `/qa-portal-page` skill with page paths `juteProduction/windingStock` and `juteProduction/report/windingWeavingDifference`, tenant `dev3`. Credentials come from `.claude/test-credentials.md` — never hardcode them.

- [ ] **Step 3: Confirm the valid path**

Add at least four lines spanning three different buckets, including one with **no yarn item** (a waste line). Confirm: MT per row equals samples × kg ÷ 1000; bucket subtotals and the grand total match; the row persists in `dev3.jute_prod_winding_stock` with the expected `bucket`, `item_id` (NULL where omitted) and `package_label`.

- [ ] **Step 4: Confirm weight prefill**

Add a second line with the same item and package label as an earlier one. The kg/sample field must prefill with the earlier value. Change it, save, and confirm a third line prefills with the *new* value — this is the whole weight-per-sample master.

- [ ] **Step 5: Probe the error paths**

Each of these must return a clean 400 with a readable message, never a 500:
a future date; an unknown bucket; a blank package label; `no_of_samples` of 0 and of −5; `wt_per_sample` of 0 and of 99999; an exact duplicate line; an `item_id` from another company; a `wastage_area_id` from another branch. Then edit a row with `co_id` belonging to a different company and confirm a **404**, not a silent cross-company edit.

- [ ] **Step 6: Confirm the lock**

Lock the day. As a Write-only role, confirm create/edit/delete return **403** with the lock message. As an Edit role, confirm the same actions succeed. Unlock and confirm normal editing resumes.

- [ ] **Step 7: Confirm copy-previous-day**

On an empty date with an earlier counted day, copy it and confirm the line count matches. Run it again on the now-populated date and confirm a **400**, not a doubled count.

- [ ] **Step 8: Confirm the report renders**

Open the report from the Production Reports dropdown. Confirm the five stock blocks, the subtotals and the grand total match the entry page for the same date, the chain warning appears when a day is skipped, and Print produces a clean one-pager.

- [ ] **Step 9: Fix anything found, with a regression test first**

Every bug gets a failing test before its fix, per the repo's testing rules.

- [ ] **Step 10: Commit any fixes**

```bash
git add -A
git commit -m "fix(winding-stock): address dev3 QA findings"
```

---

## Task 11: sls promotion and verification

**Files:** none in code (migrations only).

**Interfaces:**
- Consumes: the two migrations from Tasks 1 and 6.

**This is where the report's arithmetic actually gets proven** — sls has real winding, weaving and jute-issue data; dev3 does not.

- [ ] **Step 1: Apply both migrations to sls**

Use the `run-migration` skill, target **sls**: `create_jute_prod_winding_stock.sql`, then `20260819_winding_stock_menus.sql`.

- [ ] **Step 2: Verify the tables and menus landed**

`SHOW CREATE TABLE jute_prod_winding_stock` and the VERIFICATION queries from the menu migration.

- [ ] **Step 3: Verify the production figures against known data**

For branch 29 on a date with real weaving (e.g. 2026-01-20), call the report endpoint and check, with a direct pymysql query for each:
- winding MT equals the reconciliation sum ÷ 1000 for that branch and date;
- weaving MT equals the day slice's `production_mt` sum;
- jute issue MT equals the `_issue_cte` quintal sum ÷ 10;
- purchase yarn is **0.000** (expected — the yarn is booked against a misc-expenses item);
- sale yarn is **0.000** (expected — sls has no yarn invoices);
- cloth split is **100% OTHER** (expected — every weaving quality points at `JCLOTH-MIG`).

The last three zeroes are correct answers, not bugs. Record them in the verification note so the next person does not chase them.

- [ ] **Step 4: Enter one real counted day and reconcile**

With the mill's own sheet for a date, key the counted lines and confirm the report's grand total matches the sheet's STOCK GRAND TOTAL, and that the difference and percentage are arithmetically consistent with the winding figure shown.

- [ ] **Step 5: Record the outcome**

Append a note to the spec's risk table stating which figures were verified on sls, on what date, and which remain zero pending the data fixes (yarn item reclassification, cloth item split).

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-08-19-winding-stock-difference-design.md
git commit -m "docs(winding-stock): record sls verification outcome"
```

---

## Post-plan spec corrections

These four deviations were discovered during recon and are already reflected above. Fold them into the spec before or during Task 1 so the two documents do not disagree:

1. **Lock semantics.** The spec says create/edit/delete are "refused while locked". The module's actual convention (`weaving_lock.py`) is `require_edit_if_locked`: a locked day needs Edit (`access_type_id >= 4`), Write-only gets 403. Strictly better — a supervisor can still correct a miscount — and it is the house pattern.
2. **Sale yarn matching.** The spec matches yarn item type **or** yarn invoice type. `INVOICE_TYPE_IDS["JUTE_YARN"]` is also 4, a numeric coincidence with `item_grp_mst.item_type_id`. Line-level item type alone is both simpler and more correct; OR-ing the invoice type would pull non-yarn lines riding a yarn invoice.
3. **Purchase yarn is 0 on sls, by data not by code.** Purchased yarn is booked against item 334018 in a misc-expenses group. The query stays honest and starts working when that item is reclassified. Also: `approved_qty` carries no unit guarantee, `MTR` is metres and `MT`/`MTS` are tonnes, so unrecognised units are reported rather than converted.
4. **Report export is Print, not Excel.** The hub's Excel export comes from `ReportPanel`, which only renders flat grids. A sectioned report hand-builds its tables like the DPR and offers Print. Excel per section is deferred.
