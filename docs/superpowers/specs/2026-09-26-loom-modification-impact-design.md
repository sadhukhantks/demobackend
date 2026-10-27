# Loom Modification Register + Impact Report (loom-only, case-wise)

Design proposal, 2026-09-26 (revision 2, loom-only). No code yet.

Revision note: revision 1 (same day, morning) was a generic register + report for every
machine type with per-type production readers. The owner chose case-wise reports instead
because the generic setup was getting too complex. This revision is looms only; other
machine types get their own copies later, as separate specs.

> **AMENDED 2026-09-29 — revision 3, after the build (phases 1–2 built 2026-09-28,
> applied on dev3 and sls 2026-09-29). Owner rulings that supersede §6–§7 below:**
> 1. **Loom groups are not re-keyed.** The register's "From group" picker and the loom
>    tick-list offer the groupings that already exist in the data — bands of six
>    consecutive loom numbers (the Low Producer rule, `lowprod_weaving.py:102`, by
>    name prefix) and `machine_mst.mech_shr_code` share codes covering ≥ 2 looms —
>    computed client-side from `/setup` (`mech_shr_code` now returned). Picking one
>    ticks its looms; `loom_group_id` stays NULL. The Loom Groups master is optional.
> 2. **The report is an efficiency comparison only.** Headline = efficiency %
>    (efficiency at the baseline standard when a study loom's standard changed);
>    NO fallback to picks / yds / cuts / kg — short efficiency coverage gives
>    NOT ENOUGH DATA with the cause in the notes. Removed: metric picker, quality
>    mix and quality filters, kg/yds/cuts/picks figures, extra output / cost tiles,
>    waste and hands context, the Q flag. Kept: windows, controls / net vs control,
>    SE rule, flags S/H/W/D/N, day-wise study vs control efficiency with markers,
>    notes. Section F is now **Breakdown & mechanical** only: per study loom
>    before | after mechanical-reason stoppage hours (`jute_prod_stoppage_hours`,
>    `reason_code = 'mechanical'`) and breakdown count / minutes (`jute_mech_breakdown`).
> 3. **"This loom set vs looms not modified"** is shown as an explicit block under the
>    verdict strip (`verdict.comparison`: study vs control before / after / change,
>    difference = net effect, comparison-set description).
> 4. Titles stay free text; adding looms to an existing entry later is done through
>    Edit / "Add looms to this stage" (a "reusable project title" picker was
>    considered and dropped by the owner).
> 5. The report page must not guard its option fetch with a "fetched for this key"
>    ref: the Next dev server runs React Strict Mode and the guard left the picker
>    empty (fixed, regression test in `page.test.tsx`). The register grid mounts
>    its DataGrid only on desktop widths (MUI X zero-width warning on phones).

Everything cited below exists in the repos unless it is marked **NEW**. Row counts quoted
from sls are read-only figures as of 2026-09-26 (active rows unless stated) and are
illustrative, not load-bearing.

## 0. Summary for the owner

Three screens, looms only. (1) Loom Groups master: name the set of looms one weaver runs
(typically six) once and reuse it. (2) Loom Modifications register: enter each change on
the day it was done, against a group or hand-picked looms; later follow-ups, rectifications
and undos chain to the first entry, and the looms touched are frozen per entry. (3) Loom
Modification Impact report: before vs after each stage, skipping the work and settling
days, against similar untouched looms of the same department; verdict IMPROVED / WORSE /
NO CLEAR CHANGE / NOT ENOUGH DATA with the change in % and in units, every caveat printed
on the screen. The register never writes Weaving Standards; a standard speed changed
inside the study is detected and shown, and the verdict is measured against a constant
yardstick. Nothing is built or committed until you say go (§11).

## 1. Problem

In the owner's words (2026-09-26):

> With regards to the jute weaving production, I want another report where I can earmark
> certain looms and the loom groups these looms belong to, so that modifications or
> upgrades can be entered on the date they were done, and then we can study and analyze
> the impact of the change — whether it has increased or decreased production after the
> change, and by how much. At some point the modification may be modified again (only a
> small modification, or a rectification of the previous modification). Even this needs
> to be recorded again, and the impact thereafter also has to be seen.

Loom group, the owner's definition (2026-09-26): the set of looms, typically six, that
**one weaver runs together**, declared by users — not the physical line. Weaver allotments
change over time.

What exists today and why it does not answer this:

| Need | What exists | Gap |
|---|---|---|
| Loom groups = the weaver's unit | `machine_mst.line_no` (physical line); Low Producer `loom_group()` = bands of 6 looms by name tail (`lowprod_weaving.py:102`) | neither is a user-declared weaver unit; lines mix loom models (co 2: each line = 36 Hessian ~150 rpm + 19 S4 ~477 rpm), co 106 parks 213 of 485 looms on line '100' |
| A dated "modification done" entry | `jute_mech_overhaul` (MAJOR/MINOR, one machine per row, `jute_mech_overhaul_spare`) | no group, no follow-up/rectification chain; every save rewrites `jute_mech_machine_attr.last_overhaul_date` / `next_overhaul_due` (`overhaul.py:308-358`); 0 rows on sls |
| Before/after production per loom | `fetch_weaving_day_rows()` (`weaving_lock.py:103`), one call per day, 0.31 s/day on sls; Low Producer capped at 31 days (`lowprod_weaving.py:51`) | 90 days before + 90 after = ~56 s; no range-scoped weaving query exists |
| A standard that changes with the loom | `jute_prod_weaving_target_map` `mcid` / `speed` rows, last-date resolved (`weaving_query.py:905-916`) | a changed standard moves the efficiency yardstick exactly at the cut, so the report must detect it and also measure against a constant yardstick (§7.3) |

## 2. Principles

1. **Case-wise: this is for looms.** Tables, pages and report are loom-named and read
   `jute_prod_weaving_daily` directly. Another kind of machine gets its own copy later
   (own spec, own tables); nothing here is shared with it.
2. **Loom id is the key, groups are optional.** Every loom is in `machine_mst` (no
   `co_id` / `branch_id`; branch via `dept_mst`). The register stores `machine_id`; a
   group is a label on top. One loom with no group is a one-row entry.
3. **Membership is frozen at the event.** Each modification writes the looms it touched
   into its own snapshot table. Editing a group later never rewrites history; the report
   reads only the snapshot.
4. **Honest numbers before pretty numbers.** Ratio-of-sums, never mean-of-percentages;
   efficiency blank (never 0) where no standard exists (`dpr_reports._eff_pct`, :127);
   the settling-in period is excluded; a control set nets out shed-wide swings; a changed
   standard never drives the verdict; a verdict is withheld when the data is thin or the
   change is inside day-to-day noise; every caveat is printed on the screen.
5. **The register reads standards, never writes them** (D6). Standard speeds stay in
   Weaving Standards / the SQC Actual Speed tab; the report detects changes from
   `jute_prod_weaving_target_map`.
6. **House patterns only.** Portal persona (`get_tenant_db`), `{"data": ...}` envelope,
   Pydantic bodies, soft delete, `updated_by` / `updated_date_time` only, no approval
   workflow (none exists in jute production), menus by migration, `SearchableOptionSelect`
   / `SearchableMultiSelect` for every dropdown (never a raw MUI `Select` or `TextField
   select`, even for two options), zod for every form, branch picker only when more than
   one sidebar branch.

## 3. Process design

```
 Masters > Loom Groups (optional)         Jute Production > Loom Modifications
 ┌──────────────────────────┐            ┌────────────────────────────────────────┐
 │ group = weaver's looms   │──pick──▶   │ INITIAL  (date done, what, who, cost)  │
 │ tick-list, name range,   │            │   ↳ FOLLOWUP  (small later change)     │
 │ "Fill from line",        │            │   ↳ RECTIFY   (fixes the previous one) │
 │ "Suggest from attendance"│            │   ↳ REVERSAL  (undone, per loom)       │
 │ purpose STUDY / CONTROL  │            │ looms frozen per event (snapshot)      │
 └──────────────────────────┘            └───────────────┬────────────────────────┘
        or hand-pick looms ────────────▶                 │ "View impact"
                                                         ▼
                     Production Reports > Loom Modification Impact
   BEFORE window │ work days + settle (excluded) │ AFTER-1 │ AFTER-2 (rectification) …
   verdict + stage ladder · study vs control · per loom · day-wise · caveats
   numbers from NEW range weaving query over jute_prod_weaving_daily
```

Flow: (1) optionally define a group; (2) on the day a change is done, enter it against a
group or a picked list of looms; (3) later small changes / rectifications are entered from
the original row's "Add follow-up" action and chain to it; (4) the report picks the chain
and shows before / after each stage, against untouched looms of the same department.

## 4. Data model

House DDL style of `dbqueries/migrations/create_weaving_tables.sql:19-62` (parent:
`co_id NOT NULL`, `branch_id NULL`, `active TINYINT DEFAULT 1`, `updated_by`,
`updated_date_time TIMESTAMP ... ON UPDATE`; child: own PK + parent FK + `active` only,
no audit). Event header style of `jute_mech_overhaul`
(`create_jute_mech_maintenance_tables.sql:192-217`). All four tables are **NEW**.

Migration: `dbqueries/migrations/<YYYYMMDD>_loom_modification_tables.sql` (**NEW**; tables
+ rollback comment + verification block; dev3 first via `run-migration`, sls only after
the owner names the DB). `<YYYYMMDD>` is the date the file is created, never a planned
date. Every SQL migration of this feature follows the runner note of
`20260926_purchased_yarn_ledger_menu.sql:22-26`: the `run-migration` skill splits on
semicolons and skips chunks that start with a comment marker, so every comment block ends
with a bare `;` line, contains no semicolon of its own, and the verification queries are
semicolon-free inside comments.

**What is a loom.** An active `machine_mst` row whose type name is
`WEAVING_MACHINE_TYPE_NAME` (`constants.py:152`), compared inside SQL where the
`utf8mb4_0900_ai_ci` collation is case-insensitive (`weaving_query.py:577`,
`constants.py:300-302`) — dev3 `LOOM` (id 6) and sls `Loom` (id 7) both match. All 1,304
active sls looms are under that one name (no active machines under `Sacking Loom`,
`S4 Loom`, `Leno Loom` or `Weaving`). Every loom list in this feature is built that way;
a non-loom `machine_id` in any body is a 400 ("machine X is not a loom").

### 4.1 `jute_prod_loom_group` — NEW (the owner's "loom group")

Holds the *current* membership; every modification freezes the looms it touched in its
own snapshot (§4.4). The Low Producer's fixed bands of six by name tail
(`lowprod_weaving.py:102`) are the legacy stand-in; the master replaces them. Optional
(D2 b/c): the master can *propose* groups from the weaver-on-loom attendance marks
(`daily_ebmc_attendance` joined to the `daily_attendance` header — the same source the
weaving day-slice uses for `working_hours`) over the last N weeks, for the user to
confirm. The report shows the group's total output per day, which is the weaver's own
figure.

| Column | Type | Notes |
|---|---|---|
| `loom_group_id` | INT PK AUTO_INCREMENT | |
| `co_id` | INT NOT NULL | |
| `branch_id` | INT NULL | derived from the members' `dept_mst.branch_id`; all members must agree, and that branch must satisfy `branch_mst.co_id = :co_id` (400 otherwise — `machine_mst` has no `co_id`, and sls holds co 2 and co 106 in one DB) |
| `group_code` | VARCHAR(30) NOT NULL | dup guard in code on (`co_id`, `group_code`) among `active = 1`, `weaving_query.py:157` shape |
| `group_name` | VARCHAR(100) NOT NULL | |
| `group_purpose` | VARCHAR(10) NOT NULL DEFAULT 'STUDY' | `STUDY` \| `CONTROL` (a saved control set) |
| `remarks` | VARCHAR(500) NULL | |
| `active`, `updated_by`, `updated_date_time` | | house audit |

Keys: `KEY idx_lg_co (co_id, active)`, `KEY idx_lg_co_branch (co_id, branch_id)`.

### 4.2 `jute_prod_loom_group_dtl` — NEW (current members)

| Column | Type | Notes |
|---|---|---|
| `loom_group_dtl_id` | INT PK | |
| `loom_group_id` | INT NOT NULL FK → `jute_prod_loom_group` | |
| `machine_id` | INT NOT NULL FK → `machine_mst` | must be a loom; must be active when **newly added** — an existing member retired later is kept and labelled "inactive" in the UI (§6.1) |
| `active` | TINYINT NOT NULL DEFAULT 1 | replaced by soft-delete + reinsert on edit (`weaving_masters.py:400-425`) |

Keys: `KEY idx_lgd_group (loom_group_id, active)`, `KEY idx_lgd_machine (machine_id)`.
Not dated — history lives on the event snapshot (§4.4).

### 4.3 `jute_prod_loom_modification` — NEW (the dated event register)

| Column | Type | Notes |
|---|---|---|
| `modification_id` | INT PK | |
| `co_id` | INT NOT NULL | |
| `branch_id` | INT NULL | derived from the first snapshot loom → `dept_mst.branch_id` (`overhaul.py:391` pattern); looms spanning branches, or a branch not of `co_id` → 400 |
| `loom_group_id` | INT NULL FK | set when the looms were taken from a group; NULL = hand-picked list (a single loom is a one-row list) |
| `mod_date` | DATE NOT NULL | the day the change was done / started |
| `mod_to_date` | DATE NULL | last day of the work when it spanned days; NULL = same day |
| `settle_days` | INT NOT NULL DEFAULT 3 | settling-in days after the work, excluded from analysis (report can override) |
| `mod_kind` | VARCHAR(10) NOT NULL DEFAULT 'INITIAL' | `INITIAL` \| `FOLLOWUP` \| `RECTIFY` \| `REVERSAL` |
| `parent_modification_id` | INT NULL FK → self | the event this one follows / rectifies / undoes; NULL for INITIAL. Self-FK precedent: `cost_element_mst.parent_element_id` (`create_bom_costing_tables.sql:57,75`) |
| `root_modification_id` | INT NULL | stamped at save = parent's root, else own id; `WHERE root_modification_id = :root` fetches a chain; rewritten only by "Insert earlier stage" (§5) |
| `mod_category` | VARCHAR(20) NOT NULL | enum `MOD_CATEGORIES` in `src/juteProduction/constants.py` (**NEW** constants): `MECHANICAL`, `ELECTRICAL`, `SPEED_CHANGE`, `ACCESSORY`, `SETTING`, `OTHER`; shipped as `{code,label}` (D13) |
| `title` | VARCHAR(150) NOT NULL | e.g. "Beat-up cam replaced" |
| `description` | TEXT NULL | what exactly was changed |
| `expected_effect` | VARCHAR(300) NULL | the hypothesis, e.g. "+5% efficiency" |
| `done_by` | VARCHAR(10) NOT NULL DEFAULT 'INHOUSE' | `INHOUSE` \| `VENDOR` (same codes as `juteMechanical/constants.py:83-87`; re-declared in juteProduction constants to avoid a cross-module import) |
| `party_id` | INT NULL | required when `VENDOR` |
| `total_cost` | DOUBLE NULL | house rule for amounts (`Double`), same as `jute_mech_overhaul.total_cost` (`create_jute_mech_maintenance_tables.sql:205-207`) |
| `remarks` | VARCHAR(500) NULL | |
| `active`, `updated_by`, `updated_date_time` | | house audit |

Keys: `KEY idx_lm_co_branch_date (co_id, branch_id, mod_date)`,
`KEY idx_lm_group_date (loom_group_id, mod_date)`,
`KEY idx_lm_root (root_modification_id, mod_date)`, `KEY idx_lm_parent (parent_modification_id)`.

Phase 3 (D17, §7.4) adds `last_verdict VARCHAR(20) NULL`, `last_verdict_pct DECIMAL(8,2) NULL`,
`last_verdict_at TIMESTAMP NULL` by a separate ALTER migration; not in the phase-1 DDL.

### 4.4 `jute_prod_loom_modification_mc` — NEW (frozen membership per event)

| Column | Type | Notes |
|---|---|---|
| `modification_mc_id` | INT PK | |
| `modification_id` | INT NOT NULL FK → `jute_prod_loom_modification` | |
| `machine_id` | INT NOT NULL FK → `machine_mst` | one row per (event, loom) among `active = 1`, enforced in code |
| `done_date` | DATE NULL | per-loom start date when a change rolled across a group over several days; NULL = header `mod_date`. Validation: `mod_date <= done_date <= COALESCE(mod_to_date, mod_date)` (400 "done date of loom X is outside the event's dates — extend 'Completed on'") |
| `done_to_date` | DATE NULL | per-loom end of the work; NULL = `done_date` when set, else header `COALESCE(mod_to_date, mod_date)`. Validation: `done_date <= done_to_date <= COALESCE(mod_to_date, mod_date)` |
| `remarks` | VARCHAR(200) NULL | |
| `active` | TINYINT NOT NULL DEFAULT 1 | replaced by soft-delete + reinsert on edit (`overhaul.py:425-434` pattern) |

Keys: `KEY idx_lmm_mod (modification_id, active)`, `KEY idx_lmm_machine_date (machine_id, done_date)`.

No standard-speed columns: the standard in force on any date is resolved from
`jute_prod_weaving_target_map` when the report runs (§7.3), so nothing is copied at save.

### 4.5 Indexes for the range read — NEW, phase 2

SHOW INDEX on `jute_prod_weaving_daily` (dev3 and sls, 2026-09-26) lists eight indexes:
`PRIMARY`, `uq_weaving_daily_unit_machine (co_id, tran_date, spell_id, machine_id, active)`,
`idx_wd_co_branch_date (co_id, branch_id, tran_date)`,
`idx_wd_key (co_id, tran_date, spell_id, machine_id, weaving_quality_id)`,
`fk_wd_machine (machine_id)`, `fk_wd_spell`, `fk_wd_quality`,
`idx_wd_jugar_chain (co_id, machine_id, weaving_quality_id, tran_date, weaving_daily_id)`.
None has `machine_id` and `tran_date` adjacent. Unhinted, the optimizer ranges on
`uq_weaving_daily_unit_machine` (or `idx_wd_co_branch_date` once the branch predicate is
added): `co_id` + `tran_date` leading, every loom of the company read for the span and
`machine_id` filtered afterwards (EXPLAIN on sls, 30 co-2 looms × 117 days: type=range,
~19k rows). With `FORCE INDEX (idx_wd_jugar_chain)` the scan walks each study loom's
qualities and filters on date. Measured on sls (§9): the unhinted base scan costs ~3.5 s
against ~0.6 s hinted for 40 looms × 270 days — a real but secondary cost; the derived
tables dominate (§9).

Migration `dbqueries/migrations/<YYYYMMDD>_weaving_daily_machine_date_index.sql` (**NEW**,
both indexes in one file; runner note and date-prefix rule of §4 apply), online DDL,
`DROP INDEX` rollback comments, dev3 first, sls off-shift with the owner confirming the DB
name (D11):

- `ALTER TABLE jute_prod_weaving_daily ADD INDEX idx_wd_co_machine_date (co_id, machine_id, tran_date), ALGORITHM=INPLACE, LOCK=NONE;`
  — removes the FORCE INDEX and the ~3 s base-scan penalty (2.24M rows on sls, 207 MB data / 311 MB index).
- `ALTER TABLE daily_ebmc_attendance ADD INDEX idx_dea_mc_atten_active (mc_id, daily_atten_id, is_active), ALGORITHM=INPLACE, LOCK=NONE;`
  — today the table (1.06M rows on sls) has `PRIMARY(dtl_rec_id)`, `daily_ebmc_unique(attendace_date, branch_id, daily_atten_id, eb_id, eb_no, is_active, mc_id, spell, created_date_time)`, `FK_DAILYATTENID_idx(daily_atten_id)`, `idx_dea_atten_active_mc(daily_atten_id, is_active, mc_id)` — nothing leads with `mc_id`, so the weaver-hours derived table (`att`, §9) must scan the whole tenant's attendance for the span (3.7 s of a 7.2 s statement for 40 looms). With this index the `att` leg starts from the study/control loom list and joins back to `daily_attendance` by PK.

Note the combined online-DDL time on the 2.2M / 1.06M-row pair when scheduling on sls.

### 4.6 ORM

`src/juteProduction/loom_modification_models.py` (**NEW**): `JuteProdLoomGroup`,
`JuteProdLoomGroupDtl`, `JuteProdLoomModification`, `JuteProdLoomModificationMc` in the
SQLAlchemy 2.0 `Mapped` style of `src/juteProduction/bale_models.py` (also
`waste_models.py`, `planning_models.py`); `total_cost` as `Double`. Not
`weaving_models.py`, which is legacy `Column` style by its own docstring (:3-4), and whose
`JuteProdWeavingTargetMap` docstring (:101-103, "there are NO loom (mcid) standards") is
stale — 476 active `mcid`/`standard`/`speed` rows exist on sls (`weaving_target_map.py:5-11`
is current). Do not trust that docstring when resolving standards for the report.

## 5. Event model

**Chain = a linear linked list on one table.** The INITIAL row (parent NULL, root = self)
is "the modification". A later small change is a `FOLLOWUP` row whose
`parent_modification_id` is the row it builds on; a rectification of the previous
modification is a `RECTIFY` row with the same link; undoing a stage is a `REVERSAL` row
(per loom, below). Chains are **linear**: a FOLLOWUP / RECTIFY's parent must be the
chain's latest active event of kind ≠ REVERSAL (400 "the latest stage of this chain is
<title, date> — follow that one"), so "stage k of n", "Δ vs previous stage" and the stage
ladder never flatten a branch. A REVERSAL's parent may be any earlier non-REVERSAL stage
of the chain — it names the stage being undone. `root_modification_id` is stamped at save.

**REVERSAL is per loom.** A REVERSAL row carries its own snapshot (default: the parent's
looms; the user unticks the ones not reverted — 2 of 12 looms is a normal case). It closes
the timeline only for the looms in its snapshot: those looms accept no later stage in this
chain; the other looms continue (a later FOLLOWUP on the remaining 10 looms is allowed).
"Open" (for the Follows picker) = active, kind ≠ REVERSAL, and at least one snapshot loom
not yet reverted. A chain is closed only when the union of its reversal snapshots covers
the root snapshot. What a REVERSAL undoes: its parent **and every later stage** on those
looms — parent = INITIAL → the looms are back to original (segment "reverted to original",
compared to baseline); parent = FOLLOWUP / RECTIFY → only the tweak is undone (segment
"tweak undone", compared to the segment before that parent).

**Ordering and membership rules (400s, mirrored in the zod schema):**

- parent must be active, same `co_id`, and (FOLLOWUP / RECTIFY) the chain's latest
  non-REVERSAL event; a REVERSAL's snapshot must be a subset of its parent's snapshot;
- `mod_date` > parent's `COALESCE(mod_to_date, mod_date)` — no same-day or overlapping
  stages, no zero-length segments;
- per loom: `D_m = COALESCE(done_date, mod_date)`, `E_m = COALESCE(done_to_date, done_date, mod_to_date, mod_date)`;
  `done_date` / `done_to_date` inside `[mod_date, COALESCE(mod_to_date, mod_date)]`;
  the per-loom span `[D_m, E_m]` may not overlap the same loom's `[D_m, E_m]` on another
  active event in **any** chain (the overlap check runs on the per-loom pairs, not on the
  header);
- every snapshot row is a loom (§4); all snapshot looms are in one branch and that branch
  belongs to `co_id` (the group path inherits both from the group);
- the "must be active" check applies to **newly ticked** looms only: snapshot rows of a
  loom retired since the event are kept and shown as "inactive" in the checklist, so
  editing an old event never 400s because a loom was scrapped;
- `mod_date` and every `done_date` ≤ today (400 "date done cannot be in the future");
- `mod_to_date >= mod_date`; `party_id` required when `VENDOR`;
- `parent_modification_id` and `mod_kind` are **immutable once the row has active
  children** (400 "delete the follow-ups first"); no re-stamping of descendants, with one
  exception: **Insert earlier stage** — a root row (parent NULL) may be re-parented to a
  new, earlier INITIAL (a change done four months ago that was never entered): the new
  row is saved as INITIAL, then one `UPDATE jute_prod_loom_modification SET root_modification_id = :new WHERE root_modification_id = :old`
  plus `parent_modification_id = :new, mod_kind = 'FOLLOWUP'` on the old root (the user
  picks FOLLOWUP or RECTIFY). Requires the new stage's `COALESCE(mod_to_date, mod_date)` <
  the old root's `mod_date` (400 "the earlier stage must end before <old root date>"); its
  snapshot is free (any looms of the branch) — old-root looms not in it are labelled "not
  in previous stage" from then on. Available as a row action on root rows only;
- Delete is refused while children exist (button disabled, tooltip "has n follow-ups");
  the root row offers **Delete chain** (one `window.confirm` listing the stages, deletes
  children first in one transaction).

**Scope: group or picked looms.** `loom_group_id` set = the members were taken from a
group (the form pre-ticks the group's current active members; the user may untick some,
e.g. a rectification done on 5 of 30 looms). `loom_group_id` NULL = a hand-picked list;
one loom is a one-row list. There is no scope enum. The form offers "Save these looms as
a group" so a hand-picked list becomes a group without re-entry.

**Membership snapshot.** On create the ticked looms are written to
`jute_prod_loom_modification_mc` with optional per-loom `done_date` / `done_to_date` (a
change rolled across a group on 1 Sep and 8 Sep is one event, two dates). On edit the
snapshot is soft-deleted and reinserted. A follow-up's checklist = **union(parent
snapshot, current group members when the parent came from a group)**: parent-snapshot
looms ticked, the rest unticked and labelled "not in previous stage" — so a loom added to
the group after the first change can join a later stage; a loom in the parent snapshot
that is no longer a group member stays pre-ticked and is labelled "no longer in group"
(the user may untick it); a retired loom is labelled "inactive" and never blocks the save.
Every event also has a row action **Add looms to this stage (done on …)** that appends
snapshot rows with their own `done_date` (validated by the rule above), for the 13th loom
modified weeks after the first twelve. Per-stage loom counts are what the chain header
prints ("stage 2 of 3 — 5 of 12 looms"). Editing the group master later never touches
past events; the register labels the list "members as recorded".

