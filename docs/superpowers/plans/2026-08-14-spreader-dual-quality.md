# Spreader Dual-Quality Rolls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a spreader roll carry up to two jute qualities with an integer ratio (e.g. JTD5/ATD5 2:1), displayed everywhere as `NAME1/NAME2 (r1:r2)`, with per-component weight fields, on tenants dev3 + sls.

**Architecture:** Three nullable columns on `spreader_prod_entry` (`item_id_2`, `ratio_1`, `ratio_2`; NULL `item_id_2` = pure roll). Group lock widens from item to canonical blend tuple. All read queries emit a server-built `quality_label`; FE swaps item columns to it. No change to `spreader_roll_issue` (quality still derived via `entry_id_grp`).

**Tech Stack:** FastAPI + pydantic v2 (`model_validator` — see `src/juteProduction/winding_entry.py:153` precedent), raw SQL via `sqlalchemy.text()`, MySQL 8 (no `ADD COLUMN IF NOT EXISTS` — use information_schema-guarded prepared statements), pytest + TestClient + MagicMock session, Next.js/MUI frontend at `../vowerp3ui`.

**Spec:** `docs/superpowers/specs/2026-08-14-spreader-dual-quality-design.md`

## Global Constraints

- Handlers are `def`, never `async def` (repo sync-handler policy).
- All responses wrapped: `{"data": ...}`.
- SQL bind params: `None` for NULL, exact name match, type-cast values.
- Canonical blend order: dominant ratio first; on ratio tie, lower item_id first. `ratio_1=1` default for pure rolls; `item_id_2`/`ratio_2` NULL for pure.
- Blend label format exactly: `{name1}/{name2} ({r1}:{r2})`; pure label = `{name1}`.
- Old payloads (mobile app) with no new fields MUST keep creating pure rolls unchanged.
- Migration runner splits scripts on `;` and chokes on semicolons inside comments — NO semicolons in any SQL comment, rollback comment written without semicolons.
- Backend branch: `spreader-dual-quality` (already created from origin/main, spec committed). Frontend repo trunk is `dev` — create FE branch `spreader-dual-quality` from `dev`.
- BE tests run from repo root: `source .venv/Scripts/activate` then `pytest src/test/... -v` (or `.venv/Scripts/python.exe -m pytest`).

---

### Task 1: Blend helpers (pure functions) — `src/juteProduction/services/blend.py`

**Files:**
- Create: `src/juteProduction/services/blend.py`
- Test: `src/test/test_spreader_blend.py`

**Interfaces:**
- Produces: `canonical_blend(item_id, item_id_2, ratio_1, ratio_2) -> tuple[int, int|None, int, int|None]`
- Produces: `blend_label(item_name, item_name_2, ratio_1, ratio_2) -> str`
- Produces: `blend_label_from_db(db, item_id, item_id_2, ratio_1, ratio_2) -> str` (looks up names from `item_mst`)
- Produces: `target_maturity_hours(maturity_map: dict[int,int], item_id, item_id_2, ratio_1, ratio_2, default: int) -> int` (ratio-weighted avg, per-spec fallbacks)

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for spreader blend helpers (dual-quality rolls)."""

from src.juteProduction.services.blend import (
    blend_label,
    canonical_blend,
    target_maturity_hours,
)


class TestCanonicalBlend:
    def test_pure_roll_passthrough(self):
        assert canonical_blend(10, None, 1, None) == (10, None, 1, None)

    def test_pure_roll_defaults_ratio_to_1(self):
        assert canonical_blend(10, None, None, None) == (10, None, 1, None)

    def test_dominant_first_kept(self):
        assert canonical_blend(10, 20, 2, 1) == (10, 20, 2, 1)

    def test_dominant_first_swapped(self):
        # ATD5/JTD5 (1:2) becomes JTD5/ATD5 (2:1)
        assert canonical_blend(10, 20, 1, 2) == (20, 10, 2, 1)

    def test_ratio_tie_lower_item_first(self):
        assert canonical_blend(20, 10, 1, 1) == (10, 20, 1, 1)

    def test_ratio_tie_already_ordered(self):
        assert canonical_blend(10, 20, 3, 3) == (10, 20, 3, 3)


class TestBlendLabel:
    def test_pure(self):
        assert blend_label("JTD5", None, 1, None) == "JTD5"

    def test_blend(self):
        assert blend_label("JTD5", "ATD5", 2, 1) == "JTD5/ATD5 (2:1)"


class TestTargetMaturity:
    def test_pure_uses_item_hours(self):
        assert target_maturity_hours({10: 24}, 10, None, 1, None, 48) == 24

    def test_pure_missing_uses_default(self):
        assert target_maturity_hours({}, 10, None, 1, None, 48) == 48

    def test_blend_weighted_average(self):
        # (24*2 + 36*1) / 3 = 28
        assert target_maturity_hours({10: 24, 20: 36}, 10, 20, 2, 1, 48) == 28

    def test_blend_one_missing_uses_other(self):
        assert target_maturity_hours({10: 24}, 10, 20, 2, 1, 48) == 24
        assert target_maturity_hours({20: 36}, 10, 20, 2, 1, 48) == 36

    def test_blend_both_missing_uses_default(self):
        assert target_maturity_hours({}, 10, 20, 2, 1, 48) == 48
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/test/test_spreader_blend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.juteProduction.services.blend'`

- [ ] **Step 3: Write the implementation**

```python
"""Blend (dual-quality) helpers for spreader rolls.

