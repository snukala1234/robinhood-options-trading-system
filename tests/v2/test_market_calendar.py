"""Market calendar: 2026 holidays, session phases, no guessing beyond coverage."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src.domain.values import DomainValidationError
from src.orchestration.calendar import (
    close_time_et,
    is_trading_day,
    session_phase,
    session_times,
)


def test_regular_weekday_is_a_trading_day() -> None:
    assert is_trading_day(date(2026, 7, 22))  # Wednesday


def test_weekends_are_closed() -> None:
    assert not is_trading_day(date(2026, 7, 25))  # Saturday
    assert not is_trading_day(date(2026, 7, 26))  # Sunday


@pytest.mark.parametrize(
    "holiday",
    [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 4, 3),
        date(2026, 7, 3),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ],
)
def test_2026_holidays_are_closed(holiday: date) -> None:
    assert not is_trading_day(holiday)


def test_session_phases_across_a_summer_day() -> None:
    # 2026-07-22 is EDT (UTC-4): premarket 11:00Z, open 13:30Z, close 20:00Z.
    assert session_phase(datetime(2026, 7, 22, 10, 0, tzinfo=UTC)) == "closed"
    assert session_phase(datetime(2026, 7, 22, 12, 0, tzinfo=UTC)) == "premarket"
    assert session_phase(datetime(2026, 7, 22, 13, 30, tzinfo=UTC)) == "open"
    assert session_phase(datetime(2026, 7, 22, 19, 59, tzinfo=UTC)) == "open"
    assert session_phase(datetime(2026, 7, 22, 20, 0, tzinfo=UTC)) == "postmarket"
    assert session_phase(datetime(2026, 7, 23, 1, 0, tzinfo=UTC)) == "closed"


def test_session_phase_on_a_weekend_is_closed_all_day() -> None:
    assert session_phase(datetime(2026, 7, 25, 15, 0, tzinfo=UTC)) == "closed"


def test_early_close_days() -> None:
    from datetime import time

    assert close_time_et(date(2026, 11, 27)) == time(13, 0)  # day after Thanksgiving
    assert close_time_et(date(2026, 12, 24)) == time(13, 0)
    assert close_time_et(date(2026, 7, 22)) == time(16, 0)
    # Nov 27 is EST (UTC-5): 13:00 ET close = 18:00Z, so 18:30Z is postmarket.
    assert session_phase(datetime(2026, 11, 27, 17, 30, tzinfo=UTC)) == "open"
    assert session_phase(datetime(2026, 11, 27, 18, 30, tzinfo=UTC)) == "postmarket"


def test_session_times_are_utc_and_dst_aware() -> None:
    times = session_times(date(2026, 7, 22))
    assert times.regular_open == datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    assert times.regular_close == datetime(2026, 7, 22, 20, 0, tzinfo=UTC)
    assert not times.early_close
    early = session_times(date(2026, 12, 24))
    assert early.early_close
    assert early.regular_close == datetime(2026, 12, 24, 18, 0, tzinfo=UTC)  # EST


def test_uncovered_year_raises_instead_of_guessing() -> None:
    with pytest.raises(DomainValidationError, match="does not cover 2031"):
        is_trading_day(date(2031, 1, 5))


def test_non_trading_day_has_no_session_times() -> None:
    with pytest.raises(DomainValidationError):
        session_times(date(2026, 7, 4))  # Saturday
    with pytest.raises(DomainValidationError):
        close_time_et(date(2026, 12, 25))  # holiday


def test_naive_datetime_rejected() -> None:
    with pytest.raises(DomainValidationError):
        session_phase(datetime(2026, 7, 22, 12, 0))
