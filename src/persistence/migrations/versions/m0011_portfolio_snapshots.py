"""create portfolio_snapshots (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE portfolio_snapshots (
            id UUID PRIMARY KEY,
            observed_at TIMESTAMPTZ NOT NULL,
            total_equity NUMERIC NOT NULL,
            settled_cash NUMERIC NOT NULL,
            unsettled_cash NUMERIC NOT NULL,
            open_risk NUMERIC NOT NULL,
            net_delta NUMERIC,
            net_gamma NUMERIC,
            daily_theta NUMERIC,
            net_vega NUMERIC,
            high_water_mark NUMERIC,
            drawdown NUMERIC,
            is_paper BOOLEAN NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE portfolio_snapshots")
