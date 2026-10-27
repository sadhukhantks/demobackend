"""Raw SQL builders for the Jute Production — Selection Trolley Weight module.

Spec: ../vowerp3ui/docs/claude/modules/jute-production/selection-trolley-weight-spec.md
(§2 schema, §3 contract, decisions D1–D7).

One row per loaded trolley in ``jute_prod_selection_trolly_wt``; qualities, gate entries,
trolleys and spells come from existing tables. Conventions mirror bale_query.py:
sqlalchemy.text() with named binds, the ``:x IS NULL OR ...`` optional-filter idiom,
soft delete via ``active = 1``, ``co_id`` in every by-id read guard AND every UPDATE.
Reused builders (spells, trolleys, branch guard / name) are imported by the routers from
spinning_query / gain_loss_query / bale_reports_query — not duplicated here.

The jute-issue attribution rule (D3 / D4 / D7) is ONE fragment, ``_issue_from()``, shared
by every issue-side builder so the picker, the save-time guards and the report can never
drift apart: MR-sourced ``jute_issue`` rows only (INNER JOIN jute_mr_li + jute_mr, which
also drops waste re-issues whose jute_mr_li_id is NULL), ``status_id <>``
JUTE_ISSUE_EXCLUDED_STATUS, quality = ``jute_mr_li.actual_item_id``, scoped by
``jute_issue.branch_id`` (jute_issue has NO co_id — the router verifies the branch
belongs to the company first). The D4 window is
``issue_date > :tran_date - :days AND issue_date <= :tran_date``.

Notes on the joins:
* ``item_mst`` carries no co_id; the quality identity is the raw-jute item_id shared
  with the issue side, so no company predicate is needed on it.
* ``trolly_mst`` has NO ``active`` column; the picker filters ``trolly_type = 'T'``
  and the stage tag by machine_type NAME (D1).
* Gate entry number / date live on ``jute_mr`` (jute_gate_entry_no /
  jute_gate_entry_date); the supplier label is ``jute_supplier_mst.supplier_name``
  (LEFT JOIN, NULL-safe).
"""

from sqlalchemy import text

from src.juteProcurement.constants import JUTE_ISSUE_EXCLUDED_STATUS

_ITEM_JOIN = "LEFT JOIN item_mst im ON im.item_id = mrli.actual_item_id"
_SUPPLIER_JOIN = "LEFT JOIN jute_supplier_mst s ON s.supplier_id = jm.jute_supplier_id"


def _issue_from(*extra_joins: str) -> str:
    """Shared issue-side FROM/WHERE fragment (D3). Binds: :branch_id.

    ``extra_joins`` are LEFT JOINs a builder needs for labels (item / supplier); the
    date predicate is appended by each builder (window for the pickers / guards, range
    for the report).
    """
    joins = "".join(f"\n        {j}" for j in extra_joins)
    return f"""
        FROM jute_issue ji
        INNER JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
        INNER JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id{joins}
        WHERE ji.branch_id = :branch_id
          AND ji.status_id <> {int(JUTE_ISSUE_EXCLUDED_STATUS)}
"""

# D4 window predicate. Binds: :tran_date (date), :days (int).
_WINDOW = """
          AND ji.issue_date > DATE_SUB(:tran_date, INTERVAL :days DAY)
          AND ji.issue_date <= :tran_date
"""

# Report range predicate. Binds: :date_from, :date_to.
_RANGE = """
          AND ji.issue_date BETWEEN :date_from AND :date_to
"""


# =============================================================================
# Entry setup lookups
# =============================================================================


def get_selection_qualities_query():
    """D4: qualities issued to the branch in the window, with the issued kg.

    One row per raw-jute item (jute_mr_li.actual_item_id), ordered by item_name.
    """
    return text(
        f"""
        SELECT
            mrli.actual_item_id AS item_id,
            im.item_code,
            im.item_name,
            ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS issued_kg
        {_issue_from(_ITEM_JOIN)}
        {_WINDOW}
        GROUP BY mrli.actual_item_id, im.item_code, im.item_name
        ORDER BY im.item_name
        """
    )


