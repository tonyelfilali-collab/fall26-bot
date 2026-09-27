"""
Calling free Gemini models without going over their limits.

- RequestPacer: spaces out request starts per model (requests per minute).
- ThrottledLlm: a GeneralLlm that, before each call, checks the daily
  QuotaLedger has room and waits on its model's pacer; only a successful call
  is counted. If the model has no budget left, is overloaded, or is out of
  quota, the call goes to its backup (the next model in the chain) instead of
  retrying the same model.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time

import litellm
from forecasting_tools import GeneralLlm

from bot_helpers import PUBLIC_LOGGER_NAME
from gemini_budget import QuotaLedger

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

# Errors that mean "this model can't serve us right now", as opposed to a bad
# request: overloaded (503), server error, out of quota (429), timeout, or not
# offered to this key (404, e.g. older models closed to new projects).
_TRY_BACKUP_ERRORS = (
    litellm.NotFoundError,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.RateLimitError,
    litellm.Timeout,
    litellm.APIConnectionError,
)


# The models that answered in the current asyncio task (one forecast: the
# forecaster, then the parser). main.py sets a fresh list per forecast.
answered_models: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "answered_models", default=None
)


class NoQuotaLeft(RuntimeError):
    """No model in the chain has budget left today."""


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


def _is_daily_quota_error(error: Exception) -> bool:
    return isinstance(error, litellm.RateLimitError) and "PerDay" in str(error)


class ThrottledLlm(GeneralLlm):
    """
    A GeneralLlm that paces its calls, counts them against the daily ledger,
    and hands over to `backup` when it can't answer.

    booked: this model was booked in the ledger when the forecast was planned.
    allow_reserve: may use the model's reserve (only for a question's first
        forecast, and for parsing, so every question gets at least one).
    """

    def __init__(
        self,
        *args,
        pacer: RequestPacer,
        ledger: QuotaLedger | None = None,
        backup: ThrottledLlm | None = None,
        booked: bool = False,
        allow_reserve: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._pacer = pacer
        self._ledger = ledger
        self._backup = backup
        self._booked = booked
        self._allow_reserve = allow_reserve

    async def _mockable_direct_call_to_model(self, prompt):  # type: ignore[no-untyped-def]
        if self._ledger is not None:
            started = self._ledger.start(
                self.model, booked=self._booked, allow_reserve=self._allow_reserve
            )
            # A booking is only good for the first call.
            self._booked = False
            if not started:
                return await self._hand_over(prompt, "no budget left today")
        succeeded = False
        try:
            await self._pacer.wait_turn()
            response = await super()._mockable_direct_call_to_model(prompt)
            succeeded = True
        except _TRY_BACKUP_ERRORS as e:
            if self._ledger is not None and _is_daily_quota_error(e):
                # Google itself says the day's quota is gone.
                self._ledger.mark_used_up(self.model)
            error_name = type(e).__name__
        finally:
            # Only successful calls count against the quota.
            if self._ledger is not None:
                self._ledger.finish(self.model, succeeded)
        if not succeeded:
            return await self._hand_over(prompt, error_name)
        logger.info(f"{self.model}: answered")
        answered = answered_models.get()
        if answered is not None:
            answered.append(self.model)
        return response

    async def _hand_over(self, prompt, reason: str):  # type: ignore[no-untyped-def]
        if self._backup is None:
            raise NoQuotaLeft(f"{self.model}: {reason}, and no backup model left")
        logger.warning(f"{self.model}: {reason}, trying {self._backup.model}")
        return await self._backup._mockable_direct_call_to_model(prompt)
