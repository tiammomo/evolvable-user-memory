"""Persist bitemporal recall and outcome knowledge time.

Revision ID: 0003_bitemporal_recall
Revises: 0002_scope_integrity
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_bitemporal_recall"
down_revision: str | None = "0002_scope_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    timestamp = sa.DateTime(timezone=True)
    op.add_column("recall_traces", sa.Column("valid_at", timestamp, nullable=True))
    op.add_column("recall_traces", sa.Column("known_at", timestamp, nullable=True))
    op.add_column(
        "recall_trace_items",
        sa.Column("revision_valid_from", timestamp, nullable=True),
    )
    op.add_column(
        "recall_trace_items",
        sa.Column("revision_recorded_at", timestamp, nullable=True),
    )
    op.add_column("outcomes", sa.Column("recorded_at", timestamp, nullable=True))

    # Existing trace items already freeze the chosen revision. Its timestamps are
    # therefore authoritative for the new audit columns.
    op.execute(
        """
        UPDATE recall_trace_items AS item
        SET revision_valid_from = revision.valid_from,
            revision_recorded_at = revision.recorded_at
        FROM memory_revisions AS revision
        WHERE revision.id = item.revision_id
          AND revision.record_id = item.record_id
          AND revision.tenant_id = item.tenant_id
          AND revision.subject_id = item.subject_id
          AND (
              item.revision_valid_from IS NULL
              OR item.revision_recorded_at IS NULL
          )
        """
    )
    # Legacy recall used the transaction head without checking business time, so a
    # future-effective revision could already be present. Keep every frozen item
    # eligible by lifting valid_at to the latest referenced valid_from. An empty
    # legacy trace retains its creation instant on both axes.
    op.execute(
        """
        UPDATE recall_traces AS trace
        SET valid_at = GREATEST(
                trace.created_at,
                COALESCE(
                    (
                        SELECT MAX(item.revision_valid_from)
                        FROM recall_trace_items AS item
                        WHERE item.trace_id = trace.id
                          AND item.tenant_id = trace.tenant_id
                          AND item.subject_id = trace.subject_id
                    ),
                    trace.created_at
                )
            ),
            known_at = trace.created_at
        WHERE trace.valid_at IS NULL OR trace.known_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE outcomes
        SET recorded_at = LEAST(occurred_at, CURRENT_TIMESTAMP)
        WHERE recorded_at IS NULL
        """
    )

    op.alter_column("recall_traces", "valid_at", existing_type=timestamp, nullable=False)
    op.alter_column("recall_traces", "known_at", existing_type=timestamp, nullable=False)
    op.alter_column(
        "recall_trace_items",
        "revision_valid_from",
        existing_type=timestamp,
        nullable=False,
    )
    op.alter_column(
        "recall_trace_items",
        "revision_recorded_at",
        existing_type=timestamp,
        nullable=False,
    )
    op.alter_column("outcomes", "recorded_at", existing_type=timestamp, nullable=False)

    op.create_unique_constraint(
        "uq_revision_bitemporal_identity",
        "memory_revisions",
        [
            "id",
            "record_id",
            "tenant_id",
            "subject_id",
            "valid_from",
            "recorded_at",
        ],
    )
    op.drop_constraint(
        "fk_trace_item_revision_record_scope",
        "recall_trace_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_trace_item_revision_bitemporal_scope",
        "recall_trace_items",
        "memory_revisions",
        [
            "revision_id",
            "record_id",
            "tenant_id",
            "subject_id",
            "revision_valid_from",
            "revision_recorded_at",
        ],
        [
            "id",
            "record_id",
            "tenant_id",
            "subject_id",
            "valid_from",
            "recorded_at",
        ],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_recall_trace_known_not_after_created",
        "recall_traces",
        "known_at <= created_at",
    )
    op.create_index(
        "ix_revisions_record_bitemporal",
        "memory_revisions",
        ["record_id", "recorded_at", "sequence", "valid_from"],
    )
    op.create_index(
        "ix_outcomes_scope_revision_recorded",
        "outcomes",
        ["tenant_id", "subject_id", "revision_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outcomes_scope_revision_recorded", table_name="outcomes")
    op.drop_index("ix_revisions_record_bitemporal", table_name="memory_revisions")
    op.drop_constraint(
        "ck_recall_trace_known_not_after_created",
        "recall_traces",
        type_="check",
    )
    op.drop_constraint(
        "fk_trace_item_revision_bitemporal_scope",
        "recall_trace_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_trace_item_revision_record_scope",
        "recall_trace_items",
        "memory_revisions",
        ["revision_id", "record_id", "tenant_id", "subject_id"],
        ["id", "record_id", "tenant_id", "subject_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_revision_bitemporal_identity",
        "memory_revisions",
        type_="unique",
    )
    op.drop_column("outcomes", "recorded_at")
    op.drop_column("recall_trace_items", "revision_recorded_at")
    op.drop_column("recall_trace_items", "revision_valid_from")
    op.drop_column("recall_traces", "known_at")
    op.drop_column("recall_traces", "valid_at")
