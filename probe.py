"""
Test-only probe of other free Gemini-key models (architect, 29 Sep). Never
submits anything; at most 12 Gemini generate calls in total (each call once,
no retries).

a) Google Search grounding: one grounded query each on gemini-2.5-flash-lite
   and gemini-2.5-flash (REST API, `google_search` tool).
b) gemini-3-flash: the bot's own prompts on the 4 bot-testing-area questions
   (no research, answers read directly only: the parser is disabled).
c) gemma-4-31b: one binary test question with a ~6,000-token dossier (free
   news, no model call), to see if it fits the 16K tokens/minute limit.
flash: Flash 503 probe (architect, 29 Sep): the same binary test question
   (the bot's own prompt, no research) on gemini-3.6-flash and
   gemini-3.8-flash, each with the live setting (reasoning "high") and with
   the default (no reasoning setting sent): 4 calls, one attempt each, no
   retries, the live 300 s timeout. Every call is counted in the live quota
   ledger and uses non-reserve quota only (a model without it is not called).
   Run it twice, hours apart: at most 8 calls in total.

The repo is public: the log shows only statuses and counts. The raw answers
(grounded text and sources, forecasts' text) go to the private fall26-data
repo, probes/<date>/.

    poetry run python probe.py --part all
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

from bot_helpers import PUBLIC_LOGGER_NAME, configure_public_logging, silence_noisy_dependencies

silence_noisy_dependencies()

from forecasting_tools import BinaryQuestion, GeneralLlm, MetaculusClient  # noqa: E402

import main  # noqa: E402
from free_news import collect_free_news, format_articles  # noqa: E402
from gemini_budget import GitHubFileStore, QuotaLedger, make_store  # noqa: E402
from question_log import DATA_REPO, to_jsonable  # noqa: E402

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

API = "https://generativelanguage.googleapis.com/v1beta"
MAX_GEMINI_CALLS = 12
GROUNDING_MODELS = ("gemini-2.5-flash-lite", "gemini-2.5-flash")
FORECAST_MODEL = "gemini-3-flash"
GEMMA_MODEL = "gemma-4-31b"
DOSSIER_WORDS = 4500  # ~6,000 tokens at 0.75 words per token


class CallBudget:
    used = 0

    @classmethod
    def take(cls) -> None:
        if cls.used >= MAX_GEMINI_CALLS:
            raise RuntimeError(f"probe: {MAX_GEMINI_CALLS} Gemini calls used, stopping")
        cls.used += 1


def _key() -> str:
    return os.environ["GEMINI_API_KEY"].strip()


def resolve_models() -> dict[str, str]:
    """Wanted name -> the API's model id (listing models is not a generate call)."""
    response = requests.get(f"{API}/models", params={"pageSize": 1000}, headers={"x-goog-api-key": _key()}, timeout=30)
    response.raise_for_status()
    names = [m["name"].removeprefix("models/") for m in response.json().get("models", [])]
    found = {}
    for wanted in (*GROUNDING_MODELS, FORECAST_MODEL, GEMMA_MODEL):
        for candidate in (wanted, f"{wanted}-preview", f"{wanted}-it", f"{wanted}-latest"):
            if candidate in names:
                found[wanted] = candidate
                break
    print(f"Models listed: {len(names)}; resolved: " + ", ".join(f"{k} -> {v}" for k, v in found.items()))
    missing = [w for w in (*GROUNDING_MODELS, FORECAST_MODEL, GEMMA_MODEL) if w not in found]
    if missing:
        close = sorted(n for n in names if any(w.split("-")[0] in n and w.split("-")[1] in n for w in missing))
        print(f"Not listed: {', '.join(missing)}. Similar ids: {', '.join(close[:15])}")
    return found


# ---------------------------------------------------------------- a) grounding


