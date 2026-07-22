"""create system_events (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE system_events (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            severity TEXT NOT NULL,
            component TEXT NOT NULL,
            event_type TEXT NOT NULL,
            correlation_id UUID,
            payload JSONB NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE system_events")
