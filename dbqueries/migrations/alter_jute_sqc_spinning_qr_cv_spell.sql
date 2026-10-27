-- Migration: R-08-15 Yarn QR/CV — observed count + MR% now resolve from the
-- R-08-16 reading of the SAME (date, spell, frame, yarn) instead of the
-- date-level per-yarn AVG. The group header must therefore remember its spell.
--
-- Legacy rows keep spell_id NULL and show a blank observed count (by design).

ALTER TABLE jute_sqc_spinning_qr_cv
    ADD COLUMN spell_id INT NULL AFTER entry_date;

-- ============================================================================
-- ROLLBACK
-- ============================================================================
-- ALTER TABLE jute_sqc_spinning_qr_cv
--     DROP COLUMN spell_id;
