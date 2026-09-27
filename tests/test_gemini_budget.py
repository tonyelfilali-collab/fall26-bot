"""
Tests for the free Gemini budget: the daily ledger, per-question planning,
and model hand-over. Model calls go to a local dummy Gemini server, so no real
requests are made.

    poetry run pytest -q
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import bot_config
import gemini_budget
from bot_config import (
    GEMINI_FORECAST_MODELS,
    GEMINI_PARSER_MODELS,
    GeminiPool,
    is_free_model,
)
from gemini_budget import MemoryStore, QuotaLedger
from llm_throttle import NoQuotaLeft, RequestPacer

ALL_MODELS = (*GEMINI_FORECAST_MODELS, *GEMINI_PARSER_MODELS)


# ---------------------------------------------------------------- dummy server


class DummyGemini:
    """Answers like the Gemini API. `behaviour[model]` = "ok" | "overloaded" | "daily_quota"."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.behaviour: dict[str, str] = {}
        self.forecast_answer = "Probability: 40%"
        dummy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = self.path.split("/models/")[1].split(":")[0]
                dummy.calls.append(model)
                mode = dummy.behaviour.get(model, "ok")
                if mode == "overloaded":
                    code, out = 503, {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}
                elif mode == "daily_quota":
                    code, out = 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                        "message": "You exceeded your current quota.",
                        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}]}}
                else:
                    code, out = 200, {
                        "candidates": [{"content": {"role": "model", "parts": [{"text": dummy.forecast_answer}]}, "finishReason": "STOP"}],
                        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4},
                    }
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def dummy(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    server = DummyGemini()
    original_init = bot_config.ThrottledLlm.__init__

    def init_with_base_url(self, *args, **kwargs):
        original_init(self, *args, base_url=server.base_url, **kwargs)

    monkeypatch.setattr(bot_config.ThrottledLlm, "__init__", init_with_base_url)
    yield server
    server.server.shutdown()


def make_pool(used: dict[str, int] | None = None) -> GeminiPool:
    ledger = QuotaLedger(
        MemoryStore({"day": gemini_budget.quota_day(), "used": used or {}}),
        daily_limits={m: 20 for m in ALL_MODELS},
        reserve_fraction=0.2,
    )
    # Fast pacing for tests.
    return GeminiPool(ledger=ledger, pacers={m: RequestPacer(6000) for m in ALL_MODELS})


# ---------------------------------------------------------------- ledger


def test_ledger_usable_and_reserve():
    ledger = make_pool({"gemini/gemini-3.6-flash": 15}).ledger
    assert ledger.usable_left("gemini/gemini-3.6-flash") == 1  # 16 usable - 15
    assert ledger.total_left("gemini/gemini-3.6-flash") == 5  # 20 - 15
    assert ledger.claim("gemini/gemini-3.6-flash", booked=False, allow_reserve=False)
    assert not ledger.claim("gemini/gemini-3.6-flash", booked=False, allow_reserve=False)
    assert ledger.claim("gemini/gemini-3.6-flash", booked=False, allow_reserve=True)


def test_ledger_booking_is_held_then_claimed():
    ledger = make_pool({"gemini/gemini-3.8-flash": 15}).ledger
    ledger.book("gemini/gemini-3.8-flash")
    assert ledger.usable_left("gemini/gemini-3.8-flash") == 0
    assert ledger.claim("gemini/gemini-3.8-flash", booked=True, allow_reserve=False)
    assert ledger.used["gemini/gemini-3.8-flash"] == 16
    assert ledger.pending["gemini/gemini-3.8-flash"] == 0


def test_ledger_persists_and_resets_on_a_new_day(monkeypatch):
    store = MemoryStore({"day": gemini_budget.quota_day(), "used": {"gemini/gemini-3.6-flash": 7}})
    ledger = QuotaLedger(store, {m: 20 for m in ALL_MODELS}, 0.2)
    assert ledger.used == {"gemini/gemini-3.6-flash": 7}
    ledger.claim("gemini/gemini-3.6-flash", booked=False, allow_reserve=False)
    ledger.save()
    assert store.data["used"]["gemini/gemini-3.6-flash"] == 8
    # A saved ledger from an earlier day starts from zero.
    old = QuotaLedger(MemoryStore({"day": "2000-01-01", "used": {"gemini/gemini-3.6-flash": 20}}), {m: 20 for m in ALL_MODELS}, 0.2)
    assert old.used == {}
    # The day rolls over mid-run too.
    monkeypatch.setattr(gemini_budget, "quota_day", lambda now=None: "2999-01-01")
    assert ledger.usable_left("gemini/gemini-3.6-flash") == 16


def test_ledger_mark_used_up():
    ledger = make_pool().ledger
    ledger.mark_used_up("gemini/gemini-3.7-flash")
    assert ledger.total_left("gemini/gemini-3.7-flash") == 0


# ---------------------------------------------------------------- planning


def test_plan_gives_three_different_models():
    pool = make_pool()
    plan = pool.plan(seasonal=True)
    models = [chain.model for chain in plan]
    assert len(models) == 3 and len(set(models)) == 3
    assert all(m in GEMINI_FORECAST_MODELS for m in models)


def test_plan_prefers_models_with_most_budget_left():
    pool = make_pool({"gemini/gemini-3.6-flash": 16, "gemini/gemini-3.7-flash": 10})
    models = [chain.model for chain in pool.plan(seasonal=True)]
    assert "gemini/gemini-3.6-flash" not in models  # no usable budget left
    assert models[:2] == ["gemini/gemini-3.8-flash", "gemini/gemini-3.5-flash"]


def test_minibench_gets_one_forecast_when_budget_is_low():
    # 64 usable in the pool; low = under 16 left.
    used = {m: 13 for m in GEMINI_FORECAST_MODELS}  # 3 usable left each = 12
    assert len(make_pool(used).plan(seasonal=False)) == 1
    assert len(make_pool(used).plan(seasonal=True)) == 3
    assert len(make_pool().plan(seasonal=False)) == 3


def test_reserve_gives_at_least_one_forecast():
    used = {m: 16 for m in GEMINI_FORECAST_MODELS}  # usable gone, reserve left
    plan = make_pool(used).plan(seasonal=False)
    assert len(plan) == 1 and plan[0]._allow_reserve


def test_no_quota_left_means_no_plan():
    used = {m: 20 for m in GEMINI_FORECAST_MODELS}
    assert make_pool(used).plan(seasonal=True) == []


def test_only_model_for_tests():
    plan = make_pool().plan(seasonal=True, only_model="gemini/gemini-3.8-flash")
    assert [chain.model for chain in plan] == ["gemini/gemini-3.8-flash"]


def test_free_only_guard():
    assert is_free_model("gemini/gemini-3.8-flash")
    assert is_free_model("openrouter/nvidia/nemotron-3-super-120b-a12b:free")
    assert not is_free_model("openrouter/google/gemini-3.6-flash")
    assert not is_free_model("openrouter/anthropic/claude-opus-5.5")
    assert not is_free_model("gpt-4o")


# ---------------------------------------------------------------- hand-over (dummy server)


def test_overloaded_model_hands_over_without_retrying(dummy):
    pool = make_pool()
    [chain] = pool.plan(seasonal=True, only_model="gemini/gemini-3.6-flash")
    # only_model plans a single model; give it a backup to hand over to.
    chain._backup = pool._forecaster("gemini/gemini-3.8-flash")
    dummy.behaviour["gemini-3.6-flash"] = "overloaded"
    assert asyncio.run(chain.invoke("hi")) == "Probability: 40%"
    assert dummy.calls == ["gemini-3.6-flash", "gemini-3.8-flash"]
    # Both attempts are counted.
    assert pool.ledger.used == {"gemini/gemini-3.6-flash": 1, "gemini/gemini-3.8-flash": 1}


def test_daily_quota_error_marks_model_used_up(dummy):
    pool = make_pool()
    chain = pool._forecaster("gemini/gemini-3.7-flash", backup=pool._forecaster("gemini/gemini-3.5-flash"))
    dummy.behaviour["gemini-3.7-flash"] = "daily_quota"
    asyncio.run(chain.invoke("hi"))
    assert pool.ledger.total_left("gemini/gemini-3.7-flash") == 0


def test_model_without_budget_is_skipped_without_a_call(dummy):
    pool = make_pool({"gemini/gemini-3.6-flash": 16})
    chain = pool._forecaster("gemini/gemini-3.6-flash", backup=pool._forecaster("gemini/gemini-3.5-flash"))
    asyncio.run(chain.invoke("hi"))
    assert dummy.calls == ["gemini-3.5-flash"]


def test_everything_unavailable_raises(dummy):
    pool = make_pool()
    chain = pool._forecaster("gemini/gemini-3.6-flash")
    dummy.behaviour["gemini-3.6-flash"] = "overloaded"
    with pytest.raises(NoQuotaLeft):
        asyncio.run(chain.invoke("hi"))


def test_parser_uses_only_2_5_models(dummy):
    pool = make_pool()
    parser = pool.parser()
    chain_models = []
    node = parser
    while node is not None:
        chain_models.append(node.model)
        node = node._backup
    assert chain_models == list(GEMINI_PARSER_MODELS)
    dummy.behaviour["gemini-2.5-flash"] = "overloaded"
    asyncio.run(parser.invoke("parse this"))
    assert dummy.calls == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


# ---------------------------------------------------------------- whole question (dummy server)


def test_binary_question_gets_three_forecasts_and_the_median(dummy, monkeypatch):
    from forecasting_tools import BinaryQuestion

    import main

    pool = make_pool()
    llms = {"default": pool.unplanned_forecaster(), "parser": pool.parser(), "summarizer": pool.parser(), "researcher": "no_research"}
    bot = main.FallBot2026(llms=llms, publish_reports_to_metaculus=False, enable_summarize_research=False)
    bot.gemini_pool = pool
    answers = iter(["Probability: 20%", "Probability: 40%", "Probability: 90%"])
    monkeypatch.setattr(
        main.FallBot2026,
        "_binary_prompt_to_forecast",
        lambda self, q, prompt: _forecast_with_answer(self, next(answers)),
    )
    question = BinaryQuestion(question_text="Will X happen?", id_of_post=1, page_url="https://www.metaculus.com/questions/1")
    [report] = asyncio.run(bot.forecast_questions([question], return_exceptions=True))
    assert not isinstance(report, BaseException), report
    assert report.prediction == pytest.approx(0.4)  # median of 0.2, 0.4, 0.9
    # 3 forecasts, from 3 different models.
    assert len({m for m in dummy.calls if "2.5" not in m}) == 3


async def _forecast_with_answer(bot, answer):
    from forecasting_tools import ReasonedPrediction

    # Make the real planned model call (so the dummy server sees it), then
    # return a fixed probability.
    await bot.get_llm("default", "llm").invoke("forecast")
    probability = float(answer.split(":")[1].strip(" %")) / 100
    return ReasonedPrediction(prediction_value=probability, reasoning=answer)
