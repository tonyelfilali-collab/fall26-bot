"""Test Bot runs charge per-question spend to a per-run key; live keys are the post id."""
from types import SimpleNamespace

import main
from gemini_budget import current_question_key


def test_live_modes_keep_the_post_id():
    assert main.test_question_key_suffix("tournament", "123") == ""
    assert main.test_question_key_suffix("metaculus_cup", "123") == ""
    bot = object.__new__(main.FallBot2026)
    assert bot._question_key(SimpleNamespace(id_of_post=46224)) == "46224"


def test_test_runs_get_a_key_per_run():
    first = main.test_question_key_suffix("test_questions", "111")
    second = main.test_question_key_suffix("test_questions", "222")
    assert first == "-test-111" and second == "-test-222"
    bot = object.__new__(main.FallBot2026)
    bot.question_key_suffix = first
    assert bot._question_key(SimpleNamespace(id_of_post=46224)) == "46224-test-111"


def test_run_id_comes_from_github(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "39661217505")
    assert main.test_question_key_suffix("test_questions") == "-test-39661217505"
    monkeypatch.delenv("GITHUB_RUN_ID")
    assert main.test_question_key_suffix("test_questions").startswith("-test-")


def test_spend_is_charged_to_the_run_key_and_still_counts_for_the_day():
    from spend import SpendGuard
    from gemini_budget import MemoryStore

    guard = SpendGuard(MemoryStore({}))
    token = current_question_key.set("46224-test-111")
    try:
        guard.set_cap("46224-test-111", 3.0)
        guard.start("m")
        guard.finish("m", True, cost=0.5)
    finally:
        current_question_key.reset(token)
    assert guard.question_spent("46224-test-111") == 0.5
    assert guard.question_spent("46224") == 0.0
    assert guard.day_spent() == 0.5
