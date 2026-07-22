"""create positions (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE positions (
            id UUID PRIMARY KEY,
            proposal_id UUID REFERENCES trade_proposals(id),
            underlying TEXT NOT NULL,
            strategy TEXT NOT NULL,
            expiration DATE NOT NULL,
            legs JSONB NOT NULL,
            opened_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            entry_net_price NUMERIC NOT NULL,
            exit_net_price NUMERIC,
            quantity INT NOT NULL,
            max_loss NUMERIC NOT NULL,
            status TEXT NOT NULL,
            exit_plan JSONB NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE positions")
