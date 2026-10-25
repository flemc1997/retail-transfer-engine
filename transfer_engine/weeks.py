"""Week arithmetic on the ISO calendar, which the Power BI export uses ('Week 41, 2026').

ISO years have 52 or 53 weeks (2026 has 53), so weeks can't be counted as `yr * 52 + wk`.
These helpers go through real dates instead.
"""

from __future__ import annotations

from datetime import date, timedelta


def weeks_in_year(yr: int) -> int:
    """52 or 53. 28 December is always in the last ISO week of its year."""
    return date(yr, 12, 28).isocalendar().week


def shift_week(yr: int, wk: int, n: int) -> tuple[int, int]:
    """The week `n` weeks after (or, with a negative `n`, before) the given week."""
    monday = date.fromisocalendar(yr, wk, 1) + timedelta(weeks=n)
    iso = monday.isocalendar()
    return iso.year, iso.week


def previous_week(yr: int, wk: int) -> tuple[int, int]:
    """The week before, e.g. week 1 of 2027 -> week 53 of 2026."""
    return shift_week(yr, wk, -1)


def same_week_last_year(yr: int, wk: int) -> tuple[int, int] | None:
    """The same week number a year earlier, or None when last year had no such week (week 53)."""
    if wk > weeks_in_year(yr - 1):
        return None
    return yr - 1, wk