A roll may carry one or two jute qualities. Canonical storage order is
dominant ratio first; on a ratio tie, the lower item_id comes first, so
JTD5/ATD5 (2:1) and ATD5/JTD5 (1:2) are the same stored blend.
"""

from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session


def canonical_blend(
    item_id: int,
    item_id_2: Optional[int],
    ratio_1: Optional[int],
    ratio_2: Optional[int],
) -> Tuple[int, Optional[int], int, Optional[int]]:
    if item_id_2 is None:
        return int(item_id), None, int(ratio_1 or 1), None
    i1, i2 = int(item_id), int(item_id_2)
    r1, r2 = int(ratio_1 or 1), int(ratio_2)
    if r2 > r1 or (r2 == r1 and i2 < i1):
        return i2, i1, r2, r1
    return i1, i2, r1, r2


def blend_label(
    item_name: Optional[str],
    item_name_2: Optional[str],
    ratio_1: Optional[int],
    ratio_2: Optional[int],
) -> str:
    if not item_name_2:
        return item_name or ""
    return f"{item_name}/{item_name_2} ({int(ratio_1)}:{int(ratio_2)})"


def blend_label_from_db(
    db: Session,
    item_id: int,
    item_id_2: Optional[int],
    ratio_1: Optional[int],
    ratio_2: Optional[int],
) -> str:
    ids = [int(item_id)] + ([int(item_id_2)] if item_id_2 is not None else [])
    rows = db.execute(
        text("SELECT item_id, item_name FROM item_mst WHERE item_id IN :ids").bindparams(
            ids=tuple(ids)
        )
    ).fetchall()
    names = {int(r[0]): r[1] for r in rows}
    return blend_label(
        names.get(int(item_id), f"#{item_id}"),
        names.get(int(item_id_2), f"#{item_id_2}") if item_id_2 is not None else None,
        ratio_1,
        ratio_2,
    )


def target_maturity_hours(
    maturity_map: dict,
    item_id: int,
    item_id_2: Optional[int],
    ratio_1: Optional[int],
    ratio_2: Optional[int],
    default: int,
) -> int:
    m1 = maturity_map.get(int(item_id))
    if item_id_2 is None:
        return int(m1) if m1 is not None else int(default)
    m2 = maturity_map.get(int(item_id_2))
    if m1 is None and m2 is None:
        return int(default)
    if m1 is None:
        return int(m2)
    if m2 is None:
        return int(m1)
    r1, r2 = int(ratio_1 or 1), int(ratio_2 or 1)
    return round((int(m1) * r1 + int(m2) * r2) / (r1 + r2))
```

Note on `blend_label_from_db`: `text(...).bindparams(ids=tuple(ids))` handles the IN clause with the expanding tuple; if the repo's SQLAlchemy version complains, use `text("... WHERE item_id IN :ids").bindparams(bindparam("ids", expanding=True))` from `sqlalchemy.bindparam`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest src/test/test_spreader_blend.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/juteProduction/services/blend.py src/test/test_spreader_blend.py
git commit -m "feat: blend helpers for spreader dual-quality rolls"
```

---

### Task 2: Migration SQL + ORM columns, applied to dev3 + sls

**Files:**
- Create: `dbqueries/migrations/add_spreader_dual_quality.sql`
- Modify: `src/juteProduction/models.py:79` (SpreaderProdEntry, after `item_id`)

**Interfaces:**
- Produces: columns `spreader_prod_entry.item_id_2 INT NULL`, `ratio_1 INT NOT NULL DEFAULT 1`, `ratio_2 INT NULL`, index `idx_spe_item2`; sls also gains `spreader_machine_attr.band_edge_1..5` (dev3-drift replay).

- [ ] **Step 1: Write the migration script**

Every DDL is information_schema-guarded (MySQL 8 has no `ADD COLUMN IF NOT EXISTS`), so the script is rerunnable on both tenants. NO semicolons inside comments (runner limitation).

```sql
-- Spreader dual-quality rolls: up to 2 qualities + integer ratio per entry
-- Also replays dev3 band_edge drift onto sls (spreader_machine_attr)
-- Rollback (run manually, statements separated by newlines):
--   ALTER TABLE spreader_prod_entry DROP KEY idx_spe_item2
--   ALTER TABLE spreader_prod_entry DROP COLUMN ratio_2
--   ALTER TABLE spreader_prod_entry DROP COLUMN ratio_1
--   ALTER TABLE spreader_prod_entry DROP COLUMN item_id_2

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_prod_entry ADD COLUMN item_id_2 INT NULL AFTER item_id',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_prod_entry' AND COLUMN_NAME = 'item_id_2');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_prod_entry ADD COLUMN ratio_1 INT NOT NULL DEFAULT 1 AFTER item_id_2',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_prod_entry' AND COLUMN_NAME = 'ratio_1');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_prod_entry ADD COLUMN ratio_2 INT NULL AFTER ratio_1',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_prod_entry' AND COLUMN_NAME = 'ratio_2');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_prod_entry ADD KEY idx_spe_item2 (item_id_2)',
  'SELECT 1')
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_prod_entry' AND INDEX_NAME = 'idx_spe_item2');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_machine_attr ADD COLUMN band_edge_1 DECIMAL(10,3) NULL, ADD COLUMN band_edge_2 DECIMAL(10,3) NULL, ADD COLUMN band_edge_3 DECIMAL(10,3) NULL, ADD COLUMN band_edge_4 DECIMAL(10,3) NULL, ADD COLUMN band_edge_5 DECIMAL(10,3) NULL',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_machine_attr' AND COLUMN_NAME = 'band_edge_1');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;
```

- [ ] **Step 2: Update the ORM model**

In `src/juteProduction/models.py`, `SpreaderProdEntry`, after `item_id = Column(...)` (line 79):

```python
    item_id = Column(Integer, nullable=False, index=True)
    item_id_2 = Column(Integer, nullable=True, index=True)
    ratio_1 = Column(Integer, nullable=False, default=1, server_default="1")
    ratio_2 = Column(Integer, nullable=True)
```

(This file uses legacy `Column` style — match it, do not convert to `mapped_column`.)

- [ ] **Step 3: Apply to dev3, then sls**

Use the `run-migration` skill (pymysql via project venv, creds from `env/database.env`) — target **dev3** first, verify, then **sls** (user pre-approved both in this session's design phase).
Verify on each tenant:

```sql
SHOW COLUMNS FROM spreader_prod_entry LIKE 'item_id_2';
SHOW COLUMNS FROM spreader_prod_entry LIKE 'ratio_1';
SHOW COLUMNS FROM spreader_machine_attr LIKE 'band_edge_1';
SELECT COUNT(*) FROM spreader_prod_entry WHERE item_id_2 IS NOT NULL;  -- expect 0
```

Expected: columns exist on both tenants, 0 blend rows, existing row counts unchanged (dev3 8, sls 26 active-era rows — count may drift with live use on sls).

- [ ] **Step 4: Run existing spreader tests (regression)**

Run: `pytest src/test/test_spreader_branch_filter.py -v`
Expected: all PASS (schema change alone breaks nothing).

- [ ] **Step 5: Commit**

```bash
git add dbqueries/migrations/add_spreader_dual_quality.sql src/juteProduction/models.py
git commit -m "feat: spreader_prod_entry dual-quality columns + sls band_edge drift replay"
```

---

### Task 3: Group lock on blend tuple — `spreader_rules.py`

**Files:**
- Modify: `src/juteProduction/services/spreader_rules.py:27-62` (`_active_group_for_bin`), `:149-170` (`get_bin_state`), `:173-201` (`assign_or_reuse_entry_id_grp`)
- Test: `src/test/test_spreader_blend_lock.py`

**Interfaces:**
- Consumes: `canonical_blend`, `blend_label_from_db` from Task 1.
- Produces: `_active_group_for_bin(db, co_id, bin_id) -> tuple[int|None, tuple[int,int|None,int,int|None]|None]` — (grp, blend) or (None, None).
- Produces: `assign_or_reuse_entry_id_grp(db, co_id, bin_id, blend, candidate_dt) -> tuple[int, bool]` where `blend` is the canonical 4-tuple. **Signature change** — callers updated in Task 4.
- Produces: `get_bin_state(...)` dict gains `locked_item_id_2`, `locked_ratio_1`, `locked_ratio_2`, `locked_quality_label` (keeps `locked_item_id`).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for blend-aware group lock in spreader_rules."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.juteProduction.services.spreader_rules import (
    _active_group_for_bin,
    assign_or_reuse_entry_id_grp,
    get_bin_state,
)


def _row(*vals):
    return vals


class TestActiveGroupForBin:
    def test_no_open_group(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        grp, blend = _active_group_for_bin(db, 1, 2)
        assert grp is None and blend is None

    def test_pure_group(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(6, 10, None, 1, None)
        grp, blend = _active_group_for_bin(db, 1, 2)
        assert grp == 6
        assert blend == (10, None, 1, None)

    def test_blend_group(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(7, 10, 20, 2, 1)
        grp, blend = _active_group_for_bin(db, 1, 2)
        assert blend == (10, 20, 2, 1)


class TestAssignOrReuse:
    @patch("src.juteProduction.services.spreader_rules._next_entry_id_grp", return_value=9)
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(None, None))
    def test_empty_bin_new_group(self, mock_active, mock_next):
        grp, reused = assign_or_reuse_entry_id_grp(
            MagicMock(), 1, 2, (10, None, 1, None), datetime(2026, 8, 14, 8)
        )
        assert (grp, reused) == (9, False)

    @patch("src.juteProduction.services.spreader_rules.evaluate_4hr_window")
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(6, (10, 20, 2, 1)))
    def test_same_blend_reuses_group(self, mock_active, mock_win):
        mock_win.return_value = MagicMock(allowed=True)
        grp, reused = assign_or_reuse_entry_id_grp(
            MagicMock(), 1, 2, (10, 20, 2, 1), datetime(2026, 8, 14, 8)
        )
        assert (grp, reused) == (6, True)

    @patch("src.juteProduction.services.spreader_rules.blend_label_from_db", return_value="JTD5/ATD5 (2:1)")
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(6, (10, 20, 2, 1)))
    def test_different_blend_rejected_with_label(self, mock_active, mock_lbl):
        with pytest.raises(ValueError) as ei:
            assign_or_reuse_entry_id_grp(
                MagicMock(), 1, 2, (10, None, 1, None), datetime(2026, 8, 14, 8)
            )
        assert "JTD5/ATD5 (2:1)" in str(ei.value)

    @patch("src.juteProduction.services.spreader_rules.blend_label_from_db", return_value="X")
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(6, (10, 20, 2, 1)))
    def test_same_items_different_ratio_rejected(self, mock_active, mock_lbl):
        with pytest.raises(ValueError):
            assign_or_reuse_entry_id_grp(
                MagicMock(), 1, 2, (10, 20, 3, 1), datetime(2026, 8, 14, 8)
            )


class TestGetBinState:
    @patch("src.juteProduction.services.spreader_rules.blend_label_from_db", return_value="JTD5/ATD5 (2:1)")
    @patch("src.juteProduction.services.spreader_rules.evaluate_4hr_window", return_value=None)
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(6, (10, 20, 2, 1)))
    def test_state_exposes_blend_fields(self, mock_active, mock_win, mock_lbl):
        state = get_bin_state(MagicMock(), 1, 2, datetime(2026, 8, 14, 8))
        assert state["locked_item_id"] == 10
        assert state["locked_item_id_2"] == 20
        assert state["locked_ratio_1"] == 2
        assert state["locked_ratio_2"] == 1
        assert state["locked_quality_label"] == "JTD5/ATD5 (2:1)"

    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(None, None))
    def test_empty_bin_state(self, mock_active):
        state = get_bin_state(MagicMock(), 1, 2, datetime(2026, 8, 14, 8))
        assert state["locked_item_id"] is None
        assert state["locked_quality_label"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/test/test_spreader_blend_lock.py -v`
Expected: FAIL (tuple signatures / new keys missing)

- [ ] **Step 3: Implement**

In `spreader_rules.py` add import:

```python
from src.juteProduction.services.blend import blend_label_from_db
```

Replace `_active_group_for_bin` (keep the CONCAT trick for legacy `item_id`; plain `MIN` is safe for the new columns because a group is single-blend by construction):

```python
def _active_group_for_bin(db: Session, co_id: int, bin_id: int):
    """Return (entry_id_grp, blend) for the latest in-stock group in the bin,
    or (None, None). blend = (item_id, item_id_2, ratio_1, ratio_2)."""
    sql = text(
        """
        SELECT z.entry_id_grp, z.item_id, z.item_id_2, z.ratio_1, z.ratio_2
        FROM (
            SELECT p.entry_id_grp,
                   CAST(SUBSTRING_INDEX(
                       MIN(CONCAT(p.entry_dt, '|', LPAD(p.item_id, 9, '0'))), '|', -1
                   ) AS UNSIGNED) AS item_id,
                   MIN(p.item_id_2) AS item_id_2,
                   MIN(p.ratio_1) AS ratio_1,
                   MIN(p.ratio_2) AS ratio_2
            FROM spreader_prod_entry p
            WHERE p.co_id = :co_id AND p.bin_id = :bin_id AND p.active = 1
            GROUP BY p.entry_id_grp
        ) z
        JOIN (
            SELECT p.entry_id_grp,
                   SUM(p.no_of_rolls) AS produced,
                   COALESCE((
                       SELECT SUM(i.no_of_rolls)
                       FROM spreader_roll_issue i
                       WHERE i.entry_id_grp = p.entry_id_grp AND i.active = 1
                   ), 0) AS issued
            FROM spreader_prod_entry p
            WHERE p.co_id = :co_id AND p.bin_id = :bin_id AND p.active = 1
            GROUP BY p.entry_id_grp
        ) s ON s.entry_id_grp = z.entry_id_grp
        WHERE (s.produced - s.issued) > 0
        ORDER BY z.entry_id_grp DESC
        LIMIT 1
        """
    )
    row = db.execute(sql, {"co_id": co_id, "bin_id": bin_id}).fetchone()
    if not row:
        return None, None
    blend = (
        int(row[1]),
        int(row[2]) if row[2] is not None else None,
        int(row[3]) if row[3] is not None else 1,
        int(row[4]) if row[4] is not None else None,
    )
    return int(row[0]), blend
```

Replace `assign_or_reuse_entry_id_grp`:

```python
def assign_or_reuse_entry_id_grp(
    db: Session,
    co_id: int,
    bin_id: int,
    blend: Tuple[int, Optional[int], int, Optional[int]],
    candidate_dt: datetime,
) -> Tuple[int, bool]:
    """Decide whether the new entry joins the bin's open group or starts a fresh one.

    `blend` is the canonical (item_id, item_id_2, ratio_1, ratio_2) tuple.
    Raises ValueError on blend-lock, no-backdate or 4-hr-window violations.
    Returns (entry_id_grp, reused).
    """
    grp, locked_blend = _active_group_for_bin(db, co_id, bin_id)
    if grp is None:
        return _next_entry_id_grp(db, co_id), False

    if tuple(blend) != tuple(locked_blend):
        locked_label = blend_label_from_db(db, *locked_blend)
        raise ValueError(
            f"This bin already has an open group with a different quality: {locked_label}. "
            "Finish issuing those rolls before producing a different quality into the same bin."
        )

    win = evaluate_4hr_window(db, grp, candidate_dt)
    if win is None:
        return _next_entry_id_grp(db, co_id), False
    if not win.allowed:
        raise ValueError(win.reason)
    return grp, True
```

In `get_bin_state`, replace the `grp, locked = ...` line and return dict:

```python
    grp, locked_blend = _active_group_for_bin(db, co_id, bin_id)
    ...
    locked_label = blend_label_from_db(db, *locked_blend) if locked_blend else None
    return {
        "active_grp": grp,
        "locked_item_id": locked_blend[0] if locked_blend else None,
        "locked_item_id_2": locked_blend[1] if locked_blend else None,
        "locked_ratio_1": locked_blend[2] if locked_blend else None,
        "locked_ratio_2": locked_blend[3] if locked_blend else None,
        "locked_quality_label": locked_label,
        "window": window_info,
    }
```

(The `window_info` block between stays exactly as today.)

- [ ] **Step 4: Run tests**

Run: `pytest src/test/test_spreader_blend_lock.py -v`
Expected: PASS. Note `test_spreader_branch_filter.py` may now fail at `entry_create` — that caller is fixed in Task 4; run only the new file here.

- [ ] **Step 5: Commit**

```bash
git add src/juteProduction/services/spreader_rules.py src/test/test_spreader_blend_lock.py
git commit -m "feat: spreader group lock on canonical blend tuple"
```

---

### Task 4: Entry create/bin_state endpoints accept the blend

**Files:**
- Modify: `src/juteProduction/spreader_entry.py:36-48` (`SpreaderEntryCreate`), `:174-244` (`entry_create`)
- Test: `src/test/test_spreader_dual_quality_entry.py`

**Interfaces:**
- Consumes: `canonical_blend` (Task 1), new `assign_or_reuse_entry_id_grp(db, co_id, bin_id, blend, candidate_dt)` (Task 3).
- Produces: `POST /api/spreaderProd/entry_create` accepts optional `item_id_2`, `ratio_1`, `ratio_2`; INSERT writes them canonicalized. Old payloads unchanged → pure roll.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for dual-quality spreader entry create."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db

client = TestClient(app)


def _exec_params(mock_session, call_index: int) -> dict:
    return mock_session.execute.call_args_list[call_index][0][1]


BASE = {
    "co_id": 1,
    "branch_id": 2,
    "entry_date": "2026-08-14",
    "entry_time": 8,
    "spell": "A1",
    "machine_id": 4,
    "item_id": 10,
    "bin_id": 2,
    "no_of_rolls": 3,
    "wt_per_roll": 65.0,
}


class TestDualQualityEntryCreate:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @patch("src.juteProduction.spreader_entry.assign_or_reuse_entry_id_grp")
    def test_old_payload_creates_pure_roll(self, mock_grp):
        mock_grp.return_value = (101, False)
        ins = MagicMock()
        ins.lastrowid = 9
        self._mock_session.execute.side_effect = [ins]
        resp = client.post("/api/spreaderProd/entry_create", json=BASE)
        assert resp.status_code == 200
        params = _exec_params(self._mock_session, 0)
        assert params["item_id"] == 10
        assert params["item_id_2"] is None
        assert params["ratio_1"] == 1
        assert params["ratio_2"] is None
        # blend passed to group assignment as canonical tuple
        assert mock_grp.call_args[0][3] == (10, None, 1, None)

    @patch("src.juteProduction.spreader_entry.assign_or_reuse_entry_id_grp")
    def test_blend_payload_canonicalized(self, mock_grp):
        mock_grp.return_value = (101, False)
        ins = MagicMock()
        ins.lastrowid = 9
        self._mock_session.execute.side_effect = [ins]
        body = {**BASE, "item_id": 20, "item_id_2": 10, "ratio_1": 1, "ratio_2": 2}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 200
        params = _exec_params(self._mock_session, 0)
        # dominant-first: (10, 20, 2, 1)
        assert params["item_id"] == 10
        assert params["item_id_2"] == 20
        assert params["ratio_1"] == 2
        assert params["ratio_2"] == 1

    def test_same_item_twice_rejected(self):
        body = {**BASE, "item_id_2": 10, "ratio_1": 2, "ratio_2": 1}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    def test_ratio_2_without_item_2_rejected(self):
        body = {**BASE, "ratio_2": 1}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    def test_item_2_without_ratio_2_rejected(self):
        body = {**BASE, "item_id_2": 20}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    def test_zero_ratio_rejected(self):
        body = {**BASE, "item_id_2": 20, "ratio_1": 0, "ratio_2": 1}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    @patch("src.juteProduction.spreader_entry.assign_or_reuse_entry_id_grp")
    def test_blend_lock_violation_maps_to_400(self, mock_grp):
        mock_grp.side_effect = ValueError(
            "This bin already has an open group with a different quality: JTD5/ATD5 (2:1)."
        )
        resp = client.post("/api/spreaderProd/entry_create", json=BASE)
        assert resp.status_code == 400
        assert "JTD5/ATD5 (2:1)" in resp.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/test/test_spreader_dual_quality_entry.py -v`
Expected: FAIL (unknown fields ignored / INSERT lacks new params / old signature)

- [ ] **Step 3: Implement**

`SpreaderEntryCreate` — add fields + validator (imports: `from pydantic import BaseModel, Field, model_validator`; `from src.juteProduction.services.blend import canonical_blend`):

```python
class SpreaderEntryCreate(BaseModel):
    co_id: int
    branch_id: Optional[int] = None
    entry_date: date
    entry_time: int = Field(ge=0, le=23)
    spell: str
    machine_id: int
    item_id: int
    item_id_2: Optional[int] = None
    ratio_1: int = Field(default=1, ge=1)
    ratio_2: Optional[int] = Field(default=None, ge=1)
    bin_id: int
    trolley_no: Optional[int] = None
    no_of_rolls: int = Field(gt=0)
    wt_per_roll: float = Field(gt=0)
    remarks: Optional[str] = None

    @model_validator(mode="after")
    def _validate_blend(self):
        if self.item_id_2 is not None:
            if int(self.item_id_2) == int(self.item_id):
                raise ValueError("Second quality must differ from the first quality")
            if self.ratio_2 is None:
                raise ValueError("ratio_2 is required when a second quality is selected")
        elif self.ratio_2 is not None:
            raise ValueError("ratio_2 given without a second quality")
        return self
```

`entry_create` — canonicalize, pass blend, extend INSERT:

```python
        candidate_dt = _combine(body.entry_date, body.entry_time)
        blend = canonical_blend(body.item_id, body.item_id_2, body.ratio_1, body.ratio_2)
        grp, _reused = assign_or_reuse_entry_id_grp(
            db, body.co_id, body.bin_id, blend, candidate_dt
        )
```

INSERT SQL column list gains `item_id_2, ratio_1, ratio_2` right after `item_id` (both column and VALUES lists), and the params dict gains:

```python
                "item_id": blend[0],
                "item_id_2": blend[1],
                "ratio_1": blend[2],
                "ratio_2": blend[3],
```

(replacing the old `"item_id": int(body.item_id),` line).

- [ ] **Step 4: Run tests — new file + regression**

Run: `pytest src/test/test_spreader_dual_quality_entry.py src/test/test_spreader_branch_filter.py src/test/test_spreader_blend_lock.py -v`
Expected: all PASS (branch-filter file's `entry_create` tests still pass — old payloads still work; its `mock_grp.return_value = (101, False)` patching is signature-agnostic).

- [ ] **Step 5: Commit**

```bash
git add src/juteProduction/spreader_entry.py src/test/test_spreader_dual_quality_entry.py
git commit -m "feat: entry_create accepts second quality + ratio (canonicalized)"
```

---

### Task 5: Read queries + stock endpoints emit blend fields + quality_label

**Files:**
- Modify: `src/juteProduction/query.py` (`get_entries_by_date_query`, `get_issues_by_date_query`, `get_bins_with_stock_query`, `get_group_details_for_issue_query`, `get_bins_with_stock_for_issue_query`)
- Modify: `src/juteProduction/spreader_stock.py:41-133` (`roll_stock`, `roll_stock_quality_summary`)
- Test: `src/test/test_spreader_dual_quality_reads.py`

**Interfaces:**
- Consumes: `target_maturity_hours`, `blend_label` (Task 1).
- Produces: every row from these queries/endpoints carries `item_id_2`, `ratio_1`, `ratio_2`, `item_name_2`, `quality_label` alongside the existing `item_id`/`item_name`. Quality summary rows: `{quality_label, item_id, item_id_2, ratio_1, ratio_2, item_name, item_name_2, closing_rolls, closing_kg, closing_mt, comp1_item_id, comp1_kg, comp2_item_id, comp2_kg}`.

- [ ] **Step 1: Add the shared SQL label fragment to `query.py`**

```python
def quality_label_sql(p: str = "p", i: str = "i", i2: str = "i2") -> str:
    """SQL CASE producing 'NAME1' for pure rolls and 'NAME1/NAME2 (r1:r2)' for blends."""
    return (
        f"CASE WHEN {p}.item_id_2 IS NULL THEN {i}.item_name "
        f"ELSE CONCAT({i}.item_name, '/', {i2}.item_name, ' (', {p}.ratio_1, ':', {p}.ratio_2, ')') END"
    )
```

- [ ] **Step 2: Extend the five queries**

`get_entries_by_date_query` — in the SELECT list, after `i.item_name,` add:

```sql
            p.item_id_2,
            p.ratio_1,
            p.ratio_2,
            i2.item_name AS item_name_2,
```

and add a computed label line (use the Python fragment via f-string — the query builders already use f-strings for `JUTE_ITEM_TYPE_IDS`):

```python
    return text(
        f"""
        SELECT
            ...
            {quality_label_sql('p', 'i', 'i2')} AS quality_label,
            ...
