"""Record which transformer version produced each cached row.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    for table in ("transformed_strings", "payloads"):
        # Rows written before this revision are keyed without a version, and from here on
        # every key and payload id includes one: no lookup can reach them again, and labelled
        # with any version they would only sit there (or, labelled current, never be purged).
        # The data is a cache, so the correct migration is to drop it; it refills on demand.
        op.execute(sa.text(f"DELETE FROM {table}"))
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("transformer_version", sa.String(length=32), nullable=False))


def downgrade() -> None:
    for table in ("payloads", "transformed_strings"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("transformer_version")
