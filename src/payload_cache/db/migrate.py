"""Schema management: the database is brought to the current Alembic revision.

The revision scripts ship inside the package (``payload_cache.db.migrations``), so an installed
application migrates its own database, either on start-up or through the
``payload-cache-migrate`` command, with no checkout of the repository.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect

from payload_cache.config import Settings
from payload_cache.db.engine import make_engine
from payload_cache.logging_setup import configure_logging

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

logger = logging.getLogger(__name__)


def alembic_config() -> Config:
    """The configuration the repository's alembic.ini expresses, built in code.

    The command line reads alembic.ini; the application has no such file at hand once it is
    installed, so the one setting that matters, where the scripts are, is given here.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


# The revision whose schema the releases before Alembic created with create_all.
PRE_ALEMBIC_REVISION = "0001"


def upgrade_to_head(engine: Engine) -> None:
    """Apply every pending revision, inside one transaction.

    Nothing pending is a no-op, which is what makes running this on every start-up safe.
    The transaction is opened here, so env.py sees it and does not manage its own: a failure
    half-way leaves the database where it was, not somewhere in between, on both backends.
    """
    config = alembic_config()
    with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            # pysqlite never opens a transaction before DDL, so without this every CREATE and
            # ALTER commits on its own and a failed revision leaves a half-built schema that
            # the next start-up cannot migrate. With an explicit BEGIN the DDL is part of the
            # transaction SQLAlchemy commits or rolls back. Only this connection is affected:
            # the application's own connections keep pysqlite's default behaviour.
            connection.exec_driver_sql("BEGIN")
        config.attributes["connection"] = connection
        if _created_before_alembic(connection):
            # Tables made by create_all and no version table: that is revision 0001 exactly.
            # Recording it lets the later revisions apply instead of 0001 failing on "table
            # already exists", which would keep a service on an old volume from starting.
            logger.info(
                "adopting a database created before migrations",
                extra={"revision": PRE_ALEMBIC_REVISION},
            )
            command.stamp(config, PRE_ALEMBIC_REVISION)
        command.upgrade(config, "head")


def _created_before_alembic(connection: Connection) -> bool:
    current = MigrationContext.configure(connection).get_current_revision()
    return current is None and inspect(connection).has_table("payloads")


def ensure_at_head(engine: Engine) -> None:
    """Refuse to serve against a database that is not at the current revision.

    Used when start-up migrations are turned off: the first request would otherwise fail on a
    missing table or column with an error that says nothing about the cause, and a replica
    that was deployed before the release step ran would serve errors until restarted.
    """
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != head:
        raise RuntimeError(
            f"database schema is at revision {current or 'none'}, the application needs "
            f"{head}: run payload-cache-migrate (or set RUN_MIGRATIONS=true)."
        )


def main() -> int:
    """``payload-cache-migrate``: migrate as a release step, before the replicas start.

    Several replicas starting at once would race on the version table, so a deployment with
    more than one sets RUN_MIGRATIONS=false and runs this once instead.
    """
    settings = Settings()
    configure_logging(settings.log_level, echo_sql=settings.echo_sql)
    engine = make_engine(settings.database_url)
    try:
        upgrade_to_head(engine)
    finally:
        engine.dispose()
    logger.info("database schema is at head")
    return 0
