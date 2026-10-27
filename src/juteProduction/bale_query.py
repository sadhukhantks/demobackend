"""Raw SQL builders for the Jute Production — Bale Production (Bale Press) module.

Bale Production is a per-bale weighing log (docs/bale-production-spec-sheet.md §4–§6):
one row per bale in ``jute_prod_bale_entry`` (PROD or REPACK, D2) and a small running
counter master ``jute_prod_bale_series`` (the sheet's "Last Bale No" tab, D3). Standards
(quality type, std bale weight, packsheet, std MR %, weft yarn) are REUSED from
``jute_prod_finishing_quality``; spells, Bale Press machines and trolleys come from their
existing masters.

Conventions mirror waste_query.py / finishing_query.py: sqlalchemy.text() with named
binds, the ``:x IS NULL OR ...`` optional-filter idiom, soft delete via ``active = 1``,
``co_id`` in every by-id read guard AND every UPDATE (a tenant DB holds every company's
rows, so a primary-key-only match would let company A mutate company B's row by guessing
an id). ``spell_mst`` keys activity by ``status``, NOT ``active``. Reused builders
(spells, Bale Press machines, type-5 items) are imported by the router from
finishing_query — not duplicated here.

Notes on the joins:
* ``item_mst`` carries no co_id — company scope rides on ``item_grp_mst.co_id``.
* ``trolly_mst`` has NO ``active`` column; the picker filters ``trolly_type = 'T'`` and
  orders Bale Press-tagged trolleys first, then untagged ones (§6.3).
* ``jute_yarn_mst.jute_yarn_count`` (via ``weft_yarn_item_id``) is the "WFT USED" lbs.
* ``party_mst.supp_name`` is the display label for BOTH suppliers and customers.
* The F10 sync key is (co_id, tran_date, spell_id, machine_id, finishing_quality_id);
  machine_id is NULL-able so the daily-row match is written NULL-safe.
"""

from sqlalchemy import bindparam, text


# =============================================================================
# Entry setup lookups
# =============================================================================


def get_bale_qualities_query():
    """Finishing qualities with item label + the standards the entry page needs.

    Company-scoped, active only, branch-tolerant (the quality master is company-wide
    and rows may carry a NULL branch). ``weft_count`` comes from the linked weft yarn's
    jute_yarn_mst.jute_yarn_count (NULL when unlinked). Ordered type-then-name so the
    type-ahead picker groups hessian and sacking.
    """
    return text(
        """
        SELECT
            q.finishing_quality_id,
            q.item_id,
            im.item_code,
            im.item_name,
            q.quality_type,
            q.std_bale_weight,
            q.packsheet_wt,
            q.std_mr_pct,
            q.weft_yarn_item_id,
            wy.jute_yarn_count AS weft_count
        FROM jute_prod_finishing_quality q
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        LEFT JOIN jute_yarn_mst wy ON wy.item_id = q.weft_yarn_item_id
        WHERE q.co_id = :co_id
          AND q.active = 1
          AND (:branch_id IS NULL OR q.branch_id = :branch_id OR q.branch_id IS NULL)
        ORDER BY q.quality_type, im.item_name
        """
    )


def get_bale_series_setup_query():
    """Active series for the entry page's series picker (company + optional branch)."""
    return text(
        """
        SELECT bale_series_id, series_name, quality_type, item_id, state_id, party_id,
               last_bale_no
        FROM jute_prod_bale_series
        WHERE co_id = :co_id
          AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        ORDER BY quality_type, series_name
        """
    )


def get_bale_trollies_query():
    """Weighing trolleys for the picker: trolly_type 'T', Bale Press-tagged first then
    untagged (§6.3). Trolleys tagged with ANOTHER stage's machine type are excluded.

    trolly_mst has no ``active`` column. The Bale Press type names bind at call time via
    the expanding :machine_types (FINISHING_MACHINE_TYPE_NAMES["balepress"]) — tenant
    vocabularies differ, so no literal here. Branch is optional.
    """
    return text(
        """
        SELECT
            t.trolly_id,
            t.trolly_name,
            t.trolly_weight,
            t.machine_type_id,
            mt.machine_type_name
        FROM trolly_mst t
        LEFT JOIN machine_type_mst mt
               ON mt.machine_type_id = t.machine_type_id AND mt.active = 1
        WHERE COALESCE(t.trolly_type, 'T') = 'T'
          AND (t.machine_type_id IS NULL OR mt.machine_type_name IN :machine_types)
          AND (:branch_id IS NULL OR t.branch_id IS NULL OR t.branch_id = :branch_id)
        ORDER BY CASE WHEN t.machine_type_id IS NULL THEN 1 ELSE 0 END, t.trolly_name
        """
    ).bindparams(bindparam("machine_types", expanding=True))


