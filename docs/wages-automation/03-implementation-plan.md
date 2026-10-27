# 03 — Implementation Plan: One-Button Wage Data Preparation in vow3

**Scope of this note:** design + delivery plan only. No code has been written. Everything
here is at spec level. Product-owner decisions received 2026-07-06 (see INDEX.md):
masters not yet migrated (Phase 0 includes full migration), direct cutover (no
dual-write), FA automated, constants in config tables from day one, and — **revising §2
below — production streams source natively from the vow3 jute production module, not
from the legacy schemas; see doc 04** for the native source map, prerequisite changes
and phase amendments. Remaining open items are in §9.

## 1. Goal and guiding principles

**Goal:** on the vow3 Pay Roll page, a user selects pay scheme + period (+ branch) and
presses **one button**; vow3 computes all attendance/production-derived wage inputs and
lands them in `pay_components_custom`, ready for `pay_register_create` /
`pay_register_process`. This replaces: DataPortal process → checklists → excel download →
vow2.0 (or vow3) manual upload.

Principles:

1. **Land directly in `pay_components_custom`.** Skip the legacy staging table
   (`tbl_njm_wages_data_collection`) and the excel. The staging table only existed because
   aggregation and calculation lived in different apps.
2. **Behavior parity first, cleanup second.** v1 reproduces the legacy numbers (including
   the piece-wage cap and sardar ×1.3) so a parallel-run diff can prove correctness; only
   then refactor constants into masters.
3. **Same seam as today.** The engine writes exactly where manual Save and Excel upload
   write; the processing engine is untouched. Manual entry/upload remain as override
   paths.
4. **Tenancy-safe.** Portal persona, `Depends(get_tenant_db)`; all collector reads are
   ordinary tenant-DB queries (native sourcing — no cross-schema access, no hardcoded
   DB names).
5. **Loud about coverage.** Because absent inputs evaluate as 0 in the processor, every
   run must report per-collector row counts, unmapped employees, and missing rates.

## 2. Integration options considered

| Option | Verdict |
|---|---|
| **(a) Ad-hoc cross-schema reads from vow3** | Rejected — unstructured legacy coupling |
| **(b) DataPortal exposes an API vow3 calls** | Rejected: CI3 has no service-to-service auth, the SQL is injection-prone, and it keeps the legacy app permanently in the critical path with two-phase failure modes |
| **(c) Re-implement the aggregation in vow3 reading the legacy schemas** (`vowsls`, `EMPMILL12`) | Was the original v1 recommendation; **superseded 2026-07-06** by (d) — kept here for the record |
| **(d) Calculate natively from vow3's own capture** — jute production module + native attendance/leave, writing `pay_components_custom` directly | **DECIDED.** The jute production module already captures beaming/weaving/winding/finishing/stoppage with machine line data; the legacy computation is fully documented (doc 01 §3) as the functional spec; the stub endpoints (`/pay_roll_fetch_*`) were built for exactly this. Prerequisites (rate masters, attribution completion, line-hours source, advances/canteen/FA/layoff homes) are enumerated in doc 04 |

## 3. Target architecture — Wage Data Collection Engine

New backend module (suggested: `src/hrms/wageDataCollection.py` + queries in
`src/hrms/wage_collection_query.py`), portal persona, registered under `/api/hrms`.

### 3.1 Collectors

One collector per legacy `update_for` stream, each an independent, individually
re-runnable unit with a common interface *(inputs: tenant ctx, scheme, period, branch,
config; output: list of (eb_id, metric_key, value) + diagnostics)*.

**Sources are native vow3 tables — the authoritative collector → source table is
doc 04 §4.** Summary:

