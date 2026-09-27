"""
Pacing for rate-limited free models (the Google AI Studio free tier).

Every call through a ThrottledLlm waits for its turn on a shared RequestPacer,
so calls start at most `requests_per_minute` times a minute, across all
questions and forecasts in the run. Retries go through the pacer too.

If the model is overloaded or out of quota, the call goes to its backup model
(another free Gemini model, with its own separate quota and pacer).
"""
from __future__ import annotations

import asyncio
import logging
import time

import litellm
from forecasting_tools import GeneralLlm

from bot_helpers import PUBLIC_LOGGER_NAME

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

# Errors that mean "this model can't serve us right now", as opposed to a bad
# request: overloaded (503), server error, out of quota (429), timeout.
_TRY_BACKUP_ERRORS = (
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.RateLimitError,
    litellm.Timeout,
    litellm.APIConnectionError,
)


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
    """
    A GeneralLlm whose every model call (including retries) waits on a pacer,
    and falls over to `backup` when the model is unavailable.
    """

    def __init__(
        self,
        *args,
        pacer: RequestPacer,
        backup: ThrottledLlm | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._pacer = pacer
        self._backup = backup

    async def _mockable_direct_call_to_model(self, prompt):  # type: ignore[no-untyped-def]
        await self._pacer.wait_turn()
        try:
            return await super()._mockable_direct_call_to_model(prompt)
        except _TRY_BACKUP_ERRORS as e:
            if self._backup is None:
                raise
            logger.warning(
                f"{self.model} unavailable ({type(e).__name__}), trying {self._backup.model}"
            )
            return await self._backup._mockable_direct_call_to_model(prompt)
