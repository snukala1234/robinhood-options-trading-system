"""create order_events (Section 14, verbatim) — append-only state history."""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE order_events (
            id UUID PRIMARY KEY,
            order_id UUID REFERENCES orders(id),
            event_at TIMESTAMPTZ NOT NULL,
            previous_state TEXT,
            new_state TEXT NOT NULL,
            broker_payload JSONB,
            reason TEXT
        )
        """
    )
    # Append-only hardening: state history can never be rewritten or deleted.
    op.execute(
        """
        CREATE FUNCTION order_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'order_events is append-only (Section 12.2)';
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_order_events_append_only
        BEFORE UPDATE OR DELETE ON order_events
        FOR EACH ROW EXECUTE FUNCTION order_events_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_order_events_append_only ON order_events")
    op.execute("DROP FUNCTION order_events_append_only()")
    op.execute("DROP TABLE order_events")
