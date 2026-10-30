"""Runtime configuration for the service.

Values come from environment variables (or a local ``.env`` file) so the same container
image runs unchanged in every environment.
"""

from typing import Literal

from pydantic import Field, PositiveInt
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

    # Log SQL statements (through the JSON log); handy when checking the caching by hand.
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

    # Rows older than this are deleted by a background sweep (eviction.py); unset, the cache
    # keeps everything, which is right while the transformer is deterministic and storage
    # is not the constraint. An evicted payload answers 404 until the same request is posted
    # again, when it gets the same id back.
    # Capped at ten years: beyond that the cutoff arithmetic overflows datetime at start-up.
    cache_ttl_seconds: int | None = Field(default=None, gt=0, le=10 * 365 * 24 * 3600)
    # How often the sweep runs when a TTL is set. Rows live up to this much longer than the
    # TTL, so a few minutes is plenty; shorter only adds load.
    eviction_interval_seconds: PositiveInt = 300

    # Artificial delay per transformer call. The transformer stands in for an external
    # service, and a non-zero delay makes cache hits visibly cheaper than misses. Bounded
    # here because time.sleep() rejects a negative, NaN, infinite or very large delay, which
    # would otherwise turn every cache miss into a 500 instead of failing the start-up. A
    # minute is far more than any demo needs and far below where sleep() overflows.
    transformer_latency_seconds: float = Field(default=0.0, ge=0, le=60, allow_inf_nan=False)
