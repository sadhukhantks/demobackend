# Long Absenteeism follow-up (name struck off) — build contract

Date: 2026-10-11. Owner: sidheshsarda. Status: approved to build; lands uncommitted on `claudehost`
(both repos, main checkouts) for live review on dev3; tables + menu on dev3 AND sls.

Design doc (owner-facing, with diagrams): claude.ai artifact "Long Absenteeism Monitoring & Name
Struck-Off — Design and Process (VoWERP3)". This file is the engineering contract: names, tables,
endpoints, files, rules. Every agent builds to it; if something here is impossible, say so in the
hand-back instead of silently diverging.

## 1. What it is

The Personnel office runs an Excel "Long Absent Report" + three Word letters to deal with workers
absent for long periods: pick workers absent ≥ N days, exclude ESI / STL cases (labour office),
confirm with time keepers, post three warning letters by registered post with AD, a show cause,
a gate-board notice served on the unions, then strike the name off the muster roll. This feature
keeps one tracked **case** per worker in the Jute Production module, drafts the letters from
templates, records when each letter was **sent** (the legal sequence counts from sent dates, which
differ from letter dates), keeps Excel-style free remarks, and closes with a struck-off approval.

### Owner decisions (2026-10-11)

1. ~~Warnings 1/2/3 use the same text, numbered~~ — superseded by amendment 11.14: each warning
   has its own template (mild 1st, firmer 2nd, the owner's last-and-final 3rd); the 2nd and 3rd
   still cite the earlier letters with their dates and sent dates (`{previous_notices}` placeholder).
2. Confirming struck off **sets the employee Terminated (41)** automatically with reason
   "Struck off from muster roll, long absence" (reuse `/employee_status_update` logic).
3. Struck off goes through **one approval level** via the standard approval engine
   (`approval_mst` for this menu + branch; no row → edit access approves directly).
4. Tables + menu on **dev3 and sls together**.
5. Users must record the **sent date** of every letter (issue date ≠ sent date). The wait for the
   next letter counts from the previous letter's sent date; the next letter is blocked until the
   previous sent date is recorded (edit users may `force` with a reason; `forced=1` is kept).
6. Defaults (editable on Settings): detect 10 days, ~~lookback 90 days~~ lookback 0 = no limit
   (amendment 11.18), waits 10/10/15, reply windows
   3 (show cause) / 7 (gate notice), dismiss snooze 30, ~~exclude T-codes~~ excluded EB prefixes
   T,C,R,L (amendment 11.17), clause 14(C)(V) text,
   signatory "Personnel Manager". Union names free text with suggestions. English only.
7. **Mobile**: the page must be usable from a phone; the Reports tab is designed phone-first.

## 2. Files and ownership

**Location (amendment 11.20, owner 2026-10-12):** the page lives in the **HRMS** module — menu
`hrms/longAbsenteeism` (migration `20261012_long_absenteeism_menu_to_hrms.sql`), frontend route
`vowerp3ui/src/app/dashboardportal/hrms/longAbsenteeism/` (moved with `git mv`; the old route
`juteProduction/report/longAbsenteeism/page.tsx` is a client redirect that keeps the query string).
The **backend files stay in `src/juteProduction/`** under the unchanged `/api/longAbsenteeism` prefix
(both routers); only `long_absenteeism_common.MENU_PATH` changed. The `juteProduction/report/...`
frontend paths in the tables below are the original (pre-move) locations.

Already written (do not restructure; extend only where this file says so):

| Path | Holds |
| --- | --- |
| `vowerp3be/dbqueries/migrations/20261011_jute_prod_absent_tables.sql` | the 6 tables + functional unique key `uq_jpac_live` |
| `vowerp3be/dbqueries/migrations/20261011_jute_prod_absent_menu.sql` | menu row `juteProduction/report/longAbsenteeism` + role grants copied from Low Performer Follow-up |
| `vowerp3be/src/juteProduction/long_absenteeism_models.py` | ORM: `JuteProdAbsentRule`, `JuteProdAbsentCase`, `JuteProdAbsentLetter`, `JuteProdAbsentUnionReceipt`, `JuteProdAbsentEvent`, `JuteProdAbsentTemplate` |
| `vowerp3be/dbqueries/migrations/20261012_long_absenteeism_menu_to_hrms.sql` | menu row `hrms/longAbsenteeism` (child of HRMS), grants + struck-off approvers moved from the old row, old row deactivated (amendment 11.20) |
| `vowerp3be/dbqueries/migrations/20261012b_jute_prod_absent_letter_recorded.sql` | `jute_prod_absent_letter.recorded` (amendment 11.20) |
| `vowerp3be/src/juteProduction/long_absenteeism_pdf.py` | `build_letters_pdf(letters, company) -> bytes` (reportlab, amendment 11.20) |
| `vowerp3be/src/juteProduction/long_absenteeism_common.py` | codes + labels, `RULE_DEFAULTS` / `load_rule`, `TEMPLATE_DEFAULTS`, `PLACEHOLDERS`, `merge_text`, `previous_notices_sentence`, `fmt_dmy`, parsing helpers, access (`require_access`, `require_branch_access`, `menu_id`), loaders (`load_case`, `load_letter`, `require_live`, `require_open`), `add_event`, `case_to_dict`, `letter_to_dict`, `event_to_dict`, `codes_payload`, `next_stage_of`, `due_date_for_next`, `wait_days_for`, `reply_days_for` |

To build:

| Owner | Path | Holds |
| --- | --- | --- |
| **BE-1** | `src/juteProduction/long_absenteeism.py` (`router`, alias `long_absenteeism_router`) | setup, candidates, list, get, open, dismiss, verify, update, park, resume, exit, reopen, events, performance |
| BE-1 | `src/juteProduction/long_absenteeism_query.py` | all `text()` SQL for the above |
| BE-1 | `src/test/test_long_absenteeism.py` | tests for BE-1 + the pure helpers in `_common` |
| BE-1 | `src/main.py` | **two** lines: import + `app.include_router(long_absenteeism_router, prefix="/api/longAbsenteeism", tags=["jute-production-long-absenteeism"])` next to the follow-up lines (157 / 383). Add ONLY when the module imports cleanly (uvicorn `--reload` is live; a broken import takes the shared dev server down) |
| **BE-2** | `src/juteProduction/long_absenteeism_letters.py` (`router`, alias `long_absenteeism_letters_router`) | rule, templates, letters (preview / issue / batch / update), dispatch, union receipts, struck-off approval, reports |
| BE-2 | `src/juteProduction/long_absenteeism_letters_query.py` | SQL for the above |
| BE-2 | `src/test/test_long_absenteeism_letters.py` | tests |
| BE-2 | `src/main.py` | import + `app.include_router(long_absenteeism_letters_router, prefix="/api/longAbsenteeism", tags=["jute-production-long-absenteeism"])` (same prefix, second router; same caution) |
| BE-2 | `src/common/attachments/constants.py`, `router.py` | three new modules + gate (section 7) |
| BE-2 | `src/hrms/employee.py` | extract `apply_lifecycle_status(db, eb_id, new_status_id, on_date, reason, remarks, user_id)` from the body of `employee_status_update` (lines ~716-776) WITHOUT changing the endpoint's behaviour (it calls the helper; no commit inside the helper — the endpoint commits as before) |
| **FE-1** | `vowerp3ui/src/app/dashboardportal/juteProduction/report/longAbsenteeism/` (`page.tsx`, `_hooks/`, `_components/`, `_schemas.ts`, `_utils.ts`, tests) | watchlist, case page, dialogs, letter preview + print, mobile layouts, tab shell |
| FE-1 | `vowerp3ui/src/utils/longAbsenteeismService.ts`, `src/utils/api.ts` (`LA_*` constants in `apiRoutesPortalMasters`, after the `LPF_*` block) | ALL service functions of section 6 (both routers), typed + guarded |
| **FE-2** (after FE-1) | `.../longAbsenteeism/_components/tabs/DispatchTab.tsx`, `ReportsTab.tsx`, `TemplatesTab.tsx`, `SettingsTab.tsx`, `_export.ts` | the four tabs + Excel export in the sheet's column order |

Reference implementations to copy from: `src/juteProduction/low_performer_followup*.py`,
`src/test/test_low_performer_followup.py`, FE `src/app/dashboardportal/juteProduction/report/lowPerformerFollowup/`,
`src/utils/lowPerformerFollowupService.ts`. Both repos' `CLAUDE.md` apply (sync `def` handlers,
`{"data": ...}`, `text()` + named binds, `None` for NULL, no `any`, Zod, `SearchableOptionSelect`,
`useReportBranches`, tokens for colours).

## 3. Data model

