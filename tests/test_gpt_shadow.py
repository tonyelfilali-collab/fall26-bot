"""The GPT shadow (gpt_shadow.py + main._plan_gpt_shadows / _gpt_shadow_at_end):
built OFF, never submitted, its own caps in the shared spend ledger. Fake
replies only (0 real calls)."""
from __future__ import annotations

import asyncio
import copy
import logging
import time

from types import SimpleNamespace

import pytest
from forecasting_tools.ai_models.ai_utils.response_types import TextTokenCostResponse

import bot_config
import gpt_shadow as gpt
import main
import replay
from gemini_budget import MemoryStore
from question_log import shadows_path
from spend import SpendGuard, utc_day
from tests.test_replay import QUESTIONS

TYPES = ("binary", "numeric", "discrete", "multiple_choice")


class CreditError(Exception):
    status_code = 402


class FakeGpt(gpt.GptShadowLlm):
    """The OpenRouter call replaced: a recorded reply (binary: 97%, unlike live) and its usage."""

    mode = "ok"
    cost = 0.07
    requests: list = []
    urls: list | None = None  # cited URLs (None: no response object at all)

    async def _raw_call(self, prompt):
        FakeGpt.requests.append({"prompt": prompt, "extra_body": self.litellm_kwargs.get("extra_body"),
                                 "max_tokens": self.litellm_kwargs.get("max_tokens"), "model": self.model})
        if FakeGpt.mode == "credit":
            raise CreditError("credit balance too low")
        if FakeGpt.mode == "slow":
            await asyncio.sleep(5)
        question = replay.current_question.get()
        text = replay.recorded_reply(question)
        if question.question_type == "binary":
            text = "Searched twice.\nProbability: 97%"
        if FakeGpt.mode == "unreadable":
            text = "No numbers from me."
        cost = 0.0 if FakeGpt.mode == "unknown" else FakeGpt.cost
        usage = {"prompt_tokens": 9000, "completion_tokens": 3000, "cost": cost, "server_tool_use": {"web_search_requests": 2}}
        if FakeGpt.mode == "no_tool_use":  # as in the 22 Oct Test Bot: no server_tool_use in the usage
            usage.pop("server_tool_use")
        # LiteLLM's ModelResponse: choices[0].message.annotations (url_citation dicts, OpenRouter's shape).
        urls = FakeGpt.urls
        self._raw = None if urls is None else SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            annotations=[{"type": "url_citation", "url_citation": {"url": u, "title": "t", "content": "secret text"}} for u in urls]))])
        return TextTokenCostResponse(data=text, prompt_tokens_used=9000, completion_tokens_used=3000,
                                     total_tokens_used=12000, model=self.model, cost=cost), usage


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeGpt.mode, FakeGpt.cost, FakeGpt.requests, FakeGpt.urls = "ok", 0.07, [], None
    yield


class Recorder:
    def __init__(self):
        self.files: dict[str, dict] = {}

    def save(self, path, record):
        self.files[path] = copy.deepcopy(record)
        return True


def _spend(gpt_day: float = 0.0, other_day: float = 0.0) -> SpendGuard:
    spend = SpendGuard(MemoryStore({"days": {}}))
    if gpt_day or other_day:
        spend.days[utc_day()] = gpt_day + other_day
    if gpt_day:
        spend.gpt_shadow_days[utc_day()] = gpt_day
    return spend


def _shadow(spend=None, key_left=10.0, enabled=True) -> gpt.GptShadow:
    return gpt.GptShadow(_spend() if spend is None else spend, key_left, enabled=enabled, llm_class=FakeGpt)


def _bot(shadow=None, seasonal_by_post=None):
    lineup = bot_config.get_lineup("replay")
    bot = main.FallBot2026(llms=lineup.llms, publish_reports_to_metaculus=False, enable_summarize_research=False,
                           predictions_per_research_report=1)
    bot.keep_records = True
    bot.question_log = Recorder()
    bot.shadow_llm = replay.ReplayLlm()
    bot.gpt_shadow = shadow
    if seasonal_by_post:
        bot.seasonal_by_post = seasonal_by_post
    return bot


