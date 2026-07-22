"""create position_snapshots (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE position_snapshots (
            id UUID PRIMARY KEY,
            position_id UUID REFERENCES positions(id),
            observed_at TIMESTAMPTZ NOT NULL,
            marked_value NUMERIC,
            unrealized_pnl NUMERIC,
            net_delta NUMERIC,
            net_gamma NUMERIC,
            net_theta NUMERIC,
            net_vega NUMERIC,
            dte INT,
            liquidity JSONB,
            thesis_state JSONB
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE position_snapshots")
