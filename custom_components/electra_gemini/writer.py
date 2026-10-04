# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write coalescing and rate limiting (no Home Assistant imports).

Requests made close together are merged (the latest value of each field wins) and sent as one batch, and batches are
at least ``min_interval`` seconds apart. Nothing is ever retried or looped: a failed batch fails all its callers once.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from .registers import WriteRequest


class WriteCoalescer:
    def __init__(
        self,
        apply: Callable[[WriteRequest], Awaitable[None]],
        min_interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._apply = apply
        self._min_interval = min_interval
        self._clock = clock
        self._pending: WriteRequest | None = None
        self._waiters: list[asyncio.Future[None]] = []
        self._task: asyncio.Task[None] | None = None
        self._last_done: float | None = None

    async def submit(self, request: WriteRequest) -> None:
        """Queue a request and wait until the batch containing it has been applied (or failed)."""
        request.validate()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._pending = request if self._pending is None else self._pending.merge(request)
        self._waiters.append(future)
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run())
        await future

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for future in self._waiters:
            if not future.done():
                future.cancel()
        self._pending, self._waiters = None, []

    async def _run(self) -> None:
        while self._pending is not None:
            if self._last_done is not None:
                wait = self._last_done + self._min_interval - self._clock()
                if wait > 0:
                    await asyncio.sleep(wait)
            request, waiters = self._pending, self._waiters
            self._pending, self._waiters = None, []
            try:
                await self._apply(request)
            except Exception as err:  # noqa: BLE001 - reported to every caller of the batch
                for future in waiters:
                    if not future.done():
                        future.set_exception(err)
            else:
                for future in waiters:
                    if not future.done():
                        future.set_result(None)
            self._last_done = self._clock()
