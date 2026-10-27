"""Raw SQL for the Long Absenteeism letters router (long_absenteeism_letters.py):
templates, letters, dispatch register, union receipts, struck-off approval and
reports. Every factory returns a ``text()`` with named binds; ``None`` binds mean
"not filtered" where a docstring says nullable.

Each statement carries a ``/* la-... */`` tag so a slow-query log (and the unit
tests' fake session) can tell them apart.

Spec: docs/superpowers/specs/2026-10-11-long-absenteeism-followup.md (4.7-4.11, 7)
Reused rather than copied:
  * menu access level per branch -> long_absenteeism_common.access_level
    (approval_query.get_user_edit_access); the company-wide variant below mirrors it
  * approval_mst lookups          -> src/common/approval_utils (engine)
"""

from sqlalchemy import bindparam, text

# Generic file_attachment modules (src/common/attachments/constants.py)
LETTER_MODULE = "jute_prod_absent_letter"
RECEIPT_MODULE = "jute_prod_absent_receipt"
EVENT_MODULE = "jute_prod_absent_event"
LETTER_FILE_TYPES = ("signed_copy", "ad_card")
RECEIPT_FILE_TYPES = ("union_receipt",)
EVENT_FILE_TYPES = ("reply", "medical", "other")

_LETTER_COLS = """
    l.letter_id, l.case_id, l.stage_code, l.letter_no, l.template_code, l.issue_date,
    l.subject, l.body, l.reply_due_date, l.dispatch_mode, l.sent_date, l.ad_no,
    l.delivery_status, l.delivery_date, l.gate_display_date, l.dept_display_date,
    l.batch_ref, l.forced, l.active, l.updated_by, l.updated_date_time
"""


# =============================================================================
# Access / scope
# =============================================================================


def get_company_access_query():
    """Highest access_type_id the user holds on the menu in ANY branch of the
    company (company-wide endpoints: rule, templates). Same resolution as
    approval_query.get_user_edit_access, scoped by co_id instead of one branch.
    Binds: :user_id, :co_id, :menu_id."""
    return text(
        """
        /* la-company-access */
        SELECT MAX(CASE WHEN ccm.access_type = 1 THEN 1 ELSE rmm.access_type_id END) AS max_access_type_id
        FROM user_role_map urm
        JOIN branch_mst bm ON bm.branch_id = urm.branch_id AND bm.co_id = :co_id
        LEFT JOIN role_menu_map rmm ON rmm.role_id = urm.role_id
        LEFT JOIN menu_mst mm ON mm.menu_id = rmm.menu_id AND mm.active = 1
        LEFT JOIN control_co_module ccm ON urm.co_id = ccm.co_id AND ccm.module_id = mm.module_mst_id
        WHERE urm.user_id = :user_id
          AND rmm.menu_id = :menu_id
          AND IFNULL(ccm.access_type, 0) NOT IN (2)
        """
    )


def lock_case_query():
    """Row lock on the case (serialises letter issue on one case). A locking read
    returns the LATEST committed stage / status, whatever the transaction's
    snapshot. Binds: :case_id."""
    return text(
        """
        /* la-lock-case */
        SELECT case_id, stage_code, status, active
        FROM jute_prod_absent_case
        WHERE case_id = :case_id
        FOR UPDATE
        """
    )


def get_employee_status_query():
    """The worker's current HRMS lifecycle status (struck-off approval refuses a
    worker HR has already separated). Binds: :eb_id."""
    return text(
        """
        /* la-emp-status */
        SELECT status_id
        FROM hrms_ed_personal_details
        WHERE eb_id = :eb_id
        LIMIT 1
        """
    )


def get_case_id_by_no_query():
    """case_id of a case number in the company (Templates tab preview by case no).
    Binds: :co_id, :case_no."""
    return text(
        """
        /* la-case-by-no */
        SELECT case_id
        FROM jute_prod_absent_case
        WHERE co_id = :co_id AND case_no = :case_no AND active = 1
        ORDER BY case_id DESC
        LIMIT 1
        """
    )


