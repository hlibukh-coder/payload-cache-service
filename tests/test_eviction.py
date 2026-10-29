from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.config import Settings
from payload_cache.eviction import purge_other_versions
from payload_cache.main import create_app
from payload_cache.models import Payload, TransformedString
from payload_cache.schemas import PayloadRequest
from payload_cache.service import PayloadService
from payload_cache.transformer import UpperCaseTransformer
from tests.conftest import SAMPLE_OUTPUT, SAMPLE_REQUEST


def count(session: Session, table: type[Payload] | type[TransformedString]) -> int:
    return session.scalar(select(func.count()).select_from(table)) or 0


def test_purge_removes_only_the_rows_of_other_versions(
    session_factory: sessionmaker[Session],
    transformer: UpperCaseTransformer,
    sample_request: PayloadRequest,
) -> None:
    with session_factory() as session:
        PayloadService(session, transformer, "1").create(sample_request)
        PayloadService(session, transformer, "2").create(sample_request)

    removed = purge_other_versions(session_factory, "2")

    assert removed == {"transformed_strings": 6, "payloads": 1}
    with session_factory() as session:
        assert session.scalars(select(TransformedString.transformer_version)).all() == ["2"] * 6
        assert session.scalars(select(Payload.transformer_version)).all() == ["2"]


def test_purge_is_a_no_op_when_every_row_is_current(
    session_factory: sessionmaker[Session],
    transformer: UpperCaseTransformer,
    sample_request: PayloadRequest,
) -> None:
    with session_factory() as session:
        PayloadService(session, transformer, "1").create(sample_request)
    assert purge_other_versions(session_factory, "1") == {"transformed_strings": 0, "payloads": 0}
    with session_factory() as session:
        assert count(session, TransformedString) == 6 and count(session, Payload) == 1


def test_purged_rows_are_counted_in_the_metrics(
    session_factory: sessionmaker[Session],
    transformer: UpperCaseTransformer,
    sample_request: PayloadRequest,
) -> None:
    name = "payload_cache_evicted_rows_total"
    labels = {"table": "payloads", "reason": "version"}
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    with session_factory() as session:
        PayloadService(session, transformer, "old").create(sample_request)
    purge_other_versions(session_factory, "new")
    assert REGISTRY.get_sample_value(name, labels) == before + 1


def test_bumping_the_version_gives_new_ids_and_purges_the_old_rows(database_url: str) -> None:
    def app_for(version: str) -> TestClient:
        return TestClient(
            create_app(
                Settings(database_url=database_url, transformer_version=version, _env_file=None)
            )
        )

    with app_for("1") as client:
        first = client.post("/payload", json=SAMPLE_REQUEST)
        assert first.status_code == 201

    with app_for("2") as client:
        # Start-up purged what version 1 produced; the same request is a new payload now.
        assert client.get(f"/payload/{first.json()['id']}").status_code == 404
        second = client.post("/payload", json=SAMPLE_REQUEST)
        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]
        assert client.get(f"/payload/{second.json()['id']}").json() == {"output": SAMPLE_OUTPUT}
