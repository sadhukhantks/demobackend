# Finishing Stock — daily physical stock entry + report

**Status:** APPROVED design (user, 2026-09-17) · **Tenant:** dev3 first (sls migrations listed, not run)
**Repos:** vowerp3be (FastAPI) + vowerp3ui (Next.js) · **Module:** Jute Production
**Pattern:** mirror **Winding Stock** (`jute_prod_winding_stock`, `/api/windingStock`,
`juteProduction/windingStock`) — a flat counted-snapshot table, copy-previous-day, weights
computed at read. No lock, no approval workflow, no spell.

## 0. Decisions captured

| Decision | Choice |
|---|---|
| Second bale block (EMPIRE vs NEXGEN) | `owner_co_id` on BALE lines, picked from `co_mst`; NULL = own company. Report prints one bale block per owner. |
| Loose-stock grain | Stage × quality lines: stage (fixed code list) + quality + count + weighed kg + Dept/Contract flag. Report sums per quality. |
| Despatch block | Derived from sales invoices on the date (all cloth item lines, `item_type_id=5`), bales from `sales_invoice_hessian_dtl.qty_bales` when present. |
| Misc loose categories (HESSIAN JOINT, REJECTED P/SHEET, SPOOL BAG, PACKSHEET, JOINT BAG, SHORT BAG) | Created as `item_mst` items + `jute_prod_finishing_quality` rows (seed migration, dev3 co 1). Picker shows only finishing-quality rows. |
| Day lock | Not built (YAGNI). |
| Duplicate lines | Allowed — several lines per (stage, quality) = lots (sheet shows 19, 45, 27, 2). Report sums. |

## 1. Source sheets → model

- "Physical Stock as on 29-11-25": sacking loose by stage (S4 cuts, cutting, hemming, herackle,
  branded pressing, branded AP, P/S, joint) × quality (Blue / Blue+Black), Dept vs Contract
  blocks, counts + kg; hessian cuts by width; bale arranging.
- "Cuts in line at 10pm (from wvg dept)": stage `IN_LINE`, hessian by quality, S4 count.
- "Physical loose stock 30/11/25 7AM": per quality weights in MT → the report's LOOSE STOCK block.
- Excel report: per-owner bale blocks (Hessian / Sacking: bales, per-bale wt, MT), loose stock by
  quality (Hessian / Sacking), despatch on date, grand totals.

## 2. Data model — one new tenant table

`dbqueries/migrations/create_jute_prod_finishing_stock.sql` (+ rollback comment).
ORM: `src/juteProduction/finishing_stock_models.py` (SQLAlchemy 2.0 `Mapped` style, shared
`Base` from `src/models/mst.py`, like `winding_stock_models.py`).

```sql
CREATE TABLE jute_prod_finishing_stock (
  finishing_stock_id     INT AUTO_INCREMENT PRIMARY KEY,
  co_id                  INT NOT NULL,
  branch_id              INT NOT NULL,
  tran_date              DATE NOT NULL,
  bucket                 VARCHAR(10) NOT NULL,           -- 'BALE' | 'LOOSE'
  finishing_quality_id   INT NOT NULL,                   -- jute_prod_finishing_quality (item = identity; quality_type 1 hessian / 2 sacking)
  stage                  VARCHAR(20) NULL,               -- LOOSE only; constants.FINISHING_STOCK_STAGES code or NULL
  work_by                TINYINT NOT NULL DEFAULT 0,     -- 0 Dept, 1 Contract (LOOSE only)
  owner_co_id            INT NULL,                       -- BALE only; co_mst.co_id; NULL = own company
  qty                    DECIMAL(12,3) NOT NULL,         -- bales (BALE) / cuts, bundles, pcs (LOOSE)
  qty_uom                VARCHAR(10) NOT NULL,           -- 'BALE' | 'CUTS' | 'BDL' | 'PCS'
  wt_per_unit_kg         DECIMAL(10,3) NULL,             -- BALE: kg per bale (default = quality std_bale_weight, editable per line)
  wt_kg                  DECIMAL(14,3) NULL,             -- LOOSE: weighed total kg
  remarks                VARCHAR(255) NULL,
  active                 TINYINT NOT NULL DEFAULT 1,
  updated_by             INT NULL,
  updated_date_time      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_finstock_co_branch_date (co_id, branch_id, tran_date),
  INDEX idx_finstock_quality (finishing_quality_id),
  INDEX idx_finstock_owner (owner_co_id)
);
```

