# vowsls â†’ sls Data Migration Guide

Reusable guidelines for migrating tenant data from the **legacy** `vowsls` database (older software, `tbl_*` schema) to the **current** `sls` database (new schema, no `tbl_` prefix).

This file evolves as new tables are migrated. Add a row to the **Migration Log** at the bottom for every table you migrate.

---

## 1. Pre-flight checklist

Before touching a new table, do these in order:

1. **Compare schemas** â€” run `SHOW CREATE TABLE` on both legacy and new tables. Diff column-by-column.
2. **Profile source data** â€” `COUNT(*)`, distinct values for enum/status columns, NULL distribution, range checks (min/max IDs, dates).
3. **Identify dropped columns** â€” confirm with the product owner whether each dropped column is (a) absorbed elsewhere, (b) replaced by a derivation, (c) audit data handled by triggers/log tables, or (d) truly discarded.
4. **Identify added columns** â€” for every NOT NULL added column, define a backfill rule.
5. **Verify lookup-table parity** â€” see Â§2 below. **Never assume IDs are aligned.**
6. **Confirm scope filters** â€” usually `is_active = 1` (skip soft-deleted). Confirm with user.
7. **Plan PK preservation** â€” downstream FK tables (e.g., `proc_indent_dtl.indent_id`) depend on legacy IDs. Preserve the source PK on insert unless explicitly told otherwise.
8. **Define rollback** â€” write the `DELETE` statement that reverses the migration before running it.

---

## 2. Lookup table parity

### `status_master` (vowsls) â†” `status_mst` (sls)

| Aspect | vowsls.status_master | sls.status_mst |
|--------|----------------------|----------------|
| PK | `status_id bigint` | `status_id int` |
| AUTO_INCREMENT | 48 | 48 |
| Extra columns | `company_id`, `jute`, `store`, `auto_datetime_insert` | `status_grp` |
| Charset | latin1 | utf8mb4 |

**Verified ID alignment (1â€“47):** rows match by `status_id` and `status_name` for all entries actually used in transactions.

**Known semantic shifts (do not break migrations because unused in real data):**
| status_id | vowsls name | sls name |
|-----------|-------------|----------|
| 2 | INPROGRESS | DRAFT -1 |
| 20 | PENDING APPROVAL LEVEL 5 | PENDING APPROVAL LEVEL |

**Approval-level collapse rule (applies wherever a header has approval state):**
| Legacy status_id | New `status_id` | New `approval_level` |
|------------------|-----------------|----------------------|
| 17 (PENDING APPROVAL LEVEL 2) | 20 | 2 |
| 18 (PENDING APPROVAL LEVEL 3) | 20 | 3 |
| 19 (PENDING APPROVAL LEVEL 4) | 20 | 4 |
| 20 (PENDING APPROVAL LEVEL 5) | 20 | 5 |
| any other | passthrough | NULL |

Always verify which of 17â€“20 actually appear in the source table; for `tbl_proc_indent` only 17 was present.

### `branch_master` (vowsls) â†” `branch_mst` (sls)

`branch_id` values **are aligned** between vowsls and sls (confirmed by user).

- Single-branch companies: passthrough.
- Multi-branch companies in legacy data (currently companies 1, 2): branch IDs already aligned to the correct factory branch in sls. Pass through.
- **Default: passthrough `branch` â†’ `branch_id`.** Only remap if the user instructs otherwise for a specific table.

### `scm_indent_type_master` (vowsls) â†” `expense_type_mst` (sls)

`indent_type_id` (legacy `category` column) values map directly to `expense_type_id` in the new schema. **Type-master IDs are aligned** (confirmed by user). Use direct passthrough.

### `indent_type_id` literal codes (sls)

The new `proc_indent.indent_type_id` is `varchar(25)`. Allowed literal values:

| Code | Maps from legacy `record_type` |
|------|--------------------------------|
| `Regular` | `INDENT`, `BOQ`, `BOM` (all non-Open variants in legacy data) |
| `Open` | `OPENINDENT` |
| `BOM` | (BOM did not exist in legacy data; reserved for new entries) |

Note: legacy `record_type='BOM'` (only 2 rows in vowsls) is mapped to `Regular` per confirmation, because legacy BOM was not the same concept as new-schema BOM.

### Employee ids (`eb_id`) â€” use a map table, never identity

`eb_id` is **not** universally preserved between vowsls and sls. Four HRMS migrations built the
sls employee set, and the co 1 â†’ co 106 remap moved ~4.4k employees onto new eb_ids. Worse,
~1,900 people exist **twice** in vowsls (once under co 1, once under co 106, same `emp_code`)
but only once in sls â€” so the legacyâ†’sls eb relation is many-to-one, not one-to-one.

`sls._map_pay_eb` (built by `run_migrate_vowsls_payscheme_to_sls.py`, 26,881 rows) is the
complete map. Resolution priority, highest first:

