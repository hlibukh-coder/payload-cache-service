"""The "transformer function": a stand-in for an external service.

The transformation itself is trivial (upper-casing). What matters is that every call is
assumed to be expensive, so the implementation counts calls: tests and operators can
verify that the cache keeps that number to a minimum, which is the point of the service.
"""

import threading
import time
from typing import Protocol


class Transformer(Protocol):
    def transform(self, value: str) -> str: ...


class UpperCaseTransformer:
    def __init__(self, latency_seconds: float = 0.0) -> None:
        self._latency_seconds = latency_seconds
        self._lock = threading.Lock()
        self.calls = 0

    def transform(self, value: str) -> str:
        with self._lock:
            self.calls += 1
        if self._latency_seconds:
            # Simulates network round-trip time to the external service.
            time.sleep(self._latency_seconds)
        return value.upper()
