-- Migration: Support Ticket in-app notifications
-- Date: 2026-09-07
-- Target DB: vowconsole3 (central console DB — NOT a tenant DB)
-- Description:
--   Per-recipient notification feed for support-ticket changes. Reporters
--   (Portal / Tenant Admin users) are notified when the VOW team assigns,
--   moves or replies on their ticket; the assigned VOW member is notified when
--   the reporter replies / attaches a file or another member changes the
--   ticket. Read from the widget badge (reporter) and the Control Desk bell
--   (VOW). Depends on create_support_ticket_tables.sql having run first.

CREATE TABLE IF NOT EXISTS support_ticket_notification (
    notification_id     INT PRIMARY KEY AUTO_INCREMENT,
    ticket_id           INT NOT NULL,
    recipient_type      VARCHAR(20) NOT NULL,          -- reporter | vow
    recipient_source    VARCHAR(20) NULL,              -- reporter: portal | admin ; vow: NULL
    recipient_org_id    INT NULL,                      -- reporter (portal): con_org_id — user ids collide across tenants
    recipient_user_id   INT NOT NULL,                  -- reporter: user_mst.user_id / con_user_id ; vow: con_user_id
    event_type          VARCHAR(30) NOT NULL,          -- assigned | status_changed | comment | attachment
    title               VARCHAR(200) NOT NULL,
    message             VARCHAR(500) NULL,
    actor_name          VARCHAR(120) NULL,
    is_read             INT NOT NULL DEFAULT 0,
    read_date_time      DATETIME NULL,
    created_date_time   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_stn_recipient (recipient_type, recipient_user_id, is_read),
    INDEX idx_stn_ticket (ticket_id),
    CONSTRAINT fk_support_ticket_notification_ticket
        FOREIGN KEY (ticket_id) REFERENCES support_ticket (ticket_id)
);

-- ===== Rollback (manual) =====
-- DROP TABLE IF EXISTS support_ticket_notification;

-- APPLIED: vowconsole3 on 2026-09-20 (fixed 1146 "support_ticket_notification doesn't exist").
