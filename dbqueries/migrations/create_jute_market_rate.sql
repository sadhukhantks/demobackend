-- =============================================================================
-- Jute Market Rate (Jute Procurement) — 3 tables
--
--   jute_market_rate_source_mst  where a rate was quoted (e.g. JBA, broker X)
--   jute_rate_diff_mst           fixed diff of an item vs a base item, effective-dated
--                                (rate(item) = rate(base_item) + diff_amount; the row in
--                                force on a date is the latest effective_from <= date)
--   jute_market_rate             daily rate per (company, date, source, item)
--
-- Replaces legacy tbl_jute_daily_jute_rate (one rate per quality per day, no source).
-- Quality = raw-jute item (item_grp_mst.item_type_id = 2). Co-scoped only.
-- Multiple sources per day are allowed; each source is optional.
--
-- Target: tenant DBs (dev3, then each tenant BEFORE the code deploys)
--
-- ROLLBACK:
--   DROP TABLE IF EXISTS jute_market_rate;
--   DROP TABLE IF EXISTS jute_rate_diff_mst;
--   DROP TABLE IF EXISTS jute_market_rate_source_mst;
-- =============================================================================

CREATE TABLE IF NOT EXISTS jute_market_rate_source_mst (
    source_id           INT           NOT NULL AUTO_INCREMENT,
    co_id               INT           NOT NULL,
    source_name         VARCHAR(100)  NOT NULL,
    active              INT           NOT NULL DEFAULT 1,
    updated_by          INT               NULL,
    updated_date_time   DATETIME          NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id),
    UNIQUE KEY uk_jute_mkt_src (co_id, source_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS jute_rate_diff_mst (
    rate_diff_id        INT           NOT NULL AUTO_INCREMENT,
    co_id               INT           NOT NULL,
    item_id             INT           NOT NULL,
    base_item_id        INT           NOT NULL,
    diff_amount         DOUBLE        NOT NULL,
    effective_from      DATE          NOT NULL,
    active              INT           NOT NULL DEFAULT 1,
    updated_by          INT               NULL,
    updated_date_time   DATETIME          NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (rate_diff_id),
    UNIQUE KEY uk_jute_rate_diff (co_id, item_id, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS jute_market_rate (
    market_rate_id      INT           NOT NULL AUTO_INCREMENT,
    co_id               INT           NOT NULL,
    rate_date           DATE          NOT NULL,
    source_id           INT           NOT NULL,
    item_id             INT           NOT NULL,
    rate                DOUBLE        NOT NULL,
    active              INT           NOT NULL DEFAULT 1,
    updated_by          INT               NULL,
    updated_date_time   DATETIME          NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (market_rate_id),
    UNIQUE KEY uk_jute_market_rate (co_id, rate_date, source_id, item_id),
    KEY idx_jute_market_rate_item (co_id, item_id, rate_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
