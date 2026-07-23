"""US equity-options market calendar (deterministic, no network).

Covers the NYSE/CBOE 2026 schedule this build runs against: full holidays,
early closes, and the regular session clock in ``America/New_York`` (all
public functions take and return aware UTC datetimes). Asking about a year
the table does not cover raises — the calendar never guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from src.domain.values import DomainValidationError, require_utc

ET = ZoneInfo("America/New_York")

#: NYSE full-close holidays, per covered year.
MARKET_HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset(
        {
            date(2026, 1, 1),  # New Year's Day
            date(2026, 1, 19),  # Martin Luther King Jr. Day
            date(2026, 2, 16),  # Washington's Birthday
            date(2026, 4, 3),  # Good Friday
            date(2026, 5, 25),  # Memorial Day
            date(2026, 6, 19),  # Juneteenth
            date(2026, 7, 3),  # Independence Day (observed)
            date(2026, 9, 7),  # Labor Day
            date(2026, 11, 26),  # Thanksgiving Day
            date(2026, 12, 25),  # Christmas Day
        }
    ),
}

#: 13:00 ET early closes, per covered year.
EARLY_CLOSES: dict[int, frozenset[date]] = {
    2026: frozenset({date(2026, 11, 27), date(2026, 12, 24)}),
}

_PREMARKET_START = time(7, 0)
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_EARLY_CLOSE = time(13, 0)
_POSTMARKET_END = time(20, 0)

SessionPhase = Literal["closed", "premarket", "open", "postmarket"]


def _holidays_for(year: int) -> frozenset[date]:
    try:
        return MARKET_HOLIDAYS[year]
    except KeyError:
        raise DomainValidationError(
            f"market calendar does not cover {year}; extend MARKET_HOLIDAYS before trading"
        ) from None


def is_trading_day(day: date) -> bool:
    """Weekday and not a full-close holiday."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise DomainValidationError("day must be a date")
    holidays = _holidays_for(day.year)  # coverage check first, even for weekends
    return day.weekday() < 5 and day not in holidays


def close_time_et(day: date) -> time:
    """Regular-session close for a trading day (13:00 ET on early closes)."""
    if not is_trading_day(day):
        raise DomainValidationError(f"{day.isoformat()} is not a trading day")
    return _EARLY_CLOSE if day in EARLY_CLOSES.get(day.year, frozenset()) else _REGULAR_CLOSE


def session_phase(now: datetime) -> SessionPhase:
    """Where in the trading day an aware UTC instant falls."""
    now = require_utc("now", now)
    local = now.astimezone(ET)
    day = local.date()
    if not is_trading_day(day):
        return "closed"
    t = local.time()
    close = close_time_et(day)
    if t < _PREMARKET_START:
        return "closed"
    if t < _REGULAR_OPEN:
        return "premarket"
    if t < close:
        return "open"
    if t < _POSTMARKET_END:
        return "postmarket"
    return "closed"


@dataclass(frozen=True)
class SessionTimes:
    """The UTC session boundaries for one trading day."""

    day: date
    premarket_start: datetime
    regular_open: datetime
    regular_close: datetime

    @property
    def early_close(self) -> bool:
        return self.regular_close.astimezone(ET).time() == _EARLY_CLOSE


def session_times(day: date) -> SessionTimes:
    """UTC boundaries for a trading day; raises on non-trading days."""
    close = close_time_et(day)  # validates trading day
    return SessionTimes(
        day=day,
        premarket_start=datetime.combine(day, _PREMARKET_START, ET).astimezone(UTC),
        regular_open=datetime.combine(day, _REGULAR_OPEN, ET).astimezone(UTC),
        regular_close=datetime.combine(day, close, ET).astimezone(UTC),
    )
