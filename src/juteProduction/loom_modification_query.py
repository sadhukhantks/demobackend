"""Raw SQL builders for the Loom Modification register (Jute Production).

Spec: docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md §4, §5, §8.
Every builder is arg-less and returns a sqlalchemy.text() statement; the router
(loom_modification.py) supplies the named binds. Conventions of yarn_ledger_query.py /
stoppage_query.py: the ``:x IS NULL OR ...`` optional-filter idiom, list binds through
``bindparam(..., expanding=True)``.

WHAT IS A LOOM (§4): an active machine_mst row whose machine_type_mst name equals
:loom_type (WEAVING_MACHINE_TYPE_NAME, 'Loom'), compared inside SQL where the
utf8mb4_0900_ai_ci collation is case-insensitive — dev3 'LOOM' (id 6) and sls 'Loom'
(id 7) both match. Looms are NEVER resolved by machine_type_id. machine_mst has no
co_id / branch_id: the branch is dept_mst.branch_id and the company is
branch_mst.co_id through that branch (sls holds co 2 and co 106 in one DB).

line_no is INT on dev3 and VARCHAR(10) on sls, so it is always CAST to CHAR.

Per-loom span (§5): D_m = COALESCE(done_date, mod_date),
E_m = COALESCE(done_to_date, done_date, mod_to_date, mod_date).
"""

from sqlalchemy import bindparam, text


# =============================================================================
# Setup / lookups
# =============================================================================


def get_looms_query():
    """All active looms of a company (optionally one branch) with dept + derived branch.

    get_stoppage_machines_query shape narrowed to the loom type by NAME (ci collation)
    and joined dept_mst -> branch_mst so only branches of :co_id are returned.
    line_no as CHAR (INT on dev3, VARCHAR on sls) — the setup derives the distinct
    ``lines`` list from these rows.
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            m.mech_shr_code,
            CAST(m.line_no AS CHAR) AS line_no,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id,
            b.branch_name
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        INNER JOIN branch_mst b ON b.branch_id = d.branch_id
        WHERE m.active = 1
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND b.co_id = :co_id
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY d.branch_id, m.machine_name
        """
    )


