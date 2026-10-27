"""
Jute Procurement module constants - single source of truth for jute MR status IDs.

Status code reference (verified against src/models/jute.py:737 and
src/juteProcurement/mr.py:628-634):
    21 -> Draft
    1  -> Open
    13 -> Pending / Finalised (terminal state on MR screen, handed off to external system)
    20 -> Pending Approval
    3  -> Approved
    4  -> Rejected
    6  -> Cancelled
    48 -> Returned (lorry sent back; the retained material is still stock and paid for)

The `vw_jute_stock_outstanding` view filters MRs by `status_id IN (3, 13)` to
include both Approved and Finalised MRs as issuable stock. Downstream reports
that need "approved-or-finalised" MRs must use the same tuple to stay
consistent with the view.
"""

from datetime import date

from src.sales.constants import INVOICE_TYPE_IDS, SALES_STATUS_IDS

# MRs visible as approved-or-finalised stock (matches vw_jute_stock_outstanding).
JUTE_MR_APPROVED_STATUSES = (3, 13)

# The Jute Purchase Tally sheet carries only MRs that are Approved (3): a
# Pending (13) MR is a hand-off to the transfer app, has no MR date or number
# yet and is not a purchase voucher (owner ruling 2026-10-04).
JUTE_MR_TALLY_STATUSES = (3,)

# MRs whose stock may be DRAWN AGAINST on the Jute Issue page. Wider than
# JUTE_MR_APPROVED_STATUSES on purpose: the floor issues jute the moment the MR
# is opened (1), long before it is approved, so blocking OPEN only pushed
# operators into back-dating the issue after approval. Still narrower than the
# stock view, which also carries 4/6/21 exclusions plus 17/20/24 in-flight rows.
# The issue picker and the save-time guard MUST carry this SAME tuple — relax
# one and the other becomes cosmetic, since a direct POST bypasses the picker.
JUTE_MR_ISSUABLE_STATUSES = (1, 3, 13, 48)

# ---------------------------------------------------------------------------
# Stock eligibility — ONE definition shared by the receipt side and the issue
# side of every balance report. Receipts and issues MUST agree, otherwise
# material leaves stock that never arrived and closing drifts negative.
# ---------------------------------------------------------------------------

# An MR is stock unless it was rejected, cancelled or never left draft. Everything
# else (1 OPEN, 13 PENDING, 17 PENDING APPROVAL L2, 20 PENDING APPROVAL,
# 24 TRANSFER PENDING, 3 APPROVED, 48 RETURNED) has physically arrived.
# "Returned" is a FLAG (jute_mr.is_returned), not a status, since 2026-10-03: the
# lorry went back to the supplier, but the material the mill inspected and kept
# is bought, stocked, issuable, transferable and paid for. The MR lines hold that
# retained quantity; the header net_weight is the full lorry, so receipt weight
# for a returned MR must be summed from the lines (JUTE_MR_RECEIVED_WEIGHT_SQL).
# Status 48 is legacy (migration 20261003_jute_mr_is_returned_flag.sql converts it).
JUTE_MR_RETURNED_STATUS = 48  # legacy only; never set by code any more
JUTE_MR_STOCK_EXCLUDED_STATUSES = (4, 6, 21)

# Weight an MR contributes as received (alias `jm` = jute_mr). Normal MRs: the
# header net_weight. Returned MRs (is_returned = 1): only the retained line weight.
JUTE_MR_RECEIVED_WEIGHT_SQL = (
    "CASE WHEN jm.is_returned = 1 THEN "
    "(SELECT COALESCE(SUM(rli.actual_weight), 0) FROM jute_mr_li rli "
    "WHERE rli.jute_mr_id = jm.jute_mr_id AND (rli.status IS NULL OR rli.status NOT IN ('4', '6'))) "
    "ELSE jm.net_weight END"
)

# jute_mr_li.status is a VARCHAR, not an int — quote these in SQL.
JUTE_MR_LI_EXCLUDED_STATUSES = ("4", "6")

# An issue reduces stock unless it was rejected.
JUTE_ISSUE_EXCLUDED_STATUS = 4

# ---------------------------------------------------------------------------
# Purchased yarn ledger (Yarn Purchase / Yarn Issue) — see
# docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-design.md
# Tables: jute_yarn_purchase(_li), jute_yarn_issue(_li), vw_yarn_godown_outstanding.
# ---------------------------------------------------------------------------

# Header lifecycle is the standard one: 21 draft -> 1 open -> 20 pending ->
# 3 approved / 4 rejected / 6 cancelled.
YARN_DOC_STATUS_DRAFT = 21
YARN_DOC_STATUS_OPEN = 1
YARN_DOC_STATUS_PENDING = 20
YARN_DOC_STATUS_APPROVED = 3
YARN_DOC_STATUS_REJECTED = 4
YARN_DOC_STATUS_CANCELLED = 6

# A purchase / issue that never became a movement. The godown view carries the
# SAME exclusion in SQL (`status_id NOT IN (4, 6, 21)`); keep them in step.
YARN_DOC_STOCK_EXCLUDED_STATUSES = (4, 6, 21)

