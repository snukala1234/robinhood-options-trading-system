"""Shared pytest fixtures. Forces offline market data so the suite is hermetic."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Every test runs against the deterministic offline market-data fixture.
os.environ.setdefault("TRADING_MARKET_DATA", "offline")

from core.db import Database  # noqa: E402  (import after env is set)


@pytest.fixture()
def db() -> Iterator[Database]:
    """An ephemeral in-memory database with the full schema created."""
    database = Database.connect(":memory:")
    yield database
    database.close()
