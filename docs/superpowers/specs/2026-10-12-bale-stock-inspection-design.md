# Bale Stock & Inspection — bale-level stock derived from Bale Production, inspection marking, despatch link

**Status:** DESIGN for owner review (2026-10-12) · **Tenant:** dev3 first (sls migrations listed, not run)
**Repos:** vowerp3be (FastAPI) + vowerp3ui (Next.js) · **Module:** Jute Production
**Builds on:** `2026-09-17-finishing-stock-design.md` (Finishing Stock) and
`../../bale-production-spec-sheet.md` (Bale Production — one `jute_prod_bale_entry` row per bale).

## 0. The ask (owner, 2026-10-12)

> In the jute production module, need a new page linked to the finishing stock. An inspection takes
> place; I need to mark bales as inspected goods lying with us — sacking / govt sales only. Two
> things: (1) mark as inspected from the finishing stock when a bale line is selected there, and the
> finishing stock currently shows 0 for the non-loose stock — it should be populated from the bale
> production entries; (2) another page to select bales and mark them inspected, reflected as inspected
> in the current stock, and removed from stock when sold.

What the data says (2026-10-12): sls branch 29 has 2,705 PROD + 56 REPACK bale entries since
2026-08-26 and **zero** BALE-bucket rows in `jute_prod_finishing_stock` (114 LOOSE rows) — the
"0 bales" the owner sees. Sales invoices carry no bale numbers anywhere (`sales_invoice_hessian_dtl`
has only `qty_bales`), so "sold" has no bale-level hook today. dev3 has no bale entries at all
(test data is seeded as part of this build, §7).

## 1. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Where bale stock comes from | **Derived from `jute_prod_bale_entry`** (PROD and REPACK rows, `active = 1`) — one row is one physical bale. No new stock table; no manual re-counting of own-company bales. |
| D2 | When a bale leaves stock | (a) its number is opened for a **REPACK** (an active REPACK row in the same series carries it as `original_bale_no`; the REPACK row itself is the new bale in stock); (b) it is **linked to a sales invoice** (new `jute_prod_bale_despatch` row) whose header is not Rejected (4) / Cancelled (6); (c) its entry is soft-deleted. |
| D3 | Inspection | Per-bale marking in a new table `jute_prod_bale_inspection` (date, certificate/ref no, inspector, remarks). **Sacking bales only** (`quality_type = 2`) — owner: "only applicable for the sacking / govt sales". A bale must be in stock on the inspection date. Un-mark = soft delete. A REPACK of an inspected bale starts **uninspected** (the seal is broken when the bale is opened). |
| D4 | Sold → out of stock | Bale ↔ invoice link made **from the jute-production side** (new `jute_prod_bale_despatch`): pick an invoice of the branch (types Regular 1 / Hessian 2 / Govt Sacking 3, not status 4/6), pick the bales, save. The sales invoice pages are **not** changed in this pass; auto-picking bales while invoicing is a follow-up for the owner to decide. A despatch row pointing at an invoice that is later cancelled is void (bale back in stock) and shown as such so it can be undone / re-linked. |
| D5 | As-on date | Every stock figure is as on a date D: bale `tran_date ≤ D`; repacked only by a REPACK dated `≤ D`; despatched only by a link with `despatch_date ≤ D`; inspected only by a marking with `inspection_date ≤ D`. The Finishing Stock day view uses its `tran_date`; the new page defaults to today. |
| D6 | Weights | Derived lines use the **actual weighed net** (`Σ net_wt`), per-bale kg = total / bales, MT = kg / 1000 (3 dp). Manual BALE lines keep `qty × wt_per_unit_kg`. |
| D7 | Finishing Stock own-company bale block | When the branch has **any** bale entry dated `≤ D` (`bale_source = "BALE_PROD"`), the own-company bale block is the derived one and own-company **manual** BALE rows are not counted (returned flagged `superseded: true`, delete-only in the grid; new ones are refused with 400). Without bale production (`bale_source = "MANUAL"`) nothing changes. Manual BALE rows for **other owners** (`owner_co_id` set) stay exactly as today in both modes. |
| D8 | Where inspection is marked | (1) New page **Bale Stock & Inspection** (`juteProduction/baleStock`): filterable bale grid with checkbox + bale-number-range selection; actions Mark inspected / Undo / Link to invoice (despatch) / Undo. (2) Finishing Stock grid: each derived **sacking** line has a "Mark inspected" action that opens the same dialog pre-filtered to that quality and date. |
| D9 | Despatch without inspection | Allowed (hessian never needs it; the mill may ship uninspected sacking against non-govt orders). The response reports `uninspected_sacking` so the UI can warn. |
| D10 | Menus | One `menu_mst` row 'Bale Stock & Inspection' → `juteProduction/baleStock` under 'Jute Production' (order_by 168, right after Finishing Stock 167), `role_menu_map` mirrored from 'Finishing Stock'. |

## 2. Data model — two new tenant tables

`dbqueries/migrations/20261012_jute_prod_bale_inspection_despatch.sql` (+ rollback block).
ORM: `src/juteProduction/bale_stock_models.py` (SQLAlchemy 2.0 `Mapped`, shared `Base`).

```sql
CREATE TABLE jute_prod_bale_inspection (
  bale_inspection_id   INT AUTO_INCREMENT PRIMARY KEY,
  co_id                INT NOT NULL,
  branch_id            INT NOT NULL,
  bale_entry_id        INT NOT NULL,                 -- jute_prod_bale_entry (sacking bale)
  inspection_date      DATE NOT NULL,
  inspection_ref       VARCHAR(100) NULL,            -- certificate / inspection note no
  inspector            VARCHAR(100) NULL,            -- agency / inspector name
  remarks              VARCHAR(255) NULL,
  active               TINYINT NOT NULL DEFAULT 1,
  updated_by           INT NULL,
  updated_date_time    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_bi_bale (bale_entry_id, active),
  KEY idx_bi_co_branch_date (co_id, branch_id, inspection_date),
  CONSTRAINT fk_bi_bale FOREIGN KEY (bale_entry_id) REFERENCES jute_prod_bale_entry (bale_entry_id)
);

CREATE TABLE jute_prod_bale_despatch (
  bale_despatch_id     INT AUTO_INCREMENT PRIMARY KEY,
  co_id                INT NOT NULL,
  branch_id            INT NOT NULL,
  bale_entry_id        INT NOT NULL,
  invoice_id           BIGINT NOT NULL,              -- sales_invoice.invoice_id
  invoice_line_item_id BIGINT NULL,                  -- sales_invoice_dtl, optional (future: per-line pick)
  despatch_date        DATE NOT NULL,                -- defaults to the invoice date
  remarks              VARCHAR(255) NULL,
  active               TINYINT NOT NULL DEFAULT 1,
  updated_by           INT NULL,
  updated_date_time    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_bd_bale (bale_entry_id, active),
  KEY idx_bd_invoice (invoice_id),
  KEY idx_bd_co_branch_date (co_id, branch_id, despatch_date),
  CONSTRAINT fk_bd_bale FOREIGN KEY (bale_entry_id) REFERENCES jute_prod_bale_entry (bale_entry_id)
);
```

