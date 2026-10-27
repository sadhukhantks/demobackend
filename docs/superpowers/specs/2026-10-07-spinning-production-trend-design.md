# Spinning Production Trend report (day-wise, quality-wise, kg per frame)

Design + build note, 2026-10-07. Built the same day on `claudehost` (uncommitted) for live
review; nothing is final until the owner has seen it.

## 0. What the owner asked for

> Need to make a new set of reports in jute production module which gives day wise summary
> of spinning production (quality wise as well) and average kg per frame. Here the idea is to
> be able to see the trend over a period of a few days and quality wise. Think of what kinds
> of charts would also make it easy to see this. Think like a data analyst and assess what
> would be useful to see this.

## 1. Data-analyst assessment

### 1.1 The question behind the report

Spinning output on any day is the product of two things:

    production kg = (frame-shifts run) x (kg per frame-shift)

A day that looks "low" is low for one of two very different reasons: fewer frames or shifts
ran (a holiday, one shift only, frames down for maintenance), or the frames that ran produced
less per shift (speed, breakages, hands, a coarser/finer yarn mix). The existing DPR prints
one day; the Doff Analysis shows one frame at a time. Neither shows the two factors side by
side over a run of days, which is what tells a manager *which* lever moved. That is the gap
this report fills.

### 1.2 Metrics chosen (and why)

