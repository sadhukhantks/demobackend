# Jute Mechanical & Maintenance — module spec

Replaces the mill's "MILL SIDE MECHANICAL BREAK DOWN SUMMARY" / "spreader machine
breakdown report" Excel workbooks with a structured incident log, preventive-maintenance
and overhaul register, and the analytics the workbooks compute by hand.

## Decisions (locked)

| Decision | Value |
|---|---|
| Scope | Full mill line + utilities/services + electrical as a separate discipline |
| Entry model | Incident-level, `stop_datetime` + `restart_datetime`; duration derived |
| Depth | Breakdown log + analytics, overhaul & PM, spares & cost, machine history & alerts |
| Approval workflow | **None** — direct save. Downtime logging must be fast. `status` tracks the repair (OPEN/UNDER_REPAIR/CLOSED), not a document lifecycle |
| Relationship to Stoppage Hours | **Independent.** `jute_prod_stoppage_hours` stays the production hours-accounting feed; this is the engineering log. Neither writes to the other |
| Sidebar | New top-level module, `menu_parent_id IS NULL` |
| Target tenants | dev3 + sls |

## Masters REUSED — create none of these

`machine_mst` (machine_id, machine_name, mech_code, machine_type_id, dept_id) ·
`machine_type_mst` (== the Excel "section") · `dept_mst` (dept_id, dept_desc, branch_id) ·
`sub_dept_mst` (attending crew) · `spell_mst` (**status = 1**, not active) · `shift_mst` ·
`item_mst` (spares) · `party_mst` (overhaul vendor).

`machine_mst` has **no co_id and no branch_id**. Derive branch everywhere via:

```sql
FROM machine_mst m
INNER JOIN dept_mst d ON d.dept_id = m.dept_id      -- d.branch_id
```

Excel section → `machine_type_mst.machine_type_name`:
SPREADER→`Spreader` · BREAKER→`Breaker Card`,`Teaser` · DRAW HEAD & F/CARD→`Carding Machine`,`finishercard` ·
DRAWING→`Drawing`,`R/G` · SPG→`Spinning`.

## New tables (10) — see `dbqueries/migrations/create_jute_mech_maintenance_tables.sql`

`jute_mech_component_mst` · `jute_mech_reason_mst` · `jute_mech_machine_attr` ·
`jute_mech_pm_schedule` · `jute_mech_breakdown` · `jute_mech_breakdown_spare` ·
`jute_mech_overhaul` · `jute_mech_overhaul_spare` · `jute_mech_pm_execution` ·
`jute_mech_pm_execution_dtl`

ORM: `src/juteMechanical/models.py`. Enums: `src/juteMechanical/constants.py`.

## Derived fields (computed in Python on save, never by the DB)

```
duration_min  = body.duration_min AS TYPED (independent field, min 1, never cross-checked
                against the timestamps), else
                (restart_datetime  - stop_datetime).total_seconds() // 60   # NULL while open
response_min  = (attended_datetime - stop_datetime).total_seconds() // 60   # NULL if not recorded
tran_date     = stop_datetime.date()
branch_id     = machine -> dept_mst.branch_id
status        = CLOSED if (restart_datetime or duration_min) else (UNDER_REPAIR if attended_datetime else OPEN)
component_id  = body.component_id or reason.component_id       # default from the reason
discipline    = body.discipline  or reason.discipline
downtime_days = (to_date - from_date).days + 1                 # overhaul
next_pm_due       = pm_date + pm_freq_days                     # writes machine_attr
next_overhaul_due = to_date  + overhaul_freq_months            # writes machine_attr
```

Validation: `restart_datetime >= stop_datetime`; `attended_datetime` between the two;
`stop_datetime` not in the future; reason must belong to the machine's type or be global.

---

# API contract

All routers: bare `APIRouter()`, plain `def` handlers (never `async def`),
`db: Session = Depends(get_tenant_db)`, `token_data: dict = Depends(get_current_user_with_refresh)`,
`updated_by = token_data.get("user_id")`. Responses are always `{"data": ...}` (list endpoints
add `"total"`). Bodies are Pydantic models. SQL lives in `*_query.py` as `text()` builders.
`co_id` is required on every endpoint (400 if missing); `branch_id` optional (NULL = all).

## 1. `/api/mechMaintMasters` → `src/juteMechanical/masters.py` + `masters_query.py`

