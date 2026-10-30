"""Alembic environment.

Two ways in: the application (``payload_cache.db.migrate``) hands over a connection that is
already inside a transaction, and the ``alembic`` command line, run from a checkout, gets an
engine built from the same settings the service uses, so there is no second copy of the
database URL to fall out of step. Offline ``--sql`` generation is not supported: this schema
is applied by the application itself.
"""

from alembic import context
from sqlalchemy import Connection

from payload_cache.config import Settings
from payload_cache.db.engine import make_engine
from payload_cache.db.models import Base

target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things; batch mode rebuilds the table instead, so a future
        # column change written once applies to both backends.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    # Refused rather than half-supported: batch mode on SQLite needs the live table to
    # rebuild it, so a generated script would be wrong.
    raise RuntimeError(
        "offline (--sql) migrations are not supported; run them against the database"
    )

connection = context.config.attributes.get("connection")
if connection is not None:
    run_migrations(connection)
else:
    engine = make_engine(Settings().database_url)
    try:
        with engine.begin() as cli_connection:
            run_migrations(cli_connection)
    finally:
        engine.dispose()