def get_pdf_letterhead_query():
    """Letterhead of the PDF letters, as the screen letterhead shows it
    (useCompanyHeader): company name, address lines + state + PIN, CIN, the
    logo (co_mst.co_logo, a base64 data URL or NULL) and the branch GSTIN.
    Binds: :branch_id."""
    return text(
        """
        /* la-pdf-letterhead */
        SELECT cm.co_id, cm.co_name, cm.co_address1, cm.co_address2, cm.co_zipcode, cm.co_cin_no,
               cm.co_logo, sm.state AS state_name, bm.branch_id, bm.branch_name, bm.gst_no
        FROM branch_mst bm
        LEFT JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN state_mst sm ON sm.state_id = cm.state_id
        WHERE bm.branch_id = :branch_id
        """
    )


def get_company_letterhead_query():
    """Company name + address for the letters, through the branch (as
    inventory/query.py does for the issue print). Binds: :branch_id."""
    return text(
        """
        /* la-letterhead */
        SELECT cm.co_id, cm.co_name, cm.co_address1, cm.co_address2, cm.co_zipcode,
               bm.branch_id, bm.branch_name, bm.branch_address1, bm.branch_address2, bm.branch_zipcode
        FROM branch_mst bm
        LEFT JOIN co_mst cm ON cm.co_id = bm.co_id
        WHERE bm.branch_id = :branch_id
        """
    )


# =============================================================================
# Templates
# =============================================================================


def get_templates_query():
    """The company's saved templates (English). Binds: :co_id."""
    return text(
        """
        /* la-templates */
        SELECT template_id, template_code, lang, subject, body, updated_by, updated_date_time
        FROM jute_prod_absent_template
        WHERE co_id = :co_id AND lang = 'en' AND active = 1
        """
    )


def get_template_id_query():
    """template_id of (co_id, template_code, 'en') — any active flag (the unique
    key covers all rows). Binds: :co_id, :template_code."""
    return text(
        """
        /* la-template-id */
        SELECT template_id
        FROM jute_prod_absent_template
        WHERE co_id = :co_id AND template_code = :template_code AND lang = 'en'
        LIMIT 1
        """
    )


def delete_template_query():
    """Back to the code default (hard delete). Binds: :co_id, :template_code."""
    return text(
        """
        /* la-template-delete */
        DELETE FROM jute_prod_absent_template
        WHERE co_id = :co_id AND template_code = :template_code AND lang = 'en'
        """
    )


# =============================================================================
# Letters, receipts, attachments
# =============================================================================


def get_case_letters_query():
    """Active letters of one case, oldest first (every column: ``l.*`` carries
    ``recorded`` once migration 20261012b has run, nothing breaks before).
    Binds: :case_id."""
    return text(
        """
        /* la-case-letters */
        SELECT l.*
        FROM jute_prod_absent_letter l
        WHERE l.case_id = :case_id AND l.active = 1
        ORDER BY l.issue_date, l.letter_id
        """
    )


def get_letters_by_ids_query():
    """Active letters of active cases of the company, by id (bulk reprint / PDF;
    the caller restores the requested order and 404s the ids not returned).
    ``l.*`` so ``recorded`` comes along once migration 20261012b has run.
    Binds: :co_id, :letter_ids (expanding list)."""
    return text(
        """
        /* la-letters-by-ids */
        SELECT l.*, c.co_id, c.branch_id, c.case_no, c.emp_code, c.emp_name
        FROM jute_prod_absent_letter l
        JOIN jute_prod_absent_case c ON c.case_id = l.case_id
        WHERE l.letter_id IN :letter_ids AND l.active = 1
          AND c.co_id = :co_id AND c.active = 1
        """
    ).bindparams(bindparam("letter_ids", expanding=True))


def get_case_receipts_query():
    """Active union receipts on the case's active letters. Binds: :case_id."""
    return text(
        """
        /* la-case-receipts */
        SELECT r.receipt_id, r.letter_id, r.union_name, r.received_by, r.received_date,
               r.remarks, r.active, r.updated_by, r.updated_date_time
        FROM jute_prod_absent_union_receipt r
        JOIN jute_prod_absent_letter l ON l.letter_id = r.letter_id
        WHERE l.case_id = :case_id AND l.active = 1 AND r.active = 1
        ORDER BY r.received_date, r.receipt_id
        """
    )