One **active** row per bale in each table is an app-level rule (soft-delete pattern, no unique key).
No `created_*` columns (trigger audit). `jute_prod_bale_entry` is unchanged.

### Constants (`src/juteProduction/constants.py`, appended)

```python
BALE_STOCK_STATUS_IN_STOCK = "IN_STOCK"      # pressed, not inspected, not out
BALE_STOCK_STATUS_INSPECTED = "INSPECTED"    # in stock AND inspected
BALE_STOCK_STATUS_DESPATCHED = "DESPATCHED"  # linked to a live invoice
BALE_STOCK_STATUS_REPACKED = "REPACKED"      # opened for a REPACK
BALE_STOCK_STATUSES = (IN_STOCK, INSPECTED, DESPATCHED, REPACKED)
BALE_STOCK_IN_STOCK_STATUSES = (IN_STOCK, INSPECTED)
# grid filter codes: STOCK = both in-stock statuses; ALL = no filter
BALE_STOCK_FILTERS = ("STOCK", "INSPECTED", "NOT_INSPECTED", "DESPATCHED", "REPACKED", "ALL")
BALE_STOCK_ROW_CAP = 3000
BALE_DESPATCH_INVOICE_TYPE_KEYS = ("REGULAR", "HESSIAN", "GOVT_SKG")   # sales.constants.INVOICE_TYPE_IDS
BALE_INSPECTION_REF_MAX = 100
BALE_INSPECTION_INSPECTOR_MAX = 100
BALE_STOCK_SOURCE_BALE_PROD = "BALE_PROD"
BALE_STOCK_SOURCE_MANUAL = "MANUAL"
```

### Menu seed

