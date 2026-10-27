# Finishing Stock — stage / UoM / unit-weight masters

**Status:** APPROVED design (user, 2026-09-20) · **Tenant:** dev3 first (sls migrations listed, not run)
**Repos:** vowerp3be (FastAPI) + vowerp3ui (Next.js) · **Module:** Jute Production
**Supersedes:** the hard-coded `FINISHING_STOCK_STAGES` / `FINISHING_STOCK_LOOSE_QTY_UOMS` tuples
introduced by `2026-09-17-finishing-stock-design.md` §2, and extends that spec's §6 unit-weight
prefill with an explicit master.

## 0. Decisions captured

| Decision | Choice |
|---|---|
| Stage list | Branch-scoped master `jute_prod_finishing_stage`, carrying `quality_type` applicability, an optional `process` link into `FINISHING_PROCESSES`, and a `default_uom_id`. |
| Loose/bale UoM list | Branch-scoped master `jute_prod_finishing_stock_uom`, each row pointing at a curated `uom_mst` row. `CUTS` seeded into `uom_mst` (absent in both tenants). |
| Fact-table storage | Ids become the truth (`finishing_stage_id`, `qty_uom_id`); the existing `stage` / `qty_uom` VARCHARs stay as snapshots so a master rename or soft-delete never rewrites history. |
| Unit weight | New branch-scoped master `jute_prod_finishing_unit_wt` (quality × uom × optional stage → kg). Resolution ladder master → derived; the §6 formulas are kept as the fallback rung. |
| `uom_item_map_mst` reuse | Mechanism and data reused as a **suggestion in the master tab only** — never a rung in the entry form's ladder. Reasons in §3.3. |
| Masters packaging | ONE page `juteProduction/masters/finishingStockSetup` with three tabs, one `menu_mst` row per tenant. |
| Seed guessing | `quality_type` and `default_uom_id` seeded NULL; `process` seeded only on exact token matches. The master is editable — guessing would bake in errors nobody would look at again. |

## 1. Problem

`jute_prod_finishing_stock` keys its LOOSE lines off two Python tuples in
`src/juteProduction/constants.py`:

```python
FINISHING_STOCK_STAGES = (("IN_LINE", "Cuts in line (from weaving)"), ...)  # 10 entries
FINISHING_STOCK_LOOSE_QTY_UOMS = ("CUTS", "BDL", "PCS")
```

Every mill runs a different finishing line, so the stage vocabulary is branch data, not code. The
UoM tuple is worse: it duplicates names that `uom_mst` already owns, and `CUTS` does not exist in
`uom_mst` in either tenant, so the stock module invented a unit the rest of the ERP cannot see.

Separately, the §6 unit-weight prefill resolves for almost nothing (§9.1), so the entry form asks
for a weight it could compute if the standards had anywhere to live.

## 2. Data model

The three new tables go in `dbqueries/migrations/create_jute_prod_finishing_stock_masters.sql`
(+ rollback comment). The §2.4 ALTER and its backfill live in a **separate** file
(`alter_finishing_stock_master_ids.sql`) because the backfill has to run after the seeds — see §7
for the order. ORM additions go in `src/juteProduction/finishing_stock_models.py` alongside the
existing `JuteProdFinishingStock`.

FK policy follows each table's own precedent: the three new **master** tables carry FKs (like
`create_finishing_tables.sql` and `create_jute_prod_bale_tables.sql`); the ALTER on
`jute_prod_finishing_stock` adds **indexes only**, because that table's DDL comment states it
deliberately carries none.

No DB UNIQUE keys on the code columns — duplicates are guarded app-side among `active = 1`, the
soft-delete pattern `jute_prod_finishing_quality` already uses.

### 2.1 `jute_prod_finishing_stage`

```sql
CREATE TABLE jute_prod_finishing_stage (
  finishing_stage_id INT NOT NULL AUTO_INCREMENT,
  co_id              INT NOT NULL,
  branch_id          INT NOT NULL,
  stage_code         VARCHAR(20) NOT NULL,   -- 'HEMMING' - stable token, snapshot onto the fact row
  stage_name         VARCHAR(60) NOT NULL,   -- 'Hemming' - the label the form and report show
  quality_type       TINYINT     NULL,       -- 1 hessian | 2 sacking | NULL both
  process            VARCHAR(20) NULL,       -- FINISHING_PROCESSES token; the join to finishing production
  default_uom_id     INT         NULL,       -- uom_mst; prefills the entry form's UoM
  sort_order         INT NOT NULL DEFAULT 0,
  active             TINYINT NOT NULL DEFAULT 1,
  updated_by         INT NULL,
  updated_date_time  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (finishing_stage_id),
  KEY idx_fstage_co_branch (co_id, branch_id, active),
  CONSTRAINT fk_fstage_uom FOREIGN KEY (default_uom_id) REFERENCES uom_mst (uom_id)
);
```

