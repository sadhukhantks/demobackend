-- =============================================================================
-- Long Absenteeism follow-up (Jute Production) — tables
-- Owner request 2026-10-11: replace the Personnel office's "Long Absent Report"
-- Excel sheet + Word letters (warning 1/2/3, show cause, gate notice, name
-- struck off) with tracked cases per worker. Spec:
--   docs/superpowers/specs/2026-10-11-long-absenteeism-followup.md
--
-- Target: dev3 AND sls (owner decision 2026-10-11). Idempotent (IF NOT EXISTS),
-- except the functional unique key (see the bottom of the file).
-- Runner: tools/run_sql_migration.py <db> <this file>   (run-migration skill)
-- House style: co_id NOT NULL, active TINYINT, updated_by/updated_date_time only
-- (audit is by DB trigger — no created_* columns).
-- =============================================================================
;

-- 1. Per-company rule (thresholds, clause text, labels). Code defaults apply
--    when a company has no row (same pattern as jute_po_settle_rule).
CREATE TABLE IF NOT EXISTS jute_prod_absent_rule (
    co_id                INT          NOT NULL,
    detect_days          INT          NOT NULL DEFAULT 10,   -- absent >= N days -> candidate
    lookback_days        INT          NOT NULL DEFAULT 90,   -- only workers seen in the last N days
    wait_w2_days         INT          NOT NULL DEFAULT 10,   -- after warning 1 SENT -> warning 2 due
    wait_w3_days         INT          NOT NULL DEFAULT 10,   -- after warning 2 SENT -> warning 3 due
    wait_sc_days         INT          NOT NULL DEFAULT 15,   -- after warning 3 SENT -> show cause due
    sc_reply_days        INT          NOT NULL DEFAULT 3,    -- show cause: days to contact the office
    notice_reply_days    INT          NOT NULL DEFAULT 7,    -- gate notice: days to report
    dismiss_snooze_days  INT          NOT NULL DEFAULT 30,   -- a dismissed candidate hides for N days
    exclude_t_codes      TINYINT      NOT NULL DEFAULT 1,    -- 1 = skip EB codes starting with T (cash outsiders)
    clause_text          VARCHAR(500) NULL,
    signatory            VARCHAR(100) NULL,                  -- default 'Personnel Manager'
    extra_1_label        VARCHAR(50)  NULL,                  -- tenant-defined free-text columns
    extra_2_label        VARCHAR(50)  NULL,
    extra_3_label        VARCHAR(50)  NULL,
    extra_4_label        VARCHAR(50)  NULL,
    extra_5_label        VARCHAR(50)  NULL,
    updated_by           INT          NULL,
    updated_date_time    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (co_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. One case per long-absent worker. The snapshot columns are copied from the
--    employee master / attendance when the case opens and never re-read, so a
--    letter quotes what was true then.
CREATE TABLE IF NOT EXISTS jute_prod_absent_case (
    case_id              INT           NOT NULL AUTO_INCREMENT,
    co_id                INT           NOT NULL,
    branch_id            INT           NOT NULL,
    eb_id                INT           NOT NULL,                 -- hrms_ed_personal_details.eb_id
    case_no              VARCHAR(30)   NULL,                     -- LA/<branch_id>/<yy>/<seq>
    -- snapshot (employee master + last attendance at open time)
    emp_code             VARCHAR(20)   NULL,                     -- EB no
    emp_name             VARCHAR(150)  NULL,
    dept_id              INT           NULL,                     -- dept_mst.dept_id (via sub_dept)
    dept_desc            VARCHAR(100)  NULL,
    sub_dept_desc        VARCHAR(100)  NULL,
    designation          VARCHAR(100)  NULL,                     -- designation_mst.desig ("occupation")
    category             VARCHAR(50)   NULL,
    father_name          VARCHAR(150)  NULL,
    address              VARCHAR(500)  NULL,                     -- correspondence address, one line
    mobile               VARCHAR(20)   NULL,
    date_of_join         DATE          NULL,
    esi_no               VARCHAR(30)   NULL,
    pf_no                VARCHAR(30)   NULL,
    last_machine         VARCHAR(50)   NULL,                     -- machine on the last attendance row
    last_spell           VARCHAR(20)   NULL,
    -- absence facts
    last_working_date    DATE          NOT NULL,                 -- last seen (attendance / approved leave)
    detected_date        DATE          NOT NULL,                 -- the day the case was opened
    absent_days_at_open  INT           NOT NULL DEFAULT 0,
    rejoined_seen_date   DATE          NULL,                     -- first attendance after last_working_date (refreshed on list load)
    -- ladder
    stage_code           VARCHAR(20)   NOT NULL DEFAULT 'DETECTED', -- DETECTED|VERIFIED|WARNING_1|WARNING_2|WARNING_3|SHOW_CAUSE|GATE_NOTICE|STRUCK_OFF
    stage_date           DATE          NOT NULL,                 -- date the current stage was entered
    next_due_date        DATE          NULL,                     -- when the next step becomes possible
    status               TINYINT       NOT NULL DEFAULT 1,       -- 1 open / 2 parked (reply under review) / 0 closed
    habitual             TINYINT       NOT NULL DEFAULT 0,
    -- verification (stage 1)
    esi_stl_result       VARCHAR(10)   NULL,                     -- NONE | ESI | STL
    timekeeper_confirmed TINYINT       NOT NULL DEFAULT 0,
    verified_date        DATE          NULL,
    verify_note          VARCHAR(300)  NULL,
    -- exit
    exit_reason          VARCHAR(20)   NULL,                     -- REJOINED|ESI|STL|MEDICAL|MATERNITY|RESIGNED|EXPIRED|STRUCK_OFF|DISMISSED|OTHER
    exit_date            DATE          NULL,
    exit_remarks         VARCHAR(500)  NULL,
    expected_return_date DATE          NULL,                     -- ESI / STL / maternity: resurfaces after this
    snooze_until         DATE          NULL,                     -- DISMISSED candidates stay hidden until this date
    struck_off_date      DATE          NULL,
    -- struck-off approval (standard approval engine; NULL = not started)
    status_id            INT           NULL,                     -- NULL | 20 pending | 3 approved | 4 rejected
    approval_level       INT           NULL,
    -- performance snapshot (loaded on demand from the Low Producer helpers)
    perf_avg_eff         DECIMAL(7,2)  NULL,
    perf_kg_per_hr       DECIMAL(9,3)  NULL,
    perf_days_worked     INT           NULL,
    perf_from            DATE          NULL,
    perf_to              DATE          NULL,
    perf_dept            VARCHAR(20)   NULL,                     -- WEAVING | SPINNING | WINDING
    -- Excel-style free text
    remarks              VARCHAR(1000) NULL,
    extra_1              VARCHAR(200)  NULL,
    extra_2              VARCHAR(200)  NULL,
    extra_3              VARCHAR(200)  NULL,
    extra_4              VARCHAR(200)  NULL,
    extra_5              VARCHAR(200)  NULL,
    active               TINYINT       NOT NULL DEFAULT 1,
    updated_by           INT           NULL,
    updated_date_time    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (case_id),
    KEY idx_jpac_scope (co_id, branch_id, status, active, stage_code),
    KEY idx_jpac_eb    (eb_id, active),
    KEY idx_jpac_due   (co_id, branch_id, next_due_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. One letter per stage per case. The text is frozen at issue time.
--    issue_date = the date printed on the letter; sent_date = when it actually
--    went out (registered post / hand / board) — the legal sequence and the
--    waits for the next letter count from sent_date.
CREATE TABLE IF NOT EXISTS jute_prod_absent_letter (
    letter_id            INT           NOT NULL AUTO_INCREMENT,
    case_id              INT           NOT NULL,
    stage_code           VARCHAR(20)   NOT NULL,                 -- WARNING_1|WARNING_2|WARNING_3|SHOW_CAUSE|GATE_NOTICE|STRUCK_OFF
    letter_no            INT           NULL,                     -- 1/2/3 for warnings
    template_code        VARCHAR(20)   NOT NULL,                 -- WARNING_1|WARNING_2|WARNING_3|SHOW_CAUSE|GATE_NOTICE|STRUCK_OFF_MEMO
    issue_date           DATE          NOT NULL,
    subject              VARCHAR(300)  NULL,
    body                 TEXT          NOT NULL,
    reply_due_date       DATE          NULL,                     -- show cause / gate notice
    dispatch_mode        VARCHAR(20)   NULL,                     -- REG_POST_AD | HAND | GATE_BOARD
    sent_date            DATE          NULL,                     -- recorded by the user after posting
    ad_no                VARCHAR(50)   NULL,
    delivery_status      VARCHAR(20)   NULL,                     -- PENDING | DELIVERED | RETURNED | UNKNOWN
    delivery_date        DATE          NULL,
    gate_display_date    DATE          NULL,                     -- gate notice only
    dept_display_date    DATE          NULL,
    batch_ref            VARCHAR(30)   NULL,                     -- batch issue reference (dispatch register)
    forced               TINYINT       NOT NULL DEFAULT 0,       -- 1 = issued before the wait ran out (reason in the event)
    active               TINYINT       NOT NULL DEFAULT 1,
    updated_by           INT           NULL,
    updated_date_time    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (letter_id),
    KEY idx_jpal_case  (case_id, active, stage_code),
    KEY idx_jpal_batch (batch_ref),
    CONSTRAINT fk_jpal_case FOREIGN KEY (case_id) REFERENCES jute_prod_absent_case (case_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. Union receipts on the gate notice (one row per working union served).
CREATE TABLE IF NOT EXISTS jute_prod_absent_union_receipt (
    receipt_id           INT           NOT NULL AUTO_INCREMENT,
    letter_id            INT           NOT NULL,
    union_name           VARCHAR(100)  NOT NULL,
    received_by          VARCHAR(100)  NULL,
    received_date        DATE          NULL,
    remarks              VARCHAR(300)  NULL,
    active               TINYINT       NOT NULL DEFAULT 1,
    updated_by           INT           NULL,
    updated_date_time    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (receipt_id),
    KEY idx_jpaur_letter (letter_id, active),
    CONSTRAINT fk_jpaur_letter FOREIGN KEY (letter_id) REFERENCES jute_prod_absent_letter (letter_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. Timeline: user entries (REMARK / FEEDBACK / REPLY) and system entries
--    (VERIFY / STAGE / PARK / RESUME / EXIT / REOPEN / DISMISS / APPROVAL / SYSTEM).
CREATE TABLE IF NOT EXISTS jute_prod_absent_event (
    event_id             INT           NOT NULL AUTO_INCREMENT,
    case_id              INT           NOT NULL,
    event_type           VARCHAR(20)   NOT NULL,
    event_date           DATE          NOT NULL,
    channel              VARCHAR(20)   NULL,                     -- REPLY: IN_PERSON | UNION_REP | LETTER | PHONE | OTHER
    contact_name         VARCHAR(100)  NULL,                     -- who contacted / union representative
    text                 VARCHAR(1000) NULL,
    ref_letter_id        INT           NULL,
    active               TINYINT       NOT NULL DEFAULT 1,
    updated_by           INT           NULL,
    updated_date_time    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id),
    KEY idx_jpae_case (case_id, active, event_date),
    CONSTRAINT fk_jpae_case FOREIGN KEY (case_id) REFERENCES jute_prod_absent_case (case_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 6. Letter templates per company (code defaults apply when no row).
CREATE TABLE IF NOT EXISTS jute_prod_absent_template (
    template_id          INT           NOT NULL AUTO_INCREMENT,
    co_id                INT           NOT NULL,
    template_code        VARCHAR(20)   NOT NULL,                 -- WARNING_1 | WARNING_2 | WARNING_3 | SHOW_CAUSE | GATE_NOTICE | STRUCK_OFF_MEMO
    lang                 VARCHAR(5)    NOT NULL DEFAULT 'en',
    subject              VARCHAR(300)  NULL,
    body                 TEXT          NOT NULL,
    active               TINYINT       NOT NULL DEFAULT 1,
    updated_by           INT           NULL,
    updated_date_time    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (template_id),
    UNIQUE KEY uq_jpat_code (co_id, template_code, lang)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 7. One live (open or parked) case per worker per company + branch.
--    MySQL 8.0.13+ functional unique key, as uq_jpfp_open on the follow-up table.
--    NOT idempotent: a second run fails with error 1061 (duplicate key name),
--    which changes nothing. Pre-check (must return no rows):
--      SELECT co_id, branch_id, eb_id, COUNT(*) FROM jute_prod_absent_case
--      WHERE status <> 0 AND active = 1 GROUP BY co_id, branch_id, eb_id HAVING COUNT(*) > 1;
;
ALTER TABLE jute_prod_absent_case
    ADD UNIQUE KEY uq_jpac_live (
        (CASE WHEN status <> 0 AND active = 1 THEN 1 END),
        co_id, branch_id, eb_id
    );

-- VERIFICATION
--   SHOW TABLES LIKE 'jute_prod_absent%';          -- 6 tables
--   SHOW INDEX FROM jute_prod_absent_case WHERE Key_name = 'uq_jpac_live';
--
-- ROLLBACK
--   DROP TABLE IF EXISTS jute_prod_absent_union_receipt;
--   DROP TABLE IF EXISTS jute_prod_absent_letter;
--   DROP TABLE IF EXISTS jute_prod_absent_event;
--   DROP TABLE IF EXISTS jute_prod_absent_case;
--   DROP TABLE IF EXISTS jute_prod_absent_template;
--   DROP TABLE IF EXISTS jute_prod_absent_rule;
;
