"""tournament_info --standing: our username and leaderboard row (rank, score, 95% range)."""
from __future__ import annotations

from types import SimpleNamespace

import tournament_info

ME = {"id": 7, "username": "our-bot"}


def test_standing_finds_our_entry_and_range():
    board = {"entries": [{"user": {"id": 3, "username": "x"}, "rank": 1, "score": 9.0, "ci_lower": 1, "ci_upper": 2},
                         {"user": {"id": 7, "username": "our-bot"}, "rank": 12, "score": -3.5, "ci_lower": -8.1, "ci_upper": 1.2}]}
    line = tournament_info.standing_line("Seasonal", board, ME)
    assert "rank **12** of 2" in line and "score **-3.5**" in line and "95% range -8.1 to 1.2" in line


def test_standing_user_entry_and_missing_fields():
    board = {"entries": [], "userEntry": {"user": {"id": 7}, "rank": 40, "points": 1.0}}
    line = tournament_info.standing_line("MiniBench", board, ME)
    assert "rank **40**" in line and "95% range not given" in line and "entry fields: points, rank, user" in line


def test_standing_not_listed_and_not_readable():
    assert "not on the leaderboard yet" in tournament_info.standing_line("S", {"entries": []}, ME)
    assert "HTTP 404" in tournament_info.standing_line("S", None, ME, 404)


def test_standing_lines(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "t")
    answers = {"users/me/": (200, ME), "leaderboards/project/1/": (200, {"entries": [{"user": ME, "rank": 2, "score": 0.5}]}),
               "leaderboards/project/2/": (403, {})}

    def get(url, **_):  # type: ignore[no-untyped-def]
        status, body = answers[url.removeprefix(tournament_info.API + "/")]
        return SimpleNamespace(status_code=status, json=lambda: body)

    lines = tournament_info.standing_lines([("A", 1), ("B", 2)], get=get)
    assert "**our-bot**" in lines[0] and "rank **2** of 1" in lines[1] and "HTTP 403" in lines[2]


def test_list_answers():
    entry = {"user": ME, "rank": 5, "score": 1.0}
    assert tournament_info.leaderboards([entry]) == [("", {"entries": [entry]})]
    named = tournament_info.leaderboards([{"name": "Peer", "entries": [entry]}])
    assert named[0][0] == ", Peer" and "rank **5**" in tournament_info.standing_line("S", named[0][1], ME)
    assert tournament_info.leaderboards({"entries": []}) == [("", {"entries": []})]


def test_a_slug_is_looked_up_as_a_number(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "t")
    seen = []

    def get(url, **_):  # type: ignore[no-untyped-def]
        seen.append(url.removeprefix(tournament_info.API + "/"))
        body = {"users/me/": ME, "projects/tournaments/fall/": {"id": 33121}}.get(seen[-1], {"entries": []})
        return SimpleNamespace(status_code=200, json=lambda: body)

    tournament_info.standing_lines([("Seasonal", "fall")], get=get)
    assert seen[-1] == "leaderboards/project/33121/"
