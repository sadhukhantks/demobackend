-- =============================================================================
-- Migration: WhatsApp report subscriptions + message log (test-grade)
-- =============================================================================
-- Date: 2026-10-01   Code: src/common/whatsapp/
-- TARGET: tenant DB only — dev3 first, then sls ONLY when the owner names the DB
-- (sls holds production data).
--
-- WHAT. Two small tables for the WhatsApp Cloud API integration:
--
--   whatsapp_report_subscription  who gets which daily report on WhatsApp, and
--                                 when. One row per (report, branch, phone).
--                                 src/common/whatsapp/daily_reports.py reads the
--                                 active rows, sends the report once send_time
--                                 (IST) has passed and stamps last_sent_date with
--                                 the REPORT date it sent, so a re-run of the job
--                                 on the same day does not send twice.
--                                 template_name NULL = free-form document message
--                                 (only delivered inside the 24-hour customer
--                                 service window). Set template_name (and
--                                 template_language) to send through an approved
--                                 template with a DOCUMENT header instead
--   whatsapp_message_log          one row per send attempt (text, document or
--                                 template), SENT or FAILED, with Meta's message
--                                 id or the error text
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own. Re-running is harmless
-- (CREATE TABLE IF NOT EXISTS, seed guarded by NOT EXISTS).
;

-- 0. Sender credentials (WhatsApp Cloud API). One active row per tenant, the
-- newest active row wins. The row itself is inserted by hand / by the owner,
-- never by this script, so no secret is ever written to git
;
CREATE TABLE IF NOT EXISTS whatsapp_config (
  whatsapp_config_id   INT          NOT NULL AUTO_INCREMENT,
  business_phone_no    VARCHAR(25)  NULL,                    -- the registered sender number, for reference only
  phone_number_id      VARCHAR(50)  NOT NULL,                -- Meta's phone-number id (not the number)
  access_token         TEXT         NOT NULL,
  api_version          VARCHAR(10)  NULL,                    -- NULL = code default
  active               TINYINT      NOT NULL DEFAULT 1,
  updated_by           INT          NULL,
  updated_date_time    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (whatsapp_config_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 1. Subscriptions
;
CREATE TABLE IF NOT EXISTS whatsapp_report_subscription (
  whatsapp_report_subscription_id INT          NOT NULL AUTO_INCREMENT,
  report_code                     VARCHAR(50)  NOT NULL,               -- e.g. 'DPR' (Daily Production Report)
  co_id                           INT          NOT NULL,
  branch_id                       INT          NOT NULL,
  recipient_name                  VARCHAR(100) NULL,
  phone_no                        VARCHAR(25)  NOT NULL,               -- digits with country code, no plus sign
  send_time                       TIME         NOT NULL DEFAULT '08:00:00', -- IST, earliest time of day to send
  template_name                   VARCHAR(100) NULL,                   -- NULL = free-form document message
  template_language               VARCHAR(10)  NULL,                   -- e.g. 'en', used with template_name
  last_sent_date                  DATE         NULL,                   -- report date last sent successfully
  active                          TINYINT      NOT NULL DEFAULT 1,
  updated_by                      INT          NULL,
  updated_date_time               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (whatsapp_report_subscription_id),
  KEY idx_wrs_report_active (report_code, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Message log
;
CREATE TABLE IF NOT EXISTS whatsapp_message_log (
  whatsapp_message_log_id         INT          NOT NULL AUTO_INCREMENT,
  whatsapp_report_subscription_id INT          NULL,
  report_code                     VARCHAR(50)  NULL,
  phone_no                        VARCHAR(25)  NOT NULL,
  message_type                    VARCHAR(20)  NOT NULL,               -- text | document | template
  file_name                       VARCHAR(255) NULL,
  status                          VARCHAR(10)  NOT NULL,               -- SENT | FAILED
  wa_message_id                   VARCHAR(255) NULL,
  error_text                      TEXT         NULL,
  created_date_time               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (whatsapp_message_log_id),
  KEY idx_wml_created (created_date_time),
  KEY idx_wml_subscription (whatsapp_report_subscription_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. SLS-SPECIFIC SEED — Daily Production Report of sls branch 29 (FACTORY,
-- co_id 2) to Sidhesh Sarda at 08:00 IST. Guarded twice: it inserts only when
-- branch 29 belongs to co 2 in this tenant (true on sls, and on a tenant that
-- mirrors it) and only when the same report / branch / phone row is not already
-- there, so re-running never duplicates it. On any other tenant it inserts
-- nothing
;
INSERT INTO whatsapp_report_subscription
  (report_code, co_id, branch_id, recipient_name, phone_no, send_time)
SELECT 'DPR', 2, 29, 'Sidhesh Sarda', '918420110041', '08:00:00'
  FROM DUAL
 WHERE EXISTS (SELECT 1 FROM branch_mst bm WHERE bm.branch_id = 29 AND bm.co_id = 2)
   AND NOT EXISTS (
         SELECT 1 FROM whatsapp_report_subscription s
          WHERE s.report_code = 'DPR' AND s.branch_id = 29 AND s.phone_no = '918420110041'
       );

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SHOW CREATE TABLE whatsapp_report_subscription
--   SHOW CREATE TABLE whatsapp_message_log
--   SELECT * FROM whatsapp_report_subscription
--     expected on sls one DPR row, co 2 / branch 29 / 918420110041 / 08:00:00
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DROP TABLE IF EXISTS whatsapp_message_log
--   DROP TABLE IF EXISTS whatsapp_report_subscription
--   DROP TABLE IF EXISTS whatsapp_config
-- =============================================================================
;
