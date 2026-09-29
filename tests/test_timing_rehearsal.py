"""Run timing rehearsal: every run ends by minute 48, no question starts after
minute 30, and a job killed at any minute leaves every submitted log saved."""
from __future__ import annotations

import asyncio

import main
import timing_rehearsal
from replay import _RecordedAnswer


def test_timing_rehearsal_passes_and_puts_everything_back():
    to_thread, research, answer = asyncio.to_thread, main.run_planned_research, _RecordedAnswer._mockable_direct_call_to_model
    ok, text = timing_rehearsal.report()
    assert ok, text
    assert "Run ends by minute 48 (ends at 36.0): **yes**" in text  # the burst of 5
    assert "shadows skipped: time" in text  # the late run
    assert text.count("every submitted question's log already saved: **yes**") == 3
    assert asyncio.to_thread is to_thread and main.run_planned_research is research
    assert _RecordedAnswer._mockable_direct_call_to_model is answer
