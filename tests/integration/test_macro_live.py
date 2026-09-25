"""Live contract checks for macroeconomic data providers."""

from __future__ import annotations

import pytest

from data.sources import ecb, fred

pytestmark = pytest.mark.live


def _assert_series_contract(frame) -> None:
    assert frame.columns == ["date", "value"]
    assert not frame.is_empty()
    assert frame["date"].is_not_null().all()
    assert frame["value"].drop_nulls().len() > 0


def test_live_ecb_series_contract() -> None:
    """Parse a stable EUR/USD observation window from the ECB API."""
    _assert_series_contract(ecb.fetch_series("EXR/D.USD.EUR.SP00.A", "2024-01-02", "2024-01-10"))


def test_live_fred_series_contract() -> None:
    """Parse a stable Treasury-yield observation window from FRED."""
    _assert_series_contract(fred.fetch_series("DGS10", "2024-01-02", "2024-01-10"))
