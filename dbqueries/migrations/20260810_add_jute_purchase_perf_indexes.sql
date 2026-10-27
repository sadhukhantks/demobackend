-- 20260810_add_jute_purchase_perf_indexes.sql
-- Jute purchase list pages catastrophically slow on sls (PO list: 160s measured).
-- Root cause: jute tables have ZERO secondary indexes (PKs only) although the ORM
-- models in src/models/jute.py declare index=True on these columns. This aligns
-- the DBs with the authoritative ORM schema.
-- Targets: dev3, sls (other tenants before code deploy, per convention).

CREATE INDEX ix_jute_mr_po_id ON jute_mr (po_id);

CREATE INDEX ix_jute_mr_branch_id ON jute_mr (branch_id);

CREATE INDEX ix_jute_po_branch_id_po_date ON jute_po (branch_id, po_date);

CREATE INDEX ix_jute_mr_li_jute_mr_id ON jute_mr_li (jute_mr_id);

CREATE INDEX ix_jute_issue_branch_id_issue_date ON jute_issue (branch_id, issue_date);

-- Rollback:
-- DROP INDEX ix_jute_mr_po_id ON jute_mr;
-- DROP INDEX ix_jute_mr_branch_id ON jute_mr;
-- DROP INDEX ix_jute_po_branch_id_po_date ON jute_po;
-- DROP INDEX ix_jute_mr_li_jute_mr_id ON jute_mr_li;
-- DROP INDEX ix_jute_issue_branch_id_issue_date ON jute_issue;
