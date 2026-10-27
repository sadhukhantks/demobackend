-- GST e-invoice (IRP) integration — NEW tables only.
--
-- Context: sales invoices must be reported to a GSTN-authorized Invoice
-- Registration Portal (IRP) to obtain an IRN + signed QR code. Target IRP is
-- IRIS IRP (einvoice6.gst.gov.in), sandbox first. See docs/einvoice/ for the
-- registration runbook and API notes.
--
-- Design decision (locked): DO NOT alter existing sales tables. sales_invoice
-- already carries manually-entered irn / ack_no / ack_date / qr_code columns and
-- the e_invoice_responses audit table — both stay untouched. The API
-- integration lives entirely in the new einv_* tables below, joined to the base
-- tables by FK:
--
--   co_mst / branch_mst  ◄── einv_gstin_config   (per-GSTIN API credentials+env)
--   sales_invoice        ◄── einv_irn_registry   (one lifecycle row per invoice+env)
--   einv_irn_registry    ◄── einv_api_log        (every HTTP call, request/response)
--
-- Safe on live data: pure CREATE TABLE, no data backfill.
--
-- ROLLBACK
--   DROP TABLE IF EXISTS `einv_api_log`;
--   DROP TABLE IF EXISTS `einv_irn_registry`;
--   DROP TABLE IF EXISTS `einv_gstin_config`;