| Rule | Basis | Rows |
|------|-------|------|
| `r1_hrms_map` | existing `sls._map_hrms_eb_smallco` row | 8,682 |
| `r2_code_branch` | trimmed `emp_code` + `branch_id`, unique hit | 16,336 |
| `r3_code_br_nam` | `emp_code` + `branch_id` + first name (ties) | 0 |
| `r4_code_global` | trimmed `emp_code`, globally unique hit | 1,831 |
| `r5_identity` | same `eb_id` in sls **and** first name matches | 12 |
| `unresolved` | `new_eb_id IS NULL` â€” pay rows referencing these are skipped | 20 |

`merged = 1` marks a `new_eb_id` claimed by more than one legacy eb (3,830 rows). Any
per-employee table migrated afterwards must **dedupe on the mapped eb**, not the legacy one.

Plain `emp_code` matching without the branch is unsafe: 2,785 emp_codes are duplicated across
sls branches (only 2 within a single branch).

### Other masters (verify before using)

`company_master`, `customer_master`, `tbl_proc_project`, `tbl_proc_projects_phase`, `dept_master`, `user_master` â€” **not yet verified for ID alignment**. When migrating any table that references these, dump both source and target masters first and confirm with user before assuming passthrough.

---

## 3. Standard column transformations

Apply these consistently across all tables.

### Active flag
| Legacy | New | Action |
|--------|-----|--------|
| `is_active int` | `active tinyint(1)` | Filter source rows on `is_active = 1`. New rows get `active = 1`. |

### Audit columns (legacy, all dropped in new)
- `created_by`, `created_date`, `last_modified_by`, `last_modified_date`, `last_update`
- `approved_by`, `approved_date`, `rejected_by`, `rejected_date`, `rejected_reasons`

**Rule:** drop. New schema relies on database triggers (CLAUDE.md Â§"Audit logging"). Do not migrate audit data unless explicitly asked.

### `updated_by` / `updated_date_time` (new schema, NOT NULL)
- `updated_date_time`: omit from INSERT; column has `DEFAULT CURRENT_TIMESTAMP`.
- `updated_by`: insert sentinel `0`. No FK constraint exists; safe placeholder. Do not attempt to map legacy user strings.

### Document numbers
Legacy holds formatted strings like `'LC/FACTORY/26-27/000003'` or `'M_64'`.
New schema typically holds plain int (`indent_no`, etc.).

**Extraction rule:**
```sql
CAST(COALESCE(REGEXP_SUBSTR(legacy_seq_no, '[0-9]+$'), '0') AS UNSIGNED)
```
Handles both slash-prefixed and underscore-prefixed legacy formats.

### Remarks concatenation
Legacy often has both `remarks` and `internal_remarks`. New schema usually keeps only `remarks`.

```sql
NULLIF(
    TRIM(CONCAT(
        COALESCE(remarks, ''),
        CASE
            WHEN internal_remarks IS NOT NULL AND internal_remarks <> ''
            THEN CONCAT(
                CASE WHEN COALESCE(remarks, '') <> '' THEN ' ' ELSE '' END,
                '[internal: ', internal_remarks, ']'
            )
            ELSE ''
        END
    )),
    ''
) AS remarks
```

### Empty strings â†’ NULL
Legacy stores empty strings (`''`) in many varchar columns; new schema prefers NULL. Wrap with `NULLIF(col, '')`.

### Charset/collation
Legacy: latin1. New: utf8mb4_0900_ai_ci. INSERTâ€¦SELECT handles conversion automatically; no manual encoding step needed.

### Deterministic PK offsets (when the legacy PK cannot be preserved)

When the target table already holds sls-native rows, the legacy PK cannot be reused. Instead of
letting AUTO_INCREMENT assign ids, insert `legacy_pk + OFFSET`, with OFFSET chosen above the
current sls `MAX(id)` and below the column's max. This buys three things at once:

- **restartable** â€” `INSERT IGNORE` re-runs skip whatever already landed, so a 26M-row copy can
  be chunked by legacy id range and resumed after any interruption;
- **reversible** â€” rollback is `DELETE FROM <target> WHERE <pk> > OFFSET`;
- **traceable** â€” `new_id - OFFSET` is the legacy id, no lookup table needed.

Offsets used by the payroll migration: scheme +1,000 Â· scheme_details +100,000 Â· period +100,000 Â·
structure +10,000,000 Â· components_custom +100,000,000 Â· payroll +100,000,000.

### `BUSINESSUNIT_ID` means COMPANY in vowsls, BRANCH in sls

Do **not** pass this column through. Legacy `tbl_pay_*.BUSINESSUNIT_ID` holds a **company** id
(1, 2, 3, 46, 106â€¦); every sls consumer reads it as a **branch** id â€” e.g.
`get_pay_register_salary` filters `ep.BUSINESSUNIT_ID = :branch_id`. The values overlap
numerically (company 3 vs branch 3), so nothing errors: the filter just silently returns the
wrong rows or none at all.

Derive it instead, from the row's own pay period:

