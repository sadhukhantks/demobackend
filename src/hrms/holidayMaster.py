"""
Holiday Master (holiday_master).

Branch-scoped holiday calendar. The table has no co_id, so every read is scoped
through branch_mst — the portal company/branch sidebar selection drives the list
and every write stamps the selected branch.

holiday_type: 1 = Occasional, 2 = Weekly, 3 = Monthly.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.hrms.constants import ATT_LIVE_SQL, SHIFT_SQL, SPELL_JOIN
from src.models.hrms import HolidayMaster

router = APIRouter()

HOLIDAY_TYPES = {1: "Occasional", 2: "Weekly", 3: "Monthly"}

# Every earned holiday is credited as a full 8-hour day (legacy hardcoded 8).
HOLIDAY_HOURS = 8

STATUS_OPEN = 1
STATUS_APPROVED = 3
STATUS_REJECTED = 4
# Display names come from status_mst, not a map here — see get_holiday_list_query.


# ─── SQL Queries ────────────────────────────────────────────────────


def get_holiday_list_query():
    return text("""
        SELECT
            h.holiday_id,
            h.branch_id,
            b.branch_name,
            h.holiday,
            h.holiday_date,
            h.holiday_type,
            h.period_start_date,
            h.period_end_date,
            h.spell_id,
            s.spell_name,
            h.status,
            COALESCE(st.status_name, '') AS status_name,
            (SELECT COUNT(*) FROM hrms_holiday_transactions t
              WHERE t.holiday_id = h.holiday_id AND t.is_active = 1) AS processed_count
        FROM holiday_master h
        INNER JOIN branch_mst b ON b.branch_id = h.branch_id
        LEFT JOIN spell_mst s ON s.spell_id = h.spell_id
        LEFT JOIN status_mst st ON st.status_id = h.status
        WHERE b.co_id = :co_id
          AND (:branch_id IS NULL OR h.branch_id = :branch_id)
          AND (:date_from IS NULL OR h.holiday_date >= :date_from)
          AND (:date_to IS NULL OR h.holiday_date <= :date_to)
          AND (:search IS NULL OR h.holiday LIKE :search)
        ORDER BY h.holiday_date DESC, h.holiday_id DESC
    """)


def get_holiday_by_id_query():
    return text("""
        SELECT
            h.holiday_id,
            h.branch_id,
            h.holiday,
            h.holiday_date,
            h.holiday_type,
            h.period_start_date,
            h.period_end_date,
            h.spell_id,
            h.status
        FROM holiday_master h
        WHERE h.holiday_id = :holiday_id
    """)


def _deactivate_transactions_query():
    return text("""
        UPDATE hrms_holiday_transactions
        SET is_active = 0, updated_by = :user_id, updated_date_time = NOW()
        WHERE holiday_id = :holiday_id AND is_active = 1
    """)


def _insert_transactions_query():
    """Legacy Loan_adv_model::updateholidayaData (holget = 1), ported.

    Whoever worked a full day on the qualifying day (working_hours - idle_hours
    >= 8, attendance_type 'R') and is flagged holiday_eligibility = 'Y' earns it,
    plus — for legacy company 1 (NJM) only — anyone on approved sick/leave/casual
    leave that day. The legacy model runs the leave arm inside `if ($comp == 1)`
    and the attendance arm alone for every other company, so `:include_leave` is
    derived from the holiday's company, never guessed.

    Both legacy branches gate the result on
    `tbl_holiday_att_inc_eligibility.holiday_eligibility = 'Y'`; that table now
    carries real data in every tenant this runs against, so the gate is applied.
    Without it a branch like 87 (4,177 'N' against 425 'Y') credits roughly three
    times the employees it should.
    """
    return text(f"""
        INSERT INTO hrms_holiday_transactions
            (holiday_id, eb_id, holiday_hours, eligibility_date, branch_id,
             updated_by, updated_date_time, is_active)
        SELECT :holiday_id, g.eb_id, :holiday_hours, :elig_date, :branch_id,
               :user_id, NOW(), 1
        FROM (
            SELECT DISTINCT eb_id FROM (
                -- Worked the qualifying day. A night spell (C) clocking exactly
                -- 7.5 effective hours is credited an extra half hour.
                SELECT eb_id FROM (
                    SELECT da.eb_id,
                           (IFNULL(da.working_hours, 0) - IFNULL(da.idle_hours, 0)) AS whrs,
                           CASE WHEN {SHIFT_SQL} = 'C'
                                 AND (IFNULL(da.working_hours, 0) - IFNULL(da.idle_hours, 0)) = 7.5
                                THEN 0.5 ELSE 0 END AS nhrs
                    FROM daily_attendance da
                    {SPELL_JOIN}
                    WHERE {ATT_LIVE_SQL}
                      AND da.branch_id = :branch_id
                      AND da.attendance_date = :elig_date
                      AND da.attendance_type = 'R'
                ) a
                GROUP BY eb_id
                -- A full day. The legacy SQL says `>= 6`, but that makes the
                -- night-spell +0.5 above dead code (7.5 already clears 6); the
                -- bonus only means anything against 8, so 8 is the real rule.
                -- Verified on sls: identical result sets either way — no
                -- employee on any processed date lands in the 6..8 band.
                HAVING SUM(whrs + nhrs) >= 8
                UNION ALL
                -- Approved sick / leave / casual leave on the qualifying day.
                SELECT lt.eb_id
                FROM leave_tran_details ltd
                INNER JOIN leave_transactions lt
                        ON lt.leave_transaction_id = ltd.ltran_id
                INNER JOIN hrms_leave_types_mst ltm
                        ON ltm.leave_type_id = lt.leave_type_id
                WHERE :include_leave = 1
                  AND ltd.is_active = 1
                  AND lt.status = 3
                  AND lt.branch_id = :branch_id
                  AND ltd.leave_date = :elig_date
                  AND ltm.leave_type_code IN ('S', 'L', 'C')
            ) u
        ) g
        -- Legacy eligibility gate: only employees flagged for holiday pay.
        INNER JOIN tbl_holiday_att_inc_eligibility thaie
                ON thaie.eb_id = g.eb_id
               AND thaie.is_active = 1
               AND thaie.holiday_eligibility = 'Y'
        -- Restrict to the selected pay scheme when one was chosen. A branch's
        -- workforce spans several schemes (branch 29 on 2026-08-14: 83 on EJM
        -- VOUCHER WORKER, 31 on EMPIRE_MAIN_PAYROLL, 27 trainees, ...), so a
        -- wages run for one scheme must not credit the others. NULL = no filter,
        -- which is what per-holiday Approve uses — it has no scheme to select.
        WHERE :pay_scheme_id IS NULL
           OR EXISTS (
                SELECT 1 FROM pay_employee_payscheme eps
                WHERE eps.EMPLOYEEID = g.eb_id
                  AND eps.STATUS = 1
                  AND eps.PAY_SCHEME_ID = :pay_scheme_id)
    """)


# Branches whose holiday process credits approved leave alongside worked
# attendance. Legacy ran this as `if ($comp == 1)` — company-scoped, NJM only —
# but everything in this module is scoped by the sidebar branch, and legacy
# company 1's people were split across two sls branches by the co1 -> co106
# remap, so the rule is keyed by branch here. Branch 4 = FACTORY co 1 (NJM).
LEAVE_ARM_BRANCH_IDS = frozenset({4})


def _duplicate_query():
    """Same branch + date + spell already declared (ignoring one id)."""
    return text("""
        SELECT COUNT(*) AS cnt FROM holiday_master
        WHERE branch_id = :branch_id
          AND holiday_date = :holiday_date
          AND COALESCE(spell_id, 0) = COALESCE(:spell_id, 0)
          AND (:holiday_id IS NULL OR holiday_id <> :holiday_id)
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _branch_param(request: Request) -> int | None:
    raw = request.query_params.get("branch_id")
    return _int(raw, 0, "Branch") if raw not in (None, "", "null") else None


