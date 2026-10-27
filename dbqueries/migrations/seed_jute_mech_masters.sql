-- Migration: Jute Mechanical & Maintenance — seed component + breakdown-reason masters
-- Date: 2026-08-24
-- Database: tenant DB (dev3, sls)
-- Depends on: create_jute_mech_maintenance_tables.sql
--
-- Reason codes and descriptions are transcribed verbatim from the mill's existing
-- "MILL SIDE MECHANICAL BREAK DOWN SUMMARY" workbook, sheet REASON (obvious spelling
-- slips in the source are corrected in reason_desc; reason_code is left EXACTLY as the
-- shift staff already write it, so the codes stay familiar).
--
-- Seeds for EVERY company in co_mst (co_mst has no active column), and is idempotent — re-running
-- inserts nothing new (NOT EXISTS guards on the unique keys).
--
-- Reasons are scoped by machine_type_id, resolved BY NAME from machine_type_mst.
-- A section whose machine type does not exist in this tenant simply seeds no rows
-- for it (the INNER JOIN yields nothing) — no orphan/global rows are created.
--
-- Excel section          -> machine_type_mst.machine_type_name
--   SPREADER             -> 'Spreader'
--   BREAKER              -> 'Breaker Card', 'Teaser'
--   DRAW HEAD & F/CARD   -> 'Carding Machine', 'finishercard'
--   DRAWING              -> 'Drawing', 'R/G'
--
-- MT / PP / MW / NR appear in several section lists in the workbook; they are seeded
-- ONCE as global codes (machine_type_id IS NULL) so they are not offered twice.
--
-- ROLLBACK:
--   DELETE FROM jute_mech_reason_mst    WHERE updated_by = 0;
--   DELETE FROM jute_mech_component_mst WHERE updated_by = 0;

