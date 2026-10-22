"""tournament_info: the comment check (yes/no only) and other tournaments' facts."""
from __future__ import annotations

from types import SimpleNamespace

import tournament_info


def _get(routes):
    def get(url, params=None, headers=None, timeout=None):
        for key, (status, body) in routes.items():
            if url.endswith(key):
                return SimpleNamespace(status_code=status, json=lambda body=body: body)
        return SimpleNamespace(status_code=404, json=lambda: {})
    return get


def _routes(text, values=(0.54, 0.46), qtype="binary", private=True, author=7):
    return {
        "/users/me/": (200, {"id": 7}),
        "/posts/46180/": (200, {"question": {"type": qtype, "my_forecasts": {"latest": {"forecast_values": list(values)}}}}),
        "/comments/": (200, {"results": [
            {"author": {"id": 3}, "on_post": 46180, "text": "*Final Prediction*: 46.0%", "created_at": "2026-10-22T06:00"},
            {"author": author, "on_post": 46180, "is_private": private, "created_at": "2026-10-22T05:46",
             "text": f"# SUMMARY\n*Question*: q\n{text}\n*Total Cost*: x"},
        ]}),
    }


def test_comment_matches(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "t")
    lines = tournament_info.comment_check_lines("46180", get=_get(_routes("*Final Prediction*: 46.0%")))
    joined = "\n".join(lines)
    assert "Our comment exists: yes (1 found)" in joined and "private: yes" in joined
    assert "Final Prediction = submitted forecast: yes" in joined
    assert "46" not in joined.replace("46180", "")  # no forecast value printed


def test_comment_differs_missing_or_not_binary(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "t")
    assert "submitted forecast: no" in "\n".join(tournament_info.comment_check_lines("46180", get=_get(_routes("*Final Prediction*: 50.0%"))))
    assert "no Final Prediction line" in "\n".join(tournament_info.comment_check_lines("46180", get=_get(_routes("nothing"))))
    assert "Our comment exists: no" in "\n".join(tournament_info.comment_check_lines("46180", get=_get(_routes("x", author=9))))
    assert "not checked" in "\n".join(tournament_info.comment_check_lines("46180", get=_get(_routes("x", qtype="numeric"))))


def test_leaderboard_size(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "t")
    get = _get({"/tournaments/market-pulse-26q4/": (200, {"id": 99}),
                "/leaderboards/project/99/": (200, {"entries": [{}, {}, {}]})})
    assert tournament_info.leaderboard_size_line("market-pulse-26q4", get=get) == "- Leaderboard entries: leaderboard 3"
