"""
Report emp_codes present in vowsls.tbl_hrms_ed_official_details but missing in
sls.hrms_ed_official_details, broken down by company and branch.

Identity is resolved by eb_id, NOT by raw emp_code string: emp_code repeats across
companies, and the vowsls->sls series remapped eb_ids in two places.
  eb_map = identity
         + hrms_eb_id_co1_branch87_map.csv   (co-1 NJM factory -> sls branch 87)
         + vowsls_to_sls_transfer._map_hrms_eb_smallco          (small-company batch)

vowsls (co, branch) -> sls branch is then derived empirically from the employees that
DID migrate, so a branch/company remap is not reported as a data gap.

Grain: one row per vowsls employee (latest official row per eb_id).

Usage: python report_missing_emp_code_vowsls_vs_sls.py [out.xlsx]
"""
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import pymysql

HERE = Path(__file__).resolve().parent
ENV = {}
for line in (HERE.parents[1] / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "missing_emp_code_vowsls_vs_sls.xlsx"

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]))
cur = conn.cursor()


def norm(code):
    return (code or "").strip().upper()


# ------------------------------------------------------------------ eb_id map
eb_map = {}
with (HERE / "hrms_eb_id_co1_branch87_map.csv").open(newline="") as fh:
    for r in csv.DictReader(fh):
        eb_map[int(r["vowsls_eb_id"])] = int(r["sls_eb_id"])
cur.execute("SELECT legacy_eb_id, new_eb_id FROM vowsls_to_sls_transfer._map_hrms_eb_smallco")
eb_map.update({int(a): int(b) for a, b in cur.fetchall()})

# ------------------------------------------------------------------- vowsls
# latest official row per employee
cur.execute("""
    SELECT o.eb_id, p.company_id, o.branch_id, o.emp_code, o.legacy_code,
           TRIM(CONCAT_WS(' ', p.first_name, p.middle_name, p.last_name)),
           o.date_of_join, o.is_active, p.is_active, o.designation_id
      FROM vowsls.tbl_hrms_ed_official_details o
      JOIN vowsls.tbl_hrms_ed_personal_details p ON p.eb_id = o.eb_id
      JOIN (SELECT eb_id, MAX(tbl_hrms_ed_official_detail_id) mx
              FROM vowsls.tbl_hrms_ed_official_details GROUP BY eb_id) l
        ON l.eb_id = o.eb_id AND l.mx = o.tbl_hrms_ed_official_detail_id
""")
src = cur.fetchall()

# ---------------------------------------------------------------------- sls
cur.execute("""
    SELECT o.eb_id, b.co_id, o.branch_id, o.emp_code,
           UPPER(TRIM(CONCAT_WS(' ', p.first_name, p.middle_name, p.last_name)))
      FROM sls.hrms_ed_official_details o
      JOIN sls.branch_mst b ON b.branch_id = o.branch_id
      JOIN sls.hrms_ed_personal_details p ON p.eb_id = o.eb_id
""")
tgt = cur.fetchall()
# eb_ids are only partly preserved across the migration series — an unmapped eb_id can land on a
# completely different person, so a hit is only trusted when the name agrees.
sls_by_eb = {eb: (co, br, norm(c), " ".join(n.split())) for eb, co, br, c, n in tgt}
sls_code_at_branch = defaultdict(set)          # branch_id -> {emp_code}
for eb, co, br, c, _ in tgt:
    sls_code_at_branch[br].add(norm(c))


def hit_for(eb, name):
    h = sls_by_eb.get(eb_map.get(eb, eb))
    return h if h and h[3] == " ".join((name or "").upper().split()) else None

cur.execute("SELECT comp_id, company_name FROM vowsls.company_master")
CO = dict(cur.fetchall())
cur.execute("SELECT branch_id, branch_name FROM vowsls.branch_master")
BR_SRC = dict(cur.fetchall())
cur.execute("SELECT branch_id, branch_name, co_id FROM sls.branch_mst")
BR_TGT = {b: (n, co) for b, n, co in cur.fetchall()}

# --------------- vowsls (co,branch) -> sls branch(es): same id if it exists in sls,
# plus wherever this branch's migrated employees actually landed (co-1 factory -> br 87)
landed = defaultdict(set)
for eb, co, br, code, legacy, name, *_ in src:
    hit = hit_for(eb, name)
    if hit:
        landed[(co, br)].add(hit[1])
co_landed = defaultdict(set)
for (co, br), bs in landed.items():
    co_landed[co] |= bs
