import sys; sys.path.insert(0,"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2")
from base import conn,q,dump
c=conn(); cur=c.cursor()
CUT="2026-03-31"
# ---- build per-line ledger as a temp table in python ----
lines = q(cur, f"""
SELECT l.jute_line_item_no lid, l.jute_receive_no mr, h.jute_receive_dt dt, h.mr_good_recept_status hst,
       h.branch_id hbr, h.unit_conversion uc, h.weight hwt, h.mr_print_no pno,
       l.is_active act, l.actual_bale ab, l.actual_loose al, l.quantity qty,
       l.actual_weight mill, l.accepted_weight acck, l.shortage_kgs sh, l.rate rate,
       l.actual_quality aq, l.advised_quality adq, l.item_code ic, l.warehouse_no wh,
       l.sales_bales d_sb, l.sales_drums d_sd, l.sales_weight d_sw,
       l.bales_consumed d_bc, l.drums_consumed d_dc, l.weight_consumed d_wc, l.company_id lco
FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT} 23:59:59'""")
L={r['lid']:r for r in lines}
print("legacy lines:",len(L))

iss = q(cur, f"""SELECT issue_no, stock_id, mr_no, issue_date, quantity, total_weight, no_bales, issue_status, is_active,
  company_id, branch_id, godown_no, jute_quality, yarn_type_id, uom_code, issue_value, create_date_time
  FROM jute_issue WHERE (company_id=106 OR stock_id IN (SELECT jute_line_item_no FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no WHERE h.company_id=106))""")
print("issues touching co106:",len(iss))
sal = q(cur, f"""SELECT li.invoice_line_item_id lidid, li.invoice_id, li.mr_line_id, li.sales_weight, li.sales_bale, li.sales_drum,
  li.quantity, li.rate, li.is_active liact, li.quality_id, li.item_id,
  ih.invoice_date, ih.status ist, ih.is_active ihact, ih.company_id ico, ih.branch_id ibr, ih.invoice_type, ih.invoice_no_string
  FROM invoice_line_items li JOIN invoice_hdr ih ON ih.invoice_id=li.invoice_id
  WHERE ih.company_id=106 OR li.mr_line_id IN (SELECT jute_line_item_no FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no WHERE h.company_id=106)""")
print("sales lines touching co106:",len(sal))
import pickle
pickle.dump({'L':L,'iss':iss,'sal':sal,'lines':lines}, open("C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2/data.pkl","wb"))