def get_loom_context_query():
    """Identity + is-a-loom flag + derived branch / company for a list of machine ids.

    Used on every write (group members, event snapshot): NOT gated on active — the
    caller decides which rows must be active (newly ticked only, §5) and labels the
    rest "inactive". ``is_loom`` compares the type name inside SQL; ``branch_co_id``
    is branch_mst.co_id so the router can refuse a branch of another company.
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            m.active,
            CAST(m.line_no AS CHAR) AS line_no,
            mt.machine_type_name,
            CASE WHEN mt.machine_type_name = :loom_type THEN 1 ELSE 0 END AS is_loom,
            d.dept_id,
            d.branch_id,
            b.co_id AS branch_co_id
        FROM machine_mst m
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN branch_mst b ON b.branch_id = d.branch_id
        WHERE m.machine_id IN :machine_ids
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


def get_parties_query():
    """Active parties for the vendor dropdown (done_by = VENDOR); NULL co_id = shared."""
    return text(
        """
        SELECT
            p.party_id,
            p.supp_name AS party_name,
            p.supp_code AS party_code
        FROM party_mst p
        WHERE p.active = 1
          AND (p.co_id = :co_id OR p.co_id IS NULL)
        ORDER BY p.supp_name
        """
    )


def get_party_active_query():
    """Existence guard for a vendor party (done_by = VENDOR)."""
    return text(
        """
        SELECT p.party_id
        FROM party_mst p
        WHERE p.party_id = :party_id
          AND p.active = 1
          AND (p.co_id = :co_id OR p.co_id IS NULL)
        """
    )


# =============================================================================
# Loom groups (§4.1 / §4.2)
# =============================================================================


def get_loom_group_list_query():
    """Active groups of a company with member_count; optional branch / purpose / search."""
    return text(
        """
        SELECT
            g.loom_group_id,
            g.co_id,
            g.branch_id,
            b.branch_name,
            g.group_code,
            g.group_name,
            g.group_purpose,
            g.remarks,
            g.active,
            (SELECT COUNT(*) FROM jute_prod_loom_group_dtl gd
              WHERE gd.loom_group_id = g.loom_group_id AND gd.active = 1) AS member_count
        FROM jute_prod_loom_group g
        LEFT JOIN branch_mst b ON b.branch_id = g.branch_id
        WHERE g.active = 1
          AND g.co_id = :co_id
          AND (:branch_id IS NULL OR g.branch_id = :branch_id)
          AND (:group_purpose IS NULL OR g.group_purpose = :group_purpose)
          AND (:search IS NULL OR g.group_code LIKE :search OR g.group_name LIKE :search)
        ORDER BY g.group_code
        """
    )


def get_loom_group_row_query():
    """One active group header by id + company (404 / unknown-group guard)."""
    return text(
        """
        SELECT
            g.loom_group_id,
            g.co_id,
            g.branch_id,
            b.branch_name,
            g.group_code,
            g.group_name,
            g.group_purpose,
            g.remarks,
            g.active
        FROM jute_prod_loom_group g
        LEFT JOIN branch_mst b ON b.branch_id = g.branch_id
        WHERE g.loom_group_id = :loom_group_id
          AND g.co_id = :co_id
          AND g.active = 1
        """
    )


def check_loom_group_duplicate_query():
    """Active duplicate guard on (co_id, group_code); :exclude_id skips the row edited."""
    return text(
        """
        SELECT loom_group_id
        FROM jute_prod_loom_group
        WHERE co_id = :co_id
          AND group_code = :group_code
          AND active = 1
          AND (:exclude_id IS NULL OR loom_group_id <> :exclude_id)
        LIMIT 1
        """
    )


def get_loom_group_members_query():
    """Active members of a group with labels; m.active = 0 rows are labelled inactive."""
    return text(
        """
        SELECT
            gd.loom_group_dtl_id,
            gd.machine_id,
            m.machine_name,
            m.mech_code,
            CAST(m.line_no AS CHAR) AS line_no,
            m.active AS machine_active
        FROM jute_prod_loom_group_dtl gd
        INNER JOIN machine_mst m ON m.machine_id = gd.machine_id
        WHERE gd.loom_group_id = :loom_group_id
          AND gd.active = 1
        ORDER BY m.machine_name
        """
    )


def count_loom_group_events_query():
    """Active modification events referencing a group (guarded delete)."""
    return text(
        """
        SELECT COUNT(*) AS cnt
        FROM jute_prod_loom_modification
        WHERE loom_group_id = :loom_group_id
          AND active = 1
        """
    )


def insert_loom_group_query():
    return text(
        """
        INSERT INTO jute_prod_loom_group
            (co_id, branch_id, group_code, group_name, group_purpose, remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :group_code, :group_name, :group_purpose, :remarks, 1, :updated_by)
        """
    )


def update_loom_group_query():
    """COALESCE patch of the header fields (NULL bind = keep the stored value)."""
    return text(
        """
        UPDATE jute_prod_loom_group
        SET branch_id     = COALESCE(:branch_id, branch_id),
            group_code    = COALESCE(:group_code, group_code),
            group_name    = COALESCE(:group_name, group_name),
            group_purpose = COALESCE(:group_purpose, group_purpose),
            remarks       = COALESCE(:remarks, remarks),
            updated_by    = :updated_by
        WHERE loom_group_id = :loom_group_id
          AND active = 1
        """
    )


def soft_delete_loom_group_query():
    return text(
        """
        UPDATE jute_prod_loom_group
        SET active = 0, updated_by = :updated_by
        WHERE loom_group_id = :loom_group_id
        """
    )


def insert_loom_group_member_query():
    return text(
        """
        INSERT INTO jute_prod_loom_group_dtl (loom_group_id, machine_id, active)
        VALUES (:loom_group_id, :machine_id, 1)
        """
    )


def soft_delete_loom_group_members_query():
    return text(
        """
        UPDATE jute_prod_loom_group_dtl
        SET active = 0
        WHERE loom_group_id = :loom_group_id
          AND active = 1
        """
    )


# =============================================================================
# Modification events (§4.3) — reads
# =============================================================================


_EVENT_COLUMNS = """
            lm.modification_id,
            lm.co_id,
            lm.branch_id,
            b.branch_name,
            lm.loom_group_id,
            g.group_code,
            g.group_name,
            lm.mod_date,
            lm.mod_to_date,
            lm.settle_days,
            lm.mod_kind,
            lm.parent_modification_id,
            lm.root_modification_id,
            lm.mod_category,
            lm.title,
            lm.expected_effect,
            lm.done_by,
            lm.party_id,
            p.supp_name AS party_name,
            lm.total_cost,
            lm.remarks,
            lm.active,
            (SELECT COUNT(*) FROM jute_prod_loom_modification_mc mc
              WHERE mc.modification_id = lm.modification_id AND mc.active = 1) AS loom_count,
            (SELECT COUNT(*) FROM jute_prod_loom_modification c
              WHERE c.parent_modification_id = lm.modification_id AND c.active = 1) AS children_count,
            (SELECT COUNT(*) FROM jute_prod_loom_modification s
              WHERE s.root_modification_id = lm.root_modification_id AND s.active = 1) AS stage_count,
            (SELECT COUNT(*) FROM jute_prod_loom_modification s
              WHERE s.root_modification_id = lm.root_modification_id AND s.active = 1
                AND (s.mod_date < lm.mod_date
                     OR (s.mod_date = lm.mod_date AND s.modification_id <= lm.modification_id))) AS stage_no
