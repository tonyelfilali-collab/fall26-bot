"""
Run timing (architect, 29 Sep): a burst of questions must never push a run
past the workflow's 60-minute limit.

1. Research: at most 4 minutes per question; then the question is forecast
   with what was gathered.
2. Research order: one question researches at a time, the soonest-closing
   first (ResearchQueue).
3. Forecasting stages (planned forecasts, quick forecast, follow-up search,
   round 2): at most 12 minutes per question, counted from the end of its
   research. The last-minute backup chain (Nemotron / Flash-Lite) keeps its
   own limits.
4. Shadow forecasts run at the end of the run, all together, only if the run
   is before minute 45 (otherwise skipped: "shadows skipped: time"), and end
   by minute 47. Each question's log is saved as soon as it is submitted; the
   shadow results go in a separate file at the end (nothing is overwritten).
5. No new question starts (gets its research turn) after 30 minutes into the
   run; the rest wait for the next run. Worst case: a question starting just
   before minute 30 ends by 30 + 4 + 12 = 46; with shadows, the run ends by 47.

All times are event-loop seconds (loop.time(), the same clock as
time.monotonic() in a normal run), so a rehearsal can run on a virtual clock.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

RESEARCH_LIMIT_SECONDS = 4 * 60
FORECAST_STAGES_LIMIT_SECONDS = 12 * 60
START_CUTOFF_SECONDS = 30 * 60
# Shadow forecasts start only before this point of the run...
SHADOW_START_BEFORE_SECONDS = 45 * 60
# ...and end by this one (the run then ends by minute 48 at the latest).
SHADOW_PHASE_END_SECONDS = 47 * 60
# Research that ignores its deadline (a call that can't be interrupted) is
# cut this long after the limit.
RESEARCH_GRACE_SECONDS = 15


class QuestionDeferred(Exception):
    """The run is past START_CUTOFF_SECONDS: the question waits for the next run."""


def capped(timeout: float | None, left: float) -> float:
    """The smaller of a stage's own time limit (None = none) and the time left."""
    return left if timeout is None else min(timeout, left)


def shadow_seconds(elapsed: float, limit: float) -> float:
    """Seconds each end-of-run shadow forecast may take (0 = skip them: the
    run is at or past SHADOW_START_BEFORE)."""
    if elapsed >= SHADOW_START_BEFORE_SECONDS:
        return 0.0
    return min(limit, SHADOW_PHASE_END_SECONDS - elapsed)


def research_key(question: Any) -> tuple:
    """Soonest close first; a question without a close time last."""
    close = getattr(question, "close_time", None)
    if close is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc), getattr(question, "id_of_post", 0) or 0)
    close = close if close.tzinfo else close.replace(tzinfo=timezone.utc)
    return (0, close, getattr(question, "id_of_post", 0) or 0)


def research_order(questions: list) -> list:
    return sorted(questions, key=research_key)


class ResearchQueue:
    """
    One question researches at a time. Questions join when they start (before
    anything slow); the turn always goes to the soonest-closing question that
    hasn't researched yet, so a later-closing one waits even if it asks first.
    A question that leaves without researching (an error) is forgotten, so it
    never blocks the others. Belongs to one event loop.
    """

    def __init__(self) -> None:
        self._pending: dict[int, tuple] = {}  # token -> key: joined, not yet researched
        self._waiting: dict[int, asyncio.Future] = {}
        self._busy = False

    def join(self, token: int, key: tuple) -> None:
        self._pending[token] = key

    def leave(self, token: int) -> None:
        """The question is done (or failed): it no longer holds up the queue."""
        self._pending.pop(token, None)
        self._wake()

    async def turn(self, token: int, key: tuple) -> None:
        """Wait for this question's research turn; call release() after."""
        self._pending.setdefault(token, key)
        future = asyncio.get_running_loop().create_future()
        self._waiting[token] = future
        self._wake()
        try:
            await future
        except BaseException:
            self._waiting.pop(token, None)
            if future.done() and not future.cancelled():
                self.release()  # the turn was handed over: pass it on
            else:
                self._pending.pop(token, None)
                self._wake()
            raise

    def release(self) -> None:
        self._busy = False
        self._wake()

    def _wake(self) -> None:
        if self._busy or not self._pending:
            return
        token = min(self._pending, key=lambda t: self._pending[t])
        future = self._waiting.pop(token, None)
        if future is None:
            return  # the next question hasn't asked yet: its turn is kept
        del self._pending[token]
        self._busy = True
        future.set_result(None)
