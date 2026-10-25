"""Tests for ISO week arithmetic, including the 53-week year 2026."""

import pytest

from transfer_engine.weeks import previous_week, same_week_last_year, shift_week, weeks_in_year


def test_weeks_in_year() -> None:
    assert weeks_in_year(2025) == 52
    assert weeks_in_year(2026) == 53


@pytest.mark.parametrize(
    ("week", "expected"),
    [((2026, 41), (2026, 40)), ((2026, 1), (2025, 52)), ((2027, 1), (2026, 53))],
)
def test_previous_week(week: tuple[int, int], expected: tuple[int, int]) -> None:
    assert previous_week(*week) == expected


def test_shift_week_crosses_week_53() -> None:
    assert shift_week(2026, 52, 2) == (2027, 1)
    assert shift_week(2027, 2, -3) == (2026, 52)


def test_same_week_last_year() -> None:
    assert same_week_last_year(2026, 41) == (2025, 41)
    assert same_week_last_year(2026, 53) is None  # 2025 had no week 53
