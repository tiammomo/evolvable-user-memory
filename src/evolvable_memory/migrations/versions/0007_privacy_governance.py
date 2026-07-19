"""Add persistent privacy governance and authorization audit evidence.

Revision ID: 0007_privacy_governance
Revises: 0006_milvus_projection_queue
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_privacy_governance"
down_revision: str | None = "0006_milvus_projection_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    timestamp = sa.DateTime(timezone=True)
    reference = sa.String(length=64)

    op.create_table(
        "processing_grants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("pseudonym_key_id", sa.Text(), nullable=False),
        sa.Column("tenant_ref", reference, nullable=False),
        sa.Column("subject_ref", reference, nullable=False),
        sa.Column("purposes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("lawful_basis", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("issued_by_ref", reference, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("valid_from", timestamp, nullable=False),
        sa.Column("valid_until", timestamp, nullable=True),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("revoked_at", timestamp, nullable=True),
        sa.Column("revoked_by_ref", reference, nullable=True),
        sa.UniqueConstraint(
            "pseudonym_key_id",
            "tenant_ref",
            "subject_ref",
            "idempotency_key",
            name="uq_processing_grant_scope_idempotency",
        ),
        sa.CheckConstraint(
            "cardinality(purposes) > 0 AND array_position(purposes, '*') IS NULL "
            "AND array_position(purposes, '') IS NULL",
            name="ck_processing_grant_purposes",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_processing_grant_validity",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_ref IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_ref IS NOT NULL)",
            name="ck_processing_grant_revocation",
        ),
    )
    op.create_index(
        "ix_processing_grant_active_scope",
        "processing_grants",
        ["pseudonym_key_id", "tenant_ref", "subject_ref", "valid_from", "valid_until"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "suppression_fences",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("pseudonym_key_id", sa.Text(), nullable=False),
        sa.Column("tenant_ref", reference, nullable=False),
        sa.Column("subject_ref", reference, nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("requested_by_ref", reference, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.UniqueConstraint(
            "pseudonym_key_id",
            "tenant_ref",
            "subject_ref",
            name="uq_suppression_fence_scope",
        ),
    )

    op.create_table(
        "erasure_requests",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("pseudonym_key_id", sa.Text(), nullable=False),
        sa.Column("tenant_ref", reference, nullable=False),
        sa.Column("subject_ref", reference, nullable=False),
        sa.Column("scope_digest", reference, nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("requested_by_ref", reference, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("completed_at", timestamp, nullable=True),
        sa.Column("summary", JSONB, nullable=True),
        sa.Column("handler_results", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "pseudonym_key_id",
            "tenant_ref",
            "subject_ref",
            "idempotency_key",
            name="uq_erasure_scope_idempotency",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed')",
            name="ck_erasure_status",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL AND summary IS NOT NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL)",
            name="ck_erasure_completion",
        ),
    )
    op.create_index(
        "ix_erasure_pending",
        "erasure_requests",
        ["status", "created_at"],
        postgresql_where=sa.text("status <> 'completed'"),
    )

    op.create_table(
        "authorization_audit_events",
        sa.Column("decision_id", UUID, primary_key=True),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("plane", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("recorded_at", timestamp, nullable=False),
        sa.Column("principal_kind", sa.Text(), nullable=False),
        sa.Column("authentication_method", sa.Text(), nullable=False),
        sa.Column("pseudonym_key_id", sa.Text(), nullable=False),
        sa.Column("principal_ref", reference, nullable=False),
        sa.Column("tenant_ref", reference, nullable=False),
        sa.Column("subject_ref", reference, nullable=False),
        sa.Column("client_ref", reference, nullable=True),
        sa.Column("resource_ref", reference, nullable=True),
        sa.Column("token_ref", reference, nullable=True),
    )
    op.create_index(
        "ix_authorization_audit_scope_time",
        "authorization_audit_events",
        ["pseudonym_key_id", "tenant_ref", "subject_ref", "recorded_at"],
    )
    op.create_index(
        "ix_authorization_audit_request",
        "authorization_audit_events",
        ["request_id"],
    )
    op.execute(
        """
        CREATE FUNCTION emf_reject_authorization_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'authorization audit events are append-only'
                USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_authorization_audit_append_only
        BEFORE UPDATE OR DELETE ON authorization_audit_events
        FOR EACH ROW EXECUTE FUNCTION emf_reject_authorization_audit_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION emf_reject_suppression_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'suppression fences are append-only'
                USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_suppression_fences_append_only
        BEFORE UPDATE OR DELETE ON suppression_fences
        FOR EACH ROW EXECUTE FUNCTION emf_reject_suppression_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION emf_guard_completed_erasure_receipt()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.status = 'completed' THEN
                RAISE EXCEPTION 'completed erasure receipts are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_completed_erasure_receipts_immutable
        BEFORE UPDATE OR DELETE ON erasure_requests
        FOR EACH ROW EXECUTE FUNCTION emf_guard_completed_erasure_receipt();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_completed_erasure_receipts_immutable ON erasure_requests")
    op.execute("DROP FUNCTION emf_guard_completed_erasure_receipt()")
    op.execute("DROP TRIGGER trg_suppression_fences_append_only ON suppression_fences")
    op.execute("DROP FUNCTION emf_reject_suppression_mutation()")
    op.execute("DROP TRIGGER trg_authorization_audit_append_only ON authorization_audit_events")
    op.execute("DROP FUNCTION emf_reject_authorization_audit_mutation()")
    op.drop_index("ix_authorization_audit_request", table_name="authorization_audit_events")
    op.drop_index("ix_authorization_audit_scope_time", table_name="authorization_audit_events")
    op.drop_table("authorization_audit_events")
    op.drop_index("ix_erasure_pending", table_name="erasure_requests")
    op.drop_table("erasure_requests")
    op.drop_table("suppression_fences")
    op.drop_index("ix_processing_grant_active_scope", table_name="processing_grants")
    op.drop_table("processing_grants")