```

and after `LEFT JOIN item_mst i ON i.item_id = p.item_id` add:

```sql
        LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2
```

`get_issues_by_date_query` — inside the `pg` subquery SELECT add:

```sql
                MIN(p.item_id_2) AS item_id_2,
                MIN(p.ratio_1) AS ratio_1,
                MIN(p.ratio_2) AS ratio_2,
                MIN(i2.item_name) AS item_name_2,
                MIN(CASE WHEN p.item_id_2 IS NULL THEN i.item_name
                     ELSE CONCAT(i.item_name, '/', i2.item_name, ' (', p.ratio_1, ':', p.ratio_2, ')') END) AS quality_label,
```

plus `LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2` in the subquery, and in the outer SELECT after `pg.item_name` add `pg.item_id_2, pg.ratio_1, pg.ratio_2, pg.item_name_2, pg.quality_label`. (MIN over the uniform-per-group label is safe — a group is single-blend by construction.)

`get_bins_with_stock_query` — inner `p` subquery: SELECT adds `pe.item_id_2, pe.ratio_1, pe.ratio_2` and GROUP BY becomes `GROUP BY pe.bin_id, pe.entry_id_grp, pe.item_id, pe.item_id_2, pe.ratio_1, pe.ratio_2`. Outer SELECT after `i.item_name,` adds:

```sql
            p.item_id_2,
            p.ratio_1,
            p.ratio_2,
            i2.item_name AS item_name_2,
            {label} AS quality_label,
