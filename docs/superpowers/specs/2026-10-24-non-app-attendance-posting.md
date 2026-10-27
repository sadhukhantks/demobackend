# Non-app attendance into the attendance tables (staff via biometric) — findings, design, open questions

Date: 2026-10-24 · Status: **DESIGN — awaiting owner answers (§6)** · Reference tenant: `sls`, co 2 THE EMPIRE JUTE COMPANY LTD, branch 29 FACTORY (EJM)

## 1. Problem

Every "hands" figure the system prints (Hands Report, DPR hands + hands/MT, HRMS attendance
reports, Daily P&L wages line) is read from `daily_attendance`. On EJM that table is written
only by the mobile attendance app (face / app-manual) and, rarely, the HRMS Attendance
Calendar form. People who do not pass the app — the STAFF category (supervisors, managers,
shift in-charges, electricians, mechanics, trainees) and anyone else punching only on the
biometric devices — never reach `daily_attendance`, so they are invisible to every report.

The owner wants the attendance of those other staff / persons processed too and the numbers
included in the reports.

## 2. What exists today (verified on sls, read-only, 2026-10-24)

### 2.1 `daily_attendance` — the one table all reports read

| Writer | `attendance_source` | Who | Status written |
|---|---|---|---|
| Mobile app, face validated | `F` | workers at the gate | `3` approved immediately |
| Mobile app, manual button | `A` | workers | `3` |
| HRMS portal Attendance Calendar (`src/hrms/attendance.py`) | `M` | manual web entry | `3` (approve/reject exist) |
| Biometric pipeline | — | **never writes this table** | — |

Branch 29, 1 Sep – 23 Oct 2026: 35,879 rows / 1,055 employees. `F` 34,394 rows, `M` 631,
`A` 209 (incl. 39 rejected). Columns: `eb_id, attendance_date, attendance_source,
attendance_type (R/O), spell_id→spell_mst, spell (legacy text), spell_hours, working_hours,
idle_hours, worked_department_id (sub_dept_mst), worked_designation_id, branch_id,
status_id (varchar), is_active, entry_time, exit_time, remarks, geo_location` + offline-sync
columns. Machine marks live in `daily_ebmc_attendance`.

Hands everywhere = `(working_hours − idle_hours) / 8`; shift bucket = first letter of the
spell code (A/B/C; Hands Report sends anything else to `G`, DPR counts an unknown bucket in
the total but prints no row for it).

### 2.2 The biometric chain (`src/hrms/bioAttendance.py`, `bio_att_auto_pipeline.py`, `scheduler_main.py`)

```
punch_logs (device feed, 2 devices)  ─sync_punch_logs→  bio_attendance_table (raw punches)
   ─prepare_punches / _resolve_eb_ids (tbl_master_bio_link_mst: bio_dev_id → eb_id)→
   ─bprocess_core (essl_resolver)→  bio_attendance_basic (one row per eb/day: shift, in, out, minutes)
   ─b_atten_core→  bio_attendance_process (one R row + one O row per eb/day: spell_name, hours)
   ✗ "final process" to daily_attendance was deliberately left out of the port
```

- UI: HRMS → **Bio Attendance** page (`hrms/bioattendance`): tabs Employee Bio Link, Bio Data
  Process (run for a period, Basic/Process grids), Day Wise Report, Monthly In-Out.
- Hourly auto pipeline exists (`BIO_ATT_AUTO_*` in `env/database.env`) but is **off** on this
  VM and configured for `sls` branch **4** (NJM), not 29.
- sls data: `punch_logs` holds ~15.5k distinct punches since Jul 2026 (4 devices, 151 device
  users); `bio_attendance_table` holds **210k rows for those ~15.5k punches** — every punch is
  stored 1–13 times (bug, §2.5). 280 bio links, all clean (no duplicates, every `master_id`
  has an active official row). Sep–Oct 2026 **80 branch-29 employees** (912 employee-days) in
  `bio_attendance_basic`, **all category STAFF** (supervisor 40, manager 7, extra-jute 6, shift
  in-charge 4, mill-mech trainee 3, executives, electricians, loom fitter, markman, watch &
  ward …). Of the 80, only **27 are status JOINED**; 41 are OPEN, 10 DRAFTED, 2 left.
