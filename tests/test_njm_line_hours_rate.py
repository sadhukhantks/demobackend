"""Line Hours Statement rate (src/hrms/njmWagesData.line_hours_rate) — the
legacy njmlinehrschecklist formula, including its lost_hours > 0 guard."""
from decimal import Decimal

from src.hrms.njmWagesData import line_hours_rate


def test_line_hours_rate_matches_legacy():
    # 1000 / (((100 - 10) * 4) / 2) = 1000 / 180
    assert line_hours_rate(1000, 100, 10, 4, "H") == 5.555556
    # sardar: rounded rate * 1.3, rounded again
    assert line_hours_rate(1000, 100, 10, 4, "S") == 7.222223
    # Decimal columns straight from the DB
    assert line_hours_rate(Decimal("1000"), Decimal("100"), Decimal("10"), 4, "H") == 5.555556
    # rows lifted from the legacy linehours_2026-07-01_2026-07-31.pdf
    assert line_hours_rate(133.41, 151.01, 149.60, 30, "H") == 6.307801
    assert line_hours_rate(133.41, 151.01, 149.60, 30, "S") == 8.200141
    assert line_hours_rate(39.26, 133.55, 151.47, 30, "H") == -0.146057   # lost > running goes negative
    assert line_hours_rate(6097.64, 133.55, 72.46, 30, "H") == 6.65427
    # legacy guards: no amount, or no lost hours, print 0
    assert line_hours_rate(0, 100, 10, 4, "H") == 0
    assert line_hours_rate(1000, 100, 0, 4, "S") == 0
    assert line_hours_rate(None, None, None, None, None) == 0
