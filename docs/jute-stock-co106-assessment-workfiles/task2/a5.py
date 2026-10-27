import sys; sys.path.insert(0,"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2")
from base import conn,q,dump
c=conn();cur=c.cursor()
dump("A39_view_phantom_negative_groups.csv", q(cur,"""
SELECT jute_receive_no, mr_line_id, mr_print_no, branch_id, quality_code, quality_name, godown, rate,
       receipt_bales, receipt_drums, receipt_weight, balestock, drumstock, weightstock
FROM view_jute_mr_in_stock WHERE company_id=106 AND weightstock<0 ORDER BY weightstock"""))
dump("A40_view_positive_groups.csv", q(cur,"""
SELECT jute_receive_no, jute_receive_dt, mr_line_id, mr_print_no, branch_id, quality_name, godown, rate,
       receipt_weight, weightstock, balestock, drumstock
FROM view_jute_mr_in_stock WHERE company_id=106 AND weightstock>0 ORDER BY weightstock DESC"""))
dump("A41_transfer_mirror_open_on_source.csv", q(cur,"""
SELECT l.jute_line_item_no dest_lid, l.jute_receive_no dest_mr, h.jute_receive_dt dest_dt, h.mr_good_recept_status dest_hst,
       ROUND(l.accepted_weight/100,3) dest_acc_qtl, l.src_line_id src_lid, sh.company_id src_co, sh.jute_receive_dt src_dt,
       ROUND(s.accepted_weight/100,3) src_acc_qtl,
       ROUND(IFNULL((SELECT SUM(i.total_weight) FROM jute_issue i WHERE i.stock_id=l.src_line_id AND i.issue_status=3 AND i.is_active=1 AND i.issue_date<='2026-03-31'),0),3) src_issued,
       ROUND(IFNULL((SELECT SUM(li.sales_weight) FROM invoice_line_items li JOIN invoice_hdr ih ON ih.invoice_id=li.invoice_id WHERE li.mr_line_id=l.src_line_id AND ih.status=3 AND li.is_active=1 AND (ih.invoice_date<='2026-03-31' OR ih.invoice_date IS NULL)),0),3) src_sold
FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
JOIN scm_mr_line_item s ON s.jute_line_item_no=l.src_line_id JOIN scm_mr_hdr sh ON sh.jute_receive_no=s.jute_receive_no
WHERE h.company_id=106 AND h.jute_receive_dt<='2026-03-31' AND l.is_active=1 AND s.is_active=1 AND sh.company_id IN (1,2)
ORDER BY sh.company_id, src_acc_qtl DESC"""))
dump("A42_status31_mr_headers.csv", q(cur,"""
SELECT h.jute_receive_no mr, h.jute_receive_dt, h.mr_print_no, h.branch_id, h.gate_entry_no, h.po_num, h.vehicle_no,
       h.supp_code, h.mukam_id, h.unit_conversion, h.weight hdr_weight, COUNT(l.jute_line_item_no) nlines,
       ROUND(SUM(l.accepted_weight/100),3) acc_qtl, ROUND(SUM(l.actual_weight),3) mill_qtl, SUM(l.shortage_kgs) shortage_kg
FROM scm_mr_hdr h JOIN scm_mr_line_item l ON l.jute_receive_no=h.jute_receive_no
WHERE h.company_id=106 AND h.mr_good_recept_status='31' AND l.is_active=1
GROUP BY 1,2,3,4,5,6,7,8,9,10,11 ORDER BY h.jute_receive_dt"""))
print("done")
