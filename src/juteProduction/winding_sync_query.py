"""Raw SQL for the Winding "Sync from attendance" (TKT-000171).

Winding freezes nothing: every figure on the Winding Production page and in its
reports is computed at read time from jute_prod_winding_doff +
jute_prod_winding_jugar (winding_models.py: "Reconciled production is computed
at READ time, never persisted"), so there is no log to re-process — unlike
spinning's jute_prod_spinning_daily or weaving's jute_prod_weaving_log. The ONE
stored, attendance-shaped structure is the person -> quality map
(jute_prod_winding_daily_qlty): who winds in this (branch, date, spell), on
which machine, on which yarn. It is seeded by carrying the previous spell
forward, so it goes stale the moment attendance says otherwise (sls branch 29,
15-18 Sep 2026: the map's machine disagreed with the attendance machine on up to
16 of 26 B-spell rows). These queries read the unit's attendance so
winding_sync.py can reconcile the map against it.

Scope key is (branch_id, tran_date, spell_id) — branch, never co_id, like every
other winding read (winding-person-keyed-entry-spec.md §4.2b). Attendance
predicates match the sibling readers (lowprod_winding_query /
reportQueries._ATTENDANCE_AGG): is_active = 1, not REJECTED (status_id 4),
joined on spell_id never spell_code (the code repeats once per shift
generation on sls: 'A1' = 91/97/102).

Deliberately NO designation gate anywhere: on sls a LIKE '%WIND%' gate pulls in
'WINDING FITTER', 'SWEEPER WINDING', 'WINDING MAZDOOR', 'WINDING OILER' ... —
none of whom ever wound (checked 17 Sep 2026, 0 of 13 had a map row or a doff
in the prior 30 days). A winder is identified by the two things that are
certain: attendance marks them on a Winding-type machine, or they have doffs.
"""

from sqlalchemy import bindparam, text

# Attendance HEADER predicates for one (branch, date, spell) unit. Aliased
# ``da`` everywhere so the fragment can sit inside a JOIN ... ON as well as a
# WHERE. CAST on status_id: the column is VARCHAR (same guard as lowprod).
_ATT_UNIT_WHERE = """
              da.attendance_date = :tran_date
          AND da.spell_id = :spell_id
          AND da.branch_id = :branch_id
          AND da.is_active = 1
          AND da.eb_id IS NOT NULL
          AND (da.status_id IS NULL OR CAST(da.status_id AS UNSIGNED) <> 4)
"""

# emp_code the way every winding read resolves it (one active HRMS official
# row per winder; NULL when the record is gone, never a dropped row).
_EMP_CODE_SUBSELECT = """
           (SELECT NULLIF(TRIM(o.emp_code), '')
              FROM hrms_ed_official_details o
             WHERE o.eb_id = {alias}.eb_id AND o.active = 1
             LIMIT 1) AS emp_code
"""


def get_winding_sync_attendance_query():
    """One row per winder with active, non-rejected attendance in the unit.

    ``att_rows`` > 1 means duplicate attendance keys (summed, not collapsed,
    by the sibling checklists — disclosed here, never hidden).

    Binds: :branch_id, :tran_date, :spell_id.
    """
    return text(
        f"""
        SELECT da.eb_id,
               MIN(da.daily_atten_id) AS daily_atten_id,
               COUNT(*) AS att_rows
        FROM daily_attendance da
        WHERE {_ATT_UNIT_WHERE}
        GROUP BY da.eb_id
        """
    )


def get_winding_sync_attendance_machines_query():
    """The Winding-type machine(s) the unit's attendance marks each winder on.

    daily_ebmc_attendance joined to the attendance HEADER by daily_atten_id —
    date/spell/branch predicates live on the header, never on dea.branch_id
    (mobile leaves it NULL; spinning_query.get_operator_candidates_query).
    Only Winding-type machines of THIS branch (the same set the Quality tab's
    machine picker offers — get_winding_machines_query), so a winder marked on
    a spinning frame counts as "no attendance machine", not as a wrong one.

    Ordered first-marked first (dtl_rec_id) so the caller can pick a
    deterministic winner when one winder is marked on two machines.

    Binds: :branch_id, :tran_date, :spell_id, :machine_type_name.
    """
    return text(
        f"""
        SELECT da.eb_id,
               dea.mc_id AS machine_id,
               m.mech_code,
               m.machine_name,
               dea.dtl_rec_id
        FROM daily_ebmc_attendance dea
        INNER JOIN daily_attendance da
                ON da.daily_atten_id = dea.daily_atten_id
               AND {_ATT_UNIT_WHERE}
        INNER JOIN machine_mst m
                ON m.machine_id = dea.mc_id AND m.active = 1
        INNER JOIN machine_type_mst mt
                ON mt.machine_type_id = m.machine_type_id
               AND mt.active = 1
               AND mt.machine_type_name = :machine_type_name
        INNER JOIN dept_mst d
                ON d.dept_id = m.dept_id AND d.branch_id = :branch_id
        WHERE dea.is_active = 1
        ORDER BY da.eb_id, dea.dtl_rec_id
        """
    )


