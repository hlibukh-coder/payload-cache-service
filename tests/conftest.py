import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.config import Settings
from payload_cache.db import make_engine
from payload_cache.main import create_app
from payload_cache.models import Base
from payload_cache.schemas import PayloadRequest
from payload_cache.transformer import UpperCaseTransformer

# The example from the task statement, used as the reference case throughout.
SAMPLE_REQUEST = {
    "list_1": ["first string", "second string", "third string"],
    "list_2": ["other string", "another string", "last string"],
}
SAMPLE_OUTPUT = (
    "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING"
)
# The id the README documents for SAMPLE_REQUEST. Pinned so that a change to the canonical
# form (key order, separators, escaping) is noticed: every stored payload id would otherwise
# silently stop resolving.
SAMPLE_ID = "831db6735056b8b4eca24283fd7129380f4c8f94948fc77a2a28a61f191da401"


@pytest.fixture
def sample_request() -> PayloadRequest:
    return PayloadRequest.model_validate(SAMPLE_REQUEST)


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    # A file-backed database rather than ":memory:" so tests go through the same connection
    # handling as production, including several sessions open at once. TEST_DATABASE_URL
    # points the same suite at another backend (PostgreSQL, see README).
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    # Every test starts from an empty schema; matters when the database is a shared server.
    engine = make_engine(url)
    Base.metadata.drop_all(engine)
    engine.dispose()
    return url


@pytest.fixture
def session_factory(database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def transformer() -> UpperCaseTransformer:
    return UpperCaseTransformer()


@pytest.fixture
def app(database_url: str) -> FastAPI:
    # Every field is given explicitly and _env_file=None, so neither exported variables nor
    # a developer's local .env can change what the tests run against.
    return create_app(
        Settings(
            database_url=database_url,
            echo_sql=False,
            transformer_latency_seconds=0.0,
            _env_file=None,
        )
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # Entering the context runs the lifespan, which creates the tables.
    with TestClient(app) as client:
        yield client