def grounded_query(model_id: str, prompt: str) -> dict:
    CallBudget.take()
    response = requests.post(
        f"{API}/models/{model_id}:generateContent",
        headers={"x-goog-api-key": _key()},
        json={"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]},
        timeout=120,
    )
    out: dict[str, Any] = {"model": model_id, "http_status": response.status_code}
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200:
        error = body.get("error", {})
        out["error"] = {"status": error.get("status"), "message": (error.get("message") or "")[:500]}
        return out
    candidate = (body.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))
    metadata = candidate.get("groundingMetadata") or {}
    chunks = metadata.get("groundingChunks") or []
    out.update(
        text=text,
        grounding_metadata=metadata,
        usage=body.get("usageMetadata"),
        sources=len(chunks),
        search_queries=metadata.get("webSearchQueries") or [],
        # Does any source carry a date field, or does the text show dates?
        date_fields_in_sources=any(
            any("date" in k.lower() or "time" in k.lower() for k in (c.get("web") or {})) for c in chunks
        ),
        dates_in_text=len(re.findall(r"\b(20\d\d-\d\d-\d\d|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? 20\d\d)\b", text)),
    )
    return out


# ---------------------------------------------------------------- b), c) the bot's prompts


class CountedLlm(GeneralLlm):
    """One Gemini call per invoke, counted against the probe's budget; keeps token usage."""

    usage: list[dict] = []

    async def _mockable_direct_call_to_model(self, prompt):  # type: ignore[no-untyped-def]
        CallBudget.take()
        response = await super()._mockable_direct_call_to_model(prompt)
        CountedLlm.usage.append(
            {"model": self.model, "prompt_tokens": response.prompt_tokens_used, "answer_tokens": response.completion_tokens_used}
        )
        return response


class NoParser(GeneralLlm):
    """Read directly only: the parser is never called (no extra Gemini call)."""

    def __init__(self) -> None:
        super().__init__(model="probe/no-parser", temperature=None)

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        raise RuntimeError("probe: answer not readable directly (parser disabled)")


def _bot(llm: GeneralLlm) -> main.FallBot2026:
    parser = NoParser()
    bot = main.FallBot2026(
        llms={"default": llm, "parser": parser, "summarizer": parser, "researcher": "no_research"},
        publish_reports_to_metaculus=False,
        enable_summarize_research=False,
    )
    bot._structure_output_validation_samples = 1
    return bot


async def forecast_one(bot: main.FallBot2026, question: Any, research: str) -> dict:
    out: dict[str, Any] = {"post": question.id_of_post, "type": question.question_type}
    main.replay_question.set(question)
    try:
        if isinstance(question, BinaryQuestion):
            forecast = bot._run_forecast_on_binary(question, research)
        elif question.question_type == "multiple_choice":
            forecast = bot._run_forecast_on_multiple_choice(question, research)
        else:
            forecast = bot._run_forecast_on_numeric(question, research)
        prediction = await forecast
        out.update(status="ok", raw_output=prediction.reasoning, parsed=to_jsonable(prediction.prediction_value))
    except Exception as e:
        out.update(status=f"failed: {type(e).__name__}", error=str(e)[:500])
    record = bot._record_for(question)
    out["reading"] = record.get("reading", [])
    out["unread_replies"] = record.get("unread_replies", [])
    return out


def build_dossier(question: Any) -> str:
    """~6,000 tokens of real text, no model call: free news plus the question's
    own background, repeated if the news is short (a size test only)."""
    articles = collect_free_news(question.question_text)
    news = format_articles(articles, max_words=DOSSIER_WORDS)
    parts = [news, "Question background:\n" + (question.background_info or "")]
    text = "\n\n".join(parts)
    filler = "\n\n".join(p for p in parts if p.strip())
    while len(text.split()) < DOSSIER_WORDS and filler.strip():
        text += "\n\n(Repeated for the size test.)\n" + filler
    return " ".join(text.split()[:DOSSIER_WORDS])


# ---------------------------------------------------------------- flash: 503 probe

