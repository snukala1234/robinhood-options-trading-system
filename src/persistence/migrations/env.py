"""Alembic environment — PostgreSQL only, URL from DATABASE_URL (never committed)."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool


def _url() -> str:
    url = context.config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "No database URL: set DATABASE_URL (see .env.example) or pass "
            "sqlalchemy.url programmatically."
        )
    # psycopg3 dialect; SQLAlchemy would otherwise default postgresql:// to psycopg2.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if not url.startswith("postgresql+"):
        raise RuntimeError("PostgreSQL is the only supported engine (locked decision)")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
