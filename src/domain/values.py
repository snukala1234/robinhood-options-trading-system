"""Validation helpers shared by all domain models.

Money is ``Decimal`` end-to-end (spec Phase C: "Use Decimal for money"). Floats are
rejected outright — never coerced — because a float has already lost precision by the
time it reaches us. Missing or invalid inputs raise instead of being guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal


class DomainValidationError(ValueError):
    """Invalid domain input. Always raised, never silently corrected."""


def require_money(name: str, value: object) -> Decimal:
    """Return ``value`` as Decimal; reject anything that is not already a Decimal."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DomainValidationError(f"{name} must be a finite Decimal, got {value}")
        return value
    raise DomainValidationError(
        f"{name} must be a Decimal (never float/int/str), got {type(value).__name__}"
    )


def require_positive_money(name: str, value: object) -> Decimal:
    dec = require_money(name, value)
    if dec <= 0:
        raise DomainValidationError(f"{name} must be > 0, got {dec}")
    return dec


def require_non_negative_money(name: str, value: object) -> Decimal:
    dec = require_money(name, value)
    if dec < 0:
        raise DomainValidationError(f"{name} must be >= 0, got {dec}")
    return dec


def require_optional_money(name: str, value: object) -> Decimal | None:
    return None if value is None else require_money(name, value)


def require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise DomainValidationError(f"{name} must be > 0, got {value}")
    return value


def require_utc(name: str, value: object) -> datetime:
    """Timestamps must be timezone-aware UTC (stored as TIMESTAMPTZ)."""
    if not isinstance(value, datetime):
        raise DomainValidationError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{name} must be timezone-aware, got naive {value!r}")
    return value.astimezone(UTC)


def require_symbol(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        raise DomainValidationError(f"{name} must be a non-empty ASCII string")
    symbol = value.strip().upper()
    if not symbol.replace(".", "").replace("-", "").isalnum():
        raise DomainValidationError(f"{name} contains invalid characters: {value!r}")
    return symbol
