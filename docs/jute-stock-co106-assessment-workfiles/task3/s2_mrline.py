from lib import *
from collections import Counter
c = conn()
print("=== MR LINE SCOPE ===")
# old lines under in-scope old headers
print("old lines (co106, hdr<=cut):", q(c, f"SELECT COUNT(*) n FROM vowsls.scm_mr_line_item l JOIN vowsls.scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}'")[0]["n"])
print("  of which is_active=1:", q(c, f"SELECT COUNT(*) n FROM vowsls.scm_mr_line_item l JOIN vowsls.scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}' AND l.is_active=1")[0]["n"])
print("sls lines under matched sls hdrs (<=cut b87):", q(c, f"SELECT COUNT(*) n FROM sls.jute_mr_li li JOIN sls.jute_mr m ON m.jute_mr_id=li.jute_mr_id AND m.branch_id=87 AND m.jute_mr_date<='{CUT}'")[0]["n"])
print("sls lines under NULL-date b87 hdrs:", q(c, "SELECT COUNT(*) n FROM sls.jute_mr_li li JOIN sls.jute_mr m ON m.jute_mr_id=li.jute_mr_id AND m.branch_id=87 AND m.jute_mr_date IS NULL")[0]["n"])
print("sls line-id range legacy:", q(c, f"SELECT MIN(li.jute_mr_li_id) a, MAX(li.jute_mr_li_id) b FROM sls.jute_mr_li li JOIN sls.jute_mr m ON m.jute_mr_id=li.jute_mr_id AND m.branch_id=87 AND m.jute_mr_date<='{CUT}'")[0])
print("sls line-id range nulldate:", q(c, "SELECT MIN(li.jute_mr_li_id) a, MAX(li.jute_mr_li_id) b FROM sls.jute_mr_li li JOIN sls.jute_mr m ON m.jute_mr_id=li.jute_mr_id AND m.branch_id=87 AND m.jute_mr_date IS NULL")[0])

miss = q(c, f"""
SELECT l.jute_line_item_no, l.jute_receive_no, h.jute_receive_dt, h.branch_id, h.mr_good_recept_status hstat,
       l.is_active, l.status lstat, l.actual_weight mill_qtl, l.accepted_weight acc_kg, l.actual_bale, l.actual_loose,
       l.item_code, l.actual_quality,
       EXISTS(SELECT 1 FROM sls.jute_mr s WHERE s.jute_mr_id=l.jute_receive_no AND s.branch_id=87) hdr_in_sls,
       EXISTS(SELECT 1 FROM sls.jute_mr_li x WHERE x.jute_mr_li_id=l.jute_line_item_no) lineid_exists_anywhere
FROM vowsls.scm_mr_line_item l JOIN vowsls.scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}'
  AND NOT EXISTS(SELECT 1 FROM sls.jute_mr_li x JOIN sls.jute_mr m ON m.jute_mr_id=x.jute_mr_id AND m.branch_id=87
                 WHERE x.jute_mr_li_id=l.jute_line_item_no)
ORDER BY l.jute_line_item_no""")
print("MISSING lines in sls:", len(miss))
csvout("mr_line_missing_in_sls.csv", miss)
print("  by hdr status:", Counter(str(r["hstat"]) for r in miss))
print("  by is_active:", Counter(str(r["is_active"]) for r in miss))
print("  hdr_in_sls:", Counter(str(r["hdr_in_sls"]) for r in miss))
print("  mill qtl (active only):", round(sum(r["mill_qtl"] or 0 for r in miss if r["is_active"]==1),2))
print("  acc kg (active only):", round(sum(r["acc_kg"] or 0 for r in miss if r["is_active"]==1),2))

ext = q(c, f"""
SELECT x.jute_mr_li_id, x.jute_mr_id, m.jute_mr_date, m.status_id, x.active, x.actual_qty, x.actual_weight,
       EXISTS(SELECT 1 FROM vowsls.scm_mr_line_item l WHERE l.jute_line_item_no=x.jute_mr_li_id) old_lineid_exists
FROM sls.jute_mr_li x JOIN sls.jute_mr m ON m.jute_mr_id=x.jute_mr_id AND m.branch_id=87
WHERE (m.jute_mr_date<='{CUT}')
  AND NOT EXISTS(SELECT 1 FROM vowsls.scm_mr_line_item l JOIN vowsls.scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
                 WHERE l.jute_line_item_no=x.jute_mr_li_id AND h.company_id=106 AND h.jute_receive_dt<='{CUT}')
ORDER BY x.jute_mr_li_id""")
print("EXTRA lines in sls (dated<=cut):", len(ext))
csvout("mr_line_extra_in_sls.csv", ext)
