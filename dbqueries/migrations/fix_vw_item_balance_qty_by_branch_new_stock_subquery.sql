-- Migration: fix_vw_item_balance_qty_by_branch_new_stock_subquery.sql
-- Date: 2026-08-11
-- Tenant scope: ALL tenants. The broken definition is byte-identical (md5
--   3b88eebbbcb93e319cecd41fb26919f8) on sls, dev3 and amcl - verified via
--   SHOW CREATE VIEW. This is shipped everywhere, not tenant-specific.
--
-- PROBLEM
--   The cur_stock subquery of vw_item_balance_qty_by_branch_new is wrong in
--   four ways, and cur_stock feeds the max_indent_qty / min_indent_qty /
--   max_po_qty / min_po_qty ceilings the Create PO + Create Indent pages use:
--     1. Join key inverted: issue_hdr ih LEFT JOIN issue_li il
--        ON ih.issue_id = il.issue_li_id  -- header id vs LINE PK.
--     2. Issues aggregated by item_id only - no branch scoping, so every
--        branch is charged with every other branch's issues.
--     3. That per-item total is then joined on item_id onto a derived table
--        grouped per inward_dtl_id => the issue qty fans out, multiplied by
--        the number of inward lines for the item.
--     4. No guards: cancelled issues (status_id = 6) and inactive issue
--        headers are counted, and inward rows are taken at inward_qty
--        regardless of sr_status.
--   Measured on sls: 2,564 of 13,626 item/branch pairs go negative; worst
--   row is -18,605,936 where true stock is 0.126.
--
-- FIX (derived, not invented)
--   The correct per-inward_dtl_id stock logic already exists in this schema as
--   vw_approved_inward_qty (approved_qty - issues, correct join key
--   ih.issue_id = il.issue_id, guards on ih.active and ih.status_id <> 6,
--   filtered to sr_status = 3). vw_item_balance_qty_by_branch is nothing but
--   SUM(balance_qty) GROUP BY branch_id, item_id over it, and that view was
--   confirmed to return the correct 0.5 for item 280208 / branch 29.
--   So the whole broken stk subquery is replaced by a plain branch/item
--   rollup of vw_approved_inward_qty.balance_qty. Branch scoping and the
--   fan-out both disappear for free: balance_qty is already per
--   inward_dtl_id, which belongs to exactly one branch via proc_inward.
--
-- WHY DR/CR IS NOT SUBTRACTED  (do not "fix" this by re-adding it)
--   The broken view subtracted SUM(drcr_note_dtl.quantity) from stock. That
--   is wrong twice over, and an earlier draft of this migration wrongly kept
--   it. Both reasons were verified in code and data:
--
--   1. Rejections are ALREADY netted out. approved_qty is written only by
--      material inspection, as  approved_qty = inward_qty - rejected_qty
--      (src/procurement/material_inspection.py:249, the sole write path).
--      vw_approved_inward_qty computes balance_qty from approved_qty, so
--      goods rejected at inspection never entered stock to begin with.
--      src/procurement/drcr_note.py contains ZERO writes to proc_inward_dtl
--      (every reference there is a SELECT), and the 5 related triggers are
--      audit-log inserts only. Nothing about a DR/CR note moves stock.
--
--   2. Most DR/CR rows are not goods at all. 275 of 278 quantity-bearing
--      drcr_note_dtl rows on sls are debitnote_type = 2 (Rate Difference),
--      where sr.py sets quantity = approved_qty and rate = rate_diff purely
--      to compute a money amount. No goods move. Subtracting them erased
--      11,252 units that never left the warehouse.
--
--   Net effect: cur_stock is now exactly vw_item_balance_qty_by_branch's
--   total_balance_qty, and this view is that value plus the minmax columns
--   and the derived ceilings.
--
-- PROOF (read-only on sls before applying)
--   - This stk rollup vs vw_item_balance_qty_by_branch:
--       13,576 pairs compared, 0 disagreements.
--   - item 280208 / branch 29 => 0.5 (was -4917.0), and the max_po_qty
--       ceiling it feeds => 0 (was 4410.0).
--   - Negative pairs on sls: 2,564 before, 0 after (98 with the drcr term
--       that this version drops, 87 of which were drcr-induced).
--
-- NOT CHANGED
--   Every output column expression, the ind and po subqueries, the -1 / -2
--   sentinels and the min_order_qty rounding are preserved verbatim. Only the
--   stk subquery is replaced.
--
-- APPLIED
--   dev3  2026-08-11.
--   sls / amcl NOT applied - awaiting user confirmation. The broken
--   definition is byte-identical there, so this same script serves both.

