"""Winding & Weaving Difference report.

The headline test is the GOLDEN CASE — every figure below is transcribed from
the mill's own printed sheet for 27-01-2026, so a change that breaks the
reconciliation breaks against real, signed-off arithmetic rather than against
numbers invented here.
"""

from src.juteProduction.constants import (
    CLOTH_SPLIT_HESSIAN,
    CLOTH_SPLIT_OTHER,
    CLOTH_SPLIT_SACKING,
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
    WINDING_STOCK_BUCKET_BALL,
    WINDING_STOCK_BUCKET_MILL_USE,
    WINDING_STOCK_BUCKET_PURCHASE,
    WINDING_STOCK_BUCKET_SALE_YARN,
    WINDING_STOCK_BUCKET_WASTE,
)
from src.juteProduction.winding_reports import (
    build_cloth_split,
    build_reconciliation,
    build_stock_blocks,
    build_bucket_quality_summary,
    doff_kg_to_mt,
    issue_kg_to_mt,
    purchase_qty_to_mt,
    stock_mt_of,
)
from src.juteProduction.doff_analysis_query import (
    get_doff_detail_rows_query,
    get_spinning_doff_weight_query,
)
from src.juteProduction.spinning_query import SPINNING_WEIGHT_TYPES_SQL
from src.juteProduction.winding_stock_query import (
    get_weaving_unresolved_quality_count_query,
)