```sql
UPDATE sls.pay_employee_payroll r
  JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID
   SET r.BUSINESSUNIT_ID = p.branch_id
 WHERE p.branch_id IS NOT NULL;
```

Leave rows whose period has no branch alone rather than nulling them â€” nulling destroys the
legacy company id and buys nothing. Check any other `BUSINESSUNIT_ID` column the same way.

### `PAYSCHEME_ID` is a four-table invariant â€” never re-point it in isolation

If you change a scheme id anywhere, you must change it in all four places for the same employees.
The backend reads them as a set:

| Where | Why it breaks |
|-------|---------------|
| `pay_employee_payscheme.PAY_SCHEME_ID` | the employee's current scheme |
| `pay_employee_structure.PAYSCHEME_ID` | `query.py:1119` joins `pes.PAYSCHEME_ID = eps.PAY_SCHEME_ID` â€” a mismatch silently drops the employee's salary structure and every component falls back to the scheme default |
| `pay_period.PAYSCHEME_ID` | period lookup and the payroll join (`query.py:622`) |
| `pay_employee_payroll.PAYSCHEME_ID` | `query.py:895` deletes by `(PAYPERIOD_ID, PAYSCHEME_ID)` before regenerating â€” a mismatch misses the old rows and **doubles the payroll** |

There is no FK enforcing any of this, so a partial re-point fails silently rather than erroring.

### An EMPTY legacy table is not automatically "nothing to do"

`vowsls.tbl_pay_employee_payperiod` had 0 rows, so the payroll migration skipped it as a no-op.
But it is a **derived summary table that the new sls payroll run writes** â€” legacy computed the
same thing elsewhere â€” and `get_pay_register_salary` drives the entire Pay Register detail off it,
INNER JOINing payroll to it. Result: 26M migrated payroll rows were invisible in the UI and the
list's Net Pay column was blank, with no error anywhere.

Before writing off an empty source table, ask **who writes the target in the new system**. If the
answer is "the application, as a summary of rows we just migrated", it has to be derived, not
copied. Same question applies to `pay_employee_payroll_status`, `pay_period_status` and
`pay_company_components` â€” all empty on both sides today, all worth re-checking if a screen using
them comes up blank.

### Money columns â€” check float precision before loading

Several sls pay tables declare `float` where legacy has `double(18,2)` / `decimal(16,6)`. float32
holds only ~7 significant digits, so any amount above **167,772.16** loses its paise. Before a
bulk money load, count the affected rows:

```sql
SELECT COUNT(*) FROM <legacy_table> WHERE ABS(AMOUNT) > 167772.16 AND AMOUNT <> ROUND(AMOUNT);
```

If non-zero, `ALTER` the target column to the legacy type **first**, while the table is still
small. The ORM keeps declaring `Float`, which SQLAlchemy still returns as a Python float, so
runtime behaviour is unchanged â€” only storage gets exact.

### Type narrowing (bigint â†’ int)
Verify `MAX(legacy_id) < 2^31` before assuming the cast is safe. Indent IDs in vowsls peak at 9555, well within int range.

---

## 4. Columns to drop (encountered so far)

These columns are dropped in the new schema and have no replacement. Discard during migration.

| Column | Concept |
|--------|---------|
| `fy` | Financial year â€” derive from date if needed |
| `source` | Origin tracking â€” never used in real data |
| `total_value` | Header denormalization â€” recompute from detail rows |
| `phase` | Project phase concept removed |
| `store` | Separate store-branch concept; collapsed into `branch_id` |
| `customer` (header) | Customer reference removed from headers |
| `company` | New schema uses `branch_id` to deduce company; no `co_id` column on `proc_indent` |
| `record_type` | Replaced by `indent_type_id` |
| `indent_expiry_date` | Open-indent expiry concept removed; type alone signals open |
| `dept_id` (legacy header had no such column anyway) | Leave NULL on new side |

Always cross-check with the user when encountering a new dropped column â€” it may have been moved to a sibling table.

---

## 5. Scope rules (apply by default)

- **Active only**: `WHERE is_active = 1`.
- **No audit data**: skip `approved_*`, `rejected_*`, `created_by/date`, `last_modified_*` unless requested.
- **Preserve PKs**: insert with the legacy `indent_id`/`po_id`/etc. so downstream FK tables migrate cleanly.
- **One company at a time?** Default is "all companies in source DB", since new tenant DB receives all of them. If migrating into a multi-company tenant, filter on `company IN (...)`.

---

## 6. Migration script template

Every migration consists of two files in `dbqueries/migrations/`:

1. `migrate_<source_db>_<source_table>_to_<target_db>.sql`
   - SQL header comment block with mapping rules and rollback.
   - Single `INSERT INTO <target> (...) SELECT ... FROM <source> WHERE is_active = 1`.

2. `run_migrate_<source_db>_<source_table>_to_<target_db>.py`
   - Pymysql runner with dry-run-by-default, `--commit` flag.
   - Pre-flight: count source active rows, count target rows, abort on PK collisions.
   - Post-flight: print last 5 rows, status distribution, type distribution.
   - Use a wrapped transaction; rollback unless `--commit` passed.

