"""Persist actual memory usage receipts and bind outcomes to them.

Revision ID: 0008_memory_usage_receipts
Revises: 0007_privacy_governance
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_memory_usage_receipts"
down_revision: str | None = "0007_privacy_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    timestamp = sa.DateTime(timezone=True)
    digest = sa.String(length=64)

    op.create_table(
        "memory_usages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("trace_id", UUID, nullable=False),
        sa.Column("source_projection_sha256", digest, nullable=False),
        sa.Column("delivered_context_sha256", digest, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("occurred_at", timestamp, nullable=False),
        sa.Column("recorded_at", timestamp, nullable=False),
        sa.CheckConstraint(
            "source_projection_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_memory_usage_source_digest",
        ),
        sa.CheckConstraint(
            "delivered_context_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_memory_usage_context_digest",
        ),
        sa.ForeignKeyConstraint(
            ["trace_id", "tenant_id", "subject_id"],
            ["recall_traces.id", "recall_traces.tenant_id", "recall_traces.subject_id"],
            name="fk_memory_usage_trace_scope",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "id",
            "trace_id",
            "tenant_id",
            "subject_id",
            name="uq_memory_usage_trace_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_id",
            "idempotency_key",
            name="uq_memory_usage_scope_idempotency",
        ),
    )
    op.create_index(
        "ix_memory_usages_scope_time",
        "memory_usages",
        ["tenant_id", "subject_id", "recorded_at"],
    )

    op.create_table(
        "memory_usage_items",
        sa.Column("usage_id", UUID, nullable=False),
        sa.Column("trace_id", UUID, nullable=False),
        sa.Column("revision_id", UUID, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_memory_usage_item_ordinal"),
        sa.ForeignKeyConstraint(
            ["usage_id", "trace_id", "tenant_id", "subject_id"],
            [
                "memory_usages.id",
                "memory_usages.trace_id",
                "memory_usages.tenant_id",
                "memory_usages.subject_id",
            ],
            name="fk_memory_usage_item_usage_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trace_id", "revision_id", "tenant_id", "subject_id"],
            [
                "recall_trace_items.trace_id",
                "recall_trace_items.revision_id",
                "recall_trace_items.tenant_id",
                "recall_trace_items.subject_id",
            ],
            name="fk_memory_usage_item_trace_membership",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("usage_id", "ordinal"),
        sa.UniqueConstraint(
            "usage_id",
            "trace_id",
            "revision_id",
            "tenant_id",
            "subject_id",
            name="uq_memory_usage_item_attribution",
        ),
    )

    op.add_column("outcomes", sa.Column("usage_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_outcome_usage_membership",
        "outcomes",
        "memory_usage_items",
        ["usage_id", "trace_id", "revision_id", "tenant_id", "subject_id"],
        ["usage_id", "trace_id", "revision_id", "tenant_id", "subject_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_outcomes_scope_usage",
        "outcomes",
        ["tenant_id", "subject_id", "usage_id"],
        postgresql_where=sa.text("usage_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outcomes_scope_usage", table_name="outcomes")
    op.drop_constraint("fk_outcome_usage_membership", "outcomes", type_="foreignkey")
    op.drop_column("outcomes", "usage_id")
    op.drop_table("memory_usage_items")
    op.drop_index("ix_memory_usages_scope_time", table_name="memory_usages")
    op.drop_table("memory_usages")