def get_winding_sync_map_query():
    """The unit's active person -> quality map rows with worker + machine labels.

    LEFT joins (worker, machine) so a row whose HRMS record or machine went away
    still takes part in the reconciliation instead of silently vanishing.

    Binds: :branch_id, :tran_date, :spell_id.
    """
    return text(
        f"""
        SELECT q.winding_daily_qlty_id,
               q.eb_id,
               q.machine_id,
               q.item_id,
               q.no_of_spindle,
               {_EMP_CODE_SUBSELECT.format(alias="q")},
               TRIM(CONCAT_WS(' ', p.first_name, p.middle_name, p.last_name)) AS worker_name,
               m.mech_code,
               m.machine_name
        FROM jute_prod_winding_daily_qlty q
        LEFT JOIN hrms_ed_personal_details p ON p.eb_id = q.eb_id
        LEFT JOIN machine_mst m ON m.machine_id = q.machine_id
        WHERE q.branch_id = :branch_id
          AND q.tran_date = :tran_date
          AND q.spell_id = :spell_id
          AND q.eb_id IS NOT NULL
          AND (q.active = 1 OR q.active IS NULL)
        ORDER BY q.winding_daily_qlty_id
        """
    )


def get_winding_sync_doff_winders_query():
    """Winders with active doffs in the unit (+ the yarn they doffed, doff count).

    MAX(item_id): the doff form prefills from the map and the row's own yarn is
    authoritative for production, so one winder-spell practically carries one
    yarn; MAX only picks a deterministic value if that ever differs.

    Binds: :branch_id, :tran_date, :spell_id.
    """
    return text(
        """
        SELECT eb_id,
               MAX(item_id) AS item_id,
               COUNT(*) AS doffs
        FROM jute_prod_winding_doff
        WHERE branch_id = :branch_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND eb_id IS NOT NULL
          AND (active = 1 OR active IS NULL)
        GROUP BY eb_id
        """
    )


def get_winding_sync_worker_labels_query():
    """emp_code + worker_name for a list of winders (added-row display).

    Binds: :eb_ids (expanding, non-empty).
    """
    return text(
        f"""
        SELECT p.eb_id,
               {_EMP_CODE_SUBSELECT.format(alias="p")},
               TRIM(CONCAT_WS(' ', p.first_name, p.middle_name, p.last_name)) AS worker_name
        FROM hrms_ed_personal_details p
        WHERE p.eb_id IN :eb_ids
        """
    ).bindparams(bindparam("eb_ids", expanding=True))


def get_winding_sync_last_map_query():
    """Each winder's most recent EARLIER map row in the branch.

    The yarn + spindle count to carry onto a row Sync adds — the same inheritance
    the "Get Quality" carry-forward seed gives a winder (auto_seed_quality_query),
    just resolved per person instead of per previous spell. "Earlier" = a prior
    date, or the same date in another spell; ordered like
    get_quality_prev_seed_date_spell_query (tran_date DESC, spell_id DESC).

    Binds: :branch_id, :tran_date, :spell_id, :eb_ids (expanding, non-empty).
    """
    return text(
        """
        SELECT r.eb_id, r.item_id, r.no_of_spindle
        FROM (
            SELECT q.eb_id, q.item_id, q.no_of_spindle,
                   ROW_NUMBER() OVER (
                       PARTITION BY q.eb_id
                       ORDER BY q.tran_date DESC, q.spell_id DESC, q.winding_daily_qlty_id DESC
                   ) AS rn
            FROM jute_prod_winding_daily_qlty q
            WHERE q.branch_id = :branch_id
              AND q.eb_id IN :eb_ids
              AND (q.active = 1 OR q.active IS NULL)
              AND (q.tran_date < :tran_date
                   OR (q.tran_date = :tran_date AND q.spell_id <> :spell_id))
        ) r
        WHERE r.rn = 1
        """
    ).bindparams(bindparam("eb_ids", expanding=True))


def get_winding_sync_other_spell_attendance_query():
    """For map winders ABSENT in the unit: the other spell(s) they attended that
    day in the branch — "on the map for A, marked present in B" is a wrong-spell
    map row, not an absence, and the user should see the difference.

    Binds: :branch_id, :tran_date, :spell_id, :eb_ids (expanding, non-empty).
    """
    return text(
        """
        SELECT da.eb_id, sp.spell_code
        FROM daily_attendance da
        LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
        WHERE da.attendance_date = :tran_date
          AND da.branch_id = :branch_id
          AND da.spell_id <> :spell_id
          AND da.is_active = 1
          AND da.eb_id IN :eb_ids
          AND (da.status_id IS NULL OR CAST(da.status_id AS UNSIGNED) <> 4)
        GROUP BY da.eb_id, sp.spell_code
        ORDER BY da.eb_id, sp.spell_code
        """
    ).bindparams(bindparam("eb_ids", expanding=True))


def update_winding_sync_map_machine_query():
    """Stamp the attendance machine onto one active map row (fill or force).

    Same shape as update_quality_row_query, machine column only — yarn and
    spindle count are never touched by Sync.

    Binds: :id, :machine_id, :updated_by.
    """
    return text(
        """
        UPDATE jute_prod_winding_daily_qlty
        SET machine_id = :machine_id,
            updated_by = :updated_by
        WHERE winding_daily_qlty_id = :id
          AND (active = 1 OR active IS NULL)
        """
    )
