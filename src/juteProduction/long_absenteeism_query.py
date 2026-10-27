"""Raw SQL for the Long Absenteeism cases router (src/juteProduction/long_absenteeism.py).

Every query is a ``text()`` factory with named binds (listed in each docstring).
Spec: docs/superpowers/specs/2026-10-11-long-absenteeism-followup.md (sections 4.1-4.6).

Reused rather than copied:
  * separated lifecycle statuses -> hrms.reportQueries._SEPARATED_STATUS_SQL
  * worker display name          -> juteProduction.reportQueries._WORKER_NAME (alias ``pd``)
  * attendance "rejected" status -> hands_report_query.ATTENDANCE_STATUS_REJECTED
    (``daily_attendance.status_id`` is VARCHAR, hence the CAST)

hrms_ed_official_details keeps several rows per eb after a branch transfer, so the
row is always picked by a scalar MAX / ORDER BY ... LIMIT 1, never a bare join.
"""

from sqlalchemy import text

from src.hrms.reportQueries import _SEPARATED_STATUS_SQL
from src.juteProduction.hands_report_query import ATTENDANCE_STATUS_REJECTED
from src.juteProduction.reportQueries import _WORKER_NAME

EVENT_ATTACHMENT_MODULE = "jute_prod_absent_event"
LETTER_ATTACHMENT_MODULE = "jute_prod_absent_letter"
LETTER_ATTACHMENT_TYPES = ("signed_copy", "ad_card")
RECEIPT_ATTACHMENT_MODULE = "jute_prod_absent_receipt"
RECEIPT_ATTACHMENT_TYPE = "union_receipt"

# An attendance row that counts as "seen": active and not rejected.
_DA_SEEN = (
    "da.is_active = 1 AND COALESCE(CAST(da.status_id AS UNSIGNED), 0) "
    f"<> {ATTENDANCE_STATUS_REJECTED}"
)

# Third leg of the "seen" union (TKT-000223): an APPROVED leave (status 3) counts
# as seen through its last day, capped at :as_on, once it has started. It reads
# the leave HEADER (leave_transactions.leave_from_date / leave_to_date), not only
# the per-day leave_tran_details rows of the second leg: 15 of 5295 approved sls
# leaves have no detail rows at all, so a worker on such a leave would otherwise
# look absent (and a worker on leave today would be listed as a candidate).
# Shared by get_candidates_query (+ the optional look-back) and
# get_last_seen_query (+ :eb_id). Binds: :branch_id, :as_on.
_LEAVE_HEADER_SEEN = (
    "lt.status = 3 AND lt.branch_id = :branch_id "
    "AND lt.leave_from_date <= :as_on AND lt.leave_to_date IS NOT NULL"
)

# The worker's display name of a leave type: description, code when blank.
_LEAVE_TYPE_NAME = "COALESCE(NULLIF(TRIM(lvm.leave_type_description), ''), lvm.leave_type_code)"


def _pending_leave_sql(eb_expr: str, day_bind: str) -> str:
    """Pending-leave hint (TKT-000223, informational only, never stored): the
    earliest update date of the worker's leave REQUESTS still pending approval
    (leave_transactions.status 20) that cover ``day_bind``; NULL when none. Only
    approved leaves (status 3) ever park a case or hide a worker."""
    return (
        "(SELECT MIN(DATE(l3.updated_date_time)) FROM leave_transactions l3 "
        f"WHERE l3.eb_id = {eb_expr} AND l3.status = 20 "
        f"AND l3.leave_from_date <= {day_bind} AND l3.leave_to_date >= {day_bind})"
    )


# =============================================================================
# Candidates (4.1)
# =============================================================================


