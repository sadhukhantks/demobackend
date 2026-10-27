# 02 — vow3 Current State and the Gap

## 1. What vow3 already has (all portal persona, `/api/hrms`, tenant DB)

### Wages processing — DONE (`src/hrms/payRegister.py`)

| Endpoint | Purpose |
|---|---|
| `GET /pay_register_list`, `/pay_register_by_id/{id}`, `/pay_register_create_setup` | Period list / detail / dropdowns |
| `POST /pay_register_create` | Creates `pay_period` (status 1) and **immediately runs processing** |
| `POST /pay_register_process` | Re-runs `_run_payroll_processing` for a period |
| `PUT /pay_register_update/{id}` | Status changes (approve 3 / reject 4 / cancel 6) |
| `GET /pay_register_salary` / `_export` / `_payslips` | Pivoted grid / styled xlsx / payslip PDFs |

`_run_payroll_processing` (payRegister.py:1360-1588): mapped employees → scheme formulas →
component meta → per-employee overrides → deletes old output → iterative/topological
formula engine (handles legacy JS-style formulas) → bulk insert into
`pay_employee_payroll` + `pay_employee_payperiod` → `pay_period.STATUS=28` (PROCESSED;
note `docs/hrms-payroll-design.md` says 32 but code uses 28 — 32 is LOCKED,
payRegister.py:954).

### Variable-input capture — the seam (`src/hrms/payRoll.py`)

| Endpoint | Purpose |
|---|---|
| `GET /pay_roll_setup`, `POST /pay_roll_data` | Filter dropdowns; grid of employees × custom (TYPE=0, IS_EXCEL_DOWNLOADABLE=1) components with existing values |
| `POST /pay_roll_save` | Manual grid save → soft-replace `pay_components_custom` |
| `POST /pay_roll_download_template`, `POST /pay_roll_upload` | Blank per-scheme excel; upload maps emp_code→eb_id and CODE→component_id, writes `pay_components_custom`, returns `records_saved` + `skipped_employee_codes` |
| `GET /pay_roll_fetch_attendance`, `_generic`, `_cumulative` | **STUBS** — return ack payloads with `records_updated: 0`. Comment at payRoll.py:313-318: the backend "doesn't own those computations yet". **These are the intended hook points for this automation.** |

### Data model (tenant DB, `src/models/hrms.py`) — mirrors vow2.0's live payroll

`pay_scheme_master` + `pay_scheme_details` (formulas), `pay_components` (CODE, TYPE 0-3,
IS_CUSTOM_COMPONENT, IS_EXCEL_DOWNLOADABLE, IS_COMMULATIVE, ROUNDOF; must be STATUS=3
approved), `pay_employee_payscheme` (STATUS=1 membership), `pay_employee_structure`
(fixed per-employee amounts), **`pay_components_custom`** (COMPONENT_ID, VALUE,
EMPLOYEEID, PAY_PERIOD_ID, FROM_DATE, TO_DATE, STATUS), `pay_period`, outputs
`pay_employee_payroll` / `pay_employee_payperiod`. Employees:
`hrms_ed_personal_details` / `hrms_ed_official_details` (has `emp_code` and
`legacy_code`).

Modeled but unused (legacy-shaped): `pay_generic`, `pay_external_components`,
`pay_processed_payscheme`, `pay_slip*` — the cumulative component fields on
`pay_components` also have no producer/consumer yet.

### Native source capture in vow3 today

| Source | Status |
|---|---|
| Daily attendance | **Native** — `daily_attendance` with approval workflow (`src/hrms/attendance.py`) + mobile app entry; machine link `daily_ebmc_attendance` |
| Leave | **Native** — `leave_transactions` / `leave_ledger_tran` |
| Spinning production | Partial — `daily_doff_tbl` with operator stamping from attendance |
| Winding / finishing | Parallel capture exists (`jute_prod_winding_doff`, `finishing_entry`) but schemas differ from legacy and carry no wage-rate logic |
| Beaming, weaving/loom cuts, line hours, advances, canteen, layoff, piece-rate quality master, FA rate master | **No vow3 home at all** |

**Crucially: the payroll engine reads none of these.** No query in `src/hrms/` touches
`daily_attendance`, `daily_doff_tbl` or `leave_*` for wage computation.

## 2. The input contract (what "data available for processing" means)

For `_run_payroll_processing` to produce real wages for a scheme + period, the tenant DB
needs (query refs in `src/hrms/query.py`):

1. `pay_scheme_master` row + `pay_scheme_details` formulas (400 if none)
2. Approved `pay_components` (STATUS=3) — CODE is the formula identifier;
   IS_EXCEL_DOWNLOADABLE=1 marks the period-variable inputs
3. `pay_employee_payscheme` STATUS=1 rows joined to active employees (400 if none)
4. `pay_employee_structure` fixed overrides (optional)
5. **`pay_components_custom` rows for the period** — value precedence in
   payRegister.py:1478-1498: IS_EXCEL_DOWNLOADABLE components take the custom value **or 0
   if absent** → other custom rows → structure override → scheme formula. Matched by
   `PAY_PERIOD_ID` or exact FROM_DATE/TO_DATE