"""

_EVENT_FROM = """
        FROM jute_prod_loom_modification lm
        LEFT JOIN branch_mst b ON b.branch_id = lm.branch_id
        LEFT JOIN jute_prod_loom_group g ON g.loom_group_id = lm.loom_group_id
        LEFT JOIN party_mst p ON p.party_id = lm.party_id
"""

_TABLE_FILTERS = """
        WHERE lm.active = 1
          AND lm.co_id = :co_id
          AND (:branch_id IS NULL OR lm.branch_id = :branch_id)
          AND (:loom_group_id IS NULL OR lm.loom_group_id = :loom_group_id)
          AND (:date_from IS NULL OR lm.mod_date >= :date_from)
          AND (:date_to IS NULL OR lm.mod_date <= :date_to)
          AND (:mod_kind IS NULL OR lm.mod_kind = :mod_kind)
          AND (:machine_id IS NULL OR EXISTS (
                SELECT 1 FROM jute_prod_loom_modification_mc mc
                 WHERE mc.modification_id = lm.modification_id
                   AND mc.active = 1
                   AND mc.machine_id = :machine_id))
"""


def get_modification_table_query():
    """Paginated register (overhaul_table shape) with stage k of n and per-stage loom count.

    The machine_id filter joins the snapshot (EXISTS). description is TEXT and left to
    /modification_by_id. Ordered newest first; the data check per row comes from
    get_data_check_for_modifications_query on the page's ids.
    """
    return text(
        f"""
        SELECT {_EVENT_COLUMNS}
        {_EVENT_FROM}
        {_TABLE_FILTERS}
        ORDER BY lm.mod_date DESC, lm.modification_id DESC
        LIMIT :limit OFFSET :offset
        """
    )


def get_modification_table_count_query():
    return text(
        f"""
        SELECT COUNT(*) AS total
        FROM jute_prod_loom_modification lm
        {_TABLE_FILTERS}
        """
    )


def get_modification_by_id_query():
    """Full header (with description) for one active event of the company."""
    return text(
        f"""
        SELECT {_EVENT_COLUMNS},
            lm.description
        {_EVENT_FROM}
        WHERE lm.modification_id = :modification_id
          AND lm.co_id = :co_id
          AND lm.active = 1
        """
    )


def get_modification_children_query():
    """Summaries of the active direct children of one event (by_id payload)."""
    return text(
        f"""
        SELECT {_EVENT_COLUMNS}
        {_EVENT_FROM}
        WHERE lm.parent_modification_id = :modification_id
          AND lm.active = 1
        ORDER BY lm.mod_date, lm.modification_id
        """
    )


def get_modification_chain_query():
    """Every active event of a chain in date order (root_modification_id = :root)."""
    return text(
        f"""
        SELECT {_EVENT_COLUMNS}
        {_EVENT_FROM}
        WHERE lm.root_modification_id = :root_modification_id
          AND lm.co_id = :co_id
          AND lm.active = 1
        ORDER BY lm.mod_date, lm.modification_id
        """
    )


def get_chain_latest_stage_query():
    """The chain's latest active non-REVERSAL stage (linearity guard, §5).

    :exclude_id skips the row being edited so it is not its own successor.
    """
    return text(
        """
        SELECT modification_id, title, mod_date, mod_to_date, mod_kind
        FROM jute_prod_loom_modification
        WHERE root_modification_id = :root_modification_id
          AND active = 1
          AND mod_kind <> 'REVERSAL'
          AND (:exclude_id IS NULL OR modification_id <> :exclude_id)
        ORDER BY mod_date DESC, modification_id DESC
        LIMIT 1
        """
    )


def count_modification_children_query():
    """Active direct children of one event + the earliest child start (immutability)."""
    return text(
        """
        SELECT COUNT(*) AS cnt, MIN(mod_date) AS min_mod_date
        FROM jute_prod_loom_modification
        WHERE parent_modification_id = :modification_id
          AND active = 1
        """
    )


def get_chain_reverted_looms_query():
    """Looms whose timeline in the chain is CLOSED before :mod_date (§5): every machine
    carried by an active REVERSAL stage of the chain dated before the new stage.

    A FOLLOWUP / RECTIFY (create, edit, add members) may not include any of them —
    "those looms accept no later stage in this chain". One row per loom with the
    earliest reversal date and that reversal's title for the message.
    """
    return text(
        """
        SELECT
            rm.machine_id,
            m.machine_name,
            m.mech_code,
            MIN(r.mod_date) AS reverted_on,
            SUBSTRING_INDEX(GROUP_CONCAT(r.title ORDER BY r.mod_date, r.modification_id
                                         SEPARATOR '\n'), '\n', 1) AS title
        FROM jute_prod_loom_modification r
        INNER JOIN jute_prod_loom_modification_mc rm ON rm.modification_id = r.modification_id
        INNER JOIN machine_mst m ON m.machine_id = rm.machine_id
        WHERE r.root_modification_id = :root_modification_id
          AND r.active = 1
          AND r.mod_kind = 'REVERSAL'
          AND r.mod_date < :mod_date
          AND rm.active = 1
          AND rm.machine_id IN :machine_ids
        GROUP BY rm.machine_id, m.machine_name, m.mech_code
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


