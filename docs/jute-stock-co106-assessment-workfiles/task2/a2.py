import sys,pickle,datetime; sys.path.insert(0,"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2")
from base import conn,q,dump
D=pickle.load(open("C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2/data.pkl","rb"))
L,iss,sal=D['L'],D['iss'],D['sal']
CUT=datetime.date(2026,3,31)
c=conn(); cur=c.cursor()
# all lines in whole vowsls (for dangling checks) with their company
allline={r['jute_line_item_no']:r for r in q(cur,"SELECT l.jute_line_item_no,l.jute_receive_no,l.company_id lco,h.company_id hco,h.mr_good_recept_status hst,h.jute_receive_dt hdt,l.is_active,l.actual_weight,l.accepted_weight FROM scm_mr_line_item l LEFT JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no")}
print("all lines in vowsls:",len(allline))
def d(x): 
    return x.date() if isinstance(x,datetime.datetime) else x
R=[]
def rep(kind,rows,qtl,why,csvname=None,exkey=None):
    ex=[str(r[exkey]) for r in rows[:5]] if (rows and exkey) else []
    R.append(dict(kind=kind,count=len(rows),qtl_impact=round(qtl,2),examples=ex,why=why))
    if csvname: dump(csvname,rows)
    print(f"[{kind}] n={len(rows)} qtl={qtl:.2f} ex={ex}")

# ---------- 1. MR header status codes outside documented set ----------
known={'3','4','6','1','21','24','13'}
bad=[dict(mr=r['mr'],dt=str(r['dt']),status=r['hst'],branch=r['hbr'],lid=r['lid'],mill_qtl=r['mill'],acc_qtl=round((r['acck'] or 0)/100,2)) for r in L.values() if str(r['hst']) not in known]
mrset=sorted({r['mr'] for r in bad})
rep("mr_header_status_31_undocumented",bad,sum(r['acc_qtl'] for r in bad),
    f"{len(mrset)} MR headers carry mr_good_recept_status='31', a code outside the documented set; all have branch_id NULL, dates 2021-05-29..2024-03-13",
    "A01_status31_lines.csv","lid")

# ---------- 2. MRs in open/draft/transfer-pending status carrying weight ----------
sus=[dict(mr=r['mr'],dt=str(r['dt']),status=r['hst'],lid=r['lid'],mill_qtl=r['mill'],acc_qtl=round((r['acck'] or 0)/100,2),act=r['act']) for r in L.values() if str(r['hst']) in ('1','21','24') and (r['mill'] or 0)!=0]
rep("mr_status_1_21_24_carrying_weight",sus,sum(r['acc_qtl'] for r in sus),
    "MRs still open/draft/transfer-pending but carrying receipt weight — included in the 151,148.56 receipts baseline",
    "A02_open_draft_mrs.csv","lid")

# ---------- 3. rejected MR lines still transacted ----------
rejlids={lid for lid,r in L.items() if str(r['hst'])=='4'}
inactlids={lid for lid,r in L.items() if r['act']!=1}

# ---------- issue classification ----------
iss_leg=[r for r in iss if r['issue_date'] is not None and d(r['issue_date'])<=CUT]
iss_null=[r for r in iss if r['issue_date'] is None]
today=datetime.date(2026,8,23)
iss_future=[r for r in iss if r['issue_date'] and d(r['issue_date'])>today]
rep("issue_null_date",[dict(issue_no=r['issue_no'],stock_id=r['stock_id'],qtl=r['total_weight'],st=r['issue_status']) for r in iss_null],
    sum(r['total_weight'] or 0 for r in iss_null),"jute_issue rows with NULL issue_date cannot be placed on either side of the 31-Mar cut","A03_issue_null_date.csv","issue_no")
rep("issue_future_date",[dict(issue_no=r['issue_no'],stock_id=r['stock_id'],dt=str(r['issue_date']),qtl=r['total_weight']) for r in iss_future],
    sum(r['total_weight'] or 0 for r in iss_future),"issue dated after today","A04_issue_future_date.csv","issue_no")

