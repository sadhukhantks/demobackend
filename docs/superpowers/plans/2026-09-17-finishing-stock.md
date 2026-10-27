# Finishing Stock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daily physical stock entry (bales + loose finishing WIP) and the matching one-date report under Jute Production, on dev3.

**Architecture:** One flat tenant table `jute_prod_finishing_stock` mirroring `jute_prod_winding_stock`; a `/api/finishingStock` router (setup / rows_by_date / row CRUD / copy_previous_day / report); FE entry page copied from `windingStock/`, report page assembled from a pure `build_finishing_stock_report()`; despatch derived from sales invoices.

**Tech Stack:** FastAPI + SQLAlchemy `text()` + pymysql (BE, pytest mocked-session tests); Next.js 15 + MUI + `@mui/x-data-grid` + zod (FE, vitest).

**Spec:** `docs/superpowers/specs/2026-09-17-finishing-stock-design.md` — the API contract lives there; every task reads it.

## Global Constraints

- Handlers are plain `def`; `Depends(get_tenant_db)` + `Depends(get_current_user_with_refresh)`; responses are literal `{"data": ...}`.
- Query builders are arg-less functions returning `text()` with named binds; `None` for NULL; every bind typed (`int()`, `float()`).
- No `created_*` columns (audit via triggers); soft delete via `active=0`.
- FE: `fetchWithCookie` only, no `any`, `mounted` hydration gate, MUI `TextField type="date"`, `SearchableOptionSelect` for pickers.
- Tests: BE in `src/test/test_juteProduction_finishing_stock*.py` (house style of `test_winding_stock_entry.py`); FE vitest co-located `*.test.ts` for pure helpers.
- Do NOT commit — the lead stages and commits; report files touched.
- Target DB for migrations: **dev3 only** (creds `env/database.env`, run with `.venv/Scripts/python.exe` + pymysql; the `run-migration` skill or `tools/run_sql_migration.py`).

---

### Task 1: Backend — migrations, model, constants (run on dev3)

**Files:**
- Create: `dbqueries/migrations/create_jute_prod_finishing_stock.sql`
- Create: `dbqueries/migrations/seed_finishing_stock_misc_items.sql`
- Create: `dbqueries/migrations/seed_finishing_stock_menu.sql`
- Create: `src/juteProduction/finishing_stock_models.py`
- Modify: `src/juteProduction/constants.py` (append after the `WINDING_STOCK_*` block)

**Interfaces:**
- Produces: table `jute_prod_finishing_stock` (spec §2 DDL verbatim); constants `FINISHING_STOCK_BUCKETS`, `FINISHING_STOCK_STAGES`, `FINISHING_STOCK_STAGE_CODES`, `FINISHING_STOCK_QTY_UOM_BALE`, `FINISHING_STOCK_LOOSE_QTY_UOMS`, `FINISHING_STOCK_QTY_MAX`, `FINISHING_STOCK_WT_MAX`, `FINISHING_STOCK_WORK_BY`; ORM class `JuteProdFinishingStock`.

- [ ] Write the three SQL files exactly per spec §2 (DDL + `-- ROLLBACK:` comment; seeds guarded with `NOT EXISTS`; variable block at top of the items seed with dev3 values and sls values in a comment).
- [ ] Run all three against **dev3** with pymysql (statement-split on `;` outside comments — `tools/run_sql_migration.py` is comment-safe). Verify: `SHOW CREATE TABLE jute_prod_finishing_stock`; `SELECT COUNT(*) FROM jute_prod_finishing_quality WHERE co_id=1 AND active=1` grows by 7; `SELECT menu_id, menu_path FROM menu_mst WHERE menu_path LIKE '%finishingStock%'` returns 2 rows.
- [ ] Append constants to `constants.py` (spec §2 block verbatim).
- [ ] Write `finishing_stock_models.py` in the `winding_stock_models.py` style (`Mapped`, `mapped_column`, `Index` tuple, shared `Base`).
- [ ] `python -c "import src.juteProduction.finishing_stock_models"` imports clean.

### Task 2: Backend — queries + router (setup, rows_by_date, CRUD, copy)

**Files:**
- Create: `src/juteProduction/finishing_stock_query.py`
- Create: `src/juteProduction/finishing_stock.py`
- Modify: `src/main.py` (import + `app.include_router(finishing_stock_router, prefix="/api/finishingStock", tags=["jute-finishing-stock"])` next to `windingStock`)
- Test: `src/test/test_juteProduction_finishing_stock.py`

**Interfaces:**
- Consumes: Task 1 constants + table.
- Produces: endpoints per spec §3 (`GET /setup`, `GET /rows_by_date`, `POST /row_create`, `PUT /row_edit/{id}?co_id=`, `DELETE /row_delete/{id}?co_id=`, `POST /copy_previous_day`); query builders `get_finishing_stock_qualities_query()`, `get_finishing_stock_companies_query()`, `get_finishing_stock_by_date_query()`, `get_finishing_stock_prev_entry_date_query()`, `insert_finishing_stock_row_query()`, `get_finishing_stock_active_by_id_query()`, `update_finishing_stock_row_query()`, `soft_delete_finishing_stock_row_query()`, `copy_finishing_stock_previous_day_query()`, `verify_finishing_stock_owner_co_query()`; helper `total_kg_of(qty, wt_per_unit_kg, wt_kg) -> float` and `row_to_dict(row, stage_labels, own_co_name) -> dict` (module-level, importable by Task 3).