def get_candidates_query():
    """Workers on the roll seen (attendance or approved leave day) but not in the
    last ``detect_days``, with no live case, no snoozed dismissal and no ESI /
    STL / maternity / medical / leave-module exit still running. ``:from_date``
    is the optional look-back (amendment 11.18): NULL = no lower bound (the
    rule's lookback_days = 0); a worker never seen in the branch stays out
    either way. An approved leave counts as seen through its last day (capped at
    :as_on) from its header too (_LEAVE_HEADER_SEEN), so a worker on leave today
    is never a candidate.

    Leave columns (TKT-000223): the worker's latest approved leave (any branch)
    that ended on / after the last seen day — ``leave_transaction_id``,
    ``leave_type_id``, ``leave_from``, ``leave_to``, ``leave_type_name``
    (description, code when blank); all NULL when none. ``pending_leave_since``
    = a leave request pending approval (status 20) covering :as_on (hint only).

    ``closed_cases`` = earlier closed cases of the eb in the branch (dismissals
    are not cases and are not counted) — the UI's repeat-absentee flag.

    ``:exclude_regex`` (amendment 11.17) = a MySQL regex such as ``^[TCRL]``
    built only from validated letters by ``exclude_prefix_regex``; NULL = no
    EB-prefix exclusion.

    Binds: :co_id, :branch_id, :from_date (nullable = no look-back), :as_on,
    :detect_days, :exclude_regex (nullable), :dept_id (nullable = all departments).
    """
    return text(
        f"""
        WITH seen AS (
            -- Each leg is grouped BEFORE the union (2026-10-12): the attendance leg is
            -- answered from the covering index idx_da_branch_eb_cov and yields one row per
            -- worker, so the union holds a few thousand rows instead of every attendance
            -- row of the branch (898k on sls, 1.2 s of materialising + grouping).
            SELECT g.eb_id, MAX(g.seen_date) AS last_seen
            FROM (
                SELECT da.eb_id, MAX(da.attendance_date) AS seen_date
                FROM daily_attendance da
                WHERE {_DA_SEEN}
                  AND da.branch_id = :branch_id
                  AND da.attendance_date <= :as_on
                  AND (:from_date IS NULL OR da.attendance_date >= :from_date)
                GROUP BY da.eb_id
                UNION ALL
                SELECT lt.eb_id, MAX(ltd.leave_date) AS seen_date
                FROM leave_tran_details ltd
                JOIN leave_transactions lt ON lt.leave_transaction_id = ltd.ltran_id
                WHERE ltd.is_active = 1 AND lt.status = 3 AND lt.branch_id = :branch_id
                  AND ltd.leave_date <= :as_on
                  AND (:from_date IS NULL OR ltd.leave_date >= :from_date)
                GROUP BY lt.eb_id
                UNION ALL
                SELECT lt.eb_id, MAX(LEAST(lt.leave_to_date, CAST(:as_on AS DATE))) AS seen_date
                FROM leave_transactions lt
                WHERE {_LEAVE_HEADER_SEEN}
                  AND (:from_date IS NULL OR lt.leave_to_date >= :from_date)
                GROUP BY lt.eb_id
            ) g
            GROUP BY g.eb_id
        )
        SELECT pd.eb_id,
               NULLIF(TRIM(od.emp_code), '') AS emp_code,
               NULLIF({_WORKER_NAME}, '') AS emp_name,
               dm.dept_id, dm.dept_desc, sd.sub_dept_desc,
               dsg.desig AS designation,
               cm.cata_desc AS category,
               seen.last_seen,
               DATEDIFF(:as_on, seen.last_seen) AS absent_days,
               (SELECT COUNT(*)
                  FROM jute_prod_absent_case c2
                 WHERE c2.eb_id = pd.eb_id AND c2.co_id = :co_id
                   AND c2.branch_id = :branch_id AND c2.active = 1 AND c2.status = 0
                   AND COALESCE(c2.exit_reason, '') <> 'DISMISSED') AS closed_cases,
               lv.leave_transaction_id,
               lv.leave_type_id,
               lv.leave_from_date AS leave_from,
               lv.leave_to_date AS leave_to,
               {_LEAVE_TYPE_NAME} AS leave_type_name,
               {_pending_leave_sql("pd.eb_id", ":as_on")} AS pending_leave_since
        FROM seen
        JOIN hrms_ed_personal_details pd
             ON pd.eb_id = seen.eb_id AND pd.active = 1
            AND pd.status_id NOT IN ({_SEPARATED_STATUS_SQL})
        JOIN hrms_ed_official_details od
             ON od.eb_id = pd.eb_id AND od.active = 1
            AND od.branch_id = :branch_id
            AND od.tbl_hrms_ed_official_detail_id = (
                SELECT MAX(o2.tbl_hrms_ed_official_detail_id)
                FROM hrms_ed_official_details o2
                WHERE o2.eb_id = pd.eb_id AND o2.active = 1)
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = od.sub_dept_id
        LEFT JOIN dept_mst dm ON dm.dept_id = sd.dept_id
        LEFT JOIN designation_mst dsg ON dsg.designation_id = od.designation_id
        LEFT JOIN category_mst cm ON cm.cata_id = od.catagory_id
        LEFT JOIN LATERAL (
                SELECT l2.leave_transaction_id, l2.leave_type_id, l2.leave_from_date, l2.leave_to_date
                FROM leave_transactions l2
                WHERE l2.eb_id = pd.eb_id AND l2.status = 3
                  AND l2.leave_to_date >= seen.last_seen
                ORDER BY l2.leave_to_date DESC, l2.leave_transaction_id DESC
                LIMIT 1) lv ON TRUE
        LEFT JOIN hrms_leave_types_mst lvm ON lvm.leave_type_id = lv.leave_type_id
        WHERE DATEDIFF(:as_on, seen.last_seen) >= :detect_days
          AND (:exclude_regex IS NULL OR od.emp_code IS NULL OR od.emp_code NOT REGEXP :exclude_regex)
          AND (:dept_id IS NULL OR dm.dept_id = :dept_id)
          AND NOT EXISTS (
              SELECT 1 FROM jute_prod_absent_case c
              WHERE c.eb_id = pd.eb_id AND c.co_id = :co_id
                AND c.branch_id = :branch_id AND c.active = 1
                AND (c.status <> 0
                     OR (c.exit_reason = 'DISMISSED' AND c.snooze_until >= :as_on)
                     OR (c.exit_reason IN ('ESI', 'STL', 'MATERNITY', 'MEDICAL', 'LEAVE')
                         AND c.expected_return_date IS NOT NULL
                         AND c.expected_return_date >= :as_on)))
        ORDER BY dm.dept_desc, od.emp_code
        """
    )


