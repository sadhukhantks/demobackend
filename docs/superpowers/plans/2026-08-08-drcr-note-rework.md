# DR/CR Note Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop phantom auto credit notes on SR approval (invoice-rate baseline instead of `po_rate or 0`), add a manual inward-linked DR/CR note create flow, and hard-delete existing phantom notes.

**Architecture:** Backend = FastAPI sync handlers + raw SQL in `src/procurement/` (vowerp3be, branch `drcr-note-rework`). Frontend = Next.js portal pages in `../vowerp3ui/src/app/dashboardportal/procurement/` (create matching branch `drcr-note-rework` off main there). New `proc_inward_dtl.invoice_rate` column is the comparison baseline; auto note fires only when invoice rate is set and differs from accepted rate beyond epsilon 0.005. Spec: `docs/superpowers/specs/2026-08-08-drcr-note-rework-design.md`.

**Tech Stack:** FastAPI, SQLAlchemy `text()` raw SQL, pytest + TestClient + MagicMock session mocks, Next.js + MUI (`TransactionLineColumn` grids, `IndexWrapper`), pymysql for migrations.

## Global Constraints

- Handlers are `def`, never `async def` (sync handler policy in CLAUDE.md).
- All responses wrapped in objects (`{"data": ...}` or explicit dict) — never raw lists.
- SQL bind params: `None` for NULL, names match exactly, type-cast ints.
- No `created_by`/`created_date` columns (audit = DB triggers).
- Migration runner splits scripts on `;` — no semicolons inside SQL comments.
- Status IDs: Draft=21, Open=1, Approved=3, Rejected=4. `adjustment_type`: 1=Debit, 2=Credit. `debitnote_type`: 1=Qty Rejection, 2=Rate Difference.
- Backend tests: `pytest src/test/ -v` from repo root with `.venv` activated (`source .venv/Scripts/activate`).
- Commits: conventional format, `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.
- Migrations apply to dev3 during implementation. sls + other tenants at deploy time only (deploy checklist at bottom) — do NOT run against sls in these tasks.

---

### Task 1: Migration + ORM — `proc_inward_dtl.invoice_rate`

**Files:**
- Create: `dbqueries/migrations/add_invoice_rate_to_proc_inward_dtl.sql`
- Modify: `src/models/procurement.py` (ProcInwardDtl, ~line 608)

**Interfaces:**
- Produces: nullable `proc_inward_dtl.invoice_rate` DOUBLE column, ORM attr `ProcInwardDtl.invoice_rate` — Tasks 2-6 depend on the column existing on dev3.

- [ ] **Step 1: Write migration script**

```sql
-- Migration: add invoice_rate to proc_inward_dtl
-- Per-line supplier invoice rate captured on the SR screen. Baseline for
-- auto DR/CR note rate-difference detection (spec 2026-08-08-drcr-note-rework).
-- Rollback: ALTER TABLE proc_inward_dtl DROP COLUMN invoice_rate

ALTER TABLE proc_inward_dtl
    ADD COLUMN invoice_rate DOUBLE NULL AFTER accepted_rate;
```

- [ ] **Step 2: Update ORM model**

In `src/models/procurement.py`, class `ProcInwardDtl`, directly after the `accepted_rate` line (~608):

```python
    invoice_rate: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
```

- [ ] **Step 3: Apply to dev3**

Run via pymysql (no mysql CLI). Write a throwaway runner in the scratchpad (creds from `env/database.env`, database `dev3`), execute the ALTER, then verify:

```sql
SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = 'dev3' AND TABLE_NAME = 'proc_inward_dtl' AND COLUMN_NAME = 'invoice_rate';
```
Expected: one row, `double`.

- [ ] **Step 4: Commit**

```bash
git add dbqueries/migrations/add_invoice_rate_to_proc_inward_dtl.sql src/models/procurement.py
git commit -m "feat(procurement): add proc_inward_dtl.invoice_rate column + ORM"
```

---

### Task 2: Cleanup migration — hard-delete phantom auto credit notes

**Files:**
- Create: `dbqueries/migrations/delete_phantom_auto_credit_notes.sql`

**Interfaces:**
- Consumes: nothing from other tasks (independent).
- Produces: dev3 cleaned (2 phantom notes gone). Same script reused for sls at deploy.

A phantom = `drcr_note` with `auto_create = 1 AND adjustment_type = 2 AND status_id IN (21, 1)` where NO line joins to a PO-linked inward dtl. Backups first, delete order gst → dtl → header.

- [ ] **Step 1: Write migration script**

```sql
-- Migration: delete phantom auto-created credit notes
-- Root cause: approve_sr compared accepted_rate against po_rate coerced to 0 for
-- PO-less inward lines, creating a bogus credit note on every SR approval.
-- Scope: auto_create=1, adjustment_type=2 (credit), status Draft(21)/Open(1),
-- and no line whose inward dtl has a po_dtl_id.
-- Rollback: INSERT INTO drcr_note SELECT * FROM drcr_note_bak_20260808 and same
-- for the dtl and gst bak tables, then DROP the bak tables.

CREATE TABLE IF NOT EXISTS drcr_note_bak_20260808 AS
SELECT dn.* FROM drcr_note dn
WHERE dn.auto_create = 1 AND dn.adjustment_type = 2 AND dn.status_id IN (21, 1)
  AND NOT EXISTS (
    SELECT 1 FROM drcr_note_dtl dd
    JOIN proc_inward_dtl pid ON pid.inward_dtl_id = dd.inward_dtl_id
    WHERE dd.debit_credit_note_id = dn.debit_credit_note_id
      AND pid.po_dtl_id IS NOT NULL
  );

