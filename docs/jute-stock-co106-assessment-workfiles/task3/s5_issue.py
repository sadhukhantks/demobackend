from lib import *
from collections import Counter, defaultdict
c = conn()
print("=== OLD jute_issue co106 breakdown ===")
for r in q(c, "SELECT branch_id, issue_status, is_active, COUNT(*) n, MIN(issue_date) mn, MAX(issue_date) mx, ROUND(SUM(total_weight),2) qtl FROM vowsls.jute_issue WHERE company_id=106 GROUP BY 1,2,3 ORDER BY n DESC"):
    print("  ", r)
print("old co106 <=cut:", q(c, f"SELECT COUNT(*) n, ROUND(SUM(total_weight),2) qtl FROM vowsls.jute_issue WHERE company_id=106 AND issue_date<='{CUT}'")[0])
print("=== SLS jute_issue b87 ===")
for r in q(c, f"SELECT status_id, COUNT(*) n, MIN(issue_date) mn, MAX(issue_date) mx, ROUND(SUM(weight),2) w FROM sls.jute_issue WHERE branch_id=87 GROUP BY 1"):
    print("  ", r)
print("sls b87 <=cut:", q(c, f"SELECT COUNT(*) n, ROUND(SUM(weight),2) w FROM sls.jute_issue WHERE branch_id=87 AND issue_date<='{CUT}'")[0])

miss = q(c, f"""
SELECT i.issue_no, i.issue_date, i.branch_id, i.issue_status, i.is_active, i.mr_no, i.stock_id, i.quantity,
       i.total_weight qtl, i.issue_value, i.jute_quality, i.yarn_type_id, i.uom_code,
       EXISTS(SELECT 1 FROM sls.jute_issue s WHERE s.jute_issue_id=i.issue_no) id_exists_any,
       (SELECT s.branch_id FROM sls.jute_issue s WHERE s.jute_issue_id=i.issue_no) sls_branch,
       EXISTS(SELECT 1 FROM sls.jute_mr_li x WHERE x.jute_mr_li_id=i.stock_id) line_in_sls
FROM vowsls.jute_issue i
WHERE i.company_id=106 AND i.issue_date<='{CUT}'
  AND NOT EXISTS(SELECT 1 FROM sls.jute_issue s WHERE s.jute_issue_id=i.issue_no AND s.branch_id=87)
ORDER BY i.issue_no""")
print("\nMISSING issues in sls:", len(miss), " qtl:", round(sum(r["qtl"] or 0 for r in miss),2))
csvout("issue_missing_in_sls.csv", miss)
print("  by status:", Counter(str(r["issue_status"]) for r in miss))
print("  by is_active:", Counter(str(r["is_active"]) for r in miss))
print("  approved+active qtl:", round(sum(r["qtl"] or 0 for r in miss if r["issue_status"]==3 and r["is_active"]==1),2))
print("  line_in_sls:", Counter(str(r["line_in_sls"]) for r in miss))

ext = q(c, f"""
SELECT s.jute_issue_id, s.issue_date, s.status_id, s.jute_mr_li_id, s.quantity, s.weight, s.item_id, s.waste_entry_id,
       EXISTS(SELECT 1 FROM vowsls.jute_issue i WHERE i.issue_no=s.jute_issue_id) old_id_exists,
       (SELECT i.company_id FROM vowsls.jute_issue i WHERE i.issue_no=s.jute_issue_id) old_co
FROM sls.jute_issue s
WHERE s.branch_id=87 AND s.issue_date<='{CUT}'
  AND NOT EXISTS(SELECT 1 FROM vowsls.jute_issue i WHERE i.issue_no=s.jute_issue_id AND i.company_id=106 AND i.issue_date<='{CUT}')
ORDER BY s.jute_issue_id""")
print("\nEXTRA issues in sls:", len(ext), " qtl:", round(sum(r["weight"] or 0 for r in ext),2))
csvout("issue_extra_in_sls.csv", ext)
print("  by status:", Counter(str(r["status_id"]) for r in ext))
print("  old_id_exists:", Counter(str(r["old_id_exists"]) for r in ext))