def get_child_reversal_snapshot_query():
    """Union of the active snapshot rows of the active REVERSAL children of one stage.

    Editing that stage may not untick any of these looms (REVERSAL snapshot ⊆ parent
    snapshot is enforced on create / add members, so it must survive an edit too).
    """
    return text(
        """
        SELECT
            rm.machine_id,
            m.machine_name,
            m.mech_code,
            MIN(r.title) AS title
        FROM jute_prod_loom_modification r
        INNER JOIN jute_prod_loom_modification_mc rm ON rm.modification_id = r.modification_id
        INNER JOIN machine_mst m ON m.machine_id = rm.machine_id
        WHERE r.parent_modification_id = :modification_id
          AND r.active = 1
          AND r.mod_kind = 'REVERSAL'
          AND rm.active = 1
        GROUP BY rm.machine_id, m.machine_name, m.mech_code
        """
    )


def get_open_stages_query():
    """Every active non-REVERSAL stage of the company (optionally one branch) with
    ``snapshot_count`` and ``unreverted_count`` — the "Follows" picker feed (§5).

    A loom of stage s is reverted when an active REVERSAL of the same chain, dated
    after s, carries that loom in its own snapshot. The router keeps, per chain, the
    latest stage and lists it in ``open_events`` when unreverted_count > 0;
    ``all_stages`` is the whole list (a REVERSAL may name any non-REVERSAL stage).
    """
    return text(
        """
        SELECT
            lm.modification_id,
            lm.root_modification_id,
            lm.parent_modification_id,
            lm.branch_id,
            lm.loom_group_id,
            g.group_code,
            lm.mod_date,
            lm.mod_to_date,
            lm.mod_kind,
            lm.title,
            (SELECT COUNT(*) FROM jute_prod_loom_modification_mc mc
              WHERE mc.modification_id = lm.modification_id AND mc.active = 1) AS snapshot_count,
            (SELECT COUNT(*) FROM jute_prod_loom_modification_mc mc
              WHERE mc.modification_id = lm.modification_id
                AND mc.active = 1
                AND NOT EXISTS (
                    SELECT 1
                    FROM jute_prod_loom_modification r
                    INNER JOIN jute_prod_loom_modification_mc rm ON rm.modification_id = r.modification_id
                    WHERE r.root_modification_id = lm.root_modification_id
                      AND r.active = 1
                      AND r.mod_kind = 'REVERSAL'
                      AND r.mod_date > lm.mod_date
                      AND rm.active = 1
                      AND rm.machine_id = mc.machine_id)) AS unreverted_count
        FROM jute_prod_loom_modification lm
        LEFT JOIN jute_prod_loom_group g ON g.loom_group_id = lm.loom_group_id
        WHERE lm.active = 1
          AND lm.co_id = :co_id
          AND lm.mod_kind <> 'REVERSAL'
          AND (:branch_id IS NULL OR lm.branch_id = :branch_id)
        ORDER BY lm.root_modification_id, lm.mod_date, lm.modification_id
        """
    )