```

with `LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2` after the existing `item_mst i` join (build the whole query with an f-string and `{quality_label_sql('p', 'i', 'i2')}`).

`get_group_details_for_issue_query` — SELECT adds:

```sql
            MIN(p.item_id_2) AS item_id_2,
            MIN(p.ratio_1) AS ratio_1,
            MIN(p.ratio_2) AS ratio_2,
            MIN(i2.item_name) AS item_name_2,
            MIN(CASE WHEN p.item_id_2 IS NULL THEN i.item_name
                 ELSE CONCAT(i.item_name, '/', i2.item_name, ' (', p.ratio_1, ':', p.ratio_2, ')') END) AS quality_label,
```

plus the `i2` join.

`get_bins_with_stock_for_issue_query` — same treatment as `get_bins_with_stock_query` (inner SELECT + GROUP BY + outer cols + i2 join + label).

- [ ] **Step 3: Update `spreader_stock.py`**

Imports: `from src.juteProduction.services.blend import blend_label, target_maturity_hours`.

`roll_stock` row dict — after the `"item_name"` line add:

```python
                    "item_id_2": int(m["item_id_2"]) if m.get("item_id_2") is not None else None,
                    "item_name_2": m.get("item_name_2"),
                    "ratio_1": int(m["ratio_1"] or 1),
                    "ratio_2": int(m["ratio_2"]) if m.get("ratio_2") is not None else None,
                    "quality_label": m.get("quality_label"),
