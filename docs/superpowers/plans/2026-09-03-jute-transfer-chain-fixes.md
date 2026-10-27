# Jute Transfer Chain Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce the ERP ↔ juteTransfer contract decided on 2026-09-03 (D1–D7): party guard before Pending/Approved, freeze at Pending/Approved with a role-gated Reopen, valid rollback statuses on both sides, an explicit "sync issued weights" action, actual/accounts report bases, a Return-lorry status, and the Bill Pass / TDS recompute.

**Architecture:** Small guard helpers at the existing choke points (`mr.py` status endpoints, `update_mr`, gate-entry line rewrite, QC deactivate, juteTransfer `save_transfer_step`). No new tables. One data row (`status_mst` 48) and one view WHERE-clause change. Reports gain a `basis` query param instead of new menus. Issue sync is an explicit endpoint driven from the MR-wise Issue report, never automatic.

**Tech Stack:** FastAPI + SQLAlchemy `text()` (vowerp3be), Next.js + MUI (vowerp3ui), Streamlit + SQLAlchemy (juteTransfer), pytest, MySQL 8.

**Spec:** `c:\code\vowerp3be\docs\jute-transfer-chain-process-review.md` §6–§9 plus the owner rulings recorded in §"Owner rulings" below.

## Owner rulings (2026-09-03) — the spec for this plan

- **D1** Pending (13) is the hand-off state. juteTransfer may start a NEW chain only from a root at 13. Approved (3) rows stay visible in the juteTransfer overview for reference; the status is shown inline in that grid. Existing chains (root already has children) keep working regardless of root status.
- **D2** Reopen exists, only for users with **edit access** (`access_type_id >= 4`, the existing `_user_has_edit_access` rule); write-access users edit only while Open. Two targets: "Reopen for MR edit" (status → 1) and "Reopen for QC" (status → 1 and `qc_check = 0`, gate entry fields unlock again). Reopen is **blocked** when the MR has been sold onward in a vertical chain (any `jute_mr.src_jute_mr_id = this id`) or is itself a chain hop (`src_jute_mr_id IS NOT NULL`): the user must roll back in juteTransfer first. Chain rows are never touched by Reopen. If issues exist, Reopen **warns** (does not block) and tells the user to run "Sync issued weights" after re-finalising.
- **D3** juteTransfer un-finalize writes 13; delete-whole-chain writes 13. Never 0, never 1.
- **D4** Party AND party branch are required for Pending / Approved (no "no branch" sentinel).
- **D5** Two bases. Actual = today's reports. Accounts = receipt on `jute_mr_date`, approved weight, value = accepted × rate/100, issues dated before the MR date shifted to the MR date. Delivered as `basis=actual|accounts` on the Stock report and the Batch Cost report (toggle on the page, no new menu). Batch Cost is also fixed (drafts excluded → align with stock view; NULL `actual_rate`; NULL yarn type).
- **D6** No historical backfill now.
- **D7** Bill Pass no longer accepts totals from the body: total / claim / TDS are recomputed from lines on every update. TDS logic reviewed (see Task B1 notes).
- **Sync** "Sync issued weights" button on the Jute MR Wise Issue Detail report, per MR: issue.weight = quantity × (line actual_weight / actual_qty); `issue_value = weight/100 × actual_rate`; Approved issues updated in place.
- **Return lorry** New status 48 "RETURNED" for a lorry sent back without settlement; leaves stock and every report.
- **Moisture** MR line grid shows actual moisture as a caption beneath the allowable moisture cell.
- YAGNI, minimal diffs, agent teams on smaller models.

## Global Constraints

- Handlers are plain `def` (never `async def`); raw bodies via Pydantic models or `parse_json_body`.
- Responses are objects (`{"data": ...}` or `{"success": True, ...}`), never bare lists.
- Bind params: `None` for NULL; names match `:param` exactly; ints cast.
- sls is production: **SELECT-only** from this plan. Migrations run on **dev3 only**; sls run is a separate owner-approved step.
- No commits from workers. Do not run `git add` / `git commit`; the owner commits.
- No Alembic; DDL goes in `dbqueries/migrations/*.sql` with rollback comment; ORM in `src/models/`.
- Tests live in `src/test/test_{module}_{feature}.py`; run with `.venv/Scripts/python.exe -m pytest src/test/<file> -v` from `c:\code\vowerp3be`.
- FE: TypeScript must pass `npx tsc --noEmit -p tsconfig.json` in `c:\code\vowerp3ui` (eslint is broken repo-wide; do not fix it).
- juteTransfer tests: `python -m pytest tests -v` from `c:\code\juteTransfer`.
- Status ids: 21 Draft, 1 Open, 13 Pending, 20/17 Pending Approval, 3 Approved, 4 Rejected, 6 Cancelled, **48 Returned (new)**.

---

## File structure / ownership (parallel groups must not cross)

| Group | Files | Tasks |
|-------|-------|-------|
| A | `vowerp3be/src/juteProcurement/mr.py`, `constants.py`, `juteGateEntry.py`, `src/test/test_jute_mr_guards.py` | A1–A7 |
| B | `vowerp3be/src/juteProcurement/billPass.py`, `totals.py`, `src/test/test_jute_bill_pass_recompute.py` | B1 |
| C | `vowerp3be/src/juteProcurement/materialInspection.py`, `src/test/test_jute_qc_guards.py` | C1 |
| D | `vowerp3be/src/juteProcurement/issue.py`, `query.py`, `src/test/test_jute_issue_sync.py` | D1 |
| E | `vowerp3be/src/juteProcurement/reportQueries.py`, `reports.py`, `src/test/test_jute_report_basis.py` | E1–E3 |
| H | `vowerp3be/dbqueries/migrations/*.sql`, `src/models/jute.py` docstring | H1 |
| F | `vowerp3ui/src/app/dashboardportal/jutePurchase/mr/**`, `jutePurchase/reports/**`, `src/utils/api.ts`, `src/utils/juteReportService.ts` | F1–F3 |
| G | `juteTransfer/src/jutetransfer/{queries.py, transfer.py, warehouse_stock_ops.py, jute_mr_chain_helpers.py, pages/new_transfer_chain.py}`, `tests/test_root_eligibility.py` | G1–G2 |

