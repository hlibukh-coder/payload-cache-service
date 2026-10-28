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
    # Not consulted when serving requests. Recorded now so that an age-based eviction job, or
    # someone inspecting the table, can tell how old a row is without a migration later (the
    # cache is otherwise unbounded; see "No cache invalidation" in the README).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Payload(Base):
    """A generated payload, keyed by the digest of the request that produced it.

    Deriving the identifier from the request means re-posting the same lists resolves to
    this row directly, without consulting the transformer at all.
    """

    __tablename__ = "payloads"

    id: Mapped[str] = mapped_column(String(DIGEST_LENGTH), primary_key=True)
    output: Mapped[str] = mapped_column(Text)
    # Same purpose as TransformedString.created_at.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