# effective (stock-reducing) issues = approved
ISS_OK=[r for r in iss_leg if r['issue_status']==3 and r['is_active']==1]
# dangling / wrong-target issues (all issues in scope, any status)
dang=[];rejtgt=[];othco=[];inacttgt=[];nullsid=[]
for r in iss_leg:
    sid=r['stock_id']
    if sid is None or sid==0: nullsid.append(r); continue
    t=allline.get(sid)
    if t is None: dang.append(r); continue
    if t['hco']!=106: othco.append((r,t)); continue
    if str(t['hst'])=='4': rejtgt.append((r,t))
    if t['is_active']!=1: inacttgt.append((r,t))
def iq(rs): return sum((x['total_weight'] or 0) for x in rs)
rep("issue_stock_id_null_or_zero",[dict(issue_no=r['issue_no'],stock_id=r['stock_id'],dt=str(r['issue_date']),qtl=r['total_weight'],st=r['issue_status']) for r in nullsid],iq(nullsid),
    "issue rows with no MR-line target: weight leaves stock but cannot be attributed to any receipt line","A05_issue_stockid_null.csv","issue_no")
rep("issue_stock_id_dangling",[dict(issue_no=r['issue_no'],stock_id=r['stock_id'],mr_no=r['mr_no'],dt=str(r['issue_date']),qtl=r['total_weight'],st=r['issue_status'],act=r['is_active'],br=r['branch_id']) for r in dang],iq(dang),
    "issue.stock_id points to a scm_mr_line_item row that does not exist anywhere in vowsls","A06_issue_dangling.csv","issue_no")
rep("issue_target_other_company",[dict(issue_no=r['issue_no'],stock_id=r['stock_id'],dt=str(r['issue_date']),qtl=r['total_weight'],st=r['issue_status'],target_company=t['hco'],target_mr=t['jute_receive_no']) for r,t in othco],iq([r for r,_ in othco]),
    "issue booked under company 106 but consuming an MR line owned by a different company","A07_issue_other_company.csv","issue_no")
rep("issue_target_rejected_mr",[dict(issue_no=r['issue_no'],stock_id=r['stock_id'],dt=str(r['issue_date']),qtl=r['total_weight'],st=r['issue_status'],target_mr=t['jute_receive_no'],target_hdr_status=t['hst']) for r,t in rejtgt],iq([r for r,_ in rejtgt]),
    "issue consumes a line on a REJECTED MR — the receipt was never added to stock, so the issue drives stock negative","A08_issue_rejected_mr.csv","issue_no")
rep("issue_target_inactive_line",[dict(issue_no=r['issue_no'],stock_id=r['stock_id'],dt=str(r['issue_date']),qtl=r['total_weight'],st=r['issue_status'],target_mr=t['jute_receive_no']) for r,t in inacttgt],iq([r for r,_ in inacttgt]),
    "issue consumes a soft-deleted (is_active=0) MR line","A09_issue_inactive_line.csv","issue_no")

# issue dated BEFORE its MR receipt date
early=[]
for r in ISS_OK:
    t=L.get(r['stock_id'])
    if t and t['dt'] and d(r['issue_date'])<d(t['dt']): early.append(dict(issue_no=r['issue_no'],stock_id=r['stock_id'],issue_date=str(r['issue_date']),mr_date=str(t['dt']),mr=t['mr'],qtl=r['total_weight']))
rep("issue_before_receipt_date",early,sum(r['qtl'] or 0 for r in early),
    "approved issue dated earlier than the MR it consumes — any date-sliced stock report shows negative stock on the intervening days","A10_issue_before_receipt.csv","issue_no")

