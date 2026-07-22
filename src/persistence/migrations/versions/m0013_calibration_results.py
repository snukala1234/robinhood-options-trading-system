"""create calibration_results (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE calibration_results (
            id UUID PRIMARY KEY,
            dimension_key JSONB NOT NULL,
            window_start TIMESTAMPTZ NOT NULL,
            window_end TIMESTAMPTZ NOT NULL,
            sample_size INT NOT NULL,
            metrics JSONB NOT NULL,
            proposed_action JSONB
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE calibration_results")
