"""Leave entries for the Android app (table leave_transactions).

Follows the portal router src/hrms/leaveRequest.py, which matches the real
schema: leave types come from hrms_leave_types_mst (per company), the day count
is implied by from/to with `non_working_days` stored alongside, and
leave_transaction_id has no AUTO_INCREMENT (MAX+1). Difference: the app saves
straight as Approved (3) — its spreadsheet entry is the approval.
"""
import traceback
from datetime import datetime

from flask import Blueprint, request, jsonify
from src.hrms.leaveRequest import write_leave_days
from src.mobileapp.db import get_db

leave_bp = Blueprint('leave', __name__)

STATUS_APPROVED = 3
# Rejected (4) and cancelled (6) leaves do not block a new one on the same days.
INACTIVE_STATUSES = (4, 6)


def _error(message, code=400):
    return jsonify({'status': 'error', 'message': message}), code


# ── GET /leave-types?branch_id= ──────────────────────────────────────────────

@leave_bp.route('/leave-types', methods=['GET'])
def get_leave_types():
    """Active leave types of the branch's company."""
    try:
        branch_id = request.args.get('branch_id', type=int)
        db     = get_db()
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.leave_type_id          AS id,
                   m.leave_type_description AS leave_type_name
            FROM hrms_leave_types_mst m
            WHERE m.is_active = 1
              AND (%s IS NULL OR m.company_id =
                   (SELECT co_id FROM branch_mst WHERE branch_id = %s))
            ORDER BY m.leave_type_description
        """, (branch_id, branch_id))
        rows = cursor.fetchall()
        cursor.close()
        db.close()
        return jsonify({'status': 'success', 'leave_types': rows})
    except Exception as e:
        return _error(str(e), 500)


# ── GET /status-mst ───────────────────────────────────────────────────────────

@leave_bp.route('/status-mst', methods=['GET'])
def get_status_mst():
    """Return statuses from status_mst table."""
    try:
        db     = get_db()
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT status_id, status_name FROM status_mst ORDER BY status_id")
        rows = cursor.fetchall()
        cursor.close()
        db.close()
        return jsonify({'status': 'success', 'statuses': rows})
    except Exception as e:
        return _error(str(e), 500)


# ── GET /leave-transactions ───────────────────────────────────────────────────

@leave_bp.route('/leave-transactions', methods=['GET'])
def get_leave_transactions():
    """Leaves of a branch that overlap [from_date, to_date], newest first."""
    try:
        sql = """
            SELECT lt.leave_transaction_id              AS id,
                   lt.eb_id,
                   lt.leave_type_id,
                   ltp.leave_type_description           AS leave_type,
                   lt.leave_from_date                   AS from_date,
                   lt.leave_to_date                     AS to_date,
                   lt.non_working_days,
                   COALESCE(lt.remarks, lt.leave_purpose) AS remarks,   -- portal fills leave_purpose
                   CAST(lt.status AS UNSIGNED)          AS status_id,
                   sm.status_name                       AS status,
                   lt.branch_id,
                   COALESCE(o.emp_code, '')             AS emp_code,
                   TRIM(CONCAT(
                       COALESCE(p.first_name,''), ' ',
                       COALESCE(p.middle_name,''), ' ',
                       COALESCE(p.last_name,'')))       AS emp_name
            FROM leave_transactions lt
            LEFT JOIN hrms_ed_official_details o   ON o.eb_id = lt.eb_id AND o.active = 1
            LEFT JOIN hrms_ed_personal_details  p  ON p.eb_id = lt.eb_id
            LEFT JOIN hrms_leave_types_mst      ltp ON ltp.leave_type_id = lt.leave_type_id
            LEFT JOIN status_mst                sm  ON sm.status_id = CAST(lt.status AS UNSIGNED)
            WHERE 1=1
        """
        params = []
        branch_id = request.args.get('branch_id', type=int)
        from_date = request.args.get('from_date')
        to_date   = request.args.get('to_date')
        emp_code  = (request.args.get('emp_code') or '').strip() or None
        if branch_id:
            sql += " AND lt.branch_id = %s";        params.append(branch_id)
        if from_date:                               # overlap, not containment
            sql += " AND lt.leave_to_date >= %s";   params.append(from_date)
        if to_date:
            sql += " AND lt.leave_from_date <= %s"; params.append(to_date)
        if emp_code:
            sql += " AND o.emp_code = %s";          params.append(emp_code)
        sql += " ORDER BY lt.leave_from_date DESC, lt.leave_transaction_id DESC"

        db     = get_db()
        cursor = db.cursor(dictionary=True)
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        cursor.close()
        db.close()
        for r in rows:
            for col in ('from_date', 'to_date'):
                if r.get(col):
                    r[col] = r[col].strftime('%Y-%m-%d')
        return jsonify({'status': 'success', 'transactions': rows})
    except Exception as e:
        traceback.print_exc()
        return _error(str(e), 500)


# ── POST /leave-transactions ──────────────────────────────────────────────────

@leave_bp.route('/leave-transactions', methods=['POST'])
def save_leave_transaction():
    try:
        data          = request.json or {}
        branch_id     = data.get('branch_id')
        eb_id         = data.get('eb_id')
        leave_type_id = data.get('leave_type_id')
        from_date     = data.get('from_date')
        to_date       = data.get('to_date')
        user_id       = data.get('user_id')
        if not branch_id:
            return _error('Branch is required')
        if not eb_id:
            return _error('Employee is required')
        if not leave_type_id:
            return _error('Leave type is required')
        try:
            d1 = datetime.strptime(from_date or '', '%Y-%m-%d').date()
            d2 = datetime.strptime(to_date or '', '%Y-%m-%d').date()
        except ValueError:
            return _error('From and To dates are required (YYYY-MM-DD)')
        if d1 > d2:
            return _error('From Date cannot be after To Date')
        try:
            non_working_days = int(data.get('non_working_days') or 0)
        except (TypeError, ValueError):
            return _error('Non working days must be a number')
        if non_working_days < 0:
            return _error('Non working days cannot be negative')
        if (d2 - d1).days + 1 - non_working_days < 1:
            return _error('Leave days must be at least 1 (too many non working days)')
        remarks = (data.get('remarks') or '').strip()[:255] or None   # varchar(255)

        db     = get_db()
        cursor = db.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT 1 FROM hrms_ed_official_details
                WHERE eb_id = %s AND branch_id = %s AND active = 1 LIMIT 1
            """, (eb_id, branch_id))
            if not cursor.fetchone():
                return _error('Employee does not belong to the selected branch')

            cursor.execute("""
                SELECT leave_from_date, leave_to_date FROM leave_transactions
                WHERE eb_id = %s
                  AND CAST(status AS UNSIGNED) NOT IN (%s, %s)
                  AND leave_from_date <= %s AND leave_to_date >= %s
                LIMIT 1
            """, (eb_id, *INACTIVE_STATUSES, to_date, from_date))
            clash = cursor.fetchone()
            if clash:
                return _error('Employee already has leave from {} to {}'.format(
                    clash['leave_from_date'].strftime('%d-%m-%Y'),
                    clash['leave_to_date'].strftime('%d-%m-%Y')))

            # ponytail: MAX+1 like the portal (the column has no AUTO_INCREMENT);
            # two saves in the same instant can collide. Make the column
            # AUTO_INCREMENT if that ever shows up.
            cursor.execute("SELECT COALESCE(MAX(leave_transaction_id), 0) + 1 AS next_id FROM leave_transactions")
            new_id = cursor.fetchone()['next_id']
            cursor.execute("""
                INSERT INTO leave_transactions
                    (leave_transaction_id, branch_id, eb_id, leave_type_id,
                     leave_from_date, leave_to_date, non_working_days, remarks,
                     status, updated_by, updated_date_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (new_id, branch_id, eb_id, leave_type_id, from_date, to_date,
                  non_working_days, remarks,
                  str(STATUS_APPROVED), user_id, datetime.now()))
            write_leave_days(cursor, new_id, user_id)   # per-date rows payroll reads
            db.commit()
        finally:
            cursor.close()
            db.close()
        return jsonify({'status': 'success', 'message': 'Leave saved', 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return _error(str(e), 500)
