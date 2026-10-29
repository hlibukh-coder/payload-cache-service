"""Runtime configuration for the service.

Values come from environment variables (or a local ``.env`` file) so the same container
image runs unchanged in every environment.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from payload_cache.models import TRANSFORMER_VERSION_LENGTH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite by default so the service runs with zero infrastructure; PostgreSQL is the
    # other supported backend (see db.py, which refuses anything else at start-up).
    database_url: str = "sqlite:///./data/payload_cache.db"

    # Bring the schema to the current revision on start-up (see schema.py). A deployment
    # with several replicas turns this off and runs `payload-cache-migrate` once instead.
    run_migrations: bool = True

    # Echo SQL statements; handy when verifying the caching behaviour by hand.
    echo_sql: bool = False

    # Threshold for the JSON log on stdout (see logging_setup.py). Health-probe access lines
    # sit at DEBUG, so INFO keeps the log about real requests.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Names the behaviour of the external transformer. It is hashed into every cache key and
    # payload id, so bumping it when the transformer changes turns every cached result into
    # a miss and gives new payloads new ids, while rows of other versions are purged at
    # start-up (eviction.py). Short and URL-safe: it ends up in log lines and metrics.
    transformer_version: str = Field(
        default="1", pattern=rf"^[A-Za-z0-9._-]{{1,{TRANSFORMER_VERSION_LENGTH}}}$"
    )

    # Artificial delay per transformer call. The transformer stands in for an external
    # service, and a non-zero delay makes cache hits visibly cheaper than misses.
    transformer_latency_seconds: float = 0.0
