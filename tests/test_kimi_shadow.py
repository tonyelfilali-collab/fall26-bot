"""The Kimi shadow (kimi_shadow.py on the GPT shadow's plumbing): built OFF,
pinned to one provider, Exa search at most 3, metaculus.com excluded, never
submitted, its own caps in the shared spend ledger. Fake replies only (0 real calls)."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from forecasting_tools.ai_models.ai_utils.response_types import TextTokenCostResponse

import bot_config
import gpt_shadow as gpt
import kimi_shadow as kimi
import main
import replay
from spend import SpendGuard, utc_day
from gemini_budget import MemoryStore
from tests.test_gpt_shadow import FakeGpt, TYPES, _bot, _run, _shadow as _gpt_shadow, _spend
from tests.test_replay import QUESTIONS


class CreditError(Exception):
    status_code = 402


class FakeKimi(kimi.KimiShadowLlm):
    """The OpenRouter call replaced: a recorded reply (binary: 91%, unlike live) and its usage."""

    mode = "ok"
    cost = 0.09
    requests: list = []
    urls: list | None = None

    async def _raw_call(self, prompt):
        FakeKimi.requests.append({"prompt": prompt, "extra_body": self.litellm_kwargs.get("extra_body"),
                                  "max_tokens": self.litellm_kwargs.get("max_tokens"), "model": self.model})
        if FakeKimi.mode == "credit":
            raise CreditError("credit balance too low")
        question = replay.current_question.get()
        text = replay.recorded_reply(question)
        if question.question_type == "binary":
            text = "Searched three times.\nProbability: 91%"
        cost = FakeKimi.cost
        usage = {"prompt_tokens": 60000, "completion_tokens": 5000, "cost": cost, "server_tool_use": {"web_search_requests": 3}}
        urls = FakeKimi.urls
        self._raw = None if urls is None else SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            annotations=[{"type": "url_citation", "url_citation": {"url": u, "content": "secret text"}} for u in urls]))])
        return TextTokenCostResponse(data=text, prompt_tokens_used=60000, completion_tokens_used=5000,
                                     total_tokens_used=65000, model=self.model, cost=cost), usage


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeKimi.mode, FakeKimi.cost, FakeKimi.requests, FakeKimi.urls = "ok", 0.09, [], None
    FakeGpt.mode, FakeGpt.cost, FakeGpt.requests, FakeGpt.urls = "ok", 0.07, [], None
    yield


def _shadow(spend=None, key_left=10.0, enabled=True) -> kimi.KimiShadow:
    return kimi.KimiShadow(_spend() if spend is None else spend, key_left, enabled=enabled, llm_class=FakeKimi)


def _kimi_bot(shadow, gpt_shadow=None, seasonal_by_post=None):
    bot = _bot(gpt_shadow, seasonal_by_post)
    bot.kimi_shadow = shadow
    return bot


# ---------------------------------------------------------------- switch and request


def test_built_off_and_only_this_exact_paid_model(monkeypatch):
    assert bot_config.KIMI_SHADOW_ENABLED is False  # OFF until the Test Bot gate passes
    assert bot_config.KIMI_SHADOW_MODEL == "openrouter/moonshotai/kimi-k3"
    assert not bot_config.is_free_model(bot_config.KIMI_SHADOW_MODEL)
    for name in ("free", "gemini-free", "frontier", "replay", "replay-gemini"):
        assert bot_config.KIMI_SHADOW_MODEL not in bot_config.get_lineup(name).llm_model_names()
        assert bot_config.KIMI_SHADOW_MODEL not in bot_config.get_lineup(name).allowed_paid
    assert kimi.KimiShadow(_spend(), 10.0).reserve("1", True) == gpt.OFF
    monkeypatch.setattr(bot_config, "KIMI_SHADOW_ENABLED", True)
    assert not isinstance(kimi.KimiShadow(_spend(), 10.0).reserve("1", True), str)


def test_caps_are_the_gpt_shadows():
    assert bot_config.KIMI_SHADOW_QUESTION_CAP == bot_config.GPT_SHADOW_QUESTION_CAP == 0.25
    assert bot_config.KIMI_SHADOW_DAILY_CAP == bot_config.GPT_SHADOW_DAILY_CAP == 1.50
    assert bot_config.KIMI_SHADOW_MINIBENCH_DAILY_CAP == 1.00 and bot_config.KIMI_SHADOW_MIN_KEY_LEFT == 2.00
    # 400k input x $0.17/M + 16k output x $9.75/M + 3 searches x $0.007 = 0.068 + 0.156 + 0.021
    assert kimi.RESERVATION == pytest.approx(0.245) and kimi.RESERVATION <= bot_config.KIMI_SHADOW_QUESTION_CAP


def test_request_pins_one_provider_and_exa_search_without_metaculus():
    extras = kimi.request_extras()
    assert extras["provider"] == {"order": [bot_config.KIMI_SHADOW_PROVIDER], "allow_fallbacks": False}
    assert bot_config.KIMI_SHADOW_PROVIDER == "relace/fp4"
    [tool] = extras["tools"]
    assert tool["type"] == "openrouter:web_search"
    params = tool["parameters"]
    assert params["engine"] == "exa" and params["max_uses"] == 3 and params["max_results"] == 5
    assert params["max_total_results"] == 15 and params["excluded_domains"] == ["metaculus.com"]
    assert extras["max_tool_calls"] == 3 and extras["reasoning"] == {"effort": "medium"}
    assert "plugins" not in extras  # the server tool, not the (deprecated) web plugin
    assert "Do not use metaculus.com." in kimi.SEARCH_NOTE and "at most 3 searches" in kimi.SEARCH_NOTE
    assert kimi.SEARCH_NOTE.startswith("Before answering, search the web: make 2 to 3 searches")


def test_test_bot_input_passes_the_flag():
    text = open(".github/workflows/test_bot.yaml").read()
    assert "kimi_shadow:" in text and 'if [ "$KIMI_SHADOW" = "true" ]; then args+=(--kimi-shadow); fi' in text
    assert "KIMI_SHADOW: ${{ inputs.kimi_shadow }}" in text and "inputs.gpt_shadow || inputs.kimi_shadow" in text
    source = open("main.py").read()
    assert '"--kimi-shadow"' in source and "bot_config.KIMI_SHADOW_ENABLED or args.kimi_shadow" in source


# ---------------------------------------------------------------- on: every type, never submitted


def test_on_saves_every_type_privately_and_never_changes_the_submission(caplog):
    without: dict = {}
    _run(_bot(None), submitted=without)
    spend = _spend()
    shadow = _shadow(spend)
    with_kimi: dict = {}
    with caplog.at_level(logging.INFO, logger="fall26"):
        records, files = _run(_kimi_bot(shadow), submitted=with_kimi)
    assert with_kimi == without  # never submitted, never changes the live forecast
    assert len(FakeKimi.requests) == 4 and FakeGpt.requests == []
    for kind in TYPES:
        entry = files[kind]["kimi_shadow"]
        assert entry["status"] == gpt.SAVED and entry["model"] == "moonshotai/kimi-k3"
        assert entry["provider"] == "relace/fp4" and entry["searches"] == 3
        assert entry["cost"] == pytest.approx(0.09) and entry["reserve"] == kimi.RESERVATION
        assert entry["reading"] == ["direct"] and "value" not in entry
        assert kimi.VARIANT in files[kind]["shadow"] and gpt.VARIANT not in files[kind]["shadow"]
        assert files[kind]["gpt_shadow"]["status"] == gpt.OFF  # GPT shadow off: its entry says so
        assert "kimi_shadow" not in records[kind] and "kimi_shadow_reading" not in records[kind]
    assert files["binary"]["shadow"][kimi.VARIANT] == pytest.approx(0.91)
    request = FakeKimi.requests[0]
    assert request["model"] == bot_config.KIMI_SHADOW_MODEL and request["max_tokens"] == kimi.MAX_OUTPUT_TOKENS
    assert request["extra_body"]["provider"]["order"] == ["relace/fp4"]
    assert request["extra_body"]["tools"][0]["parameters"]["excluded_domains"] == ["metaculus.com"]
    assert replay.REPLAY_RESEARCH[:40] in str(request["prompt"]) and "Do not use metaculus.com." in str(request["prompt"])
    # Ledger: its own day share and calls; the GPT shadow's and paid Flash's untouched.
    calls = [c for q in ("1", "2", "3", "4") for c in spend.questions[q]["calls"]]
    assert len(calls) == 4 and all(c["model"] == "moonshotai/kimi-k3" and c["shadow"] == "kimi"
                                   and c["provider"] == "relace/fp4" and c["searches"] == 3 for c in calls)
    assert spend.day_spent() == pytest.approx(0.36) and spend.shadow_day_spent("kimi") == pytest.approx(0.36)
    assert spend.gpt_shadow_day_spent() == 0.0 and spend.flash_day_spent() == pytest.approx(0.0)
    assert spend.questions["1"]["kimi_shadow"] == pytest.approx(0.09) and spend.questions["1"]["spent"] == 0.0
    assert spend.snapshot()["kimi_shadow_days"] == {utc_day(): pytest.approx(0.36)}
    assert "gpt_shadow_days" not in spend.snapshot()
    assert spend.kimi_shadow_in_flight == {"1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0}
    public = [r.getMessage() for r in caplog.records if r.name == "fall26" and "Kimi shadow" in r.getMessage()]
    assert "Question 1: Kimi shadow saved, $0.0900" in public and len(public) == 4
    assert not any("91" in line for line in public)
    assert shadow.summary().startswith("Kimi shadow (moonshotai/kimi-k3, never submitted): saved 4; $0.3600")


def test_off_kimi_adds_nothing_to_the_shadows_file():
    _, files = _run(_kimi_bot(None))
    assert FakeKimi.requests == [] and all("kimi_shadow" not in files[kind] for kind in TYPES)


def test_both_shadows_on_share_one_ledger_and_keep_their_own_caps():
    spend = _spend()
    _, files = _run(_kimi_bot(_shadow(spend), gpt_shadow=_gpt_shadow(spend)), [QUESTIONS[0]])
    assert files["binary"]["gpt_shadow"]["status"] == gpt.SAVED and files["binary"]["kimi_shadow"]["status"] == gpt.SAVED
    assert files["binary"]["shadow"][gpt.VARIANT] == pytest.approx(0.97)
    assert files["binary"]["shadow"][kimi.VARIANT] == pytest.approx(0.91)
    assert spend.gpt_shadow_day_spent() == pytest.approx(0.07) and spend.shadow_day_spent("kimi") == pytest.approx(0.09)
    assert spend.day_spent() == pytest.approx(0.16) and spend.flash_day_spent() == pytest.approx(0.0)
    # Each shadow's question cap is its own.
    assert _shadow(spend).reserve("1", True) == gpt.QUESTION_CAP
    assert spend.shadow_question_spent("gpt", "1") == pytest.approx(0.07)


# ---------------------------------------------------------------- caps


def _kimi_day(day: float) -> SpendGuard:
    spend = _spend(other_day=day)
    spend.kimi_shadow_days[utc_day()] = day
    return spend


@pytest.mark.parametrize("day, seasonal, expected", [
    (1.25, True, None), (1.26, True, gpt.DAY_CAP), (1.30, False, gpt.DAY_CAP),
    (0.755, False, None), (0.76, False, gpt.MINIBENCH_PRIORITY), (0.76, None, gpt.MINIBENCH_PRIORITY),
])
def test_day_cap_and_minibench_under_one_dollar(day, seasonal, expected):
    result = _shadow(_kimi_day(day)).reserve("9", seasonal)
    assert (None if not isinstance(result, str) else result) == expected


def test_gpt_shadow_days_do_not_count_against_kimi():
    spend = _spend(gpt_day=1.40)
    assert not isinstance(_shadow(spend).reserve("a", True), str)
    assert _gpt_shadow(spend).reserve("b", True) == gpt.DAY_CAP


def test_question_cap_and_key_low():
    shadow = _shadow()
    assert not isinstance(shadow.reserve("q", True), str)
    assert shadow.reserve("q", True) == gpt.QUESTION_CAP  # one reservation per question
    assert _shadow(key_left=2.20).reserve("k", True) == gpt.KEY_LOW
    assert _shadow(key_left=None).reserve("k", True) == gpt.KEY_UNKNOWN
    assert _shadow(SpendGuard(MemoryStore(None))).reserve("a", True) == gpt.LEDGER


def test_key_check_counts_both_shadows_running():
    spend = _spend()
    assert not isinstance(_shadow(_spend(), key_left=2.45).reserve("b", True), str)  # alone: 2.45 - 0.245 >= 2
    gpt_shadow = _gpt_shadow(spend, key_left=2.45)
    assert not isinstance(gpt_shadow.reserve("a", True), str)  # 2.45 - 0.25 = 2.20 left
    assert _shadow(spend, key_left=2.45).reserve("b", True) == gpt.KEY_LOW  # 2.20 - 0.245 < 2


def test_seasonal_first_in_the_run():
    spend = _kimi_day(0.755)
    _, files = _run(_kimi_bot(_shadow(spend), seasonal_by_post={1: False, 4: True}), [QUESTIONS[0], QUESTIONS[3]])
    assert files["multiple_choice"]["kimi_shadow"]["status"] == gpt.SAVED
    assert files["binary"]["kimi_shadow"]["status"] == gpt.MINIBENCH_PRIORITY


# ---------------------------------------------------------------- failures and domains


def test_credit_error_is_not_retried_and_stops_the_kimi_shadow():
    FakeKimi.mode = "credit"
    spend = _spend()
    shadow = _shadow(spend)
    records, files = _run(_kimi_bot(shadow), [QUESTIONS[0]])
    assert len(FakeKimi.requests) == 1
    assert files["binary"]["kimi_shadow"]["status"] == "failed (CreditError)" and files["binary"]["kimi_shadow"]["cost"] == 0.0
    assert shadow.off == "credit error" and shadow.reserve("2", True) == "skipped: credit error"
    assert records["binary"]["final_forecast"] is not None


def test_call_over_its_reservation_switches_it_off_and_alerts():
    FakeKimi.cost = 0.30
    spend = _spend()
    shadow = _shadow(spend)
    _, files = _run(_kimi_bot(shadow), [QUESTIONS[0]])
    assert files["binary"]["kimi_shadow"]["cost"] == pytest.approx(0.30)
    assert shadow.off == "over its reservation" and shadow.overruns == 1
    alerts = []
    assert gpt.overrun_alert(shadow, lambda title, body: alerts.append((title, body)))
    assert "Kimi shadow call over its reservation" in alerts[0][0] and "$0.24 reservation" in alerts[0][1]
    assert spend.alerts.get("kimi-shadow-overrun") == utc_day()


@pytest.mark.parametrize("urls, cited", [
    (["https://www.reuters.com/a", "https://bls.gov/c"], False),
    (["https://www.metaculus.com/questions/1/", "https://bls.gov/c"], True),
])
def test_domains_in_the_private_file_count_and_flag_in_the_public_log(caplog, urls, cited):
    FakeKimi.urls = urls
    with caplog.at_level(logging.INFO, logger="fall26"):
        _, files = _run(_kimi_bot(_shadow()), [QUESTIONS[0]])
    entry = files["binary"]["kimi_shadow"]
    assert entry["domains_count"] == 2 and entry["metaculus_cited"] is cited and "secret text" not in str(entry)
    [line] = [r.getMessage() for r in caplog.records if r.name == "fall26" and "Kimi shadow saved" in r.getMessage()]
    assert line == f"Question 1: Kimi shadow saved, $0.0900, domains 2, metaculus cited: {'yes' if cited else 'no'}"


def test_one_call_per_llm():
    shadow = _shadow()
    llm = shadow.make_llm(shadow.reserve("s", True), 10)
    assert isinstance(llm, FakeKimi)

    async def twice():
        replay.current_question.set(QUESTIONS[0])
        await llm._mockable_direct_call_to_model("x")
        with pytest.raises(RuntimeError, match="Kimi shadow: one call per question"):
            await llm._mockable_direct_call_to_model("x")

    asyncio.run(twice())
    assert isinstance(kimi.KimiShadow(_spend(), 10.0, enabled=True).make_llm(
        kimi.KimiShadow(_spend(), 10.0, enabled=True).reserve("t", True), 10), kimi.KimiShadowLlm)


def test_scoreboard_scores_the_kimi_shadow_with_and_without_metaculus():
    import scoreboard

    def files(post, cited):
        extra = {"kimi_shadow": {"status": "saved", "metaculus_cited": cited}, "shadow": {kimi.VARIANT: 0.8}}
        return {f"q/{post}.json": {"question": {"id_of_post": post, "question_type": "binary"}, "final_forecast": 0.6},
                f"q/{post}_shadows.json": extra}

    records = scoreboard.merge_shadow_files({**files(1, False), **files(2, True)})
    assert records[0]["kimi_shadow"]["status"] == "saved"
    resolution = {1: "yes", 2: "yes"}.get
    report = scoreboard.report_markdown(scoreboard.score_records(records, resolution))
    assert f"| binary | {kimi.VARIANT} | 2 |" in report
    assert f"| binary | {scoreboard.KIMI_SHADOW_NO_METACULUS} | 1 |" in report
    names = [m for row in scoreboard.score_models(records, resolution) for m, _, _ in row["models"]]
    assert names.count(scoreboard.KIMI_SHADOW_NAME) == 2 and names.count(scoreboard.KIMI_SHADOW_NAME_NO_METACULUS) == 1
    assert scoreboard.KIMI_SHADOW_VARIANT == kimi.VARIANT and scoreboard.model_group(scoreboard.KIMI_SHADOW_NAME) == "other"


def test_frontier_report_counts_the_kimi_shadow_apart():
    import frontier

    spend = _spend()
    planner = frontier._frontier_pool(MemoryStore(), spend, None)
    _shadow(spend).reserve("7", True).settle(ok=True, cost=0.08, seconds=40.0, searches=3, input_tokens=60000, output_tokens=5000)
    report = frontier.frontier_report(planner)
    assert "| OpenRouter (Kimi shadow) | moonshotai/kimi-k3 | 1 | 1 |" in report and "| OpenRouter (Kimi shadow) | 7 | $0.0800 |" in report


def test_main_wires_one_spend_guard_for_both_shadows():
    source = open("main.py").read()
    assert "template_bot.kimi_shadow = KimiShadow(shadow_spend, key_left, enabled=True)" in source
    assert "template_bot.gpt_shadow = GptShadow(shadow_spend, key_left, enabled=True)" in source
    assert main.PAID_SHADOW_SLOTS == ("gpt_shadow", "kimi_shadow")
