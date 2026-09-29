"""NEXT 1b: the MiniBench burst rehearsal passes, and puts everything back after."""
from __future__ import annotations

import asyncio

import burst_rehearsal
import gemini_budget
import main


def test_burst_rehearsal_every_question_forecast_reserve_holds():
    sleep, quota_day, research = asyncio.sleep, gemini_budget.quota_day, main.FallBot2026._run_research_unguarded
    questions, stats = burst_rehearsal.run(seed=29)
    ok, text = burst_rehearsal.report(questions, stats)
    assert ok, text
    assert len([q for q in questions if not q.seasonal]) == 60
    assert all(q.done_at and q.done_at < q.closes for q in questions)
    assert stats.flash_lite_reserve_misuse == 0
    # Every patch is undone.
    assert asyncio.sleep is sleep and gemini_budget.quota_day is quota_day
    assert main.FallBot2026._run_research_unguarded is research
