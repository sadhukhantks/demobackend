"""Canteen entries for the Android app (table canteen_details).

Mirrors the portal router src/hrms/canteenDetails.py, except rows are created
as Approved (3) -- the app saves straight from its spreadsheet entry, no approval
step. Only a draft can be edited or deleted (delete = soft cancel to 6), one row
per employee per date, and the meal rate is fixed (not input).
"""
import traceback
from datetime import datetime

from flask import Blueprint, jsonify, request

from src.hrms.canteenDetails import (
    DEFAULT_RATE_OF_MEALS, STATUS_APPROVED, STATUS_CANCELLED, STATUS_DRAFT,
)
from src.mobileapp.db import get_db

canteen_bp = Blueprint('canteen', __name__)


def _error(message, code=400):
    return jsonify({'status': 'error', 'message': message}), code


# ── GET /canteen-entries ──────────────────────────────────────────────────────

@canteen_bp.route('/canteen-entries', methods=['GET'])
def get_canteen_entries():
    try:
        sql = """
            SELECT c.tran_id                                   AS id,
                   c.eb_id,
                   c.tran_date,
                   c.no_of_meals,
                   c.rate_of_meals,
                   IFNULL(c.no_of_meals, 0) * IFNULL(c.rate_of_meals, 0) AS amount,
                   c.status_id,
                   sm.status_name                              AS status,
                   c.branch_id,
                   COALESCE(o.emp_code, '')                    AS emp_code,
                   TRIM(CONCAT(COALESCE(p.first_name, ''), ' ',
                               COALESCE(p.middle_name, ''), ' ',
                               COALESCE(p.last_name, '')))     AS emp_name
            FROM canteen_details c
            LEFT JOIN hrms_ed_official_details o ON o.eb_id = c.eb_id AND o.active = 1
            LEFT JOIN hrms_ed_personal_details p ON p.eb_id = c.eb_id
            LEFT JOIN status_mst              sm ON sm.status_id = c.status_id
            WHERE c.status_id <> %s
        """
        params = [STATUS_CANCELLED]
        branch_id = request.args.get('branch_id', type=int)
        from_date = request.args.get('from_date')
        to_date   = request.args.get('to_date')
        emp_code  = (request.args.get('emp_code') or '').strip() or None
        if branch_id:
            sql += " AND c.branch_id = %s";  params.append(branch_id)
        if from_date:
            sql += " AND c.tran_date >= %s"; params.append(from_date)
        if to_date:
            sql += " AND c.tran_date <= %s"; params.append(to_date)
        if emp_code:
            sql += " AND o.emp_code = %s";   params.append(emp_code)
        sql += " ORDER BY c.tran_date DESC, c.tran_id DESC"

        db = get_db()
        cursor = db.cursor(dictionary=True)
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        cursor.close()
        db.close()
        for r in rows:
            if r.get('tran_date'):
                r['tran_date'] = r['tran_date'].strftime('%Y-%m-%d')
        return jsonify({'status': 'success', 'entries': rows})
    except Exception as e:
        traceback.print_exc()
        return _error(str(e), 500)


# ── POST /canteen-entries  (create, or edit when "id" is sent) ───────────────

@canteen_bp.route('/canteen-entries', methods=['POST'])
def save_canteen_entry():
    try:
        data      = request.json or {}
        tran_id   = data.get('id')
        eb_id     = data.get('eb_id')
        branch_id = data.get('branch_id')
        tran_date = data.get('tran_date')
        user_id   = data.get('user_id')
        if not branch_id:
            return _error('Branch is required')
        if not eb_id:
            return _error('Employee is required')
        try:
            datetime.strptime(tran_date or '', '%Y-%m-%d')
        except ValueError:
            return _error('Date is required (YYYY-MM-DD)')
        try:
            meals = int(data.get('no_of_meals'))
        except (TypeError, ValueError):
            return _error('No. of meals must be a number')
        if meals <= 0:
            return _error('No. of meals must be greater than 0')

        db = get_db()
        cursor = db.cursor(dictionary=True)
        try:
            if tran_id:
                cursor.execute("SELECT status_id FROM canteen_details WHERE tran_id = %s", (tran_id,))
                row = cursor.fetchone()
                if not row:
                    return _error('Canteen entry not found', 404)
                if row['status_id'] != STATUS_DRAFT:
                    return _error('Only draft canteen entries can be edited')

            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM canteen_details
                WHERE status_id <> %s AND eb_id = %s AND tran_date = %s
                  AND (%s IS NULL OR tran_id <> %s)
            """, (STATUS_CANCELLED, eb_id, tran_date, tran_id, tran_id))
            if cursor.fetchone()['cnt'] > 0:
                return _error('A canteen entry already exists for this employee and date')

            now = datetime.now()
            if tran_id:
                # rate_of_meals is fixed and never changed on edit.
                cursor.execute("""
                    UPDATE canteen_details
                    SET branch_id = %s, eb_id = %s, tran_date = %s, no_of_meals = %s,
                        updated_by = %s, updated_date_time = %s
                    WHERE tran_id = %s
                """, (branch_id, eb_id, tran_date, meals, user_id, now, tran_id))
            else:
                cursor.execute("""
                    INSERT INTO canteen_details
                        (branch_id, eb_id, tran_date, no_of_meals, rate_of_meals,
                         status_id, updated_by, updated_date_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (branch_id, eb_id, tran_date, meals, DEFAULT_RATE_OF_MEALS,
                      STATUS_APPROVED, user_id, now))
                tran_id = cursor.lastrowid
            db.commit()
        finally:
            cursor.close()
            db.close()
        return jsonify({'status': 'success', 'message': 'Canteen entry saved', 'id': tran_id})
    except Exception as e:
        traceback.print_exc()
        return _error(str(e), 500)


# ── DELETE /canteen-entries/<id>  (soft cancel, drafts only) ─────────────────

@canteen_bp.route('/canteen-entries/<int:tran_id>', methods=['DELETE'])
def delete_canteen_entry(tran_id):
    try:
        db = get_db()
        cursor = db.cursor(dictionary=True)
        try:
            cursor.execute("SELECT status_id FROM canteen_details WHERE tran_id = %s", (tran_id,))
            row = cursor.fetchone()
            if not row:
                return _error('Canteen entry not found', 404)
            if row['status_id'] != STATUS_DRAFT:
                return _error('Only draft canteen entries can be deleted')
            cursor.execute("UPDATE canteen_details SET status_id = %s WHERE tran_id = %s",
                           (STATUS_CANCELLED, tran_id))
            db.commit()
        finally:
            cursor.close()
            db.close()
        return jsonify({'status': 'success', 'message': 'Canteen entry deleted'})
    except Exception as e:
        traceback.print_exc()
        return _error(str(e), 500)
