# Bale Production (Bale Press) — Entry + Reports Spec Sheet

**Status:** DRAFT for review (2026-09-16) — built from the mill's Google Sheet
*"BALE PRODUCTION (Responses)"* plus a survey of what already exists in `vowerp3be` / `vowerp3ui`.
Open decisions are in **§9**; everything else is a proposal that can be red-lined before or during the build.
**Persona:** Portal (tenant DB). **Default tenant:** `dev3`. **No approval workflow** (direct CRUD + soft
delete, like every other jute-production daily entry).
**Sibling docs:** `docs/finishing-department-spec-sheet.md` (Bale Press is finishing process #9 there),
`docs/winding-person-keyed-entry-spec.md` (the rapid-weighing entry idiom this page copies).

---

## 0. The one-paragraph version

Today every bale that leaves the bale press is weighed and typed into a Google Form: date, spell, quality,
machine, "vendor" (really the consignee/state series for Govt sacking), bale no, gross weight, trolley no.
Spreadsheet tabs then keep the **last bale number per series**, the **standard bale weight + standard MR%
per quality**, take the SQC **MR% readings**, and produce two reports: a printed **Daily Bale Sheet** (all
bales of the day, grouped by quality) and the **Bale Heavy/Light (HY/LT)** report (observed vs standard vs
moisture-corrected weight per quality, with Hessian / Sacking / Overall totals). This spec moves that into
the ERP as **one new per-bale table + one small bale-series master**, and **reuses** the finishing quality
master (item + hessian/sacking type + std bale weight + packsheet), the SQC Packing-MR readings, the
Bale Press machines, spells and trolleys that already exist.

## 1. What the Google Sheet does today (source of truth for this spec)

Workbook **"BALE PRODUCTION (Responses)"** — a Google Form feeds the two entry tabs; every other tab is
a formula/pivot layer on top of them. Tabs found (names as the owner refers to them):

| Tab | Rows sampled | Role | Columns (verbatim) |
|---|---|---|---|
| **BALE** | form responses since 1-Jan-2024 | Main production entry — one row per bale weighed at the bale press | `Timestamp, Date, SPELL, Quality, Machine No, VENDOR NAME, Bale No, Gross Weight, Trolley No` |
| **REPACK** | form responses since 24-Jul-2024 | Same form, separate tab — bales that were opened and re-pressed; the **original bale no is re-used** (duplicates within the tab are normal) | identical to BALE (`Date` mostly blank — only `Timestamp` is filled) |
| **STD** (std bale weight) | 31 qualities | Standards per quality | `Quality (full name), Pack quality / short name, Std bale wt (kg), MR (std MR%), code, QUALITY (type), WFT (weft count)` |
| **Last Bale No** | 3 hessian + 7 sacking rows | Running bale-number counters | Hessian: `Quality → Last Bale No.`; Sacking: `State → Last Bale No.` |
| **MR%** | Jan-2026 | SQC moisture-regain readings: 10 readings + average, keyed by **date + quality type** (HESSIAN/SACKING) in the log, and by **quality name** in the day table used by HY/LT | `QUALITY NAME, MR% 1..MR% 10, Average` and `DATE, QUALITY(type), 1..10, AVG MR%` |
| **HY/LT** | one day (27-Jan-2026) | **Bale Heavy/Light** report — observed vs standard vs moisture-corrected bale weight per quality, with HESSIAN / SACKING / OVERALL totals | `QUALITY, CODE NO(type), NO OF BALE, WFT USED, AVG MR%, STD WT(KG), OBS (KG)/BALE, CORR (KG)/BALE, OBS %, CORR %, FOR TODATE STD/OBS/CORR` |
| (unnamed, 33 cols) | Jan-2026 | Month-to-date HY/LT working: per-day bale counts, per-quality MTD totals `NO OF BALE, TOTAL OBS(KG), OBS(KG)/BALE(EXC-5KG), STD BALE WT, MR%, CORR(KG)/BALE, OBS %, CORR %` | derived |
| (unnamed, 48 / 55 cols) | today | **Daily Bale Sheet** print layout — for one date, 7–8 side-by-side blocks (one per quality): `SL NO, VENDOR NAME, Bale No, Gross Weight, Trolley No, BALE ACTUAL WEIGHT` × 41 rows | derived |
| (unnamed, 12 cols) | — | Date-parsing helper over BALE | derived — ignore |

### 1.1 Column semantics (as observed in the data)

| Sheet column | Observed values | Meaning |
|---|---|---|
| `SPELL` | `A1, A2, B1, B2` | Working spell of the day (mill spell master). |
| `Quality` | `39.00"- 8x 7 - 5.85LOCAL / 2000.0 YDSCode:1036/J`, `22.50"- 6x 6 - 9.44LOCAL / 500.0 PCSCode:2124/J`, `Govt-BT-WILL 580 GM BAG`, `39"- 5.85`, `40"-6.50`, `H/Bag`, … | The **finished-goods item**. The long form is the ERP item description (width" – ends x picks – oz/wt, LOCAL, pack qty + UOM, `Code:<item code>/J`). Short forms (`39"- 5.85`, `40"-6.50`) are hand-typed aliases of the same items — the STD tab maps both to one pack-quality (`39(8X7)`, `40(8X8)`) and one item code (1036, 1083, 1189, …). |
| `Machine No` | always `2` | Bale press machine. |
| `VENDOR NAME` | `PUNJAB, BIHAR, CHHATISGARH, ANDHRAPARDESH, TELENGANA, TAMILNADU, HARIYANA, JHARKHAND, FCI` for sacking; `HESSIAN VENDOR` (placeholder) for hessian | For **sacking** (Govt bag orders) this is the **consignee / order series** — a state procurement agency or FCI. Hessian has no such split. |
| `Bale No` | sacking 17795…24746; hessian 1088…2386 | Running serial **within a series**. Sacking series are per **state** (Last Bale No tab: one counter per state, shared across the 22.50" bag qualities and Govt-BT-WILL); hessian series are per **quality**. |
| `Gross Weight` | 280–453 kg | Bale weight as read on the scale (bale + trolley on the platform). |
| `Trolley No` | `61, 62, 71` | The weighing trolley used. The Daily Bale Sheet has a `BALE ACTUAL WEIGHT` column next to it; HY/LT deducts a **flat 5 kg** per bale (`OBS(KG)/BALE(EXC-5KG)`). |
| `Std bale wt` | 290 (sacking A-type), 332 / 369 / 437 / 405 / 380 / 311.85 (hessian), 144 (H/Bag) | Standard **net** bale weight per quality. |
| `MR` (std) | 16 = hessian, 20 = sacking | Standard moisture-regain % by quality type. |
| `WFT` | 9 (hessian), 12 / 13 (sacking) | Weft yarn count (lbs) of the quality — informational in HY/LT (`WFT USED`). |
| `code` | 1036, 1083 | ERP item code (matches `Code:1036/J` in the description). |

### 1.2 HY/LT formulas — reverse-engineered and verified against the sheet's own numbers

Sample (27-Jan-2026, quality `40.00"- 8x 8 - 6.50 … Code:1189/J`): 9 bales, Σ gross = 3435 kg, std 369 kg, std MR 16, avg MR 19.11.

| # | Formula | Check |
|---|---|---|
| F1 | `obs_per_bale = Σ gross_wt / n_bales − DEDUCTION` where DEDUCTION = **5 kg** flat in the sheet | 3435/9 − 5 = **376.67** ✓ (sheet 376.67) |
| F2 | `corr_per_bale = obs_per_bale × (100 + std_mr) / (100 + avg_mr)` | 376.67 × 116 / 119.11 = **366.83** ✓ |
| F3 | `obs_pct = (obs_per_bale − std_wt) / std_wt × 100` | (376.67 − 369)/369 = **+2.08 %** ✓ |
| F4 | `corr_pct = (corr_per_bale − std_wt) / std_wt × 100` | (366.83 − 369)/369 = **−0.59 %** ✓ |
| F5 | `todate_std = n × std_wt`, `todate_obs = n × obs_per_bale`, `todate_corr = n × corr_per_bale` | 3321 / 3390 / 3301.49 ✓ |
| F6 | Group rows (SACKING, HESSIAN, OVERALL): sum F5 across qualities; `obs_pct = (Σobs − Σstd)/Σstd`, `corr_pct = (Σcorr − Σstd)/Σstd` | HESSIAN 6380 / 6441 / 6292 → **+0.96 % / −1.37 %** ✓ |
| F7 | Second check, MTD SACKING Jan-2026: 278 bales, Σ 79465, std 290, MR 19.52, std MR 20 | 79465/278 − 5 = 280.845 ✓; ×120/119.52 = 281.97 ✓; −3.16 % / −2.77 % ✓ |

`avg_mr` per quality comes from the MR% tab's day table (10 readings per quality, averaged); when a quality
has no reading that day the sheet falls back to the **type-level** (HESSIAN / SACKING) daily average.
Positive % = bale is **heavy** (over standard), negative = **light**.

---

## 2. What already exists in the ERP (verified against source, 2026-09-16)

| Concept in the sheet | Existing ERP home | Verdict |
|---|---|---|
| Quality (the cloth/bag item) | `item_mst` rows with `item_grp_mst.item_type_id = 5` ("Jute Cloth"); the long form `22.50"- 6x 6 - 9.44LOCAL / 500.0 PCSCode:2115/J` **is** `item_mst.item_name` migrated from the legacy system, `Code:2115` = `item_mst.item_code` / `legacy_item_code`. Picker SQL: `get_finishing_items_query()` (`src/juteProduction/finishing_query.py:36`). | **Reuse.** Entry stores `finishing_quality_id` (below), which carries the item. The hand-typed aliases (`39"- 5.85`, `40"-6.50`) disappear — the dropdown is the item list. |
| Quality type HESSIAN / SACKING + std bale weight + packsheet | `jute_prod_finishing_quality` (`finishing_models.py:32`): `finishing_quality_id, co_id, branch_id, quality_type (1=Hessian, 2=Sacking), item_id, packsheet_wt kg, std_bale_weight kg, no_of_bags`. CRUD at `/api/finishingMasters/quality_*`; FE `juteProduction/masters/finishingQualityMaster`. | **Reuse as the standards master** — the STD tab's *Std bale wt* and *QUALITY* columns are already there. **Extend** with two nullable columns: `std_mr_pct` (STD tab "MR": 16 hessian / 20 sacking) and `weft_yarn_item_id` (STD tab "WFT" → link to the yarn item whose `jute_yarn_mst.jute_yarn_count` is 9 / 12 / 13 lbs). |
| Std MR% | `jute_sqc_std_mr_mst` is keyed on **jute-yarn item groups** (`item_type_id = 4`) and four card/drawing processes — it cannot hold a cloth-item standard without re-keying. The SQC bag-weight page (`jute_sqc_bag_weight.std_mr_pct`) keeps the standard next to the item instead. | **Do not touch** `jute_sqc_std_mr_mst`. Put `std_mr_pct` on the finishing quality (above). |
| MR% readings (10 per quality per day, averaged) | `jute_sqc_packing_mr` (`src/models/jute.py:1222`): `entry_date, item_id (optional cloth item), quality_group 'HESSIAN'/'SACKING', readings JSON, calc_avg_mr`. Page `juteSQC/packingMr`, endpoints `/api/juteSQC/*packing_mr*`. | **Reuse as-is.** HY/LT reads `calc_avg_mr` for (date, item) first, falls back to (date, quality_group) — exactly the sheet's two MR tables. **Nothing new to build for MR entry.** |
| SPELL A1/A2/B1/B2 | `spell_mst` (`status`, not `active`); branch scope via `shift_mst.branch_id`; `get_finishing_spells_query()` + `spinning_entry._resolve_spell()`. | **Reuse.** |
| Machine No | `machine_mst` joined to `machine_type_mst.machine_type_name = 'Bale Press'` (`FINISHING_MACHINE_TYPE_NAMES["balepress"]`, `get_finishing_machines_query()`). | **Reuse.** Auto-selected when the branch has exactly one bale press. |
| Trolley No 61 / 62 / 71 | `trolly_mst` (`trolly_id, trolly_name, trolly_weight, trolly_type 'T', machine_type_id`), CRUD `/api/spinningMasters/trolly_*`. | **Reuse.** Trolleys tagged with the Bale Press `machine_type_id` appear in the picker; `trolly_weight` is the tare (see §9 Q3). |
| VENDOR NAME (state / FCI / "HESSIAN VENDOR") + Last Bale No | Nothing. `state_mst` exists (`state, state_code`) but FCI is not a state; `party_mst` has customers but no series concept; `sales_order_govtskg.pcso_no` is the Govt order reference but is per order, not a running series. | **New small master** `jute_prod_bale_series` (§4.1). One row = one running counter = one sheet "vendor"/hessian quality. Optional links to `state_mst` / `party_mst` so a series can be matched to existing records without forcing it. |
| One row per bale (bale no, gross, trolley) | **Nothing.** `jute_prod_finishing_daily` (balepress) stores one aggregate qty per (date, spell, machine, quality) and upserts on that key — structurally incompatible with per-bale rows. Confirmed by exhaustive grep: no `bale_no`, series, or per-bale weight anywhere. | **New table** `jute_prod_bale_entry` (§4.2). |
| Heavy/Light maths | `src/juteSQC/bag_weight.py` `row_corr(obs, mr, std_mr_pct)` + hy/lt % helpers; DPR `build_yarn_heavy_light()`. | **Reuse the helper**; same sign convention (positive = heavy). |
| Printing a day sheet | `juteSQC/_shared/SqcPrintButton` + `printReport` (`SqcPrintReport`, `PrintSection`, `PrintColumn`); report shell `src/components/reports/` (`useReportBranches`, `ReportPanel`, `ReportGrid`, `exportRowsToExcel`, `exportRowsToPdf`). | **Reuse.** |
| Bale counts feeding the DPR | `jute_prod_finishing_daily` (`process='balepress'`, `prod_uom='bale'`) is what `dpr_reports.py` rolls up today; the DPR explicitly flags "Bale / Loose Stock" as not derivable. | **Out of scope for this pass** — see §9 Q7 (whether per-bale rows should auto-feed the balepress daily figure). |

### 2.1 Linked-data decision summary

- **Match, don't create:** items (`item_mst`), quality type + std bale wt + packsheet (`jute_prod_finishing_quality`), MR readings (`jute_sqc_packing_mr`), spells, Bale Press machines, trolleys, states (`state_mst`, optional link), customers (`party_mst`, optional link).
- **Create (no existing home):** `jute_prod_bale_series` (the "Last Bale No" tab as data), `jute_prod_bale_entry` (the BALE + REPACK tabs as data).
- **Extend (two nullable columns):** `jute_prod_finishing_quality.std_mr_pct`, `.weft_yarn_item_id`.
- **Data hygiene the ERP fixes for free:** the sheet has the same item typed three ways (`39"- 5.85`, `39(8X7)`, `39.00"- 8x 7 - 5.85LOCAL … Code:1036/J`); a bale-no duplicate typo (17833–17836 re-entered on 2-Jan-2024); `Date` blank on most REPACK rows; `#N/A` / `#DIV/0!` in the MR% lookups when a quality has no reading. Each of those becomes a validation or a fallback rule below.

---

## 3. Process model

```mermaid
flowchart LR
    FQ["Finishing Quality Master\n(item, type, std bale wt,\npacksheet, std MR%, weft)"] --> E
    BS["Bale Series Master\n(PUNJAB, FCI, 39\"-5.85 …\nlast_bale_no)"] --> E
    MC["Bale Press machine\n(machine_mst)"] --> E
    TR["Trolley (trolly_mst)"] --> E
    E["Bale Production Entry\none row per bale\nPROD | REPACK"] --> DS["Daily Bale Sheet\n(print)"]
    E --> HL["Bale Heavy/Light\n(day / range)"]
    MR["SQC Packing MR\n(jute_sqc_packing_mr)"] --> HL
    E -. "bumps last_bale_no" .-> BS
```

**Entry flow (per bale, ~2–6 seconds):** date + spell + machine are set once per session. The operator picks the
quality (type-ahead), the series is auto-resolved (hessian → the item's own series; sacking → the consignee
series list, remembered from the previous bale), **bale no is pre-filled with `last_bale_no + 1`**, they type
the gross weight, pick/keep the trolley, press **Enter** → row saved, grid refreshes, bale no advances, weight
field clears, focus returns to weight. Repack is the same form with the *Repack* toggle on: the row still takes the **next series number** as its
bale no, and the operator types the **original bale no** of the bale that was opened (D2).

---

## 4. Data model

All tables: `co_id` + `branch_id` scoping, soft delete `active TINYINT DEFAULT 1`, audit `updated_by` /
`updated_date_time` only (no `created_*` — DB triggers), SQLAlchemy 2.0 `Mapped[]` models in
`src/juteProduction/bale_models.py` sharing `Base` from `src/models/mst.py`.

### 4.1 `jute_prod_bale_series` — running bale-number counters (new)

| column | type | notes |
|---|---|---|
| `bale_series_id` | INT PK AI | |
| `co_id` | INT NOT NULL | |
| `branch_id` | INT NOT NULL | counters are per mill |
| `series_name` | VARCHAR(60) NOT NULL | what the sheet calls VENDOR NAME: `PUNJAB`, `FCI`, `CHHATTISGARH`; for hessian the item's short name |
| `quality_type` | TINYINT NOT NULL | 1 = Hessian, 2 = Sacking — same codes as `jute_prod_finishing_quality` |
| `item_id` | INT NULL FK `item_mst` | set → series is bound to **one** quality (hessian pattern); NULL → any quality of that type (sacking/state pattern) |
| `state_id` | INT NULL FK `state_mst` | optional match to the state master |
| `party_id` | INT NULL FK `party_mst` | optional match to the customer (e.g. FCI) |
| `last_bale_no` | INT NOT NULL DEFAULT 0 | the "Last Bale No" tab. Bumped by every PROD save whose `bale_no` is higher; **editable** in the master = a reset |
| `active`, `updated_by`, `updated_date_time` | | |

Index `idx_bs_co_branch (co_id, branch_id)`, `idx_bs_item (item_id)`. App-level duplicate guard on
`(co_id, branch_id, series_name)` among `active = 1`.

Seed for dev3 (from the sheet's Last Bale No tab, values as of Feb-2026 — confirm before applying):
Sacking: CHHATISGARH 21600, ANDHRAPARDESH 24241, PUNJAB 24746, BIHAR 22277, TELENGANA 21359, FCI 23003,
JHARKHAND 2009; Hessian: `40"-6.50` 205, `39"- 5.85` 0, `38" - 7.14` 0. Spellings are the sheet's; the master
lets each be linked to the properly spelled `state_mst` row.

### 4.2 `jute_prod_bale_entry` — one row per bale (new)

| column | type | notes |
|---|---|---|
| `bale_entry_id` | INT PK AI | |
| `co_id`, `branch_id` | INT NOT NULL | |
| `tran_date` | DATE NOT NULL | production date (the form's *Date*, never blank) |
| `spell_id` | INT NOT NULL FK `spell_mst` | |
| `machine_id` | INT NULL FK `machine_mst` | Bale Press; NULL allowed for branches without a machine row |
| `entry_type` | VARCHAR(8) NOT NULL DEFAULT 'PROD' | `PROD` (BALE tab) or `REPACK` (REPACK tab) — same table, flagged (D2) |
| `finishing_quality_id` | INT NOT NULL FK `jute_prod_finishing_quality` | gives item, type, standards |
| `bale_series_id` | INT NOT NULL FK `jute_prod_bale_series` | |
| `bale_no` | INT NOT NULL | this row's own number (REPACK rows take the next series number too) |
| `original_bale_no` | INT NULL | REPACK only: the number of the bale that was opened and re-pressed; NULL on PROD rows (D2) |
| `trolly_id` | INT NULL FK `trolly_mst` | |
| `trolly_wt` | DECIMAL(10,3) NOT NULL DEFAULT 0 | snapshot of `trolly_mst.trolly_weight` at save |
| `packsheet_wt` | DECIMAL(10,3) NOT NULL DEFAULT 0 | snapshot of `jute_prod_finishing_quality.packsheet_wt` at save |
| `gross_wt` | DECIMAL(10,3) NOT NULL | scale reading, kg, `> 0` |
| `net_wt` | DECIMAL(10,3) NOT NULL | `gross_wt − trolly_wt − packsheet_wt` (server-computed, §5 F0) |
| `std_bale_wt` | DECIMAL(10,3) NULL | snapshot of the standard at save (HY/LT history survives later standard edits) |
| `weigh_time` | DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP | the form's *Timestamp*; orders the Daily Bale Sheet within a spell (business field, not audit) |
| `remarks` | VARCHAR(255) NULL | |
| `active`, `updated_by`, `updated_date_time` | | |

Indexes: `idx_be_co_branch_date (co_id, branch_id, tran_date)`, `idx_be_series_no (bale_series_id, bale_no)`
(**non-unique** — REPACK re-uses numbers and a series reset legitimately re-uses old numbers),
`idx_be_quality_date (finishing_quality_id, tran_date)`, `idx_be_spell (spell_id)`.

Bounds (constants in `src/juteProduction/constants.py`): `BALE_GROSS_MIN, BALE_GROSS_MAX = 1, 2000` kg;
`BALE_NO_MAX = 9_999_999`.

### 4.3 `jute_prod_finishing_quality` — two new nullable columns (alter)

| column | type | notes |
|---|---|---|
| `std_mr_pct` | DECIMAL(5,2) NULL | standard moisture regain %; migration seeds 16.00 for `quality_type = 1`, 20.00 for `= 2` where NULL (the STD tab values) |
| `weft_yarn_item_id` | INT NULL FK `item_mst` | the weft yarn item (`item_type_id = 4`); HY/LT shows its `jute_yarn_mst.jute_yarn_count` as "WFT USED" |

Existing `std_bale_weight` and `packsheet_wt` are used unchanged. The Finishing Quality Master page gains the
two fields.

### 4.4 Migration scripts (`dbqueries/migrations/`, run with the `run-migration` skill against dev3)

1. `create_jute_prod_bale_tables.sql` — 4.1 + 4.2 + the 4.3 ALTER + seed of `std_mr_pct`; rollback block at the end.
2. `seed_bale_production_menus.sql` — §7 menus (entry page + series master + 2 reports); `role_menu_map`
   mirrored from the hub for the report rows, entry/master rows left to the tenant admin (house convention).
3. `seed_bale_series_dev3.sql` — the 4.1 seed, **optional / confirm first**.

---

## 5. Formulas (server-side, mirrored in `utils/baleCalc.ts` for the live preview)

| # | Formula | Source |
|---|---|---|
| F0 | `net_wt = gross_wt − trolly_wt − packsheet_wt` | sheet's `BALE ACTUAL WEIGHT` / `EXC-5KG` (see §9 Q3 for what the 5 kg is) |
| F1 | `obs_per_bale = Σ net_wt / n_bales` (PROD rows only — REPACK is re-processing, not fresh press output; D2) | §1.2 F1 |
| F2 | `corr_per_bale = obs_per_bale × (100 + std_mr_pct) / (100 + avg_mr)` | §1.2 F2 — `bag_weight.row_corr()` |
| F3 | `obs_pct = (obs_per_bale − std_bale_wt) / std_bale_wt × 100` | §1.2 F3 |
| F4 | `corr_pct = (corr_per_bale − std_bale_wt) / std_bale_wt × 100` | §1.2 F4 |
| F5 | `total_std = n × std_bale_wt`, `total_obs = Σ net_wt`, `total_corr = n × corr_per_bale` | §1.2 F5 |
| F6 | Group / overall: sums of F5; `% = (Σ − Σstd) / Σstd × 100` | §1.2 F6 |
| F7 | `avg_mr` for (date, item): mean of `jute_sqc_packing_mr.calc_avg_mr` where `item_id = item` on that date; else where `quality_group = type`; else **NULL** → F2/F4 blank and the row is flagged `mr_source = 'none'` (`'item'` / `'group'` otherwise) | the two MR tables in the sheet |
| F8 | Range (e.g. month-to-date): per quality, `avg_mr` = bale-count-weighted mean of the per-day F7 values; `std_bale_wt` = the snapshot on the rows (so a mid-month standard change does not rewrite history) | data33 tab MTD block |
| F9 | Next bale no = `series.last_bale_no + 1`; on every save (PROD and REPACK) `last_bale_no = max(last_bale_no, bale_no)`. Deleting / renumbering the series' TOP bale winds it back to `max(opening_bale_no, highest remaining active bale_no ≤ that number)`; `opening_bale_no` = the value last set in the master (2026-09-21, old register not migrated) | Last Bale No tab |
| F10 | **DPR sync (D4):** after every create / edit / delete, the matching `jute_prod_finishing_daily` row (`process='balepress'`, key `co_id, tran_date, spell_id, machine_id, finishing_quality_id`) is upserted with `prod_qty` = count of active PROD bales, `prod_uom='bale'`, `prod_wt_kg` = Σ net_wt; count 0 → that row is soft-deleted. An edit that moves a bale to another key syncs both keys. | merge with finishing Bale Press entry |

Sign convention: **positive = Heavy, negative = Light** (same as `bag_weight.py` and the DPR yarn HY/LT).

---

## 6. Backend

Router `src/juteProduction/bale_entry.py` (+ `bale_query.py`, `bale_models.py`), registered
`app.include_router(bale_entry_router, prefix="/api/baleProd", tags=["jute-bale-production"])`.
Reports `src/juteProduction/bale_reports.py` (+ `bale_reports_query.py`) under the shared
`/api/juteProductionReports` prefix. Handlers are sync `def`, `Depends(get_tenant_db)` +
`Depends(get_current_user_with_refresh)`, `{"data": …}` responses, `None` for SQL NULL, typed binds.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/baleProd/entry_setup?co_id&branch_id[&tran_date]` | `{qualities, series, machines, trollies, spells, entries_today_count}` — qualities join item + standards + weft count |
| GET | `/api/baleProd/next_bale_no?co_id&bale_series_id` | `{last_bale_no, next_bale_no, last_entry}` |
| GET | `/api/baleProd/entries_by_date?co_id&branch_id&tran_date[&spell_id][&entry_type]` | rows + per-quality totals (n, Σgross, Σnet, avg net) |
| POST | `/api/baleProd/entry_create` | body §6.1; **409** with the clashing row when `(series, bale_no)` already exists as active PROD unless `allow_duplicate = true` |
| PUT | `/api/baleProd/entry_edit/{id}` | same body minus type; recomputes snapshots |
| DELETE | `/api/baleProd/entry_delete/{id}?co_id` | soft delete (co-scoped in read guard and UPDATE) |
| GET | `/api/baleProd/bale_search?co_id&branch_id&bale_no[&bale_series_id]` | history of a bale number (PROD + REPACK) |
| GET | `/api/baleProd/series_setup?co_id[&branch_id]` | `{items (type 5), states, parties}` for the series master form |
| GET/POST/PUT/DELETE | `/api/baleProd/series_list?co_id[&branch_id]`, `series_create`, `series_edit/{id}`, `series_delete/{id}?co_id` | series master (rides on the same router, waste-entry precedent) |
| GET | `/api/juteProductionReports/bale_daily_sheet?co_id&branch_id&tran_date[&spell_id][&entry_type]` | §8.3 |
| GET | `/api/juteProductionReports/bale_heavy_light?co_id&branch_id&date_from&date_to` | §8.4 |

### 6.1 `entry_create` body

```json
{ "co_id": 1, "branch_id": 4, "tran_date": "2026-09-16", "spell_id": 91, "machine_id": 210,
  "entry_type": "PROD", "finishing_quality_id": 12, "bale_series_id": 3, "bale_no": 24747,
  "trolly_id": 61, "gross_wt": 294.0, "remarks": null, "allow_duplicate": false }
```

Validation (400 unless stated): required ids exist and belong to `co_id`/`branch_id`; quality's `quality_type`
must equal the series' `quality_type`; if the series has `item_id` it must equal the quality's item; spell via
`_resolve_spell`; `tran_date` not in the future; `gross_wt` within bounds and `net_wt > 0`; `bale_no ≥ 1`;
REPACK requires `original_bale_no ≥ 1`; if no active bale with that number exists in the series the API answers
**409** ("No bale 4456 found in series CHHATTISGARH to repack") unless `allow_duplicate`; PROD rows must send
`original_bale_no = null`. A `(series, bale_no)` clash with any active row → **409** unless `allow_duplicate`.

### 6.3 Response contract (FE and BE build against this; every response is `{"data": ...}`)

```ts
// GET /api/baleProd/entry_setup?co_id&branch_id
{ qualities: [{ finishing_quality_id, item_id, item_code, item_name, quality_type /*1|2*/,
                std_bale_weight, packsheet_wt, std_mr_pct, weft_yarn_item_id, weft_count /*lbs|null*/ }],
  series:    [{ bale_series_id, series_name, quality_type, item_id, state_id, party_id, last_bale_no }],
  machines:  [{ machine_id, machine_name, mech_code }],          // Bale Press only, branch-scoped
  trollies:  [{ trolly_id, trolly_name, trolly_weight }],        // trolly_type 'T', Bale Press-tagged first then untagged
  spells:    [{ spell_id, spell_code, spell_name, working_hours }] }

// GET /api/baleProd/next_bale_no?co_id&bale_series_id
{ bale_series_id, series_name, last_bale_no, next_bale_no,
  last_entry: { bale_entry_id, tran_date, bale_no, gross_wt, net_wt, trolly_id, finishing_quality_id } | null }

// Row shape shared by entries_by_date.rows, bale_search.rows, entry_create/entry_edit echo
BaleRow = { bale_entry_id, co_id, branch_id, tran_date /*YYYY-MM-DD*/, spell_id, spell_code, machine_id, machine_name,
            entry_type /*'PROD'|'REPACK'*/, finishing_quality_id, item_id, item_name, quality_type,
            bale_series_id, series_name, bale_no, original_bale_no, trolly_id, trolly_name, trolly_wt,
            packsheet_wt, gross_wt, net_wt, std_bale_wt, weigh_time /*ISO datetime*/, remarks }

// GET /api/baleProd/entries_by_date?co_id&branch_id&tran_date[&spell_id][&entry_type]
{ rows: BaleRow[],
  totals: [{ finishing_quality_id, item_name, quality_type, entry_type, bales, total_gross, total_net, avg_net, std_bale_wt }] }

// POST /api/baleProd/entry_create   (PUT /api/baleProd/entry_edit/{id} takes the same body)
body { co_id, branch_id, tran_date, spell_id, machine_id?, entry_type, finishing_quality_id, bale_series_id,
       bale_no, original_bale_no?, trolly_id?, gross_wt, remarks?, allow_duplicate? }
-> { row: BaleRow, last_bale_no }          // 409 detail (string) on clash / unknown original unless allow_duplicate

// DELETE /api/baleProd/entry_delete/{id}?co_id      -> { bale_entry_id }
// GET /api/baleProd/bale_search?co_id&branch_id&bale_no[&bale_series_id]  -> { rows: BaleRow[] }

// GET /api/baleProd/series_setup?co_id[&branch_id]
{ items: [{ item_id, item_code, item_name }] /*item_type 5*/, states: [{ state_id, state }], parties: [{ party_id, supp_name }] }
// GET /api/baleProd/series_list?co_id[&branch_id]
{ rows: [{ bale_series_id, co_id, branch_id, branch_name, series_name, quality_type, item_id, item_name, state_id, state,
           party_id, party_name, last_bale_no, bales_total /*active rows*/ }] }
// POST series_create body { co_id, branch_id, series_name, quality_type, item_id?, state_id?, party_id?, last_bale_no }
// PUT  series_edit/{id} same body;  DELETE series_delete/{id}?co_id  (400 if active bales reference it)

// GET /api/juteProductionReports/bale_daily_sheet?co_id&branch_id&tran_date[&spell_id][&entry_type]
{ tran_date, branch_name,
  blocks: [{ finishing_quality_id, item_name, quality_type, std_bale_wt, packsheet_wt,
             rows: [{ sl_no, entry_type, series_name, bale_no, original_bale_no, spell_code, gross_wt, trolly_name, net_wt, weigh_time }],
             prod_bales, repack_bales, total_gross, total_net, avg_net /*PROD*/, dev_pct /*(avg_net-std)/std*100 | null*/ }],
  totals: { by_type: [{ quality_type, prod_bales, repack_bales, total_gross, total_net }],
            overall:  { prod_bales, repack_bales, total_gross, total_net } } }

// GET /api/juteProductionReports/bale_heavy_light?co_id&branch_id&date_from&date_to
{ date_from, date_to,
  rows:   [{ finishing_quality_id, item_name, quality_type, bales, weft_count, avg_mr, mr_source /*'item'|'group'|'none'*/,
             std_bale_wt, std_mr_pct, obs_per_bale, corr_per_bale, obs_pct, corr_pct, total_std, total_obs, total_corr }],
  groups: [{ label /*'SACKING'|'HESSIAN'|'OVERALL'*/, bales, total_std, total_obs, total_corr, obs_pct, corr_pct }],
  notes: string[] }   // e.g. "2 qualities had no MR reading — corrected columns left blank"
```

### 6.2 Tests (`src/test/`)

`test_jute_production_bale_entry.py` (setup shape, next-no, create happy path + every 400/409 branch,
REPACK rules, edit/delete scoping, series CRUD) and `test_bale_production_reports.py` (pure builders for F1–F8
on stub rows, endpoint param validation, MR fallback flags). Pattern: `TestClient(app)` + dependency
overrides + `MagicMock` session, no DB.

---

## 7. Menus (`menu_mst`, tenant DB; no `/dashboardportal` prefix in `menu_path`)

| Menu | Parent | Path | Icon | report |
|---|---|---|---|---|
| Bale Production | Jute Production | `juteProduction/baleProduction` | `inventory_2` | 0 |
| Bale Series Master | Jute Production › Masters (the parent of `finishingQualityMaster`) | `juteProduction/masters/baleSeriesMaster` | `format_list_numbered` | 0 |
| Daily Bale Sheet | Production Reports (`juteProduction/report`) | `juteProduction/report/baleDailySheet` | `receipt_long` | 1 |
| Bale Heavy Light | Production Reports | `juteProduction/report/baleHeavyLight` | `scale` | 1 |

The proxy permission check and the landing tiles are menu-driven — no frontend registration beyond the pages.
Users must log out/in once (sidebar caches the menu tree).

---

## 8. Frontend (`vowerp3ui`)

Route constants in `src/utils/api.ts` → `apiRoutesPortalMasters`: `BALE_PROD_ENTRY_SETUP`,
`BALE_PROD_NEXT_BALE_NO`, `BALE_PROD_ENTRIES_BY_DATE`, `BALE_PROD_ENTRY_CREATE`, `BALE_PROD_ENTRY_EDIT`,
`BALE_PROD_ENTRY_DELETE` (base paths), `BALE_PROD_BALE_SEARCH`, `BALE_SERIES_LIST/CREATE/EDIT/DELETE`,
`BALE_REPORT_DAILY_SHEET`, `BALE_REPORT_HEAVY_LIGHT`. Every dropdown is `SearchableSelect` /
`SearchableOptionSelect`; forms are Zod + react-hook-form (string fields, numeric `.refine`s, payload builders);
co/branch from the sidebar; branch picker only when more than one branch is in scope.

### 8.1 `juteProduction/baleProduction/` — the entry page

```
page.tsx                       container: co/branch, date, spell, machine, entry-type toggle, signals
_components/BaleEntryForm.tsx  quality → series (auto) → bale no (pre-filled) → gross wt → trolley; Enter saves
_components/DailyBaleGrid.tsx  the day's rows (both types), per-row edit/delete, quality totals footer
_components/SeriesCounterStrip.tsx  chips: "PUNJAB · last 24746", one per series used today / in scope
hooks/useBaleSetup.ts  useBaleEntriesByDate.ts  useNextBaleNo.ts  useBaleMutations.ts
types/baleTypes.ts  utils/baleSchema.ts  utils/baleCalc.ts (+ .test.ts)
```

Behaviours copied from the winding/spinning doff forms: keep quality/series/trolley after save, clear only the
weight, advance the pre-filled bale no, `useEnterOnSingleMatch` + Enter-advance, live preview of
`net = gross − trolley − packsheet` and of the deviation vs standard (`+2.1 %` / `−0.6 %`) with a soft colour
band (warn beyond ±3 %, configurable constant; never blocks). A 409 opens a confirm dialog showing the clashing
row (date, spell, weight) with *Save anyway* → resend with `allow_duplicate`. A *Find bale* box calls
`bale_search`. `juteProduction/finishing/balePress/page.tsx` becomes a redirect to this page (D4) so bale
press output is entered in exactly one place.

### 8.2 `juteProduction/masters/baleSeriesMaster/` — series master

DataGrid + dialog: name, quality type, bound item (optional, hessian), state (optional, `state_mst`), party
(optional), **last bale no** (editable = reset, with a confirm). Plus: `finishingQualityMaster/page.tsx`
gains *Std MR %* and *Weft yarn* fields.

### 8.3 `juteProduction/report/baleDailySheet/` — Daily Bale Sheet

Filters: branch (scoped), date (single), spell (optional), type (PROD / REPACK / both). Output: one block per
quality, `SL NO | Series | Bale No | Gross | Trolley | Net`, block footer (bales, Σ gross, Σ net, avg net,
std, deviation %), grand footer per type and overall. REPACK rows sit in the same quality block, flagged
*Repack* with the *Original Bale No* column filled (D2); block/grand footers show PROD and REPACK counts separately. Print (landscape, page break
between qualities when needed) and Excel export. This is the 48/55-column tab, as a list instead of side-by-side
blocks.

### 8.4 `juteProduction/report/baleHeavyLight/` — Bale Heavy/Light

Filters: branch, date range (defaults: today; quick buttons *Today*, *MTD*). Grid columns exactly as the sheet:
`Quality | Type | No of bales | Weft (lbs) | Avg MR % | Std wt | Obs kg/bale | Corr kg/bale | Obs % | Corr % |
Total std | Total obs | Total corr`, then SACKING / HESSIAN / OVERALL rows; `mr_source` shown as a small tag
(item / group / **none** — corrected columns blank). Excel + print. Tint: heavy positive, light negative.

---

## 9. Decisions (locked with the owner, 2026-09-16)

| # | Decision | Chosen |
|---|---|---|
| D1 | The 5 kg deduction | **Packsheet, per quality** — `net_wt = gross_wt − trolly_wt − packsheet_wt`, packsheet from `jute_prod_finishing_quality.packsheet_wt` (5 kg seeded where blank), trolley tare from `trolly_mst.trolly_weight` (0 when the scale is tared). |
| D2 | Repack | **Same table, flagged `entry_type='REPACK'`, two number columns**: `bale_no` (this row's own number) and `original_bale_no` (the bale that was re-pressed; blank on PROD rows). Reports show the flag + original number. Production counts / HY/LT use PROD rows (assumption — see Q4 below). |
| D3 | Bale series | **One series master, optional item / state / party links, manual reset** by editing `last_bale_no`. |
| D4 | DPR feed | **Merge** — bale saves maintain the finishing balepress daily row (F10); the Finishing › Bale Press tile redirects to the new page. One entry, no duplicate work. |

### 9.1 Remaining assumptions (defaults in **bold** are built; say so if different)

| # | Question | Options / default |
|---|---|---|
| Q1 | ~~answered → D3~~ **Series model.** Sacking bale numbers run per consignee (state / FCI) across all bag qualities; hessian per quality. Is one *Bale Series* master with an optional item link (§4.1) the right shape, or do you want the series keyed strictly on `state_mst` + a separate hessian counter per quality? | **One master, optional `state_id` / `party_id` links** |
| Q2 | ~~answered → D3~~ **Series reset.** The sheet shows hessian counters back at 0 while sacking continues into the 24000s. Reset by editing `last_bale_no` in the master (manual), or automatic per financial year / per PCSO order? | **Manual edit in the master** |
| Q3 | ~~answered → D1~~ **What is the 5 kg?** HY/LT deducts a flat 5 kg per bale ("EXC-5KG"). Is it the **packsheet** (so use `jute_prod_finishing_quality.packsheet_wt` per quality — it already exists, and Govt sacking sales carries `pack_sheet` too), or the trolley tare (then `trolly_mst.trolly_weight` per trolley)? Does the scale reading include the trolley? | **Packsheet from the quality master (5 kg seeded where blank); trolley tare from `trolly_mst.trolly_weight`, 0 if the scale is tared** |
| Q4 | **Repack in totals.** D2 fixes the columns; still open is whether REPACK rows count in production totals / HY/LT. | **Excluded from production counts and HY/LT (they re-press an already-counted bale); listed and sub-totalled on the Daily Bale Sheet** |
| Q5 | **Duplicate bale no.** Block hard, or warn + allow with confirmation (the sheet has real typos like 17833–17836 twice)? | **409 + confirm dialog (allow with `allow_duplicate`)** |
| Q6 | **MR% source.** Confirm the SQC *Packing MR* page is (or will be) where the 10 readings are entered, so HY/LT reads `jute_sqc_packing_mr` and no second MR entry screen is built. Quality-level readings first, type-level fallback? | **Yes, reuse; item → group fallback; blank when neither** |
| Q7 | ~~answered → D4~~ **Feed the DPR?** Should saving bales also upsert the `jute_prod_finishing_daily` balepress row (`prod_qty` = bale count per date/spell/machine/quality) so the DPR keeps working from one source? | **Not in this pass** (documented hook point) |
| Q8 | **Weft.** Link the quality to a weft yarn item (`weft_yarn_item_id`) or store a plain `weft_count_lbs` number as the sheet does? | **Yarn item link** (matches existing yarn master) |
| Q9 | **Who weighs.** Add an optional operator (EB no) on the bale row? Not in the sheet. | **No** |
| Q10 | **Tenant.** Build/seed on dev3 only; sls later via `migrate-tenant-tables`? | **dev3 only** |

---

## 10. Build sequence

1. Migration 4.4-1 + ORM models + constants (BE).
2. `bale_entry.py` + `bale_query.py` (setup, next no, CRUD, search, series CRUD) + tests.
3. `bale_reports.py` (daily sheet, heavy/light; pure builders unit-tested) + tests.
4. `main.py` registration; `pytest src/test -k bale`.
5. FE constants → entry page → series master → finishing quality master fields → two reports; `tsc`, `vitest`.
6. Menu seed SQL; module docs (`../vowerp3ui/docs/claude/modules/jute-production/`); commit both repos.
7. After the migrations are applied on dev3: `/qa-portal-page juteProduction/baleProduction` browser run.
