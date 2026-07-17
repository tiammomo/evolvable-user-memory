"""Add independent, resumable jobs for disposable search projections.

Revision ID: 0006_milvus_projection_queue
Revises: 0005_evolution_experiments
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_milvus_projection_queue"
down_revision: str | None = "0005_evolution_experiments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "projection_jobs",
        sa.Column("projection_name", sa.Text(), nullable=False),
        sa.Column(
            "event_id",
            UUID,
            sa.ForeignKey("outbox_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", timestamp, nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", timestamp, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", timestamp, nullable=True),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.PrimaryKeyConstraint("projection_name", "event_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'failed', 'succeeded', 'dead_letter')",
            name="ck_projection_job_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_projection_job_attempts"),
        sa.CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) "
            "OR (status <> 'processing' AND lease_owner IS NULL AND lease_until IS NULL)",
            name="ck_projection_job_lease",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND processed_at IS NOT NULL) "
            "OR (status <> 'succeeded' AND processed_at IS NULL)",
            name="ck_projection_job_completion",
        ),
    )
    op.create_index(
        "ix_projection_jobs_claim",
        "projection_jobs",
        ["projection_name", "status", "available_at", "lease_until"],
    )
    op.create_table(
        "projection_cursors",
        sa.Column("projection_name", sa.Text(), primary_key=True),
        sa.Column(
            "last_event_id",
            UUID,
            sa.ForeignKey("outbox_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_event_occurred_at", timestamp, nullable=True),
        sa.Column("updated_at", timestamp, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("projection_cursors")
    op.drop_index("ix_projection_jobs_claim", table_name="projection_jobs")
    op.drop_table("projection_jobs")
