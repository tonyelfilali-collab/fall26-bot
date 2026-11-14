"""Claude measurement (claude_measure.py; architect, 14 Nov): money limits, resume,
scoring next to submitted / Flash / claude_first, the table. 0 real calls."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from forecasting_tools import BinaryQuestion

import claude_measure as cm
import frontier

NOW = datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)


def _money(spent=0.0, live=150.0, budget=170.0, now=NOW):
    return cm.LabMoney(spent, lambda: live, budget, now=lambda: now)


def _record(post, models=("anthropic/claude-opus-5-5-agent", "anthropic/claude-opus-5-5-agent2", "gemini/gemini-3.6-flash", "gemini/gemini-3.8-flash"),
            values=(0.8, 0.7, 0.3, 0.2), final=0.5, sections=True):
    q = BinaryQuestion(question_text="Will X?", id_of_post=post, page_url=f"https://www.metaculus.com/questions/{post}")
    record = {"question": q.model_dump(mode="json", exclude={"api_json"}), "final_forecast": final, "submitted": True,
              "forecasts": [{"kind": "planned", "status": "ok", "parsed": v, "forecast_model": m} for m, v in zip(models, values)]}
    if sections:
        record["dossier_sections"] = {"base": {"text": "Base."}}
    return record


# ---------------------------------------------------------------- money


def test_lab_cap_14():
    money = _money(spent=13.50, live=100.0)
    assert money.reserve(0.36)  # 13.86 <= 14
    assert not money.reserve(0.36) and "lab cap" in money.stopped


def test_stops_when_the_cycle_would_have_under_9_left():
    money = _money(live=160.0)  # $10 left
    assert money.reserve(0.72)  # 170 - 160 - 0.72 = 9.28
    assert not money.reserve(0.36) and "under $9.00" in money.stopped  # 8.92 left


def test_live_spend_is_read_fresh_and_counts():
    live = [150.0]
    money = cm.LabMoney(0.0, lambda: live[0], 170.0, now=lambda: NOW)
    assert money.reserve(0.72)
    money.charge(0.72, 0.15)
    live[0] = 160.5  # the live bot spent more meanwhile
    assert not money.reserve(0.72)


def test_unreadable_live_ledger_stops():
    def broken():
        raise OSError("down")

    money = cm.LabMoney(0.0, broken, 170.0, now=lambda: NOW)
    assert not money.reserve(0.36) and "could not be read" in money.stopped


def test_no_call_after_the_old_credit_expires():
    money = _money(now=cm.OLD_CREDIT_END)
    assert not money.reserve(0.36) and "expired" in money.stopped
    assert cm.OLD_CREDIT_END == datetime(2026, 11, 16, tzinfo=timezone.utc)


def test_real_cost_charged_after_the_reservation():
    money = _money()
    money.reserve(0.72)
    assert money.in_flight == pytest.approx(0.72)
    money.charge(0.72, 0.13)
    assert money.in_flight == 0.0 and money.spent == pytest.approx(0.13)


# ---------------------------------------------------------------- one replay


def test_credit_error_stops_the_run_and_is_never_retried():
    money = _money()
    calls = []

    def forecast(model, q, research, costs):
        calls.append(model)
        raise cm.CreditError("credit balance is too low")

    value, cost, status = cm.replay(cm.MODELS["fable"], None, "R", money, forecast)
    assert value is None and status == "credit error" and money.stopped == "credit error" and calls == [cm.MODELS["fable"]]
    assert cm.replay(cm.MODELS["opus"], None, "R", money, forecast)[2].startswith("not run")
    assert calls == [cm.MODELS["fable"]]


def test_plain_claude_call_effort_high_no_tools_and_cost(monkeypatch):
    import anthropic

    from tests.test_frontier import _fake_client, _response

    seen: dict = {}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _fake_client(_response(), seen))
    costs: list[float] = []
    llm = cm.claude_forecaster(cm.MODELS["fable"], costs)
    asyncio.run(llm._mockable_direct_call_to_model("Forecast this."))
    assert seen["model"] == "claude-fable-5-2" and seen["output_config"] == {"effort": "high"}
    assert "tools" not in seen and "thinking" not in seen
    assert costs == [pytest.approx((7000 * 8 + 1200 * 40) / 1e6)]


def test_credit_error_from_the_sdk_becomes_a_stop(monkeypatch):
    import anthropic

    class Broke:
        def __init__(self, **kwargs):
            self.messages = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def create(self, **request):
            error = RuntimeError("Your credit balance is too low to access the Anthropic API.")
            error.status_code = 400  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Broke)
    llm = cm.claude_forecaster(cm.MODELS["opus"], [])
    with pytest.raises(cm.CreditError):
        asyncio.run(llm._mockable_direct_call_to_model("Forecast this."))


# ---------------------------------------------------------------- scoring


def test_row_uses_live_claude_when_two_or_more():
    row = cm.question_row(_record(1), "yes", {"opus": 0.9, "fable": 0.95}, combine=lambda s, v: sorted(v)[len(v) // 2])
    assert row["claude_first_from"] == "live"
    assert row["claude_first"] == pytest.approx(cm_score(0.8))  # median of the 2 live Claude (0.7, 0.8): upper here
    assert row["opus"] == pytest.approx(cm_score(0.9)) and row["fable"] == pytest.approx(cm_score(0.95))
    assert row["flash"] == pytest.approx(cm_score(0.3)) and row["submitted"] == pytest.approx(cm_score(0.5))


def test_row_fills_claude_first_with_replays_when_live_claude_is_missing():
    record = _record(2, models=("gemini/gemini-3.6-flash", "gemini/gemini-3.8-flash"), values=(0.3, 0.2))
    row = cm.question_row(record, "no", {"opus": 0.1, "fable": 0.2}, combine=lambda s, v: sum(v) / len(v))
    assert row["claude_first_from"] == "0 live + replays"
    assert row["claude_first"] == pytest.approx(cm_score(0.15, yes=False))
    no_replays = cm.question_row(record, "no", {"opus": None, "fable": 0.2}, combine=lambda s, v: sum(v) / len(v))
    assert no_replays["claude_first"] is None and no_replays["opus"] is None


def cm_score(p, yes=True):
    from scoreboard import binary_score

    return binary_score(p, yes)


def test_table_groups_and_counts():
    rows = [
        {"post": 1, "type": "binary", "submitted": 0.1, "flash": 0.0, "claude_first": 0.2, "opus": 0.3, "fable": 0.4},
        {"post": 2, "type": "numeric", "submitted": 1.0, "flash": None, "claude_first": 1.5, "opus": None, "fable": 2.0},
        {"post": 3, "type": "multiple_choice", "submitted": -0.5, "flash": -0.7, "claude_first": None, "opus": -0.2, "fable": -0.1},
    ]
    text = cm.table(rows)
    assert "| binary | 1 | +0.100 (n=1) | +0.000 (n=1) | +0.200 (n=1) | +0.300 (n=1) | +0.400 (n=1) |" in text
    assert "| numeric + discrete | 1 |" in text and "n/a (n=0)" in text
    assert "| all | " in text and "+0.100 (n=2)" in text  # Fable minus Opus on 2 questions


# ---------------------------------------------------------------- the run


def _source(records, resolutions):
    paths = [f"questions/tournament/2026-11-0{i % 9 + 1}/{r['question']['id_of_post']}_090000.json" for i, r in enumerate(records)]
    by_path = dict(zip(paths, records))
    return cm.Source(paths + ["questions/tournament/2026-11-01/9_090000_shadows.json"], by_path.__getitem__, resolutions.get)


def test_run_end_to_end_resumes_and_skips_unresolved_and_no_dossier():
    records = [_record(10), _record(11), _record(12, sections=False), _record(13)]
    resolutions = {10: "yes", 11: "no", 12: "yes"}  # 13 unresolved
    calls: list[tuple[str, int]] = []

    def forecast(model, q, research, costs):
        calls.append((model, q.id_of_post))
        costs.append(0.10)
        return 0.9 if "fable" in model else 0.6

    saves: list[dict] = []
    money = _money(live=100.0)
    rows, data = cm.run(_source(records, resolutions), money, {}, saves.append, forecast, workers=2)
    assert sorted(p for _, p in calls) == [10, 10, 11, 11]  # 12: no dossier; 13: unresolved
    assert {r["post"] for r in rows} == {10, 11} and money.spent == pytest.approx(0.40)
    assert data["questions"]["10"]["fable"]["status"] == "ok" and saves[-1]["spent"] == pytest.approx(0.40)
    # A second run on the saved results: nothing is called again.
    calls.clear()
    rows2, _ = cm.run(_source(records, resolutions), _money(spent=0.40, live=100.0), data, saves.append, forecast)
    assert calls == [] and len(rows2) == 2


def test_run_stops_at_the_floor_and_keeps_what_it_has():
    records = [_record(p) for p in range(20, 30)]
    resolutions = {p: "yes" for p in range(20, 30)}

    def forecast(model, q, research, costs):
        costs.append(0.50)
        return 0.7

    money = _money(live=170.0 - 9.0 - 3.0)  # room for about $3 of lab spend
    rows, data = cm.run(_source(records, resolutions), money, {}, lambda d: None, forecast, workers=1)
    assert money.stopped and money.spent <= 3.0 + 1e-9
    assert 0 < sum(1 for e in data["questions"].values() for k in cm.MODELS if (e.get(k) or {}).get("status") == "ok") < 20


def test_run_stops_starting_work_near_the_time_limit():
    clock = [0.0]

    def forecast(model, q, research, costs):
        clock[0] += 400
        costs.append(0.01)
        return 0.5

    records = [_record(p) for p in range(40, 50)]
    rows, data = cm.run(_source(records, {p: "yes" for p in range(40, 50)}), _money(live=0.0), {}, lambda d: None,
                        forecast, clock=lambda: clock[0], limit=cm.CALL_SECONDS + 1000, workers=1)
    assert len(data["questions"]) < 10


def test_lab_reservations_are_the_live_worst_case():
    assert frontier.claude_reserve(cm.MODELS["fable"]) == pytest.approx(0.72)
    assert frontier.claude_reserve(cm.MODELS["opus"]) == pytest.approx(0.36)
    assert cm.LAB_CAP == 14.00 and cm.CYCLE_FLOOR == 9.00
