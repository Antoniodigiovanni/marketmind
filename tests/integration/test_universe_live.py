"""Live contract checks for index membership and symbol-resolution sources."""

from __future__ import annotations

import pytest

from data.universe.sources.sp500 import fetch_cspx_holdings
from data.universe.sources.stoxx import fetch_selection_list
from data.universe.sources.yahoo import search_by_id, symbol_has_data

pytestmark = pytest.mark.live


def test_live_blackrock_holdings_contract() -> None:
    """Parse the current CSPX fund document through the production source."""
    frame = fetch_cspx_holdings()

    assert frame.columns == ["ticker", "name", "sector", "weight", "currency"]
    assert frame.height >= 450
    assert frame["ticker"].drop_nulls().len() == frame.height


def test_live_stoxx_selection_contract() -> None:
    """Discover and parse the current official STOXX Europe 600 list."""
    frame = fetch_selection_list()

    assert frame.columns == [
        "internal_key",
        "ric",
        "name",
        "country",
        "exchange",
        "currency",
        "rank",
    ]
    assert 550 <= frame.height <= 600
    assert frame["internal_key"].n_unique() == frame.height
    assert frame["rank"].max() <= 600


def test_live_yahoo_search_contract() -> None:
    """Resolve Apple's ISIN through Yahoo's search response."""
    result = search_by_id("US0378331005", name_hint="Apple Inc")

    assert result is not None
    assert result["symbol"] == "AAPL"
    assert result["name"]


def test_live_yahoo_chart_contract() -> None:
    """Validate that Yahoo's chart response still exposes timestamped bars."""
    assert symbol_has_data("AAPL")
