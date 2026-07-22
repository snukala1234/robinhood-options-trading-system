"""Immutable strategy config versions: append-only rows, human-gated promotion."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import errors

from src.config.tunables import DEFAULT_TUNABLES
from src.persistence.repositories import ConfigVersionError, ConfigVersionRepository

PARAMS = {"tunables": DEFAULT_TUNABLES.to_dict()}


def _repo(conn: psycopg.Connection[Any]) -> ConfigVersionRepository:
    return ConfigVersionRepository(conn)


def test_insert_and_get_roundtrip(conn: psycopg.Connection[Any]) -> None:
    repo = _repo(conn)
    vid = repo.insert_version(PARAMS, proposed_by="performance_auditor")
    row = repo.get(vid)
    assert row is not None
    assert row["status"] == "draft"
    assert row["parameters"] == PARAMS
    assert row["approved_by"] is None


def test_parameters_update_rejected_by_trigger(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo(conn)
    vid = repo.insert_version(PARAMS)
    with pytest.raises(errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute(
            "UPDATE strategy_config_versions SET parameters = '{}'::jsonb WHERE id = %s",
            (vid,),
        )
    # The row survives untouched after the rejected write.
    row = repo.get(vid)
    assert row is not None and row["parameters"] == PARAMS


def test_delete_rejected_by_trigger(conn: psycopg.Connection[Any]) -> None:
    repo = _repo(conn)
    vid = repo.insert_version(PARAMS)
    with pytest.raises(errors.RaiseException, match="cannot be deleted"), conn.transaction():
        conn.execute("DELETE FROM strategy_config_versions WHERE id = %s", (vid,))
    assert repo.get(vid) is not None


def test_promotion_to_active_requires_human_approver(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo(conn)
    vid = repo.insert_version(PARAMS)
    repo.transition(vid, "shadow")
    with pytest.raises(ConfigVersionError, match="human approver"):
        repo.transition(vid, "active")
    repo.transition(vid, "active", approved_by="santosh")
    row = repo.active_version()
    assert row is not None and row["id"] == vid
    assert row["approved_by"] == "santosh"
    assert row["approved_at"] is not None


def test_illegal_lifecycle_transitions_rejected(
    conn: psycopg.Connection[Any],
) -> None:
    repo = _repo(conn)
    vid = repo.insert_version(PARAMS)
    with pytest.raises(ConfigVersionError, match="illegal transition"):
        repo.transition(vid, "active", approved_by="santosh")  # draft -> active
    with pytest.raises(ConfigVersionError, match="illegal transition"):
        repo.transition(vid, "rolled_back")  # draft -> rolled_back
    repo.transition(vid, "rejected")
    with pytest.raises(ConfigVersionError, match="illegal transition"):
        repo.transition(vid, "shadow")  # rejected is final


def test_new_version_cannot_start_active(
    conn: psycopg.Connection[Any],
) -> None:
    with pytest.raises(ConfigVersionError, match="draft or shadow"):
        _repo(conn).insert_version(PARAMS, status="active")
