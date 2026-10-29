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
    # Rows that predate the column were produced before a version could be configured, that
    # is, by version "1"; the server default labels them so, and costs nothing afterwards
    # because the application always writes the column explicitly.
    for table in ("transformed_strings", "payloads"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column(
                    "transformer_version", sa.String(length=32), nullable=False, server_default="1"
                )
            )


def downgrade() -> None:
    for table in ("payloads", "transformed_strings"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("transformer_version")