`process` is validated against `FINISHING_PROCESSES` at save (400 otherwise). It exists so a later
report can join counted stage stock to `jute_prod_finishing_daily` production on the same token;
nothing in this spec reads it yet.

`quality_type` NULL means the stage applies to both Hessian and Sacking — the same NULL-tolerant
convention `jute_prod_finishing_quality.branch_id` already uses.

### 2.2 `jute_prod_finishing_stock_uom`

```sql
CREATE TABLE jute_prod_finishing_stock_uom (
  fs_uom_id          INT NOT NULL AUTO_INCREMENT,
  co_id              INT NOT NULL,
  branch_id          INT NOT NULL,
  uom_id             INT NOT NULL,           -- uom_mst
  bucket             VARCHAR(10) NOT NULL,   -- 'BALE' | 'LOOSE'
  sort_order         INT NOT NULL DEFAULT 0,
  active             TINYINT NOT NULL DEFAULT 1,
  updated_by         INT NULL,
  updated_date_time  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (fs_uom_id),
  KEY idx_fsuom_co_branch (co_id, branch_id, bucket, active),
  CONSTRAINT fk_fsuom_uom FOREIGN KEY (uom_id) REFERENCES uom_mst (uom_id)
);
```

This is the curation layer over a dirty master: `uom_mst` carries ` MT`, `MT`, `MTS`, `KG`, `KGS`,
`Kgs.`, `PCS`, `PCS.`, ` PCS  `, `100 PCS`, `PER 100 PCS ` as separate active rows. The branch
picks the handful it actually counts in; the entry form never sees the rest.

### 2.3 `jute_prod_finishing_unit_wt`

```sql
CREATE TABLE jute_prod_finishing_unit_wt (
  unit_wt_id           INT NOT NULL AUTO_INCREMENT,
  co_id                INT NOT NULL,
  branch_id            INT NOT NULL,
  finishing_quality_id INT NOT NULL,
  uom_id               INT NOT NULL,
  finishing_stage_id   INT NULL,             -- NULL = applies to every stage
  wt_per_unit_kg       DECIMAL(10,3) NOT NULL,
  active               TINYINT NOT NULL DEFAULT 1,
  updated_by           INT NULL,
  updated_date_time    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (unit_wt_id),
  KEY idx_fuw_lookup (co_id, branch_id, finishing_quality_id, uom_id, active),
  CONSTRAINT fk_fuw_quality FOREIGN KEY (finishing_quality_id) REFERENCES jute_prod_finishing_quality (finishing_quality_id),
  CONSTRAINT fk_fuw_uom     FOREIGN KEY (uom_id)               REFERENCES uom_mst (uom_id),
  CONSTRAINT fk_fuw_stage   FOREIGN KEY (finishing_stage_id)   REFERENCES jute_prod_finishing_stage (finishing_stage_id)
);
```

**Why `wt_per_unit_kg` and not `uom_item_map_mst`'s `map_from_id / map_to_id / relation_value`
shape:** that shape is direction-ambiguous, and the ambiguity has already cost this codebase —
`src/sales/hessian_calculations.py:33` documents that it treats `relation_value` "as bales-per-MT
regardless of direction", and `get_hessian_mt_conversion()` has to find the row with a `uom_name`
REGEXP. Finishing stock only ever converts one way, into kg. A single signed-by-construction
column cannot be entered backwards.

### 2.4 `ALTER TABLE jute_prod_finishing_stock`

```sql
ALTER TABLE jute_prod_finishing_stock
  MODIFY COLUMN qty_uom VARCHAR(30) NOT NULL,        -- 'PER 100 PCS ' is 12 chars; VARCHAR(10) truncates
  ADD COLUMN finishing_stage_id INT NULL AFTER stage,
  ADD COLUMN qty_uom_id         INT NULL AFTER qty_uom,
  ADD KEY idx_finstock_stage (finishing_stage_id),
  ADD KEY idx_finstock_uom   (qty_uom_id);
```

