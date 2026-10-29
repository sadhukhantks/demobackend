"""
Module landing-page dashboards: document counts, totals, monthly trend and
status split per module, scoped to the sidebar's company + branches.

One config table drives every module — a module is a list of sources, each a
table with a date column, an optional measure (₹ / kg / hrs) and a scope filter.
Tables missing on a tenant (schema drift) are skipped, not fatal.
"""

import logging
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.inventory.query import lot_rate_sql

logger = logging.getLogger(__name__)

router = APIRouter()

BR = "t.branch_id IN :branches"
CO = "t.co_id = :co_id"
# Wages tables carry no branch_id: scope by the department's branch.
DEPT_BR = "t.dept_id IN (SELECT dept_id FROM dept_mst WHERE branch_id IN :branches)"
FORTNIGHT = "(SELECT f.start_date FROM forthnight_period_mst f WHERE f.forthnight_id = t.period_id)"


def S(label, table, date_col=None, measure=None, unit=None, status=False, where=BR):
    """A source. date_col=None = snapshot count (ignores the date range).
    date_col / measure: a column of ``t``, or a SQL expression over ``t`` (e.g. a subquery)."""
    return dict(label=label, table=table, date_col=date_col, measure=measure,
                unit=unit, status=status, where=where)


ISSUE_VALUE = f"""(SELECT SUM(il.issue_qty * {lot_rate_sql('pid')})
    FROM issue_li il LEFT JOIN proc_inward_dtl pid ON pid.inward_dtl_id = il.inward_dtl_id
    WHERE il.issue_id = t.issue_id)"""

# Closing stock as of :date_to — same movements and valuation as the
# Inventory Stock report's closing_val (approved SRs - active issues - DR returns).
STOCK_SQL = f"""
SELECT COALESCE(SUM(s.q > 0.0005), 0) AS items, COALESCE(SUM(s.v), 0) AS val FROM (
    SELECT x.item_id, SUM(x.q) AS q, SUM(x.v) AS v FROM (
        SELECT pid.item_id, pid.approved_qty AS q, pid.approved_qty * {lot_rate_sql('pid')} AS v
        FROM proc_inward_dtl pid JOIN proc_inward pi ON pi.inward_id = pid.inward_id
        WHERE pid.active = 1 AND pi.sr_status = 3 AND pi.branch_id IN :branches
          AND pi.inward_date <= :date_to
        UNION ALL
        SELECT il.item_id, -il.issue_qty, -il.issue_qty * {lot_rate_sql('lot')}
        FROM issue_li il JOIN issue_hdr ih ON ih.issue_id = il.issue_id
        LEFT JOIN proc_inward_dtl lot ON lot.inward_dtl_id = il.inward_dtl_id
        WHERE (ih.active = 1 OR ih.active IS NULL) AND ih.status_id IN (1, 3, 5, 20)
          AND ih.branch_id IN :branches AND ih.issue_date <= :date_to
        UNION ALL
        SELECT r.item_id, -r.return_qty, -r.return_qty * {lot_rate_sql('lot')}
        FROM vw_drcr_return_qty r JOIN proc_inward_dtl lot ON lot.inward_dtl_id = r.inward_dtl_id
        WHERE r.branch_id IN :branches AND r.note_date <= :date_to
    ) x GROUP BY x.item_id
) s"""


def _tile(label, unit=None, count=0, amount=None, monthly=(), note=None, value_tile=False):
    """A custom KPI tile. value_tile: the big number is ``amount``, not ``count``.
    Tiles with monthly data join the trend chart; none join "Records by type"."""
    return {"label": label, "unit": unit, "snapshot": not monthly, "count": count,
            "amount": amount, "monthly": list(monthly), "status": [],
            "value_tile": value_tile, "note": note}


def _stock_stats(db: Session, params: dict) -> dict:
    row = _run(db, STOCK_SQL, params)[0]
    items = int(row["items"])
    return _tile("Stock in Hand", "₹", items, round(float(row["val"]), 2),
                 note=f"{items:,} items · as of {params['date_to']:%d %b %y}", value_tile=True)