CREATE TABLE IF NOT EXISTS drcr_note_dtl_bak_20260808 AS
SELECT dd.* FROM drcr_note_dtl dd
JOIN drcr_note_bak_20260808 b ON b.debit_credit_note_id = dd.debit_credit_note_id;

CREATE TABLE IF NOT EXISTS drcr_note_dtl_gst_bak_20260808 AS
SELECT g.* FROM drcr_note_dtl_gst g
JOIN drcr_note_dtl_bak_20260808 db ON db.drcr_note_dtl_id = g.drcr_note_dtl_id;

DELETE g FROM drcr_note_dtl_gst g
JOIN drcr_note_dtl_bak_20260808 db ON db.drcr_note_dtl_id = g.drcr_note_dtl_id;

DELETE dd FROM drcr_note_dtl dd
JOIN drcr_note_bak_20260808 b ON b.debit_credit_note_id = dd.debit_credit_note_id;

DELETE dn FROM drcr_note dn
JOIN drcr_note_bak_20260808 b ON b.debit_credit_note_id = dn.debit_credit_note_id;
```

- [ ] **Step 2: Apply to dev3 + verify**

Run via pymysql against dev3. Verify:

```sql
SELECT COUNT(*) FROM drcr_note WHERE auto_create = 1 AND adjustment_type = 2;
SELECT COUNT(*) FROM drcr_note_bak_20260808;
```
Expected on dev3: first count 0, backup count 2. Also check no orphan dtl rows: `SELECT COUNT(*) FROM drcr_note_dtl dd LEFT JOIN drcr_note dn ON dn.debit_credit_note_id = dd.debit_credit_note_id WHERE dn.debit_credit_note_id IS NULL` → 0.

- [ ] **Step 3: Commit**

```bash
git add dbqueries/migrations/delete_phantom_auto_credit_notes.sql
git commit -m "fix(procurement): migration deleting phantom auto credit notes (PO-less SR bug)"
```

---

### Task 3: `save_sr` persists `invoice_rate`; SR GET exposes it

**Files:**
- Modify: `src/procurement/sr.py` (SRLineItemUpdate ~line 64; save loop ~line 514; `get_sr_by_inward_id` line-item mapping ~line 325)
- Modify: `src/procurement/query.py` (`get_inward_dtl_for_sr_query` SELECT ~line 2064; `update_inward_dtl_sr` ~line 2110)
- Test: `src/test/test_procurement_sr_invoice_rate.py` (new)

**Interfaces:**
- Consumes: `invoice_rate` column (Task 1).
- Produces: `SRLineItemUpdate.invoice_rate: Optional[float]`; `save_sr` writes it; SR line-item GET responses include `invoice_rate` (server-side default: stored value, else PO rate when PO-linked, else None). Task 4 reads `item["invoice_rate"]` from `get_inward_dtl_for_sr_query` rows; Task 7 (FE) reads/sends `invoice_rate`.

- [ ] **Step 1: Write failing tests**

New file `src/test/test_procurement_sr_invoice_rate.py`, following the mock pattern of `src/test/test_procurement_sr_approve.py` (dependency_overrides for `get_tenant_db` + `get_current_user_with_refresh`, MagicMock session). Two tests:

```python
# src/test/test_procurement_sr_invoice_rate.py
"""Tests that save_sr persists per-line invoice_rate (spec 2026-08-08-drcr-note-rework)."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)


def _override_auth():
    return {"user_id": 1}


def _make_mock_db_override(mock_session):
    def _override():
        yield mock_session
    return _override


class TestSaveSRInvoiceRate:
    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def _line_update_params(self):
        """Return the params dict of the proc_inward_dtl UPDATE issued by save_sr."""
        for c in self.mock_session.execute.call_args_list:
            if len(c[0]) > 1 and isinstance(c[0][1], dict) and "accepted_rate" in c[0][1]:
                return c[0][1]
        return None

    def test_save_sr_persists_invoice_rate(self):
        resp = client.post("/api/storesReceipt/save_sr", json={
            "inward_id": 7,
            "sr_date": "2026-08-08",
            "line_items": [{
                "inward_dtl_id": 16,
                "accepted_rate": 90.0,
                "invoice_rate": 100.0,
                "amount": 900.0,
            }],
        })
        assert resp.status_code == 200
        params = self._line_update_params()
        assert params is not None
        assert params["invoice_rate"] == 100.0

    def test_save_sr_invoice_rate_optional_defaults_none(self):
        resp = client.post("/api/storesReceipt/save_sr", json={
            "inward_id": 7,
            "sr_date": "2026-08-08",
            "line_items": [{
                "inward_dtl_id": 16,
                "accepted_rate": 90.0,
                "amount": 900.0,
            }],
        })
        assert resp.status_code == 200
        params = self._line_update_params()
        assert params is not None
        assert params["invoice_rate"] is None
```

Note: `save_sr` runs many queries before/after the line update against the MagicMock (all succeed silently); if the endpoint 500s on something unrelated to mocks, inspect `resp.json()` and adjust mock `side_effect` only if a specific `fetchone()` result is required (mirror how `test_procurement_sr_editable_header.py` sets up its session).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/test/test_procurement_sr_invoice_rate.py -v`
Expected: FAIL — `invoice_rate` not in params dict (KeyError/assert None mismatch).

- [ ] **Step 3: Implement**

a. `src/procurement/sr.py` `SRLineItemUpdate` (after `accepted_rate: float`, line 67):

```python
    invoice_rate: Optional[float] = None
```

b. `src/procurement/query.py` `update_inward_dtl_sr()` — add to SET list:

```sql
        accepted_rate = :accepted_rate,
        invoice_rate = :invoice_rate,
```

c. `src/procurement/sr.py` save loop (~line 514) — add to the execute params:

```python
                "invoice_rate": line.invoice_rate,
```

d. `src/procurement/query.py` `get_inward_dtl_for_sr_query()` — add to SELECT after `pid.accepted_rate,` (line 2064):

```sql
        pid.invoice_rate,
```

e. `src/procurement/sr.py` `get_sr_by_inward_id` line-item loop (~lines 325-332, where `accepted_rate` is defaulted) — add server-side prefill so the FE shows PO rate as the starting invoice rate for PO-linked lines:

```python
            # Default invoice_rate: stored value wins, else PO rate for PO-linked
            # lines, else None (PO-less lines start blank).
            if item.get("invoice_rate") is None and item.get("po_dtl_id"):
                item["invoice_rate"] = item.get("po_rate")
```

- [ ] **Step 4: Run tests**

Run: `pytest src/test/test_procurement_sr_invoice_rate.py src/test/test_procurement_sr_editable_header.py src/test/test_procurement_sr_warehouse.py -v`
Expected: new tests PASS; existing SR save tests still PASS (if any assert exact param dicts, add the `invoice_rate: None` key to their expectations).

- [ ] **Step 5: Commit**

```bash
git add src/procurement/sr.py src/procurement/query.py src/test/test_procurement_sr_invoice_rate.py
git commit -m "feat(procurement): capture per-line invoice_rate on SR save"
```

---

### Task 4: `approve_sr` — invoice-rate-gated note logic (the bug fix)

**Files:**
- Modify: `src/procurement/sr.py:939-977` (comparison loop only — header/GST/insert blocks unchanged)
- Test: `src/test/test_procurement_sr_approve.py` (extend + update existing)

**Interfaces:**
- Consumes: `invoice_rate` in `get_inward_dtl_for_sr_query` rows (Task 3d).
- Produces: new trigger semantics — rate-diff notes ONLY when `invoice_rate` set and `abs(invoice_rate - accepted_rate) > 0.005`; rejection debit note rate = `invoice_rate or po_rate or accepted_rate`.

- [ ] **Step 1: Update existing tests for new semantics**

Every `_mock_row` line-item mapping in `src/test/test_procurement_sr_approve.py` must gain an `"invoice_rate"` key. Semantics change:
- `test_approve_sr_with_rejected_qty_passes_debit_credit_note_id`: add `"invoice_rate": 200.0` (equal to accepted — rejection note only, unchanged assertions).
- `test_approve_sr_with_rate_increase_creates_credit_note_with_note_id`: add `"invoice_rate": 100.0` with `accepted_rate: 120.0` — credit note now driven by invoice rate, assertions unchanged.
- Any rate-decrease/debit test: add `"invoice_rate"` = the old `po_rate` value.
- `test_approve_sr_no_drcr_when_rates_match_and_no_rejection` (if present): add `"invoice_rate"` equal to accepted_rate.

- [ ] **Step 2: Write new failing tests**

Append to `src/test/test_procurement_sr_approve.py` (same class/pattern; reuse `_mock_row`, `_make_mock_db_override`):

```python
    def _approve(self, line_rows, extra_side_effects_after_lines=None):
        """POST approve_sr with mocked line rows. Returns response."""
        header_row = MagicMock()
        header_row.branch_id = 1
        header_row.sr_no = "SR-EXISTING"
        header_row.sr_date = date(2025, 1, 1)
        side = [
            MagicMock(fetchall=MagicMock(return_value=line_rows)),   # line items
            MagicMock(fetchone=MagicMock(return_value=None)),        # GST prep inward_info (india_gst off)
        ]
        if extra_side_effects_after_lines:
            side.extend(extra_side_effects_after_lines)
        side.extend([
            MagicMock(fetchone=MagicMock(return_value=header_row)),  # header for sr_no minting
            MagicMock(),                                             # update SR status
        ])
        self.mock_session.execute.side_effect = side
        return client.post("/api/storesReceipt/approve_sr", json={"inward_id": 7})

    def test_poless_line_without_invoice_rate_creates_no_note(self):
        """Regression: PO-less line (po_rate None) + blank invoice rate must NOT create any note."""
        line_row = _mock_row({
            "inward_dtl_id": 30, "po_rate": None, "rate": None, "invoice_rate": None,
            "accepted_rate": 84.75, "rejected_qty": 0, "approved_qty": 10.0,
        })
        response = self._approve([line_row])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is False

    def test_invoice_rate_above_accepted_creates_debit_note(self):
        """Invoice 100 vs accepted 90 - supplier billed high - debit note, rate 10."""
        note_id_row = MagicMock(); note_id_row.id = 61
        line_row = _mock_row({
            "inward_dtl_id": 31, "po_rate": None, "rate": None, "invoice_rate": 100.0,
            "accepted_rate": 90.0, "rejected_qty": 0, "approved_qty": 5.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),                                             # insert note header
            MagicMock(fetchone=MagicMock(return_value=note_id_row)), # LAST_INSERT_ID
            MagicMock(),                                             # insert dtl
        ])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is True
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["debit_credit_note_id"] == 61
        assert dtl_params["rate"] == pytest.approx(10.0)
        hdr_params = self.mock_session.execute.call_args_list[2][0][1]
        assert hdr_params["adjustment_type"] == 1

    def test_invoice_rate_below_accepted_creates_credit_note(self):
        """Invoice 80 vs accepted 90 - we owe supplier - credit note, rate 10."""
        note_id_row = MagicMock(); note_id_row.id = 62
        line_row = _mock_row({
            "inward_dtl_id": 32, "po_rate": None, "rate": None, "invoice_rate": 80.0,
            "accepted_rate": 90.0, "rejected_qty": 0, "approved_qty": 5.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is True
        hdr_params = self.mock_session.execute.call_args_list[2][0][1]
        assert hdr_params["adjustment_type"] == 2

    def test_rate_diff_within_epsilon_creates_no_note(self):
        line_row = _mock_row({
            "inward_dtl_id": 33, "po_rate": 100.0, "rate": 100.0, "invoice_rate": 100.004,
            "accepted_rate": 100.0, "rejected_qty": 0, "approved_qty": 5.0,
        })
        response = self._approve([line_row])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is False

    def test_rejection_note_rate_falls_back_invoice_then_po(self):
        """Rejected qty debit note uses invoice_rate when present."""
        note_id_row = MagicMock(); note_id_row.id = 63
        line_row = _mock_row({
            "inward_dtl_id": 34, "po_rate": 95.0, "rate": 95.0, "invoice_rate": 100.0,
            "accepted_rate": 100.0, "rejected_qty": 3.0, "approved_qty": 7.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["rate"] == pytest.approx(100.0)
        assert dtl_params["quantity"] == pytest.approx(3.0)
```

(Adjust `call_args_list` indices only if a run shows the endpoint issues a different number of pre-insert queries — count them from the failure output, don't guess.)

- [ ] **Step 3: Run to verify new tests fail**

Run: `pytest src/test/test_procurement_sr_approve.py -v`
Expected: new tests FAIL (phantom credit note created in `test_poless_line_without_invoice_rate_creates_no_note`, wrong adjustment_type in invoice-diff tests). Updated old tests may pass or fail — note which.

- [ ] **Step 4: Implement — replace sr.py:939-977 comparison loop**

```python
        # Check for rate differences and rejected quantities
        RATE_EPSILON = 0.005  # ignore float noise below half a paisa
        drcr_lines_debit = []  # rejected qty, or invoice rate above accepted
        drcr_lines_credit = []  # invoice rate below accepted

        for row in line_items:
            item = dict(row._mapping)
            po_rate = item.get("po_rate")
            invoice_rate = item.get("invoice_rate")
            accepted_rate = item.get("accepted_rate") or 0
            rejected_qty = item.get("rejected_qty") or 0
            approved_qty = item.get("approved_qty") or 0

            # Rejected quantity - always creates debit note
            if rejected_qty > 0:
                drcr_lines_debit.append({
                    "inward_dtl_id": item.get("inward_dtl_id"),
                    "debitnote_type": 1,  # Quantity rejection
                    "quantity": rejected_qty,
                    "rate": invoice_rate or po_rate or accepted_rate,
                })

            # Rate difference - only when a real invoice rate was captured.
            # No invoice rate means no baseline, so no note (fixes phantom
            # credit notes on PO-less inwards where po_rate coerced to 0).
            if invoice_rate and invoice_rate > 0:
                rate_diff = invoice_rate - accepted_rate
                if rate_diff > RATE_EPSILON:
                    # Supplier billed above accepted - debit note (supplier owes us)
                    drcr_lines_debit.append({
                        "inward_dtl_id": item.get("inward_dtl_id"),
                        "debitnote_type": 2,  # Rate difference
                        "quantity": approved_qty,
                        "rate": rate_diff,
                    })
                elif rate_diff < -RATE_EPSILON:
                    # Supplier billed below accepted - credit note (we owe supplier)
                    drcr_lines_credit.append({
                        "inward_dtl_id": item.get("inward_dtl_id"),
                        "debitnote_type": 2,  # Rate difference
                        "quantity": approved_qty,
                        "rate": abs(rate_diff),
                    })
```

Everything below (header inserts at 979+, GST, sr_no minting) stays untouched.

- [ ] **Step 5: Run full SR test set**

Run: `pytest src/test/ -k "sr" -v`
Expected: all PASS (fix any existing tests still asserting old po_rate-driven behavior per Step 1 rules).

- [ ] **Step 6: Commit**

```bash
git add src/procurement/sr.py src/test/test_procurement_sr_approve.py
git commit -m "fix(procurement): gate auto DR/CR rate-diff notes on captured invoice_rate

PO-less inward lines coerced po_rate to 0, so every SR approval minted a
phantom credit note. Rate-diff notes now require an explicit per-line
invoice_rate baseline (epsilon 0.005); rejection debit notes unchanged."
```

---

### Task 5: Repair `get_inward_for_drcr_note` + new eligible-inwards endpoint

**Files:**
- Modify: `src/procurement/drcr_note.py:532-629` (rewrite queries), add `get_eligible_inwards_for_drcr` endpoint
- Modify: `src/procurement/query.py` (two new query functions after `approve_drcr_note_query`, ~line 2420)
- Test: `src/test/test_drcr_note_manual.py` (new)

**Interfaces:**
- Consumes: `invoice_rate` column (Task 1).
- Produces:
  - `GET /api/drcrNote/get_inward_for_drcr_note/{inward_id}?co_id=` → `{"header": {...}, "line_items": [...]}` with real-schema joins; line items include `inward_dtl_id, item_id, item_code, item_name, item_grp_name, uom_name, inward_qty, rejected_qty, approved_qty, rate, accepted_rate, invoice_rate, po_rate, hsn_code`.
  - `GET /api/drcrNote/get_eligible_inwards?co_id=&branch_id=&search=&limit=` → `{"data": [{"inward_id", "inward_no", "sr_no", "sr_date", "supplier_id", "supplier_name", "branch_id"}]}` (approved-SR inwards only).
  - Query fns: `get_inward_header_for_drcr_query()`, `get_inward_dtl_for_drcr_query()`, `get_eligible_inwards_for_drcr_query()`.

- [ ] **Step 1: Write failing tests**

```python
# src/test/test_drcr_note_manual.py
"""Tests for manual DRCR note endpoints: prefill, eligible list, create hardening."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from datetime import date

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)


