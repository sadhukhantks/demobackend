-- =============================================================================
-- Migration: tally_link_mst — ERP record -> Tally master name mapping
-- =============================================================================
-- Date: 2026-10-06
-- TARGET: tenant DB (dev3 first; sls before the Tally-export BE deploy).
--
-- Tally imports reject a voucher whose ledger / stock item / godown name does
-- not match its books exactly. ERP names differ ("JAGRATI TRADE SERVICES PVT.
-- LTD." vs Tally "Jagrati Trade Services Pvt. Ltd."), and every company keeps
-- its own Tally books, so the mapping is per co_id. Replaces the legacy
-- EMPMILL12.tbl_tally_link_file (link_for M -> PARTY, Q -> ITEM, G -> GODOWN).
--
--   link_type  PARTY   ref_id = party_mst.party_id
--              ITEM    ref_id = item_mst.item_id
--              GODOWN  ref_id = 0 (company-level default godown)
--
-- Exporters fall back to the ERP name when no active row exists.
--
-- ROLLBACK:
--   DROP TABLE tally_link_mst;
;

CREATE TABLE IF NOT EXISTS tally_link_mst (
    tally_link_id INT NOT NULL AUTO_INCREMENT,
    co_id         INT NOT NULL,
    link_type     VARCHAR(20) NOT NULL,
    ref_id        INT NOT NULL DEFAULT 0,
    tally_name    VARCHAR(255) NOT NULL,
    active        INT NOT NULL DEFAULT 1,
    PRIMARY KEY (tally_link_id),
    UNIQUE KEY uq_tally_link (co_id, link_type, ref_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
