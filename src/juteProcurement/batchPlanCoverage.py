"""
Batch Cost Report — plan coverage messages (TKT-000206).

"The planned weight is not appearing. ... If the batch plan is not assigned
for the day due to which it is not showing then please display a message
stating the same."

Planned weight = batch plan % x that day's actual issue for the yarn type, and
only Open/Approved batch daily assignments count. So it reads 0 when:
  1. no batch plan is assigned to the yarn type for the day (or only a
     Draft / Pending-approval one), or
  2. nothing was issued that day (plan % x 0).

`summarize_plan_coverage` turns the rows of get_batch_cost_plan_coverage_query
into those explanations. Pure — no DB, no FastAPI.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

# How many dates the MTD message lists before "and N more".
MAX_LISTED_DAYS = 10

ASSIGN_MENU = "Jute Batch Yarn Assign Daily"


def _iso(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _dmy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}-{m}-{y}"


def _assignment_state(row: dict) -> str:
    if row.get("counted"):
        return "assigned"
    if row.get("pending"):
        return "pending"
    if row.get("draft"):
        return "draft"
    return "none"


def _yarn_label(row: dict) -> str:
    name = row.get("yarn_type_name") or f"yarn type {row.get('yarn_type_id')}"
    state = _assignment_state(row)
    if state == "pending":
        return f"{name} (assignment pending approval)"
    if state == "draft":
        return f"{name} (assignment still in Draft)"
    return name


def summarize_plan_coverage(rows: Iterable[dict], report_date: str) -> dict:
    """
    rows: get_batch_cost_plan_coverage_query output (d, yarn_type_id,
    yarn_type_name, weight, counted, pending, draft).
    report_date: 'YYYY-MM-DD'.

    Returns {
      "day_has_issue": bool,
      "day_has_plan": bool,               # any Open/Approved assignment that day
      "day_unplanned_yarns": [{yarn_type_id, yarn_type_name, weight, assignment}],
      "mtd_unplanned_days": ['YYYY-MM-DD', ...],   # before the report date
      "messages": [{"severity": "warning" | "info", "text": str}],
    }
    `assignment` is "none", "draft" or "pending".
    """
    day_rows: list[dict] = []
    mtd_days: set[str] = set()
    for r in rows:
        d = _iso(r["d"])
        if d == report_date:
            day_rows.append(r)
        elif float(r.get("weight") or 0) > 0 and not r.get("counted"):
            mtd_days.add(d)

    issued = [r for r in day_rows if float(r.get("weight") or 0) > 0]
    unplanned = [r for r in issued if not r.get("counted")]
    day_has_plan = any(r.get("counted") for r in day_rows)
    when = _dmy(report_date)

    messages: list[dict] = []
    if not issued:
        if day_has_plan:
            text = (
                f"No jute was issued on {when}, so planned weight is 0 "
                "(planned weight = batch plan % x the day's actual issue)."
            )
        else:
            text = f"No jute was issued and no batch plan is assigned for {when}."
        messages.append({"severity": "info", "text": text})
    elif len(unplanned) == len(issued) and not day_has_plan:
        text = (
            f"Batch plan is not assigned for {when}, so planned weight is not shown. "
            f"Assign it in {ASSIGN_MENU} (only Open or Approved assignments count)."
        )
        waiting = [_yarn_label(r) for r in unplanned if _assignment_state(r) != "none"]
        if waiting:
            text += " Not yet counted: " + ", ".join(waiting) + "."
        messages.append({"severity": "warning", "text": text})
    elif unplanned:
        names = ", ".join(_yarn_label(r) for r in unplanned)
        messages.append({
            "severity": "warning",
            "text": (
                f"Batch plan is not assigned on {when} for: {names}. Planned weight "
                f"for these yarn qualities is 0 — assign them in {ASSIGN_MENU} "
                "(only Open or Approved assignments count)."
            ),
        })

    mtd_list = sorted(mtd_days)
    if mtd_list:
        shown = ", ".join(_dmy(d) for d in mtd_list[:MAX_LISTED_DAYS])
        more = len(mtd_list) - MAX_LISTED_DAYS
        if more > 0:
            shown += f" and {more} more"
        plural = "day" if len(mtd_list) == 1 else "days"
        messages.append({
            "severity": "info",
            "text": (
                f"MTD planned weight leaves out {len(mtd_list)} earlier {plural} of "
                f"this month where a yarn quality was issued without a batch plan: {shown}."
            ),
        })

    return {
        "day_has_issue": bool(issued),
        "day_has_plan": day_has_plan,
        "day_unplanned_yarns": [
            {
                "yarn_type_id": r.get("yarn_type_id"),
                "yarn_type_name": r.get("yarn_type_name"),
                "weight": float(r.get("weight") or 0),
                "assignment": _assignment_state(r),
            }
            for r in unplanned
        ],
        "mtd_unplanned_days": mtd_list,
        "messages": messages,
    }