def get_selection_gate_entries_query():
    """D7: one row per jute_mr whose lines were issued to the branch in the window.

    ``item_ids`` is the comma-separated distinct actual_item_id list of those issued
    lines (the router splits it); ``issued_kg`` is the MR's issued weight in the
    window. Supplier label via jute_supplier_mst (LEFT JOIN — NULL when unlinked).
    Ordered by gate entry number, newest first.
    """
    return text(
        f"""
        SELECT
            jm.jute_mr_id,
            jm.jute_gate_entry_no AS gate_entry_no,
            jm.jute_gate_entry_date AS gate_entry_date,
            s.supplier_name,
            GROUP_CONCAT(DISTINCT mrli.actual_item_id ORDER BY mrli.actual_item_id) AS item_ids,
            ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS issued_kg
        {_issue_from(_SUPPLIER_JOIN)}
        {_WINDOW}
        GROUP BY jm.jute_mr_id, jm.jute_gate_entry_no, jm.jute_gate_entry_date, s.supplier_name
        ORDER BY jm.jute_gate_entry_no DESC, jm.jute_mr_id DESC
        """
    )


# =============================================================================
# Validation / lookup builders (write path)
# =============================================================================


def verify_selection_quality_query():
    """Save-time D4 guard: :item_id was issued to :branch_id in the window.

    Returns one row (the item id) or nothing -> 400 in the router.
    """
    return text(
        f"""
        SELECT mrli.actual_item_id AS item_id
        {_issue_from()}
        {_WINDOW}
          AND mrli.actual_item_id = :item_id
        LIMIT 1
        """
    )


def verify_selection_gate_entry_query():
    """Save-time D7 guard: :jute_mr_id has a line of :item_id issued to :branch_id in
    the window. Returns one row or nothing -> 400 in the router."""
    return text(
        f"""
        SELECT jm.jute_mr_id
        {_issue_from()}
        {_WINDOW}
          AND jm.jute_mr_id = :jute_mr_id
          AND mrli.actual_item_id = :item_id
        LIMIT 1
        """
    )


def get_selection_trolly_row_query():
    """Trolly row for the D1 checks + tare snapshot: type, branch and stage tag name.

    trolly_mst has no ``active`` column. machine_type_mst is LEFT-joined so an
    untagged trolley still comes back (with a NULL machine_type_name) and the router
    can answer the specific "not tagged to Selection" 400.
    """
    return text(
        """
        SELECT t.trolly_id, t.trolly_name, t.trolly_weight, t.branch_id,
               COALESCE(t.trolly_type, 'T') AS trolly_type,
               t.machine_type_id, mt.machine_type_name
        FROM trolly_mst t
        LEFT JOIN machine_type_mst mt
               ON mt.machine_type_id = t.machine_type_id AND mt.active = 1
        WHERE t.trolly_id = :trolly_id
        """
    )


def get_selection_active_by_id_query():
    """One active row by id, scoped to the caller's company (edit / delete guard).

    Returns the stored tare snapshot and trolly_id so an edit that keeps the trolley
    keeps the snapshot (§3.2 tare snapshot rule). The ``co_id`` predicate is
    load-bearing (see module docstring).
    """
    return text(
        """
        SELECT selection_wt_id, co_id, branch_id, tran_date, spell_id, item_id, trolly_id,
               jute_mr_id, trolly_wt, gross_wt, net_wt, weigh_time, remarks
        FROM jute_prod_selection_trolly_wt
        WHERE selection_wt_id = :id AND co_id = :co_id AND active = 1
        """
    )


# =============================================================================
# Entry CRUD builders
# =============================================================================