| Metric | Definition | Why it is in |
|---|---|---|
| Production kg | SUM of `daily_doff_tbl.net_weight`, spinning weight types ('SPG1','Auto'), active rows, scoped by the machine spine (same predicate as Doff Analysis / winding-weaving difference) | The headline. Same source and scope as every other spinning figure so the numbers agree across reports |
| Frame-shifts | COUNT DISTINCT (date, frame, shift letter). Shift letter = first character of `spell_code` (A1/A2 collapse into A), same convention as the DPR's "frame runs" | The volume lever. A frame that ran 3 shifts counts 3; a Sunday with one shift is comparable with a weekday |
| Kg per frame-shift | production kg / frame-shifts | **The "average kg per frame" the owner asked for**, normalised per shift so days with different shift counts compare. This is the productivity lever |
| Frames run | COUNT DISTINCT frame per day | Shown next to frame-shifts so "fewer frames" and "fewer shifts" are distinguishable |
| Kg per frame (day) | production kg / frames run | The naive "per frame" figure, kept in the table for people who think in it; the chart uses the per-shift version |
| Avg doff kg | production kg / doffs | A bobbin-fill signal: a falling average with steady frame-shifts means lighter doffs, not fewer |
| Std target kg | Sum of the Spinning Production grid's *Std Prod* per (date, spell, frame, yarn) unit, resolved exactly as Doff Analysis does (`build_units`: frozen `jute_prod_spinning_daily.std_prod` when non-zero, else recomputed from Spinning Standards + the day's SQC count) | Puts "is this good?" on the chart. Without a target a trend only says up/down |
| Target achievement % | actual kg of the units that HAVE a target / their target kg x 100, with a **coverage %** (share of the day's kg that had a target) | Avoids the Doff Analysis "blank if any unit lacks a target" rule, which would blank most days of a long range; comparing covered actual with covered target never overstates achievement. Coverage below 100% is printed with the figure |
| Quality share % | quality kg / period kg | Mix explains totals: a swing from 13-SKWP to 7.5 LBS twine changes kg/frame-shift with no change in frame performance |
| Previous period | The same number of days immediately before the range | Every KPI tile carries a delta so the reader does not have to remember last week |

What was deliberately **left out**: efficiency % at machine level (the Spinning Production
planning grid owns that), worker-level cuts (Spinning Low Producer owns that), doff weight
bands (Doff Analysis owns that), and any new table — everything is read from the doff log and
the masters that already exist.

### 1.3 Charts chosen (the "what kinds of charts" part)

Rules applied: one y-axis per chart (never a dual axis), categorical colour only where the
series are the subject, a legend for every multi-series chart, every chart has a table twin,
phone-first sizing.

| # | Chart | Form | Reads as |
|---|---|---|---|
| A | Production by day, **stacked by quality** (toggle: stack by shift) | stacked columns, one colour per quality in fixed volume order, qualities past 7 folded into "Other" | the total trend and the mix at once; switching to shift shows whether a dip is a missing shift |
| B | Kg per frame-shift by day | single line (emphasis colour), period-average reference line, grey standard-target line when standards exist | the productivity lever on its own axis, against "normal" and against the norm |
| C | Kg per frame-shift by quality over days | multi-line, same quality colours as chart A | which quality is drifting; a coarse count naturally sits higher, so lines are compared with themselves over time, not with each other |
| D | Frames ranked by kg per frame-shift | horizontal bars, one hue, fleet-average reference line | which frames pull the average down; the table beside it says LOW / WATCH against same-quality peers |
| KPI row | Production, avg per day, kg per frame-shift, frames per day, target achievement, best/worst day | stat tiles with deltas vs the previous period | the summary a phone screen can show without scrolling |

Not used: pies/donuts (mix is better read on the stacked columns and in the share column),
dual-axis "kg + kg/frame" overlays (invents correlation), a frame x date heatmap (Doff
Analysis already has that pivot).

## 2. Scope and rules

- **Source**: `daily_doff_tbl` only. Scope predicate imported from `doff_analysis_query`
  (`_SCOPE_WHERE`), frame label `_FRAME_EXPR`, quality label `_QUALITY_EXPR` — one definition.
- **Range cap**: 93 days (a quarter). Default on the page: 14 days ending on the hub's date.
- **Quality filter**: optional `item_id`, server-side (so frame-shift counts stay exact for the
  chosen yarn). Options come from the qualities seen in the unfiltered load.
- **Shift letter**: A/B/C from the spell code's first character, anything else 'G'.
- **Frame-shift per quality**: a frame that changed yarn mid-shift counts once under each
  yarn (same reading as Doff Analysis' quality x shift average). Noted on the page.
- **Targets** are an overlay: if the standards tables are missing on a tenant the report still
  loads with blank targets and a note (same fallback as Doff Analysis).
- **Previous period**: `[date_from - N, date_from - 1]`, N = range length; only production,
  frame-shifts and frames are compared (no targets), fetched in the same query.
- Day averages use **producing days** (days with at least one doff); calendar days are shown.
- **Frame "vs peers" %**: the frame's kg per frame-shift divided by the expected figure for
  its own quality mix (weighted by its frame-shifts per quality, using each quality's period
  average). LOW < 90 %, WATCH 90–97 %, OK otherwise. Text label, never colour alone.

## 3. API

`GET /api/juteProductionReports/spinning_production_trend?co_id&branch_id&date_from&date_to[&item_id]`

```
data: {
  summary: { date_from, date_to, calendar_days, producing_days, production_kg, avg_kg_per_day,
             frame_shifts, kg_per_frame_shift, frames_avg_per_day, doffs, avg_doff_kg,
             target_kg, target_ach_pct, target_coverage_pct,
             best_day: {date, kg} | null, worst_day: {date, kg} | null,
             previous: { date_from, date_to, producing_days, production_kg, avg_kg_per_day,
                         frame_shifts, kg_per_frame_shift, frames_avg_per_day } },
  days:        [{ date, weekday, shifts: [..], frames, frame_shifts, doffs, kg, kg_per_frame_shift,
                  kg_per_frame, avg_doff_kg, target_kg, target_ach_pct, target_coverage_pct }],
  day_quality: [{ date, item_id, quality, kg, frames, frame_shifts, doffs, kg_per_frame_shift,
                  target_kg, target_ach_pct }],
  day_shift:   [{ date, shift, kg, frames, doffs }],
  qualities:   [{ item_id, quality, kg, share_pct, days, frames, frame_shifts, doffs,
                  kg_per_frame_shift, avg_doff_kg, target_kg, target_ach_pct, target_coverage_pct }],
  frames:      [{ machine_id, frame, kg, days, frame_shifts, doffs, kg_per_frame_shift,
                  expected_kg_per_frame_shift, vs_peers_pct, status, qualities: [..],
                  target_kg, target_ach_pct }],
  meta: { co_id, branch_id, date_from, date_to, item_id, notes: [..] }
}
```

Files: `src/juteProduction/spinning_trend_query.py`, `src/juteProduction/spinning_trend.py`
(pure builder `build_trend` + router), tests `src/test/test_juteprod_spinning_trend.py`.

## 4. Frontend

Page `juteProduction/report/spinningProductionTrend` (hub dropdown entry via the menu
migration below). Tabs: **Daily Trend** (KPI row, chart A, chart B, day table), **Loss
Analysis** (see §8), **Quality
Wise** (chart C, quality table, day x quality pivot with a measure toggle kg / kg per
frame-shift / frame-shifts), **Frame Wise** (chart D, frame table). Filter row: branch (sidebar
norm), from/to with 7/14/30-day presets, quality, Submit. Excel / PDF / Print of the tables of
the open tab through the shared `GridSection` kit; charts are on-screen only.

Charts: `@mui/x-charts` 8.29.3 (added to `package.json`; matches MUI 7 / Data Grid 8).
Colours: the validated reference categorical palette, slots assigned in volume order and
never recycled (run `scripts/validate_palette.js` from the dataviz skill: all checks pass on a
white surface; aqua/yellow/magenta are below 3:1 so every chart has a table twin).

## 5. Menu

`dbqueries/migrations/20261007_add_spinning_production_trend_menu.sql` — one `menu_mst` row
under the Production Reports hub (`report = 1`), role grants keyed off `DATABASE()` copying
what Doff Analysis holds on each tenant (dev3: roles 1, 3, 12 edit; sls: 13 edit, 14 / 33 / 53 /
54 / 56 / 59 / 61 view). Applied on **dev3** and on **sls** (owner-requested for
`slsuser@vowerp.co.in`, role 13) on 2026-10-07 — menu_id 1075 on sls, 8 grants. The page was
already reachable by URL without the row because the portal permission check falls back to
the parent `juteProduction/report` menu; the row puts it in the hub dropdown.

## 6. Data facts behind the defaults (read-only, 2026-10-07)

- sls branch 29 (co 2): ~700–900 doffs/day, 29–36 frames, 5–9 qualities, 3 shift letters, 24–31 t
  on a full day, 8.6 t on a one-shift Sunday (2026-09-27) — the case the frame-shift
  normalisation exists for. Standards + SQC counts present; 2,303 of 2,307 frozen
  planning rows carry a std_prod, so the target line is populated there.
- sls branch 87 (co 106) started doffing on 2026-10-06 (24 frames, 3 qualities).
- dev3 has 25 doff rows in June–July 2026 and nothing after, so the live preview with real
  shape is on sls (read-only report).

## 7. Open points for the owner

1. Target achievement uses "covered actual vs covered target" with a coverage %. OK, or
   prefer the Doff Analysis rule (blank unless every unit has a target)?
2. LOW / WATCH thresholds for frames (90 % / 97 % of same-quality peers) — adjust?
3. ~~Should the sls menu row be applied now?~~ Applied 2026-10-07 on the owner's instruction.
4. Default range 14 days ending on the hub date; cap 93 days — fine?

## 8. Loss analysis + global filters (added 2026-10-07, afternoon)

Owner: "Think like an analyst ... the main idea is to be able to identify where we are
losing out on productivity/efficiency" and "apply the other filters (shift wise, count wise,
others) to the other charts as well".

### 8.1 What the sls data said (br 29, 24 Sep–7 Oct 2026, read-only)

- 58 installed spinning frames, 33 doffed at all: frame-shift utilisation vs installed
  capacity 45 %. The biggest "loss" is capacity not run, not slow frames.
- Frames that ran achieved 70.5 % of standard (gap 142 t). The gap is systemic, not a tail:
  median frame-shift 72 %, top-10 frames hold 42 % of the gap.
- Quality-specific: 16(96)-SKWP 91 %, the two big SKWP counts 70 %, twine / HSWP 50–58 %.
- Shift C 67 % vs A 73 %; Sun 66 % / Wed 68 % lowest weekdays; Thu 74 % best.
- Contract hands: 37 frame-shifts at 240 kg per frame-shift vs 337 for regular spinners.
- No stoppage entries exist for spinning; no actual speed / TPI rows for co 2, so rate
  losses cannot be attributed to speed vs twist vs count — recorded as a data gap.

### 8.2 Loss decomposition (the "Loss Analysis" tab)

    capacity at standard = installed frames x producing days x shift letters seen x std/frame-shift
      - frames that never ran        (installed - frames that doffed) x days x shifts x std/fs
      - shifts not run               frames that ran x days x shifts - frame-shifts run, x std/fs
      = standard of the frame-shifts that ran   (= target_kg when coverage is 100 %)
      - below standard while running (standard - actual)
      = actual

Idle frame-shifts are costed at the period's average standard per frame-shift and include
planned stops — stated on the tab. The bridge is hidden under a quality filter (frames are not
dedicated to a quality); under a frame filter installed = 1; under a shift filter shifts = 1.

Cuts of the efficiency gap, all from the same allocated unit targets: by shift (+ achievement
% by shift per day), by weekday, by quality (gap kg + share), by frame (Pareto with cumulative
share, mean / sd of the frame's per-frame-shift achievement), by spinner (top 10 by gap; a
unit's target is split over its spinners by kg share), regular vs contract hands. Actionable
lists: short frame-shifts (doffs <= 60 % of the frame's median doffs per frame-shift, median
>= 4 — the only time-lost proxy while spinning has no stoppage entries) and the 25 worst
frame-shifts (date, shift, frame, spinner, quality, doffs, kg, standard, gap). Frame x day
achievement grid (diverging around 100 %, number in every cell), worst frame first.
Histogram of frame-shifts by achievement band + median: a centred hump reads "systemic",
a long left tail reads "specific frame-shifts".

### 8.3 Global filters

Quality (count), shift letter and frame are server-side binds on the one scan (and the target
units are cut the same way), so every chart, table, KPI and the previous-period comparison use
the same slice. Options come from the unfiltered load; changing a filter refetches at once
(dates still apply on Submit). `meta` echoes the active filters; the export letterhead carries
them.

### 8.4 Data gaps worth closing (recommendations)

1. Record spinning stoppages (the Stoppage Hours entry exists; no spinning rows on sls) so
   the time-lost component stops being a doff-count proxy.
2. Record actual speed and actual TPI per frame / quality (the standards map already has
   `value_role = 'actual'`), so the rate loss can be split into speed vs twist vs count.
3. Review the twine / HSWP standards: 50–58 % achievement across every frame on those
   qualities points at the standard, not the frames.

### 8.5 Frames needing attention + drill-down into the frame-wise reports (added 2026-10-07, evening)

Owner: link this to the frame-wise report with the worker ("low producer") detail so the
analysis breaks down to the frames that require attention.

- **Attention list** (`loss.attention`, built by `build_attention`): a frame is listed when any
  rule fires, with the reasons printed — LOW / WATCH vs same-quality peers; mean achievement
  10+ points under the fleet median; achievement swinging 15+ points (sd) across its
  frame-shifts (3+ frame-shifts); 2+ short frame-shifts; 2+ appearances in the worst-25 list.
  Biggest gap first. Shown as chips on the Frame Wise tab and as the first section of the
  Loss Analysis tab; exported with the loss tables.
- **Drill-down**: tapping (phone) / double-clicking (desktop) a frame — attention chip or row,
  Pareto row, worst or short frame-shift row, heat-grid frame label — opens a frame dialog
  (bottom sheet on phones) with the period figures and reasons, and links that carry the
  branch, the period and the active shift / quality filter:
  - Doff Analysis → Frame Analysis with that frame's drill-down open (day x shift weights,
    doff events per spinner); range clamped to its 31-day cap from the end;
  - Spinning Low Producer filtered to the frame (+ shift / quality), range clamped to 62 days;
  - for one frame-shift: Doff Analysis → Doff Details of that date, and Low Producer for that
    day + shift (the spinner's record);
  - "Focus this report on it" sets the trend report's own frame filter.
- Both target pages now read these query params on load (`doffAnalysis`: `tab`, `frame`,
  `date_from`, `date_to`; `spinningLowProducer`: `date_from`, `date_to`, `frame`, `shift`,
  `item_id`), on top of the hub's `branch_id` / `date`. Link builders: `drillLinks.ts`.

### 8.6 Standard and target as lines, not achievement % (owner ruling 2026-10-07, evening)

> "Achievement of std is not really a useful parameter. Instead give standard in line charts
> and in addition to that the actual." + "Also include targets if parameter entered."

- Every line chart now plots the **actual** (solid, coloured) with the **standard** (dashed,
  ink grey) and, where entered, the **target** (dotted, lighter grey) on the same axis:
  production by day (standard / target lines over the stacked actual), kg per frame-shift by
  day, one small multiple per quality, one small multiple per shift.
- KPI tiles show kg, never %: standard for the frame-shifts run vs actual vs gap; standard
  per frame-shift vs actual (and target); a target tile only when a target is entered.
- Achievement % survives only as a non-phone table column ("Actual / std %") and as the
  COLOUR of the frame x day grid (the number in each cell is actual - standard in kg; the
  colour is that gap relative to the standard so frames on different counts still compare).
  The achievement histogram was replaced by an actual-vs-standard scatter: one point per
  frame-shift (standard kg across, actual kg up), coloured by shift, with the "actual =
  standard" diagonal — a cloud under the line is systemic, stragglers are specific
  frame-shifts. `loss.frame_shift_points` carries the points (date, shift, frame, kg,
  std_kg, tgt_kg); `loss.ach_histogram` stays in the API but is no longer drawn.
- **Target** = the planning grid's `target_prod` = `p100prod x target_eff / 100`
  (`spinning_query.py`), i.e. the same 100 %-production as the standard at the yarn's
  *target* efficiency (`jute_prod_spng_target_map`, `id_type='qid'`, `value_role='target'`,
  `param='eff'`); a frozen non-zero `jute_prod_spinning_daily.target_prod` wins. Only
  units with a target efficiency carry a target (`tgt_*` fields), so on sls co 2 (no target
  eff rows) only the standard shows; dev3 co 1 has target eff rows and shows both.
- API renames (breaking, same day): `target_kg -> std_kg`, `target_ach_pct -> std_ach_pct`,
  `target_coverage_pct -> std_coverage_pct`; new `std_kg_per_frame_shift`,
  `std_frame_shifts`, `tgt_kg`, `tgt_kg_per_frame_shift`, `tgt_frame_shifts` on every cut.
  Standard / target per frame-shift divide by the frame-shifts the overlay covers, so a
  half-covered day is not understated. Query: `get_trend_target_inputs_query`.

### 8.7 Value labels and the standard at the standard count (TKT-000222, owner asks 2026-10-11)

> "Add data point in graphs so that numbers are visible" (ticket) + "include this for the
> standard/targets as well. Additionally add another line which gives the production as per
> the std count instead of the actual count."

- **Value labels** on every chart but the frame-shift scatter: day totals over the stacked
  bars, the figure at every point of every line (actual, standard, target, std at std count),
  the number after each ranked / waterfall bar and above the histogram bars. Dense ranges thin
  to every k-th point (latest point always labelled; `chartLabels.ts`). The actual is labelled
  in text ink, the context lines in their own colour; each number sits on the side of its
  point with the wider gap to the nearest other line (above for the actual, below for a
  context line when nothing is near), and a value two lines share at a point is printed once.
  Drawn as chart children (`ValueLabels` /
  `BarEndLabels` in `TrendCharts.tsx`), not the library's `barLabel`, which is clipped at the
  drawing area.
- **Standard at the standard count** (`stdc_*`, dash-dot, muted ochre `STDC_COLOR`). The
  planning formula `p100prod = std_speed x minutes x act_count x spindles / (36 x 14400 x
  2.2046 x std_tpi)` takes the day's SQC count, so a yarn spun heavier than its nominal count
  inflates the standard together with the actual. The new line restates the standard with the
  yarn's standard count (`jute_yarn_mst.jute_yarn_count`) in place of `act_count`:
  `unit_stdc` recomputes a live standard with the standard count, and scales a frozen
  (Processed) standard by `std_count / act_count` using the counts the planning grid stored on
  that row (`jute_prod_spinning_daily.std_count / act_count`, falling back to the live yarn
  count and SQC average). Blank — never a copy of the standard — where a unit has no standard
  or either count is unknown, so the line is drawn only where it means something; the API
  note states the coverage ("n of m frame-shifts with a standard"). Carried on every cut as
  `stdc_kg`, `stdc_kg_per_frame_shift`, `stdc_frame_shifts`; drawn on production by day, kg
  per frame-shift by day, the quality multiples and the shift multiples; table columns
  (desktop only), a pivot measure and the standard KPI tiles' captions carry the figure.
  Target is not restated at the standard count (not asked).
