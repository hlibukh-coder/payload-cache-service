"""Database plumbing: engine construction and the one non-portable statement we need."""

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.sql import Insert

from payload_cache.models import Base

# insert_or_ignore is spelled out for these two; anything else is refused when the engine
# is built rather than on the first write.
SUPPORTED_DIALECTS = frozenset({"sqlite", "postgresql"})
# RETURNING, which the service relies on, arrived in SQLite 3.35.
MIN_SQLITE_VERSION = (3, 35, 0)


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend not in SUPPORTED_DIALECTS:
        raise ValueError(f"Unsupported database {backend!r}: use sqlite or postgresql.")
    connect_args: dict[str, Any] = {}

    if backend == "sqlite":
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError(
                f"SQLite {sqlite3.sqlite_version} is too old: RETURNING needs 3.35 or newer."
            )
        # FastAPI runs sync endpoints in a thread pool, so a pooled connection can be
        # handed to a different thread than the one that opened it.
        connect_args["check_same_thread"] = False
        if url.database and url.database != ":memory:":
            # SQLite does not create missing directories, and a fresh checkout has none.
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, echo=echo, connect_args=connect_args)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
            # WAL lets readers proceed while a request is writing, and the busy timeout
            # makes concurrent writers queue instead of failing with "database is locked".
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def insert_or_ignore(session: Session, table: type[Base]) -> Insert:
    """``INSERT ... ON CONFLICT DO NOTHING`` for the session's dialect.

    Both caches are content-addressed, so two requests racing to insert the same key are
    writing the same value; keeping whichever row landed first is the correct outcome.
    SQLAlchemy Core has no portable spelling for this, hence the dispatch on dialect.
    """
    if session.get_bind().dialect.name == "postgresql":
        return postgresql.insert(table).on_conflict_do_nothing()
    # make_engine admits nothing else, so this is SQLite.
    return sqlite.insert(table).on_conflict_do_nothing()
