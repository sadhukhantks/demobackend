-- Hands Report norms master (MIS port).
-- Effective-dated manpower norms per (co_id, designation_id): fixed shift counts
-- and/or a variable machine-linked norm (machine_type_id + hands_per_machine).
-- Consumed by GET /api/juteProductionReports/hands_report.
-- Audit columns intentionally absent — audit is handled by DB triggers.

CREATE TABLE IF NOT EXISTS jute_prod_hands_norms_mst (
  hands_norm_id INT AUTO_INCREMENT PRIMARY KEY,
  co_id INT NOT NULL,
  branch_id INT NULL,
  designation_id INT NOT NULL,
  direct_indirect VARCHAR(1) NULL,
  variable_fixed VARCHAR(1) NULL,
  shift_a DECIMAL(7,2) NOT NULL DEFAULT 0,
  shift_b DECIMAL(7,2) NOT NULL DEFAULT 0,
  shift_c DECIMAL(7,2) NOT NULL DEFAULT 0,
  shift_g DECIMAL(7,2) NOT NULL DEFAULT 0,
  machine_type_id INT NULL,
  hands_per_machine DECIMAL(9,4) NULL,
  norms_text VARCHAR(80) NULL,
  effective_date DATE NOT NULL,
  active INT NOT NULL DEFAULT 1,
  KEY ix_hands_norm_scope (co_id, designation_id, effective_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- rollback: DROP TABLE jute_prod_hands_norms_mst;