**Standard speed: read, never written (D6).** A speed change done with the modification
is still entered in Weaving Standards (or the SQC Actual Speed tab) as today. The register
records the change only as `mod_category = SPEED_CHANGE` plus the title. The report finds
every `mcid`/`speed` row of either role effective inside the span and raises the `S` flag
and the section-D markers from it; the verdict uses the constant-yardstick efficiency
(§7.3) and picks per working hour, which needs no speed standard at all.

**Segments per loom (what the report compares).** For each loom, its snapshot rows across
**all** chains sorted by `D_m` give its timeline:

```
 ... BEFORE ... │ work + settle (excluded) │ AFTER-1 ... │ work + settle │ AFTER-2 ...
   D1 - W .. D1-1   D1 .. E1 + S              E1+S+1 .. D2-1
```

`D_m,k` / `E_m,k` = the loom's start / end date for stage k (rule above), `S` = settle
days, `W` = window days. The `after` of a stage is cut at the loom's next event (any
chain) minus one, at `W` days, and at the last date that has an active production row for
the branch. Only the INITIAL stage has a `before` (the **baseline**); stages ≥ 2 are
compared to the baseline of the same looms and to the previous stage's `after` (§7.3). A
REVERSAL starts a segment "reverted to original" or "tweak undone" as defined above.

