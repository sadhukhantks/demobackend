"""Server-only extraction for the ABC consumption prototype (read-only on sls).
Writes data.json: per branch, item x year x expense-type consumption for the
2020-2024 window plus the per-item position (stock, pending PO/indent, latest rate).
"""
import pymysql, os, json, datetime, time, decimal

RATE = "(COALESCE(pid.accepted_rate, pid.rate, 0) - COALESCE(pid.discount_amount / NULLIF(pid.approved_qty, 0), 0))"
BRANCHES = [(29, 2, "FACTORY (co 2, EJM)"), (4, 1, "FACTORY (co 1, NJM legacy)")]
DATE_FROM, DATE_TO = "2020-01-01", "2024-12-31"

def f(x):
    if x is None: return None
    if isinstance(x, decimal.Decimal): return float(x)
    if isinstance(x, (datetime.date, datetime.datetime)): return x.isoformat()[:10]
    return x

c = pymysql.connect(host=os.environ["DATABASE_HOST"], port=int(os.environ["DATABASE_PORT"]),
                    user=os.environ["DATABASE_USER"], password=os.environ["DATABASE_PASSWORD"],
                    database="sls", read_timeout=900)
cur = c.cursor()
cur.execute("SELECT expense_type_id, expense_type_name FROM expense_type_mst ORDER BY expense_type_id")
et_names = [r[1] for r in cur.fetchall()]
out = {"generated": datetime.datetime.now().isoformat()[:16], "tenant": "sls",
       "window": [DATE_FROM, DATE_TO], "years": list(range(2020, 2025)),
       "expense_types": et_names + ["(none)"], "branches": []}
et_idx = {n: i for i, n in enumerate(out["expense_types"])}