`dbqueries/migrations/20261012_seed_bale_stock_menu.sql` — `menu_mst` row (name 'Bale Stock &
Inspection', path `CONCAT(parent, '/baleStock')`, icon `fact_check`, order_by 168, module 'Jute
Production') + `role_menu_map` mirrored from 'Finishing Stock' (same access level per role).
Nothing hardcodes an id; the runner note (`;` after comment blocks) as in `seed_bale_production_menus.sql`.

## 3. Status derivation (one SQL, one pure function)

`bale_stock_query.get_bale_stock_rows_query()` — every filter optional (`:x IS NULL OR …`), rows
capped by `:row_cap + 1` so the router can report `truncated`:

```sql
SELECT * FROM (
  SELECT be.bale_entry_id, be.tran_date, be.entry_type, be.bale_series_id, bs.series_name,
         be.bale_no, be.original_bale_no, be.finishing_quality_id, q.item_id, im.item_name,
         q.quality_type, be.gross_wt, be.net_wt, be.weigh_time,
         rp.bale_entry_id AS repacked_by_id, rp.bale_no AS repacked_by_no, rp.tran_date AS repacked_on,
         ins.bale_inspection_id, ins.inspection_date, ins.inspection_ref, ins.inspector,
         ins.remarks AS inspection_remarks,
         dp.bale_despatch_id, dp.invoice_id, dp.despatch_date, dp.remarks AS despatch_remarks,
         si.invoice_no, si.invoice_date, si.invoice_type, si.status_id AS invoice_status_id,
         p.supp_name AS party_name,
         CASE WHEN rp.bale_entry_id IS NOT NULL THEN 'REPACKED'
              WHEN dp.bale_despatch_id IS NOT NULL
                   AND (si.status_id IS NULL OR si.status_id NOT IN :cancelled_status) THEN 'DESPATCHED'
              WHEN ins.bale_inspection_id IS NOT NULL THEN 'INSPECTED'
              ELSE 'IN_STOCK' END AS status
  FROM jute_prod_bale_entry be
  LEFT JOIN jute_prod_finishing_quality q ON q.finishing_quality_id = be.finishing_quality_id
  LEFT JOIN item_mst im ON im.item_id = q.item_id
  LEFT JOIN jute_prod_bale_series bs ON bs.bale_series_id = be.bale_series_id
  -- a REPACK consumes the most recent earlier active bale carrying its original number in the series
  LEFT JOIN (
      SELECT r.bale_entry_id, r.bale_no, r.tran_date,
             (SELECT MAX(b.bale_entry_id) FROM jute_prod_bale_entry b
               WHERE b.bale_series_id = r.bale_series_id AND b.bale_no = r.original_bale_no
                 AND b.active = 1 AND b.bale_entry_id < r.bale_entry_id) AS consumed_id
      FROM jute_prod_bale_entry r
      WHERE r.entry_type = 'REPACK' AND r.active = 1 AND r.original_bale_no IS NOT NULL
        AND r.co_id = :co_id AND r.branch_id = :branch_id AND r.tran_date <= :as_on
  ) rp ON rp.consumed_id = be.bale_entry_id
  LEFT JOIN jute_prod_bale_inspection ins
         ON ins.bale_entry_id = be.bale_entry_id AND ins.active = 1 AND ins.inspection_date <= :as_on
  LEFT JOIN jute_prod_bale_despatch dp
         ON dp.bale_entry_id = be.bale_entry_id AND dp.active = 1 AND dp.despatch_date <= :as_on
  LEFT JOIN sales_invoice si ON si.invoice_id = dp.invoice_id
  LEFT JOIN party_mst p ON p.party_id = si.party_id
  WHERE be.active = 1 AND be.co_id = :co_id AND be.branch_id = :branch_id AND be.tran_date <= :as_on
    AND (:quality_type IS NULL OR q.quality_type = :quality_type)
    AND (:finishing_quality_id IS NULL OR be.finishing_quality_id = :finishing_quality_id)
    AND (:bale_series_id IS NULL OR be.bale_series_id = :bale_series_id)
    AND (:bale_no_from IS NULL OR be.bale_no >= :bale_no_from)
    AND (:bale_no_to IS NULL OR be.bale_no <= :bale_no_to)
    AND (:date_from IS NULL OR be.tran_date >= :date_from)
    AND (:date_to IS NULL OR be.tran_date <= :date_to)
    AND (:bale_entry_ids_set = 0 OR be.bale_entry_id IN :bale_entry_ids)
) x
WHERE (:status_filter IS NULL
       OR (:status_filter = 'STOCK' AND x.status IN ('IN_STOCK', 'INSPECTED'))
       OR (:status_filter = 'NOT_INSPECTED' AND x.status = 'IN_STOCK')
       OR (:status_filter IN ('INSPECTED', 'DESPATCHED', 'REPACKED') AND x.status = :status_filter))
ORDER BY x.quality_type, x.series_name, x.bale_no, x.bale_entry_id
LIMIT :row_cap
```

`:cancelled_status` and `:bale_entry_ids` are EXPANDING binds; when no id list is given the router
passes `bale_entry_ids_set = 0` and `bale_entry_ids = [0]`. `ALL` maps to `status_filter = None`.

`get_bale_stock_summary_query()` = the inner select grouped by quality (`bales` = rows with status in
the in-stock pair, `inspected_bales`, `total_kg`, `inspected_kg`, plus `despatched_bales` to date for
information). Ordered quality_type, item_name.

`bale_stock.serialize_bale_row(row) -> dict` turns the flat row into the BaleStockRow shape (§4) —
nested `inspection` / `despatch` / `repacked_by` objects or `None`; Decimals → float, dates → str.

## 4. Backend — `/api/baleStock` (new router `src/juteProduction/bale_stock.py`)

Portal persona (`Depends(get_tenant_db)` + `Depends(get_current_user_with_refresh)`), plain `def`,
`{"data": …}`, 400 before MySQL 500, co_id on every read guard and write. Registered in `src/main.py`
as `prefix="/api/baleStock", tags=["jute-bale-stock"]`. Files: `bale_stock.py`,
`bale_stock_query.py`, `bale_stock_models.py`.

### GET `/setup?co_id&branch_id`
```json
{"data": {
  "qualities": [{"finishing_quality_id":2,"item_id":270011,"item_name":"PRINTED TYPE A …","quality_type":2}],
  "series":    [{"bale_series_id":1,"series_name":"PUNJAB","quality_type":2,"item_id":270011}],
  "statuses":  [{"code":"STOCK","label":"In stock (all)"},{"code":"INSPECTED","label":"Inspected"},
                {"code":"NOT_INSPECTED","label":"Not inspected"},{"code":"DESPATCHED","label":"Despatched"},
                {"code":"REPACKED","label":"Repacked"},{"code":"ALL","label":"All"}],
  "bale_source": "BALE_PROD" | "MANUAL"
}}
```
qualities = `get_bale_qualities_query` rows (reuse); series = active series of the branch
(`get_bale_series_list_query` or a slim builder); `bale_source` = BALE_PROD when any active bale entry
exists for (co, branch) dated ≤ today.

### GET `/bales?co_id&branch_id&as_on[&status=STOCK][&quality_type][&finishing_quality_id][&bale_series_id][&bale_no_from][&bale_no_to][&date_from][&date_to]`
```json
{"data": {"as_on":"2026-10-12","rows":[ROW…],"truncated":false}}
```
ROW (BaleStockRow):
```json
{"bale_entry_id":17,"tran_date":"2026-10-10","entry_type":"PROD","bale_series_id":1,"series_name":"PUNJAB",
 "bale_no":612,"original_bale_no":null,"finishing_quality_id":2,"item_id":270011,"item_name":"…","quality_type":2,
 "gross_wt":340.0,"net_wt":290.0,"status":"INSPECTED",
 "inspection":{"bale_inspection_id":3,"inspection_date":"2026-10-11","inspection_ref":"IC/22","inspector":"IJIRA","remarks":null},
 "despatch":null,
 "repacked_by":null}
```
`despatch` when present: `{bale_despatch_id, invoice_id, invoice_no, invoice_date, invoice_type,
invoice_status_id, party_name, despatch_date, remarks}` — it is returned even when the invoice is
cancelled (status then reads IN_STOCK / INSPECTED and the FE shows "linked to cancelled invoice").
`repacked_by`: `{bale_entry_id, bale_no, tran_date}`. Default `status` filter = `STOCK`; `as_on`
defaults to today and may not be in the future (400).

### GET `/summary?co_id&branch_id&as_on`
```json
{"data": {"as_on":"…","bale_source":"BALE_PROD",
  "lines":[{"finishing_quality_id":2,"item_id":270011,"item_name":"…","quality_type":2,
            "bales":120,"inspected_bales":80,"total_kg":34800.0,"inspected_kg":23200.0,
            "per_wt_kg":290.0,"wt_mt":34.8,"despatched_bales":45}],
  "totals":{"bales":…, "inspected_bales":…, "wt_mt":…, "hessian":{"bales":…,"wt_mt":…},"sacking":{…}}}}
```
`build_bale_stock_summary(rows) -> dict` is pure (also used by Finishing Stock, §5).

### GET `/invoices?co_id&branch_id&as_on[&days=120]`
Candidate invoices for the despatch link: `sales_invoice` of the branch, `invoice_type` in
BALE_DESPATCH_INVOICE_TYPE_KEYS, `COALESCE(active,1)=1`, status not in (4,6), `invoice_date` within
`as_on - days … as_on`, newest first, with their cloth lines and how many bales are already linked:
```json
{"data": {"rows":[{"invoice_id":171,"invoice_no":52,"invoice_date":"2026-05-08","invoice_type":3,
  "invoice_type_name":"Govt Sacking","party_name":"BIHAR STATE FOOD CIVIL SUPPLIES","status_id":3,
  "lines":[{"invoice_line_item_id":…,"item_id":269961,"item_name":"…","quantity":240,"uom_name":"100 PCS","qty_bales":null}],
  "linked_bales":0}]}}
```

### POST `/inspect`
Body `{co_id, branch_id, bale_entry_ids: [..], inspection_date, inspection_ref?, inspector?, remarks?}`.
Gates (400, message constants): ids non-empty and ≤ 500; `inspection_date` ≤ today; every id resolves
via the rows query (filter by id list, `as_on = inspection_date`, no status filter) to this co/branch
— else 404 `BALE_NOT_FOUND_MSG`; `quality_type == 2` else `INSPECT_SACKING_ONLY_MSG` ("Only sacking
bales are inspected — bale {no} ({item}) is hessian"); `tran_date ≤ inspection_date` else
`INSPECT_BEFORE_PRESS_MSG`; status `IN_STOCK` — `INSPECTED` → `ALREADY_INSPECTED_MSG`,
`DESPATCHED`/`REPACKED` → `NOT_IN_STOCK_MSG`; `inspection_ref` ≤ 100, `inspector` ≤ 100,
`remarks` ≤ 255. Then one INSERT per bale (`executemany`-style loop), commit →
`{"data": {"marked": n}}`.

### POST `/uninspect` body `{co_id, bale_entry_ids}`
Soft-deletes the active inspection rows of those bales (join on `jute_prod_bale_entry.co_id = :co_id`)
→ `{"data": {"unmarked": n}}`. 404 when none of the ids has an active marking.

### POST `/despatch`
Body `{co_id, branch_id, bale_entry_ids, invoice_id, despatch_date?, remarks?}`.
Invoice gates: exists, `branch_id` matches (`INVOICE_BRANCH_MSG`), type allowed
(`INVOICE_TYPE_MSG`), status not 4/6 (`INVOICE_CANCELLED_MSG`). `despatch_date` defaults to
`invoice_date`; ≤ today. Bales via the rows query with `as_on = despatch_date`: status in the
in-stock pair else `NOT_IN_STOCK_MSG` / `ALREADY_DESPATCHED_MSG` (a live link exists);
`tran_date ≤ despatch_date`. INSERT per bale →
`{"data": {"linked": n, "uninspected_sacking": m}}` (m = sacking bales linked without a marking, D9).

### POST `/undespatch` body `{co_id, bale_entry_ids}` → `{"data": {"unlinked": n}}`.

### Tests — `src/test/test_juteProduction_bale_stock.py`
Mocked session in the house style (`test_juteProduction_finishing_stock.py`): setup shape +
`bale_source`; `/bales` default filter + `truncated`; `serialize_bale_row` nesting;
`build_bale_stock_summary` totals (pure); `/inspect` happy path + the 400 matrix (hessian, already
inspected, despatched, repacked, before press date, future date, too many ids, ref too long);
`/despatch` happy path + invoice gates; `/uninspect` / `/undespatch` 404 when nothing active;
`_assert_binds_complete` on every statement.

## 5. Finishing Stock changes (`finishing_stock.py` / `finishing_stock_query.py`)

- New builder `get_finishing_stock_bale_source_query()` — `SELECT 1 … jute_prod_bale_entry WHERE
  co_id, branch_id, active=1, tran_date <= :tran_date LIMIT 1`. Helper `_bale_source(db, co_id,
  branch_id, as_on) -> "BALE_PROD" | "MANUAL"`.
- **GET `/setup`** adds `"bale_source"` (as of today).
- **GET `/rows_by_date`** → `{"rows", "prev_entry_date", "bale_source", "derived_bales"}`.
  `derived_bales` = `build_bale_stock_summary(...)["lines"]` for `tran_date` (empty list in MANUAL
  mode). Each manual row gains `"superseded": bool` — true for own-company BALE rows when
  `bale_source == "BALE_PROD"`.
- **GET `/report`** → when BALE_PROD, `build_finishing_stock_report(...)` takes `derived_lines` and
  builds the own block from them: `bales` = count, `per_wt_kg` = total_kg / bales, `wt_mt`, plus
  `"inspected_bales"` on every bale line (null on manual / other-owner lines) and `"bale_source"` at
  the top level. Own-company manual BALE rows are dropped from the report in that mode. The report's
  own block stays zero-filled over every active quality of the company.
- **POST `/row_create` / PUT `/row_edit`** → 400 `OWN_BALE_DERIVED_MSG` ("Own-company bales are
  counted from Bale Production for this date — use the Bale bucket only for bales held for another
  company") when bucket BALE, owner resolves to own company and `_bale_source(tran_date) == BALE_PROD`.
- Tests: extend `test_juteProduction_finishing_stock_report.py` for the derived own block and
  `test_juteProduction_finishing_stock.py` for the 400 + response keys.

## 6. Frontend

### API constants — `src/utils/api.ts` (`apiRoutesPortalMasters`, after `FINISHING_STOCK_*`)
```ts
BALE_STOCK_SETUP: `${API_URL}/baleStock/setup`,
BALE_STOCK_BALES: `${API_URL}/baleStock/bales`,
BALE_STOCK_SUMMARY: `${API_URL}/baleStock/summary`,
BALE_STOCK_INVOICES: `${API_URL}/baleStock/invoices`,
BALE_STOCK_INSPECT: `${API_URL}/baleStock/inspect`,
BALE_STOCK_UNINSPECT: `${API_URL}/baleStock/uninspect`,
BALE_STOCK_DESPATCH: `${API_URL}/baleStock/despatch`,
BALE_STOCK_UNDESPATCH: `${API_URL}/baleStock/undespatch`,
```

### Service — `src/utils/baleStockService.ts`
Types mirroring §4 1:1 (`BaleStockStatus`, `BaleStockFilter`, `BaleStockSetup`, `BaleStockRow`,
`BaleStockInspection`, `BaleStockDespatch`, `BaleStockSummaryLine`, `BaleStockSummary`,
`BaleStockInvoice`, payloads) and functions `fetchBaleStockSetup`, `fetchBaleStockRows`,
`fetchBaleStockSummary`, `fetchBaleStockInvoices`, `markBalesInspected`, `unmarkBalesInspected`,
`linkBalesToInvoice`, `unlinkBalesFromInvoice` — `{ok, error, …}` via `fetchWithCookie`.
`finishingStockService.ts` gains `bale_source`, `derived_bales`, `superseded`, `inspected_bales`.

### Shared components — `src/components/juteProduction/baleStock/`
- `BaleStockGrid.tsx` — DataGrid (v8 selection model: `{type, ids}` → `idsOf()` helper as in
  `hrms/longAbsenteeism/_components/WatchlistGrid.tsx`), columns Date · Series · Bale No · Quality ·
  Type (Prod/Repack chip) · Net kg · Status chip · Inspection (date · ref) · Invoice (no · date · party);
  a despatch row whose invoice is cancelled shows a warning chip "invoice cancelled".
- `BaleRangeSelector.tsx` — Series + From–To bale no → selects the matching loaded rows (client-side
  helper `selectRange(rows, seriesId, from, to)` in `baleStockCalc.ts`).
- `MarkInspectedDialog.tsx` — date (default today, max today), inspection ref, inspector, remarks;
  shows the count and the quality list of the selection; disabled with a reason when any selected
  bale is hessian or not in stock.
- `LinkInvoiceDialog.tsx` — `SearchableOptionSelect` over `/invoices` (label `#no · date · party ·
  type`), despatch date (default invoice date), remarks; warns when sacking bales in the selection
  are uninspected (D9) and when the invoice already has linked bales.
- `BaleInspectQuickDialog.tsx` — the one Finishing Stock opens: loads `/bales?status=NOT_INSPECTED&
  finishing_quality_id&as_on=tran_date`, shows the grid + range selector + the Mark-inspected form in
  one dialog, returns `{marked}` so the page refreshes its day.
- `baleStockCalc.ts` (+ `.test.ts`): `selectRange`, `statusLabel`, `statusColor`, `canInspect(rows)`,
  `summaryTotals(lines)`, `formatKg`.

### New page — `src/app/dashboardportal/juteProduction/baleStock/page.tsx`
Container (mounted gate, `useSelectedCompanyCoId`, sidebar-branch resolution exactly as
`finishingStock/page.tsx`): header "Bale Stock & Inspection", As-on date (≤ today), summary table
(Quality · In stock · Inspected · Not inspected · MT; Hessian / Sacking subtotals, chips for totals),
filter bar (`_components/BaleStockFilters.tsx`: status, quality type, quality, series, bale no from/to,
pressed-date from/to, Apply / Reset), selection toolbar (count · Mark inspected · Undo inspection ·
Link to invoice · Unlink), the shared grid with the range selector, Snackbar/Alert feedback,
"truncated — narrow the filters" notice. Refresh rows + summary after every mutation.

### Finishing Stock page changes
- `FinishingStockGrid.tsx`: when `bale_source === "BALE_PROD"` the first block is
  **"Bales · from Bale Production"** (Hessian, Sacking sections) built from `derived_bales`: Quality ·
  Bales · Inspected · Kg/Bale · MT, no edit/delete, and a "Mark inspected" icon action on sacking
  lines (opens `BaleInspectQuickDialog`); manual own-company rows flagged `superseded` render greyed
  under "Manual own-company bales (not counted)" with delete only. Subtotals / `StockTotalsFooter`
  count derived lines as "Bales (own)" and skip superseded rows (`footerTotals(rows, derived)`).
- `FinishingStockForm.tsx`: in BALE_PROD mode the Owner picker drops the own company, label "Owner
  (other company)", helper "Own-company bales are counted from Bale Production".
- `page.tsx`: pass `bale_source` / `derived_bales`, host the quick dialog, add a "Bale Stock &
  Inspection" link button.
- `finishingStockReport/page.tsx` + `utils/reportShape.ts`: own block shows an **Inspected** column
  when `bale_source === "BALE_PROD"` (sacking lines; "—" on hessian); `flattenReport` gains
  `inspected` in `FlatRow` / `EXPORT_COLUMNS`; `.test.ts` updated.

Every page: `mounted` gate, `fetchWithCookie` only through services, no `any`, searchable dropdowns
only, zod on the dialog forms, theme tokens.

## 7. Delivery

1. Migrations (two scripts) → run on **dev3** (owner's standing default; sls after the owner OKs).
2. Backend router + finishing-stock changes + pytest.
3. Frontend service + shared components + page + finishing-stock changes + vitest.
4. **dev3 test data** (dev3 has no bale entries): seed 3 bale series (PUNJAB, BIHAR sacking;
   one hessian) and ~40 PROD + 2 REPACK entries over three days for Empire / Factory through the real
   `/api/baleProd` endpoints, so the DPR sync runs too. Script kept server-side
   (`/root/projects/vowerp3/.claude/tools/seed_dev3_bales.sh`), not committed.
5. Verify: `uv run pytest src/test -k "bale_stock or finishing_stock"`, `pnpm exec tsc --noEmit`,
   vitest, eslint on touched files, then a headless-browser pass on dev3 (login → Jute Production →
   Bale Stock & Inspection; Finishing Stock derived block; mark → check DB).
6. Preview URLs for the owner: `http://dev3.187-77-145-36.nip.io:3000/dashboardportal/juteProduction/baleStock`
   and `…/juteProduction/finishingStock` (log out / in once — the sidebar caches menus).

## 7a. Series-wise breakup of sacking bales (owner ask, 2026-10-12, after the first preview)

Sacking bales are numbered per consignee series (PUNJAB, BIHAR, FCI …), and that is how the mill
despatches and gets them inspected, so every stock view gets a series breakup next to the
quality-wise figures.

Backend (same status derivation, grouped by series as well as quality):
- `bale_stock_query.get_bale_stock_series_summary_query()` — the inner select of §3 grouped by
  `(bale_series_id, series_name, finishing_quality_id, item_id, item_name, quality_type)` with the
  same measures as the quality summary (`bales`, `inspected_bales`, `total_kg`, `inspected_kg`,
  `despatched_bales`); ordered quality_type, series_name, item_name.
- `build_bale_stock_summary(rows, series_rows=None)` adds `"series_lines": [SeriesLine]` where
  SeriesLine = the summary-line fields + `bale_series_id`, `series_name` (`per_wt_kg`, `wt_mt`
  computed the same way). All quality types are returned; consumers filter to sacking.
- GET `/baleStock/summary` → `series_lines` added.
- GET `/finishingStock/rows_by_date` → `"derived_series"`: the sacking (`quality_type = 2`) series
  lines for the day (`[]` in MANUAL mode).
- GET `/finishingStock/report` → the own block gains `"series": [{bale_series_id, series_name,
  finishing_quality_id, item_name, bales, inspected_bales, per_wt_kg, wt_mt}]` (sacking only,
  BALE_PROD mode, series with 0 bales omitted) and `"series_total": {bales, inspected_bales, wt_mt}`;
  other-owner blocks and MANUAL mode carry `"series": []` and a zero `series_total`.

Frontend:
- `baleStockService.ts`: `BaleStockSeriesLine = BaleStockSummaryLine & {bale_series_id, series_name}`;
  `BaleStockSummary.series_lines`. `finishingStockService.ts`: `FinishingStockDay.derived_series`,
  `FinishingStockBaleBlock.series` + `series_total`.
- Bale Stock page: `_components/BaleSeriesSummaryTable.tsx` "Sacking bales by series" under the
  quality summary — Series · Quality · In stock · Inspected · Not inspected · Per-bale kg · MT, a
  Sacking total row; clicking a series row applies that series as the grid filter.
- Finishing Stock grid: under each derived sacking quality line, one indented child row per series
  ("↳ PUNJAB") with bales, inspected, kg/bale, MT; no actions, not counted twice in subtotals.
- Finishing Stock Report: inside the own bale card (BALE_PROD mode) a "Sacking bales by series"
  table after the grand total — Series · Quality · Bales · Inspected · Per-bale Kg · MT + total;
  `flattenReport` emits those rows with section `SACKING BY SERIES` and Excel gets them too.

## 7b. Despatch without an invoice (owner option 2, 2026-10-12)

Owner question: "does it have to be linked to invoice?" — answer chosen: **no**. Bales can be marked
despatched on a delivery challan / delivery order / transfer / sample, and linked to the invoice later.

Data: `20261012b_jute_prod_bale_despatch_no_invoice.sql` — `invoice_id` NULLable, new
`despatch_reason VARCHAR(20)` (code) and `despatch_ref VARCHAR(100)` (challan / DO / gate-pass no).

Constants: `BALE_DESPATCH_REASONS = (("CHALLAN","Delivery challan"), ("DELIVERY_ORDER","Delivery
order"), ("TRANSFER","Transfer to another unit"), ("SAMPLE","Sample"), ("OTHER","Other"))`,
`BALE_DESPATCH_REASON_CODES`, `BALE_DESPATCH_REF_MAX = 100`.

Backend:
- Status: `DESPATCHED` when an active despatch row exists AND (`invoice_id IS NULL` OR the invoice is
  not Rejected/Cancelled). A no-invoice despatch is never void.
- Rows: `despatch` gains `invoice_id: int|null`, `despatch_reason`, `despatch_reason_label`,
  `despatch_ref` (invoice fields null when no invoice).
- GET `/setup` adds `despatch_reasons: [{code,label}]`.
- POST `/despatch` body: `invoice_id` optional; `despatch_date` (required when no invoice, else
  defaults to the invoice date), `despatch_reason` (required when no invoice, else optional, must be a
  code), `despatch_ref` (≤ 100, optional), `remarks`. Rules per selected bale: in stock → INSERT;
  already despatched **without** invoice and the body carries an invoice → UPDATE that row's
  `invoice_id` (+ reason/ref/remarks if given; despatch date untouched) = "link later"; already
  despatched with a live invoice → 400 `ALREADY_DESPATCHED_MSG`; repacked → 400. Response
  `{linked, relinked, uninspected_sacking}`.
- POST `/undespatch` unchanged (soft-deletes the row, with or without invoice).
- GET `/invoices.linked_bales` unchanged.

Frontend:
- `BaleStockDespatch.invoice_id: number | null`, `despatch_reason`, `despatch_reason_label`,
  `despatch_ref`; `BaleStockSetup.despatch_reasons`; `LinkInvoicePayload.invoice_id: number | null`,
  `despatch_reason`, `despatch_ref`; `linkBalesToInvoice` returns `relinked` too.
- The shared invoice form (`InvoiceLinkFields` / `useInvoiceLinkForm` / `linkSchema`) gets a mode
  toggle **Against invoice / Without invoice**. Without invoice: Reason (searchable, required),
  Reference no (challan / DO), despatch date (required, default = as-on date capped at today),
  remarks. Against invoice: as today, plus optional Reference no. When the selection already holds
  bales despatched without invoice, the form shows "n bales despatched without invoice will be linked
  to this invoice" and forces the Against-invoice mode.
- Grid: status chip "Despatched"; Invoice column shows `#no · date · party` or, without invoice,
  `No invoice · <reason label> · <ref>`; `canLink` also allows DESPATCHED rows whose despatch has no
  invoice (so Link to invoice works on them); toolbar label stays "Link to invoice / Mark despatched".
- Finishing Stock despatch quick dialog and the Bale Stock page both use the shared form, so both get
  the option.

## 7c. Bale Register report — inspection & despatch register (owner ask, 2026-10-12)

> "need to make an inspection and dispatch report. this can actually be something like a register,
> which has opening, production, despatch, closing" — and then: "inspection not required in this as
> already covered in the finishing stock report".

A stock register for a date range, day by day, per quality (default) or per sacking series, built
from the same events as the stock page: bale entries (pressed), REPACK consumptions (repacked out)
and live despatch links (despatched, split invoice / no-invoice). **No inspection figures** — the
Finishing Stock report already carries them (owner, 2026-10-12).

Definitions for a group G and day d (all `active = 1`; "live" despatch = `invoice_id IS NULL` or the
invoice not Rejected/Cancelled):
- `opening(d)` = bales of G with `tran_date < d` − repacked out before d − despatched before d.
- `pressed(d)` = entries of G with `tran_date = d` (PROD **and** REPACK — a REPACK row is a new bale).
- `repacked_out(d)` = bales of G consumed by a REPACK dated d (the §3 "consumed_id" rule).
- `despatched_invoice(d)` / `despatched_no_invoice(d)` = live despatch rows of G dated d; `despatched = sum`.
- `closing(d)` = opening + pressed − repacked_out − despatched (= opening(d+1)).
Period figures: opening = opening(date_from); pressed / repacked / despatched = sums; closing =
closing(date_to); `closing_kg` / `opening_kg` = Σ net_wt of the bales in stock (MT at 3 dp).

Backend: NEW router `src/juteProduction/bale_register.py` (+ `bale_register_query.py`) registered
under the existing `/api/juteProductionReports` prefix (like `bale_reports.py`), endpoint
`GET /bale_register?co_id&branch_id&date_from&date_to[&quality_type][&finishing_quality_id][&bale_series_id][&group_by=quality|series]`.
Range ≤ 92 days (400 otherwise), `date_to` ≤ today, `date_from` ≤ `date_to`. Fetch once (bales of the
branch dated ≤ date_to with quality/series labels; repack consumptions; despatch rows dated ≤ date_to
joined to the invoice header for status/no/date/party), then
`build_bale_register(...)` — PURE, unit-tested — produces:

```json
{"data": {
  "date_from":"2026-10-06","date_to":"2026-10-12","group_by":"quality","co_name":"…","branch_name":"…",
  "groups":[{"key":"q:2","finishing_quality_id":2,"item_name":"…","quality_type":2,"bale_series_id":null,"series_name":null,
    "days":[{"date":"2026-10-06","opening":100,"pressed":13,"repacked_out":0,"despatched_invoice":2,"despatched_no_invoice":0,"despatched":2,"closing":111}, …],
    "period":{"opening":100,"opening_kg":29000.0,"opening_mt":29.0,"pressed":39,"repacked_out":2,"despatched_invoice":2,"despatched_no_invoice":2,"despatched":4,"closing":133,"closing_kg":38570.0,"closing_mt":38.57}}],
  "totals":{"hessian":PERIOD,"sacking":PERIOD,"all":PERIOD},
  "despatch_detail":[{"date":"2026-10-12","series_name":"PUNJAB","item_name":"…","quality_type":2,"bale_nos":"606-607","count":2,"invoice_id":null,"invoice_no":null,"invoice_date":null,"party_name":null,"despatch_reason_label":"Delivery challan","despatch_ref":"CH-77","remarks":null}]
}}
```
`days` lists EVERY calendar day of the range (zero rows included — it is a register). `group_by=series`
keys groups by (bale_series_id, finishing_quality_id) (`"key":"s:2:2"`); hessian bales group by their
per-quality series the same way. Detail rows group the despatches of one day × series × quality ×
(invoice or reason/ref) and compress bale numbers with
`compress_bale_nos([601,603,604]) -> "601, 603-604"` (pure, tested). Groups with no activity and zero
opening are omitted; groups are ordered quality_type, item_name, series_name.

Frontend: report page `src/app/dashboardportal/juteProduction/report/baleRegister/page.tsx`
(shell as `report/finishingStockReport/page.tsx`: `useReportBranches`, Submit, Print with the
mandatory `@page { margin: 0 }` rule, Excel via `exportRowsToExcel`), filters: branch, date from / to
(default: last 7 days ending today), quality type (All / Hessian / Sacking), quality, series
(sacking), group-by toggle (Quality / Series). Sections: (1) **Register** — one card per group with
the day table (Date · Opening · Pressed · Repacked out · Despatched (Invoice) · Despatched (Challan) ·
Closing) and a period row; (2) **Period summary** table across groups (+ Hessian / Sacking / All
totals, closing MT); (3) **Despatch detail** table (date · series · quality · bale nos · count ·
invoice or challan reference · party). Service function in `src/utils/juteProductionReportService.ts`; constant
`BALE_REPORT_REGISTER` in `api.ts`; pure `flattenRegister` (+ test) for Excel.
Menu: `20261012c_seed_bale_register_menu.sql` — 'Bale Register' → `juteProduction/report/baleRegister`
under 'Production Reports' (report = 1, order_by MAX+1, role_menu_map mirrored from the hub, as
`seed_bale_production_menus.sql` rows 3–5).

## 7d. Backdating window — 60 days (owner ask, 2026-10-12)

> "need to allow backdated entries for upto 60 days"

Every date a user types in this feature may be backdated up to **60 days** before today and no
further: inspection date, despatch date, Finishing Stock count date and Bale Production press date
(the Bale Stock "as on" date stays free — it is a view, not an entry). The existing floors stay:
inspection / despatch not before the bale's press date. Constant `BACKDATE_DAYS_MAX = 60`
(`src/juteProduction/constants.py`); backend 400 `"{field} cannot be more than 60 days in the past"`;
the date inputs carry `min = max(today − 60 days, last press date)` and the zod schemas refuse older
dates with the same wording.

## 7e. Suggested bale pick by state + count; bales picked on the sales invoice (owner, 2026-10-12)

Owner rulings closing §8: **O1 + O2** — manual pick with a suggested default: the user enters the
state (bale series) and the number of bales, the system proposes that many bales, the user adjusts
by hand. This applies to the inspection marking AND to the sale, which is now picked on the sales
invoice itself. **O3** — hessian never gets the inspection mark (already enforced).

Backend (`bale_stock.py`):
- `GET /baleStock/suggest?co_id&branch_id&bale_series_id&count&purpose=inspect|despatch[&as_on][&finishing_quality_id]`
  → `{"data": {"rows": [BaleStockRow…], "available": n, "purpose": "...", "bale_series_id": id}}`.
  Eligible set as on `as_on` (default today): `inspect` → status IN_STOCK of that series, **sacking
  only** (400 `INSPECT_SACKING_ONLY_MSG` when the series' quality_type is 1); `despatch` → status
  IN_STOCK or INSPECTED of that series. Order: `despatch` = INSPECTED first, then press date, then
  bale no (oldest inspected stock goes first); `inspect` = press date, bale no. Returns the first
  `count` rows (1 ≤ count ≤ BALE_STOCK_MAX_IDS) and `available` = the eligible total, so the UI can
  say "only 12 available". Optional `finishing_quality_id` narrows the series to one quality.
- `GET /baleStock/bales` gains `invoice_id` (bales whose active despatch row points at that
  invoice, live or void — the invoice page's read-only list; combine with `status=ALL`).

Frontend — shared picker `src/components/juteProduction/baleStock/BaleSuggestPicker.tsx`
(default export; contract fixed because two pages import it):
```ts
export interface BaleSuggestPickerProps {
  coId: number; branchId: number; asOn: string;
  purpose: "inspect" | "despatch";
  /** controlled selection */ selectedIds: number[];
  onSelectionChange: (ids: number[], rows: BaleStockRow[]) => void;
  /** optional: restrict to one quality */ finishingQualityId?: number | null;
  disabled?: boolean;
  /** compact grid (dialogs) */ compact?: boolean;
}
```
Renders: Series (SearchableOptionSelect over `/setup.series`, sacking only for `inspect`) ·
Number of bales · **Suggest** → `fetchBaleSuggestions` → the suggested ids become the selection;
below it the eligible rows of that series (`fetchBaleStockRows` with `status` NOT_INSPECTED for
inspect / STOCK for despatch, `baleSeriesId`) in a `BaleStockGrid` with checkboxes so the user
adjusts by hand; caption "n selected of m available · x.xxx MT"; "only m available" warning when
count > available. Keeps the existing from–to range selector as a second way to pick
(`BaleRangeSelector` stays).

Where it is used:
1. `BaleInspectQuickDialog`, `BaleDespatchQuickDialog` and the Bale Stock page (above the grid, as
   the first way to pick) — replacing nothing, adding the Series + Count suggestion next to the
   range selector.
2. **Sales invoice create page** (`sales/salesInvoice/createSalesInvoice`): for invoice types
   Regular (1), Hessian (2), Govt Sacking (3), when `fetchBaleStockSetup().bale_source ===
   "BALE_PROD"` for the invoice's branch, a card **"Bales despatched"** below the line items holds
   the picker (purpose `despatch`, asOn = invoice date capped at today). Create mode: the
   selection lives in page state; after `createInvoice` succeeds the submission hook calls
   `linkBalesToInvoice({ co_id, branch_id, bale_entry_ids, invoice_id, despatch_date: invoice
   date, despatch_reason: null, despatch_ref: null, remarks: null })`; a failure there shows a
   toast "Invoice saved, but the bales could not be linked — link them from Bale Stock &
   Inspection" and never blocks the invoice. Edit / view mode: the card lists the bales already
   linked to the invoice (`fetchBaleStockRows({ invoiceId, status: "ALL" })`) read-only with
   their status, and a button "Change on Bale Stock & Inspection" (link to the page). No change to
   the backend invoice endpoints.

## 8. Open points for the owner

| # | Question | Default taken |
|---|---|---|
| O1 | Should the **sales invoice** page itself pick bales (auto-link while invoicing) or is the link-from-stock page enough? | **Owner 2026-10-12: manual pick on the invoice with a suggested default (state + no. of bales) — §7e.** |
| O2 | Inspection fields: is a certificate / inspection-note number and an agency name enough, or is there a per-visit lot (date + agency + consignee) that should group bales? | **Owner 2026-10-12: flat marking stays; bales are picked by state + no. of bales with a suggested default — §7e.** |
| O3 | Can hessian bales ever need the inspection mark? | **Owner 2026-10-12: no** — sacking only (D3), enforced with a 400. |
| O4 | sls: run the two migrations on sls now so branch 29 sees its 2,700 bales in Finishing Stock? | **Done 2026-10-12 on the owner's instruction** (tables created, menu 1080, 6 role grants mirrored from Finishing Stock). |

## 9. Build notes (2026-10-12 — built, uncommitted on `claudehost`, awaiting owner preview)

Resolutions taken during the build where the text above was under-specified:

1. **Write gates read the bale's CURRENT state** (`_fetch_bales_for_ids(..., as_on=today)`), and the
   D5 date rule (`tran_date ≤ inspection_date / despatch_date`) is applied in Python
   (`INSPECT_BEFORE_PRESS_MSG`, `DESPATCH_BEFORE_PRESS_MSG`). Evaluating "as on the marking date"
   would let a backdated marking create a second active row for a bale that already carries a
   later one, and the LEFT JOINs would then fan the grid out. Consequence: a despatched bale cannot
   receive a backdated inspection — unlink it first.
2. **Re-linking a bale whose link is void** (invoice cancelled): `/despatch` soft-deletes the void
   link in the same transaction before inserting, so a bale never has two active links.
3. Unknown invoice → **404** `INVOICE_NOT_FOUND_MSG` (also when the header is inactive); branch /
   type / cancelled gates stay 400.
4. `get_bale_stock_source_query` is branch-tolerant so `/setup` answers without a branch.
5. Duplicate ids in a body are de-duplicated (first-seen order). `/despatch` → 400
   `DESPATCH_DATE_REQUIRED_MSG` when neither the body nor the invoice carries a date.
6. The REPACK join is as written in §3; two active REPACK rows naming the same original with no
   intervening bale of that number (a genuine double entry) would still fan the consumed bale out.
7. Filter labels live in `bale_stock.BALE_STOCK_FILTER_LABELS`; `_bale_source` / `_summary_lines` are
   defined once in `bale_stock.py` and imported by `finishing_stock.py`.
8. Frontend: `BaleInspectQuickDialog` defaults the inspection date to the Finishing Stock day it was
   opened from (capped at today), so the day view shows the marking after its refresh;
   `LinkInvoiceDialog.onDone` also returns `invoiceRef` for the snackbar; `todayISO` is local to
   `baleStockCalc.ts` (nothing under `src/components` imports from `src/app`); the summary table
   recomputes totals client-side from `lines`.
9. Finishing Stock footer `totalMt` is the sum of the three rounded chips (so they always add up);
   in MANUAL mode this can differ from the old single rounding by ≤ 0.001 MT.
11. §7a / §7b built the same day (series breakup; despatch without invoice + link later). §7b
    extras: `ALREADY_DESPATCHED_NO_INVOICE_MSG` (separate wording), `RELINK_STALE_MSG` when the relink
    UPDATE hits 0 rows, reason codes accepted case-insensitively, `despatch_reason_label` falls back to
    the code. Frontend: the Finishing Stock despatch dialog merges in-stock rows with no-invoice
    despatches of the quality so they can be linked from there; snackbar wording in
    `despatchDoneMessage`. Migration `20261012b_…` applied on dev3 and sls 2026-10-12. Live smoke on
    dev3: PUNJAB 606/607 despatched on challan CH-77, 606 relinked to invoice 52 (date kept).
12. **Bug found by the owner on sls (2026-10-12):** "Mark n bales despatched" did nothing and showed
    no error. Cause: in Without-invoice mode the invoice picker is not rendered, react-hook-form then
    reports the unmounted `invoice_id` as `undefined`, and the schema's `z.string()` rejected the
    submission with the error parked on a field that was not on screen (the reason field had the
    same trap in the other direction). Fix: both mode-specific fields are `z.string().optional()`
    and read as blank when absent; and every refused submission now shows "Cannot save: <field> —
    <reason>" in the dialog (`describeLinkErrors`), so a validation refusal can never be silent again.
13. **Owner ruling (2026-10-12): no pressed-bale entry on Finishing Stock in derived mode.** The
    bucket toggle is gone there (loose stock only, a caption says why), the grid offers delete but no
    edit on any remaining manual BALE row, and the backend refuses EVERY manual BALE line (own or other
    owner) with `OWN_BALE_DERIVED_MSG` = "Pressed bales are counted from Bale Production for this
    date — this page takes loose finishing stock only". D7's "other owners counted by hand" is
    withdrawn for derived branches; MANUAL branches are unchanged.
14. §7d built 2026-10-12: `BACKDATE_DAYS_MAX = 60` in constants.py; floors in `bale_stock._not_future`
    (inspect / despatch), `finishing_stock._validate_tran_date`, `bale_entry._prepare_entry`; the
    `as_on` view date deliberately has no floor. Frontend `src/utils/backdate.ts` (todayISO,
    minBackdateISO, laterOf, backdateMessage) feeds inspectSchema / linkSchema / stockSchema and the
    date inputs' `min` (Finishing Stock form + day picker, Bale Production date, both dialogs).
    Backend test fixtures now use relative dates (today − 10) so they stay inside the window.
15. §7c Bale Register built 2026-10-12: `bale_register.py` + `bale_register_query.py` on
    `/api/juteProductionReports/bale_register` (42 tests), page `juteProduction/report/baleRegister`
    (22 tests), menu seeded dev3 1052 / sls 1082. dev3 check for 2026-10-06..12 by series: pressed 41,
    repacked out 2, despatched 3 invoice + 2 challan, closing 34 (10.857 MT); browser pass clean in both
    groupings.
16. §7e built 2026-10-12 after the first push: `GET /baleStock/suggest` (+ `invoice_id` filter on
    `/bales`; 31 new tests), `BaleSuggestPicker` on the Bale Stock page ("Pick by state and count",
    Inspect / Despatch toggle) and in both Finishing Stock dialogs, and the "Bales despatched" card
    on the sales invoice create page (types 1 / 2 / 3, BALE_PROD branches; create mode links the
    picked bales right after `createInvoice`; edit / view lists the linked bales read-only). dev3
    browser pass: page 3 of 15 PUNJAB (inspected first), dialog 2 of 10 BIHAR, invoice card
    2 picked; zero errors.
10. Post-build polish after the browser pass: the Finishing Stock Report wraps quality names (the
    ~120-character item names were pushing every numeric column off the card); the Finishing Stock
    form opens on the Loose Stock bucket in BALE_PROD mode; the Bale Stock summary leaves per-bale
    kg blank on the hessian + sacking grand total; `LinkInvoiceDialog` lists invoices from the last
    **365** days (the backend default stays 120 and is passed explicitly) — dev3's newest cloth
    invoice is from May 2026, so a 120-day window showed nothing.

Verification on 2026-10-12: backend 230 tests in the five touched files (63 new), full suite
6,425 passed with 64 pre-existing failures in untouched sales / HRMS files; frontend `tsc` clean,
1,945 unit tests passed (32 + 65 new/updated); dev3 migrations applied (menu 1051); dev3 seeded
with 3 series / 39 PROD + 2 REPACK bales through `/api/baleProd` (server script
`.claude/tools/seed_dev3_bales.py`), live smoke (`.claude/tools/smoke_bale_stock.py`): 39 in
stock, 2 REPACKED, hessian + duplicate inspection refused, 3 bales inspected, 2 linked to invoice
52; headless browser pass of the three pages with zero console / network errors.