# Pay register components (see src/hrms/ejmPayRegister.py): Net Pay, EPF, ESIC.
NET, EPF, ESIC = 21, 18, 19
# One row per employee per month; totals and distinct counts are folded in Python
# so the 26M-row pay_employee_payroll is scanned once. Same period rule as the
# pay register's date-range reports: skip cancelled (4) periods and lines.
# ponytail: ~20s on demo for 12 months — an index on (PAYPERIOD_ID, COMPONENT_ID) is the fix.
PAYROLL_SQL = f"""
SELECT DATE_FORMAT(p.FROM_DATE, '%Y-%m') AS month, k.EMPLOYEEID AS eb_id,
    SUM(CASE WHEN k.COMPONENT_ID = {NET} THEN k.AMOUNT ELSE 0 END) AS net,
    SUM(CASE WHEN k.COMPONENT_ID = {EPF} THEN k.AMOUNT ELSE 0 END) AS pf,
    SUM(CASE WHEN k.COMPONENT_ID = {ESIC} THEN k.AMOUNT ELSE 0 END) AS esi
FROM pay_period p JOIN pay_employee_payroll k ON k.PAYPERIOD_ID = p.ID
WHERE p.FROM_DATE >= :date_from AND p.FROM_DATE < DATE_ADD(:date_to, INTERVAL 1 DAY)
  AND (p.STATUS IS NULL OR p.STATUS <> 4) AND k.STATUS <> 4
  AND k.COMPONENT_ID IN ({NET}, {EPF}, {ESIC})
  AND k.EMPLOYEEID IN (SELECT eb_id FROM hrms_ed_official_details WHERE branch_id IN :branches)
GROUP BY month, k.EMPLOYEEID"""


def _payroll_stats(db: Session, params: dict) -> list[dict]:
    rows = _run(db, PAYROLL_SQL, params)
    months: dict[str, dict] = {}
    paid, in_pf, in_esi = set(), set(), set()
    for r in rows:
        m = months.setdefault(r["month"], {"net": 0.0, "pf": 0.0, "esi": 0.0, "emps": 0})
        net, pf, esi = float(r["net"]), float(r["pf"]), float(r["esi"])
        m["net"] += net; m["pf"] += pf; m["esi"] += esi
        if net > 0:
            m["emps"] += 1; paid.add(r["eb_id"])
        if pf > 0: in_pf.add(r["eb_id"])
        if esi > 0: in_esi.add(r["eb_id"])

    def series(key):
        return [{"month": k, "count": v["emps"], "amount": round(v[key], 2)} for k, v in sorted(months.items())]

    total = {k: round(sum(v[k] for v in months.values()), 2) for k in ("net", "pf", "esi")}
    pay_months = sum(1 for v in months.values() if v["net"] > 0)
    emp_months = sum(v["emps"] for v in months.values())
    per_emp = total["net"] / emp_months if emp_months else 0
    return [
        _tile("Net Amount", "₹", len(paid), total["net"], series("net"), value_tile=True),
        _tile("PF Amount", "₹", len(in_pf), total["pf"], series("pf"), value_tile=True),
        _tile("ESI Amount", "₹", len(in_esi), total["esi"], series("esi"), value_tile=True),
        _tile("Employees", count=len(paid), note="paid in the period"),
        _tile("Employees in PF", count=len(in_pf), note="with a PF deduction"),
        _tile("Employees in ESI", count=len(in_esi), note="with an ESI deduction"),
        _tile("Avg Monthly Wages", "₹", amount=round(total["net"] / pay_months, 2) if pay_months else 0,
              note=f"₹ {per_emp:,.0f} per employee / month", value_tile=True),
    ]