def insert_selection_entry_query():
    """Insert one row (active = 1). weigh_time takes the column default (now).

    trolly_wt / net_wt are the server-computed snapshot and net — never trusted from
    the client.
    """
    return text(
        """
        INSERT INTO jute_prod_selection_trolly_wt
            (co_id, branch_id, tran_date, spell_id, item_id, trolly_id, jute_mr_id,
             trolly_wt, gross_wt, net_wt, remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, :item_id, :trolly_id, :jute_mr_id,
             :trolly_wt, :gross_wt, :net_wt, :remarks, 1, :updated_by)
        """
    )


def update_selection_entry_query():
    """Full replace of one row's editable columns (snapshot / net decided by the
    router). weigh_time is kept (it records when the trolley was actually weighed).
    Carries the same ``co_id`` predicate as the read guard."""
    return text(
        """
        UPDATE jute_prod_selection_trolly_wt
        SET branch_id  = :branch_id,
            tran_date  = :tran_date,
            spell_id   = :spell_id,
            item_id    = :item_id,
            trolly_id  = :trolly_id,
            jute_mr_id = :jute_mr_id,
            trolly_wt  = :trolly_wt,
            gross_wt   = :gross_wt,
            net_wt     = :net_wt,
            remarks    = :remarks,
            updated_by = :updated_by
        WHERE selection_wt_id = :id AND co_id = :co_id AND active = 1
        """
    )


def soft_delete_selection_entry_query():
    """Soft delete (active = 0), scoped to the caller's company."""
    return text(
        """
        UPDATE jute_prod_selection_trolly_wt
        SET active = 0, updated_by = :updated_by
        WHERE selection_wt_id = :id AND co_id = :co_id AND active = 1
        """
    )


# =============================================================================
# Reads — SelectionRow (shared by entries_by_date and the create / edit echo)
# =============================================================================


def get_selection_rows_query():
    """The SelectionRow shape (§3.1), one builder for every read.

    Every filter is optional (NULL bind = no filter) so the same statement serves the
    day grid (branch + tran_date [+ spell_id]) and the single-row echo after create /
    edit (selection_wt_id). Item / trolly / spell / MR are LEFT joins so a retired
    master never hides a row. Ordered by weigh_time within the day, then id.
    """
    return text(
        """
        SELECT
            sw.selection_wt_id,
            sw.co_id,
            sw.branch_id,
            sw.tran_date,
            sw.spell_id,
            sp.spell_code,
            sw.item_id,
            im.item_code,
            im.item_name,
            sw.trolly_id,
            t.trolly_name,
            sw.jute_mr_id,
            jm.jute_gate_entry_no AS gate_entry_no,
            jm.jute_gate_entry_date AS gate_entry_date,
            sw.trolly_wt,
            sw.gross_wt,
            sw.net_wt,
            sw.weigh_time,
            sw.remarks
        FROM jute_prod_selection_trolly_wt sw
        LEFT JOIN item_mst im ON im.item_id = sw.item_id
        LEFT JOIN trolly_mst t ON t.trolly_id = sw.trolly_id
        LEFT JOIN spell_mst sp ON sp.spell_id = sw.spell_id
        LEFT JOIN jute_mr jm ON jm.jute_mr_id = sw.jute_mr_id
        WHERE sw.active = 1
          AND sw.co_id = :co_id
          AND (:selection_wt_id IS NULL OR sw.selection_wt_id = :selection_wt_id)
          AND (:branch_id IS NULL OR sw.branch_id = :branch_id)
          AND (:tran_date IS NULL OR sw.tran_date = :tran_date)
          AND (:spell_id IS NULL OR sw.spell_id = :spell_id)
        ORDER BY sw.tran_date, sw.weigh_time, sw.selection_wt_id
        """
    )