```

and replace the `target_maturity_hrs` line with:

```python
                    "target_maturity_hrs": target_maturity_hours(
                        maturity_map,
                        int(m["item_id"]),
                        int(m["item_id_2"]) if m.get("item_id_2") is not None else None,
                        int(m["ratio_1"] or 1),
                        int(m["ratio_2"]) if m.get("ratio_2") is not None else None,
                        DEFAULT_MATURITY_HOURS,
                    ),
```

`roll_stock_quality_summary` — key on the blend tuple and add component weights:

```python
        summary: dict = {}
        for r in rows:
            m = dict(r._mapping)
            item_id = int(m["item_id"])
            item_id_2 = int(m["item_id_2"]) if m.get("item_id_2") is not None else None
            ratio_1 = int(m["ratio_1"] or 1)
            ratio_2 = int(m["ratio_2"]) if m.get("ratio_2") is not None else None
            key = (item_id, item_id_2, ratio_1, ratio_2)
            entry = summary.setdefault(
                key,
                {
                    "item_id": item_id,
                    "item_id_2": item_id_2,
                    "ratio_1": ratio_1,
                    "ratio_2": ratio_2,
                    "item_name": m.get("item_name"),
                    "item_name_2": m.get("item_name_2"),
                    "quality_label": m.get("quality_label")
                    or blend_label(m.get("item_name"), m.get("item_name_2"), ratio_1, ratio_2),
                    "closing_rolls": 0,
                    "closing_kg": 0.0,
                },
            )
            entry["closing_rolls"] += int(m["current_rolls"])
            entry["closing_kg"] += float(m["current_kg"] or 0)
        data = []
        for entry in summary.values():
            entry["closing_mt"] = round(entry["closing_kg"] / 1000.0, 3)
            r1, r2 = entry["ratio_1"], entry["ratio_2"]
            if entry["item_id_2"] is not None:
                share1 = r1 / (r1 + r2)
                entry["comp1_item_id"] = entry["item_id"]
                entry["comp1_kg"] = round(entry["closing_kg"] * share1, 3)
                entry["comp2_item_id"] = entry["item_id_2"]
                entry["comp2_kg"] = round(entry["closing_kg"] - entry["comp1_kg"], 3)
            else:
                entry["comp1_item_id"] = entry["item_id"]
                entry["comp1_kg"] = round(entry["closing_kg"], 3)
                entry["comp2_item_id"] = None
                entry["comp2_kg"] = 0.0
            data.append(entry)
        data.sort(key=lambda x: (x["quality_label"] or ""))
        return {"data": data}