`qty_uom_id` stores a **`uom_mst.uom_id`**, not an `fs_uom_id` — the branch UoM table is the
permission list, not the identity. Validation checks that the id appears in the branch's list with
the right bucket; the stored value stays comparable across branches.

Backfill in the same file, by code match against the seeded masters, scoped per (co, branch):

```sql
UPDATE jute_prod_finishing_stock fs
  JOIN jute_prod_finishing_stage st
    ON st.co_id = fs.co_id AND st.branch_id = fs.branch_id
   AND st.stage_code = fs.stage AND st.active = 1
   SET fs.finishing_stage_id = st.finishing_stage_id
 WHERE fs.finishing_stage_id IS NULL AND fs.stage IS NOT NULL;

UPDATE jute_prod_finishing_stock fs
  JOIN jute_prod_finishing_stock_uom fu
    ON fu.co_id = fs.co_id AND fu.branch_id = fs.branch_id AND fu.active = 1
  JOIN uom_mst um ON um.uom_id = fu.uom_id
   AND UPPER(TRIM(um.uom_name)) = UPPER(TRIM(fs.qty_uom))
   SET fs.qty_uom_id = fu.uom_id
 WHERE fs.qty_uom_id IS NULL;
```

Affects 13 live dev3 rows (7 BALE/`BALE`, 6 LOOSE/`CUTS`); sls has none yet. The migration must run
**after** the seed file (§4) — ordering is stated in §7.

### 2.5 `uom_mst` seed — `CUTS`

`dbqueries/migrations/seed_uom_cuts.sql`, with the same `@updated_by` variable block the other
seeds use. Neither tenant has a CUTS row, so the branch UoM master has nothing to point at until
this runs. Guarded:

```sql
INSERT INTO uom_mst (uom_name, active, updated_by, updated_date_time)
SELECT 'CUTS', 1, @updated_by, NOW()
 WHERE NOT EXISTS (SELECT 1 FROM uom_mst WHERE UPPER(TRIM(uom_name)) = 'CUTS');
```

## 3. Weight resolution

### 3.1 The ladder

One pure function in `src/juteProduction/finishing_stock.py`, no DB access, unit-tested on its own:

```
resolve_unit_weight(quality, uom_id, stage_id, master_rows, derived) ->
  1. master row matching (co, branch, quality, uom, stage_id)     -> source 'master'
  2. master row matching (co, branch, quality, uom, stage NULL)   -> source 'master'
  3. derived formula for that uom (cut / bag / bundle, spec §6)   -> source 'derived'
  4. None                                                          -> form leaves Weight kg manual
```

`GET /setup` returns a flat list of every combination that resolves:

```json
"unit_weights": [
  {"finishing_quality_id": 4, "uom_id": 143, "finishing_stage_id": null,
   "wt_per_unit_kg": 0.580, "source": "master"}
]
```

The form looks up exact `(quality, uom, stage)` first, then `(quality, uom, null)`, and shows the
source in helper text — `std 0.580 kg/PCS (master)` vs `(derived)`. A weighed `wt_kg` on a counted
line still always wins; nothing resolved here is ever stored on the fact row.

The existing `unit_weights_for()` and its two lookup queries stay exactly as they are — they become
rung 3.

### 3.2 `default_uom_id` prefill

When a stage is picked and its `default_uom_id` is set and present in the branch's LOOSE list, the
form selects that UoM. Purely a keystroke saver; the user can change it.

### 3.3 `uom_item_map_mst` — suggestion only

The item master already stores per-item unit conversions, and they independently reproduce the
finishing master's numbers (evidence in §9.2). The Unit Weights tab therefore shows, next to each
typed value, the item's map rows and the kg/unit they imply, with a one-click action that writes
the implied value into `jute_prod_finishing_unit_wt`.

It is deliberately **not** a rung in the entry form's ladder:

- **Coverage** — 3 of 12 dev3 finishing qualities have any map row; 0 of 21 on sls. No `CUTS` row
  exists in `uom_mst`, so the main loose unit could never resolve there.
- **Wrong grain** — `item_mst` has no `co_id` (co-scope is via `item_grp_mst.co_id`), so one item
  shared across branches would carry one weight. The whole point of this change is branch scope.