- Overlap with app rows: **0 employees, 0 days** (nobody is in both today).
- Processing is manual and gappy: basic/process rows exist for **18 of the 53 days** 1 Sep –
  23 Oct (Sep 11/30, Oct 7/23), created in ad-hoc runs from the Bio Data Process tab. The
  hourly pipeline never ran on sls (no `bio_att_auto_state` table). Sync is ~1 day behind
  `punch_logs`.
- Unlinked device users: of 142 codes with `eb_id NULL`, **129 are phantom duplicate rows of
  already-linked staff**; only **13 device users (589 punches)** are genuinely unlinked, and
  their numbers (168, 877, 261, 5065, 5044, 278, 277, 272, 291, 5048, 9999, 276, 5067) match
  the shop-floor worker code families C0xxx / L0xxx / T0xxx — most likely workers who also
  mark on the app, one test id (9999), one unknown (261). They cannot be auto-linked (the
  numeric tail is shared by three employees each).
- Size of the addition: on a fully processed day staff add **55–75 hands against 800–880 app
  hands (7–9 %)**, ≈ 0.9 hand per staff member punching.
- Spell names produced: `A, A1, B, B1, B2, C, C1, GS (general shift 10:00–18:00), O`.
  `spell_mst` (branch via `shift_mst.branch_id`) has **no general shift**: `GS` (236 rows),
  `O` (243), `C1` (5) have no spell row; A/A1/A2/B/B1/B2/C map to branch-29 spell ids
  109/97/98/110/99/100/101. Bio windows differ from `spell_mst` (bio C = 8 h, spell C = 7.5 h
  overnight; bio A1 = 06–14, spell A1 = 06–11 5 h).
- Hours are actual punch hours and noisy: **55.6 % of basic rows are "No OutPunch"** and get a
  synthesized full-shift OUT (only ~1/3 of those are truly single punches; the rest are stale
  same-day runs or split sessions); 15 employee-days exceed 12 h (max 29.85 h); every
  employee is treated as OT-eligible, so staff OT is overstated. Each employee-day yields an
  `R` row (work, capped 8 h) and possibly an `O` row (overtime); on the weekly off-day the
  whole span is one `O` row.

### 2.3 Headcount view, branch 29 (active, status Joined)

| Category | Employees | App attendance last 30 d | Bio attendance last 30 d | Neither |
|---|---|---|---|---|
| CASUALS | 1,459 | 130 | 0 | 1,329 |
| CARDHOLDER | 674 | 62 | 0 | 612 |
| PF BADLI | 401 | 345 | 0 | 56 |
| PERMANENTS | 227 | 211 | 0 | 16 |
| SP. BADLI | 145 | 126 | 0 | 19 |
| CONTRACTOR | 121 | 85 | 0 | 36 |
| LEARNER | 85 | 9 | 0 | 76 |
| **STAFF** | **31** | **0** | **27** | 4 |
| RETIRED / RETIRED VOUCHER / SR VOUCHER / others | 204 | 25 | 0 | 179 |

