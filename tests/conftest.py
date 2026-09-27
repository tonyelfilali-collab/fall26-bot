"""Shared test setup: no network from the unit tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_market_lookups(monkeypatch):
    """Market matching (Step 9) makes web calls: off unless a test turns it on."""
    import main

    monkeypatch.setattr(main, "MARKET_MODE", "off")
