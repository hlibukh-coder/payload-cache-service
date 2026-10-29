"""Runtime configuration for the service.

Values come from environment variables (or a local ``.env`` file) so the same container
image runs unchanged in every environment.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite by default so the service runs with zero infrastructure; PostgreSQL is the
    # other supported backend (see db.py, which refuses anything else at start-up).
    database_url: str = "sqlite:///./data/payload_cache.db"

    # Echo SQL statements; handy when verifying the caching behaviour by hand.
    echo_sql: bool = False

    # Threshold for the JSON log on stdout (see logging_setup.py). Health-probe access lines
    # sit at DEBUG, so INFO keeps the log about real requests.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Artificial delay per transformer call. The transformer stands in for an external
    # service, and a non-zero delay makes cache hits visibly cheaper than misses.
    transformer_latency_seconds: float = 0.0
