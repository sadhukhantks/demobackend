-- =============================================================================
-- Migration: Joining Pass (gate pass letter after an absence of more than 3 working days)
-- =============================================================================
-- Date: 2026-10-24 (owner request, sample = EJM "JOINING PASS FOR DEPARTMENT")
-- TARGET: tenant DB only -- dev3 first, sls only after the owner's OK.
--
-- hrms_joining_pass       one issued pass per worker absence (register + reprint).
--                         The letter figures are frozen at issue (absent from, fortnight
--                         presence / working days / efficiency) so a reprint never changes.
-- hrms_joining_pass_rule  per company: the "more than N working days" threshold, the
--                         look-back of the absentee list and the fortnight length.
--
-- Blocking attendance without a pass is done by the separate attendance app, it can read
-- the active rows of hrms_joining_pass (eb_id, branch_id, join_date, last_seen_date).
-- Runner note: each comment block ends with a lone semicolon for the pymysql runner.
;

CREATE TABLE IF NOT EXISTS hrms_joining_pass (
    joining_pass_id      INT AUTO_INCREMENT PRIMARY KEY,
    pass_no              VARCHAR(30)   NULL,
    co_id                INT           NOT NULL,
    branch_id            INT           NOT NULL,
    eb_id                BIGINT        NOT NULL,
    emp_code             VARCHAR(30)   NULL,
    emp_name             VARCHAR(150)  NULL,
    dept_id              INT           NULL,
    dept_desc            VARCHAR(100)  NULL,
    designation          VARCHAR(100)  NULL,
    last_seen_date       DATE          NOT NULL COMMENT 'last attendance / approved leave day before the absence',
    absent_from          DATE          NOT NULL COMMENT 'first working day of the absence',
    absent_working_days  INT           NOT NULL,
    absent_calendar_days INT           NOT NULL,
    join_date            DATE          NOT NULL COMMENT 'allowed to join w.e.f.',
    issue_date           DATE          NOT NULL COMMENT 'pass issued on',
    spell_id             INT           NULL COMMENT 'spell the worker joins in (spell_mst)',
    spell_code           VARCHAR(25)   NULL,
    authority            VARCHAR(50)   NULL COMMENT 'authorised by (Welfare Officer ...)',
    fn_from              DATE          NULL COMMENT 'last fortnight of working: first day',
    fn_to                DATE          NULL COMMENT 'last fortnight of working: last attendance day',
    fn_days_present      INT           NULL,
    fn_working_days      INT           NULL,
    fn_efficiency        DECIMAL(7,2)  NULL COMMENT 'NULL = no production basis (blank on the letter)',
    fn_eff_dept          VARCHAR(20)   NULL COMMENT 'WEAVING / SPINNING / WINDING basis of fn_efficiency',
    la_case_id           INT           NULL COMMENT 'open Long Absenteeism case at issue, if any',
    remarks              VARCHAR(500)  NULL COMMENT 'reasons',
    active               INT           NOT NULL DEFAULT 1 COMMENT '0 = cancelled',
    cancel_reason        VARCHAR(300)  NULL,
    cancelled_by         INT           NULL,
    cancelled_date_time  DATETIME      NULL,
    issued_by            INT           NULL COMMENT 'user who issued the pass',
    updated_by           INT           NULL,
    updated_date_time    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_hjp_branch_issue (branch_id, active, issue_date),
    KEY idx_hjp_eb (eb_id, branch_id, active, join_date)
);

CREATE TABLE IF NOT EXISTS hrms_joining_pass_rule (
    co_id             INT PRIMARY KEY,
    min_absent_days   INT       NOT NULL DEFAULT 3  COMMENT 'pass needed when absent MORE than this many working days',
    lookback_days     INT       NOT NULL DEFAULT 60 COMMENT 'absentee list: last seen within this many days',
    fortnight_days    INT       NOT NULL DEFAULT 14,
    updated_by        INT       NULL,
    updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- =============================================================================
-- VERIFICATION
--   SHOW CREATE TABLE hrms_joining_pass;
--   SHOW CREATE TABLE hrms_joining_pass_rule;
-- ROLLBACK
--   DROP TABLE hrms_joining_pass
--   DROP TABLE hrms_joining_pass_rule
-- =============================================================================
;
