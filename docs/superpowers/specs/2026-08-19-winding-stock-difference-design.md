# Winding Stock Daily Entry + Winding & Weaving Difference Report

**Date:** 2026-08-19
**Module:** Jute Production (`src/juteProduction/`, `../vowerp3ui/src/app/dashboardportal/juteProduction/`)
**Status:** design approved in chat; awaiting spec review

---

## 1. Purpose

The mill takes a daily physical count of all yarn on the floor (the handwritten sheet) and types it into a spreadsheet that produces the **Winding & Weaving Difference** report. The report reconciles counted stock against book stock:

```
should_be_closing = opening + winding_production + purchase_yarn - weaving_production - sale_yarn
difference        = actual_counted_closing - should_be_closing
difference_pct    = difference / winding_production * 100
```

This spec replaces both halves with ERP screens: a daily winding-stock entry page, and a derived difference report under the jute production reports hub.

Verified against the supplied printed sheet (27-01-2026):

| Figure | Sheet | Check |
|---|---|---|
| Mill use total (incl. purchase yarn 6.038) | 28.744 | |
| Sale yarn old stock | 24.856 | |
| Ball stock | 2.116 | |
| Waste | 0.134 | |
| **Stock grand total** | **55.850** | 28.744 + 24.856 + 2.116 + 0.134 = 55.850 |
| Should-be closing | 56.290 | |
| Difference | -0.440 | 55.850 - 56.290 = -0.440 |
| Difference % | -3.47% | -0.440 / 12.697 = -3.465% |

These become the golden test case.

---

## 2. Data model

### 2.1 `jute_prod_winding_stock` (new)

One row per counted line per date. **The first counted-snapshot table in the ERP** — every other stock figure in the system is derived from movements.

```
winding_stock_id   INT PK AUTO_INCREMENT
co_id              INT           NOT NULL
branch_id          INT           NOT NULL
tran_date          DATE          NOT NULL      -- as-on date; NO spell (a count, not a shift)
bucket             VARCHAR(20)   NOT NULL      -- MILL_USE | PURCHASE | SALE_YARN | BALL | WASTE
                                               -- PURCHASE here is COUNTED purchased yarn sitting in
                                               -- stock (the sheet's "OUT SIDE PURCHASE MACKROL FOR S4",
                                               -- 6.038 MT). It is NOT the purchase-yarn INFLOW figure in
                                               -- the production block, which is a period movement.
item_id            INT           NULL          -- FK item_mst; yarn item, NULL where no master row exists
package_label      VARCHAR(60)   NOT NULL      -- 'MACKROLL SPOOL', 'COP BUNDLE (SKG)WFT', 'THREAD WASTE'
wastage_area_id    INT           NULL          -- FK wastage_area_mst (the existing area master)
no_of_samples      DECIMAL(12,3) NOT NULL
wt_per_sample      DECIMAL(10,3) NOT NULL      -- kg per piece
remarks            VARCHAR(255)  NULL
active             INT           NOT NULL DEFAULT 1
updated_by         INT           NULL
updated_date_time  DATETIME      DEFAULT CURRENT_TIMESTAMP
INDEX idx_winding_stock_co_branch_date (co_id, branch_id, tran_date)
INDEX idx_winding_stock_item (item_id)
```

Design decisions:

- **No `stock_mt` column.** MT is `no_of_samples * wt_per_sample / 1000`, computed at read time. A corrected weight then fixes history rather than leaving a stale derived value behind. Consistent with the module rule "views may format, never accumulate".
- **No spell.** The sheet is an as-on-date count, not a shift entry.
- **`item_id` nullable.** Lines like `BEAM ON FLOOR-HESS`, `PURCHASE PW FOR S4` and the four waste lines (`THREAD WASTE`, `DIRTY BOBBIN`, `DAMAGE SPOOL`, `ENTANGLE COPS`) have no `jute_yarn_mst` row. `package_label` carries them.
- **No UNIQUE constraint.** MySQL treats NULLs as distinct, so a unique key over nullable `item_id` / `wastage_area_id` would not catch the duplicate lines that actually matter. The duplicate check runs in the create/edit handler instead.
- **No weight-per-sample master.** The default is the most recent saved `wt_per_sample` for the same `(co_id, branch_id, item_id, package_label)` — one lookup against this same table. Overtyping a row makes the new value the next default.

