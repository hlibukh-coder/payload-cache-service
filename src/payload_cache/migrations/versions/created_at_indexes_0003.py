"""Index created_at on both cache tables for the age-based eviction sweep.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Named as SQLAlchemy names an index=True column, so models.py and the database agree.
INDEXES = (
    ("ix_transformed_strings_created_at", "transformed_strings"),
    ("ix_payloads_created_at", "payloads"),
)


def upgrade() -> None:
    # Without these, every sweep scans the whole table to find the few rows old enough.
    for name, table in INDEXES:
        op.create_index(name, table, ["created_at"])


def downgrade() -> None:
    for name, table in INDEXES:
        op.drop_index(name, table_name=table)
