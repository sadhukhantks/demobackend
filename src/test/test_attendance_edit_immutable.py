"""Self-check: the attendance edit UPDATE never writes the immutable columns.

Rule: attendance_mark and attendance_source are fixed when the record is
created. The form disables them, but a disabled input is not a guarantee — the
endpoint must not write them either, whatever the body says. This holds the SQL
to that. The spell is deliberately NOT immutable (see the spell_id guard below).

Run: python -m src.test.test_attendance_edit_immutable
"""
import re

from src.hrms.attendance import (
    ATTENDANCE_EDIT_IMMUTABLE_COLUMNS,
    get_daily_attendance_edit_query,
)


def _set_clause(sql: str) -> str:
    """The SET list only, whitespace-normalised — a column may legitimately
    appear in the WHERE, and the SQL pads its `=` into a column."""
    flat = re.sub(r"\s+", " ", sql.lower())
    start = flat.index(" set ")
    return flat[start:flat.index(" where ", start)]


def test_edit_does_not_write_immutable_columns():
    set_clause = _set_clause(str(get_daily_attendance_edit_query()))

    for column in ATTENDANCE_EDIT_IMMUTABLE_COLUMNS:
        assert f"{column} =" not in set_clause, (
            f"attendance edit must not update {column} — it is fixed at creation"
        )

    # Guard the guard: the assertion above is only worth anything if the SET
    # clause is actually being read and does contain the correctable fields.
    for column in ("attendance_type", "working_hours", "worked_department_id",
                   "spell_id"):
        assert column in set_clause, f"edit should still update {column}"


if __name__ == "__main__":
    test_edit_does_not_write_immutable_columns()
    print("OK - attendance edit leaves the immutable columns alone")
