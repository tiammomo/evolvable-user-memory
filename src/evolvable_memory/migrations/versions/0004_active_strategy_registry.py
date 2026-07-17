"""Add append-only active strategy history.

Revision ID: 0004_active_strategy_registry
Revises: 0003_bitemporal_recall
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_active_strategy_registry"
down_revision: str | None = "0003_bitemporal_recall"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_check_constraint(
        "ck_strategy_root_version",
        "strategy_snapshots",
        "parent_id IS NOT NULL OR version = 1",
    )
    op.create_table(
        "strategy_activations",
        sa.Column(
            "sequence",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column("id", UUID, nullable=False, unique=True),
        sa.Column(
            "strategy_id",
            UUID,
            sa.ForeignKey("strategy_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "previous_strategy_id",
            UUID,
            sa.ForeignKey("strategy_snapshots.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("experiment_id", UUID, nullable=True),
        sa.CheckConstraint(
            "kind IN ('bootstrap', 'promotion', 'rollback')",
            name="ck_strategy_activation_kind",
        ),
        sa.CheckConstraint(
            "previous_strategy_id IS NULL OR previous_strategy_id <> strategy_id",
            name="ck_strategy_activation_changes_strategy",
        ),
        sa.CheckConstraint(
            "(kind = 'bootstrap' AND previous_strategy_id IS NULL "
            "AND experiment_id IS NULL) OR "
            "(kind IN ('promotion', 'rollback') AND previous_strategy_id IS NOT NULL "
            "AND experiment_id IS NOT NULL)",
            name="ck_strategy_activation_evidence",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION emf_reject_strategy_activation_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'strategy activations are append-only'
                USING ERRCODE = '23514';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_activations_append_only
        BEFORE UPDATE OR DELETE ON strategy_activations
        FOR EACH ROW
        EXECUTE FUNCTION emf_reject_strategy_activation_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("strategy_activations")
    op.execute("DROP FUNCTION emf_reject_strategy_activation_mutation()")
    op.drop_constraint("ck_strategy_root_version", "strategy_snapshots", type_="check")