- [ ] Write the failing tests first (mocked session, `dependency_overrides`, `execute.side_effect` list, `_assert_binds_complete`). Minimum cases:

```python
def test_create_bale_defaults_per_bale_weight_from_quality():
    # quality row std_bale_weight=437.0, payload omits wt_per_unit_kg -> insert bind == 437.0
def test_create_bale_explicit_weight_wins():           # 437.384 stored
def test_create_loose_requires_wt_kg():                # 400
def test_create_bale_rejects_stage():                  # 400
def test_create_loose_rejects_owner():                 # 400
def test_create_rejects_future_date():                 # 400
def test_create_rejects_bad_bucket_stage_uom():        # 400 x3 (parametrize)
def test_create_bale_without_std_weight_400():         # quality std NULL + no wt -> 400
def test_create_owner_equal_to_co_normalised_to_null():
def test_create_unknown_owner_404_or_400():            # verify query returns None -> 400
def test_edit_wrong_co_404():
def test_delete_soft():                                # UPDATE ... active=0, {"deleted": True}
def test_copy_refuses_non_empty_day():                 # 400
def test_copy_no_previous_404():
def test_rows_by_date_shape_and_total_kg():            # total_kg = wt_kg or qty*wt_per_unit
```
- [ ] Run: `pytest src/test/test_juteProduction_finishing_stock.py -v` → all FAIL (module missing).
- [ ] Implement `finishing_stock_query.py` (SQL per spec; `rows_by_date` joins `jute_prod_finishing_quality q`, `item_mst im`, `co_mst oc ON oc.co_id = s.owner_co_id`; order bucket DESC? no — `CASE bucket WHEN 'BALE' THEN 0 ELSE 1 END, q.quality_type, im.item_name, s.stage, s.finishing_stock_id`).
- [ ] Implement `finishing_stock.py` (Pydantic bodies `FinishingStockCreateBody`, `FinishingStockEditBody`, `FinishingStockDayRef`; validators as module constants; `_validate_line(body, quality_row, owner_exists)` pure function used by create and edit).
- [ ] Register in `src/main.py`.
- [ ] Run tests → PASS. Also `pytest src/test/test_winding_stock_entry.py -q` still green (shared patterns untouched).

### Task 3: Backend — report endpoint

**Files:**
- Modify: `src/juteProduction/finishing_stock_query.py` (add `get_finishing_stock_report_rows_query()`, `get_finishing_stock_despatch_query()`, `get_finishing_stock_unit_names_query()` — co_name + branch_name)
- Modify: `src/juteProduction/finishing_stock.py` (add `GET /report` + pure `build_finishing_stock_report(rows, qualities, despatch_rows, own_co_id, own_co_name)`)
- Test: `src/test/test_juteProduction_finishing_stock_report.py`

**Interfaces:**
- Consumes: Task 2 `row_to_dict`, `total_kg_of`; Task 1 constants.
- Produces: `GET /report?co_id&branch_id&tran_date` → spec §3 JSON; `build_finishing_stock_report(...) -> dict`.

- [ ] Failing tests for the pure builder:

```python
def test_own_block_zero_fills_every_quality():     # 3 qualities, 1 row -> 3 lines, zeros
def test_other_owner_block_lists_only_rows():
def test_blocks_order_own_first_then_owner_name():
def test_per_wt_is_weighted_average_over_lots():   # 70@437.384 + 30@400 -> per_wt = total/100
def test_loose_sums_lots_and_stages_per_quality():
def test_loose_lines_carry_stage_labels():
def test_totals_stock_mt_equals_bales_plus_loose():
def test_despatch_passthrough_bales_null_when_none():
def test_report_endpoint_binds_complete():          # every bind in every statement present
```
- [ ] Run → FAIL. Implement builder + endpoint (despatch SQL per spec: join `item_grp_mst.item_type_id = 5`, `LEFT JOIN sales_invoice_hessian_dtl`, `LEFT JOIN uom_mst`, status filter `NOT IN (4, 6)` NULL-safe). MT rounded to 3 dp with `round()`.
- [ ] Run both test files → PASS. `pytest src/test -k finishing_stock -q`.

### Task 4: Frontend — entry page

**Files:**
- Create: `src/app/dashboardportal/juteProduction/finishingStock/page.tsx`
- Create: `src/app/dashboardportal/juteProduction/finishingStock/_components/FinishingStockForm.tsx`
- Create: `src/app/dashboardportal/juteProduction/finishingStock/_components/FinishingStockGrid.tsx`
- Create: `src/app/dashboardportal/juteProduction/finishingStock/_components/StockTotalsFooter.tsx`
- Create: `src/app/dashboardportal/juteProduction/finishingStock/utils/stockCalc.ts` + `stockCalc.test.ts`
- Create: `src/app/dashboardportal/juteProduction/finishingStock/utils/stockSchema.ts`
- Already present (lead): `src/utils/finishingStockService.ts`, `src/utils/api.ts` constants.