**Independence.** Nothing writes to `jute_prod_stoppage_hours`, `jute_prod_weaving_target_map`
or the Jute Mechanical tables (their spec locks "neither writes to the other"). Work days
are excluded by date arithmetic in the report (D8). Phase 3 lists modification events
read-only on the Machine History Card.

## 6. Entry pages

### 6.1 Loom Groups master — `/dashboardportal/juteProduction/masters/loomGroupMaster` (NEW)

Clones `src/app/dashboardportal/juteProduction/masters/weavingQualityMaster/page.tsx`
(single-file page: DataGrid + MUI Dialog, `useSelectedCompanyCoId`, `fetchWithCookie`) —
but **not** its hand validation: that page has no zod schema (`type FormState`, :50), so
the clone adds `types/loomGroupTypes.ts` with a `loomGroupSchema` (zod + `superRefine`:
code / name non-blank, ≥ 1 member, one branch) and `loomGroupTypes.test.ts`, following the
overhaul types pattern. The Dialog is `fullScreen` under `sm` (the precedent's
`maxWidth="md"` dialog at :391 is unusable for a 40-row tick-list on a phone).

Fields: Branch (auto when one sidebar branch, `SearchableOptionSelect` when several),
Group code, Group name, Purpose (Study / Control, `SearchableOptionSelect`), Remarks,
**Members** = a **tick-list** (the register's `LoomChecklist`, §6.2), not chips: rows
`mech_code — machine_name (line N)` from the active looms of the branch, "Select all /
none", a **name-range pick** ("from S101 to S112", by the numeric tail of
`machine_name`), **"Fill from line"** (`SearchableOptionSelect` of distinct
`CAST(machine_mst.line_no AS CHAR)` for the branch — `line_no` is INT on dev3 and
VARCHAR(10) on sls, so both sides compare as strings), optional **"Suggest from
attendance"** (D2 b/c: groups proposed from the last N weeks of weaver-on-loom marks, the
user confirms), and a count badge "Selected 12 of 40". `SearchableMultiSelect`
(`src/components/ui/SearchableSelect.tsx:258`, usage precedent
`juteProduction/rollStockReports/page.tsx:193`) remains the control for the small loom
pickers in the report filters, not for group membership.

Validations (backend 400, zod superRefine): code and name non-blank; dup code; at least
one member; every member a loom; **newly ticked** members active — an existing member
retired since is kept, shown as "inactive" in the tick-list and never makes an edit 400;
members in one branch, branch of `co_id`. Delete refused while an active event references
the group (guarded delete, `juteMechanical/masters.py:620-627` pattern).

Menu: `menu_mst` row "Loom Groups", `report` NULL, parent = "Jute Production" hub (dev3
768), path `juteProduction/masters/loomGroupMaster`, icon `group_work`, `order_by` after
"Weaving Standards" (204) — `seed_weaving_menu.sql:20-34` shape.

### 6.2 Loom Modification register — `/dashboardportal/juteProduction/loomModification` (NEW)

Clones the folder `src/app/dashboardportal/juteMechanical/overhaul/`:

| Overhaul file | New file |
|---|---|
| `page.tsx` (smart container, branch resolution :51-77, resetToken edit flow :105-120) | `loomModification/page.tsx` |
| `_components/OverhaulEntryForm.tsx` (RHF + zod, `Controller` + `SearchableOptionSelect`) | `_components/ModificationEntryForm.tsx` |
| `_components/OverhaulSpareLinesEditor.tsx` (child lines) | `_components/LoomChecklist.tsx` (tick list: select all/none, name range, per-row done date / done-to date, labels "not in previous stage", "no longer in group" (pre-ticked, may be unticked) and "inactive"; shared with the groups master) |
| `_components/OverhaulGrid.tsx` (DataGrid + CSS mobile cards, `DESKTOP_ONLY_SX` / `MOBILE_ONLY_SX`) | `_components/ModificationGrid.tsx` |
| `hooks/useOverhaulSetup.ts`, `hooks/useOverhaulTable.ts` | `hooks/useModificationSetup.ts`, `hooks/useModificationTable.ts` |
| `types/overhaulTypes.ts` (+ `breakdown/types/breakdownTypes.test.ts` as the test model) | `types/modificationTypes.ts` + `modificationTypes.test.ts` |

Service: `src/utils/loomModificationService.ts` (**NEW**) copying `buildQuery` /
`requestData` / `requestList` from `src/utils/juteMechanicalService.ts:139-177`; route
constants `LOOM_MODIFICATION_*` and `LOOM_GROUP_*` in `src/utils/api.ts` beside
`WEAVING_QUALITY_*` (:1399). The path has no `/create` or `/edit` segment
(`portalPermissions.ts:12-13` would raise the access level).

Form (entry above, register grid below). Every choice field is a `SearchableOptionSelect`
(no raw MUI `Select`, no `TextField select`, even for two options):

