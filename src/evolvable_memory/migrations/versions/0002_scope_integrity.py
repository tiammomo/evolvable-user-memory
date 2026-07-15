"""Tighten cross-table scope and attribution integrity.

Revision ID: 0002_scope_integrity
Revises: 0001_authoritative_memory
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_scope_integrity"
down_revision: str | None = "0001_authoritative_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("fk_revision_supersedes", "memory_revisions", type_="foreignkey")
    op.create_foreign_key(
        "fk_revision_supersedes_record_scope",
        "memory_revisions",
        "memory_revisions",
        ["supersedes_revision_id", "record_id", "tenant_id", "subject_id"],
        ["id", "record_id", "tenant_id", "subject_id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("fk_candidate_revision_scope", "candidates", type_="foreignkey")
    op.create_foreign_key(
        "fk_candidate_revision_record_scope",
        "candidates",
        "memory_revisions",
        ["accepted_revision_id", "accepted_record_id", "tenant_id", "subject_id"],
        ["id", "record_id", "tenant_id", "subject_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_candidate_acceptance_references",
        "candidates",
        "(state = 'accepted' AND accepted_record_id IS NOT NULL "
        "AND accepted_revision_id IS NOT NULL) OR "
        "(state <> 'accepted' AND accepted_record_id IS NULL "
        "AND accepted_revision_id IS NULL)",
    )

    op.create_foreign_key(
        "fk_transition_from_revision_record_scope",
        "revision_transitions",
        "memory_revisions",
        ["from_revision_id", "record_id", "tenant_id", "subject_id"],
        ["id", "record_id", "tenant_id", "subject_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_transition_to_revision_record_scope",
        "revision_transitions",
        "memory_revisions",
        ["to_revision_id", "record_id", "tenant_id", "subject_id"],
        ["id", "record_id", "tenant_id", "subject_id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "recall_trace_items_revision_id_tenant_id_subject_id_fkey",
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

    op.create_check_constraint(
        "ck_strategy_version_positive",
        "strategy_snapshots",
        "version > 0",
    )
    op.create_check_constraint(
        "ck_strategy_min_score",
        "strategy_snapshots",
        "min_score >= 0 AND min_score <= 1",
    )
    op.create_check_constraint(
        "ck_strategy_recency_half_life",
        "strategy_snapshots",
        "recency_half_life_days >= 1 AND recency_half_life_days <= 3650",
    )
    op.create_unique_constraint(
        "uq_strategy_id_version",
        "strategy_snapshots",
        ["id", "version"],
    )
    op.create_foreign_key(
        "fk_trace_strategy_version",
        "recall_traces",
        "strategy_snapshots",
        ["policy_id", "policy_version"],
        ["id", "version"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_trace_strategy_version", "recall_traces", type_="foreignkey")
    op.drop_constraint("uq_strategy_id_version", "strategy_snapshots", type_="unique")
    op.drop_constraint("ck_strategy_recency_half_life", "strategy_snapshots", type_="check")
    op.drop_constraint("ck_strategy_min_score", "strategy_snapshots", type_="check")
    op.drop_constraint("ck_strategy_version_positive", "strategy_snapshots", type_="check")

    op.drop_constraint(
        "fk_trace_item_revision_record_scope",
        "recall_trace_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "recall_trace_items_revision_id_tenant_id_subject_id_fkey",
        "recall_trace_items",
        "memory_revisions",
        ["revision_id", "tenant_id", "subject_id"],
        ["id", "tenant_id", "subject_id"],
    )

    op.drop_constraint(
        "fk_transition_to_revision_record_scope",
        "revision_transitions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_transition_from_revision_record_scope",
        "revision_transitions",
        type_="foreignkey",
    )

    op.drop_constraint("ck_candidate_acceptance_references", "candidates", type_="check")
    op.drop_constraint(
        "fk_candidate_revision_record_scope",
        "candidates",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_candidate_revision_scope",
        "candidates",
        "memory_revisions",
        ["accepted_revision_id", "tenant_id", "subject_id"],
        ["id", "tenant_id", "subject_id"],
    )

    op.drop_constraint(
        "fk_revision_supersedes_record_scope",
        "memory_revisions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_revision_supersedes",
        "memory_revisions",
        "memory_revisions",
        ["supersedes_revision_id"],
        ["id"],
    )
