"""create trade_proposals (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trade_proposals (
            id UUID PRIMARY KEY,
            candidate_id UUID REFERENCES opportunity_candidates(id),
            created_at TIMESTAMPTZ NOT NULL,
            proposal JSONB NOT NULL,
            portfolio_impact JSONB NOT NULL,
            risk_decision JSONB NOT NULL,
            approval_status TEXT NOT NULL,
            config_version_id UUID NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE trade_proposals")