# Keys match ModuleMenuGrid's MODULES (lower-cased route segment).
# ponytail: hand-kept table map; add a source here when a module grows a new document.
MODULES: dict[str, list[dict]] = {
    "procurement": [
        S("Indents", "proc_indent", "indent_date", status=True, where=f"{BR} AND t.active = 1"),
        S("Purchase Orders", "proc_po", "po_date", "net_amount", "₹", status=True),
        S("Inwards", "proc_inward", "inward_date", "net_amount", "₹"),
        S("Bill Passes", "proc_inward", "billpass_date", "net_amount", "₹",
          where=f"{BR} AND t.billpass_no IS NOT NULL"),
    ],
    "sales": [
        S("Quotations", "sales_quotation", "quotation_date", "net_amount", "₹", True, f"{BR} AND t.active = 1"),
        S("Sales Orders", "sales_order", "sales_order_date", "net_amount", "₹", True, f"{BR} AND t.active = 1"),
        S("Delivery Orders", "sales_delivery_order", "delivery_order_date", "net_amount", "₹", True, f"{BR} AND t.active = 1"),
        S("Invoices", "sales_invoice", "invoice_date", "invoice_amount", "₹", True, f"{BR} AND t.active = 1"),
    ],
    "jutepurchase": [
        S("Jute POs", "jute_po", "po_date", "jute_po_value", "₹", status=True),
        S("Jute MRs", "jute_mr", "jute_mr_date", "total_amount", "₹", status=True),
        S("Jute Bill Passes", "jute_mr", "bill_pass_date", "net_total", "₹",
          where=f"{BR} AND t.bill_pass_no IS NOT NULL"),
        S("Jute Issues", "jute_issue", "issue_date", "issue_value", "₹", status=True),
    ],
    "inventory": [
        dict(label="Stock in Hand", custom=_stock_stats),
        # Issue value = issue_qty * receipt-lot rate, same as the issue reports
        S("Issues", "issue_hdr", "issue_date", ISSUE_VALUE, "₹", True, f"{BR} AND t.active = 1"),
        S("Stores Receipts", "proc_inward", "sr_date", "sr_value", "₹", where=f"{BR} AND t.sr_no IS NOT NULL"),
        S("Transfers", "proc_transfer", "transfer_date", status=True),
    ],
    "juteproduction": [
        S("Beaming", "jute_prod_beaming_daily", "tran_date", "act_prod_kg", "kg", where=f"{BR} AND t.active = 1"),
        S("Finishing", "jute_prod_finishing_daily", "tran_date", "prod_wt_kg", "kg", where=f"{BR} AND t.active = 1"),
        S("Waste", "jute_prod_waste_entry", "tran_date", "waste_qty", "kg", where=f"{BR} AND t.active = 1"),
        S("Spinning Entries", "jute_prod_spinning_daily", "tran_date", where=f"{BR} AND t.active = 1"),
        S("Stoppage", "jute_prod_stoppage_hours", "tran_date", "stoppage_hours", "hrs", where=f"{BR} AND t.active = 1"),
        S("Bales Packed", "jute_prod_bale_entry", "tran_date", where=f"{BR} AND t.active = 1"),
        S("Bale Despatches", "jute_prod_bale_despatch", "despatch_date", where=f"{BR} AND t.active = 1"),
    ],
    "jutesqc": [
        S(label, table, "entry_date", where=f"{BR} AND t.active = 1")
        for label, table in [
            ("Morrah Weight", "jute_sqc_morrah_wt"),
            ("Drawing Sliver", "jute_sqc_draw_sliver_wt"),
            ("Breaker Card", "jute_sqc_breaker_card_swt"),
            ("Finisher Drawing", "jute_sqc_fin_draw_sliver_wt"),
            ("Spinning Count", "jute_sqc_spinning_count"),
            ("Beam MR", "jute_sqc_beam_mr"),
            ("Packing MR", "jute_sqc_packing_mr"),
            ("Fabric Faults", "jute_sqc_fabric_fault"),
        ]
    ],
    "jutemechanical": [
        S("Breakdowns", "jute_mech_breakdown", "tran_date", "production_loss_qty", "kg", where=f"{BR} AND t.active = 1"),
        S("PM Executions", "jute_mech_pm_execution", "pm_date", where=f"{BR} AND t.active = 1"),
        S("Overhauls", "jute_mech_overhaul", "from_date", where=f"{BR} AND t.active = 1"),
    ],
    "hrms": [
        S("Active Employees", "hrms_ed_official_details", where=f"{BR} AND t.active = 1"),
        S("New Joinings", "hrms_ed_official_details", "date_of_join", where=f"{BR} AND t.active = 1"),
        S("Leave Applications", "leave_transactions", "leave_from_date"),
        S("Pay Periods", "pay_period", "FROM_DATE"),
    ],
    "wages": [
        dict(label="Payroll", custom=_payroll_stats),
        S("Machine Hours", "ejm_wages_group_mc_hours", FORTNIGHT, "mc_hours", "hrs", where=DEPT_BR),
    ],
    "hrmsmasters": [
        S("Departments", "dept_mst"),
        S("Designations", "designation_mst", where=f"{BR} AND t.active = 1"),
        S("Categories", "category_mst"),
        S("Contractors", "contractor_mst"),
        S("Holidays", "holiday_master", "holiday_date"),
    ],
    "masters": [
        S("Items", "item_mst",
          where="t.active = 1 AND t.item_grp_id IN (SELECT item_grp_id FROM item_grp_mst WHERE co_id = :co_id)"),
        S("Item Groups", "item_grp_mst", where=f"{CO} AND t.active = 1"),
        S("Parties", "party_mst", where=f"{CO} AND t.active = 1"),
        S("Warehouses", "warehouse_mst"),
        S("Projects", "project_mst", where=f"{BR} AND t.active = 1"),
        S("Jute Qualities", "jute_quality_mst", where=CO),
    ],
    "accounting": [
        S("Vouchers", "acc_voucher", "voucher_date", "total_amount", "₹", True, f"{CO} AND t.active = 1"),
        S("Ledgers", "acc_ledger", where=CO),
    ],
    "bomcosting": [
        S("Cost Entries", "bom_cost_entry", "effective_date", "amount", "₹", where=f"{CO} AND t.active = 1"),
        S("Cost Elements", "cost_element_mst", where=f"{CO} AND t.active = 1"),
    ],
    "jutecosting": [
        S("Market Rates", "jute_market_rate", "rate_date", where=f"{CO} AND t.active = 1"),
        S("Cost Parameters", "jute_cost_param", "effective_date", where=f"{CO} AND t.active = 1"),
    ],
}