def _run(bot, questions=QUESTIONS, submitted=None):
    if submitted is not None:
        async def submit(self, question, report):
            submitted[question.question_type] = main.to_jsonable(report.prediction)
            return True

        bot._submit_if_still_open = submit.__get__(bot)
    reports = asyncio.run(bot.forecast_questions(questions, return_exceptions=True))
    assert not any(isinstance(r, BaseException) for r in reports), reports
    records = {r["question"]["question_type"]: r for r in bot.__dict__["kept_records"]}
    files = {f["question"]["question_type"]: f for p, f in bot.question_log.files.items() if p.endswith("_shadows.json")}
    return records, files


# ---------------------------------------------------------------- switch


def test_switched_on_live_and_only_this_exact_paid_model(monkeypatch):
    assert bot_config.GPT_SHADOW_ENABLED is True  # ON (architect pre-approval, 22 Oct)
    assert bot_config.GPT_SHADOW_MODEL == "openrouter/openai/gpt-6.1-sol"
    assert not bot_config.is_free_model(bot_config.GPT_SHADOW_MODEL)
    # No lineup carries it (the lineup guard still refuses every other paid model).
    for name in ("free", "gemini-free", "frontier", "replay", "replay-gemini"):
        assert bot_config.GPT_SHADOW_MODEL not in bot_config.get_lineup(name).llm_model_names()
        assert bot_config.GPT_SHADOW_MODEL not in bot_config.get_lineup(name).allowed_paid
    # The switch read from bot_config: off -> no reservation.
    monkeypatch.setattr(bot_config, "GPT_SHADOW_ENABLED", False)
    assert gpt.GptShadow(_spend(), 10.0).reserve("1", True) == gpt.OFF


def test_off_run_makes_no_call_and_notes_it_privately():
    records, files = _run(_bot(None))
    assert FakeGpt.requests == []
    for kind in TYPES:
        assert files[kind]["gpt_shadow"]["status"] == gpt.OFF
        assert gpt.VARIANT not in files[kind]["shadow"]
        assert files[kind]["shadow_model"]["status"] == "ok"  # the free shadow as before


def test_test_bot_input_passes_the_flag():
    text = open(".github/workflows/test_bot.yaml").read()
    assert "gpt_shadow:" in text and 'if [ "$GPT_SHADOW" = "true" ]; then args+=(--gpt-shadow); fi' in text
    assert "GPT_SHADOW: ${{ inputs.gpt_shadow }}" in text
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in text
    source = open("main.py").read()
    assert '"--gpt-shadow"' in source and "bot_config.GPT_SHADOW_ENABLED or args.gpt_shadow" in source


# ---------------------------------------------------------------- on: every type, never submitted