# duplicate issues (same line, date, qty, weight)
from collections import defaultdict
g=defaultdict(list)
for r in ISS_OK: g[(r['stock_id'],d(r['issue_date']),round(r['quantity'] or 0,3),round(r['total_weight'] or 0,3))].append(r)
dups=[]; dupq=0.0
for k,v in g.items():
    if len(v)>1:
        dupq+=sum((x['total_weight'] or 0) for x in v[1:])
        for x in v: dups.append(dict(issue_no=x['issue_no'],stock_id=k[0],issue_date=str(k[1]),qty=k[2],qtl=k[3],grp_size=len(v),branch=x['branch_id'],dept=x['dept_id'] if 'dept_id' in x else None,created=str(x['create_date_time'])))
rep("duplicate_issue_rows",dups,dupq,
    "approved issues identical in (stock_id, date, quantity, weight) — likely double-posted; qtl impact = weight of the surplus copies","A11_duplicate_issues.csv","issue_no")

# ---------- sales ----------
sal_leg=[r for r in sal if (r['invoice_date'] is None) or d(r['invoice_date'])<=CUT]
SAL_OK=[r for r in sal_leg if r['ist']==3 and r['ihact']==1 and (r['liact'] in (1,None))]
nulldate=[r for r in sal_leg if r['invoice_date'] is None]
rep("invoice_null_date",[dict(invoice_id=r['invoice_id'],line=r['lidid'],no=r['invoice_no_string'],st=r['ist'],mr_line=r['mr_line_id'],qtl=r['sales_weight']) for r in nulldate],
    sum(r['sales_weight'] or 0 for r in nulldate),
    "approved raw-jute invoices with invoice_date NULL — a date-filtered 31-Mar report silently drops them, an unfiltered one keeps them","A12_invoice_null_date.csv","invoice_id")

draft=[r for r in sal_leg if r['ist'] in (0,1) and r['ihact']==1 and (r['sales_weight'] or 0)!=0]
rep("draft_open_invoice_holding_stock",[dict(invoice_id=r['invoice_id'],line=r['lidid'],no=r['invoice_no_string'],st=r['ist'],dt=str(r['invoice_date']),mr_line=r['mr_line_id'],qtl=r['sales_weight']) for r in draft],
    sum(r['sales_weight'] or 0 for r in draft),
    "invoices still in status 0/1 (open/draft) but already consuming MR-line weight — stock is neither sold nor free","A13_draft_invoices.csv","invoice_id")

badmr=[];othmr=[];dangmr=[]
for r in sal_leg:
    m=r['mr_line_id']
    if m is None or m==0: badmr.append(r); continue
    t=allline.get(m)
    if t is None: dangmr.append(r); continue
    if t['hco']!=106: othmr.append((r,t))
rep("sales_line_mr_line_id_null_or_zero",[dict(invoice_id=r['invoice_id'],line=r['lidid'],dt=str(r['invoice_date']),st=r['ist'],mr_line=r['mr_line_id'],qtl=r['sales_weight'],item=r['item_id'],qty=r['quantity']) for r in badmr],
    sum(r['sales_weight'] or 0 for r in badmr),
    "sales lines with no MR-line link — weight sold cannot be deducted from any receipt line, so per-line stock overstates","A14_sales_no_mrline.csv","invoice_id")
rep("sales_line_mr_line_dangling",[dict(invoice_id=r['invoice_id'],line=r['lidid'],mr_line=r['mr_line_id'],dt=str(r['invoice_date']),st=r['ist'],qtl=r['sales_weight']) for r in dangmr],
    sum(r['sales_weight'] or 0 for r in dangmr),"sales line points at a non-existent MR line","A15_sales_dangling.csv","invoice_id")
rep("sales_line_mr_line_other_company",[dict(invoice_id=r['invoice_id'],line=r['lidid'],mr_line=r['mr_line_id'],target_company=t['hco'],dt=str(r['invoice_date']),st=r['ist'],qtl=r['sales_weight']) for r,t in othmr],
    sum((r['sales_weight'] or 0) for r,_ in othmr),"company-106 invoice selling an MR line owned by another company","A16_sales_other_company.csv","invoice_id")