| Verb | Path | Notes |
|---|---|---|
| GET | `/masters_setup` | one call, all dropdowns: `{machines, machine_types, departments, sub_departments, spells, components, disciplines, downtime_types, criticalities, pm_frequencies}` |
| GET | `/reason_table` | `page, limit, search, machine_type_id, discipline`. Joins machine_type_mst + component for display names |
| GET | `/reason_by_id/{reason_id}` | |
| POST | `/reason_create` | dup check on (co_id, machine_type_id, reason_code) |
| PUT | `/reason_edit/{reason_id}` | dup check excluding self |
| DELETE | `/reason_delete/{reason_id}` | soft delete; 400 if referenced by a breakdown |
| GET | `/component_table` | `page, limit, search, component_group` |
| GET | `/component_by_id/{component_id}` | |
| POST | `/component_create` | dup check on (co_id, component_name) |
| PUT | `/component_edit/{component_id}` | |
| DELETE | `/component_delete/{component_id}` | soft delete; 400 if referenced by a reason or breakdown |
| GET | `/machine_attr_table` | `page, limit, search, branch_id, machine_type_id`. LEFT JOIN from machine_mst so machines **without** an attr row still appear (attr fields NULL) |
| GET | `/machine_attr_by_id/{machine_attr_id}` | |
| POST | `/machine_attr_create` | upsert on (co_id, machine_id); computes next_overhaul_due / next_pm_due |
| PUT | `/machine_attr_edit/{machine_attr_id}` | recomputes the two due dates |
| DELETE | `/machine_attr_delete/{machine_attr_id}` | soft delete |
| GET | `/pm_schedule_table` | `page, limit, search, machine_type_id, machine_id, frequency` |
| GET | `/pm_schedule_by_id/{pm_schedule_id}` | |
| POST | `/pm_schedule_create` | `freq_days` resolved from `PM_FREQUENCY_DAYS` if not given |
| PUT | `/pm_schedule_edit/{pm_schedule_id}` | |
| DELETE | `/pm_schedule_delete/{pm_schedule_id}` | soft delete |

## 2. `/api/mechBreakdown` → `src/juteMechanical/breakdown_entry.py` + `breakdown_query.py`

| Verb | Path | Notes |
|---|---|---|
| GET | `/entry_create_setup` | `{machines, spells, components, sub_departments, disciplines, downtime_types, statuses}` |
| GET | `/reasons_by_machine` | `co_id, machine_id` → reasons for that machine's type **plus** all global (machine_type_id IS NULL) codes, ordered code |
| GET | `/entries` | `co_id, branch_id, date_from, date_to, machine_id, machine_type_id, discipline, status, page, limit` → `{data, total}`. Joins machine/dept/reason/component for display names |
| GET | `/entry_by_id/{breakdown_id}` | header + `spares: [...]` |
| POST | `/entry_create` | body below |
| PUT | `/entry_edit/{breakdown_id}` | replaces spares (soft-delete old, insert new) |
| DELETE | `/entry_delete/{breakdown_id}` | soft delete, cascades to spares |
| POST | `/entry_close/{breakdown_id}` | quick close from the live board: `{restart_datetime, duration_min?, action_taken?}` → duration as typed or computed, sets CLOSED |
| GET | `/open_breakdowns` | `co_id, branch_id` → currently-open incidents (`restart_datetime IS NULL AND duration_min IS NULL`) + minutes elapsed so far |

`POST /entry_create` body (`BreakdownCreate`):
```
co_id: int                      machine_id: int                 stop_datetime: datetime
reason_id: int                  branch_id: int|None = None      spell_id: int|None = None
attended_datetime: datetime|None   restart_datetime: datetime|None
duration_min: int|None (ge=1)   # entered minutes; wins over the computed value
component_id: int|None          discipline: str|None            downtime_type: str = "BREAKDOWN"
attended_dept_id: int|None      attended_by: str|None           reported_by: str|None
root_cause: str|None            action_taken: str|None          preventive_action: str|None
production_loss_qty: float|None remarks: str|None
spares: list[{item_id, qty, uom_id, rate, amount, remarks}] = []
```

## 3. `/api/mechOverhaul` → `src/juteMechanical/overhaul.py` + `overhaul_query.py`

| Verb | Path | Notes |
|---|---|---|
| GET | `/overhaul_create_setup` | `{machines, parties, overhaul_types, done_by_options}` |
| GET | `/overhaul_table` | `co_id, branch_id, date_from, date_to, machine_id, overhaul_type, page, limit` |
| GET | `/overhaul_by_id/{overhaul_id}` | header + `spares: [...]` |
| POST | `/overhaul_create` | computes downtime_days, spare_cost, total_cost; **updates `jute_mech_machine_attr.last_overhaul_date` + `next_overhaul_due`** (upserts the attr row if absent) |
| PUT | `/overhaul_edit/{overhaul_id}` | same recomputation |
| DELETE | `/overhaul_delete/{overhaul_id}` | soft delete, cascades to spares |

## 4. `/api/mechPM` → `src/juteMechanical/pm.py` + `pm_query.py`