So "the hands entered through the app" = workers; the STAFF category is the group the
reports miss, and it is already captured end-to-end up to `bio_attendance_process`.
(The mill-GM knowledge base, INSIGHTS #67, already records the hands basis as "approved
worker attendance only; staff salaries sit in overheads".)

### 2.4 Report consumers of `daily_attendance`

See §7 (consumer audit).

### 2.5 Bugs found on the side (fix regardless of the design decision)

1. **Punch duplication** — `sync_punch_logs` dedupes on `(emp_code, log_date)`, but
   `prepare_punches` then overwrites `emp_code` with the official code, so the next sync
   re-inserts every punch (1–13 copies each; 210k rows for 15.5k punches). Dedupe on
   `(bio_id, log_date)` and clean the table.
2. **Hands Complement** (`hrms/reportQueries.py:403`) filters hardcoded category ids for
   branches 4 and 87 only — branch 29 is excluded entirely.
3. `_LAST_DAILY_ATT_SQL` in `bioAttendance.py` takes dept/designation from the last
   `daily_attendance` row of the employee; once bio rows are posted it feeds on itself.
4. `hrms_ed_official_details` carries duplicate rows for some employees (27,916 rows for
   27,233 eb_ids) — every join must take the latest active row or hands double count.

## 3. Options considered

| # | Option | Verdict |
|---|---|---|
| A | **Post the processed bio rows into `daily_attendance`** with their own `attendance_source` code, idempotently per date, never touching app rows | **Recommended.** One write path; every existing report, including future ones, sees the staff automatically; exclusions (wages, cash) are one filter each |
| B | Make each report UNION `bio_attendance_process` | Every report (≈30 consumers) changes; the wages/cash exclusions are still needed; two sources of truth for "present" |
| C | HR keys staff into the Attendance Calendar form | Exists today, zero code, but 30–80 rows/day by hand and it ignores the punches already collected |
| D | Make staff mark on the face app at the gate | Process change, no code; but the bio devices already hold their punches and the app is sized for the worker gate |

## 4. Proposed design (Option A)

### 4.1 Posting step — `post_bio_to_attendance(db, tran_date)` in `src/hrms/bioAttendance.py`

Source: `bio_attendance_process` rows of the date (built from first/last punch per session,
so not inflated by the raw-table duplicates) joined to the employee's **latest active
official record** (`hrms_ed_official_details`: `branch_id, sub_dept_id, designation_id,
catagory_id`) and to `hrms_ed_personal_details` for the employee status filter (Q5b: JOINED
only, or also OPEN/DRAFTED — 53 of the 80 punching staff are not JOINED).

| `daily_attendance` column | Value |
|---|---|
| `eb_id`, `eb_code` | process row eb_id, official `emp_code` |
| `attendance_date` | `attendance_date` |
| `attendance_source` | **`BIO`** (new code; same literal `bio_attendance_process` already carries). HRMS / mobile source labels gain `BIO → Biometric` |
| `attendance_type` | process `attendance_type` (`R` regular / `O` off-day or overtime) |
| `spell_id` / `spell` | `spell_mst` row whose `spell_code` = process `spell_name` on the employee's branch; `spell` text left **NULL** (legacy half-day report keys on it; unique index). General shift `GS` needs a spell row where missing (Q3) |
| `spell_hours` | 8 |
| `working_hours` / `idle_hours` | **rule to decide (Q4)**; recommended **flat 8 h per present day** (bio hours are synthesized for 55 % of days and overtime is overstated), `O` overtime rows dropped, off-day `O` row posted as one 8 h `R`-type? → Q4 |
| `worked_department_id` / `worked_designation_id` | official `sub_dept_id` / `designation_id` (staff have no "worked elsewhere" concept) |
| `branch_id` | official `branch_id` |
| `status_id` | `'3'` approved like the app, or `'1'` open for HR approval (Q5) |
| `entry_time` / `exit_time` | `check_in` / `check_out` |
| `is_active` 1, `remarks` `'BIO'`, `created_by` the running user / 0 for the scheduler |

Rules:
- **Idempotent per date and branch:** hard `DELETE … WHERE attendance_date = :d AND attendance_source = 'BIO' AND branch_id IN (:branches)` then insert (the unique index `daily_attendance_unique` makes a soft-delete collide on the next run). `F` / `A` / `M` rows are never touched.
- **Precedence (Q6):** a bio row is skipped when a live, non-rejected app/manual row already exists for the same `eb_id` + date (+ spell) — the app mark wins; the skip is counted and shown in the run result.
- No `daily_ebmc_attendance` rows (staff carry no machine).
- Dates are re-posted together with the bio rebuild (the pipeline already re-processes the previous day for night shifts), so a late OUT punch corrects the posted row the next run.

### 4.2 Where it runs

1. **Bio Data Process tab** — the existing run (`POST /api/hrmsMasters/bio_att_process_run`) gets a `post_to_attendance` flag (checkbox "Post to daily attendance", default on once the owner approves) and reports `posted / skipped (app row exists) / unlinked codes` per date.
2. **Hourly auto pipeline** — `_scheduled_job` calls the same function after `b_atten_core`, behind `BIO_ATT_POST_DAILY=true` (default false), and the pipeline's tenant/branch config is widened to a branch **list** (`BIO_ATT_AUTO_BRANCH=29,4`). Whether/where the scheduler runs for sls is a deployment question (Q9).
3. The posting step runs **after** `bprocess_core` + `b_atten_core` for the same date, so a
   date that was never processed (35 of the last 53 days on sls) is built and posted in one
   run; the open day is re-run after the last punch arrives (previous-day re-processing
   already exists).
4. **Backfill** — one-off script `scripts/post_bio_attendance_backfill.py --tenant sls --branch 29 --from 2026-07-01 --to … [--apply]`, dry-run by default, owner's OK before `--apply`.

### 4.3 Report changes (small, after posting lands)

- **DPR hands**: add the general-shift row (`G`) so staff on `GS` are visible, and split the hands block into *Workers* / *Staff* (by `attendance_source = 'BIO'` or by category) so day-to-day comparability of worker hands is kept; hands/MT — owner's call (Q7) whether staff count in the ratio.
- **Hands Report**: staff designations have no norm → they land in the "no norm" note. Add a Staff group (or a toggle) so they are listed but kept out of excess/short totals, same as kind N.
- **HRMS reports**: source label `Bio`; Attendance Summary / Full Attendance just gain rows. *Bug found on the side:* Hands Complement filters hardcoded category ids for branches 4 and 87 only — branch 29 is excluded entirely (separate fix).
- **Daily P&L (ITM-027) wages line**: hand-days × ₹ proxy — staff rows would inflate it unless filtered to workers (Q7).

### 4.4 Processes that must keep ignoring bio rows

EJM / NJM wages data collection, Cash Hands, S4 incentive and any pay process: they already
restrict to pay-scheme-mapped employees / T-numbers, but each gets an explicit
`attendance_source <> 'BIO'` guard so a staff member accidentally mapped to a scheme can never
be paid daily wages from a punch. Exact list: §7.

### 4.5 Unlinked device users (13 real codes)

After the duplication bug is fixed only 13 device users remain unlinked, and they look like
workers (C/L/T code families) who also mark on the app, plus a test id. Add to the Bio
Attendance page an **Unlinked punches** view (code, punches, first/last, device, candidate
employees by numeric tail) with a "link to employee" action (reuses the Employee Bio Link
dialog) so HR decides case by case; the bulk-import scripts
`dbqueries/migrations/run_import_biomap_*.py` remain for mass loads. Only linked people can
ever be posted — `daily_attendance` requires an `eb_id` — and a linked worker who also marks
on the app is covered by the precedence rule (app wins).

### 4.6 Persons who are not employees at all

Contractor gangs under one T-number, security agency, canteen etc. have no employee record.
Posting needs an `eb_id`, so either HR creates records for them (category CONTRACTOR /
STAFF) or they stay out. Owner to say whether a "persons" master is wanted (Q2).

### 4.7 Schema / migration

No change to `daily_attendance` (`attendance_source` is varchar). A general-shift row is
needed for branch 29: one `shift_mst` row (G, 10:00–18:00, 8 h) + one `spell_mst` row
(`GS`), per branch that posts (Q3). The 2026-09-16 attendance indexes are already on dev3 and
sls.

### 4.8 Tests

Pure mapping function (process row → daily_attendance params) unit-tested; posting SQL
binding + idempotency + precedence tested with the mocked session pattern of
`src/test/test_hrms_*`; a dry-run on dev3 (8.7k raw punches present, no links yet — links must
be created on dev3 first) before sls.

## 5. Rollout

dev3 first (link a few employees, run the tab, verify Hands Report / DPR / attendance
reports), then sls branch 29 backfill from 2026-07-01 (device start) after the owner's go,
then switch the hourly posting on wherever the scheduler is deployed.

## 6. Open questions for the owner

1. **Scope of "other staff / persons":** (a) the STAFF category that already punches on the
   biometric devices (80 people at EJM, 27 of them status JOINED) — yes by default; (b) the 13
   genuinely unlinked device users (worker-like codes) — HR to link case by case, or ignore;
   (c) people with no employee record at all — in or out?
2. Do non-employee persons (contractor gangs, security, canteen) need their own master so
   they can be counted, or are they out of scope?
3. General shift: staff on `GS` (10:00–18:00) — is there / should there be a `G`/`GS` spell in
   `spell_mst` for branch 29, and should staff hands show under a separate "General" shift
   column in DPR / Hands Report?
4. Hours rule for staff: actual punch hours (noisy: 0.25 h to 29 h seen, overtime rows) or a
   flat 8 h per present day (staff are salaried, so "hands" = present)? Suggested: 8 h for a
   day with > 4 h, 4 h for 2–4 h, else 0, overtime rows dropped.
5. (a) Status of the posted row: approved (`3`, same as the app) or open (`1`) for HR to
   approve on the Attendance Calendar? (b) Which **employees** may be posted: only status
   JOINED (27 of the 80 punching staff) or also OPEN / DRAFTED records (53), which would mean
   HR first completes those employee records?
6. Precedence when the same person has an app mark and a bio punch on the same day: app wins
   (suggested), bio wins, or both when the spells differ?
7. Reports: staff **added to the same totals** (DPR total hands, hands/MT, Daily P&L wages
   line) or shown as a **separate Staff line with worker totals unchanged**? Suggested: separate
   line, hands/MT stays worker-only, with a "incl. staff" figure beside it.
8. Hands Report: staff designations have no norms — list them in a Staff group with blank
   targets (suggested), or leave them out of that report entirely?
9. Branch / tenant scope and the hourly job: EJM branch 29 only, or also Limelight (87, 3 bio
   employees) and NJM (4, the branch the pipeline is currently configured for)? Should the
   scheduler run on this VM against sls, or only in production?
10. Backfill: post history from 2026-07-01 (device start) or from a cut-over date, e.g.
    1 Oct 2026? (The bio chain itself must be re-run for the 35 unprocessed days either way.)
11. May the punch-duplication bug (§2.5-1) be fixed and `bio_attendance_table` on sls
    deduplicated as part of this work (write on production sls, needs your go)?

## 7. Consumer audit (agent, 2026-10-24)

Every reader of `daily_attendance` / `daily_ebmc_attendance` under `src/`, and what happens
once staff rows with a bio source (and spell `GS`, actual hours, off-day `O` rows) appear.
Classes: **a** safe · **b** staff included, split/label needed · **c** must exclude (payments,
incentives, worker statistics) · **d** breaks something.

| file | function / query | class | required change |
|---|---|---|---|
| hrms/attendance.py | list / by_id | b | label for the bio source |
| hrms/attendance.py | `_DUP_ATTENDANCE_SQL` :54, `_check_spell_hours_cap` :357 | d | a bio row blocks a manual entry on the same spell; a 12 h bio row blocks any top-up → posting skips eb/date/spell already covered by M/F/A rows (and flat hours, Q4) |
| hrms/attendance.py | open/close slice :882-946 | d | "Open" flips bio rows to status 1 (drops them from every status=3 report) and the next rebuild re-closes them → exclude the bio source from the slice |
| juteProduction/hands_report_query.py `get_hands_actuals_query` :57 | actuals | b | staff land in their own designations (norm kind N); needs the Staff group / split |
| hands_report.py + UI handsReport | shifts a/b/c/g | a | G already handled |
| juteProduction/dprQueries.py `get_dpr_hands_query` :312 → dpr_reports.build_hands_section :1156 | hands block | d+b | `G` is not in `SHIFT_ORDER` ("A","B","C"): counted in Total, no row → A+B+C ≠ Total on screen. Add a G row; staff split |
| dprQueries.py `get_dpr_hands_by_dept_query` :348 → dpr_hands_stages | hands/MT | b/c | staff depts (mech/elec/general) fall in "common" and are spread into yarn/cloth hands/MT → exclude bio or show separately (Q7) |
| juteCosting/daily_pl_query.py `get_hands_daily_query` :145, daily_pl_breakdown_query.py :318-390 | wages line | c | staff hand-days priced at the worker wage → exclude bio |
| hrms/reportQueries.py attendance summary :16, spell-wise :332, shift-wise :365 | | b | GS / G appear as new columns (confirm UI pivots are dynamic) |
| reportQueries.py employee working :209 | | d | no status filter, `CEIL(SUM/8)` per month: a 9 h GS day = 1.125 days → staff exceed calendar days. Count distinct dates for bio rows (or flat 8 h) |
| reportQueries.py hands complement :403 | | a | hardcoded worker category ids (br 4/87 only; br 29 missing — separate bug) |
| reportQueries.py daily hands complement :1067 | | b/d | G only reaches totals; staff off-day `O` rows show as OT hands |
| reportQueries.py full attendance :513 | | b | source decode A/M/F/P only → add bio label |
| reportQueries.py absenteeism :562, worker master :107, overstay :695 | | b | improvement: staff now count as "seen" |
| reportQueries.py half-day :632 | | a/d | uses legacy `da.spell IN (A1,A2,B1,B2)` → safe only if posting leaves `da.spell` NULL (so: leave `spell` NULL, use `spell_id`) |
| reportQueries.py **extra advance payment** :1137/1213 | | **c** | pays every status-3 type-`O` row (catch-all rate) → staff off-day work gets a priced advance. Exclude bio |
| hrms/ejmWagesData.py ATT arm :53, NSH :130, INC :237 (+ ejmTextPrint.py:351 trainee variant without scheme join), loom mistry :2478 | wages | **c** | staff on the scheme being built / eligibility list get bio hours + OT in wages. Exclude bio in every arm |
| ejmWagesData.py cash arms :1198, hrms/cashHands.py :109 | cash | a/c | safe for staff (T-prefix); becomes c if outsiders are ever bio-posted |
| hrms/holidayMaster.py holiday credit :111 | | c | staff with eligibility 'Y' get holiday hours from bio rows. Exclude bio |
| hrms/njmWagesData.py arms :250-1443 | | a/c | safe while posting is branch 29 only; c if extended to NJM |
| juteProduction/reportQueries.py attendance checklist :569 | | b | add a source column |
| juteProduction/long_absenteeism_query.py :96 | | **c** | no category filter → staff with unrecorded leave become long-absentee candidates (letters, auto-Terminate). Exclude STAFF / bio |
| lowprod_*, s4_incentive, spinning/weaving/finishing/winding entries, loom_modification_impact | | a | all keyed on `daily_ebmc_attendance` or EB prefix — fine as long as posting never writes ebmc rows |
| juteProduction/winding_sync_query.py :54 → winding_sync.py :141 | | **d** | bio rows on the same spell_id defeat the "attendance not captured yet" guard → remove_absent could wipe the winder map. Exclude bio |
| hrms/bioAttendance.py `_LAST_DAILY_ATT_SQL` :620 | | b/d | feeds back on itself after the first post (official designation changes never picked up) → exclude bio there |
| mobileapp attendance.py `_spell_hours_conflict` :141, `FIND_DUPLICATE_ATTENDANCE`, `_supersede` :413 | | d | for people in both populations an offline app punch deactivates the bio row or gets parked as `dup_of` it → keep the populations apart; posting skips covered eb/date/spell |
| mobileapp employees/query.py last-worked pre-fill | | b | exclude bio rows from the pre-fill |
| mobileapp dashboard.py :106-201, query.py `COUNT_PRESENT_*` | | b | Face bucket already lists the literal `'BIO'`; Face + Manual ≠ Total → pick the source value deliberately and give bio its own bucket |
| UI dailyProductionReport/page.tsx :800 | | d | inherits the missing G row |

**Schema facts (read-only, dev3 + sls):** unique index `daily_attendance_unique` =
(attendance_date, eb_id, eb_no, created_by, is_active, spell, worked_department_id,
working_hours, worked_designation_id); `attendance_source` is varchar(255). The rebuild must
therefore **hard-DELETE** the previous bio rows for the date (soft-deleting collides on the
second run), and `spell` should stay NULL (half-day report + non-binding key).

**Highest-risk items:** extra advance payment; the four EJM wage arms; holiday credit; long
absenteeism auto-flow; Daily P&L wages line; winding-sync guard; dup/cap/open-close checks;
DPR missing G row + inflated hands/MT.

**Source code value:** `bio_attendance_process.attendance_source` is already `'BIO'` (SJM
convention) and the mobile dashboard knows the literal. Proposal: write `'BIO'` into
`daily_attendance.attendance_source` for parity, and give it its own bucket in the dashboard
and the HRMS source labels (`A`/`M` Manual, `F` Facial, `BIO` Biometric).

## 8. Bio data profile (agent, 2026-10-24, read-only on sls; SQL kept in the session scratchpad)

- `shift_mst`: A/B/C per branch (br 29: 7 A 06–14 8 h, 8 B 11–22 8 h, 9 C 22–06 7.5 h
  overnight); no general shift anywhere. `spell_mst` has no branch column (via `shift_id`);
  `spell_code` = `spell_name` always; codes repeat across branches, so lookups go through
  `shift_mst.branch_id`. Branch 87 has no A/B and uses A1/B1 as its 8 h spells.
- Bio spell label semantics (`_spell_label`): R row = regular hours capped at 8; O row =
  overtime; label from first-punch hour and an hours bucket (> 420 min full label, 180–420
  half label, less → `O`). `spell_hours` is always written as 8.0.
- Branch-29 staff, Sep–Oct: 80 employees, 912 employee-days; work-minute buckets < 120: 50
  (all off-day-present), 120–359: 28, 360–479: 349, 480–600: 472, > 600: 13; `actual_out`
  NULL on 507 (55.6 %); 290 days with OT; 69 days > 10 h, 15 days > 12 h. Raw punches per
  employee-day: 1 punch 18 %, 2 punches 76 %, 3+ 5 %. Staff punching per day: min 7, avg 61,
  max 77; Sunday lowest (38).
- Oct 1–23 side by side (app hands = Σ(working − idle)/8, status 3; bio hands = Σ hours/8):
  10-04 app 741 / bio 74 (10.0 %), 10-05 770 / 52, 10-11 291 / 24, 10-12 881 / 62, 10-21 24 /
  7, 10-22 663 / 61, 10-23 (partial) 739 / 44; unprocessed days would add ≈ 60–65 hands.
- Link table: 280 rows, clean keys; 175 links are branch-29 STAFF, the rest HO/other
  branches; 136 linked employees are personal-inactive (resigned/terminated), 151 links had
  no punches in Sep–Oct. Branch 87: 3 bio people (2 trainees, 1 resigned manager still
  punching). Branch 4: 5 links, no punches since Jul.
- Overlap bio vs app: 0 employees, 0 days, any branch, any status.
- Processing state: latest raw punch 2026-10-23 12:46, latest basic row 2026-10-23 (run
  12:50), `punch_logs` already at 2026-10-24 18:38 (258 punches not yet synced). Basic
  coverage by month: Jul 24/24, Aug 31/31, Sep 11/30, Oct 7/23.
- Near-shutdown days (10-02, 10-19…10-21) are real in both sources.