| Field | Control | Rule |
|---|---|---|
| Branch | auto / `SearchableOptionSelect` | exactly one branch, like overhaul |
| From group / Looms | toggle; group picker (`SearchableOptionSelect`, STUDY groups of the branch) **or** the tick-list | at least one loom after ticking; "Save these looms as a group" |
| Members | `LoomChecklist` — all ticked; optional Done date / Done-to date per row | untick looms not touched; dates inside the event's dates |
| Kind | `SearchableOptionSelect` Initial / Follow-up / Rectification / Reversal | Initial when no "Follows" |
| Follows | `SearchableOptionSelect` of open events of the branch (latest stage per chain; any non-reversal stage when Kind = Reversal); prefills members from its snapshot ∪ group members | required unless Initial |
| Date done, Completed on, Settling days (default 3) | date, date, number | ordering rules of §5; Date done and every per-loom done date ≤ today (400 "date done cannot be in the future") |
| Category, Title, What was changed, Expected effect | `SearchableOptionSelect` / text | title required; Category "Speed change" shows the hint "enter the new speed in Weaving Standards — the report detects it" |
| Done by, Party, Cost | `SearchableOptionSelect` / party picker / number | party when Vendor |
| Remarks | text | |

Save feedback (phase 1): the create/edit response carries `data_check` = producing
loom-days found in `jute_prod_weaving_daily` in the 90 days before and after `mod_date`
for the ticked looms (one COUNT per save), and the snackbar prints it ("saved — 0
producing loom-days in the 90 days before: the report will say NOT ENOUGH DATA"), so an
entry dated inside the sls data holes is visible at entry time.

Row actions: Edit (inline above grid), Delete (disabled with tooltip while children
exist; `window.confirm`, soft), **Delete chain** (root rows), **Insert earlier stage**
(root rows, §5), **Add looms to this stage**, **Add follow-up** (opens the form
prefilled: Follows = this row, Kind = Follow-up, members = its snapshot ∪ group members,
date = today), **View impact** (deep link `report/loomModificationImpact?modification_id=<id>`;
any stage id resolves to its root chain, §7.1; the action is **hidden until the report menu
row exists** — phase 2 — so phase 1 ships no dead link).
Grid columns: date, kind, title, scope ("Group EJM-L4 — 12 looms" / "5 looms picked"),
stage `k of n` with per-stage loom count, category, done by, cost, **data check** chip
(phase 2, same figure as the save feedback, red when a side is 0), and from phase 3
latest verdict · Δ % (§7.4).

Menu: "Loom Modifications", `report` NULL, parent "Jute Production", path
`juteProduction/loomModification`, icon `build`, `order_by` after Loom Groups.

## 7. Report — `/dashboardportal/juteProduction/report/loomModificationImpact` (NEW)

Skeleton: `report/weavingLowProducer/page.tsx` (Suspense + `useReportBranches` +
`useMounted`, filter Paper, meta strip, Tabs rendered conditionally, `TabGrid` with
per-tab Excel export) with `SearchableOptionSelect` filters as `report/handsReport`
(never the low-producer `TextField select`), branch norm and `page.test.tsx` harness from
`report/purchasedYarnLedger`, `KpiCard` from `report/dailyProductionReport/_components/DprPieces.tsx:43`,
phone cards from `report/doffAnalysis/_components/FrameCards.tsx`, Excel / PDF / Print
trio from `report/doffAnalysis/_components/ExportActions.tsx`, colours from
`src/styles/tokens.ts` (`tokens.report.header`). `_service.ts` + `_service.test.ts` clone
`weavingLowProducer/_service.ts`; route constants `JP_LOOM_MODIFICATION_IMPACT` and
`JP_LOOM_MODIFICATION_IMPACT_OPTIONS` beside `JP_HANDS_REPORT` (`api.ts:1128`).

Menu: "Loom Modification Impact", `report = 1`, under the `juteProduction/report` hub
(dev3 945), migration cloned from `20260926_purchased_yarn_ledger_menu.sql` (hub resolved
by path, `role_menu_map` mirrored, idempotent, users re-login).

### 7.1 Filters

| Filter | Control | Default |
|---|---|---|
| Branch | text when one sidebar branch, else `SearchableOptionSelect` | sidebar / hub `?branch_id` |
| Modification (chain) | `SearchableOptionSelect` of root events: "dd-MM-yy · title · Group X / n looms" | from `?modification_id` — any stage id (a follow-up's, a reversal's) resolves to its root chain via `root_modification_id` |
| Window days per side | `SearchableOptionSelect` 30 / 60 / 90 / 180, with **30** and **60** as one-tap presets | 60 unless the owner says 30 (D4) |
| Settling days | `SearchableOptionSelect` 0 / 3 / 7 / event value | the event's `settle_days` |
| Headline metric | `SearchableOptionSelect`: Auto, Efficiency %, Picks per working hour, Yds per working hour, Yds per available hour, Cuts per loom-day, Kg per loom-day | Auto (D3) |
| Control | `SearchableOptionSelect`: Same dept + same std-speed band, not modified (default) / Same dept, not modified / a CONTROL group / None | speed band (D5) |
| Same quality only | checkbox (kg and yds modes) | off |
| Quality | `SearchableOptionSelect` All / one `weaving_quality_id` | All |

No shift filter: per-spell comparison is never offered (spell model changed from five
legacy spells to A/B/C on 2026-08-17 for co 2; co 106 A2/B2 overlap A1/B1). Hour-based
figures use `working_hours` or `available_hours` only.

### 7.2 Sections

**A. Chain header.** One row per stage: seq, kind, date(s), title, looms ("5 of 12"),
done by, cost, control used ("43 un-modified Hessian looms, line 4" or the group name).
Coverage line per side: calendar days, **producing days**, **days with measurable
efficiency**, producing loom-days, std / hours / picks coverage %, looms with thin data.
Window line: "Before 01-Jul..29-Aug (60 d) | Excluded 30-Aug..04-Sep | After
05-Sep..25-Sep (21 d, cut by last data date)". Data source line (D15).

**B. Verdict strip (KpiCard tiles) + stage ladder.** Sub-line first: "since 31-Aug-26 ·
Rectification: small tweak (stage 2 of 2, 5 of 12 looms)". Tiles: headline metric Before
| After (the stage named) | Change (points and %) | Net vs control; the **per available
hour / per loom-day** figure Before | After (yds per available hour, plus stoppage hours
per loom-day); kg per loom-day Before | After (labelled quality-dependent); **extra
output over the after window** = Δ per loom-day × after loom-days (kg and yds) beside
**cost entered** (Σ `total_cost` over the chain) — value and payback are out of scope: no
rate per kg or yard exists in the system, so the report never prices the output; verdict
chip **IMPROVED / WORSE / NO CLEAR CHANGE / NOT ENOUGH DATA**; sub-line "k of m looms
improved; coverage before x %, after y %"; "within day-to-day variation" tag when it
applies (§7.3). The strip shows the **latest stage that meets the minimum**; when the
latest stage is too thin it says "latest stage (31-Aug) has 4 days — verdict shown for
stage 1". When the latest stage covers only some looms, a second line reports the rest
("25 looms still on After-1: Δ …"); reverted looms are excluded and named. Below the
strip the **stage ladder**: one compact card per segment — Before, After-1 (Initial),
After-2 (Rectification)…, reverted / tweak-undone segments, with its shaded control
figures — showing dates, n of N looms, loom-days, producing days, hours run, available
hours, yds, kg, cuts, picks, coverage %, efficiency %, efficiency at baseline std %,
headline before → after, **Δ vs baseline** (points and %), **Δ vs previous**, net vs
control, SE, verdict chip. Rendered as `FrameCards` under `sm` and as a ReportGrid (one
row per segment, control row shaded, `getRowClass` `report-perf-low` when Δ% ≤ −threshold,
`report-perf-watch` when thin) on desktop — this is the phone answer to "did it work, by
how much, since which change".

**C. Per loom (ReportGrid; FrameCards under `sm`).** Loom, line, speed band (baseline
std), since date, producing days before | after, headline before | after | Δ %, kg/day
before | after, eff % before | after (blank when unmeasured; "eff vs baseline std" shown
beside it when the standard changed), dominant quality before → after, standard speed at
baseline start → latest, **flags**: `S` effective standard (actual or standard role)
changed inside a window, `Q` dominant quality share moved ≥ 20 pts or quality sets
disjoint, `H` hours coverage moved ≥ 20 pts between windows, `W` dominant weaver (`eb_id`)
share moved ≥ 20 pts or weaver sets disjoint, `D` coverage < 50 %, `N` no standard, `—`
for stages the loom was not in. Sorted by Δ % ascending (worst first). Segment selector:
Baseline vs latest (default) or any stage. Group TOTAL and CONTROL TOTAL rows
(`isTotalRow`).

**D. Day-wise trend (ReportGrid).** Date, phase (Before / Excluded / After-k), study
looms run, study metric, study 7-day rolling, control metric, control 7-day rolling, gap,
**markers** (event dates and every `mcid`/`speed` effective date of either role for
member looms inside the span, from `jute_prod_weaving_target_map`: "STD SPEED 148 → 160
on 3 looms", "ACT SPEED set on 2 looms"). Excluded rows greyed. No chart in phase 2 (no
chart library in vowerp3ui); a weekly SVG bar strip is phase 3 (D10).

**E. Quality mix (small grid per segment).** Quality, loom-days, share %, kg, yds, and
"kg per loom-day at before quality mix" — explains a kg move that is only a quality
switch.

**F. Downtime, breakdown, waste, hands.** Per loom before | after: stoppage hours by
reason (`jute_prod_stoppage_hours`), breakdown count and minutes (`jute_mech_breakdown`,
failures only), waste kg (`jute_prod_waste_entry`), hands (`daily_ebmc_attendance` marks)
— built from the `hands_report_query.py:137` `_leg()` legs (:196-254) restricted to
`machine_id IN (:looms)` and the same span. The hands leg uses the
`daily_ebmc_attendance (mc_id, …)` index of §4.5; until it exists the leg filters through
`daily_atten_id` after the date range.

**G. Notes (Alert info, always rendered, server-generated `meta.notes`).** Data source
(D15); the cause of every coverage gap, separately: "n loom-days produced with no weaver
marked", "m days with no SQC pick reading", "k looms with no standard" (on sls the
efficiency gaps come mainly from missing weaver marks and missing daily pick readings,
not from standards — co 106 line 7 had 735 produced-but-unmanned loom-spell rows in 50
days); which headline was chosen and why; standard changed inside a window (either role)
and which baseline speed the constant yardstick uses per loom; quality mix changed on n
looms; weaver changed on k looms; data hole (≥ 7 consecutive calendar days with zero
rows for the branch); short after window; before window extended to N days to reach the
minimum; baseline includes n loom-days after another chain's event; no eligible controls
/ control standard changed on n looms / control-group members dropped and why; spell
model differs between windows; n looms returned no weaving rows; that a manned loom with
no output counts as 0 % efficiency here (unlike the Low Producer shed average);
thresholds in force (`meta.thresholds`, incl. n1, n2, s_p, SE).

### 7.3 Method and formulas

**Loom-day rows** come from `weaving_range_loomday_sql` (§9), built from spell rows with
the slice's own formulas (`weaving_query.py:833-838`:
`production_yds = jugar × finished_length / no_of_jugar_per_cut`,
`std_prod_yds = eff_speed × working_hours × 60 / (36 × std_picks)`; :885-888
`working_hours = GREATEST(0, LEAST(spell hours, weaver attendance hours) − stoppage)`;
:853 `eff_speed` = actual-role speed if > 0 else standard), aggregated per
(`machine_id`, `tran_date`):

