"""Typed repositories over the Section 14 schema.

Phase B ships the repository the foundations need — immutable strategy config
versions. Later phases add repositories per table as their services land. Methods do
not commit; the caller owns the transaction so multi-table writes stay atomic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from src.config import risk_policy
from src.persistence.db import Connection

#: Legal status transitions for a config version (Section 13.4 promotion process).
_STATUS_TRANSITIONS = frozenset(
    {
        ("draft", "shadow"),
        ("draft", "rejected"),
        ("shadow", "active"),
        ("shadow", "rejected"),
        ("active", "rolled_back"),
    }
)


class ConfigVersionError(RuntimeError):
    """Illegal config-version operation (bad transition, missing human approval)."""


@dataclass(frozen=True)
class ConfigVersionRepository:
    """Append-only strategy config versions. ``parameters`` is immutable.

    Immutability is enforced twice: this repository exposes no way to change
    ``parameters``, and a database trigger (migration 0004) rejects UPDATEs of
    ``parameters``/``created_at`` and all DELETEs, so even raw SQL cannot rewrite
    history.
    """

    conn: Connection

    def insert_version(
        self,
        parameters: dict[str, Any],
        *,
        status: str = "draft",
        proposed_by: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        if status not in {"draft", "shadow"}:
            raise ConfigVersionError(f"a new version starts as draft or shadow, not {status!r}")
        version_id = uuid.uuid4()
        self.conn.execute(
            """INSERT INTO strategy_config_versions
               (id, created_at, parameters, status, proposed_by, evidence,
                approved_by, approved_at)
               VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL)""",
            (
                version_id,
                datetime.now(UTC),
                Jsonb(parameters),
                status,
                proposed_by,
                Jsonb(evidence) if evidence is not None else None,
            ),
        )
        return version_id

    def get(self, version_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM strategy_config_versions WHERE id = %s", (version_id,)
        )
        return cur.fetchone()

    def active_version(self) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM strategy_config_versions WHERE status = 'active' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        return cur.fetchone()

    def transition(
        self, version_id: uuid.UUID, new_status: str, *, approved_by: str | None = None
    ) -> None:
        """Move a version through the Section 13.4 lifecycle.

        Promotion to ``active`` requires a named human approver
        (``REQUIRE_HUMAN_APPROVAL_FOR_CONFIG_PROMOTION`` is a hard guardrail).
        """
        row = self.get(version_id)
        if row is None:
            raise ConfigVersionError(f"unknown config version {version_id}")
        current = str(row["status"])
        if (current, new_status) not in _STATUS_TRANSITIONS:
            raise ConfigVersionError(f"illegal transition {current!r} -> {new_status!r}")
        if new_status == "active":
            if risk_policy.REQUIRE_HUMAN_APPROVAL_FOR_CONFIG_PROMOTION and not approved_by:
                raise ConfigVersionError("promotion to active requires a human approver")
            self.conn.execute(
                "UPDATE strategy_config_versions SET status = %s, approved_by = %s, "
                "approved_at = %s WHERE id = %s",
                (new_status, approved_by, datetime.now(UTC), version_id),
            )
        else:
            self.conn.execute(
                "UPDATE strategy_config_versions SET status = %s WHERE id = %s",
                (new_status, version_id),
            )
