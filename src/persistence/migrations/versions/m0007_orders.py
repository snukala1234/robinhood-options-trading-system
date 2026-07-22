"""create orders (Section 14, verbatim)"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE orders (
            id UUID PRIMARY KEY,
            proposal_id UUID REFERENCES trade_proposals(id),
            idempotency_key TEXT UNIQUE NOT NULL,
            broker_order_id TEXT,
            current_state TEXT NOT NULL,
            submitted_at TIMESTAMPTZ,
            raw_request JSONB,
            raw_response JSONB
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE orders")
