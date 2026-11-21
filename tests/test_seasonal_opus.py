"""Seasonal plain Opus (architect, 16 Nov; bot_config.FRONTIER_SEASONAL_OPUS_ENABLED):
with two seasonal agents in full mode, seasonal = agent + agent2 + Fable + plain Opus
5.5 + Flash x2. The question cap rises by plain Opus's $0.36 reservation; the seasonal
room under the $20 day cap rises too (MiniBench day limit $14.70 -> $14.40)."""
from __future__ import annotations

from pathlib import Path

import pytest

import bot_config
import frontier
from frontier import AGENT, AGENT2, FABLE, OPUS_55
from tests.test_fable import _plan, _start
from tests.test_frontier_agent import _budget

OPUS_RESERVE = 0.36


@pytest.fixture(autouse=True)
def fable_and_opus_on(monkeypatch):
    monkeypatch.setattr(bot_config, "FRONTIER_FABLE_ENABLED", True)
    monkeypatch.setattr(bot_config, "FRONTIER_SEASONAL_OPUS_ENABLED", True)


def test_switched_on_live():
    assert "FRONTIER_SEASONAL_OPUS_ENABLED = True" in Path(bot_config.__file__).read_text()
    assert frontier.claude_reserve(OPUS_55) == pytest.approx(OPUS_RESERVE)


def test_seasonal_two_agents_fable_and_plain_opus():
    planner, line = _start()
    assert "seasonal plain Opus on" in line
    models = _plan(planner, True, "81")
    assert models[:4] == [AGENT, AGENT2, OPUS_55, FABLE]
    assert len(models) == 6 and all("flash" in m for m in models[4:])  # Flash x2 unchanged
    assert planner.claude.caps["81"] == pytest.approx(frontier.AGENT2_SEASONAL_QUESTION_CAP + OPUS_RESERVE + 0.72)
    assert planner.last_plan["seasonal_opus"] is True and planner.last_plan["fable"] is True


def test_day_cap_unchanged_minibench_limit_lowered():
    planner, _ = _start()
    # 20 Nov: agent $3.51 + agent2 $2.86 + Fable $0.72 + plain Opus $0.36 = $7.45 -> $7.50; day cap $25
    # (3 seasonal questions' worst case at once), MiniBench an explicit $12.50.
    assert planner.claude.daily_cap == 25.00 and planner.claude.minibench_daily_cap == pytest.approx(12.50)
    assert frontier.seasonal_reserve(2, fable=True, opus=True) == pytest.approx(7.50)
    assert frontier.seasonal_reserve(2, opus=True) == pytest.approx(6.80)
    assert frontier.seasonal_reserve(2, fable=True) == pytest.approx(7.10)  # without the Opus
    assert frontier.seasonal_reserve(1, fable=True, opus=True) == frontier.seasonal_reserve(1, fable=True)


def test_minibench_unchanged():
    planner, _ = _start()
    models = _plan(planner, False, "82")
    assert models[:3] == [AGENT, OPUS_55, FABLE] and len(models) == 5
    assert planner.claude.caps["82"] == pytest.approx(frontier.AGENT_MINIBENCH_QUESTION_CAP + 0.72)
    assert "seasonal_opus" not in planner.last_plan


def test_one_agent_unchanged():
    planner, line = _start(agents=1)
    assert "seasonal plain Opus" not in line
    assert _plan(planner, True, "83")[:3] == [AGENT, OPUS_55, FABLE]
    assert planner.claude.caps["83"] == pytest.approx(frontier.AGENT_SEASONAL_QUESTION_CAP + 0.72)
    assert planner.claude.minibench_daily_cap == 10.40  # $15 - $4.60 (20 Nov)


def test_low_mode_unchanged():
    claude = _budget()
    claude.cycles[claude.cycle.isoformat()] = 170 - 10.0
    planner, line = _start(claude)
    assert planner.mode == "low" and "seasonal plain Opus off (low mode)" in line
    models = _plan(planner, True, "84")
    assert [m for m in models if m.startswith("anthropic/")] == [OPUS_55]
    assert planner.claude.minibench_daily_cap == 12.50  # explicit (20 Nov)


def test_paced_2_drops_agent2_no_extra_opus():
    planner, _ = _start()
    planner.pace = frontier.PACED_2
    models = _plan(planner, True, "85")
    assert models[:3] == [AGENT, OPUS_55, FABLE] and models.count(OPUS_55) == 1


def test_refused_skipped_cap_as_before(monkeypatch):
    planner, _ = _start()
    real = planner.claude.refusal
    monkeypatch.setattr(planner.claude, "refusal", lambda model: "day cap" if model.endswith(OPUS_55) else real(model))
    models = _plan(planner, True, "86")
    assert models[:3] == [AGENT, AGENT2, FABLE] and OPUS_55 not in models
    assert planner.claude.caps["86"] == pytest.approx(frontier.AGENT2_SEASONAL_QUESTION_CAP + 0.72)
    assert planner.last_plan["seasonal_opus"] is False


def test_switch_off_is_the_old_lineup(monkeypatch):
    monkeypatch.setattr(bot_config, "FRONTIER_SEASONAL_OPUS_ENABLED", False)
    planner, line = _start()
    assert "seasonal plain Opus" not in line
    assert _plan(planner, True, "87")[:3] == [AGENT, AGENT2, FABLE]
    assert planner.claude.minibench_daily_cap == 12.50  # explicit (20 Nov)