Groups A–E and H are independent files inside vowerp3be and may run in parallel. F depends on the BE endpoint names defined in A6/A7/D1/E1/E2/E3 (listed in each task's **Produces**). G is a separate repo.

---

## Group A — MR status guards (`mr.py`)

### Task A1: Shared party-completeness guard on Pending / Approve / change_status

**Files:**
- Modify: `src/juteProcurement/mr.py` (constants block ~line 930; `change_mr_status` ~807-909; `pending_mr` ~1250-1326; `approve_mr` ~1396-1400)
- Test: `src/test/test_jute_mr_guards.py` (create)

**Interfaces:**
- Produces: `require_mr_party_complete(party_id, party_branch_id) -> None` (raises `HTTPException(400)`), module-level in `mr.py`.

- [ ] **Step 1: Write the failing tests**

```python
# src/test/test_jute_mr_guards.py
"""Guards added 2026-09-03 (jute transfer chain review, decisions D1-D4)."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement import mr as mr_module

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    yield db
    app.dependency_overrides.clear()


def _row(**cols):
    m = MagicMock()
    m._mapping = cols
    for k, v in cols.items():
        setattr(m, k, v)
    return m


class TestRequirePartyComplete:
    def test_missing_party_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mr_module.require_mr_party_complete(None, 5)
        assert exc.value.status_code == 400
        assert "Party" in exc.value.detail

    def test_missing_branch_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mr_module.require_mr_party_complete("123", None)
        assert exc.value.status_code == 400
        assert "branch" in exc.value.detail.lower()

    def test_complete_passes(self):
        assert mr_module.require_mr_party_complete("123", 5) is None


class TestPendingMrPartyGuard:
    def test_pending_blocked_without_branch(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(
            jute_mr_id=1, status_id=1, party_id="123", party_branch_id=None,
            src_jute_mr_id=None,
        )
        r = client.post("/api/juteMR/pending_mr", json={"mr_id": "1", "branch_id": "29"})
        assert r.status_code == 400
        assert "branch" in r.json()["detail"].lower()
        mock_db.commit.assert_not_called()


class TestApproveMrPartyGuard:
    def test_approve_blocked_without_party(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(
            jute_mr_id=1, status_id=1, party_id=None, party_branch_id=None,
            branch_id=29, branch_mr_no=None, jute_mr_date=None,
        )
        r = client.post("/api/juteMR/approve_mr", json={"mr_id": "1", "branch_id": "29", "mr_date": "2026-09-03"})
        assert r.status_code == 400
        assert "Party" in r.json()["detail"]


class TestChangeStatusPartyGuard:
    def test_change_status_to_approved_needs_party(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(
            jute_mr_id=1, status_id=1, party_id="123", party_branch_id=None,
        )
        r = client.put("/api/juteMR/change_status/1?co_id=2", json={"new_status_id": 3})
        assert r.status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_jute_mr_guards.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'require_mr_party_complete'` and 200s where 400s are expected.

- [ ] **Step 3: Implement the helper and wire it**

Add after the `MR_STATUS_*` constants (~line 936):

```python
MR_STATUS_RETURNED = 48  # lorry returned to supplier, no settlement (2026-09-03)


def require_mr_party_complete(party_id, party_branch_id) -> None:
    """Pending (13) and Approved (3) both need party AND party branch (decision D4).

    One helper, three call sites: pending_mr, approve_mr, change_mr_status.
    Do not re-implement this check inline anywhere else.
    """
    if not party_id:
        raise HTTPException(
            status_code=400,
            detail="Party must be selected before the MR can be set to Pending or Approved.",
        )
    if not party_branch_id:
        raise HTTPException(
            status_code=400,
            detail="Party branch must be selected before the MR can be set to Pending or Approved. "
                   "Add a branch for this party in Party Master if none exist.",
        )
```

In `pending_mr` (~1266-1272) change the check query to also select party columns and call the helper right after the status check:

```python
        check_query = text("""
            SELECT jute_mr_id, status_id, party_id, party_branch_id
            FROM jute_mr
            WHERE jute_mr_id = :mr_id
        """)
        ...
        if current_status not in (MR_STATUS_OPEN, MR_STATUS_PENDING_APPROVAL):
            raise HTTPException(...)  # existing
        require_mr_party_complete(mr.get("party_id"), mr.get("party_branch_id"))
```

In `approve_mr` replace the block at ~1395-1400 (`if party_id and not effective_party_branch_id: raise ...`) with:

```python
        require_mr_party_complete(party_id, effective_party_branch_id)
```

In `change_mr_status` replace the Draft-only guard at ~857-868 with:

```python
        if body.new_status_id in (MR_STATUS_APPROVED, MR_STATUS_PENDING) or (
            current_status_id == MR_STATUS_DRAFT and body.new_status_id != MR_STATUS_DRAFT
        ):
            require_mr_party_complete(party_id, party_branch_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_jute_mr_guards.py -v`
Expected: PASS (5 tests). Also run `.venv/Scripts/python.exe -m pytest src/test/test_jute_godown_party_branch.py -v` — must still pass.

### Task A2: Freeze `update_mr` and the gate-entry line rewrite once Pending/Approved or chained

**Files:**
- Modify: `src/juteProcurement/mr.py` (`update_jute_mr` ~619-655), `src/juteProcurement/juteGateEntry.py` (`jute_gate_entry_update`, the `line_items` branch ~1050)
- Test: `src/test/test_jute_mr_guards.py` (append)

**Interfaces:**
- Produces: `assert_mr_editable(db, mr_id, status_id=None) -> None` in `mr.py` (raises `HTTPException(409)`); `mr_chain_state(db, mr_id) -> dict(is_hop: bool, child_count: int)` in `mr.py`.

- [ ] **Step 1: Write the failing tests** (append to `test_jute_mr_guards.py`)

```python
class TestAssertMrEditable:
    def test_pending_is_frozen(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(is_hop=0, child_count=0)
        with pytest.raises(HTTPException) as exc:
            mr_module.assert_mr_editable(db, 1, status_id=13)
        assert exc.value.status_code == 409

    def test_approved_is_frozen(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(is_hop=0, child_count=0)
        with pytest.raises(HTTPException):
            mr_module.assert_mr_editable(db, 1, status_id=3)

    def test_open_root_with_children_is_frozen(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(is_hop=0, child_count=2)
        with pytest.raises(HTTPException) as exc:
            mr_module.assert_mr_editable(db, 1, status_id=1)
        assert "juteTransfer" in exc.value.detail

    def test_open_unchained_passes(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(is_hop=0, child_count=0)
        assert mr_module.assert_mr_editable(db, 1, status_id=1) is None

    def test_tolerates_tenant_without_chain_columns(self):
        db = MagicMock()
        db.execute.side_effect = Exception("Unknown column 'src_jute_mr_id'")
        assert mr_module.assert_mr_editable(db, 1, status_id=1) is None
        db.rollback.assert_called()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_jute_mr_guards.py -k Editable -v`
Expected: FAIL with AttributeError.

- [ ] **Step 3: Implement**

Add below `require_mr_party_complete` in `mr.py`:

```python
MR_FROZEN_STATUSES = (MR_STATUS_PENDING, MR_STATUS_APPROVED)

_CHAIN_STATE_SQL = text("""
    SELECT CASE WHEN m.src_jute_mr_id IS NOT NULL THEN 1 ELSE 0 END AS is_hop,
           (SELECT COUNT(*) FROM jute_mr c
             WHERE c.src_jute_mr_id = m.jute_mr_id AND c.jute_mr_id <> m.jute_mr_id) AS child_count
    FROM jute_mr m
    WHERE m.jute_mr_id = :mr_id
""")


def mr_chain_state(db: Session, mr_id: int) -> dict:
    """juteTransfer linkage of an MR. Tolerant of tenants that lack src_jute_mr_id
    (sls and dev3 have it; others may not): an SQL error means 'no chain feature'."""
    try:
        row = db.execute(_CHAIN_STATE_SQL, {"mr_id": int(mr_id)}).fetchone()
    except Exception:  # ponytail: unknown-column on tenants without the transfer schema
        db.rollback()
        return {"is_hop": False, "child_count": 0}
    if not row:
        return {"is_hop": False, "child_count": 0}
    return {"is_hop": bool(row.is_hop), "child_count": int(row.child_count or 0)}


def assert_mr_editable(db: Session, mr_id: int, status_id=None) -> None:
    """Decision D2: once Pending/Approved, or once a vertical chain exists on it,
    nothing on the MR may change from the ERP side except through reopen_mr."""
    if status_id in MR_FROZEN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="MR is Pending/Approved and locked. Use Reopen (edit access) to change it.",
        )
    chain = mr_chain_state(db, mr_id)
    if chain["is_hop"] or chain["child_count"]:
        raise HTTPException(
            status_code=409,
            detail="MR is part of a vertical transfer chain. Roll it back in juteTransfer first.",
        )
```

In `update_jute_mr`, right after `header = dict(header_result._mapping)` (~648):

```python
        assert_mr_editable(db, mr_id, header.get("status_id"))
```

In `juteGateEntry.py` inside `jute_gate_entry_update`, immediately before the `line_items` DELETE branch (~1050, `if payload.line_items` / the `DELETE FROM jute_mr_li` statement):

```python
            from src.juteProcurement.mr import assert_mr_editable  # local import: mr.py imports nothing from here
            assert_mr_editable(db, jute_mr_id, existing.status_id)
```

(`existing.status_id` is already fetched at ~776.)

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python.exe -m pytest src/test/test_jute_mr_guards.py src/test/test_jute_godown_party_branch.py -v`
Expected: PASS.

### Task A3: Reject / Cancel refuse when issues or chain children exist

**Files:**
- Modify: `src/juteProcurement/mr.py` (`reject_mr` ~1616-1715 both paths, `cancel_mr` ~1718-1788)
- Test: `src/test/test_jute_mr_guards.py` (append)

**Interfaces:**
- Produces: `live_issue_summary(db, mr_id) -> dict(count: int, weight: float)`; `assert_no_downstream(db, mr_id) -> None` (409).

- [ ] **Step 1: Write the failing tests**

```python
class TestRejectCancelGuards:
    def test_reject_blocked_when_issues_exist(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=1),          # status check
            _row(cnt=2, wt=500.0),                    # live_issue_summary
        ]
        r = client.post("/api/juteMR/reject_mr", json={"mr_id": "1", "branch_id": "29", "reason": "x"})
        assert r.status_code == 409
        assert "issue" in r.json()["detail"].lower()

    def test_cancel_blocked_when_chain_children(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=1),
            _row(cnt=0, wt=0.0),
            _row(is_hop=0, child_count=1),
        ]
        r = client.post("/api/juteMR/cancel_mr", json={"mr_id": "1", "branch_id": "29"})
        assert r.status_code == 409
        assert "chain" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run to verify failure** — `-k RejectCancel`; expected 200/other, not 409.

- [ ] **Step 3: Implement**

Add to `mr.py` after `assert_mr_editable`:

```python
_LIVE_ISSUES_SQL = text("""
    SELECT COUNT(*) AS cnt, COALESCE(SUM(ji.weight), 0) AS wt
    FROM jute_issue ji
    JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
    WHERE li.jute_mr_id = :mr_id AND ji.status_id <> 4
""")


def live_issue_summary(db: Session, mr_id: int) -> dict:
    row = db.execute(_LIVE_ISSUES_SQL, {"mr_id": int(mr_id)}).fetchone()
    return {"count": int(row.cnt or 0), "weight": float(row.wt or 0)} if row else {"count": 0, "weight": 0.0}


def assert_no_downstream(db: Session, mr_id: int) -> None:
    """Reject / Cancel / Return must not orphan issues or chain hops."""
    issues = live_issue_summary(db, mr_id)
    if issues["count"]:
        raise HTTPException(
            status_code=409,
            detail=f"{issues['count']} issue entries ({issues['weight']:.0f} kg) are drawn against this MR. "
                   "Reject/delete those issues first.",
        )
    chain = mr_chain_state(db, mr_id)
    if chain["is_hop"] or chain["child_count"]:
        raise HTTPException(
            status_code=409,
            detail="MR is part of a vertical transfer chain. Roll it back in juteTransfer first.",
        )
```