- `cuts`, `production_yds`, `production_kg`;
- `hours_run = Σ working_hours`; `available_hours = Σ spell_mst.working_hours` of the
  entered spell rows (before attendance / stoppage deductions); `stoppage_hours`;
- `actual_measured = Σ production_yds` and `capacity_measured = Σ std_prod_yds` over
  spell rows with `std_prod_yds > 0` (both NULL when none);
- `cap_per_rpm = Σ working_hours × 60 / (36 × std_picks)` over rows with `std_picks > 0`
  — capacity per unit of speed, so the constant yardstick below is a multiplication in
  Python and the SQL needs no per-loom speed parameter;
- `picks_delivered = Σ production_yds × 36 × std_picks` over rows with `std_picks > 0`
  only (a missing exact-day SQC reading gives `std_picks = 0`, `weaving_query.py:849, 878`,
  and must not count as zero picks); `picks_hours = Σ working_hours` over those rows;
- `has_standard`, `std_picks_present`; quality rows kept at spell-row grain
  (`weaving_quality_id`, yds, kg) for the quality filter; dominant quality; the set of
  `eb_id` on the day; `std_speed_resolved` (effective, either role).

**Baseline standard speed (constant yardstick).** For each loom m,
`baseline_speed_m` = the `mcid`/`speed` value in force on the **first day of the before
window** (after any extension), resolved by the slice's own rule: the `actual` role if an
active row is effective on that date, else the `standard` role; last effective date,
newest id (`weaving_query.py:853, 905-916`). Resolved from `jute_prod_weaving_target_map`
in Python at report time (the ~500 active rows are fetched once for the markers and the
`S` flag anyway). When no standard is in force on that day, the earliest standard
effective inside the before window is used and the note says so; when none exists at all
the loom carries `N` and the constant-yardstick efficiency is blank for it.

**Windows** (per loom m and stage k; `S` = settle days, `W` = window days):

- `D_m,k = COALESCE(done_date, mod_date)`; `E_m,k = COALESCE(done_to_date, done_date, mod_to_date, mod_date)`;
- `excluded_m,k = [D_m,k, E_m,k + S]`;
- `before_m = [D_m,1 − W, D_m,1 − 1]` — the **INITIAL stage only**; stages ≥ 2 have no
  before window of their own;
- `after_m,k = [E_m,k + S + 1, min(E_m,k + S + W, D_m,k+1 − 1, last data date)]` where
  `D_m,k+1` is the loom's next event in any chain and *last data date* is the last date
  with an active production row for the branch (synced spells for today exist, so never
  "yesterday");
- loom-days inside **any other** chain's excluded span for the same loom are dropped from
  every window; loom-days inside another chain's *after* window are kept and the baseline
  is annotated "includes n loom-days after <other event>";
- **Producing-day rule:** if the group's `before` holds fewer producing loom-days or
  producing days than the minima below, extend it backwards up to the 180-day cap; the
  extension never crosses a data hole (≥ 7 consecutive zero-row days for the branch) — it
  stops at the hole and the verdict is NOT ENOUGH DATA if still short. The extended window
  applies identically to the control set. Same check on `after` when cut by the last data
  date.

**Aggregates** (ratio-of-sums over the segment's loom-days, never mean of ratios):

- `days_present` = loom-days with a row; `days_run` = loom-days with `production_kg > 0`
  (the Low Producer "looms run" rule, `lowprod_weaving.py:308-310` on its row key
  `act_prod_kg`); `producing_days` = distinct dates with any `days_run`;
- `measured_days` = `days_run` with `capacity_measured > 0` (a subset, so coverage ≤ 100 %);
  `std_coverage = measured_days / days_run`; `hours_coverage` = `days_run` with
  `hours_run > 0` / `days_run`; `picks_coverage` = `days_run` with `std_picks_present` /
  `days_run`; `eff_days` = distinct dates with Σ `capacity_measured > 0`;
- `efficiency_pct = Σ actual_measured × 100 / Σ capacity_measured`, **None** when the
  denominator is 0 (`dpr_reports._eff_pct`, :127). Unlike the Low Producer shed average
  (`lowprod_weaving.py:21-25, 202-206` drops zero efficiencies), a manned loom with no
  output counts as 0 % here — correct for an impact study and rare (2 such rows on co 2
  line 4) — and unmanned looms drop out through `working_hours = 0`, which is the
  principled form of that exclusion;
- `efficiency_at_baseline_std = Σ actual_measured × 100 / Σ (baseline_speed_m × cap_per_rpm)`
  — the constant-yardstick efficiency (equal to `efficiency_pct` while the effective
  speed is unchanged). A 150 → 160 rpm change with identical output shows −6.3 % on
  `efficiency_pct` and 0 % here; the verdict uses this figure for any loom whose
  effective speed changed inside the span (`S` flag), and section C shows both;
- `picks_per_hour = Σ picks_delivered / Σ picks_hours` — quality-neutral and
  standard-independent;
- `yds_per_hour = Σ production_yds / Σ hours_run` over rows with `hours_run > 0`
  ("while running"); `yds_per_available_hour = Σ production_yds / Σ available_hours`
  ("per available hour", includes downtime);
- `cuts_per_loom_day = Σ cuts / days_run`; `kg_per_loom_day = Σ production_kg / days_run`
  (quality-dependent via `ozs_yds`, always labelled); `stoppage_hours_per_loom_day`;
- `Δ = after − before`. For efficiency `Δ` is reported in **points** and
  `Δ% = Δ_points × 100 / before_eff`; for every metric the verdict threshold applies to
  the **relative** `Δ%`, published per metric in `meta.thresholds` (default 3 for all,
  tunable). Section B shows both the point and the relative change;
- per stage k both `Δ vs baseline` (the INITIAL `before` restricted to the looms in
  stage k) and `Δ vs previous` (the parent stage's `after` over the same looms, blank when
  that segment is below the minimum). Like for like: each segment and its comparison use
  **only the looms covered by that segment**.

**Two headline figures.** Efficiency and yds per working hour are blind to downtime:
fewer stoppage hours raise `working_hours` and `std_prod_yds` in step with production, so
a modification whose whole point is fewer stoppages reads flat on them. The report
therefore always prints (1) a **while-running** figure (efficiency, picks or yds per
working hour — speed / pick utilisation while the loom runs) and (2) a **per available
hour / per loom-day** figure (yds per available hour, kg per loom-day, stoppage hours per
loom-day — includes downtime), and the verdict names which one moved.

**Headline (Auto), D3 (a).** In order: efficiency (needs `std_coverage ≥ 80 %` **and**
`hours_coverage ≥ 80 %` on both sides and `eff_days ≥ MIN_EFF_DAYS`), picks per working
hour (`hours_coverage` and `picks_coverage ≥ 80 %`), yds per working hour
(`hours_coverage ≥ 80 %`), cuts per loom-day; kg per loom-day is always shown, labelled
quality-dependent, and is the headline only when picked by hand. Computed server-side and
stated in `meta.notes`.

**Control (difference-in-differences), D5 (a).** Eligible controls (default): active
looms of the same dept, **ineligible** if any of their own snapshot rows (any chain) has
`[D_m − W, E_m + S + W]` overlapping the study span; control loom-days after a control's
own effective-standard change inside the span are dropped and the note says "control
standard changed on n looms" (the `S` test runs on controls too — 216 co 2 looms were
re-standardised on 07-09-2026 and 118 on 25-08-2026). The speed band is matched **per
study loom**: each study loom's band = its `baseline_speed_m` ± 10 %, a control's speed
resolved the same way on the same date, the control set = union of the bands, DiD
computed per band and combined weighted by study loom-days (lines mix ~150 rpm Hessian
and ~477 rpm S4 looms, so one band would be meaningless); when neither side has a
standard, same `machine_name` prefix (letters before the digit run). Requires ≥ 5
controls with ≥ 30 producing loom-days and ≥ 10 producing days per window, capped by §8;
otherwise "no control" and the verdict uses the plain Δ with a caveat. A saved `CONTROL`
group overrides the automatic set but passes through the **same ineligibility rules**:
members with a snapshot row (any chain) overlapping the study span are dropped, a member's
loom-days after its own effective-standard change inside the span are dropped, and members
that are also in the study snapshot are excluded; every excluded member is named in
`meta.notes` ("control group EJM-C1: S305 dropped — modified 12-Aug; S310 dropped — in
the study"). Control aggregates use the **same calendar windows** (extended `before`
included): one pair per distinct `D_m` in the stage, combined weighted by the study
loom-days on that date (one window in the common single-date case).
`net_Δ% = Δ%_study − Δ%_control`.

**Weaver check.** Every loom row carries `eb_id` (`weaving_query.py:814`); per loom the
dominant weaver share before vs after gives the `W` flag (≥ 20 pts moved, or disjoint
sets) and the count "weaver changed on k looms" in section B — the most common confounder
of a per-loom study, and the reason a group is the weaver's unit.

**Quality normalisation.** Efficiency and picks per hour are quality-neutral; kg and yds
are not. "Same quality only" works at **spell-row grain**: keep rows whose
`weaving_quality_id` occurs in that loom's rows in both windows, rebuild the loom-days
from the kept rows, re-apply the minima. Section E adds `kg_per_loom_day_std_mix` =
Σ_q (before share of loom-days of quality q) × (after kg per loom-day of quality q),
labelled "at before quality mix"; qualities absent on one side are dropped with a note.
The `Q` flag exposes the mix per loom.

**Verdict** (constants in `src/juteProduction/constants.py`, published in
`meta.thresholds`): `MIN_LOOM_DAYS_GROUP = 30`, `MIN_LOOM_DAYS_LOOM = 10`,
`MIN_DAYS_PER_SIDE_GROUP = 10`, `MIN_DAYS_PER_SIDE_LOOM = 5` (producing days — a 30-loom
group would otherwise pass the gate after a single day, on which one holiday or power cut
decides the verdict), `MIN_EFF_DAYS = 5`, `VERDICT_THRESHOLD_PCT = {metric: 3}`,
`SE_MULTIPLIER = 2`. NOT ENOUGH DATA when any minimum fails on either side, or fewer than
half the looms meet the per-loom minimum. Otherwise the noise rule is a t-like test on the
**day-level** series (loom-days share day effects, so days are the unit): per side compute
the day-level metric (ratio-of-sums per day), `n1`, `n2` producing days and within-window
SDs `s1`, `s2`; `s_p = sqrt(((n1 − 1) s1² + (n2 − 1) s2²) / (n1 + n2 − 2))`;
`SE = s_p × sqrt(1/n1 + 1/n2)`; tag "within day-to-day variation" when
`|Δ| < SE_MULTIPLIER × SE`. With a control, `SE_control` is computed the same way over the
control's daily series and `SE_net = sqrt(SE_study² + SE_control²)`. IMPROVED when
`Δ% ≥ +threshold`, `|Δ| ≥ SE_MULTIPLIER × SE`, and (with control)
`|net Δ| ≥ SE_MULTIPLIER × SE_net` with the same sign; WORSE symmetrically; else NO CLEAR
CHANGE. Computed per stage; the strip shows the latest stage that meets the minima
(§7.2 B). `n1`, `n2`, `s_p`, `SE` are published in `meta.thresholds` / `evidence`.
"k of m looms improved" = looms with own `Δ > 0` and ≥ `MIN_LOOM_DAYS_LOOM` producing
loom-days per side. No p-values, no numpy. (Why not "1 SD of the day series": measured on
sls co 2 line 4 the day-level efficiency SD is 3.8 pts on a 53.7 % mean — a +3 % relative
change is 1.6 pts, under 1 SD on every shed; with 30 days a side the SE of the difference
is ≈ 0.26 SD, so the SD rule would be ~4× too strict and would almost never confirm the
3–5 % the owner looks for.)

