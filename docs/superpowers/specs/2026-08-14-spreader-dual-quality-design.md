# Spreader Dual-Quality Rolls — Design Spec

**Date:** 2026-08-14 · **Status:** Approved by user (chat) · **Repos:** vowerp3be + vowerp3ui · **Tenants:** dev3 + sls

## Problem

Spreader production entries carry exactly one jute quality (`spreader_prod_entry.item_id`).
Mills blend up to **two** qualities into one roll with a ratio (e.g. JTD5/ATD5 at 2:1).
The system must record both quality ids + ratio, display the blend everywhere as
`JTD5/ATD5 (2:1)`, and split roll **weight** per component quality where weight matters.
Single-quality entry must keep working unchanged (including the Android app's existing payload).

## Decisions (user-confirmed)

| Question | Decision |
|---|---|
| Ratio semantics | Real weight split — component weights = wt × ratio share |
| Ratio input | Two small positive integers (e.g. 2 : 1) |
| Blend in stock/reports | Blend = its **own row** (whole rolls); component weights exposed as extra fields |
| Blend maturity target | Ratio-weighted average of the two items' `maturity_hours` |
| Storage | Columns on `spreader_prod_entry` (approach A) — no satellite table, no pseudo-items |

## 1. Schema (one migration, dev3 + sls)

```sql
ALTER TABLE spreader_prod_entry
  ADD COLUMN item_id_2 INT NULL AFTER item_id,
  ADD COLUMN ratio_1 INT NOT NULL DEFAULT 1 AFTER item_id_2,
  ADD COLUMN ratio_2 INT NULL AFTER ratio_1,
  ADD KEY idx_spe_item2 (item_id_2);
-- rollback: ALTER TABLE spreader_prod_entry DROP KEY idx_spe_item2,
--   DROP COLUMN ratio_2, DROP COLUMN ratio_1, DROP COLUMN item_id_2;
```

- `item_id_2 IS NULL` ⇒ pure roll (all existing rows on both tenants remain valid; no data rewrite).
- Same migration replays the known `spreader_machine_attr.band_edge_1..band_edge_5` drift
  (dev3 has them, sls lacks them) onto sls — guarded `ADD COLUMN IF NOT EXISTS`-style (information_schema check) so it is rerunnable on dev3.
- ORM: `SpreaderProdEntry` (src/juteProduction/models.py) gains the 3 mapped columns.
- Rule kept: migration applied to a tenant **before** code deploy. Columns are nullable/defaulted, so old code also runs against the new schema.

## 2. Save + validation (src/juteProduction/spreader_entry.py)

- `SpreaderEntryCreate` gains optional `item_id_2: int | None`, `ratio_1: int = 1`, `ratio_2: int | None`.
  Validation: `item_id_2` ⇒ `ratio_2` required; `item_id_2 != item_id`; ratios ≥ 1 integers;
  `ratio_2` without `item_id_2` ⇒ 400.
- **Canonical form** (applied at save): dominant quality first — if `ratio_2 > ratio_1`, swap the
  (item, ratio) pairs; on equal ratios, lower `item_id` first. So ATD5/JTD5 (1:2) stores identically
  to JTD5/ATD5 (2:1).
- `SpreaderEntryUpdate` stays without quality fields (quality remains immutable on edit — delete + recreate, as today).
- Mobile app posts today's payload (no new fields) → creates a pure roll. No mobile change required.

## 3. Group lock (src/juteProduction/services/spreader_rules.py)

- The open-group invariant widens from "one item per bin group" to "one **blend** per bin group":
  lock key = canonical `(item_id, item_id_2, ratio_1, ratio_2)`.
- `_active_group_for_bin` returns the full tuple (derivation keeps the MIN-over-group trick, applied
  to all four columns — safe because a group is single-blend by construction).
- `assign_or_reuse_entry_id_grp` rejects a mismatched blend with the blend **label** in the error
  ("bin already has an open group with a different quality: JTD5/ATD5 (2:1)").
- `get_bin_state` / `entry_bin_state` response: `locked_item_id` kept for compatibility, plus
  `locked_item_id_2`, `locked_ratio_1`, `locked_ratio_2`, `locked_quality_label`.

## 4. Reads — blend label + derived quality

- **One shared label helper** (backend): SQL fragment / Python helper producing
  `item_name` for pure rows and `NAME1/NAME2 (r1:r2)` for blends. Every list/stock/report row gains
  `quality_label` while keeping raw `item_id`, `item_id_2`, `ratio_1`, `ratio_2` (ids must stay
  addressable — user requirement).
- The five single-quality assumption sites — query.py issues_by_date (~139), bins_with_stock (~189),
  group_details (~240), bins_with_stock_for_issue (~276), and spreader_rules `_active_group_for_bin`
  (~34) — extend from scalar `item_id` to the 4-column tuple.
- **Quality-wise closing stock** (spreader_stock.py `roll_stock_quality_summary`): aggregation key =
  blend tuple; blend rows show whole rolls + total weight + `comp1_item_id/comp1_wt` and
  `comp2_item_id/comp2_wt` (wt × ratio share).
- **Reports** (reports.py `spreader_production_summary` pivot, reportQueries.py): pivot entity =
  blend tuple, entity_name = blend label. `sel_items` filter matches a row if the selected item is
  either component.
- **Maturity**: target hours = ratio-weighted average of the two items' `item_maturity_mst.maturity_hours`;
  if one item has no row, use the other's value; if neither, NULL (as today).
- **Issue flow**: no schema change (`spreader_roll_issue` still carries no quality); derived group
  quality becomes the blend tuple + label.
- **Out of scope (unchanged):** juteSQC roll-wt/sliver-wt/morrah pages (independent single-quality
  readings), emulsion, drawing, batch daily assign, `jute_spreader_quality_attr`.

## 5. Frontend (vowerp3ui, spreader page + reports)

- `ProductionEntryForm`: "+ second quality" toggle reveals 2nd item dropdown + two integer ratio
  fields rendered `r1 : r2`. Validation mirrors BE. Payload adds `item_id_2`, `ratio_1`, `ratio_2`.
- Bin-lock: when the bin's open group is a blend, both dropdowns + ratio fields are forced/disabled;
  `BinStateBanner` shows `locked_quality_label`.
- Display: all grids (`DailyEntriesGrid`, `DailyIssuesGrid`, `RollStockTable` incl. quality-summary,
  `MaturityReportTable`, report pivot) switch their item column to BE-provided `quality_label`.
  No client-side label building.
- `spreaderTypes.ts` / `reportTypes.ts` gain the optional fields; issue form untouched.

## 6. Testing

- BE (src/test/): pure create unchanged; blend create + canonical swap; `item_id_2 == item_id` 400;
  `ratio_2` orphan 400; group-lock accepts same blend / rejects different blend or different ratio;
  quality summary blend row + component weights; maturity weighted-average (incl. missing-item
  fallbacks); issues_by_date blend label.
- FE: type-check + existing page behavior; browser QA via /qa-portal-page on dev3 after wiring.

## 7. Rollout order

1. Migration on dev3 → BE+FE dev test → migration on sls.
2. Code deploy (BE + FE together). Other production tenants: run the migration before their next
   code deploy (columns nullable → old code tolerant, but keep the standing rule).