def test_on_saves_every_type_privately_and_never_changes_the_submission(caplog):
    without: dict = {}
    _run(_bot(None), submitted=without)
    spend = _spend()
    shadow = _shadow(spend)
    with_gpt: dict = {}
    with caplog.at_level(logging.INFO, logger="fall26"):
        records, files = _run(_bot(shadow), submitted=with_gpt)
    assert with_gpt == without  # never submitted, never changes the live forecast
    assert len(FakeGpt.requests) == 4  # one call per question
    for kind in TYPES:
        entry = files[kind]["gpt_shadow"]
        assert entry["status"] == gpt.SAVED and entry["model"] == "openai/gpt-6.1-sol"
        assert entry["cost"] == pytest.approx(0.07) and entry["searches"] == 2
        assert entry["input_tokens"] == 9000 and entry["output_tokens"] == 3000 and entry["reserve"] == gpt.RESERVATION
        assert entry["reading"] == ["direct"] and "value" not in entry
        assert gpt.VARIANT in files[kind]["shadow"]
        assert "gpt_shadow" not in records[kind] and "gpt_shadow_reading" not in records[kind]
        assert gpt.VARIANT not in (records[kind].get("shadow") or {})  # the question's own log: untouched
    assert files["binary"]["shadow"][gpt.VARIANT] == pytest.approx(0.97)
    assert records["binary"]["final_forecast"] != pytest.approx(0.97)
    # The same prompt as the main forecasters, the dossier included, plus the search note.
    request = FakeGpt.requests[0]
    assert request["model"] == bot_config.GPT_SHADOW_MODEL and request["max_tokens"] == gpt.MAX_OUTPUT_TOKENS
    assert request["extra_body"]["plugins"] == [{"id": "web", "engine": "native", "max_results": 5}]
    assert request["extra_body"]["max_tool_calls"] == 3 and request["extra_body"]["reasoning"] == {"effort": "medium"}
    assert replay.REPLAY_RESEARCH[:40] in str(request["prompt"]) and "at most 3 searches" in str(request["prompt"])
    # Ledger: the same spend ledger, its own day share; paid Flash's day total unchanged.
    calls = [c for q in ("1", "2", "3", "4") for c in spend.questions[q]["calls"]]
    assert all(c["model"] == "openai/gpt-6.1-sol" and c["shadow"] == "gpt" and c["charged"] == pytest.approx(0.07)
               and c["searches"] == 2 for c in calls) and len(calls) == 4
    assert spend.day_spent() == pytest.approx(0.28) and spend.gpt_shadow_day_spent() == pytest.approx(0.28)
    assert spend.flash_day_spent() == pytest.approx(0.0) and spend.questions["1"]["spent"] == 0.0
    assert spend.questions["1"]["gpt_shadow"] == pytest.approx(0.07)
    assert spend.snapshot()["gpt_shadow_days"] == {utc_day(): pytest.approx(0.28)}
    assert spend.gpt_shadow_in_flight == {"1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0}
    # Public log: status and cost only, never the value.
    public = [r.getMessage() for r in caplog.records if r.name == "fall26" and "GPT shadow" in r.getMessage()]
    assert "Question 1: GPT shadow saved, $0.0700" in public and len(public) == 4
    assert not any("97" in line or "0.97" in line for line in public)
    assert "saved 4" in shadow.summary() and "$0.2800" in shadow.summary()


def test_scoreboard_scores_the_gpt_shadow_per_type_and_per_model():
    import scoreboard

    files = {
        "q/1.json": {"question": {"id_of_post": 1, "question_type": "binary"}, "final_forecast": 0.6, "submitted": True,
                     "forecasts": [{"status": "ok", "parsed": 0.6, "forecast_model": "gemini/gemini-3.6-flash"}]},
        "q/1_shadows.json": {"gpt_shadow": {"model": "openai/gpt-6.1-sol", "status": "saved"},
                             "shadow": {gpt.VARIANT: 0.8}},
    }
    [record] = scoreboard.merge_shadow_files(files)
    assert record["gpt_shadow"]["status"] == "saved" and record["shadow"][gpt.VARIANT] == 0.8
    report = scoreboard.report_markdown(scoreboard.score_records([record], {1: "yes"}.get))
    assert f"| binary | {gpt.VARIANT} | 1 |" in report
    [row] = scoreboard.score_models([record], {1: "yes"}.get)
    names = [m for m, _, _ in row["models"]]
    assert scoreboard.GPT_SHADOW_NAME in names and "gemini/gemini-3.6-flash" in names
    assert "flash-only" in row["subsets"] and row["subsets"]["flash-only"] == scoreboard.forecast_score("binary", 0.6, "yes")
    assert scoreboard.model_group(scoreboard.GPT_SHADOW_NAME) == "other"
    assert scoreboard.GPT_SHADOW_VARIANT == gpt.VARIANT


def test_frontier_report_counts_the_gpt_shadow_apart():
    import frontier

    spend = _spend()
    planner = frontier._frontier_pool(MemoryStore(), spend, None)
    reservation = _shadow(spend).reserve("7", True)
    reservation.settle(ok=True, cost=0.05, seconds=30.0, searches=3, input_tokens=8000, output_tokens=2000)
    spend.questions.setdefault("7", {})["calls"].append({"model": "google/gemini-3.6-flash", "ok": True, "charged": 0.02})
    report = frontier.frontier_report(planner)
    assert "| OpenRouter (GPT shadow) | openai/gpt-6.1-sol | 1 | 1 |" in report and "$0.0500" in report
    assert "| OpenRouter | google/gemini-3.6-flash | 1 | 1 |" in report
    assert "| OpenRouter (GPT shadow) | 7 | $0.0500 |" in report and "| OpenRouter | 7 | $0.0200 |" in report


# ---------------------------------------------------------------- caps


def test_reservation_arithmetic():
    # 60k input x $2/M + 10k output x $10/M + 3 searches x $0.01 = 0.12 + 0.10 + 0.03
    assert gpt.RESERVATION == pytest.approx(0.25)
    assert gpt.RESERVATION <= bot_config.GPT_SHADOW_QUESTION_CAP


def test_question_cap():
    spend = _spend()
    spend.questions["5"] = {"spent": 0.0, "gpt_shadow": 0.01}
    assert _shadow(spend).reserve("5", True) == gpt.QUESTION_CAP
    shadow = _shadow()
    assert not isinstance(shadow.reserve("6", True), str)
    assert shadow.reserve("6", True) == gpt.QUESTION_CAP  # one $0.25 reservation per question


@pytest.mark.parametrize("day, seasonal, expected", [
    (1.25, True, None), (1.26, True, gpt.DAY_CAP), (1.30, False, gpt.DAY_CAP),
    (0.75, False, None), (0.76, False, gpt.MINIBENCH_PRIORITY), (0.76, None, gpt.MINIBENCH_PRIORITY),
    (1.00, True, None),
])
def test_day_cap_and_minibench_under_one_dollar(day, seasonal, expected):
    result = _shadow(_spend(gpt_day=day)).reserve("9", seasonal)
    assert (None if not isinstance(result, str) else result) == expected


def test_running_reservations_count_in_the_day():
    shadow = _shadow(_spend(gpt_day=0.50))
    assert not isinstance(shadow.reserve("a", True), str)  # 0.75 committed
    assert not isinstance(shadow.reserve("b", False), str)  # 1.00: MiniBench just fits
    assert shadow.reserve("c", False) == gpt.MINIBENCH_PRIORITY
    assert not isinstance(shadow.reserve("d", True), str)  # 1.25
    assert not isinstance(shadow.reserve("e", True), str)  # 1.50
    assert shadow.reserve("f", True) == gpt.DAY_CAP


def test_gpt_shadow_spend_never_eats_the_paid_flash_caps():
    spend = _spend(gpt_day=1.40, other_day=0.20)
    spend.daily_cap, spend.minibench_daily_cap = 1.00, 0.50
    spend.estimates["google/gemini-3.6-flash"] = 0.03
    from gemini_budget import current_question_key
    from spend import question_seasonal

    current_question_key.set("x")
    question_seasonal.set(True)
    assert spend.flash_day_spent() == pytest.approx(0.20)
    assert spend.refusal("openrouter/google/gemini-3.6-flash") is None
    question_seasonal.set(False)
    assert spend.refusal("openrouter/google/gemini-3.6-flash") is None  # 0.23 <= 0.50


def test_seasonal_first_in_the_run():
    """MiniBench question first in the run, the day already at $0.75: the
    seasonal question is reserved first (to $1.00), so MiniBench is skipped."""
    spend = _spend(gpt_day=0.75)
    questions = [QUESTIONS[0], QUESTIONS[3]]  # post 1 (MiniBench here), post 4 (seasonal)
    records, files = _run(_bot(_shadow(spend), seasonal_by_post={1: False, 4: True}), questions)
    assert files["multiple_choice"]["gpt_shadow"]["status"] == gpt.SAVED
    assert files["binary"]["gpt_shadow"]["status"] == gpt.MINIBENCH_PRIORITY
    assert files["binary"]["gpt_shadow"]["cost"] == 0.0 and len(FakeGpt.requests) == 1
    assert records["binary"]["seasonal"] is False and records["multiple_choice"]["seasonal"] is True


def test_minibench_runs_when_the_day_is_low():
    records, files = _run(_bot(_shadow(), seasonal_by_post={1: False}), [QUESTIONS[0]])
    assert files["binary"]["gpt_shadow"]["status"] == gpt.SAVED


@pytest.mark.parametrize("key_left, expected", [(2.24, gpt.KEY_LOW), (1.50, gpt.KEY_LOW), (None, gpt.KEY_UNKNOWN), (2.25, None)])
def test_key_under_two_dollars(key_left, expected):
    result = _shadow(key_left=key_left).reserve("k", True)
    assert (None if not isinstance(result, str) else result) == expected


def test_key_check_counts_this_runs_spend():
    spend = _spend()
    shadow = _shadow(spend, key_left=2.60)
    spend.days[utc_day()] = 0.20  # paid Flash spent $0.20 since the run started: $2.40 left
    assert not isinstance(shadow.reserve("a", True), str)  # 2.40 - 0.25 = 2.15 left
    assert shadow.reserve("b", True) == gpt.KEY_LOW  # 2.15 - 0.25 < 2


def test_ledger_unreadable_or_blocked_means_none():
    assert _shadow(SpendGuard(MemoryStore(None))).reserve("a", True) == gpt.LEDGER
    blocked = _spend()
    blocked.blocked = "OpenRouter key usage could not be read"
    assert _shadow(blocked).reserve("a", True) == gpt.BLOCKED
    assert gpt.GptShadow(None, 10.0, enabled=True).reserve("a", True) == gpt.LEDGER


# ---------------------------------------------------------------- failures


def test_credit_error_is_not_retried_and_stops_the_gpt_shadow(caplog):
    FakeGpt.mode = "credit"
    spend = _spend()
    shadow = _shadow(spend)
    records, files = _run(_bot(shadow), [QUESTIONS[0]])
    assert len(FakeGpt.requests) == 1  # one try, no retry
    entry = files["binary"]["gpt_shadow"]
    assert entry["status"] == "failed (CreditError)" and entry["cost"] == 0.0  # 402: never billed
    [call] = spend.questions["1"]["calls"]
    assert call["ok"] is False and call["http_status"] == 402 and call["charged"] == 0.0
    assert shadow.off == "credit error" and shadow.reserve("2", True) == "skipped: credit error"
    assert records["binary"]["final_forecast"] is not None


def test_timeout_is_charged_the_reservation(monkeypatch):
    monkeypatch.setattr(gpt, "TIMEOUT_SECONDS", 0.2)
    FakeGpt.mode = "slow"
    spend = _spend()
    records, files = _run(_bot(_shadow(spend)), [QUESTIONS[0]])
    entry = files["binary"]["gpt_shadow"]
    assert entry["status"] == gpt.TIMED_OUT and entry["cost"] == pytest.approx(gpt.RESERVATION)
    assert spend.gpt_shadow_day_spent() == pytest.approx(gpt.RESERVATION) == spend.day_spent()
    assert gpt.VARIANT not in files["binary"]["shadow"] and records["binary"]["final_forecast"] is not None
    assert spend.gpt_shadow_in_flight["1"] == 0.0


def test_unknown_cost_is_charged_the_reservation():
    FakeGpt.mode = "unknown"
    spend = _spend()
    shadow = _shadow(spend)
    _, files = _run(_bot(shadow), [QUESTIONS[0]])
    assert files["binary"]["gpt_shadow"]["cost"] == pytest.approx(gpt.RESERVATION)
    assert shadow.unknown_cost_calls == 1 and spend.unknown_cost_calls == 1
    [call] = spend.questions["1"]["calls"]
    assert call["cost_known"] is False


def test_unreadable_answer_is_no_answer_without_a_parser_call(monkeypatch):
    async def parser_must_not_run(*args, **kwargs):
        raise AssertionError("the parser model was called for the GPT shadow")

    monkeypatch.setattr(main, "structure_output", parser_must_not_run)
    FakeGpt.mode = "unreadable"
    records, files = _run(_bot(_shadow()), [QUESTIONS[0]])
    entry = files["binary"]["gpt_shadow"]
    assert entry["status"] == gpt.NO_ANSWER and entry["reading"] == ["dropped"]
    assert entry["cost"] == pytest.approx(0.07)  # answered: its real cost
    assert files["binary"]["shadow_model"]["reading"] == ["direct"]  # the free shadow's own reading


def test_call_over_its_reservation_switches_it_off_and_alerts():
    FakeGpt.cost = 0.40
    spend = _spend()
    shadow = _shadow(spend)
    _, files = _run(_bot(shadow), [QUESTIONS[0]])
    assert files["binary"]["gpt_shadow"]["cost"] == pytest.approx(0.40)  # charged in full
    assert shadow.off == "over its reservation" and shadow.overruns == 1
    alerts = []
    assert gpt.overrun_alert(shadow, lambda title, body: alerts.append(title)) and len(alerts) == 1
    assert gpt.overrun_alert(shadow, lambda title, body: alerts.append(title)) and len(alerts) == 1  # once a day


def test_after_minute_45_skipped_time_and_no_call():
    bot = _bot(_shadow())
    bot.run_started = time.monotonic() - 46 * 60
    main.START_CUTOFF_SECONDS, cutoff = 3600, main.START_CUTOFF_SECONDS
    try:
        _, files = _run(bot, [QUESTIONS[0]])
    finally:
        main.START_CUTOFF_SECONDS = cutoff
    assert files["binary"]["gpt_shadow"]["status"] == gpt.TIME and FakeGpt.requests == []


def test_one_call_per_llm_and_release_without_a_call():
    shadow = _shadow()
    reservation = shadow.reserve("r", True)
    reservation.release()
    assert shadow.spend.gpt_shadow_in_flight["r"] == 0.0 and shadow.spend.day_spent() == 0.0
    llm = shadow.make_llm(shadow.reserve("s", True), 10)

    async def twice():
        replay.current_question.set(QUESTIONS[0])
        await llm._mockable_direct_call_to_model("x")
        with pytest.raises(RuntimeError, match="one call per question"):
            await llm._mockable_direct_call_to_model("x")

    asyncio.run(twice())
    assert len(FakeGpt.requests) == 1 and len(shadow.spend.questions["s"]["calls"]) == 1
    assert shadows_path("questions/tournament/x/1.json").endswith("_shadows.json")


# ---------------------------------------------------------------- metaculus.com (architect, 22 Oct)


def test_system_note_says_do_not_use_metaculus():
    assert "Do not use metaculus.com." in gpt.SEARCH_NOTE and "at most 3 searches" in gpt.SEARCH_NOTE
    _run(_bot(_shadow()), [QUESTIONS[0]])
    assert "Do not use metaculus.com." in str(FakeGpt.requests[0]["prompt"])


def test_cited_domains_shapes():
    def raw(annotations):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(annotations=annotations))])

    cite = lambda u: {"type": "url_citation", "url_citation": {"url": u}}  # noqa: E731
    assert gpt.cited_domains(None) is None
    assert gpt.cited_domains(raw(None)) == []  # no annotations
    assert gpt.cited_domains(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace())])) == []
    assert gpt.cited_domains(raw([cite("https://www.Reuters.com/a?b=1"), cite("https://reuters.com/x"),
                                  cite("http://bls.gov/cpi"), cite("not a url"), {"type": "url_citation"}, "junk"])) == [
        "bls.gov", "reuters.com"]
    # dict-shaped responses work too
    assert gpt.cited_domains({"choices": [{"message": {"annotations": [cite("https://a.org/1")]}}]}) == ["a.org"]
    assert gpt.citation_numbers(raw([cite("https://www.metaculus.com/questions/1")]))["metaculus_cited"] is True
    assert gpt.citation_numbers(raw([cite("https://forum.metaculus.com/x")]))["metaculus_cited"] is True
    assert gpt.citation_numbers(raw([cite("https://notmetaculus.com/x")]))["metaculus_cited"] is False
    assert gpt.citation_numbers(None) == {}


