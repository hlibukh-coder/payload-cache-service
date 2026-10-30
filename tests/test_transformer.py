import math
import time

import pytest
from pydantic import ValidationError

from payload_cache.config import Settings
from payload_cache.core.transformer import UpperCaseTransformer


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


@pytest.mark.parametrize("latency", [-1.0, math.nan, math.inf, 1e10])
def test_latency_setting_rejects_what_sleep_cannot_take(latency: float) -> None:
    # time.sleep() raises on these, so accepting them would fail every cache miss with a
    # 500 rather than the start-up.
    with pytest.raises(ValidationError, match="transformer_latency_seconds"):
        Settings(transformer_latency_seconds=latency, _env_file=None)
