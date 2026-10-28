from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payload_cache.cache import LOOKUP_CHUNK_SIZE, TransformCache
from payload_cache.models import TransformedString
from payload_cache.transformer import UpperCaseTransformer


def test_results_map_each_original_to_its_transformation(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    cache = TransformCache(session, transformer)

    assert cache.transform_many(["a", "b"]) == {"a": "A", "b": "B"}


def test_repeated_values_in_one_batch_cost_one_call(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    cache = TransformCache(session, transformer)

    cache.transform_many(["a", "a", "b", "a"])

    assert transformer.calls == 2


def test_cached_values_do_not_call_the_transformer_again(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    cache = TransformCache(session, transformer)
    cache.transform_many(["a", "b"])
    session.commit()

    result = cache.transform_many(["b", "c", "a"])

    assert result == {"a": "A", "b": "B", "c": "C"}
    assert transformer.calls == 3  # a, b from the first batch; only c now


def test_cache_survives_across_sessions(session_factory, transformer: UpperCaseTransformer) -> None:
    with session_factory() as first:
        TransformCache(first, transformer).transform_many(["a"])
        first.commit()

    with session_factory() as second:
        TransformCache(second, transformer).transform_many(["a"])

    assert transformer.calls == 1


def test_lookups_larger_than_one_chunk_are_complete(
    session: Session, transformer: UpperCaseTransformer
) -> None:
    values = [f"value {i}" for i in range(LOOKUP_CHUNK_SIZE * 2 + 1)]
    cache = TransformCache(session, transformer)
    cache.transform_many(values)
    session.commit()

    result = cache.transform_many(values)

    assert len(result) == len(values)
    assert transformer.calls == len(values)
    stored = session.scalar(select(func.count()).select_from(TransformedString))
    assert stored == len(values)
