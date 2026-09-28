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
