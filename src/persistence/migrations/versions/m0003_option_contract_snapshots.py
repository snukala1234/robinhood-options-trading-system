"""create option_contract_snapshots (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE option_contract_snapshots (
            id UUID PRIMARY KEY,
            underlying TEXT NOT NULL,
            option_symbol TEXT NOT NULL,
            expiration DATE NOT NULL,
            strike NUMERIC NOT NULL,
            option_type TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            bid NUMERIC,
            ask NUMERIC,
            midpoint NUMERIC,
            volume BIGINT,
            open_interest BIGINT,
            implied_volatility NUMERIC,
            delta NUMERIC,
            gamma NUMERIC,
            theta NUMERIC,
            vega NUMERIC,
            greek_source TEXT,
            raw_payload JSONB
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE option_contract_snapshots")