| Collector | Mirrors | Reads (native vow3) | Notes |
|---|---|---|---|
| `attendance` | ATT | `daily_attendance` + `designation_mst.time_piece/piece_rate_type` | Hours pivot by time/piece/OT class, C-shift days, ESI days; layoff needs a native home or is dropped (doc 04 §2E) |
| `leave_canteen` | LEAVE | `leave_transactions` chain, holiday data; canteen capture per doc 04 §2E | Canteen rate from config |
| `advances` | IADV | new vow3 advances module (doc 04 §2E) | One-time opening-balance migration from `tbl_company_advance`; then balances net against vow3-collected history — see §8 risk 4 |
| `beaming` | BEAM | `jute_prod_beaming_daily` + `jute_prod_bm_quality` + rate master | Direct `eb_id` on entry; validate vs attendance (doc 04 §2B) |
| `winding` | WIND | `jute_prod_winding_doff` (+ jugar reconciliation) + rate master | Operator backfill + cop-rule decision (doc 04 §2B/2D) |
| `finishing` | FNG | `jute_prod_finishing_daily` + rate master | hemming/herackle eb-keying (doc 04 §2B) |
| `weaving` | WEAV | `vw_weaving_daily` + `jute_prod_weaving_quality` + rate master | Weaver attribution via `daily_ebmc_attendance` join; yards/meters unit decision (doc 04 §2B/2D) |
| `line_incentive_sh` | line pot + SH | `vw_weaving_daily` + `machine_mst(line_no, mech_shr_code)` + `jute_prod_stoppage_hours` + attendance | Line running/lost hours derived from spells − stoppage (replaces the legacy excel, subject to parity diff — doc 04 §2C); speed-band source decision |
| `iftu` | V (`njmwrkvardupdate`) | attendance + leave + holidays | Flat amount from config |
| `fa` | FA (manual excel today) | new FA rate master × attendance hours/8 | **Decided: automated.** The legacy review step (download statement → review → re-upload) is replaced by the run results panel; a manual correction remains possible via the Pay Roll grid |

Deliberately **not** a collector: Other/Adjs stays manual via the existing
`/pay_roll_upload` / `/pay_roll_save` — it is by nature human-entered overrides.

### 3.2 Metric → component resolution

New tenant-DB mapping table (suggested name `pay_scheme_metric_map`): one row per
`(payscheme_id, metric_key, component_id)`, seeded by a **one-time import** of
`EMPMILL12.tbl_wages_data_payschm_map_table` joined to `tbl_pay_components.CODE`,
re-resolving CODE against the tenant's `pay_components` (a migration script — not a
runtime dependency). This keeps the per-scheme column layout as configuration (as it is
today) rather than code. Derived metrics computed at excel time today (piece-wage cap
1200/208 split into `piece_wages` / `piece_wages_inc`) become explicit derivation rules
applied after collection, before landing.

### 3.3 Landing rules

- Soft-replace scoped to *(period × employees in scope × components produced by
  automation)*: set STATUS=0 on prior rows, insert fresh rows with `PAY_PERIOD_ID` set
  explicitly (avoids the payRoll.py:251/547 bind quirk).
- **Source marker:** add a `SOURCE` column to `pay_components_custom` (values e.g.
  `AUTO`, `MANUAL`, `UPLOAD`) via migration. Re-collect replaces only `AUTO` rows —
  the vow3 equivalent of DataPortal preserving `V`/`FA`/`OTH`. Manual save/upload keep
  writing their own markers and win by later timestamp within the processor's existing
  precedence.
- Whole run in one transaction per collector batch; a failed collector rolls back only its
  own stream and is reported, not silently skipped.

### 3.4 Run audit

New tables `pay_data_collection_run` (run id, co_id, branch_id, payscheme_id,
pay_period_id, from/to, status, triggered_by, started/finished) and
`pay_data_collection_run_dtl` (per collector: rows read, employees covered, rows written,
warnings JSON — unmapped employees, missing rates, zero-coverage flags). This powers the
UI result panel and is the audit trail replacing the legacy checklists.

### 3.5 Config

- **Piece-rate master:** new effective-dated `wage_piece_rate_mst` (stream, ref_type,
  ref_id, rate, uom, effective_from/to) with CRUD + FE page — replaces
  `EMPMILL12.tbl_wages_quality_master`; seeded by one-time import. Details: doc 04 §2A.
- **Wage constants (per tenant, per effective-date):** canteen rate, sardar multiplier,
  base wage, target factor, piece-wage cap, IFTU amount, cop divisor (if kept),
  worker category set, sardar/helper/weaver designation mappings, line-group mapping,
  speed bands. **Decided: config tables from day one**, seeded with parity values.
  Note: `pay_param` is unsuitable — despite the name it is pay-period CRUD over
  `pay_period` (`src/hrms/payParam.py`), not a key/value store; a dedicated
  `wage_config` table is needed.
