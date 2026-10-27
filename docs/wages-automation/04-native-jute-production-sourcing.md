# 04 — Native Sourcing from the vow3 Jute Production Module (Revised Decision)

**Decision (product owner, 2026-07-06):** production-derived wage inputs are calculated
**directly from vow3's own jute production module**, not from the legacy
`vowsls`/`EMPMILL12` schemas. This supersedes the "option (c) read-legacy-first" sourcing
in doc 03 §2 for the production streams — the collector architecture, landing rules, API
surface and run-audit design in doc 03 are unchanged; only **where collectors read from**
changes, plus the prerequisite work listed here.

## 1. What the jute production module already provides (verified 2026-07-06)

All under `src/juteProduction/`, portal persona, tenant DB, soft-delete `active=0/1`
(note: **no approval/status lifecycle on any production table**).

| Wage stream | Native source | What exists | Worker attribution today |
|---|---|---|---|
| BEAM (cuts × rate) | `jute_prod_beaming_daily` — grain (date, spell, machine, item, bm_quality); `act_cuts`, quality `jute_prod_bm_quality.bm_quality_code`, working_hours net of stoppage | Complete production capture | **Direct `eb_id` column** (nullable, client-picked, unvalidated) — stronger than legacy; `daily_ebmc_attendance` fallback join possible |
| WEAV (production × rate) | `jute_prod_weaving_daily` (cuts, close_jugar, less_production) + view `vw_weaving_daily` (`production_yds`/`production_kg`, working_hours = spell − stoppage, std/act picks & speed, `line_no`) + quality/beam maps + SQC picks | Complete production capture; **native unit is yards** (legacy view was meters) | `eb_id` column exists but is **always written NULL** ("resolved via attendance later" — `weaving_entry.py:652`); needs the attendance join |
| WIND (doff qty × rate) | `jute_prod_winding_doff` — `production_qty` (net kg per machine per doff), `item_id` (yarn item = quality identity), jugar open/close reconciliation, daily quality per machine/spell | Complete at machine grain | `operator_id` nullable, "back-filled from attendance later" — **backfill not implemented** |
| FNG (qty × rate per process) | `jute_prod_finishing_daily` — per (date, spell, process, machine/eb, quality); process tokens incl. `hemming`, `herackle`, `sacksewing`; `prod_qty` + `prod_uom` | Complete | Only `sacksewing` rows carry `eb_id` (per-worker grain native); `hemming`/`herackle` are machine-keyed with `eb_id=NULL` |
| Line incentive + SH inputs | `vw_weaving_daily` cuts + working_hours per loom; `machine_mst.line_no` **and `mech_shr_code`** (sardar/helper share code — the legacy `mach_shr_code`); `jute_prod_weaving_quality.shots`; `jute_prod_stoppage_hours` per machine/spell | Cuts, line identity, share codes, shots, per-machine downtime all present | n/a (pot is per line; S/H workers found via attendance + designation) |
| Reference pattern | Spinning `operator_stamp_query` (`spinning_query.py:411-441`) stamps `daily_doff_tbl.eb_id` by joining `daily_ebmc_attendance` → `daily_attendance` on date + spell + machine | **This is the template** for weaving/winding/finishing attribution — it is exactly the legacy worker↔machine match | |

ATT and LEAVE streams were already native (`daily_attendance` with approval workflow,
`leave_transactions`, `designation_mst.time_piece`/`piece_rate_type`).

## 2. Changes required — grouped

### A. Piece-rate masters (the biggest single gap — nothing exists)

No wage/piece-rate column exists anywhere in vow3 (verified: all jute quality masters
carry only technical attributes; the target-map EAV tables whitelist speed/eff/picks
params and reject anything else; `std_rate_card` in BOM costing is hour-based costing,
wrong grain). **A new effective-dated wages rate master is required**, keyed
polymorphically per stream:

| Stream | Rate key |
|---|---|
| BEAM | `bm_quality_id` (or bm_quality_code) |
| WEAV | `weaving_quality_id` |
| WIND | yarn `item_id` (winding has no quality master — item is identity) |
| FNG | process token × `finishing_quality_id`/`item_id` |

Suggested: one table (e.g. `wage_piece_rate_mst`: stream, ref_type, ref_id, rate, uom,
effective_from/to, co_id, branch_id) + master CRUD + FE page, replacing legacy
`EMPMILL12.tbl_wages_quality_master`. Seed v1 by one-time import from the legacy master
(migration script, not a runtime dependency).