def get_bale_entries_count_query():
    """Count of the day's active bales (entry_setup.entries_today_count)."""
    return text(
        """
        SELECT COUNT(*) AS cnt
        FROM jute_prod_bale_entry
        WHERE co_id = :co_id
          AND active = 1
          AND tran_date = :tran_date
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        """
    )


# =============================================================================
# Validation / lookup builders (write path)
# =============================================================================


def get_bale_quality_row_query():
    """One active finishing quality (co-scoped) with the standards to snapshot.

    No row = unknown / foreign / retired quality -> 400 in the router.
    """
    return text(
        """
        SELECT q.finishing_quality_id, q.co_id, q.quality_type, q.item_id,
               q.packsheet_wt, q.std_bale_weight, q.std_mr_pct, im.item_name
        FROM jute_prod_finishing_quality q
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        WHERE q.finishing_quality_id = :finishing_quality_id
          AND q.co_id = :co_id
          AND q.active = 1
        """
    )


def get_bale_series_row_query():
    """One active series by id, scoped to the caller's company (400/404 guard)."""
    return text(
        """
        SELECT bale_series_id, co_id, branch_id, series_name, quality_type, item_id,
               state_id, party_id, last_bale_no
        FROM jute_prod_bale_series
        WHERE bale_series_id = :bale_series_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def get_bale_machine_row_query():
    """Bale Press machine guard: active machine, with its branch via the dept spine.

    machine_mst has no branch_id of its own — branch travels via dept_mst. The router
    checks the branch matches the entry's and that the type is a Bale Press type.
    """
    return text(
        """
        SELECT m.machine_id, m.machine_name, m.mech_code, d.branch_id, mt.machine_type_name
        FROM machine_mst m
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        WHERE m.machine_id = :machine_id
          AND m.active = 1
        """
    )


def get_bale_trolly_row_query():
    """Trolly row for the tare snapshot (trolly_mst has no active column)."""
    return text(
        """
        SELECT t.trolly_id, t.trolly_name, t.trolly_weight, t.branch_id
        FROM trolly_mst t
        WHERE t.trolly_id = :trolly_id
        """
    )


def get_bale_duplicate_query():
    """The (series, bale_no) clash probe — any ACTIVE row, PROD or REPACK (§6.1).

    ``:exclude_id`` is the row being edited (None on create) so an edit that keeps its
    own number does not collide with itself. The clashing row's identity comes back so
    the 409 detail can name it (date / spell / weight) for the confirm dialog.
    """
    return text(
        """
        SELECT be.bale_entry_id, be.tran_date, be.spell_id, sp.spell_code, be.entry_type,
               be.gross_wt, be.net_wt
        FROM jute_prod_bale_entry be
        LEFT JOIN spell_mst sp ON sp.spell_id = be.spell_id
        WHERE be.bale_series_id = :bale_series_id
          AND be.bale_no = :bale_no
          AND be.active = 1
          AND (:exclude_id IS NULL OR be.bale_entry_id <> :exclude_id)
        ORDER BY be.bale_entry_id DESC
        LIMIT 1
        """
    )


def get_bale_original_query():
    """REPACK guard: an active bale with number :original_bale_no exists in the series.

    Any active row qualifies (the opened bale may itself have been a repack).
    ``:exclude_id`` keeps an edited row from vouching for itself.
    """
    return text(
        """
        SELECT bale_entry_id, tran_date, entry_type, gross_wt, net_wt
        FROM jute_prod_bale_entry
        WHERE bale_series_id = :bale_series_id
          AND bale_no = :original_bale_no
          AND active = 1
          AND (:exclude_id IS NULL OR bale_entry_id <> :exclude_id)
        ORDER BY bale_entry_id DESC
        LIMIT 1
        """
    )


def get_bale_active_by_id_query():
    """One active bale row by id, scoped to the caller's company (edit / delete guard).

    Returns the raw key columns the router needs to re-sync the OLD F10 key on edit and
    on delete. The ``co_id`` predicate is load-bearing (see module docstring).
    """
    return text(
        """
        SELECT bale_entry_id, co_id, branch_id, tran_date, spell_id, machine_id, entry_type,
               finishing_quality_id, bale_series_id, bale_no, original_bale_no, trolly_id,
               trolly_wt, packsheet_wt, gross_wt, net_wt, std_bale_wt, weigh_time, remarks
        FROM jute_prod_bale_entry
        WHERE bale_entry_id = :id AND co_id = :co_id AND active = 1
        """
    )


def get_bale_series_last_entry_query():
    """The most recent active bale of a series (next_bale_no.last_entry)."""
    return text(
        """
        SELECT bale_entry_id, tran_date, bale_no, gross_wt, net_wt, trolly_id,
               finishing_quality_id
        FROM jute_prod_bale_entry
        WHERE bale_series_id = :bale_series_id
          AND co_id = :co_id
          AND active = 1
        ORDER BY bale_entry_id DESC
        LIMIT 1
        """
    )


# =============================================================================
# Entry CRUD builders
# =============================================================================


def insert_bale_entry_query():
    """Insert one bale (active = 1). weigh_time takes the column default (now).

    trolly_wt / packsheet_wt / std_bale_wt / net_wt are the server-computed snapshots
    (F0) — never trusted from the client.
    """
    return text(
        """
        INSERT INTO jute_prod_bale_entry
            (co_id, branch_id, tran_date, spell_id, machine_id, entry_type,
             finishing_quality_id, bale_series_id, bale_no, original_bale_no,
             trolly_id, trolly_wt, packsheet_wt, gross_wt, net_wt, std_bale_wt,
             remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, :machine_id, :entry_type,
             :finishing_quality_id, :bale_series_id, :bale_no, :original_bale_no,
             :trolly_id, :trolly_wt, :packsheet_wt, :gross_wt, :net_wt, :std_bale_wt,
             :remarks, 1, :updated_by)
        """
    )


def update_bale_entry_query():
    """Full replace of one bale's editable columns (snapshots recomputed by the router).

    weigh_time is kept (it records when the bale was actually weighed). Carries the same
    ``co_id`` predicate as the read guard so the scope check cannot drift apart.
    """
    return text(
        """
        UPDATE jute_prod_bale_entry
        SET branch_id            = :branch_id,
            tran_date            = :tran_date,
            spell_id             = :spell_id,
            machine_id           = :machine_id,
            entry_type           = :entry_type,
            finishing_quality_id = :finishing_quality_id,
            bale_series_id       = :bale_series_id,
            bale_no              = :bale_no,
            original_bale_no     = :original_bale_no,
            trolly_id            = :trolly_id,
            trolly_wt            = :trolly_wt,
            packsheet_wt         = :packsheet_wt,
            gross_wt             = :gross_wt,
            net_wt               = :net_wt,
            std_bale_wt          = :std_bale_wt,
            remarks              = :remarks,
            updated_by           = :updated_by
        WHERE bale_entry_id = :id AND co_id = :co_id AND active = 1
        """
    )


def soft_delete_bale_entry_query():
    """Soft delete (active = 0), scoped to the caller's company."""
    return text(
        """
        UPDATE jute_prod_bale_entry
        SET active = 0, updated_by = :updated_by
        WHERE bale_entry_id = :id AND co_id = :co_id AND active = 1
        """
    )


def bump_bale_series_last_no_query():
    """F9: last_bale_no = max(last_bale_no, bale_no) on every save (PROD and REPACK).

    GREATEST keeps a backdated / duplicate-confirmed lower number from ever winding the
    counter backwards; a reset is only ever done by editing the master (D3).
    """
    return text(
        """
        UPDATE jute_prod_bale_series
        SET last_bale_no = GREATEST(last_bale_no, :bale_no),
            updated_by   = :updated_by
        WHERE bale_series_id = :bale_series_id AND co_id = :co_id AND active = 1
        """
    )


def rewind_bale_series_last_no_query():
    """Counter follows a removed bale: when :bale_no WAS the top of the series (the bale
    was deleted or renumbered away) wind last_bale_no back to the highest remaining
    active bale_no, never below opening_bale_no (the number set in the master while the
    old register is not migrated). A bale removed from the middle leaves the counter as is.

    ``<= :bale_no`` (not every row) keeps an allow_duplicate twin at the same number
    holding the counter, and ignores old-cycle rows above a downward master reset.
    """
    return text(
        """
        UPDATE jute_prod_bale_series bs
        SET bs.last_bale_no = GREATEST(
                bs.opening_bale_no,
                COALESCE((SELECT MAX(be.bale_no) FROM jute_prod_bale_entry be
                          WHERE be.bale_series_id = bs.bale_series_id
                            AND be.active = 1 AND be.bale_no <= :bale_no), 0)),
            bs.updated_by = :updated_by
        WHERE bs.bale_series_id = :bale_series_id AND bs.co_id = :co_id AND bs.active = 1
          AND bs.last_bale_no = :bale_no
        """
    )


def get_bale_series_last_no_query():
    """Current counter of an active series (echoed after a rewind)."""
    return text(
        """
        SELECT last_bale_no FROM jute_prod_bale_series
        WHERE bale_series_id = :bale_series_id AND co_id = :co_id AND active = 1
        """
    )


# =============================================================================
# Reads — BaleRow (shared by entries_by_date / bale_search / create & edit echo)
# =============================================================================


def get_bale_rows_query():
    """The BaleRow shape (§6.3), one builder for every read.

    Every filter is optional (NULL bind = no filter) so the same statement serves the
    day grid (tran_date [+ spell_id, entry_type]), the bale finder (bale_no matches the
    row's own number OR its original_bale_no — a repack IS part of that bale's history)
    and the single-row echo after create / edit (bale_entry_id). Quality / series /
    spell / machine / trolly are LEFT joins so a retired master never hides a row.
    Ordered by weigh_time within the day (the Daily Bale Sheet order).
    """
    return text(
        """
        SELECT
            be.bale_entry_id,
            be.co_id,
            be.branch_id,
            be.tran_date,
            be.spell_id,
            sp.spell_code,
            be.machine_id,
            m.machine_name,
            be.entry_type,
            be.finishing_quality_id,
            q.item_id,
            im.item_name,
            q.quality_type,
            be.bale_series_id,
            bs.series_name,
            be.bale_no,
            be.original_bale_no,
            be.trolly_id,
            t.trolly_name,
            be.trolly_wt,
            be.packsheet_wt,
            be.gross_wt,
            be.net_wt,
            be.std_bale_wt,
            be.weigh_time,
            be.remarks
        FROM jute_prod_bale_entry be
        LEFT JOIN jute_prod_finishing_quality q ON q.finishing_quality_id = be.finishing_quality_id
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        LEFT JOIN jute_prod_bale_series bs ON bs.bale_series_id = be.bale_series_id
        LEFT JOIN spell_mst sp ON sp.spell_id = be.spell_id
        LEFT JOIN machine_mst m ON m.machine_id = be.machine_id
        LEFT JOIN trolly_mst t ON t.trolly_id = be.trolly_id
        WHERE be.active = 1
          AND be.co_id = :co_id
          AND (:bale_entry_id IS NULL OR be.bale_entry_id = :bale_entry_id)
          AND (:branch_id IS NULL OR be.branch_id = :branch_id)
          AND (:tran_date IS NULL OR be.tran_date = :tran_date)
          AND (:spell_id IS NULL OR be.spell_id = :spell_id)
          AND (:entry_type IS NULL OR be.entry_type = :entry_type)
          AND (:bale_series_id IS NULL OR be.bale_series_id = :bale_series_id)
          AND (:bale_no IS NULL OR be.bale_no = :bale_no OR be.original_bale_no = :bale_no)
        ORDER BY be.tran_date, be.weigh_time, be.bale_entry_id
        """
    )


def get_bale_totals_by_date_query():
    """Per-quality (and per entry_type) totals for the day grid footer (§6.3 totals).

    PROD and REPACK are separate groups so the FE can show both counts (Q4). std_bale_wt
    is the row snapshot (MAX over identical snapshots), not the live master — the footer
    must agree with the rows it sums.
    """
    return text(
        """
        SELECT
            be.finishing_quality_id,
            im.item_name,
            q.quality_type,
            be.entry_type,
            COUNT(*)          AS bales,
            SUM(be.gross_wt)  AS total_gross,
            SUM(be.net_wt)    AS total_net,
            AVG(be.net_wt)    AS avg_net,
            MAX(be.std_bale_wt) AS std_bale_wt
        FROM jute_prod_bale_entry be
        LEFT JOIN jute_prod_finishing_quality q ON q.finishing_quality_id = be.finishing_quality_id
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        WHERE be.active = 1
          AND be.co_id = :co_id
          AND be.tran_date = :tran_date
          AND (:branch_id IS NULL OR be.branch_id = :branch_id)
          AND (:spell_id IS NULL OR be.spell_id = :spell_id)
          AND (:entry_type IS NULL OR be.entry_type = :entry_type)
        GROUP BY be.finishing_quality_id, im.item_name, q.quality_type, be.entry_type
        ORDER BY q.quality_type, im.item_name, be.entry_type
        """
    )


# =============================================================================
# Series master builders
# =============================================================================


def get_bale_series_list_query():
    """Master grid: active series of the company (optionally one branch) with joined
    labels and the count of active bales referencing each (bales_total, §6.3)."""
    return text(
        """
        SELECT
            bs.bale_series_id,
            bs.co_id,
            bs.branch_id,
            b.branch_name,
            bs.series_name,
            bs.quality_type,
            bs.item_id,
            im.item_name,
            bs.state_id,
            st.state,
            bs.party_id,
            pm.supp_name AS party_name,
            bs.last_bale_no,
            (SELECT COUNT(*) FROM jute_prod_bale_entry be
              WHERE be.bale_series_id = bs.bale_series_id AND be.active = 1) AS bales_total
        FROM jute_prod_bale_series bs
        LEFT JOIN branch_mst b ON b.branch_id = bs.branch_id
        LEFT JOIN item_mst im ON im.item_id = bs.item_id
        LEFT JOIN state_mst st ON st.state_id = bs.state_id
        LEFT JOIN party_mst pm ON pm.party_id = bs.party_id
        WHERE bs.co_id = :co_id
          AND bs.active = 1
          AND (:branch_id IS NULL OR bs.branch_id = :branch_id)
        ORDER BY bs.quality_type, bs.series_name
        """
    )


def get_bale_series_duplicate_query():
    """App-level duplicate guard on (co_id, branch_id, series_name) among active = 1.

    ``:exclude_id`` is the row being edited (None on create). Case-insensitive under
    MySQL's default collation, which is what the operator expects ('Punjab' = 'PUNJAB').
    """
    return text(
        """
        SELECT bale_series_id
        FROM jute_prod_bale_series
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND series_name = :series_name
          AND active = 1
          AND (:exclude_id IS NULL OR bale_series_id <> :exclude_id)
        LIMIT 1
        """
    )


def verify_bale_series_item_query():
    """A bound series item must be an active 'Jute Cloth' item (type 5) of the company.

    Same one-query shape as the finishing item picker: item_mst has no co_id, so scope
    and type both hang off item_grp_mst.
    """
    return text(
        """
        SELECT im.item_id
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE im.item_id = :item_id
          AND im.active = 1
          AND ig.item_type_id = :item_type_id
          AND ig.co_id = :co_id
        """
    )


def count_bale_series_active_bales_query():
    """Active bales referencing a series — blocks delete (400) when > 0."""
    return text(
        """
        SELECT COUNT(*) AS cnt
        FROM jute_prod_bale_entry
        WHERE bale_series_id = :bale_series_id
          AND active = 1
        """
    )


def insert_bale_series_query():
    return text(
        """
        INSERT INTO jute_prod_bale_series
            (co_id, branch_id, series_name, quality_type, item_id, state_id, party_id,
             last_bale_no, opening_bale_no, active, updated_by)
        VALUES
            (:co_id, :branch_id, :series_name, :quality_type, :item_id, :state_id, :party_id,
             :last_bale_no, :last_bale_no, 1, :updated_by)
        """
    )


def update_bale_series_query():
    """Replace every field, including last_bale_no (editing it IS the manual reset, D3).

    A CHANGED last_bale_no also becomes the new opening_bale_no (the rewind floor).
    opening_bale_no is assigned BEFORE last_bale_no on purpose: MySQL evaluates SET
    left to right, so the comparison still sees the old counter.
    """
    return text(
        """
        UPDATE jute_prod_bale_series
        SET branch_id       = :branch_id,
            series_name     = :series_name,
            quality_type    = :quality_type,
            item_id         = :item_id,
            state_id        = :state_id,
            party_id        = :party_id,
            opening_bale_no = IF(last_bale_no <> :last_bale_no, :last_bale_no, opening_bale_no),
            last_bale_no    = :last_bale_no,
            updated_by   = :updated_by
        WHERE bale_series_id = :bale_series_id AND co_id = :co_id AND active = 1
        """
    )


def soft_delete_bale_series_query():
    return text(
        """
        UPDATE jute_prod_bale_series
        SET active = 0, updated_by = :updated_by
        WHERE bale_series_id = :bale_series_id AND co_id = :co_id AND active = 1
        """
    )


# =============================================================================
# Series setup lookups (states / customers; items reuse get_finishing_items_query)
# =============================================================================


def get_bale_states_query():
    """state_mst for the optional state link (no co / active columns on that table)."""
    return text(
        """
        SELECT state_id, state, state_code
        FROM state_mst
        ORDER BY state
        """
    )


def get_bale_parties_query():
    """Active parties of the company for the optional party link (e.g. FCI).

    Deliberately NOT narrowed by party_type (customer / supplier flags are a
    comma-separated VARCHAR on party_mst): a consignee such as FCI may be filed under
    any type, and the link is optional.
    """
    return text(
        """
        SELECT party_id, supp_name
        FROM party_mst
        WHERE co_id = :co_id
          AND active = 1
        ORDER BY supp_name
        """
    )


# =============================================================================
# F10 — jute_prod_finishing_daily (process = 'balepress') sync builders
# =============================================================================


def aggregate_prod_bales_for_key_query():
    """Count + Σ net of ACTIVE PROD bales on one F10 key.

    Key = (co_id, tran_date, spell_id, machine_id, finishing_quality_id); machine_id is
    NULL-able, hence the NULL-safe match. REPACK rows are excluded on purpose — they
    re-press an already-counted bale (Q4).
    """
    return text(
        """
        SELECT COUNT(*) AS bales, COALESCE(SUM(net_wt), 0) AS total_net
        FROM jute_prod_bale_entry
        WHERE co_id = :co_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND ((:machine_id IS NULL AND machine_id IS NULL) OR machine_id = :machine_id)
          AND finishing_quality_id = :finishing_quality_id
          AND entry_type = :entry_type
          AND active = 1
        """
    )


def get_balepress_daily_row_query():
    """The active jute_prod_finishing_daily row on the F10 key (process bound).

    The machine predicate is written NULL-safe so a branch without a Bale Press machine
    row (machine_id NULL) still keys onto exactly one daily row.
    """
    return text(
        """
        SELECT finishing_daily_id, prod_qty, prod_wt_kg
        FROM jute_prod_finishing_daily
        WHERE co_id = :co_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND process = :process
          AND ((:machine_id IS NULL AND machine_id IS NULL) OR machine_id = :machine_id)
          AND finishing_quality_id = :finishing_quality_id
          AND active = 1
        ORDER BY finishing_daily_id DESC
        LIMIT 1
        """
    )


def insert_balepress_daily_query():
    """Insert the balepress daily figure: prod_qty = bales, prod_uom 'bale', prod_wt_kg = Σ net."""
    return text(
        """
        INSERT INTO jute_prod_finishing_daily
            (co_id, branch_id, tran_date, spell_id, process, machine_id, finishing_quality_id,
             prod_qty, prod_uom, prod_wt_kg, active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, :process, :machine_id, :finishing_quality_id,
             :prod_qty, :prod_uom, :prod_wt_kg, 1, :updated_by)
        """
    )


def update_balepress_daily_query():
    """Refresh the figure on an existing balepress daily row."""
    return text(
        """
        UPDATE jute_prod_finishing_daily
        SET branch_id  = :branch_id,
            prod_qty   = :prod_qty,
            prod_uom   = :prod_uom,
            prod_wt_kg = :prod_wt_kg,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE finishing_daily_id = :id
        """
    )


def soft_delete_balepress_daily_query():
    """Count 0 on the key -> the daily row is soft-deleted (F10)."""
    return text(
        """
        UPDATE jute_prod_finishing_daily
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE finishing_daily_id = :id
        """
    )