### 2.2 `jute_prod_winding_stock_lock` (new)

Clone of `jute_prod_weaving_process_lock` minus `spell_id`.

```
lock_id            INT PK AUTO_INCREMENT
co_id              INT      NOT NULL
branch_id          INT      NOT NULL
tran_date          DATE     NOT NULL
is_locked          TINYINT  NOT NULL DEFAULT 0
locked_by          INT      NULL
locked_date_time   DATETIME NULL
UNIQUE uk_winding_stock_lock (co_id, branch_id, tran_date)
```

Locking is an **explicit action**, not a side effect of running the report — a GET must not mutate. Create / edit / delete are refused while locked.

### 2.3 Reused, unchanged

| Table | Use |
|---|---|
| `wastage_area_mst` | the PLACE column. Existing area master (dept-owned, branch-scoped). Legacy name; a second area table is not justified. |
| `jute_yarn_mst` | yarn items — `jute_yarn_count` is the lbs count, quality name via `item_grp_mst.item_grp_name`. **`item_id` is non-unique here — dedupe or the join fans out.** |
| `jute_prod_winding_doff` / `_jugar` | winding production (kg) |
| `jute_prod_weaving_daily` | weaving day-slice (cuts + jugar) |
| `jute_prod_weaving_quality` | cloth constants + `item_id` for the hessian/sacking split |
| `jute_prod_finishing_quality` | `quality_type` 1 = Hessian, 2 = Sacking |
| `jute_issue` | jute issue (informational) |
| `sales_invoice` + `sales_invoice_dtl` | sale yarn |

**No schema change to any existing table.**

---

## 3. Backend

New files, module-local to match `winding_models.py` / `winding_query.py`:

- `src/juteProduction/winding_stock_models.py`
- `src/juteProduction/winding_stock_query.py`
- `src/juteProduction/winding_stock.py` — router, prefix `/api/windingStock`, registered in `src/main.py`

All handlers are `def` (not `async def`), take `Depends(get_tenant_db)` and `Depends(get_current_user_with_refresh)`, read bodies via `parse_json_body`, and wrap every response as `{"data": ...}`.

| Endpoint | Behaviour |
|---|---|
| `GET /setup` | bucket list; yarn items from `jute_yarn_mst` (deduped) with count-lbs + quality group; areas from `wastage_area_mst` (co + branch scoped, active) |
| `GET /rows_by_date` | `?co_id&branch_id&tran_date` -> rows grouped by bucket, per-bucket subtotals, grand total MT, lock state, previous entry date |
| `GET /wt_default` | `?co_id&branch_id&item_id&package_label` -> last saved `wt_per_sample` |
| `POST /row_create` | duplicate guard on (date, bucket, item, package, area); lock guard |
| `PUT /row_edit/{id}` | lock guard; `tran_date` immutable |
| `DELETE /row_delete/{id}` | soft delete (`active = 0`); lock guard |
| `POST /copy_previous_day` | clones the previous entry date's lines into `tran_date`; refuses if rows already exist |
| `GET /lock_state`, `POST /lock`, `POST /unlock` | per (co, branch, date) |

`copy_previous_day` is deliberate: entry lines are added manually per the agreed design, but the sheet repeats around 30 near-identical rows daily and retyping them is the single biggest source of operator error. It is one endpoint and forces no template on anyone.

### Report endpoint

Added to the existing `src/juteProduction/winding_reports.py`, already mounted on `/api/juteProductionReports`:

```
GET /winding_weaving_difference?co_id&branch_id&tran_date
```

Response shape:

```jsonc
{
  "data": {
    "stock":       { "<BUCKET>": { "rows": [], "subtotal_mt": 0.0 }, "grand_total_mt": 0.0 },
    "production":  { "day": {}, "mtd": {} },          // members listed in section 4
    "difference":  { "day_mt": 0.0, "day_pct": 0.0, "mtd_mt": 0.0, "mtd_pct": 0.0 },
    "cloth_split": { "HESSIAN": 0.0, "SACKING": 0.0, "OTHER": 0.0 },
    "chain":       { "ok": true, "prev_entry_date": "2026-01-26" }
  }
}
```

