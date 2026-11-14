"""The Replay lab's Claude spend counts in the live pacing (architect, 14 Nov):
status/claude_lab_spend.json, written by the lab only, read by the live bot."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import claude_health
import claude_measure as cm
import frontier
import health_check
from frontier import ClaudeBudget
from gemini_budget import MemoryStore

RESET = date(2026, 10, 14)
TODAY = date(2026, 10, 21)


class Broken(MemoryStore):
    def load(self):  # type: ignore[no-untyped-def]
        raise OSError("down")


def _budget(live=100.0, lab=None):
    data = {"days": {}, "cycles": {"2026-10-14": live}}
    return ClaudeBudget(MemoryStore(data), 170.0, RESET, today=TODAY, lab_store=lab)


def test_lab_spend_counts_in_the_cycle_spend_and_step_downs():
    claude = _budget(lab=MemoryStore({"cycles": {"2026-10-14": 60.0, "2026-09-14": 99.0}}))
    assert claude.cycle_spent() == pytest.approx(160.0) and claude.lab_cycle_spent() == 60.0
    assert frontier.claude_mode(claude) == ("low", "$10.00 of the cycle budget left")


def test_lab_spend_counts_in_pacing():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    without = _budget(live=40.0).pacing(now)
    with_lab = _budget(live=40.0, lab=MemoryStore({"cycles": {"2026-10-14": 25.0}})).pacing(now)
    assert with_lab[1] == pytest.approx(without[1] + 25.0) and with_lab[3] == pytest.approx(without[3] + 25.0)


def test_no_lab_file_yet_is_zero_and_never_written_by_live():
    lab = MemoryStore(None)
    claude = _budget(lab=lab)
    assert claude.cycle_spent() == 100.0 and frontier.claude_mode(claude)[0] == "full"
    claude.save()
    assert lab.load() is None  # the live bot never writes the lab file
    assert "lab" not in str(claude.snapshot().get("cycles"))


def test_unreadable_lab_file_means_no_claude():
    claude = _budget(lab=Broken())
    assert frontier.claude_mode(claude) == ("off", "Claude lab spend could not be read")


def test_live_charges_stay_in_the_live_ledger_only():
    from gemini_budget import current_question_key

    claude = _budget(lab=MemoryStore({"cycles": {"2026-10-14": 5.0}}))
    token = current_question_key.set("1")
    try:
        claude.start(frontier.OPUS_55)
        claude.finish(frontier.OPUS_55, True, cost=0.20)
    finally:
        current_question_key.reset(token)
    assert claude.snapshot()["cycles"]["2026-10-14"] == pytest.approx(100.20)
    assert claude.cycle_spent() == pytest.approx(105.20)


def test_the_live_lineup_reads_the_lab_file(monkeypatch):
    import bot_config

    paths = []
    monkeypatch.setattr(bot_config, "make_store", lambda path: paths.append(path) or MemoryStore())
    bot_config._frontier_lineup()
    assert frontier.CLAUDE_LAB_SPEND_PATH in paths and frontier.CLAUDE_LAB_SPEND_PATH == "status/claude_lab_spend.json"


def test_health_pacing_line_includes_the_lab():
    ledger = {"days": {"2026-10-20": 40.0}, "cycles": {"2026-10-14": 40.0}}
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    lines = claude_health.claude_days_lines(ledger, now.date(), RESET, 170.0, reset=RESET, now=now,
                                            lab={"cycles": {"2026-10-14": 11.5}})
    assert any("Replay lab Claude $ this cycle: $11.5000" in line for line in lines)
    assert any(line.startswith("Pacing:") and "cycle spent $51.50" in line for line in lines)


def test_health_red_when_the_lab_file_cannot_be_read(monkeypatch):
    monkeypatch.setenv("DATA_REPO_TOKEN", "x")
    monkeypatch.setattr(claude_health, "load_claude_ledger", lambda token: {"days": {}, "cycles": {}})
    monkeypatch.setattr(claude_health, "load_claude_lab_spend", lambda token: None)
    monkeypatch.setattr(health_check, "frontier_runs", lambda since: [])
    monkeypatch.setattr(claude_health, "recent_question_logs", lambda token, since: [])
    report = health_check.Report()
    health_check.check_claude(datetime(2026, 10, 21, tzinfo=timezone.utc), report)
    assert any("lab spend" in r for r in report.red)


# ---------------------------------------------------------------- the lab writes it


def test_measurement_charges_go_to_the_lab_ledger_by_day_and_cycle():
    now = [datetime(2026, 11, 14, 23, 50, tzinfo=timezone.utc)]
    money = cm.LabMoney(11.49, lambda: 146.0, 170.0, cycle_base=11.49, now=lambda: now[0])
    assert money.reserve(0.36)
    money.charge(0.36, 0.05)
    now[0] = datetime(2026, 11, 15, 0, 10, tzinfo=timezone.utc)
    assert money.reserve(0.36)
    money.charge(0.36, 0.04)
    before = {"days": {"2026-11-14": 11.49}, "cycles": {"2026-10-18": 11.49}}
    after = money.lab_ledger(before, "2026-10-18")
    assert after["days"] == {"2026-11-14": pytest.approx(11.54), "2026-11-15": pytest.approx(0.04)}
    assert after["cycles"] == {"2026-10-18": pytest.approx(11.58)}


def test_measurement_floor_counts_earlier_lab_spend_this_cycle():
    # Live $146, lab already $11.49 this cycle: 170 - 146 - 11.49 = 12.51 left; a $0.72 call
    # keeps 11.79 >= 9; once the lab has $3.51 more, no call leaves $9.
    money = cm.LabMoney(0.0, lambda: 146.0, 170.0, cycle_base=11.49, cap=100.0,
                        now=lambda: datetime(2026, 11, 14, 22, tzinfo=timezone.utc))
    assert money.reserve(0.72)
    money.charge(0.72, 3.0)
    assert not money.reserve(0.72) and "under $9.00" in money.stopped
