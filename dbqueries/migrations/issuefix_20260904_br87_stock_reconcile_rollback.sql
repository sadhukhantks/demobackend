-- Rollback for ISSUEFIX-20260904. Target DB: sls.
-- Substitute the issue_id printed by the apply run for :new_issue_id.

DELETE FROM issue_li  WHERE issue_id = :new_issue_id AND item_id = 333521 AND inward_dtl_id = 41898;
DELETE FROM issue_hdr WHERE issue_id = :new_issue_id AND branch_id = 87 AND issue_date = '2026-09-04';

UPDATE issue_li
SET issue_qty = 4,
    remarks = NULLIF(REPLACE(COALESCE(remarks, ''),
                             'ISSUEFIX-20260904 qty 4->3 (keying correction)', ''), ''),
    updated_date_time = NOW()
WHERE issue_li_id = 163902 AND issue_id = 58320 AND issue_qty = 3;
