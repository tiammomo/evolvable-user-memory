"""Persist gated evolution experiments and transition evidence.

Revision ID: 0005_evolution_experiments
Revises: 0004_active_strategy_registry
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_evolution_experiments"
down_revision: str | None = "0004_active_strategy_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
STAGES = "'proposed', 'offline_passed', 'shadow', 'canary', 'promoted', 'rejected', 'rolled_back'"


def upgrade() -> None:
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "evolution_experiments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "baseline_id",
            UUID,
            sa.ForeignKey("strategy_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            UUID,
            sa.ForeignKey("strategy_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.CheckConstraint(f"stage IN ({STAGES})", name="ck_evolution_experiment_stage"),
        sa.CheckConstraint(
            "baseline_id <> candidate_id",
            name="ck_evolution_experiment_distinct_strategies",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_evolution_experiment_time_order",
        ),
    )
    op.create_table(
        "evolution_experiment_transitions",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("id", UUID, nullable=False, unique=True),
        sa.Column(
            "experiment_id",
            UUID,
            sa.ForeignKey("evolution_experiments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_stage", sa.Text(), nullable=True),
        sa.Column("to_stage", sa.Text(), nullable=False),
        sa.Column("transitioned_at", timestamp, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"from_stage IS NULL OR from_stage IN ({STAGES})",
            name="ck_evolution_transition_from_stage",
        ),
        sa.CheckConstraint(
            f"to_stage IN ({STAGES})",
            name="ck_evolution_transition_to_stage",
        ),
        sa.CheckConstraint(
            "(from_stage IS NULL AND to_stage = 'proposed') OR from_stage IS NOT NULL",
            name="ck_evolution_transition_creation",
        ),
        sa.CheckConstraint(
            "from_stage IS NULL OR from_stage <> to_stage",
            name="ck_evolution_transition_changes_stage",
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_evolution_transition_idempotency_key",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_evolution_transition_request_fingerprint",
        ),
    )
    op.create_foreign_key(
        "fk_strategy_activation_experiment",
        "strategy_activations",
        "evolution_experiments",
        ["experiment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE FUNCTION emf_validate_evolution_experiment()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'evolution experiments cannot be deleted'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.stage <> 'proposed' OR NEW.created_at <> NEW.updated_at THEN
                    RAISE EXCEPTION 'evolution experiments must start at proposed'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.id <> OLD.id
               OR NEW.baseline_id <> OLD.baseline_id
               OR NEW.candidate_id <> OLD.candidate_id
               OR NEW.created_at <> OLD.created_at
               OR NEW.updated_at < OLD.updated_at THEN
                RAISE EXCEPTION 'immutable experiment identity or time changed'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT (
                (OLD.stage = 'proposed' AND NEW.stage IN ('offline_passed', 'rejected'))
                OR (OLD.stage = 'offline_passed' AND NEW.stage IN ('shadow', 'rejected'))
                OR (OLD.stage = 'shadow' AND NEW.stage IN ('canary', 'rolled_back'))
                OR (OLD.stage = 'canary' AND NEW.stage IN ('promoted', 'rolled_back'))
                OR (OLD.stage = 'promoted' AND NEW.stage = 'rolled_back')
            ) THEN
                RAISE EXCEPTION 'illegal evolution experiment transition'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evolution_experiments_guard
        BEFORE INSERT OR UPDATE OR DELETE ON evolution_experiments
        FOR EACH ROW
        EXECUTE FUNCTION emf_validate_evolution_experiment()
        """
    )
    op.execute(
        """
        CREATE FUNCTION emf_guard_evolution_transition_history()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            current_stage text;
            current_updated_at timestamptz;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'evolution transition history is append-only'
                    USING ERRCODE = '23514';
            END IF;
            SELECT stage, updated_at
            INTO current_stage, current_updated_at
            FROM evolution_experiments
            WHERE id = NEW.experiment_id;
            IF current_stage IS NULL
               OR current_stage <> NEW.to_stage
               OR current_updated_at <> NEW.transitioned_at THEN
                RAISE EXCEPTION 'transition evidence does not match experiment state'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evolution_transitions_append_only
        BEFORE INSERT OR UPDATE OR DELETE ON evolution_experiment_transitions
        FOR EACH ROW
        EXECUTE FUNCTION emf_guard_evolution_transition_history()
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_strategy_activation_experiment",
        "strategy_activations",
        type_="foreignkey",
    )
    op.drop_table("evolution_experiment_transitions")
    op.drop_table("evolution_experiments")
    op.execute("DROP FUNCTION emf_guard_evolution_transition_history()")
    op.execute("DROP FUNCTION emf_validate_evolution_experiment()")