# ---------- per-line ledger: negative stock / over-consumption ----------
issby=defaultdict(float); issn=defaultdict(list)
for r in ISS_OK:
    issby[r['stock_id']]+=(r['total_weight'] or 0); issn[r['stock_id']].append(r)
salby=defaultdict(float); saln=defaultdict(list)
for r in SAL_OK:
    salby[r['mr_line_id']]+=(r['sales_weight'] or 0); saln[r['mr_line_id']].append(r)
neg=[]; negq=0.0
for lid,r in L.items():
    if str(r['hst'])=='4' or r['act']!=1: continue
    acc=(r['acck'] or 0)/100.0
    bal=acc-issby.get(lid,0)-salby.get(lid,0)
    if bal < -0.005:
        negq+=-bal
        neg.append(dict(lid=lid,mr=r['mr'],mr_date=str(r['dt']),hdr_status=r['hst'],accepted_qtl=round(acc,3),mill_qtl=r['mill'],
                        issued_qtl=round(issby.get(lid,0),3),sold_qtl=round(salby.get(lid,0),3),balance_qtl=round(bal,3)))
neg.sort(key=lambda x:x['balance_qtl'])
rep("line_negative_stock",neg,negq,
    "MR lines where approved issues+sales exceed accepted receipt weight as at 31-Mar — a per-line stock report either shows negative stock or (if clamped at 0) loses this weight","A17_negative_lines.csv","lid")

# over-consumption: transaction that occurs on an already-exhausted line
over=[]; overq=0.0
for lid,r in L.items():
    if str(r['hst'])=='4' or r['act']!=1: continue
    acc=(r['acck'] or 0)/100.0
    ev=[]
    for x in issn.get(lid,[]): ev.append((d(x['issue_date']),'ISSUE',x['issue_no'],x['total_weight'] or 0))
    for x in saln.get(lid,[]): ev.append((d(x['invoice_date']) or datetime.date(1900,1,1),'SALE',x['invoice_id'],x['sales_weight'] or 0))
    ev.sort(key=lambda e:(e[0],e[1]))
    run=0.0
    for dt_,ty,ref,w in ev:
        if run>=acc-0.005 and w>0:
            over.append(dict(lid=lid,mr=r['mr'],accepted_qtl=round(acc,3),already_qtl=round(run,3),tran_type=ty,tran_ref=ref,tran_date=str(dt_),tran_qtl=round(w,3)))
            overq+=w
        run+=w
rep("tran_on_exhausted_line",over,overq,
    "an issue/sale posted against an MR line already fully issued or sold — the whole of that transaction's weight is unbacked","A18_over_consumption.csv","lid")

# ---------- qty vs weight residue ----------
qty0w=[];w0qty=[]
for lid,r in L.items():
    if r['act']!=1: continue
    bales=(r['ab'] or 0); loose=(r['al'] or 0); qty=(r['qty'] or 0); acc=(r['acck'] or 0)/100.0; mill=(r['mill'] or 0)
    if (bales+loose)==0 and qty==0 and (acc!=0 or mill!=0):
        qty0w.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hst=r['hst'],bale=bales,loose=loose,qty=qty,mill_qtl=mill,accepted_qtl=round(acc,3)))
    if (bales+loose)>0 and acc==0 and mill==0:
        w0qty.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hst=r['hst'],bale=bales,loose=loose,qty=qty,mill_qtl=mill,accepted_qtl=round(acc,3)))
rep("line_qty_zero_weight_residue",qty0w,sum(r['accepted_qtl'] for r in qty0w),
    "0 bales / 0 loose / 0 qty but non-zero weight — bale-count stock reports show nothing, weight reports show stock","A19_qty0_weight.csv","lid")
rep("line_weight_zero_qty_residue",w0qty,0.0,
    "bales/loose present but zero weight — weight reports show nothing, bale-count reports show stock","A20_weight0_qty.csv","lid")