- **It gets wiped** — `src/masters/items.py:696` runs
  `DELETE FROM uom_item_map_mst WHERE item_id = :item_id` and re-inserts on every item save. A
  masters clerk editing an unrelated field would silently drop finishing weights mid-month.
- **Direction is unreliable** — see §2.3, and the live contradiction in §9.2 (dev3 q1 implies
  500 kg/bale where the finishing master says 400).

Surfacing it in the master puts a stale or backwards row in front of a human once, instead of
quietly weighting every daily count.

Computing the implied kg/unit from a map row:

- Read `relation_value` literally: *map_to units per one map_from unit*.
- Recognise the mass side with the **existing** `PURCHASE_UOM_MT` / `PURCHASE_UOM_KG` tuples
  (`src/juteProduction/constants.py:501`) — they already resolve the MTS metre-vs-tonne trap they
  were written for. Exactly one side must be a mass unit.
- mass side is `map_to`: `kg_per_unit = relation_value * (1000 if MT else 1)`.
- mass side is `map_from`: `kg_per_unit = (1000 if MT else 1) / relation_value`.
- If the count side's `uom_name` carries a multiplier (`PER 100 PCS`, `100 PCS`), do **not**
  compute — display the raw row only. No name parsing, no guessing.

The batch lookup reuses `get_item_uom_mappings()` (`src/masters/query.py:1275`) verbatim — it
already returns `item_id, map_from_id, map_from_name, map_to_id, uom_name, relation_value,
rounding` for an expanding `:item_ids` bindparam. No new query function.

## 4. Seeds

`dbqueries/migrations/seed_finishing_stock_stages_uoms.sql`, variable block at the top
(dev3 `@co_id=1, @branch_id=2, @updated_by=1`; further branches and the sls values in comments).
Every insert guarded by `NOT EXISTS` on the active row, so re-running is safe.

**Branches seeded** — those that already hold finishing qualities, listed explicitly:
dev3 co 1 / br 2 and co 9 / br 12; sls co 2 / br 29, co 106 / br 87, co 1 / br 3.

**Stages** — the ten codes from today's constant, in the same order (`sort_order` 10, 20, … 100):

| stage_code | stage_name | process | quality_type | default_uom_id |
|---|---|---|---|---|
| IN_LINE | Cuts in line (from weaving) | NULL | NULL | NULL |
| S4_CUTS | S4 cuts | NULL | NULL | NULL |
| CAL_CUTS | Calendered cuts | `calendering` | NULL | NULL |
| CUTTING | Cutting | `cutting` | NULL | NULL |
| HEMMING | Hemming | `hemming` | NULL | NULL |
| HERACKLE | Herackle | `herackle` | NULL | NULL |
| BRANDING | Branded pressing | NULL | NULL | NULL |
| AP | Branded AP | NULL | NULL | NULL |
| BALE_ARRANGING | Bale arranging | NULL | NULL | NULL |
| OTHER | Other | NULL | NULL | NULL |

`process` is filled only where the stage code matches a `FINISHING_PROCESSES` token exactly.
`damping`, `lapping`, `rolling`, `sacksewing` and `balepress` get no stage — they are production
processes with no counted-stock stage on the source sheet.

`quality_type` and `default_uom_id` are NULL everywhere. The source sheet's Hessian/Sacking split
is not unambiguous (bale arranging and the joint categories appear under both), and a wrong value
seeded into a master nobody revisits is worse than a blank one the user fills the first time they
open the page.

**UoMs** — matching today's hard-coded lists exactly:

| bucket | uom_name | dev3 uom_id | sls uom_id | sort_order |
|---|---|---|---|---|
| BALE | BALE / BALES | 201 | 172 | 10 |
| LOOSE | CUTS | (seeded §2.5) | (seeded §2.5) | 10 |
| LOOSE | BDL | 170 | 170 | 20 |
| LOOSE | PCS | 143 | 143 | 30 |

dev3 has both `BALE` (201) and `BALES` (172); sls has only `BALES`. The seed resolves the id by
name per tenant rather than hard-coding it, preferring an exact `BALE` then `BALES`.

`dbqueries/migrations/seed_finishing_stock_setup_menu.sql` — one `menu_mst` row, path
`juteProduction/masters/finishingStockSetup`, parent `Masters` under Jute Production, derived via
`CONCAT(p.menu_path, ...)` off the parent like `seed_finishing_menu.sql`. Roles mapped by the
tenant admin, not seeded.

