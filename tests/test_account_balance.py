"""Daily health: the OpenRouter ACCOUNT balance, and the alert under $4 (once a UTC day)."""
from __future__ import annotations

import health_check


def test_balance_ok_line():
    report = health_check.Report()
    health_check.check_account_balance(200, {"total_credits": 90, "total_usage": 79.348}, report, "2026-10-22",
                                       notify=lambda *a: (_ for _ in ()).throw(AssertionError("no alert")))
    assert report.ok == ["OpenRouter account balance $10.65 (bought $90.00, used $79.35)"] and not report.warnings


def test_balance_under_4_alerts_once_with_the_date():
    report, sent = health_check.Report(), []
    health_check.check_account_balance(200, {"total_credits": 90, "total_usage": 86.5}, report, "2026-10-22",
                                       notify=lambda title, body: sent.append(title) or True)
    assert sent == ["OpenRouter account balance under $4 (2026-10-22)"]
    assert report.warnings == ["OpenRouter account balance $3.50 (bought $90.00, used $86.50): under $4.00, Tony adds $10"]
    assert not report.red


def test_balance_alert_not_opened_and_not_readable():
    report = health_check.Report()
    health_check.check_account_balance(200, {"total_credits": 10, "total_usage": 9}, report, "d", notify=lambda *a: False)
    assert report.warnings[0].endswith("(alert issue not opened)")
    report = health_check.Report()
    health_check.check_account_balance(403, None, report, "d")
    assert report.warnings == ["OpenRouter account balance not readable (HTTP 403)"] and not report.red
