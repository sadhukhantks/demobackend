---
name: sync-vowsls-pay-scheme
description: Use when a legacy vowsls pay scheme's per-employee salary structure or scheme components must be brought onto its sls mirror scheme (NJM, co 106 / branch 87) — requests like "update all components value into sls.pay_employee_structure for payscheme 3166 from vowsls scheme 166", "insert missing components to sls", "sync legacy 164 structure to sls", or when an sls pay register prices every employee at the same scheme default (C_RATE_, C_TARATE_, DA) while legacy has per-employee values.
---

# Sync a vowsls pay scheme onto its sls mirror

Two scripts in `vowerp3be/dbqueries/migrations/` already do every read, write, backup, verification
and log entry. This skill is the decision layer: pick the ids, run both dry runs, read their output,
then commit or stop. Don't hand-write SQL for the sync itself.

## Ids

Source `vowsls.tbl_pay_employee_structure` (users misspell it; `tbl_pay_customer_employee_structure`
is unrelated). Target `sls.pay_employee_structure`.

| legacy | sls | name |
|---|---|---|
| 1 | 3001 | NJM 12 Elements (needs `--legacy-branch 4`) |
| 21 | 3021 | NJM14ELE_NO_BEN (needs `--legacy-branch 4`) |
| 23 | 3023 | NJM 12 ELEMENTS & NO GAS (needs `--legacy-branch 4`) |
| 24 | 3024 | NJM14ELE WITH PF,ESI,O_B (needs `--legacy-branch 4`) |
| 96 | 3096 | NJM Blue Force No ESI (needs `--legacy-branch 4`) |
| 163 | 3163 | NJM_CONT_WAGES_NEW |
| 164 | **2015** | njm_main_payroll |
| 166 | 3166 | njm_ofb_clerk |
| 167 | 3167 | njm_retired_payroll |
| 169 | **2017** | njm_newbudli_payroll |
| 174 | 3174 | njm_newbudli_rtd |

Rule: legacy + 3000, except 164 and 169, whose sls schemes pre-existed and were matched by name.
`legacy + 1000`, 15 and 17 are the branch-4 generation — never target them. For an id not in the
table, match `vowsls.tbl_pay_scheme.NAME` (key `ID`) against `sls.pay_scheme_master.payscheme_name`
with `branch_id = 87`; no match means ask. Earlier runs: `vowsls_to_sls_transfer.migration_log`,
steps `pay_structure_<L>_to_<S>` and `pay_scheme_details_<L>_to_<S>`.

## Dry run

From `d:\vownextjs\vowerp3be`, both at once:

```
.venv\Scripts\python.exe dbqueries\migrations\run_copy_structure_vowsls_scheme_to_sls.py --legacy L --sls S --branch 87 --update-existing
.venv\Scripts\python.exe dbqueries\migrations\run_sync_scheme_details_vowsls_to_sls.py --legacy L --sls S
.venv\Scripts\python.exe dbqueries\migrations\run_dedupe_pay_employee_structure.py --sls S
```

Always pass `--update-existing`: with no existing rows it is a plain copy, and without it a re-sync
whose values changed skips the updates and fails its own verification. The structure dry run writes
only its helper map table in `vowsls_to_sls_transfer`, which is no reason to ask first.

**The dry runs are the pre-flight.** Together with the commit's in-transaction verification they
check emp_code uniqueness, branch 87, primary-key collisions, existing / differing / missing pairs,
duplicate (employee, component) rows and every per-employee value. Pre-flight is those two commands
plus one read — whether `vowsls_to_sls_transfer._bak_struct_L_to_S` already exists (Commit step 1).