## 5. Backend

New router `src/juteProduction/finishing_stock_masters.py`, mounted at
`prefix="/api/finishingStockMasters", tags=["jute-finishing-stock-masters"]` in `src/main.py`
(one import + one `include_router`, beside the existing `finishing_stock_router` at line 320).
Queries go in `src/juteProduction/finishing_stock_masters_query.py` as arg-less `text()` builders.

Portal persona throughout: `Depends(get_tenant_db)` + `Depends(get_current_user_with_refresh)`.
Handlers are plain `def`. Literal `{"data": ...}` returns. Soft delete via `active = 0`.
Validators raise 400 before MySQL can 500.

```
GET    /setup?co_id&branch_id     -> {uoms: [uom_mst picker], processes: [...],
                                      qualities: [...], stages: [...]}   -- feeds all three tabs
GET    /stage_list?co_id&branch_id
POST   /stage_create    PUT /stage_edit/{id}    DELETE /stage_delete/{id}
GET    /uom_list?co_id&branch_id[&bucket]
POST   /uom_create      PUT /uom_edit/{id}      DELETE /uom_delete/{id}
GET    /unit_wt_list?co_id&branch_id[&finishing_quality_id]
                                  -> rows + `item_map` suggestions per quality (§3.3)
POST   /unit_wt_create  PUT /unit_wt_edit/{id}  DELETE /unit_wt_delete/{id}
```

Validation (400, messages as module constants, house style of `winding_stock.py`):

- **stage** — `stage_code` non-empty and unique among active rows of (co, branch); `stage_name`
  non-empty; `process` NULL or in `FINISHING_PROCESSES`; `quality_type` NULL or in (1, 2);
  `default_uom_id` NULL or an active row of the branch's LOOSE UoM list.
- **uom** — `uom_id` an active `uom_mst` row; `bucket` in `FINISHING_STOCK_BUCKETS`; unique among
  active rows of (co, branch, uom_id).
- **unit weight** — quality active in `co_id`; `uom_id` in the branch's UoM list;
  `finishing_stage_id` NULL or active in (co, branch); `wt_per_unit_kg` > 0 and <=
  `FINISHING_STOCK_WT_MAX`; unique among active rows of (co, branch, quality, uom, stage), the
  NULL stage being its own slot.

### Changes to `src/juteProduction/finishing_stock.py`

- `GET /setup` returns `stages` as master rows
  (`{finishing_stage_id, stage_code, stage_name, quality_type, process, default_uom_id, sort_order}`),
  a new `uoms` list (`{fs_uom_id, uom_id, uom_name, bucket, sort_order}`) replacing
  `loose_qty_uoms`, and the `unit_weights` list of §3.1. `qualities` keeps its derived
  `cut_wt_kg` / `bag_wt_kg` / `bundle_wt_kg` fields — they are rung 3.
- Create/edit accept `finishing_stage_id` and `qty_uom_id`. Validation becomes: stage id active in
  (co, branch) and its `quality_type` compatible with the picked quality; `qty_uom_id` active in
  (co, branch) with the matching `bucket`. `stage` and `qty_uom` are written as snapshots from the
  resolved master rows, never from the request.
- `row_to_dict` resolves `stage_label` from the stage master, falling back to the stored `stage`
  snapshot for rows whose master row was since deleted.
- `copy_previous_day` copies the id columns along with everything else — no change beyond the
  column list.

### Changes to `src/juteProduction/constants.py`

`FINISHING_STOCK_STAGES`, `FINISHING_STOCK_STAGE_CODES` and `FINISHING_STOCK_LOOSE_QTY_UOMS` are
demoted to seed-only reference: kept, with a comment pointing at this spec and at the seed file,
and removed from every runtime code path. A branch with no stage rows simply offers no stages —
`stage` is nullable, so that degrades to today's "stage optional" behaviour rather than an error.

## 6. Frontend

### API constants — `src/utils/api.ts`, inside `apiRoutesPortalMasters` after `FINISHING_STOCK_*`

