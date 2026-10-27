"""Raw SQL for the Low Performer Follow-up (src/juteProduction/low_performer_followup.py).

Only the follow-up register itself lives here (list with timeline counts, guards,
timeline with attachment + spell labels). Every production / efficiency figure
comes from the Low Producer modules' shared fetch helpers — never re-derived here.

Reused rather than copied:
  * co/branch pair guard  -> lowprod_weaving_query.get_branch_in_company_query
  * EB no -> employee      -> hrms.leaveRequest.get_worker_by_eb_no_query
  * branch spells + hours  -> spinning_query.get_spells_query
  * emp_code / worker name -> reportQueries._emp_code_expr / _WORKER_NAME
  * menu access level      -> approval_query.get_user_edit_access (router)
"""

from sqlalchemy import text

from src.juteProduction.reportQueries import _WORKER_NAME, _emp_code_expr

ATTACHMENT_MODULE = "jute_prod_followup"
ATTACHMENT_FILE_TYPE = "notice"


def get_followup_rows_query():
    """Follow-up headers + timeline counts, one row per follow-up.

    Binds: :co_id (required), :branch_id, :dept_code, :status, :followup_id
    (each nullable = not filtered). eb_no is the scalar emp_code subquery (never a
    join — hrms_ed_official_details holds one row per branch transfer). The event
    roll-up only reads events of this company's follow-ups.
    """
    return text(
        f"""
        SELECT f.followup_id, f.co_id, f.branch_id, f.eb_id,
               {_emp_code_expr('f')} AS eb_no,
               NULLIF({_WORKER_NAME}, '') AS worker_name,
               f.dept_code, f.reason, f.start_date, f.review_date, f.status,
               f.closed_date, f.close_reason, f.close_remarks, f.updated_date_time,
               COALESCE(ev.remark_count, 0) AS remark_count,
               COALESCE(ev.notice_count, 0) AS notice_count,
               COALESCE(ev.hours_cut_count, 0) AS hours_cut_count,
               COALESCE(ev.total_hours_cut, 0) AS total_hours_cut,
               ev.last_event_date,
               (SELECT e2.notice_type
                  FROM jute_prod_followup_event e2
                 WHERE e2.followup_id = f.followup_id AND e2.active = 1
                   AND e2.event_type = 'NOTICE'
                 ORDER BY e2.event_date DESC, e2.followup_event_id DESC
                 LIMIT 1) AS last_notice_type
        FROM jute_prod_followup_person f
        LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = f.eb_id
        LEFT JOIN (
            SELECT e.followup_id,
                   SUM(e.event_type = 'REMARK') AS remark_count,
                   SUM(e.event_type = 'NOTICE') AS notice_count,
                   SUM(e.event_type = 'HOURS_CUT') AS hours_cut_count,
                   SUM(CASE WHEN e.event_type = 'HOURS_CUT'
                            THEN COALESCE(e.hours_cut, 0) ELSE 0 END) AS total_hours_cut,
                   MAX(e.event_date) AS last_event_date
            FROM jute_prod_followup_event e
            INNER JOIN jute_prod_followup_person fp
                    ON fp.followup_id = e.followup_id AND fp.co_id = :co_id
            WHERE e.active = 1
            GROUP BY e.followup_id
        ) ev ON ev.followup_id = f.followup_id
        WHERE f.active = 1
          AND f.co_id = :co_id
          AND (:branch_id IS NULL OR f.branch_id = :branch_id)
          AND (:dept_code IS NULL OR f.dept_code = :dept_code)
          AND (:status IS NULL OR f.status = :status)
          AND (:followup_id IS NULL OR f.followup_id = :followup_id)
        ORDER BY f.status DESC, f.start_date DESC, f.followup_id DESC
        """
    )


def get_open_duplicate_query():
    """Another OPEN active follow-up of the same person + branch + dept (the
    one-open-follow-up guard; the DB backs it with the functional unique key
    uq_jpfp_open, see 20261007b_jute_prod_followup_open_unique.sql).
    :exclude_id skips the row being reopened."""
    return text(
        """
        SELECT followup_id
        FROM jute_prod_followup_person
        WHERE co_id = :co_id AND branch_id = :branch_id AND eb_id = :eb_id
          AND dept_code = :dept_code AND status = 1 AND active = 1
          AND (:exclude_id IS NULL OR followup_id <> :exclude_id)
        LIMIT 1
        """
    )


def get_employee_in_branch_query():
    """The eb_id has an active official-details row in the branch (an eb_id sent
    directly, e.g. from a Low Producer row, gets the same check as an EB no)."""
    return text(
        """
        SELECT o.eb_id, NULLIF(TRIM(o.emp_code), '') AS eb_no
        FROM hrms_ed_official_details o
        WHERE o.eb_id = :eb_id AND o.branch_id = :branch_id AND o.active = 1
        ORDER BY o.tbl_hrms_ed_official_detail_id DESC
        LIMIT 1
        """
    )


def get_earliest_event_date_query():
    """First active timeline date — the start date may not move past it."""
    return text(
        """
        SELECT MIN(event_date) AS first_date
        FROM jute_prod_followup_event
        WHERE followup_id = :followup_id AND active = 1
        """
    )


def get_latest_event_date_query():
    """Last active timeline date — a follow-up may not close before it."""
    return text(
        """
        SELECT MAX(event_date) AS last_date
        FROM jute_prod_followup_event
        WHERE followup_id = :followup_id AND active = 1
        """
    )


