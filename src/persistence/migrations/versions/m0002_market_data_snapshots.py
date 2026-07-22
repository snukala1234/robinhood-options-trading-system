"""create market_data_snapshots (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE market_data_snapshots (
            id UUID PRIMARY KEY,
            symbol TEXT NOT NULL,
            instrument_type TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL,
            source TEXT NOT NULL,
            payload JSONB NOT NULL,
            quality_flags JSONB NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE market_data_snapshots")
