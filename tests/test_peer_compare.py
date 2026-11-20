"""peer_compare (architect, 20 Nov): read-only per-type / era peer scores, us vs reference bots. No network."""
import peer_compare as pc


def _entry(rank, name, uid):
    return {"rank": rank, "user": {"id": uid, "username": name}, "score": 100 - rank}


def test_pick_users_neighbours_and_reference_bots():
    entries = [_entry(i, f"bot{i}", i) for i in range(1, 9)]
    entries[4] = _entry(5, pc.ME, 5)
    entries.append(_entry(30, pc.REFERENCE_BOTS[0], 30))
    picked = pc.pick_users(pc.leaderboard_entries({"entries": entries[::-1]}))
    assert [p[0] for p in picked] == ["bot3", "bot4", pc.ME, "bot6", "bot7", pc.REFERENCE_BOTS[0]]


def test_contribution_fields():
    assert pc.contribution_post({"post_id": 7}) == 7
    assert pc.contribution_post({"post": {"id": 8}}) == 8
    assert pc.contribution_post({"question": {"post_id": 9, "id": 99}}) == 9
    assert pc.contribution_post({"question": {"id": 99}}) is None
    assert pc.contribution_score({"score": 1.5}) == 1.5 and pc.contribution_score({"x": 1}) is None


def test_eras_and_table():
    posts = [{"id": 1, "question": {"id": 11, "type": "binary", "open_time": "2026-10-01T00:00:00Z"}},
             {"id": 2, "question": {"id": 12, "type": "numeric", "open_time": "2026-10-01T00:00:00Z"}},
             {"id": 3, "question": {"id": 13, "type": "date"}}]
    info = pc.post_info(posts, {2: "2026-10-22T10:00:00+00:00"})  # our forecast time decides
    assert info[1] == ("binary", "before") and info[2] == ("numeric", "after") and 3 not in info
    assert info[11] == info[1]
    lines = pc.table({"us": [(1, 10.0), (11, 20.0), (2, -5.0)]}, info, {"us": 112})
    assert "| us (112) | before | +15.0 (n=2) | - | - | - | +15.0 (n=2) |" in lines
    assert "| us (112) | after | - | - | -5.0 (n=1) | - | -5.0 (n=1) |" in lines


class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def test_fetch_tries_the_next_path_and_retries_429(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "x")
    answers = iter([_Resp(429), _Resp(404), _Resp(200, {"contributions": [{"post_id": 1, "score": 2.0}, "junk"]})])
    seen = []

    def get(url, **kw):
        seen.append(url)
        return next(answers)

    items, note = pc.fetch_contributions(5, get=get, sleep=lambda s: None)
    assert items == [{"post_id": 1, "score": 2.0}] and note.endswith("200")
    assert "medals/contributions/" in seen[0] and "medals/contributions/" in seen[1] and "leaderboards/project/" in seen[2]


def test_fetch_gives_up_with_the_statuses(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "x")
    items, note = pc.fetch_contributions(5, get=lambda url, **kw: _Resp(404), sleep=lambda s: None)
    assert items is None and note.count("HTTP 404") == 2