`total_kg` is never stored: `wt_kg` when not NULL else `qty * wt_per_unit_kg`. MT = kg / 1000
(3 decimals) at read. Audit via triggers — no `created_*` columns.

### Constants (`src/juteProduction/constants.py`, appended)

```python
FINISHING_STOCK_BUCKET_BALE = "BALE"
FINISHING_STOCK_BUCKET_LOOSE = "LOOSE"
FINISHING_STOCK_BUCKETS = (FINISHING_STOCK_BUCKET_BALE, FINISHING_STOCK_BUCKET_LOOSE)
# (code, label) — ordered as the shop-floor sheet lists them
FINISHING_STOCK_STAGES = (
    ("IN_LINE", "Cuts in line (from weaving)"),
    ("S4_CUTS", "S4 cuts"),
    ("CAL_CUTS", "Calendered cuts"),
    ("CUTTING", "Cutting"),
    ("HEMMING", "Hemming"),
    ("HERACKLE", "Herackle"),
    ("BRANDING", "Branded pressing"),
    ("AP", "Branded AP"),
    ("BALE_ARRANGING", "Bale arranging"),
    ("OTHER", "Other"),
)
FINISHING_STOCK_STAGE_CODES = tuple(c for c, _ in FINISHING_STOCK_STAGES)
FINISHING_STOCK_QTY_UOM_BALE = "BALE"
FINISHING_STOCK_LOOSE_QTY_UOMS = ("CUTS", "BDL", "PCS")
FINISHING_STOCK_QTY_MAX = 999_999
FINISHING_STOCK_WT_MAX = 999_999          # kg, per line (loose total or per-bale)
FINISHING_STOCK_WORK_BY = (0, 1)          # 0 Dept, 1 Contract
```

### Seeds

`dbqueries/migrations/seed_finishing_stock_misc_items.sql` — variables at top
(`@co_id=1, @branch_id=2, @hess_grp=625, @sack_grp=626, @uom_id=166, @updated_by=1` for dev3;
sls values in a comment: co 2 / branch 29 / groups 2269 + 2270). Inserts, each guarded by
`NOT EXISTS (item_name in that group)`:

| item_name | item_code | group | quality_type |
|---|---|---|---|
| HESSIAN JOINT | HESS-JOINT | @hess_grp | 1 |
| REJECTED P/SHEET | HESS-REJ-PS | @hess_grp | 1 |
| SPOOL BAG | HESS-SPOOL-BAG | @hess_grp | 1 |
| PACKSHEET | SACK-PACKSHEET | @sack_grp | 2 |
| JOINT BAG | SACK-JOINT-BAG | @sack_grp | 2 |
| SHORT BAG | SACK-SHORT-BAG | @sack_grp | 2 |

item_mst: `tangible=1, saleable=1, manufacturable=1, uom_id=@uom_id, updated_by=@updated_by,
active=1`. Then one `jute_prod_finishing_quality` row per item (`co_id, branch_id,
quality_type, item_id, active=1, updated_by`), guarded by NOT EXISTS on (co_id, item_id, active=1).
dev3-only QA block (same file, clearly marked): quality row for item 268939
(HESSIAN CLOTH 38-8.12) with `std_bale_weight=437.384`.

`dbqueries/migrations/seed_finishing_stock_menu.sql` — `menu_mst` only (roles by tenant admin):

| menu_name | parent | path | order_by | icon |
|---|---|---|---|---|
| Finishing Stock | 'Jute Production' (dev3 768) | `juteProduction/finishingStock` | 167 | inventory_2 |
| Finishing Stock Report | 'Production Reports' (dev3 945) | `juteProduction/report/finishingStockReport` | 62 | assessment |

Both derived via `CONCAT(p.menu_path, ...)` off the parent row like `seed_finishing_menu.sql`.

## 3. Backend — `/api/finishingStock`

Files: `src/juteProduction/finishing_stock.py` (router), `finishing_stock_query.py`
(arg-less `text()` builders), `finishing_stock_models.py`; register in `src/main.py`
(`prefix="/api/finishingStock", tags=["jute-finishing-stock"]`). Portal persona:
`Depends(get_tenant_db)` + `Depends(get_current_user_with_refresh)`. Handlers are plain `def`.
Literal `{"data": ...}` returns. Validators raise 400 before MySQL can 500.

### GET `/setup?co_id&branch_id`