def _months_back(d: date, n: int) -> date:
    m = d.year * 12 + d.month - 1 - n
    return date(m // 12, m % 12 + 1, 1)


def _run(db: Session, sql: str, params: dict):
    stmt = text(sql)
    if ":branches" in sql:
        stmt = stmt.bindparams(bindparam("branches", expanding=True))
    return db.execute(stmt, params).mappings().all()


def _col(x: str) -> str:
    """Bare column name -> t.<col>; anything else is already an expression."""
    return f"t.{x}" if x.isidentifier() else x


def _source_stats(db: Session, src: dict, params: dict) -> dict:
    t, d, m = src["table"], src["date_col"], src["measure"]
    measure = f"COALESCE(SUM({_col(m)}), 0)" if m else "NULL"
    out = {"label": src["label"], "unit": src["unit"], "snapshot": d is None,
           "count": 0, "amount": None, "monthly": [], "status": []}

    if d is None:
        row = _run(db, f"SELECT COUNT(*) AS c FROM {t} t WHERE {src['where']}", params)[0]
        out["count"] = int(row["c"])
        return out

    d = _col(d)
    where = f"{src['where']} AND {d} >= :date_from AND {d} < DATE_ADD(:date_to, INTERVAL 1 DAY)"
    rows = _run(db, f"""
        SELECT DATE_FORMAT({d}, '%Y-%m') AS month, COUNT(*) AS c, {measure} AS a
        FROM {t} t WHERE {where} GROUP BY month ORDER BY month""", params)
    out["monthly"] = [{"month": r["month"], "count": int(r["c"]),
                       "amount": float(r["a"]) if m else None} for r in rows]
    out["count"] = sum(r["count"] for r in out["monthly"])
    if m:
        out["amount"] = round(sum(r["amount"] for r in out["monthly"]), 2)

    if src["status"]:
        rows = _run(db, f"""
            SELECT COALESCE(s.status_name, 'UNKNOWN') AS status, COUNT(*) AS c
            FROM {t} t LEFT JOIN status_mst s ON s.status_id = t.status_id
            WHERE {where} GROUP BY status ORDER BY c DESC""", params)
        out["status"] = [{"status": r["status"], "count": int(r["c"])} for r in rows]
    return out


@router.get("/{module}")
def get_module_dashboard(
    module: str,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int = Query(...),
    branch_ids: str = Query("", description="Comma-separated branch ids"),
    date_from: date | None = None,
    date_to: date | None = None,
):
    sources = MODULES.get(module.lower())
    if sources is None:
        raise HTTPException(status_code=404, detail=f"No dashboard for module '{module}'")
    try:
        branches = [int(b) for b in branch_ids.split(",") if b.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="branch_ids must be comma-separated integers")

    date_to = date_to or date.today()
    date_from = date_from or _months_back(date_to, 11)
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from is after date_to")

    # [-1] matches no branch: an empty selection shows zeros, never every branch
    params = {"co_id": co_id, "branches": branches or [-1],
              "date_from": date_from, "date_to": date_to}

    data, skipped = [], []
    for src in sources:
        try:
            out = src["custom"](db, params) if "custom" in src else _source_stats(db, src, params)
            data.extend(out if isinstance(out, list) else [out])
        except SQLAlchemyError as e:  # table/column absent on this tenant
            db.rollback()
            logger.warning("module dashboard %s/%s skipped: %s", module, src["label"], e.orig if hasattr(e, "orig") else e)
            skipped.append(src["label"])

    return {"data": {"module": module.lower(), "date_from": date_from.isoformat(),
                     "date_to": date_to.isoformat(), "sources": data, "skipped": skipped}}
