-- =============================================================================
-- Jute Production checklist config map (jute_prod_checklist_map)
--
-- The wage-reconciliation checklists (finishing / winding / beaming) used to
-- pick their attendance designations and machine types by NAME pattern. On sls
-- the finishing pattern ('%finish%') proved 100% false positives: it caught the
-- carding/drawing "FINISHER FEEDER/RECEIVER" designations in the Preparing
-- department plus finishing-department mechanics, and ZERO of the real hemming
-- / herackle / sack-sewing staff. Names cannot be fixed centrally, so the set
-- is now a per-branch config table.
--
-- DB-ONLY config: there is no UI and none is planned — rows are inserted by
-- hand (see seed_jute_prod_checklist_map_sls.sql for a ready-to-edit template).
--
--   checklist   'finishing' | 'winding' | 'beaming'
--   ref_type    'designation' -> designation_mst.designation_id
--               'machine_type' -> machine_type_mst.machine_type_id
--   active      0 disables a row without deleting it
--
-- No co_id: branch_id implies the company (branch_mst.co_id), and the report
-- scopes co-wide reads as branch_id IN (branches of :co_id).
--
-- Behaviour once deployed:
--   finishing — map-driven ONLY. No rows for the scope => the report returns
--               an empty grid (never the old false positives).
--   winding / beaming — map rows override, no rows => unchanged name matching
--               ('winder%' / machine_type_name 'beaming').
--
-- Target: tenant DBs (dev3, sls, and every other tenant BEFORE the code deploys)
--
-- ROLLBACK:
--   DROP TABLE IF EXISTS jute_prod_checklist_map;
-- =============================================================================

CREATE TABLE IF NOT EXISTS jute_prod_checklist_map (
    checklist_map_id  INT         NOT NULL AUTO_INCREMENT,
    branch_id         INT         NOT NULL,
    checklist         VARCHAR(20) NOT NULL,
    ref_type          VARCHAR(20) NOT NULL,
    ref_id            INT         NOT NULL,
    active            INT         NOT NULL DEFAULT 1,
    PRIMARY KEY (checklist_map_id),
    UNIQUE KEY uq_chk_map (branch_id, checklist, ref_type, ref_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
