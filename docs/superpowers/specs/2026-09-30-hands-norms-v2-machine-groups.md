# Hands Norms v2 — machine groups, production norms and no-norm kinds

**Status:** design per owner decisions of 2026-09-30 · **Tenant:** dev3 first, then sls (with the held items) · **Repos:** vowerp3be + vowerp3ui · **Module:** Jute Production (Hands Report / Hands Norms, TKT-000165 / 167 / 202)

## 1. Problem

The Hands Report (`src/juteProduction/hands_report.py`) targets a variable norm as `machines of machine_type_id that ran in the shift × hands_per_machine` (`_targets`, hands_report.py:198-224). Tenant machine types are too coarse for that: on sls all 334 branch-29 looms are one type 7 "Loom" (Hessian 200, S4 114, Sacking 12, Rapier 4, Leno 4), so WEAVER(H) "2 MC = 1 HAND" cannot be linked without being targeted off every loom — which is why the TKT-165 repair links only 9 of 50 per-machine norms. Drawing norms are "4 POINTS" per hand (a machine is 10 points, not 1), carding norms span breaker + finisher cards, and 41 sls norms are production-based ("315 KGS / WINDER") or have no norm at all ("NO NORMS", "AS ACTUAL") — today those print a real-looking 0 target and every hand on them reads as excess. The TKT-202 master (`hands_norms.py`, page `juteProduction/masters/handsNorms`) only knows F/V.

## 2. Decisions (owner, 2026-09-30)

1. **Machine groups with per-member units.** New `jute_prod_machine_group_mst` + `jute_prod_machine_group_dtl` (members may mix machine types). A norm gains `machine_group_id` (nullable); `machine_type_id` stays as the fallback when no group is set. Shift target = Σ units of member machines that ran in that shift ÷ units per hand.
2. **Production norms (kind P):** scope (group, type **or department**, owner 2026-09-30) + `qty_per_hand` + `qty_uom`; qty per hand is per hand **per shift**; shift target = production qty of the scoped machines / department that shift ÷ qty per hand; day / MTD add the shifts. Qty comes from the same run-source legs (§4.3). Department scope exists because winding production is person-keyed: with machine attribution only, ~13 % of sls winding kg has no machine and would be lost.
3. **Two more kinds:** N "no norm" (blank target, excluded from excess/short) and A "as actual" (target = hands present). Kinds: **F** fixed, **M** machine-based (was V), **P** production, **N**, **A**. Existing rows are mapped (§3.4); the report stays backward compatible.
4. **Groups are a tab on the Hands Norms page**, not a separate master (§7).
5. **Access:** view-only roles cannot save — FE hides Add/Edit/Save, API returns 403 (§6.3).
6. **Menu/roles:** as Hands Report, already in the held menu migration. No other module reads hands norms, so nothing else changes.
7. **Migrations:** hand-written SQL, applied on dev3 + sls together with the held items; no seed data.

Latitude taken (one line each): **units per hand is a new column** (`units_per_hand`), `hands_per_machine` kept as a derived legacy mirror — the user types "N per hand", storing N makes the target an exact division and avoids the 1/N rounding the dialog already papers over (22 → 0.0455 → 21.98). **`norm_kind` is a new CHAR(1) column**, `variable_fixed` kept and mirrored (M,P → 'V'; F,N,A → 'F') — adding a column lets the migration run before the code deploy without breaking the current report. **Access uses `user_menu_access_level()`** (spinning_lock.py:48). `jute_prod_loom_group` (20260928, loom-only STUDY/CONTROL, no units) is deliberately **not** reused.

## 3. Data model

Migration `dbqueries/migrations/20260930_hands_norms_v2_machine_groups.sql`, house style of `20260928_loom_modification_tables.sql` (comment blocks end with a bare `;`, `CREATE TABLE IF NOT EXISTS`, FKs on masters, no DB UNIQUE on code columns — dup guards in code among `active = 1`, no created_* columns). ORM: two new classes beside `JuteProdHandsNormsMst` in `src/models/mst.py:203`, plus the new columns on it.

### 3.1 `jute_prod_machine_group_mst`

