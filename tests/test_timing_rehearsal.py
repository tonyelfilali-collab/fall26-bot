"""Run timing rehearsal: a burst of 5 at every limit ends within 35 min, in
closing order; an overload leaves the latest-closing questions for the next run."""
from __future__ import annotations

import asyncio

import main
import timing_rehearsal
from replay import _RecordedAnswer


def test_timing_rehearsal_passes_and_puts_everything_back():
    to_thread, research, answer = asyncio.to_thread, main.run_planned_research, _RecordedAnswer._mockable_direct_call_to_model
    ok, text = timing_rehearsal.report()
    assert ok, text
    assert "Run ends within 35 min (33.0 min): **yes**" in text
    assert asyncio.to_thread is to_thread and main.run_planned_research is research
    assert _RecordedAnswer._mockable_direct_call_to_model is answer