# =============================================================================
# Opening a case: worker snapshot, last attendance, last seen (4.2)
# =============================================================================


def get_worker_snapshot_query():
    """The worker in the branch with everything the case snapshot freezes. The
    worker's LATEST active official row overall must be in the branch (same rule
    as the candidates query): a worker transferred out of the branch is "not in
    this branch" even though an older active row of it remains. Snapshot: name
    parts, father / spouse, mobile
    (personal, else the latest active contact row), emp_code, date of join,
    dept / sub-dept / designation / category, the correspondence address (active
    row, ``is_correspondent_address = 1`` preferred) with the state name, and the
    latest active ESI / PF numbers. No row = not in this branch.

    Binds: :eb_id, :branch_id.
    """
    return text(
        f"""
        SELECT od.eb_id,
               pd.status_id AS emp_status,
               NULLIF(TRIM(od.emp_code), '') AS emp_code,
               NULLIF({_WORKER_NAME}, '') AS emp_name,
               NULLIF(TRIM(pd.father_spouse_name), '') AS father_name,
               COALESCE(
                   NULLIF(TRIM(pd.mobile_no), ''),
                   (SELECT NULLIF(TRIM(cd.mobile_no), '')
                      FROM hrms_ed_contact_details cd
                     WHERE cd.eb_id = od.eb_id AND cd.active = 1
                     ORDER BY cd.contact_detail_id DESC
                     LIMIT 1)) AS mobile,
               od.date_of_join,
               dm.dept_id, dm.dept_desc, sd.sub_dept_desc,
               dsg.desig AS designation,
               cm.cata_desc AS category,
               ad.address_line_1, ad.address_line_2, ad.city_name,
               sm.state AS state_name, ad.pin_code,
               (SELECT NULLIF(TRIM(e.esi_no), '')
                  FROM hrms_ed_esi e
                 WHERE e.eb_id = od.eb_id AND e.active = 1
                 ORDER BY e.tbl_hrms_ed_esi_id DESC
                 LIMIT 1) AS esi_no,
               (SELECT NULLIF(TRIM(p.pf_no), '')
                  FROM hrms_ed_pf p
                 WHERE p.eb_id = od.eb_id AND p.active = 1
                 ORDER BY p.tbl_hrms_ed_pf_id DESC
                 LIMIT 1) AS pf_no
        FROM hrms_ed_official_details od
        LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = od.eb_id
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = od.sub_dept_id
        LEFT JOIN dept_mst dm ON dm.dept_id = sd.dept_id
        LEFT JOIN designation_mst dsg ON dsg.designation_id = od.designation_id
        LEFT JOIN category_mst cm ON cm.cata_id = od.catagory_id
        LEFT JOIN hrms_ed_address_details ad
             ON ad.tbl_hrms_ed_contact_details_id = (
                SELECT a2.tbl_hrms_ed_contact_details_id
                FROM hrms_ed_address_details a2
                WHERE a2.eb_id = od.eb_id AND a2.active = 1
                ORDER BY a2.is_correspondent_address DESC, a2.tbl_hrms_ed_contact_details_id DESC
                LIMIT 1)
        LEFT JOIN state_mst sm ON sm.state_id = ad.state_id
        WHERE od.eb_id = :eb_id AND od.branch_id = :branch_id AND od.active = 1
          AND od.tbl_hrms_ed_official_detail_id = (
              SELECT MAX(o2.tbl_hrms_ed_official_detail_id)
              FROM hrms_ed_official_details o2
              WHERE o2.eb_id = od.eb_id AND o2.active = 1)
        ORDER BY od.tbl_hrms_ed_official_detail_id DESC
        LIMIT 1
        """
    )


