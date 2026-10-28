"""Runtime configuration for the service.

Values come from environment variables (or a local ``.env`` file) so the same container
image runs unchanged in every environment.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite by default so the service runs with zero infrastructure. Any SQLAlchemy URL
    # works (PostgreSQL included) because the data layer sticks to portable constructs.
    database_url: str = "sqlite:///./data/payload_cache.db"

    # Echo SQL statements; handy when verifying the caching behaviour by hand.
    echo_sql: bool = False

    # Artificial delay per transformer call. The transformer stands in for an external
    # service, and a non-zero delay makes cache hits visibly cheaper than misses.
    transformer_latency_seconds: float = 0.0
