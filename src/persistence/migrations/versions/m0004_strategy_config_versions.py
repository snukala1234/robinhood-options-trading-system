"""create strategy_config_versions (Section 14, verbatim) + immutability triggers.

The triggers are additive hardening on top of the verbatim DDL: ``parameters`` and
``created_at`` can never be rewritten, and rows can never be deleted — history is
append-only even against raw SQL. Rollback happens through status, not deletion.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE strategy_config_versions (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            parameters JSONB NOT NULL,
            status TEXT NOT NULL,
            proposed_by TEXT,
            evidence JSONB,
            approved_by TEXT,
            approved_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION strategy_config_versions_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'strategy_config_versions rows cannot be deleted';
            END IF;
            IF NEW.parameters IS DISTINCT FROM OLD.parameters
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'strategy_config_versions.parameters/created_at are immutable';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_config_versions_immutable
        BEFORE UPDATE OR DELETE ON strategy_config_versions
        FOR EACH ROW EXECUTE FUNCTION strategy_config_versions_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_strategy_config_versions_immutable ON strategy_config_versions")
    op.execute("DROP FUNCTION strategy_config_versions_immutable()")
    op.execute("DROP TABLE strategy_config_versions")
