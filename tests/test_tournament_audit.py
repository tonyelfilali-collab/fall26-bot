"""Tournament info --audit: every question and whether we forecast it (fake client)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forecasting_tools import BinaryQuestion

import tournament_info


def test_audit_flags_closed_questions_we_missed(monkeypatch):
    now = datetime.now(timezone.utc)

    def q(post, forecast, closes_in_hours):
        return BinaryQuestion(
            question_text=f"Q{post}?", id_of_post=post, page_url=f"https://www.metaculus.com/questions/{post}",
            open_time=now - timedelta(hours=10), close_time=now + timedelta(hours=closes_in_hours),
            already_forecasted=forecast,
        )

    questions = [q(1, True, -2), q(2, False, -1), q(3, False, 5)]

    class FakeClient:
        async def get_questions_matching_filter(self, api_filter):
            assert api_filter.allowed_tournaments == [33125]
            assert set(api_filter.allowed_statuses) == {"upcoming", "open", "closed", "resolved"}
            return questions

    monkeypatch.setattr(tournament_info, "MetaculusClient", FakeClient)
    lines = tournament_info.audit_lines(33125)
    assert "Questions in any state: 3; forecast by us: 1; closed without our forecast: 1" in lines[0]
    assert sum("**no**" in line for line in lines) == 2  # 2 closed-missed, 3 still open