def get_case_attachments_query():
    """Latest active attachment per (module, entity, file_type) for the case's
    letters (signed_copy / ad_card) and receipts (union_receipt), company-scoped.
    Binds: :co_id, :case_id, :letter_module, :receipt_module."""
    return text(
        """
        /* la-case-attachments */
        SELECT fa.module, fa.entity_id, fa.file_type, fa.attachment_id, fa.original_filename
        FROM file_attachment fa
        JOIN (
            SELECT a.module, a.entity_id, a.file_type, MAX(a.attachment_id) AS attachment_id
            FROM file_attachment a
            WHERE a.co_id = :co_id AND a.active = 1
              AND (
                   (a.module = :letter_module
                    AND a.entity_id IN (SELECT l.letter_id FROM jute_prod_absent_letter l
                                         WHERE l.case_id = :case_id))
                OR (a.module = :receipt_module
                    AND a.entity_id IN (SELECT r.receipt_id
                                          FROM jute_prod_absent_union_receipt r
                                          JOIN jute_prod_absent_letter l2 ON l2.letter_id = r.letter_id
                                         WHERE l2.case_id = :case_id))
              )
            GROUP BY a.module, a.entity_id, a.file_type
        ) x ON x.attachment_id = fa.attachment_id
        """
    )


def get_branch_open_cases_query():
    """Open (status 1) active cases of a branch at a stage that has a next letter
    (VERIFIED .. SHOW_CAUSE) — the candidates of /letters_due. Binds: :co_id,
    :branch_id."""
    return text(
        """
        /* la-branch-open-cases */
        SELECT c.case_id, c.case_no, c.eb_id, c.emp_code, c.emp_name, c.dept_id, c.dept_desc,
               c.designation, c.last_working_date, c.rejoined_seen_date, c.stage_code,
               c.stage_date, c.next_due_date, c.status, c.status_id
        FROM jute_prod_absent_case c
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1 AND c.status = 1
          AND c.stage_code IN ('VERIFIED', 'WARNING_1', 'WARNING_2', 'WARNING_3', 'SHOW_CAUSE')
        ORDER BY c.dept_desc, c.emp_code, c.case_id
        """
    )


def get_branch_open_case_letters_query():
    """Active letters of the branch's open cases (one round trip for /letters_due).
    Binds: :co_id, :branch_id."""
    return text(
        f"""
        /* la-branch-open-letters */
        SELECT {_LETTER_COLS}
        FROM jute_prod_absent_letter l
        JOIN jute_prod_absent_case c ON c.case_id = l.case_id
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1 AND c.status = 1
          AND l.active = 1
        ORDER BY l.case_id, l.issue_date, l.letter_id
        """
    )


def get_dispatch_query():
    """Letters issued in a date range (dispatch register), newest first.
    Binds: :co_id, :branch_id, :date_from, :date_to, :batch_ref (nullable),
    :stage_code (nullable)."""
    return text(
        """
        /* la-dispatch */
        SELECT l.letter_id, l.case_id, c.case_no, c.eb_id, c.emp_code, c.emp_name, c.dept_desc,
               c.designation, c.address, l.stage_code, l.letter_no, l.template_code, l.issue_date,
               l.reply_due_date, l.dispatch_mode, l.sent_date, l.ad_no, l.delivery_status,
               l.delivery_date, l.gate_display_date, l.dept_display_date, l.batch_ref, l.forced
        FROM jute_prod_absent_letter l
        JOIN jute_prod_absent_case c ON c.case_id = l.case_id
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1 AND l.active = 1
          AND l.issue_date BETWEEN :date_from AND :date_to
          AND (:batch_ref IS NULL OR l.batch_ref = :batch_ref)
          AND (:stage_code IS NULL OR l.stage_code = :stage_code)
        ORDER BY l.issue_date DESC, (l.batch_ref IS NULL), l.batch_ref DESC, c.emp_code, l.letter_id
        """
    )