# =============================================================================
# Modification events — writes
# =============================================================================


def insert_modification_query():
    return text(
        """
        INSERT INTO jute_prod_loom_modification
            (co_id, branch_id, loom_group_id, mod_date, mod_to_date, settle_days, mod_kind,
             parent_modification_id, root_modification_id, mod_category, title, description,
             expected_effect, done_by, party_id, total_cost, remarks, active, updated_by)
        VALUES
            (:co_id, :branch_id, :loom_group_id, :mod_date, :mod_to_date, :settle_days, :mod_kind,
             :parent_modification_id, :root_modification_id, :mod_category, :title, :description,
             :expected_effect, :done_by, :party_id, :total_cost, :remarks, 1, :updated_by)
        """
    )


def stamp_root_modification_query():
    """INITIAL rows: root = own id, known only after the insert."""
    return text(
        """
        UPDATE jute_prod_loom_modification
        SET root_modification_id = :modification_id
        WHERE modification_id = :modification_id
        """
    )


def update_modification_query():
    """Full replace of the header (edit carries the whole create body)."""
    return text(
        """
        UPDATE jute_prod_loom_modification
        SET branch_id              = :branch_id,
            loom_group_id          = :loom_group_id,
            mod_date               = :mod_date,
            mod_to_date            = :mod_to_date,
            settle_days            = :settle_days,
            mod_kind               = :mod_kind,
            parent_modification_id = :parent_modification_id,
            root_modification_id   = :root_modification_id,
            mod_category           = :mod_category,
            title                  = :title,
            description            = :description,
            expected_effect        = :expected_effect,
            done_by                = :done_by,
            party_id               = :party_id,
            total_cost             = :total_cost,
            remarks                = :remarks,
            updated_by             = :updated_by
        WHERE modification_id = :modification_id
          AND active = 1
        """
    )


def set_modification_group_query():
    """Stamp loom_group_id after "Save these looms as a group" on create."""
    return text(
        """
        UPDATE jute_prod_loom_modification
        SET loom_group_id = :loom_group_id, updated_by = :updated_by
        WHERE modification_id = :modification_id
        """
    )


def soft_delete_modification_query():
    return text(
        """
        UPDATE jute_prod_loom_modification
        SET active = 0, updated_by = :updated_by
        WHERE modification_id = :modification_id
        """
    )


def soft_delete_chain_query():
    """Delete chain: every active event with root = :root (children first is moot — one UPDATE)."""
    return text(
        """
        UPDATE jute_prod_loom_modification
        SET active = 0, updated_by = :updated_by
        WHERE root_modification_id = :root_modification_id
          AND active = 1
        """
    )


def rewrite_chain_root_query():
    """Insert earlier stage: move every event of the old chain under the new root."""
    return text(
        """
        UPDATE jute_prod_loom_modification
        SET root_modification_id = :new_root
        WHERE root_modification_id = :old_root
        """
    )