```

- [ ] **Step 4: Write + run the tests**

```python
"""Tests for blend fields in spreader read endpoints."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _stock_row(**over):
    base = {
        "bin_id": 1, "bin_code": "B1", "entry_id_grp": 6,
        "item_id": 10, "item_name": "JTD5",
        "item_id_2": 20, "item_name_2": "ATD5", "ratio_1": 2, "ratio_2": 1,
        "quality_label": "JTD5/ATD5 (2:1)",
        "produced_rolls": 10, "issued_rolls": 0, "current_rolls": 10,
        "produced_kg": 650.0, "issued_kg": 0.0, "current_kg": 650.0,
        "avg_entry_dt": None,
    }
    base.update(over)
    return _mock_row(base)


class TestBase:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()


class TestRollStock(TestBase):
    def test_blend_fields_and_weighted_maturity(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_stock_row()],
            [_mock_row({"item_id": 10, "maturity_hours": 24}), _mock_row({"item_id": 20, "maturity_hours": 36})],
        ]
        resp = client.get("/api/spreaderProd/roll_stock?co_id=1")
        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["quality_label"] == "JTD5/ATD5 (2:1)"
        assert row["item_id_2"] == 20
        assert row["target_maturity_hrs"] == 28  # (24*2 + 36*1) / 3

    def test_pure_row_unchanged(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_stock_row(item_id_2=None, item_name_2=None, ratio_2=None, quality_label="JTD5")],
            [_mock_row({"item_id": 10, "maturity_hours": 24})],
        ]
        resp = client.get("/api/spreaderProd/roll_stock?co_id=1")
        row = resp.json()["data"][0]
        assert row["quality_label"] == "JTD5"
        assert row["item_id_2"] is None
        assert row["target_maturity_hrs"] == 24


class TestQualitySummary(TestBase):
    def test_blend_own_row_with_component_weights(self):
        self._mock_session.execute.return_value.fetchall.return_value = [
            _stock_row(),
            _stock_row(entry_id_grp=7, current_rolls=5, current_kg=325.0),
            _stock_row(entry_id_grp=8, item_id=10, item_id_2=None, item_name_2=None,
                       ratio_2=None, quality_label="JTD5", current_rolls=4, current_kg=260.0),
        ]
        resp = client.get("/api/spreaderProd/roll_stock_quality_summary?co_id=1")
        data = resp.json()["data"]
        assert len(data) == 2  # blend row + pure row, not merged
        blend = next(d for d in data if d["item_id_2"] == 20)
        assert blend["closing_rolls"] == 15
        assert blend["closing_kg"] == 975.0
        assert blend["comp1_kg"] == 650.0   # 975 * 2/3
        assert blend["comp2_kg"] == 325.0
        pure = next(d for d in data if d["item_id_2"] is None)
        assert pure["comp1_kg"] == 260.0
        assert pure["comp2_kg"] == 0.0
```

Run: `pytest src/test/test_spreader_dual_quality_reads.py -v` — iterate until PASS.

- [ ] **Step 5: Regression + commit**

Run: `pytest src/test/ -k "spreader" -v`
Expected: all PASS.

```bash
git add src/juteProduction/query.py src/juteProduction/spreader_stock.py src/test/test_spreader_dual_quality_reads.py
git commit -m "feat: blend fields + quality_label in spreader reads, stock + summary"
```

---

### Task 6: Reports — blend pivot, either-component filter, weighted maturity target

**Files:**
- Modify: `src/juteProduction/reportQueries.py:6-73` (both queries)
- Modify: `src/juteProduction/reports.py:124-194` (`spreader_production_summary`)
- Test: `src/test/test_spreader_dual_quality_reports.py`

**Interfaces:**
- Consumes: `quality_label_sql` (Task 5).
- Produces: maturity report rows gain `item_id_2/ratio_1/ratio_2/item_name_2/quality_label` and ratio-weighted `target_maturity_hrs`; production summary rows gain the same fields, `quality_pivot` keyed/labelled by `quality_label`, `items` filter matches either component.

- [ ] **Step 1: Extend `get_maturity_time_report_query`**

SELECT adds (after `i.item_name,`):

```sql
            p.item_id_2,
            p.ratio_1,
            p.ratio_2,
            i2.item_name AS item_name_2,
```

plus label via f-string `{quality_label_sql('p', 'i', 'i2')} AS quality_label,`; replace the `target_maturity_hrs` line with:

```sql
            ROUND(CASE WHEN p.item_id_2 IS NULL THEN COALESCE(mm.maturity_hours, 48)
                 ELSE (COALESCE(mm.maturity_hours, mm2.maturity_hours, 48) * p.ratio_1
                     + COALESCE(mm2.maturity_hours, mm.maturity_hours, 48) * p.ratio_2)
                     / (p.ratio_1 + p.ratio_2) END) AS target_maturity_hrs
```

joins added after the existing `item_maturity_mst mm` join:

```sql
        LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2
        LEFT JOIN item_maturity_mst mm2
               ON mm2.item_id = p.item_id_2 AND mm2.co_id = r.co_id AND mm2.active = 1
```

(import `quality_label_sql` from `src.juteProduction.query` and switch the string to an f-string).

- [ ] **Step 2: Extend `get_spreader_production_report_query`**

SELECT adds `p.item_id_2, p.ratio_1, p.ratio_2, i2.item_name AS item_name_2,` + `{quality_label_sql('p', 'i', 'i2')} AS quality_label,`; add `LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2`.

- [ ] **Step 3: Update `spreader_production_summary` in reports.py**

Replace the item filter line (`if sel_items and int(m["item_id"]) not in sel_items: continue`) with:

```python
            if sel_items:
                comp_ids = {int(m["item_id"])}
                if m.get("item_id_2") is not None:
                    comp_ids.add(int(m["item_id_2"]))
                if not comp_ids & set(sel_items):
                    continue
```

Replace the quality pivot line with:

```python
        quality_pivot = _build_pivot(rows, entity_key="quality_label", entity_label_key="quality_label")
```

Replace `unique_items` with distinct labels:

```python
                    "unique_items": len({r["quality_label"] for r in rows if r.get("quality_label")}),
```

- [ ] **Step 4: Write + run tests**

```python
"""Tests for dual-quality handling in jute production reports."""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _prod_row(**over):
    base = {
        "spreader_prod_entry_id": 1, "entry_date": date(2026, 8, 14), "entry_time": 8,
        "entry_dt": None, "spell": "A1", "machine_id": 4, "machine_name": "SPREADER 1",
        "mech_code": "SP1", "item_id": 10, "item_name": "JTD5",
        "item_id_2": 20, "ratio_1": 2, "ratio_2": 1, "item_name_2": "ATD5",
        "quality_label": "JTD5/ATD5 (2:1)",
        "no_of_rolls": 3, "wt_per_roll": 65.0, "weight_kg": 195.0,
    }
    base.update(over)
    return _mock_row(base)


class TestProductionSummary:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_pivot_keyed_by_blend_label(self):
        self._mock_session.execute.return_value.fetchall.return_value = [
            _prod_row(),
            _prod_row(spreader_prod_entry_id=2, item_id=10, item_id_2=None, ratio_2=None,
                      item_name_2=None, quality_label="JTD5"),
        ]
        resp = client.get("/api/juteProductionReports/spreader_production_summary?co_id=1&report_date=2026-08-14")
        assert resp.status_code == 200
        pivot = resp.json()["data"]["quality_pivot"]
        names = {p["entity_name"] for p in pivot}
        assert names == {"JTD5/ATD5 (2:1)", "JTD5"}
        assert resp.json()["data"]["totals"]["unique_items"] == 2

    def test_item_filter_matches_second_component(self):
        self._mock_session.execute.return_value.fetchall.return_value = [_prod_row()]
        resp = client.get(
            "/api/juteProductionReports/spreader_production_summary"
            "?co_id=1&report_date=2026-08-14&items=20"
        )
        assert len(resp.json()["data"]["rows"]) == 1

    def test_item_filter_excludes_nonmatching(self):
        self._mock_session.execute.return_value.fetchall.return_value = [_prod_row()]
        resp = client.get(
            "/api/juteProductionReports/spreader_production_summary"
            "?co_id=1&report_date=2026-08-14&items=99"
        )
        assert len(resp.json()["data"]["rows"]) == 0
```

Run: `pytest src/test/test_spreader_dual_quality_reports.py -v` — iterate until PASS.

- [ ] **Step 5: Full BE regression + commit**

Run: `pytest src/test/ -k "spreader or jute_production" -v` (57 unrelated pre-existing failures elsewhere in the suite are known — scope to these).
Expected: PASS.

```bash
git add src/juteProduction/reportQueries.py src/juteProduction/reports.py src/test/test_spreader_dual_quality_reports.py
git commit -m "feat: blend-aware spreader reports (pivot, filter, weighted maturity)"
```

---

### Task 7: FE — types, entry form second quality, bin-lock banner

**Files (all in `c:\code\vowerp3ui`, branch `spreader-dual-quality` off `dev`):**
- Modify: `src/app/dashboardportal/juteProduction/spreader/types/spreaderTypes.ts`
- Modify: `src/app/dashboardportal/juteProduction/spreader/_components/ProductionEntryForm.tsx`
- Modify: `src/app/dashboardportal/juteProduction/spreader/_components/BinStateBanner.tsx`

**Interfaces:**
- Consumes: BE `entry_bin_state` fields `locked_item_id_2`, `locked_ratio_1`, `locked_ratio_2`, `locked_quality_label` (Task 3); `entry_create` payload fields `item_id_2`, `ratio_1`, `ratio_2` (Task 4).
- Produces: rows/types used by Task 8 grids (`quality_label` on `SpreaderEntryRow`, `SpreaderIssueRow`, `RollStockRow`, `QualitySummaryRow`, `IssueBinOption`).

- [ ] **Step 1: Extend types**

In `spreaderTypes.ts`:

```ts
export type BinState = {
	active_grp: number | null;
	locked_item_id: number | null;
	locked_item_id_2: number | null;
	locked_ratio_1: number | null;
	locked_ratio_2: number | null;
	locked_quality_label: string | null;
	window: BinStateWindow;
};
```

Add to `SpreaderEntryRow` (after `item_name`): `item_id_2: number | null; item_name_2: string | null; ratio_1: number; ratio_2: number | null; quality_label: string;`
Add to `SpreaderIssueRow` (after `item_name`): `item_id_2: number | null; ratio_1: number | null; ratio_2: number | null; item_name_2: string | null; quality_label: string | null;`
Add to `RollStockRow` (after `item_name`): `item_id_2: number | null; item_name_2: string | null; ratio_1: number; ratio_2: number | null; quality_label: string;`
Replace `QualitySummaryRow`:

```ts
export type QualitySummaryRow = {
	item_id: number;
	item_id_2: number | null;
	ratio_1: number;
	ratio_2: number | null;
	item_name: string;
	item_name_2: string | null;
	quality_label: string;
	closing_rolls: number;
	closing_kg: number;
	closing_mt: number;
	comp1_item_id: number;
	comp1_kg: number;
	comp2_item_id: number | null;
	comp2_kg: number;
};
```

Add to `IssueBinOption`: `quality_label: string;` and to `IssueAvailableWeightsResponse.group`: `quality_label?: string;`.

- [ ] **Step 2: ProductionEntryForm — second quality + ratio inputs**

Add state after `itemId` (line 33):

```tsx
	const [item2Id, setItem2Id] = React.useState<number | "">("");
	const [ratio1, setRatio1] = React.useState<string>("1");
	const [ratio2, setRatio2] = React.useState<string>("1");
	const [showSecond, setShowSecond] = React.useState(false);
```

Replace the bin-lock effect (lines 58-63):

```tsx
	// If bin has an open group, force the full blend to the locked values.
	React.useEffect(() => {
		if (binState?.locked_item_id) {
			setItemId(binState.locked_item_id);
			if (binState.locked_item_id_2) {
				setShowSecond(true);
				setItem2Id(binState.locked_item_id_2);
				setRatio1(String(binState.locked_ratio_1 ?? 1));
				setRatio2(String(binState.locked_ratio_2 ?? 1));
			} else {
				setShowSecond(false);
				setItem2Id("");
			}
		}
	}, [binState?.locked_item_id, binState?.locked_item_id_2, binState?.locked_ratio_1, binState?.locked_ratio_2]);
```

Extend `formInvalid` with:

```tsx
		(showSecond &&
			(!item2Id ||
				Number(item2Id) === Number(itemId) ||
				Number(ratio1) < 1 ||
				Number(ratio2) < 1 ||
				!Number.isInteger(Number(ratio1)) ||
				!Number.isInteger(Number(ratio2)))) ||
```

Extend the save payload (after `item_id`):

```tsx
			item_id_2: showSecond && item2Id ? Number(item2Id) : null,
			ratio_1: showSecond ? Number(ratio1) : 1,
			ratio_2: showSecond && item2Id ? Number(ratio2) : null,
```

UI, after the existing Item TextField (line 215): a small row with toggle + conditional fields (locked when the group is locked):

```tsx
				{!showSecond ? (
					<Button
						size="small"
						onClick={() => setShowSecond(true)}
						disabled={!!binState?.locked_item_id}
						sx={{ alignSelf: "center", justifySelf: "start" }}
					>
						+ Second quality
					</Button>
				) : (
					<Box sx={{ display: "flex", gap: 1, alignItems: "center" }}>
						<TextField
							select
							label="Second quality"
							value={item2Id}
							onChange={(e) => setItem2Id(e.target.value === "" ? "" : Number(e.target.value))}
							size="small"
							sx={{ flex: 1 }}
							disabled={!!binState?.locked_item_id}
						>
							{setup.items
								.filter((it) => it.item_id !== Number(itemId))
								.map((it) => (
									<MenuItem key={it.item_id} value={it.item_id}>
										{it.item_name}
									</MenuItem>
								))}
						</TextField>
						<TextField
							type="number"
							label="Ratio 1"
							value={ratio1}
							onChange={(e) => setRatio1(e.target.value)}
							size="small"
							sx={{ width: 80 }}
							disabled={!!binState?.locked_item_id}
						/>
						<TextField
							type="number"
							label="Ratio 2"
							value={ratio2}
							onChange={(e) => setRatio2(e.target.value)}
							size="small"
							sx={{ width: 80 }}
							disabled={!!binState?.locked_item_id}
						/>
						{!binState?.locked_item_id ? (
							<Button size="small" onClick={() => { setShowSecond(false); setItem2Id(""); setRatio1("1"); setRatio2("1"); }}>
								✕
							</Button>
						) : null}
					</Box>
				)}
```

Also reset the second-quality state after a successful save (inside `handleSubmit` success path, with the other resets) ONLY when the bin is not locked:

```tsx
		if (!binState?.locked_item_id) {
			setShowSecond(false);
			setItem2Id("");
			setRatio1("1");
			setRatio2("1");
		}
```

- [ ] **Step 3: BinStateBanner shows the blend label**

The `lockedItemName` memo in ProductionEntryForm (lines 53-56) becomes:

```tsx
	const lockedItemName = React.useMemo(() => {
		if (!binState?.locked_item_id) return null;
		if (binState.locked_quality_label) return binState.locked_quality_label;
		return setup.items.find((i) => i.item_id === binState.locked_item_id)?.item_name ?? null;
	}, [binState, setup.items]);
```

`BinStateBanner.tsx` line 52 text becomes quality wording:

```tsx
					Open group locked to quality: {itemName ?? `#${state.locked_item_id ?? "?"}`}
```

- [ ] **Step 4: Typecheck**

Run (in `c:\code\vowerp3ui`): `npx tsc --noEmit`
Expected: no NEW errors (pre-existing errors, if any, unchanged).

- [ ] **Step 5: Commit (FE repo)**

```bash
git -C c:/code/vowerp3ui checkout -b spreader-dual-quality dev
git -C c:/code/vowerp3ui add src/app/dashboardportal/juteProduction/spreader/types/spreaderTypes.ts src/app/dashboardportal/juteProduction/spreader/_components/ProductionEntryForm.tsx src/app/dashboardportal/juteProduction/spreader/_components/BinStateBanner.tsx
git -C c:/code/vowerp3ui commit -m "feat: spreader entry second quality + ratio, blend-aware bin lock"
```

---

### Task 8: FE — all displays switch to quality_label

**Files (all in `c:\code\vowerp3ui`):**
- Modify: `src/app/dashboardportal/juteProduction/spreader/_components/DailyEntriesGrid.tsx:60`
- Modify: `src/app/dashboardportal/juteProduction/spreader/_components/DailyIssuesGrid.tsx:59`
- Modify: `src/app/dashboardportal/juteProduction/spreader/_components/RollStockTable.tsx` (grid col ~40, chips 16-34, summary grid 67-80 + 124-137)
- Modify: `src/app/dashboardportal/juteProduction/spreader/_components/IssueEntryForm.tsx:126-131, 165-171`
- Modify: `src/app/dashboardportal/juteProduction/rollStockReports/_components/MaturityReportTable.tsx:22`
- Modify: `src/app/dashboardportal/juteProduction/rollStockReports/page.tsx:52-57`
- Modify: `src/app/dashboardportal/juteProduction/rollStockReports/types/reportTypes.ts`

**Interfaces:**
- Consumes: `quality_label` fields from Tasks 5-7 types.

- [ ] **Step 1: Grid columns**

In `DailyEntriesGrid.tsx` and `DailyIssuesGrid.tsx`, replace the item column def:

```tsx
	{ field: "quality_label", headerName: "Quality", width: 220 },
```

In `MaturityReportTable.tsx` same swap (`item_name` → `quality_label`, header "Quality").

- [ ] **Step 2: RollStockTable**

- Main grid column: `{ field: "quality_label", headerName: "Quality", width: 220 }`.
- Filter chips (lines 16-34): key/filter on `quality_label` (string) instead of `item_id` — chip set built from `new Map(rows.map(r => [r.quality_label, r.quality_label]))`; the filter predicate compares `r.quality_label`.
- Quality summary grid: column `{ field: "quality_label", headerName: "Quality", width: 220 }`; row id `id: r.quality_label` (labels are unique per blend by construction); optionally show `comp1_kg`/`comp2_kg` columns:

```tsx
	{ field: "comp1_kg", headerName: "Comp 1 (kg)", width: 110, type: "number" },
	{ field: "comp2_kg", headerName: "Comp 2 (kg)", width: 110, type: "number" },
```

- [ ] **Step 3: IssueEntryForm**

Bin option label (126-131): replace `b.item_name` with `b.quality_label`. Read-only "Item (auto)" field (165-171): label "Quality (auto)", value `selectedBin?.quality_label ?? ""`.

- [ ] **Step 4: Reports page + types**

`reportTypes.ts`:
- `MaturityReportRow` adds `item_id_2: number | null; ratio_1: number | null; ratio_2: number | null; item_name_2: string | null; quality_label: string;`
- `SpreaderProductionRow` adds the same five fields.
- `PivotRow.entity_id: number | string;`

`rollStockReports/page.tsx` item-filter options (52-57): build from both components so filtering by a component finds blends:

```tsx
	const itemOptions = React.useMemo(() => {
		const map = new Map<number, string>();
		for (const r of prodRows) {
			map.set(r.item_id, r.item_name);
			if (r.item_id_2 != null && r.item_name_2) map.set(r.item_id_2, r.item_name_2);
		}
		return Array.from(map, ([id, name]) => ({ id, name }));
	}, [prodRows]);
```

(match the existing variable names in the file — the map-building block currently keyed on `item_id`/`item_name` rows.)

- [ ] **Step 5: Typecheck + commit**

Run: `npx tsc --noEmit` — no new errors.

```bash
git -C c:/code/vowerp3ui add -A src/app/dashboardportal/juteProduction
git -C c:/code/vowerp3ui commit -m "feat: spreader grids/reports display blend quality_label"
```

---

### Task 9: Full verification + browser QA on dev3

**Files:** none new.

- [ ] **Step 1: BE suite**

Run: `pytest src/test/ -k "spreader or blend or jute_production" -v`
Expected: all PASS (document any pre-existing unrelated failures).

- [ ] **Step 2: FE build check**

Run in `c:\code\vowerp3ui`: `npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Browser QA (dev3)**

Dispatch the `portal-ui-flow-tester` agent (or `/qa-portal-page juteProduction/spreader`) with scenarios:
1. Pure entry into an empty bin → saves, grid shows plain item name.
2. Blend entry (2 qualities, ratio 2:1) into another empty bin → saves; grid + stock show `NAME1/NAME2 (2:1)`.
3. Reversed entry (same two qualities entered swapped, ratio 1:2) into the SAME bin within the window → must JOIN the same group (canonicalization).
4. Different blend into the same bin → error toast names the locked blend label.
5. Issue from the blend group → issue grid shows the blend label; roll stock decrements.
6. Quality-wise closing stock shows the blend as its own row with comp weights.
7. DB verify via pymysql on dev3: `SELECT item_id, item_id_2, ratio_1, ratio_2 FROM spreader_prod_entry ORDER BY spreader_prod_entry_id DESC LIMIT 5` — canonical order stored.

Expected: all pass; fix + re-verify anything found.

- [ ] **Step 4: Update memory + final commit if fixes were made**

```bash
git add -A && git commit -m "fix: browser QA follow-ups for spreader dual-quality" # only if fixes
```

---

## Self-review notes

- Spec §1 → Task 2; §2 → Tasks 1+4; §3 → Task 3; §4 → Tasks 5+6; §5 → Tasks 7+8; §6 → per-task tests; §7 → Task 2 Step 3 + rollout note. No gaps.
- `SpreaderEntryUpdate` intentionally untouched (quality immutable on edit — spec §2).
- Issue endpoints untouched except reads (spec §4 "no schema change").
- Mobile back-compat covered by Task 4 `test_old_payload_creates_pure_roll`.
