"""One runnable self-check: the decomposition must sum to the measured gap."""
import pickle
rows = pickle.load(open("rows.pkl","rb"))
T = lambda f: round(sum(r[f] for r in rows), 2)
gap_mill = T("bal_sls") - T("bal_old_mill")
gap_acc  = T("bal_sls") - T("bal_old_acc")
parts_m  = T("d_recv_mill") + T("d_iss") + T("d_sale")
parts_a  = T("d_recv_acc")  + T("d_iss") + T("d_sale")
assert abs(gap_mill - parts_m) < 0.02, (gap_mill, parts_m)
assert abs(gap_acc  - parts_a) < 0.02, (gap_acc, parts_a)
# named causes must sum to the sales/issue/receipt legs
sales_named = 2697.33 + 1080.34 + 114.10 + 101.00
assert abs(sales_named - T("d_sale")) < 0.02, (sales_named, T("d_sale"))
recv_named = 566.79 - 150.69
assert abs(recv_named - T("d_recv_acc")) < 0.02, (recv_named, T("d_recv_acc"))
print("OK  gap_vs_mill=%.2f  gap_vs_acc=%.2f  (decomposition closes)" % (gap_mill, gap_acc))