CREATE OR REPLACE
ALGORITHM = UNDEFINED
SQL SECURITY DEFINER
VIEW `vw_item_balance_qty_by_branch_new` AS
SELECT
    `imm`.`branch_id`                                            AS `branch_id`,
    `imm`.`item_id`                                              AS `item_id`,
    coalesce(`ind`.`bal_tot_ind_qty`, 0)                         AS `bal_tot_ind_qty`,
    coalesce(`ind`.`open_bal_ind_tot_qty`, 0)                    AS `open_bal_Ind_tot_qty`,
    coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`, 0)    AS `capital_maintance_bal_Ind_tot_qty`,
    ((coalesce(`ind`.`bal_tot_ind_qty`, 0) - coalesce(`ind`.`open_bal_ind_tot_qty`, 0))
        - coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`, 0))
                                                                 AS `bal_qty_ind_to_validate`,
    coalesce(`po`.`bal_tot_po_qty`, 0)                           AS `bal_tot_po_qty`,
    coalesce(`po`.`open_bal_tot_po_qty`, 0)                      AS `open_bal_tot_po_qty`,
    coalesce(`po`.`capital_maintainance_bal_tot_po_qty`, 0)      AS `capital_maintainance_bal_tot_po_qty`,
    ((coalesce(`po`.`bal_tot_po_qty`, 0) - coalesce(`po`.`open_bal_tot_po_qty`, 0))
        - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`, 0))
                                                                 AS `bal_qty_po_to_validate`,
    ifnull(`imm`.`maxqty`, 0)                                    AS `maxqty`,
    ifnull(`imm`.`minqty`, 0)                                    AS `minqty`,
    ifnull(`imm`.`min_order_qty`, 0)                             AS `min_order_qty`,
    ifnull(`stk`.`cur_stock`, 0)                                 AS `cur_stock`,

    (CASE
        WHEN (coalesce(`ind`.`open_bal_ind_tot_qty`, 0) > 0) THEN -(2)
        WHEN (coalesce(ifnull(`imm`.`maxqty`, 0), 0) = 0) THEN -(1)
        WHEN (coalesce(ifnull(`imm`.`min_order_qty`, 0), 0) = 0)
            THEN greatest((((coalesce(ifnull(`imm`.`maxqty`, 0), 0) - coalesce(ifnull(`stk`.`cur_stock`, 0), 0))
                - ((coalesce(`ind`.`bal_tot_ind_qty`, 0) - coalesce(`ind`.`open_bal_ind_tot_qty`, 0))
                    - coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`, 0)))
                - ((coalesce(`po`.`bal_tot_po_qty`, 0) - coalesce(`po`.`open_bal_tot_po_qty`, 0))
                    - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`, 0))), 0)
        ELSE greatest((ceiling((greatest((((coalesce(ifnull(`imm`.`maxqty`, 0), 0) - coalesce(ifnull(`stk`.`cur_stock`, 0), 0))
                - ((coalesce(`ind`.`bal_tot_ind_qty`, 0) - coalesce(`ind`.`open_bal_ind_tot_qty`, 0))
                    - coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`, 0)))
                - ((coalesce(`po`.`bal_tot_po_qty`, 0) - coalesce(`po`.`open_bal_tot_po_qty`, 0))
                    - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`, 0))), 0)
                / coalesce(ifnull(`imm`.`min_order_qty`, 0), 1))) * coalesce(ifnull(`imm`.`min_order_qty`, 0), 1)), 0)
    END)                                                         AS `max_indent_qty`,

    (CASE
        WHEN (coalesce(`ind`.`open_bal_ind_tot_qty`, 0) > 0) THEN -(2)
        WHEN (coalesce(ifnull(`imm`.`maxqty`, 0), 0) = 0) THEN -(1)
        WHEN (coalesce(ifnull(`imm`.`min_order_qty`, 0), 0) = 0)
            THEN coalesce(ifnull(`imm`.`minqty`, 0), 0)
        ELSE least((ceiling((greatest(coalesce(ifnull(`imm`.`minqty`, 0), 0), 0)
                / coalesce(ifnull(`imm`.`min_order_qty`, 0), 1))) * coalesce(ifnull(`imm`.`min_order_qty`, 0), 1)),
            greatest((ceiling((greatest((((coalesce(ifnull(`imm`.`maxqty`, 0), 0) - coalesce(ifnull(`stk`.`cur_stock`, 0), 0))
                - ((coalesce(`ind`.`bal_tot_ind_qty`, 0) - coalesce(`ind`.`open_bal_ind_tot_qty`, 0))
                    - coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`, 0)))
                - ((coalesce(`po`.`bal_tot_po_qty`, 0) - coalesce(`po`.`open_bal_tot_po_qty`, 0))
                    - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`, 0))), 0)
                / coalesce(ifnull(`imm`.`min_order_qty`, 0), 1))) * coalesce(ifnull(`imm`.`min_order_qty`, 0), 1)), 0))
    END)                                                         AS `min_indent_qty`,

    (CASE
        WHEN (coalesce(`po`.`open_bal_tot_po_qty`, 0) > 0) THEN -(2)
        WHEN (coalesce(ifnull(`imm`.`maxqty`, 0), 0) = 0) THEN -(1)
        WHEN (coalesce(ifnull(`imm`.`min_order_qty`, 0), 0) = 0)
            THEN greatest(((coalesce(ifnull(`imm`.`maxqty`, 0), 0) - coalesce(ifnull(`stk`.`cur_stock`, 0), 0))
                - ((coalesce(`po`.`bal_tot_po_qty`, 0) - coalesce(`po`.`open_bal_tot_po_qty`, 0))
                    - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`, 0))), 0)
        ELSE greatest((ceiling((greatest(((coalesce(ifnull(`imm`.`maxqty`, 0), 0) - coalesce(ifnull(`stk`.`cur_stock`, 0), 0))
                - ((coalesce(`po`.`bal_tot_po_qty`, 0) - coalesce(`po`.`open_bal_tot_po_qty`, 0))
                    - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`, 0))), 0)
                / coalesce(ifnull(`imm`.`min_order_qty`, 0), 1))) * coalesce(ifnull(`imm`.`min_order_qty`, 0), 1)), 0)
    END)                                                         AS `max_po_qty`,

    (CASE
        WHEN (coalesce(`po`.`open_bal_tot_po_qty`, 0) > 0) THEN -(2)
        WHEN (coalesce(ifnull(`imm`.`maxqty`, 0), 0) = 0) THEN -(1)
        WHEN (coalesce(ifnull(`imm`.`min_order_qty`, 0), 0) = 0)
            THEN coalesce(ifnull(`imm`.`minqty`, 0), 0)
        ELSE least((ceiling((greatest(coalesce(ifnull(`imm`.`minqty`, 0), 0), 0)
                / coalesce(ifnull(`imm`.`min_order_qty`, 0), 1))) * coalesce(ifnull(`imm`.`min_order_qty`, 0), 1)),
            greatest((ceiling((greatest(((coalesce(ifnull(`imm`.`maxqty`, 0), 0) - coalesce(ifnull(`stk`.`cur_stock`, 0), 0))
                - ((coalesce(`po`.`bal_tot_po_qty`, 0) - coalesce(`po`.`open_bal_tot_po_qty`, 0))
                    - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`, 0))), 0)
                / coalesce(ifnull(`imm`.`min_order_qty`, 0), 1))) * coalesce(ifnull(`imm`.`min_order_qty`, 0), 1)), 0))
    END)                                                         AS `min_po_qty`