def get_last_attendance_query():
    """The eb's latest active, non-rejected attendance row in the branch: date,
    spell code (``spell_mst.spell_code``, else the legacy text ``da.spell``) and
    the machine of the latest active man-machine mark (``daily_ebmc_attendance``
    joined through ``daily_atten_id``), if any.

    Binds: :eb_id, :branch_id, :as_on.
    """
    return text(
        f"""
        SELECT da.daily_atten_id,
               da.attendance_date,
               NULLIF(TRIM(COALESCE(sp.spell_code, da.spell)), '') AS spell_code,
               (SELECT m.machine_name
                  FROM daily_ebmc_attendance t
                  JOIN machine_mst m ON m.machine_id = t.mc_id
                 WHERE t.daily_atten_id = da.daily_atten_id AND t.is_active = 1
                 ORDER BY t.dtl_rec_id DESC
                 LIMIT 1) AS machine_name
        FROM daily_attendance da
        LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
        WHERE da.eb_id = :eb_id AND da.branch_id = :branch_id
          AND {_DA_SEEN}
          AND da.attendance_date <= :as_on
        ORDER BY da.attendance_date DESC, da.daily_atten_id DESC
        LIMIT 1
        """
    )


def get_last_seen_query():
    """The eb's last "seen" date in the branch up to :as_on — the candidate rule's
    union of attendance (active, non-rejected) and approved leave (detail days,
    and the leave header through its last day capped at :as_on, see
    _LEAVE_HEADER_SEEN), with no lookback. NULL when never seen.

    Binds: :eb_id, :branch_id, :as_on.
    """
    return text(
        f"""
        SELECT MAX(g.seen_date) AS last_seen
        FROM (
            SELECT da.attendance_date AS seen_date
            FROM daily_attendance da
            WHERE da.eb_id = :eb_id AND da.branch_id = :branch_id
              AND {_DA_SEEN}
              AND da.attendance_date <= :as_on
            UNION ALL
            SELECT ltd.leave_date
            FROM leave_tran_details ltd
            JOIN leave_transactions lt ON lt.leave_transaction_id = ltd.ltran_id
            WHERE lt.eb_id = :eb_id AND lt.branch_id = :branch_id
              AND ltd.is_active = 1 AND lt.status = 3
              AND ltd.leave_date <= :as_on
            UNION ALL
            SELECT LEAST(lt.leave_to_date, CAST(:as_on AS DATE)) AS seen_date
            FROM leave_transactions lt
            WHERE lt.eb_id = :eb_id AND {_LEAVE_HEADER_SEEN}
        ) g
        """
    )