### 7.4 Stored verdict — phase 3, optional (D17)

Three columns on `jute_prod_loom_modification` (§4.3): `last_verdict`,
`last_verdict_pct`, `last_verdict_at`, added by an ALTER migration
(`<YYYYMMDD>_loom_modification_verdict_columns.sql`, **NEW**). The report endpoint writes
them on the chain's **root row** every time it runs with the default filters (event settle
days, default window, Auto metric, default control): the strip's verdict and its headline
Δ %. Runs with non-default filters do not overwrite. `/modification_table` and the
register grid then show "latest verdict · Δ %" per chain — the overview across
modifications without recomputing any report.

## 8. Endpoints

Router `loom_modification_router` in `src/juteProduction/loom_modification.py` (**NEW**)
mounted at `/api/loomModification` next to `main.py:344`; queries in
`loom_modification_query.py` (**NEW**, arg-less `text()` builders, `yarn_ledger_query.py:1`
convention). Helpers `_require_co_id` / `_optional_branch_id` as `weaving_masters.py:50-67`.
All responses `{"data": ...}`; GET 400 on bad params, writes roll back on error.

| Method | Path | Params / body | Returns |
|---|---|---|---|
| GET | `/setup` | `co_id, branch_id` | `looms` (all active looms, `get_stoppage_machines_query` shape `stoppage_query.py:27`, joined `dept_mst → branch_mst` filtered by `co_id`), `lines` (distinct `CAST(line_no AS CHAR)`), `groups`, `mod_kinds`, `mod_categories`, `done_by_options` as `{code,label}`, `parties`, `open_events` for "Follows" (latest non-REVERSAL stage per chain with un-reverted looms) |
| GET | `/loom_group_list` | `co_id, branch_id?, group_purpose?, search?` | groups with `member_count` |
| POST | `/loom_group_create` | `{co_id, branch_id?, group_code, group_name, group_purpose, remarks?, machine_ids[]}` | `{loom_group_id}` |
| PUT | `/loom_group_edit/{id}?co_id=` | COALESCE patch + optional `machine_ids` (replace) | `{message}` |
| GET | `/loom_group_detail/{id}?co_id=` | | header + `members` + count of events referencing it |
| GET | `/loom_group_suggest` | `co_id, branch_id, weeks=4` | optional (D2 b/c): proposed groups from weaver-on-loom attendance marks (`daily_ebmc_attendance` ⋈ `daily_attendance`), one row per weaver with the looms marked most often |
| DELETE | `/loom_group_delete/{id}?co_id=` | | 400 while referenced |
| GET | `/modification_table` | `co_id, branch_id?, loom_group_id?, machine_id?, date_from?, date_to?, mod_kind?, page, limit` | `{data, total, page, page_size}` (`overhaul_table` shape, limit ≤ 200, `machine_id` filter joins the snapshot); rows carry stage `k of n`, per-stage loom count, `data_check`, and from phase 3 `last_verdict` / `last_verdict_pct` (§7.4) |
| GET | `/modification_by_id/{id}?co_id=` | | header + `members` (labels, `done_date`, `done_to_date`) + parent summary + children summaries |
| GET | `/modification_chain/{root_id}?co_id=` | | every event of the chain in date order with snapshots |
| GET | `/machine_timeline` | `co_id, machine_id` | all snapshot rows for one loom in date order (stage boundaries; phase 3 Machine History panel) |
| POST | `/modification_create` | `{co_id, branch_id?, loom_group_id?, members:[{machine_id, done_date?, done_to_date?, remarks?}], mod_date, mod_to_date?, settle_days, mod_kind, parent_modification_id?, mod_category, title, description?, expected_effect?, done_by, party_id?, total_cost?, remarks?, save_as_group?:{group_code, group_name}}` | `{modification_id, root_modification_id, data_check:{before_days, after_days}, message}` |
| PUT | `/modification_edit/{id}` | same body, `co_id` in body (overhaul contract); snapshot replaced; parent/kind immutable with children | `{message, data_check}` |
| POST | `/modification_add_members/{id}` | `{co_id, members:[{machine_id, done_date, done_to_date?}]}` | `{message}` ("Add looms to this stage") |
| POST | `/modification_insert_earlier/{root_id}` | create body for the new INITIAL + `old_root_kind` (FOLLOWUP / RECTIFY) | `{modification_id}` (re-parents the old root, §5) |
| DELETE | `/modification_delete/{id}?co_id=&chain=0` | | 400 while children exist unless `chain=1` on a root |

`_save_modification(db, body, user_id, modification_id=None)` (shared by create/edit,
`overhaul.py:381` pattern). Execute order, documented in the test file: validate parent
and chain rules → loom context (is-a-loom, branch, branch-of-co) → per-loom overlap check
→ insert or update header → diff old vs new snapshot (edit) → insert snapshot rows →
data-check COUNT. Caller commits once.