- **Employee identity:** native everywhere — production rows and attendance carry vow3
  `eb_id` directly, so no legacy bridge at runtime. Rows with NULL/unresolvable
  operator attribution surface as warnings in the run detail, mirroring the upload
  endpoint's `skipped_employee_codes`.

### 3.6 API surface (spec)

| Endpoint | Behavior |
|---|---|
| `POST /api/hrms/pay_roll_prepare_data?co_id=` body `{payscheme_id, from_date, to_date, branch_id, pay_period_id?, collectors?}` | The one button. Resolves/validates period, runs all (or selected) collectors, lands values, returns run id + summary. Long-running: v1 synchronous with generous timeout is acceptable (legacy PHP ran ~minutes with `max_execution_time 6000`); if too slow, background the run and add `GET /pay_roll_prepare_status/{run_id}` |
| `GET /api/hrms/pay_roll_prepare_runs` / `.../{run_id}` | Run history + per-collector detail for the results panel |
| Existing stubs `/pay_roll_fetch_attendance` `_generic` `_cumulative` | Either re-point to the engine (attendance → `attendance`+`leave_canteen` collectors; generic/cumulative → their streams) or deprecate in favor of the single endpoint — decide with FE; the FE currently renders three buttons |
| Guards | Reject when the target `pay_period` is PROCESSED(28)/APPROVED(3)/LOCKED(32) unless an explicit re-open/re-collect flag is passed; always require co_id + payscheme_id + period |

Checklist replacements (validation the legacy flow did before processing) become
pre-flight checks inside the run + surfaced warnings: employees with attendance but no
scheme mapping; production rows with no rate; line-hours coverage; employees with no
components mapped.

## 4. Frontend plan (summary — full note in `vowerp3ui/docs/wages-automation-ui-plan.md`)

- Pay Roll page: one primary **"Prepare Wage Data"** button (replacing/consolidating the
  three stub buttons), disabled until scheme+period+branch chosen; progress state;
  results panel from the run summary (per-collector counts + warnings); auto-refresh the
  grid (`pay_roll_data`) so the user sees the landed values; guard banner when the period
  is already processed.
- Pay Register page: "data ready" indicator (latest successful run for scheme+period)
  next to Create/Process; optionally a combined "Prepare + Process" action later —
  explicitly out of v1 scope since the user asked for "make data available for
  processing".
- Keep Download Template / Upload Excel / manual grid as override paths (they become the
  FA/OTH equivalent).

## 5. Delivery phases

