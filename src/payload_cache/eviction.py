"""Removing cache rows that will never be served again.

Both caches are keyed by content and transformer version, so a row stops being useful in
exactly one way: the transformer version moved on and nothing will ever look its key up
again. Such rows are dead weight that only grows the tables, so they are deleted.
"""

import logging

from sqlalchemy import CursorResult, delete
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.metrics import EVICTED_ROWS
from payload_cache.models import Payload, TransformedString

logger = logging.getLogger(__name__)

CACHE_TABLES: tuple[type[TransformedString] | type[Payload], ...] = (TransformedString, Payload)


def purge_other_versions(
    session_factory: sessionmaker[Session], transformer_version: str
) -> dict[str, int]:
    """Delete every row a different transformer version produced; returns rows per table."""
    removed: dict[str, int] = {}
    with session_factory() as session:
        for table in CACHE_TABLES:
            result = session.execute(
                delete(table).where(table.transformer_version != transformer_version)
            )
            assert isinstance(result, CursorResult)  # DML; narrows the type for rowcount
            removed[table.__tablename__] = result.rowcount
        session.commit()
    for name, count in removed.items():
        EVICTED_ROWS.labels(name, "version").inc(count)
    logger.log(
        logging.INFO if any(removed.values()) else logging.DEBUG,
        "purged rows of other transformer versions",
        extra={"transformer_version": transformer_version, "removed": removed},
    )
    return removed