FLASH_PROBE = (
    ("gemini/gemini-3.6-flash", "high"),
    ("gemini/gemini-3.6-flash", "default"),
    ("gemini/gemini-3.8-flash", "high"),
    ("gemini/gemini-3.8-flash", "default"),
)
FLASH_PROBE_GAP_SECONDS = 20  # between calls (the free tier allows 5 a minute per model)


def live_ledger() -> QuotaLedger:
    import bot_config

    return QuotaLedger(
        make_store(bot_config.GEMINI_LEDGER_PATH),
        daily_limits=bot_config.GEMINI_DAILY_LIMITS,
        reserve_fraction=bot_config.GEMINI_FREE_RESERVE,
        buckets=bot_config.GEMINI_QUOTA_BUCKETS,
    )


def error_kind(error: BaseException) -> str:
    name = type(error).__name__
    return {"ServiceUnavailableError": "503", "RateLimitError": "429", "InternalServerError": "500",
            "Timeout": "timeout"}.get(name, name)


class LedgerTimedLlm(GeneralLlm):
    """One Gemini call, counted in the live quota ledger (non-reserve only),
    timed, its outcome kept."""

    def __init__(self, model: str, setting: str, ledger: QuotaLedger, outcome: dict) -> None:
        extra = {"reasoning_effort": "high"} if setting == "high" else {}
        super().__init__(model=model, temperature=None, timeout=300, allowed_tries=1, **extra)
        self._ledger = ledger
        self._outcome = outcome

    async def _mockable_direct_call_to_model(self, prompt):  # type: ignore[no-untyped-def]
        if not self._ledger.start(self.model, booked=False, allow_reserve=False):
            self._outcome["status"] = f"not called: {self._ledger.last_refusal.get(self.model)}"
            raise RuntimeError(self._outcome["status"])
        CallBudget.take()
        self._outcome["called_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.monotonic()
        succeeded = False
        try:
            response = await super()._mockable_direct_call_to_model(prompt)
            succeeded = True
            self._outcome.update(status="answered", answer_tokens=response.completion_tokens_used,
                                 prompt_tokens=response.prompt_tokens_used)
            return response
        except BaseException as e:
            self._outcome.update(status=error_kind(e), error=str(e)[:500])
            raise
        finally:
            self._outcome["seconds"] = round(time.monotonic() - started, 1)
            self._ledger.finish(self.model, succeeded)
            self._ledger.save()


def flash_probe(binary: Any, max_calls: int = len(FLASH_PROBE)) -> list[dict]:
    ledger = live_ledger()
    outcomes = []
    for i, (model, setting) in enumerate(FLASH_PROBE[:max_calls]):
        if i:
            time.sleep(FLASH_PROBE_GAP_SECONDS)
        outcome: dict[str, Any] = {"model": model, "setting": setting, "non_reserve_left_before": ledger.usable_left(model)}
        r = asyncio.run(forecast_one(_bot(LedgerTimedLlm(model, setting, ledger, outcome)), binary,
                                     "No research is available for this test."))
        outcome.update(forecast_status=r["status"], reading=r["reading"], raw_output=r.get("raw_output"))
        outcomes.append(outcome)
        print(
            f"flash) {model.split('/')[-1]} reasoning {setting}: {outcome.get('status')}"
            + (f" after {outcome['seconds']} s" if "seconds" in outcome else "")
            + (f", answer tokens {outcome['answer_tokens']}" if outcome.get("answer_tokens") is not None else "")
            + f" (UTC {outcome.get('called_at', '-')})"
        )
    return outcomes


# ---------------------------------------------------------------- run


def main_probe(part: str) -> int:
    configure_public_logging()
    started = datetime.now(timezone.utc)
    models = resolve_models()
    client = MetaculusClient()
    questions = main.pick_test_questions(client.get_all_open_questions_from_tournament(main.BOT_TESTING_AREA_ID))
    binary = next(q for q in questions if q.question_type == "binary")
    results: dict[str, Any] = {"started_at": started.isoformat(), "models": models, "questions": [q.id_of_post for q in questions]}

    if part in ("a", "all"):
        prompt = (
            "Find the most recent news relevant to this forecasting question and summarise it. "
            "For each source give its title, publisher and publish date.\n\nQuestion: "
            + binary.question_text
        )
        results["a_grounding"] = []
        for wanted in GROUNDING_MODELS:
            if wanted not in models:
                results["a_grounding"].append({"model": wanted, "error": "not listed for this key"})
                print(f"a) {wanted}: not listed for this key, no call made")
                continue
            r = grounded_query(models[wanted], prompt)
            results["a_grounding"].append(r)
            print(
                f"a) {wanted}: HTTP {r['http_status']}"
                + (f" {r['error']['status']}" if "error" in r else
                   f", cited sources {r['sources']}, search queries {len(r['search_queries'])}, "
                   f"date fields in sources: {'yes' if r['date_fields_in_sources'] else 'no'}, "
                   f"dates in text: {r['dates_in_text']}")
            )

    if part in ("b", "all"):
        results["b_gemini_3_flash"] = []
        if FORECAST_MODEL not in models:
            print(f"b) {FORECAST_MODEL}: not listed for this key, no call made")
        else:
            llm = CountedLlm(model=f"gemini/{models[FORECAST_MODEL]}", temperature=None, timeout=300, allowed_tries=1, reasoning_effort="high")
            bot = _bot(llm)
            for q in questions:
                r = asyncio.run(forecast_one(bot, q, "No research is available for this test."))
                results["b_gemini_3_flash"].append(r)
                print(f"b) {FORECAST_MODEL} on {q.id_of_post} ({q.question_type}): {r['status']}; reading {r['reading'] or ['none']}")

    if part in ("c", "all"):
        if GEMMA_MODEL not in models:
            print(f"c) {GEMMA_MODEL}: not listed for this key, no call made")
            results["c_gemma"] = {"error": "not listed for this key"}
        else:
            dossier = build_dossier(binary)
            llm = CountedLlm(model=f"gemini/{models[GEMMA_MODEL]}", temperature=None, timeout=300, allowed_tries=1)
            before = len(CountedLlm.usage)
            r = asyncio.run(forecast_one(_bot(llm), binary, dossier))
            r["dossier_words"] = len(dossier.split())
            r["usage"] = CountedLlm.usage[before:]
            results["c_gemma"] = r
            tokens = r["usage"][0] if r["usage"] else {}
            print(
                f"c) {GEMMA_MODEL} on {binary.id_of_post} (binary), dossier {r['dossier_words']} words: {r['status']}; "
                f"reading {r['reading'] or ['none']}; prompt tokens {tokens.get('prompt_tokens', '?')}, "
                f"answer tokens {tokens.get('answer_tokens', '?')}"
            )

    if part == "flash":
        results["flash_probe"] = flash_probe(binary)

    results["b_c_usage"] = CountedLlm.usage
    results["gemini_calls"] = CallBudget.used
    print(f"Gemini generate calls made: {CallBudget.used} (limit {MAX_GEMINI_CALLS}); nothing submitted")
    token = os.getenv("DATA_REPO_TOKEN")
    if token:
        path = f"probes/{started.date().isoformat()}/probe_{part}_{started.strftime('%H%M%S')}.json"
        GitHubFileStore(DATA_REPO, path, token).save(results, message=f"Probe {part}")
        print(f"Raw answers saved to fall26-data: {path}")
    else:
        print("DATA_REPO_TOKEN not set: raw answers not saved")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test-only probe of other free Gemini models")
    parser.add_argument("--part", choices=["a", "b", "c", "all", "flash"], default="all")
    sys.exit(main_probe(parser.parse_args().part))
