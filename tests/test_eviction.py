import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.api.app import create_app
from payload_cache.api.schemas import PayloadRequest
from payload_cache.config import Settings
from payload_cache.core.service import PayloadService
from payload_cache.core.transformer import UpperCaseTransformer
from payload_cache.db.eviction import evict_expired, purge_other_versions, run_periodically
from payload_cache.db.models import Payload, TransformedString
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


def backdate_everything(session_factory: sessionmaker[Session], by: timedelta) -> None:
    with session_factory() as session:
        for table in (TransformedString, Payload):
            session.execute(update(table).values(created_at=datetime.now(UTC) - by))
        session.commit()


def test_evict_expired_removes_only_rows_older_than_the_ttl(
    session_factory: sessionmaker[Session],
    transformer: UpperCaseTransformer,
    sample_request: PayloadRequest,
) -> None:
    with session_factory() as session:
        PayloadService(session, transformer, "1").create(sample_request)
    backdate_everything(session_factory, timedelta(hours=2))
    with session_factory() as session:
        PayloadService(session, transformer, "1").create(
            PayloadRequest(list_1=["fresh"], list_2=["rows"])
        )

    removed = evict_expired(session_factory, ttl=timedelta(hours=1))

    assert removed == {"transformed_strings": 6, "payloads": 1}
    with session_factory() as session:
        assert count(session, TransformedString) == 2 and count(session, Payload) == 1


def test_a_row_exactly_as_old_as_the_ttl_is_kept(
    session_factory: sessionmaker[Session],
    transformer: UpperCaseTransformer,
    sample_request: PayloadRequest,
) -> None:
    with session_factory() as session:
        PayloadService(session, transformer, "1").create(sample_request)
        # One timestamp for every row: within a request the strings are stored a little
        # before the payload, which would blur the boundary this test is about.
        created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        for table in (TransformedString, Payload):
            session.execute(update(table).values(created_at=created_at))
        session.commit()
    ttl = timedelta(hours=1)
    assert evict_expired(session_factory, ttl, now=created_at + ttl) == {
        "transformed_strings": 0,
        "payloads": 0,
    }
    a_moment_later = created_at + ttl + timedelta(microseconds=1)
    assert evict_expired(session_factory, ttl, now=a_moment_later) == {
        "transformed_strings": 6,
        "payloads": 1,
    }


def test_evicted_rows_are_counted_in_the_metrics(
    session_factory: sessionmaker[Session],
    transformer: UpperCaseTransformer,
    sample_request: PayloadRequest,
) -> None:
    name = "payload_cache_evicted_rows_total"
    labels = {"table": "transformed_strings", "reason": "ttl"}
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    with session_factory() as session:
        PayloadService(session, transformer, "1").create(sample_request)
    backdate_everything(session_factory, timedelta(days=1))
    evict_expired(session_factory, ttl=timedelta(hours=1))
    assert REGISTRY.get_sample_value(name, labels) == before + 6


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_sweep_runs_at_start_up_and_then_periodically(
    database_url: str, session_factory: sessionmaker[Session], transformer: UpperCaseTransformer
) -> None:
    def create_old_payload(request: PayloadRequest) -> None:
        with session_factory() as session:
            PayloadService(session, transformer, "1").create(request)
        backdate_everything(session_factory, timedelta(days=2))

    def payloads() -> int:
        with session_factory() as session:
            return count(session, Payload)

    create_old_payload(PayloadRequest(list_1=["a"], list_2=["b"]))
    settings = Settings(
        database_url=database_url,
        cache_ttl_seconds=24 * 3600,
        eviction_interval_seconds=1,
        _env_file=None,
    )
    with TestClient(create_app(settings)):
        assert payloads() == 0  # the start-up sweep ran before the first request
        create_old_payload(PayloadRequest(list_1=["c"], list_2=["d"]))
        assert wait_until(lambda: payloads() == 0)  # and the loop keeps sweeping


def test_without_a_ttl_nothing_is_swept(
    database_url: str, session_factory: sessionmaker[Session], transformer: UpperCaseTransformer
) -> None:
    with session_factory() as session:
        PayloadService(session, transformer, "1").create(PayloadRequest(list_1=["a"], list_2=["b"]))
    backdate_everything(session_factory, timedelta(days=3650))
    with TestClient(create_app(Settings(database_url=database_url, _env_file=None))) as client:
        assert client.get("/health").status_code == 200
    with session_factory() as session:
        assert count(session, Payload) == 1


def test_a_failing_sweep_is_logged_and_the_loop_goes_on(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[int] = []

    def sweep() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("database is away")

    async def run_three_sweeps() -> None:
        # The first tick comes after one interval; the loop then keeps going past a failure.
        task = asyncio.create_task(run_periodically(sweep, interval_seconds=0.001))
        # Bounded, so a loop that died on the first failure fails the test instead of hanging.
        async with asyncio.timeout(5):
            while len(calls) < 3:
                assert not task.done(), task
                await asyncio.sleep(0.001)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.ERROR, logger="payload_cache.db.eviction"):
        asyncio.run(run_three_sweeps())

    assert len(calls) >= 3
    [record] = [r for r in caplog.records if r.name == "payload_cache.db.eviction"]
    assert "database is away" in record.getMessage() or "RuntimeError" in (record.exc_text or "")
