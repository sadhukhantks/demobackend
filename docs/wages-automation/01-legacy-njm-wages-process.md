# 01 — Legacy NJM Wages Process (As-Is): DataPortal → Excel → vow2.0

This documents exactly what happens today, so the vow3 automation can replicate it
faithfully. All paths are relative to the sibling repos (`../DataPortal`,
`../vow_backend_2.0`, `../vow-ui-2.0`).

## 1. Systems and databases

| System | Tech | Database(s) |
|--------|------|-------------|
| DataPortal | CodeIgniter 3 (PHP), raw string-interpolated SQL | Connects to `vowsls` (the vow2.0 HRMS/tenant DB); cross-schema queries into `EMPMILL12` (legacy mill DB) on the **same MySQL server** |
| vow2.0 backend | Java Spring multi-service (`sls-parent/*`) | `vowsls` (live payroll = `tbl_pay_*` tables) + `EMPMILL12` |
| vow3 | FastAPI + tenant DBs | Same MySQL server (see `env/database.env`) — so vow3 can technically reach `vowsls`/`EMPMILL12` with grants |

Key point: `vowsls` and `EMPMILL12` are co-located with the vow3 tenant DBs on one MySQL
instance. Every cross-schema join DataPortal does is reproducible from vow3.

## 2. The DataPortal page and monthly journey

Page: **"Holiday/Attendance Incentive Data"** — served by
`application/controllers/Data_entry.php:1071`, view
`application/views/other_data_entry/holiday_att_incn_process.php`. The **"NJM Wages Data →
Process"** button opens the "Njm Wages Data Preparation1" modal
(`application/views/modals/ejm_stl_modal.php:190-369`).

