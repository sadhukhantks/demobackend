-- Jute Production — Wastage Area master.
-- Each area belongs to exactly one department (and therefore one branch).
-- Target tenant DBs: dev3, sls.
-- Rollback: DROP TABLE IF EXISTS wastage_area_mst
CREATE TABLE wastage_area_mst (
    wastage_area_id   INT          NOT NULL AUTO_INCREMENT,
    co_id             INT          NOT NULL,
    branch_id         INT          NOT NULL,
    dept_id           INT          NOT NULL,
    area_name         VARCHAR(100) NOT NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (wastage_area_id),
    UNIQUE KEY uk_wastage_area_name (branch_id, dept_id, area_name),
    KEY idx_wastage_area_co_branch (co_id, branch_id),
    KEY idx_wastage_area_dept (dept_id),
    CONSTRAINT fk_wastage_area_dept FOREIGN KEY (dept_id) REFERENCES dept_mst (dept_id)
);