def lock_case_query():
    """Row lock on one case (SELECT ... FOR UPDATE), the first statement of every
    lifecycle change (park / resume / exit / reopen) so two of them, or one and a
    letter / struck-off step of the letters router (its own lock_case_query on
    the same row), never interleave. The locking read sees the latest committed
    row. Binds: :case_id.
    """
    return text(
        """
        SELECT case_id, stage_code, status, active
        FROM jute_prod_absent_case
        WHERE case_id = :case_id
        FOR UPDATE
        """
    )


def get_live_case_query():
    """Another LIVE (open or parked) active case of the eb in the company +
    branch — the app half of the one-live-case rule (the DB half is the
    functional unique key uq_jpac_live). :exclude_id (nullable) skips the case
    being reopened.

    Binds: :co_id, :branch_id, :eb_id, :exclude_id.
    """
    return text(
        """
        SELECT case_id, case_no
        FROM jute_prod_absent_case
        WHERE co_id = :co_id AND branch_id = :branch_id AND eb_id = :eb_id
          AND status <> 0 AND active = 1
          AND (:exclude_id IS NULL OR case_id <> :exclude_id)
        ORDER BY case_id
        LIMIT 1
        """
    )


# =============================================================================
# Rejoin detection (4.6)
# =============================================================================


def get_rejoin_query():
    """Per LIVE case of the branch: the stored ``rejoined_seen_date`` and the
    first active, non-rejected attendance date of the eb after the case's last
    working date (``first_seen``, NULL when none). One round trip for the whole
    branch; :case_id (nullable) narrows it to one case.

    Binds: :co_id, :branch_id, :case_id.
    """
    return text(
        f"""
        SELECT c.case_id, c.rejoined_seen_date, MIN(da.attendance_date) AS first_seen
        FROM jute_prod_absent_case c
        LEFT JOIN daily_attendance da
               ON da.eb_id = c.eb_id
              AND da.attendance_date > c.last_working_date
              AND {_DA_SEEN}
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id
          AND c.active = 1 AND c.status <> 0
          AND (:case_id IS NULL OR c.case_id = :case_id)
        GROUP BY c.case_id, c.rejoined_seen_date
        """
    )


def get_set_rejoined_query():
    """Store a refreshed rejoined_seen_date (no event, no updated_by change).

    Binds: :case_id, :rejoined_seen_date (nullable).
    """
    return text(
        """
        UPDATE jute_prod_absent_case
        SET rejoined_seen_date = :rejoined_seen_date
        WHERE case_id = :case_id
        """
    )


# =============================================================================
# Leave module link (TKT-000223)
# =============================================================================