| Output | Meaning | Action |
|---|---|---|
| `differing 0`, `missing 0`, `sls-only 0`; details `reactivate 0, insert 0` | Already in sync | Report it; no commit. If the user said legacy was revised, say legacy L matches sls and ask which scheme they edited |
| `differing D` / `missing M` > 0 and no row below applies | Values to update, pairs to insert | Commit. The user's "update / sync / insert missing / fix" request is the go-ahead |
| `active rows already under sls S ...: 0 rows` | No per-employee structure, so payroll uses the scheme-detail constants | This is the "everyone on the default rate" cause; the commit fixes it |
| `legacy unmapped [...]` / `target-only [...]` non-empty | Employees assigned on one side only | Not synced. List them in the report; scheme assignment is the user's call, not a blocker |
| `sls-only (left alone)` > 0 | Mapped employees hold components legacy lacks | **Stop.** The commit's per-employee check counts every active sls row, so it will roll back. Ask whether to deactivate those rows (back up, `STATUS = 0` by ID) first |
| `map table: X rows` ≠ `mapped by emp_code Y` | Stale map from an earlier run | `DROP TABLE vowsls_to_sls_transfer._map_struct_L_to_S`, dry-run again |
| `AssertionError: emp_code is not unique on one side` | Legacy holds one person as a co-1 (branch 4) record plus co-106 (branch 87) clones, all assigned to L | Legacy pays one branch: `vowsls.tbl_pay_period.branch_id` for `PAYSCHEME_ID = L`. Clones can carry stale values (96: D002 12400 vs paid 15000) — show the user, then dry-run again with `--legacy-branch <that branch>` |
| Dedupe dry run `rows to deactivate` > 0 | Duplicate active (employee, component) pairs on sls (the many-to-one `_map_pay_eb` merged clones) | The commit's check rolls back on them. Ask, then re-run the dedupe with `--commit` before the structure commit: keeps the lowest ID, aborts if a pair's values differ, backs up to `_bak_dedupe_struct_S` |
| `legacy duplicate active rows skipped (identical copies): N` > 0 | Legacy double-saved some (employee, component) rows with the same values (1: U0249) | Nothing to do — the script keeps the lowest ID. A negative `sls-only` means you are on a script version without this; `AssertionError: legacy holds differing active rows` means the copies disagree: stop and report |
| Other AssertionError, or `target PKs taken by another scheme` > 0 | Employee off branch 87, or a row that must be inserted has its ID taken | Stop and report. The guard counts only rows to insert: AUTO_INCREMENT sits inside +20M, so other schemes' UI saves own offset IDs of pairs that already exist |
| Details `reactivate` / `insert` > 0, `ABORT`, or active-on-sls-only warning | Scheme component set differs | The engine ignores structure rows of components inactive in the scheme. Ask, then re-run the details script with `--commit`. Never `--overwrite-kept-defaults`: it zeroes the hand-tuned NJM rate constants |
| `FORMULA (whitespace only)` | Trailing newline | Ignore |

## Commit

1. If `_bak_struct_L_to_S` exists from an earlier run, `RENAME TABLE` it to `_bak_struct_L_to_S_<yyyymmdd>`.
   The script fills it with `CREATE TABLE IF NOT EXISTS` + `INSERT IGNORE`, so an old backup row
   would win over this run's before-value and a rollback would restore the wrong amount.
2. Save the rollback baseline: `SELECT ID FROM sls.pay_employee_structure WHERE PAYSCHEME_ID = S AND
   ID BETWEEN 20000000 AND 29999999` to the scratchpad. That range already holds earlier copies, and
   AUTO_INCREMENT sits inside it, so new UI saves land there too — the docstring's range DELETE is
   not a safe rollback.
3. Same structure command plus `--commit` (run it in the background for thousands of rows). It backs
   up overwritten rows, updates by ID, inserts at legacy ID + 20,000,000, verifies inside the
   transaction, rolls back on any mismatch and logs on success. Pass means `COMMITTED`,
   `inserted` = dry-run missing, `updated` = dry-run differing.
4. Immediately save the inserted IDs: the same range query minus the baseline, keeping only IDs where
   `ID - 20000000` is a legacy L row ID (a UI save may have landed in between). Count = `inserted`.
5. `VERIFICATION FAILED` with `inserted` below the dry-run missing: some target IDs already exist at
   `STATUS = 0` — an sls UI re-save deactivated the copied rows and `INSERT IGNORE` skipped them.
   Nothing was kept; report it and ask.
6. The INSERT…SELECT share-locks the vowsls rows it scans until commit, so legacy structure saves wait
   meanwhile. If it hangs, `KILL QUERY <id>` after checking the query text — killing the client does
   not stop the statement.

## Report

Counts (updated, inserted, backed up), unmapped employees, the details result, and the follow-up:
**the pay register for S must be re-processed** (Create Pay Register, co 106 / branch 87) because
processed payroll keeps the old values. Per-period uploads in `pay_components_custom` still override
structure values.

## Rollback

- Updates: `UPDATE sls.pay_employee_structure s JOIN vowsls_to_sls_transfer._bak_struct_L_to_S b
  ON b.ID = s.ID SET ...` (primary key on both sides; the script docstring has the full statement).
- Inserts: `DELETE ... WHERE ID IN (<IDs saved in commit step 4>)`, in chunks. Then re-process the register.

## Never

- A hand-written multi-table `UPDATE … JOIN` matched on EMPLOYEEID/COMPONENT_ID against
  `sls.pay_employee_structure`. Both structure tables have only `PRIMARY(ID)`; one such statement ran
  106 minutes locking 4.8M rows of live payroll. Match in SELECTs, write by ID — the script does.
- Target `legacy + 1000` or any branch-4 scheme.
