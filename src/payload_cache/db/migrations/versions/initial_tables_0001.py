"""Initial schema: the two content-addressed cache tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Mirrors db/models.py at this revision; test_migrations.py checks that the two agree.
    op.create_table(
        "transformed_strings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("original", sa.Text(), nullable=False),
        sa.Column("transformed", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "payloads",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("payloads")
    op.drop_table("transformed_strings")