@pytest.mark.parametrize("urls, cited", [
    (["https://www.reuters.com/a", "https://reuters.com/b", "https://bls.gov/c"], False),
    (["https://www.metaculus.com/questions/1/", "https://bls.gov/c"], True),
])
def test_domains_in_the_private_file_count_and_flag_in_the_public_log(caplog, urls, cited):
    FakeGpt.urls = urls
    with caplog.at_level(logging.INFO, logger="fall26"):
        _, files = _run(_bot(_shadow()), [QUESTIONS[0]])
    entry = files["binary"]["gpt_shadow"]
    domains = ["bls.gov", "metaculus.com"] if cited else ["bls.gov", "reuters.com"]
    assert entry["domains"] == domains and entry["domains_count"] == 2 and entry["metaculus_cited"] is cited
    assert "secret text" not in str(entry) and not any("/" in d for d in entry["domains"])  # domains only
    [line] = [r.getMessage() for r in caplog.records if r.name == "fall26" and "GPT shadow saved" in r.getMessage()]
    assert line == f"Question 1: GPT shadow saved, $0.0700, domains 2, metaculus cited: {'yes' if cited else 'no'}"
    assert not any(d in line for d in domains)


def test_scoreboard_scores_the_gpt_shadow_with_and_without_metaculus():
    import scoreboard

    def files(post, cited):
        extra = {"gpt_shadow": {"status": "saved"}, "shadow": {gpt.VARIANT: 0.8}}
        if cited is not None:
            extra["gpt_shadow"]["metaculus_cited"] = cited
        return {f"q/{post}.json": {"question": {"id_of_post": post, "question_type": "binary"}, "final_forecast": 0.6},
                f"q/{post}_shadows.json": extra}

    every = {**files(1, False), **files(2, True), **files(3, None)}
    records = scoreboard.merge_shadow_files(every)
    resolution = {1: "yes", 2: "yes", 3: "yes"}.get
    report = scoreboard.report_markdown(scoreboard.score_records(records, resolution))
    assert f"| binary | {gpt.VARIANT} | 3 |" in report
    assert f"| binary | {scoreboard.GPT_SHADOW_NO_METACULUS} | 1 |" in report  # cited and unknown left out
    names = [m for row in scoreboard.score_models(records, resolution) for m, _, _ in row["models"]]
    assert names.count(scoreboard.GPT_SHADOW_NAME) == 3 and names.count(scoreboard.GPT_SHADOW_NAME_NO_METACULUS) == 1