-- ---------------------------------------------------------------------------
-- 1. einv_gstin_config — one row per (GSTIN, environment, IRP provider).
--    Holds the IRP API credentials issued on the IRP portal (sandbox or
--    production) plus endpoint overrides. Base URLs / paths default from
--    application constants when NULL here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `einv_gstin_config` (
    `einv_gstin_config_id` INT          NOT NULL AUTO_INCREMENT,
    `co_id`                INT          NOT NULL COMMENT 'co_mst.co_id',
    `branch_id`            INT          NULL     COMMENT 'branch_mst.branch_id - NULL = company-level config',
    `gstin`                VARCHAR(15)  NOT NULL COMMENT 'seller GSTIN this credential set belongs to',
    `irp_provider`         VARCHAR(20)  NOT NULL DEFAULT 'IRIS' COMMENT 'IRIS (einvoice6) | NIC (einvoice1 sandbox)',
    `env`                  VARCHAR(10)  NOT NULL DEFAULT 'SANDBOX' COMMENT 'SANDBOX | PRODUCTION',
    `api_base_url`         VARCHAR(255) NULL     COMMENT 'override; NULL = provider default from constants',
    `auth_path`            VARCHAR(255) NULL     COMMENT 'override auth endpoint path',
    `generate_irn_path`    VARCHAR(255) NULL     COMMENT 'override generate-IRN endpoint path',
    `cancel_irn_path`      VARCHAR(255) NULL     COMMENT 'override cancel-IRN endpoint path',
    `get_irn_path`         VARCHAR(255) NULL     COMMENT 'override get-IRN-details endpoint path',
    `encryption_mode`      VARCHAR(10)  NOT NULL DEFAULT 'NIC' COMMENT 'NIC = RSA/AES SEK flow per e-invoice standard | PLAIN = plain JSON over TLS',
    `client_id`            VARCHAR(255) NULL     COMMENT 'API client id issued by the IRP',
    `client_secret`        VARCHAR(255) NULL     COMMENT 'API client secret issued by the IRP - prefer env var EINVOICE_CLIENT_SECRET in production',
    `api_username`         VARCHAR(100) NULL     COMMENT 'IRP API username created for the GSTIN',
    `api_password`         VARCHAR(255) NULL     COMMENT 'IRP API password - prefer env var EINVOICE_API_PASSWORD in production',
    `public_key_pem`       MEDIUMTEXT   NULL     COMMENT 'IRP RSA public key (PEM) used to encrypt password/AppKey in NIC mode',
    `active`               INT          NOT NULL DEFAULT 1,
    `updated_by`           INT          NULL,
    `updated_date_time`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`einv_gstin_config_id`),
    UNIQUE KEY `uq_einv_cfg_gstin_env_provider` (`gstin`, `env`, `irp_provider`),
    KEY `idx_einv_cfg_co_branch` (`co_id`, `branch_id`),
    CONSTRAINT `fk_einv_cfg_co`     FOREIGN KEY (`co_id`)     REFERENCES `co_mst` (`co_id`),
    CONSTRAINT `fk_einv_cfg_branch` FOREIGN KEY (`branch_id`) REFERENCES `branch_mst` (`branch_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='e-invoice IRP API credentials + endpoint config per GSTIN/env';

-- ---------------------------------------------------------------------------
-- 2. einv_irn_registry — the e-invoice lifecycle of one sales invoice in one
--    environment. UNIQUE(invoice_id, env): an invoice holds at most one live
--    IRN per environment (re-attempts update the same row; the per-call trail
--    lives in einv_api_log).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `einv_irn_registry` (
    `einv_irn_id`          BIGINT        NOT NULL AUTO_INCREMENT,
    `invoice_id`           BIGINT        NOT NULL COMMENT 'sales_invoice.invoice_id',
    `einv_gstin_config_id` INT           NULL     COMMENT 'credential set used',
    `co_id`                INT           NULL     COMMENT 'denormalized for listing filters',
    `branch_id`            INT           NULL     COMMENT 'denormalized for listing filters',
    `env`                  VARCHAR(10)   NOT NULL DEFAULT 'SANDBOX' COMMENT 'SANDBOX | PRODUCTION',
    `einv_status`          VARCHAR(20)   NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING | GENERATED | CANCELLED | FAILED',
    `irn`                  VARCHAR(64)   NULL     COMMENT '64-char IRN hash returned by the IRP',
    `ack_no`               VARCHAR(30)   NULL,
    `ack_date`             DATETIME      NULL,
    `signed_invoice`       MEDIUMTEXT    NULL     COMMENT 'JWS signed invoice returned by IRP',
    `signed_qr_code`       MEDIUMTEXT    NULL     COMMENT 'JWS signed QR payload returned by IRP',
    `ewb_no`               VARCHAR(20)   NULL     COMMENT 'e-way bill no when generated with IRN',
    `ewb_date`             DATETIME      NULL,
    `ewb_valid_till`       DATETIME      NULL,
    `cancel_date`          DATETIME      NULL,
    `cancel_reason_code`   VARCHAR(5)    NULL     COMMENT 'IRP cancel reason code (1=duplicate, 2=data entry mistake, ...)',
    `cancel_remarks`       VARCHAR(255)  NULL,
    `request_payload`      MEDIUMTEXT    NULL     COMMENT 'INV-01 JSON exactly as submitted (pre-encryption)',
    `last_error`           VARCHAR(1000) NULL     COMMENT 'latest failure detail, cleared on success',
    `updated_by`           INT           NULL,
    `updated_date_time`    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`einv_irn_id`),
    UNIQUE KEY `uq_einv_irn_invoice_env` (`invoice_id`, `env`),
    UNIQUE KEY `uq_einv_irn_irn` (`irn`),
    KEY `idx_einv_irn_status` (`einv_status`),
    KEY `idx_einv_irn_co_branch` (`co_id`, `branch_id`),
    CONSTRAINT `fk_einv_irn_invoice` FOREIGN KEY (`invoice_id`)           REFERENCES `sales_invoice` (`invoice_id`),
    CONSTRAINT `fk_einv_irn_cfg`     FOREIGN KEY (`einv_gstin_config_id`) REFERENCES `einv_gstin_config` (`einv_gstin_config_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='e-invoice IRN lifecycle per sales invoice per environment';

-- ---------------------------------------------------------------------------
-- 3. einv_api_log — immutable trail of every HTTP call made to the IRP
--    (auth, generate, cancel, get). Secrets are redacted by the client before
--    insert; payloads stored pre-encryption for debuggability.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `einv_api_log` (
    `einv_api_log_id`      BIGINT        NOT NULL AUTO_INCREMENT,
    `einv_irn_id`          BIGINT        NULL     COMMENT 'einv_irn_registry.einv_irn_id - NULL for auth/connectivity calls',
    `einv_gstin_config_id` INT           NULL,
    `invoice_id`           BIGINT        NULL     COMMENT 'sales_invoice.invoice_id - NULL for auth calls',
    `api_name`             VARCHAR(30)   NOT NULL COMMENT 'AUTH | GENERATE_IRN | CANCEL_IRN | GET_IRN',
    `http_status`          INT           NULL,
    `success`              INT           NOT NULL DEFAULT 0,
    `error_code`           VARCHAR(20)   NULL     COMMENT 'IRP error code (e.g. 2150 duplicate IRN)',
    `error_message`        VARCHAR(1000) NULL,
    `request_json`         MEDIUMTEXT    NULL     COMMENT 'request body pre-encryption, secrets redacted',
    `response_json`        MEDIUMTEXT    NULL     COMMENT 'response body post-decryption',
    `duration_ms`          INT           NULL,
    `called_by`            INT           NULL     COMMENT 'user_mst.user_id when triggered from the portal',
    `called_at`            TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`einv_api_log_id`),
    KEY `idx_einv_log_invoice` (`invoice_id`),
    KEY `idx_einv_log_irn` (`einv_irn_id`),
    KEY `idx_einv_log_called_at` (`called_at`),
    CONSTRAINT `fk_einv_log_irn`     FOREIGN KEY (`einv_irn_id`)          REFERENCES `einv_irn_registry` (`einv_irn_id`),
    CONSTRAINT `fk_einv_log_cfg`     FOREIGN KEY (`einv_gstin_config_id`) REFERENCES `einv_gstin_config` (`einv_gstin_config_id`),
    CONSTRAINT `fk_einv_log_invoice` FOREIGN KEY (`invoice_id`)           REFERENCES `sales_invoice` (`invoice_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='HTTP call audit trail for e-invoice IRP APIs';
