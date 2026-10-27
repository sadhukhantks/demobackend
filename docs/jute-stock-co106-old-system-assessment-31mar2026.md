# Old-system (vowsls) jute stock as on 31-Mar-2026 — Limelight Commercial Pvt. Ltd. (company_id 106)

All weights **QUINTAL** unless labelled kg. `scm_mr_line_item.accepted_weight` is stored in **kg**; the old views
divide it by 100. Source tags: **[V]** = re-verified by me this run (`old31/report/verify.py`, read-only SQL),
**[S]** stock-recon agent, **[A]** anomaly agent, **[F]** fidelity agent.

---

## 1. Headline

**The old system's own stock screen answer is 1,421.05 qtl = 142,105 kg over 28 MR lines (616 bales + 2,505 drums).**
The screen as shipped actually *printed* 1,417.28 qtl / 26 lines. [V]

The screen is `Jute_mr_in_stock_model.php` (`c:\code\awscc-github\live-portal\application\models\`), reading
`vowsls.view_jute_receipt_issue_sale`. Its definition: [S]

| Element | Old rule |
|---|---|
| Opening basis | `accepted_weight/100` (kg to qtl) — **accepted** weight, i.e. after shortage, not mill weight |
| Issues | `jute_issue.total_weight`, `is_active=1`, **no status filter** |
| Sales | `invoice_line_items.sales_weight`, `is_active=1 AND quality_id>0`, **no invoice-status filter** |
| Date cut | `tran_date <= '2026-03-31'` inside the aggregation subquery only (`tran_date` is DATETIME) |
| MR filter | on the receipt row only: `tran_status NOT IN (4,6)`, `company_id='106'` |
| Row display filter | `round(BalesStock,0)!=0 OR round(DrumsStock,0)!=0` — **weight is never tested** |

**Movement to the cut, all eligible lines [V]:** receipts 151,148.56 − issues 60,951.71 − sales 88,775.80 = **1,421.05**.

### The same date under every alternative definition [V]

| # | Definition | Lines | Qtl | kg | vs 1,421.05 |
|---|---|---|---|---|---|
| 1 | **Old screen exactly as shipped** (bale/drum row filter) | 26 | **1,417.28** | 141,728 | −3.77 |
| 2 | Row filter on weight instead of bale/drum | 27 | 1,421.05 | 142,105 | 0 |
| 3 | **Row filter bale/drum OR weight (full truth)** | 28 | **1,421.05** | 142,105 | 0 |
| 4 | No row filter — company closing over all 1,818 eligible lines | 1,818 | 1,421.05 | 142,105 | 0 |
| 5 | Old **quality-wise** report (`Jute_with_value`, "Opening Wt") | 25 qualities | 1,320.05 | 132,005 | −101.00 |
| 6 | Old view's raw sales sum (adds the company-0 line 7952) | — | 1,205.94 | 120,594 | −215.11 |
| 7 | **Mill** (`actual_weight`) basis instead of accepted | 28 | 1,987.84 | 198,784 | +566.79 |
| 8 | Drop DRAFT/open invoices from sales (approved only, dated) | — | 2,501.39 | 250,139 | +1,080.34 |
| 9 | Approved only, and count the NULL-dated invoice 17744 | — | 2,388.29 | 238,829 | +967.24 |
| 10 | Count the NULL-dated invoice under the old (draft-inclusive) rule | — | 1,307.95 | 130,795 | −113.10 |
| 11 | Receipts APPROVED (status 3) only — drops AUTO GENERATED 31 | 846 | 1,148.71 | 114,871 | −272.34 |
| 12 | No MR status filter at all (rejected receipts included) | 1,837 | 3,047.43 | 304,743 | +1,626.38 |

**Spread across defensible readings: ~1,206 to ~2,501 qtl — 1,295 qtl of pure definition on a ~1,400 qtl answer. Add
the mill-vs-accepted choice and the ambiguity is ~1,860 qtl, larger than the number itself.**

### Branch 87 (FACTORY)

**Old-system branch 87 closing = EXACTLY 0.00 qtl.** [V] Branch is not usable in the old data:

| MR-header branch | Lines | Receipts | Issues | Sales | Closing |
|---|---|---|---|---|---|
| NULL | 963 | 78,033.49 | 0.00 | 77,772.70 | **260.79** |
| 23 (HO) | 641 | 57,049.22 | 52,254.07 | 3,634.89 | **1,160.26** |
| 87 (FACTORY) | 214 | 16,065.85 | 8,697.64 | 7,368.21 | **0.00** |
| **Company** | 1,818 | 151,148.56 | 60,951.71 | 88,775.80 | **1,421.05** |

Two branch slices exist and only this one (slice by the **MR line's** branch) is coherent. Slicing by each
*transaction's own* branch gives NULL +78,033.49 / br-87 −44,885.87 / br-23 −30,759.34 [A], because every issue is filed
to 87 and every raw-jute invoice to 23 regardless of where the jute came from. Branch 87 reads 0.00 because a back-dated
batch of 28 issues (`issue_no` 81784..81916, 2,246.52 qtl, dated 2026-03-31, keyed 31-Mar 17:50 through 23-Apr) zeroed
it at the cut. [S] **The figure to quote for "branch 87" is the company figure, 1,421.05.**

---

## 2. Stock by quality and by godown (basis: definition #3, accepted weight)

| Quality (id) | Lines | Bales | Drums | Qtl | | Quality (id) | Lines | Bales | Drums | Qtl |
|---|---|---|---|---|---|---|---|---|---|---|
| A TD-9(U) (1535) | 3 | 73 | 370 | 219.04 | | A-TD10(L) (1831) | 1 | 20 | 0 | 28.50 |
| M-6 MIDD (1745) | 3 | 117 | 0 | 158.79 | | C TD-6 (744) | 1 | 0 | 71 | 26.50 |
| **NULL quality** | 1 | 0 | 775 | 150.69 | | A-TD8(U) (697) | 1 | 0 | **−24** | 9.29 |
| M.B.Bott. (1599) | 1 | 86 | 0 | 118.00 | | A TD-9(L) (1637) | 1 | 0 | 0 | 3.75 |
| TD3 (656) | 1 | 74 | 0 | 110.10 | | M.Bott. (740) | 1 | 1 | 0 | 1.49 |
| A TD-7(U) (696) | 1 | 0 | 957 | 109.28 | | A TD-8(L) (1597) | 1 | 1 | 0 | 1.48 |
| C TD-5 (743) | 2 | 0 | 208 | 105.32 | | ' BxBOT' (1706) | 1 | 1 | 0 | 1.25 |
| M-5 MIDD (1744) | 1 | 73 | 0 | 90.44 | | M.Midd. (739) | 1 | 0 | 1 | 0.23 |
| D TD-3 (1739) | 1 | 54 | 0 | 76.34 | | A-TD10(U) (1784) | 1 | 2 | 0 | 0.00 |
| J-TD-8 (737) | 2 | 53 | 0 | 72.12 | | | | | | |
| TD8(C) (1931) | 1 | 0 | 147 | 54.06 | | | | | | |
| B-BOT (1703) | 1 | 35 | 0 | 47.62 | | | | | | |
| D TD-2 (1741) | 1 | 26 | 0 | 36.76 | | **TOTAL** | **28** | **616** | **2,505** | **1,421.05** |

The old quality-wise screen instead prints 25 rows / 1,320.05 qtl: it adds two **phantom negative** rows
(`D TD-4` −96.00, `HABIJABI` −5.00, both from duplicate invoice 4146) and splits quality 697 into `A-TD8(U)` +2,706.62 /
`A TD-8(U)` −2,697.33 because it groups by name as well as code. [S]

| Godown | Lines | Bales | Drums | Qtl |
|---|---|---|---|---|
| 244 LCPL_JUTE | 19 | 547 | 2,205 | 1,218.19 |
| 356 GMPL_JUTE | 9 | 69 | 300 | 202.86 |
| **TOTAL** | **28** | **616** | **2,505** | **1,421.05** |

Both godowns are registered under branch 87 in `warehouse_details` even though the MR headers holding the stock are
filed to branch 23 or NULL — the godown is the only field in the old data that agrees the jute is at the factory. [S]

---

## 3. The 28 MR lines holding stock on 31-Mar-2026, and what happened after the cut

| Line | MR | MR print no | MR date | Br | Quality | Godown | Bales | Drums | Qtl | Fate after 31-Mar-2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| 29343 | 18495 | (A)LC98/23-24 | 2023-12-15 | NULL | *none* | LCPL | 0 | 775 | 150.69 | **never moved. Still in stock today. MISSING FROM sls** |
| 45007 | 27932 | LC369/25-26 | 2026-02-27 | 23 | M.B.Bott. | LCPL | 86 | 0 | 118.00 | issued 03–13 Apr (6 issues) |
| 7950 | 5320 | (A)3/20-21 | 2021-05-29 | NULL | TD3 | LCPL | 74 | 0 | 110.10 | **never moved in 5 yrs. Still in stock today** |
| 45034 | 27953 | LC390/25-26 | 2026-03-31 | 23 | A TD-7(U) | LCPL | 0 | 957 | 109.28 | issued 01–11 Apr (5) |
| 44644 | 27685 | LC323/25-26 | 2025-12-29 | 23 | M-6 MIDD | LCPL | 74 | 0 | 106.52 | issued 08–18 Apr (7) |
| 45014 | 27938 | LC376/25-26 | 2026-03-06 | 23 | A TD-9(U) | LCPL | 73 | 1 | 105.95 | issued Apr–15 May (4); sls `actual_qty`=1 vs 74 |
| 45033 | 27952 | LC389/25-26 | 2026-03-31 | 23 | C TD-5 | LCPL | 0 | 208 | 105.30 | issued 01–29 Apr (7) |
| 44491 | 27597 | LC304/25-26 | 2025-12-10 | 23 | A TD-9(U) | GMPL | 0 | 323 | 101.54 | issued 08–09 Apr (2) |
| 44986 | 27923 | LC360/25-26 | 2026-02-14 | 23 | M-5 MIDD | LCPL | 73 | 0 | 90.44 | issued 04–23 Apr (8) |
| 37539 | 23451 | LC59/24-25 | 2024-12-17 | 23 | D TD-3 | LCPL | 54 | 0 | 76.34 | **sold on the NULL-dated invoice LCP/DEC/049/24-25** |
| 45028 | 27949 | LC386/25-26 | 2026-03-23 | 23 | TD8(C) | LCPL | 0 | 147 | 54.06 | issued 03–08 Apr (3) |
| 45030 | 27951 | LC388/25-26 | 2026-03-26 | 23 | B-BOT | LCPL | 35 | 0 | 47.62 | issued 01–29 Apr (4) |
| 44563 | 27637 | LC317/25-26 | 2025-12-20 | 23 | J-TD-8 | GMPL | 33 | 0 | 43.59 | issued 08–22 Apr (2) |
| 44826 | 27815 | LC340/25-26 | 2026-01-17 | 23 | M-6 MIDD | LCPL | 30 | 0 | 37.83 | issued 18–23 Apr (4) |
| 37538 | 23451 | LC59/24-25 | 2024-12-17 | 23 | D TD-2 | LCPL | 26 | 0 | 36.76 | **sold on the same NULL-dated invoice** |
| 44332 | 27504 | LC299/25-26 | 2025-11-30 | 23 | J-TD-8 | GMPL | 20 | 0 | 28.53 | issued 06 Apr (1) |
| 45012 | 27937 | LC375/25-26 | 2026-03-06 | 23 | A-TD10(L) | LCPL | 20 | 0 | 28.50 | issued 01 Apr (1) |
| 45029 | 27950 | LC387/25-26 | 2026-03-25 | 23 | C TD-6 | LCPL | 0 | 71 | 26.50 | issued 01–08 Apr (2) |
| 44564 | 27637 | LC317/25-26 | 2025-12-20 | 23 | M-6 MIDD | GMPL | 13 | 0 | 14.44 | issued 23 Apr (1) |
| 44673 | 27697 | LC324/25-26 | 2025-12-30 | 23 | A TD-9(U) | LCPL | 0 | 46 | 11.55 | issued 23 Apr–15 May (3); MR status 24 |
| 43289 | 26913 | LC235/25-26 | 2025-10-21 | 23 | A-TD8(U) | GMPL | 0 | **−24** | 9.29 | **never moved. Still today. 211 drums issued vs 187 received** |
| 40725 | 25412 | LC122/25-26 | 2025-07-25 | 23 | A TD-9(L) | GMPL | 0 | 0 | 3.75 | **never moved. HIDDEN by the old screen's row filter** |
| 44758 | 27758 | LC334/25-26 | 2026-01-08 | 23 | M.Bott. | GMPL | 1 | 0 | 1.49 | issued 10 Apr (1) |
| 44832 | 27820 | LC346/25-26 | 2026-01-17 | 23 | A TD-8(L) | LCPL | 1 | 0 | 1.48 | issued 01 Apr (1) |
| 45031 | 27951 | LC388/25-26 | 2026-03-26 | 23 | ' BxBOT' | LCPL | 1 | 0 | 1.25 | issued 08 Apr (1) |
| 38463 | 24002 | LC84/24-25 | 2025-03-09 | 23 | M.Midd. | GMPL | 0 | 1 | 0.23 | issued 23 Apr (1) |
| 44899 | 27873 | LC347/25-26 | 2026-01-25 | 23 | C TD-5 | LCPL | 0 | 0 | 0.02 | **never moved. HIDDEN by the row filter** (rounding dust) |
| 43340 | 26942 | LC241/25-26 | 2025-10-22 | 23 | A-TD10(U) | GMPL | 2 | 0 | 0.00 | never moved; **shown by the old screen at 0.00 qtl** (2 stray bales) |

**Fate summary [S]:** 1,034.10 qtl issued to production Apr–May 2026 · 113.10 qtl sold on the undated invoice ·
**273.85 qtl still sitting in the old system today** (lines 29343, 7950, 43289, 40725, 44899, 43340).

---

## 4. Source-data issues in the old system, ranked by qtl impact

| # | Issue | Count | Qtl at risk | Treatment |
|---|---|---|---|---|
| 1 | **Half the receipts are synthetic** — `mr_good_recept_status='31'` AUTO GENERATED, branch NULL, shortage exactly 0, dated 2021-05-29 to 2024-03-13; first real issue is 2024-06-27 | 963 lines / 599 MRs | 78,033.49 (51.6% of receipts) | **KEEP** — it is the opening block and cannot be re-derived from gate entries. Never branch-filter a legacy report; never treat status 31 as unapproved |
| 2 | **Rejected + soft-deleted lines still carry weight** (status 4: 21 lines / 1,738.58; `is_active=0`: 7 lines / 590.95) | 28 lines | 2,329.53 | **KEEP, filter explicitly** — a raw `SUM(accepted_weight)` over-receipts by roughly the whole closing balance |
| 3 | **Invoice-status ambiguity in sales** — 18 lines on 8 DRAFT/open invoices (1,181.34), 1 active line on a REJECTED invoice (114.10), 2 lines on the NULL-dated approved invoice 17744 (113.10) | 21 lines | 1,408.54 | **FIX IN OLD DATA BEFORE IMPORT** — approve or void the drafts, date invoice 17744, deactivate line 4120 |
| 4 | **Mill vs accepted basis** — the two differ on 682 lines and `shortage_kgs` explains only 133.81 of it | 691 lines | 566.79 | **RULING (R2).** If accepted wins, backfill `sls.jute_mr_li.accepted_weight`; do not reconstruct from `shortage_kgs` (recovers 24%) |
| 5 | **Duplicate invoice 4146** keyed against dangling `mr_line_id` 11279/11280, re-entered correctly as invoice 4561 | 2 lines | 101.00 | **FIX IN OLD DATA** — deactivate 4146; sole cause of the 1,421.05 vs 1,320.05 split and of the phantom negative quality rows |
| 6 | **Sale line pointing at another company's MR line** — line 4120 to mr_line 7952, whose MR header is `company_id=0` | 1 line | 114.10 | **FIX IN OLD DATA** — repoint or deactivate. Today it only distorts the quality report |
| 7 | **Two dead lines never moved in 5 years** — 7950 (110.10, since 2021) and 29343 (150.69, no quality at all) | 2 lines | 260.79 | **RULING (R8)** — write off, or carry forward as opening stock |
| 8 | **Split-yarn double-posted issues** — pairs 81759/81763 and 81650/81654: same line, date, qty, weight, created in the same second, differing only in `yarn_type_id` | 4 rows | 15.00 over-issued | **FIX IN OLD DATA** — deactivate the surplus copy of each pair |
| 9 | **Negative / stray counts** — 43289 is −24 drums (211 issued vs 187 received); 43340 holds 2 bales at 0.00 qtl; 45014 leaves 1 stray drum | 3 lines | 0 (count only) | **KEEP + note** — weight is right, the counts are not. Do not open sls on these counts |
| 10 | **Issue dated before its own receipt** (issue 75717) | 1 row | 34.79 | **KEEP** — period total correct; only date-sliced ledgers show a transient negative |
| 11 | **Inter-company mirror lines** (`src_line_id` clones, not transfers) — 4,427.39 qtl still un-consumed on cos 1 and 2; 9 co-106 lines holding 312.96 qtl of this closing are mirrored downstream | 1,111 lines | 4,427.39 group-wide (312.96 in this closing) | **KEEP for co 106, flag for group** — consolidated group stock double-counts unless netted |
| 12 | **Denormalised counters are dead** — `weight_consumed`/`bales_consumed`/`drums_consumed` = 0 on all 1,844 legacy lines; `sales_weight` disagrees with real invoices on 17 lines by 1,190.44 | 718 lines | 62,142 if believed | **DROP** — never read them. `jute_current_stock` / `quality_stock` / `quality_wise_stock` are built on them and return 113,900 / 12.3M qtl |
| 13 | **`view_jute_mr_in_stock` is structurally broken** — the sales UNION leg hardcodes `mr_print_no=''` which is in the GROUP BY, so 1,109 phantom negative groups (−88,888.90) sit beside 1,120 positive (+89,677.82) | 1 view | 89,677.82 if filtered `>0` | **DROP** — not a reconciliation target. Only `Jute_mr_in_stock_model.php` is authoritative |
| 14 | **UNION (not UNION ALL)** in `view_jute_receipt_issue_sale` silently collapses identical issue rows | 2 rows | 15.00 under-issued | **KEEP, know about it** — systemic under-count of issues; small here |
| 15 | **`tran_date` is DATETIME** — 458 receipt rows carry a time component; a naive string compare drops all 28 movements booked on the cut date | 458 rows | 2,246.52 | **KEEP** — a query-writing trap, not a data defect |

**What is NOT wrong [A]:** zero dangling / cross-company / rejected-target `jute_issue` rows; zero MR headers
disagreeing with their lines; zero duplicate MR lines; zero over-consumed lines; exactly one negative line, by 0.01 qtl.
The old data is referentially **clean** and definitionally **ambiguous** — pinning the number down needs rulings, not
repairs.

---

## 5. Import fidelity into sls, and the 31-Mar gap

| Old to sls | Old | Matched | Missing | Extra | Value fidelity | Real losses / dropped columns |
|---|---|---|---|---|---|---|
| `scm_mr_hdr` → `jute_mr` | 1,191 | 1,175 | 16 | 83 | dates, vehicle, challan, weight **0 mismatches** | 16 missing = all REJECTED (correct). `out_date` is not the MR date on 859 rows. GE back-ref lost on 73, reused on 288 |
| `scm_mr_line_item` → `jute_mr_li` | 1,844 | 1,817 | 27 | 174 | `actual_weight`, rate, quality, warehouse, shortage, active **0/1,817** | **1 genuine loss: line 29343 (150.69 qtl).** `accepted_weight` NULL 100%, `total_price` NULL 100%, `challan_quality_id` lost on 855, `actual_qty` wrong on 45014 (1 vs 74) |
| `jute_issue` → `jute_issue` | 2,684 | 2,260 | 424 | 0 | date, branch, line link, qty, weight, value **0/2,260** | **27 APPROVED issues lost = 2,226.79 qtl.** `jute_quality_id`/`yarn_type_id` NULL on all 2,260; godown, bale_loose, uom, fin_year dropped |
| `invoice_hdr` → `sales_invoice` | 692 | 685 | 7 | 0 | amounts, tax, dates **0/685** | 7 missing = the 7 rejected invoices (correct); invoice no. text collapsed to integer |
| `invoice_line_items` → `sales_invoice_dtl` | 1,114 | 1,093 | 21 | 0 | weight, qty, rate, amounts **0/1,093** | **18 APPROVED lines silently skipped = 2,697.33 qtl** (all item 6 / quality 697, no `_map_invoice_items` entry; `n_skipped_lines`=18). 2 lines lost `jute_mr_li_id` (101.00) [V]. No column for sales_bale / sales_drum / quality_id |

**Unit finding: no scale error was introduced.** Every weight crossed verbatim in its original unit — MR line qtl→qtl
(0/1,817), `shortage_kgs` kg→kg, issue qtl→qtl (0/2,260), sales qtl→qtl (0/1,093). [F] The real basis problem is that
the old screens open at **accepted** weight while sls has `accepted_weight` NULL on 100% of legacy lines and can only
open at **mill** weight: a permanent **+566.79 qtl (+0.38%)** over 691 lines, of which only 133.81 is recoverable from
`shortage_kgs`. **Live hazard (post-cut, not legacy):** sls branch 87 holds 83 headers (ids 27954–28068,
`jute_mr_date` NULL) whose 174 lines carry 1,006,940 in `actual_weight` keyed in **KILOGRAMS**, in the same column where
all 1,817 legacy rows are **QUINTAL** — ~1.0M qtl of phantom stock if read as quintal, ~10,069 qtl if read correctly.

| 31-Mar-2026 | Old (accepted, old screen) | Old (mill) | sls (approved-only) |
|---|---|---|---|
| Receipts | 151,148.56 | 151,715.35 | 151,564.66 |
| Issues | 60,951.71 | 60,951.71 | 58,724.92 |
| Sales | 88,775.80 | 88,775.80 | 84,998.13 |
| **Closing** | **1,421.05** | **1,987.84** | **7,841.61** |

**Gap: sls is 6,420.56 qtl HIGH, and it reconciles to the cent [V]**

| Leg | Cause | Qtl |
|---|---|---|
| Sales | 18 approved sale lines skipped by the migration (item 6 / quality 697) | **+2,697.33** |
| Sales | 9 draft invoices — the old view deducted them, sls (`status_id`=1) does not (definition, not data loss) | **+1,080.34** |
| Issues | 27 approved issue rows never imported | **+2,226.79** |
| Receipts | mill-vs-accepted basis | +566.79 |
| Receipts | MR line 29343 never imported | −150.69 |
| | **Total = 7,841.61 − 1,421.05** | **+6,420.56** |

Anchoring on the old *screen* (1,421.05) gives a cleaner split than anchoring on the raw view sum (1,205.94) — the
114.10 company-0 line and the 101.00 duplicate-invoice pair cancel out of both sides. **~4,773 qtl of the gap is
genuinely missing data, 1,080 qtl is a stock-rule difference, 567 qtl is the basis change.**

**Contradictions between the three input reports, resolved [V].** (a) 1,421.05 / 1,205.94 / 2,388.28 are all
arithmetically correct and differ only in which sale lines count — 1,421.05 = the old screen, 1,205.94 = raw view sum
(adds 101.00 dangling + 114.10 company-0), 2,388.28 = approved invoices only; **trust 1,421.05** as "what the old system
said". (b) The fidelity agent's claim that a counted sale hangs off **line 7950** is wrong: its only sale (invoice 2942,
line 4118) is `is_active=0` on a rejected header, so the view never counts it and 7950 legitimately holds 110.10 qtl;
the 215.10 is line 7952 (114.10, MR header `company_id=0`) plus 11279/11280 (101.00, dangling). (c) The anomaly agent's
"branch 87 = −44,885.87" is correct but meaningless — it slices movements by their own branch field; sliced by the MR
line's branch, branch 87 is exactly 0.00.

---

## 6. Rulings the user must give (decision list only — no plan steps)

| # | Ruling | Options | Default I would take |
|---|---|---|---|
| R1 | **Which figure IS "the old stock at 31-Mar"?** | 1,417.28 (screen as printed) / **1,421.05** (screen, weight-complete) / 1,320.05 (quality report) / 2,501.39 (approved sales only) | 1,421.05 |
| R2 | **Accepted or mill weight** as the opening basis in sls | accepted (backfill `jute_mr_li.accepted_weight`, matches every old report) / mill (status quo, +566.79) | accepted |
| R3 | **Branch** — is the whole legacy balance branch 87, or does branch stay unassigned for legacy rows | all to 87 (what sls already did) / preserve the NULL vs 23 distinction | all to 87 |
| R4 | **DRAFT invoices** — 8 invoices / 18 lines / 1,181.34 qtl, open since 2022 | approve (stock falls) / void (stock rises 1,080.34) / leave, accepting permanent old-vs-new divergence | void; four years stale |
| R5 | **NULL-dated invoice 17744** (approved, 113.10 qtl, lines 37538+37539) | date it before the cut (removes 113.10) / date it after / leave NULL (both systems keep the jute as stock) | date it |
| R6 | **Line 4120 / invoice 2944** — active sale line on a rejected invoice against a `company_id=0` MR line, 114.10 qtl | deactivate the line / leave | deactivate |
| R7 | **Duplicate invoice 4146** — 101.00 qtl on dangling lines 11279/11280, already re-keyed as invoice 4561 | deactivate 4146 / leave | deactivate |
| R8 | **The 2 dead lines** — 7950 (110.10, 2021) and 29343 (150.69, no quality, absent from sls) | write off both / carry both as opening stock / carry 7950, write off 29343 | needs a physical stock check first |
| R9 | **Do the 28 lines with old stock stay OPEN in sls?** carry the 31-Mar balance as opening stock, or treat sls's own balance (its post-cut issues already consumed 1,034.10 of it) as authoritative | carry the old closing / trust sls post-cut movements / reconcile line by line | carry the old closing, then replay post-cut movements |
| R10 | **Re-import the 27 missing approved issues (2,226.79) and the 18 skipped sale lines (2,697.33)?** | yes / no | yes — every target line already exists in sls, so it is mechanically safe |
| R11 | **Re-import MR line 29343** (150.69 qtl; `actual_quality` NULL, so it needs an item mapping first) | yes, with a quality assigned / write off | tied to R8 |
| R12 | **Bale/drum counts** — 43289 is −24 drums, 43340 holds 2 bales at 0 weight, 45014 imported as qty 1 vs 74 | fix counts from old / open on weight only and reset the counts | fix 45014, reset the rest |
| R13 | **Split-yarn duplicate issues** — 15.00 qtl over-issued across 4 rows | deactivate the surplus copy / leave | deactivate |
| R14 | **Group mirror lines** — 312.96 qtl of this closing also sits on cos 1/2 books (4,427.39 group-wide) | net at group level / out of scope for co 106 | out of scope now, flag it |
| R15 | **Post-cut KG rows in sls** — 83 headers / 174 lines with `actual_weight` in kg inside a quintal column | normalise to quintal / add a unit column / keep and make every report vintage-aware | normalise — the only fix that does not have to be repeated in every report |

**Working files:** verification SQL `old31/report/verify.py` · stock recon `old31/stockrecon/` (A1/A3, by_quality, by_godown)
· anomalies `old31/task2/` (INDEX.txt, A36 closing lines) · fidelity `old31/task3/` (stock_recon_per_line.csv).
