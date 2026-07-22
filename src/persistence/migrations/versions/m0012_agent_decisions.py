"""create agent_decisions (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_decisions (
            id UUID PRIMARY KEY,
            correlation_id UUID NOT NULL,
            agent_name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            model_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_snapshot_ids JSONB NOT NULL,
            output JSONB NOT NULL,
            validation_result JSONB NOT NULL,
            latency_ms INT,
            token_usage JSONB
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_decisions")