def get_selection_by_quality_query():
    """Per-quality roll-up for the day grid footer (entries_by_date.by_quality)."""
    return text(
        """
        SELECT
            sw.item_id,
            im.item_name,
            COUNT(*)       AS trolleys,
            SUM(sw.net_wt) AS net_wt
        FROM jute_prod_selection_trolly_wt sw
        LEFT JOIN item_mst im ON im.item_id = sw.item_id
        WHERE sw.active = 1
          AND sw.co_id = :co_id
          AND sw.branch_id = :branch_id
          AND sw.tran_date = :tran_date
          AND (:spell_id IS NULL OR sw.spell_id = :spell_id)
        GROUP BY sw.item_id, im.item_name
        ORDER BY im.item_name
        """
    )


# =============================================================================
# Report — selection_vs_issue (§3.3)
# =============================================================================


def get_issue_daily_by_quality_query():
    """Issue side of the report: D3 rule grouped by (issue_date, actual_item_id).

    ``issue_date`` is aliased ``tran_date`` so both sides share the merge key.
    """
    return text(
        f"""
        SELECT
            ji.issue_date AS tran_date,
            mrli.actual_item_id AS item_id,
            im.item_code,
            im.item_name,
            ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS issue_kg
        {_issue_from(_ITEM_JOIN)}
        {_RANGE}
        GROUP BY ji.issue_date, mrli.actual_item_id, im.item_code, im.item_name
        ORDER BY ji.issue_date, im.item_name
        """
    )


def get_selection_daily_by_quality_query():
    """Selection side of the report: active rows grouped by (tran_date, item_id)."""
    return text(
        """
        SELECT
            sw.tran_date,
            sw.item_id,
            im.item_code,
            im.item_name,
            ROUND(SUM(sw.net_wt), 3) AS selection_kg,
            COUNT(*) AS trolleys
        FROM jute_prod_selection_trolly_wt sw
        LEFT JOIN item_mst im ON im.item_id = sw.item_id
        WHERE sw.active = 1
          AND sw.co_id = :co_id
          AND sw.branch_id = :branch_id
          AND sw.tran_date BETWEEN :date_from AND :date_to
        GROUP BY sw.tran_date, sw.item_id, im.item_code, im.item_name
        ORDER BY sw.tran_date, im.item_name
        """
    )


def get_selection_by_gate_entry_query():
    """by_gate_entry selection side: rows carrying a jute_mr_id, grouped by
    (jute_mr_id, gate_entry_no, item_id)."""
    return text(
        """
        SELECT
            sw.jute_mr_id,
            jm.jute_gate_entry_no AS gate_entry_no,
            jm.jute_gate_entry_date AS gate_entry_date,
            sw.item_id,
            im.item_name,
            ROUND(SUM(sw.net_wt), 3) AS selection_kg,
            COUNT(*) AS trolleys
        FROM jute_prod_selection_trolly_wt sw
        LEFT JOIN item_mst im ON im.item_id = sw.item_id
        LEFT JOIN jute_mr jm ON jm.jute_mr_id = sw.jute_mr_id
        WHERE sw.active = 1
          AND sw.co_id = :co_id
          AND sw.branch_id = :branch_id
          AND sw.jute_mr_id IS NOT NULL
          AND sw.tran_date BETWEEN :date_from AND :date_to
        GROUP BY sw.jute_mr_id, jm.jute_gate_entry_no, jm.jute_gate_entry_date,
                 sw.item_id, im.item_name
        ORDER BY jm.jute_gate_entry_no DESC, im.item_name
        """
    )


def get_issue_by_gate_entry_query():
    """by_gate_entry issue side: the D3 rule restricted per MR, grouped by
    (jute_mr_id, actual_item_id) over the report range."""
    return text(
        f"""
        SELECT
            jm.jute_mr_id,
            mrli.actual_item_id AS item_id,
            ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS issue_kg
        {_issue_from()}
        {_RANGE}
        GROUP BY jm.jute_mr_id, mrli.actual_item_id
        """
    )
