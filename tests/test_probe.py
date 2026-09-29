"""The test-only model probe: call cap, grounding reply reading, dossier size. No real calls."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import probe


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(probe.CallBudget, "used", 0)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy ")


def test_never_more_than_12_gemini_calls():
    for _ in range(12):
        probe.CallBudget.take()
    with pytest.raises(RuntimeError):
        probe.CallBudget.take()


def _reply(status, body):
    return SimpleNamespace(status_code=status, headers={"content-type": "application/json"}, json=lambda: body)


def test_grounded_reply_is_read(monkeypatch):
    body = {
        "candidates": [{
            "content": {"parts": [{"text": "News from Sep 25, 2026 and 2026-09-20."}]},
            "groundingMetadata": {
                "webSearchQueries": ["q1", "q2"],
                "groundingChunks": [{"web": {"uri": "https://a", "title": "A"}}, {"web": {"uri": "https://b", "title": "B"}}],
            },
        }],
        "usageMetadata": {"promptTokenCount": 10},
    }
    sent = {}

    def post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, json=json)
        return _reply(200, body)

    monkeypatch.setattr(probe.requests, "post", post)
    r = probe.grounded_query("gemini-2.5-flash", "prompt")
    assert sent["json"]["tools"] == [{"google_search": {}}]
    assert sent["headers"]["x-goog-api-key"] == "dummy"  # key trimmed, never in the URL
    assert (r["sources"], len(r["search_queries"]), r["dates_in_text"]) == (2, 2, 2)
    assert r["date_fields_in_sources"] is False
    assert probe.CallBudget.used == 1


def test_grounding_error_is_kept(monkeypatch):
    monkeypatch.setattr(probe.requests, "post", lambda *a, **k: _reply(400, {"error": {"status": "INVALID_ARGUMENT", "message": "no"}}))
    r = probe.grounded_query("gemini-2.5-flash", "prompt")
    assert r["http_status"] == 400 and r["error"]["status"] == "INVALID_ARGUMENT"


def test_dossier_is_about_6k_tokens(monkeypatch):
    monkeypatch.setattr(probe, "collect_free_news", lambda title: [])
    q = SimpleNamespace(question_text="Will X?", background_info="Some background words here. " * 20)
    dossier = probe.build_dossier(q)
    assert len(dossier.split()) == probe.DOSSIER_WORDS


def test_parser_is_never_called():
    import asyncio

    with pytest.raises(RuntimeError):
        asyncio.run(probe.NoParser().invoke("x"))


def test_flash_probe_counts_in_the_ledger_and_never_uses_the_reserve(monkeypatch):
    import litellm
    from forecasting_tools import BinaryQuestion, GeneralLlm

    import bot_config
    from gemini_budget import MemoryStore, QuotaLedger

    ledger = QuotaLedger(MemoryStore(), daily_limits=dict(bot_config.GEMINI_DAILY_LIMITS),
                         reserve_fraction=bot_config.GEMINI_FREE_RESERVE, buckets=bot_config.GEMINI_QUOTA_BUCKETS)
    ledger.used["gemini/gemini-3.8-flash"] = 16  # 3.8: non-reserve quota used up, only the reserve left
    monkeypatch.setattr(probe, "live_ledger", lambda: ledger)
    monkeypatch.setattr(probe, "FLASH_PROBE_GAP_SECONDS", 0)
    sent = []

    async def fake_call(self, prompt):  # type: ignore[no-untyped-def]
        sent.append((self.model, self.litellm_kwargs.get("reasoning_effort"), self.litellm_kwargs.get("timeout")))
        if len(sent) == 2:
            raise litellm.ServiceUnavailableError(message="overloaded", llm_provider="gemini", model=self.model)
        return SimpleNamespace(data="Reasoning.\nProbability: 40%", completion_tokens_used=50, prompt_tokens_used=900,
                               total_tokens_used=950, cost=0.0)

    monkeypatch.setattr(GeneralLlm, "_mockable_direct_call_to_model", fake_call)
    question = BinaryQuestion(question_text="Will it rain?", id_of_post=1, page_url="https://www.metaculus.com/questions/1")
    outcomes = probe.flash_probe(question)
    assert [o["status"] for o in outcomes] == ["answered", "503", "not called: no budget left today", "not called: no budget left today"]
    assert probe.CallBudget.used == 2
    # Live setting (reasoning high) vs the default (no reasoning setting sent); the live 300 s timeout.
    assert sent == [("gemini/gemini-3.6-flash", "high", 300), ("gemini/gemini-3.6-flash", None, 300)]
    assert ledger.used["gemini/gemini-3.6-flash"] == 2  # both attempts counted (the 503 too)
    assert ledger.used["gemini/gemini-3.8-flash"] == 16  # the reserve was never touched
    assert all("seconds" in o for o in outcomes[:2])
