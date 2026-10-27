-- rollback for tkt125_srn_repoint_20260827 on sls
UPDATE issue_li SET inward_dtl_id = 15500, updated_by = 22 WHERE issue_li_id = 159931 AND inward_dtl_id = 41915;  -- TKT-000125
UPDATE issue_li SET inward_dtl_id = 15500, updated_by = 22 WHERE issue_li_id = 159984 AND inward_dtl_id = 41915;  -- TKT-000125
UPDATE issue_li SET inward_dtl_id = 15500, updated_by = 22 WHERE issue_li_id = 159992 AND inward_dtl_id = 41915;  -- TKT-000125
UPDATE issue_li SET inward_dtl_id = 15500, updated_by = 22 WHERE issue_li_id = 159993 AND inward_dtl_id = 41915;  -- TKT-000125