# Purchases whose stock may be DRAWN on the Yarn Issue page: approved only
# (design §3.1). The picker and the save-time guard MUST carry this same tuple.
# Widen to (1, 20, 3) if the floor needs to issue before approval, as happened
# with JUTE_MR_ISSUABLE_STATUSES.
YARN_PURCHASE_ISSUABLE_STATUSES = (3,)

YARN_ISSUE_TYPE_ISSUE = "ISSUE"    # GODOWN -> department floor
YARN_ISSUE_TYPE_RETURN = "RETURN"  # department floor -> GODOWN
YARN_ISSUE_TYPES = (YARN_ISSUE_TYPE_ISSUE, YARN_ISSUE_TYPE_RETURN)

# party_type_mst row that tags yarn suppliers (seeded by
# dbqueries/migrations/20260925_yarn_supplier_party_type.sql); resolved by name.
YARN_SUPPLIER_PARTY_TYPE_NAME = "Yarn Supplier"

YARN_PURCHASE_MENU_PATH = "jutePurchase/yarnPurchase"
YARN_ISSUE_MENU_PATH = "jutePurchase/yarnIssue"

# Cut-over (owner ruling 7, design §5): Yarn Purchase documents exist from the
# start of FY 2026-27; the sls Stores receipts of the legacy item are backfilled
# from this date. The reports raise the "Stores receipts but no Yarn Purchase"
# transition warning only for windows that end on or after it.
YARN_PURCHASE_CUTOVER_DATE = date(2026, 4, 1)

# Raw jute IS sold; an approved raw-jute sales invoice reduces stock.
# Re-exported from the sales module so the two can never drift apart.
RAW_JUTE_INVOICE_TYPE_ID = INVOICE_TYPE_IDS["RAW_JUTE"]
SALES_APPROVED_STATUS_ID = SALES_STATUS_IDS["APPROVED"]

# Detail reports (as opposed to the balance CTEs above) list every invoice that
# still exists, not just the approved ones — mirroring the issue side, which
# excludes only rejections. Cancelled joins rejected here because a cancelled
# invoice is not a movement at all.
SALES_MOVEMENT_EXCLUDED_STATUSES = (
    SALES_STATUS_IDS["REJECTED"],
    SALES_STATUS_IDS["CANCELLED"],
)

# ---------------------------------------------------------------------------
# Jute PO reports (/juteReports/po-options, /po-mr-receipt-detail, /po-pending)
# ---------------------------------------------------------------------------

# POs that never became (or stopped being) real orders: drafts (2, 21),
# rejected (4), cancelled (6). CLOSED (5) is deliberately NOT here — a closed
# PO has real receipts and belongs in the receipt-comparison report.
JUTE_PO_INACTIVE_STATUSES = (2, 4, 6, 21)

# A closed PO is complete by definition — the Pending PO report excludes it
# even when its received weight falls short of the ordered weight.
JUTE_PO_CLOSED_STATUS = 5

# Live POs: the only ones the settlement engine (po_settlement.py) may close
# and the only ones a user may short-close. Drafts (2, 21), pending approval
# (17, 20), rejected (4) and cancelled (6) are never touched by the engine.
JUTE_PO_LIVE_STATUSES = (1, 3)
JUTE_PO_SETTLEMENT_SKIPPED_STATUSES = (2, 4, 6, 17, 20, 21)

# ---------------------------------------------------------------------------
# Jute Issue Planner report (/juteReports/issue-planner)
# ---------------------------------------------------------------------------

# Batch daily assigns the planner treats as "planned". Wider than the
# batch-cost report's (1, 3) on purpose: future days are usually still Draft
# (21) when the morning plan is made, and excluding them would show an empty
# plan window. Rejected (4) stays out.
ISSUE_PLANNER_ASSIGN_STATUSES = (21, 1, 3)

# Mill day boundary: roll stock is snapshotted at 06:00 of the issue day,
# matching the C-shift cutoff used by the spreader production summary.
ISSUE_PLANNER_SNAPSHOT_HOUR = 6

# Batch plan look-ahead window in days (issue day inclusive). Target-stock
# dates land at issue day + maturity days, so the window must cover the
# longest maturity in use (default 48h -> +2 days).
ISSUE_PLANNER_WINDOW_DAYS = 5

# Reference production assumed for a planned yarn with no previous-day doff
# production (mirrors the MIS planner's 1 MT default).
ISSUE_PLANNER_DEFAULT_REFERENCE_KG = 1000.0

# Standard jute bale weight for the Required Issue (Bales) conversion.
JUTE_BALE_WEIGHT_KG = 148.0

# item_grp_mst.item_type_id — the master flag that says what a group holds.
# 2 = purchased raw jute, 3 = process waste. Both are issuable jute stock (the
# issue picker offers groups of either type), but they are bought, valued and
# reported differently, so anything summarising jute movement splits on this.
RAW_JUTE_ITEM_TYPE_ID = 2
JUTE_WASTE_ITEM_TYPE_ID = 3

# Package types accepted on a Yarn Purchase line (jute_yarn_purchase_li.package_type).
# Counting aid only — weight_kg is the unit of record, there is no uom column.
YARN_PACKAGE_TYPES = ("SPOOL", "BAG", "CONE", "BUNDLE")
