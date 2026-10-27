-- TKT-000143 internal [AI-TRIAGE] verdict comment. Target DB: vowconsole3.
-- Internal only (is_internal = 1) — no status move, no assignment, no reporter reply.
-- Rollback: DELETE FROM support_ticket_comment WHERE ticket_id = 143 AND comment_text LIKE '[AI-TRIAGE]%';

INSERT INTO support_ticket_comment
    (ticket_id, author_type, author_user_id, author_name, comment_text, is_internal)
VALUES (143, 'system', NULL, 'AI Triage',
'[AI-TRIAGE] Reclassified: data_issue -> bug (real defect, systemic). Reported instance has since self-resolved; the defect has not.

FINDING
Item 134-5516 (item_id 335008, sls co 106 br 87) has exactly one lot: inward_dtl_id 23090,
SR LC/FACTORY/25-26/000944, approved 1. At the time the ticket was raised (02-Sep) that unit was
fully consumed by issue_id 58424 = Issue #1433 dated 31-Aug-2026, then status 21 DRAFTED, entered
by the reporter themselves (user 22). The stock was never lost -- it was held by their own
unsubmitted draft, which is why the Item Ledger still showed 1 PC while the posting screen showed
nothing. Measured at diagnosis: ledger closing 1.0, picker closing 0.0, draft-held 1.0.

Issue #1433 has since been approved (03-Sep-2026 08:53). Both definitions now agree at 0 for this
lot, so the reported symptom is gone WITHOUT anyone fixing anything. The underlying defect stands.

ROOT CAUSE
Two stock definitions disagree on which issue statuses consume stock:
  - Issue picker  -> view vw_approved_inward_qty: counts every issue with status_id <> 6,
                     so DRAFTED (21) DOES consume stock.
  - Item Ledger / Stock Report -> src/inventory/reportQueries.py: counts only
                     status_id IN (1,3,5,20), so DRAFTED (21) does NOT consume stock.
A drafted issue therefore eats picker stock while staying invisible in the ledger, and the lot
disappears from the posting screen entirely -- no row, no zero, no explanation.

SCOPE -- not a one-off
Branch 87 currently holds 49 open DRAFTED issues. These are NOT abandoned junk: the branch enters
issues as drafts and approves them in batches later (the ticket''s own #1433 was approved a day
after it was raised), so this is normal working practice, and the invisibility hits routine data
entry every time. Across sls, 57 lots are currently invisible in the picker purely because a draft
holds them. Worst case measured: lot 23358, item 139-1783 (ALUMINIUM FLANGE FOR A.D BOBBIN
CARRIER), approved 200, entirely held by draft Issue #510 -- the ledger shows 200, the posting
screen shows nothing at all.

FIX SHIPPED (option C, approved by product)
Leave both definitions alone. Make the picker SHOW the held lot with an explanation instead of
silently filtering it out:
  - src/inventory/query.py -- get_available_inward_inventory_query,
    get_searchable_inventory_list_query and get_searchable_inventory_count_query share a new
    _DRAFT_HELD_JOIN over issue_li/issue_hdr where status_id = 21, returning draft_held_qty,
    draft_held_refs (the holding issue numbers) and ledger_qty. Row filter relaxed to
    (balance_qty > 0 OR draft_qty > 0), identically in the count query so pagination stays in sync.
  - Frontend create-issue picker gains an "In Draft" column: an amber chip reading "In draft 1.00"
    whose tooltip names the holding issue and the ledger figure, so the user can go approve or
    cancel it. Zero-available rows are visible but non-selectable ("Held").
  - _assert_lots_available deliberately NOT relaxed, so visibility never becomes a route to
    over-issue. Pinned by test.
  - No view change and no SQL migration, so there is nothing to roll out per tenant.
Tests: src/test/test_inventory_draft_held_stock.py (8 passing) + 2 frontend picker cases.

Rejected alternatives: adding 21 to the ledger status set (pollutes a financial ledger with
unconfirmed drafts); removing 21 from the view (drops lot reservation, would instantly release the
49 open drafts'' stock, and invites over-issue when they are approved).

REPLY TO REPORTER (needs a human to send)
"The 1 PC was not missing -- it was already committed on your own draft Issue #1433 of 31-Aug.
That issue was approved on 03-Sep, so the item has now genuinely gone out and both the ledger and
the posting screen correctly show zero. We are also changing the posting screen so that in future a
lot held by an unapproved draft is still listed, marked with the issue number holding it, instead
of disappearing."

RESIDUAL, RAISED SEPARATELY -- not part of this ticket
7 of the 57 newly-visible lots carry a NEGATIVE balance (e.g. lot 14323, item 109-0036 WASHER
(M S) 5/8", at -248.26). Pre-existing negative-stock data on br 87, previously masked by the
balance_qty > 0 filter and now surfaced. Un-issuable and shown as a distinct data-issue state, but
these rows need their own repair pass.', 1);