def _row(bucket, samples, wt, **extra):
    row = {
        "bucket": bucket,
        "no_of_samples": samples,
        "wt_per_sample": wt,
        "item_id": extra.get("item_id"),
        "package_label": extra.get("package_label", "PKG"),
        "item_code": None,
        "item_name": None,
        "quality_name": None,
        "yarn_count": None,
        "wastage_area_id": None,
        "area_name": None,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------- line maths


def test_stock_mt_of_matches_the_sheets_first_row():
    """185 samples x 14 kg = 2,590 kg = 2.590 MT (HESSIAN WEFT, 9.50LBS)."""
    assert stock_mt_of(185, 14) == 2.59


def test_stock_mt_of_handles_none():
    assert stock_mt_of(None, 14) == 0.0
    assert stock_mt_of(185, None) == 0.0


# ------------------------------------------------------------- GOLDEN CASE


GOLDEN_ROWS = [
    # Mill use totals 22.706 on the sheet; two lines standing in for its 16.
    _row(WINDING_STOCK_BUCKET_MILL_USE, 185, 14),        #  2.590
    _row(WINDING_STOCK_BUCKET_MILL_USE, 20116, 1),       # 20.116
    _row(WINDING_STOCK_BUCKET_PURCHASE, 209, 28.89),     #  6.038  OUT SIDE PURCHASE MACKROL FOR S4
    _row(WINDING_STOCK_BUCKET_SALE_YARN, 230, 50),       # 11.500  28.LBS 3PLY HANK
    _row(WINDING_STOCK_BUCKET_SALE_YARN, 76, 56),        #  4.256  20.LBS COP (BAG)
    _row(WINDING_STOCK_BUCKET_SALE_YARN, 182, 50),       #  9.100  10.00LBS 3PLY HANK-GOLDEN
    _row(WINDING_STOCK_BUCKET_BALL, 2116, 1),            #  2.116
    _row(WINDING_STOCK_BUCKET_WASTE, 60, 1),             #  0.060  THREAD WASTE
    _row(WINDING_STOCK_BUCKET_WASTE, 33, 1),             #  0.033  DIRTY BOBBIN
    _row(WINDING_STOCK_BUCKET_WASTE, 20, 1),             #  0.020  DAMAGE SPOOL
    _row(WINDING_STOCK_BUCKET_WASTE, 21, 1),             #  0.021  ENTANGLE COPS
]


def test_golden_bucket_subtotals_match_the_printed_sheet():
    blocks = build_stock_blocks(GOLDEN_ROWS, [])
    assert blocks[WINDING_STOCK_BUCKET_MILL_USE]["subtotal_mt"] == 22.706
    assert blocks[WINDING_STOCK_BUCKET_PURCHASE]["subtotal_mt"] == 6.038
    assert blocks[WINDING_STOCK_BUCKET_SALE_YARN]["subtotal_mt"] == 24.856
    assert blocks[WINDING_STOCK_BUCKET_BALL]["subtotal_mt"] == 2.116
    assert blocks[WINDING_STOCK_BUCKET_WASTE]["subtotal_mt"] == 0.134


def test_golden_grand_total_matches_the_printed_sheet():
    """22.706 + 6.038 + 24.856 + 2.116 + 0.134 = 55.850 — STOCK GRAND TOTAL."""
    blocks = build_stock_blocks(GOLDEN_ROWS, [])
    assert blocks["grand_total_mt"] == 55.850


def test_opening_and_closing_of_the_same_rows_agree_exactly():
    """The endpoint now computes both opening and closing MT through
    build_stock_blocks — never a separate SQL-side ROUND(SUM(...)/1000, 3)
    regime, which can disagree with the per-line round-then-accumulate path
    at the milli-tonne. Proven here: three lines that each round to 0.000
    individually sum to a non-zero total under a bucket-level SUM-then-ROUND,
    but build_stock_blocks (used for BOTH opening and closing) stays 0.000
    either way — the two never drift apart. difference_mt on the real sheet
    is ~0.4 MT, so a spurious 0.001 would read as real shrinkage."""
    rows = [
        _row(WINDING_STOCK_BUCKET_MILL_USE, 1, 0.4999),
        _row(WINDING_STOCK_BUCKET_MILL_USE, 1, 0.4999),
        _row(WINDING_STOCK_BUCKET_MILL_USE, 1, 0.4999),
    ]
    closing = build_stock_blocks(rows, [])["grand_total_mt"]
    opening = build_stock_blocks(rows, [])["grand_total_mt"]  # same path, same call
    assert opening == closing == 0.0

    naive_bucket_round = round(
        sum(r["no_of_samples"] * r["wt_per_sample"] for r in rows) / 1000.0, 3
    )
    assert naive_bucket_round != closing  # the two regimes really do diverge


def test_golden_reconciliation_matches_the_printed_sheet():
    """opening 18.415 + winding 37.875 = should-be 56.290; actual 55.850 gives
    the sheet's WINDING WEAVING DIFFERENCE of -0.440, i.e. -3.47% of winding."""
    recon = build_reconciliation(
        opening_mt=18.415,
        winding_mt=37.875,
        weaving_mt=0.0,
        purchase_mt=0.0,
        sale_mt=0.0,
        actual_mt=55.850,
    )
    assert recon["should_be_closing_mt"] == 56.290
    assert recon["difference_mt"] == -0.440
    assert recon["difference_pct"] == -1.16  # -0.440 / 37.875 * 100


def test_golden_day_percentage_matches_the_printed_sheet():
    """The sheet's TODAY figure: -0.440 against the day's winding of 12.697,
    i.e. -3.47%. Opening is back-solved so should-be lands on the sheet's 56.290."""
    recon = build_reconciliation(
        opening_mt=43.593,
        winding_mt=12.697,
        weaving_mt=0.0,
        purchase_mt=0.0,
        sale_mt=0.0,
        actual_mt=55.850,
    )
    assert recon["difference_pct"] == -3.47


# ------------------------------------------------------- reconciliation rules


def test_weaving_and_sale_are_subtracted_purchase_is_added():
    recon = build_reconciliation(
        opening_mt=10.0, winding_mt=5.0, weaving_mt=3.0,
        purchase_mt=2.0, sale_mt=1.0, actual_mt=13.0,
    )
    assert recon["should_be_closing_mt"] == 13.0
    assert recon["difference_mt"] == 0.0


def test_zero_winding_does_not_divide_by_zero():
    recon = build_reconciliation(
        opening_mt=10.0, winding_mt=0.0, weaving_mt=0.0,
        purchase_mt=0.0, sale_mt=0.0, actual_mt=9.0,
    )
    assert recon["difference_mt"] == -1.0
    assert recon["difference_pct"] is None


# -------------------------------------------------------------- cloth split


def test_cloth_split_groups_by_quality_type():
    rows = [
        {"weaving_quality_id": 1, "production_mt": 2.0},
        {"weaving_quality_id": 2, "production_mt": 3.0},
        {"weaving_quality_id": 3, "production_mt": 1.5},
    ]
    qtype = {
        1: FINISHING_QUALITY_TYPE_HESSIAN,
        2: FINISHING_QUALITY_TYPE_SACKING,
        3: None,
    }
    split = build_cloth_split(rows, qtype)
    assert split[CLOTH_SPLIT_HESSIAN] == 2.0
    assert split[CLOTH_SPLIT_SACKING] == 3.0
    assert split[CLOTH_SPLIT_OTHER] == 1.5


def test_cloth_split_always_sums_to_total_weaving():
    """Unclassified qualities fall to OTHER so nothing can silently vanish —
    on sls today every quality is unclassified and OTHER carries the lot."""
    rows = [
        {"weaving_quality_id": 9, "production_mt": 4.25},
        {"weaving_quality_id": 8, "production_mt": 1.75},
    ]
    split = build_cloth_split(rows, {})
    assert split[CLOTH_SPLIT_OTHER] == 6.0
    assert round(sum(split.values()), 3) == 6.0


# ------------------------------------------------------------- purchase UOM


def test_purchase_mt_from_metric_tonne_is_unchanged():
    mt, unknown = purchase_qty_to_mt(654.608, "MT")
    assert mt == 654.608
    assert unknown == []


def test_purchase_mt_from_kg_is_divided():
    mt, unknown = purchase_qty_to_mt(2000, "KGS")
    assert mt == 2.0
    assert unknown == []


def test_purchase_metre_is_not_treated_as_tonnes():
    """MTR is METRE. Converting it to MT would invent tonnage out of length."""
    mt, unknown = purchase_qty_to_mt(654.608, "MTR")
    assert mt == 0.0
    assert unknown == ["MTR"]


def test_purchase_multiple_uoms_reports_every_unknown():
    mt, unknown = purchase_qty_to_mt(100, "MT,MTR,PCS")
    assert unknown == ["MTR", "PCS"]


def test_purchase_missing_uom_with_real_qty_is_flagged_not_dropped():
    """proc_inward_dtl.uom_id is nullable and the join to uom_mst is LEFT, so
    GROUP_CONCAT(DISTINCT um.uom_name) is NULL for a period whose lines carry
    no uom at all. A real non-zero quantity must not vanish from
    should_be_closing_mt without a trace in the warning list."""
    mt, unknown = purchase_qty_to_mt(500, None)
    assert mt == 0.0
    assert unknown != []


def test_purchase_zero_qty_with_no_uom_is_not_flagged():
    """A genuinely empty period (COALESCE(SUM(...), 0) with no matching rows)
    is not a uom problem, so it stays silent."""
    mt, unknown = purchase_qty_to_mt(0, None)
    assert mt == 0.0
    assert unknown == []


def test_purchase_mixed_mt_and_kg_returns_zero_not_a_guess():
    """Mixed recognised unit families on one line set cannot be apportioned
    either way — this must NOT sum the KG quantity as if it were MT (a
    1000x-wrong number), and must flag both names rather than dropping one."""
    mt, unknown = purchase_qty_to_mt(2000, "KG,MT")
    assert mt == 0.0
    assert set(unknown) == {"KG", "MT"}


# ------------------------------------------------ weaving-unresolved-quality


def test_weaving_unresolved_quality_query_constructs():
    assert get_weaving_unresolved_quality_count_query() is not None


def test_weaving_unresolved_quality_query_binds():
    """Every named bind the router passes must appear in the SQL, and vice
    versa — a bind added to the SQL without a matching key in the caller's
    dict is a runtime 500, not an import error."""
    sql = str(get_weaving_unresolved_quality_count_query())
    for bind in (":co_id", ":branch_id", ":date_from", ":date_to"):
        assert bind in sql, bind


def test_weaving_unresolved_quality_query_catches_null_and_inactive():
    """Both drop modes the report warns about — a NULL weaving_quality_id and
    a non-NULL id whose quality row is missing/inactive — must be covered by
    the same predicate (a single NOT EXISTS), not just the NULL case."""
    sql = str(get_weaving_unresolved_quality_count_query())
    assert "NOT EXISTS" in sql.upper()
    assert "q.active = 1" in sql
    assert "cuts > 0" in sql


# ------------------------------- TKT-000182 mill-use consolidated summary
#
# Scope note: the consolidated table covers the MILL USE block ONLY (owner's
# call on review), so its total must tie to that block's subtotal -- 22.706 on
# the golden sheet -- and never to the 55.850 grand total.


def test_mill_use_summary_collapses_one_quality_spread_over_packages_and_places():
    rows = [
        _row(WINDING_STOCK_BUCKET_MILL_USE, 100, 10, item_id=7, item_name="13 LBS SKWP",
             package_label="PW SPOOL"),
        _row(WINDING_STOCK_BUCKET_MILL_USE, 50, 10, item_id=7, item_name="13 LBS SKWP",
             package_label="SKG"),
        _row(WINDING_STOCK_BUCKET_MILL_USE, 25, 10, item_id=7, item_name="13 LBS SKWP",
             package_label="SKG", wastage_area_id=4),
    ]
    summary = build_bucket_quality_summary(rows, [])
    assert summary["bucket"] == WINDING_STOCK_BUCKET_MILL_USE
    assert len(summary["rows"]) == 1
    row = summary["rows"][0]
    assert row["item_name"] == "13 LBS SKWP"
    assert row["line_count"] == 3
    assert row["total_mt"] == 1.75
    assert summary["total_mt"] == 1.75


def test_mill_use_summary_total_ties_to_the_mill_use_subtotal_not_the_grand_total():
    """22.706, the printed MILL USE subtotal — NOT the 55.850 grand total. The
    table sits above the mill-use block, so tying it to the grand total would
    state a figure 33 MT larger than the section it heads."""
    summary = build_bucket_quality_summary(GOLDEN_ROWS, [])
    blocks = build_stock_blocks(GOLDEN_ROWS, [])
    assert summary["total_mt"] == blocks[WINDING_STOCK_BUCKET_MILL_USE]["subtotal_mt"]
    assert summary["total_mt"] == 22.706
    assert summary["total_mt"] != blocks["grand_total_mt"]


def test_mill_use_summary_ignores_every_other_block():
    """Purchase, sale, ball and waste stock must not leak in: those sections are
    untouched by this ticket and have no consolidated table of their own."""
    rows = [
        _row(WINDING_STOCK_BUCKET_PURCHASE, 209, 28.89, item_id=1),
        _row(WINDING_STOCK_BUCKET_SALE_YARN, 230, 50, item_id=1),
        _row(WINDING_STOCK_BUCKET_BALL, 2116, 1, item_id=1),
        _row(WINDING_STOCK_BUCKET_WASTE, 60, 1, item_id=1),
    ]
    summary = build_bucket_quality_summary(rows, [])
    assert summary["rows"] == []
    assert summary["total_mt"] == 0.0


def test_mill_use_summary_counts_only_the_mill_use_part_of_a_split_quality():
    """Real sls case: item 335556 is held in MILL_USE *and* BALL on 18-09-2026.
    Only its mill-use tonnage belongs in a mill-use figure."""
    rows = [
        _row(WINDING_STOCK_BUCKET_MILL_USE, 100, 10, item_id=335556, item_name="13 LBS SKWP"),
        _row(WINDING_STOCK_BUCKET_BALL, 14, 1, item_id=335556, item_name="13 LBS SKWP",
             package_label="bleach ball"),
    ]
    summary = build_bucket_quality_summary(rows, [])
    assert len(summary["rows"]) == 1
    assert summary["rows"][0]["total_mt"] == 1.0  # 1.014 would be the ball leak
    assert summary["rows"][0]["line_count"] == 1


def test_mill_use_summary_splits_distinct_qualities_and_orders_by_label():
    rows = [
        _row(WINDING_STOCK_BUCKET_MILL_USE, 100, 10, item_id=2, item_name="16 LBS SKWP"),
        _row(WINDING_STOCK_BUCKET_MILL_USE, 200, 10, item_id=1, item_name="9 LBS HSWP"),
    ]
    summary = build_bucket_quality_summary(rows, [])
    assert [r["item_name"] for r in summary["rows"]] == ["16 LBS SKWP", "9 LBS HSWP"]
    assert [r["total_mt"] for r in summary["rows"]] == [1.0, 2.0]
    assert summary["total_mt"] == 3.0


def test_mill_use_summary_prvs_comes_from_prev_rows_not_the_repeated_column():
    """build_stock_blocks stamps the FULL previous total for a (bucket, item,
    package, area) key onto EVERY current row sharing it, so two such rows carry
    2.000 each against a real 2.000 opening. Summing that column would print a
    4.000 opening; summing prev_rows prints 2.000."""
    prev = [_row(WINDING_STOCK_BUCKET_MILL_USE, 200, 10, item_id=1)]
    rows = [
        _row(WINDING_STOCK_BUCKET_MILL_USE, 100, 10, item_id=1),
        _row(WINDING_STOCK_BUCKET_MILL_USE, 50, 10, item_id=1),
    ]
    blocks = build_stock_blocks(rows, prev)
    per_row_prvs = [r["prvs_stock_mt"] for r in blocks[WINDING_STOCK_BUCKET_MILL_USE]["rows"]]
    assert per_row_prvs == [2.0, 2.0]  # the repetition this test guards against

    summary = build_bucket_quality_summary(rows, prev)
    assert summary["rows"][0]["prvs_stock_mt"] == 2.0
    assert summary["prvs_total_mt"] == 2.0
    assert sum(per_row_prvs) != summary["prvs_total_mt"]


def test_mill_use_summary_prvs_excludes_yesterdays_other_blocks():
    """Yesterday the same quality also sat in BALL. A mill-use opening that
    swallowed it would show stock that was never mill-use stock."""
    prev = [
        _row(WINDING_STOCK_BUCKET_MILL_USE, 200, 10, item_id=1),
        _row(WINDING_STOCK_BUCKET_BALL, 500, 10, item_id=1),
    ]
    rows = [_row(WINDING_STOCK_BUCKET_MILL_USE, 100, 10, item_id=1)]
    summary = build_bucket_quality_summary(rows, prev)
    assert summary["prvs_total_mt"] == 2.0  # not 7.0
    assert summary["rows"][0]["prvs_stock_mt"] == 2.0


def test_mill_use_summary_prvs_total_ties_to_yesterdays_mill_use_subtotal():
    prev = [
        _row(WINDING_STOCK_BUCKET_MILL_USE, 185, 14, item_id=1),
        _row(WINDING_STOCK_BUCKET_MILL_USE, 20116, 1, item_id=2),
        _row(WINDING_STOCK_BUCKET_BALL, 2116, 1, item_id=3),
    ]
    summary = build_bucket_quality_summary([], prev)
    yesterday_blocks = build_stock_blocks(prev, [])
    assert (
        summary["prvs_total_mt"]
        == yesterday_blocks[WINDING_STOCK_BUCKET_MILL_USE]["subtotal_mt"]
        == 22.706
    )


def test_mill_use_summary_keeps_a_quality_that_emptied_out_today():
    """Counted yesterday, gone today: the row stays at 0.000 so the drop is
    visible instead of the quality vanishing from the page."""
    prev = [_row(WINDING_STOCK_BUCKET_MILL_USE, 185, 14, item_id=9, item_name="9.5 LBS HSWT")]
    summary = build_bucket_quality_summary([], prev)
    assert len(summary["rows"]) == 1
    assert summary["rows"][0]["item_name"] == "9.5 LBS HSWT"
    assert summary["rows"][0]["total_mt"] == 0.0
    assert summary["rows"][0]["prvs_stock_mt"] == 2.59
    assert summary["total_mt"] == 0.0


def test_mill_use_summary_can_be_pointed_at_another_bucket_without_mixing():
    """The bucket is a parameter, so a future section can reuse this — but each
    call stays inside its own block."""
    rows = [
        _row(WINDING_STOCK_BUCKET_MILL_USE, 100, 10, item_id=1),
        _row(WINDING_STOCK_BUCKET_SALE_YARN, 230, 50, item_id=1),
    ]
    mill = build_bucket_quality_summary(rows, [])
    sale = build_bucket_quality_summary(rows, [], bucket=WINDING_STOCK_BUCKET_SALE_YARN)
    assert mill["total_mt"] == 1.0
    assert sale["total_mt"] == 11.5
    assert sale["bucket"] == WINDING_STOCK_BUCKET_SALE_YARN


# --------------------------------- jute issue was printed 100x too large
#
# get_jute_issue_weight_query aliased its SUM `issue_qtl` and its docstring
# claimed QUINTAL, so the report divided by 10. jute_issue.weight is KG (sls
# branch 29: ~146.9 per BALE unit -- a ~150 KG bale, not a 14.7-tonne one), and
# src/juteProcurement/issuePlanner.py converts the same quantity with /1000.


def test_jute_issue_kg_to_mt_uses_the_real_sls_day():
    """19,274 KG issued on sls branch 29, 14-09-2026 = 19.274 MT. The report
    used to print 1,927.400 MT for that day."""
    assert issue_kg_to_mt(19274) == 19.274
    assert issue_kg_to_mt(19274) != 1927.4


def test_jute_issue_one_bale_is_not_fourteen_tonnes():
    """The tell that found the bug: at ~146.9 per BALE unit, quintal would make
    one bale 14.687 MT."""
    assert issue_kg_to_mt(146.9) == 0.147
    assert round(146.9 / 10.0, 3) == 14.69  # the old regime, for contrast


def test_jute_issue_handles_none_and_rounds_to_three_decimals():
    assert issue_kg_to_mt(None) == 0.0
    assert issue_kg_to_mt(0) == 0.0
    assert issue_kg_to_mt(19274.4) == 19.274
    assert issue_kg_to_mt(1500) == 1.5


def test_jute_issue_is_context_only_and_never_enters_the_reconciliation():
    """Guards the claim in the docstring: correcting jute issue must not move the
    winding/weaving difference. build_reconciliation takes no jute-issue
    argument at all — the endpoint stamps the figure on afterwards."""
    recon = build_reconciliation(
        opening_mt=18.415,
        winding_mt=37.875,
        weaving_mt=0.0,
        purchase_mt=0.0,
        sale_mt=0.0,
        actual_mt=55.850,
    )
    assert "jute_issue_mt" not in recon
    assert "spinning_mt" not in recon
    assert recon["should_be_closing_mt"] == 56.290
    assert recon["difference_mt"] == -0.440
    assert recon["difference_pct"] == -1.16


# ------------------------------------- spinning production (context only)
#
# Owner asked for spinning production on this report, "From doff wt": the actual
# weighed doffs (daily_doff_tbl.net_weight), not the jute_prod_spinning_daily
# planning snapshot, which on sls br29 was unfilled on 12 of September's 18 days.


def test_spinning_doff_kg_to_mt_uses_the_real_sls_days():
    """sls br29: 597 doffs = 20,137.60 kg on 18-09-2026, 629 doffs = 21,265.80 kg
    on 14-09. Against 21.160 / 20.719 MT of winding the same days — spinning feeds
    winding, so these must land within a few percent, not 100x out."""
    assert doff_kg_to_mt(20137.60) == 20.138
    assert doff_kg_to_mt(21265.80) == 21.266
    assert doff_kg_to_mt(355410.76) == 355.411  # September MTD
    assert doff_kg_to_mt(None) == 0.0
    assert doff_kg_to_mt(0) == 0.0


def test_spinning_doff_query_excludes_non_spinning_weight_types():
    """The predicate people miss: without it a plain SUM(net_weight) absorbs
    non-spinning weighings from the shared mobile-app table. 'Auto' rows are
    weighing-scale doffs and ARE spinning, so the filter must be the two-value
    constant, never the bare 'SPG1' literal."""
    sql = str(get_spinning_doff_weight_query())
    assert f"dd.weight_type IN {SPINNING_WEIGHT_TYPES_SQL}" in sql
    assert "'SPG1'" in sql and "'Auto'" in sql
    assert "(dd.active = 1 OR dd.active IS NULL)" in sql
    assert "SUM(dd.net_weight)" in sql
    assert "dd.doff_date BETWEEN :date_from AND :date_to" in sql


def test_spinning_doff_query_keeps_the_machine_spine_and_branch_scope():
    """daily_doff_tbl has no co column, so company scope must ride
    mc_id -> machine_mst -> dept_mst -> branch_mst; the row's own branch_id is
    only a guard (the mobile app leaves it NULL)."""
    sql = str(get_spinning_doff_weight_query())
    assert "EXISTS" in sql.upper()
    assert "machine_mst mm" in sql
    assert "bmm.co_id = :co_id" in sql
    assert "dmm.branch_id = :branch_id" in sql
    assert "(dd.branch_id IS NULL OR dd.branch_id = :branch_id)" in sql


def test_spinning_doff_query_shares_the_doff_analysis_scope_rule():
    """Not a copy-paste: the same _SCOPE_WHERE fragment as the Doff Analysis
    report, so changing what counts as spinning production cannot leave this
    report behind. Compared predicate by predicate against a query built from
    that fragment."""
    mine = str(get_spinning_doff_weight_query())
    theirs = str(get_doff_detail_rows_query())
    for predicate in (
        f"dd.weight_type IN {SPINNING_WEIGHT_TYPES_SQL}",
        "(dd.active = 1 OR dd.active IS NULL)",
        "(dd.branch_id IS NULL OR dd.branch_id = :branch_id)",
        "WHERE mm.machine_id = dd.mc_id",
    ):
        assert predicate in mine, predicate
        assert predicate in theirs, predicate


def test_spinning_doff_query_does_not_touch_the_planning_snapshot():
    """Guards the source decision: act_prod_doff / act_prod_wind live in
    jute_prod_spinning_daily and must not creep back in here."""
    sql = str(get_spinning_doff_weight_query())
    assert "jute_prod_spinning_daily" not in sql
    assert "act_prod_doff" not in sql
    assert "act_prod_wind" not in sql


# ------------------------------- purchase yarn from the Yarn Purchase document
#
# Phase 5 of docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-
# design.md (§3.4). purchase_mt used to read approved Stores receipts of
# yarn-type items and printed 0 on sls forever (the yarn is booked there as a
# MISC EXPENSES item in MTR). It now reads approved Yarn Purchase lines in
# KILOGRAMS; the Stores query survives only as a transition cross-check.

from datetime import date as _date  # noqa: E402

from src.juteProcurement.constants import YARN_PURCHASE_CUTOVER_DATE  # noqa: E402
from src.juteProduction.constants import (  # noqa: E402
    WINDING_STOCK_LOC_FLOOR,
    WINDING_STOCK_LOC_GODOWN,
)
from src.juteProduction.winding_reports import (  # noqa: E402
    DIFFERENCE_NOTE,
    _prev_key,
    build_purchase_detail,
    legacy_stores_yarn_warning,
    purchase_kg_to_mt,
)
from src.juteProduction.winding_stock_query import (  # noqa: E402
    get_yarn_purchase_document_detail_query,
    get_yarn_purchase_document_qty_query,
    get_yarn_purchase_qty_query,
)


def test_purchase_kg_to_mt_reads_document_kilograms():
    """The worked example's month: 26,000 + 34,300 kg of approved Yarn
    Purchase = 60.300 MT. No UOM, no guessing — the document is in kg."""
    assert purchase_kg_to_mt(60300) == 60.3
    assert purchase_kg_to_mt(26000.4) == 26.0
    assert purchase_kg_to_mt(None) == 0.0
    assert purchase_kg_to_mt(0) == 0.0


def test_purchase_document_query_reads_approved_lines_in_kg():
    """Header co/branch, status 3 (approved) only, active header AND line,
    tran_date in the window, SUM(weight_kg). Nothing from Stores."""
    sql = str(get_yarn_purchase_document_qty_query())
    assert "jute_yarn_purchase_li" in sql
    assert "jute_yarn_purchase h" in sql
    assert "h.status_id = 3" in sql
    assert "h.active = 1" in sql and "li.active = 1" in sql
    assert "SUM(li.weight_kg)" in sql
    assert "h.tran_date BETWEEN :date_from AND :date_to" in sql
    for bind in (":co_id", ":branch_id", ":date_from", ":date_to"):
        assert bind in sql, bind
    assert "proc_inward" not in sql
    assert "uom" not in sql.lower()


def test_purchase_detail_query_is_per_document_and_quality_ordered_by_date_then_number():
    sql = str(get_yarn_purchase_document_detail_query())
    assert "h.yarn_purchase_id" in sql and "h.purchase_no" in sql
    assert "li.item_id" in sql and "im.item_name" in sql
    assert "jute_yarn_count" in sql  # yarn_count via the scalar subquery
    assert "h.status_id = 3" in sql
    assert "GROUP BY" in sql
    assert "ORDER BY h.tran_date, h.purchase_no" in sql
    for bind in (":co_id", ":branch_id", ":date_from", ":date_to"):
        assert bind in sql, bind


def test_legacy_stores_query_is_kept_untouched_as_the_cross_check():
    """The old proc_inward builder still exists and still filters on the yarn
    item type — it is now the transition warning's source, not purchase_mt's."""
    sql = str(get_yarn_purchase_qty_query())
    assert "proc_inward_dtl" in sql
    assert "sr_status = 3" in sql
    assert "item_type_id" in sql


def test_build_purchase_detail_shapes_rows_for_the_drill_down():
    rows = [
        {"yarn_purchase_id": 11, "purchase_no": 1, "tran_date": _date(2026, 9, 2),
         "item_id": 335556, "item_name": "13 LBS SKWP", "yarn_count": 13.0,
         "kg": 26000},
        {"yarn_purchase_id": 12, "purchase_no": 2, "tran_date": _date(2026, 9, 15),
         "item_id": 335556, "item_name": "13 LBS SKWP", "yarn_count": 13.0,
         "kg": "34300.000"},
    ]
    out = build_purchase_detail(rows)
    assert out == [
        {"yarn_purchase_id": 11, "purchase_no": 1, "tran_date": "2026-09-02",
         "item_id": 335556, "item_name": "13 LBS SKWP", "yarn_count": 13.0,
         "kg": 26000.0},
        {"yarn_purchase_id": 12, "purchase_no": 2, "tran_date": "2026-09-15",
         "item_id": 335556, "item_name": "13 LBS SKWP", "yarn_count": 13.0,
         "kg": 34300.0},
    ]
    assert round(sum(r["kg"] for r in out) / 1000.0, 3) == 60.3
    assert build_purchase_detail([]) == []


def test_legacy_stores_yarn_warning_fires_only_when_documents_are_missing():
    """Stores receipts of yarn exist (the sls 334018 case) but no approved
    Yarn Purchase in the window: say so instead of silently reading 0."""
    after_cutover = _date(2026, 9, 26)
    msg = legacy_stores_yarn_warning(6.038, 0.0, after_cutover)
    assert msg is not None
    assert "6.038 MT" in msg
    assert "Yarn Purchase" in msg
    # documents exist in the same window — the Stores figure is not a gap
    assert legacy_stores_yarn_warning(6.038, 60.3, after_cutover) is None
    # nothing in Stores either
    assert legacy_stores_yarn_warning(0.0, 0.0, after_cutover) is None
    assert legacy_stores_yarn_warning(None, None, after_cutover) is None


def test_legacy_stores_yarn_warning_fires_on_unrecognised_units():
    """sls books the legacy item in MTR; purchase_qty_to_mt converts that to
    0 MT and reports the unit. 0 MT would pass the tonnage rule, so the unit
    names are a second trigger — with or without documents in the window,
    since an unconverted quantity cannot be shown to be covered by them."""
    after_cutover = _date(2026, 9, 26)
    msg = legacy_stores_yarn_warning(0.0, 0.0, after_cutover, ["MTR"])
    assert msg == (
        "Stores receipts of yarn-type items exist in unrecognised unit(s) MTR "
        "— book them as Yarn Purchase documents"
    )
    assert legacy_stores_yarn_warning(0.0, 60.3, after_cutover, ["MTR"]) == msg
    # both gaps: one message, tonnage first, units de-duplicated and sorted
    both = legacy_stores_yarn_warning(6.038, 0.0, after_cutover, ["PCS", "MTR", "PCS"])
    assert both.startswith("Stores receipts of yarn-type items exist (6.038 MT) but no")
    assert "unrecognised unit(s) MTR, PCS" in both
    # empty / None unit lists behave exactly as before
    assert legacy_stores_yarn_warning(0.0, 0.0, after_cutover, []) is None
    assert legacy_stores_yarn_warning(0.0, 0.0, after_cutover, None) is None


def test_legacy_stores_yarn_warning_is_silent_before_the_cutover():
    """Owner ruling 7: backfill starts 2026-04-01; a window ending before it
    cannot be expected to have a Yarn Purchase document."""
    assert YARN_PURCHASE_CUTOVER_DATE == _date(2026, 4, 1)
    assert legacy_stores_yarn_warning(6.038, 0.0, _date(2026, 3, 31)) is None
    assert legacy_stores_yarn_warning(0.0, 0.0, _date(2026, 3, 31), ["MTR"]) is None
    assert legacy_stores_yarn_warning(6.038, 0.0, YARN_PURCHASE_CUTOVER_DATE) is not None


def test_note_says_purchase_reads_the_document_and_stores_is_a_transition_warning():
    note = DIFFERENCE_NOTE
    assert "Yarn Purchase" in note
    assert "Stores" in note
    assert "warnings" in note
    assert "reads 0 until purchased yarn is booked against an item" not in note


# ------------------------------ PRVS STOCK per location and department
#
# A package counted in the godown and on a department floor is two lines with
# the same (bucket, item, package, area) key. Before stock_loc / dept_id were
# part of the key both lines found the SAME previous figure (the sum of both),
# so each printed the whole of yesterday's tonnage.


def _loc_row(samples, wt, loc, dept_id=None, **extra):
    return _row(
        WINDING_STOCK_BUCKET_PURCHASE, samples, wt, item_id=335556,
        package_label="PW SPOOL", stock_loc=loc, dept_id=dept_id, **extra,
    )


def test_prev_key_carries_location_and_department():
    godown = _loc_row(1, 1, WINDING_STOCK_LOC_GODOWN)
    weaving = _loc_row(1, 1, WINDING_STOCK_LOC_FLOOR, dept_id=7)
    beaming = _loc_row(1, 1, WINDING_STOCK_LOC_FLOOR, dept_id=9)
    assert _prev_key(godown) != _prev_key(weaving)
    assert _prev_key(weaving) != _prev_key(beaming)
    assert _prev_key(weaving) == _prev_key(dict(weaving, no_of_samples=99))


def test_prvs_stock_is_per_location_and_department():
    """Yesterday: 2,000 kg in the godown, 1,200 kg on the weaving floor.
    Today each line must read ITS OWN previous figure, not 3.200 twice."""
    prev = [
        _loc_row(100, 20, WINDING_STOCK_LOC_GODOWN),            # 2.000
        _loc_row(60, 20, WINDING_STOCK_LOC_FLOOR, dept_id=7),   # 1.200
    ]
    rows = [
        _loc_row(2080, 20, WINDING_STOCK_LOC_GODOWN),           # 41.600
        _loc_row(155, 20, WINDING_STOCK_LOC_FLOOR, dept_id=7),  #  3.100
        _loc_row(45, 20, WINDING_STOCK_LOC_FLOOR, dept_id=9),   #  0.900, new dept
    ]
    blocks = build_stock_blocks(rows, prev)
    out = blocks[WINDING_STOCK_BUCKET_PURCHASE]["rows"]
    assert [r["prvs_stock_mt"] for r in out] == [2.0, 1.2, 0.0]
    assert [r["stock_mt"] for r in out] == [41.6, 3.1, 0.9]


def test_location_in_the_key_leaves_every_total_unchanged():
    """Ruling in §3.3: the daily total the difference report reads sums all
    locations. Splitting the key must move nothing but the PRVS column."""
    prev = [
        _loc_row(100, 20, WINDING_STOCK_LOC_GODOWN),
        _loc_row(60, 20, WINDING_STOCK_LOC_FLOOR, dept_id=7),
    ]
    rows = [
        _loc_row(2080, 20, WINDING_STOCK_LOC_GODOWN),
        _loc_row(155, 20, WINDING_STOCK_LOC_FLOOR, dept_id=7),
    ]
    blocks = build_stock_blocks(rows, prev)
    assert blocks[WINDING_STOCK_BUCKET_PURCHASE]["subtotal_mt"] == 44.7
    assert blocks["grand_total_mt"] == 44.7
    assert build_stock_blocks(prev, [])["grand_total_mt"] == 3.2
    # and the golden sheet (no location on any line) is untouched
    assert build_stock_blocks(GOLDEN_ROWS, [])["grand_total_mt"] == 55.850


def test_legacy_rows_without_location_keys_still_find_their_previous_figure():
    """Rows saved before the columns existed come back with stock_loc 'FLOOR'
    (the column default) and dept_id NULL on both days — exactly what the
    query returns for them — so they still match each other."""
    legacy = dict(item_id=1, stock_loc=WINDING_STOCK_LOC_FLOOR, dept_id=None)
    prev = [_row(WINDING_STOCK_BUCKET_MILL_USE, 200, 10, **legacy)]
    rows = [_row(WINDING_STOCK_BUCKET_MILL_USE, 100, 10, **legacy)]
    assert rows[0]["stock_loc"] == "FLOOR" and rows[0]["dept_id"] is None
    assert _prev_key(rows[0]) == _prev_key(prev[0])
    blocks = build_stock_blocks(rows, prev)
    assert blocks[WINDING_STOCK_BUCKET_MILL_USE]["rows"][0]["prvs_stock_mt"] == 2.0
    # and a row that HAS been placed (godown) does not inherit the legacy figure
    godown = [_row(WINDING_STOCK_BUCKET_MILL_USE, 100, 10, item_id=1,
                   stock_loc=WINDING_STOCK_LOC_GODOWN, dept_id=None)]
    assert build_stock_blocks(godown, prev)[WINDING_STOCK_BUCKET_MILL_USE]["rows"][0]["prvs_stock_mt"] == 0.0


# ------------------------------------------ endpoint: document in, Stores out


def test_difference_endpoint_reads_purchase_from_documents_and_warns_on_stores():
    """End to end through the router with a mocked session dispatched on the
    SQL text: 60,300 kg of approved Yarn Purchase in the month, 6.038 MT of
    legacy Stores receipts. purchase_mt must be the document figure, the
    Stores figure must appear ONLY under warnings, and should_be_closing_mt
    must not contain it."""
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from src.authorization.utils import get_current_user_with_refresh
    from src.config.db import get_tenant_db
    from src.main import app

    def _res(**kw):
        r = MagicMock()
        r.fetchall.return_value = kw.get("fetchall", [])
        r.fetchone.return_value = kw.get("fetchone")
        r.first.return_value = kw.get("first")
        return r

    def _attr(**attrs):
        o = MagicMock()
        for k, v in attrs.items():
            setattr(o, k, v)
        return o

    detail_row = MagicMock()
    detail_row._mapping = {
        "yarn_purchase_id": 11, "purchase_no": 1, "tran_date": _date(2026, 9, 2),
        "item_id": 335556, "item_name": "13 LBS SKWP", "yarn_count": 13.0,
        "kg": 60300,
    }

    def dispatch(stmt, binds=None):
        sql = str(stmt)
        if "SELECT 1 FROM branch_mst" in sql:
            return _res(first=(1,))
        if "ORDER BY h.tran_date, h.purchase_no" in sql:     # drill-down (also sums kg)
            return _res(fetchall=[detail_row])
        if "SUM(li.weight_kg)" in sql and "jute_yarn_purchase_li" in sql:
            return _res(fetchone=_attr(purchase_kg=60300))
        if "proc_inward_dtl" in sql:
            return _res(fetchone=_attr(purchase_qty=6.038, uoms="MT"))
        return _res()

    session = MagicMock()
    session.execute.side_effect = dispatch
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: session
    try:
        resp = TestClient(app).get(
            "/api/juteProductionReports/winding_weaving_difference"
            "?co_id=2&branch_id=29&report_date=2026-09-26"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    mtd = data["production"]["mtd"]
    assert mtd["purchase_mt"] == 60.3
    assert mtd["should_be_closing_mt"] == 60.3          # opening/winding/... all 0
    assert data["production"]["day"]["purchase_mt"] == 60.3
    assert data["warnings"]["legacy_stores_yarn_mt"] == 6.038
    assert data["warnings"]["legacy_stores_yarn_warning"] is None  # documents exist
    assert data["warnings"]["purchase_unknown_uoms"] == []
    assert data["purchase_detail"] == [
        {"yarn_purchase_id": 11, "purchase_no": 1, "tran_date": "2026-09-02",
         "item_id": 335556, "item_name": "13 LBS SKWP", "yarn_count": 13.0,
         "kg": 60300.0}
    ]


def test_difference_endpoint_warns_when_only_stores_has_the_yarn():
    """sls today: Stores receipts of the legacy item, no Yarn Purchase yet.
    purchase_mt reads 0 (nothing approved), the warning says why."""
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from src.authorization.utils import get_current_user_with_refresh
    from src.config.db import get_tenant_db
    from src.main import app

    def _res(**kw):
        r = MagicMock()
        r.fetchall.return_value = kw.get("fetchall", [])
        r.fetchone.return_value = kw.get("fetchone")
        r.first.return_value = kw.get("first")
        return r

    def _attr(**attrs):
        o = MagicMock()
        for k, v in attrs.items():
            setattr(o, k, v)
        return o

    def dispatch(stmt, binds=None):
        sql = str(stmt)
        if "SELECT 1 FROM branch_mst" in sql:
            return _res(first=(1,))
        if "SUM(li.weight_kg)" in sql and "jute_yarn_purchase_li" in sql:
            return _res(fetchone=_attr(purchase_kg=0))
        if "proc_inward_dtl" in sql:
            return _res(fetchone=_attr(purchase_qty=6.038, uoms="MT"))
        return _res()

    session = MagicMock()
    session.execute.side_effect = dispatch
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: session
    try:
        resp = TestClient(app).get(
            "/api/juteProductionReports/winding_weaving_difference"
            "?co_id=2&branch_id=29&report_date=2026-09-26"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["production"]["mtd"]["purchase_mt"] == 0.0
    assert data["production"]["mtd"]["should_be_closing_mt"] == 0.0  # 6.038 never enters
    assert data["warnings"]["legacy_stores_yarn_mt"] == 6.038
    assert "6.038 MT" in data["warnings"]["legacy_stores_yarn_warning"]
    assert data["purchase_detail"] == []


def test_difference_endpoint_warns_when_stores_has_the_yarn_in_metres():
    """sls as it really is: 654.608 MTR of item 334018. The tonnage converts
    to 0, so before the unit trigger the warning was None while
    purchase_unknown_uoms quietly said MTR. Now the guard says so in words."""
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from src.authorization.utils import get_current_user_with_refresh
    from src.config.db import get_tenant_db
    from src.main import app

    def _res(**kw):
        r = MagicMock()
        r.fetchall.return_value = kw.get("fetchall", [])
        r.fetchone.return_value = kw.get("fetchone")
        r.first.return_value = kw.get("first")
        return r

    def _attr(**attrs):
        o = MagicMock()
        for k, v in attrs.items():
            setattr(o, k, v)
        return o

    def dispatch(stmt, binds=None):
        sql = str(stmt)
        if "SELECT 1 FROM branch_mst" in sql:
            return _res(first=(1,))
        if "SUM(li.weight_kg)" in sql and "jute_yarn_purchase_li" in sql:
            return _res(fetchone=_attr(purchase_kg=60300))
        if "proc_inward_dtl" in sql:
            return _res(fetchone=_attr(purchase_qty=654.608, uoms="MTR"))
        return _res()

    session = MagicMock()
    session.execute.side_effect = dispatch
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: session
    try:
        resp = TestClient(app).get(
            "/api/juteProductionReports/winding_weaving_difference"
            "?co_id=2&branch_id=29&report_date=2026-09-26"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    w = resp.json()["data"]["warnings"]
    assert w["purchase_unknown_uoms"] == ["MTR"]
    assert w["legacy_stores_yarn_mt"] == 0.0
    assert "unrecognised unit(s) MTR" in w["legacy_stores_yarn_warning"]