```json
{"data": {
  "qualities": [{"finishing_quality_id":1,"item_id":269959,"item_code":"531010","item_name":"HESSIAN CLOTH 40-6.5 ...","quality_type":1,"std_bale_weight":400.0,"packsheet_wt":2.0,"no_of_bags":null}],
  "stages":    [{"code":"IN_LINE","label":"Cuts in line (from weaving)"}, ...],
  "companies": [{"co_id":1,"co_name":"The Empire Jute Company Limited"}, ...],
  "loose_qty_uoms": ["CUTS","BDL","PCS"]
}}
```
qualities = active `jute_prod_finishing_quality` rows of `co_id` (both types, branch NULL-tolerant
as `get_finishing_qualities_query`), joined to item_mst, ordered quality_type, item_name.
companies = all `co_mst` rows ordered by co_name (owner picker).

### GET `/rows_by_date?co_id&branch_id&tran_date`

```json
{"data": {"rows": [ROW, ...], "prev_entry_date": "2026-09-15" | null}}
```
ROW = `{finishing_stock_id, co_id, branch_id, tran_date, bucket, finishing_quality_id, item_id,
item_code, item_name, quality_type, stage, stage_label, work_by, owner_co_id, owner_co_name,
qty, qty_uom, wt_per_unit_kg, wt_kg, total_kg, remarks}`. `owner_co_name` = own company name when
`owner_co_id` NULL. Ordered bucket (BALE first), quality_type, item_name, stage, id.
`prev_entry_date` = MAX(tran_date) < tran_date with active rows for (co, branch).

### POST `/row_create` · PUT `/row_edit/{finishing_stock_id}` · DELETE `/row_delete/{id}`

Body (Pydantic):
```json
{"co_id":1,"branch_id":2,"tran_date":"2026-09-16","bucket":"BALE","finishing_quality_id":1,
 "stage":null,"work_by":0,"owner_co_id":null,"qty":70,"qty_uom":"BALE",
 "wt_per_unit_kg":437.384,"wt_kg":null,"remarks":null}
```
Edit body = same minus `co_id/branch_id/tran_date` (identity fixed; read from the row).
Edit and delete take `?co_id=` as a query param (like `/api/windingStock`); the row's `co_id`
must match or 404.
Create → `{"data": {"finishing_stock_id": n}}`; edit → `{"data": {"updated": true}}`;
delete (soft, active=0) → `{"data": {"deleted": true}}`; missing/inactive id → 404.

Validation (400, messages as module constants like winding_stock.py):
- `tran_date` not in the future; `bucket` in BUCKETS.
- quality: active row of `co_id` (`get_finishing_quality_for_entry_query` shape) else 400.
- BALE: `stage` NULL, `work_by` 0, `qty` > 0 and ≤ QTY_MAX, `qty_uom == "BALE"`, `wt_kg` NULL,
  `wt_per_unit_kg` > 0 and ≤ WT_MAX — when omitted default to quality `std_bale_weight`; if that
  is NULL too → 400 "per-bale weight required". `owner_co_id`: NULL, or must exist in `co_mst`;
  if equal to `co_id` normalise to NULL.
- LOOSE: `owner_co_id` NULL, `stage` NULL or in STAGE_CODES, `work_by` in (0,1), `qty` ≥ 0 and
  ≤ QTY_MAX, `qty_uom` in LOOSE_QTY_UOMS, `wt_kg` required ≥ 0 and ≤ WT_MAX, `wt_per_unit_kg` NULL.
- `remarks` ≤ 255.

### POST `/copy_previous_day` body `{co_id, branch_id, tran_date}`

Clone the latest earlier day's active rows (all columns except id/date) into `tran_date`.
400 if target day already has rows; 404 if no earlier day. → `{"data": {"copied": n, "from_date": "…"}}`.

### GET `/report?co_id&branch_id&tran_date`

