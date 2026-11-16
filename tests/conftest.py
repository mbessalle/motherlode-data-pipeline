"""Tests run offline: no market lookups and no buyer check, whatever is installed or set."""
import pytest


@pytest.fixture(autouse=True)
def no_market_lookups(monkeypatch):
    monkeypatch.setenv("PIPELINE_MARKET_LOOKUP", "0")
    monkeypatch.setenv("PIPELINE_BUYER_CHECK", "0")