FROM `item_minmax_mst` `imm`

LEFT JOIN (
    SELECT
        `vpion`.`branch_id`                                                  AS `branch_id`,
        `vpion`.`item_id`                                                    AS `item_id`,
        sum(`vpion`.`bal_ind_qty`)                                           AS `bal_tot_ind_qty`,
        sum((CASE WHEN (`vpion`.`indent_type_id` = 'Open') THEN `vpion`.`bal_ind_qty` ELSE 0 END))
                                                                             AS `open_bal_ind_tot_qty`,
        sum((CASE WHEN (`vpion`.`expense_type_id` IN (5, 7)) THEN `vpion`.`bal_ind_qty` ELSE 0 END))
                                                                             AS `capital_maintainance_bal_ind_tot_qty`
    FROM `vw_proc_indent_outstanding_new` `vpion`
    GROUP BY `vpion`.`branch_id`, `vpion`.`item_id`
) `ind`
    ON ((`ind`.`branch_id` = `imm`.`branch_id`) AND (`ind`.`item_id` = `imm`.`item_id`))

LEFT JOIN (
    SELECT
        `vppon`.`branch_id`                                                  AS `branch_id`,
        `vppon`.`item_id`                                                    AS `item_id`,
        sum(`vppon`.`bal_po_qty`)                                            AS `bal_tot_po_qty`,
        sum((CASE WHEN (`vppon`.`po_type` = 'Open') THEN `vppon`.`bal_po_qty` ELSE 0 END))
                                                                             AS `open_bal_tot_po_qty`,
        sum((CASE WHEN (`vppon`.`expense_type_id` IN (5, 7)) THEN `vppon`.`bal_po_qty` ELSE 0 END))
                                                                             AS `capital_maintainance_bal_tot_po_qty`
    FROM `vw_proc_po_outstanding_new` `vppon`
    GROUP BY `vppon`.`branch_id`, `vppon`.`item_id`
) `po`
    ON ((`po`.`branch_id` = `imm`.`branch_id`) AND (`po`.`item_id` = `imm`.`item_id`))

