"""The dashboard's SELECT-only database access (structural read-only).

``ensure_readonly_role`` provisions a dedicated PostgreSQL role with SELECT
grants and nothing else, plus a role-level read-only transaction default as a
second lock. The dashboard connects ONLY as this role, so a coding mistake in
a route dies at the database with ``insufficient_privilege`` — the write
simply cannot happen. Connections are localhost-only per Section 17.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from src.config.environments import ConfigurationError

DASHBOARD_ROLE = "dashboard_ro"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def ensure_readonly_role(admin_conn: psycopg.Connection[DictRow], *, password: str) -> None:
    """Idempotently provision the SELECT-only dashboard role."""
    if not password:
        raise ConfigurationError("the dashboard role requires a non-empty password")
    admin_conn.execute(
        """DO $$ BEGIN
               IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
                   CREATE ROLE dashboard_ro LOGIN;
               END IF;
           END $$"""
    )
    admin_conn.execute(
        sql.SQL("ALTER ROLE dashboard_ro LOGIN PASSWORD {}").format(sql.Literal(password))
    )
    admin_conn.execute("GRANT USAGE ON SCHEMA public TO dashboard_ro")
    admin_conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_ro")
    admin_conn.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dashboard_ro"
    )
    # Explicit even though never granted: the role holds no write privilege.
    admin_conn.execute(
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM dashboard_ro"
    )
    admin_conn.execute("REVOKE CREATE ON SCHEMA public FROM dashboard_ro")
    # Second lock: even a table accidentally granted later is read-only here.
    admin_conn.execute("ALTER ROLE dashboard_ro SET default_transaction_read_only = on")


def readonly_url(base_url: str, *, password: str) -> str:
    """The base database URL rewritten to connect as the dashboard role."""
    parts = urlsplit(base_url)
    if parts.hostname not in _LOCAL_HOSTS:
        raise ConfigurationError(
            f"dashboard database host {parts.hostname!r} is not local (Section 17)"
        )
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{DASHBOARD_ROLE}:{password}@{parts.hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@contextmanager
def readonly_connection(url: str) -> Iterator[psycopg.Connection[DictRow]]:
    """One SELECT-only connection; read-only enforced again at session level."""
    host = urlsplit(url).hostname
    if host not in _LOCAL_HOSTS:
        raise ConfigurationError(f"dashboard database host {host!r} is not local (Section 17)")
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = on")
        yield conn
