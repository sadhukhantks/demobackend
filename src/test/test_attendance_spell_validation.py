"""Self-check for _check_spell_hours_cap spell_id validation + hours cap.

Rule: a submitted spell_id must exist in spell_mst (else 400), and the
date+spell net hours may not exceed the spell's working hours.
Run: python -m src.test.test_attendance_spell_validation
"""
from fastapi import HTTPException

from src.hrms.attendance import (
    STATUS_REJECTED,
    _DUP_ATTENDANCE_SQL,
    _check_spell_hours_cap,
)


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _DB:
    """Stub Session: db.execute(...) returns the queued rows in order."""
    def __init__(self, *rows):
        self._rows = list(rows)
        self.sql = []

    def execute(self, *args, **kwargs):
        self.sql.append(str(args[0]))
        return _Result(self._rows.pop(0))


def _raises(db, spell_id, working_hours):
    try:
        _check_spell_hours_cap(db, 1, "2026-07-14", spell_id, 8, working_hours, 0)
        return None
    except HTTPException as e:
        return e.detail


def test_spell_validation():
    # No spell_id / no date -> no check runs.
    assert _check_spell_hours_cap(_DB(), 1, "2026-07-14", None, 8, 8, 0) is None
    assert _check_spell_hours_cap(_DB(), 1, None, 5, 8, 8, 0) is None

    # spell_id not in spell_mst -> 400 Invalid spell.
    assert _raises(_DB(None), 999, 8) == "Invalid spell selected"

    # Valid spell, within cap -> passes.
    assert _raises(_DB(_Row(working_hours=8), _Row(worked=0)), 5, 8) is None

    # Valid spell, cap exceeded (4 already saved + 6 new > 8) -> 400.
    assert "more than the" in _raises(_DB(_Row(working_hours=8), _Row(worked=4)), 5, 6)


def test_rejected_rows_ignored():
    """Rejected rows are dead — neither the hours cap nor the duplicate guard
    may count them, so a re-entry after a rejection is always possible."""
    ignore = f"<> {STATUS_REJECTED}"
    db = _DB(_Row(working_hours=8), _Row(worked=0))
    _check_spell_hours_cap(db, 1, "2026-07-14", 5, 8, 8, 0)
    assert ignore in db.sql[-1], db.sql[-1]
    assert ignore in _DUP_ATTENDANCE_SQL, _DUP_ATTENDANCE_SQL


if __name__ == "__main__":
    test_spell_validation()
    test_rejected_rows_ignored()
    print("all spell-validation checks passed")
