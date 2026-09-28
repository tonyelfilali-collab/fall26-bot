"""
Tests for PLAN.md Step 5 (bench.py): scoring with worked examples, bootstrap,
question mix, resumable runs, and refusing the live Gemini quota. No network.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from forecasting_tools import BinaryQuestion

import bench


def test_worked_example_binary_kl():
    # Community 70%, ours 60%: 0.7 ln(0.7/0.6) + 0.3 ln(0.3/0.4) = 0.02160...
    assert bench.kl_binary(0.7, 0.6) == pytest.approx(0.7 * math.log(0.7 / 0.6) + 0.3 * math.log(0.3 / 0.4))
    assert bench.kl_binary(0.7, 0.6) == pytest.approx(0.0216, abs=1e-4)
    assert bench.kl_binary(0.4, 0.4) == pytest.approx(0.0)


def test_worked_example_multiple_choice_kl():
    # Community (0.5, 0.3, 0.2) vs ours (0.4, 0.4, 0.2).
    expected = 0.5 * math.log(0.5 / 0.4) + 0.3 * math.log(0.3 / 0.4)
    assert bench.kl_discrete([0.5, 0.3, 0.2], [0.4, 0.4, 0.2]) == pytest.approx(expected)


def test_worked_example_cdf_kl():
    # 3-point CDFs: pmf = [below, bucket 1, bucket 2, above].
    assert bench.cdf_to_pmf([0.1, 0.5, 0.9]) == pytest.approx([0.1, 0.4, 0.4, 0.1])
    assert bench.kl_cdf([0.1, 0.5, 0.9], [0.1, 0.5, 0.9]) == pytest.approx(0.0)
    assert bench.kl_cdf([0.1, 0.5, 0.9], [0.2, 0.5, 0.8]) > 0


def test_kl_never_blows_up_on_zeros():
    assert math.isfinite(bench.kl_binary(1.0, 0.0))
    assert math.isfinite(bench.kl_discrete([1.0, 0.0], [0.0, 1.0]))


def test_bootstrap_ci_contains_the_mean_and_is_reproducible():
    diffs = [0.01, -0.02, 0.03, 0.0, 0.02, -0.01, 0.04, 0.01]
    low, high = bench.bootstrap_ci(diffs)
    assert low <= sum(diffs) / len(diffs) <= high
    assert bench.bootstrap_ci(diffs) == (low, high)  # fixed seed
    assert bench.bootstrap_ci([0.05] * 10) == pytest.approx((0.05, 0.05))


def test_question_mix():
    assert bench.question_counts(30) == {"binary": 18, "numeric": 3, "discrete": 3, "multiple_choice": 6}
    assert sum(bench.question_counts(60).values()) == 60


def test_community_prediction_from_api_json():
    q = BinaryQuestion(question_text="x", id_of_post=1, api_json={"question": {"aggregations": {"recency_weighted": {"latest": {"centers": [0.62], "forecast_values": [0.38, 0.62]}}}}})
    assert bench.community_prediction(q) == pytest.approx(0.62)
    hidden = BinaryQuestion(question_text="x", id_of_post=2, api_json={"question": {"aggregations": {}}})
    assert bench.community_prediction(hidden) is None


def test_gemini_free_is_refused():
    with pytest.raises(SystemExit):
        bench.make_bot("gemini-free")


def test_report_single_and_paired():
    a = [bench.Scored(1, "binary", 0.02, 0.0), bench.Scored(2, "binary", 0.04, 0.0), bench.Scored(3, "multiple_choice", 0.1, 0.0)]
    single = bench.report_markdown("free-1", a)
    assert "| all | 3 | 0.0533 |" in single
    b = [bench.Scored(1, "binary", 0.01, 0.0), bench.Scored(2, "binary", 0.05, 0.0), bench.Scored(3, "multiple_choice", 0.1, 0.0)]
    paired = bench.report_markdown("free-1", a, "free-2", b)
    assert "| binary | 2 |" in paired and "+0.0000" in paired  # mean diff (−0.01 + 0.01) / 2


def test_run_config_resumes_from_saved_results(monkeypatch):
    store = bench.BenchStore(memory={})
    q = BinaryQuestion(question_text="Will X?", id_of_post=7, api_json={"question": {"aggregations": {"recency_weighted": {"latest": {"centers": [0.7]}}}}})
    store.save("quick/research/7.json", {"fetched_at": "t", "text": "frozen research"})
    calls = []

    class FakeBot:
        frozen_research = {}

        async def forecast_questions(self, questions, return_exceptions=True):
            calls.append([x.id_of_post for x in questions])
            return [SimpleNamespace(prediction=0.6, price_estimate=0.0)]

        async def _asknews_with_free_fallback(self, q, prompt):
            raise AssertionError("research is frozen")

    monkeypatch.setattr(bench, "make_bot", lambda config: FakeBot())
    first = bench.run_config("free", "free-1", [q], store, "quick")
    second = bench.run_config("free", "free-1", [q], store, "quick")
    assert calls == [[7]]  # the second run reused the saved result
    assert first[0].kl == pytest.approx(bench.kl_binary(0.7, 0.6)) == second[0].kl


@pytest.mark.parametrize("question_type", ["binary", "numeric", "discrete", "multiple_choice"])
def test_question_filter_is_valid_for_every_type(question_type):
    # The library only accepts the community-prediction filter for binary questions.
    f = bench.question_filter(question_type)
    assert f.allowed_types == [question_type]
    assert f.community_prediction_exists == (True if question_type == "binary" else None)


def test_mini_size_mix():
    assert bench.question_counts(10) == {"binary": 6, "numeric": 1, "discrete": 1, "multiple_choice": 2}


def test_the_bot_never_sees_the_community_prediction():
    api = {"question": {"aggregations": {"recency_weighted": {"latest": {"centers": [0.62]}}}}}
    q = BinaryQuestion(question_text="x", id_of_post=1, api_json=api, community_prediction_at_access_time=0.62)
    seen = bench.blind(q)
    assert seen.community_prediction_at_access_time is None
    assert bench.community_prediction(seen) is None
    assert bench.community_prediction(q) == pytest.approx(0.62)  # the original keeps it for scoring


def test_the_live_bot_refuses_the_read_token():
    import os
    import subprocess
    import sys

    env = dict(os.environ, METACULUS_READ_TOKEN="x")
    result = subprocess.run([sys.executable, "main.py", "--mode", "test_questions"], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    assert "METACULUS_READ_TOKEN must not be given to the bot" in result.stderr


def test_selection_keeps_the_community_prediction_from_the_list(monkeypatch):
    # A plain post fetch leaves the prediction out, so the list's data is kept (no refetch).
    with_cp = BinaryQuestion(question_text="x", id_of_post=1, api_json={"question": {"aggregations": {"recency_weighted": {"latest": {"centers": [0.4]}}}}})
    without = BinaryQuestion(question_text="y", id_of_post=2, api_json={"question": {"aggregations": {}}})

    class FakeClient:
        async def get_questions_matching_filter(self, api_filter, **kwargs):
            return [with_cp, without] if api_filter.allowed_types == ["binary"] else []

        def get_question_by_post_id(self, post_id):
            raise AssertionError("no refetch without with_cp")

    chosen = bench.select_questions(FakeClient(), 10)
    assert [q.id_of_post for q in chosen] == [1]
