-- Migration: add invoice_rate to proc_inward_dtl
-- Per-line supplier invoice rate captured on the SR screen. Baseline for
-- auto DR/CR note rate-difference detection (spec 2026-08-08-drcr-note-rework).
-- Rollback: ALTER TABLE proc_inward_dtl DROP COLUMN invoice_rate

ALTER TABLE proc_inward_dtl
    ADD COLUMN invoice_rate DOUBLE NULL AFTER accepted_rate;
