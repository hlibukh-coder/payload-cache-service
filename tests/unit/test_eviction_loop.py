import asyncio
import logging
from contextlib import suppress

import pytest

from payload_cache.db.eviction import run_periodically


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
