# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write coalescing and rate limiting."""
import asyncio
import time

import pytest

from custom_components.electra_gemini.registers import WriteRequest
from custom_components.electra_gemini.writer import WriteCoalescer


async def test_requests_made_together_are_merged_and_latest_wins():
    batches = []

    async def apply(request):
        batches.append(request)

    w = WriteCoalescer(apply, min_interval=0.05)
    await asyncio.gather(
        w.submit(WriteRequest(setpoint=23)),
        w.submit(WriteRequest(setpoint=25)),
        w.submit(WriteRequest(fan=2)),
    )
    assert batches == [WriteRequest(None, 2, 25)]


async def test_batches_are_at_least_min_interval_apart():
    times = []

    async def apply(request):
        times.append(time.monotonic())

    w = WriteCoalescer(apply, min_interval=0.2)
    await w.submit(WriteRequest(setpoint=23))
    await w.submit(WriteRequest(setpoint=24))
    await w.submit(WriteRequest(setpoint=25))
    assert times[1] - times[0] >= 0.19 and times[2] - times[1] >= 0.19


async def test_requests_during_the_wait_are_coalesced():
    batches = []

    async def apply(request):
        batches.append(request)

    w = WriteCoalescer(apply, min_interval=0.2)
    await w.submit(WriteRequest(setpoint=23))
    late = [asyncio.create_task(w.submit(WriteRequest(setpoint=s))) for s in (24, 25, 26)]
    await asyncio.gather(*late)
    assert batches == [WriteRequest(setpoint=23), WriteRequest(setpoint=26)]


async def test_failure_reaches_every_caller_once_and_is_not_retried():
    calls = []

    async def apply(request):
        calls.append(request)
        raise RuntimeError("boom")

    w = WriteCoalescer(apply, min_interval=0.01)
    results = await asyncio.gather(
        w.submit(WriteRequest(setpoint=23)), w.submit(WriteRequest(fan=1)), return_exceptions=True
    )
    assert all(isinstance(r, RuntimeError) for r in results)
    assert len(calls) == 1
    await asyncio.sleep(0.1)
    assert len(calls) == 1  # no retry loop


async def test_invalid_request_is_rejected_immediately():
    async def apply(request):
        raise AssertionError("must not be called")

    with pytest.raises(ValueError):
        await WriteCoalescer(apply).submit(WriteRequest(setpoint=99))