See the migration for columns. Key semantics:

- `jute_prod_absent_case.status`: 1 open, 2 parked (a reply is under review; the case does not
  age — `next_due_date` is pushed on resume), 0 closed. `uq_jpac_live`: one live (status ≠ 0)
  active row per (co_id, branch_id, eb_id) — guard in code with a SELECT AND map `IntegrityError`
  on that key to 409 like the follow-up does.
- `stage_code` = the last step completed (`DETECTED` … `STRUCK_OFF`); `stage_date` = when it was
  entered; `next_due_date` = when the next step is possible (`due_date_for_next`).
- Snapshot columns are written once at `/open` and never re-read from the master.
- `jute_prod_absent_letter`: `issue_date` (printed), `sent_date` (recorded later), `reply_due_date`
  (= issue_date + reply days for SHOW_CAUSE / GATE_NOTICE / warnings' 7 days), `batch_ref`
  (`B<yyyymmdd>-<HHMM>-<user_id>`), `forced`.
- `status_id` / `approval_level` on the case are ONLY the struck-off approval (NULL = not started,
  20 pending, 3 approved = struck off done, 4 rejected).
- Codes: `long_absenteeism_common.py` is the single source. The FE gets labels from `/setup`.
- Leave link columns (amendment 11.19, migration `20261012_jute_prod_absent_leave_link.sql`):
  `leave_type_id BIGINT NULL`, `leave_type_name VARCHAR(100) NULL` (snapshot of the type's
  description, its code when blank), `leave_from DATE NULL`, `leave_to DATE NULL`,
  `leave_transaction_id BIGINT NULL` (non-null = linked to an approved leave in the leave module;
  NULL = not linked: a manual exit pick, or none), `leave_parked TINYINT NOT NULL DEFAULT 0`
  (1 = the case was parked by the leave refresh, not by a person).

## 4. Business rules

### 4.1 Candidates (BE-1, `GET /candidates`)

Workers on the roll who were seen in the lookback window but not in the last `detect_days`:

```sql
WITH seen AS (
  SELECT eb_id, MAX(seen_date) AS last_seen FROM (
    SELECT da.eb_id, da.attendance_date AS seen_date
      FROM daily_attendance da
     WHERE da.is_active = 1 AND COALESCE(CAST(da.status_id AS UNSIGNED), 0) <> 4
       AND da.branch_id = :branch_id
       AND da.attendance_date <= :as_on
       AND (:from_date IS NULL OR da.attendance_date >= :from_date)   -- 11.18
    UNION ALL
    SELECT lt.eb_id, ltd.leave_date
      FROM leave_tran_details ltd
      JOIN leave_transactions lt ON lt.leave_transaction_id = ltd.ltran_id
     WHERE ltd.is_active = 1 AND lt.status = 3 AND lt.branch_id = :branch_id
       AND ltd.leave_date <= :as_on
       AND (:from_date IS NULL OR ltd.leave_date >= :from_date)        -- 11.18
  ) g GROUP BY eb_id
)
SELECT pd.eb_id, od.emp_code, <name>, dm.dept_id, dm.dept_desc, sd.sub_dept_desc, dsg.desig AS designation,
       cm.cata_desc AS category, seen.last_seen, DATEDIFF(:as_on, seen.last_seen) AS absent_days
  FROM seen
  JOIN hrms_ed_personal_details pd ON pd.eb_id = seen.eb_id AND pd.active = 1
       AND pd.status_id NOT IN (<_SEPARATED_STATUS_SQL from src/hrms/reportQueries.py>)
  LEFT JOIN hrms_ed_official_details od ON od.eb_id = pd.eb_id AND od.active = 1
       AND od.tbl_hrms_ed_official_detail_id = (SELECT MAX(o2.tbl_hrms_ed_official_detail_id)
             FROM hrms_ed_official_details o2 WHERE o2.eb_id = pd.eb_id AND o2.active = 1)
  LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = od.sub_dept_id
  LEFT JOIN dept_mst dm ON dm.dept_id = sd.dept_id
  LEFT JOIN designation_mst dsg ON dsg.designation_id = od.designation_id
  LEFT JOIN category_mst cm ON cm.cata_id = od.catagory_id
 WHERE DATEDIFF(:as_on, seen.last_seen) >= :detect_days
   AND (:exclude_regex IS NULL OR od.emp_code IS NULL OR od.emp_code NOT REGEXP :exclude_regex)  -- 11.17
   AND (:dept_id IS NULL OR dm.dept_id = :dept_id)
   AND NOT EXISTS (SELECT 1 FROM jute_prod_absent_case c WHERE c.eb_id = pd.eb_id AND c.co_id = :co_id
                   AND c.branch_id = :branch_id AND c.active = 1
                   AND (c.status <> 0 OR (c.exit_reason = 'DISMISSED' AND c.snooze_until >= :as_on)
                        OR (c.exit_reason IN ('ESI','STL','MATERNITY','MEDICAL') AND c.expected_return_date IS NOT NULL
                            AND c.expected_return_date >= :as_on)))
 ORDER BY dm.dept_desc, od.emp_code
```

`from_date = as_on - lookback_days`, or NULL (no lower bound) when `lookback_days = 0` (amendment
11.18). `as_on` defaults to today (IST). The `official_details`
scalar pick mirrors `_emp_code_expr` in `src/juteProduction/reportQueries.py` (branch transfers
leave several rows). Reuse `_SEPARATED_STATUS_SQL`. Company scope: `verify_branch` already proves
the branch belongs to `co_id`. Note `daily_attendance.status_id` is **varchar**.

Also return, per candidate, `closed_cases` (count of earlier closed cases of that eb in the
branch) so the UI can flag repeat absentees.

### 4.2 Opening a case (BE-1, `POST /open`)

Body `{co_id, branch_id, eb_id, last_working_date?, remarks?}`. Level create. Steps:

1. `require_branch_access(create)`; load the worker in the branch (official row active, same
   branch — 400 "Worker not found in this branch" otherwise); 409 if a live case exists.
2. Snapshot from `hrms_ed_personal_details` (name parts, `father_spouse_name`, `mobile_no`),
   `hrms_ed_official_details` (emp_code, `date_of_join`, sub_dept → dept, designation, category),
   `hrms_ed_address_details` (active row, prefer `is_correspondent_address = 1`; one line:
   `address_line_1, address_line_2, city_name, state name, pin_code`), `hrms_ed_esi.esi_no`,
   `hrms_ed_pf.pf_no`, `hrms_ed_contact_details.mobile_no` (fallback), and the last attendance
   row (`daily_attendance` latest active non-rejected row for the eb; `last_spell` = spell letter
   via `spell_mst.spell_code` / `da.spell`; `last_machine` = `machine_mst.machine_name` of the
   latest `daily_ebmc_attendance` row joined through `daily_atten_id`, if any).
3. `last_working_date` = body value if given, else the candidate "last seen" (same union of
   attendance + approved leave, no lookback). 400 if none can be found.
4. `detected_date = today`, `absent_days_at_open = today - lwd`, `stage_code = DETECTED`,
   `stage_date = today`, `next_due_date = today`, `status = 1`, `case_no = LA/<branch_id>/<yy>/<seq>`
   where seq = count of cases of that branch in the calendar year + 1, zero-padded 4.
5. `add_event(SYSTEM, "Case opened. Last working date dd.mm.yyyy, N days absent.")`, commit,
   return `{"data": case_to_dict(case)}`.

### 4.3 Verification (BE-1, `POST /verify`)

Body `{co_id, case_id, esi_stl_result: NONE|ESI|STL, timekeeper_confirmed: bool, habitual: bool, note?}`.
Level create; case must be open and at `DETECTED` (re-verifying at a later stage only updates the
fields + event, never moves the stage back). Sets the fields and `verified_date = today`. If
`esi_stl_result = NONE` and `timekeeper_confirmed` → `stage_code = VERIFIED`, `stage_date = today`,
`next_due_date = today` (warning 1 due now). ESI / STL does NOT auto-exit (the FE offers `/exit`
with that reason). Event `VERIFY` with a one-line summary. (amendment 11.19: the manual verification step is dropped;
leave comes from the leave module)

### 4.4 Remarks and extras (BE-1, `POST /update`)

Body `{co_id, case_id, remarks?, extra_1..extra_5?, habitual?, last_working_date?}`; only sent
keys change. Level create for remarks / extras / habitual, **edit** for `last_working_date`
(recompute `absent_days_at_open`; event SYSTEM "Last working date changed from … to …"). Allowed
on closed cases too (remarks are Excel cells). No event for remarks.

### 4.5 Park / resume / exit / reopen / dismiss (BE-1)

- `POST /park {co_id, case_id, note}` create: open → parked; event PARK. 400 if `status_id = 20`.
- `POST /resume {co_id, case_id, note?}` edit: parked → open; `next_due_date = max(next_due_date, today)`;
  event RESUME.
- `POST /exit {co_id, case_id, exit_reason (USER_EXIT_REASONS), exit_date, exit_remarks?, expected_return_date?}`
  edit: live → closed (`status = 0`), stage unchanged, 400 if `status_id = 20` ("struck-off
  approval pending — reject it first"); event EXIT "<label>: remarks". (amendment 11.19: leave types from the leave module)
- `POST /reopen {co_id, case_id, note}` edit: closed → open, clears exit fields (the event keeps
  them), `next_due_date = max(old, today)`; 400 if `exit_reason = STRUCK_OFF` (the employee has
  left the roll — HR reverses that in HRMS) or if a live case exists for the eb (409). Event REOPEN.
- `POST /dismiss {co_id, branch_id, eb_id, reason, snooze_days?}` create: a candidate that should
  not become a case. Creates a CLOSED case row (snapshot as in 4.2 but minimal: emp_code, name,
  dept, designation; `stage_code = DETECTED`, `status = 0`, `exit_reason = DISMISSED`,
  `exit_date = today`, `exit_remarks = reason`, `snooze_until = today + (snooze_days or rule)`),
  event DISMISS. 409 if a live case exists.

### 4.6 Rejoin detection (BE-1, on `GET /list` and `GET /get`)

For the branch's live cases: one query over `daily_attendance` (active, non-rejected,
`attendance_date > case.last_working_date`, same eb) → `MIN(attendance_date)`; store it in
`rejoined_seen_date` when it changes (commit silently, no event). Rows with a value land in the
"Marked present" band. `absent_days` (live) stops at `rejoined_seen_date`. (amendment 11.19: the leave refresh and leave parking run with it)

### 4.7 Letters (BE-2)

Issue order and gates (`next_stage_of`, `PREVIOUS_STAGE`, `due_date_for_next`):

| Letter stage | Allowed when `stage_code` is | Wait (from previous letter's `sent_date`) | Reply window |
| --- | --- | --- | --- |
| WARNING_1 | VERIFIED | none | 7 days |
| WARNING_2 | WARNING_1 | `wait_w2_days` | 7 |
| WARNING_3 | WARNING_2 | `wait_w3_days` | 7 |
| SHOW_CAUSE | WARNING_3 | `wait_sc_days` | `sc_reply_days` |
| GATE_NOTICE | SHOW_CAUSE | `sc_reply_days` (the show-cause window) | `notice_reply_days` |
| STRUCK_OFF (memo, optional) | STRUCK_OFF (after approval) | — | — |

`POST /letter_preview {co_id, case_id, stage_code?}` (view): returns the merged subject/body for
the next stage (or the given one) plus `blocked_reason` (string or null), `can_force` (true when
the caller has edit) and `editable: false` (amendment 11.15: the text is never edited by hand). Blocked reasons, in order: case not open; wrong stage; previous letter has no
`sent_date` ("Record the sent date of the <previous> first"); today < due date ("Due on dd.mm.yyyy").

`POST /letter_issue {co_id, case_id, stage_code, issue_date?, dispatch_mode?, force?, force_reason?}`
(edit): same checks; the subject / body are merged on the server from the company's template for
the stage (amendment 11.15 — `subject` / `body` sent by an older client are ignored); with `force = true` + reason (edit level) the wait / missing-sent-date blocks
are bypassed and `forced = 1`, event STAGE text includes "FORCED: <reason>". Inserts the letter
(`letter_no` for warnings, `template_code`, `reply_due_date`), advances `stage_code`,
`stage_date = issue_date`, `next_due_date = due_date_for_next(...)` (which uses sent_date when
present, else issue_date), event STAGE "<stage label> issued, letter dated …", returns
`{"data": {"letter": ..., "case": ...}}`. 409 if a letter for that stage already exists (active).

`POST /letter_issue_batch {co_id, branch_id, case_ids[], issue_date?, dispatch_mode?}` (edit):
for each case, preview with the default template text and issue if not blocked; never forces.
One `batch_ref` for all. Returns `{"data": {"batch_ref", "issued": [{case_id, letter_id, stage_code}], "skipped": [{case_id, reason}]}}`.

`POST /letter_update {co_id, letter_id, sent_date?, dispatch_mode?, ad_no?, delivery_status?, delivery_date?, gate_display_date?, dept_display_date?}`
(edit): only sent keys change; when `sent_date` is set / changed, recompute the case's
`next_due_date` via `due_date_for_next` (if the letter is the latest of the case) and add event
SYSTEM "<stage> sent on dd.mm.yyyy (A.D. …)". `sent_date` ≥ `issue_date` else 400.

`GET /letters?co_id&case_id` (view): letters (active) with `receipts[]` and the latest active
attachments per letter (`signed_copy`, `ad_card`: id + filename) via `file_attachment`
(module `jute_prod_absent_letter`, entity = letter_id).

`GET /letters_due?co_id&branch_id&stage_code?` (view): live open cases whose next stage's letter is
issuable today (not blocked), with `next_stage_code`, for the batch dialog.

`GET /dispatch?co_id&branch_id&date_from&date_to&batch_ref?&stage_code?` (view): letters issued in
the range (join case: emp_code, emp_name, dept_desc, case_id) flat rows, newest batch first.

Merge fields: build from `case_to_dict`, the rule, `co_mst` (`co_name`, address lines + zipcode;
join through `branch_mst` as `src/inventory/query.py` ~100-125 does) and the case's earlier letters
(`previous_notices_sentence`, `warning1_date/_sent` … `show_cause_date/_sent`, `gate_notice_date`).
`letter_date = issue_date`, `absent_days` as on issue_date, `reply_days = reply_days_for(stage)`,
`reply_due_date`, `letter_no`, `letter_ordinal`, `sc_reply_days = rule`. Dates through `fmt_dmy`.
Pure function `build_merge_fields(case_dict, letters, rule, company, stage_code, issue_date) -> dict`
unit-tested.

### 4.8 Templates and rule (BE-2)

- `GET /templates?co_id` (view): the 6 templates (amendment 11.14: WARNING_1, WARNING_2, WARNING_3,
  SHOW_CAUSE, GATE_NOTICE, STRUCK_OFF_MEMO) — DB row if present else `TEMPLATE_DEFAULTS`,
  each `{template_code, label, lang, subject, body, is_default}` + `placeholders`.
- `POST /template_save {co_id, template_code, subject, body}` (edit): upsert (`ON DUPLICATE KEY` or
  ORM get-or-create on `(co_id, template_code, 'en')`); body ≤ 20000 chars; 400 on unknown token
  `{xyz}` not in `PLACEHOLDERS` (report the token).
- `POST /template_reset {co_id, template_code}` (edit): delete the row (hard delete is fine) → default.
- `GET /rule?co_id` (view): `load_rule`. `POST /rule_save {co_id, ...}` (edit): validate ints
  (0..365; `detect_days ≥ 1`; `lookback_days` 0 = no limit or `detect_days`..3650, amendment 11.18),
  texts ≤ max, `exclude_prefixes` letters only (amendment 11.17);
  upsert the row; return the merged rule.

### 4.9 Union receipts (BE-2)

`POST /receipt_create {co_id, letter_id, union_name, received_by?, received_date?, remarks?}`,
`POST /receipt_update {co_id, receipt_id, ...}`, `POST /receipt_deactivate {co_id, receipt_id}`
(edit; the letter must be a GATE_NOTICE of a live or struck-off case), `GET /union_names?co_id&branch_id`
(view; distinct `union_name` of the branch's receipts, most used first).

### 4.10 Struck-off approval (BE-2) — standard engine

`menu_id` = `long_absenteeism_common.menu_id(db)`. Queries for the engine:
`get_doc_fn` → `SELECT status_id, approval_level, branch_id FROM jute_prod_absent_case WHERE case_id = :case_id`;
`update_status_fn` → `UPDATE jute_prod_absent_case SET status_id = :status_id, approval_level = :approval_level, updated_by = :updated_by, updated_date_time = :updated_date_time WHERE case_id = :case_id`.

- `POST /struck_off/send {co_id, case_id}` (edit): case open, `stage_code = GATE_NOTICE`, the gate
  notice letter has `sent_date` and today ≥ its `reply_due_date` (else 400 with the date), `status_id`
  NULL / 4 → set `status_id = 20`, `approval_level = 1`; event APPROVAL "Sent for struck-off approval".
- `POST /struck_off/approve {co_id, case_id, struck_off_date?}`: `process_approval(..., commit=False)`;
  the engine itself checks the approver (hierarchy or edit fallback). If `new_status_id == 3`:
  `stage_code = STRUCK_OFF`, `stage_date = struck_off_date or today`, `struck_off_date`, `status = 0`,
  `exit_reason = STRUCK_OFF`, `exit_date`, `next_due_date = NULL`; call
  `apply_lifecycle_status(db, eb_id, 41, struck_off_date, "Struck off from muster roll, long absence", f"Case {case_no}", user_id)`;
  event APPROVAL "Struck off approved; employee marked Terminated". One commit. If the engine
  returns 20 (next level), only event APPROVAL "Approved at level n". Returns
  `{"data": {"case": ..., "approval": <engine result>}}`.
- `POST /struck_off/reject {co_id, case_id, reason}`: `process_rejection(..., menu_id=...)`; event
  APPROVAL "Struck-off rejected: reason". Case stays at GATE_NOTICE, open.
- `POST /struck_off/reopen {co_id, case_id}` (edit): `status_id` 4 → NULL; event APPROVAL.
- `GET /struck_off/permissions?co_id&case_id` (view): `calculate_approval_permissions(user_id, menu_id, branch_id, status_id or 1, approval_level, db)`
  (read its return shape in `src/common/approval_utils.py:473`) + `{status_id, approval_level, hierarchy_exists, can_send}`
  where `can_send` = caller has edit AND the send preconditions hold (return `send_blocked_reason` otherwise).

### 4.11 Reports (BE-2, all view)

- `GET /report/struck_off_register?co_id&branch_id&date_from&date_to&dept_id?` → rows: case_no,
  emp_code, emp_name, dept_desc, designation, last_working_date, warning1_date, warning1_sent,
  warning2_date, warning2_sent, warning3_date, warning3_sent, show_cause_date, gate_notice_date,
  struck_off_date, remarks. Struck-off cases with `struck_off_date` in range.
- `GET /report/stage_summary?co_id&branch_id&as_on?` → `{"data": {"by_dept": [{dept_desc, counts: {DETECTED: n, ..., STRUCK_OFF: n}, parked: n, total_live: n}], "exits": [{exit_reason, label, count}] (closed in the last 90 days), "as_on"}}`.
- `GET /report/monthly_trend?co_id&branch_id&months=12` → `[{month: 'YYYY-MM', opened, struck_off, rejoined, other_exits}]`.

### 4.12 Performance (BE-1, `GET /performance?co_id&case_id`)

Window = `[last_working_date - 30, last_working_date]`. Department mapping: designation starts with
`weaver` → WEAVING, `spinner` → SPINNING, dept/sub-dept name contains `WIND` → WINDING, else
`{"data": {"available": false, "reason": "..."}}`. Call
`src.juteProduction.low_performer_followup.fetch_dept_rows(db, dept, co_id, branch_id, d_from, d_to)`
then `build_person_rows(rows, eb_id)` and `build_person_summary(...)` (read their signatures); cap
the window to that dept's max days (weaving 31). Store `perf_avg_eff`, `perf_kg_per_hr`,
`perf_days_worked`, `perf_from`, `perf_to`, `perf_dept` on the case; return
`{"data": {"available": true, "dept": ..., "summary": {...}, "rows": [...]}}`. Level view (it writes
a cache only).

## 5. Access levels

| Action | Level |
| --- | --- |
| read anything, candidates, preview, permissions, reports | view (1) |
| open case, dismiss, verify, update remarks / extras / habitual, park, add / edit own REMARK / FEEDBACK / REPLY events, performance | create (3) |
| resume, exit, reopen, change last_working_date, issue letters (single / batch / force), letter_update, receipts, templates, rule, struck_off send / reopen, event deactivate | edit (4) |
| struck_off approve / reject | the approval engine decides (hierarchy, else edit) |

Every endpoint: id-based calls check the level on the stored case's `branch_id`; list-type calls
use `require_branch_access`. `/setup` returns `access_level` for the branch.

## 6. API contract (prefix `/api/longAbsenteeism`, all `{"data": ...}`)

BE-1 router:

| Method | Path | Query / body | Returns |
| --- | --- | --- | --- |
| GET | `/setup` | `co_id, branch_id` | `codes_payload()` + `depts: [{dept_id, dept_desc}]` (branch departments: `dept_mst` where `branch_id`), `rule`, `today`, `access_level` |
| GET | `/candidates` | `co_id, branch_id, as_on?, dept_id?` | `[{eb_id, emp_code, emp_name, dept_id, dept_desc, sub_dept_desc, designation, category, last_seen, absent_days, closed_cases}]` |
| GET | `/list` | `co_id, branch_id, view=due\|open\|parked\|closed\|all, dept_id?, stage_code?, q?` | rows = `case_to_dict` + `w1_date, w1_sent, w2_date, w2_sent, w3_date, w3_sent, sc_date, sc_sent, gn_date, gn_sent` (from letters) + `due_band: VERIFY\|LETTER_DUE\|MARKED_PRESENT\|REPLY_REVIEW\|APPROVAL\|NONE` + `next_stage_code` + `next_stage_label`. `view=due` = live cases with a band ≠ NONE. `q` matches emp_code / emp_name |
| GET | `/get` | `co_id, case_id` | `{case, letters (letter_to_dict + receipts + attachments), events (newest first), earlier_cases: [{case_id, case_no, detected_date, stage_code, exit_reason, exit_date}]}` |
| POST | `/open` | 4.2 | `case` |
| POST | `/dismiss` | 4.5 | `case` |
| POST | `/verify` | 4.3 | `case` |
| POST | `/update` | 4.4 | `case` |
| POST | `/park`, `/resume`, `/exit`, `/reopen` | 4.5 | `case` |
| GET | `/events` | `co_id, case_id` | `[event_to_dict + attachment_id, attachment_filename]` (module `jute_prod_absent_event`) |
| POST | `/event_create` | `{co_id, case_id, event_type ∈ USER_EVENT_TYPES, event_date, channel? (REPLY), contact_name?, text, park?}` create | `{event, case}` — a REPLY with `park` (default true) parks an open case |
| POST | `/event_update` | `{co_id, event_id, event_date?, channel?, contact_name?, text?}` create (own) / edit; user types only | `event` |
| POST | `/event_deactivate` | `{co_id, event_id}` edit; user types only | `{event_id, active: 0}` |
| GET | `/performance` | `co_id, case_id` | 4.12 |

BE-2 router: `/rule`, `/rule_save`, `/templates`, `/template_save`, `/template_reset`, `/letters`,
`/letters_due`, `/letter_preview`, `/letter_issue`, `/letter_issue_batch`, `/letter_update`,
`/dispatch`, `/letters_print`, `/letters_pdf`, `/letter_record` (amendment 11.20), `/receipt_create`,
`/receipt_update`, `/receipt_deactivate`, `/union_names`,
`/struck_off/send`, `/struck_off/approve`, `/struck_off/reject`, `/struck_off/reopen`,
`/struck_off/permissions`, `/report/struck_off_register`, `/report/stage_summary`,
`/report/monthly_trend` — shapes in section 4.

Errors: 400 validation (message names the field), 403 access, 404 not found, 409 duplicate live
case / duplicate stage letter, 500 with `logger.exception`. `rollback(db)` on failure.

## 7. Attachments (BE-2)

`src/common/attachments/constants.py`: add modules `jute_prod_absent_letter` (file types
`signed_copy`, `ad_card`), `jute_prod_absent_receipt` (`union_receipt`), `jute_prod_absent_event`
(`reply`, `medical`, `other`). `router._check_entity_access`: three branches that lazily import
`check_absent_attachment_access(db, token_data, co_id, entity_id, action, module)` from
`long_absenteeism_letters.py`: read needs view on the case's branch; upload needs create (event
module) / edit (letter, receipt modules); delete needs edit; the entity must be active and belong
to `co_id`. FE uses the existing `AttachmentUploader` with those module / file_type values.

## 8. Frontend contract

Route `src/app/dashboardportal/hrms/longAbsenteeism/page.tsx` (amendment 11.20; it was
`juteProduction/report/longAbsenteeism`, which now only redirects here with the query string; HRMS
pages have no "Back to …" layout, so none is added) (`"use client"`,
Suspense + `useReportBranches` + `useMounted`, `hasMenuAccess(pathname, "create"|"edit")` exactly as
the follow-up page). Views on ONE route: list (no `case_id`), case (`?case_id=N`). Tabs on the list
view via `?tab=watchlist|dispatch|reports|templates|settings` (default watchlist); tabs other than
watchlist / reports render only when `canEdit`. Never name a sub-route with `/edit`, `/create`,
`/print` (the proxy maps those to permission actions).

**Service** `src/utils/longAbsenteeismService.ts`: one `request()` wrapper over `fetchWithCookie`
returning `payload.data` and throwing `Error(res.error)`; type guards for every response; functions
named `fetchLaSetup`, `fetchLaCandidates`, `fetchLaList`, `fetchLaCase`, `openLaCase`,
`dismissLaCandidate`, `verifyLaCase`, `updateLaCase`, `parkLaCase`, `resumeLaCase`, `exitLaCase`,
`reopenLaCase`, `fetchLaEvents`, `createLaEvent`, `updateLaEvent`, `deactivateLaEvent`,
`fetchLaPerformance`, `fetchLaRule`, `saveLaRule`, `fetchLaTemplates`, `saveLaTemplate`,
`resetLaTemplate`, `fetchLaLetters`, `fetchLaLettersDue`, `previewLaLetter`, `issueLaLetter`,
`issueLaLetterBatch`, `updateLaLetter`, `fetchLaDispatch`, `createLaReceipt`, `updateLaReceipt`,
`deactivateLaReceipt`, `fetchLaUnionNames`, `sendLaStruckOff`, `approveLaStruckOff`,
`rejectLaStruckOff`, `reopenLaStruckOff`, `fetchLaStruckOffPermissions`,
`fetchLaStruckOffRegister`, `fetchLaStageSummary`, `fetchLaMonthlyTrend`, and (amendment 11.20)
`fetchLaLettersPrint`, `downloadLaLettersPdf` (axios blob + `file-saver`, the `fetchExcelBlob`
pattern), `recordLaLetter`. Constants `LA_*` in `apiRoutesPortalMasters` (`/longAbsenteeism/<path>`).

**Watchlist tab** (FE-1): filter bar (branch picker only when > 1 branch; department
`SearchableOptionSelect`; view chips Due today / Open / Parked / Closed / Candidates; search).
Due today groups rows into bands with headers: New candidates (from `/candidates`), Verification
pending, Letters due (sub-grouped by `next_stage_label`), Marked present, Replies to review,
Awaiting approval. Desktop: MUI X `DataGrid` with the sheet's columns (EB No, Name, Dept,
Designation, Last working date, Absent days, 1st / 2nd / 3rd letter, Show cause, Notice, Struck
off, Report = exit label, Remarks **editable cell** → `updateLaCase`, extra columns with the rule's
labels when set, Open button). Row selection + "Issue letters for selected" → batch dialog (lists
`letters_due`, issues, shows issued / skipped, then opens the print window for the batch).
Candidates view: Open case / Dismiss (reason + snooze) per row. Export button → FE-2's
`_export.ts` (stub a no-op button until FE-2 lands).

**Mobile** (`useMediaQuery(theme.breakpoints.down("md"))`): watchlist and reports render stacked
**cards** (name + EB, stage chip, absent days, next due, remarks line, Open) instead of the grid,
with a "Show as table" toggle that renders the grid in a horizontally scrollable box; case page
stacks its panels in one column; dialogs use `MOBILE_FULL_BLEED_PAPER` / `DIALOG_FORM_SCROLL_SX`
with 44 px touch targets (copy `FollowupTimeline.tsx`); letter print shows a "Print from a desktop
browser" note on phones but still allows it.

**Case page** (FE-1): header card (snapshot fields, live absent days, stage chip, next due, status
chip, rejoined banner, approval status chip when `status_id`), action row (Verify, Issue next letter
[label = next stage], Add remark / feedback / reply, Park / Resume, Exit, Reopen, Struck off
approval bar), panels: Verification, Letters (one card per letter: dates, sent date + AD + delivery
inline-editable via dialog, Print again, attachments uploader for `signed_copy` / `ad_card`, union
receipts list + add on GATE_NOTICE), Timeline (newest first, user entries editable / removable per
level, attachment uploader on REPLY events for `reply` / `medical` / `other`), Performance (button
"Load last 30 days" → `fetchLaPerformance`), Earlier cases. Struck-off approval uses
`ApprovalActionsBar` from `@/components/ui/transaction` fed by `fetchLaStruckOffPermissions`
(statusId = `status_id ?? 1`, show Send for approval when `can_send`).

**Letter preview dialog** (FE-1): `previewLaLetter` → letterhead (`CompanyHeaderBlock` from
`src/app/dashboardportal/procurement/_shared/PrintHeader.tsx` with `useCompanyHeader`), subject and
body **read-only** (pre-wrap text, amendment 11.15), `blocked_reason` alert with a Force checkbox + reason
when `can_force`, buttons Issue & print (→ `issueLaLetter` then `openStyledPrintWindow` from
`src/utils/printUtils.ts` with the stored text) and Cancel. "Print again" on an issued letter
opens the same print view from the stored body. Batch print = one window, `page-break-after: always`
per letter.

**FE-2 tabs**: Dispatch (date range + batch filter, grid / cards of letters with inline edit of
sent date, AD no, delivery status via a small dialog, Print register; amendment 11.20: row
selection — grid `checkboxSelection`, a checkbox per card — with "Reprint selected", "Download PDF"
and a "Reprint batch" per batch, and a "Recorded" chip); Reports (phone-first: stage
summary as one small card per department with a horizontal bar per stage using `@mui/x-charts`
`BarChart` or plain MUI `LinearProgress` rows; struck-off register table / cards with Export Excel;
monthly trend `LineChart`); Templates (one accordion per template: subject, body multiline, the
placeholder chips that insert `{code}` at the cursor, Save, Reset to default, "Preview on a case"
by case number via `previewLaLetter`); Settings (the rule fields with Zod, Save). `_export.ts`
exports the watchlist with `exportRowsToExcel` (`src/components/reports/exportExcel.ts`) in the
column order: SL NO, EB No, Name, DEPT, Occupation, Last Working/Leave Date, Absent Days,
1ST LETTER, 1ST SENT, 2ND LETTER, 2ND SENT, 3rd & Last Warning Letter, 3RD SENT, SHOW CAUSE,
NOTICE FOR STRUCK OFF, STRUCK OFF, REMARKS, REPORT, then the extra columns.

**Amendment 11.20 on the case page / dialogs:** Open case dialog has "Detected on" (default today);
the case page has "Record a letter already issued" (edit access, live open case with a next letter;
`RecordLetterDialog.tsx`); every letter card has "Download PDF" and a "Recorded" chip when
`recorded = 1`; the letter preview's issued view has "Download PDF" next to "Print again". Letter
printing goes through the shared `openStyledPrintWindow` (print frame: 15 mm on every page, no
browser header / footer); `_print.ts` adds only `LETTER_PRINT_CSS` (one letter per page via the
`.la-page-break` wrapper: `break-after: page` + `page-break-after: always`).

Dates: display `DD-MM-YYYY` via `fmtDate` from `src/components/lowPerformerFollowup/schemas.ts`;
inputs `TextField type="date"` (ISO). Zod for every dialog. Tests: `_schemas.test.ts`,
`_utils.test.ts`, `longAbsenteeismService.test.ts` (mock `@/utils/apiClient2`).

## 9. Tests

BE: `src/test/test_long_absenteeism.py` / `..._letters.py` with the follow-up's pattern
(`dependency_overrides` for `get_tenant_db` + `get_current_user_with_refresh`, `MagicMock` rows
with `._mapping`, patch `access_level`). Cover: pure helpers (merge, previous notices, due dates,
wait tables), each endpoint's validation + happy path + access 403 + 404 other company, duplicate
409 (SELECT and IntegrityError), letter gates (wrong stage, missing sent date, not due, force),
struck-off approve finalisation (engine patched to return 3 → employee helper called with 41),
reject, reports' bind values. Keep `uv run pytest src/test/ -q` green (there are known pre-existing
failures unrelated to this feature — compare against a clean run).