Each stock row carries: count (lbs), quality, package label, `no_of_samples`, `wt_per_sample`, `prvs_stock_mt` (same row key on the previous entry date), `stock_mt`, and place.

---

## 4. Report computation

Every production figure is **derived at read time from existing builders**. No new production SQL, nothing persisted.

| Figure | Source | Unit handling |
|---|---|---|
| Winding production | `get_winding_reconciliation_query` (day) / `get_dpr_winding_mtd_query` (MTD) — `SUM(production_qty) - opening_jugar + closing_jugar` | kg -> MT (`/1000`) |
| Weaving production | day slice `jute_prod_weaving_daily` + `jute_prod_weaving_quality` constants: `total_jugar = cuts*njpc + close_jugar - open_jugar - less_production` -> `yds = total_jugar*finished_length/njpc` -> `kg = yds*ozs_yds*28.35/1000` -> MT | derived -> MT |
| Jute issue | `_issue_cte()` in `src/juteProcurement/reportQueries.py` (handles the waste-issue leg and the `actual_item_id` attribution rule) | **quintal** -> MT (`/10`) |
| Purchase yarn | procurement inward / store receipt lines whose item is yarn (`item_type_id = 4`) | kg -> MT |
| Sale yarn | `sales_invoice` + `sales_invoice_dtl`, approved only (`status_id = 3`), line item resolving to a yarn item group (`item_grp_mst.item_type_id = 4`) OR the invoice carrying a yarn `invoice_type` — whichever the tenant uses; both are matched so neither classification misses | kg -> MT |
| Opening | previous entry date's stock grand total | MT |

Three units are in play (quintal, kg, MT) and no table declares its unit — conversions are centralised in one place in `winding_stock_query.py` and asserted in tests.

### Reconciliation

Computed twice, printed side by side:

- **TODAY** — day-over-day: opening = previous entry date's actual closing; production figures = that date's day slice.
- **TODATE** — month-to-date: opening = the month's first entry; production figures = MTD.

**JUTE ISSUE is printed, not subtracted.** It feeds spinning, not winding stock, and the sheet's own arithmetic closes without it (18.415 + 37.875 = 56.290).

**Chain breaks are reported, not hidden.** If the previous entry date is not the calendar previous day, `chain.ok = false` and the report names the date it actually reconciled against, rather than silently using a stale opening. This is the failure mode created by combining "opening = prior day's entry" with a lock.

### Cloth split (replaces the sheet's LOMM DETAIL block)

The block becomes **total weaving production split by cloth type** — no loom counts, no shift breakdown. Resolution:

```
jute_prod_weaving_daily.weaving_quality_id
  -> jute_prod_weaving_quality.item_id
  -> jute_prod_finishing_quality.quality_type   (active, co-scoped)
       1    -> HESSIAN
       2    -> SACKING        (S4 is a sacking variant, not a separate bucket)
       else -> OTHER
```

The three buckets always sum to total weaving production, so nothing can go missing.

**Known data state:** on sls all weaving qualities point at the `JCLOTH-MIG` "JUTE CLOTH (MIGRATED)" catch-all items (335522 co 2 — 513 qualities on one item; 335523 co 106), none of which has a finishing-quality row. So the split reads 100% OTHER on sls until the weaving qualities are pointed at real cloth items in the existing masters. That is a data task, not a code change, and it matches the agreed rule: unclassified qualities fall to OTHER. dev3 demonstrates the join working — weaving quality on co 9 points at item 270186 `Sacking Bag 37 x 22.5 B.Twill`, which has `quality_type = 2`.

---

## 5. Frontend

### Entry page — `juteProduction/windingStock/page.tsx`

Cloned from `wasteEntry` (same date-keyed daily-entry shape, same area master).

- Header: branch + date; lock banner; **Copy previous day** and **Lock** buttons.
- Grid grouped by bucket with per-bucket subtotals and a grand total footer.
- Add/edit dialog: bucket, yarn item (autocomplete — set `renderOption` key to `item_id`; dev3 and sls both carry duplicate display names), package label, area, no. of samples, kg/sample (prefilled from `/wt_default`, editable).
- MT shown per row, computed client-side, mirroring the server formula.

