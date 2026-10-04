"""Raw tournament listing (architect, 4 Oct): every post kind, every page, no library."""
from __future__ import annotations

from types import SimpleNamespace

import tournament_info


def test_raw_row_kinds_and_ours():
    q = {"question": {"type": "date", "open_time": "2026-10-01T14:00:00Z", "scheduled_close_time": "2026-10-01T17:00:00Z",
                      "my_forecasts": {"latest": {"start_time": 1}}}, "id": 1, "status": "closed"}
    assert tournament_info.raw_row(q) == {"id": 1, "kind": "date", "state": "closed", "opens": "2026-10-01T14:00:00Z",
                                          "closes": "2026-10-01T17:00:00Z", "ours": True}
    assert tournament_info.raw_row({"id": 2, "group_of_questions": {"questions": []}})["kind"] == "group_of_questions"
    assert tournament_info.raw_row({"id": 3, "conditional": {}})["kind"] == "conditional"
    assert tournament_info.raw_row({"id": 4, "notebook": {}})["kind"] == "notebook"
    assert tournament_info.raw_row({"id": 5, "question": {"type": "binary"}})["ours"] is False


def test_raw_posts_reads_every_page(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "x")
    pages = {0: {"results": [{"id": i, "question": {"type": "binary"}} for i in range(100)], "next": "more"},
             100: {"results": [{"id": 100, "question": {"type": "numeric"}}], "next": None}}
    seen = []

    def get(url, params, headers, timeout):
        seen.append(params)
        assert "statuses" not in params and "forecast_type" not in params  # no filters
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: pages[params["offset"]])

    rows = tournament_info.raw_posts(33121, get=get)
    assert len(rows) == 101 and [p["offset"] for p in seen] == [0, 100]
