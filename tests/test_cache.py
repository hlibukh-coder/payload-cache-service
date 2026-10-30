from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.cache import LOOKUP_CHUNK_SIZE, TransformCache, cache_key
from payload_cache.db.models import TransformedString
from payload_cache.transformer import UpperCaseTransformer


def test_results_map_each_original_to_its_transformation(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    cache = TransformCache(session, transformer, "1")

    assert cache.transform_many(["a", "b"]) == {"a": "A", "b": "B"}


def test_repeated_values_in_one_batch_cost_one_call(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    cache = TransformCache(session, transformer, "1")

    cache.transform_many(["a", "a", "b", "a"])

    assert transformer.calls == 2


def test_cached_values_do_not_call_the_transformer_again(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    cache = TransformCache(session, transformer, "1")
    cache.transform_many(["a", "b"])
    session.commit()

    result = cache.transform_many(["b", "c", "a"])

    assert result == {"a": "A", "b": "B", "c": "C"}
    assert transformer.calls == 3  # a, b from the first batch; only c now


def test_cache_survives_across_sessions(
    session_factory: sessionmaker[Session], transformer: UpperCaseTransformer
) -> None:
    with session_factory() as first:
        TransformCache(first, transformer, "1").transform_many(["a"])
        first.commit()

    with session_factory() as second:
        TransformCache(second, transformer, "1").transform_many(["a"])

    assert transformer.calls == 1


def test_connection_is_released_while_the_transformer_runs(
    session_factory: sessionmaker[Session],
) -> None:
    """A slow transformer must not pin a pooled connection for the duration of the call."""
    engine = session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    checked_out_during_transform: list[int] = []

    class ObservingTransformer:
        def transform(self, value: str) -> str:
            checked_out_during_transform.append(engine.pool.checkedout())
            return value.upper()

    with session_factory() as session:
        TransformCache(session, ObservingTransformer(), "1").transform_many(["a", "b"])
        session.commit()

    assert checked_out_during_transform == [0, 0]


def test_lookups_larger_than_one_chunk_are_complete(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    values = [f"value {i}" for i in range(LOOKUP_CHUNK_SIZE * 2 + 1)]
    cache = TransformCache(session, transformer, "1")
    cache.transform_many(values)
    session.commit()

    result = cache.transform_many(values)

    assert len(result) == len(values)
    assert transformer.calls == len(values)
    stored = session.scalar(select(func.count()).select_from(TransformedString))
    assert stored == len(values)


def test_a_different_transformer_version_does_not_share_cached_results(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    # Committing after each call is the service's contract (transform_many ends the open
    # transaction before calling the transformer, which would discard unflushed writes).
    TransformCache(session, transformer, "1").transform_many(["a", "b"])
    session.commit()
    assert transformer.calls == 2
    # Version 2 may answer differently, so its results are computed and stored apart.
    TransformCache(session, transformer, "2").transform_many(["a", "b"])
    session.commit()
    assert transformer.calls == 4
    TransformCache(session, transformer, "1").transform_many(["a", "b"])
    assert transformer.calls == 4


def test_cache_keys_differ_by_version_and_are_digests() -> None:
    assert cache_key("a", "1") != cache_key("a", "2")
    assert len(cache_key("a", "1")) == 64