def get_case_leave_query():
    """Per LIVE case of the branch: what the leave refresh decides from, in one
    round trip — the case state (``eb_id``, ``status``, ``status_id``,
    ``stage_code``, ``next_due_date``, ``rejoined_seen_date``, ``leave_parked``),
    the stored leave link (``stored_ltid``, ``stored_type_id``,
    ``stored_type_name``, ``stored_from``, ``stored_to``) and the worker's
    approved leave (leave_transactions, status 3 only, any branch) that ends
    AFTER the case's last working date — ``leave_transaction_id``,
    ``leave_type_id``, ``leave_from_date``, ``leave_to_date``,
    ``leave_type_name`` (description, code when blank); all NULL when none.

    Which leave: one covering :today wins, else the latest leave_to_date (then
    the highest id) — so a worker on leave today with a later leave already
    approved is seen as on leave now, not as "upcoming". :case_id (nullable)
    narrows it to one case (same shape as get_rejoin_query).

    Performance (2026-10-12, sls): the leave pick is a LATERAL derived table so it
    runs ONCE PER CASE. Written as ``LEFT JOIN leave_transactions lv ON lv.id =
    (correlated subquery)`` MySQL 8 scanned the whole leave table and re-ran the
    subquery for every row of it (5.5k x 5.5k = 25 s per call); the lateral form
    takes a few milliseconds per case even with no index on leave_transactions.

    Binds: :co_id, :branch_id, :case_id, :today.
    """
    return text(
        f"""
        SELECT c.case_id,
               c.eb_id,
               c.status,
               c.status_id,
               c.stage_code,
               c.next_due_date,
               c.rejoined_seen_date,
               c.leave_parked,
               c.leave_transaction_id AS stored_ltid,
               c.leave_type_id AS stored_type_id,
               c.leave_type_name AS stored_type_name,
               c.leave_from AS stored_from,
               c.leave_to AS stored_to,
               lv.leave_transaction_id,
               lv.leave_type_id,
               lv.leave_from_date,
               lv.leave_to_date,
               {_LEAVE_TYPE_NAME} AS leave_type_name
        FROM jute_prod_absent_case c
        LEFT JOIN LATERAL (
                  SELECT l2.leave_transaction_id, l2.leave_type_id, l2.leave_from_date, l2.leave_to_date
                  FROM leave_transactions l2
                  WHERE l2.eb_id = c.eb_id AND l2.status = 3
                    AND l2.leave_to_date > c.last_working_date
                  ORDER BY (l2.leave_from_date <= :today AND l2.leave_to_date >= :today) DESC,
                           l2.leave_to_date DESC, l2.leave_transaction_id DESC
                  LIMIT 1) lv ON TRUE
        LEFT JOIN hrms_leave_types_mst lvm ON lvm.leave_type_id = lv.leave_type_id
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id
          AND c.active = 1 AND c.status <> 0
          AND (:case_id IS NULL OR c.case_id = :case_id)
        """
    )


def get_set_case_leave_query():
    """Write the five leave-link columns of one case (no event, no updated_by
    change). Not used by the refresh itself, which writes through the ORM so it
    can log SYSTEM entries (refresh_leaves_branch / refresh_leave_case); kept for
    ad-hoc repairs / backfills.

    Binds: :case_id, :leave_transaction_id, :leave_type_id, :leave_type_name,
    :leave_from, :leave_to (each nullable but :case_id).
    """
    return text(
        """
        UPDATE jute_prod_absent_case
        SET leave_transaction_id = :leave_transaction_id,
            leave_type_id = :leave_type_id,
            leave_type_name = :leave_type_name,
            leave_from = :leave_from,
            leave_to = :leave_to
        WHERE case_id = :case_id
        """
    )


def get_pending_leave_query():
    """The pending-leave hint of one worker (see _pending_leave_sql): one row,
    ``pending_leave_since`` NULL when no pending request covers :today.

    Binds: :eb_id, :today.
    """
    return text(f"SELECT {_pending_leave_sql(':eb_id', ':today')} AS pending_leave_since")


def get_leave_types_query():
    """The company's active leave types (HRMS leave master) for the /verify and
    /exit leave dropdowns.

    Binds: :co_id.
    """
    return text(
        """
        SELECT leave_type_id, leave_type_code, leave_type_description, payable
        FROM hrms_leave_types_mst
        WHERE company_id = :co_id AND is_active = 1
        ORDER BY leave_type_description, leave_type_code
        """
    )