-- =============================================================================
-- PART 1 — COMPONENT MASTER
-- Cross-section failure components. Enables mill-wide Pareto ("how much downtime
-- did GEARS cost me?") which section-specific reason codes alone cannot answer.
-- =============================================================================
INSERT INTO jute_mech_component_mst (co_id, component_name, component_group, active, updated_by)
SELECT c.co_id, s.component_name, s.component_group, 1, 0
FROM co_mst c
CROSS JOIN (
              SELECT 'Gear'             AS component_name, 'Transmission' AS component_group
    UNION ALL SELECT 'Chain',              'Transmission'
    UNION ALL SELECT 'Sprocket',           'Transmission'
    UNION ALL SELECT 'Shaft',              'Transmission'
    UNION ALL SELECT 'Pinion',             'Transmission'
    UNION ALL SELECT 'Belt',               'Transmission'
    UNION ALL SELECT 'Timer Belt',         'Transmission'
    UNION ALL SELECT 'Coupling',           'Transmission'
    UNION ALL SELECT 'Bearing',            'Bearing'
    UNION ALL SELECT 'Bush',               'Bearing'
    UNION ALL SELECT 'Roller',             'Roller'
    UNION ALL SELECT 'Roll Former',        'Roller'
    UNION ALL SELECT 'Pressing Roller',    'Roller'
    UNION ALL SELECT 'Carriage',           'Structural'
    UNION ALL SELECT 'Boss',               'Structural'
    UNION ALL SELECT 'Lever',              'Structural'
    UNION ALL SELECT 'Pin',                'Structural'
    UNION ALL SELECT 'Bar',                'Structural'
    UNION ALL SELECT 'Selvedge Plate',     'Structural'
    UNION ALL SELECT 'Coiler Plate',       'Structural'
    UNION ALL SELECT 'Can',                'Structural'
    UNION ALL SELECT 'Doffer',             'Structural'
    UNION ALL SELECT 'Cylinder',           'Structural'
    UNION ALL SELECT 'Frame',              'Structural'
    UNION ALL SELECT 'Guard',              'Structural'
    UNION ALL SELECT 'Pump',               'Hydraulic'
    UNION ALL SELECT 'Hydraulic Hose',     'Hydraulic'
    UNION ALL SELECT 'Motor',              'Electrical'
    UNION ALL SELECT 'Panel',              'Electrical'
    UNION ALL SELECT 'Wiring',             'Electrical'
    UNION ALL SELECT 'Drive / VFD',        'Electrical'
    UNION ALL SELECT 'Starter',            'Electrical'
    UNION ALL SELECT 'Fuse',               'Electrical'
    UNION ALL SELECT 'Contactor',          'Electrical'
    UNION ALL SELECT 'Auto Stop Motion',   'Control'
) s
WHERE NOT EXISTS (
    SELECT 1 FROM jute_mech_component_mst x
    WHERE x.co_id = c.co_id AND x.component_name = s.component_name
);

-- =============================================================================
-- PART 2 — GLOBAL REASON CODES (machine_type_id IS NULL — offered on every machine)
-- =============================================================================
INSERT INTO jute_mech_reason_mst (co_id, machine_type_id, reason_code, reason_desc, discipline, component_id, active, updated_by)
SELECT c.co_id, NULL, s.reason_code, s.reason_desc, s.discipline,
       (SELECT cm.component_id FROM jute_mech_component_mst cm
         WHERE cm.co_id = c.co_id AND cm.component_name = s.component_name),
       1, 0
FROM co_mst c
CROSS JOIN (
    -- Mechanical, common to every section (workbook lists these under several sections)
              SELECT 'NR'    AS reason_code, 'No Reason mentioned'            AS reason_desc, 'MECHANICAL'  AS discipline, NULL              AS component_name
    UNION ALL SELECT 'MT',    'Motor Trouble',                  'MECHANICAL',  'Motor'
    UNION ALL SELECT 'PP',    'Pump Problem',                   'MECHANICAL',  'Pump'
    UNION ALL SELECT 'MW',    'Machine Work',                   'MECHANICAL',  NULL
    -- Electrical stream (kept as its own discipline so electrical downtime is separable
    -- from mechanical; today it all hides inside "MT / Motor Trouble")
    UNION ALL SELECT 'EMOT',  'Motor Burnt / Winding Failure',  'ELECTRICAL',  'Motor'
    UNION ALL SELECT 'EPNL',  'Panel Fault',                    'ELECTRICAL',  'Panel'
    UNION ALL SELECT 'EWIR',  'Wiring Fault',                   'ELECTRICAL',  'Wiring'
    UNION ALL SELECT 'EDRV',  'Drive / VFD Fault',              'ELECTRICAL',  'Drive / VFD'
    UNION ALL SELECT 'ESTR',  'Starter Trouble',                'ELECTRICAL',  'Starter'
    UNION ALL SELECT 'EFUS',  'Fuse Blown',                     'ELECTRICAL',  'Fuse'
    UNION ALL SELECT 'ECON',  'Contactor / Switch Fault',       'ELECTRICAL',  'Contactor'
    UNION ALL SELECT 'EERT',  'Earthing Trouble',               'ELECTRICAL',  NULL
    UNION ALL SELECT 'EPWR',  'Power Failure',                  'ELECTRICAL',  NULL
    -- Utility / services
    UNION ALL SELECT 'USTM',  'Steam / Boiler Not Available',   'UTILITY',     NULL
    UNION ALL SELECT 'UAIR',  'Compressed Air Failure',         'UTILITY',     NULL
    UNION ALL SELECT 'UWTR',  'Water Supply Failure',           'UTILITY',     NULL
    -- Operational (not a machine fault — keeps non-mechanical downtime out of the MTTR maths)
    UNION ALL SELECT 'ONOM',  'No Material',                    'OPERATIONAL', NULL
    UNION ALL SELECT 'ONOO',  'No Operator',                    'OPERATIONAL', NULL
    UNION ALL SELECT 'OCHG',  'Quality / Batch Changeover',     'OPERATIONAL', NULL
    UNION ALL SELECT 'OCLN',  'Cleaning / Housekeeping',        'OPERATIONAL', NULL
) s
WHERE NOT EXISTS (
    SELECT 1 FROM jute_mech_reason_mst x
    WHERE x.co_id = c.co_id AND x.machine_type_id IS NULL AND x.reason_code = s.reason_code
);

-- =============================================================================
-- PART 3 — SPREADER reason codes (workbook sheet REASON, column A)
-- =============================================================================
INSERT INTO jute_mech_reason_mst (co_id, machine_type_id, reason_code, reason_desc, discipline, component_id, active, updated_by)
SELECT c.co_id, mt.machine_type_id, s.reason_code, s.reason_desc, 'MECHANICAL',
       (SELECT cm.component_id FROM jute_mech_component_mst cm
         WHERE cm.co_id = c.co_id AND cm.component_name = s.component_name),
       1, 0
FROM co_mst c
JOIN machine_type_mst mt ON mt.machine_type_name = 'Spreader'
CROSS JOIN (
              SELECT 'RF'  AS reason_code, 'Roll Former'         AS reason_desc, 'Roll Former' AS component_name
    UNION ALL SELECT 'BJ',   'Bar Jam',              'Bar'
    UNION ALL SELECT 'DRJ',  'Drawing Roller Jam',   'Roller'
    UNION ALL SELECT 'PB',   'Pin Broken',           'Pin'
    UNION ALL SELECT 'CB',   'Chain Broken',         'Chain'
    UNION ALL SELECT 'SG',   'Sprocket & Gear',      'Sprocket'
    UNION ALL SELECT 'BRJ',  'Builder Roller Jam',   'Roller'
) s
WHERE NOT EXISTS (
    SELECT 1 FROM jute_mech_reason_mst x
    WHERE x.co_id = c.co_id AND x.machine_type_id = mt.machine_type_id AND x.reason_code = s.reason_code
);

-- =============================================================================
-- PART 4 — BREAKER CARD reason codes (also applied to 'Teaser' — the T/C machines)
-- =============================================================================
INSERT INTO jute_mech_reason_mst (co_id, machine_type_id, reason_code, reason_desc, discipline, component_id, active, updated_by)
SELECT c.co_id, mt.machine_type_id, s.reason_code, s.reason_desc, 'MECHANICAL',
       (SELECT cm.component_id FROM jute_mech_component_mst cm
         WHERE cm.co_id = c.co_id AND cm.component_name = s.component_name),
       1, 0
FROM co_mst c
JOIN machine_type_mst mt ON mt.machine_type_name IN ('Breaker Card', 'Teaser')
CROSS JOIN (
              SELECT 'GT'  AS reason_code, 'Gear Trouble'              AS reason_desc, 'Gear' AS component_name
    UNION ALL SELECT 'FRJ',  'Feed Roller Jam',            'Roller'
    UNION ALL SELECT 'DCP',  'Duplex Chain Problem',       'Chain'
    UNION ALL SELECT 'RFT',  'Roll Former Trouble',        'Roll Former'
    UNION ALL SELECT 'CJ',   'Cylinder Jam',               'Cylinder'
    UNION ALL SELECT 'SJ',   'Side Jam',                   'Frame'
    UNION ALL SELECT 'DCB',  'Doffer Compound Broken',     'Doffer'
    UNION ALL SELECT 'RBB',  'Roller Bearing Broken',      'Bearing'
    UNION ALL SELECT 'SPP',  'Selvedge Plate Problem',     'Selvedge Plate'
    UNION ALL SELECT 'DRP',  'Drawing Roller Problem',     'Roller'
    UNION ALL SELECT 'DP',   'Droppage Problem',           NULL
    UNION ALL SELECT 'RHP',  'Reversing Handle Problem',   'Lever'
) s
WHERE NOT EXISTS (
    SELECT 1 FROM jute_mech_reason_mst x
    WHERE x.co_id = c.co_id AND x.machine_type_id = mt.machine_type_id AND x.reason_code = s.reason_code
);

-- =============================================================================
-- PART 5 — DRAW HEAD & FINISHER CARD reason codes
-- (workbook sheet REASON, column D — the longest list, 20 codes)
-- =============================================================================
INSERT INTO jute_mech_reason_mst (co_id, machine_type_id, reason_code, reason_desc, discipline, component_id, active, updated_by)
SELECT c.co_id, mt.machine_type_id, s.reason_code, s.reason_desc, 'MECHANICAL',
       (SELECT cm.component_id FROM jute_mech_component_mst cm
         WHERE cm.co_id = c.co_id AND cm.component_name = s.component_name),
       1, 0
FROM co_mst c
JOIN machine_type_mst mt ON mt.machine_type_name IN ('Carding Machine', 'finishercard')
CROSS JOIN (
              SELECT 'DHGT'   AS reason_code, 'Drawhead Gear Trouble'                  AS reason_desc, 'Gear' AS component_name
    UNION ALL SELECT 'CJ',      'Carriage Jam',                            'Carriage'
    UNION ALL SELECT 'DHCH',    'Drawhead Chain Broken',                   'Chain'
    UNION ALL SELECT 'CPT',     'Coiler Plate Trouble',                    'Coiler Plate'
    UNION ALL SELECT 'DRBB',    'Drawing Roller Bearing Broken',           'Bearing'
    UNION ALL SELECT 'CTP',     'Can Trumping Problem',                    'Can'
    UNION ALL SELECT 'TBA',     'Timer Belt Adjustment',                   'Timer Belt'
    UNION ALL SELECT 'FCSJ',    'Finisher Card Cylinder Side Jam',         'Cylinder'
    UNION ALL SELECT 'FCGT',    'Finisher Card Gear Trouble',              'Gear'
    UNION ALL SELECT 'FCBB',    'Finisher Card Bearing Broken',            'Bearing'
    UNION ALL SELECT 'FCCB',    'Finisher Card Chain Broken',              'Chain'
    UNION ALL SELECT 'DHDBT',   'Drawhead Delivery Boss Trouble',          'Boss'
    UNION ALL SELECT 'DHDPBT',  'Drawhead Pressing Boss Trouble',          'Boss'
    UNION ALL SELECT 'FCPJ',    'Finisher Card Picking Jam',               NULL
    UNION ALL SELECT 'DHRPRT',  'Drawhead Rubber Pressing Roller Trouble', 'Pressing Roller'
    UNION ALL SELECT 'DHDRJ',   'Drawhead Drawing Roller Jam',             'Roller'
    UNION ALL SELECT 'DHTBT',   'Drawhead Timer Belt Trouble',             'Timer Belt'
    UNION ALL SELECT 'FCSP',    'Finisher Card Selvedge Problem',          'Selvedge Plate'
    UNION ALL SELECT 'DHCT',    'Drawhead Can Trumping',                   'Can'
) s
WHERE NOT EXISTS (
    SELECT 1 FROM jute_mech_reason_mst x
    WHERE x.co_id = c.co_id AND x.machine_type_id = mt.machine_type_id AND x.reason_code = s.reason_code
);

-- =============================================================================
-- PART 6 — DRAWING reason codes (workbook sheet REASON, column A lower block)
-- Applied to 'Drawing' and 'R/G' (the R/G machines appear in the DRAWING sheet)
-- =============================================================================
INSERT INTO jute_mech_reason_mst (co_id, machine_type_id, reason_code, reason_desc, discipline, component_id, active, updated_by)
SELECT c.co_id, mt.machine_type_id, s.reason_code, s.reason_desc, 'MECHANICAL',
       (SELECT cm.component_id FROM jute_mech_component_mst cm
         WHERE cm.co_id = c.co_id AND cm.component_name = s.component_name),
       1, 0
FROM co_mst c
JOIN machine_type_mst mt ON mt.machine_type_name IN ('Drawing', 'R/G')
CROSS JOIN (
              SELECT 'GT'   AS reason_code, 'Gear Trouble'                          AS reason_desc, 'Gear' AS component_name
    UNION ALL SELECT 'CT',    'Carriage Trouble',                       'Carriage'
    UNION ALL SELECT 'CPLB',  'Can Packer Lever Broken',                'Lever'
    UNION ALL SELECT 'CSGWO', 'Cam Shaft Gear & Bush Worn Out',         'Gear'
    UNION ALL SELECT 'DPRAT', 'Delivery Pressure Roller Assembly Trouble', 'Pressing Roller'
    UNION ALL SELECT 'CPT',   'Coiler Plate Trouble',                   'Coiler Plate'
    UNION ALL SELECT 'DSPLO', 'Delivery Shaft Pinion Line Out',         'Pinion'
    UNION ALL SELECT 'RRT',   'Revolving Roller Trouble',               'Roller'
    UNION ALL SELECT 'BSGWO', 'Back Shaft Gear Worn Out',               'Gear'
    UNION ALL SELECT 'RPRT',  'Rubber Pressing Roller Trouble',         'Pressing Roller'
    UNION ALL SELECT 'CPHA',  'Can Packing Humble Assembly',            'Can'
    UNION ALL SELECT 'DRT',   'Drawing Roller Trouble',                 'Roller'
    UNION ALL SELECT 'ASMT',  'Auto Stop Motion Trouble',               'Auto Stop Motion'
    UNION ALL SELECT 'CB',    'Chain Broken',                           'Chain'
) s
WHERE NOT EXISTS (
    SELECT 1 FROM jute_mech_reason_mst x
    WHERE x.co_id = c.co_id AND x.machine_type_id = mt.machine_type_id AND x.reason_code = s.reason_code
);

-- VERIFICATION
-- SELECT COUNT(*) FROM jute_mech_component_mst;                  -- 35 per company
-- SELECT mt.machine_type_name, COUNT(*) AS codes
--   FROM jute_mech_reason_mst r
--   LEFT JOIN machine_type_mst mt ON mt.machine_type_id = r.machine_type_id
--  GROUP BY mt.machine_type_name ORDER BY codes DESC;
-- SELECT COUNT(*) FROM jute_mech_reason_mst WHERE component_id IS NULL;  -- only the
--   codes with no meaningful component (NR, MW, DP, FCPJ, utility, operational)
