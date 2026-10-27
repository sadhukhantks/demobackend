# DR/CR Note Rework — Design

**Date:** 2026-08-08
**Repos:** vowerp3be (backend), vowerp3ui (frontend)
**Status:** Approved by user (interactive Q&A, 2026-08-08)

## Problem

1. **Every SR approval on a PO-less inward auto-creates a phantom credit note.**
   Root cause: `approve_sr` (src/procurement/sr.py:939-977) computes
   `rate_diff = po_rate - accepted_rate` where `po_rate = item.get("po_rate") or item.get("rate") or 0`.
   A PO-less inward line has no baseline rate, so `po_rate` coerces to `0`,
   `rate_diff` is always negative, and a credit note (adjustment_type=2) is created
   on every approval. DB-confirmed: on sls all 137 auto credit-note lines have
   `po_dtl_id IS NULL` / `po_rate IS NULL` (79 phantom notes); dev3 has 2.
2. **No manual DR/CR note entry exists.** Backend `POST /api/drcrNote/create_drcr_note`
   works but has zero frontend callers; its prefill endpoint
   `get_inward_for_drcr_note` queries nonexistent tables (`con_supplier_master`,
   `branch_master`, ...) and would 500.

## Decisions (user-confirmed)

| Question | Decision |
|---|---|
| Baseline for auto rate-diff note | **Per-line invoice rate** — new field on SR line; compare invoice rate vs accepted rate. No invoice rate entered → no rate-diff note. |
| Qty-difference trigger | **Rejection only** (current behavior): `rejected_qty > 0` → debit note. No invoiced-vs-received qty comparison. |
| Manual note scope | **Inward-linked only** — must reference an approved-SR inward. No standalone supplier-level notes. |
| Existing phantom notes | **Hard delete** (with backup tables). |

## Part A — Auto-note trigger rework

### DB migration
- `ALTER TABLE proc_inward_dtl ADD COLUMN invoice_rate DOUBLE NULL;`
- ORM: add `invoice_rate` to the inward detail model in `src/models/procurement.py`.
- Apply to dev3 + sls (and all other tenants **before** code deploy).

### Frontend (`../vowerp3ui/src/app/dashboardportal/procurement/sr/createSR/page.tsx`)
- New editable per-line column **Invoice Rate**.
- Prefill: PO rate when the line is PO-linked; blank when PO-less.
- Optional — may be left blank.

### Backend `save_sr` (src/procurement/sr.py)
- `SRLineItemUpdate` gains `invoice_rate: Optional[float]`.
- Persist via the inward-detail update query (src/procurement/query.py:2111).

### Backend `approve_sr` note logic (src/procurement/sr.py:939-977)
- **Rejection debit note (unchanged trigger):** `rejected_qty > 0` → debit line.
  Note rate = `invoice_rate`, fallback PO rate, fallback accepted rate.
- **Rate-diff note (new gate):** only when `invoice_rate` is set and > 0:
  `diff = invoice_rate - accepted_rate`, epsilon 0.005 (float noise guard).
  - diff > 0 → debit note line (supplier billed high; supplier owes us).
  - diff < 0 → credit note line (we owe supplier).
  - qty = approved qty.
- **No invoice rate → no rate-diff note.** PO-less line with blank invoice rate
  produces nothing (rejection debit still possible).
- GST breakup insert per line unchanged (existing `_insert_drcr_gst` helper).

## Part B — Manual note creation (inward-linked)

### Backend repairs (src/procurement/drcr_note.py)
- Rewrite `get_inward_for_drcr_note` against the real schema
  (`branch_mst`, `party_mst`, `item_mst`, `item_grp_mst`, `uom_mst`, `item_make`);
  keep its existing gate: inward's SR must be Approved.
- Harden `create_drcr_note`:
  - Validate the inward's SR status is Approved (currently unvalidated).
  - Add GST breakup insert (`drcr_note_dtl_gst`) mirroring the auto path —
    manual notes currently skip GST, which understates tax in bill pass/accounting.

### Frontend
- **New Note** button on `drcrNote` list page → create page:
  pick approved-SR inward (supplier/branch filtered), lines prefill from inward,
  choose Debit/Credit, per-line qty + rate + remarks.
- Creates a Draft note (`auto_create=0`); existing view-page workflow
  (Open → Approve → Reject) unchanged.

### Bill pass / accounting
- No change. Bill pass already nets only Approved notes
  (src/procurement/query.py:2620, `status_id = 3` filter).
- A note approved after its bill pass was approved stands in accounting via the
  existing `post_document` call on approve; it does not retro-edit the bill pass.

## Part C — Phantom cleanup migration

- Delete auto-created credit notes whose lines are **all** PO-less:
  `auto_create = 1 AND adjustment_type = 2` and every line's
  `proc_inward_dtl.po_dtl_id IS NULL`.
- Delete order: `drcr_note_dtl_gst` → `drcr_note_dtl` → `drcr_note`.
- Backup rows into `drcr_note_bak_20260808` / `drcr_note_dtl_bak_20260808` /
  `drcr_note_dtl_gst_bak_20260808` before deleting (rollback = re-insert).
- Targets: sls (79 notes: 78 Draft + 1 Open), dev3 (2 Draft). amcl empty.
- Rejected/approved **debit** notes untouched.

## Deploy order

1. Column migration (Part A) + cleanup migration (Part C) — dev3 first, then sls,
   then remaining tenants.
2. Backend + frontend code deploy.

## Testing

- pytest (src/test/): regression *PO-less line + blank invoice rate → zero notes
  on approve*; invoice-diff → debit / credit cases; epsilon boundary; rejection
  debit note rate fallback chain; manual create writes GST rows; manual create
  rejected when SR not approved.
- One commit per repo.