FE: `pnpm exec vitest run --project unit`, `pnpm exec eslint <changed files>`, and `pnpm build`
must pass at the end (FE-2 runs the build).

## 10. Deployment

1. `tools/run_sql_migration.py dev3 dbqueries/migrations/20261011_jute_prod_absent_tables.sql`,
   then `..._menu.sql`; same on `sls` (owner decision 4). Lead runs these, not the agents.
2. Users log out / in once (menu cache). sls: role 13 / 14 have edit; the approver for struck off
   is set in Tenant Admin → Approval Hierarchy for menu "Long Absenteeism" per branch.
3. Owner reviews at `http://dev3.187-77-145-36.nip.io:3000/dashboardportal/hrms/longAbsenteeism`
   (amendment 11.20; the old address redirects there). Also run `20261012_long_absenteeism_menu_to_hrms.sql`
   and `20261012b_jute_prod_absent_letter_recorded.sql` on dev3 + sls; users log out / in once.
4. Commit on `claudehost` only after the owner's OK.

## 11. Amendments after the backend review (2026-10-11, lead decisions)

1. **Rejoined workers cannot be struck off or sent a letter.** `/struck_off/send`, `/struck_off/approve`,
   `/letter_issue` and `/letter_preview` re-run the rejoin query; a first attendance after the last
   working date blocks them (`blocked_code = REJOINED`, not forceable).