**Interfaces:**
- Consumes: `finishingStockService.ts` types + fetchers (fixed contract — do not edit).
- Produces: route `/dashboardportal/juteProduction/finishingStock`; `stockCalc.ts` exports `lineTotalKg(row)`, `kgToMt(kg)`, `groupRows(rows)` → `{ bale: { own: Row[], others: Record<ownerName, Row[]> }, loose: { hessian: Row[], sacking: Row[] } }`, `sectionTotals(rows)` → `{ bales, kg, mt }`, `footerTotals(rows)` → `{ baleOwnMt, baleOthersMt, looseMt, totalMt }`, `todayISO()`.

- [ ] Failing vitest for `stockCalc.ts` (kgToMt(16002)=16.002; lineTotalKg bale = qty×per; loose = wt_kg; groupRows splits own/others by `owner_co_id`; footerTotals sums).
- [ ] Implement `stockCalc.ts` → tests PASS (`npx vitest run src/app/dashboardportal/juteProduction/finishingStock`).
- [ ] `stockSchema.ts`: zod schema (strings), `buildCreatePayload(values, ctx)` / `buildEditPayload(values)` returning `FinishingStockCreatePayload` / `FinishingStockEditPayload`; BALE → `stage:null, work_by:0, wt_kg:null, qty_uom:"BALE"`; LOOSE → `owner_co_id:null, wt_per_unit_kg:null`.
- [ ] `page.tsx`: copy `windingStock/page.tsx` container flow (mounted gate, coId, branch resolution, date ≤ today, setup/day loads, copy-previous-day with `prev_entry_date` hint, edit state, Snackbar/Alert). Remove lock handling.
- [ ] `FinishingStockForm.tsx`: Bucket toggle (MUI `ToggleButtonGroup`), Quality `SearchableOptionSelect` (label `"Hessian · " | "Sacking · "` + item_name), BALE fields (Owner company default own, Bales, Per-bale kg prefilled from `std_bale_weight` on quality change), LOOSE fields (Stage optional, Work by Dept/Contract, Count, UoM, Weight kg), Remarks, live "Total: X kg / Y.YYY MT".
- [ ] `FinishingStockGrid.tsx`: `DataGrid` with rows + synthetic subtotal rows per (bucket, section) from `groupRows`; edit/delete icon actions (`window.confirm` on delete).
- [ ] `StockTotalsFooter.tsx`: four chips from `footerTotals`.
- [ ] `npx tsc --noEmit` clean; vitest green.

### Task 5: Frontend — report page

**Files:**
- Create: `src/app/dashboardportal/juteProduction/report/finishingStockReport/page.tsx`
- Create: `src/app/dashboardportal/juteProduction/report/finishingStockReport/utils/reportShape.ts` + `reportShape.test.ts`

**Interfaces:**
- Consumes: `fetchFinishingStockReport`, `FinishingStockReport` from `finishingStockService.ts`; `useReportBranches` (`@/components/reports/useReportBranches`), `todayIso` (`@/components/reports/reportDates`), `exportRowsToExcel` (`@/components/reports/exportExcel`).
- Produces: route `/dashboardportal/juteProduction/report/finishingStockReport`; `flattenReport(report) -> FlatRow[]` where `FlatRow = { block: string; section: string; item: string; bales: number | null; per_wt_kg: number | null; wt_mt: number }`.

- [ ] Failing vitest for `flattenReport` (one row per bale line per owner block, per loose line, totals rows appended per block, despatch rows with section "DESPATCH").
- [ ] Implement `reportShape.ts` → PASS.
- [ ] `page.tsx`: `<Suspense>` wrapper; filters (branch + single date, Submit); layout per spec §4 (left: owner bale cards + despatch table; right: loose stock card + collapsible loose-by-stage detail); KPI footer line; Print button (`window.print()` + `GlobalStyles` `@media print` hiding the sidebar/filters like `report/dailyProductionReport/page.tsx`) and Excel button (`exportRowsToExcel(flattenReport(report), "finishing-stock-<date>")`). Numbers: MT 3 dp, per-bale kg 3 dp, bales integer.
- [ ] `npx tsc --noEmit` clean; vitest green.

### Task 6: Verification (lead)

- [ ] `pytest src/test -k finishing_stock -v` green; `pytest src/test/test_winding_stock_entry.py -q` green.
- [ ] `npx tsc --noEmit` and `npm run test` in vowerp3ui green.
- [ ] Reviewer pass (`reviewer` agent) on BE diff; fix findings.
- [ ] Browser QA on dev3 (Empire / Factory) via `portal-ui-flow-tester`: bale line with owner default + other owner, loose lines at two stages, copy previous day, edit, delete, report renders both blocks + despatch for a date with a cloth invoice (2026-05-07 has one on dev3), negatives (future date, loose without weight).
- [ ] Memory note + final report; list uncommitted files in both repos.
