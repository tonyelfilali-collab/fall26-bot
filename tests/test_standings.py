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
    assert lines[0] == ("- Fall (33121): rank 12, score 12.50, 95% range 3.10 to 20.40, prize shown n/a; "
                        "change since 2026-10-19: rank up 3 (was 15), score +2.50 (was 10.00)")
    assert lines[1] == "  - field (2 entries): #1 99.00, #10 12.50, #50 ?, last paid rank n/a (no prizes shown)"
    assert lines[2] == "  - resolved questions: n/a"
    assert "95% range n/a" in lines[3] and "rank down 1 (was 3), score -1.00 (was 5.00)" in lines[3]
    assert store.data["2026-10-26"]["Fall"] == {"rank": 12, "score": 12.5, "low": 3.1, "high": 20.4, "prize": None,
                                                "tid": 33121}
    assert store.saved == ["Standings 2026-10-26"]


def test_first_monday_new_round_and_one_missing():
    store = FakeStore({"2026-10-19": {"MiniBench": {"rank": 3, "score": 5.0, "tid": 33125}}})
    get = fake_get({"leaderboards/project/33132/": [{"name": "Peer", "entries": board(7, 1.0)["entries"]}],
                    "projects/tournaments/minibench/": {"id": 33132}})
    lines = standings.standings_lines(MONDAY.date(), [("Fall", 33121), ("MiniBench", "minibench")], store, get)
    assert lines[0] == "- Fall (33121): unavailable (leaderboard not readable)"
    assert "MiniBench (33132): rank 7" in lines[1] and "change: n/a (new round since 2026-10-19)" in lines[1]
    first = standings.standings_lines(MONDAY.date(), [("MiniBench", 33132)], FakeStore(None), get)
    assert "no earlier Monday stored" in first[0]


def test_not_on_board_or_api_down_is_one_line():
    other = board(1, 1.0, user={"id": 5, "username": "someone"})
    for get, why in ((fake_get({}), " (Fall (33121): unavailable (leaderboard not readable))"),
                     (fake_get({"leaderboards/project/33121/": other}),
                      " (Fall (33121): unavailable (not on the leaderboard yet (2 entries)))"),
                     (lambda path: 1 / 0, "")):
        store = FakeStore({})
        assert standings.standings_lines(MONDAY.date(), [("Fall", 33121)], store, get) == [standings.UNAVAILABLE + why]
        assert store.saved == []


def test_prize_field_scores_and_resolved_count():
    entries = [{"user": {"id": 100 + r, "username": f"b{r}"}, "rank": r, "score": 100.0 - r,
                "prize": 500.0 / r if r <= 136 else 0.0, "contribution_count": 40 - (r % 3)} for r in range(1, 200)]
    entries[111] = {"user": dict(ME), "rank": 112, "score": 20.36, "ci_lower": 17.9, "ci_upper": 22.8,
                    "prize": 95.0, "contribution_count": 38}
    get = fake_get({"leaderboards/project/33121/": {"entries": entries},
                    "posts/?tournaments=33121&statuses=resolved&limit=1": {"count": 61, "results": []}})
    lines = standings.standings_lines(MONDAY.date(), [("Fall", 33121)], FakeStore({}), get)
    assert lines[0].startswith("- Fall (33121): rank 112, score 20.36, 95% range 17.90 to 22.80, prize shown $95; ")
    assert lines[1] == "  - field (199 entries): #1 99.00, #10 90.00, #50 50.00, last paid #136 -36.00"
    assert lines[2] == "  - resolved questions: 61"
    # No posts count: the most questions any entry was scored on.
    get = fake_get({"leaderboards/project/33121/": {"entries": entries}})
    lines = standings.standings_lines(MONDAY.date(), [("Fall", 33121)], FakeStore({}), get)
    assert lines[2] == "  - resolved questions: 40 (most questions scored on the leaderboard)"


def test_score_at_skips_missing_ranks():
    entries = standings.ranked({"entries": [{"rank": 1, "score": 9.0}, {"rank": 12, "score": 3.0}, {"rank": None}]})
    assert standings.score_at(entries, 10) == 3.0 and standings.score_at(entries, 50) is None
    assert standings.last_paid(entries) is None


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
    assert len(report.standings) == 1 and report.standings[0].startswith(standings.UNAVAILABLE)
    assert not report.red and not report.warnings
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


def test_claude_days_lines_pacing():
    from datetime import datetime, timezone

    ledger = {"days": {"2026-10-22": 40.0}, "cycles": {"2026-10-18": 40.0}}
    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    lines = claude_health.claude_days_lines(ledger, now.date(), date(2026, 10, 18), 170.0,
                                            reset=date(2026, 11, 18), now=now)
    assert lines[-1] == "Pacing: paced-1; cycle spent $40.00; line $24.68; gap $+15.32"
    ledger["cycles"]["2026-10-18"] = 20.0
    lines = claude_health.claude_days_lines(ledger, now.date(), date(2026, 10, 18), 170.0, reset=date(2026, 11, 18), now=now)
    assert lines[-1].startswith("Pacing: full; cycle spent $20.00") and lines[-1].endswith("gap $-4.68")
