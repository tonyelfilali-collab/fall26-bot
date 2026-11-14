"""Shared test setup: no network from the unit tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_market_lookups(monkeypatch):
    """Market matching (Step 9) makes web calls: off unless a test turns it on."""
    import main

    monkeypatch.setattr(main, "MARKET_MODE", "off")


@pytest.fixture(autouse=True)
def fable_off_by_default(monkeypatch):
    """Fable 5.2 (bot_config.FRONTIER_FABLE_ENABLED, ON live since 12 Nov) is an
    extra Claude slot; the older plan tests check the lineup without it.
    tests/test_fable.py switches it on itself."""
    import bot_config

    monkeypatch.setattr(bot_config, "FRONTIER_FABLE_ENABLED", False)


@pytest.fixture(autouse=True)
def no_real_credit_test_call(monkeypatch):
    """The hourly credit test call (frontier.credit_test_call) never reaches Anthropic in tests."""
    import frontier

    def blocked() -> bool:
        raise AssertionError("a real credit test call in a test")

    monkeypatch.setattr(frontier, "credit_test_call", blocked)
