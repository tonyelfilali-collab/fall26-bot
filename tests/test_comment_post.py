"""The private comment (comment_post.py): status and id recorded, a failure never raises, read back."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import requests

import health_check
from comment_post import COMMENT_FAILED_ANNOTATION, CommentRecordingClient


def _client(monkeypatch, status=201, body=None, raises=None):
    monkeypatch.setenv("METACULUS_TOKEN", "t")
    client = CommentRecordingClient(sleep_seconds_between_requests=0, sleep_jitter_seconds=0)
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json))
        if raises:
            raise raises
        return SimpleNamespace(status_code=status, json=lambda: body if body is not None else {})

    monkeypatch.setattr(requests, "post", post)
    return client, sent


def test_comment_posted_records_status_and_id(monkeypatch, caplog, capsys):
    client, sent = _client(monkeypatch, body={"id": 555, "text": "secret"})
    with caplog.at_level(logging.INFO, logger="fall26"):
        client.post_question_comment(46180, "the reasoning")
    assert client.outcome == {"status": 201, "id": 555, "private": True}
    assert sent[0][0].endswith("/comments/create/") and sent[0][1]["is_private"] is True
    assert "comment posted (HTTP 201, id returned)" in caplog.text and "reasoning" not in caplog.text
    assert COMMENT_FAILED_ANNOTATION not in capsys.readouterr().out


@pytest.mark.parametrize("kwargs, status", [({"status": 500}, 500), ({"raises": requests.ConnectionError()}, "error (ConnectionError)")])
def test_comment_failure_never_raises_and_annotates(monkeypatch, capsys, kwargs, status):
    client, _ = _client(monkeypatch, **kwargs)
    client.post_question_comment(46180, "the reasoning")  # no exception
    assert client.outcome["status"] == status and client.outcome["id"] is None
    out = capsys.readouterr().out
    assert f"::warning title={COMMENT_FAILED_ANNOTATION}::Question 46180" in out and "reasoning" not in out


def _get(by_id=200, by_post=200, results=()):
    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/comments/"):
            return SimpleNamespace(status_code=by_post, json=lambda: {"results": list(results)})
        return SimpleNamespace(status_code=by_id, json=lambda: {})
    return get


def test_read_back(monkeypatch):
    client, _ = _client(monkeypatch)
    ours = {"id": 555, "author": {"id": 309271}}
    found = client.read_back(46180, 555, get=_get(results=[ours, {"id": 1, "author": 3}]))
    assert found == {"by_id_status": 200, "by_id": True, "by_post_status": 200, "by_post_count": 1, "by_post": True, "exists": True}
    found = client.read_back(46180, None, get=_get(by_post=200, results=[]))
    assert found["exists"] is False and "by_id_status" not in found
    found = client.read_back(46180, 555, get=_get(by_id=404, results=[]))
    assert found["exists"] is False and found["by_id_status"] == 404


def test_health_warns_on_comment_failures():
    report = health_check.Report()
    health_check.check_log_failures([COMMENT_FAILED_ANNOTATION], report)
    assert any("comment post failure" in w for w in report.warnings) and not report.red
    assert any("No JSON log" in o for o in report.ok)


@pytest.mark.parametrize("run_mode", ["tournament", "test_questions"])
def test_submit_records_the_comment_in_the_private_log(monkeypatch, caplog, run_mode):
    import asyncio

    import main

    _client(monkeypatch, body={"id": 555})
    monkeypatch.setattr(requests, "get", _get(results=[{"id": 555, "author": {"id": 309271}}]))
    bot = object.__new__(main.FallBot2026)
    question = SimpleNamespace(id_of_post=46180, id_of_question=46409, state=None, actual_resolution_time=None,
                               resolution_string=None, close_time=None)
    record: dict = {}
    bot.__dict__.update(fail_submission=False, submit_forecasts=True, run_mode=run_mode, _question_records={id(question): record},
                        metaculus_client=SimpleNamespace(get_question_by_post_id=lambda *a: question))
    posted = []

    class Report:
        async def publish_report_to_metaculus(self, metaculus_client):
            posted.append("forecast")
            metaculus_client.post_question_comment(46180, "the reasoning")

    with caplog.at_level(logging.INFO, logger="fall26"):
        assert asyncio.run(bot._submit_if_still_open(question, Report())) is True
    assert posted == ["forecast"] and record["comment"]["status"] == 201 and record["comment"]["id"] == 555
    if run_mode == "test_questions":
        assert record["comment"]["read_back"]["exists"] is True
        assert "comment read back: yes (create HTTP 201, by id HTTP 200, by post+author HTTP 200, 1 found)" in caplog.text
    else:
        assert "read_back" not in record["comment"] and "read back" not in caplog.text