Wire it: in `reject_mr`, before `if menu_id is not None:` (both paths must be covered, so it goes before the branch) — the status check for the `process_rejection` path happens inside the utility, so first fetch status the same way the direct path does and call the guard once:

```python
        pre = db.execute(text("SELECT jute_mr_id, status_id FROM jute_mr WHERE jute_mr_id = :mr_id"), {"mr_id": mr_id}).fetchone()
        if not pre:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")
        assert_no_downstream(db, mr_id)
```

(Keep the existing direct-path status check as is.) In `cancel_mr`, after the `current_status != MR_STATUS_OPEN` check:

```python
        assert_no_downstream(db, mr_id)
```

- [ ] **Step 4: Run tests** — expected PASS.

### Task A4: Money recompute at Pending; `total_price` written with `accepted_weight`

**Files:**
- Modify: `src/juteProcurement/mr.py` (`update_jute_mr` per-line UPDATE ~698-741; `pending_mr` ~1290-1310; helper block near `calculate_mr_amounts`)
- Test: `src/test/test_jute_mr_guards.py` (append)

**Interfaces:**
- Produces: `recompute_mr_money(db, mr_id, tds_amount=None) -> dict` — writes `jute_mr_li.total_price` for all active lines and `jute_mr.total_amount/claim_amount/roundoff/net_total` (and `tds_amount` when given); returns the dict of stored values.

- [ ] **Step 1: Failing test**

```python
class TestRecomputeMrMoney:
    def test_writes_line_total_price_and_header(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(total_amount=50000.0, claim_amount=500.0)
        out = mr_module.recompute_mr_money(db, 1)
        assert out["net_total"] == 49500.0
        sqls = " ".join(str(c.args[0]) for c in db.execute.call_args_list)
        assert "total_price" in sqls and "net_total" in sqls
```

- [ ] **Step 2: Run** — FAIL (AttributeError).

- [ ] **Step 3: Implement**

Add after `calculate_tds_amount` in `mr.py`:

```python
_LINE_TOTAL_PRICE_SQL = text("""
    UPDATE jute_mr_li
    SET total_price = ROUND((COALESCE(accepted_weight, actual_weight, 0) / 100) * COALESCE(rate, 0), 2)
    WHERE jute_mr_id = :mr_id AND (active = 1 OR active IS NULL)
""")


def recompute_mr_money(db: Session, mr_id: int, tds_amount=None) -> dict:
    """One writer for the approved-weight money basis (decision D5/P5-1/P5-2):
    line total_price + header total_amount/claim_amount/roundoff/net_total.
    tds_amount None = keep the stored TDS (pending_mr); a float = store it (approve/bill pass)."""
    db.execute(_LINE_TOTAL_PRICE_SQL, {"mr_id": int(mr_id)})
    amounts = calculate_mr_amounts(db, mr_id)
    total_amount = round(amounts["total_amount"], 2)
    claim_amount = round(amounts["claim_amount"], 2)
    if tds_amount is None:
        row = db.execute(text("SELECT COALESCE(tds_amount, 0) AS t FROM jute_mr WHERE jute_mr_id = :mr_id"), {"mr_id": int(mr_id)}).fetchone()
        tds_amount = float(row.t or 0) if row else 0.0
    roundoff, net_total = compute_jute_totals(total_amount, claim_amount, tds_amount)
    db.execute(text("""
        UPDATE jute_mr
        SET total_amount = :total_amount, claim_amount = :claim_amount,
            tds_amount = :tds_amount, roundoff = :roundoff, net_total = :net_total
        WHERE jute_mr_id = :mr_id
    """), {"mr_id": int(mr_id), "total_amount": total_amount, "claim_amount": claim_amount,
           "tds_amount": round(float(tds_amount), 2), "roundoff": roundoff, "net_total": net_total})
    return {"total_amount": total_amount, "claim_amount": claim_amount,
            "tds_amount": round(float(tds_amount), 2), "roundoff": roundoff, "net_total": net_total}
```

In `update_jute_mr`'s per-line UPDATE (~698-722) add one SET line after `accepted_weight = :accepted_weight,`:

```sql
                    total_price = ROUND((COALESCE(:accepted_weight, :actual_weight, 0) / 100) * COALESCE(:rate, 0), 2),
```

In `pending_mr`, immediately before the `UPDATE jute_mr SET status_id = :new_status` execute:

```python
        recompute_mr_money(db, mr_id)  # Pending is the hand-off snapshot: carry real totals (P5-2)
```

- [ ] **Step 4: Run tests** — PASS.

### Task A5: `approve_mr` keeps an existing `bill_pass_no`

**Files:**
- Modify: `src/juteProcurement/mr.py` (~1500-1503 and the `SELECT` at ~1367-1372)
- Test: `src/test/test_jute_mr_guards.py` (append)

- [ ] **Step 1: Failing test**

```python
class TestApproveKeepsBillPassNo:
    def test_existing_bill_pass_no_is_kept(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=1, party_id="123", party_branch_id=5, branch_id=29,
                 branch_mr_no=50, jute_mr_date=None, bill_pass_no=80),
            _row(total_amount=1000.0, claim_amount=0.0),   # calculate_mr_amounts
            _row(cumulative_value=0.0),                    # cumulative
            _row(t=0.0),                                   # recompute tds read (if called)
        ]
        mock_db.execute.return_value.fetchone.return_value = None
        r = client.post("/api/juteMR/approve_mr", json={"mr_id": "1", "branch_id": "29", "mr_date": "2026-09-03"})
        assert r.status_code == 200
        sqls = " ".join(str(c.args[0]) for c in mock_db.execute.call_args_list)
        assert "MAX(bill_pass_no)" not in sqls
```

- [ ] **Step 2: Run** — FAIL (the MAX query is issued).

- [ ] **Step 3: Implement**

Add `bill_pass_no` to the header SELECT (~1368) and read `db_bill_pass_no = mr.get("bill_pass_no")`. Replace ~1500-1503:

```python
            if not db_bill_pass_no:
                new_bill_pass_no = get_next_bill_pass_no_for_fy(db, db_branch_id, parsed_mr_date)
            else:
                new_bill_pass_no = db_bill_pass_no  # re-approval after Reopen keeps the number (P2-2)
            update_fields.append("bill_pass_no = :bill_pass_no")
            update_params["bill_pass_no"] = new_bill_pass_no
```

Also replace the `calculate_mr_amounts` + manual totals block (~1505-1545) so `total_price` gets written too: keep the TDS computation, then call `recompute_mr_money(db, mr_id, tds_amount=tds_amount)` and drop the five `update_fields.append("total_amount..."/"claim_amount"/"roundoff"/"net_total"/"tds_amount")` lines (the helper writes them). Keep `calculated_amounts` for the response using the helper's return.

- [ ] **Step 4: Run** all of `test_jute_mr_guards.py` — PASS. Note: the mock ordering in the test may need adjusting to the exact `fetchone` sequence; the assertion that matters is the absence of `MAX(bill_pass_no)`.

### Task A6: `POST /juteMR/reopen_mr` (role-gated, chain-safe, warns on issues)

**Files:**
- Modify: `src/juteProcurement/mr.py` (new endpoint after `cancel_mr`; new Pydantic model next to `MRApprovalRequest`)
- Test: `src/test/test_jute_mr_guards.py` (append)

**Interfaces:**
- Produces: `POST /api/juteMR/reopen_mr` body `{"mr_id": str, "branch_id": str, "menu_id": int, "target": "open"|"qc"}` → `{"success": True, "mr_id", "status_id": 1, "target", "issues": {"count", "weight"}, "warning": str|None}`. 403 without edit access; 409 when hop/children; 400 if status not in (13, 3).

- [ ] **Step 1: Failing tests**

```python
class TestReopenMr:
    def _mr(self, status_id=13):
        return _row(jute_mr_id=1, status_id=status_id, branch_id=29)

    def test_requires_edit_access(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._mr()
        with patch.object(mr_module, "_user_has_edit_access", return_value=False):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "open"})
        assert r.status_code == 403

    def test_blocked_when_sold_onward(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [self._mr(), _row(is_hop=0, child_count=1)]
        with patch.object(mr_module, "_user_has_edit_access", return_value=True):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "open"})
        assert r.status_code == 409

    def test_reopen_for_qc_clears_qc_and_numbers_and_warns(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            self._mr(3), _row(is_hop=0, child_count=0), _row(cnt=3, wt=1200.0),
        ]
        with patch.object(mr_module, "_user_has_edit_access", return_value=True):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "qc"})
        assert r.status_code == 200
        body = r.json()
        assert body["status_id"] == 1 and body["issues"]["count"] == 3 and body["warning"]
        sqls = " ".join(str(c.args[0]) for c in mock_db.execute.call_args_list)
        assert "qc_check = 0" in sqls and "bill_pass_no = NULL" in sqls and "branch_mr_no" not in sqls.split("SET",1)[1]
```

- [ ] **Step 2: Run** — FAIL (404 route).

- [ ] **Step 3: Implement**

Import at top of `mr.py`: `from src.common.approval_utils import process_approval, process_rejection, calculate_approval_permissions, _user_has_edit_access`.

Model next to `MRApprovalRequest`:

```python
class MRReopenRequest(BaseModel):
    mr_id: str
    branch_id: str
    menu_id: int
    target: str = "open"  # "open" = MR editable again; "qc" = also redo QC (qc_check = 0)
```

