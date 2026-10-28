import time

from payload_cache.transformer import UpperCaseTransformer


def test_transform_upper_cases() -> None:
    assert UpperCaseTransformer().transform("first string") == "FIRST STRING"


def test_every_call_is_counted() -> None:
    transformer = UpperCaseTransformer()
    transformer.transform("a")
    transformer.transform("a")
    assert transformer.calls == 2


def test_latency_is_applied_per_call() -> None:
    transformer = UpperCaseTransformer(latency_seconds=0.02)
    started = time.perf_counter()
    transformer.transform("a")
    assert time.perf_counter() - started >= 0.02