def get_leave_type_query():
    """One active leave type of the company (no row = not a type of this company
    or inactive).

    Binds: :co_id, :leave_type_id.
    """
    return text(
        """
        SELECT leave_type_id, leave_type_code, leave_type_description, payable
        FROM hrms_leave_types_mst
        WHERE company_id = :co_id AND is_active = 1
          AND leave_type_id = :leave_type_id
        LIMIT 1
        """
    )


# =============================================================================
# List (watchlist) and case page
# =============================================================================

# Letter stage -> list column prefix (w1_date / w1_sent ...).
LETTER_COLUMNS = {
    "WARNING_1": "w1",
    "WARNING_2": "w2",
    "WARNING_3": "w3",
    "SHOW_CAUSE": "sc",
    "GATE_NOTICE": "gn",
}


def _letter_pivot_sql() -> str:
    parts = []
    for stage, p in LETTER_COLUMNS.items():
        parts.append(f"MAX(CASE WHEN l.stage_code = '{stage}' THEN l.issue_date END) AS {p}_date")
        parts.append(f"MAX(CASE WHEN l.stage_code = '{stage}' THEN l.sent_date END) AS {p}_sent")
    return ",\n                   ".join(parts)


def get_case_list_query():
    """Case rows of a branch (all case columns) with the per-stage letter dates
    pivoted from the active letters (w1_date, w1_sent ... gn_date, gn_sent) and
    the pending-leave hint ``pending_leave_since`` (a leave request pending
    approval covering :today; informational, never stored).

    Binds: :co_id, :branch_id, :status_mode ('live' | 'open' | 'parked' |
    'closed' | 'all'), :dept_id, :stage_code, :q (each nullable = not filtered;
    :q is a LIKE pattern matched against emp_code, emp_name and case_no), :today.
    """
    return text(
        f"""
        SELECT c.*,
               lp.w1_date, lp.w1_sent, lp.w2_date, lp.w2_sent, lp.w3_date, lp.w3_sent,
               lp.sc_date, lp.sc_sent, lp.gn_date, lp.gn_sent,
               {_pending_leave_sql("c.eb_id", ":today")} AS pending_leave_since
        FROM jute_prod_absent_case c
        LEFT JOIN (
            SELECT l.case_id,
                   {_letter_pivot_sql()}
            FROM jute_prod_absent_letter l
            INNER JOIN jute_prod_absent_case lc
                    ON lc.case_id = l.case_id AND lc.co_id = :co_id
                   AND lc.branch_id = :branch_id AND lc.active = 1
            WHERE l.active = 1
            GROUP BY l.case_id
        ) lp ON lp.case_id = c.case_id
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1
          AND (:status_mode IS NULL OR :status_mode = 'all'
               OR (:status_mode = 'live' AND c.status <> 0)
               OR (:status_mode = 'open' AND c.status = 1)
               OR (:status_mode = 'parked' AND c.status = 2)
               OR (:status_mode = 'closed' AND c.status = 0))
          AND (:dept_id IS NULL OR c.dept_id = :dept_id)
          AND (:stage_code IS NULL OR c.stage_code = :stage_code)
          AND (:q IS NULL OR c.emp_code LIKE :q OR c.emp_name LIKE :q OR c.case_no LIKE :q)
        ORDER BY c.status = 0, c.next_due_date IS NULL, c.next_due_date, c.dept_desc, c.emp_code,
                 c.case_id
        """
    )


def get_branch_depts_query():
    """Departments of the branch (dept_mst has no active flag).

    Binds: :branch_id.
    """
    return text(
        """
        SELECT dept_id, dept_desc
        FROM dept_mst
        WHERE branch_id = :branch_id
        ORDER BY order_id IS NULL, order_id, dept_desc
        """
    )


