-- Jute Production — Waste Entry event log (per SPEC.md §4).
-- Target tenant DB: dev3.
-- Rollback: DROP TABLE IF EXISTS jute_prod_waste_entry;
CREATE TABLE jute_prod_waste_entry (
    waste_entry_id      INT           NOT NULL AUTO_INCREMENT,
    co_id               INT           NOT NULL,
    branch_id           INT           NULL,
    tran_date           DATE          NOT NULL,
    spell_id            INT           NOT NULL,
    dept_id             INT           NOT NULL,
    machine_id          INT           NULL,
    waste_type_code     VARCHAR(20)   NOT NULL,   -- caddis | hard_waste | soft_waste | sweeping | yarn_waste | cloth_waste | other (fixed enum, app-enforced)
    waste_qty           DECIMAL(10,3) NOT NULL,   -- kilograms, > 0 (enforced by the API)
    remarks             VARCHAR(255)  NULL,
    active              TINYINT       NOT NULL DEFAULT 1,
    updated_by          INT           NULL,
    updated_date_time   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (waste_entry_id),
    KEY idx_waste_co_branch_date   (co_id, branch_id, tran_date),
    KEY idx_waste_dept_date_spell  (dept_id, tran_date, spell_id),   -- non-unique (event log): allows many waste rows per dept/date/spell
    KEY idx_waste_machine          (machine_id),
    KEY idx_waste_spell            (spell_id),
    CONSTRAINT fk_waste_dept    FOREIGN KEY (dept_id)    REFERENCES dept_mst    (dept_id),
    CONSTRAINT fk_waste_machine FOREIGN KEY (machine_id) REFERENCES machine_mst (machine_id),
    CONSTRAINT fk_waste_spell   FOREIGN KEY (spell_id)   REFERENCES spell_mst   (spell_id)
);