def _override_auth():
    return {"user_id": 1}


def _make_mock_db_override(mock_session):
    def _override():
        yield mock_session
    return _override


def _mock_row(mapping):
    row = MagicMock()
    row._mapping = mapping
    return row


class TestDrcrManualEndpoints:
    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def test_prefill_queries_use_real_schema_tables(self):
        """The rewritten queries must not reference legacy con_* / *_master tables."""
        from src.procurement.query import (
            get_inward_header_for_drcr_query,
            get_inward_dtl_for_drcr_query,
            get_eligible_inwards_for_drcr_query,
        )
        for q in (get_inward_header_for_drcr_query(),
                  get_inward_dtl_for_drcr_query(),
                  get_eligible_inwards_for_drcr_query()):
            sql = str(q)
            for bad in ("branch_master", "con_supplier_master", "con_company_info",
                        "con_item_master", "con_item_group", "con_item_make", "con_uom_master"):
                assert bad not in sql, f"legacy table {bad} still referenced"

    def test_prefill_rejects_unapproved_sr(self):
        header = _mock_row({
            "inward_id": 7, "inward_sequence_no": 5, "inward_date": date(2026, 8, 1),
            "branch_id": 2, "branch_name": "Factory", "branch_prefix": "FA",
            "supplier_id": 9, "supplier_name": "ACME", "sr_no": None, "sr_date": None,
            "sr_status": 1, "challan_no": None, "challan_date": None, "co_prefix": "EM",
        })
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=header)),
        ]
        resp = client.get("/api/drcrNote/get_inward_for_drcr_note/7")
        assert resp.status_code == 400
        assert "approved" in resp.json()["detail"].lower()

    def test_eligible_inwards_returns_wrapped_data(self):
        row = _mock_row({
            "inward_id": 7, "inward_sequence_no": 5, "inward_date": date(2026, 8, 1),
            "sr_no": 12, "sr_date": date(2026, 8, 2), "supplier_id": 9,
            "supplier_name": "ACME", "branch_id": 2, "branch_prefix": "FA", "co_prefix": "EM",
        })
        self.mock_session.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[row])),
        ]
        resp = client.get("/api/drcrNote/get_eligible_inwards?co_id=1")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["data"], list)
        assert body["data"][0]["inward_id"] == 7
        assert body["data"][0]["supplier_name"] == "ACME"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest src/test/test_drcr_note_manual.py -v`
Expected: FAIL — ImportError (query fns don't exist), 404 on `/get_eligible_inwards`.

- [ ] **Step 3: Implement query functions** (append after `approve_drcr_note_query()` in `src/procurement/query.py`)

```python
def get_inward_header_for_drcr_query():
    """Inward header for the manual DRCR-note create screen."""
    sql = """SELECT
        pi.inward_id,
        pi.inward_sequence_no,
        pi.inward_date,
        pi.branch_id,
        bm.branch_name,
        bm.branch_prefix,
        cm.co_prefix,
        pi.supplier_id,
        pm.supp_name AS supplier_name,
        pi.sr_no,
        pi.sr_date,
        pi.sr_status,
        pi.challan_no,
        pi.challan_date
    FROM proc_inward AS pi
    LEFT JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
    LEFT JOIN co_mst AS cm ON cm.co_id = bm.co_id
    LEFT JOIN party_mst AS pm ON pm.party_id = pi.supplier_id
    WHERE pi.inward_id = :inward_id;"""
    return text(sql)