def get_followup_menu_id_query():
    """menu_id of the follow-up page (exact path, active) — the menu whose role
    grants gate every endpoint. Binds: :menu_path."""
    return text(
        """
        SELECT menu_id
        FROM menu_mst
        WHERE active = 1 AND TRIM(BOTH '/' FROM menu_path) = :menu_path
        ORDER BY menu_id
        LIMIT 1
        """
    )


def get_hours_cut_spells_query():
    """The branch's active spells with their own hours AND their shift's hours —
    the hour-cut caps (one spell; all spells of a shift letter). Binds: :branch_id."""
    return text(
        """
        SELECT sp.spell_id, sp.spell_code, sp.working_hours,
               sh.working_hours AS shift_hours
        FROM spell_mst sp
        INNER JOIN shift_mst sh ON sh.shift_id = sp.shift_id
        WHERE sp.status = 1 AND sh.status = 1 AND sh.branch_id = :branch_id
        """
    )


def get_person_hours_cuts_query():
    """Every active hour cut of the PERSON (eb_id) on one date, across all of their
    follow-ups (any department / branch), with the spell code, excluding the row
    being edited. The new cut plus these must fit the spell, the shift and the day."""
    return text(
        """
        SELECT e.followup_event_id, e.spell_id, sp.spell_code, e.hours_cut
        FROM jute_prod_followup_event e
        INNER JOIN jute_prod_followup_person f ON f.followup_id = e.followup_id
        LEFT JOIN spell_mst sp ON sp.spell_id = e.spell_id
        WHERE f.eb_id = :eb_id AND f.active = 1
          AND e.event_type = 'HOURS_CUT' AND e.active = 1
          AND e.event_date = :event_date
          AND (:exclude_id IS NULL OR e.followup_event_id <> :exclude_id)
        """
    )


def get_notice_event_scope_query():
    """A timeline row with its follow-up's company / branch / state — the gate of
    the generic attachment API for module 'jute_prod_followup'
    (entity_id = followup_event_id). Binds: :followup_event_id."""
    return text(
        """
        SELECT e.followup_event_id, e.event_type, e.active AS event_active,
               f.followup_id, f.co_id, f.branch_id, f.status, f.active AS followup_active
        FROM jute_prod_followup_event e
        INNER JOIN jute_prod_followup_person f ON f.followup_id = e.followup_id
        WHERE e.followup_event_id = :followup_event_id
        """
    )


def get_events_query():
    """Active timeline rows of one follow-up, newest first, with the spell code and
    the latest active notice scan (generic file_attachment, company-scoped).

    Binds: :followup_id, :co_id, :module, :file_type, :followup_event_id (nullable
    = all events of the follow-up)."""
    return text(
        """
        SELECT e.followup_event_id, e.followup_id, e.event_type, e.event_date,
               e.notice_type, e.issued_by, e.spell_id, sp.spell_code, e.hours_cut,
               e.remarks, e.updated_by, e.updated_date_time,
               fa.attachment_id, fa.original_filename AS attachment_filename
        FROM jute_prod_followup_event e
        LEFT JOIN spell_mst sp ON sp.spell_id = e.spell_id
        LEFT JOIN (
            SELECT a.entity_id, MAX(a.attachment_id) AS attachment_id
            FROM file_attachment a
            WHERE a.module = :module AND a.file_type = :file_type
              AND a.co_id = :co_id AND a.active = 1
              AND a.entity_id IN (SELECT x.followup_event_id
                                    FROM jute_prod_followup_event x
                                   WHERE x.followup_id = :followup_id)
            GROUP BY a.entity_id
        ) fa1 ON fa1.entity_id = e.followup_event_id
        LEFT JOIN file_attachment fa ON fa.attachment_id = fa1.attachment_id
        WHERE e.followup_id = :followup_id AND e.active = 1
          AND (:followup_event_id IS NULL OR e.followup_event_id = :followup_event_id)
        ORDER BY e.event_date DESC, e.followup_event_id DESC
        """
    )


def get_winding_machine_by_winder_range_query():
    """winding_query.get_winding_machine_by_winder_query over :date_from .. :date_to
    in ONE round trip (that one is single-date; looping it cost one round trip per
    day of the window).

    Same source (the person -> quality map's Machine column), same filters, same
    within-date order (winding_daily_qlty_id), plus tran_date so the caller can
    key rows by (date, spell_id, eb_id). Map rows without a machine are left out
    here: the caller skips them anyway ("first row that names a machine wins").

    Binds: :branch_id (required), :date_from, :date_to.
    """
    return text(
        """
        SELECT
            q.tran_date,
            q.spell_id,
            q.eb_id,
            q.machine_id,
            m.mech_code,
            m.machine_name
        FROM jute_prod_winding_daily_qlty q
        LEFT JOIN machine_mst m ON m.machine_id = q.machine_id
        WHERE q.branch_id = :branch_id
          AND q.tran_date BETWEEN :date_from AND :date_to
          AND q.eb_id IS NOT NULL
          AND q.machine_id IS NOT NULL
          AND (q.active = 1 OR q.active IS NULL)
        ORDER BY q.tran_date, q.winding_daily_qlty_id
        """
    )


def get_tracked_query():
    """OPEN follow-ups of a branch + dept — the Low Producer pages' badge."""
    return text(
        """
        SELECT followup_id, eb_id, dept_code, start_date, review_date
        FROM jute_prod_followup_person
        WHERE co_id = :co_id AND branch_id = :branch_id AND dept_code = :dept_code
          AND status = 1 AND active = 1
        ORDER BY followup_id
        """
    )
