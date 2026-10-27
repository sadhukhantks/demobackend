from lib import *
c = conn()

# ---- population ----
print("=== MR HEADER SCOPE ===")
for lbl, sql in [
 ("old co106 all", "SELECT COUNT(*) n FROM vowsls.scm_mr_hdr WHERE company_id=106"),
 ("old co106 <=cut", f"SELECT COUNT(*) n FROM vowsls.scm_mr_hdr WHERE company_id=106 AND jute_receive_dt<='{CUT}'"),
 ("sls b87 all", "SELECT COUNT(*) n FROM sls.jute_mr WHERE branch_id=87"),
 ("sls b87 <=cut", f"SELECT COUNT(*) n FROM sls.jute_mr WHERE branch_id=87 AND jute_mr_date<='{CUT}'"),
 ("sls b87 nulldate", "SELECT COUNT(*) n FROM sls.jute_mr WHERE branch_id=87 AND jute_mr_date IS NULL"),
]:
    print(lbl, q(c, sql)[0]["n"])

# matched pairs (old<=cut, sls on b87)
pairs = q(c, f"""
SELECT h.jute_receive_no, h.branch_id ob, h.jute_receive_dt, h.mr_good_recept_status ostat,
       h.gate_entry_no, h.unit_conversion ouc, h.mukam_id omuk, h.vehicle_no oveh, h.weight owt,
       h.frieght_paid ofp, h.challan_no ocn, h.challan_date ocd, h.po_num, h.supp_code, h.mr_print_no,
       h.src_hdr_id, h.src_com_id oscid, h.remarks orem,
       s.jute_mr_id, s.branch_id sb, s.jute_mr_date, s.out_date, s.jute_gate_entry_date, s.status_id,
       s.jute_gate_entry_no, s.unit_conversion suc, s.mukam_id smuk, s.vehicle_no sveh,
       s.actual_weight sawt, s.mr_weight smwt, s.challan_weight scw, s.frieght_paid sfp,
       s.challan_no scn, s.challan_date scd, s.po_id, s.party_id, s.jute_supplier_id, s.branch_mr_no,
       s.src_jute_mr_id, s.src_com_id sscid, s.remarks srem, s.transfer_mode
FROM vowsls.scm_mr_hdr h JOIN sls.jute_mr s ON s.jute_mr_id=h.jute_receive_no AND s.branch_id=87
WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}'""")
print("matched pairs:", len(pairs))

missing = q(c, f"""
SELECT h.jute_receive_no, h.branch_id, h.jute_receive_dt, h.mr_good_recept_status,
       h.mr_print_no, h.gate_entry_no, h.weight,
       (SELECT COUNT(*) FROM vowsls.scm_mr_line_item l WHERE l.jute_receive_no=h.jute_receive_no) n_lines,
       (SELECT COALESCE(SUM(l.actual_weight),0) FROM vowsls.scm_mr_line_item l WHERE l.jute_receive_no=h.jute_receive_no AND l.is_active=1) mill_qtl,
       (SELECT COALESCE(SUM(l.accepted_weight),0) FROM vowsls.scm_mr_line_item l WHERE l.jute_receive_no=h.jute_receive_no AND l.is_active=1) acc_kg,
       EXISTS(SELECT 1 FROM sls.jute_mr s2 WHERE s2.jute_mr_id=h.jute_receive_no) id_exists_other_branch
FROM vowsls.scm_mr_hdr h
WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}'
  AND NOT EXISTS(SELECT 1 FROM sls.jute_mr s WHERE s.jute_mr_id=h.jute_receive_no AND s.branch_id=87)
ORDER BY h.jute_receive_no""")
print("MISSING in sls:", len(missing))
csvout("mr_hdr_missing_in_sls.csv", missing)
from collections import Counter
print(" by old status:", Counter(str(r["mr_good_recept_status"]) for r in missing))
print(" by old branch:", Counter(str(r["branch_id"]) for r in missing))

extra = q(c, f"""
SELECT s.jute_mr_id, s.jute_mr_date, s.status_id, s.branch_mr_no, s.jute_gate_entry_no,
       (SELECT COUNT(*) FROM sls.jute_mr_li li WHERE li.jute_mr_id=s.jute_mr_id) n_lines,
       (SELECT COALESCE(SUM(li.actual_weight),0) FROM sls.jute_mr_li li WHERE li.jute_mr_id=s.jute_mr_id AND li.active=1) wt,
       EXISTS(SELECT 1 FROM vowsls.scm_mr_hdr h WHERE h.jute_receive_no=s.jute_mr_id) old_id_exists_anyco,
       (SELECT h.company_id FROM vowsls.scm_mr_hdr h WHERE h.jute_receive_no=s.jute_mr_id) old_co
FROM sls.jute_mr s
WHERE s.branch_id=87 AND (s.jute_mr_date<='{CUT}' OR s.jute_mr_date IS NULL)
  AND NOT EXISTS(SELECT 1 FROM vowsls.scm_mr_hdr h WHERE h.jute_receive_no=s.jute_mr_id AND h.company_id=106 AND h.jute_receive_dt<='{CUT}')
ORDER BY s.jute_mr_id""")
print("EXTRA in sls (<=cut or nulldate):", len(extra))
csvout("mr_hdr_extra_in_sls.csv", extra)
print(" by status:", Counter(str(r["status_id"]) for r in extra))
