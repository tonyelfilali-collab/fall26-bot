"""
Pacing for rate-limited free models (the Google AI Studio free tier).

Every call through a ThrottledLlm waits for its turn on a shared RequestPacer,
so calls start at most `requests_per_minute` times a minute, across all
questions and forecasts in the run. Retries go through the pacer too.
"""
from __future__ import annotations

import asyncio
import time

from forecasting_tools import GeneralLlm


class RequestPacer:
    """Spaces out request starts evenly: one every 60 / requests_per_minute seconds."""

    def __init__(self, requests_per_minute: float) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.requests_per_minute = requests_per_minute
        self._interval = 60.0 / requests_per_minute
        self._next_start = 0.0
        # asyncio locks belong to one event loop, and main.py may run several.
        self._locks: dict[int, asyncio.Lock] = {}

    async def wait_turn(self) -> None:
        loop_id = id(asyncio.get_running_loop())
        lock = self._locks.setdefault(loop_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + self._interval
        if start > now:
            await asyncio.sleep(start - now)


class ThrottledLlm(GeneralLlm):
    """A GeneralLlm whose every model call (including retries) waits on a pacer."""

    def __init__(self, *args, pacer: RequestPacer, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pacer = pacer

    async def _mockable_direct_call_to_model(self, prompt):  # type: ignore[no-untyped-def]
        await self._pacer.wait_turn()
        return await super()._mockable_direct_call_to_model(prompt)
