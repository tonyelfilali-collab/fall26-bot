"""Run timing (architect, 29 Sep): research queue order, time limits, shadow budget."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import research
import run_timing
from run_timing import ResearchQueue

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def test_capped_and_shadow_budget():
    assert run_timing.capped(None, 30.0) == 30.0
    assert run_timing.capped(100.0, 30.0) == 30.0
    assert run_timing.capped(10.0, 30.0) == 10.0
    end = run_timing.SHADOW_PHASE_END_SECONDS
    assert run_timing.shadow_seconds(0, 240) == 240
    assert run_timing.shadow_seconds(end - 60, 240) == 60
    assert run_timing.shadow_seconds(end - 29, 240) == 0.0  # under 30 s left: skipped
    assert run_timing.shadow_seconds(end + 600, 240) == 0.0
    assert run_timing.shadow_seconds(0, 0.2) == 0.2  # a short limit is kept, not skipped


def test_research_order_soonest_first_no_close_last():
    q = lambda post, minutes: SimpleNamespace(id_of_post=post, close_time=None if minutes is None else NOW + timedelta(minutes=minutes))  # noqa: E731
    assert [x.id_of_post for x in run_timing.research_order([q(1, 90), q(2, None), q(3, 10), q(4, 45)])] == [3, 4, 1, 2]


def _run_queue(asks, leave_early=(), cancel=()):
    """asks: (token, key, delay before asking). Returns the order of turns."""
    order = []

    async def main():
        queue = ResearchQueue()
        for token, key, _ in asks:
            queue.join(token, key)

        async def one(token, key, delay):
            await asyncio.sleep(delay)
            if token in leave_early:
                queue.leave(token)
                return
            try:
                await queue.turn(token, key)
            except asyncio.CancelledError:
                return
            order.append(token)
            await asyncio.sleep(0.01)
            queue.release()

        tasks = [asyncio.create_task(one(*a)) for a in asks]
        await asyncio.sleep(0.001)
        for token in cancel:
            tasks[[a[0] for a in asks].index(token)].cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)

    asyncio.run(main())
    return order


def test_queue_soonest_first_even_if_a_later_one_asks_first():
    # 3 closes soonest but asks last: it still goes first.
    assert _run_queue([(1, (0, 30), 0), (2, (0, 20), 0), (3, (0, 10), 0.005)]) == [3, 2, 1]


def test_queue_a_question_that_leaves_never_blocks_the_others():
    assert _run_queue([(1, (0, 10), 0), (2, (0, 20), 0), (3, (0, 30), 0)], leave_early={1}) == [2, 3]


def test_queue_a_cancelled_waiter_is_skipped():
    assert _run_queue([(1, (0, 10), 0), (2, (0, 20), 0), (3, (0, 30), 0)], cancel={2}) == [1, 3]


class _Q:
    id_of_post = 1
    question_text = "Will it happen?"
    resolution_criteria = "Yes if it happens."
    question_type = "binary"


def test_research_stops_at_the_deadline_and_keeps_what_was_gathered():
    article = SimpleNamespace(eng_title="Found it", summary="A fact.", pub_date=NOW, source_id="src")

    async def helper(prompt):
        if "plan news research" in prompt:
            return '{"queries": ["a", "b"], "key_facts": [], "entities": []}'
        await asyncio.sleep(10)  # the dossier writer is too slow
        return "never"

    async def asknews(query):
        return [article, article, article]

    async def go():
        deadline = asyncio.get_running_loop().time() + 0.2
        return await research.run_planned_research(_Q(), "dates", helper, asknews, lambda q: [], deadline=deadline)

    result = asyncio.run(go())
    assert result.time_limit_hit == "dossier"
    assert not result.dossier_written
    assert "Found it" in result.dossier  # the articles gathered are the research
    assert result.asknews_calls == 2


def test_research_without_deadline_unchanged():
    async def helper(prompt):
        if "plan news research" in prompt:
            return '{"queries": ["a"], "key_facts": [], "entities": []}'
        return "## Current status\nAll quiet.\nMISSING: none"

    async def asknews(query):
        return []

    result = asyncio.run(research.run_planned_research(_Q(), "dates", helper, asknews, lambda q: []))
    assert result.time_limit_hit is None and result.dossier_written
    assert "All quiet." in result.dossier


def test_research_time_up_before_starting_closes_the_call():
    called = []

    async def helper(prompt):
        called.append(prompt)
        return "{}"

    async def go():
        return await research.run_planned_research(
            _Q(), "dates", helper, lambda q: None, lambda q: [], deadline=asyncio.get_running_loop().time() - 1
        )

    result = asyncio.run(go())
    assert result.time_limit_hit == "plan" and called == []  # the planner call was never started
    assert result.dossier == "No recent news articles were found."