tgt_branches, co_tgt_branches = {}, {}
for eb, co, br, *_ in src:
    tgt_branches[(co, br)] = ({br} if br in BR_TGT else set()) | landed.get((co, br), set())
    co_tgt_branches.setdefault(co, set()).update(tgt_branches[(co, br)] | co_landed[co])


def codes_at(branches):
    out = set()
    for b in branches:
        out |= sls_code_at_branch[b]
    return out


# ------------------------------------------------------------------- classify
rows = []
for eb, co, br, code, legacy, name, doj, o_act, p_act, desig in src:
    ncode = norm(code)
    tb = tgt_branches.get((co, br), set())
    hit = hit_for(eb, name)
    cb = co_tgt_branches.get(co, set())
    if not ncode:
        status = "BLANK emp_code in vowsls"
    elif ncode in codes_at(tb):
        if hit and hit[2] == ncode:
            continue                          # same employee, same code -> fine
        status = "PRESENT in sls under a different eb_id (legacy duplicate)"
    elif ncode in codes_at(cb):
        status = "PRESENT in sls under another branch of the same company"
    elif hit:
        status = "employee migrated but emp_code differs in sls"
    else:
        status = "MISSING in sls"
    rows.append({
        "co_id": co, "company": CO.get(co, ""),
        "branch_id": br, "branch": BR_SRC.get(br, ""),
        "emp_code": code, "legacy_code": legacy,
        "eb_id_vowsls": eb, "employee_name": name, "date_of_join": doj,
        "active_official": o_act, "active_personal": p_act,
        "status": status,
        "employee_found_in_sls_by_eb_id": "Y" if hit else "N",
        "sls_branch_checked": ",".join(str(x) for x in sorted(tb)),
        "sls_emp_code": hit[2] if hit else "",
        "eb_id_sls": eb_map.get(eb, eb) if hit else "",
        "emp_code_elsewhere_in_sls": ",".join(
            sorted(f"br{b}" for b, cs in sls_code_at_branch.items() if ncode and ncode in cs and b not in tb)
        ) if ncode and ncode not in codes_at(tb) else "",
    })

miss = pd.DataFrame(rows).sort_values(["co_id", "branch_id", "emp_code"])

# -------------------------------------------------------------------- summary
sm = defaultdict(Counter)
for eb, co, br, code, legacy, name, *_ in src:
    sm[(co, br)]["vowsls_employees"] += 1
    if hit_for(eb, name):
        sm[(co, br)]["migrated"] += 1
for r in rows:
    sm[(r["co_id"], r["branch_id"])][r["status"]] += 1

summary = pd.DataFrame([
    {"co_id": co, "company": CO.get(co, ""), "branch_id": br, "branch": BR_SRC.get(br, ""),
     "sls_branch_checked": ",".join(str(x) for x in sorted(tgt_branches.get((co, br), ()))),
     "vowsls_employees": c["vowsls_employees"],
     "emp_code_found_in_sls": c["vowsls_employees"] - c["MISSING in sls"]
     - c["employee migrated but emp_code differs in sls"] - c["BLANK emp_code in vowsls"],
     "missing_in_sls": c["MISSING in sls"],
     "present_under_other_eb": c["PRESENT in sls under a different eb_id (legacy duplicate)"],
     "present_other_branch": c["PRESENT in sls under another branch of the same company"],
     "emp_code_differs": c["employee migrated but emp_code differs in sls"],
     "blank_emp_code": c["BLANK emp_code in vowsls"]}
    for (co, br), c in sorted(sm.items())
])
NUM = ["vowsls_employees", "emp_code_found_in_sls", "missing_in_sls",
       "present_under_other_eb", "present_other_branch", "emp_code_differs", "blank_emp_code"]
total = {c: "" for c in summary.columns} | {"company": "TOTAL"} | {c: int(summary[c].sum()) for c in NUM}
summary = pd.concat([summary.astype(object), pd.DataFrame([total])], ignore_index=True)

with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
    summary.to_excel(xl, sheet_name="Summary", index=False)
    miss[miss.status == "MISSING in sls"].to_excel(xl, sheet_name="Missing_in_SLS", index=False)
    miss[miss.status != "MISSING in sls"].to_excel(xl, sheet_name="Other_Discrepancies", index=False)
    for sh in xl.sheets:
        ws = xl.sheets[sh]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col in ws.columns:
            w = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(max(w + 2, 10), 38)

print(f"vowsls employees: {len(src)}   discrepancies: {len(miss)}")
print(miss.status.value_counts().to_string())
print(f"\nwrote {OUT}")
