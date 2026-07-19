"""Persist the projection algorithm and character budget on usage receipts.

Revision ID: 0009_usage_projection
Revises: 0008_memory_usage_receipts

The first deployed 0008 schema persisted attribution digests and revisions but
not the projection parameters returned by the public usage contract.  Keep
0008 immutable and evolve existing and fresh databases through this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_usage_projection"
down_revision: str | None = "0008_memory_usage_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_usages",
        sa.Column("algorithm", sa.Text(), nullable=True),
    )
    op.add_column(
        "memory_usages",
        sa.Column("budget_characters", sa.Integer(), nullable=True),
    )
    # Historical v0.1 consumers used this bounded projection profile.  The
    # original schema did not retain request parameters, so this explicit
    # compatibility value is the only reproducible representation available.
    op.execute(
        sa.text(
            "UPDATE memory_usages "
            "SET algorithm = 'exact-deduplicated-v1', budget_characters = 2000 "
            "WHERE algorithm IS NULL OR budget_characters IS NULL"
        )
    )
    op.alter_column("memory_usages", "algorithm", nullable=False)
    op.alter_column("memory_usages", "budget_characters", nullable=False)
    op.create_check_constraint(
        "ck_memory_usage_algorithm",
        "memory_usages",
        "algorithm IN ('ranked-extractive-v1', 'exact-deduplicated-v1')",
    )
    op.create_check_constraint(
        "ck_memory_usage_budget",
        "memory_usages",
        "budget_characters BETWEEN 64 AND 100000",
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_usage_budget", "memory_usages", type_="check")
    op.drop_constraint("ck_memory_usage_algorithm", "memory_usages", type_="check")
    op.drop_column("memory_usages", "budget_characters")
    op.drop_column("memory_usages", "algorithm")
