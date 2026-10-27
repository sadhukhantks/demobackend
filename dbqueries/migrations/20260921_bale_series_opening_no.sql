-- =============================================================================
-- Migration: jute_prod_bale_series.opening_bale_no + drop the state/party FKs
-- =============================================================================
-- Date: 2026-09-21
-- Applies to: dev3 (QA) and sls. Run with tools/run_sql_migration.py (pymysql).
--
-- Why: bale history from the old register is NOT being migrated yet, so each series
-- is started in the Bale Series Master at the last number already pressed (e.g.
-- PUNJAB = 17794). opening_bale_no remembers that number. When the TOP bale of a
-- series is deleted (or renumbered) the counter winds back to
--   GREATEST(opening_bale_no, MAX(active bale_no <= the removed number))
-- so a mistyped or deleted bale never leaves the counter stuck ahead, and never
-- drops below the operator's starting number while the old bales are missing.
-- The master sets opening_bale_no whenever it writes a new last_bale_no.
--
-- state_id / party_id stay as nullable columns (left blank for now); only their
-- foreign keys go. sls had them dropped by hand on 2026-09-21, so each DROP is
-- guarded by an information_schema check and is a no-op where already gone.
;

ALTER TABLE jute_prod_bale_series
    ADD COLUMN opening_bale_no INT NOT NULL DEFAULT 0 AFTER last_bale_no;

-- Backfill: a series with no active bales yet started at its current counter.
;
UPDATE jute_prod_bale_series bs
SET bs.opening_bale_no = bs.last_bale_no
WHERE NOT EXISTS (
    SELECT 1 FROM jute_prod_bale_entry be
    WHERE be.bale_series_id = bs.bale_series_id AND be.active = 1
);

-- Drop fk_bs_state where it still exists
;
SET @s = (SELECT IF(COUNT(*) > 0, 'ALTER TABLE jute_prod_bale_series DROP FOREIGN KEY fk_bs_state', 'DO 0')
          FROM information_schema.REFERENTIAL_CONSTRAINTS
          WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_bale_series'
            AND CONSTRAINT_NAME = 'fk_bs_state');
PREPARE st FROM @s;
EXECUTE st;
DEALLOCATE PREPARE st;

-- Drop fk_bs_party where it still exists
;
SET @s = (SELECT IF(COUNT(*) > 0, 'ALTER TABLE jute_prod_bale_series DROP FOREIGN KEY fk_bs_party', 'DO 0')
          FROM information_schema.REFERENTIAL_CONSTRAINTS
          WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_bale_series'
            AND CONSTRAINT_NAME = 'fk_bs_party');
PREPARE st FROM @s;
EXECUTE st;
DEALLOCATE PREPARE st;

-- VERIFICATION
;
SELECT bale_series_id, series_name, last_bale_no, opening_bale_no FROM jute_prod_bale_series;

-- =============================================================================
-- ROLLBACK:
-- ALTER TABLE jute_prod_bale_series DROP COLUMN opening_bale_no;
-- ALTER TABLE jute_prod_bale_series
--     ADD CONSTRAINT fk_bs_state FOREIGN KEY (state_id) REFERENCES state_mst (state_id),
--     ADD CONSTRAINT fk_bs_party FOREIGN KEY (party_id) REFERENCES party_mst (party_id);
-- =============================================================================
;
