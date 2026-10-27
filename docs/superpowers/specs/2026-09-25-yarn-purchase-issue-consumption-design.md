# Purchased Yarn: purchase → godown → department → consumption

Design proposal, 2026-09-25. Follow-up to TKT-000127 and TKT-000187. No code yet.

## 1. Problem

The Winding & Weaving Difference report needs `purchase_mt`, the purchased yarn that
entered winding stock. Today it reads 0 on sls and always has, because:

- purchased yarn is booked in Stores as item 334018 "spools/yarn" (MISC EXPENSES group,
  `item_type_id = 1`) under company 106 / branch 87, mostly in UOM `MTR`;
- the report looks for approved store receipts of yarn-type items (`item_type_id = 4`)
  at the mill's own company 2 / branch 29 in MT or KG (`winding_stock_query.py:566`).

Beyond the report, the mill has no document for what happens to purchased yarn after it
arrives: nothing records the issue from godown to a department, and nothing separates
"issued and consumed" from "issued but still lying on the floor".

What exists today (see `2026-08-19-winding-stock-difference-design.md` and the map in
this file's appendix):

| Movement | Recorded by | Gap |
|---|---|---|
| Purchase inflow | Stores receipt at co 106 / br 87 | wrong company, branch, item type, UOM; no kg |
| Godown → department | nothing | no yarn issue document at all |
| Consumption | derived from cloth MT via quality standards | never recorded; own yarn and purchased yarn indistinguishable |
| Floor / godown stock | `jute_prod_winding_stock` physical count, bucket `PURCHASE` | one flat count, no location, no department |
| Return to godown | nothing | no reverse document anywhere in the system |

## 2. Principles

1. **Kilograms are the unit of record.** Packages (spools, bags, cones) are captured for
   counting, weight in kg is authoritative, MT is derived. Never MTR.
2. **The mill's own company and branch.** Every yarn document is keyed by the co/branch
   whose winding report consumes it (sls: co 2 / branch 29). Money can stay where the
   accountants want it; quantity lives with production.
3. **One ledger, three places.** Yarn is either in the **godown**, on a **department
   floor**, or **consumed**. Every document moves kg between two of these.
4. **Counts reconcile, documents move.** Consumption is not keyed by hand every day. It
   is derived from documents and periodic floor counts, exactly like the winding stock
   difference already works. An explicit consumption entry is an optional later phase.
5. **Reuse the house patterns.** Approval lifecycle 21 → 1 → 20 → 3/4/6
   (`add-approval-workflow`), per-day lock for counts (`winding_stock_lock.py`), outstanding
   views for pickers (`vw_jute_stock_outstanding`), menus by migration (`add-menu`).

## 3. Process design

```
 Supplier ──(Yarn Purchase, approved)──▶ GODOWN ──(Yarn Issue)──▶ DEPT FLOOR ──(derived)──▶ CONSUMED
                                           ▲                          │
                                           └──────(Yarn Return)───────┘
 Counts:  godown count (per quality)      floor count per dept (per quality)  ← Winding Stock page
```

### 3.1 Yarn Purchase (new menu under Jute Purchase)

The receipt of purchased yarn at the mill. One header per lorry / challan, lines per yarn
quality.

- Header: date, supplier (party), challan no/date, vehicle, optional link to the Stores
  receipt (`proc_inward.inward_id`) when the money is booked there, remarks, status,
  approval level, purchase number per financial year.
- Line: yarn item (`item_mst` row that has a `jute_yarn_mst` entry; e.g. 335556 "13 LBS
  SKWP"), package type, number of packages, weight per package, **weight kg**, optional
  rate per kg and amount.
- Approval: draft → open → pending → approved. Only approved documents count as stock.
- Effect: `+kg` in GODOWN for that quality.

### 3.2 Yarn Issue and Yarn Return (new menu under Jute Purchase, next to Jute Issue)

- Header: date, department (any active `dept_mst` row of the branch), type ISSUE or
  RETURN, issue number, status, approval level.
- Line: yarn item, **purchase lot (mandatory; a line of an approved Yarn Purchase, listed
  oldest first)**, packages, **weight kg**.
- Picker shows godown outstanding per quality and per lot (new view, see §4).
- Effect: ISSUE moves kg GODOWN → FLOOR(dept). RETURN moves kg FLOOR(dept) → GODOWN.
- Guard: an ISSUE cannot exceed godown outstanding (hard block, same as jute issue). A
  RETURN cannot exceed the department's floor book balance (warning, not block; counts
  will correct it).

### 3.3 Floor stock and consumption

The department does not key consumption. Instead:

- The existing **Winding Stock** count gains a **location** (GODOWN or FLOOR) and, for
  FLOOR, a **department**. Bucket `PURCHASE` lines are then "purchased yarn counted at
  place X". Nothing else on the page changes; the daily total the difference report uses
  is unchanged because it sums all locations.
- **Consumption for a period** = floor opening + issues − returns − floor closing, per
  department and per quality. This is the same opening/inflow/closing arithmetic the
  mill already trusts for the winding difference.
- **Godown check** = godown opening + purchases − issues + returns − godown closing count.
  Should be 0. Anything else is a booking error or shrinkage and is surfaced.

Why derived rather than keyed: the mill already counts stock on the winding page; an
extra daily "we used 1,240 kg" screen is a second source of truth that will drift from
the count and generate its own tickets. If the owner later wants day-level consumption
(for example to cost cloth by lot), phase 6 adds an explicit consumption document that
moves FLOOR → CONSUMED and the derived figure becomes the check on it.

### 3.4 Impact on the Winding & Weaving Difference report

- `purchase_mt` reads approved Yarn Purchase lines (kg / 1000) at the report's co/branch
  and date range. The Stores query stays as a **cross-check warning**: if approved store
  receipts of the legacy item exist in the period but no Yarn Purchase does, the report
  says so instead of silently reading 0.
- `should_be_closing_mt` is unchanged in shape. Issues and returns are internal moves
  (godown ↔ floor) and do not enter it. Consumption of purchased yarn is already inside
  `weaving_mt`, which is derived from cloth production.
- New drill-down: purchase by quality with document numbers, so the figure is auditable.
- The `stock` block already shows bucket `PURCHASE`; with location it can show godown vs
  floor for purchased yarn.

### 3.5 Monitoring: Purchased Yarn Ledger (new report under Jute Production reports)

Per branch and month, per yarn quality, with a department breakdown:

| Column | Source |
|---|---|
| Godown opening | last godown count before the period, else derived |
| Purchases | approved Yarn Purchase |
| Issues, Returns | approved Yarn Issue / Return |
| Godown closing (book) | opening + purchases − issues + returns |
| Godown closing (count) | Winding Stock, location GODOWN |
| Godown difference | count − book |
| Floor opening, Floor closing (count) | Winding Stock, location FLOOR, per dept |
| Consumption (derived) | floor opening + issues − returns − floor closing |
| Days of stock on floor | floor closing / average daily consumption |

Exceptions the page highlights: godown book negative; godown difference beyond a
tolerance; a department with issues but no floor count in the period; floor stock above
N days; Stores receipts of the legacy item with no Yarn Purchase (transition guard); yarn
items in a purchase with no `jute_yarn_mst` row.

## 4. Data model

New tables, all with `co_id`, `branch_id`, `active`, `updated_by`, `updated_date_time`:

- `jute_yarn_purchase` (header): `yarn_purchase_id`, `tran_date`, `purchase_no`,
  `party_id`, `challan_no`, `challan_date`, `vehicle_no`, `inward_id` NULL,
  `status_id`, `approval_level`, `remarks`.
- `jute_yarn_purchase_li`: `yarn_purchase_li_id`, `yarn_purchase_id`, `item_id`,
  `package_type`, `no_of_packages` DECIMAL(12,3), `wt_per_package` DECIMAL(10,3),
  `weight_kg` DECIMAL(12,3), `rate_per_kg` NULL, `amount` NULL, `remarks`.
- `jute_yarn_issue` (header): `yarn_issue_id`, `tran_date`, `issue_no`, `issue_type`
  ENUM('ISSUE','RETURN'), `dept_id`, `status_id`, `approval_level`, `remarks`.
- `jute_yarn_issue_li`: `yarn_issue_li_id`, `yarn_issue_id`, `item_id`,
  `yarn_purchase_li_id` NULL (lot), `no_of_packages`, `weight_kg`.
- `vw_yarn_godown_outstanding`: per purchase line: received − issued + returned, approved
  documents only, statuses per `JUTE_MR_STOCK_EXCLUDED_STATUSES` convention.
- `jute_prod_winding_stock`: add `stock_loc` VARCHAR(10) NOT NULL DEFAULT 'FLOOR' and
  `dept_id` INT NULL. Existing rows stay valid (FLOOR, no dept).

Numbering follows `get_next_bill_pass_no_for_fy` (per branch, per financial year).
Migrations in `dbqueries/migrations/` with rollback and verification blocks after the last
statement, applied via `run-migration` to dev3 first.

## 5. Decisions (owner rulings, 2026-09-25)

1. **Money.** Build quantity-only first. An accounting entry for the Yarn Purchase is
   required and is recorded as follow-up work, to be designed once the quantity flow is
   live (see §7).
2. **Departments.** Any department from `dept_mst`, not a fixed list. The issue page
   offers every active department of the branch.
3. **Consumption.** Derived from floor counts (§3.3). Explicit consumption entry stays an
   optional later phase.
4. **Godown check.** The godown is counted daily together with the winding stock count.
   Any mismatch between godown book and godown count is reported (ledger report and a
   warning on the difference report).
5. **Lot tracking.** Lot selection on every issue line is **mandatory**. The picker lists
   lots oldest first (FIFO) and the user picks the lot the yarn is physically issued
   from. Skipping an older lot that still has stock is allowed but flagged in the ledger.
6. **Backfill.** Yes: create Yarn Purchase documents for the sls Stores receipts of item
   334018. The Stores entries stay untouched for now; see §7 for their removal.
7. **Cut-over.** 1 April 2026, the start of the financial year. Backfill covers receipts
   from that date onward. Before that date the report shows no transition warning.

## 7. Follow-up work recorded, not in scope now

- **Accounting entry for Yarn Purchase** (ruling 1): to be designed after the quantity
  flow is live. Likely shape: bill pass on the Yarn Purchase header like the Jute MR, or a
  link to the Stores bill pass, whichever the accountants rule. Until then the money stays
  in Stores at co 106.
- **Removal of Stores item 334018 receipts** (ruling 6): once the backfilled Yarn Purchase
  documents have been checked against the mill's records, the Stores receipts since
  1 April 2026 are to be cancelled or reclassified so they are not counted twice.

## 6. Phases

| Phase | Deliverable | Skills / patterns |
|---|---|---|
| 1 | Migrations (4 tables, view, 2 columns), ORM models, constants, menus on dev3 | `run-migration`, `add-menu`, `new-master` |
| 2 | Yarn Purchase page + API + approval workflow | `wire-api`, `add-approval-workflow`, Jute MR page as template |
| 3 | Yarn Issue / Return page + godown lot picker (lot mandatory, oldest first) | Jute Issue page and `get_stock_outstanding` as template |
| 4 | Winding Stock: location + department fields, totals unchanged | `winding_stock.py`, `WindingStockForm.tsx` |
| 5 | Report changes: `purchase_mt` from Yarn Purchase, transition warning, drill-down; Purchased Yarn Ledger report | `winding_stock_query.py`, `build_reconciliation`, golden tests |
| 6 | Optional explicit consumption entry | only if decision 3 asks for it |
| 7 | sls rollout: migrations, `is_twisted` migration (still pending on sls), yarn items for purchased qualities, backfill from 2026-04-01, cut-over | `run-migration` with owner approval per tenant |

Each phase is testable on dev3 on its own. Phase 5 is the one that changes numbers the
mill already looks at, so it ships with the golden-value tests in
`test_winding_stock_report.py` and `test_winding_twisting_netting.py` updated first.

## Appendix: where things are today

- Report: `src/juteProduction/winding_reports.py` (`build_reconciliation` line 819,
  `split_twisting_mt` line 800, endpoint line 1049); purchase query
  `src/juteProduction/winding_stock_query.py:566`.
- Physical count: `jute_prod_winding_stock`, `src/juteProduction/winding_stock.py`,
  buckets in `constants.py:434`, page `juteProduction/windingStock`.
- Yarn masters: `jute_yarn_mst` (`src/models/jute.py:610`), yarn item 1:1 via
  `src/masters/yarnMaster.py`; yarn type = `item_grp_mst` with `item_type_id = 4`.
- Stores chain: `proc_indent` → `proc_po` → `proc_inward` (+ SR columns) → `issue_hdr` /
  `issue_li` (`dept_id`, `cost_factor_id`, `inward_dtl_id`).
- Jute issue precedent: `src/juteProcurement/issue.py`, `vw_jute_stock_outstanding`,
  waste-entry-to-issue pattern in `src/juteProduction/waste_entry.py`.
- Forensics: `dbqueries/migrations/tkt187_sls_purchase_yarn_verify.sql`.