Report router `loom_modification_impact_router` in
`src/juteProduction/loom_modification_impact.py` (**NEW**), mounted with prefix
`/api/juteProductionReports` in the `main.py:350-361` block:

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/loom_modification_impact` | `co_id, branch_id, modification_id (any stage id, resolved to its root chain), window_days=60, settle_days?, metric=auto, control=speed_band, control_group_id?, same_quality_only=0, weaving_quality_id?` | `{data: {chain, windows, verdict, stages[], segments[], per_loom[], daywise[], quality_mix[], context{stoppage, breakdown, waste, hands}, evidence[], meta{source, metrics[], thresholds, notes[], server_ms}}}` |
| GET | `/loom_modification_impact_options` | `co_id, branch_id` | root events for the picker |

Validation as `reports.py:49` `_checklist_scope` style, with **caps that reject with a
400 naming the cap** (never a silent truncation): `window_days ≤ 180`; total span (before
+ excluded + after, all stages) ≤ **200 days** (`IMPACT_MAX_SPAN_DAYS`); study looms
≤ 100; study + control looms together ≤ **120** (`IMPACT_MAX_LOOMS`; the automatic control
set is trimmed to the cap by closest speed band first, and the note says so). Larger spans
and control sets return only when the phase-2 timing gate passes at those sizes or the
summary table exists (§9, D15). Server time per request is logged and returned as
`meta.server_ms`.

## 9. Performance plan (sls volumes)

Not used: the per-day loop (`fetch_weaving_day_rows`, 0.31 s/day from the 61 ms round
trip = ~56 s for 180 days) and, for phase 2, the frozen `jute_prod_weaving_log` (Aug 2026
onward only, 91.9 % soft-deleted rows — 319,931 of 348,200 — single
`(co_id, tran_date, spell_id)` index, stale headers; see D15 for the contract).

**What was measured (EXPLAIN ANALYZE on sls, read-only, 2026-09-26)** with the first
draft's statement (FORCE INDEX `idx_wd_jugar_chain`, `LEAD()` interval target map,
range-scoped `stp` + `att`, GROUP BY machine, date; `tmq` / `pks` / `pka` not yet
included):

| Case | Spell rows → loom-days | Server time | Where it went |
|---|---|---|---|
| Q1 co 2, 40 looms, 270 days | 54,200 → 10,840 | **7.2 s** | base scan + plain joins 0.58 s; `tms` join 2.6 s; `att` materialise 3.66 s |
| Q2 co 106, 300 looms × 366 days (first-draft caps) | 395,032 → 79,859 | **17.2 s** | base 3.2 s; `tms` 9 s; `att` 2.8 s; aggregate 1 s |
| Q3 = Q1 without FORCE INDEX | same | 8.4 s | base scan 3.5 s instead of 0.6 s |

Two structural lessons: (1) every loom-day is ~5 spell rows on both companies (not the
2.3 the first draft assumed), and (2) joining the (machine, date)- and (quality, date)-keyed
standards to **every spell row** is the dominant cost (~20–70 µs per lookup into a
materialised derived table, linear in spell rows), followed by `att`, which cannot be
scoped by loom without the §4.5 index. The base scan is secondary.

**`weaving_range_loomday_sql()`** in `src/juteProduction/loom_modification_impact_query.py`
(**NEW**; its own module so `weaving_query.py`, which other sessions edit, stays
untouched), a **three-stage aggregation**:

1. *Spell grain, only spell-keyed joins.* `FROM jute_prod_weaving_daily wd` (FORCE INDEX
   `idx_wd_jugar_chain` until `idx_wd_co_machine_date` exists) `WHERE wd.active = 1 AND
   wd.co_id = :co_id AND wd.machine_id IN :machines AND wd.tran_date BETWEEN :date_from AND :date_to`
   (branch predicate NULL-tolerant as the slice's) joined to `spell_mst`, `stp`
   (`GROUP BY tran_date, machine_id, spell_id` over the range; 1,568 active rows on sls,
   `idx_stop_probe` fits) and `att` (`GROUP BY attendance_date, mc_id, spell_id` keeping
   the `STRAIGHT_JOIN` + weaver-prefix gate, `weaving_query.py:972-990`, restricted to
   `dea.mc_id IN :machines` and the date range; materialised **once** for the union of
   study + control ids, never per chunk). Output `GROUP BY machine_id, tran_date, weaving_quality_id`:
   Σ cuts, Σ total_jugar, Σ working_hours, Σ spell hours, Σ stoppage hours, eb_id set,
   rows with hours > 0 — roughly 5× fewer rows than the spell grain.
2. *Loom-quality-day grain.* Join `tms` (machine, date), `tmq` (quality, date), `pks`
   (exact-day picks, R-08-21; 5,471 active rows), `pka` and the quality master to that
   set (co 2 Q1: ~11k rows instead of 54k) and compute `production_yds`, `production_kg`,
   `std_prod_yds`, `cap_per_rpm`, `picks_delivered`. Interval joins: `tms` / `tmq` =
   `LEAD()` over rows ranked by `(effective_date, weaving_target_map_id)` per ref / role /
   param — same tie-break as the slice's
   `ROW_NUMBER() ... ORDER BY effective_date DESC, weaving_target_map_id DESC` (:905-912),
   so two active rows on one date resolve to the newest id (the older row's interval is
   empty); `pka` = `LEAD(entry_date) OVER (PARTITION BY weaving_quality_id ORDER BY entry_date)`
   over rows with `picks IS NOT NULL`, joined as an interval (the slice's per-day
   `MAX(entry_date)` probe, :945-957, must not become a row-multiplying join).
   Alternative if simpler in code: fetch the ~500 target-map rows and ~5.5k pick rows once
   and resolve them in Python by `bisect` (the target-map rows are fetched anyway for the
   baseline speed, the `S` flag and the markers).
3. *Loom-day grain.* `GROUP BY machine_id, tran_date` to the §7.3 columns with an
   `is_control` flag; study + control ids in **one** statement (the §8 cap keeps it under
   120 ids, so no chunking).

Because `ROUND(…, 3)` now applies per aggregate rather than per spell row, the parity
test compares with a tolerance (yds / kg within 0.01, efficiency within 0.05 pts), not
"copied verbatim".

**Expected cost after restructuring** is not claimed here — it is a **phase-2 exit gate**:
re-measure on sls read-only (a) the owner's case (30 looms + ~55 controls, 60 / 3 / 60
days) and (b) the §8 caps (120 looms × 200 days), with and without the §4.5 indexes.
Target ≤ 3 s server time at the caps before the sls menu row is added. If the target is
missed, build the loom-day summary table (D15 option c) instead of tuning further: a
`jute_prod_weaving_loomday` table (`co_id, branch_id, machine_id, tran_date, weaving_quality_id, cuts, jugar, production_yds, production_kg, working_hours, available_hours, stoppage_hours, std_prod_yds, std_speed, has_standard`,
index `(co_id, machine_id, tran_date)`) written by the same sync / reprocess path that
maintains `jute_prod_weaving_log` (`weaving_lock.flag_reprocess_if_locked` :149,
`_flag_stale_for_standard` `weaving_target_map.py:65` already mark units to re-sync),
backfilled once via the day slice (~683 days × 0.31 s ≈ 4 min per company). The report
then reads one range seek and needs no async job either way. The connection pool is
shared by the tenant (`src/config/db.py:22-23` pool 10 + 30 overflow, read timeout
1800 s), so a slow report is everyone's problem — hence the caps and the gate.

**Parity gate before the report is shown on sls:** unit test (in
`src/test/test_juteprod_loom_modification_impact.py`, **NEW**) comparing the range SQL
aggregated to a loom-day against `weaving_day_slice_sql` on a sample day
(`src/test/test_weaving_dayslice.py` precedent), including same-day duplicate target-map
rows, NULL-picks days and a loom with an `actual`-role speed, **and**
`dbqueries/verify_weaving_range_parity.py` (**NEW**, after
`dbqueries/verify_weaving_dayslice_parity.py`) on three sample days per company.

Register pages are trivial volume.

## 10. Build phases

Each phase is shippable on its own and ends with an owner check from the phone on dev3
before anything is committed.

| Phase | Deliverable | Tests |
|---|---|---|
| **1 — Register: Loom Groups + Loom Modifications** | Migration `<YYYYMMDD>_loom_modification_tables.sql`; ORM models (Mapped style); `MOD_KINDS`, `MOD_CATEGORIES`, done-by constants; `loom_modification.py` + `_query.py` (setup, group CRUD, optional `loom_group_suggest`, modification CRUD with linear-chain / per-loom REVERSAL / done-date / is-a-loom / branch-of-co rules, per-loom overlap, snapshot, add-members, insert-earlier-stage, delete-chain, `machine_timeline`, `data_check` in the save response); FE Loom Groups master (tick-list, name range, "Fill from line", "Suggest from attendance", zod schema, fullScreen dialog), Loom Modification register (form, loom checklist with done dates, Add follow-up, Add looms, Insert earlier stage, grid + mobile cards, save snackbar with data check); `api.ts` constants; service file; menu migration `<YYYYMMDD>_loom_modification_menus.sql` (2 sidebar rows, `role_menu_map` mirrored from "Weaving Standards"); dev3 demo: a 6-loom group S293..S298, one chain INITIAL + RECTIFY + partial REVERSAL | `src/test/test_juteprod_loom_modification.py` (dependency overrides + `execute.side_effect` in the documented order; commit not called on every 400 path; chain rules; per-loom overlap; done-date range; non-loom rejected; immutability; delete chain); `types/modificationTypes.test.ts`, `types/loomGroupTypes.test.ts` (zod rules, payload builder) |
| **2 — Loom impact report** | `weaving_range_loomday_sql` (three-stage); `loom_modification_impact.py` (per-loom windows, baseline speed resolution, producing-day rule with data-hole stop, segments and stage ladder, controls with per-loom bands and control-S test, DiD, SE rule, two headline figures, coverage measures, flags S/Q/H/W/D/N, section F legs, notes); FE report page (filters with 30 / 60 presets, KPI strip + stage ladder, tabs A–G, exports, phone cards); "data check" chip on the register grid; "View impact" row action unhidden; report menu migration `<YYYYMMDD>_loom_modification_report_menu.sql`; index migration `<YYYYMMDD>_weaving_daily_machine_date_index.sql` (dev3, then sls off-shift on owner go-ahead, D11); dev3 seed of `mcid` standard speeds for co 9 looms (none exist) so efficiency renders — via the Weaving Standards page on dev3 (or `dbqueries/seed_dev3_loom_speeds.sql`, **NEW**); **timing gate** on sls read-only (owner's case and the §8 caps, ≤ 3 s server time) — summary table (D15 c) if missed; cross-reference to D15 added to the process-lock spec | `src/test/test_juteprod_loom_modification_impact.py`: parity unit test (with tolerance, duplicate-date, NULL-picks and actual-role cases) + `verify_weaving_range_parity.py`; pure-function tests for window maths (per-loom D/E, rolled-out dates, cut at next event), baseline speed resolution, segment / stage ladder, DiD per band, SE rule, verdict, flags (no DB); endpoint test patching the range query (`test_juteprod_lowprod_weaving.py:449` pattern); `_service.test.ts`, `page.test.tsx` |
| **3 — Polish (optional, on request)** | **Overview list** (D17, §7.4): ALTER migration `<YYYYMMDD>_loom_modification_verdict_columns.sql`, the report endpoint writes `last_verdict` / `last_verdict_pct` / `last_verdict_at` on the root row, `/modification_table` and the register grid show latest verdict · Δ % per chain; read-only "Modifications" panel on `juteMechanical/machineHistory` (`get_machine_history` in `src/juteMechanical/reports.py:1411` gains a `modifications` array); weekly SVG bar strip (D10); Excel of the whole report as one workbook (`gridSections`) | stored-verdict write test (default filters write, non-default do not); machine-history payload test |

### 10.1 Rollout checklist

1. dev3: tables migration + register menu rows (phase 1) → owner checks the register from the phone on dev3.
2. sls: tables migration — only when the owner names the DB (sls and vowconsole3 hold production data).
3. sls: register menu rows (users log out / in).
4. sls: the two indexes off-shift, online DDL, on the owner's go-ahead (D11).
5. dev3: report menu row → owner checks the report on dev3; timing gate on sls read-only (§9).
6. sls: report menu row only after the timing gate passes.

Nothing is committed to git until the owner has verified on dev3; every sls step is
announced to the owner before it runs.

## 11. Owner decisions

### 11.1 Decided (owner's ticks and chat, 2026-09-26)

- **D1 — scope: case-wise, loom-only.** Loom Groups master, Loom Modification register and
  Loom Modification Impact report; no shared register or report frame; another kind of
  machine gets its own copy later as a separate spec.
- **D3 (a) — headline figures.** Auto = efficiency %, then picks per working hour, then
  yards per working hour, then cuts per loom-day, as coverage allows; kg per loom-day
  always shown and labelled quality-dependent; two figures always printed (while-running
  and per available hour / per loom-day), the verdict naming which one moved.
- **D5 (a) — controls.** Untouched looms of the same department in the same speed band,
  matched per study loom; a saved CONTROL group can replace them; ineligible controls
  (modified or re-standardised inside the span, or in the study) are dropped and named.
- **D6 (c) — standards.** The register does not write Weaving Standards; a speed change is
  entered there as today. The report keeps the `S` flag (standard changed inside the span,
  from `jute_prod_weaving_target_map` `mcid`/`speed` rows of either role) and the
  constant-yardstick efficiency against the standard in force at the start of the before
  window, because a standard change still moves the yardstick.
- **D7 (a) — menu.** Jute Production: Loom Groups under masters, Loom Modifications entry
  page, Loom Modification Impact under Production Reports; optional read-only panel on
  Machine History in phase 3.
- **D8 (a) — work days.** Excluded from both windows by date arithmetic; stoppage hours
  entered as today, no auto-created stoppage rows.
- **D9 (a) — edit / delete.** Free edit and soft delete, no approval. Chain rules stay: a
  row with follow-ups cannot be deleted or re-parented; Delete chain from the root row;
  Insert earlier stage for a change entered late.
- **D10 (a) — no chart library.** Tables, KPI tiles, stage ladder, phone cards; a
  hand-drawn weekly SVG strip in phase 3 if missed.
- **D11 (a) — indexes.** `jute_prod_weaving_daily (co_id, machine_id, tran_date)` and
  `daily_ebmc_attendance (mc_id, daily_atten_id, is_active)` on sls in phase 2, off-shift,
  online DDL, the owner naming the DB on the day.
- **D13 (a) — categories.** Fixed list Mechanical / Electrical / Speed change / Accessory /
  Setting / Other; promote to a master only if it grows past ~10.
- **D17 (b) — overview list of all modifications with latest verdict.** Later: phase 3,
  optional (§7.4).
- D12 (winding) and D16 (one type per entry) fell away with the loom-only scope.

### 11.2 Still open

- **D2 — how are loom groups declared?** Options: (a) declared by users in the Loom Groups
  master (tick-list, name range "S101 to S106", "Fill from line"); (b) proposed from the
  weaver-on-loom attendance marks of the last weeks, user confirms / edits; (c) both — (a)
  to declare, (b) as a one-tap "Suggest from attendance" inside the master.
  Recommendation: (a); (c) is cheap if wanted. `line_no` alone is not it — lines mix loom
  models and co 106 parks 213 looms on line '100'.
- **D4 — default window.** The owner ticked both 60 and 30 days per side; both stay as
  one-tap presets, 3 settling days, all adjustable on the report. Default 60 unless the
  owner says 30. The before window stretches (up to 180, never across a data hole) until
  it holds 30 producing loom-days and 10 producing days.

### 11.3 Builder defaults (taken unless the owner objects)

- **D14 — minimum data and the noise rule.** Per side 30 producing loom-days and 10
  producing days for a group (10 and 5 for a single loom), 5 days with measurable
  efficiency before efficiency may be the headline; change ≥ 3 % relative (per metric,
  tunable) and beyond twice the standard error of the day-level series; numbers always
  shown, only the verdict withheld, "within day-to-day variation" tagged. Why: a one-SD
  rule measured ~4× too strict on sls and would almost never confirm a 3–5 % change.
- **D15 — where the report reads loom figures from.** Live recompute from
  `jute_prod_weaving_daily` through the new range query (printed "recomputed live, may
  differ from the locked DPR", cross-referenced in the process-lock spec), moving to a
  `jute_prod_weaving_loomday` summary table maintained by the sync / reprocess path if the
  phase-2 timing gate (≤ 3 s at the caps) is missed. Why: the summary table removes both
  the divergence and the performance risk at the cost of one more table and a write in
  the sync path; merging the frozen log per day is the slowest and most complex option.

## 12. Risks / open items

- **sls data reality.** Efficiency, `std_speed` and SQC picks are 0 on 100 % of weaving
  rows before Aug 2026; co 2 has no rows Feb–Jul 2026 and only 112–167 of 334 looms since;
  co 106 has none in Jul 2026 and 347/485 looms without a standard. Any study dated before
  Aug 2026 runs on yds / cuts / kg only; the owner's first scenario (10-Aug-26, co 2) has
  zero producing loom-days in its 60-day before window and in the 180-day extension — the
  save snackbar says so from phase 1; windows straddling holes are flagged, and the first
  studies will be thin.
- **Efficiency coverage is attendance- and SQC-driven, not only standards-driven.** Frozen
  log, active rows: co 2 line 4 has 264 loom-spell rows with production but zero loom
  hours (no weaver marked) of ~960; co 106 line 7 had measurable group efficiency on only
  14 of 50 days (no pick reading → `std_picks 0` → `std_prod_yds 0` for every loom). If
  marking discipline changes after a modification (plausible while a study runs),
  efficiency and yds/hour shift without any real change — the `H` flag, `hours_coverage`
  and the per-cause notes are the defence; yds per available hour and kg per loom-day do
  not depend on marks.
- **Re-standardisation looks like impact.** 216 co 2 looms got their first standard on
  2026-09-07 and 118 on 2026-08-25. `efficiency_at_baseline_std`, the `S` flag on study
  and control looms, the standard markers (both roles) and the control set mitigate; a
  user ignoring the flag will misread it. Because the register no longer records the
  speed change itself (D6), a user who forgets to update Weaving Standards leaves the
  report measuring against the old speed — the `SPEED_CHANGE` category without a matching
  `S` flag is called out in the notes.
- **Quality mix.** 199 of 243 producing co 106 looms ran two qualities in the last 30
  days; kg and yds are quality-dependent. Efficiency, picks per hour, "same quality only"
  (spell-row grain) and section E with the "at before quality mix" figure are the
  defences; "same quality only" can shrink a loom to too few days.
- **Parity of the range SQL.** Re-keying the as-of derived tables by date can drift from
  the slice on edge cases (composite qualities, NULL-branch rows, overnight spell C,
  same-day duplicate target-map rows, `actual`-role speeds, NULL-picks readings). The unit
  test and the verify script are gates, not options.
- **Performance rests on measured timings.** The first draft's 0.5–1 s claim did not
  reproduce (7.2 s for 40 looms × 270 days; 17.2 s at the first-draft caps); the
  restructured statement is unmeasured until built. Caps are tight until the phase-2
  timing gate, and the summary table (D15 c) is the fallback. sls index statistics are
  stale (EXPLAIN row estimates unreliable) — use EXPLAIN ANALYZE.
- **Small samples and simple statistics.** The SE rule and the ± 3 % thresholds are
  heuristics; the verdict must be read with the notes. Thresholds are constants the owner
  can tune.
- **Membership snapshot vs live group.** Users may edit a group expecting old studies to
  change; the register says "members as recorded".
- **Tenant vocabulary and drift.** `LOOM` (dev3) vs `Loom` (sls): the loom test runs inside
  SQL under the case-insensitive collation. `machine_mst.line_no` is INT on dev3 and
  VARCHAR(10) on sls — compared as strings everywhere.
- **Ordering-based tests.** `_save_modification` has many hidden executes; a query added
  mid-handler silently breaks `execute.side_effect` tests — hence the documented order and
  the pure-function tests for the report maths.
- **dev3 demo needs seed data**: co 9 has no `mcid` standard rows and 350 weaving rows
  (2026-06-22..08-03) — seed via the Weaving Standards page on dev3 (or
  `dbqueries/seed_dev3_loom_speeds.sql`); real validation happens against sls read-only
  after phase 2.
- **Loom names repeat across departments** (dept 44 duplicates all 485 dept-348 names);
  every picker keys on `machine_id` and shows the dept.
- **Other sessions edit the same checkouts**; phase 1 should be built in a worktree cut
  from `claudehost`, visible on the live preview only after merge.
- **Menu cache.** Users must log out / in to see the three new rows.

## Appendix: file registry

**Backend (`vowerp3be/`) — NEW**

| Path | Content |
|---|---|
| `dbqueries/migrations/<YYYYMMDD>_loom_modification_tables.sql` (phase 1) | 4 tables, rollback, verification (runner note; `<YYYYMMDD>` = the date the file is created, §4) |
| `dbqueries/migrations/<YYYYMMDD>_loom_modification_menus.sql` (phase 1) | "Loom Groups" (`juteProduction/masters/loomGroupMaster`), "Loom Modifications" (`juteProduction/loomModification`) under the Jute Production hub; `role_menu_map` mirrored from "Weaving Standards" |
| `dbqueries/migrations/<YYYYMMDD>_loom_modification_report_menu.sql` (phase 2) | "Loom Modification Impact" `report = 1` under `juteProduction/report` (clone of `20260926_purchased_yarn_ledger_menu.sql`) |
| `dbqueries/migrations/<YYYYMMDD>_weaving_daily_machine_date_index.sql` (phase 2) | `idx_wd_co_machine_date` on `jute_prod_weaving_daily`, `idx_dea_mc_atten_active` on `daily_ebmc_attendance` |
| `dbqueries/migrations/<YYYYMMDD>_loom_modification_verdict_columns.sql` (phase 3, optional) | ALTER: `last_verdict`, `last_verdict_pct`, `last_verdict_at` on `jute_prod_loom_modification` (§7.4, D17) |
| `dbqueries/seed_dev3_loom_speeds.sql` (phase 2, optional) | dev3-only `mcid`/`speed` standard rows for co 9 looms, when not seeded through the Weaving Standards page |
| `dbqueries/verify_weaving_range_parity.py` (phase 2) | range vs day-slice parity on sample days (tolerance) |
| `src/juteProduction/loom_modification_models.py` | ORM (Mapped style, `bale_models.py` precedent) |
| `src/juteProduction/loom_modification.py` | register router `/api/loomModification` |
| `src/juteProduction/loom_modification_query.py` | `text()` builders for the register |
| `src/juteProduction/loom_modification_impact.py` | report router under `/api/juteProductionReports` |
| `src/juteProduction/loom_modification_impact_query.py` | `weaving_range_loomday_sql()` (three-stage) + section F legs |
| `src/juteProduction/constants.py` (edit) | `MOD_KINDS`, `MOD_KIND_LABELS`, `MOD_CATEGORIES`, `MOD_CATEGORY_LABELS`, `MOD_DONE_BY`, `MIN_LOOM_DAYS_GROUP`, `MIN_LOOM_DAYS_LOOM`, `MIN_DAYS_PER_SIDE_GROUP`, `MIN_DAYS_PER_SIDE_LOOM`, `MIN_EFF_DAYS`, `VERDICT_THRESHOLD_PCT`, `SE_MULTIPLIER`, `IMPACT_MAX_SPAN_DAYS`, `IMPACT_MAX_LOOMS` |
| `src/main.py` (edit) | two `include_router` lines |
| `src/test/test_juteprod_loom_modification.py`, `src/test/test_juteprod_loom_modification_impact.py` | tests (`test_{module}_{feature}.py` rule); the parity test lives in the impact file |
| `docs/superpowers/specs/2026-07-07-weaving-entry-process-lock-design.md` (edit, phase 2) | cross-reference to D15 |

**Backend precedents cloned:** `src/juteProduction/weaving_masters.py` (master CRUD),
`src/juteMechanical/overhaul.py` (`_save_overhaul` :381, `_machine_context` :226,
`_validate_header` :236, `overhaul_table` :504), `src/juteProduction/stoppage_query.py:27`
(`get_stoppage_machines_query`), `src/juteProduction/weaving_query.py:737` (slice),
`src/juteProduction/hands_report_query.py:137` (`_leg`),
`src/juteProduction/lowprod_weaving.py` (report skeleton, `build_meta` :368, looms-run
rule :308-310), `src/juteProduction/dpr_reports.py:106-135` (`_f`, `_round`, `_ratio`,
`_eff_pct`), `src/juteProduction/reports.py:49-92` (`_checklist_scope`, `_parse_multi`),
`src/juteMechanical/reports.py:1411` (`get_machine_history`, phase 3).

**Frontend (`vowerp3ui/`) — NEW**

| Path | Cloned from |
|---|---|
| `src/app/dashboardportal/juteProduction/masters/loomGroupMaster/{page.tsx, types/loomGroupTypes.ts, types/loomGroupTypes.test.ts}` | `masters/weavingQualityMaster/page.tsx` (shell only; zod added) + the register's `LoomChecklist` |
| `src/app/dashboardportal/juteProduction/loomModification/{page.tsx, _components/ModificationEntryForm.tsx, _components/LoomChecklist.tsx, _components/ModificationGrid.tsx, hooks/useModificationSetup.ts, hooks/useModificationTable.ts, types/modificationTypes.ts, types/modificationTypes.test.ts}` | `juteMechanical/overhaul/` + `juteMechanical/breakdown/types/breakdownTypes.test.ts` |
| `src/app/dashboardportal/juteProduction/report/loomModificationImpact/{page.tsx, page.test.tsx, _service.ts, _service.test.ts, impactRows.ts, impactRows.test.ts, _components/StageLadder.tsx, _components/…}` | `report/weavingLowProducer`, `report/handsReport` (filters), `report/purchasedYarnLedger` (branch norm, test harness), `report/doffAnalysis/_components/{FrameCards,ExportActions}.tsx`, `report/dailyProductionReport/_components/DprPieces.tsx` |
| `src/utils/loomModificationService.ts` | helpers from `src/utils/juteMechanicalService.ts:139-177` |
| `src/utils/api.ts` (edit) | `LOOM_GROUP_*`, `LOOM_MODIFICATION_*` beside `WEAVING_QUALITY_*` (:1399); `JP_LOOM_MODIFICATION_IMPACT`, `JP_LOOM_MODIFICATION_IMPACT_OPTIONS` beside `JP_HANDS_REPORT` (:1128) |
| `src/app/dashboardportal/juteMechanical/machineHistory/_components/MachineHistoryPanels.tsx` (phase 3 edit) | new "Modifications" panel |
| `docs/claude/modules/jute-production/weaving/SPEC.md` (edit, phase 1) | pointer to this spec; page catalogue rows |

**Menus (tenant `menu_mst`)**

| Name | Path | Parent | `report` |
|---|---|---|---|
| Loom Groups | `juteProduction/masters/loomGroupMaster` | Jute Production (dev3 768) | NULL |
| Loom Modifications | `juteProduction/loomModification` | Jute Production | NULL |
| Loom Modification Impact | `juteProduction/report/loomModificationImpact` | Production Reports (dev3 945) | 1 |
