"""Minimal Shanghai Stock Exchange trading-day calendar.

The calendar contains the official SSE holiday closures for 2025 and 2026.
For every other year it deliberately falls back to a Monday-to-Friday rule;
call :func:`calendar_mode` (or :func:`calendar_status`) when the distinction
matters to the caller.

Only the Python standard library is used.  Dates returned by this module are
``datetime.date`` instances, and backward-looking lists are in reverse
chronological order (newest first).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Union


DateLike = Union[date, datetime, str]

CALENDAR_MODE_OFFICIAL = "official_sse"
CALENDAR_MODE_WEEKDAY_FALLBACK = "weekday_fallback"
OFFICIAL_YEARS = frozenset({2025, 2026})


# Inclusive ranges from the SSE's annual holiday-closure notices.  Weekend
# dates are retained here so the data mirrors the notices; weekends are also
# rejected independently by ``is_trading_day``.
_OFFICIAL_HOLIDAY_RANGES = {
    2025: (
        ("2025-01-01", "2025-01-01"),
        ("2025-01-28", "2025-02-04"),
        ("2025-04-04", "2025-04-06"),
        ("2025-05-01", "2025-05-05"),
        ("2025-05-31", "2025-06-02"),
        # Mid-Autumn Festival and National Day were combined in 2025.
        ("2025-10-01", "2025-10-08"),
    ),
    2026: (
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ),
}


def _coerce_date(value: DateLike) -> date:
    """Convert an ISO date, date, or datetime to ``datetime.date``."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid ISO calendar date: {value!r}") from exc
    raise TypeError("calendar date must be a date, datetime, or YYYY-MM-DD string")


def _expand_holiday_ranges() -> frozenset[date]:
    holidays: set[date] = set()
    for ranges in _OFFICIAL_HOLIDAY_RANGES.values():
        for start_text, end_text in ranges:
            current = date.fromisoformat(start_text)
            end = date.fromisoformat(end_text)
            while current <= end:
                holidays.add(current)
                current += timedelta(days=1)
    return frozenset(holidays)


OFFICIAL_HOLIDAYS = _expand_holiday_ranges()


def calendar_mode(value: DateLike) -> str:
    """Return the data mode used for ``value``'s year.

    ``official_sse`` means that the year's SSE holiday closures are encoded.
    ``weekday_fallback`` means that only Monday through Friday is known, so a
    future or historical public holiday may be classified as a trading day.
    """

    day = _coerce_date(value)
    if day.year in OFFICIAL_YEARS:
        return CALENDAR_MODE_OFFICIAL
    return CALENDAR_MODE_WEEKDAY_FALLBACK


def is_trading_day(value: DateLike) -> bool:
    """Return whether ``value`` is treated as an A-share trading day."""

    day = _coerce_date(value)
    if day.weekday() >= 5:
        return False
    if day.year in OFFICIAL_YEARS and day in OFFICIAL_HOLIDAYS:
        return False
    return True


def calendar_status(value: DateLike) -> dict[str, object]:
    """Return a JSON-friendly result including the calendar-mode marker."""

    day = _coerce_date(value)
    return {
        "date": day.isoformat(),
        "isTradingDay": is_trading_day(day),
        "calendarMode": calendar_mode(day),
    }


def latest_trading_day(value: DateLike) -> date:
    """Return ``value`` if open, otherwise the closest earlier trading day."""

    current = _coerce_date(value)
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def previous_trading_days(
    value: DateLike,
    count: int,
    include_current: bool = True,
) -> list[date]:
    """Return the latest ``count`` trading days at or before ``value``.

    Results are newest first.  When ``include_current`` is false, the calendar
    date supplied in ``value`` is skipped even if it is itself a trading day.
    A weekend or holiday input is naturally skipped in either mode.
    """

    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("count must be an integer")
    if count < 0:
        raise ValueError("count must be non-negative")
    if not isinstance(include_current, bool):
        raise TypeError("include_current must be a boolean")
    if count == 0:
        return []

    current = _coerce_date(value)
    if not include_current:
        current -= timedelta(days=1)

    result: list[date] = []
    while len(result) < count:
        if is_trading_day(current):
            result.append(current)
        current -= timedelta(days=1)
    return result


def trading_day_gap(start: DateLike, end: DateLike) -> int:
    """Count trading-session transitions from ``start`` to ``end``.

    Friday to the following Monday is one trading-day gap, as is the last
    session before a holiday to the first session after it.  The result is
    zero when ``end`` is not later than ``start``.
    """

    first = _coerce_date(start)
    last = _coerce_date(end)
    if last <= first:
        return 0
    current = first + timedelta(days=1)
    count = 0
    while current <= last:
        if is_trading_day(current):
            count += 1
        current += timedelta(days=1)
    return count


__all__ = [
    "CALENDAR_MODE_OFFICIAL",
    "CALENDAR_MODE_WEEKDAY_FALLBACK",
    "OFFICIAL_HOLIDAYS",
    "OFFICIAL_YEARS",
    "calendar_mode",
    "calendar_status",
    "is_trading_day",
    "latest_trading_day",
    "previous_trading_days",
    "trading_day_gap",
]