**Phase 0 — Foundations (blockers, mostly non-code):**
name/provision NJM's vow3 tenant and **migrate the payroll masters** — decided: they are
NOT yet in vow3 — schemes, components (STATUS=3), formulas, employee-scheme mappings and
employee master (source: `vowsls` `tbl_pay_scheme` / `tbl_pay_components` /
`tbl_pay_scheme_details` / `tbl_pay_employee_payscheme` / `tbl_pay_employee_structure`,
which vow3's tables mirror almost 1:1). Resolve the doc 04 decisions: winding cop rule,
weaving unit, layoff/canteen scope, line-hours derivation vs upload. Confirm NJM's daily
production entry + attendance-with-machine-mapping adoption in the vow3 jute production
module (doc 04 §2G). Transition mode is decided: **direct cutover** after Phase 3
validation — no dual-write work needed. (Legacy grants / weaving-view DDL: no longer
required — native sourcing.)
*Acceptance: a scheme+period in the NJM tenant can be processed end-to-end using a
hand-uploaded excel (proves masters are right before any automation).*

**Phase 1 — Engine core + masters:** migrations (`SOURCE` column,
`pay_scheme_metric_map`, run-audit tables, `wage_config`, **`wage_piece_rate_mst`** +
CRUD + FE page) + one-time seed imports (metric map, piece rates, advance opening
balances); ORM fixes (`MachineMst` stale columns, `daily_ebmc_attendance` model — doc 04
§2F); engine skeleton + `attendance`, `leave_canteen`, `iftu` collectors (already fully
native, no rate master needed); `pay_roll_prepare_data` endpoint + run detail endpoints;
pytest suite per repo conventions (mocked DB). *Acceptance: for a test period, ATT/LEAVE/V metrics land in
`pay_components_custom` and `pay_register_process` produces wages using them; manual rows
survive re-collect.*

**Phase 2 — Production collectors + attribution completion:** each collector lands
together with its attribution fix (doc 04 §2B): `beaming` (+eb validation), `winding`
(+operator backfill), `finishing` (+hemming/herackle eb-keying), `weaving` (+weaver
attendance join), `line_incentive_sh` (+line-hours derivation, speed-band config),
`advances` + `fa` (+ their new mini-modules/masters per doc 04 §2E), derivation rules
(piece-wage cap/split). *Acceptance: full collector set runs green on a real month of
native production data.*

**Phase 3 — Parallel-run validation (mandatory before cutover):** run legacy chain and
vow3 engine for the same month+scheme; diff (a) collected inputs vs the legacy excel
values per employee×component, (b) final `pay_employee_payroll` vs
`tbl_pay_employee_payroll`. The diff doubles as a **data-entry completeness check** —
gaps in native production entry show up as under-collection (doc 04 §2G). Build the diff
as a small report script/endpoint so it can be re-run every month of the transition.
*Acceptance: zero unexplained differences for one full month; sign-off from payroll
owner.*

**Phase 4 — Frontend + cutover:** one-button UI per §4; direct cutover to vow3 for real
month-ends; legacy DataPortal chain retired. (The former "flip collectors from legacy to
native later" tail is gone — collectors are native from day one.)

## 6. Testing requirements (per CLAUDE.md — mandatory)

Unit tests per collector (SQL param binding, aggregation edge cases: layoff subtraction,
OT classes, cop/spool split if kept, sardar multiplier, advance balance depletion, period
boundaries, NULL-operator attribution warnings), endpoint tests (auth, missing params,
processed-period guard, preserve-manual semantics), and the parallel-run diff as the
integration-level proof. dev3 gets seeded jute-production + attendance fixture data so
collectors are testable end-to-end without any production tenant.

## 7. Explicitly out of scope for v1

Contractor wages (`njmcntwagesprocessdata` — separate staging table and flow), EJM and
other mills' variants (same architecture applies later; collectors + config make it
per-tenant), payslip/bank-sheet outputs (vow3 already has register/payslips), automating
the Other/Adjs upload (stays manual by design), pay_generic / cumulative components
(stub streams exist; wire only if a scheme in scope actually uses them).

## 8. Risks and mitigations

1. **Silent zeroes** — missing inputs become 0-valued wages, not errors. → Coverage
   report per run + pre-flight checks + parallel-run diff.
2. **No approval lifecycle on production tables** — jute production entries are
   soft-delete only; numbers can change between prepare and process. → Period
   cutoff/lock concept, or a run warning listing production rows edited after the last
   collection (doc 04 §2F).
3. **Legacy data quality/drift** — DataPortal SQL tolerates quirks (dead updates,
   unscoped writes) that a faithful port must consciously exclude (documented in doc 01
   §3). → Port from this analysis, not by transliterating the PHP.
4. **Advance-balance continuity** — legacy IADV nets prior staging rows; the new vow3
   advances module starts from a one-time opening-balance migration and thereafter nets
   against vow3-collected history; the transition month must not double-count. →
   Cutover checklist item + explicit test.
5. **Constants change** (rates, multipliers renegotiated). → Decided: config tables with
   effective-dates from day one, v1 seeds parity values.
6. **Data-entry completeness** — native sourcing is only as good as daily entry:
   missing production rows, un-mapped machine attendance, or absent stoppage entries
   silently shrink wages. → Per-collector coverage reporting (§3.4), pre-flight checks,
   and the Phase 3 diff double as the adoption monitor (doc 04 §2G).

## 9. Remaining open items

Decided 2026-07-06 (see INDEX.md): masters not migrated yet → Phase 0 migrates them;
direct cutover, no dual-write; FA automated; constants in config tables from day one;
**native jute-production sourcing** (doc 04).

Still open, needed before Phase 0 completes: (1) which tenant (name/shortname) is NJM in
vow3, (2) winding cop rule — keep or retire (doc 04 §2D), (3) layoff + canteen capture
scope (doc 04 §2E), (4) line-hours derivation vs ported upload (doc 04 §2C), (5) weaving
wage unit yards vs meters (doc 04 §2D). Retired: legacy DB grants, weaving view DDL,
runtime employee-id bridge.
