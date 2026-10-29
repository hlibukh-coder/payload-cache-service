"""Schema management: the database is brought to the current Alembic revision.

The revision scripts ship inside the package (``payload_cache.migrations``), so an installed
application migrates its own database, either on start-up or through the
``payload-cache-migrate`` command, with no checkout of the repository.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from payload_cache.config import Settings
from payload_cache.db import make_engine
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


def upgrade_to_head(engine: Engine) -> None:
    """Apply every pending revision, inside one transaction.

    Nothing pending is a no-op, which is what makes running this on every start-up safe.
    The transaction is opened here, so env.py sees it and does not manage its own: a failure
    half-way leaves the database at the previous revision, not somewhere in between (on
    PostgreSQL; SQLite also rolls DDL back).
    """
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def main() -> int:
    """``payload-cache-migrate``: migrate as a release step, before the replicas start.

    Several replicas starting at once would race on the version table, so a deployment with
    more than one sets RUN_MIGRATIONS=false and runs this once instead.
    """
    settings = Settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url, echo=settings.echo_sql)
    try:
        upgrade_to_head(engine)
    finally:
        engine.dispose()
    logger.info("database schema is at head")
    return 0