for br, co, label in BRANCHES:
    t0 = time.time()
    cur.execute("DROP TEMPORARY TABLE IF EXISTS tmp_cons")
    cur.execute(f"""
      CREATE TEMPORARY TABLE tmp_cons AS
      SELECT il.item_id, YEAR(ih.issue_date) AS yr, COALESCE(etm.expense_type_name, '(none)') AS et,
             SUM(il.issue_qty) AS qty, SUM(il.issue_qty * {RATE}) AS val,
             BIT_OR(1 << (MONTH(ih.issue_date) - 1)) AS mmask, COUNT(*) AS n_lines
      FROM issue_li il JOIN issue_hdr ih ON ih.issue_id = il.issue_id
      LEFT JOIN proc_inward_dtl pid ON pid.inward_dtl_id = il.inward_dtl_id
      LEFT JOIN expense_type_mst etm ON etm.expense_type_id = il.expense_type_id
      WHERE (ih.active = 1 OR ih.active IS NULL) AND ih.status_id IN (1, 3, 5, 20)
        AND ih.branch_id = %s AND ih.issue_date BETWEEN %s AND %s
      GROUP BY il.item_id, yr, et""", (br, DATE_FROM, DATE_TO))
    cur.execute("SELECT DISTINCT item_id FROM tmp_cons")
    ids_sql = ",".join(str(r[0]) for r in cur.fetchall()) or "0"
    cur.execute("SELECT item_id, yr, et, qty, val, mmask, n_lines FROM tmp_cons")
    cons = [[r[0], r[1], et_idx.get(r[2], len(out["expense_types"]) - 1), round(f(r[3]) or 0, 3),
             round(f(r[4]) or 0, 2), int(r[5] or 0), int(r[6])] for r in cur.fetchall()]
    print(br, "cons rows", len(cons), round(time.time() - t0, 1), "s", flush=True)

    cur.execute(f"""
      SELECT im.item_id, vip.full_item_code, im.item_name, igm.item_grp_name, um.uom_name,
             im.active, im.purchaseable, im.tangible,
             COALESCE(stk.qty, 0), COALESCE(ind.qty, 0), COALESCE(po.qty, 0),
             lr.rate, lr.inward_date, li.last_issue
      FROM item_mst im
      LEFT JOIN vw_item_with_group_path vip ON vip.item_id = im.item_id
      LEFT JOIN item_grp_mst igm ON igm.item_grp_id = im.item_grp_id
      LEFT JOIN uom_mst um ON um.uom_id = im.uom_id
      LEFT JOIN (
        SELECT item_id, SUM(q) AS qty FROM (
          SELECT pid.item_id, SUM(pid.approved_qty) AS q FROM proc_inward_dtl pid
            JOIN proc_inward pi ON pi.inward_id = pid.inward_id
            WHERE pid.active = 1 AND pi.sr_status = 3 AND pi.branch_id = %(br)s
              AND pid.item_id IN ({{ids}}) GROUP BY pid.item_id
          UNION ALL
          SELECT il.item_id, -SUM(il.issue_qty) FROM issue_li il JOIN issue_hdr ih ON ih.issue_id = il.issue_id
            WHERE (ih.active = 1 OR ih.active IS NULL) AND ih.status_id IN (1, 3, 5, 20) AND ih.branch_id = %(br)s
              AND il.item_id IN ({{ids}}) GROUP BY il.item_id
          UNION ALL
          SELECT r.item_id, -SUM(r.return_qty) FROM vw_drcr_return_qty r WHERE r.branch_id = %(br)s
              AND r.item_id IN ({{ids}}) GROUP BY r.item_id
        ) s GROUP BY item_id
      ) stk ON stk.item_id = im.item_id
      LEFT JOIN (
        SELECT pid.item_id, SUM(GREATEST(pid.qty
             - COALESCE((SELECT SUM(c.cancelled_qty) FROM proc_indent_dtl_cancel c WHERE c.indent_dtl_id = pid.indent_dtl_id), 0)
             - COALESCE((SELECT SUM(ppd.qty - COALESCE((SELECT SUM(pc.cancel_qty) FROM proc_po_dtl_cancel pc WHERE pc.po_dtl_id = ppd.po_dtl_id), 0))
                         FROM proc_po_dtl ppd JOIN proc_po pp ON pp.po_id = ppd.po_id
                         WHERE ppd.indent_dtl_id = pid.indent_dtl_id AND ppd.active = 1 AND pp.status_id NOT IN (4, 6)), 0), 0)) AS qty
        FROM proc_indent_dtl pid JOIN proc_indent pi ON pi.indent_id = pid.indent_id
        WHERE pi.branch_id = %(br)s AND pid.active = 1 AND pi.status_id NOT IN (4, 6)
          AND pid.item_id IN ({{ids}})
        GROUP BY pid.item_id
      ) ind ON ind.item_id = im.item_id
      LEFT JOIN (
        SELECT ppd.item_id, SUM(GREATEST(ppd.qty
             - COALESCE((SELECT SUM(pid.inward_qty) FROM proc_inward_dtl pid WHERE pid.po_dtl_id = ppd.po_dtl_id AND pid.active = 1 AND (pid.status_id IS NULL OR pid.status_id <> 6)), 0)
             + COALESCE((SELECT SUM(r.return_qty) FROM vw_drcr_return_qty r JOIN proc_inward_dtl pid2 ON pid2.inward_dtl_id = r.inward_dtl_id WHERE pid2.po_dtl_id = ppd.po_dtl_id), 0), 0)) AS qty
        FROM proc_po_dtl ppd JOIN proc_po pp ON pp.po_id = ppd.po_id
        WHERE pp.branch_id = %(br)s AND ppd.active = 1 AND pp.status_id NOT IN (4, 6)
          AND ppd.item_id IN ({{ids}})
        GROUP BY ppd.item_id
      ) po ON po.item_id = im.item_id
      LEFT JOIN (
        SELECT item_id, rate, inward_date FROM (
          SELECT pid.item_id, {RATE} AS rate, pi.inward_date,
                 ROW_NUMBER() OVER (PARTITION BY pid.item_id ORDER BY pi.inward_date DESC, pid.inward_dtl_id DESC) AS rn
          FROM proc_inward_dtl pid JOIN proc_inward pi ON pi.inward_id = pid.inward_id
          JOIN branch_mst bm ON bm.branch_id = pi.branch_id
          WHERE bm.co_id = %(co)s AND pid.active = 1 AND pi.sr_status = 3
            AND pid.item_id IN ({{ids}})
        ) x WHERE rn = 1
      ) lr ON lr.item_id = im.item_id
      LEFT JOIN (
        SELECT il.item_id, MAX(ih.issue_date) AS last_issue FROM issue_li il JOIN issue_hdr ih ON ih.issue_id = il.issue_id
        WHERE (ih.active = 1 OR ih.active IS NULL) AND ih.status_id IN (1, 3, 5, 20) AND ih.branch_id = %(br)s
          AND il.item_id IN ({{ids}}) GROUP BY il.item_id
      ) li ON li.item_id = im.item_id
      WHERE im.item_id IN ({{ids}})
      """.replace("{ids}", ids_sql), {"br": br, "co": co})
    items = []
    for r in cur.fetchall():
        items.append({"id": r[0], "code": r[1] or "", "name": (r[2] or "").strip(), "grp": r[3] or "", "uom": r[4] or "",
                      "active": int(r[5] or 0), "purch": int(bool(r[6])), "tang": None if r[7] is None else int(bool(r[7])),
                      "stock": round(f(r[8]) or 0, 3), "pind": round(f(r[9]) or 0, 3), "ppo": round(f(r[10]) or 0, 3),
                      "lrate": None if r[11] is None else round(f(r[11]), 2), "lrdate": f(r[12]), "lissue": f(r[13])})
    print(br, "items", len(items), round(time.time() - t0, 1), "s", flush=True)
    out["branches"].append({"branch_id": br, "co_id": co, "label": label, "items": items, "cons": cons})

c.close()
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
with open(path, "w") as fh:
    json.dump(out, fh, separators=(",", ":"))
print("wrote", path, os.path.getsize(path) // 1024, "KB")
