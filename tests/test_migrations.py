from collections.abc import Iterator

import pytest
from alembic import command, op
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect

from payload_cache.config import Settings
from payload_cache.db import migrate
from payload_cache.db.engine import make_engine
from payload_cache.db.models import Base
from payload_cache.main import create_app
from tests.conftest import SAMPLE_REQUEST

TABLES = {"payloads", "transformed_strings"}


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str:
    head = ScriptDirectory.from_config(migrate.alembic_config()).get_current_head()
    assert head is not None
    return head


def test_upgrade_builds_exactly_the_schema_the_models_describe(engine: Engine) -> None:
    migrate.upgrade_to_head(engine)
    assert TABLES.issubset(inspect(engine).get_table_names())
    with engine.connect() as connection:
        # An empty diff means a revision was not forgotten: autogenerate would add nothing.
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    assert current_revision(engine) == head_revision()


def test_upgrading_an_up_to_date_database_is_a_no_op(engine: Engine) -> None:
    migrate.upgrade_to_head(engine)
    migrate.upgrade_to_head(engine)
    assert current_revision(engine) == head_revision()


def test_downgrade_to_base_removes_the_tables(engine: Engine) -> None:
    migrate.upgrade_to_head(engine)
    config = migrate.alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
    assert not TABLES & set(inspect(engine).get_table_names())
    assert current_revision(engine) is None


def test_the_application_migrates_on_start_up(database_url: str, engine: Engine) -> None:
    app = create_app(Settings(database_url=database_url, _env_file=None))
    assert current_revision(engine) is None
    with TestClient(app) as client:
        assert current_revision(engine) == head_revision()
        assert client.post("/payload", json=SAMPLE_REQUEST).status_code == 201


def test_start_up_migrations_can_be_turned_off(database_url: str, engine: Engine) -> None:
    # The deployment ran payload-cache-migrate as its release step; the service only checks.
    migrate.upgrade_to_head(engine)
    app = create_app(Settings(database_url=database_url, run_migrations=False, _env_file=None))
    with TestClient(app) as client:
        assert client.post("/payload", json=SAMPLE_REQUEST).status_code == 201
    assert current_revision(engine) == head_revision()


@pytest.mark.parametrize("prepare", ["empty", "one revision behind"])
def test_without_start_up_migrations_a_database_behind_head_is_refused(
    database_url: str, engine: Engine, prepare: str
) -> None:
    if prepare == "one revision behind":
        migrate.upgrade_to_head(engine)
        config = migrate.alembic_config()
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "-1")
    app = create_app(Settings(database_url=database_url, run_migrations=False, _env_file=None))
    # Refused at start-up, naming the fix, rather than failing on the first request.
    with pytest.raises(RuntimeError, match="payload-cache-migrate"), TestClient(app):
        pass


def test_migrate_command_brings_the_database_to_head(
    database_url: str, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert migrate.main() == 0
    assert current_revision(engine) == head_revision()


def test_a_database_created_before_migrations_is_adopted(engine: Engine) -> None:
    # What the releases before Alembic left on their volume: the 0001 tables (0001 mirrors
    # their create_all schema) with data in them, and no version table.
    config = migrate.alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")
        connection.exec_driver_sql(
            "INSERT INTO payloads (id, output, created_at) VALUES ('"
            + "a" * 64
            + "', 'OLD', CURRENT_TIMESTAMP)"
        )
    assert current_revision(engine) is None

    migrate.upgrade_to_head(engine)

    assert current_revision(engine) == head_revision()
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        # Keyed without a version, so unreachable under the new ids: dropped, not kept forever.
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM payloads").scalar() == 0


def test_a_revision_failing_half_way_leaves_the_database_untouched(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []
    real_create_index = op.create_index

    def create_one_index_then_fail(name: str, *args: object, **kwargs: object) -> None:
        if created:
            raise RuntimeError("simulated failure inside revision 0003")
        real_create_index(name, *args, **kwargs)  # type: ignore[arg-type]
        created.append(name)

    monkeypatch.setattr(op, "create_index", create_one_index_then_fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        migrate.upgrade_to_head(engine)

    # One transaction for the whole run, DDL included, on SQLite as on PostgreSQL.
    assert created
    assert current_revision(engine) is None
    assert not set(inspect(engine).get_table_names())