def get_inward_dtl_for_drcr_query():
    """Inward line items for the manual DRCR-note create screen."""
    sql = """SELECT
        pid.inward_dtl_id,
        pid.item_id,
        im.item_code,
        im.item_name,
        ig.item_grp_name,
        vip.full_item_code,
        pid.uom_id,
        um.uom_name,
        pid.inward_qty,
        pid.rejected_qty,
        pid.approved_qty,
        pid.rate,
        pid.accepted_rate,
        pid.invoice_rate,
        ppd.rate AS po_rate,
        pid.hsn_code
    FROM proc_inward_dtl AS pid
    LEFT JOIN item_mst AS im ON im.item_id = pid.item_id
    LEFT JOIN item_grp_mst AS ig ON ig.item_grp_id = im.item_grp_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = pid.item_id
    LEFT JOIN uom_mst AS um ON um.uom_id = pid.uom_id
    LEFT JOIN proc_po_dtl AS ppd ON ppd.po_dtl_id = pid.po_dtl_id
    WHERE pid.inward_id = :inward_id
        AND pid.active = 1
    ORDER BY pid.inward_dtl_id;"""
    return text(sql)


def get_eligible_inwards_for_drcr_query():
    """Approved-SR inwards a manual DRCR note can be raised against."""
    sql = """SELECT
        pi.inward_id,
        pi.inward_sequence_no,
        pi.inward_date,
        pi.sr_no,
        pi.sr_date,
        pi.supplier_id,
        pm.supp_name AS supplier_name,
        pi.branch_id,
        bm.branch_prefix,
        cm.co_prefix
    FROM proc_inward AS pi
    JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
    JOIN co_mst AS cm ON cm.co_id = bm.co_id
    LEFT JOIN party_mst AS pm ON pm.party_id = pi.supplier_id
    WHERE pi.sr_status = 3
        AND (:co_id IS NULL OR bm.co_id = :co_id)
        AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
        AND (
            :search_like IS NULL
            OR pi.sr_no LIKE :search_like
            OR pm.supp_name LIKE :search_like
        )
    ORDER BY pi.sr_date DESC, pi.inward_id DESC
    LIMIT :limit;"""
    return text(sql)