# ---------- accepted vs mill vs shortage ----------
mism=[];mq=0.0
for lid,r in L.items():
    if r['act']!=1: continue
    acc=(r['acck'] or 0)/100.0; mill=(r['mill'] or 0); sh=(r['sh'] or 0)/100.0
    diff=mill-acc
    if abs(diff-sh)>0.005:
        mq+=abs(diff-sh)
        mism.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hst=r['hst'],mill_qtl=round(mill,3),accepted_qtl=round(acc,3),shortage_qtl=round(sh,3),unexplained_qtl=round(diff-sh,3)))
mism.sort(key=lambda x:-abs(x['unexplained_qtl']))
rep("accepted_vs_mill_shortage_mismatch",mism,mq,
    "mill weight - accepted weight does not equal shortage_kgs; the two candidate 'receipt' figures differ by an amount the data cannot explain","A21_accepted_mill_mismatch.csv","lid")
tot_sh=sum(((r['sh'] or 0)/100.0) for r in L.values() if r['act']==1)
tot_mill=sum((r['mill'] or 0) for r in L.values() if r['act']==1)
tot_acc=sum(((r['acck'] or 0)/100.0) for r in L.values() if r['act']==1)
print(f"TOTALS active legacy lines: mill={tot_mill:.2f} accepted={tot_acc:.2f} shortage={tot_sh:.2f} mill-acc={tot_mill-tot_acc:.2f}")

# ---------- rate 0/NULL ----------
r0=[dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hst=r['hst'],rate=r['rate'],accepted_qtl=round((r['acck'] or 0)/100,3),mill_qtl=r['mill']) for lid,r in L.items() if r['act']==1 and not (r['rate'] or 0)]
rep("line_rate_zero_or_null",r0,sum(x['accepted_qtl'] for x in r0),
    "MR line with rate 0/NULL — the closing-stock VALUE for this weight is zero, so any valued stock report understates","A22_rate_zero.csv","lid")

# ---------- quality ids ----------
qids={x['id'] for x in q(cur,"SELECT id FROM jute_quality_price_master")}
badq=[dict(lid=lid,mr=r['mr'],dt=str(r['dt']),actual_quality=r['aq'],advised_quality=r['adq'],accepted_qtl=round((r['acck'] or 0)/100,3)) for lid,r in L.items() if r['act']==1 and (r['aq'] is None or r['aq'] not in qids)]
rep("line_quality_id_unresolvable",badq,sum(x['accepted_qtl'] for x in badq),
    "actual_quality missing from jute_quality_price_master — quality-wise stock report drops or mislabels the row","A23_bad_quality.csv","lid")
badqi=[dict(issue_no=r['issue_no'],stock_id=r['stock_id'],dt=str(r['issue_date']),jute_quality=r['jute_quality'],qtl=r['total_weight']) for r in ISS_OK if r['jute_quality'] is None or r['jute_quality'] not in qids]
rep("issue_quality_id_unresolvable",badqi,sum(x['qtl'] or 0 for x in badqi),
    "approved issue whose jute_quality is not a jute_quality_price_master id","A24_issue_bad_quality.csv","issue_no")
badqs=[dict(invoice_id=r['invoice_id'],line=r['lidid'],quality_id=r['quality_id'],qtl=r['sales_weight']) for r in SAL_OK if r['quality_id'] is None or r['quality_id'] not in qids]
rep("sales_quality_id_unresolvable",badqs,sum(x['qtl'] or 0 for x in badqs),
    "approved sales line whose quality_id is not a jute_quality_price_master id","A25_sales_bad_quality.csv","invoice_id")

