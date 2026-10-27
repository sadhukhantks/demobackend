-- Migrate the legacy pay register / payslip column config for NJM (legacy
-- company 1 / branch 4) from EMPMILL12.tbl_payslip_print_component_updated
-- into sls.tbl_payslip_print_component as company 106 / branch 87, with the
-- pay scheme id shifted by +3000 (the vowsls -> sls scheme id convention), so
-- each scheme's Pay Register export lays out exactly like its legacy
-- Payregister_<scheme>_<date>.xlsx from Njmallwagesprocess::njmnallpayregister.
--
-- Per scheme, in this order:
--   1-7  identity columns: legacy component_id = 0 rows, mapped BY payslip_order
--        (the PHP ignored their desc_print): 1 eb_no, 2 emp_name, 3 dept_code,
--        4 department, 5 designation, 6 uan_no, 7 esi_no -> meta sentinels
--        -1 -2 -7 -3 -8 -5 -6; header text = the legacy SQL alias the sheet printed.
--   8    eb_id (-9): legacy selected it after the identity columns for every scheme.
--   9+   the component rows (component ids are preserved 1:1 between vowsls and
--        sls) in legacy payslip_order; a tie (163 has Canteen / OT Days both 12)
--        resolves higher pay_id first, which is how the legacy sheet came out.
-- Only rows the legacy register used (is_active = 1 AND payslip_print = 1) come
-- across; legacy's unprinted rows (e.g. 163's PF Employer / PF Employee) stay out.
-- payslip_order is renumbered 1..N per scheme.
--
-- Scheme id: legacy + 3000, except the two whose sls scheme was created under
-- another id (matched by name for co 106 / branch 87): 164 njm_main_payroll
-- -> 2015, 169 njm_newbudli_payroll -> 2017. A target scheme missing from
-- pay_scheme_master is skipped — config for a scheme id that does not exist
-- can never be used.
-- Idempotent: a scheme that already has active rows for 106 / 87 is skipped.
-- Target DB: sls (same server as EMPMILL12; needs MySQL 8 for ROW_NUMBER).
-- Rollback:
--   DELETE FROM tbl_payslip_print_component
--    WHERE company_id = 106 AND branch_id = 87 AND updated_by = 4
--      AND payscheme_id IN (3024, 3163, 3166, 3167, 3174, 2015, 2017);

INSERT INTO tbl_payslip_print_component
    (payscheme_id, company_id, branch_id, component_id, desc_print, payslip_order,
     fixed_var_cols, total_print, payslip_print, is_active, updated_by)
SELECT t.target_id, 106, 87, t.component_id, t.desc_print,
       ROW_NUMBER() OVER (PARTITION BY t.target_id ORDER BY t.sort_order, t.tie),
       t.fixed_var_cols, t.total_print, 1, 1, 4
FROM (
SELECT COALESCE(o.target_id, s.payscheme_id + 3000) AS target_id, s.*
FROM (
    SELECT l.payscheme_id,
           CASE l.payslip_order WHEN 1 THEN -1 WHEN 2 THEN -2 WHEN 3 THEN -7 WHEN 4 THEN -3
                                WHEN 5 THEN -8 WHEN 6 THEN -5 WHEN 7 THEN -6 END AS component_id,
           CASE l.payslip_order WHEN 1 THEN 'eb_no' WHEN 2 THEN 'emp_name' WHEN 3 THEN 'dept_code'
                                WHEN 4 THEN 'department' WHEN 5 THEN 'designation'
                                WHEN 6 THEN 'uan_no' WHEN 7 THEN 'esi_no' END AS desc_print,
           l.payslip_order AS sort_order, 0 AS tie, 'F' AS fixed_var_cols, 0 AS total_print
    FROM EMPMILL12.tbl_payslip_print_component_updated l
    WHERE l.company_id = 1 AND l.branch_id = 4 AND l.component_id = 0
      AND l.is_active = 1 AND l.payslip_print = 1 AND l.payslip_order BETWEEN 1 AND 7
    UNION ALL
    SELECT DISTINCT l.payscheme_id, -9, 'eb_id', 8, 0, 'F', 0
    FROM EMPMILL12.tbl_payslip_print_component_updated l
    WHERE l.company_id = 1 AND l.branch_id = 4 AND l.is_active = 1 AND l.payslip_print = 1
    UNION ALL
    SELECT l.payscheme_id, l.component_id,
           COALESCE(NULLIF(TRIM(l.desc_print), ''), pc.CODE, CONCAT('Component ', l.component_id)),
           8 + COALESCE(l.payslip_order, 0), -l.pay_id,
           COALESCE(NULLIF(TRIM(l.fixed_var_cols), ''), 'V'), COALESCE(l.total_print, 0)
    FROM EMPMILL12.tbl_payslip_print_component_updated l
    LEFT JOIN pay_components pc ON pc.ID = l.component_id
    WHERE l.company_id = 1 AND l.branch_id = 4 AND l.component_id > 0
      AND l.is_active = 1 AND l.payslip_print = 1
) AS s
LEFT JOIN (
    SELECT 164 AS legacy_id, 2015 AS target_id
    UNION ALL SELECT 169, 2017
) AS o ON o.legacy_id = s.payscheme_id
) AS t
WHERE EXISTS (
    SELECT 1 FROM pay_scheme_master m WHERE m.payscheme_id = t.target_id
)
  AND NOT EXISTS (
    SELECT 1 FROM tbl_payslip_print_component x
    WHERE x.payscheme_id = t.target_id AND x.company_id = 106
      AND x.branch_id = 87 AND x.is_active = 1
)
ORDER BY t.target_id, t.sort_order, t.tie;
