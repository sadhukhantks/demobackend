CREATE TABLE IF NOT EXISTS jute_waste_rate_mst (
    waste_rate_id      INT           NOT NULL AUTO_INCREMENT,
    co_id              INT           NOT NULL,
    branch_id          INT           NULL,
    item_id            INT           NOT NULL,
    rate               DECIMAL(12,2) NOT NULL DEFAULT 0,
    valid_from         DATE          NOT NULL,
    valid_to           DATE          NULL,
    active             TINYINT       NOT NULL DEFAULT 1,
    updated_by         INT           NULL,
    updated_date_time  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (waste_rate_id),
    KEY idx_waste_rate_lookup (co_id, item_id, valid_from),
    KEY idx_waste_rate_branch (branch_id),
    CONSTRAINT fk_waste_rate_item FOREIGN KEY (item_id) REFERENCES item_mst (item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE jute_issue
    ADD COLUMN waste_entry_id BIGINT NULL AFTER jute_mr_li_id,
    ADD INDEX idx_jute_issue_waste (waste_entry_id);

CREATE OR REPLACE VIEW vw_jute_waste_stock_outstanding AS
SELECT
    w.waste_entry_id,
    w.co_id,
    COALESCE(w.branch_id, d.branch_id)                      AS branch_id,
    w.tran_date                                             AS inward_date,
    w.item_id,
    w.waste_qty                                             AS actual_qty,
    w.waste_qty                                             AS actual_weight,
    ROUND(w.waste_qty - IFNULL(iss.issqty, 0), 3)           AS bal_qty,
    ROUND(w.waste_qty - IFNULL(iss.isswt, 0), 3)            AS bal_weight
FROM jute_prod_waste_entry w
INNER JOIN dept_mst d ON d.dept_id = w.dept_id
LEFT JOIN (
    SELECT ji.waste_entry_id,
           SUM(ji.quantity) AS issqty,
           SUM(ji.weight)   AS isswt
    FROM jute_issue ji
    WHERE ji.waste_entry_id IS NOT NULL
      AND (ji.status_id IS NULL OR ji.status_id <> 4)
    GROUP BY ji.waste_entry_id
) iss ON iss.waste_entry_id = w.waste_entry_id
WHERE w.active = 1;

-- ===========================================================================
-- Migration: jute waste as issuable stock                    (2026-08-10)
--
-- NOTE ON COMMENT PLACEMENT: the project migration runner does
-- `sql.split(';')` and skips any chunk whose strip() starts with '--'. A
-- comment placed ABOVE a statement therefore SWALLOWS that statement. All
-- commentary in this file lives after the final ';' on purpose. Do not move
-- it to the top.
--
-- WHAT THIS DOES
-- 1. jute_waste_rate_mst — effective-dated valuation rate (per QUINTAL) for a
--    waste item. branch_id NULL = applies to every branch of the company; a
--    branch-specific row wins over a company-wide one for the same item/date.
--    Waste has no purchase rate of its own (it is a production byproduct), so
--    the issue value of a waste line is priced from this master.
-- 2. jute_issue.waste_entry_id — the waste counterpart of jute_mr_li_id. An
--    issue line carries exactly one of the two; NULL on every pre-existing row,
--    which is why it is nullable and why the MR stock view is unaffected (its
--    issue subquery joins on jute_mr_li_id, and NULL never matches).
-- 3. vw_jute_waste_stock_outstanding — waste stock keyed per waste ENTRY,
--    mirroring vw_jute_stock_outstanding: booked qty minus non-rejected issues.
--    Waste is kg-only, so actual_qty = actual_weight = waste_qty and the issue
--    UI shows unit 'KG'.
--
-- Deliberate deviation from vw_jute_stock_outstanding: the issue subquery here
-- treats a NULL status_id as "counts against stock" (`status_id IS NULL OR
-- status_id <> 4`). The MR view's bare `status_id <> 4` silently drops
-- NULL-status rows from the issued total, over-reporting stock. Waste starts
-- clean; the MR view is left alone as an established, separately-owned object.
--
-- ROLLBACK
-- DROP VIEW IF EXISTS vw_jute_waste_stock_outstanding;
-- ALTER TABLE jute_issue DROP INDEX idx_jute_issue_waste, DROP COLUMN waste_entry_id;
-- DROP TABLE IF EXISTS jute_waste_rate_mst;
-- ===========================================================================
