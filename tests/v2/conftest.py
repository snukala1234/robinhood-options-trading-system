"""V2 test fixtures — a REAL ephemeral PostgreSQL, never a mock or substitute engine.

"pytest green" must mean green against the production engine. The session fixture
starts a disposable Postgres in Docker (testcontainers), runs every Alembic migration,
and tears the container down afterwards. Setting ``TEST_DATABASE_URL`` (localhost
only) skips the container and uses a standing local test database instead — e.g. a
docker-compose test service. There is deliberately no fallback to any other engine.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]

# Ryuk (the testcontainers reaper) is flaky on some Windows Docker Desktop setups;
# containers are stopped explicitly by the fixture, so the reaper is redundant here.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    external = os.environ.get("TEST_DATABASE_URL", "").strip()
    if external:
        yield external
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def alembic_cfg(pg_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "persistence" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def migrated(pg_url: str, alembic_cfg: AlembicConfig) -> str:
    command.upgrade(alembic_cfg, "head")
    return pg_url


@pytest.fixture
def conn(migrated: str) -> Iterator[psycopg.Connection[Any]]:
    """A per-test connection; everything a test writes is rolled back at the end."""
    with psycopg.connect(migrated, row_factory=dict_row, autocommit=False) as c:
        yield c
        c.rollback()
