import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.core.service import CreateResult, PayloadService, interleave, payload_id_for
from payload_cache.core.transformer import UpperCaseTransformer
from payload_cache.db.models import Payload, TransformedString
from payload_cache.schemas import PayloadRequest
from tests.conftest import SAMPLE_ID, SAMPLE_OUTPUT, FailingTransformer


def test_interleave_alternates_between_lists() -> None:
    assert interleave(["A", "C"], ["B", "D"]) == "A, B, C, D"


def test_interleave_rejects_lists_of_different_length() -> None:
    with pytest.raises(ValueError):
        interleave(["A"], ["B", "C"])


def test_payload_id_is_deterministic(sample_request: PayloadRequest) -> None:
    again = PayloadRequest.model_validate(sample_request.model_dump())

    assert payload_id_for(sample_request, "1") == payload_id_for(again, "1")
    assert len(payload_id_for(sample_request, "1")) == 64


def test_sample_request_has_the_documented_id(sample_request: PayloadRequest) -> None:
    assert payload_id_for(sample_request, "1") == SAMPLE_ID


def test_payload_id_depends_on_order_and_on_which_list_holds_what() -> None:
    base = PayloadRequest(list_1=["a", "b"], list_2=["c", "d"])
    reordered = PayloadRequest(list_1=["b", "a"], list_2=["d", "c"])
    swapped = PayloadRequest(list_1=["c", "d"], list_2=["a", "b"])

    assert (
        len(
            {
                payload_id_for(base, "1"),
                payload_id_for(reordered, "1"),
                payload_id_for(swapped, "1"),
            }
        )
        == 3
    )


def test_create_then_read_round_trip(
    session: Session, transformer: UpperCaseTransformer, sample_request: PayloadRequest
) -> None:
    service = PayloadService(session, transformer, "1")

    result = service.create(sample_request)

    assert result.created is True
    assert service.get_output(result.payload_id) == SAMPLE_OUTPUT
    assert transformer.calls == 6


def test_identical_request_reuses_id_without_transforming(
    session: Session, transformer: UpperCaseTransformer, sample_request: PayloadRequest
) -> None:
    service = PayloadService(session, transformer, "1")
    first = service.create(sample_request)

    second = service.create(sample_request)

    assert second == CreateResult(first.payload_id, created=False)
    assert transformer.calls == 6


def test_only_unseen_strings_are_transformed(
    session: Session, transformer: UpperCaseTransformer, sample_request: PayloadRequest
) -> None:
    service = PayloadService(session, transformer, "1")
    service.create(sample_request)

    overlapping = PayloadRequest(list_1=["first string", "brand new"], list_2=["last string", "x"])
    result = service.create(overlapping)

    assert result.created is True
    assert service.get_output(result.payload_id) == "FIRST STRING, LAST STRING, BRAND NEW, X"
    assert transformer.calls == 6 + 2


def test_unknown_id_reads_as_none(session: Session, transformer: UpperCaseTransformer) -> None:
    assert PayloadService(session, transformer, "1").get_output("0" * 64) is None


def test_concurrent_identical_requests_converge_on_one_payload(
    session_factory: sessionmaker[Session],
    transformer: UpperCaseTransformer,
    sample_request: PayloadRequest,
) -> None:
    """Simulates several workers racing on the same request.

    Exactly one of them must report the payload as created and the database must end up
    with a single payload row; every worker must still receive the same identifier.
    """
    workers = 8
    start_together = threading.Barrier(workers)

    def create() -> CreateResult:
        with session_factory() as session:
            start_together.wait()
            return PayloadService(session, transformer, "1").create(sample_request)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: create(), range(workers)))

    assert len({result.payload_id for result in results}) == 1
    assert sum(result.created for result in results) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Payload)) == 1
        assert session.scalar(select(func.count()).select_from(TransformedString)) == 6


class _SynchronisedTransformer:
    """Upper-cases, but holds each worker's first call until every worker has arrived.

    Guarantees that all workers have missed the cache lookup before any of them writes,
    which is the only window in which the order of the inserts matters.
    """

    def __init__(self, workers: int) -> None:
        self._barrier = threading.Barrier(workers)
        self._arrived = threading.local()

    def transform(self, value: str) -> str:
        if not getattr(self._arrived, "done", False):
            self._arrived.done = True
            self._barrier.wait()
        return value.upper()


def test_concurrent_overlapping_requests_in_different_order_both_succeed(
    session_factory: sessionmaker[Session],
) -> None:
    """Two requests missing on the same strings, enumerated in opposite order.

    PostgreSQL makes an ``ON CONFLICT`` insert wait for an uncommitted row with the same
    key, so without one global insert order the two transactions would wait on each other
    and one would fail with a deadlock error. SQLite serialises writers, so there the test
    is trivially green.
    """
    strings = [f"string {i}" for i in range(300)]
    forward = PayloadRequest(list_1=strings, list_2=strings)
    backward = PayloadRequest(list_1=strings[::-1], list_2=strings[::-1])
    transformer = _SynchronisedTransformer(workers=2)

    def create(request: PayloadRequest) -> CreateResult:
        with session_factory() as session:
            return PayloadService(session, transformer, "1").create(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [forward, backward]))

    assert all(result.created for result in results)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TransformedString)) == len(strings)


def test_the_transformer_version_is_part_of_the_payload_id(sample_request: PayloadRequest) -> None:
    assert payload_id_for(sample_request, "1") != payload_id_for(sample_request, "2")


def test_a_transformer_failure_stores_nothing_and_a_retry_starts_over(
    session: Session, sample_request: PayloadRequest
) -> None:
    # All or nothing: the results already paid for in the failed request are not kept, so
    # the retry calls the transformer for all six strings again (see TransformCache._fill).
    failing = FailingTransformer(fail_on_call=5)

    with pytest.raises(ConnectionError):
        PayloadService(session, failing, "1").create(sample_request)

    assert failing.calls == 5
    assert session.scalar(select(func.count()).select_from(TransformedString)) == 0
    assert session.scalar(select(func.count()).select_from(Payload)) == 0

    healthy = UpperCaseTransformer()
    result = PayloadService(session, healthy, "1").create(sample_request)

    assert result == CreateResult(SAMPLE_ID, created=True)
    assert healthy.calls == 6
    assert PayloadService(session, healthy, "1").get_output(SAMPLE_ID) == SAMPLE_OUTPUT