### B. Worker attribution completion

1. **Weaving:** implement the deferred attendance join — generalize
   `operator_stamp_query` to stamp/resolve `jute_prod_weaving_daily.eb_id` (weaver
   designation, e.g. legacy 1113 → config) from `daily_ebmc_attendance` on
   date + spell + machine. Note `daily_attendance.spell` holds the spell **name string**
   while production tables hold `spell_id` — the spinning code already handles this.
2. **Winding:** implement the promised `operator_id` backfill (same pattern), or make
   the entry field mandatory.
3. **Finishing:** extend `LABOUR_PROCESSES` (`finishing_entry.py:54`) so `hemming` and
   `herackle` rows are keyed by `eb_id` like `sacksewing`, or add the machine→worker
   attendance join for them.
4. **Beaming:** keep the direct `eb_id` but validate it against attendance
   (date + spell) at save or at collection time; fall back to the attendance join when
   NULL.
5. Multiple-workers-per-machine: the stamp query is last-write-wins single `eb_id` —
   decide whether to split quantities across workers sharing a machine/spell (legacy
   BEAM grouped per worker via the attendance join, effectively splitting by whoever
   was on the machine); the collector should use the attendance join at aggregation
   time rather than trusting a single stamped id when several workers match.

### C. Line incentive / sardar-helper inputs

- **Line running/lost hours:** legacy used a monthly excel into
  `tbl_run_loom_line_hours`. Native replacement (recommended): **derive** per
  `line_no` — running hours from `vw_weaving_daily.working_hours` per loom (spell −
  stoppage) and lost hours from `jute_prod_stoppage_hours` — aggregated over the
  period, with `no_of_looms` from `machine_mst` per line. This eliminates the last
  recurring excel. Fallback if derivation can't match the manual sheet: port the
  line-hours upload as a small vow3 endpoint + table. Parity-diff both against a real
  month before choosing.
- **Sardar/helper identity:** S/H designation ids → config (constants store, doc 03
  §3.5); line share grouping via `machine_mst.mech_shr_code` + `line_no`; the legacy
  dept-code 029-043 → A/B/C wgroup mapping becomes config keyed on native dept ids.
- **Speed bands:** `reed_speed` exists nowhere in vow3. Options: (i) add a `reed_speed`
  column to `machine_mst` (+ stale-ORM fix, see §E), or (ii) use the loom `speed`
  param already in `jute_prod_weaving_target_map` and re-express the legacy
  168/162.5/144 banding as config over it. Decide during Phase 1 design; (i) is closest
  to parity.
- `shots` for the target formula is already on `jute_prod_weaving_quality.shots`.

### D. Stream-specific rules with no native carrier yet

- **Winding cop rule:** the legacy spool-vs-cop distinction (cop qty = net_weight/15,
  separate qualcodes 5401/6501) has no vow3 concept — every doff is spool-weighed kg.
  If cop wages still exist operationally, add a package-type dimension to
  `jute_prod_winding_doff` (or an item-level flag); otherwise record the rule as
  retired. **Ask operations.**
- **Weaving units:** native yards vs legacy meters — pick the unit the rate master is
  expressed in and convert in one place (collector), documented in the rate master UOM
  column.
- **`loom_production` component:** define which native figure feeds it
  (`production_yds` or `production_kg` per weaver) — it was a separate excel column in
  the legacy contract.

### E. Non-production inputs that must also leave EMPMILL12

The jute-production decision covers BEAM/WIND/FNG/WEAV/SH. To be fully legacy-free the
remaining inputs need vow3 homes too (all confirmed absent in vow3 today):

| Input | Legacy source | Needed in vow3 |
|---|---|---|
| Advances / installments (IADV) | `EMPMILL12.tbl_company_advance` | Small advances master + ledger (employee, amount, installment, balance); **one-time opening-balance migration** so installment recovery continues seamlessly |
| Canteen deduction | `canteen_details` | Canteen consumption capture (or feed from an existing canteen system) + rate in config |
| Layoff hours | `daily_layoff_deptwise` | Dept-level layoff entry (small master/entry page) — or confirm layoff is no longer practiced |
| FA rates | `EMPMILL12.tbl_njm_food_allowance` | FA rate master (decided: FA is automated, so this master is required) |

### F. Technical debt to fix on the way

