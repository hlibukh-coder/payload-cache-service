"""Removing cache rows that will not, or should not, be served again.

Two reasons: the transformer version moved on, so nothing will ever look the row's key up
again (purged once, at start-up, when the version can change), or the row is older than the
configured lifetime (swept periodically, by the indexed created_at). The version purge scans
the tables, once per start-up; it is not indexed because it runs nowhere else.

During a rolling deploy, replicas still on the old version keep writing old-version rows after
the new ones have purged at start-up. Those rows are unreachable and harmless; the next
start-up removes them, or the TTL sweep when one is configured.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import CursorResult, delete
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.db.models import Payload, TransformedString
from payload_cache.observability.metrics import EVICTED_ROWS

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


def evict_expired(
    session_factory: sessionmaker[Session], ttl: timedelta, now: datetime | None = None
) -> dict[str, int]:
    """Delete every row created more than ``ttl`` ago; returns rows per table.

    One statement per table. A deployment expiring millions of rows per sweep would batch
    the deletes to keep the transaction short; this one is sized for a sweep every few
    minutes, which deletes what accumulated since the last one.
    """
    cutoff = (now or datetime.now(UTC)) - ttl
    removed: dict[str, int] = {}
    with session_factory() as session:
        for table in CACHE_TABLES:
            result = session.execute(delete(table).where(table.created_at < cutoff))
            assert isinstance(result, CursorResult)  # DML; narrows the type for rowcount
            removed[table.__tablename__] = result.rowcount
        session.commit()
    for name, count in removed.items():
        EVICTED_ROWS.labels(name, "ttl").inc(count)
    logger.log(
        logging.INFO if any(removed.values()) else logging.DEBUG,
        "evicted rows older than the TTL",
        extra={"ttl_seconds": ttl.total_seconds(), "removed": removed},
    )
    return removed


async def run_periodically(sweep: Callable[[], object], interval_seconds: float) -> None:
    """Run ``sweep`` every ``interval_seconds``, starting one interval from now, until cancelled.

    The first sweep is the lifespan's, run before the service accepts requests; this loop
    keeps it up. The sweep is synchronous database work, so it runs in a worker thread and
    never blocks the event loop. A failing sweep (the database away for a moment) is logged
    and retried at the next tick rather than ending the loop: a cache that stops being
    trimmed is a slow leak, not a reason to take the service down.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(sweep)
        except Exception:
            logger.exception("cache eviction failed; next attempt in %s s", interval_seconds)