6. A `pay_period` row (pay_register_create makes it)

Item 5 is the entire gap. Items 1–4 are setup/master data (migration questions, not
engineering gaps). Item 6 exists.

## 3. End-to-end data-contract map (legacy chain → vow3 home)

| Legacy source (`vowsls`/`EMPMILL12`) | Staging alias | vow3 landing | vow3 source available natively? |
|---|---|---|---|
| `daily_attendance` + `designation.time_piece/piece_rate_type` − `daily_layoff_deptwise` | `hours_wkd_1/2, piece_hours, extra_hours_t/p, c_shift_days, esi_days` | `pay_components_custom` (component by CODE) | Attendance **yes** (`daily_attendance`, `designation_mst.time_piece/piece_rate_type` exist); layoff table **no** |
| `tbl_company_advance` | `advance, advance_date, installment_advance` | same | **No** advance/loan table in vow3 |
| Holiday txn + leave + `canteen_details` | `festival_hours, sl_days, ul_days, el_days, canteen, esi_days` | same | Leave **yes**; holiday partial; canteen **no** |
| `beaming_daily_production` (+ machine attendance + quality rates) | `production, qual_rate, act_prod_amount` | same | **No** beaming capture |
| `winding_doff_entry` + `yarn_type_master` | same | same | Parallel table `jute_prod_winding_doff` (different schema, no rate join) |
| `finishing_entries` + `process_type_master` | same | same | Parallel `finishing_entry` module (no wage-rate logic) |
| `view_proc_njm_loom_data_for_wages` (DB view, DDL unknown) | `production, qual_rate, act_prod_amount, loom_production` | same | **No** loom/weaving production |
| `cuts_jugar_buff_1` + `mechine_master` + `tbl_run_loom_line_hours` | `sardhelp_amt, lineno, linehrs, qual_rate` | same | **No** cuts entry / line hours / pot logic |
| flat ₹60 (`njmwrkvardupdate`) | `iftu_amount` → `iftu_eleg` | same | Trivially computable |
| FA excel (rates in `tbl_njm_food_allowance`) | `other_pay` | same | **No** FA rate master; upload path exists |
| OTH excel (~27 `C_*` overrides) | 1:1 override aliases | same | **Equivalent exists** — `/pay_roll_upload` / `/pay_roll_save` |
| Population: `tbl_pay_employee_payscheme` STATUS=1 + `worker_master.cata_id IN (15,16,17,20,21)` | scoping | `pay_employee_payscheme` (exists) | Employee bridge needed: legacy `eb_no` ↔ `hrms_ed_official_details.emp_code`/`legacy_code` — **nothing implements it yet** |
| Metric→component map: `tbl_wages_data_payschm_map_table` + `tbl_pay_components.CODE` | column layout | — | **No vow3 counterpart** — must be imported/recreated |

`tbl_njm_wages_data_collection` is referenced nowhere in vowerp3be (grep-confirmed).

## 4. Precise statement of the hole

The single button must, for a chosen **(pay scheme, pay period, branch)** in the vow3
tenant DB, produce:

> one `pay_components_custom` row per *(employee mapped via `pay_employee_payscheme`
> STATUS=1)* × *(each input pay component the scheme's formulas reference)*, VALUE = the
> aggregate the DataPortal chain computes (hours by time/piece/OT class, leave/holiday
> days, canteen deduction, advance installment, piece-production wage amounts,
> sardar/helper share, IFTU flag), with period-scoped soft-replace and `PAY_PERIOD_ID`
> set **explicitly**.

Then `pay_register_create` / `pay_register_process` already do the rest — there is no
other gap on the vow3 side.

## 5. Implementation cautions found during analysis

- **Bind quirk:** `pay_roll_save` / `pay_roll_upload` pass `co_id` into the `branch_id`
  bind of `find_pay_period_by_dates` (payRoll.py:251, 547; query.py:998). The engine must
  set `PAY_PERIOD_ID` explicitly rather than relying on date/branch resolution (the
  dates-fallback in `get_custom_component_values` currently rescues NULL-period rows).
- **Preserve-manual semantics:** DataPortal's re-run preserves `V`/`FA`/`OTH` rows. vow3's
  `pay_components_custom` has no source marker — the engine needs one (see plan §4) so an
  automated re-collect doesn't wipe manually keyed/uploaded adjustments, and vice versa.
- **Zero-default behavior:** absent custom values evaluate as 0 for IS_EXCEL_DOWNLOADABLE
  components — silent under-collection produces silently wrong (not failed) wages. The
  engine must report per-collector coverage, not just succeed.
- Legacy identifiers hardcoded in DataPortal (designation ids, category ids, dept-code
  ranges) refer to **legacy** master rows; vow3 masters have their own ids. Config must
  map them per tenant, not copy the numbers.
