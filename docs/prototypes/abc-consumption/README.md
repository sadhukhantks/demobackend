# ABC consumption analysis – interactive prototype

Design artifact for the planned **Inventory → Reports → ABC Consumption Analysis** report
(rank items by issue quantity and value in classes A/B/C/…, then derive a "should be
purchased" list from usage trends). It is a single self-contained HTML page with a
read-only snapshot of one tenant's issue data embedded, so every option recomputes in the
browser. It is **not** product code: nothing here is imported by `src/`.

## Files

| File | Purpose |
|---|---|
| `extract.py` | Read-only pull from the tenant DB (default `sls`, branches 29 and 4, window 2020-01-01..2024-12-31): item × year × expense-type issue qty/value (receipt-lot rate, same `lot_rate_sql` expression as the inventory reports), plus per-item current stock, pending PO, pending indent, latest approved SR rate and last issue date. Writes `data.json`. |
| `template.html` | The page: filters, Pareto curve, class breakdown, year totals, top-20, classification table, suggested-purchase table, method notes. Placeholder `/*__DATA__*/` receives the JSON. |
| `build.py` | Embeds `data.json` into `template.html` → `index.html`. |
| `data.json`, `index.html` | **Generated, git-ignored** – they contain real tenant consumption and stock figures. |

## Regenerate

```bash
cd vowerp3be
set -a && . ./env/database.env && set +a          # DB credentials
uv run python -I docs/prototypes/abc-consumption/extract.py   # ~10 min on sls (pending PO/indent subqueries)
python3 -I docs/prototypes/abc-consumption/build.py
```

Open `index.html` directly in a browser. On the claudehost dev VM it is also served to
logged-in portal users at `/prototypes/abc-consumption` by a server-only Next route that
is deliberately kept out of git.

## Rules the prototype implements (candidate rules for the real report)

- Issues counted: `issue_hdr.status_id IN (1,3,5,20)`, active, by `issue_date`; value =
  `issue_qty × lot rate` (`COALESCE(accepted_rate, rate)` net of the SR line discount).
- Value rank → class. Fixed buckets (default 100 per class, A/B/C named, D = rest) or
  Pareto (A ≤ 70 %, B ≤ 90 % of cumulative value, C rest).
- Qty rank with the same rule, within each UOM (default) or overall.
- Capital and Overhauling expense types excluded by default (one-off, not consumption).
- Names/groups containing "CHARGES" / "MISC EXPNS" excluded by default (service lines;
  `item_mst.tangible` is 1 for almost every item so it cannot be used).
- Avg annual qty = total qty ÷ years with an issue. Trend = last two selected years vs the
  earlier ones (missing years = 0): Up/Down beyond ±15 %, else Flat; "New" if only recent.
- Suggested qty = avg annual × horizon/12 + avg annual × safety/12 − stock − pending PO −
  pending indent (deduction optional), floored at 0; valued at the latest approved SR rate
  in the company (fallback: historic average rate). Default: 12 months + 1 month safety,
  classes A+B, only items issued since 2022.

## Findings from the sls snapshot (2026-10-11)

- Branch 29: 5,035 active non-service items, 1,691 lakh; the top 100 carry 78.5 % of value,
  120 items cover 80 %. 2024 is already a weak year (≈ half of 2023), 2020 is low too.
- Pending PO/indent figures look stale (one oil item: 3.1 lakh L pending PO + 2.2 lakh L
  pending indent), so deducting them can hide real needs.
- Duplicate item masters split consumption (SERVO ECOFIN J / SERVO ECO FIN J / SERVO ECOFIN-N).
- The pending PO/indent SQL (copied from the Add-Items dialog position query) takes ~7 min
  for all 5.4k items at branch 29; the live endpoint needs join-based aggregates or must
  compute position only for the classes being planned.
