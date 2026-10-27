"""Calendar arithmetic shared across the Jute Mechanical & Maintenance module.

``add_months`` lives here rather than in each router because the machine-attribute
master and the overhaul register BOTH compute ``next_overhaul_due`` for the same
machine. Two copies that drift would show two different due dates for one machine
depending on which screen wrote it last, and the disagreement would surface as a
machine appearing on (or vanishing from) the maintenance-due report for no visible
reason.

dateutil is deliberately not used — it is not a dependency of this backend, and
month arithmetic with last-day clamping is a handful of lines.
"""

import calendar
from datetime import date, timedelta
from typing import Optional


def add_months(start: date, months: int) -> date:
    """Add whole months, clamping to the last valid day of the target month.

    31 Jan + 1 month is 28 Feb, or 29 Feb in a leap year; 31 Mar + 1 month is
    30 Apr. Clamping (rather than rolling into the next month) is what a
    maintenance engineer means by "every month" — an overhaul due at the end of
    January is due at the end of February, not on 2 or 3 March.
    """
    total = (start.year * 12 + start.month - 1) + int(months)
    year, month = divmod(total, 12)
    month += 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_overhaul_due(
    last_overhaul_date: Optional[date],
    overhaul_freq_months: Optional[int],
) -> Optional[date]:
    """last_overhaul_date + overhaul_freq_months, or None if either is missing.

    None is correct rather than a guess: a machine with no configured interval
    genuinely has no due date, and the due report shows it as "interval not
    configured" instead of inventing a deadline.
    """
    if last_overhaul_date is None or not overhaul_freq_months:
        return None
    return add_months(last_overhaul_date, int(overhaul_freq_months))


def next_pm_due(
    last_pm_date: Optional[date],
    pm_freq_days: Optional[int],
) -> Optional[date]:
    """last_pm_date + pm_freq_days, or None if either is missing."""
    if last_pm_date is None or not pm_freq_days:
        return None
    return last_pm_date + timedelta(days=int(pm_freq_days))