| Verb | Path | Notes |
|---|---|---|
| GET | `/pm_create_setup` | `{machines, pm_frequencies, pm_results}` |
| GET | `/pm_checklist` | `co_id, machine_id, frequency?` → schedule tasks matching that machine **or** its machine type, ordered seq_no. Seeds the detail grid |
| GET | `/pm_execution_table` | `co_id, branch_id, date_from, date_to, machine_id, page, limit` |
| GET | `/pm_execution_by_id/{pm_exec_id}` | header + `tasks: [...]` |
| POST | `/pm_execution_create` | snapshots `task_desc` into each detail row; **updates `machine_attr.last_pm_date` + `next_pm_due`** |
| PUT | `/pm_execution_edit/{pm_exec_id}` | replaces detail rows |
| DELETE | `/pm_execution_delete/{pm_exec_id}` | soft delete, cascades to detail |

## 5. `/api/mechMaintReports` → `src/juteMechanical/reports.py` + `reportQueries.py`

Kebab-case paths (house reports convention). All accept `co_id` (req), `branch_id`,
`date_from`, `date_to`, `machine_type_id`, `machine_id`, `discipline`, `page`, `limit`
(limit capped 10000, SQL-side `LIMIT/OFFSET`), and return `{data, total}`.

| Path | Replaces / delivers |
|---|---|
| `/breakdown-register` | raw filterable incident log |
| `/machine-summary` | per machine: breakdown count, total downtime min, MTTR — **the workbook's main sheet** |
| `/reason-pareto` | per reason: count, total duration, duration-per-breakdown, % share, cumulative % — **the workbook's reason block** |
| `/date-machine-grid` | date × machine matrix of reason codes — **exact replica of the sheet they key today**. Returns `{data: [{tran_date, "<machine_id>": "RF/DRJ", ...}], columns: [{machine_id, machine_name}]}` |
| `/department-summary` | per dept + machine type: count, duration, % of mill downtime |
| `/reliability` | per machine: MTBF, MTTR, availability %, downtime per 1000 running hrs |
| `/reason-trend` | month × reason count + duration — **the workbook's Summary sheets** |
| `/maintenance-due` | PM due/overdue + overhaul due, ordered by criticality then days-overdue |
| `/spares-consumption` | per machine/item: qty, value; maintenance cost per machine |
| `/top-problem-machines` | top-N by downtime and by count, plus repeat-failure list (same machine+component within `REPEAT_FAILURE_WINDOW_DAYS`) |
| `/machine-history` | `machine_id` req — every breakdown, PM, overhaul and spare for one machine |
| `/open-breakdown-board` | currently-open incidents, elapsed minutes, ordered longest-first |

### Metric formulas (apply consistently across reports)

```
Only downtime_type = 'BREAKDOWN' AND active = 1 counts as a failure.

breakdown_count = COUNT(*)
total_downtime  = SUM(duration_min)
MTTR (min)      = SUM(duration_min) / COUNT(*)
available_min   = days_in_range * 1440         (or spell working_hours where resolvable)
MTBF (min)      = (available_min - SUM(duration_min)) / COUNT(*)
availability_%  = (available_min - SUM(duration_min)) / available_min * 100
unattributed_%  = downtime on reason_code 'NR' / total downtime * 100
```

`NR` ("No Reason mentioned") stays a valid code but every summary report surfaces
`unattributed_%` so it cannot quietly absorb real causes.

---

# Frontend

`src/app/dashboardportal/juteMechanical/`

```
page.tsx                       module landing
breakdown/                     entry + daily grid + live open board
overhaul/                      overhaul register
preventiveMaintenance/         PM execution with checklist
machineHistory/                one machine, everything
reports/                       hub + layout + 12 report pages
masters/
  reasonMaster/  componentMaster/  machineAttribute/  pmSchedule/
```

Each page: `page.tsx` (smart) + `_components/` (dumb) + `hooks/` + `types/` + `utils/`.
Route constants go in `apiRoutesPortalMasters` in `src/utils/api.ts` under a
`// ── Jute Mechanical & Maintenance ──` block, prefix `MECH_`.
Service layer: `src/utils/juteMechanicalService.ts`.

Mandatory: `SearchableSelect` / `SearchableOptionSelect` for **every** dropdown (never a
raw MUI `Select`, `TextField select` or native `<select>`); `co_id`/`branch_id` from
`useSelectedCompanyCoId()` + `useSidebarContext()`; `mounted` guard against hydration
mismatch; theme tokens, never hardcoded colors; Zod on every form; no `any`.

Reports use the shared kit: `ReportGrid`, `numCol`, `ReportPanel`, `exportRowsToExcel`,
`useReportBranches`, `currentMonthRange`, `BackToReports`, and a hub `page.tsx` driven by
`fetchReportMenuTree("juteMechanical/reports")`.

# Menu

New top-level `menu_mst` row `Jute Mechanical & Maintenance` (`menu_parent_id = NULL`,
`menu_path = 'juteMechanical'`, relative path — the sidebar prepends `/dashboardportal/`),
a new `module_mst` row of the same name, child rows for each page, and report rows with
`report = 1` under a `Reports` child. Only **two** levels reach the sidebar; level-3 rows
are reports reached via `/report_menu_tree`. Grants into `role_menu_map`.
Users must re-login — the sidebar caches menus in `localStorage`.
