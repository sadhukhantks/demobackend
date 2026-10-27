import sys,pickle,datetime,json; sys.path.insert(0,"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2")
from base import conn,q,dump
from collections import defaultdict
D=pickle.load(open("C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2/data.pkl","rb"))
L,iss,sal=D['L'],D['iss'],D['sal']
CUT=datetime.date(2026,3,31)
def d(x): return x.date() if isinstance(x,datetime.datetime) else x
c=conn();cur=c.cursor()
mirrored={r['src_line_id'] for r in q(cur,"SELECT DISTINCT d.src_line_id FROM scm_mr_line_item d JOIN scm_mr_hdr dh ON dh.jute_receive_no=d.jute_receive_no WHERE dh.company_id<>106 AND d.is_active=1 AND d.src_line_id IS NOT NULL")}
ISS_OK=[r for r in iss if r['issue_date'] and d(r['issue_date'])<=CUT and r['issue_status']==3 and r['is_active']==1]
sal_leg=[r for r in sal if (r['invoice_date'] is None) or d(r['invoice_date'])<=CUT]
issby=defaultdict(float); salby=defaultdict(float)
for r in ISS_OK: issby[r['stock_id']]+=(r['total_weight'] or 0)
def salsum(pred):
    g=defaultdict(float)
    for r in sal_leg:
        if pred(r): g[r['mr_line_id']]+=(r['sales_weight'] or 0)
    return g
S_appr   = salsum(lambda r: r['ist']==3 and r['ihact']==1 and r['liact'] in (1,None))
S_appr_d = salsum(lambda r: r['ist']==3 and r['ihact']==1 and r['liact'] in (1,None) and r['invoice_date'] is not None)
S_view   = salsum(lambda r: r['liact']==1 and (r['quality_id'] or 0)>0)   # what the old view counts: no status filter
S_ctx    = salsum(lambda r: r['liact']==1 and (r['quality_id'] or 0)>0 and r['ist'] in (0,1,3) and r['invoice_date'] is not None)
base=[(lid,r) for lid,r in L.items() if r['act']==1 and str(r['hst'])!='4']
def close(S):
    tot=0.0
    for lid,r in base: tot += (r['acck'] or 0)/100 - issby.get(lid,0) - S.get(lid,0)
    return tot
rec=sum(((r['acck'] or 0)/100) for _,r in base)
recmill=sum((r['mill'] or 0) for _,r in base)
issT=sum(issby.get(lid,0) for lid,_ in base)
print(f"receipts accepted={rec:.2f}  mill={recmill:.2f}  issues={issT:.2f}")
for nm,S in [("approved sales, dated only",S_appr_d),("approved sales incl NULL-dated",S_appr),
             ("approved+draft+rejected-line (what view_jute_receipt_issue_sale counts)",S_view),
             ("approved+draft, dated only (the 88,876.80 baseline)",S_ctx)]:
    print(f"  sales[{nm}] = {sum(S.values()):.2f}  ->  CLOSING = {close(S):.2f} qtl")
# mirrored residual
mr_rows=[];mq=0.0
for lid,r in base:
    if lid not in mirrored: continue
    bal=(r['acck'] or 0)/100 - issby.get(lid,0) - S_appr.get(lid,0)
    if bal>0.005:
        mq+=bal; mr_rows.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hst=r['hst'],accepted_qtl=round((r['acck'] or 0)/100,3),issued=round(issby.get(lid,0),3),sold=round(S_appr.get(lid,0),3),closing_qtl=round(bal,3)))
mr_rows.sort(key=lambda x:-x['closing_qtl'])
dump("A35_mirrored_lines_still_open.csv",mr_rows)
print(f"[mirrored_line_still_open] n={len(mr_rows)} qtl={mq:.2f} ex={[str(x['lid']) for x in mr_rows[:5]]}")
# top closing lines
tops=[]
for lid,r in base:
    bal=(r['acck'] or 0)/100 - issby.get(lid,0) - S_appr.get(lid,0)
    if abs(bal)>0.005:
        tops.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hdr_status=r['hst'],branch=r['hbr'],accepted_qtl=round((r['acck'] or 0)/100,3),
                         issued_qtl=round(issby.get(lid,0),3),sold_qtl=round(S_appr.get(lid,0),3),closing_qtl=round(bal,3),
                         mirrored_downstream=int(lid in mirrored),rate=r['rate'],warehouse=r['wh']))
tops.sort(key=lambda x:-x['closing_qtl'])
dump("A36_closing_lines_31mar.csv",tops)
print(f"lines with non-zero 31-Mar balance: {len(tops)}, sum={sum(x['closing_qtl'] for x in tops):.2f} qtl")
# branch-sliced
brrec=defaultdict(float); 
for lid,r in base: brrec[r['hbr']]+=(r['acck'] or 0)/100
briss=defaultdict(float)
for r in ISS_OK: briss[r['branch_id']]+=(r['total_weight'] or 0)
brsal=defaultdict(float)
for r in sal_leg:
    if r['ist']==3 and r['ihact']==1 and r['liact'] in (1,None): brsal[r['ibr']]+=(r['sales_weight'] or 0)
rows=[]
for b in set(list(brrec)+list(briss)+list(brsal)):
    rows.append(dict(branch=b,receipts_qtl=round(brrec.get(b,0),2),issues_qtl=round(briss.get(b,0),2),sales_qtl=round(brsal.get(b,0),2),
                     branch_closing_qtl=round(brrec.get(b,0)-briss.get(b,0)-brsal.get(b,0),2)))
dump("A37_branch_slice.csv",rows)
for x in rows: print("BRANCH",x)