2. **HR separation wins.** Approve refuses (400) when `hrms_ed_personal_details.status_id` is already a
   separation status (`RESIGN_DETAIL_STATUSES`); the user exits the case instead.
3. **Counting rule.** A wait of N days runs from the previous letter's `sent_date`; the next step opens
   on day N + 1 (`due_date_for_next = base + wait + 1`; WARNING_1 = the verified date). Reply windows:
   `{reply_due_date}` printed on the letter is the LAST day to reply; struck off can be sent from the
   day after the gate notice window (`gate_notice_due = sent_date + notice_reply_days`, blocked while
   `today <= due`; `struck_off_date > due`).
4. **Memo wording.** New placeholder `{letters_issued}` (neutral list of the letters issued with dates and
   sent dates); the struck-off memo uses it instead of `{previous_notices}`.
5. **Dismiss → reopen** re-runs the full snapshot and assigns a case number.
6. `/list?view=closed` skips the rejoin refresh. sls already has `idx_da_dedupe (eb_id, attendance_date, …)`.
7. `SELECT … FOR UPDATE` on the case is the first statement of send / approve / reject / reopen (struck
   off) and of exit / reopen / park / resume.
8. `/open` and `/dismiss` pick the worker's LATEST active official row overall and require it to be in
   the branch (same rule as `/candidates`).