```json
{"data": {
  "tran_date":"2026-09-16","co_name":"…","branch_name":"Factory",
  "bales": [
    {"owner_co_id":null,"owner_co_name":"The Empire Jute Company Limited","is_own":true,
     "hessian":[{"finishing_quality_id":1,"item_id":269959,"item_name":"…","bales":70,"per_wt_kg":228.6,"wt_mt":16.002}],
     "sacking":[…],
     "hessian_total":{"bales":70,"wt_mt":16.002},"sacking_total":{"bales":4,"wt_mt":1.16},
     "total":{"bales":74,"wt_mt":17.162}}
  ],
  "loose": {"hessian":[{"finishing_quality_id":…,"item_id":…,"item_name":"…","wt_mt":0.246}],
            "sacking":[…],"hessian_total_mt":10.674,"sacking_total_mt":18.645,"total_mt":29.319},
  "loose_lines": [{"stage":"HEMMING","stage_label":"Hemming","work_by":0,"quality_type":2,"item_name":"…","qty":130,"qty_uom":"CUTS","wt_mt":1.885}],
  "despatch": [{"item_id":270002,"item_name":"…","bales":22,"qty":6.123,"uom_name":"MT"}],
  "totals": {"bales_mt":36.671,"loose_mt":29.319,"stock_mt":65.99}
}}
```
Rules:
- Own-company bale block and loose block list **every** active quality of the company for the
  section, zero-filled (the Excel shows 0.000 rows). Other owners' blocks list only qualities
  with rows. Own block first, then owners by name.
- `per_wt_kg` = total_kg / bales (weighted average over lots), 3 dp; `wt_mt` 3 dp.
- despatch = `sales_invoice si JOIN sales_invoice_dtl d … JOIN item_mst/item_grp_mst (item_type_id=5)
  LEFT JOIN sales_invoice_hessian_dtl h ON h.invoice_line_item_id = d.invoice_line_item_id
  LEFT JOIN uom_mst` where `si.invoice_date = :tran_date AND si.branch_id = :branch_id AND
  si.active = 1 AND (si.status_id IS NULL OR si.status_id NOT IN (4, 6))`, grouped by item:
  `bales = SUM(h.qty_bales)` (NULL when none), `qty = SUM(d.quantity)`, `uom_name` = first uom.
- `build_finishing_stock_report(rows, qualities, despatch_rows, own_co)` is a pure function
  (no DB) so it is unit-testable; the handler only fetches and calls it.

### Tests (`src/test/`)

`test_juteProduction_finishing_stock.py` (mocked session, house style of
`test_winding_stock_entry.py`): setup shape; create BALE default per-bale weight from quality;
create BALE with explicit weight; 400 matrix (future date, bad bucket, LOOSE with owner, BALE with
stage, missing wt_kg on LOOSE, bad stage, bad uom, per-bale weight missing when std NULL, unknown
owner co); owner == co_id normalised to NULL; edit; delete 404; copy refuses non-empty; copy 404
no previous. `test_juteProduction_finishing_stock_report.py`: `build_finishing_stock_report`
(zero-fill, owner blocks ordering, weighted per_wt, totals, despatch pass-through) + endpoint
bind completeness (`_assert_binds_complete`).

## 4. Frontend

### API constants — `src/utils/api.ts` (inside `apiRoutesPortalMasters`, after `FINISHING_PROD_*`)

```ts
FINISHING_STOCK_SETUP: `${API_URL}/finishingStock/setup`,
FINISHING_STOCK_ROWS_BY_DATE: `${API_URL}/finishingStock/rows_by_date`,
FINISHING_STOCK_ROW_CREATE: `${API_URL}/finishingStock/row_create`,
FINISHING_STOCK_ROW_EDIT: `${API_URL}/finishingStock/row_edit`,      // + /{id}
FINISHING_STOCK_ROW_DELETE: `${API_URL}/finishingStock/row_delete`,  // + /{id}
FINISHING_STOCK_COPY_PREVIOUS_DAY: `${API_URL}/finishingStock/copy_previous_day`,
FINISHING_STOCK_REPORT: `${API_URL}/finishingStock/report`,
```

### Service — `src/utils/finishingStockService.ts` (mirrors `windingStockService.ts`)

Types mirroring §3 1:1 (`FinishingStockQuality`, `FinishingStockStage`, `FinishingStockSetup`,
`FinishingStockRow`, `FinishingStockDay`, `FinishingStockPayload`, `FinishingStockReport` …) and
functions `fetchFinishingStockSetup`, `fetchFinishingStockDay`, `createFinishingStockRow`,
`editFinishingStockRow`, `deleteFinishingStockRow`, `copyFinishingStockPreviousDay`,
`fetchFinishingStockReport` — each returning `{ok, data}` / `{ok, error}` via `fetchWithCookie`.

### Entry page — `src/app/dashboardportal/juteProduction/finishingStock/`

Copy of `windingStock/` structure: `page.tsx` container (mounted gate, `useSelectedCompanyCoId`,
sidebar branch resolution, date ≤ today, setup + day loads, Copy-previous-day button with
`prev_entry_date` hint, Snackbar/Alert), `_components/FinishingStockForm.tsx`,
`_components/FinishingStockGrid.tsx`, `_components/StockTotalsFooter.tsx`,
`utils/stockCalc.ts` (+ `.test.ts`), `utils/stockSchema.ts` (zod, strings parsed at submit).

