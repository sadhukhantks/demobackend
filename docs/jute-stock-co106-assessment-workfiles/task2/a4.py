import sys,pickle,datetime; sys.path.insert(0,"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2")
from base import dump
from collections import defaultdict
D=pickle.load(open("C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2/data.pkl","rb"))
L,iss,sal=D['L'],D['iss'],D['sal']; CUT=datetime.date(2026,3,31)
def d(x): return x.date() if isinstance(x,datetime.datetime) else x
ISS=[r for r in iss if r['issue_date'] and d(r['issue_date'])<=CUT and r['issue_status']==3 and r['is_active']==1]
issby=defaultdict(float)
for r in ISS: issby[r['stock_id']]+=(r['total_weight'] or 0)
sal_leg=[r for r in sal if (r['invoice_date'] is None) or d(r['invoice_date'])<=CUT]
SV=defaultdict(float)
for r in sal_leg:
    if r['liact']==1 and (r['quality_id'] or 0)>0: SV[r['mr_line_id']]+=(r['sales_weight'] or 0)
neg=[];nq=0.0
for lid,r in L.items():
    if r['act']!=1 or str(r['hst'])=='4': continue
    bal=(r['acck'] or 0)/100-issby.get(lid,0)-SV.get(lid,0)
    if bal<-0.005:
        nq+=-bal; neg.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hst=r['hst'],accepted_qtl=round((r['acck'] or 0)/100,3),issued=round(issby.get(lid,0),3),sold_viewdef=round(SV.get(lid,0),3),balance=round(bal,3)))
neg.sort(key=lambda x:x['balance']); dump("A38_negative_lines_viewdef.csv",neg)
print(f"[negative under old-view sales definition] n={len(neg)} qtl={nq:.2f} ex={[str(x['lid']) for x in neg[:5]]}")
# mill-weight based receipts variant
mill=sum((r['mill'] or 0) for lid,r in L.items() if r['act']==1 and str(r['hst'])!='4')
acc=sum(((r['acck'] or 0)/100) for lid,r in L.items() if r['act']==1 and str(r['hst'])!='4')
print(f"receipt basis: accepted={acc:.2f} mill={mill:.2f} delta={mill-acc:.2f}")