```

- [ ] **Step 4: Rewrite endpoint + add eligible-inwards endpoint** in `src/procurement/drcr_note.py`

Replace the two inline `text(...)` queries inside `get_inward_for_drcr_note` (lines 543-556 and 587-601) with the new query functions (import them from `src.procurement.query`). Keep the existing response shape and the `sr_status != STATUS_APPROVED → 400` gate. In the line-items loop, format nothing extra — pass through the mapped dict (it now has `invoice_rate`, `po_rate`).

Add new endpoint (plain `def`, same file):

```python
@router.get("/get_eligible_inwards")
def get_eligible_inwards(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    search: str | None = None,
    limit: int = 20,
):
    """Approved-SR inwards for the manual DRCR-note create screen."""
    try:
        limit = max(min(limit, 100), 1)
        search_like = f"%{search.strip()}%" if search else None
        rows = db.execute(get_eligible_inwards_for_drcr_query(), {
            "co_id": co_id,
            "branch_id": branch_id,
            "search_like": search_like,
            "limit": limit,
        }).fetchall()
        data = []
        for row in rows:
            m = dict(row._mapping)
            inward_no = ""
            raw_no = m.get("inward_sequence_no")
            if raw_no:
                try:
                    inward_no = format_inward_no(
                        inward_sequence_no=int(raw_no),
                        co_prefix=m.get("co_prefix"),
                        branch_prefix=m.get("branch_prefix"),
                        inward_date=m.get("inward_date"),
                    )
                except Exception:
                    inward_no = str(raw_no)
            data.append({
                "inward_id": m.get("inward_id"),
                "inward_no": inward_no,
                "sr_no": m.get("sr_no") or "",
                "sr_date": m.get("sr_date").isoformat() if m.get("sr_date") else None,
                "supplier_id": m.get("supplier_id"),
                "supplier_name": m.get("supplier_name") or "",
                "branch_id": m.get("branch_id"),
            })
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching eligible inwards for DRCR note")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
```

- [ ] **Step 5: Run tests**

Run: `pytest src/test/test_drcr_note_manual.py -v`
Expected: the three tests written so far PASS.

- [ ] **Step 6: Commit**

```bash
git add src/procurement/drcr_note.py src/procurement/query.py src/test/test_drcr_note_manual.py
git commit -m "fix(procurement): repair drcr-note prefill queries, add eligible-inwards endpoint"
```

---

### Task 6: Harden `create_drcr_note` — SR-approved gate + GST breakup

**Files:**
- Modify: `src/procurement/drcr_note.py:340-418` (`create_drcr_note`)
- Test: `src/test/test_drcr_note_manual.py` (extend)

**Interfaces:**
- Consumes: `insert_drcr_note_dtl_gst()` (exists, query.py:2380), `calculate_gst_amounts` (import from `src.procurement.gst_utils` — verify actual module by checking the import in `src/procurement/sr.py` and mirror it).
- Produces: `POST /api/drcrNote/create_drcr_note` returns 400 when the inward's SR isn't approved; inserts `drcr_note_dtl_gst` rows when india_gst applies.

- [ ] **Step 1: Write failing tests** (append to `TestDrcrManualEndpoints`)

```python
    def test_create_rejects_unapproved_sr(self):
        sr_row = MagicMock()
        sr_row.sr_status = 1
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=sr_row)),  # sr_status check
        ]
        resp = client.post("/api/drcrNote/create_drcr_note", json={
            "inward_id": 7,
            "note_date": "2026-08-08",
            "adjustment_type": 1,
            "line_items": [{"inward_dtl_id": 16, "debitnote_type": 2, "quantity": 5, "rate": 10}],
        })
        assert resp.status_code == 400
        assert "approved" in resp.json()["detail"].lower()
        self.mock_session.commit.assert_not_called()

    def test_create_inserts_gst_when_india_gst(self):
        sr_row = MagicMock()
        sr_row.sr_status = 3
        note_id_row = MagicMock(); note_id_row.id = 71
        dtl_id_row = MagicMock(); dtl_id_row.id = 501
        gst_ctx_row = _mock_row({"supplier_branch_id": 4, "ship_branch_id": 2, "co_id": 1})
        cfg_row = _mock_row({"india_gst": 1})
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=sr_row)),        # sr_status check
            MagicMock(fetchone=MagicMock(return_value=gst_ctx_row)),   # inward_info
            MagicMock(fetchone=MagicMock(return_value=cfg_row)),       # co_config
            MagicMock(fetchone=MagicMock(return_value=(29,))),         # supplier state
            MagicMock(fetchone=MagicMock(return_value=(29,))),         # ship state
            MagicMock(fetchall=MagicMock(return_value=[               # proc_gst tax pcts
                _mock_row({"proc_inward_dtl": 16, "tax_pct": 18.0}),
            ])),
            MagicMock(),                                               # insert header
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),   # LAST_INSERT_ID header
            MagicMock(),                                               # insert dtl
            MagicMock(fetchone=MagicMock(return_value=dtl_id_row)),    # LAST_INSERT_ID dtl
            MagicMock(),                                               # insert gst
        ]
        resp = client.post("/api/drcrNote/create_drcr_note", json={
            "inward_id": 7,
            "note_date": "2026-08-08",
            "adjustment_type": 1,
            "line_items": [{"inward_dtl_id": 16, "debitnote_type": 2, "quantity": 5, "rate": 10}],
        })
        assert resp.status_code == 200, resp.json()
        gst_params = self.mock_session.execute.call_args_list[-1][0][1]
        assert "cgst_amount" in gst_params
        assert gst_params["drcr_note_dtl_id"] == 501
