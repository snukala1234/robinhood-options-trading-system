"""PostgreSQL connection layer (psycopg3).

PostgreSQL is the only engine — prototype through live (docs/V1_TO_V2_TRACEABILITY.md
§4.1). psycopg3 adapts NUMERIC to/from :class:`decimal.Decimal` natively, so money
never passes through a float on the way to or from the database.

Schema management belongs exclusively to Alembic (``src/persistence/migrations``);
nothing in this module creates tables.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import psycopg
from psycopg.rows import DictRow, dict_row

from src.config import environments

Connection = psycopg.Connection[DictRow]


def _assert_local(url: str) -> str:
    host = urlsplit(url).hostname
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise environments.ConfigurationError(
            f"database host {host!r} is not local (Section 17: localhost only)"
        )
    return url


def connect(url: str | None = None) -> Connection:
    """Open a psycopg3 connection with dict rows and explicit transactions.

    ``url`` defaults to the environment-gated ``DATABASE_URL``; an explicitly passed
    URL (tests, ephemeral containers) is still held to the localhost rule.
    """
    conn_url = _assert_local(url) if url is not None else environments.database_url()
    return psycopg.connect(conn_url, row_factory=dict_row, autocommit=False)
