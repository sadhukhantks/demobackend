from src.hrms.ejmWagesData import _parse_ot_advance_rows


def test_parse_ot_advance_rows():
    rows = [("EB No", "Amount"), (1234.0, 500), ("T12", "x"), (None, None),
            ("1234", 10), (None, 5), ("A1", 0)]
    parsed, errors = _parse_ot_advance_rows(rows)
    assert parsed == [("1234", 500.0), ("A1", 0.0)]
    assert len(errors) == 3 and "repeated" in errors[1] and "missing" in errors[2]