Endpoint after `cancel_mr`:

```python
@router.post("/reopen_mr")
def reopen_mr(
    request: Request,
    body: MRReopenRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Decision D2: role-gated rollback of a Pending (13) / Approved (3) MR to Open (1).

    - edit access (access_type_id >= 4) on the MR menu is required
    - refused if the MR is a chain hop or has chain children (roll back in juteTransfer first)
    - clears what approval created (bill_pass_no/date, totals, TDS, approval_level); branch_mr_no is kept
    - target 'qc' also resets qc_check so Material Inspection can be redone
    - never blocks on issues: returns a warning so the user runs 'Sync issued weights' afterwards
    """
    try:
        mr_id = int(body.mr_id)
        branch_id = int(body.branch_id)
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")
        if body.target not in ("open", "qc"):
            raise HTTPException(status_code=400, detail="target must be 'open' or 'qc'")

        row = db.execute(text("SELECT jute_mr_id, status_id, branch_id FROM jute_mr WHERE jute_mr_id = :mr_id"), {"mr_id": mr_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")
        if row.status_id not in MR_FROZEN_STATUSES:
            raise HTTPException(status_code=400, detail="Only Pending or Approved MRs can be reopened")
        if not _user_has_edit_access(user_id, branch_id, body.menu_id, db):
            raise HTTPException(status_code=403, detail="Reopen needs edit access on the Jute MR menu")

        chain = mr_chain_state(db, mr_id)
        if chain["is_hop"] or chain["child_count"]:
            raise HTTPException(status_code=409, detail="MR has been transferred in a vertical chain. Roll back from juteTransfer first.")

        issues = live_issue_summary(db, mr_id)
        sets = [
            "status_id = :open", "approval_level = NULL",
            "bill_pass_no = NULL", "bill_pass_date = NULL",
            "total_amount = NULL", "claim_amount = NULL", "tds_amount = NULL", "roundoff = NULL", "net_total = NULL",
            "updated_by = :updated_by", "updated_date_time = :now",
        ]
        if body.target == "qc":
            sets.append("qc_check = 0")
        db.execute(text(f"UPDATE jute_mr SET {', '.join(sets)} WHERE jute_mr_id = :mr_id"),
                   {"mr_id": mr_id, "open": MR_STATUS_OPEN, "updated_by": user_id, "now": now_ist()})
        db.commit()

        warning = None
        if issues["count"]:
            warning = (f"{issues['count']} issue entries ({issues['weight']:.0f} kg) already drawn against this MR. "
                       "After you re-finalise, run 'Sync issued weights' on the Jute MR Wise Issue Detail report "
                       "so issued bales carry the updated weights.")
        return {"success": True, "mr_id": mr_id, "status_id": MR_STATUS_OPEN, "target": body.target,
                "issues": issues, "warning": warning}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error reopening MR")
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 4: Run tests** — PASS.

### Task A7: `POST /juteMR/return_mr` (lorry returned) + status constant

**Files:**
- Modify: `src/juteProcurement/mr.py` (endpoint after `reopen_mr`), `src/juteProcurement/constants.py:44`
- Test: `src/test/test_jute_mr_guards.py` (append)

**Interfaces:**
- Produces: `POST /api/juteMR/return_mr` body `{"mr_id", "branch_id", "reason"}` → `{"success": True, "status_id": 48, "status_name": "Returned"}`; allowed from 1 or 13; 409 via `assert_no_downstream`. `constants.JUTE_MR_STOCK_EXCLUDED_STATUSES == (4, 6, 21, 48)`.

- [ ] **Step 1: Failing test**

```python
class TestReturnMr:
    def test_return_from_open(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=1), _row(cnt=0, wt=0.0), _row(is_hop=0, child_count=0),
        ]
        r = client.post("/api/juteMR/return_mr", json={"mr_id": "1", "branch_id": "29", "reason": "no settlement"})
        assert r.status_code == 200 and r.json()["status_id"] == 48

    def test_excluded_from_stock_constant(self):
        from src.juteProcurement.constants import JUTE_MR_STOCK_EXCLUDED_STATUSES
        assert 48 in JUTE_MR_STOCK_EXCLUDED_STATUSES
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement**

`constants.py:44` → `JUTE_MR_STOCK_EXCLUDED_STATUSES = (4, 6, 21, 48)` and add above it: `JUTE_MR_RETURNED_STATUS = 48  # lorry returned to supplier without settlement (status_mst row added 2026-09-03)`. Update the comment block (lines ~40-43) to mention 48.

Endpoint:

```python
@router.post("/return_mr")
def return_mr(
    request: Request,
    body: MRApprovalRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Lorry came in but was sent back to the supplier (no settlement) → status 48 RETURNED.
    Allowed from Open (1) or Pending (13). Leaves stock via JUTE_MR_STOCK_EXCLUDED_STATUSES."""
    try:
        mr_id = int(body.mr_id)
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")
        row = db.execute(text("SELECT jute_mr_id, status_id FROM jute_mr WHERE jute_mr_id = :mr_id"), {"mr_id": mr_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")
        if row.status_id not in (MR_STATUS_OPEN, MR_STATUS_PENDING):
            raise HTTPException(status_code=400, detail="Only Open or Pending MRs can be marked Returned")
        assert_no_downstream(db, mr_id)
        reason = (body.reason or "").strip()
        db.execute(text("""
            UPDATE jute_mr
            SET status_id = :st, approval_level = NULL,
                remarks = CASE WHEN :reason = '' THEN remarks
                               ELSE CONCAT(COALESCE(remarks, ''), ' [Returned: ', :reason, ']') END,
                updated_by = :updated_by, updated_date_time = :now
            WHERE jute_mr_id = :mr_id
        """), {"mr_id": mr_id, "st": MR_STATUS_RETURNED, "reason": reason, "updated_by": user_id, "now": now_ist()})
        db.commit()
        return {"success": True, "mr_id": mr_id, "status_id": MR_STATUS_RETURNED, "status_name": "Returned"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error returning MR")
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 4: Run** the whole guards file + `src/test/test_jute_mr_stock_guard.py` + `src/test/test_jute_report_endpoint_guards.py` — PASS.

---

## Group B — Bill Pass recompute (D7) and TDS

### Task B1: Bill Pass ignores body totals; recomputes total / claim / TDS from lines

**Files:**
- Modify: `src/juteProcurement/billPass.py` (`update_jute_bill_pass` ~321-500), `src/juteProcurement/mr.py` only if a circular import appears (then move `calculate_mr_amounts`, `calculate_tds_amount`, `get_cumulative_mr_value_for_party_in_fy`, `TDS_THRESHOLD`, `TDS_RATE` into `src/juteProcurement/totals.py` and re-import them in `mr.py`).
- Test: `src/test/test_jute_bill_pass_recompute.py` (create)

**Interfaces:**
- Consumes: `recompute_mr_money(db, mr_id, tds_amount)` (A4), `calculate_tds_amount`, `get_cumulative_mr_value_for_party_in_fy` from `mr.py`.

**TDS review result (for the record, no code change beyond the recompute):** `calculate_tds_amount` is correct 194Q incremental logic on gross `total_amount` (docstring wrongly says net — fix the docstring). On sls FY26-27 every one of the 80 approved MRs without TDS is a juteTransfer-written row (57 hops + 23 finalized roots) that never passed through `approve_mr`; Bill Pass recomputing TDS on save closes that for any root that reaches Bill Pass. Hop MRs never reach Bill Pass; that is a juteTransfer gap to raise with the owner, not fixed here.

- [ ] **Step 1: Failing test**

```python
# src/test/test_jute_bill_pass_recompute.py
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement import billPass as bp

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    yield db
    app.dependency_overrides.clear()


def _row(**cols):
    m = MagicMock(); m._mapping = cols
    for k, v in cols.items(): setattr(m, k, v)
    return m


def test_body_totals_are_ignored_and_recomputed(mock_db):
    mock_db.execute.return_value.fetchone.return_value = _row(
        jute_mr_id=1, status_id=3, total_amount=1.0, claim_amount=1.0, tds_amount=1.0,
        party_id="123", jute_mr_date=__import__("datetime").date(2026, 9, 3),
    )
    with patch.object(bp, "get_cumulative_mr_value_for_party_in_fy", return_value=0.0), \
         patch.object(bp, "recompute_mr_money", return_value={"total_amount": 50000.0, "claim_amount": 0.0,
                                                            "tds_amount": 0.0, "roundoff": 0.0, "net_total": 50000.0}) as rc, \
         patch.object(bp, "calculate_mr_amounts", return_value={"total_amount": 50000.0, "claim_amount": 0.0}):
        r = client.put("/api/billPass/update_bill_pass/1?co_id=2",
                       json={"total_amount": 95000, "claim_amount": 5, "tds_amount": 9})
    assert r.status_code == 200
    rc.assert_called_once()
    sqls = " ".join(str(c.args[0]) for c in mock_db.execute.call_args_list)
    assert ":total_amount" not in sqls.split("UPDATE jute_mr")[-1] or "recompute" in sqls  # body totals never written directly
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement**

