"""7 Oct: failed paid calls record the HTTP status code and the provider's
error type (numbers and short labels, never message text)."""
from __future__ import annotations

import asyncio
import json

import httpx
import litellm

import bot_config
import main
import spend as spend_mod
from gemini_budget import MemoryStore, current_question_key
from llm_throttle import RequestPacer
from replay import ReplayChainLlm, _RecordedAnswer
from spend import SpendGuard, error_details

SECRET = "the model said 73% because of reasons"
BODY = {"error": {"code": 402, "message": SECRET, "metadata": {
    "provider_name": "Google AI Studio",
    "raw": json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": SECRET}}),
}}}


def openrouter_error(status: int = 402, body: dict = BODY) -> Exception:
    """Like LiteLLM's: APIError <- OpenRouterException <- httpx.HTTPStatusError (with the response)."""
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status, request=request, json=body)
    try:
        try:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as http_error:
                raise RuntimeError("OpenRouterException") from http_error
        except RuntimeError as wrapped:
            raise litellm.APIError(status_code=status, message=f"APIError: OpenRouterException - {response.text}",
                                   llm_provider="openrouter", model="google/gemini-3.6-flash", request=request) from wrapped
    except litellm.APIError as e:
        return e


def test_status_code_and_provider_labels_without_message_text():
    details = error_details(openrouter_error())
    assert details["http_status"] == 402
    assert details["provider_error"] == "code=402; provider=Google AI Studio; provider status=RESOURCE_EXHAUSTED; provider code=429"
    assert SECRET not in json.dumps(details)


def test_labels_read_from_the_message_when_there_is_no_response():
    e = litellm.APIError(status_code=403, message='OpenRouterException - {"error":{"code":403,"message":"' + SECRET + '",'
                         '"metadata":{"provider_name":"Google"}}}', llm_provider="openrouter", model="m",
                         request=httpx.Request("POST", "https://openrouter.ai"))
    details = error_details(e)
    assert details["http_status"] == 403 and "provider=Google" in details["provider_error"]
    assert SECRET not in json.dumps(details)


def test_nothing_known_means_nothing_recorded():
    assert error_details(RuntimeError("boom")) == {}
    assert error_details(None) == {}


def test_spend_ledger_entry_records_the_status_and_charges_nothing_for_402():
    guard = SpendGuard(MemoryStore({}))
    token = current_question_key.set("77")
    try:
        guard.start(bot_config.PAID_FLASH_MODEL)
        guard.finish(bot_config.PAID_FLASH_MODEL, False, error=openrouter_error())
    finally:
        current_question_key.reset(token)
    [call] = guard.questions["77"]["calls"]
    assert call["ok"] is False and call["charged"] == 0.0  # 402: refused before generating
    assert call["http_status"] == 402 and "RESOURCE_EXHAUSTED" in call["provider_error"]


def test_question_log_records_the_status_of_a_failed_forecast(monkeypatch):
    async def refused(self, prompt):  # type: ignore[no-untyped-def]
        raise openrouter_error()

    monkeypatch.setattr(_RecordedAnswer, "_mockable_direct_call_to_model", refused)
    pool = bot_config._gemini_pool(MemoryStore(), llm_class=ReplayChainLlm)
    pool.pacers = {m: RequestPacer(60000) for m in pool.pacers}
    pool.nemotron = None
    parser = pool.parser()
    bot = main.FallBot2026(llms={"default": pool.unplanned_forecaster(), "parser": parser, "summarizer": parser,
                                 "researcher": "replay"},
                           publish_reports_to_metaculus=False, enable_summarize_research=False,
                           predictions_per_research_report=3, required_successful_predictions=0)
    bot.planner = pool
    bot.question_log = None
    bot.keep_records = True
    monkeypatch.setattr(main, "QUICK_FORECAST_RETRY_WAIT_SECONDS", 0)
    from tests.test_replay import QUESTIONS

    asyncio.run(bot.forecast_questions(QUESTIONS[:1], return_exceptions=True))
    [record] = bot.__dict__["kept_records"]
    failed = [f for f in record["forecasts"] if f.get("status") == "failed"]
    assert failed and all(f.get("http_status") == 402 for f in failed)
    assert all("provider=Google AI Studio" in f.get("provider_error", "") for f in failed)
    assert SECRET not in json.dumps(record["forecasts"])
    assert spend_mod.error_details  # imported by main for the log