# ---------- unit_conversion ----------
ucnull=[dict(mr=r['mr'],lid=lid,dt=str(r['dt']),hst=r['hst'],uc=r['uc'],bale=r['ab'],loose=r['al'],accepted_qtl=round((r['acck'] or 0)/100,3)) for lid,r in L.items() if r['act']==1 and not r['uc']]
rep("mr_unit_conversion_null",ucnull,sum(x['accepted_qtl'] for x in ucnull),
    "MR header unit_conversion NULL — BALE vs LOOSE cannot be decided, so bale/drum stock buckets are wrong","A26_uc_null.csv","mr")
ucbad=[]
for lid,r in L.items():
    if r['act']!=1 or not r['uc']: continue
    b=(r['ab'] or 0); lo=(r['al'] or 0)
    if r['uc']=='BALE' and (b==0 or lo>0): ucbad.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),uc=r['uc'],bale=b,loose=lo,accepted_qtl=round((r['acck'] or 0)/100,3)))
    elif r['uc']=='LOOSE' and (lo==0 or b>0): ucbad.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),uc=r['uc'],bale=b,loose=lo,accepted_qtl=round((r['acck'] or 0)/100,3)))
rep("mr_unit_conversion_inconsistent",ucbad,sum(x['accepted_qtl'] for x in ucbad),
    "header says BALE but line has no bales (or has loose), or vice versa — bale/drum split unreliable","A27_uc_inconsistent.csv","lid")

# ---------- header weight vs sum of lines ----------
hsum=defaultdict(float); hdr={}
for lid,r in L.items():
    if r['act']!=1: continue
    hsum[r['mr']]+=(r['mill'] or 0); hdr[r['mr']]=r
hw=[];hwq=0.0
for mr,r in hdr.items():
    h=(r['hwt'] or 0); s=hsum[mr]
    if abs(h-s)>0.005:
        hwq+=abs(h-s); hw.append(dict(mr=mr,dt=str(r['dt']),hst=r['hst'],hdr_weight=round(h,3),sum_lines_mill_qtl=round(s,3),diff_qtl=round(h-s,3)))
hw.sort(key=lambda x:-abs(x['diff_qtl']))
rep("mr_header_weight_ne_sum_lines",hw,hwq,
    "scm_mr_hdr.weight disagrees with SUM(line actual_weight) — header-level and line-level receipt totals differ","A28_hdr_vs_lines.csv","mr")

# ---------- denormalised counters vs transactions ----------
dsw=[];dswq=0.0
for lid,r in L.items():
    if r['act']!=1: continue
    a=(r['d_sw'] or 0); b=salby.get(lid,0)
    if abs(a-b)>0.005:
        dswq+=abs(a-b); dsw.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),denorm_sales_weight=round(a,3),actual_approved_sold_qtl=round(b,3),diff_qtl=round(a-b,3)))
dsw.sort(key=lambda x:-abs(x['diff_qtl']))
rep("denorm_sales_weight_disagrees",dsw,dswq,
    "scm_mr_line_item.sales_weight (used by the old stock widgets) disagrees with the sum of approved invoice lines","A29_denorm_sales.csv","lid")
dwc=[];dwcq=0.0
for lid,r in L.items():
    if r['act']!=1: continue
    a=(r['d_wc'] or 0); b=issby.get(lid,0)
    if abs(a-b)>0.005:
        dwcq+=abs(a-b); dwc.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),denorm_weight_consumed=round(a,3),actual_approved_issued_qtl=round(b,3),diff_qtl=round(a-b,3)))
dwc.sort(key=lambda x:-abs(x['diff_qtl']))
rep("denorm_weight_consumed_dead",dwc,dwcq,
    "scm_mr_line_item.weight_consumed / bales_consumed never maintained (all zero) while 60,951 qtl of approved issues exist — any report reading the counter shows zero consumption","A30_denorm_consumed.csv","lid")
dsb=[];dsbq=0.0
for lid,r in L.items():
    if r['act']!=1: continue
    a=(r['d_sb'] or 0); b=sum((x['sales_bale'] or 0) for x in saln.get(lid,[]))
    if abs(a-b)>0.005: dsbq+=abs(a-b); dsb.append(dict(lid=lid,mr=r['mr'],denorm_sales_bales=a,actual_sold_bales=b,diff=round(a-b,3)))