### Report page — `juteProduction/report/windingWeavingDifference/page.tsx`

Reproduces the printed layout: header block (date, stock-on date, TODAY / TODATE %), production details, cloth split, then the five stock blocks with PRVS STOCK and STOCK POSITION columns. Reads `branch_id` + `date` from the query string the hub pushes. Excel export via the hub's existing mechanism.

Route constants in `src/utils/api.ts`; service functions alongside the existing jute production report services.

---

## 6. Migrations and menus

| Script | Contents |
|---|---|
| `dbqueries/migrations/create_jute_prod_winding_stock.sql` | both tables + rollback SQL as a comment |
| `dbqueries/migrations/20260819_winding_stock_menus.sql` | entry menu under the winding parent; report menu under the `juteProduction/report` hub with `report = 1`; `role_menu_map` rows mirroring each role's hub access |

The menu script follows `20260814_daily_production_report_menu.sql` verbatim — it resolves the hub by `menu_path LIKE '%juteProduction/report'` and derives `menu_path` / `order_by` / `module_mst_id` from it. Tenant DBs only; `portal_menu_mst` untouched. Users must re-login (sidebar menus are cached in localStorage).

Applied to **dev3 first, then sls**, via the `run-migration` skill. Note the known runner defect: it splits on `;` and drops comment-led chunks, so no statement may lead with a comment block.

---

## 7. Testing

Location `src/test/`, naming `test_winding_stock_*.py`.

1. **Golden case from the printed sheet** — bucket subtotals 28.744 / 24.856 / 2.116 / 0.134 -> grand total 55.850; should-be 56.290; difference -0.440; percentage -3.47%.
2. MT arithmetic: `samples * wt / 1000`, rounding, and the quintal->MT and kg->MT conversions.
3. Lock guard blocks create, edit and delete; unlock restores them.
4. Duplicate-line guard.
5. `wt_default` returns the most recent value for the key, and nothing for an unseen key.
6. Chain-break detection sets `chain.ok = false` and names the real prior date.
7. Cloth split: classified -> HESSIAN / SACKING, unclassified -> OTHER, and the three always sum to total weaving production.
8. Parameter binding on every query function (`None` for SQL NULL, never `"null"`; names match the bind placeholders).

---

## 8. Risks and open items

| # | Item |
|---|---|
| R1 | **dev3 has zero winding rows.** The report renders empty there; real verification has to happen against sls. |
| R2 | `jute_prod_weaving_log` has **0 rows on sls** against 2.26M day-slice rows — never backfilled. Reading it would silently return empty on the production tenant, so the report computes from `jute_prod_weaving_daily` instead. |
| R3 | `jute_yarn_mst.item_id` is non-unique — every join must dedupe or results fan out. |
| R4 | Weaving tolerates NULL `branch_id` by design; winding requires it. Branch is pinned throughout the report. |
| R5 | Three units in play with no unit column anywhere. Conversions centralised and asserted. |
| R6 | Cloth split reads 100% OTHER on sls until weaving qualities point at real cloth items (section 4). Data task, flagged. |
| R7 | Purchase-yarn sourcing (procurement inward of `item_type_id = 4`) returns 0 on current data and is unverified against a real yarn purchase. |
| R8 | Sale yarn returns 0.000 on sls today — no yarn invoices exist (all 17,753 sls invoices are `invoice_type = 5` raw jute). Matches the sheet; starts working when yarn is actually sold. |
| R9 | Branch 29 switched from split A1/A2 spells to single 8-hour A/B spells (109/110) on 2026-08-19. Does not affect this report (no shift breakdown) but does affect the underlying day slices. |

## 9. Explicitly out of scope

- Loom-count reporting (the sheet's LOMM DETAIL A/B/C grid) — dropped by decision. The printed block is hand-maintained and internally inconsistent: its SKG row sums 0 + 0 + 0 to 249, and HESS 124 exceeds every loom count in the data (all-time max of distinct producing HESS looms is 108).
- Any per-quality winding-to-weaving reconciliation. The report reconciles a single total, so no yarn-to-cloth bridge is needed.
- Repairing the `JCLOTH-MIG` catch-all cloth items.
- Backfilling `jute_prod_weaving_log` on sls.