**SQL parser caveat:** the runner strips lines starting with `--` before splitting on `;`. Do not put `--` comments inside SQL string literals; do not use `/* */` block comments unless the runner is updated to strip them.

**Reference implementation:**
- [migrate_vowsls_proc_indent_to_sls.sql](migrate_vowsls_proc_indent_to_sls.sql)
- [run_migrate_vowsls_proc_indent_to_sls.py](run_migrate_vowsls_proc_indent_to_sls.py)

---

## 7. Verification queries (run after every commit)

```sql
-- Row count parity
SELECT
    (SELECT COUNT(*) FROM vowsls.<source> WHERE is_active = 1) AS source_active,
    (SELECT COUNT(*) FROM sls.<target>)                        AS target_after;

-- PK preservation spot check
SELECT s.indent_id, t.indent_id
  FROM vowsls.<source> s
  LEFT JOIN sls.<target> t USING (indent_id)
 WHERE s.is_active = 1 AND t.indent_id IS NULL
 LIMIT 20;     -- should be empty

-- Status distribution sanity
SELECT status_id, approval_level, COUNT(*) FROM sls.<target> GROUP BY 1, 2;

-- FK integrity (branch_id, project_id, expense_type_id, status_id)
SELECT 'orphan_branch'   AS chk, COUNT(*) FROM sls.<target> t LEFT JOIN sls.branch_mst       b USING(branch_id)        WHERE b.branch_id IS NULL AND t.branch_id IS NOT NULL
UNION ALL SELECT 'orphan_status', COUNT(*) FROM sls.<target> t LEFT JOIN sls.status_mst      s USING(status_id)        WHERE s.status_id IS NULL AND t.status_id IS NOT NULL
UNION ALL SELECT 'orphan_expense', COUNT(*) FROM sls.<target> t LEFT JOIN sls.expense_type_mst e USING(expense_type_id) WHERE e.expense_type_id IS NULL AND t.expense_type_id IS NOT NULL;
```

All counts in the `orphan_*` checks should be zero. Investigate any non-zero result before proceeding to dependent tables.

---

## 8. Tables to migrate (planned order)

Migrate in dependency order. Headers first, then detail/line tables, then transactional follow-ons (PO, inward, issue, invoice).

| Order | Source (`vowsls`) | Target (`sls`) | Status |
|-------|-------------------|----------------|--------|
| 1 | `tbl_proc_indent` | `proc_indent` | âœ… Done â€” 4234 rows (2026-05-04) |
| 2 | `tbl_proc_indent_detail` | `proc_indent_dtl` | Pending |
| 3 | `tbl_proc_po` | `proc_po` | Pending |
| 4 | `tbl_proc_po_detail` | `proc_po_dtl` | Pending |
| 5 | `tbl_proc_inward` | `proc_inward` | Pending |
| 6 | `tbl_proc_inward_detail` | `proc_inward_dtl` | Pending |
| ... | ... | ... | ... |

Pay-scheme / payroll chain (all âœ… 2026-08-15, `run_migrate_vowsls_payscheme_to_sls.py`):

| Source (`vowsls`) | Target (`sls`) | Note |
|-------------------|----------------|------|
| `tbl_pay_scheme` | `pay_scheme_master` | 13 reuse existing sls ids, 126 new (`+1000`) |
| `tbl_payscheme_master` | `pay_scheme_master` | 3 rows â†’ ids 301â€“303 |
| `tbl_pay_scheme_details` | `pay_scheme_details` | new schemes only; reused schemes keep hand-made formulas |
| `tbl_pay_employee_payscheme` | `pay_employee_payscheme` | STATUS=1 only, deduped, sls-native assignments win |
| `employee_payscheme` | `pay_employee_payscheme` | `payscheme_id` column family |
| `tbl_pay_period` | `pay_period` | `+100000` |
| `tbl_pay_employee_structure` | `pay_employee_structure` | `+10000000` |
| `tbl_pay_components_custom` | `pay_components_custom` | `+100000000`, dangling period â†’ NULL |
| `tbl_pay_employee_payroll` | `pay_employee_payroll` | `+100000000`, dangling period â†’ row dropped |
| `tbl_pay_wages_mode`, `payscheme_parameter_category`, `processed_payscheme`, `tbl_pay_customer_employee_payscheme`, `tbl_pay_generic` | same-named `pay_*` | small lookups |

Not migrated (empty in vowsls): `tbl_pay_employee_payperiod`, `tbl_pay_period_status`,
`tbl_pay_employee_payroll_status`, `tbl_pay_company_components`, `tbl_pay_external_components`,
`temp_payscheme_details` (scratch), `sls.pay_scheme` (legacy-shaped, unread by the backend).

Update this table after each migration is committed.

---

## 9. Migration Log