Form: **Bucket** toggle (Bale / Loose) → **Quality** (`SearchableOptionSelect`, grouped label
"Hessian · …" / "Sacking · …") →
BALE: **Owner company** (default own; `companies` from setup), **Bales**, **Per-bale kg**
(prefilled from quality `std_bale_weight` on quality change, editable) ·
LOOSE: **Stage** (optional), **Work by** (Dept / Contract), **Count** + **UoM** (CUTS/BDL/PCS),
**Weight kg** → **Remarks**. Computed read-only "Total kg / MT" preview.
Grid (`DataGrid`): rows grouped by bucket → section (Hessian / Sacking) with per-group subtotal
rows (bales, MT); edit/delete actions; footer = Bale MT (own / others), Loose MT, Total MT.

### Report page — `src/app/dashboardportal/juteProduction/report/finishingStockReport/page.tsx`

Filters: branch (`useReportBranches`, wrap in `<Suspense>`) + single date (default today), Submit.
Layout mirrors the Excel: left column = one card per owner block (title = owner co name, date)
with Hessian table (Quality · Bales · Per-bale kg · MT) + Sacking table + totals + GRAND TOTAL;
below it "Despatch on date" table (Item · Bales · Qty · UoM). Right column = LOOSE STOCK card
(Hessian rows, total; Sacking rows, total; GRAND TOTAL) and a collapsible "Loose by stage" detail
table from `loose_lines`. Footer KPI line: Bales MT · Loose MT · Stock MT. Buttons: Print
(`window.print()` + `@media print` GlobalStyles like `dailyProductionReport`) and Excel
(`exportRowsToExcel` of a flattened row list: block, section, item, bales, per_wt_kg, wt_mt).
Pure helper `flattenReport(report)` in `utils/reportShape.ts` + `.test.ts`.

Every page: `mounted` gate, `fetchWithCookie` only, no `any`, MUI `TextField type="date"`.

## 5. Delivery

Parallel agent team off this contract: **BE** (migrations → run on dev3 → model/constants/query/
router/main.py → pytest), **FE-entry**, **FE-report** (service + api.ts written up front by the
lead so both FE agents import a fixed contract). Then reviewer pass, `pytest src/test -k
finishing_stock`, `npx tsc --noEmit`, vitest, browser QA on dev3 (Empire / Factory) via
`portal-ui-flow-tester`. sls: run the 3 migrations (with sls variable block) before FE deploy.

## 6. Addendum 2026-09-17 — unit-weight prefill (user: reuse what upstream already saves)

`/setup` qualities gain three derived, read-only fields (never stored; `wt_kg` stays the weighed
figure and always wins):

| field | source | formula |
|---|---|---|
| `cut_wt_kg` | `jute_prod_weaving_quality` sharing the quality's `item_id` (co-scoped, active; if several, the highest `weaving_quality_id`) | `finished_length × ozs_yds × 28.35 / 1000` — same as weaving's production_kg |
| `bag_wt_kg` | finishing quality | `std_bale_weight / no_of_bags` (both > 0) |
| `bundle_wt_kg` | finishing spec sheet: `jute_prod_finishing_target_map` process `sacksewing`, `id_type='qid'`, `ref_id = finishing_quality_id`, `value_role='standard'`, `param='pcs_per_bundle'`, latest `effective_date ≤ today`, active | `pcs_per_bundle × bag_wt_kg` |

Any of them is `null` when unresolvable → the form leaves Weight kg manual for that unit.

Entry form (LOOSE): when the picked quality resolves a unit weight for the chosen UoM
(CUTS → cut_wt_kg, BDL → bundle_wt_kg, PCS → bag_wt_kg), Weight kg is prefilled as
`round(count × unit kg, 3)` whenever count or UoM or quality changes and the user has not typed a
weight since the last prefill (a typed value is kept until the quality/UoM changes). Helper text
shows "std 14.500 kg/bdl". BALE unchanged (per-bale kg prefill exists). No report change.

Known data gap: sls weaving qualities all point at placeholder item 335522 ("JUTE CLOTH (MIGRATED)"),
so cut prefill resolves nothing there until those rows are re-pointed to the real cloth items
(data fix, user's call); dev3 hessian items have no weaving quality yet either.