```

(As in Task 4: if the implemented call order differs, fix the side-effect list from the actual failure output; state assertions — 400 gate, gst params keys — stay as written.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest src/test/test_drcr_note_manual.py -v`
Expected: new tests FAIL (no 400 gate; no GST insert call).

- [ ] **Step 3: Implement**

In `create_drcr_note`, after the `user_id` check:

```python
        # The inward's SR must be approved before any manual note.
        sr_row = db.execute(
            text("SELECT sr_status FROM proc_inward WHERE inward_id = :inward_id"),
            {"inward_id": request_body.inward_id},
        ).fetchone()
        if not sr_row:
            raise HTTPException(status_code=404, detail="Inward not found")
        if sr_row.sr_status != STATUS_APPROVED:
            raise HTTPException(status_code=400, detail="SR must be approved before creating DRCR Note")
```

Then, before the header insert, build the GST context — copy the exact prep block from `approve_sr` (`src/procurement/sr.py:839-909`: inward_info query, co_config india_gst check, supplier/ship `state_id` lookups, `proc_gst` tax_pct map keyed by `inward_dtl_id`) substituting `request_body.inward_id` and the request's `line.inward_dtl_id` list. Import `calculate_gst_amounts` from the same module `sr.py` imports it from, plus `insert_drcr_note_dtl_gst` from `src.procurement.query`.