| Date | Source â†’ Target | Rows inserted | Notes |
|------|-----------------|---------------|-------|
| 2026-05-04 | `vowsls.tbl_proc_indent` â†’ `sls.proc_indent` | 4234 | 4 inactive skipped. 17â†’20+L2 (58 rows). 7 rows with `M_xxx` / `G_xxx` numbering handled via `REGEXP_SUBSTR`. BOQ/BOM legacy folded into `Regular`. |
| 2026-07-27 | HRMS "small companies" (all except comp_id 67,65,2,106,139,1) â†’ `sls` | 336 employees + 8 ed child tables, 3201 daily_attendance, 59+101 leave txn/details, 29 leave types; masters: 30 dept_mst, 33 sub_dept_mst (legacy dept ids preserved), 43 designation_mst, 22 category_mst (ids preserved) | `run_migrate_vowsls_hrms_smallco_to_sls.py`. eb_ids preserved except 6 collisions remapped to 59082â€“59087; full map in `sls._map_hrms_eb_smallco`. Inactive employees migrated (active=0 kept). Skipped: payroll (`tbl_pay_*`), attendance_summary, holiday tables, leave_ledger/policies (no compatible target), 5 junk leave rows with company_id=1000001 (co-1 employees). Rollback block in script header; run logged in `sls.migration_log` step `vowsls_hrms_smallco`. |
| 2026-07-28 | HRMS comp_id 1 + 2 (EJM), ALL remaining emp_code-missing employees â†’ `sls` | 8,238 employees (8,229 co-2 identity-mapped + 9 co-1 remapped to fresh eb_ids 59088â€“59096); 8,255 official rows, ~8,150 rows each addr/bank/contact/esi/pf/resign (per-table eb guards skipped ~30â€“84 partially-migrated actives), 9 personal (co-1 only), 17,847 daily_attendance (INSERT IGNORE vs 89 pre-existing), 20+279 leave txn/details, 1 experience; masters: only designation 909 & 1190 created | `run_migrate_vowsls_hrms_missing_rest_to_sls.py`. co-2 personal rows pre-existed 1:1 (earlier EJM migration copied personal for everyone, official only for a subset) â†’ identity map, personal untouched; collision rule = remap ONLY on first-name mismatch. 2 co-2 officials had branch_id=0 â†’ sls personal branch fallback. 2 junk test employees (`ejclx`/`ejclxx`, all-zero FK refs) excluded by design; `mohit@abc.com` is a historical junk code of T20010 (employee present). After this run the vowslsâ†’sls emp_code diff is EMPTY apart from those 3 junk codes. Log step `vowsls_hrms_missing_rest`; rollback block in script header (NOTE: identity personal rows + pre-existing child rows for skipped ebs are NOT migration-owned). |
| 2026-07-28 | HRMS comp_id 67 (AVPL) + 106 (LLCPL) + 139 (VES), emp_code-missing employees only â†’ `sls` | 101 employees (12+30+59) + personal/official/6 ed child tables, 9 experience, 740 daily_attendance, 32+1525 leave txn/details, 15 leave types; masters: 9 dept_mst (15125â€“15133), 11 sub_dept_mst (96,97,117,170,179,180,181,182,387,389,391), 24 designation_mst, 4 category_mst (29,42,59,68) â€” legacy ids preserved | `run_migrate_vowsls_hrms_co67_106_139_to_sls.py`. Scope = employees whose trimmed emp_code is absent from sls (co 106 has 4,366 of 4,399 already present via the co1â†’106 remap; 11 employees without any emp_code skipped). eb_ids preserved (0 collisions); map appended to `sls._map_hrms_eb_smallco`. `reporting_eb_id=0` nulled; 38 junk legacy reporting ids passed through. dept 182 referenced only by attendance â†’ master collection unions attendance worked ids. One junk `leave_type_id=0` nulled. Log step `vowsls_hrms_co67_106_139`. Remaining gaps: co 1 (9 codes, sls eb slots occupied â†’ need fresh eb_ids), co 2 EJM (8,232 personal-only, no official rows â€” scope decision pending). |
| 2026-08-15 | `pay_employee_payperiod` backfill (fixes the empty Pay Register) | 363,302 rows derived | `run_backfill_pay_employee_payperiod.py`, log step `pay_employee_payperiod_backfill`. **Symptom:** Pay Register > View showed "No rows" for every migrated period and the list's Net Pay column was blank â€” e.g. branch-4 period 105806 (NJM_CONT_WAGES_NEW, Jul-2026) had 11,076 payroll rows and 0 epp rows. **Cause:** `get_pay_register_salary` drives the detail grid `FROM pay_employee_payperiod` and INNER JOINs payroll to it; the list reads `SUM(epp.NET)`. `vowsls.tbl_pay_employee_payperiod` was empty so the payroll migration skipped it â€” but it is a summary the *new* sls payroll run writes, so it had to be derived, not copied. **Fix:** one row per distinct (EMPLOYEEID, PAYPERIOD_ID, PAYSCHEME_ID) in `pay_employee_payroll`. Component mapping verified against sls-native data (period 18, eb 31526: epp NET 6157.00 / GROSS 7623.03) â†’ `NET` = component 21 `C_NET` (present in 362,947 of 363,302 groups, fallback 357), `GROSS` = 350 else 349, `BASIC` = 7. NET is the only field either screen reads; GROSS/BASIC are best-effort and scheme-specific. Gross is NOT `SUM(type=1)` â€” the earnings set contains running aggregates (`totalwagesamt`, `gross1amt`, `PFG`) and summing double-counts. Guarded on the natural key, so re-runs are no-ops. Rollback: `DELETE FROM sls.pay_employee_payperiod WHERE ID > 185`. |
| 2026-08-15 | Pay schemes mirrored branch 4 â†’ branch 87 | 17 `pay_scheme_master` (co 106 / branch 87), 1,304 `pay_scheme_details`; re-pointed 219 assignments, 1,135 periods, 263,088 structure rows, 165,205 payroll rows | `run_mirror_pay_schemes_branch4_to_branch87.py`, log step `pay_scheme_mirror_branch87`, map `sls._map_pay_scheme_b87`. Branch-87 employees were running co-1 (NJM) schemes, so the Pay Scheme page showed nothing useful under co 106. Source set = the 17 co-1 schemes referenced by a branch-4 period or branch-4 assignment; new `payscheme_id = source + 2000`, detail `ID + 500000`, and these are the first rows in the table with `branch_id` actually populated. **The whole PAYSCHEME_ID chain moved together** â€” see the four-table invariant above; creating the schemes without re-pointing structure and payroll would have failed silently (lost structures, doubled payroll). Branch 4 untouched: no eb_id spans both branches, checked explicitly. Still open: 2,678 active branch-87 employees have no pay scheme at all and nothing to derive one from. |
| 2026-08-15 | co-1 employees â†’ branch 4, then branch 4 mirrored into branch 87 | 1,800 new employees on branch 4 (eb 60000â€“61799) + 10 on branch 87 (eb 61800â€“61809), each with one active official row and their address/bank/contact/esi/pf/resign rows | `run_sync_co1_employees_branch4_branch87.py`, log step `hrms_co1_branch4_branch87_sync`, map `sls._map_hrms_eb_branch_clone`. **Measure the gap, don't re-import:** vowsls co-1 has 4,601 emp_codes and *none* was absent from sls â€” the co1â†’co106 remap had put them under branch 87 (2,801 were already on branch 4, 2,792 on both). Re-importing all 4,601 would have duplicated the 2,792. Actual gaps: 1,800 missing from branch 4, 10 missing from branch 87. Copied from the **existing sls record**, not vowsls â€” same person, and it guarantees sub_dept/designation/category FKs already resolve. `emp_code` is intentionally duplicated across the two branches (already the norm: 2,785 codes span branches; no unique key forbids it). Branch 4: 2,802 â†’ 4,602; branch 87: 4,597 â†’ 4,607. **The clones carry no pay scheme and no payroll** â€” that stays on the source record. |
| 2026-08-15 | `pay_period` missing-branch repair (root cause of the residual) | 512 of 646 branchless periods given a branch; 296,084 further payroll rows corrected | `run_fix_pay_period_missing_branch.py`, log step `pay_period_missing_branch_fix`. Legacy `branch_id = 0` became NULL on 646 periods, which made them invisible to `find_pay_period_by_dates` **and** left their payroll rows with no branch to derive `BUSINESSUNIT_ID` from. Derived per period, recorded in `sls._map_pay_period_branch`: `employee_branch` 429 (modal branch of the period's payroll employees via their ACTIVE official row, â‰¥90% share), `employee_branch_co_fixed` 27 (same, but the branch belongs to another company â€” all 27 are the co1â†’co106 remap, so `COMPANY_ID` was corrected to follow the branch), `company_single_branch` 56 (company owns exactly one branch). **Left NULL on purpose:** `ambiguous_mixed` 31 (payroll genuinely straddles two branches, top share 0.50â€“0.83) and `ambiguous_no_evidence` 103 (no employee has an active official row) â€” 11,458 payroll rows. Guessing there would file salary under the wrong factory. **Measure `share` against the evidence base** (rows whose employee has an active official row), not the period's total rows â€” using the total vetoes periods where every resolvable employee agrees, and cost 219 periods on the first attempt. |
| 2026-08-15 | Pay setup branch 4 â†’ branch 87, + `BUSINESSUNIT_ID` fix | 1,135 `pay_period`, 7 `tbl_payslip_print_component`; 25,683,175 payroll rows corrected | `run_copy_pay_branch4_to_branch87.py`, log step `pay_branch4_to_87_and_bu_fix`. **Fix:** the payroll migration passed legacy `BUSINESSUNIT_ID` (a company id) straight through, but sls reads that column as a branch id â€” 0 of 7.3M rows under branch-4 periods matched their employee's branch, so the Pay Register branch filter found nothing. Backfilled from `pay_period.branch_id`; ~307k rows whose period has no branch left as-is. **Copy:** branch 4 (FACTORY co 1) and 87 (FACTORY co 106) are both live and hold their own payroll â€” this is a copy, not a relocation. Periods copied with `ID + 500000`, `COMPANY_ID` rewritten to 106, `STATUS` verbatim (copies therefore look processed but carry no payroll). Both inserts guarded on the natural key, so re-running is a no-op. |
| 2026-08-15 | Pay-scheme + full payroll history â†’ `sls` | 126+3 `pay_scheme_master`, 5,093 `pay_scheme_details`, 5,730 `pay_period`, 3,744+859 `pay_employee_payscheme`, 1,563,079 `pay_employee_structure`, 9,303,280 `pay_components_custom`, **25,998,509 `pay_employee_payroll`**, 16 lookup rows | `run_migrate_vowsls_payscheme_to_sls.py`, log step `vowsls_payscheme_payroll`. Maps: `_map_pay_eb` (26,881; 20 unresolved, 3,830 `merged`), `_map_pay_scheme` (139: 13 reuse / 126 new), `_map_pay_scheme_master` (3), `_map_pay_period` (5,730) + CSVs in `maps/`. Scheme ids NOT preserved â€” the 13 legacy schemes matching an existing sls scheme by CODE **and** company reuse its id and keep their hand-made details; the rest get `legacy+1000`. Employeeâ†”scheme: STATUS=1 only, deduped per (sls eb, sls scheme), and **skipped entirely for the 44 employees already holding a live sls-native assignment**. Dropped: 1,974 payroll + 1,105 structure rows (dangling period/scheme or unresolved eb); 4,308 custom rows kept with `PAY_PERIOD_ID` nulled. `pay_employee_payroll.AMOUNT` / `pay_employee_structure.AMOUNT` widened float â†’ decimal(18,2)/(16,6) **before** loading (float32 loses paise above 167,772.16 â€” would have corrupted 21,298+898 rows). Verified: 0 orphans, payroll sum reconciles exactly at â‚¹77,208,523,797.53. Open item: legacy periods now coexist with the sls-native fresh periods for schemes 4 and 15 (overlapping months). |
| 2026-08-15 | Holiday Master + holiday transactions â†’ `sls` | 573 `holiday_master`, 23,416 `hrms_holiday_transactions` | `run_migrate_vowsls_holiday_to_sls.py`, log step `vowsls_holiday`, map `sls._map_holiday_branch`. Both targets were empty, so **legacy PKs preserved 1:1** and rollback is a plain `DELETE FROM` both tables. **Company â†’ branch is the whole problem:** legacy `holiday_master` is company-scoped (`branch_id` NULL on 517 of 573) but `sls.holiday_master` is branch-scoped â€” the portal list `INNER JOIN branch_mst ... WHERE b.co_id` makes a branchless row invisible. Derived per company as the branch holding the most sls employees (tie â†’ `FACTORY` â†’ lowest id): co 1â†’4, co 2â†’29, co 106â†’87, co 3â†’5 (HO â€” its FACTORY branch 103 has zero employees, so a blind "prefer FACTORY" rule would have been wrong), co 139â†’101. A legacy `branch_id` is trusted **only if it exists in sls under the same company** â€” holiday 523 carries branch 2 under co 2, but branch 2 is co 1's HO in vowsls, so it falls back to the map. Derivations: `status` NULL on every row â†’ 3 (Approved) for the 53 holidays that have transactions, else 1 (Open); `holiday_type` NULL/0 â†’ 1 (Occasional), since the backend only accepts 1/2/3 and would reject an edit otherwise; free-text `spell` â†’ `spell_id` via `spell_mst.spell_code` + `shift_mst.branch_id` (only branches 4/29/87 have spells; all 40 rows carrying one resolved). `created_by` is a varchar username with no sls user-id equivalent â†’ `updated_by` NULL. Transactions: **active rows only** (23,428 of 153,059 â€” the rest are superseded process re-runs), eb via `sls._map_pay_eb`, deduped on (holiday_id, mapped eb) keeping the lowest legacy tran id. That drops 12 rows â€” the co1/co106 doubles plus a few legacy dups â€” which would otherwise credit those employees holiday hours **twice** into the wages process. Their `auto_date_time_insert` is NULL on every active row, so `updated_date_time` carries the holiday's timestamp. Result: branch 29 17,587 rows / 140,696 hrs, branch 4 5,829 rows / 46,052 hrs. Not migrated: `tbl_hrms_holiday_eligibility`, `holiday_wage_*`, `tbl_alm_holiday` (all empty in vowsls). |
| 2026-09-01 | `vowsls.worker_master.cash_rate` (co 2, active='Y', rate>0) â†’ `sls.outsider_rate_approve` | 3,538 rows (all branch 29) | `run_seed_co2_outsider_rate_from_worker_master.py --commit`, log step `co2_outsider_rate_from_worker_master` (in `vowsls_to_sls_transfer.migration_log`). Seeds the Cash/Daily Rate master so EJM cash-hand attendance stops pricing at 0 (`cashHands.py` reads `outsider_rate_approve`; the tenant schema dropped `worker_master.cash_rate`). Mapped by `emp_code` into co-2 branches â€” 3,538/3,538 clean, 0 ambiguous. `rate_approve = ROUND(cash_rate)` (int column; only 10 legacy values carried paise), `app_date = 2022-01-01` (effective-from baseline, superseded by any UI-entered approval), `app_shift` NULL, `desig_id` = the employee's own sls designation (legacy `desg_id` disagrees on 431). PK = `900000 + legacy eb_id`. Guarded by NOT EXISTS on an active rate, so re-runs are no-ops. Rollback: `DELETE FROM sls.outsider_rate_approve WHERE outsider_rate_app_id >= 900000`. |
| 2026-07-28 | HRMS comp_id 65 (ACPL, Anurashi Commotrade) â†’ `sls` | 7 employees (official + 6 ed child tables; personal rows pre-existed with same eb_ids and were left untouched), 329 daily_attendance, 8+8 leave txn/details, 2 leave types; masters: 2 dept_mst (15080, 15081), sub_dept 120/164, designation 1210/1211, category 44 (legacy ids preserved) | `run_migrate_vowsls_hrms_co65_to_sls.py`. Identity eb map (no collisions) appended to `sls._map_hrms_eb_smallco` with company_id=65 â€” those personal rows are NOT migration-owned, never delete them on rollback. Run logged in `sls.migration_log` step `vowsls_hrms_co65`. Still missing in sls by emp_code afterwards: co 67 AVPL (12 emp), co 106 LLCPL (30), co 139 VES (59) â€” all eb absent from sls, user-excluded on 2026-07-27; co 1 (9 codes, sls eb slots occupied â†’ would need new eb_ids); co 2 EJM (8,240 employees with personal rows but no official rows in sls â€” earlier EJM migration scope); co 120 ` BFSPL032` is a false positive (leading space; in sls trimmed). |

---

## 10. Open questions / deferrals

- **`dept_id` source**: legacy `tbl_proc_indent` has no dept column. Currently inserting NULL. If a future table has a usable dept reference, revisit.
- **Multi-company tenants**: confirm before migrating any table that the target tenant DB is intended to receive all source companies' data, not a subset.
- **Lookup masters not yet verified**: `company_master`, `customer_master`, `tbl_proc_project`, `tbl_proc_projects_phase`, `dept_master`, `user_master`. Verify on first encounter.

## 2026-09-09 - pay_employee_structure: scheme 163 -> 3163 (NJM_CONT_WAGES_NEW, branch 87)

`run_copy_structure_163_to_3163.py` (dry-run default, `--commit`). Copied the 11,076 current (STATUS=1)
structure rows of legacy scheme 163 (284 employees) onto sls scheme 3163, mapping legacy eb -> sls branch-87 eb by
emp_code (map `vowsls_to_sls_transfer._map_struct_163_to_3163`). Target ID = legacy ID + 20,000,000 (1163's copy is
+10,000,000). Verified every (employee, component) amount/status/dates equal, no duplicates. Logged as
`migration_log` step `pay_structure_163_to_3163`. Two sls-native employees on 3163 (C910273, C960143) untouched.
Rollback: `DELETE FROM sls.pay_employee_structure WHERE PAYSCHEME_ID=3163 AND ID BETWEEN 20000000 AND 29999999`.
Still open on 3163: component 76 CODE `C_LTAs_` (NET formula wants `C_LTA_`), and the ROUNDOF_TYPE semantics gap.

## 2026-09-10 - pay_employee_structure gap fill, scheme 3163

`run_fill_structure_gaps.py --scheme 3163 --commit`: for every employee on the scheme, inserted a current (STATUS=1)
structure row for each active `pay_scheme_details` component that was missing - 69 rows for 3 employees (56690, 61813,
61814, who had only the 16 TYPE-0 inputs because the UI "save structure" writes inputs only and deactivates earlier
rows). Derived components get AMOUNT 0; a missing TYPE-0 input would get the scheme formula constant. Rows carry
REMARKS 'gap-fill 2026-09-10 from pay_scheme_details' (rollback = DELETE on that tag). Verified: all 286 employees
now hold exactly 39 current components, no duplicates. migration_log step `pay_structure_gap_fill_3163`.
Side note: employee 56690 has 3 `pay_employee_payscheme` rows on 3163 (harmless - the engine selects DISTINCT).

`run_repoint_dedup_component_codes.py` (formula re-point for the 4 renamed component codes, 43 formulas / 36 schemes)
was applied 2026-09-10 (43 formulas, backup `vowsls_to_sls_transfer._bak_pay_scheme_details_formula_20260909`, migration_log step `pay_scheme_formula_dedup_codes`). Verified by creating the Aug-2026 register for 3163 through the UI (period 605879): 39 rows per employee, C_NET and C_banks_ written for all 286 employees, pay_employee_payperiod NET 1,218,376.00.

