"""
Period Master (period_type_mst, period_type_slot, period_mst).

Reusable periods that reports and wages can reference. A period type is the
rule, scoped by co_id + branch_id:

  FORTNIGHTLY  slots F1 day 1..15, F2 day 16..month end
  MONTHLY      one slot, day 1..month end (or e.g. 26th prev month..25th)
  CUSTOM       any contiguous split of the month
  WEEKLY       no slots; 7-day weeks starting on week_start_day (ISO 1=Mon)

POST /period_generate turns a type into dated period_mst rows for a year.
Generation is idempotent (unique type + from_date) and never overlaps rows
already there. Once a type has periods its rule is locked; delete the
generated periods to change it.

Standalone for now: the legacy forthnight_period_mst (EJM FNE screens) and
pay_period (one row per wage run) are untouched.
"""

import calendar
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import bindparam
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db

router = APIRouter()

FREQUENCIES = ("FORTNIGHTLY", "MONTHLY", "WEEKLY", "CUSTOM")
SLOT_COUNTS = {"FORTNIGHTLY": 2, "MONTHLY": 1}
WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# A slot that is not the last must end by the 27th, so the next slot still has
# at least one day in February.
MAX_INNER_TO_DAY = 27
# A cycle that starts in the previous month must start on a day every month has.
MAX_PREV_MONTH_FROM_DAY = 28


# ─── Pure rule helpers ──────────────────────────────────────────────