```sql
CREATE TABLE IF NOT EXISTS jute_prod_machine_group_mst (
  machine_group_id  INT          NOT NULL AUTO_INCREMENT,
  co_id             INT          NOT NULL,
  branch_id         INT          NOT NULL,                 -- members are machines of this branch's departments
  group_code        VARCHAR(30)  NOT NULL,                 -- dup guard in code on (co_id, branch_id, group_code) among active = 1
  group_name        VARCHAR(100) NOT NULL,
  unit_label        VARCHAR(20)  NOT NULL DEFAULT 'machine', -- machine | point | spindle | ... (free text, shown in the UI)
  remarks           VARCHAR(255) NULL,
  active            TINYINT      NOT NULL DEFAULT 1,
  updated_by        INT          NULL,
  updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (machine_group_id),
  KEY idx_mg_co_branch (co_id, branch_id, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.2 `jute_prod_machine_group_dtl`

```sql
CREATE TABLE IF NOT EXISTS jute_prod_machine_group_dtl (
  machine_group_dtl_id INT          NOT NULL AUTO_INCREMENT,
  machine_group_id     INT          NOT NULL,
  machine_id           INT          NOT NULL,              -- active machine whose dept_mst.branch_id = group branch (checked on write)
  units                DECIMAL(9,3) NOT NULL DEFAULT 1,    -- > 0; e.g. loom 1, drawing frame 10 points, heavy spinning frame 2
  active               TINYINT      NOT NULL DEFAULT 1,    -- soft-delete + reinsert on edit; one active row per machine per group (code)
  PRIMARY KEY (machine_group_dtl_id),
  KEY idx_mgd_group   (machine_group_id, active),
  KEY idx_mgd_machine (machine_id),
  CONSTRAINT fk_mgd_group   FOREIGN KEY (machine_group_id) REFERENCES jute_prod_machine_group_mst (machine_group_id),
  CONSTRAINT fk_mgd_machine FOREIGN KEY (machine_id)       REFERENCES machine_mst (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

A machine may belong to several groups ("All looms" and "Hessian looms"); no cross-group uniqueness.

### 3.3 `jute_prod_hands_norms_mst` — new columns

```sql
ALTER TABLE jute_prod_hands_norms_mst
  ADD COLUMN norm_kind        CHAR(1)       NOT NULL DEFAULT 'F' AFTER variable_fixed,  -- F | M | P | N | A
  ADD COLUMN machine_group_id INT           NULL AFTER machine_type_id,
  ADD COLUMN scope_dept_id    INT           NULL AFTER machine_group_id,                 -- M/P: whole department instead of group/type (dept_mst.dept_id)
  ADD COLUMN units_per_hand   DECIMAL(9,3)  NULL AFTER hands_per_machine,               -- M: group/type units one hand covers
  ADD COLUMN qty_per_hand     DECIMAL(12,3) NULL AFTER units_per_hand,                  -- P: production per hand per shift
  ADD COLUMN qty_uom          VARCHAR(10)   NULL AFTER qty_per_hand,                    -- P: 'kg' | 'cuts' (PROD_UOMS)
  ADD KEY idx_hands_norm_group (machine_group_id),
  ADD CONSTRAINT fk_hands_norm_group FOREIGN KEY (machine_group_id) REFERENCES jute_prod_machine_group_mst (machine_group_id),
  ADD CONSTRAINT fk_hands_norm_dept  FOREIGN KEY (scope_dept_id)    REFERENCES dept_mst (dept_id);
```

Exactly one of `machine_group_id`, `machine_type_id`, `scope_dept_id` is set on a complete M/P row (the "scope"); the API clears the other two on save.

Kept, now derived on every save: `variable_fixed` ('V' for M/P, 'F' for F/N/A) and `hands_per_machine` (`ROUND(1/units_per_hand, 4)` for M, NULL otherwise). Both are read only by code that predates v2; drop them in a later cleanup migration once v2 is on every tenant.

### 3.4 Mapping of existing rows (same file, after the ALTER; order matters)

```sql
UPDATE jute_prod_hands_norms_mst SET norm_kind = 'A' WHERE UPPER(TRIM(COALESCE(norms_text,''))) = 'AS ACTUAL';
UPDATE jute_prod_hands_norms_mst SET norm_kind = 'M', units_per_hand = ROUND(1 / hands_per_machine, 3)
 WHERE norm_kind = 'F' AND hands_per_machine > 0;
-- zero-shift rows whose text is a real norm become INCOMPLETE M / P (owner 2026-09-30) so the page lists them for completion.
-- The P statement runs BEFORE the 'V' one: sls 114 / 115 / 206 ('15 BALES OR 2.5 MT / HAND', '140 BNDLS / HAND') are
-- flagged 'V' with zero shifts and no rate, and their text is a production norm (found while sizing, 2026-09-30).
UPDATE jute_prod_hands_norms_mst SET norm_kind = 'P', qty_uom = 'kg'
 WHERE norm_kind = 'F' AND hands_per_machine IS NULL AND shift_a + shift_b + shift_c + shift_g = 0
   AND UPPER(COALESCE(norms_text,'')) REGEXP 'KG|MT |BALE|BNDL';
UPDATE jute_prod_hands_norms_mst SET norm_kind = 'M' WHERE norm_kind = 'F' AND UPPER(COALESCE(variable_fixed,'')) = 'V';
UPDATE jute_prod_hands_norms_mst SET norm_kind = 'M'
 WHERE norm_kind = 'F' AND hands_per_machine IS NULL AND shift_a + shift_b + shift_c + shift_g = 0
   AND UPPER(COALESCE(norms_text,'')) REGEXP 'POINT|MC|FRM|FRAME|SPINDLE|/ HAND';
UPDATE jute_prod_hands_norms_mst SET norm_kind = 'N'
 WHERE norm_kind = 'F' AND hands_per_machine IS NULL AND shift_a + shift_b + shift_c + shift_g = 0;
```

Sized on sls on 2026-09-30 (SELECT only, statements simulated in this order; ids in the file's VERIFY block): P 8 = 50, 173, 174, 175, 218 ('n KGS / WINDER') + 114, 115 ('n BALES OR x MT / HAND') + 206 ('140 BNDLS / HAND'); M-by-'V' 1 = 113 ('10 MT / HAND, 5 IN A', shift_a 5 so not a zero row); M-by-REGEXP 13 = 140–146, 149, 152–155 ('n POINTS') + 162 ('1 / 8 MC FOR 120 SPINDLE'); N 34 = 33 'NO NORMS' + 109 ('A=1' — stays F instead if the held `tkt202_sls_hands_norms_cleanup` ran first, it sets its shift_a = 1). Every real-text zero row lands where its text says. Incomplete M/P rows have no scope and no rate, so the report blanks them with the existing "not linked" note and the page's "cannot compute" banner lists them.

Everything else stays F. Final counts: **sls** 242 rows → A 2, M 64 (50 with a rate [47 F + 3 V] + 1 'V' + 13 REGEXP), P 8, N 34, F 134; **dev3** 3 rows → M 1 (id 5), F 2. The held TKT-165 repair sets 9 of the 47 to `V` + machine type first; they land in M either way. VERIFICATION block: `SELECT norm_kind, COUNT(*) … GROUP BY norm_kind` (expect the counts above) and a listing of the incomplete M/P rows (`norm_kind IN ('M','P') AND machine_group_id IS NULL AND machine_type_id IS NULL AND scope_dept_id IS NULL`) for the owner to complete on the page. Rollback comment: `ALTER TABLE … DROP FOREIGN KEY fk_hands_norm_group, DROP KEY idx_hands_norm_group, DROP COLUMN norm_kind, …; DROP TABLE jute_prod_machine_group_dtl; DROP TABLE jute_prod_machine_group_mst;`.

## 4. Target rules per norm kind

"Machine ran in the shift" is unchanged: a (date, shift letter, machine) counts when at least one RUN leg (production entry or attendance mark, `RUN_SOURCES` hands_report_query.py:121-133) carries it; a machine seen only in Stoppage/Breakdown is listed, not counted (`_machine_runs`, hands_report.py:352). Shift letter = first char of the spell code, else G (`_shift_letter`, query.py:33).

`_machine_runs` (hands_report.py:328) additionally returns `ran[(date, shift)] -> {machine_id: {"kg": float|None, "cuts": float|None}}` for counted machine-shifts. `_targets` (hands_report.py:198) gains `actual` (the cell's per-shift hands) and `members[machine_group_id] -> {machine_id: units}` (new `get_hands_group_members_query()`, one query for the active members of every active group of the co/branch). The norm's machine set is `members[group]` when `machine_group_id` is set, else every machine of `machine_type_id`, else every active machine whose `machine_mst.dept_id = scope_dept_id` (units = 1 each outside a group — `machine_mst` has no units).

| Kind | Per-shift target | Day / MTD |
|---|---|---|
| **F** | the row's `shift_a..g` (as today) | sum of shifts / sum of days (unchanged) |
| **M** | `Σ units(member ∈ machine set, ran that shift) ÷ units_per_hand`; legacy row without `units_per_hand` → `Σ units × hands_per_machine` | same |
| **P** | `Σ qty(member ∈ machine set, ran that shift)[qty_uom] ÷ qty_per_hand`; member units are ignored (qty is already a quantity). **Department scope:** qty = Σ of the legs' qty for machines of that department **plus** person-keyed winding doffs whose winder belongs to that department (§4.3) | same |
| **N** | `None` on every shift → `total_target`, `excess`, `short`, `mtd_*` all `None` | `_rollup`/`_mtd_sums` already skip None (hands_report.py:236-290) |
| **A** | `= actual[shift]` → `excess = short = 0`, target adds to totals | same |

Blank vs zero: N and an **incomplete** M/P (no group/type, or no rate) → blank + note, as TKT-165 does today; a complete M/P whose machines never ran / produced → a real **0**.

**Worked examples (sls, branch 29):**
- Hessian looms: group "Hessian looms" = the 200 `machine_type 7` machines named `Hessian Loom - n`, units 1 each. WEAVER(H) norm M, group, `units_per_hand = 2` (mirror `hands_per_machine = 0.5`). Shift A: 180 Hessian looms ran → 180 ÷ 2 = **90** hands; 30 S4 looms that also ran are not members and do not count. Day = A + B + C targets; MTD = Σ days.
- 1st Drawing F/S: group "1st Drawing F/S", 5 frames × units 10 (`unit_label = 'point'`), norm "4 points per hand" → `units_per_hand = 4`. Shift with 4 frames run → 40 ÷ 4 = **10**.
- Carding F/S: group of breaker + finisher cards (on sls both are type 32 "R/G", names `Breaker…`/`Finisher…`), units 1, `units_per_hand = 2` → 6 cards run → **3**.
- Heavy spinning frames: group "Spinning frames", normal frames units 1, frames > 12 lbs units 2; spinner norm `units_per_hand = 2` → a heavy frame needs a full spinner, a normal one half.
- "315 KGS / WINDER": norm P, **scope = department WINDING**, `qty_per_hand = 315`, `qty_uom = 'kg'`. Shift A 2,520 kg of winding doffs (all winders of the department, machine or not) → **8** hands; B 1,890 → 6; day = 14, MTD adds days. A P norm on a group/type (e.g. a specific set of beaming machines) works the same on machine-keyed kg.

### 4.3 Production quantity per run-source leg

Each leg of `get_hands_machine_runs_query()` (hands_report_query.py:196-254) gains two columns, `prod_kg` and `prod_cuts` (NULL where the leg has none), and the outer SELECT (query.py:256-283) returns `SUM(k.prod_kg) AS prod_kg, SUM(k.prod_cuts) AS prod_cuts`. Double counting is prevented by carrying each quantity on exactly one leg:

| Leg | Table | kg | Note |
|---|---|---|---|
| Weaving | `jute_prod_weaving_daily` | **none** — `prod_cuts = SUM(cuts)` | kg exists only in the read-time day-slice (`production_kg`, weaving_query.py:821); usable with `qty_uom = 'cuts'` only |
| Doff | `daily_doff_tbl` | `SUM(net_weight)` | the spinning kg source |
| Spinning | `jute_prod_spinning_daily` | none (NULL) | `act_prod_doff` is derived from the same doffs — would double count |
| Beaming | `jute_prod_beaming_daily` | `SUM(act_prod_kg)` | |
| Finishing | `jute_prod_finishing_daily` | `SUM(prod_wt_kg)` **excluding `process = 'balepress'`** | that row is a synced copy of bale entries (services/bale_sync.py); `prod_qty` is mixed UoM, not used |
| Spreader | `spreader_prod_entry` | `SUM(no_of_rolls * wt_per_roll)` | |
| Drawing | `jute_prod_drawing_entry` | **none** | `diff_meter` is metres — not usable for P |
| Winding | `jute_prod_winding_daily_qlty` | `SUM(wd.kg)` via `LEFT JOIN` to per-winder doff sums | doffs are person-keyed (`jute_prod_winding_doff.production_qty`, its `machine_id` is dead); join `(co_id, tran_date, spell_id, eb_id)` to the winder's **earliest** active quality row of that shift (`MIN(winding_daily_qlty_id)`) so a winder on two qualities is counted once. Doffs whose winder has no quality row with a machine are **not** lost: a second query `get_hands_dept_production_query()` returns per (date, shift, dept_id) kg from `jute_prod_winding_doff` grouped by the winder's department — the dept of the `eb_id`'s designation in `daily_attendance` for that date (the rows the report already loads, hands_report_query.py:63-79), falling back to the quality-row machine's dept, else dropped and counted in a note (sls Sep-2026: 9,184 of 10,508 doffs machine-attributable; the rest attach by attendance). Department-scoped P norms use this per-dept total for winding plus the machine legs' kg summed by `machine_mst.dept_id` for everything else |
| Bale Press | `jute_prod_bale_entry` | `SUM(net_wt) WHERE entry_type = 'PROD'` | REPACK re-presses an existing bale |
| Waste | `jute_prod_waste_entry` | none | `waste_qty` is waste, not production |
| Attendance / Stoppage / Breakdown | — | none | |

## 5. Report changes (`hands_report.py`)

- `get_hands_norms_query` (query.py:87-115) adds `norm_kind, machine_group_id, units_per_hand, qty_per_hand, qty_uom` and joins `jute_prod_machine_group_mst.group_name AS machine_group`. Backward compatibility: `_kind(norm)` derives the kind when `norm_kind` is absent/blank from a row dict (tests, pre-migration tenants): `'V'` or a machine link → M, else F — the current `_is_variable` (hands_report.py:194) becomes this helper.
- `_detail_rows` (hands_report.py:428-508): rows carry `norm_kind`, `machine_group_id`, `machine_group`, `units_per_hand`, `qty_per_hand`, `qty_uom`; `variable_fixed` stays for the FE. `by_occupation` rollup keys (hands_report.py:693-706) add `norm_kind`, `machine_group`.
- Notes (hands_report.py:611-675): the "unresolvable" and "rate unlinked" scans skip kinds N/A; add `"{n} designation(s) have no norm by design (kind N) — target blank, excluded from excess/short: …"`, `"{n} designation(s) are targeted as actual (kind A): …"`, `"{n} kg of winding doffs had no machine attribution and were left out of production targets"` (when > 0), and the existing "no machine … counted as run" note now names groups too. Machines Run rows and `type_summary` gain `prod_kg` / `prod_cuts`.
- Totals: unchanged rules — excess/short per designation-day then summed; N contributes actuals only; A contributes target = actual. `meta.total_target` etc. therefore exclude N rows exactly as designations without a norm row are excluded today.
- FE report `handsReport/page.tsx:197` column "V/F" → "Kind" (F/M/P/N/A labels); `_service.ts:59` type adds the new fields; notes already render (`page.tsx:602`).

## 6. API changes (`src/juteProduction/hands_norms.py`, prefix `/api/handsNorms`, main.py:353)

### 6.1 Norms (existing endpoints, extended)

- `GET /setup` (hands_norms.py:510) adds `machine_groups` (id, code, name, unit_label, member_count, total_units, active) and `machines` (every active machine of the branch's departments: `machine_id, machine_name, mech_code, machine_type_id, machine_type_name, dept_id, dept`; ~700 rows on sls, filtered client-side).
- `GET /list` (hands_norms.py:543): `_serialise` (hands_norms.py:424) adds `norm_kind, machine_group_id, machine_group, unit_label, units_per_hand, qty_per_hand, qty_uom`; `is_computable` (hands_norms.py:410) becomes per kind — F/N/A always; M needs a scope (group, type or department) and (`units_per_hand > 0` or legacy `hands_per_machine > 0`); P needs a scope and `qty_per_hand > 0`.
- `POST /create` / `PUT /edit/{id}` (hands_norms.py:575, 620): `HandsNormCreate`/`HandsNormUpdate` (hands_norms.py:241-271) add `norm_kind`, `machine_group_id`, `scope_dept_id`, `units_per_hand`, `hands_per_unit` (input-only alias: stored as `units_per_hand = 1/hands_per_unit`), `qty_per_hand`, `qty_uom`. `variable_fixed` in the body is accepted as a legacy alias (`V` → M, `F` → F) when `norm_kind` is absent. `normalise_norm` (hands_norms.py:320) per kind: **F** shifts kept, links/rates cleared; **M** exactly one scope of group / type / department required (the other two stored NULL), `units_per_hand > 0`, shifts 0, `hands_per_machine` mirrored; **P** one scope of group / type / department, `qty_per_hand > 0`, `qty_uom ∈ ('kg','cuts')`, shifts 0; **N/A** everything cleared. `strict` (active row) semantics as today. New checks: `_check_group(db, co_id, branch_id, group_id)` — active group of the same co and branch (norm `branch_id` NULL → any branch of the co); `_check_dept(db, co_id, branch_id, dept_id)` — active `dept_mst` row of that branch. `GET /setup` also returns the branch's departments (`dept_id, dept_desc`). Duplicate active norm per (co, designation, branch, effective_date) refused as today (`_check_duplicate`, hands_norms.py:476).

### 6.2 Groups (new endpoints, same router)

| Endpoint | Body / params | Notes |
|---|---|---|
| `GET /groups?co_id&branch_id&include_inactive` | — | rows + `member_count`, `total_units`, `norms_using` (active norms referencing it) |
| `GET /groups/{id}?co_id` | — | header + active members (`machine_id, machine_name, mech_code, machine_type_id, machine_type_name, dept, units`) |
| `POST /groups/create` | `{co_id, branch_id, group_code, group_name, unit_label, remarks, members: [{machine_id, units}]}` | code dup guard among active; each member: active `machine_mst` row whose `dept_mst.branch_id = branch_id`, `units > 0`, no repeats |
| `PUT /groups/edit/{id}?co_id` | `{group_name?, unit_label?, remarks?, active?, members?}` | `members` is a full replacement: soft-delete rows not in the list, update units, insert new. `active: 0` refused with 400 while any active norm references the group (`"{n} active norm(s) use this group: …"`) |

Responses `{"data": ...}`; handlers are plain `def`; SQL via `text()` in the same module (query section, hands_norms.py:64-228).

### 6.3 Access check

Reuse `user_menu_access_level(db, user_id, co_id, branch_id, menu_path)` from `src/juteProduction/spinning_lock.py:48` (already shared by `spinning_quality_map.py:39`), which runs `get_user_menu_access_level_query()` (`src/common/portal/query.py:166`: `user_role_map → role_menu_map → menu_mst.menu_path`, `control_co_module` cap, MAX over roles; 1 Read, 2 Print, 3 Write, 4 Edit). `HANDS_NORMS_MENU_PATH = "juteProduction/masters/handsNorms"` (the held menu migration's row). `_require_level(db, token_data, co_id, branch_id, level)` raises `403 "This role can view hands norms but not change them"`. Thresholds mirror the FE action map (`sidebarContext.tsx:177`): `POST /create`, `POST /groups/create` need ≥ 3; `PUT /edit/*`, `PUT /groups/edit/*` need ≥ 4. `GET` endpoints stay at the page's view level (the sidebar/middleware already gate the page). The check runs after `_require_int` parsing and before any query.

## 7. UI (`vowerp3ui/src/app/dashboardportal/juteProduction/masters/handsNorms/`)

- **Tabs** on `page.tsx` (MUI `Tabs`, under the title/branch block, page.tsx:285-320): **Norms** (existing grid + dialog) and **Machine groups**. `canCreate = hasMenuAccess(pathname, "create")`, `canEdit = hasMenuAccess(pathname, "edit")` from `useSidebarContext()` (as `windingStock/page.tsx:55-59`); New buttons need `canCreate`, the Edit / Revision / group-edit actions `canEdit`; without either the page shows a "View only" chip and the action column is hidden (page.tsx:191-212, 296-303).
- **Norms grid:** "Type" column (page.tsx:216-221) shows F Fixed / M Machine / P Production / N No norm / A As actual; "Target" (`describeTarget`, `_schema.ts:218`) per kind: `"Σ units of Hessian looms ÷ 2 per hand"`, `"kg of Winding ÷ 315 kg per hand"`, `"No norm — blank"`, `"As actual"`; "Hands / machine" column becomes "Per hand" (`units_per_hand` + unit label, or `qty_per_hand` + uom).
- **Norm dialog** (`_components/HandsNormDialog.tsx`, kind select replaces `NORM_TYPE_OPTIONS` `_schema.ts:29`, fields block `HandsNormDialog.tsx:164-241`): kind (searchable, 5 options). **F** → shift fields (as today). **M** → "Scope" radio: Machine group (`SearchableOptionSelect`, label `name (n members · X units)`) / Machine type (existing select) / Department (searchable dept select) — the three are mutually exclusive, picking one clears the others; "Units per hand" (step 0.001) with the inverse box "or: hands per unit" (replaces machines-per-hand, `HandsNormDialog.tsx:205-218`); helper text "Target = Σ units of the machines that ran in the shift ÷ units per hand". **P** → same scope radio (department is the normal choice for winding), "Qty per hand per shift", "Unit" (kg / cuts, searchable). **N / A** → one info line, nothing else. Designation, effective date, D/I, norms text, active switch unchanged.
- **Machine groups tab:** grid (code, name, unit label, members, total units, norms using, status) + "New group". **Group dialog** (`_components/MachineGroupDialog.tsx`, `maxWidth="md"`, full-screen on xs): code, name, unit label (free text with suggestions machine/point/spindle), remarks; member picker = machine-type select + "Name contains" text (e.g. `Hessian`) + "Add all matching (n)" + "Units for selection" number + "Apply to selected"; members DataGrid (machine, type, dept, units — editable cell, remove) with checkbox selection and select-all; footer "n members · total units X"; Save.
- **Validation** (`_schema.ts` Zod, mirrored by the API): active M needs one scope (group / type / department) and `units_per_hand > 0` (≤ 999999.999); active P needs one scope, `qty_per_hand > 0`, uom picked; F shift fields ≥ 0; N/A nothing; group needs code (≤ 30), name (≤ 100), unit label, every member `units > 0`, no duplicate machine; retiring a group in use is refused by the API (error shown in the dialog). Duplicate active norm per designation/branch/date: API 400 as today.
- `_service.ts`: `NormKind = "F"|"M"|"P"|"N"|"A"`, new row/payload fields, `fetchMachineGroups`, `fetchMachineGroup`, `createMachineGroup`, `updateMachineGroup`; `src/utils/api.ts:1446-1450` adds `HANDS_NORMS_GROUPS`, `HANDS_NORMS_GROUP` (base path + `/${id}`), `HANDS_NORMS_GROUP_CREATE`, `HANDS_NORMS_GROUP_EDIT`. `formFromRow` (`_schema.ts:186`): legacy `V` or F-with-rate rows open as M with `units_per_hand = 1/hands_per_machine` when the column is empty; incomplete M/P rows from the migration open with kind preset and no scope, so the banner's "Show them" filter leads straight to the fields to fill.

## 8. Migration plan

Order per tenant, dev3 then sls (run-migration skill, owner names sls): (1) held `20260930_hands_norms_menu.sql` (menu + role grants — the access check needs the `menu_path` row); (2) held `dbqueries/repairs/tkt165_sls_hands_norm_machine_type_link_20260930.sql` (sls only); (3) new `20260930_hands_norms_v2_machine_groups.sql` (§3.1-3.4, verification block, rollback comment). Deploy code after (3); the current report keeps working between (3) and the deploy because it reads only the old columns. Groups are keyed in by the owner on the page afterwards (no seed). Users log out/in for the menu (sidebar cache).

## 9. Tests

Backend `src/test/test_juteprod_hands_report.py` (pure `build_hands_report` fixtures, new `members`/`ran` inputs): `test_group_norm_sums_member_units_of_machines_that_ran_and_divides_by_units_per_hand`, `test_group_member_seen_only_in_stoppage_adds_no_units`, `test_group_with_mixed_machine_types_counts_every_member`, `test_type_norm_without_group_still_uses_hands_per_machine`, `test_units_per_hand_beats_hands_per_machine_when_both_present`, `test_production_norm_divides_shift_kg_by_qty_per_hand_and_adds_shifts_for_the_day`, `test_production_norm_with_no_production_targets_zero_not_blank`, `test_production_norm_in_cuts_reads_prod_cuts`, `test_department_scoped_production_norm_adds_unattributed_winding_kg_by_winder_department`, `test_department_scoped_machine_norm_counts_every_machine_of_the_department`, `test_no_norm_kind_blanks_the_target_and_is_excluded_from_excess_short_and_named_in_notes`, `test_as_actual_kind_targets_the_hands_present_so_excess_and_short_are_zero`, `test_rows_without_norm_kind_resolve_from_the_legacy_flag`, `test_incomplete_machine_norm_is_blank_and_noted_but_n_and_a_are_not`, `test_machines_run_rows_and_type_summary_carry_prod_kg_and_cuts`, `test_mtd_adds_production_targets_day_by_day`.

Backend `src/test/test_juteprod_hands_norms.py`: `TestNormaliseNorm` — `test_machine_norm_with_group_clears_machine_type`, `test_scope_is_exactly_one_of_group_type_department`, `test_department_scope_must_belong_to_the_branch`, `test_active_machine_norm_needs_group_or_type_and_units_per_hand`, `test_hands_per_unit_input_is_stored_as_units_per_hand`, `test_production_norm_needs_qty_per_hand_and_uom`, `test_no_norm_and_as_actual_clear_shifts_links_and_rates`, `test_legacy_variable_fixed_body_maps_to_kind`, `test_variable_fixed_and_hands_per_machine_mirrors_are_written`; new `TestAccess` — `test_view_role_gets_403_on_norm_create_edit_and_group_writes`, `test_write_role_can_create_but_not_edit`, `test_setup_and_list_stay_open_to_view_role`, `test_access_check_uses_the_hands_norms_menu_path`; new `TestGroups` — `test_create_group_with_members_and_units`, `test_member_outside_branch_is_400`, `test_duplicate_member_or_zero_units_is_400`, `test_duplicate_active_group_code_is_400`, `test_edit_replaces_members_soft_deleting_removed_ones`, `test_retiring_group_used_by_an_active_norm_is_400`, `test_list_returns_member_count_total_units_and_norms_using`, `test_setup_returns_machines_and_groups`; `TestCreate` — `test_group_of_another_branch_is_400`.

Frontend `handsNorms/_schema.test.ts`: `validateHandsNormForm` per kind (M group/type + units > 0, P qty > 0 + uom, N/A nothing, F unchanged), `hands per unit converts to units per hand`, `toPayload sends only the kind's fields`, `formFromRow maps legacy V and F-with-rate rows to M with derived units per hand`, `describeTarget per kind`. New `_groupSchema.test.ts`: `filterCandidates by type and name`, `applyUnitsToSelection`, `totalUnits`, `validateGroupForm` (code/name/unit label, units > 0, duplicate machine). `handsReport/_service.test.ts`: kind label mapping.

## 10. Resolved questions (owner, 2026-09-30)

1. Winding P norms: **department scope added** (§2.2, §3.3, §4, §4.3, §6.1, §7) so person-keyed winding doffs count even when the winder has no machine that shift.
2. Zero-shift rows with real norm text: **mapped to incomplete M/P** (§3.4), not N, so the page lists them for completion.

## 11. Machines worked from attendance (owner, 2026-10-01)

Owner rulings after the first sls groups were keyed (WVGS4, WVGHESS, CRD):

1. **A machine "worked" in a shift only when attendance says so**: at least one worker is marked on it (`daily_ebmc_attendance.mc_id`, through a live, non-rejected `daily_attendance` header — the existing Attendance leg, hands_report_query.py). A production, waste, stoppage or breakdown entry alone no longer makes a machine count. sls Sep-2026 coverage of production machine-shifts by a worker mark: looms / spinning 96 %, winding 88 %, drawing 73 %, hemming / herackle / bale press 25–31 %, cutting 7 % — so the report must **note** the machine-shifts that have production but no worker marked (by machine type), for the mill to fix the marking.
2. **A machine stopped for the whole shift does not count** even with a worker marked: `GREATEST(attendance mc_stoppage_hours, jute_prod_stoppage_hours, breakdown_min / 60) >= shift hours` (shift hours = `spell_mst.working_hours` of the shift letter's full spell; fallback 8). Listed in the tab as "Stopped all shift".
3. **Storage: a real table rebuilt on open.** `jute_prod_machine_shift_worked` holds one row per (branch, date, shift letter, machine) with everything the report needs; every Hands Report request first rebuilds the rows of its own window (month start of From Date .. To Date) from the sources, in one transaction, then reads the table. Other reports may read the table; it is as fresh as the last open (`synced_at`).
4. **A "Machines Worked" tab on the Hands Report page** replaces the "Machines Run" entry of the view selector.

### 11.1 Table (`dbqueries/migrations/20261001_machine_shift_worked_table.sql`)

```sql
CREATE TABLE IF NOT EXISTS jute_prod_machine_shift_worked (
  machine_shift_worked_id INT           NOT NULL AUTO_INCREMENT,
  co_id                   INT           NOT NULL,
  branch_id               INT           NOT NULL,
  tran_date               DATE          NOT NULL,
  shift                   CHAR(1)       NOT NULL,               -- A | B | C | G (shift letter)
  machine_id              INT           NOT NULL,
  workers_marked          INT           NOT NULL DEFAULT 0,     -- distinct workers marked on the machine in attendance
  att_stoppage_hours      DECIMAL(6,2)  NOT NULL DEFAULT 0,     -- max mc_stoppage_hours on those marks
  stoppage_hours          DECIMAL(6,2)  NOT NULL DEFAULT 0,     -- jute_prod_stoppage_hours
  breakdown_min           DECIMAL(8,2)  NOT NULL DEFAULT 0,     -- jute_mech_breakdown
  shift_hours             DECIMAL(5,2)  NULL,
  prod_kg                 DECIMAL(14,3) NULL,
  prod_cuts               DECIMAL(12,3) NULL,
  winding_kg              DECIMAL(14,3) NULL,
  sources                 VARCHAR(160)  NULL,                   -- e.g. 'Attendance,Stoppage,Weaving'
  stopped_all_shift       TINYINT       NOT NULL DEFAULT 0,
  worked                  TINYINT       NOT NULL DEFAULT 0,     -- workers_marked > 0 AND stopped_all_shift = 0
  synced_at               DATETIME      NOT NULL,
  PRIMARY KEY (machine_shift_worked_id),
  UNIQUE KEY uq_msw (branch_id, tran_date, shift, machine_id),
  KEY idx_msw_co_branch_date (co_id, branch_id, tran_date),
  KEY idx_msw_machine (machine_id, tran_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

No FK to `machine_mst` (the rebuild is a bulk INSERT … SELECT of rows that already join it). Rollback: `DROP TABLE jute_prod_machine_shift_worked;`.

### 11.2 Rebuild (`hands_report_query.py` + `hands_report.py`)

`rebuild_machine_shift_worked(db, co_id, branch_id, d_from, d_to)`: one transaction — `DELETE … WHERE co_id, branch_id AND tran_date BETWEEN`, then `INSERT … SELECT` of the existing all-sources machine-runs SELECT (same legs, so production kg / cuts and both stoppage books land in the row) with `shift_hours`, `stopped_all_shift`, `worked` computed in SQL and `synced_at = NOW()`; `ON DUPLICATE KEY UPDATE` so two simultaneous opens cannot fail on the unique key. A deadlock / lock-wait (1213 / 1205) is retried once, then the request reads what the other open wrote. The endpoint calls it before reading, for every view; the read query is a plain SELECT on the table joined to `machine_mst` / `machine_type_mst` / `dept_mst` returning the same columns the builder takes today plus `workers_marked`, `shift_hours`, `stopped_all_shift`, `worked`, `synced_at`. `build_hands_report` stays pure (rows in, dict out).

### 11.3 Rules in the builder

- `_machine_runs`: a machine-shift is **counted** iff `worked` (when the row has no `worked` key — older fixtures — derive it: `Attendance` in sources and not stopped all shift). `runs` / `ran` index counted rows only. Each machine row gains `workers` (= workers marked), `shift_hours`, `status` (`WORKED` | `STOPPED_ALL_SHIFT` | `NO_WORKER_MARKED`), `group_ids` / `groups` (machine groups the machine belongs to); `counted` and `hands` stay.
- **M norms** read the counted set (unchanged code path). **P norms** sum the production of every scope machine row that carries production, worked or not — kg produced is kg produced.
- Notes: `"{n} machine-shift(s) have production entered but no worker marked in attendance — not counted for machine targets: Herackle 12, Hemming 9, …"`; `"{n} machine-shift(s) had a worker marked but were stopped the whole shift — not counted."`; the explanatory note says a machine counts when a worker is marked on it in attendance and it was not stopped all shift.
- `type_summary` rows gain `stopped_all_shift` and `no_worker_marked` counts; `meta` gains `stopped_all_shift`, `no_worker_marked`, `synced_at`. The machines view also returns `scopes`: one entry per M/P norm in force — `{designation_id, designation, norm_kind, scope: 'group'|'type'|'dept', scope_id, scope_name, units_per_hand}` — so the tab can filter machines by designation.

### 11.4 UI (`handsReport/page.tsx`)

MUI `Tabs`: **Manning** (the four manning views, unchanged) and **Machines Worked** (the former `machines_run` view; same endpoint, `view=machines_run`). Tab body: chips "Worked n · Stopped all shift n · No worker marked n" and "Synced hh:mm"; filters (all `SearchableSelect`): Shift, Status, Machine type, Group, Designation (uses `scopes`); the by-type summary; the grid — Date, Shift, Machine, Type, Department, Group, Workers marked, Stoppage hrs, Breakdown min, Prod kg / cuts, Entries, Status (chip). Excel export of what is filtered.

### 11.5 Tests

Builder: `test_machine_counts_only_when_a_worker_is_marked`, `test_production_without_a_worker_mark_is_listed_not_counted_and_noted`, `test_marked_machine_stopped_all_shift_is_not_counted_and_noted`, `test_production_norm_still_sums_kg_of_unmarked_machines`, `test_machine_rows_carry_status_workers_and_groups`, `test_scopes_list_the_machine_and_production_norms`, `test_rows_without_worked_flag_derive_it_from_sources`. Rebuild: `test_rebuild_deletes_then_inserts_the_window_in_one_transaction`, `test_rebuild_retries_once_on_deadlock`, `test_endpoint_rebuilds_before_reading`. FE: status labels, filters (shift / status / type / group / designation), chips.

## 12. Manual additions and exclusions of worked machines (owner, 2026-10-01)

Attendance may miss machines (or mark a wrong one). Owner rulings: **manual add with suggestions**, **both ways** (add a missed machine, exclude a wrongly marked one), allowed to **roles with edit access to the Hands Report menu**; every change keeps who, when and a reason.

### 12.1 Data (`dbqueries/migrations/20261001_machine_shift_override.sql`)

```sql
CREATE TABLE IF NOT EXISTS jute_prod_machine_shift_override (
  machine_shift_override_id INT          NOT NULL AUTO_INCREMENT,
  co_id                     INT          NOT NULL,
  branch_id                 INT          NOT NULL,
  tran_date                 DATE         NOT NULL,
  shift                     CHAR(1)      NOT NULL,              -- A | B | C | G
  machine_id                INT          NOT NULL,
  action                    VARCHAR(10)  NOT NULL,              -- ADD | EXCLUDE
  reason                    VARCHAR(255) NOT NULL,
  active                    TINYINT      NOT NULL DEFAULT 1,    -- undo = 0; one active row per machine-shift (code)
  updated_by                INT          NULL,
  updated_date_time         TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (machine_shift_override_id),
  KEY idx_mso_window (co_id, branch_id, tran_date, active),
  KEY idx_mso_machine (machine_id, tran_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE jute_prod_machine_shift_worked
  ADD COLUMN manual_action VARCHAR(10) NULL AFTER worked;      -- ADD | EXCLUDE, copied from the active override at rebuild
```

Rollback: `ALTER TABLE jute_prod_machine_shift_worked DROP COLUMN manual_action; DROP TABLE jute_prod_machine_shift_override;`.

### 12.2 Rule

`worked = ADD override OR (workers_marked > 0 AND NOT stopped_all_shift AND NOT EXCLUDE override)`. An ADD counts even with no entry anywhere and even when the stoppage books say stopped all shift (the supervisor said it worked). The rebuild applies the active overrides of its window in the same transaction, after the source INSERT: ADD rows are upserted (a machine-shift with no source row gets a row with `sources = NULL`), EXCLUDE rows are updated; `manual_action` is set on both. Rows of machines with a retired override simply rebuild from the sources.

Status precedence on a machine row: `ADDED_MANUALLY` > `EXCLUDED_MANUALLY` > `STOPPED_ALL_SHIFT` (marked and stopped) > `WORKED` > `NO_WORKER_MARKED`. A row is `suggested` when it has a production source, no worker marked and no active override.

### 12.3 API (router of `hands_report.py`, prefix `/api/juteProductionReports`)

- `POST /hands_report_machine_override` — body `{co_id, branch_id, tran_date, shift, machine_ids: number[], action: "ADD"|"EXCLUDE", reason}`. Validates: date not in the future, shift in A/B/C/G, machines are active machines of the branch (same check as the group members), reason 3–255 chars, at most 500 machines. For each machine: retire any active override of that machine-shift, insert the new one; then rebuild that date. Returns `{"data": {"saved": n}}`.
- `POST /hands_report_machine_override_undo` — body `{co_id, branch_id, override_ids: number[]}`; sets `active = 0` (only rows of that co/branch), rebuilds the affected dates. Returns `{"data": {"undone": n}}`.
- `GET /hands_report_machines?co_id&branch_id` — the branch's active machines for the picker: `machine_id, machine, mech_code, machine_type_id, machine_type, dept_id, dept, group_ids, groups`.
- Access: both POSTs need `user_menu_access_level(...)` ≥ 4 on the Hands Report menu path (resolve it from `menu_mst`; the GETs stay at view level); 403 `"This role can view the hands report but not change the machines worked"`.
- Machines view (`view=machines_run`) rows gain `manual_action` (`"ADD"|"EXCLUDE"|null`), `override_id`, `manual_reason`, `manual_by` (user name), `manual_at` (ISO), `suggested` (bool); `status` gains `ADDED_MANUALLY` and `EXCLUDED_MANUALLY`; `meta` gains `added_manually`, `excluded_manually`, `can_edit` (bool, the caller's access). Notes: `"{n} machine-shift(s) were added manually and {m} excluded manually — see the Machines Worked tab."`.

### 12.4 UI (Machines Worked tab)

- Chips "Added manually n", "Excluded n"; Status filter gains the two statuses; grid shows the reason / who / when in the Status cell tooltip and an Excel column "Manual".
- With edit access (`meta.can_edit`, and `hasMenuAccess(pathname, "edit")`): an **Add machines** button → dialog: Date (within the loaded window), Shift, a **Suggested** list for that date and shift (rows with `suggested`, select all) and a searchable multi-select of every branch machine (filters: machine type, group), Reason (required); row actions — **Count** on a suggested / no-worker row (reason pre-filled "Production entered, no worker marked", editable), **Exclude** on a worked row (asks the reason), **Undo** on an added / excluded row. After a save both tabs reload.

### 12.5 Tests

Backend: `test_added_machine_counts_even_without_any_entry`, `test_excluded_machine_does_not_count_though_a_worker_is_marked`, `test_add_beats_stopped_all_shift`, `test_status_precedence_with_overrides`, `test_suggested_flags_production_without_mark_and_no_override`, `test_override_post_needs_edit_access_and_validates`, `test_override_post_retires_the_previous_override_and_rebuilds_the_date`, `test_undo_sets_inactive_and_rebuilds`, `test_rebuild_applies_overrides_after_the_source_insert`, `test_machines_endpoint_lists_branch_machines_with_groups`. Frontend: status labels for the two new statuses, suggestion list for a date/shift, payload builders, action visibility by `can_edit`.
