"""resolved_review.py (architect, 2 Nov): peer score per resolved Fall question,
worst first, and Metaculus's submitted forecast vs our private log. Fake posts
and logs only (0 real calls)."""
from __future__ import annotations

import resolved_review as rr

SCORED_AT = 1_793_491_200  # 1 Nov 2026 00:00 UTC, epoch seconds as Metaculus sends them


def _post(post, kind, forecast_values, resolution, peer, **question):  # type: ignore[no-untyped-def]
    history = [{"start_time": SCORED_AT - 100, "end_time": None, "forecast_values": forecast_values}]
    q = {"type": kind, "resolution": resolution, "spot_scoring_time": SCORED_AT,
         "my_forecasts": {"history": history, "score_data": {"spot_peer_score": peer, "baseline_score": 1.0}}}
    q.update(question)
    return {"id": post, "title": f"Q{post}", "question": q}


def _log(post, final, kind, started="2026-10-25T12:00:00+00:00", **question):  # type: ignore[no-untyped-def]
    return {"question": {"id_of_post": post, "question_type": kind, **question}, "final_forecast": final,
            "started_at": started, "submitted": True,
            "forecasts": [{"status": "ok", "forecast_model": "anthropic/claude-opus-5-5-agent"},
                          {"status": "failed", "forecast_model": "gemini/gemini-3.6-flash"}]}


SCALING = {"range_min": 0.0, "range_max": 100.0, "zero_point": None}
CDF = [min(0.999, max(0.001, i / 200)) for i in range(201)]  # uniform on 0-100: median 50
DECLARED = [{"percentile": 0.001, "value": 0.0}, {"percentile": 0.5, "value": 50.0}, {"percentile": 0.999, "value": 100.0}]
BOUNDS = {"lower_bound": 0.0, "upper_bound": 100.0, "zero_point": None, "open_lower_bound": True, "open_upper_bound": True}


def test_binary_same_upside_down_and_different():
    q = {}
    entry = {"forecast_values": [0.2, 0.8]}
    assert rr.submission_check("binary", entry, q, _log(1, 0.8, "binary")) == "same"
    assert rr.submission_check("binary", entry, q, _log(1, 0.2, "binary")).startswith("UPSIDE DOWN")
    assert rr.submission_check("binary", entry, q, _log(1, 0.5, "binary")).startswith("DIFFERENT")
    assert rr.submission_check("binary", None, q, None) == "no forecast on Metaculus"


def test_multiple_choice_options_in_the_wrong_order_are_flagged():
    q = {"options": ["A", "B", "C"]}
    log = _log(2, {"predicted_options": [{"option_name": "A", "probability": 0.7},
                                         {"option_name": "B", "probability": 0.2},
                                         {"option_name": "C", "probability": 0.1}]}, "multiple_choice")
    assert rr.submission_check("multiple_choice", {"forecast_values": [0.7, 0.2, 0.1]}, q, log) == "same"
    assert "order?" in rr.submission_check("multiple_choice", {"forecast_values": [0.1, 0.2, 0.7]}, q, log)
    assert rr.summary("multiple_choice", rr.submitted_values("multiple_choice", {"forecast_values": [0.7, 0.2, 0.1]}, q), q) == "top: A 0.70"


def test_numeric_median_and_bounds_check():
    q = {"scaling": SCALING, "open_lower_bound": True, "open_upper_bound": True}
    entry = {"forecast_values": CDF}
    med, p10, p90 = rr.submitted_values("numeric", entry, q)
    assert abs(med - 50) < 0.6 and abs(p10 - 10) < 0.6 and abs(p90 - 90) < 0.6
    assert rr.submission_check("numeric", entry, q, _log(3, {"declared_percentiles": DECLARED}, "numeric", **BOUNDS)) == "same"
    wrong = dict(BOUNDS, upper_bound=1000.0, open_upper_bound=False)
    check = rr.submission_check("numeric", entry, q, _log(3, {"declared_percentiles": DECLARED}, "numeric", **wrong))
    assert "upper bound: log 1000.0 vs Metaculus 100.0" in check and "open upper end" in check


def test_log_scale_grid_ends_on_the_bounds():
    grid = rr.grid_values({"range_min": 1.0, "range_max": 1000.0, "zero_point": 0.0}, 201)
    assert abs(grid[0] - 1) < 1e-9 and abs(grid[-1] - 1000) < 1e-6 and abs(grid[100] - 31.62) < 0.01


def test_rows_worst_first_with_period_models_and_scored_log():
    posts = [
        _post(10, "binary", [0.1, 0.9], "no", -80.0),
        _post(11, "binary", [0.7, 0.3], "no", 12.0),
        _post(12, "binary", None, "annulled", None),
        {"id": 13, "group_of_questions": {}},  # no single question: skipped
    ]
    logs = {
        10: [_log(10, 0.9, "binary", started="2026-10-20T10:00:00+00:00"),
             _log(10, 0.5, "binary", started="2026-12-01T00:00:00+00:00")],  # after scoring: not the scored one
        11: [_log(11, 0.3, "binary")],
        12: [],
    }
    posts[2]["question"]["my_forecasts"] = {}
    rows = rr.build_rows(posts, lambda post: logs[post])
    assert [r["post"] for r in rows] == [10, 11, 12]
    worst = rows[0]
    assert worst["peer"] == -80.0 and worst["ours"] == "0.90" and worst["outcome"] == "no"
    assert worst["period"] == "before" and worst["check"] == "same" and worst["log_count"] == 2
    assert worst["models"] == "claude-opus-5-5-agent"
    assert rows[1]["period"] == "since"
    assert rows[2]["peer"] is None and rows[2]["check"] == "no forecast on Metaculus"
    text = rr.markdown(rows, rr.when("2026-11-02T13:00:00Z"))
    assert "Sum of peer scores: -68.0 over 2 scored question(s)." in text
    assert "| [10](https://www.metaculus.com/questions/10) | binary | -80.0 |" in text


def test_workflow_option_saves_privately():
    from pathlib import Path

    text = Path(".github/workflows/scoreboard.yml").read_text()
    assert "resolved_review:" in text and "python resolved_review.py" in text
    assert "inputs.resolved_review != true" in text
