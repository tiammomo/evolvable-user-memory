"""Create the authoritative memory schema.

Revision ID: 0001_authoritative_memory
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_authoritative_memory"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID_ARRAY = postgresql.ARRAY(UUID)


def upgrade() -> None:
    op.create_table(
        "observations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.CheckConstraint("kind IN ('message','action','tool_result','user_feedback','outcome')"),
        sa.UniqueConstraint(
            "tenant_id", "subject_id", "idempotency_key", name="uq_observation_scope_idempotency"
        ),
    )
    op.create_index(
        "ix_observations_scope_time",
        "observations",
        ["tenant_id", "subject_id", "ingested_at"],
    )

    op.create_table(
        "evidence_spans",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "observation_id",
            UUID,
            sa.ForeignKey("observations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("stance", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.CheckConstraint("stance IN ('supports','contradicts')"),
        sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset"),
    )

    op.create_table(
        "memory_records",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("context", JSONB, nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_revision_id", UUID, nullable=True),
        sa.CheckConstraint(
            "kind IN ('preference','episodic','semantic','procedural','prospective')"
        ),
        sa.UniqueConstraint("id", "tenant_id", "subject_id", name="uq_record_id_scope"),
        sa.UniqueConstraint(
            "tenant_id", "subject_id", "key", "context_fingerprint", name="uq_memory_identity"
        ),
    )
    op.create_index("ix_memory_records_scope", "memory_records", ["tenant_id", "subject_id", "key"])

    op.create_table(
        "memory_revisions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("record_id", UUID, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("support_count", sa.Integer(), nullable=False),
        sa.Column("contradiction_count", sa.Integer(), nullable=False),
        sa.Column("source_diversity", sa.Integer(), nullable=False),
        sa.Column(
            "source_keys",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("last_evidence_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ids", UUID_ARRAY, nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_revision_id", UUID, nullable=True),
        sa.CheckConstraint("sequence > 0"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1"),
        sa.CheckConstraint(
            "support_count >= 0 AND contradiction_count >= 0 AND source_diversity >= 0"
        ),
        sa.ForeignKeyConstraint(
            ["record_id", "tenant_id", "subject_id"],
            ["memory_records.id", "memory_records.tenant_id", "memory_records.subject_id"],
            name="fk_revision_record_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_revision_id"], ["memory_revisions.id"], name="fk_revision_supersedes"
        ),
        sa.UniqueConstraint("record_id", "sequence", name="uq_revision_sequence"),
        sa.UniqueConstraint("id", "tenant_id", "subject_id", name="uq_revision_id_scope"),
        sa.UniqueConstraint(
            "id", "record_id", "tenant_id", "subject_id", name="uq_revision_record_scope"
        ),
    )
    op.create_index("ix_revisions_record_time", "memory_revisions", ["record_id", "sequence"])
    op.create_foreign_key(
        "fk_record_active_revision",
        "memory_records",
        "memory_revisions",
        ["active_revision_id", "id", "tenant_id", "subject_id"],
        ["id", "record_id", "tenant_id", "subject_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "candidates",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("observation_id", UUID, nullable=False, unique=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("context", JSONB, nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evidence_ids", UUID_ARRAY, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("accepted_record_id", UUID, nullable=True),
        sa.Column("accepted_revision_id", UUID, nullable=True),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1"),
        sa.CheckConstraint("state IN ('proposed','accepted','rejected','quarantined')"),
        sa.ForeignKeyConstraint(["observation_id"], ["observations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["accepted_record_id", "tenant_id", "subject_id"],
            ["memory_records.id", "memory_records.tenant_id", "memory_records.subject_id"],
            name="fk_candidate_record_scope",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_revision_id", "tenant_id", "subject_id"],
            ["memory_revisions.id", "memory_revisions.tenant_id", "memory_revisions.subject_id"],
            name="fk_candidate_revision_scope",
        ),
    )

    op.create_table(
        "revision_transitions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("record_id", UUID, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("to_revision_id", UUID, nullable=True),
        sa.Column("from_revision_id", UUID, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('created','superseded','retracted','suppressed','restored')"),
        sa.ForeignKeyConstraint(
            ["record_id", "tenant_id", "subject_id"],
            ["memory_records.id", "memory_records.tenant_id", "memory_records.subject_id"],
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "recall_traces",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("context", JSONB, nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("policy_id", UUID, nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "tenant_id", "subject_id", name="uq_trace_id_scope"),
    )
    op.create_index(
        "ix_traces_scope_time",
        "recall_traces",
        ["tenant_id", "subject_id", "created_at"],
    )

    op.create_table(
        "recall_trace_items",
        sa.Column("trace_id", UUID, nullable=False),
        sa.Column("revision_id", UUID, nullable=False),
        sa.Column("record_id", UUID, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("context", JSONB, nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_breakdown", JSONB, nullable=False),
        sa.Column("evidence_ids", UUID_ARRAY, nullable=False),
        sa.CheckConstraint("rank > 0"),
        sa.CheckConstraint("score >= 0 AND score <= 1"),
        sa.ForeignKeyConstraint(
            ["trace_id", "tenant_id", "subject_id"],
            ["recall_traces.id", "recall_traces.tenant_id", "recall_traces.subject_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "tenant_id", "subject_id"],
            ["memory_revisions.id", "memory_revisions.tenant_id", "memory_revisions.subject_id"],
        ),
        sa.PrimaryKeyConstraint("trace_id", "rank"),
        sa.UniqueConstraint(
            "trace_id",
            "revision_id",
            "tenant_id",
            "subject_id",
            name="uq_trace_revision_scope",
        ),
    )

    op.create_table(
        "outcomes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("trace_id", UUID, nullable=False),
        sa.Column("revision_id", UUID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('helpful','accepted','harmful','rejected','corrected')"),
        sa.CheckConstraint("weight > 0 AND weight <= 10"),
        sa.ForeignKeyConstraint(
            ["trace_id", "revision_id", "tenant_id", "subject_id"],
            [
                "recall_trace_items.trace_id",
                "recall_trace_items.revision_id",
                "recall_trace_items.tenant_id",
                "recall_trace_items.subject_id",
            ],
            name="fk_outcome_trace_membership",
        ),
        sa.UniqueConstraint(
            "tenant_id", "subject_id", "idempotency_key", name="uq_outcome_scope_idempotency"
        ),
    )

    op.create_table(
        "utility_estimates",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("revision_id", UUID, nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("positive_weight", sa.Float(), nullable=False),
        sa.Column("negative_weight", sa.Float(), nullable=False),
        sa.Column("last_outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("positive_weight >= 0 AND negative_weight >= 0"),
        sa.ForeignKeyConstraint(
            ["revision_id", "tenant_id", "subject_id"],
            ["memory_revisions.id", "memory_revisions.tenant_id", "memory_revisions.subject_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "subject_id", "revision_id", "context_fingerprint"),
    )

    op.create_table(
        "strategy_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("weights", JSONB, nullable=False),
        sa.Column("min_score", sa.Float(), nullable=False),
        sa.Column("recency_half_life_days", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parent_id", UUID, sa.ForeignKey("strategy_snapshots.id"), nullable=True),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox_events",
        ["occurred_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("strategy_snapshots")
    op.drop_table("utility_estimates")
    op.drop_table("outcomes")
    op.drop_table("recall_trace_items")
    op.drop_table("recall_traces")
    op.drop_table("revision_transitions")
    op.drop_table("candidates")
    op.drop_constraint("fk_record_active_revision", "memory_records", type_="foreignkey")
    op.drop_table("memory_revisions")
    op.drop_table("memory_records")
    op.drop_table("evidence_spans")
    op.drop_table("observations")
