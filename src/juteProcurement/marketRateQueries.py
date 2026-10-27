"""SQL for the Jute Market Rate pages (src/juteProcurement/marketRate.py)."""

from sqlalchemy import text


def get_raw_jute_items_query():
    """Raw-jute items (quality = item) of a company, with their jute type (item group)."""
    return text(
        """
        SELECT im.item_id, im.item_name, ig.item_grp_id, ig.item_grp_name
        FROM item_mst im
        JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE ig.item_type_id = 2
          AND ig.co_id = :co_id
          AND (ig.active IN ('1', 'Y') OR ig.active IS NULL)
          AND (im.active = 1 OR im.active IS NULL)
        ORDER BY ig.item_grp_name, im.item_name
        """
    )


def get_sources_query():
    return text(
        """
        SELECT source_id, source_name
        FROM jute_market_rate_source_mst
        WHERE co_id = :co_id AND active = 1
        ORDER BY source_name
        """
    )


def get_rate_diff_table_query():
    """All active diff rows, newest first per item. in_force = the row that applies on :as_of."""
    return text(
        """
        SELECT d.rate_diff_id, d.item_id, im.item_name, ig.item_grp_name,
               d.base_item_id, bim.item_name AS base_item_name,
               d.diff_amount, d.effective_from,
               CASE WHEN d.effective_from = (
                    SELECT MAX(d2.effective_from) FROM jute_rate_diff_mst d2
                    WHERE d2.co_id = d.co_id AND d2.item_id = d.item_id
                      AND d2.active = 1 AND d2.effective_from <= :as_of)
                    THEN 1 ELSE 0 END AS in_force
        FROM jute_rate_diff_mst d
        LEFT JOIN item_mst im ON im.item_id = d.item_id
        LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN item_mst bim ON bim.item_id = d.base_item_id
        WHERE d.co_id = :co_id AND d.active = 1
        ORDER BY ig.item_grp_name, im.item_name, d.effective_from DESC
        """
    )


def get_rate_diff_as_of_query():
    """The diff row in force per item on :as_of (latest effective_from <= :as_of)."""
    return text(
        """
        SELECT d.item_id, d.base_item_id, d.diff_amount
        FROM jute_rate_diff_mst d
        WHERE d.co_id = :co_id AND d.active = 1
          AND d.effective_from = (
              SELECT MAX(d2.effective_from) FROM jute_rate_diff_mst d2
              WHERE d2.co_id = d.co_id AND d2.item_id = d.item_id
                AND d2.active = 1 AND d2.effective_from <= :as_of)
        """
    )


def get_rates_for_day_query():
    return text(
        """
        SELECT item_id, rate
        FROM jute_market_rate
        WHERE co_id = :co_id AND rate_date = :rate_date AND source_id = :source_id AND active = 1
        """
    )


def get_day_source_counts_query():
    """Which sources already have rates on :rate_date (each source is optional per day)."""
    return text(
        """
        SELECT s.source_id, s.source_name, COUNT(*) AS item_count
        FROM jute_market_rate r
        JOIN jute_market_rate_source_mst s ON s.source_id = r.source_id
        WHERE r.co_id = :co_id AND r.rate_date = :rate_date AND r.active = 1
        GROUP BY s.source_id, s.source_name
        ORDER BY s.source_name
        """
    )