# ---------------------------------------------------------------- ledger saved twice (22 Oct Test Bot 39791878519)


class JsonStore(MemoryStore):
    """Like the data repo: each load is a fresh copy of what was last saved."""

    def load(self):
        import json

        return None if self.data is None else json.loads(json.dumps(self.data))


def _run_with_store(store):
    spend = SpendGuard(store)
    _run(_bot(_shadow(spend)), [QUESTIONS[0], QUESTIONS[3]])  # two seasonal GPT shadows, $0.07 each
    spend.save()
    return spend


def test_two_saves_at_the_end_of_a_run_keep_the_gpt_shadow_days():
    store = JsonStore({"days": {}, "future_field": {"x": 1}})
    _run_with_store(store)
    assert store.data["gpt_shadow_days"] == {utc_day(): pytest.approx(0.14)}
    # A second writer (another SpendGuard: the planner's, or the next run) loads and saves again.
    second = SpendGuard(store)
    second.days[utc_day()] += 0.03  # e.g. a paid Flash call
    second.save()
    assert store.data["gpt_shadow_days"] == {utc_day(): pytest.approx(0.14)}
    assert store.data["future_field"] == {"x": 1}  # fields it doesn't know are kept too
    again = SpendGuard(store)
    assert again.gpt_shadow_day_spent() == pytest.approx(0.14) and again.flash_day_spent() == pytest.approx(0.03)