def reparent_old_root_query():
    """Insert earlier stage: the old root becomes a FOLLOWUP / RECTIFY of the new INITIAL."""
    return text(
        """
        UPDATE jute_prod_loom_modification
        SET parent_modification_id = :new_root,
            root_modification_id   = :new_root,
            mod_kind               = :mod_kind,
            updated_by             = :updated_by
        WHERE modification_id = :old_root
        """
    )


# =============================================================================
# Snapshot (§4.4)
# =============================================================================


def get_snapshot_members_query():
    """Active snapshot rows of one event with loom labels and the resolved D_m / E_m."""
    return text(
        """
        SELECT
            mc.modification_mc_id,
            mc.modification_id,
            mc.machine_id,
            m.machine_name,
            m.mech_code,
            CAST(m.line_no AS CHAR) AS line_no,
            m.active AS machine_active,
            mc.done_date,
            mc.done_to_date,
            mc.remarks,
            COALESCE(mc.done_date, lm.mod_date) AS d_m,
            COALESCE(mc.done_to_date, mc.done_date, lm.mod_to_date, lm.mod_date) AS e_m
        FROM jute_prod_loom_modification_mc mc
        INNER JOIN jute_prod_loom_modification lm ON lm.modification_id = mc.modification_id
        INNER JOIN machine_mst m ON m.machine_id = mc.machine_id
        WHERE mc.modification_id = :modification_id
          AND mc.active = 1
        ORDER BY m.machine_name
        """
    )


def get_chain_snapshot_members_query():
    """Active snapshot rows of every active event of a chain (modification_chain payload)."""
    return text(
        """
        SELECT
            mc.modification_mc_id,
            mc.modification_id,
            mc.machine_id,
            m.machine_name,
            m.mech_code,
            m.active AS machine_active,
            mc.done_date,
            mc.done_to_date,
            mc.remarks,
            COALESCE(mc.done_date, lm.mod_date) AS d_m,
            COALESCE(mc.done_to_date, mc.done_date, lm.mod_to_date, lm.mod_date) AS e_m
        FROM jute_prod_loom_modification_mc mc
        INNER JOIN jute_prod_loom_modification lm ON lm.modification_id = mc.modification_id
        INNER JOIN machine_mst m ON m.machine_id = mc.machine_id
        WHERE lm.root_modification_id = :root_modification_id
          AND lm.active = 1
          AND mc.active = 1
        ORDER BY lm.mod_date, lm.modification_id, m.machine_name
        """
    )


def get_snapshot_machine_ids_query():
    """Machine ids of one event's active snapshot (subset rule, newly-ticked diff)."""
    return text(
        """
        SELECT machine_id
        FROM jute_prod_loom_modification_mc
        WHERE modification_id = :modification_id
          AND active = 1
        """
    )


