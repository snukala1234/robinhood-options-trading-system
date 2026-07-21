"""Small shared helpers: UUIDs and UTC-aware timestamps.

Timestamps are stored as ISO-8601 UTC strings throughout (SQLite has no native
TIMESTAMPTZ). All producers use :func:`utcnow`; all parsers use :func:`parse_iso`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def new_uuid() -> str:
    """Return a fresh random UUID4 as a string (primary keys in Section 4 tables)."""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Serialize a datetime to an ISO-8601 UTC string.

    Naive datetimes are assumed to already be UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return to_iso(utcnow())


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