```ts
FINISHING_STOCK_MASTERS_SETUP:  `${API_URL}/finishingStockMasters/setup`,
FINISHING_STOCK_STAGE_LIST:     `${API_URL}/finishingStockMasters/stage_list`,
FINISHING_STOCK_STAGE_CREATE:   `${API_URL}/finishingStockMasters/stage_create`,
FINISHING_STOCK_STAGE_EDIT:     `${API_URL}/finishingStockMasters/stage_edit`,      // + /{id}
FINISHING_STOCK_STAGE_DELETE:   `${API_URL}/finishingStockMasters/stage_delete`,    // + /{id}
FINISHING_STOCK_UOM_LIST:       `${API_URL}/finishingStockMasters/uom_list`,
FINISHING_STOCK_UOM_CREATE:     `${API_URL}/finishingStockMasters/uom_create`,
FINISHING_STOCK_UOM_EDIT:       `${API_URL}/finishingStockMasters/uom_edit`,        // + /{id}
FINISHING_STOCK_UOM_DELETE:     `${API_URL}/finishingStockMasters/uom_delete`,      // + /{id}
FINISHING_STOCK_UNIT_WT_LIST:   `${API_URL}/finishingStockMasters/unit_wt_list`,
FINISHING_STOCK_UNIT_WT_CREATE: `${API_URL}/finishingStockMasters/unit_wt_create`,
FINISHING_STOCK_UNIT_WT_EDIT:   `${API_URL}/finishingStockMasters/unit_wt_edit`,    // + /{id}
FINISHING_STOCK_UNIT_WT_DELETE: `${API_URL}/finishingStockMasters/unit_wt_delete`,  // + /{id}
```

### Service — `src/utils/finishingStockMastersService.ts`

Types mirroring §5 1:1, functions returning `{ok, data}` / `{ok, error}` via `fetchWithCookie`,
same shape as `finishingStockService.ts`.

### Page — `src/app/dashboardportal/juteProduction/masters/finishingStockSetup/`

`page.tsx` container (mounted gate, `useSelectedCompanyCoId`, the same sidebar branch resolution as
`finishingStock/page.tsx`, MUI `Tabs`), with `_components/StagesTab.tsx`, `UomsTab.tsx`,
`UnitWeightsTab.tsx` rendering from props only.

- **Stages** — DataGrid + dialog: code, name, quality type (Hessian / Sacking / Both), process
  (select from `setup.processes`, blank allowed), default UoM (from the branch LOOSE list), sort.
- **UOMs** — DataGrid + dialog: UoM (`SearchableOptionSelect` over `setup.uoms`, the full `uom_mst`
  picker), bucket toggle, sort.
- **Unit Weights** — DataGrid + dialog: quality, UoM, stage (blank = all stages), kg/unit. Extra
  read-only column showing the item-map suggestion and its raw row, with a "use" action writing the
  implied value into the form (§3.3).

### Entry form — `finishingStock/_components/FinishingStockForm.tsx`

`stageOptions` comes from `setup.stages` filtered by the picked quality's `quality_type` (a stage
with NULL `quality_type` always shows); `uomOptions` from `setup.uoms` filtered by the picked
bucket. Picking a stage selects its `default_uom_id` when set. Weight prefill reads the
`unit_weights` lookup of §3.1 and names the source in helper text. Payload sends
`finishing_stage_id` and `qty_uom_id`.

Grid and report shapes are unchanged — `stage_label` and `qty_uom` keep their existing meaning.

Every page: `mounted` gate, `fetchWithCookie` only, no `any`, MUI `TextField type="date"`.

## 7. Rollout order (per tenant)

1. `seed_uom_cuts.sql` — `CUTS` must exist before anything references it.
2. `create_jute_prod_finishing_stock_masters.sql` — the three tables.
3. `seed_finishing_stock_stages_uoms.sql` — stages + UoMs per branch.
4. `alter_finishing_stock_master_ids.sql` — the §2.4 ALTER **and** the two backfill UPDATEs
   (separate from step 2 because the backfill needs the seeds of step 3).
5. `seed_finishing_stock_setup_menu.sql` — the menu row.
6. Deploy BE, then FE.

dev3 first. sls runs the same five with its variable block before the FE deploy. All via the
`run-migration` skill (pymysql; no mysql CLI on dev machines).

**Deploy order matters:** the BE must be live before the FE, because the new FE sends
`finishing_stage_id` / `qty_uom_id` that an old BE would reject.

## 8. Tests

`src/test/test_juteProduction_finishing_stock_masters.py` (mocked session, house style of
`test_winding_stock_entry.py`):