-- ===== REPLACED SUBQUERY: correct join key, branch-scoped, no fan-out =====
-- vw_approved_inward_qty already nets issues off approved_qty per
-- inward_dtl_id using ih.issue_id = il.issue_id, guards ih.active and
-- ih.status_id <> 6, and filters sr_status = 3. Reused verbatim rather than
-- re-deriving the stock logic a second time.
--
-- DO NOT ADD A DR/CR SUBTRACTION HERE. See "WHY DR/CR IS NOT SUBTRACTED" above.
LEFT JOIN (
    SELECT
        `a`.`branch_id`             AS `branch_id`,
        `a`.`item_id`               AS `item_id`,
        sum(`a`.`balance_qty`)      AS `cur_stock`
    FROM `vw_approved_inward_qty` `a`
    GROUP BY `a`.`branch_id`, `a`.`item_id`
) `stk`
    ON ((`stk`.`branch_id` = `imm`.`branch_id`) AND (`stk`.`item_id` = `imm`.`item_id`));


-- =========================================================================
-- ROLLBACK - current (broken) definition, captured verbatim from
-- SHOW CREATE VIEW on sls 2026-08-11. Identical on dev3 and amcl
-- (md5 3b88eebbbcb93e319cecd41fb26919f8). Uncomment and run to revert.
-- =========================================================================
--   CREATE ALGORITHM=UNDEFINED DEFINER=`Tarun`@`%` SQL SECURITY DEFINER VIEW `vw_item_balance_qty_by_branch_new` AS select `imm`.`branch_id` AS `branch_id`,`imm`.`item_id` AS `item_id`,coalesce(`ind`.`bal_tot_ind_qty`,0) AS `bal_tot_ind_qty`,coalesce(`ind`.`open_bal_ind_tot_qty`,0) AS `open_bal_Ind_tot_qty`,coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`,0) AS `capital_maintance_bal_Ind_tot_qty`,((coalesce(`ind`.`bal_tot_ind_qty`,0) - coalesce(`ind`.`open_bal_ind_tot_qty`,0)) - coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`,0)) AS `bal_qty_ind_to_validate`,coalesce(`po`.`bal_tot_po_qty`,0) AS `bal_tot_po_qty`,coalesce(`po`.`open_bal_tot_po_qty`,0) AS `open_bal_tot_po_qty`,coalesce(`po`.`capital_maintainance_bal_tot_po_qty`,0) AS `capital_maintainance_bal_tot_po_qty`,((coalesce(`po`.`bal_tot_po_qty`,0) - coalesce(`po`.`open_bal_tot_po_qty`,0)) - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`,0)) AS `bal_qty_po_to_validate`,ifnull(`imm`.`maxqty`,0) AS `maxqty`,ifnull(`imm`.`minqty`,0) AS `minqty`,ifnull(`imm`.`min_order_qty`,0) AS `min_order_qty`,ifnull(`stk`.`cur_stock`,0) AS `cur_stock`,(case when (coalesce(`ind`.`open_bal_ind_tot_qty`,0) > 0) then -(2) when (coalesce(ifnull(`imm`.`maxqty`,0),0) = 0) then -(1) when (coalesce(ifnull(`imm`.`min_order_qty`,0),0) = 0) then greatest((((coalesce(ifnull(`imm`.`maxqty`,0),0) - coalesce(ifnull(`stk`.`cur_stock`,0),0)) - ((coalesce(`ind`.`bal_tot_ind_qty`,0) - coalesce(`ind`.`open_bal_ind_tot_qty`,0)) - coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`,0))) - ((coalesce(`po`.`bal_tot_po_qty`,0) - coalesce(`po`.`open_bal_tot_po_qty`,0)) - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`,0))),0) else greatest((ceiling((greatest((((coalesce(ifnull(`imm`.`maxqty`,0),0) - coalesce(ifnull(`stk`.`cur_stock`,0),0)) - ((coalesce(`ind`.`bal_tot_ind_qty`,0) - coalesce(`ind`.`open_bal_ind_tot_qty`,0)) - coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`,0))) - ((coalesce(`po`.`bal_tot_po_qty`,0) - coalesce(`po`.`open_bal_tot_po_qty`,0)) - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`,0))),0) / coalesce(ifnull(`imm`.`min_order_qty`,0),1))) * coalesce(ifnull(`imm`.`min_order_qty`,0),1)),0) end) AS `max_indent_qty`,(case when (coalesce(`ind`.`open_bal_ind_tot_qty`,0) > 0) then -(2) when (coalesce(ifnull(`imm`.`maxqty`,0),0) = 0) then -(1) when (coalesce(ifnull(`imm`.`min_order_qty`,0),0) = 0) then coalesce(ifnull(`imm`.`minqty`,0),0) else least((ceiling((greatest(coalesce(ifnull(`imm`.`minqty`,0),0),0) / coalesce(ifnull(`imm`.`min_order_qty`,0),1))) * coalesce(ifnull(`imm`.`min_order_qty`,0),1)),greatest((ceiling((greatest((((coalesce(ifnull(`imm`.`maxqty`,0),0) - coalesce(ifnull(`stk`.`cur_stock`,0),0)) - ((coalesce(`ind`.`bal_tot_ind_qty`,0) - coalesce(`ind`.`open_bal_ind_tot_qty`,0)) - coalesce(`ind`.`capital_maintainance_bal_ind_tot_qty`,0))) - ((coalesce(`po`.`bal_tot_po_qty`,0) - coalesce(`po`.`open_bal_tot_po_qty`,0)) - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`,0))),0) / coalesce(ifnull(`imm`.`min_order_qty`,0),1))) * coalesce(ifnull(`imm`.`min_order_qty`,0),1)),0)) end) AS `min_indent_qty`,(case when (coalesce(`po`.`open_bal_tot_po_qty`,0) > 0) then -(2) when (coalesce(ifnull(`imm`.`maxqty`,0),0) = 0) then -(1) when (coalesce(ifnull(`imm`.`min_order_qty`,0),0) = 0) then greatest(((coalesce(ifnull(`imm`.`maxqty`,0),0) - coalesce(ifnull(`stk`.`cur_stock`,0),0)) - ((coalesce(`po`.`bal_tot_po_qty`,0) - coalesce(`po`.`open_bal_tot_po_qty`,0)) - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`,0))),0) else greatest((ceiling((greatest(((coalesce(ifnull(`imm`.`maxqty`,0),0) - coalesce(ifnull(`stk`.`cur_stock`,0),0)) - ((coalesce(`po`.`bal_tot_po_qty`,0) - coalesce(`po`.`open_bal_tot_po_qty`,0)) - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`,0))),0) / coalesce(ifnull(`imm`.`min_order_qty`,0),1))) * coalesce(ifnull(`imm`.`min_order_qty`,0),1)),0) end) AS `max_po_qty`,(case when (coalesce(`po`.`open_bal_tot_po_qty`,0) > 0) then -(2) when (coalesce(ifnull(`imm`.`maxqty`,0),0) = 0) then -(1) when (coalesce(ifnull(`imm`.`min_order_qty`,0),0) = 0) then coalesce(ifnull(`imm`.`minqty`,0),0) else least((ceiling((greatest(coalesce(ifnull(`imm`.`minqty`,0),0),0) / coalesce(ifnull(`imm`.`min_order_qty`,0),1))) * coalesce(ifnull(`imm`.`min_order_qty`,0),1)),greatest((ceiling((greatest(((coalesce(ifnull(`imm`.`maxqty`,0),0) - coalesce(ifnull(`stk`.`cur_stock`,0),0)) - ((coalesce(`po`.`bal_tot_po_qty`,0) - coalesce(`po`.`open_bal_tot_po_qty`,0)) - coalesce(`po`.`capital_maintainance_bal_tot_po_qty`,0))),0) / coalesce(ifnull(`imm`.`min_order_qty`,0),1))) * coalesce(ifnull(`imm`.`min_order_qty`,0),1)),0)) end) AS `min_po_qty` from (((`item_minmax_mst` `imm` left join (select `vpion`.`branch_id` AS `branch_id`,`vpion`.`item_id` AS `item_id`,sum(`vpion`.`bal_ind_qty`) AS `bal_tot_ind_qty`,sum((case when (`vpion`.`indent_type_id` = 'Open') then `vpion`.`bal_ind_qty` else 0 end)) AS `open_bal_ind_tot_qty`,sum((case when (`vpion`.`expense_type_id` in (5,7)) then `vpion`.`bal_ind_qty` else 0 end)) AS `capital_maintainance_bal_ind_tot_qty` from `vw_proc_indent_outstanding_new` `vpion` group by `vpion`.`branch_id`,`vpion`.`item_id`) `ind` on(((`ind`.`branch_id` = `imm`.`branch_id`) and (`ind`.`item_id` = `imm`.`item_id`)))) left join (select `vppon`.`branch_id` AS `branch_id`,`vppon`.`item_id` AS `item_id`,sum(`vppon`.`bal_po_qty`) AS `bal_tot_po_qty`,sum((case when (`vppon`.`po_type` = 'Open') then `vppon`.`bal_po_qty` else 0 end)) AS `open_bal_tot_po_qty`,sum((case when (`vppon`.`expense_type_id` in (5,7)) then `vppon`.`bal_po_qty` else 0 end)) AS `capital_maintainance_bal_tot_po_qty` from `vw_proc_po_outstanding_new` `vppon` group by `vppon`.`branch_id`,`vppon`.`item_id`) `po` on(((`po`.`branch_id` = `imm`.`branch_id`) and (`po`.`item_id` = `imm`.`item_id`)))) left join (select `m`.`branch_id` AS `branch_id`,`m`.`item_id` AS `item_id`,ifnull(sum(`m`.`inward_qty`),0) AS `inward_qty`,ifnull(sum(`m`.`drcrqty`),0) AS `drcrqty`,ifnull(sum(`m`.`issqty`),0) AS `issqty`,((ifnull(sum(`m`.`inward_qty`),0) - ifnull(sum(`m`.`drcrqty`),0)) - ifnull(sum(`m`.`issqty`),0)) AS `cur_stock` from (select `k`.`branch_id` AS `branch_id`,`k`.`item_id` AS `item_id`,`k`.`inward_dtl_id` AS `inward_dtl_id`,`k`.`inward_qty` AS `inward_qty`,`g`.`drcrqty` AS `drcrqty`,`i`.`issqty` AS `issqty` from (((select `pi`.`branch_id` AS `branch_id`,`pid`.`item_id` AS `item_id`,`pid`.`inward_dtl_id` AS `inward_dtl_id`,sum(`pid`.`inward_qty`) AS `inward_qty` from (`proc_inward_dtl` `pid` left join `proc_inward` `pi` on((`pid`.`inward_id` = `pi`.`inward_id`))) group by `pi`.`branch_id`,`pid`.`item_id`,`pid`.`inward_dtl_id`) `k` left join (select `dnd`.`inward_dtl_id` AS `inward_dtl_id`,sum(`dnd`.`quantity`) AS `drcrqty` from ((`drcr_note_dtl` `dnd` left join `drcr_note` `dn` on((`dnd`.`debit_credit_note_id` = `dn`.`debit_credit_note_id`))) left join `proc_inward_dtl` `pid` on((`pid`.`inward_dtl_id` = `dnd`.`inward_dtl_id`))) group by `dnd`.`inward_dtl_id`) `g` on((`k`.`inward_dtl_id` = `g`.`inward_dtl_id`))) left join (select `il`.`item_id` AS `item_id`,sum(`il`.`issue_qty`) AS `issqty` from (`issue_hdr` `ih` left join `issue_li` `il` on((`ih`.`issue_id` = `il`.`issue_li_id`))) group by `il`.`item_id`) `i` on((`i`.`item_id` = `k`.`item_id`)))) `m` group by `m`.`item_id`,`m`.`branch_id`) `stk` on(((`stk`.`branch_id` = `imm`.`branch_id`) and (`stk`.`item_id` = `imm`.`item_id`))))