def get_union_names_query():
    """Union names used on the branch's gate-notice receipts, most used first.
    Binds: :co_id, :branch_id."""
    return text(
        """
        /* la-union-names */
        SELECT r.union_name, COUNT(*) AS uses
        FROM jute_prod_absent_union_receipt r
        JOIN jute_prod_absent_letter l ON l.letter_id = r.letter_id
        JOIN jute_prod_absent_case c ON c.case_id = l.case_id
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1
          AND r.active = 1 AND l.active = 1
        GROUP BY r.union_name
        ORDER BY uses DESC, r.union_name
        """
    )


def get_letter_scope_query():
    """Attachment gate, module jute_prod_absent_letter. Binds: :entity_id."""
    return text(
        """
        /* la-scope-letter */
        SELECT l.letter_id AS entity_id, l.active AS entity_active, c.case_id, c.co_id,
               c.branch_id, c.active AS case_active, c.status
        FROM jute_prod_absent_letter l
        JOIN jute_prod_absent_case c ON c.case_id = l.case_id
        WHERE l.letter_id = :entity_id
        """
    )


def get_receipt_scope_query():
    """Attachment gate, module jute_prod_absent_receipt (receipt and its letter
    must both be active). Binds: :entity_id."""
    return text(
        """
        /* la-scope-receipt */
        SELECT r.receipt_id AS entity_id, (r.active = 1 AND l.active = 1) AS entity_active,
               c.case_id, c.co_id, c.branch_id, c.active AS case_active, c.status
        FROM jute_prod_absent_union_receipt r
        JOIN jute_prod_absent_letter l ON l.letter_id = r.letter_id
        JOIN jute_prod_absent_case c ON c.case_id = l.case_id
        WHERE r.receipt_id = :entity_id
        """
    )


def get_event_scope_query():
    """Attachment gate, module jute_prod_absent_event. Binds: :entity_id."""
    return text(
        """
        /* la-scope-event */
        SELECT e.event_id AS entity_id, e.active AS entity_active, e.event_type, c.case_id,
               c.co_id, c.branch_id, c.active AS case_active, c.status
        FROM jute_prod_absent_event e
        JOIN jute_prod_absent_case c ON c.case_id = e.case_id
        WHERE e.event_id = :entity_id
        """
    )


# =============================================================================
# Struck-off approval (standard engine, src/common/approval_utils.py)
# =============================================================================


def get_case_approval_doc_query():
    """get_doc_fn for process_approval / process_rejection. Binds: :case_id."""
    return text(
        """
        /* la-approval-doc */
        SELECT status_id, approval_level, branch_id
        FROM jute_prod_absent_case
        WHERE case_id = :case_id
        """
    )


def update_case_approval_status_query():
    """update_status_fn for the engine. Binds: :case_id, :status_id,
    :approval_level, :updated_by, :updated_date_time."""
    return text(
        """
        /* la-approval-update */
        UPDATE jute_prod_absent_case
        SET status_id = :status_id,
            approval_level = :approval_level,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time
        WHERE case_id = :case_id
        """
    )


# =============================================================================
# Reports
# =============================================================================


