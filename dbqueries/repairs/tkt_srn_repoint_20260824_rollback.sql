-- rollback for tkt_srn_repoint_20260824 on sls
UPDATE issue_li SET inward_dtl_id = 18764, updated_by = 22 WHERE issue_li_id = 161085 AND inward_dtl_id = 42145;  -- TKT-000108
UPDATE issue_li SET inward_dtl_id = 42098, updated_by = 22 WHERE issue_li_id = 159808 AND inward_dtl_id = 23622;  -- TKT-000105
UPDATE issue_li SET inward_dtl_id = 42153, updated_by = 22 WHERE issue_li_id = 159809 AND inward_dtl_id = 23789;  -- TKT-000105
UPDATE issue_li SET inward_dtl_id = 23789, updated_by = 22 WHERE issue_li_id = 160684 AND inward_dtl_id = 42098;  -- TKT-000105
UPDATE issue_li SET inward_dtl_id = 23622, updated_by = 22 WHERE issue_li_id = 160863 AND inward_dtl_id = 42153;  -- TKT-000105
UPDATE issue_li SET inward_dtl_id = 41931, updated_by = 22 WHERE issue_li_id = 159807 AND inward_dtl_id = 23625;  -- TKT-000107
UPDATE issue_li SET inward_dtl_id = 23625, updated_by = 22 WHERE issue_li_id = 161191 AND inward_dtl_id = 41931;  -- TKT-000107