def get_loom_spans_query():
    """Every active per-loom span [D_m, E_m] of the given looms across ALL chains.

    Feeds the per-loom overlap check (§5): the router compares each new span with
    these in Python. :exclude_id skips the event being edited.
    """
    return text(
        """
        SELECT
            mc.machine_id,
            m.machine_name,
            lm.modification_id,
            lm.title,
            lm.mod_kind,
            COALESCE(mc.done_date, lm.mod_date) AS d_m,
            COALESCE(mc.done_to_date, mc.done_date, lm.mod_to_date, lm.mod_date) AS e_m
        FROM jute_prod_loom_modification_mc mc
        INNER JOIN jute_prod_loom_modification lm ON lm.modification_id = mc.modification_id
        INNER JOIN machine_mst m ON m.machine_id = mc.machine_id
        WHERE mc.active = 1
          AND lm.active = 1
          AND lm.co_id = :co_id
          AND mc.machine_id IN :machine_ids
          AND (:exclude_id IS NULL OR lm.modification_id <> :exclude_id)
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


def get_machine_timeline_query():
    """All active snapshot rows of one loom in date order (stage boundaries)."""
    return text(
        """
        SELECT
            mc.modification_mc_id,
            lm.modification_id,
            lm.root_modification_id,
            lm.parent_modification_id,
            lm.mod_kind,
            lm.mod_category,
            lm.title,
            lm.mod_date,
            lm.mod_to_date,
            lm.settle_days,
            lm.loom_group_id,
            g.group_code,
            mc.done_date,
            mc.done_to_date,
            mc.remarks,
            COALESCE(mc.done_date, lm.mod_date) AS d_m,
            COALESCE(mc.done_to_date, mc.done_date, lm.mod_to_date, lm.mod_date) AS e_m
        FROM jute_prod_loom_modification_mc mc
        INNER JOIN jute_prod_loom_modification lm ON lm.modification_id = mc.modification_id
        LEFT JOIN jute_prod_loom_group g ON g.loom_group_id = lm.loom_group_id
        WHERE mc.active = 1
          AND lm.active = 1
          AND lm.co_id = :co_id
          AND mc.machine_id = :machine_id
        ORDER BY d_m, lm.modification_id
        """
    )


def insert_snapshot_member_query():
    return text(
        """
        INSERT INTO jute_prod_loom_modification_mc
            (modification_id, machine_id, done_date, done_to_date, remarks, active)
        VALUES
            (:modification_id, :machine_id, :done_date, :done_to_date, :remarks, 1)
        """
    )


def soft_delete_snapshot_query():
    return text(
        """
        UPDATE jute_prod_loom_modification_mc
        SET active = 0
        WHERE modification_id = :modification_id
          AND active = 1
        """
    )


def soft_delete_chain_snapshots_query():
    """Delete chain: the snapshot rows of every event with root = :root."""
    return text(
        """
        UPDATE jute_prod_loom_modification_mc mc
        INNER JOIN jute_prod_loom_modification lm ON lm.modification_id = mc.modification_id
        SET mc.active = 0
        WHERE lm.root_modification_id = :root_modification_id
          AND mc.active = 1
        """
    )


# =============================================================================
# Data check (§6.2 save feedback) — producing loom-days around mod_date
# =============================================================================

_PRODUCING = "wd.active = 1 AND (COALESCE(wd.cuts, 0) > 0 OR COALESCE(wd.close_jugar, 0) > 0)"


def get_data_check_query():
    """Producing loom-days (distinct loom x date, cuts > 0 or close_jugar > 0) of the
    given looms in the 90 days before and after :mod_date — one COUNT per save."""
    return text(
        f"""
        SELECT
            COUNT(DISTINCT CASE WHEN wd.tran_date < :mod_date
                                THEN CONCAT(wd.machine_id, '-', wd.tran_date) END) AS before_days,
            COUNT(DISTINCT CASE WHEN wd.tran_date > :mod_date
                                THEN CONCAT(wd.machine_id, '-', wd.tran_date) END) AS after_days
        FROM jute_prod_weaving_daily wd
        WHERE wd.co_id = :co_id
          AND {_PRODUCING}
          AND wd.machine_id IN :machine_ids
          AND wd.tran_date BETWEEN DATE_SUB(:mod_date, INTERVAL 90 DAY)
                               AND DATE_ADD(:mod_date, INTERVAL 90 DAY)
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


def get_data_check_for_modifications_query():
    """The same figure for a page of register rows, keyed by modification_id."""
    return text(
        f"""
        SELECT
            lm.modification_id,
            COUNT(DISTINCT CASE WHEN wd.tran_date < lm.mod_date
                                THEN CONCAT(wd.machine_id, '-', wd.tran_date) END) AS before_days,
            COUNT(DISTINCT CASE WHEN wd.tran_date > lm.mod_date
                                THEN CONCAT(wd.machine_id, '-', wd.tran_date) END) AS after_days
        FROM jute_prod_loom_modification lm
        INNER JOIN jute_prod_loom_modification_mc mc
                ON mc.modification_id = lm.modification_id AND mc.active = 1
        INNER JOIN jute_prod_weaving_daily wd
                ON wd.co_id = lm.co_id
               AND wd.machine_id = mc.machine_id
               AND wd.tran_date BETWEEN DATE_SUB(lm.mod_date, INTERVAL 90 DAY)
                                    AND DATE_ADD(lm.mod_date, INTERVAL 90 DAY)
        WHERE lm.modification_id IN :modification_ids
          AND {_PRODUCING}
        GROUP BY lm.modification_id
        """
    ).bindparams(bindparam("modification_ids", expanding=True))
