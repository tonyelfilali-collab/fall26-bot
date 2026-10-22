"""Daily report additions (health_check.py): the Claude ledger $ per UTC day
and, on Mondays, our Fall / MiniBench standing (standings.py). Fake data, no network."""
from __future__ import annotations

from datetime import date, datetime, timezone

import claude_health
import health_check
import standings

MONDAY = datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)
ME = standings.OUR_BOT


class FakeStore:
    def __init__(self, data=None, fail_load=False, fail_save=False):
        self.data, self.fail_load, self.fail_save, self.saved = data, fail_load, fail_save, []

    def load(self):
        if self.fail_load:
            raise RuntimeError("down")
        return self.data

    def save(self, data, message=None):
        if self.fail_save:
            raise RuntimeError("down")
        self.data = data
        self.saved.append(message)


def board(rank, score, low=None, high=None, user=ME):
    entry = {"user": dict(user), "rank": rank, "score": score}
    if low is not None:
        entry.update(ci_lower=low, ci_upper=high)
    return {"entries": [{"user": {"id": 1, "username": "x"}, "rank": 1, "score": 99.0}, entry]}


def fake_get(answers):
    return lambda path: answers.get(path)


# ---------------------------------------------------------------- standings


def test_monday_lines_with_change_since_last_monday():
    store = FakeStore({"2026-10-19": {"Fall": {"rank": 15, "score": 10.0, "tid": 33121},
                                      "MiniBench": {"rank": 3, "score": 5.0, "tid": 33132}}})
    get = fake_get({"leaderboards/project/33121/": board(12, 12.5, 3.1, 20.4),
                    "leaderboards/project/33132/": board(4, 4.0)})
    lines = standings.standings_lines(MONDAY.date(), [("Fall", 33121), ("MiniBench", 33132)], store, get)
    assert lines[0] == ("- Fall (33121): rank 12, score 12.50, 95% range 3.10 to 20.40; "
                        "change since 2026-10-19: rank up 3 (was 15), score +2.50 (was 10.00)")
    assert "95% range n/a" in lines[1] and "rank down 1 (was 3), score -1.00 (was 5.00)" in lines[1]
    assert store.data["2026-10-26"]["Fall"] == {"rank": 12, "score": 12.5, "low": 3.1, "high": 20.4, "tid": 33121}
    assert store.saved == ["Standings 2026-10-26"]


def test_first_monday_new_round_and_one_missing():
    store = FakeStore({"2026-10-19": {"MiniBench": {"rank": 3, "score": 5.0, "tid": 33125}}})
    get = fake_get({"leaderboards/project/33132/": [{"name": "Peer", "entries": board(7, 1.0)["entries"]}],
                    "projects/tournaments/minibench/": {"id": 33132}})
    lines = standings.standings_lines(MONDAY.date(), [("Fall", 33121), ("MiniBench", "minibench")], store, get)
    assert lines[0] == "- Fall: unavailable"
    assert "MiniBench (33132): rank 7" in lines[1] and "change: n/a (new round since 2026-10-19)" in lines[1]
    first = standings.standings_lines(MONDAY.date(), [("MiniBench", 33132)], FakeStore(None), get)
    assert "no earlier Monday stored" in first[0]


def test_not_on_board_or_api_down_is_one_line():
    other = board(1, 1.0, user={"id": 5, "username": "someone"})
    for get in (fake_get({}), fake_get({"leaderboards/project/33121/": other}), lambda path: 1 / 0):
        store = FakeStore({})
        assert standings.standings_lines(MONDAY.date(), [("Fall", 33121)], store, get) == [standings.UNAVAILABLE]
        assert store.saved == []


def test_store_failures_still_show_the_standing():
    get = fake_get({"leaderboards/project/33121/": board(2, 3.0)})
    unread = FakeStore(fail_load=True)
    assert "rank 2" in standings.standings_lines(MONDAY.date(), [("Fall", 33121)], unread, get)[0]
    assert unread.saved == []  # never saved over a file it couldn't read
    lines = standings.standings_lines(MONDAY.date(), [("Fall", 33121)], FakeStore({}, fail_save=True), get)
    assert "could not be stored" in lines[-1]


def test_health_adds_standings_only_soft():
    report = health_check.Report()
    health_check.add_standings(MONDAY, 33132, report, store=FakeStore({}), get=lambda path: None)
    assert report.standings == [standings.UNAVAILABLE] and not report.red and not report.warnings
    assert standings.is_monday(MONDAY.date()) and not standings.is_monday(date(2026, 10, 22))


# ---------------------------------------------------------------- Claude ledger per day


def test_claude_days_lines():
    ledger = {"days": {"2026-10-17": 9.0, "2026-10-18": 1.5, "2026-10-20": 2.25, "2026-10-21": 3.0},
              "cycles": {"2026-10-18": 6.75}}
    lines = claude_health.claude_days_lines(ledger, date(2026, 10, 21), date(2026, 10, 18), 170.0)
    assert lines[2] == "| 2026-10-15 | $0.0000 |" and lines[8] == "| 2026-10-21 | $3.0000 |"
    assert lines[9] == "| last 7 days | $15.7500 |"
    assert lines[-1] == "Cycle total (since 2026-10-18): $6.7500 of $170.00"
    ledger["cycles"]["2026-10-18"] = 7.0
    changed = claude_health.claude_days_lines(ledger, date(2026, 10, 21), date(2026, 10, 18), 170.0)
    assert "(ledger cycle field: $7.0000)" in changed[-1]
    assert "n/a" in claude_health.claude_days_lines({}, date(2026, 10, 21), None, 170.0)[-1]


def test_publish_shows_the_new_sections(capsys):
    report = health_check.Report(claude_days=["| 2026-10-21 | $3.0000 |"], standings=["standings: unavailable"])
    health_check.publish(report)
    out = capsys.readouterr().out
    assert "### Claude ledger per UTC day" in out and "### Our standing (Mondays)" in out
