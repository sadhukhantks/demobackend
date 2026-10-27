"""SQL for the Tally Link Master (src/masters/tallyLink.py)."""

from sqlalchemy import text

# Company-level Tally ledgers (link_type, ref_id = 0). The Tally exports use this wording
# when the company has no active row -- the names on the sample sheets the users supplied.
LEDGER_DEFAULTS = {
    "PURCHASE_LEDGER": "Purchase of Raw Jute",
    "PURCHASE_CLAIM": "Claim on Gross Purchase",
    "TDS_194Q": "TDS ON PURCHASE OF GOODS (194Q)",
    "SALES_LEDGER": "Sale of Raw Jute",
    "SALES_CLAIM": "Claim on Gross Sales",
    "ROUND_OFF": "Round Off",
}


def get_tally_link_table_query():
    """All rows of a company (inactive included, so they can be re-activated), ERP name resolved."""
    return text(
        """
        SELECT t.tally_link_id, t.co_id, t.link_type, t.ref_id, t.tally_name, t.active,
               CASE t.link_type
                    WHEN 'PARTY' THEN pm.supp_name
                    WHEN 'DESTINATION_PARTY' THEN pm.supp_name
                    WHEN 'ITEM' THEN im.item_name
                    WHEN 'DESTINATION' THEN bm.branch_name
                    ELSE 'Company default' END AS erp_name
        FROM tally_link_mst t
        LEFT JOIN party_mst pm ON t.link_type IN ('PARTY', 'DESTINATION_PARTY') AND pm.party_id = t.ref_id
        LEFT JOIN item_mst im ON t.link_type = 'ITEM' AND im.item_id = t.ref_id
        LEFT JOIN branch_mst bm ON t.link_type = 'DESTINATION' AND bm.branch_id = t.ref_id
        WHERE t.co_id = :co_id
          AND (:link_type IS NULL OR t.link_type = :link_type)
          AND (:search IS NULL OR t.tally_name LIKE :search
               OR pm.supp_name LIKE :search OR im.item_name LIKE :search
               OR bm.branch_name LIKE :search)
        ORDER BY t.link_type, erp_name
        """
    )


def get_parties_query():
    """Active parties of a company; :ref_id narrows to one (create-time validation)."""
    return text(
        """
        SELECT pm.party_id AS ref_id, pm.supp_name AS erp_name
        FROM party_mst pm
        WHERE pm.co_id = :co_id AND pm.active = 1
          AND (:ref_id IS NULL OR pm.party_id = :ref_id)
        ORDER BY pm.supp_name
        """
    )


def get_branches_query():
    """Branches of a company (DESTINATION picker); :ref_id narrows to one (create-time validation)."""
    return text(
        """
        SELECT bm.branch_id AS ref_id, bm.branch_name AS erp_name
        FROM branch_mst bm
        WHERE bm.co_id = :co_id
          AND (:ref_id IS NULL OR bm.branch_id = :ref_id)
        ORDER BY bm.branch_name
        """
    )


def get_raw_jute_items_query():
    """Raw-jute items of a company (item_grp_mst.item_type_id = 2); :ref_id narrows to one."""
    return text(
        """
        SELECT im.item_id AS ref_id, im.item_name AS erp_name, ig.item_grp_name
        FROM item_mst im
        JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE ig.item_type_id = 2
          AND ig.co_id = :co_id
          AND (ig.active IN ('1', 'Y') OR ig.active IS NULL)
          AND (im.active = 1 OR im.active IS NULL)
          AND (:ref_id IS NULL OR im.item_id = :ref_id)
        ORDER BY ig.item_grp_name, im.item_name
        """
    )