The pay scheme selector (`#att_payschm`, populated from `vowsls.tbl_pay_scheme` via
`Varaha_model::getAllPayschemes()`) and the period pickers on the *main page* drive every
modal action (the modal's own multi-select is only used by the staff bank sheet).

Month-end sequence a user actually performs:

1. Select period (to-date auto-snaps to month-end) + pay scheme; open the NJM modal.
2. **Upload Line Hours** excel → `EMPMILL12.tbl_run_loom_line_hours`
   (`Njmwagesprocess::wrklinehoursupload`, controller lines 2027-2177; positional columns
   `date_from, date_to, line_no, sard_helper(S/H), running_hours, lost_hours, wgroup,
   no_of_looms`; whole file rejected if row-1 dates ≠ selected period).
3. Run checklists: **Attendance With PaySchemes** (xlsx — flags workers with attendance but
   no scheme mapping, missing PF/ESI) and **Line Hours Checklist** (PDF — sanity-checks
   the sardar/helper rate denominator).
4. **Process** → `POST Njmwagesprocess/njmwagesprocessdata` (controller line 4041 →
   model `Njmallwagesprocess::njmwagesprocessdata`, lines 996-1612). See §3.
5. Review **Production Checklist** (per-quality production/rates captured) and download the
   **FA statement** (Food Allowance, computed from `EMPMILL12.tbl_njm_food_allowance`
   rates × attendance hours/8).
6. **Upload FA File** (header-keyed `TICKET_NO`,`AMOUNT` → staging rows `update_for='FA'`,
   value into `other_pay`; all-or-nothing on unmatched ticket numbers) and **Upload
   Other/Adjs File** (header-keyed `EB_NO` + ~27 `C_*` override columns → `update_for='OTH'`,
   manual arrears/advances/misc overrides). Run **Cad/Vard/GWF Update**
   (`njmwrkvardupdate`, model 1616-1665 → one row per employee with period hours > 0,
   `update_for='V'`, flat `iftu_amount=60` union dues).
7. **Download the payroll excel** (`Data_entry/njmwagesexceldownload`,
   `Data_entry.php:382-626`) → `payroll_{payscheme}_{todate}.xlsx` — **this is the file
   manually uploaded into vow2.0**. See §5.

## 3. What `njmwagesprocessdata` computes

Everything lands in one staging table, **`EMPMILL12.tbl_njm_wages_data_collection`** — one
row per `(eb_id, date_from, date_to, payscheme_id, update_for)`, soft-deleted (`is_active=0`)
and re-inserted per run. The run first deactivates all prior rows for the period+scheme
**except** `update_for IN ('V','FA','OTH')` (manual/flat rows survive re-processing).

Population scope on *every* insert: employee must be on the scheme
(`vowsls.tbl_pay_employee_payscheme.PAY_SCHEME_ID = :att_payschm AND STATUS = 1`); the
attendance/leave blocks additionally filter worker categories
`worker_master.cata_id IN (15,16,17,20,21)`; `company_id = 1` is hardcoded throughout.
The pay scheme is purely a **population filter** — there are zero scheme-specific
computation branches.

| `update_for` | Source tables | Computation → staging columns |
|---|---|---|
| `ATT` | `vowsls.daily_attendance` + `designation.time_piece/piece_rate_type` − `daily_layoff_deptwise` | Pivot per employee: time-rated regular hrs → `hours_wkd_1` (`time_piece='T'`, `piece_rate_type<>2`) / `hours_wkd_2` (`piece_rate_type=2`); piece-rated regular hrs → `piece_hours`; OT hrs → `extra_hours_t`/`extra_hours_p`; C-spell night shifts `(working−idle)/8` → `c_shift_days`; distinct 'R' days → `esi_days`. Layoff hours subtracted per dept/date/spell |
| `IADV` | `EMPMILL12.tbl_company_advance` | Remaining balance = advance − Σ prior active `IADV` deductions; inserts `installment_amount` → `advance` (+`advance_date`) where balance > 0 and the worker attended in the period |
| `LEAVE` | `tbl_hrms_holiday_transactions`+`holiday_master`, `leave_tran_details`+`leave_transactions`+`leave_types` (status=3 approved), `canteen_details` | `festival_hours` (holiday hrs), `sl_days` (codes S/C), `ul_days` (A/U), `el_days` (L), `canteen` = meals × **₹40**, `esi_days` = festival/8 + EL |
| `BEAM` | `beaming_daily_production` + `EMPMILL12.beaming_weaving_quality_master` (maps `quality_code` → `beam_wage_code`) + `daily_ebmc_attendance` (worker↔machine on date+spell+mc) + `EMPMILL12.tbl_wages_quality_master` (rates) | cuts × rate1 → `act_prod_amount`; `production`/`prod_balance` = cuts; `qualcode`, `qual_rate` |
| `WIND` | `winding_doff_entry` + `yarn_type_master` | Spool: prod = net_weight (qualcode 5401); Cop: net_weight/**15** (qualcode 6501); wage = prod × rate1 |
| `FNG` | `finishing_entries` + `process_type_master` (`work_type IN (13,14,21,18,19,22,20)`) | Σ production × rate1 grouped by employee + process_code; requires matching attendance on date+spell |
| `WEAV` | **`EMPMILL12.view_proc_njm_loom_data_for_wages`** (DB view — DDL not in any repo) | `production` = Σ meters, `qual_rate` = weighted avg, `act_prod_amount` = Σ(production × rate1), `loom_production` |
| line incentive | `cuts_jugar_buff_1` (5 shift columns unpivoted) + `mechine_master` (reed_speed bands → speed 168/162.5/144) + weaver attendance (designation 1113) | Per line+shift-group pot: target `tg=((spd×hrs×60)/(shots×36))×.65`, base wage **46.15**; UPDATEs `tbl_run_loom_line_hours.quantity,pamount` |
| `SH` | `tbl_run_loom_line_hours` pot + attendance of sardars (designations 1115, 1005) / helpers (1114) | rate = `(pamount/((running−lost)×30))×2`, **×1.3 for sardar**; amt = rate × worked hrs → `sardhelp_amt`; loom-line groups: dept_code 029-033→A, 034-038→B, 039-043→C |
| `V` | separate button (`njmwrkvardupdate`) | Flat `iftu_amount=60` per employee with period hours > 0 (attendance + 8/leave day + 8/holiday) |
| `FA` / `OTH` | manual excel uploads (§2 step 6) | FA → `other_pay`; OTH → ~27 override columns |

Magic constants to carry into vow3 config: company_id=1; canteen ₹40/meal; sardar ×1.3;
SH denominators ×30/×2; base wage 46.15; target factor 0.65; loom-meter floor 120; cop
divisor 15; qualcodes 5401/6501; designations 1005/1114/1115 (sardar/helper), 1113
(weaver); worker categories (15,16,17,20,21); loom dept codes '029'–'043'.

### Known quirks — do NOT port these

- HELP/SARD insert blocks at model lines 1325-1408 are **dead** (query calls commented out);
  what survives is two **unscoped** `UPDATE ... SET upd_type=2/3 WHERE update_for='SARD'/'HELP'`
  with no period filter (lines 1410-1413).
- A first-draft IADV query with a hardcoded `advance_date='2026-01-05'` (1079-1090) is
  overwritten before execution.
- `njmwrkfaupload` (controller 2182-2367) is a dead legacy positional FA upload with a
  hardcoded `PAY_SCHEME_ID=169` — the live one is `njmwrkfauploadjs`.
- Checklist dropdown options 2/5/6 have no handler.
- All SQL is string-interpolated (injection-prone) and non-transactional. The vow3 engine
  must use bound parameters and wrap each run in a transaction.

## 4. The excel file contract (DataPortal → vow2.0 handoff)

`payroll_{att_payschm}_{periodtodate}.xlsx` (`Data_entry.php:382-626`):

- Sheet 1 `Template`; row 1 headers: `Employee Id` (A, eb_no as text), `Employee Name` (B),
  `PayScheme` (C, scheme NAME), then **one column per pay component from D onward, dynamic
  per scheme**.
- The column set and their values are driven by
  **`EMPMILL12.tbl_wages_data_payschm_map_table`** (filtered by `payscheme_id`) joined to
  `tbl_pay_components`: header = `tbl_pay_components.CODE`, cell value = the staging
  aggregate named in `data_column_no`. **The metric → component mapping already exists as
  data** — this is the Rosetta stone the vow3 engine should import/reuse.
- Available aggregate aliases (from `tbl_njm_wages_data_collection` where `is_active=1`):
  `hours_wkd_1, hours_wkd_2, esi_days, el_days, sl_days, lay_off_hrs, piece_hours, canteen,
  sunday_adv, other_adv, el_advance, adv_ded, installment_advance, extra_hours_t,
  extra_hours_p, c_shift_days, festival_hours, arrear_plus, arrear_minus, other_pay,
  cl_days, prod_balance, ul_days, advance, act_prod_amount, loom_production, iltime_hrs,
  ilpiece_hrs, sardhelp_rate, minus_bal, cadeligibility(=0), gwf_eleg(=0), iftu_eleg,
  piece_wages, piece_wages_inc`.
- Derived in the download itself (`Data_entry.php:474-515`): `piece_wages` is **capped at
  rate 1200/208 per (piece_hours + extra_hours_p)**, the excess split into
  `piece_wages_inc`. One row = one active employee on the scheme
  (`tbl_pay_employee_payscheme.STATUS=1` + `worker_master.active='Y'`).

## 5. vow2.0 ingestion and processing (live path: `sls-payroll-service`)

vow2.0 has two payroll generations; the **live** one is `sls-payroll-service` over the
`tbl_pay_*` tables. (`sls-vowpay-service` / `TBL_PAYSCHEME_MASTER` / `PayRollProceesing`
is the older generation — JSON-only, no file upload, not part of this chain.)

1. **Upload** — UI page "Upload Payroll Data" (`/data_collection`,
   `vow-ui-2.0/src/Pages/HR_Management/PayRollData/index.js`) →
   `POST /PayProcess/processDatacollectionSheet/{fromDate}/{toDate}/{companyId}/{createdBy}`
   multipart `payrollInputSheet`
   (`sls-payroll-service/.../controller/PayProcessController.java:226`).
   Parsing (`PayProcessServiceImpl.processSheet`, lines 1427-1540, Apache POI): row 0 cells
   3+ **must be `tbl_pay_components.CODE` values** (unknown code aborts the upload); data
   cells must be numeric (any failure aborts); rows keyed by eb_no in col 0 — **unknown
   eb_no rows are skipped with a log**, not rejected. Prior rows for period+employees are
   soft-inactivated (`STATUS=0`) — last upload wins.
2. **Landing table** — `tbl_pay_components_custom` (`EMPLOYEEID, COMPONENT_ID, VALUE,
   FROM_DATE, TO_DATE, STATUS=1`), one row per employee × component.
3. **Processing** — creating a pay period (`PayPeriod/addPayPeriod`) auto-triggers
   `POST /PayProcess/uploadOrPayProcessPay/{paySchemeId}/{payPeriodId}` (frontend saga):
   merges scheme formulas (`tbl_pay_scheme_details`) ← per-employee fixed amounts
   (`tbl_pay_employee_structure`) ← custom components (custom values **override**
   formulas), evaluates formulas iteratively via a JS engine with per-component rounding,
   and writes one row per employee × component into **`tbl_pay_employee_payroll`**.
   Period status: 30 (data collected) → 28 (processed) → 3 approve / 4 reject.
4. vow2.0 also has its own native collector precedent:
   `GET /PayProcess/processDatacollectionFromDailyAttendance/...` computes
   worked-days/hours/OT/leaves from `daily_attendance` into the same
   `tbl_pay_components_custom` — the "Fetch Attendance" button on the upload page. This is
   the pattern vow3 generalizes.

## 6. Why the chain is shaped this way (and what that means for vow3)

The staging table + excel exist only because the aggregation logic lives in a separate app
(DataPortal) from the wage calculator (vow2.0). The excel's entire information content is
`(employee, pay-component CODE, numeric value)` for a period+scheme — exactly one
INSERT-shaped payload. vow3 owns both ends (raw data reachability *and* the calculator),
so the automation can collapse the staging table, the excel, and the upload into a single
in-DB computation that writes `pay_components_custom` directly.