def _month_end(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def validate_slots(slots: list[dict]) -> list[dict]:
    """Check a month split is contiguous, gap-free and covers one full cycle.

    Returns the slots sorted and renumbered 1..n. Raises ValueError with a
    user-facing message.
    """
    if not slots:
        raise ValueError("Add at least one slot")
    slots = sorted(slots, key=lambda s: s["slot_no"])
    codes = set()
    for i, s in enumerate(slots, start=1):
        s["slot_no"] = i
        label = f"Slot {i}"
        if not s["slot_name"]:
            raise ValueError(f"{label}: name is required")
        if not s["slot_code"]:
            raise ValueError(f"{label}: code is required")
        if s["slot_code"].upper() in codes:
            raise ValueError(f"{label}: code {s['slot_code']} is used twice")
        codes.add(s["slot_code"].upper())
        if not 1 <= s["from_day"] <= 31:
            raise ValueError(f"{label}: from day must be 1 to 31")
        if s["to_day"] is not None and not 1 <= s["to_day"] <= 31:
            raise ValueError(f"{label}: to day must be 1 to 31, or blank for month end")
        if s["from_prev_month"] and i != 1:
            raise ValueError(f"{label}: only the first slot can start in the previous month")

    first, last = slots[0], slots[-1]
    if first["from_prev_month"]:
        start = first["from_day"]
        if not 2 <= start <= MAX_PREV_MONTH_FROM_DAY:
            raise ValueError(f"Slot 1: a previous-month start must be day 2 to {MAX_PREV_MONTH_FROM_DAY}")
        if last["to_day"] != start - 1:
            raise ValueError(f"Slot {last['slot_no']}: must end on day {start - 1}, the day before slot 1 starts")
    else:
        if first["from_day"] != 1:
            raise ValueError("Slot 1: must start on day 1 (or tick 'starts in previous month')")
        if last["to_day"] is not None:
            raise ValueError(f"Slot {last['slot_no']}: must end at month end (leave to day blank)")

    for i, s in enumerate(slots):
        is_last = i == len(slots) - 1
        if not is_last:
            if s["to_day"] is None:
                raise ValueError(f"Slot {s['slot_no']}: only the last slot can end at month end")
            if s["to_day"] > MAX_INNER_TO_DAY:
                raise ValueError(f"Slot {s['slot_no']}: must end by day {MAX_INNER_TO_DAY} so every month has the next slot")
        # A previous-month first slot runs from last month into this one, so its
        # to_day is compared within the current month only.
        if not s["from_prev_month"] and s["to_day"] is not None and s["to_day"] < s["from_day"]:
            raise ValueError(f"Slot {s['slot_no']}: to day is before from day")
        if i > 0:
            prev_to = slots[i - 1]["to_day"]
            if s["from_day"] != prev_to + 1:
                raise ValueError(f"Slot {s['slot_no']}: must start on day {prev_to + 1}, the day after slot {i} ends")
    return slots


def slot_dates(slot: dict, year: int, month: int) -> tuple[date, date]:
    """From/to dates of a slot for the period labelled (year, month)."""
    if slot["from_prev_month"]:
        py, pm = _prev_month(year, month)
        from_date = date(py, pm, slot["from_day"])
    else:
        from_date = date(year, month, slot["from_day"])
    to_day = slot["to_day"] if slot["to_day"] is not None else _month_end(year, month)
    return from_date, date(year, month, to_day)


def build_month_periods(slots: list[dict], year: int) -> list[dict]:
    """Dated periods for every month of the year, in date order."""
    out = []
    for month in range(1, 13):
        label = date(year, month, 1).strftime("%b-%Y")
        for s in slots:
            from_date, to_date = slot_dates(s, year, month)
            out.append({
                "period_type_slot_id": s.get("period_type_slot_id"),
                "period_year": year,
                "period_month": month,
                "period_no": s["slot_no"],
                "period_code": f"{year}{month:02d}-{s['slot_code']}",
                "period_name": f"{label} {s['slot_name']}",
                "from_date": from_date,
                "to_date": to_date,
            })
    return out


def build_week_periods(week_start_day: int, year: int) -> list[dict]:
    """Weeks whose first day falls in the year, numbered 1..52/53."""
    d = date(year, 1, 1)
    while d.isoweekday() != week_start_day:
        d += timedelta(days=1)
    out = []
    n = 1
    while d.year == year:
        end = d + timedelta(days=6)
        out.append({
            "period_type_slot_id": None,
            "period_year": year,
            "period_month": d.month,
            "period_no": n,
            "period_code": f"{year}-W{n:02d}",
            "period_name": f"Week {n:02d} ({d:%d-%b} to {end:%d-%b-%Y})",
            "from_date": d,
            "to_date": end,
        })
        d += timedelta(days=7)
        n += 1
    return out


def slot_summary(frequency: str, week_start_day: int | None, slots: list[dict]) -> str:
    if frequency == "WEEKLY":
        if not week_start_day:
            return ""
        return f"{WEEKDAY_SHORT[week_start_day - 1]}–{WEEKDAY_SHORT[(week_start_day + 5) % 7]}"
    parts = []
    for s in slots:
        start = f"{s['from_day']}(prev)" if s["from_prev_month"] else str(s["from_day"])
        end = "EOM" if s["to_day"] is None else str(s["to_day"])
        parts.append(f"{s['slot_code']} {start}–{end}")
    return ", ".join(parts)


# ─── SQL ────────────────────────────────────────────────────────────


def get_period_type_list_query():
    return text("""
        SELECT t.period_type_id, t.co_id, t.branch_id, bm.branch_name, t.period_type_name,
               t.frequency, t.week_start_day, t.remarks, t.active,
               (SELECT COUNT(*) FROM period_mst p WHERE p.period_type_id = t.period_type_id) AS period_count
        FROM period_type_mst t
        LEFT JOIN branch_mst bm ON bm.branch_id = t.branch_id
        WHERE t.co_id = :co_id
          AND (:branch_id IS NULL OR t.branch_id = :branch_id)
        ORDER BY t.active DESC, t.period_type_name
    """)


def get_period_type_query():
    return text("""
        SELECT period_type_id, co_id, branch_id, period_type_name, frequency, week_start_day, remarks, active,
               (SELECT COUNT(*) FROM period_mst p WHERE p.period_type_id = t.period_type_id) AS period_count
        FROM period_type_mst t
        WHERE period_type_id = :period_type_id
    """)


def get_slots_query():
    return text("""
        SELECT period_type_slot_id, period_type_id, slot_no, slot_name, slot_code,
               from_day, from_prev_month, to_day
        FROM period_type_slot
        WHERE period_type_id IN :period_type_ids
        ORDER BY period_type_id, slot_no
    """).bindparams(bindparam("period_type_ids", expanding=True))


def get_periods_query():
    return text("""
        SELECT period_id, period_type_id, period_type_slot_id, period_year, period_month, period_no,
               period_code, period_name, from_date, to_date, active
        FROM period_mst
        WHERE period_type_id = :period_type_id
          AND (:year IS NULL OR period_year = :year)
        ORDER BY from_date
    """)


def get_period_options_query():
    return text("""
        SELECT p.period_id, p.period_type_id, t.period_type_name, t.frequency,
               p.period_code, p.period_name, p.from_date, p.to_date
        FROM period_mst p
        JOIN period_type_mst t ON t.period_type_id = p.period_type_id
        WHERE p.co_id = :co_id
          AND p.branch_id = :branch_id
          AND p.active = 1 AND t.active = 1
          AND (:period_type_id IS NULL OR p.period_type_id = :period_type_id)
          AND (:from_date IS NULL OR p.to_date >= :from_date)
          AND (:to_date IS NULL OR p.from_date <= :to_date)
        ORDER BY t.period_type_name, p.from_date
    """)


def _overlap_query():
    return text("""
        SELECT period_code, from_date, to_date FROM period_mst
        WHERE period_type_id = :period_type_id
          AND from_date <= :to_date AND to_date >= :from_date
          AND (:period_id IS NULL OR period_id <> :period_id)
        LIMIT 1
    """)


def _duplicate_name_query():
    return text("""
        SELECT COUNT(*) AS cnt FROM period_type_mst
        WHERE co_id = :co_id AND branch_id = :branch_id
          AND period_type_name = :period_type_name
          AND (:period_type_id IS NULL OR period_type_id <> :period_type_id)
    """)


# ─── Request helpers ────────────────────────────────────────────────


def _int(value, default, label: str):
    if value in (None, "", "null"):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} must be a number")