9. Batch issue skips STRUCK_OFF cases (the memo is issued from the case page only).
10. `merge_text` removes only the line of an emptied placeholder; template blank lines are preserved.
11. New watchlist band `STRUCK_OFF_DUE` ("Struck off due — send for approval") for a gate-notice case
    whose window has ended; `LETTER_DUE` is never produced for GATE_NOTICE. Band order: candidates,
    verification pending, letters due, struck off due, marked present, replies to review, awaiting approval.
12. One `case_out` shape (case + `next_stage_code`, `next_stage_label`, `due_band`) from both routers.
13. WARNING_1 `issue_date` ≥ verified date unless forced; event attachments only on REMARK / FEEDBACK /
    REPLY; case numbers derive from `case_id` (`LA/<branch_id>/<yy>/<case_id:04>`), collision-free.
14. **Three warning templates** (owner 2026-10-11, later; improvement item 9 "go ahead as
    recommended"). The single `WARNING` template is replaced by `WARNING_1` ("1st warning letter"),
    `WARNING_2` ("2nd warning letter") and `WARNING_3` ("3rd & last warning letter");
    `TEMPLATE_OF_STAGE` maps each warning stage to its own template. Same subject and header block
    (Date / Ref / To / NAME / EB NO / DEPT / DESIGNATION / `{previous_notices}`) and signatory block.
    WARNING_1 is mild (records the absence, cites clause 14(C)(V), report within `{reply_days}`
    days or inform the Personnel Office with valid proof, "further action as per the company
    norms"); WARNING_2 is firmer ("Despite our earlier letter …", a second and serious warning,
    then a final warning and the name liable to be struck off); WARNING_3 is the owner's Word text
    (gross negligence, last and final opportunity). `template_code` stays VARCHAR(20): no
    migration. `/templates` returns six; `/template_save` / `/template_reset` accept the new codes
    and refuse the retired `WARNING` with 400 (a saved `WARNING` row, if any, is no longer read;
    letters issued under it keep their stored text and the label "Warning letter (1st / 2nd / 3rd)").
    The Templates tab shows six accordions; "Preview on a case" previews template WARNING_n as
    stage WARNING_n (the "As the" warning picker is gone).
15. **Letters are rendered from the template at issue; the user cannot edit the text** (owner item
    13: "letters won't be edited except the fields like dates, names etc"). `/letter_issue` and
    `/letter_issue_batch` always merge the company's template for the stage on the server
    (`build_merge_fields` + `merge_text`, one helper `merge_case_letter` shared with the preview) and
    store that text; `subject` / `body` in the request are still accepted for backward
    compatibility but ignored. `/letter_preview` keeps returning the rendered text plus
    `editable: false` (its `template_subject` / `template_body` overrides remain for the Templates
    tab's preview of unsaved text — nothing is issued from it). The letter preview dialog shows
    the subject and body read-only; only timing is the user's call (Force + reason). The wording is
    changed on the Templates tab (edit access), never per letter.
16. **Habitual history on the letters** (owner item 8 "go ahead"). New placeholders
    `{habitual_note}` — "Your attendance record also shows that you have been habitually absent
    without leave." when the case's `habitual` = 1, else empty — and `{earlier_cases}` — "Earlier
    long-absence cases on your record: LA/2/25/0012 (detected 05.08.2025, rejoined); LA/2/26/0003
    (detected 02.02.2026, on ESI)." listing the worker's earlier CLOSED cases in the branch, oldest
    first, excluding DISMISSED and any case detected after the letter's own case, else empty. The
    letters router reads them with BE-1's `get_other_cases_query` (the `/get` earlier_cases query;
    no duplicated SQL); the wording is the pure `earlier_cases_sentence(rows)` /
    `habitual_note_sentence(habitual)` in `long_absenteeism_common.py`. Both are used in the
    WARNING_3 and SHOW_CAUSE defaults after the `{previous_notices}` paragraph; an empty one drops
    its paragraph (amendment 10).
17. **Excluded EB prefixes are a setting** (owner 2026-10-11: "exclude C and R too", then L as
    well after the sls candidate list showed L codes). New column
    `jute_prod_absent_rule.exclude_prefixes VARCHAR(50) NULL` (migration
    `20261011b_jute_prod_absent_rule_prefixes.sql`, dev3 + sls); NULL = the code default
    `RULE_DEFAULTS["exclude_prefixes"] = "T,C,R,L"`. Workers whose EB No (`emp_code`) starts with
    one of the letters are not candidates (4.1): `/candidates` binds
    `:exclude_regex = exclude_prefix_regex(rule["exclude_prefixes"])` (e.g. `^[TCRL]`, NULL when the
    list has no letter) into `AND (:exclude_regex IS NULL OR od.emp_code IS NULL OR od.emp_code NOT
    REGEXP :exclude_regex)`. The regex is built only from validated A–Z letters and is always a
    bind, never interpolated. `/rule_save` takes `exclude_prefixes` as text: comma / space separated
    single ASCII letters, case-insensitive; stored canonical (upper case, comma separated, no spaces,
    repeats dropped in first-seen order: `" t c, r ,T"` → `T,C,R`); blank / null → NULL (the
    default); anything else (`TX`, `1`, `T;C`, non-text, > 50 characters) → 400 naming
    `exclude_prefixes`. `/rule` and `/setup.rule` return the canonical value (a NULL / blank row
    value → `T,C,R,L`). Consequence: "no exclusion" cannot be configured (blank means the default).
    **`exclude_t_codes` is retired**: the column stays in the table, is no longer read or written,
    and the response keeps `exclude_t_codes: 1` (the default) only for older clients; a 0 stored
    there no longer re-includes T codes. Settings tab: the "Leave out T-codes" switch is replaced by
    the text field "Excluded EB prefixes" (Zod: optional, letters / commas / spaces only, ≤ 50).
18. **The look-back window is optional and off by default** (owner 2026-10-11).
    `RULE_DEFAULTS["lookback_days"] = 0` = no limit: every worker on the roll absent ≥
    `detect_days` is a candidate, however long ago they were last seen. In `get_candidates_query`
    both legs of the seen-union keep the `<= :as_on` upper bound and take an optional lower bound
    (`AND (:from_date IS NULL OR da.attendance_date >= :from_date)` /
    `AND (:from_date IS NULL OR ltd.leave_date >= :from_date)`); `/candidates` binds
    `from_date = NULL` when the rule's `lookback_days` is 0, else `as_on - lookback_days`.
    `/rule_save`: `lookback_days` is 0 (no limit) or `detect_days`..3650 (`MAX_LOOKBACK_DAYS`); the
    other day fields stay 0..365. Workers with no attendance or approved leave at all in the branch
    (no last working date) stay out of the list, as before. Verified read-only on sls (no window,
    T/C/R/L excluded): 134 / 241 / 8 rows for branches 29 / 87 / 4 in ≤ 2.4 s, so no index work.
    A company that already has a rule row keeps its stored `lookback_days` (the new default only
    applies with no row, or after Settings is saved with 0). Settings tab helper for "Look back":
    "0 = no limit (every worker on the roll absent at least the detect days is listed). Set a
    number of days to list only workers seen recently."
19. **Leave types and approved leave come from the leave module; the manual verification step is
    dropped** (TKT-000223, 2026-10-12, sls company 2 / branch 29; reporter: "make leave dynamic — the
    dropdown should work from the leave table; persons already on leave should get the data from there
    automatically"; owner ruling 2026-10-12: "there is no additional verification required here since
    the leave marking is already done on the Leave Request page (hrms/LeaveRequest)"). Lead decision:
    the company's own leave types replace the fixed ESI / STL / MATERNITY answers, a worker's approved
    leave in the leave module is read onto the case and parks it by itself, and a new case no longer
    waits for a manual verification. Existing rows with ESI / STL / MATERNITY keep working unchanged.
    - **Source of the choices.** The company's HRMS leave-type master `hrms_leave_types_mst`
      (`company_id = co_id`, `is_active = 1`; CRUD on the Leave Type Master page
      `hrmsmasters/LeaveMaster`). `/setup` now returns `leave_types` (the rows), `leave_options` and
      `exit_options`, built by the pure `build_leave_options` / `exit_options` in
      `long_absenteeism_common.py`; there is NO `verify_options`. Each option is
      `{code, leave_type_id, label, legacy_code, source}`: a master type has `code = "LEAVE"` plus its
      `leave_type_id` (label "STL LEAVE (L)"). The legacy built-ins ESI / STL / MATERNITY are mapped to
      a master type by name where one exists (regex `\bESI\b`, `\bSTL\b`, `\bMATERN` on the type's
      code or description, or code equality; on sls company 2 "STL LEAVE (L)", id 24, carries
      `legacy_code = STL`) and are offered as stand-alone legacy options only when no type matches
      (sls has no ESI or maternity type, so those two stay available). `exit_options` = REJOINED, the
      leave options ("On leave: …" for master types), MEDICAL, RESIGNED, EXPIRED, OTHER.
    - **Codes.** `EXIT_REASONS` gains `LEAVE` "On leave" (user-selectable).
      `RETURN_DATE_EXIT_REASONS` = ESI, STL, MATERNITY, MEDICAL, LEAVE hide the worker from
      `/candidates` while `expected_return_date >= as_on` (the NOT EXISTS list in 4.1 gains
      `'LEAVE'`). Legacy ESI / STL / MATERNITY stay readable on old rows.
    - **Columns.** `jute_prod_absent_case` gains SIX columns: `leave_type_id BIGINT NULL`,
      `leave_type_name VARCHAR(100) NULL` (snapshot of the description, the code when blank),
      `leave_from DATE NULL`, `leave_to DATE NULL`, `leave_transaction_id BIGINT NULL` (non-null =
      linked to an approved leave in the leave module; NULL = not linked: a manual exit pick, or none)
      and `leave_parked TINYINT NOT NULL DEFAULT 0` (1 = parked by the leave refresh, not by a person).
      Migration `20261012_jute_prod_absent_leave_link.sql`, dev3 + sls, idempotent.
    - **No manual verification.** `/open` creates the case at stage VERIFIED (`stage_date = today`,
      `next_due_date = today`, `verified_date = today`, `verify_note = "Leave module checked
      automatically"`, `esi_stl_result` NULL), so the 1st warning is due at once; a reopened dismissal
      (amendment 5) is promoted the same way. Live cases still at DETECTED are promoted on the next
      `/list` / `/get` refresh with a SYSTEM event "Verification step removed — 1st warning due now
      (leave module checked automatically)". `/verify` (4.3) stays only for API compatibility (NONE /
      ESI / STL, unchanged) with no UI path, and it does not take `leave_type_id` or
      `expected_return_date`. `habitual` stays editable in the case details dialog (`/update`).
    - **Only approved leave counts.** `leave_transactions.status = 3` is the only status used anywhere
      in this feature; pending (20), rejected (4) and cancelled (6) leaves never park a case or hide a
      worker.
    - **Automatic leave and parking.** On `/list` (views other than closed), `/get` and after `/open`,
      `refresh_leaves_branch` / `refresh_leave_case` run together with the rejoin refresh of 4.6 and
      look up, per live case, the worker's approved leave with `leave_to_date > last_working_date`,
      latest `leave_to_date` first. The header date range is used, not `leave_tran_details`, because 15
      of 5295 approved sls leaves have no detail rows. Then: when that leave covers today, the worker
      has not rejoined and no struck-off approval is pending, the case stores `leave_transaction_id`,
      `leave_type_id`, `leave_type_name`, `leave_from`, `leave_to` (silent when unchanged) and — once
      per leave transaction — an open case is PARKED with `leave_parked = 1` and a SYSTEM event "On
      <type> leave dd.mm.yyyy to dd.mm.yyyy (leave module)". When that leave has ended
      (`leave_to < today`) and the case is still leave-parked it RESUMES: `status` open,
      `leave_parked = 0`, `next_due_date = max(next_due_date, leave_to + 1)`, SYSTEM event "Leave ended
      dd.mm.yyyy, still absent". A future leave is stored only. A withdrawn leave clears the leave
      columns and resumes a leave-parked case ("Leave record withdrawn in the leave module; case
      resumed"). Attendance after the last working date still wins as Marked present (4.6). A manual
      `/resume` clears `leave_parked`. Letters and struck off stay blocked while the case is parked
      (existing rule).
    - **Pending hint.** `pending_leave_since` = the earliest `updated_date_time` of a status-20
      request covering today, on list / get / candidates rows; the frontend shows "Leave request
      pending approval since dd.mm.yyyy". Informational only, no state change.
    - **Exit.** Unchanged in shape: a manual `exit_reason = LEAVE` for long leaves the office prefers
      to close takes `leave_type_id` (required, a type from the master) and `expected_return_date`.
      Event texts use "On leave: <type name>" (+ " till dd.mm.yyyy").
    - **Case dict and watchlist.** `case_to_dict` adds `esi_stl_result_label`, `on_leave_now` (module
      leave covering today), `on_leave_label` and `pending_leave_since`; `exit_reason_label` for LEAVE
      is "On leave: <type>". New watchlist band `ON_LEAVE` ("On leave (leave module)"; module leave
      covering today), tested right after MARKED_PRESENT and before REPLY_REVIEW. The band order of
      11.11 applies with `ON_LEAVE` inserted after marked present and without verification pending,
      which no longer appears (no case rests at DETECTED).
    - **Candidates (4.1).** Behaviour unchanged. The seen-union gains a third, header-range leg: an
      approved leave counts as "seen" through `LEAST(leave_to_date, as_on)` once
      `leave_from_date <= as_on`, so a worker on recorded leave is never an unexplained absentee even
      without detail rows. The same leg is in `get_last_seen_query` (the `/open` last-working-date
      fallback). Each candidate row carries `leave_transaction_id`, `leave_type_id`,
      `leave_type_name`, `leave_from`, `leave_to` of the approved leave that ended on the last seen
      day (the worker did not return after it), null otherwise, plus `pending_leave_since`.
    - **Frontend.** The Verify dialog, the Verification panel, the VERIFY band and the verification
      wording are removed. The case page gets a Leave panel (on leave now / ended / upcoming / none,
      plus the pending hint "Leave request pending approval since dd.mm.yyyy") and header chips "On
      leave: … till …" / "Leave … ended … — not returned". The exit dialog lists `exit_options`, and
      the case page offers one-click "Exit as on leave" (ConfirmDialog → `/exit` with LEAVE, the type
      and `leave_to` as the expected return date) when a module leave is linked. Watchlist column
      "Leave", card chip, the `ON_LEAVE` band, candidate line "Leave <type> ended dd-mm-yyyy (leave
      module)".
    - **Letters unaffected.** No new placeholder; `{earlier_cases}` shows "on leave" for LEAVE exits
      via the label. A case parked for leave is blocked from letters and struck off by the existing
      parked rule, not by a new check.
20. **Four owner decisions (2026-10-12): HRMS module, bulk reprint, back-dated entries, PDF.**
    - **A. HRMS module.** Menu path `hrms/longAbsenteeism` (migration
      `20261012_long_absenteeism_menu_to_hrms.sql`: new row under HRMS next to Leave Request, the old
      row's grants and struck-off approvers moved, the old row deactivated). Backend:
      `long_absenteeism_common.MENU_PATH = "hrms/longAbsenteeism"`, `MENU_PATHS = (MENU_PATH,
      "juteProduction/report/longAbsenteeism")`; `menu_id()` reads the ACTIVE rows of both paths and
      takes the first path of `MENU_PATHS` that has one (`pick_menu_id`), so access checks and the
      struck-off engine keep working on a tenant before and after the menu migration. Error texts
      unchanged ("… access to Long Absenteeism …"). Backend code stays in `src/juteProduction/`, prefix
      `/api/longAbsenteeism`. Frontend: folder moved to `src/app/dashboardportal/hrms/longAbsenteeism`
      (`git mv`), tests' pathname mocks updated; the old route keeps a `page.tsx` that
      `router.replace`s to the HRMS path with the same query string (the proxy's permission check walks
      up the path, so it is reachable for users with the Jute Production Reports menu). No "Back to
      HRMS" link (HRMS pages have none). Until the menu migration runs, the HRMS path has only the HRMS
      parent's (view) level in the sidebar, so the edit tabs show after the migration + a fresh login.
    - **B. Bulk reprint.** `GET /letters_print?co_id&letter_ids=1,2,3` (view): ≤ 200 ids
      (`MAX_PRINT_LETTERS`; order kept, repeats dropped; 400 blank / non-positive / too many); the
      letters must be active and belong to active cases of `co_id` (`get_letters_by_ids_query`, `l.*`),
      any id not found → 404 "Letter not found: 8, 12"; then view access on EVERY branch of the set.
      Returns, in the requested order, `{letter_id, case_id, branch_id, case_no, emp_code, emp_name,
      stage_code, stage_label, subject, body, issue_date, sent_date, recorded}`. Frontend Dispatch tab:
      selection (grid checkboxes / card checkboxes), "Reprint selected", "Reprint batch" per batch →
      `fetchLaLettersPrint` → `printLetters(head, letters, title)` (letterhead from `useCompanyHeader`),
      one letter per page, in the register order. The single-letter "Print again" stays.
    - **C. Back-dated entries.** Migration `20261012b_jute_prod_absent_letter_recorded.sql`
      (idempotent via information_schema + PREPARE / EXECUTE; rollback in the file):
      `jute_prod_absent_letter.recorded TINYINT NOT NULL DEFAULT 0`. ORM column **deferred, no Python
      default**: `db.get` / the issue INSERTs never name it and `model_to_dict` reports a deferred
      column only when loaded, while the letter reads that should carry it (`/get`, `/letters`,
      `/letters_print`, `/letters_pdf`) use `l.*` — so everything except `/letter_record` also runs
      before the migration (the key is then absent and the UI treats it as 0).
      `POST /letter_record {co_id, case_id, stage_code, issue_date, sent_date?, dispatch_mode?, ad_no?,
      delivery_status?, delivery_date?, note?}` (edit): lock the case first; open only (not parked /
      closed); `stage_code` must be `next_stage_of(case)` (letters recorded in order; STRUCK_OFF / the
      memo → 400; a gate-notice case → 400 "the case goes for struck-off approval next"); duplicate
      active letter of the stage → 409; NO wait check and NO previous-sent-date check (historical facts);
      `issue_date` required, ≤ today, ≥ `last_working_date` and ≥ the previous letter's issue date
      (`check_issue_date_order`); `sent_date` between issue_date and today; `delivery_date` ≥ sent_date
      (else issue_date) and ≤ today. Subject / body merged now from the company's template via
      `merge_case_letter` (dated `issue_date`), `recorded = 1`, `forced = 0`, sent / A.D. / delivery
      stored on the row; the case advances exactly as `/letter_issue` (`_advance_case`: stage,
      stage_date = issue_date, next_due_date from the sent date when given); event STAGE "<stage
      label> recorded as issued dd.mm.yyyy, sent dd.mm.yyyy (back-dated entry): <note>" dated
      issue_date. Returns `{letter, case}` (letter with `recorded: 1`). `/open` takes optional
      `detected_date` (≤ today, ≥ the last working date — the typed one or the last day seen; default
      today): detected / stage / verified date, `next_due_date`, `absent_days_at_open` and the case
      number year use it; the opening event says "… when detected on dd.mm.yyyy (back-dated entry,
      entered dd.mm.yyyy)". `/dismiss` unchanged. Frontend: Open case dialog "Detected on" (Zod: not in
      the future, not before the typed last working date; today is sent as null); case page "Record a
      letter already issued" (edit; the next stage read-only, letter date, sent date, dispatch mode, A.D.
      no, delivery status / date — not for the gate notice — and note; Zod mirrors the server); letter
      cards and dispatch rows show a "Recorded" chip. `/letters_print`, `/letters_pdf` and the dispatch
      register include recorded letters like any other.
    - **D. Download PDF.** `GET /letters_pdf?co_id&letter_ids=…` (view, same checks as B) →
      `StreamingResponse(media_type="application/pdf", Content-Disposition: attachment;
      filename="long-absenteeism-letters-<yyyymmdd>.pdf")`. Pure `long_absenteeism_pdf.build_letters_pdf(
      letters, company) -> bytes`: A4, margins 20 / 15 mm, one letter per page (`PageBreak`; a long body
      flows on), letterhead = logo (`co_mst.co_logo` when it is a decodable base64 image data URL, else
      skipped) + company name (Times-Bold 15) + address ("addr1, addr2, state - PIN" as
      `useCompanyHeader`) + "GSTIN: … | CIN: …" (branch `gst_no`, `co_cin_no`; the GSTIN is left out
      when the letters span several branches), a rule below; subject centred bold underlined; body one
      Paragraph per line (Times-Roman 11 / 14, word-wrapped, indentation and blank lines kept) — the
      signature block is part of the body. Letterhead row: `get_pdf_letterhead_query` (branch → co_mst
      → state_mst). Frontend `downloadLaLettersPdf(coId, letterIds)` (file name from
      Content-Disposition; a JSON error inside the Blob becomes the message): "Download PDF" on each
      letter card, in the letter preview's issued view and next to "Reprint selected" on Dispatch.

