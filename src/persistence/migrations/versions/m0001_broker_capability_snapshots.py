"""create broker_capability_snapshots (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE broker_capability_snapshots (
            id UUID PRIMARY KEY,
            observed_at TIMESTAMPTZ NOT NULL,
            account_id_hash TEXT NOT NULL,
            capabilities JSONB NOT NULL,
            source_version TEXT,
            is_current BOOLEAN NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE broker_capability_snapshots")