At top of `billPass.py`: `from src.juteProcurement.mr import calculate_mr_amounts, calculate_tds_amount, get_cumulative_mr_value_for_party_in_fy, recompute_mr_money`. If Python raises `ImportError` (circular), move those three functions + `TDS_THRESHOLD`/`TDS_RATE` to `totals.py` and import them from there in both files (keep names).

In `update_jute_bill_pass`: extend `verify_query` to also select `jm.party_id, jm.jute_mr_date`. Delete the three `if body.total_amount / claim_amount / tds_amount is not None` blocks and the whole `recompute_needed` block (~402-444). After `db.execute(update_query, update_params); db.commit()` is **not** the place — do the recompute inside the same transaction, before the UPDATE of the other fields:

```python
        # Decision D7: totals are never taken from the client. Recompute from lines,
        # and re-derive 194Q TDS from the party's cumulative approved value in the FY.
        amounts = calculate_mr_amounts(db, bill_pass_id)
        cumulative_previous = 0.0
        if stored.get("party_id") and stored.get("jute_mr_date"):
            cumulative_previous = get_cumulative_mr_value_for_party_in_fy(
                db, stored["party_id"], stored["jute_mr_date"], exclude_mr_id=bill_pass_id
            )
        tds_amount, _, _ = calculate_tds_amount(cumulative_previous, amounts["total_amount"])
        recompute_mr_money(db, bill_pass_id, tds_amount=tds_amount)
```

Keep `BillPassUpdate`'s `total_amount/claim_amount/tds_amount/net_total/roundoff` fields (the FE still sends them) but they are now ignored — add a one-line comment on the model saying so. Fix `calculate_tds_amount`'s docstring in `mr.py`: "0.1% of the gross total_amount".

- [ ] **Step 4: Run** `src/test/test_jute_bill_pass_recompute.py` and `src/test/test_jute_mr_bp_formatters.py` — PASS.

---

## Group C — QC (`materialInspection.py`)

### Task C1: QC writes `total_price`; refuses to deactivate a line with live issues

**Files:**
- Modify: `src/juteProcurement/materialInspection.py` (line UPDATE that writes `accepted_weight` ~804-830; deactivate block ~951-966)
- Test: `src/test/test_jute_qc_guards.py` (create)

- [ ] **Step 1: Failing test**

```python
# src/test/test_jute_qc_guards.py
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    yield db
    app.dependency_overrides.clear()


def test_complete_inspection_blocks_deactivating_issued_line(mock_db):
    header = MagicMock(); header._mapping = {"jute_mr_id": 1, "qc_check": 0, "branch_id": 29}
    issued = MagicMock(); issued.cnt = 1
    mock_db.execute.return_value.fetchone.side_effect = [header, issued]
    mock_db.execute.return_value.fetchall.return_value = []
    r = client.post("/api/materialInspection/complete_inspection?co_id=2",
                    json={"jute_mr_id": 1, "line_items": [], "lines_cleared": True, "save_only": False})
    assert r.status_code == 409
```