rep("denorm_sales_bales_disagrees",dsb,0.0,"scm_mr_line_item.sales_bales disagrees with approved invoice sales_bale (bales, not qtl)","A31_denorm_bales.csv","lid")

# ---------- inactive lines still transacted ----------
inact=[]
for lid,r in L.items():
    if r['act']==1: continue
    i=issby.get(lid,0); s=salby.get(lid,0)
    inact.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hst=r['hst'],mill_qtl=r['mill'],accepted_qtl=round((r['acck'] or 0)/100,3),issued_qtl=round(i,3),sold_qtl=round(s,3)))
rep("inactive_line_with_weight",inact,sum(x['accepted_qtl'] for x in inact),
    "soft-deleted (is_active=0) MR lines that still carry receipt weight and in some cases transactions","A32_inactive_lines.csv","lid")

# ---------- rejected MRs still carrying / transacted ----------
rej=[]
for lid,r in L.items():
    if str(r['hst'])!='4': continue
    rej.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),act=r['act'],mill_qtl=r['mill'],accepted_qtl=round((r['acck'] or 0)/100,3),issued_qtl=round(issby.get(lid,0),3),sold_qtl=round(salby.get(lid,0),3)))
rep("rejected_mr_lines",rej,sum(x['accepted_qtl'] for x in rej),
    "REJECTED MRs (status 4) — excluded by view_jute_mr_in_stock but present in raw sums; a raw SUM(accepted_weight) over-receipts by this much","A33_rejected_mrs.csv","lid")

# ---------- never-moved old lines (dead opening stock) ----------
dead=[]
for lid,r in L.items():
    if r['act']!=1 or str(r['hst'])=='4': continue
    acc=(r['acck'] or 0)/100.0
    if acc<=0: continue
    if issby.get(lid,0)==0 and salby.get(lid,0)==0 and d(r['dt'])<datetime.date(2024,4,1):
        dead.append(dict(lid=lid,mr=r['mr'],dt=str(r['dt']),hst=r['hst'],bale=r['ab'],loose=r['al'],accepted_qtl=round(acc,3),mill_qtl=r['mill']))
dead.sort(key=lambda x:x['dt'])
rep("pre_fy25_line_never_moved",dead,sum(x['accepted_qtl'] for x in dead),
    "MR lines received before 2024-04 that were never issued or sold in either system — they still sit as open stock on 31-Mar-2026","A34_never_moved_old.csv","lid")

# ---------- branch spread ----------
brs=defaultdict(lambda:[0,0.0])
for r in ISS_OK: brs[r['branch_id']][0]+=1; brs[r['branch_id']][1]+=(r['total_weight'] or 0)
print("ISSUE branches:",{k:(v[0],round(v[1],2)) for k,v in brs.items()})
brl=defaultdict(lambda:[0,0.0])
for lid,r in L.items():
    if r['act']==1: brl[r['hbr']][0]+=1; brl[r['hbr']][1]+=(r['acck'] or 0)/100
print("MR branches:",{k:(v[0],round(v[1],2)) for k,v in brl.items()})
brs2=defaultdict(lambda:[0,0.0])
for r in SAL_OK: brs2[r['ibr']][0]+=1; brs2[r['ibr']][1]+=(r['sales_weight'] or 0)
print("SALE branches:",{k:(v[0],round(v[1],2)) for k,v in brs2.items()})
print(f"\nTOTAL approved issues<=cut {sum(x['total_weight'] or 0 for x in ISS_OK):.2f} qtl over {len(ISS_OK)} rows")
print(f"TOTAL approved sales<=cut {sum(x['sales_weight'] or 0 for x in SAL_OK):.2f} qtl over {len(SAL_OK)} rows")
import json
json.dump(R,open("C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2/anomalies.json","w"),indent=1)