def get_other_cases_query():
    """The eb's other active case rows in the company + branch (earlier cases and
    dismissals), newest first.

    Binds: :co_id, :branch_id, :eb_id, :case_id (the case being shown, excluded).
    """
    return text(
        """
        SELECT case_id, case_no, detected_date, stage_code, status,
               exit_reason, exit_date, struck_off_date
        FROM jute_prod_absent_case
        WHERE co_id = :co_id AND branch_id = :branch_id AND eb_id = :eb_id
          AND active = 1 AND case_id <> :case_id
        ORDER BY detected_date DESC, case_id DESC
        """
    )


def get_case_letters_query():
    """Active letters of a case, oldest first (all letter columns).

    Binds: :case_id.
    """
    return text(
        """
        SELECT l.*
        FROM jute_prod_absent_letter l
        WHERE l.case_id = :case_id AND l.active = 1
        ORDER BY l.issue_date, l.letter_id
        """
    )


def get_case_receipts_query():
    """Active union receipts of the case's active letters (gate notice), oldest first.

    Binds: :case_id.
    """
    return text(
        """
        SELECT r.receipt_id, r.letter_id, r.union_name, r.received_by, r.received_date,
               r.remarks, r.active, r.updated_by, r.updated_date_time
        FROM jute_prod_absent_union_receipt r
        INNER JOIN jute_prod_absent_letter l ON l.letter_id = r.letter_id
        WHERE l.case_id = :case_id AND l.active = 1 AND r.active = 1
        ORDER BY r.received_date IS NULL, r.received_date, r.receipt_id
        """
    )


def get_case_letter_attachments_query():
    """The latest active attachment per (module, entity, file_type) of the case's
    letters (module jute_prod_absent_letter, entity = letter_id, file types
    signed_copy / ad_card) and union receipts (module jute_prod_absent_receipt,
    entity = receipt_id, file type union_receipt); generic file_attachment,
    company-scoped. Same rows as the letters router's /letters.

    Binds: :case_id, :co_id, :letter_module, :receipt_module.
    """
    return text(
        """
        SELECT fa.module, fa.entity_id, fa.file_type, fa.attachment_id,
               fa.original_filename AS filename
        FROM file_attachment fa
        INNER JOIN (
            SELECT a.module, a.entity_id, a.file_type, MAX(a.attachment_id) AS attachment_id
            FROM file_attachment a
            WHERE a.co_id = :co_id AND a.active = 1
              AND ((a.module = :letter_module
                    AND a.entity_id IN (SELECT x.letter_id
                                          FROM jute_prod_absent_letter x
                                         WHERE x.case_id = :case_id))
                OR (a.module = :receipt_module
                    AND a.entity_id IN (SELECT r.receipt_id
                                          FROM jute_prod_absent_union_receipt r
                                          JOIN jute_prod_absent_letter y ON y.letter_id = r.letter_id
                                         WHERE y.case_id = :case_id)))
            GROUP BY a.module, a.entity_id, a.file_type
        ) m ON m.attachment_id = fa.attachment_id
        """
    )


def get_events_query():
    """Active timeline rows of one case, newest first, with the latest active
    attachment of any file type (generic file_attachment, company-scoped; module
    jute_prod_absent_event, entity = event_id).

    Binds: :case_id, :co_id, :module, :event_id (nullable = every event).
    """
    return text(
        """
        SELECT e.*, fa.attachment_id, fa.original_filename AS attachment_filename
        FROM jute_prod_absent_event e
        LEFT JOIN (
            SELECT a.entity_id, MAX(a.attachment_id) AS attachment_id
            FROM file_attachment a
            WHERE a.module = :module AND a.co_id = :co_id AND a.active = 1
              AND a.entity_id IN (SELECT x.event_id
                                    FROM jute_prod_absent_event x
                                   WHERE x.case_id = :case_id)
            GROUP BY a.entity_id
        ) fa1 ON fa1.entity_id = e.event_id
        LEFT JOIN file_attachment fa ON fa.attachment_id = fa1.attachment_id
        WHERE e.case_id = :case_id AND e.active = 1
          AND (:event_id IS NULL OR e.event_id = :event_id)
        ORDER BY e.event_date DESC, e.event_id DESC
        """
    )