def _date(value, label: str):
    """Parse a YYYY-MM-DD string; empty/None means the column stays NULL."""
    if value in (None, "", "null"):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} must be a valid date")


def _int(value, default: int, label: str) -> int:
    """Parse a numeric query param; anything else is a 400, never a 500."""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} must be a number")


def _validate(body: dict) -> dict:
    """Validates and normalises a holiday payload into column values."""
    branch_id = body.get("branch_id")
    if not branch_id:
        raise HTTPException(status_code=400, detail="Branch is required")

    holiday = (body.get("holiday") or "").strip()
    if not holiday:
        raise HTTPException(status_code=400, detail="Holiday name is required")

    holiday_date = _date(body.get("holiday_date"), "Holiday date")
    if holiday_date is None:
        raise HTTPException(status_code=400, detail="Holiday date is required")

    try:
        holiday_type = int(body.get("holiday_type"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Holiday type is required")
    if holiday_type not in HOLIDAY_TYPES:
        raise HTTPException(status_code=400, detail="Invalid holiday type")

    period_start = _date(body.get("period_start_date"), "Period start date")
    period_end = _date(body.get("period_end_date"), "Period end date")
    if period_start and period_end and period_end < period_start:
        raise HTTPException(
            status_code=400,
            detail="Period end date cannot be before period start date",
        )

    return {
        "branch_id": int(branch_id),
        "holiday": holiday,
        "holiday_date": holiday_date,
        "holiday_type": holiday_type,
        "period_start_date": period_start,
        "period_end_date": period_end,
        "spell_id": int(body["spell_id"]) if body.get("spell_id") else None,
    }


def _check_duplicate(db: Session, values: dict, holiday_id: int | None):
    dup = db.execute(_duplicate_query(), {
        "branch_id": values["branch_id"],
        "holiday_date": values["holiday_date"],
        "spell_id": values["spell_id"],
        "holiday_id": holiday_id,
    }).fetchone()
    if dup and dup.cnt > 0:
        raise HTTPException(
            status_code=400,
            detail="A holiday is already declared for this branch, date and spell",
        )


def _serialize(row: dict) -> dict:
    for key in ("holiday_date", "period_start_date", "period_end_date"):
        if row.get(key):
            row[key] = row[key].isoformat()
    if "holiday_type" in row:
        row["holiday_type_name"] = HOLIDAY_TYPES.get(row["holiday_type"], "")
    return row


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/get_holiday_table")
def get_holiday_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated list of holidays for the selected company/branch."""
    try:
        co_id = request.query_params.get("co_id")
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        search = request.query_params.get("search")
        page = _int(request.query_params.get("page"), 1, "Page")
        limit = _int(request.query_params.get("limit"), 10, "Limit")

        rows = db.execute(get_holiday_list_query(), {
            "co_id": _int(co_id, 0, "co_id"),
            "branch_id": _branch_param(request),
            # Parsed, not passed through — an unparseable date reaching MySQL
            # returns a 500 carrying the whole SQL statement to the browser.
            "date_from": _date(request.query_params.get("date_from"), "Date from"),
            "date_to": _date(request.query_params.get("date_to"), "Date to"),
            "search": f"%{search}%" if search else None,
        }).fetchall()

        all_data = [_serialize(dict(row._mapping)) for row in rows]
        total = len(all_data)
        start = (page - 1) * limit
        return {
            "data": all_data[start:start + limit],
            "total": total,
            "page": page,
            "limit": limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_holiday_by_id/{holiday_id}")
def get_holiday_by_id(
    holiday_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Single holiday row, for the edit dialog."""
    try:
        row = db.execute(get_holiday_by_id_query(), {"holiday_id": holiday_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Holiday not found")
        return {"data": _serialize(dict(row._mapping))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/holiday_create")
def holiday_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create a holiday."""
    try:
        values = _validate(parse_json_body(request))
        _check_duplicate(db, values, None)

        record = HolidayMaster(
            **values,
            status=STATUS_OPEN,
            updated_by=token_data.get("user_id") if token_data else None,
            updated_date_time=datetime.now(),
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        return {
            "message": "Holiday created successfully",
            "holiday_id": record.holiday_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


def _run_holiday_process(db: Session, holiday: dict, user_id: int | None,
                         pay_scheme_id: int | None = None) -> tuple[int, int]:
    """Rebuild hrms_holiday_transactions for one holiday. Does not commit.

    Re-runnable: the holiday's previous transactions are deactivated first,
    exactly like the legacy PHP process.
    """
    if not holiday.get("branch_id"):
        raise HTTPException(status_code=400, detail="Holiday has no branch")

    # Which arms run is a property of the branch being processed, not a
    # caller's choice.
    include_leave = int(holiday["branch_id"]) in LEAVE_ARM_BRANCH_IDS

    # The qualifying day whose attendance/leave decides who earns the holiday —
    # the legacy process used period_start_date for this.
    elig_date = holiday.get("period_start_date")
    if not elig_date:
        raise HTTPException(
            status_code=400,
            detail="Set the Period Start Date (the qualifying attendance day) before approving",
        )

    deactivated = db.execute(_deactivate_transactions_query(), {
        "holiday_id": holiday["holiday_id"], "user_id": user_id,
    }).rowcount
    inserted = db.execute(_insert_transactions_query(), {
        "holiday_id": holiday["holiday_id"],
        "branch_id": holiday["branch_id"],
        "elig_date": elig_date,
        "holiday_hours": HOLIDAY_HOURS,
        "include_leave": 1 if include_leave else 0,
        "pay_scheme_id": pay_scheme_id,
        "user_id": user_id,
    }).rowcount
    return deactivated, inserted


def _holidays_in_range_query():
    return text("""
        SELECT holiday_id, branch_id, holiday, holiday_date,
               period_start_date, status
        FROM holiday_master
        WHERE branch_id = :branch_id
          AND holiday_date >= :date_from
          AND holiday_date <= :date_to
        ORDER BY holiday_date, holiday_id
    """)


@router.post("/holiday_process_range")
def holiday_process_range(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Legacy "Holiday Data" batch (Loan_adv_model::updateholidayaData, holget=1).

    The legacy screen took a date range plus the logged-in company and processed
    every holiday it found. This does the same for the **selected branch** — the
    unit everything in this module is scoped by — running the identical
    per-holiday SQL as holiday_approve.

    Approval is final here, so an already-approved holiday is reported as skipped
    rather than silently reprocessed (legacy re-ran them unconditionally, which
    would rewrite hours a payroll run may already have consumed).
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        if not branch_id:
            raise HTTPException(status_code=400, detail="Branch is required")

        # The wages screen always sends one; per-holiday Approve sends none and
        # then every scheme on the branch is credited.
        raw_scheme = body.get("pay_scheme_id")
        pay_scheme_id = int(raw_scheme) if raw_scheme not in (None, "", "null") else None

        date_from = _date(body.get("date_from"), "From date")
        date_to = _date(body.get("date_to"), "To date")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="From and To dates are required")
        if date_to < date_from:
            raise HTTPException(
                status_code=400, detail="To date cannot be earlier than From date",
            )

        rows = db.execute(_holidays_in_range_query(), {
            "branch_id": int(branch_id),
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        user_id = token_data.get("user_id") if token_data else None
        processed, skipped, total_inserted = [], [], 0

        for row in rows:
            holiday = dict(row._mapping)
            label = f"{holiday['holiday']} ({holiday['holiday_date']})"

            if holiday["status"] == STATUS_APPROVED:
                skipped.append({"holiday": label, "reason": "already approved"})
                continue
            # The qualifying day drives the whole process; without it there is
            # nothing to measure attendance against.
            if not holiday["period_start_date"]:
                skipped.append({"holiday": label, "reason": "no period start date"})
                continue

            # Claim the holiday BEFORE processing. Two overlapping requests
            # (a double-clicked Submit) would otherwise both pass the status
            # check above, and the second one's deactivate would wipe the rows
            # the first had just inserted — leaving an approved holiday with
            # zero active transactions and no way to re-run it.
            claimed = db.execute(text("""
                UPDATE holiday_master
                SET status = :status, updated_by = :user_id, updated_date_time = NOW()
                WHERE holiday_id = :holiday_id AND status <> :status
            """), {
                "status": STATUS_APPROVED,
                "user_id": user_id,
                "holiday_id": holiday["holiday_id"],
            }).rowcount
            if not claimed:
                skipped.append({"holiday": label, "reason": "already approved"})
                continue

            _, inserted = _run_holiday_process(db, holiday, user_id, pay_scheme_id)
            total_inserted += inserted
            processed.append({"holiday": label, "inserted": inserted})

        db.commit()

        if not rows:
            message = "No holidays found in that date range for the selected branch"
        else:
            message = (
                f"{len(processed)} holiday(s) processed, "
                f"{total_inserted} employee credit(s) written"
            )
            if skipped:
                message += f", {len(skipped)} skipped"

        return {
            "message": message,
            "found": len(rows),
            "processed": processed,
            "skipped": skipped,
            "inserted": total_inserted,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/holiday_approve/{holiday_id}")
def holiday_approve(
    holiday_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Approve a holiday and process it into hrms_holiday_transactions.

    Approval is what triggers processing — only approved holidays credit hours,
    and approval is final. The legacy screen ran this over a date range for a
    whole company; here it is per-holiday, but the SQL it runs is the same.
    """
    try:
        existing = db.query(HolidayMaster).filter(
            HolidayMaster.holiday_id == holiday_id,
        ).first()
        if not existing:
            raise HTTPException(status_code=404, detail="Holiday not found")
        if existing.status == STATUS_APPROVED:
            raise HTTPException(status_code=400, detail="Holiday is already approved")

        _, inserted = _run_holiday_process(
            db,
            {
                "holiday_id": holiday_id,
                "branch_id": existing.branch_id,
                "period_start_date": existing.period_start_date,
            },
            token_data.get("user_id") if token_data else None,
        )

        existing.status = STATUS_APPROVED
        existing.updated_by = token_data.get("user_id") if token_data else None
        existing.updated_date_time = datetime.now()
        db.commit()

        return {
            "message": f"Holiday approved — {inserted} employee(s) processed",
            "holiday_id": holiday_id,
            "status": STATUS_APPROVED,
            "inserted": inserted,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/holiday_reject/{holiday_id}")
def holiday_reject(
    holiday_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reject a holiday. Rejected holidays stay editable and re-approvable."""
    try:
        existing = db.query(HolidayMaster).filter(
            HolidayMaster.holiday_id == holiday_id,
        ).first()
        if not existing:
            raise HTTPException(status_code=404, detail="Holiday not found")
        if existing.status == STATUS_REJECTED:
            raise HTTPException(status_code=400, detail="Holiday is already rejected")
        # Approval is final — the UI hides Reject on an approved holiday, and the
        # API enforces the same rule so the credited hours cannot be pulled back
        # out from under a payroll run that has already consumed them.
        if existing.status == STATUS_APPROVED:
            raise HTTPException(
                status_code=400,
                detail="Approved holidays cannot be rejected",
            )

        # Rejecting an Open holiday clears any transactions a previous
        # (since-reopened) run left behind.
        db.execute(_deactivate_transactions_query(), {
            "holiday_id": holiday_id,
            "user_id": token_data.get("user_id") if token_data else None,
        })

        existing.status = STATUS_REJECTED
        existing.updated_by = token_data.get("user_id") if token_data else None
        existing.updated_date_time = datetime.now()
        db.commit()

        return {
            "message": "Holiday rejected successfully",
            "holiday_id": holiday_id,
            "status": STATUS_REJECTED,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/holiday_edit/{holiday_id}")
def holiday_edit(
    holiday_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update a holiday."""
    try:
        values = _validate(parse_json_body(request))

        existing = db.query(HolidayMaster).filter(
            HolidayMaster.holiday_id == holiday_id,
        ).first()
        if not existing:
            raise HTTPException(status_code=404, detail="Holiday not found")
        if existing.status == STATUS_APPROVED:
            raise HTTPException(
                status_code=400,
                detail="Approved holidays cannot be edited",
            )

        _check_duplicate(db, values, holiday_id)

        for column, value in values.items():
            setattr(existing, column, value)
        existing.updated_by = token_data.get("user_id") if token_data else None
        existing.updated_date_time = datetime.now()
        db.commit()

        return {"message": "Holiday updated successfully", "holiday_id": holiday_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
