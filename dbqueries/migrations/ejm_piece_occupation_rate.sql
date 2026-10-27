-- EJM piece-rate per department/occupation, ported 1:1 from the FoxPro
-- E:\ejmhands\WG150426\OCCURATE.DBF (9 rows, DEPTCODE C(2), OCCUCODE C(2),
-- F_B_RATE N(10,6)). Legacy codes are zero-padded strings, so they stay CHAR(2).
--
-- dept_id / desig_id resolve the legacy codes onto sls EJM (branch 29) masters.
-- Source of the mapping: E:\ejmhands\WG150426\foxOCCUMAST_matched.xlsx, sheet
-- "Sheet7", column `dpco` (= DEPTCODE || OCCUCODE) -> dept_id / desig_id. All 9
-- codes resolve 1:1 and were verified against sls.dept_mst (dept_code matches
-- DEPTCODE exactly) and sls.designation_mst (all active, branch 29).
--
-- p_occu_id is a surrogate single-column row id (the real key is composite) so
-- the "Ejm Piece Occu Rate Entry" page can address a row by one value. It is
-- NOT auto-increment: the backend assigns MAX+1 on insert.
--
-- ponytail: no branch_id/audit columns -- single-mill legacy master, every row is
-- branch 29 via designation_mst. Add when a second branch needs its own rates.
-- Target DB: sls
--
-- ROLLBACK: DROP TABLE ejm_piece_occupation_rate;

CREATE TABLE IF NOT EXISTS ejm_piece_occupation_rate (
  dept_code CHAR(2)       NOT NULL,
  occu_code CHAR(2)       NOT NULL,
  f_b_rate  DECIMAL(10,6) NOT NULL,
  p_occu_id INT           DEFAULT NULL,
  dept_id   INT           DEFAULT NULL,
  desig_id  INT           DEFAULT NULL,
  PRIMARY KEY (dept_code, occu_code),
  UNIQUE KEY uq_epor_desig (desig_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- SLS ONLY below this line. dept_id/desig_id are sls (branch 29) master ids and
-- mean nothing in another tenant, so for any other DB run the CREATE TABLE above
-- and stop.
--
-- dept_code, occu_code, p_occu_id, dept_id, desig_id, f_b_rate   -- sls designation
INSERT INTO ejm_piece_occupation_rate (dept_code, occu_code, p_occu_id, dept_id, desig_id, f_b_rate) VALUES
  ('01', '01', 1,  14,   2, 3.258850),   -- JUTE(EJM)           / JUTE ISSUE MAZDOOR
  ('05', '01', 2,  19, 199, 3.303060),   -- SPOOL WINDING(EJM)  / SPOOL WINDER HWP
  ('05', '02', 3,  19, 200, 3.303060),   -- SPOOL WINDING(EJM)  / SPOOL WINDER SWP
  ('06', '01', 4, 154, 201, 3.272500),   -- COP WINDING(EJM)    / HESS WFT WINDER
  ('07', '01', 5,  20, 501, 3.297210),   -- BEAMING(EJM)        / HESSIAN 1ST BEAMER
  ('07', '02', 6,  20,  67, 3.270380),   -- BEAMING(EJM)        / HESSIAN BEAMER
  ('08', '01', 7,  21,  71, 3.303060),   -- WEAVING(EJM)        / WEAVER(H)
  ('09', '02', 8,  22, 114, 3.275630),   -- FINISHING(EJM)      / 2ND PRESS MAN
  ('10', '02', 9,  75, 105, 3.283470)    -- SACK SEWING(EJM)    / HEMMING OPERATOR
ON DUPLICATE KEY UPDATE p_occu_id = VALUES(p_occu_id), dept_id = VALUES(dept_id),
                        desig_id = VALUES(desig_id), f_b_rate = VALUES(f_b_rate);

-- Any row that predates p_occu_id must still be addressable by the page, and
-- each needs its OWN number -- hence the counter rather than one MAX+1.
SET @p_occu_next := (SELECT COALESCE(MAX(p_occu_id), 0) FROM ejm_piece_occupation_rate);
UPDATE ejm_piece_occupation_rate
   SET p_occu_id = (@p_occu_next := @p_occu_next + 1)
 WHERE p_occu_id IS NULL;
