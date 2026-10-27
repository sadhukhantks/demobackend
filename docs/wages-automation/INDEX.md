# NJM Wages Automation — Analysis & Implementation Plan (Index)

**Status:** Analysis note — no code written yet. Produced 2026-07-06 from a full read of
DataPortal, vow_backend_2.0, vow-ui-2.0, vowerp3be and vowerp3ui.

**Goal:** Replace the manual three-system month-end wages chain
(DataPortal *process → download excel* → vow2.0 *upload → process*) with a **single button in
vow3** that makes the attendance/production-derived wage input data available for wages
processing for a chosen pay scheme + pay period. vow3 already owns the final
wages-processing step (`pay_register_create` / `pay_register_process`); the automation fills
the one missing stage: computing the per-employee variable inputs.

## Reading order

| Doc | Contents |
|-----|----------|
| [01-legacy-njm-wages-process.md](01-legacy-njm-wages-process.md) | As-is analysis: DataPortal `Njmwagesprocess::njmwagesprocessdata`, the staging table, the excel file contract, and how vow2.0 ingests and processes it |
| [02-vow3-current-state-and-gap.md](02-vow3-current-state-and-gap.md) | What vow3 already has, the exact input contract of its processing engine, the end-to-end data-contract map, and the precise hole to fill |
| [03-implementation-plan.md](03-implementation-plan.md) | Target architecture (Wage Data Collection Engine), integration options, API/table design at spec level, phased delivery plan, validation strategy, risks |
| [04-native-jute-production-sourcing.md](04-native-jute-production-sourcing.md) | **Revised sourcing decision:** production streams calculate directly from vow3's jute production module (not EMPMILL12/vowsls) — native source map, required changes, simplifications, phase amendments |
| `../../../vowerp3ui/docs/wages-automation-ui-plan.md` | Frontend note: where the single button lives, UX states, API wiring (in the vowerp3ui repo) |

## TL;DR

1. **The hole is exactly one stage.** vow3's payroll engine (`_run_payroll_processing`,
   `src/hrms/payRegister.py:1360`) already computes final wages from scheme formulas +
   per-employee structure, taking all **period-variable inputs** from
   `pay_components_custom`. The DataPortal → excel → vow2.0-upload chain exists only to
   populate the vow2.0 twin of that table (`tbl_pay_components_custom`). Nothing in vow3
   computes those values today — the Pay Roll page's three fetch buttons are explicit
   no-op stubs (`src/hrms/payRoll.py:313-383`).
2. **Approach:** re-implement the `njmwagesprocessdata` aggregation as a native vow3
   **Wage Data Collection Engine** — a set of collectors (attendance, leave/canteen,
   advances, FA, beaming, winding, finishing, weaving, sardar/helper, IFTU) that write
   `pay_components_custom` directly with period-scoped soft-replace, skipping the legacy
   staging table and the excel entirely. The Pay Roll page's stub endpoints were built
   for exactly this.
3. **Sourcing (revised 2026-07-06):** production streams calculate **directly from vow3's
   own jute production module** (`jute_prod_beaming_daily`, `vw_weaving_daily`,
   `jute_prod_winding_doff`, `jute_prod_finishing_daily`, `jute_prod_stoppage_hours`,
   `machine_mst.line_no/mech_shr_code`) — no runtime reads of `EMPMILL12`/`vowsls`.
   Attendance and leave were already native. Decisive prerequisites: a new
   effective-dated **piece-rate master** (none exists anywhere in vow3), completion of
   **worker attribution** on weaving/winding/finishing entries, a line running/lost-hours
   source, and vow3 homes for advances/canteen/FA-rates/layoff. Full map: doc 04.
4. **Validation is mandatory:** parallel-run one real month (legacy chain vs vow3 engine)
   and diff both the collected inputs and the final payroll before cutover — this also
   validates that daily production entry in vow3 is complete, not just that the math is
   right.

## Product-owner decisions (2026-07-06)

Five decisions recorded:

0. **Native jute-production sourcing** — production-derived wage inputs (beaming,
   winding, finishing, weaving, line incentive/sardar-helper) are calculated directly
   from vow3's jute production module, not from `EMPMILL12`/`vowsls`. See doc 04 for the
   required changes. This retires the legacy-grants/weaving-view-DDL open item and the
   runtime employee-id bridge.

1. **NJM masters are NOT yet migrated to vow3** — migrating pay schemes, components,
   formulas and employee-scheme mappings into the NJM tenant DB is the first Phase 0
   work item (vow3 HRMS currently proves out on dev3 test data).
2. **Transition = direct cutover** — no automated dual-write to vow2.0. Once validated
   against one historic month (Phase 3 parallel-run diff), vow3 becomes the only wages
   path.
3. **FA is automated too** — an `fa` collector computes Food Allowance from the
   `tbl_njm_food_allowance` rate master × attendance hours/8; only Other/Adjs remains a
   manual override path.
4. **Constants live in config tables from day one** — effective-dated tenant config
   seeded with parity values (canteen ₹40/meal, sardar ×1.3, base wage 46.15, piece-wage
   cap 1200/208, IFTU ₹60, category/designation ids, loom dept ranges).

## Still open

1. **Which tenant is NJM** in vow3 (name/shortname) — needed before Phase 0 migration.
2. **Winding cop rule:** does the spool-vs-cop distinction (cop qty = net_weight/15,
   separate rate) still exist operationally? vow3 winding has no cop concept — either a
   package-type dimension is added or the rule is retired (doc 04 §2D).
3. **Layoff and canteen:** is dept-wise layoff still practiced, and where should canteen
   consumption be captured? Both need vow3 homes to keep those deductions automated
   (doc 04 §2E).
4. **Line hours:** accept the derived running/lost hours (from spells − stoppage per
   `line_no`) in place of the legacy monthly excel, subject to a parity diff (doc 04 §2C)?
5. **Weaving wage unit:** express weaving piece rates in yards (native) or meters
   (legacy) (doc 04 §2D).

Retired: legacy DB grants + `view_proc_njm_loom_data_for_wages` DDL (superseded by
`vw_weaving_daily`); runtime employee-id bridge (native `eb_id` everywhere — the
eb_no↔emp_code mapping matters only for one-time seeds like advance opening balances).