def _required_int(value, label: str) -> int:
    v = _int(value, None, label)
    if v is None:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    return v


def _date(value, label: str) -> date:
    if not value:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD")


def _flag(value, label: str) -> int:
    flag = _int(value, 1, label)
    if flag not in (0, 1):
        raise HTTPException(status_code=400, detail=f"{label} must be Yes or No")
    return flag


def _user_id(token_data: dict) -> int | None:
    return token_data.get("user_id") if isinstance(token_data, dict) else None


def parse_type_body(body: dict) -> dict:
    """Validate a period type create/edit body. Raises HTTPException(400)."""
    name = str(body.get("period_type_name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Name must be at most 100 characters")
    frequency = str(body.get("frequency") or "").strip().upper()
    if frequency not in FREQUENCIES:
        raise HTTPException(status_code=400, detail="Frequency must be Fortnightly, Monthly, Weekly or Custom")
    remarks = (str(body.get("remarks") or "").strip() or None)
    if remarks and len(remarks) > 255:
        raise HTTPException(status_code=400, detail="Remarks must be at most 255 characters")

    week_start_day = None
    slots: list[dict] = []
    if frequency == "WEEKLY":
        week_start_day = _int(body.get("week_start_day"), None, "Week start day")
        if week_start_day is None or not 1 <= week_start_day <= 7:
            raise HTTPException(status_code=400, detail="Pick the day the week starts on")
    else:
        for raw in body.get("slots") or []:
            slots.append({
                "slot_no": _int(raw.get("slot_no"), len(slots) + 1, "Slot no"),
                "slot_name": str(raw.get("slot_name") or "").strip()[:50],
                "slot_code": str(raw.get("slot_code") or "").strip()[:10],
                "from_day": _required_int(raw.get("from_day"), "From day"),
                "from_prev_month": 1 if _int(raw.get("from_prev_month"), 0, "Starts in previous month") else 0,
                "to_day": _int(raw.get("to_day"), None, "To day"),
            })
        expected = SLOT_COUNTS.get(frequency)
        if expected and len(slots) != expected:
            raise HTTPException(status_code=400, detail=f"{frequency.title()} needs exactly {expected} slot(s)")
        try:
            slots = validate_slots(slots)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return {
        "period_type_name": name,
        "frequency": frequency,
        "week_start_day": week_start_day,
        "remarks": remarks,
        "active": _flag(body.get("active"), "Active"),
        "slots": slots,
    }


def _load_slots(db: Session, period_type_ids: list[int]) -> dict[int, list[dict]]:
    by_type: dict[int, list[dict]] = {}
    if not period_type_ids:
        return by_type
    for r in db.execute(get_slots_query(), {"period_type_ids": period_type_ids}).fetchall():
        by_type.setdefault(r.period_type_id, []).append(dict(r._mapping))
    return by_type


def _load_type(db: Session, period_type_id: int):
    row = db.execute(get_period_type_query(), {"period_type_id": period_type_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Period type not found")
    return row


def _check_duplicate_name(db: Session, co_id: int, branch_id: int, name: str, period_type_id: int | None):
    dup = db.execute(_duplicate_name_query(), {
        "co_id": co_id, "branch_id": branch_id,
        "period_type_name": name, "period_type_id": period_type_id,
    }).fetchone()
    if dup and dup.cnt > 0:
        raise HTTPException(status_code=400, detail=f"A period type named '{name}' already exists for this branch")


def _insert_slots(db: Session, period_type_id: int, slots: list[dict]):
    for s in slots:
        db.execute(text("""
            INSERT INTO period_type_slot
                (period_type_id, slot_no, slot_name, slot_code, from_day, from_prev_month, to_day)
            VALUES (:period_type_id, :slot_no, :slot_name, :slot_code, :from_day, :from_prev_month, :to_day)
        """), {"period_type_id": period_type_id, **s})


def _period_row(m: dict) -> dict:
    return {
        **m,
        "from_date": m["from_date"].isoformat() if m.get("from_date") else None,
        "to_date": m["to_date"].isoformat() if m.get("to_date") else None,
    }


# ─── Period type endpoints ──────────────────────────────────────────


@router.get("/get_period_type_table")
def get_period_type_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id = _required_int(request.query_params.get("co_id"), "co_id")
        branch_id = _int(request.query_params.get("branch_id"), None, "Branch")
        search = (request.query_params.get("search") or "").strip().lower()
        page = _int(request.query_params.get("page"), 1, "Page")
        limit = _int(request.query_params.get("limit"), 10, "Limit")

        rows = [dict(r._mapping) for r in db.execute(
            get_period_type_list_query(), {"co_id": co_id, "branch_id": branch_id},
        ).fetchall()]
        slots = _load_slots(db, [r["period_type_id"] for r in rows])
        all_data = []
        for r in rows:
            r["slot_summary"] = slot_summary(r["frequency"], r["week_start_day"], slots.get(r["period_type_id"], []))
            # A handful of types per branch — filter in Python.
            if search and search not in f"{r['period_type_name']} {r['frequency']} {r['branch_name'] or ''}".lower():
                continue
            all_data.append(r)

        start = (page - 1) * limit
        return {"data": all_data[start:start + limit], "total": len(all_data), "page": page, "limit": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_period_type_by_id/{period_type_id}")
def get_period_type_by_id(
    period_type_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        data = dict(_load_type(db, period_type_id)._mapping)
        data["slots"] = _load_slots(db, [period_type_id]).get(period_type_id, [])
        data["rules_locked"] = data["period_count"] > 0
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/period_type_create")
def period_type_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        body = parse_json_body(request)
        co_id = _required_int(body.get("co_id"), "Company")
        if not body.get("branch_id"):
            raise HTTPException(status_code=400, detail="Select a branch in the sidebar before saving")
        branch_id = _required_int(body.get("branch_id"), "Branch")
        values = parse_type_body(body)
        _check_duplicate_name(db, co_id, branch_id, values["period_type_name"], None)

        result = db.execute(text("""
            INSERT INTO period_type_mst
                (co_id, branch_id, period_type_name, frequency, week_start_day, remarks, active, updated_by)
            VALUES (:co_id, :branch_id, :period_type_name, :frequency, :week_start_day, :remarks, :active, :updated_by)
        """), {
            "co_id": co_id, "branch_id": branch_id, "updated_by": _user_id(token_data),
            **{k: v for k, v in values.items() if k != "slots"},
        })
        period_type_id = int(result.lastrowid)
        _insert_slots(db, period_type_id, values["slots"])
        db.commit()
        return {"message": "Period type created successfully", "period_type_id": period_type_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/period_type_edit/{period_type_id}")
def period_type_edit(
    period_type_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Company/branch stay as created. The rule (frequency, week start, slots)
    can only change while the type has no generated periods."""
    try:
        values = parse_type_body(parse_json_body(request))
        existing = _load_type(db, period_type_id)
        _check_duplicate_name(db, existing.co_id, existing.branch_id, values["period_type_name"], period_type_id)

        if existing.period_count > 0:
            old_slots = _load_slots(db, [period_type_id]).get(period_type_id, [])
            keys = ("slot_no", "slot_name", "slot_code", "from_day", "from_prev_month", "to_day")
            old_rule = [tuple(s[k] for k in keys) for s in old_slots]
            new_rule = [tuple(s[k] for k in keys) for s in values["slots"]]
            if (existing.frequency != values["frequency"]
                    or existing.week_start_day != values["week_start_day"]
                    or old_rule != new_rule):
                raise HTTPException(
                    status_code=400,
                    detail="Rules are locked because periods were generated; delete the generated periods to change them",
                )

        db.execute(text("""
            UPDATE period_type_mst
            SET period_type_name = :period_type_name, frequency = :frequency, week_start_day = :week_start_day,
                remarks = :remarks, active = :active, updated_by = :updated_by
            WHERE period_type_id = :period_type_id
        """), {
            "period_type_id": period_type_id, "updated_by": _user_id(token_data),
            **{k: v for k, v in values.items() if k != "slots"},
        })
        if existing.period_count == 0:
            db.execute(text("DELETE FROM period_type_slot WHERE period_type_id = :period_type_id"),
                       {"period_type_id": period_type_id})
            _insert_slots(db, period_type_id, values["slots"])
        db.commit()
        return {"message": "Period type updated successfully", "period_type_id": period_type_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── Dated periods ──────────────────────────────────────────────────


@router.post("/period_generate")
def period_generate(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create the year's periods. Periods already there (same from date) are
    skipped; one that would overlap a different existing period is reported
    as a conflict and not created."""
    try:
        body = parse_json_body(request)
        period_type_id = _required_int(body.get("period_type_id"), "Period type")
        year = _required_int(body.get("year"), "Year")
        if not 2000 <= year <= 2100:
            raise HTTPException(status_code=400, detail="Year must be between 2000 and 2100")
        ptype = _load_type(db, period_type_id)
        if not ptype.active:
            raise HTTPException(status_code=400, detail="Period type is inactive")

        if ptype.frequency == "WEEKLY":
            if not ptype.week_start_day:
                raise HTTPException(status_code=400, detail="Period type has no week start day")
            candidates = build_week_periods(ptype.week_start_day, year)
        else:
            slots = _load_slots(db, [period_type_id]).get(period_type_id, [])
            if not slots:
                raise HTTPException(status_code=400, detail="Period type has no slots")
            candidates = build_month_periods(slots, year)

        existing = {
            r.from_date: r for r in db.execute(text("""
                SELECT period_code, from_date, to_date FROM period_mst WHERE period_type_id = :period_type_id
            """), {"period_type_id": period_type_id}).fetchall()
        }
        created, skipped, conflicts = 0, 0, []
        for p in candidates:
            if p["from_date"] in existing:
                skipped += 1
                continue
            clash = next((e for e in existing.values()
                          if e.from_date <= p["to_date"] and e.to_date >= p["from_date"]), None)
            if clash:
                conflicts.append(f"{p['period_code']} overlaps {clash.period_code}")
                continue
            db.execute(text("""
                INSERT INTO period_mst
                    (period_type_id, period_type_slot_id, co_id, branch_id, period_year, period_month, period_no,
                     period_code, period_name, from_date, to_date, active, updated_by)
                VALUES (:period_type_id, :period_type_slot_id, :co_id, :branch_id, :period_year, :period_month,
                        :period_no, :period_code, :period_name, :from_date, :to_date, 1, :updated_by)
            """), {
                **p, "period_type_id": period_type_id, "co_id": ptype.co_id,
                "branch_id": ptype.branch_id, "updated_by": _user_id(token_data),
            })
            created += 1
        db.commit()
        return {
            "message": f"{created} period(s) created, {skipped} already existed"
                       + (f", {len(conflicts)} skipped for overlap" if conflicts else ""),
            "created": created, "skipped": skipped, "conflicts": conflicts,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_period_table")
def get_period_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        period_type_id = _required_int(request.query_params.get("period_type_id"), "Period type")
        year = _int(request.query_params.get("year"), None, "Year")
        rows = db.execute(get_periods_query(), {"period_type_id": period_type_id, "year": year}).fetchall()
        return {"data": [_period_row(dict(r._mapping)) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/period_edit/{period_id}")
def period_edit(
    period_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Adjust one generated period (e.g. a holiday-shifted fortnight)."""
    try:
        body = parse_json_body(request)
        name = str(body.get("period_name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")
        from_date = _date(body.get("from_date"), "From date")
        to_date = _date(body.get("to_date"), "To date")
        if to_date < from_date:
            raise HTTPException(status_code=400, detail="To date is before from date")
        active = _flag(body.get("active"), "Active")

        row = db.execute(text("SELECT period_type_id FROM period_mst WHERE period_id = :period_id"),
                         {"period_id": period_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Period not found")
        clash = db.execute(_overlap_query(), {
            "period_type_id": row.period_type_id, "from_date": from_date,
            "to_date": to_date, "period_id": period_id,
        }).fetchone()
        if clash:
            raise HTTPException(status_code=400, detail=f"Dates overlap period {clash.period_code}")

        db.execute(text("""
            UPDATE period_mst
            SET period_name = :period_name, from_date = :from_date, to_date = :to_date,
                active = :active, updated_by = :updated_by
            WHERE period_id = :period_id
        """), {
            "period_name": name[:100], "from_date": from_date, "to_date": to_date,
            "active": active, "updated_by": _user_id(token_data), "period_id": period_id,
        })
        db.commit()
        return {"message": "Period updated successfully"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/period_delete/{period_id}")
def period_delete(
    period_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Hard delete: nothing references period_mst yet. Once reports or wages
    store period_id, check those references here first."""
    try:
        result = db.execute(text("DELETE FROM period_mst WHERE period_id = :period_id"), {"period_id": period_id})
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Period not found")
        db.commit()
        return {"message": "Period deleted"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_period_options")
def get_period_options(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Active periods for a branch, for report / wages pickers. Optional
    period_type_id and a from/to date window (periods overlapping it)."""
    try:
        q = request.query_params
        params = {
            "co_id": _required_int(q.get("co_id"), "co_id"),
            "branch_id": _required_int(q.get("branch_id"), "Branch"),
            "period_type_id": _int(q.get("period_type_id"), None, "Period type"),
            "from_date": _date(q.get("from_date"), "From date") if q.get("from_date") else None,
            "to_date": _date(q.get("to_date"), "To date") if q.get("to_date") else None,
        }
        rows = db.execute(get_period_options_query(), params).fetchall()
        return {"data": [_period_row(dict(r._mapping)) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