## 12. Performance notes (2026-10-12, owner: "APIs are very slow, especially after opening a case")

Measured on sls (branch 29) and on the VM: one round trip to the remote MySQL = **63 ms**.

| Finding | Before | After | How |
| --- | --- | --- | --- |
| `get_case_leave_query` (per case, on `/get` and `/list`) | 25 s | 0.07 s | the `LEFT JOIN leave_transactions lv ON lv.id = (correlated subquery)` was planned as a scan of the whole leave table re-running the subquery per row; rewritten as `LEFT JOIN LATERAL (…) lv ON TRUE` |
| candidates query, per-candidate ended-leave lookup | 4.0 s | 3.3 s | same lateral rewrite |
| candidates query, last-seen scan of the branch's attendance (898k rows) | ~2.3 s of 3.3 s | 0.75 s whole query (br 29), 0.45 s (br 87) | two changes: (a) each leg of the `seen` CTE is grouped BEFORE the union, so the union holds one row per worker instead of every attendance row; (b) covering index `idx_da_branch_eb_cov (branch_id, eb_id, attendance_date, is_active, status_id)` — a plain `(branch_id, eb_id, attendance_date)` index made it 4x SLOWER (875k row lookups), the covering one answers the leg from the index. Plus `leave_transactions (eb_id, status, leave_to_date)`, `leave_tran_details (ltran_id, leave_date)`. `20261012c_long_absenteeism_perf_indexes.sql`, applied on dev3 AND sls (owner OK 2026-10-12) |
| menu id, branch-in-company check, company rule | 3 round trips per request | 0 when warm | process-local `TtlCache` (600 s / 600 s / 60 s; the rule cache is invalidated by `/rule_save`) in `long_absenteeism_common.py` |
| candidates list | one 3 s query per watchlist load | cached 120 s per (db, co, branch, as_on, dept, rule) | `_CANDIDATES_CACHE` in `long_absenteeism.py`; `invalidate_candidates()` on `/open`, `/dismiss`, `/exit`, `/reopen` (now and after commit) |
| visibility | none | `TIMING … total=…ms db=…ms queries=N` log line + `X-Server-Time-Ms` / `X-DB-Time-Ms` / `X-DB-Queries` headers | `src/common/request_timing.py`, installed in `main.py` for `/api/longAbsenteeism` |

Fixed cost per request that remains (profiled with cProfile, all of it socket reads from MySQL):
`pool_pre_ping` SELECT (1 RTT) + `Session.close()` rollback (1 RTT) + pool `reset_on_return` rollback
(1 RTT) ≈ 190 ms before the first real query. These are engine-level settings in `src/config/db.py`
shared by every route — a team decision (e.g. `pool_recycle` instead of pre-ping, a single rollback
on release), or a MySQL instance in the same region as the API server.