def get_struck_off_register_query():
    """Struck-off cases with struck_off_date in the range, with their letter
    dates (conditional aggregation over the active letters).
    Binds: :co_id, :branch_id, :date_from, :date_to, :dept_id (nullable)."""
    return text(
        """
        /* la-struck-off-register */
        SELECT c.case_id, c.case_no, c.eb_id, c.emp_code, c.emp_name, c.dept_id, c.dept_desc,
               c.designation, c.last_working_date, c.struck_off_date, c.remarks,
               MAX(CASE WHEN l.stage_code = 'WARNING_1' THEN l.issue_date END) AS warning1_date,
               MAX(CASE WHEN l.stage_code = 'WARNING_1' THEN l.sent_date END) AS warning1_sent,
               MAX(CASE WHEN l.stage_code = 'WARNING_2' THEN l.issue_date END) AS warning2_date,
               MAX(CASE WHEN l.stage_code = 'WARNING_2' THEN l.sent_date END) AS warning2_sent,
               MAX(CASE WHEN l.stage_code = 'WARNING_3' THEN l.issue_date END) AS warning3_date,
               MAX(CASE WHEN l.stage_code = 'WARNING_3' THEN l.sent_date END) AS warning3_sent,
               MAX(CASE WHEN l.stage_code = 'SHOW_CAUSE' THEN l.issue_date END) AS show_cause_date,
               MAX(CASE WHEN l.stage_code = 'SHOW_CAUSE' THEN l.sent_date END) AS show_cause_sent,
               MAX(CASE WHEN l.stage_code = 'GATE_NOTICE' THEN l.issue_date END) AS gate_notice_date,
               MAX(CASE WHEN l.stage_code = 'GATE_NOTICE' THEN l.sent_date END) AS gate_notice_sent
        FROM jute_prod_absent_case c
        LEFT JOIN jute_prod_absent_letter l ON l.case_id = c.case_id AND l.active = 1
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1
          AND c.exit_reason = 'STRUCK_OFF'
          AND c.struck_off_date BETWEEN :date_from AND :date_to
          AND (:dept_id IS NULL OR c.dept_id = :dept_id)
        GROUP BY c.case_id
        ORDER BY c.struck_off_date, c.emp_code, c.case_id
        """
    )


def get_stage_summary_live_query():
    """Live (open / parked) cases by department and stage.
    Binds: :co_id, :branch_id."""
    return text(
        """
        /* la-stage-summary-live */
        SELECT c.dept_id, c.dept_desc, c.stage_code,
               SUM(CASE WHEN c.status = 2 THEN 1 ELSE 0 END) AS parked,
               COUNT(*) AS n
        FROM jute_prod_absent_case c
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1 AND c.status <> 0
        GROUP BY c.dept_id, c.dept_desc, c.stage_code
        """
    )


def get_stage_summary_exits_query():
    """Closed cases by exit reason with exit_date in [:date_from, :as_on].
    Binds: :co_id, :branch_id, :date_from, :as_on."""
    return text(
        """
        /* la-stage-summary-exits */
        SELECT c.exit_reason, COUNT(*) AS n
        FROM jute_prod_absent_case c
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1 AND c.status = 0
          AND c.exit_date BETWEEN :date_from AND :as_on
        GROUP BY c.exit_reason
        ORDER BY n DESC, c.exit_reason
        """
    )


def get_monthly_trend_query():
    """Per month: cases opened (detected_date; dismissed candidates excluded) and
    exits by kind (exit_date). Binds: :co_id, :branch_id, :date_from, :date_to."""
    return text(
        """
        /* la-monthly-trend */
        SELECT DATE_FORMAT(c.detected_date, '%Y-%m') AS month, 'opened' AS kind, COUNT(*) AS n
        FROM jute_prod_absent_case c
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1
          AND c.detected_date BETWEEN :date_from AND :date_to
          AND COALESCE(c.exit_reason, '') <> 'DISMISSED'
        GROUP BY DATE_FORMAT(c.detected_date, '%Y-%m')
        UNION ALL
        SELECT DATE_FORMAT(c.exit_date, '%Y-%m') AS month,
               CASE WHEN c.exit_reason = 'STRUCK_OFF' THEN 'struck_off'
                    WHEN c.exit_reason = 'REJOINED' THEN 'rejoined'
                    ELSE 'other_exits' END AS kind,
               COUNT(*) AS n
        FROM jute_prod_absent_case c
        WHERE c.co_id = :co_id AND c.branch_id = :branch_id AND c.active = 1 AND c.status = 0
          AND c.exit_date BETWEEN :date_from AND :date_to
          AND COALESCE(c.exit_reason, '') <> 'DISMISSED'
        GROUP BY DATE_FORMAT(c.exit_date, '%Y-%m'),
                 CASE WHEN c.exit_reason = 'STRUCK_OFF' THEN 'struck_off'
                      WHEN c.exit_reason = 'REJOINED' THEN 'rejoined'
                      ELSE 'other_exits' END
        """
    )
