"""create opportunity_candidates (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE opportunity_candidates (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            underlying TEXT NOT NULL,
            strategy TEXT NOT NULL,
            expiration DATE NOT NULL,
            legs JSONB NOT NULL,
            analytics JSONB NOT NULL,
            score_components JSONB NOT NULL,
            total_score NUMERIC NOT NULL,
            status TEXT NOT NULL,
            rejection_reasons JSONB,
            config_version_id UUID NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE opportunity_candidates")