After each `insert_drcr_note_dtl()` execute in the line loop, add:

```python
            if india_gst and supplier_state_id and shipping_state_id:
                tax_pct = tax_pct_by_inward_dtl.get(line.inward_dtl_id, 0.0)
                if tax_pct > 0:
                    dtl_id_row = db.execute(text("SELECT LAST_INSERT_ID() AS id")).fetchone()
                    drcr_note_dtl_id = dtl_id_row.id if dtl_id_row else None
                    if drcr_note_dtl_id:
                        gst = calculate_gst_amounts(
                            calculate_line_amount(line), tax_pct,
                            supplier_state_id, shipping_state_id,
                        )
                        db.execute(insert_drcr_note_dtl_gst(), {
                            "drcr_note_dtl_id": drcr_note_dtl_id,
                            "cgst_amount": gst["c_tax_amount"],
                            "igst_amount": gst["i_tax_amount"],
                            "sgst_amount": gst["s_tax_amount"],
                        })
```

- [ ] **Step 4: Run tests**

Run: `pytest src/test/test_drcr_note_manual.py src/test/test_procurement_sr_approve.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/procurement/drcr_note.py src/test/test_drcr_note_manual.py
git commit -m "feat(procurement): SR-approved gate + GST breakup on manual DRCR note create"
```

---

### Task 7 (FE repo): Invoice Rate column on Create SR

**Files (all under `c:\code\vowerp3ui`):**
- Modify: `src/app/dashboardportal/procurement/sr/createSR/components/SRLineItemsTable.tsx` (~line 202, after the `po_rate` column)
- Modify: `src/app/dashboardportal/procurement/sr/createSR/hooks/useSRApproval.ts` (~line 81, `lineItemsPayload`)
- Modify: the `SRLineItem` type + line-item load mapping (grep `accepted_rate` in `src/app/dashboardportal/procurement/sr/createSR/` — same type/hook files that define/map it, likely `types.ts` and `useSRLineItems`)

**Interfaces:**
- Consumes: BE GET now returns `invoice_rate` per line (Task 3e prefills PO rate server-side); BE save accepts `invoice_rate` (Task 3a-c).
- Produces: editable `invoice_rate` on every SR line, round-tripped through save.

- [ ] **Step 1: Create branch in FE repo**

```bash
cd c:\code\vowerp3ui
git fetch origin main
git checkout -b drcr-note-rework origin/main
```

- [ ] **Step 2: Add `invoice_rate` to the `SRLineItem` type and load mapping**

Add `invoice_rate: number | null;` to the `SRLineItem` type. In the hook that maps the GET response into line items (where `accepted_rate` is mapped), add `invoice_rate: item.invoice_rate ?? null,`.

- [ ] **Step 3: Add the column** in `SRLineItemsTable.tsx`, immediately after the read-only `po_rate` column block (ends ~line 202), cloning the `accepted_rate` editable pattern:

```tsx
			{
				id: "invoice_rate",
				header: "Invoice Rate",
				width: "0.7fr",
				minWidth: "110px",
				renderCell: ({ item }) => (
					<EditableNumberCell
						initialValue={item.invoice_rate}
						disabled={!canEdit}
						onCommit={(val) => onLineItemChange(item.id, "invoice_rate", val)}
					/>
				),
			},
```

- [ ] **Step 4: Send it on save** — in `useSRApproval.ts` `lineItemsPayload` (line ~81), after `accepted_rate`:

```ts
				invoice_rate: item.invoice_rate ?? null,
```

- [ ] **Step 5: Typecheck/build**

