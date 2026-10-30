"""ORM models: the two things the service caches.

Both tables are content-addressed. Keys are SHA-256 digests rather than the raw text so
the primary key has a fixed, index-friendly size no matter how long the input is (and
PostgreSQL caps the size of indexed values), and so identifiers are reproducible across
processes without any coordination.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Length of a hex-encoded SHA-256 digest.
DIGEST_LENGTH = 64
# Bound on the configured transformer version (config.py validates against it).
TRANSFORMER_VERSION_LENGTH = 32


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TransformedString(Base):
    """Cached outcome of the transformer for a single input string."""

    __tablename__ = "transformed_strings"

    key: Mapped[str] = mapped_column(String(DIGEST_LENGTH), primary_key=True)
    # The original is not needed to serve requests (lookups go by key) but makes the table
    # inspectable when debugging what the transformer was asked for.
    original: Mapped[str] = mapped_column(Text)
    transformed: Mapped[str] = mapped_column(Text)
    # Which transformer produced the row. The version is also hashed into the key, so a row
    # of another version is never looked up; the column is what lets such rows be purged.
    transformer_version: Mapped[str] = mapped_column(String(TRANSFORMER_VERSION_LENGTH))
    # Not consulted when serving requests: it is what the age-based eviction sweep deletes by
    # (db/eviction.py), hence the index, and what someone inspecting the table reads.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class Payload(Base):
    """A generated payload, keyed by the digest of the request and transformer version.

    Deriving the identifier from the request means re-posting the same lists resolves to
    this row directly, without consulting the transformer at all. Including the transformer
    version keeps a payload immutable: a bump yields new identifiers rather than new content
    under an old one.
    """

    __tablename__ = "payloads"

    id: Mapped[str] = mapped_column(String(DIGEST_LENGTH), primary_key=True)
    output: Mapped[str] = mapped_column(Text)
    # Same purpose as TransformedString.transformer_version.
    transformer_version: Mapped[str] = mapped_column(String(TRANSFORMER_VERSION_LENGTH))
    # Same purpose as TransformedString.created_at.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