(Adjust the exact payload keys to `MaterialInspectionComplete`'s model in the file; the assertion is the 409.)

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement**

In the per-line UPDATE that sets `accepted_weight` (~804-830) add `total_price = ROUND((COALESCE(:accepted_weight, :actual_weight, 0) / 100) * COALESCE(:rate, 0), 2),` (bind names must match the ones already used in that statement; if `rate` is not bound there, add it from the line payload).

Before the deactivate UPDATE (~955), guard the dropped lines:

```python
            dropped_with_issues = db.execute(text(f"""
                SELECT COUNT(*) AS cnt
                FROM jute_issue ji
                JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
                WHERE li.jute_mr_id = :jute_mr_id AND ji.status_id <> 4
                  {"AND li.jute_mr_li_id NOT IN (" + ", ".join(f":keep_{i}" for i in range(len(kept_li_ids))) + ")" if kept_li_ids else ""}
            """), {"jute_mr_id": jute_mr_id, **{f"keep_{i}": li_id for i, li_id in enumerate(kept_li_ids)}}).fetchone()
            if dropped_with_issues and int(dropped_with_issues.cnt or 0):
                raise HTTPException(status_code=409, detail="A removed line already has issue entries against it. Reject those issues first.")
```

- [ ] **Step 4: Run** `src/test/test_jute_qc_guards.py` + `src/test/test_jute_godown_party_branch.py` — PASS.

---

## Group D — Issue sync

### Task D1: `POST /juteIssue/sync_issue_weights`

**Files:**
- Modify: `src/juteProcurement/issue.py` (new model + endpoint at end of file), `src/juteProcurement/query.py` (new query next to `get_jute_mr_line_for_issue_query` ~2335)
- Test: `src/test/test_jute_issue_sync.py` (create)

**Interfaces:**
- Produces: `POST /api/juteIssue/sync_issue_weights?co_id=` body `{"jute_mr_id": int, "branch_id": int}` → `{"data": [per-issue before/after], "totals": {...}}`.

- [ ] **Step 1: Failing test**

```python
# src/test/test_jute_issue_sync.py
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7, "sub": "qa"}
    yield db
    app.dependency_overrides.clear()


def _r(**c):
    m = MagicMock()
    for k, v in c.items(): setattr(m, k, v)
    return m


def test_sync_rescales_from_bale_unit_weight(mock_db):
    mock_db.execute.return_value.fetchall.return_value = [
        _r(jute_mr_li_id=10, actual_qty=104.0, actual_weight=4168.0, actual_rate=4500.0,
           jute_issue_id=87688, quantity=22.0, weight=900.0, issue_value=40500.0, status_id=3),
    ]
    r = client.post("/api/juteIssue/sync_issue_weights?co_id=2", json={"jute_mr_id": 5, "branch_id": 29})
    assert r.status_code == 200
    row = r.json()["data"][0]
    assert row["weight_after"] == 881.69 and row["issue_value_after"] == 39676.05
    assert r.json()["totals"]["issues_changed"] == 1


def test_sync_scales_down_when_no_qty_ratio(mock_db):
    mock_db.execute.return_value.fetchall.return_value = [
        _r(jute_mr_li_id=10, actual_qty=0, actual_weight=1000.0, actual_rate=100.0,
           jute_issue_id=1, quantity=0, weight=800.0, issue_value=800.0, status_id=1),
        _r(jute_mr_li_id=10, actual_qty=0, actual_weight=1000.0, actual_rate=100.0,
           jute_issue_id=2, quantity=0, weight=400.0, issue_value=400.0, status_id=1),
    ]
    r = client.post("/api/juteIssue/sync_issue_weights?co_id=2", json={"jute_mr_id": 5, "branch_id": 29})
    rows = r.json()["data"]
    assert [x["weight_after"] for x in rows] == [666.67, 333.33]
```

- [ ] **Step 2: Run** — FAIL (404).

- [ ] **Step 3: Implement**

`query.py` (after `get_jute_mr_line_for_issue_query`):

```python
def get_jute_mr_lines_and_issues_for_sync_query():
    """All lines of one MR plus their non-rejected issues (sync_issue_weights)."""
    return text(f"""
        SELECT jml.jute_mr_li_id, jml.actual_qty, jml.actual_weight, jml.actual_rate,
               ji.jute_issue_id, ji.quantity, ji.weight, ji.issue_value, ji.status_id
        FROM jute_mr_li jml
        JOIN jute_mr jm ON jm.jute_mr_id = jml.jute_mr_id
        JOIN branch_mst bm ON bm.branch_id = jm.branch_id AND bm.co_id = :co_id
        LEFT JOIN jute_issue ji
               ON ji.jute_mr_li_id = jml.jute_mr_li_id
              AND ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}
        WHERE jml.jute_mr_id = :jute_mr_id
          AND jm.branch_id = :branch_id
          AND (jml.active = 1 OR jml.active IS NULL)
        ORDER BY jml.jute_mr_li_id, ji.jute_issue_id
    """)
```

(`JUTE_ISSUE_EXCLUDED_STATUS` is already imported in `query.py`; if not, import from `src.juteProcurement.constants`.)

`issue.py` — model and endpoint (uses the code sketch from the recon, adapted):

```python
class JuteIssueSyncWeights(BaseModel):
    jute_mr_id: int
    branch_id: int


@router.post("/sync_issue_weights")
def sync_issue_weights(
    body: JuteIssueSyncWeights,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Re-derive weight/issue_value of every non-rejected issue on one MR from the line's
    CURRENT actual_weight/actual_qty (bale unit weight). Deliberately updates Approved issues
    too — this is the explicit, user-triggered sync after an MR correction (owner ruling 2026-09-03)."""
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)
        rows = db.execute(get_jute_mr_lines_and_issues_for_sync_query(),
                          {"co_id": co_id, "jute_mr_id": body.jute_mr_id, "branch_id": body.branch_id}).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="MR not found for this branch/company")
        lines: dict = {}
        for r in rows:
            line = lines.setdefault(r.jute_mr_li_id, {
                "aq": float(r.actual_qty or 0), "aw": float(r.actual_weight or 0),
                "rate": float(r.actual_rate or 0), "issues": []})
            if r.jute_issue_id is not None:
                line["issues"].append(r)
        now = now_ist()
        upd = text("""UPDATE jute_issue SET weight = :w, issue_value = :v, updated_by = :u, update_date_time = :t
                      WHERE jute_issue_id = :id""")
        results, changed = [], 0
        for li_id, line in lines.items():
            if not line["issues"]:
                continue
            unit_wt = line["aw"] / line["aq"] if line["aq"] > 0 else None
            sum_w = sum(float(i.weight or 0) for i in line["issues"])
            for i in line["issues"]:
                qty = float(i.quantity or 0)
                before_w, before_v = float(i.weight or 0), float(i.issue_value or 0)
                if unit_wt is not None and qty > 0:
                    new_w = round(qty * unit_wt, 2)
                elif sum_w > line["aw"] > 0:
                    new_w = round(before_w * line["aw"] / sum_w, 2)  # no bale ratio: scale down to fit
                else:
                    new_w = before_w
                new_v = round(new_w / 100 * line["rate"], 2)
                if new_w != before_w or new_v != before_v:
                    db.execute(upd, {"w": new_w, "v": new_v, "u": token_data.get("user_id"), "t": now, "id": i.jute_issue_id})
                    changed += 1
                results.append({"jute_issue_id": i.jute_issue_id, "jute_mr_li_id": li_id, "quantity": qty,
                                "status_id": i.status_id, "weight_before": before_w, "weight_after": new_w,
                                "issue_value_before": before_v, "issue_value_after": new_v})
        db.commit()
        return {"data": results, "totals": {
            "lines_processed": len(lines), "issues_processed": len(results), "issues_changed": changed,
            "weight_before": round(sum(x["weight_before"] for x in results), 2),
            "weight_after": round(sum(x["weight_after"] for x in results), 2)}}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error syncing issue weights: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error syncing issue weights: {e}")
```

Check `jute_issue.updated_by` type in `src/models/jute.py` (JuteIssueDev3); if it is a string column, bind `token_data.get("sub", "system")` instead of `user_id`.

- [ ] **Step 4: Run** `src/test/test_jute_issue_sync.py` + `src/test/test_jute_issue.py` — PASS.

---

## Group E — Reports

### Task E1: Stock report `basis=actual|accounts`

**Files:**
- Modify: `src/juteProcurement/reportQueries.py` (`_receipt_cte` ~92-130, `_issue_cte` ~133-176, `get_jute_stock_report_query` ~216-292), `src/juteProcurement/reports.py` (`GET /stock` ~49-96)
- Test: `src/test/test_jute_report_basis.py` (create)

**Interfaces:**
- Produces: `get_jute_stock_report_query(basis="actual")`; `GET /api/juteReports/stock?branch_id&date&basis=actual|accounts` (default actual, 400 otherwise).

- [ ] **Step 1: Failing test**

```python
# src/test/test_jute_report_basis.py
from src.juteProcurement import reportQueries as rq


def test_stock_accounts_basis_uses_mr_date_and_accepted_weight():
    sql = str(rq.get_jute_stock_report_query(basis="accounts"))
    assert "GREATEST(ji.issue_date, jm.jute_mr_date)" in sql
    assert "COALESCE(mrli.accepted_weight, 0)" in sql
    assert "(COALESCE(mrli.accepted_weight, 0) / 100) * COALESCE(mrli.rate, 0)" in sql


def test_stock_actual_basis_unchanged():
    sql = str(rq.get_jute_stock_report_query())
    assert "GREATEST(" not in sql and "COALESCE(mrli.actual_weight, 0)" in sql
```

- [ ] **Step 2: Run** — FAIL (TypeError: unexpected kwarg).

- [ ] **Step 3: Implement**

`_receipt_cte(period, basis="actual")`: at the top

```python
    if basis == "accounts":
        date_expr, wt_expr = "jm.jute_mr_date", "COALESCE(mrli.accepted_weight, 0)"
        val_expr = "(COALESCE(mrli.accepted_weight, 0) / 100) * COALESCE(mrli.rate, 0)"
    else:
        date_expr, wt_expr, val_expr = _MR_RECEIPT_DATE, "COALESCE(mrli.actual_weight, 0)", "COALESCE(mrli.total_price, 0)"
```

and substitute `{wt_expr} AS wt`, `{val_expr} AS val`, `{period.format(d=date_expr)}` in the MR arm only (waste arm untouched).

`_issue_cte(period, basis="actual")`: `issue_date_expr = "GREATEST(ji.issue_date, jm.jute_mr_date)" if basis == "accounts" else "ji.issue_date"` and use `{period.format(d=issue_date_expr)}` in the MR-backed arm only (the waste arm has no `jm`).

`get_jute_stock_report_query(basis="actual")`: thread `basis` into all `_receipt_cte(..., basis)` / `_issue_cte(..., basis)` calls. `_sold_cte` unchanged.

`reports.py` `GET /stock`: after the date validation

```python
        q_basis = (request.query_params.get("basis") or "actual").lower()
        if q_basis not in ("actual", "accounts"):
            raise HTTPException(status_code=400, detail="basis must be 'actual' or 'accounts'")
        ...
        query = get_jute_stock_report_query(basis=q_basis)
```

- [ ] **Step 4: Run** `src/test/test_jute_report_basis.py` + `src/test/test_jute_report_endpoint_guards.py` — PASS.

### Task E2: Batch Cost report fix + `basis`

**Files:**
- Modify: `src/juteProcurement/reportQueries.py` (`get_batch_cost_report_query` ~295-402), `src/juteProcurement/reports.py` (`GET /batch-cost` ~99-144)
- Test: `src/test/test_jute_report_basis.py` (append)

Defects being fixed (verified on sls 2026-09-03): (1) `ji.status_id IN (1, 3)` hides every Draft issue while the stock view counts them — align to `<> 4`; (2) `AVG(mrli.actual_rate)` is unweighted and `actual_rate` is NULL on 99.5 % of lines — derive the rate from `SUM(issue_value)/SUM(weight)*100`; (3) NULL `yarn_type_id` rows fall into a blank yarn-type bucket — label them.

- [ ] **Step 1: Failing test**

```python
def test_batch_cost_counts_draft_issues_and_weighted_rate():
    sql = str(rq.get_batch_cost_report_query())
    assert "ji.status_id IN (1, 3)" not in sql
    assert "SUM(COALESCE(ji.issue_value, 0)) / SUM(ji.weight) * 100" in sql
    assert "'(unassigned)'" in sql


def test_batch_cost_accounts_basis_shifts_issue_date():
    sql = str(rq.get_batch_cost_report_query(basis="accounts"))
    assert "GREATEST(ji.issue_date, jm.jute_mr_date)" in sql
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement**

Signature `get_batch_cost_report_query(basis: str = "actual")`. Build once:

```python
    issue_status = f"ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}"  # same rule as vw_jute_stock_outstanding
    if basis == "accounts":
        mr_join = "LEFT JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id LEFT JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id"
        date_expr = "COALESCE(GREATEST(ji.issue_date, jm.jute_mr_date), ji.issue_date)"
        value_expr = "ROUND(COALESCE(SUM((ji.weight / 100) * COALESCE(mrli.rate, ji.issue_value / NULLIF(ji.weight, 0) * 100)), 0), 2) AS issue_value"
    else:
        mr_join = "LEFT JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id"
        date_expr = "ji.issue_date"
        value_expr = "ROUND(COALESCE(SUM(ji.issue_value), 0), 2) AS issue_value"
```

Then in the SQL: `yarn_totals` gets `{mr_join}` and `WHERE ji.branch_id = :branch_id AND {date_expr} = :report_date AND {issue_status}`; `actual` uses `{mr_join}`, the same WHERE, `ROUND(CASE WHEN SUM(ji.weight) > 0 THEN SUM(COALESCE(ji.issue_value, 0)) / SUM(ji.weight) * 100 ELSE 0 END, 2) AS avg_rate`, and `{value_expr}`; both yarn-name expressions become `COALESCE(yim.item_name, jym.jute_yarn_name, '(unassigned)') AS yarn_type_name`. `bda.status_id IN (1, 3)` stays (different table). `reports.py` `GET /batch-cost`: same `basis` validation as E1 and pass it through.

- [ ] **Step 4: Run** the basis tests + `src/test/test_jute_report_endpoint_guards.py` — PASS.

### Task E3: MR Wise Issue Detail — reconciliation columns

**Files:**
- Modify: `src/juteProcurement/reportQueries.py` (`get_jute_mr_issue_detail_query` ~1475-1557)
- Test: `src/test/test_jute_report_basis.py` (append)

**Interfaces:**
- Produces: new columns `accepted_weight`, `unit_weight` (= actual_weight/actual_qty), `total_issued_weight` (per line, all non-rejected issues), `expected_issued_weight` (= Σ quantity × unit_weight), `weight_diff` (= total_issued − expected), `line_issue_count`.

- [ ] **Step 1: Failing test**

```python
def test_mr_issue_detail_has_reconciliation_columns():
    sql = str(rq.get_jute_mr_issue_detail_query(has_mr_filter=True, has_date_filter=False))
    for col in ("accepted_weight", "unit_weight", "total_issued_weight", "expected_issued_weight", "weight_diff"):
        assert f"AS {col}" in sql
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** — in the SELECT list after `actual_rate` add:

```sql
            ROUND(mrli.accepted_weight, 3) AS accepted_weight,
            ROUND(mrli.actual_weight / NULLIF(mrli.actual_qty, 0), 4) AS unit_weight,
            agg.line_issue_count,
            agg.total_issued_weight,
            agg.expected_issued_weight,
            ROUND(agg.total_issued_weight - agg.expected_issued_weight, 3) AS weight_diff,
```

and add a per-line aggregate join (after the `jute_mr_li mrli` join):

```sql
        LEFT JOIN (
            SELECT ji2.jute_mr_li_id,
                   COUNT(*) AS line_issue_count,
                   ROUND(SUM(ji2.weight), 3) AS total_issued_weight,
                   ROUND(SUM(ji2.quantity * l2.actual_weight / NULLIF(l2.actual_qty, 0)), 3) AS expected_issued_weight
            FROM jute_issue ji2
            JOIN jute_mr_li l2 ON l2.jute_mr_li_id = ji2.jute_mr_li_id
            WHERE ji2.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}
            GROUP BY ji2.jute_mr_li_id
        ) agg ON agg.jute_mr_li_id = mrli.jute_mr_li_id
```

- [ ] **Step 4: Run** basis tests + `src/test/test_jute_mr_issue_report.py` — PASS (update column-shape assertions there if they enumerate columns).

---

## Group H — Data / view migration (dev3 only from this plan)

### Task H1: `status_mst` 48 + `vw_jute_stock_outstanding` exclusion

**Files:**
- Create: `dbqueries/migrations/add_jute_mr_returned_status_48.sql`
- Modify: `src/models/jute.py` docstring at ~707 (`NOT IN (4, 6, 21)` → `(4, 6, 21, 48)`)

- [ ] **Step 1: Write the migration** (copy the full `CREATE OR REPLACE VIEW vw_jute_stock_outstanding AS ...` body verbatim from `dbqueries/migrations/add_sales_dtl_jute_mr_li_link_and_view_sold.sql:100-170`, changing only the WHERE):

```sql
-- add_jute_mr_returned_status_48.sql (2026-09-03)
-- New jute_mr status 48 RETURNED: lorry came in, sent back to supplier without settlement.
-- Must leave stock: excluded in constants.JUTE_MR_STOCK_EXCLUDED_STATUSES (code) AND in the view (here).
-- Run on dev3 first; sls only after owner approval; any tenant on the 2026-08-23 view definition.
-- Rollback:
--   DELETE FROM status_mst WHERE status_id = 48;
--   re-run dbqueries/migrations/add_sales_dtl_jute_mr_li_link_and_view_sold.sql section 3 (view only).
INSERT INTO status_mst (status_id, status_name)
SELECT 48, 'RETURNED' FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM status_mst WHERE status_id = 48);

CREATE OR REPLACE VIEW vw_jute_stock_outstanding AS
<verbatim SELECT from add_sales_dtl_jute_mr_li_link_and_view_sold.sql lines 100-165>
WHERE jm.status_id NOT IN (4, 6, 21, 48)
<verbatim remainder of that WHERE (jml.active / jml.status predicates) unchanged>;
```

Check `status_mst`'s other NOT NULL columns with `DESCRIBE status_mst` on dev3 before finalising the INSERT column list.

- [ ] **Step 2: Run on dev3** via the `run-migration` skill (target dev3). Verify: `SELECT status_id, status_name FROM status_mst WHERE status_id = 48;` and `SHOW CREATE VIEW vw_jute_stock_outstanding` contains `48`.
- [ ] **Step 3: Do NOT run on sls.** Record in the final report that sls needs the same migration before Return lorry can be used there.

---

## Group F — Frontend (vowerp3ui)

### Task F1: MR approval bar — party mirror, Reopen, Return lorry, Returned status

**Files:**
- Modify: `src/utils/api.ts` (~316-327: add `JUTE_MR_REOPEN: \`${API_URL}/juteMR/reopen_mr\``, `JUTE_MR_RETURN: \`${API_URL}/juteMR/return_mr\``), `jutePurchase/mr/utils/mrConstants.ts` (add `RETURNED: 48` + label `"Returned"`), `jutePurchase/mr/utils/mrService.ts` (add `reopenMR`, `returnMR`), `jutePurchase/mr/hooks/useMRApproval.ts`, `jutePurchase/mr/components/MRApprovalBar.tsx`, create `jutePurchase/mr/components/MRReopenDialog.tsx`.

**Interfaces:**
- Consumes: A6 `POST reopen_mr` `{mr_id, branch_id, menu_id, target}` → `{status_id, issues:{count,weight}, warning}`; A7 `POST return_mr` `{mr_id, branch_id, reason}`.

- [ ] **Step 1: Service functions** (`mrService.ts`, same template as `rejectMR`):

```ts
export interface ReopenResponse extends StatusChangeResponse { target: "open" | "qc"; issues: { count: number; weight: number }; warning: string | null }
export async function reopenMR(mrId: string, branchId: string, target: "open" | "qc", menuId: string | null): Promise<ReopenResponse> {
	const payload = { mr_id: mrId, branch_id: branchId, target, menu_id: Number(menuId) };
	const { data, error } = await fetchWithCookie<ReopenResponse>(apiRoutesPortalMasters.JUTE_MR_REOPEN, "POST", payload);
	if (error) throw new Error(error);
	if (!data) throw new Error("Empty response from reopen API");
	return data;
}
export async function returnMR(mrId: string, branchId: string, reason: string): Promise<StatusChangeResponse> {
	const { data, error } = await fetchWithCookie<StatusChangeResponse>(apiRoutesPortalMasters.JUTE_MR_RETURN, "POST", { mr_id: mrId, branch_id: branchId, reason });
	if (error) throw new Error(error);
	if (!data) throw new Error("Empty response from return API");
	return data;
}
```

- [ ] **Step 2: Party guard mirror in `useMRApproval.ts`**: `canSetPending` (~189-191) becomes `mode !== "view" && approvalPermissions.canApprove && isFinalLevel && !!header?.party_id && !!header?.party_branch_id`; in `handleApprove` (~322-330) replace the `header?.party_id &&` short-circuit with an unconditional check that both `party_id` and the effective `party_branch_id` are set, surfacing the same message as the backend.

- [ ] **Step 3: Reopen** — new `MRReopenDialog.tsx` (clone `MRApprovalDialog.tsx`'s Dialog shell): a `RadioGroup` with `open` ("Reopen for MR edit") / `qc` ("Reopen for QC — gate entry and inspection can be redone"), an `Alert severity="warning"` that shows `warning` when provided, Confirm/Cancel. In `useMRApproval.ts` add `reopenDialogOpen` state, `handleReopenClick`, `handleReopenConfirm(target)` → `reopenMR(...)` → on success store `data.warning` in a `reopenWarning` state shown by the page (`setPageError`-style but as a warning `Alert`) and `await onRefresh()`. Gate: `canReopen = (statusId === MR_STATUS_IDS.PENDING || statusId === MR_STATUS_IDS.APPROVED) && hasMenuAccess(pathname, "edit")` using `useSidebarContext().hasMenuAccess` + `usePathname()` (pattern: `juteProduction/spinning/_components/DailyDoffGrid.tsx:46-51`). In `MRApprovalBar.tsx` render a "Reopen" button next to the custom Pending button when `canReopen`.

- [ ] **Step 4: Return lorry** — in `MRApprovalBar.tsx` add a "Return lorry" button visible when `statusId` is OPEN or PENDING and `permissions.canReject` (same audience as Reject); confirm dialog copied from `ApprovalActionsBar.tsx:340-372` (reason required); handler `handleReturn(reason)` in the hook → `returnMR` → `onRefresh()`.

- [ ] **Step 5: Returned status** — `mrConstants.ts` `MR_STATUS_IDS.RETURNED = 48`, `MR_STATUS_LABELS[48] = "Returned"`; `useMRApproval.ts` permissions branch for 48 = same as CANCELLED (view log only); status chip color `error`; `MRStatusId` union gets `| 48` if it is a literal union.

- [ ] **Step 6: Verify** — `npx tsc --noEmit -p tsconfig.json` passes; open `http://localhost:3000/dashboardportal/jutePurchase/mr` on dev3 and confirm the buttons render per status (manual, or via the `portal-ui-flow-tester` agent).

### Task F2: Actual moisture caption under allowable moisture

**Files:**
- Modify: `jutePurchase/mr/hooks/useMRLineItems.tsx` (`allowableMoisture` column ~116-149)

- [ ] **Step 1:** Wrap the existing `TextField` (and the read-only span branch) in `<Box sx={{ display: "flex", flexDirection: "column" }}>` and append `<Typography variant="caption" color="text.secondary">Actual: {item.actualMoisture != null ? Number(item.actualMoisture).toFixed(2) : "-"}%</Typography>`. Keep the `actualMoisture` column as is (still editable).
- [ ] **Step 2:** `npx tsc --noEmit -p tsconfig.json` passes.

### Task F3: Reports — basis toggles + Sync issued weights button

**Files:**
- Modify: `src/utils/juteReportService.ts` (`fetchJuteStockReport`, `fetchBatchCostReport` gain `basis` param; add `syncIssueWeights(coId, mrId, branchId)` POST to `${API_URL}/juteIssue/sync_issue_weights?co_id=`), `src/utils/api.ts` (add `JUTE_ISSUE_SYNC_WEIGHTS`), `jutePurchase/reports/juteStock/page.tsx`, `jutePurchase/reports/batchCost/page.tsx`, `jutePurchase/reports/mrIssueDetail/page.tsx`, `jutePurchase/reports/types/reportTypes.ts` (`JuteMrIssueDetailRow` + new fields).

**Interfaces:**
- Consumes: E1/E2 `?basis=`; E3 columns; D1 sync endpoint.

- [ ] **Step 1: Toggles** — in `juteStock/page.tsx` and `batchCost/page.tsx` add `const [basis, setBasis] = useState<"actual"|"accounts">("actual")` and a `ToggleButtonGroup` (pattern: `procurement/drcrNote/page.tsx:361-370`) above `<ReportPanel>`; pass `basis` into the fetcher closure; titles get a suffix `(Actual)` / `(Accounts)`.
- [ ] **Step 2: Columns** — `reportTypes.ts` `JuteMrIssueDetailRow` gains `accepted_weight, unit_weight, line_issue_count, total_issued_weight, expected_issued_weight, weight_diff` (all `number | null`); `mrIssueDetail/page.tsx` adds `numCol` entries for `accepted_weight` ("Approved Wt"), `total_issued_weight` ("Issued (line)"), `expected_issued_weight` ("Expected"), `weight_diff` ("Diff") with `cellClassName` red when `Math.abs(weight_diff) > 0.5`.
- [ ] **Step 3: Sync button** — add a `GridColDef` `{ field: "sync", headerName: "", type: "actions", getActions: (p) => [...] }` using `GridActionsCellItem` (pattern: `hrmsmasters/PayRegisterDispSet/page.tsx:414-422`) rendered only on the first row of each `jute_mr_id` (compute a `firstRowIdByMr` map in a `useMemo` over the fetched rows) and only when `weight_diff` of any row of that MR is non-zero; onClick: `window.confirm("Recompute issued weights for this MR from the current line weights?")` → `syncIssueWeights(...)` → toast with `totals.issues_changed` → re-run the panel fetch (ReportPanel exposes `onRefresh`/the `fetcher`; if not, keep the last filters in state and call the fetcher again).
- [ ] **Step 4:** `npx tsc --noEmit -p tsconfig.json` passes.

---

## Group G — juteTransfer

### Task G1: Status inline in the overview; new chains only from Pending (13)

**Files:**
- Modify: `src/jutetransfer/queries.py` (~182-187), `src/jutetransfer/pages/new_transfer_chain.py` (COMPACT_COLUMNS ~38; `_render_chain_editor` ~338-350), `src/jutetransfer/transfer.py` (`save_transfer_step` ~1389-1395), `src/jutetransfer/jute_mr_chain_helpers.py` (new pure helper)
- Test: `tests/test_root_eligibility.py` (create)

- [ ] **Step 1: Failing test**

```python
# tests/test_root_eligibility.py
from src.jutetransfer.jute_mr_chain_helpers import is_root_eligible_for_new_chain, ROOT_STATUS_LABELS


def test_only_pending_starts_a_chain():
    assert is_root_eligible_for_new_chain(13) is True
    for s in (1, 3, 4, 6, 20, 21, 48, None):
        assert is_root_eligible_for_new_chain(s) is False


def test_labels_cover_erp_vocabulary():
    assert ROOT_STATUS_LABELS[13] == "Pending (Transfer)" and ROOT_STATUS_LABELS[3] == "Approved"
```

- [ ] **Step 2: Run** `python -m pytest tests/test_root_eligibility.py -v` — FAIL.

- [ ] **Step 3: Implement**

`jute_mr_chain_helpers.py`:

```python
ROOT_STATUS_LABELS = {21: "Draft", 1: "Open", 13: "Pending (Transfer)", 20: "Pending Approval",
                      17: "Pending Approval", 3: "Approved", 4: "Rejected", 6: "Cancelled", 48: "Returned"}
NEW_CHAIN_ROOT_STATUS = 13  # decision D1 (2026-09-03): only an ERP-Pending MR may start a chain


def is_root_eligible_for_new_chain(status_id) -> bool:
    try:
        return int(status_id) == NEW_CHAIN_ROOT_STATUS
    except (TypeError, ValueError):
        return False
```

`queries.py` ~182-187: replace the CASE with the real vocabulary and add the raw id:

```sql
            mr.status_id AS status_id_raw,
            CASE mr.status_id
                WHEN 21 THEN 'Draft' WHEN 1 THEN 'Open' WHEN 13 THEN 'Pending (Transfer)'
                WHEN 20 THEN 'Pending Approval' WHEN 17 THEN 'Pending Approval'
                WHEN 3 THEN 'Approved' WHEN 4 THEN 'Rejected' WHEN 6 THEN 'Cancelled' WHEN 48 THEN 'Returned'
                ELSE CONCAT('Status-', mr.status_id)
            END AS Status,
```

`new_transfer_chain.py`: add `"Status"` to the local `COMPACT_COLUMNS` (~38). In `_render_chain_editor`, inside `if mr_id not in transfers:` right after `chain_data = chains_map.get(mr_id)`:

```python
            is_new_chain = chain_data is None or chain_data.empty
            if is_new_chain and not is_finalized:
                root_status = row.get("status_id_raw")
                if not is_root_eligible_for_new_chain(root_status):
                    st.warning(f"This MR is '{row.get('Status')}' in the ERP. Only MRs set to Pending (Transfer) can start a new chain; Approved rows are shown for reference.")
                    return
```

`transfer.py` `save_transfer_step`, right after `source_mr = get_source_mr_full(...)`:

```python
    if is_first_step:
        root_status = conn.execute(text("SELECT status_id FROM jute_mr WHERE jute_mr_id = :id"), {"id": root_mr_id}).scalar()
        if not is_root_eligible_for_new_chain(root_status):
            raise ValueError(f"Root MR {root_mr_id} is not Pending (13) in the ERP; cannot start a new transfer chain (status={root_status})")
```

(import the helper from `.jute_mr_chain_helpers`.)

- [ ] **Step 4: Run** `python -m pytest tests -v` and the import check from `juteTransfer/CLAUDE.md` — PASS.

### Task G2: Rollback statuses, guards, party branch

**Files:**
- Modify: `src/jutetransfer/transfer.py` (`revert_original_mr` ~1272/1344; `delete_chain_from_step` ~1659-1672; `delete_transfer_step` ~1530; `_ensure_company_as_party` ~234-235 and ~265/289; `_ensure_supplier_party` ~383-389), `src/jutetransfer/warehouse_stock_ops.py` (legacy branch ~856), `src/jutetransfer/queries.py` docstring ~658-665.

- [ ] **Step 1:** `revert_original_mr`: line 1344 `status_id = 0,` → `status_id = 13,`; docstring/comments at ~1272 and ~1315-1319 say "back to Pending (13)".
- [ ] **Step 2:** `delete_chain_from_step` `from_idx == 0` branch (~1669): `SET status_id = 1` → `SET status_id = 13`; comment: "root returns to Pending (13): the ERP hand-off state, never Open".
- [ ] **Step 3:** `delete_transfer_step` right after `if not mr_info: return` (~1530):

```python
    issued = conn.execute(text(
        "SELECT 1 FROM jute_issue ji JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id "
        "WHERE li.jute_mr_id = :id AND COALESCE(ji.status_id, 0) <> 4 LIMIT 1"), {"id": jute_mr_id}).fetchone()
    if issued:
        raise ValueError("This transfer step has ERP issue entries (consumption started); cannot delete")
```

- [ ] **Step 4:** `warehouse_stock_ops.py` legacy branch, right after `if source_mr_id and moved:` (~856): `if conn.execute(text(_CHAIN_CHILD_SQL), {"sid": int(source_mr_id)}).fetchone(): raise ValueError(f"Source MR {source_mr_id} now feeds a vertical chain; cannot restore weights onto it")`.
- [ ] **Step 5:** party branch `None` → `raise ValueError(...)` at the three sites listed in Files (existing-party paths at ~234-235 and ~383-389; created-party paths after `new_branch_id` is set, ~185/265).
- [ ] **Step 6:** `queries.py:658-665` docstring: finalize → 3, full rollback → 13, in-progress root → 13.
- [ ] **Step 7:** `python -m pytest tests -v` and `python -c "from src.jutetransfer import transfer, warehouse_stock_ops, queries; from src.jutetransfer.pages import new_transfer_chain"` — PASS.

---

## Verification (main thread, after all groups)

1. `cd c:\code\vowerp3be && .venv/Scripts/python.exe -m pytest src/test -q -k "jute or bill_pass or issue"` — no new failures vs baseline (57 pre-existing failures repo-wide are known).
2. `cd c:\code\vowerp3ui && npx tsc --noEmit -p tsconfig.json` — clean.
3. `cd c:\code\juteTransfer && python -m pytest tests -v` — pass.
4. dev3 browser QA (`/qa-portal-page jutePurchase/mr`): Pending without branch → 400 message; Reopen visible only for edit-access user; Return lorry from Open; MR Wise Issue report shows Diff + Sync.
5. sls: **nothing written**. Report the two sls follow-ups: run `add_jute_mr_returned_status_48.sql`, and the 8 Open chain roots must be set to Pending before juteTransfer can add steps to them (G1 preflight blocks only NEW chains, so existing ones keep working).