Run: `npx tsc --noEmit` (or `npm run build` if that's the repo norm — check package.json scripts).
Expected: no new type errors.

- [ ] **Step 6: Commit (FE repo)**

```bash
git add src/app/dashboardportal/procurement/sr/createSR
git commit -m "feat(procurement): per-line Invoice Rate entry on Create SR"
```

---

### Task 8 (FE repo): Manual DR/CR note create page + New Note button

**Files (all under `c:\code\vowerp3ui`):**
- Create: `src/app/dashboardportal/procurement/drcrNote/create/page.tsx`
- Modify: `src/app/dashboardportal/procurement/drcrNote/page.tsx` (add `createAction` to `IndexWrapper`, ~line 332)
- Modify: `src/utils/api.ts` (~line 497: add `DRCR_NOTE_ELIGIBLE_INWARDS`)

**Interfaces:**
- Consumes: `GET /drcrNote/get_eligible_inwards` (Task 5), `GET /drcrNote/get_inward_for_drcr_note/{id}` (Task 5), `POST /drcrNote/create_drcr_note` (Task 6). Existing constants `DRCR_NOTE_CREATE`, `DRCR_NOTE_GET_INWARD_FOR_CREATE` (api.ts:493,497).
- Produces: working manual-note flow ending on the existing view page (`/dashboardportal/procurement/drcrNote/view?id=<id>`).

- [ ] **Step 1: Add API constant** in `src/utils/api.ts` next to the other DRCR constants:

```ts
    DRCR_NOTE_ELIGIBLE_INWARDS: `${API_URL}/drcrNote/get_eligible_inwards`,
```

- [ ] **Step 2: Add New Note button** in `drcrNote/page.tsx` — inside the `<IndexWrapper ...>` props (mirror `inward/page.tsx:235`):

```tsx
			createAction={{ onClick: () => router.push("/dashboardportal/procurement/drcrNote/create"), label: "New Note" }}
```

(`router` already exists in the component for `handleView`/`handleEdit`.)

- [ ] **Step 3: Build the create page** `drcrNote/create/page.tsx`. Structure (client component, `fetchWithCookie` from `@/utils/apiClient2`, constants from `apiRoutesPortalMasters`, `useSidebarContext` for `coId`/branch with the mounted-guard hydration pattern):

1. **Header form:** note date (default today), adjustment type toggle (Debit Note / Credit Note), remarks multiline, and an inward `Autocomplete`:
   - Options fetched from `DRCR_NOTE_ELIGIBLE_INWARDS?co_id=&branch_id=&search=` on open/debounced input.
   - Option label: `` `${o.inward_no} — ${o.supplier_name}` ``.
2. **On inward select:** GET `` `${apiRoutesPortalMasters.DRCR_NOTE_GET_INWARD_FOR_CREATE}/${inwardId}?co_id=${coId}` `` → show header chips (supplier, SR no, SR date) and a line table.
3. **Line table:** one row per inward line — read-only item/uom/accepted qty/accepted rate/invoice rate columns; per-row checkbox `include`; editable `debitnote_type` select (`1` Qty Rejection / `2` Rate Difference), `quantity` (default `approved_qty`), `rate` (default blank). Use the same `EditableNumberCell`/`Autocomplete` cell components as `SRLineItemsTable.tsx` if importable, else plain MUI `TextField type="number"` — do not build new shared components.
4. **Validation before save:** at least one included line; every included line `quantity > 0 && rate > 0`; toast on violation.
5. **Save:** POST `DRCR_NOTE_CREATE` with:

```ts
			const { data, error } = await fetchWithCookie(apiRoutesPortalMasters.DRCR_NOTE_CREATE, "POST", {
				inward_id: Number(selectedInwardId),
				note_date: noteDate, // "YYYY-MM-DD"
				adjustment_type: adjustmentType, // 1 | 2
				remarks: remarks || null,
				line_items: includedLines.map((l) => ({
					inward_dtl_id: l.inward_dtl_id,
					debitnote_type: l.debitnote_type,
					quantity: Number(l.quantity),
					rate: Number(l.rate),
				})),
			});
```

6. **On success:** toast with returned `note_no`, then `router.push(`/dashboardportal/procurement/drcrNote/view?id=${data.drcr_note_id}`)`.
7. Back button → `/dashboardportal/procurement/drcrNote`.

- [ ] **Step 4: Typecheck**

Run: `npx tsc --noEmit`
Expected: clean.

- [ ] **Step 5: Commit (FE repo)**

```bash
git add src/app/dashboardportal/procurement/drcrNote src/utils/api.ts
git commit -m "feat(procurement): manual DR/CR note create page + New Note action"
```

---

### Task 9: Full test suites + browser QA on dev3

**Files:** none new (fixes only if QA finds bugs).

- [ ] **Step 1: Full backend suite**

Run: `pytest src/test/ -v` (vowerp3be). Expected: no NEW failures vs main (57 pre-existing failures noted in memory — compare against a main-branch run if unsure).

- [ ] **Step 2: Browser QA** — dispatch the `portal-ui-flow-tester` agent (creds/registry per `.claude/test-credentials.md`, tenant dev3, FE :3000 + BE :8000 up) covering:
1. `procurement/sr/createSR` — PO-less inward: leave Invoice Rate blank → save → approve → assert NO DR/CR note created (check `drcr_note` table + list page).
2. Same page — set Invoice Rate above accepted rate → approve → debit note Draft appears with rate = diff; set below → credit note.
3. `procurement/drcrNote` — New Note → pick approved inward → include one line, qty+rate → save → lands on view page → Open → Approve → verify `drcr_note_dtl_gst` rows and accounting posting response.
4. Negative: create with zero qty/rate (expect blocked), create against unapproved-SR inward via direct URL (expect 400 toast).

- [ ] **Step 3: Fix anything QA surfaces**, commit fixes in the owning repo with `fix(procurement): ...` messages.

---

## Deploy checklist (after code review/merge — user-gated)

1. Run `add_invoice_rate_to_proc_inward_dtl.sql` on sls, amcl, and every other tenant DB (confirm list with user).
2. Run `delete_phantom_auto_credit_notes.sql` on sls (deletes 79: 78 Draft + 1 Open). Confirm with user before touching sls.
3. Deploy vowerp3be + vowerp3ui together (FE column useless without BE, BE harmless without FE).
4. Tell users: rate-diff notes now require entering Invoice Rate on the SR; blank = no auto note.