- `MachineMst` ORM (`src/masters/models.py:178-190`) is **stale** — missing
  `mech_shr_code`, `line_no`, `no_of_mechines`; update before the engine uses it.
- `daily_ebmc_attendance` has **no ORM model** (raw SQL only) — add one.
- Production tables have no approval lifecycle; wage collection will read live
  soft-deleted-filtered data. Add a **period cutoff/lock concept** (or at minimum a
  collection-run warning listing production rows edited after the last run) so numbers
  don't shift silently between prepare and process.

### G. Operational prerequisite (as important as the code)

Native sourcing only works if NJM actually enters, daily and completely, in vow3:
beaming/weaving/winding/finishing production, stoppage hours, attendance **with machine
mapping** (`daily_ebmc_attendance` — written by the HRMS attendance screen/mobile app).
The engine's per-collector coverage report (doc 03 §3.4) doubles as the adoption
monitor: zero-coverage or thin streams indicate entry gaps, not code bugs. The
parallel-run month therefore validates **data-entry completeness and computation
parity** simultaneously.

## 3. What this decision simplifies (vs the legacy-read design)

1. **No legacy employee-id bridge** for collection — production rows carry native
   `eb_id`; the eb_no↔emp_code mapping matters only for one-time seeds (advance
   balances, rate-master import).
2. **No `vowsls`/`EMPMILL12` grants and no per-tenant legacy-schema config** at
   runtime; the opaque `view_proc_njm_loom_data_for_wages` DDL is no longer needed —
   `vw_weaving_daily` replaces it natively.
3. **No cross-schema coupling risk** (doc 03 §8 risk 6 retired); collectors are ordinary
   tenant-DB queries, testable on dev3 with fixtures.
4. The Phase 4 "flip collectors from legacy to native later" migration disappears —
   collectors are native from day one.
5. Parallel-run validation compares vow3 output against the legacy **excel/payroll
   outputs** (files/reports), which needs no DB grants.

## 4. Revised collector sourcing table (supersedes doc 03 §3.1 "Reads" column)

| Collector | Reads (native) | New prerequisites |
|---|---|---|
| `attendance` | `daily_attendance` (+ `designation_mst.time_piece/piece_rate_type`) | Layoff source (§2E) or drop layoff |
| `leave_canteen` | `leave_transactions` chain, holiday data | Canteen capture (§2E); holiday txn home verified in tenant DB |
| `advances` | new advances module | §2E + opening-balance migration |
| `fa` | FA rate master × attendance | §2E rate master |
| `beaming` | `jute_prod_beaming_daily` + `jute_prod_bm_quality` | Rate master (§2A); eb_id validation (§2B4) |
| `winding` | `jute_prod_winding_doff` (+ jugar reconciliation) | Rate master; operator backfill (§2B2); cop decision (§2D) |
| `finishing` | `jute_prod_finishing_daily` | Rate master; hemming/herackle eb keying (§2B3) |
| `weaving` | `vw_weaving_daily` + `jute_prod_weaving_quality` | Rate master; weaver attendance join (§2B1); unit decision (§2D) |
| `line_incentive_sh` | `vw_weaving_daily` + `machine_mst(line_no, mech_shr_code)` + `jute_prod_stoppage_hours` + attendance | Line-hours derivation decision (§2C); speed-band source (§2C); S/H designation config |
| `iftu` | attendance + leave + holidays | none |

## 5. Impact on delivery phases (amends doc 03 §5)

- **Phase 0** additionally: decisions on cop rule, weaving unit, layoff/canteen scope
  (§2D/E); legacy grants/view-DDL items are **dropped**; the employee bridge shrinks to
  a one-time seed concern.
- **Phase 1** additionally: `wage_piece_rate_mst` + CRUD + FE page; constants store;
  `MachineMst`/`daily_ebmc_attendance` ORM fixes. Collectors `attendance`,
  `leave_canteen`, `iftu` unchanged (already native).
- **Phase 2** becomes "production collectors + their attribution work": weaving
  attendance join, winding backfill, finishing labour-process extension, beaming
  validation, line-hours derivation — each collector lands together with its
  attribution fix. Advances/canteen/FA/layoff mini-modules are built here (or
  descoped by the §2D/E decisions).
- **Phase 3** (parallel-run) unchanged in method; adds the data-entry completeness
  dimension (§2G).
- **Phase 4** shrinks to frontend + cutover — the "migrate sources one by one"
  tail is gone.