- setup shape; stage / uom / unit-weight create, edit, soft delete, 404 on unknown id.
- 400 matrix: blank stage code, duplicate active code, `process` not in `FINISHING_PROCESSES`,
  `quality_type` not in (1, 2), `default_uom_id` outside the branch list, inactive `uom_id`,
  bucket not in `FINISHING_STOCK_BUCKETS`, duplicate active (co, branch, uom),
  `wt_per_unit_kg` <= 0 or over `FINISHING_STOCK_WT_MAX`, cross-branch `finishing_stage_id`,
  duplicate active unit-weight slot including the NULL-stage slot.
- bind completeness via `_assert_binds_complete`.

Pure unit tests (no DB) for:

- `resolve_unit_weight` — all four rungs, stage-specific beating stage-NULL, `source` correctness.
- the §3.3 item-map arithmetic — mass side on either end, MT vs KG scaling, multiplier-named count
  units returning no computed value, neither-side-mass and both-sides-mass returning none.
  Regression fixtures from the real dev3 rows in §9.2, including the q1 contradiction.

Extend `test_juteProduction_finishing_stock.py`: create / edit with the id fields, snapshot
writing, cross-branch stage rejected, bucket/UoM mismatch rejected, `stage_label` falling back to
the stored snapshot when the master row is gone.

FE: vitest for the stage / UoM filtering and the prefill lookup; `npx tsc --noEmit`.

Then browser QA on dev3 (Empire / Factory) via the `portal-ui-flow-tester` agent — the new masters
page and the entry form, per `.claude/test-credentials.md`.

## 9. Findings from the data probe (2026-09-20, dev3 + sls)

### 9.1 The §6 weight chain is built but unfed

| Source | Wired | Live data |
|---|---|---|
| `finishing_quality.std_bale_weight` -> kg/bale | yes | dev3 4 of 10 on co 1 (6 of 12 tenant-wide), sls 5 of 21 |
| weaving quality -> `cut_wt_kg` | yes | **0 of 10** dev3 co 1 (1 of 12 tenant-wide), **0 of 21** sls — no `jute_prod_weaving_quality` row shares a finishing item |
| `std_bale_weight / no_of_bags` -> kg/bag | yes | dev3 4 of 12, sls 2 of 21 |
| target map `pcs_per_bundle` -> kg/bundle | yes | dev3 1 row, sls **0** |
| `jute_prod_finishing_daily.prod_wt_kg` | **never written** | 0 of 929 sls rows |
| `jute_prod_bale_entry.net_wt` | only writer of `prod_wt_kg` | table **empty**, both tenants |

This is why the unit-weight master exists: it routes around every one of these.

### 9.2 `uom_item_map_mst` cross-check (dev3)

| Quality | Map row | Implies | Finishing master | |
|---|---|---|---|---|
| q12 HESSIAN CLOTH 38-8.12 | `MTS -> BALES 2.28632` | 1000 / 2.28632 = **437.384** kg/bale | `std_bale_weight` 437.384 | agrees |
| q4 PRINTED TYPE A | `PER 100 PCS -> KG 58.0` | **0.58** kg/bag (item name: "580 GMS") | 290 / 500 = 0.58 | agrees |
| q4 | `PER 100 PCS -> BALES 0.2` | 500 pcs/bale | `no_of_bags` 500 | agrees |
| q1 HESSIAN CLOTH 40-6.5 | `MTS -> BALES 2.0` | **500** kg/bale | `std_bale_weight` **400.000** | **conflicts** |

Three of four agree to the last decimal, which validates both the mechanism and the literal
direction reading of §3.3. The fourth is a live data error in one of the two masters — surfacing it
in the Unit Weights tab is the point.

## 10. Reported, not fixed — outside this spec

1. `src/juteProduction/finishing_entry.py:319` hard-codes `"prod_wt_kg": None`. Finishing
   Production Entry is count-only, so the column that would carry observed kg/unit is never
   written. Adding a weight field is a change in that module.
2. sls weaving qualities all point at placeholder item 335522 ("JUTE CLOTH (MIGRATED)"), so
   `cut_wt_kg` derives for nothing there. Re-pointing them is a data fix, the user's call.
3. `jute_prod_bale_entry` is empty in both tenants, so no actual per-bale weight exists yet.
4. dev3 q1's 500-vs-400 kg/bale contradiction (§9.2) needs a ruling on which master is right.

None of these block this work.