def test_a_writer_that_drops_gpt_shadow_days_cannot_reset_the_caps():
    """What happened on 22 Oct: a live run on older code (no gpt_shadow_days)
    rewrote the ledger right after the Test Bot. Its `questions` (with the GPT
    shadow calls and their day) are saved as they are, so the caps still hold."""
    store = JsonStore({"days": {}})
    _run_with_store(store)
    old = store.load()
    del old["gpt_shadow_days"]  # the older writer's snapshot
    store.save(old)
    fresh = SpendGuard(store)
    assert "gpt_shadow_days" not in store.data
    assert fresh.gpt_shadow_day_spent() == pytest.approx(0.14)
    assert fresh.flash_day_spent() == pytest.approx(0.0) and fresh.day_spent() == pytest.approx(0.14)
    fresh.gpt_shadow_days.clear()
    fresh.questions["x"] = {"spent": 0.0, "calls": [{"shadow": "gpt", "day": utc_day(), "charged": 1.20}]}
    assert _shadow(fresh).reserve("y", True) == gpt.DAY_CAP  # 1.34 + 0.25 > 1.50, from the calls alone
    assert _shadow(fresh).reserve("y", False) == gpt.DAY_CAP


def test_searches_estimated_when_openrouter_does_not_report_them():
    # (0.07 - (9000 x $2 + 3000 x $10) / 1M) / 0.01 = (0.07 - 0.048) / 0.01 = 2.2 -> 2
    assert gpt.estimate_searches(0.07, 9000, 3000) == 2
    assert gpt.estimate_searches(0.04, 9000, 3000) == 0  # never negative
    assert gpt.estimate_searches(None, 9000, 3000) is None and gpt.estimate_searches(0.07, None, 3000) is None
    FakeGpt.mode = "no_tool_use"
    spend = _spend()
    _, files = _run(_bot(_shadow(spend)), [QUESTIONS[0]])
    entry = files["binary"]["gpt_shadow"]
    assert entry["searches"] is None and entry["searches_estimated"] == 2
    [call] = spend.questions["1"]["calls"]
    assert call["searches"] is None and "searches_estimated" not in call  # private shadows file only
    FakeGpt.mode = "ok"
    _, files = _run(_bot(_shadow()), [QUESTIONS[0]])
    assert files["binary"]["gpt_shadow"]["searches"] == 2 and "searches_estimated" not in files["binary"]["gpt_shadow"]
