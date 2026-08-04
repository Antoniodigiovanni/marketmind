"""Tests for Yahoo lookup/validation helpers (mocked, no live calls)."""

from __future__ import annotations

import json

import pytest
import requests

from data.universe.sources.yahoo import search_by_id, symbol_has_data


def _fake_session(payload: dict, status: int = 200) -> object:
    class FakeResponse:
        status_code = status
        text = json.dumps(payload)

        def json(self) -> dict:
            return payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

    class FakeSession:
        def get(self, url: str, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    return FakeSession()


def test_search_by_id_prefers_equity_quote() -> None:
    payload = {
        "quotes": [
            {"symbol": "AAPL", "quoteType": "ETF", "shortname": "Apple ETF"},
            {"symbol": "AAPL", "quoteType": "EQUITY", "shortname": "Apple Inc."},
        ]
    }
    result = search_by_id("US0378331005", session=_fake_session(payload))
    assert result == {"symbol": "AAPL", "name": "Apple Inc."}


def test_search_by_id_returns_none_without_quotes() -> None:
    assert search_by_id("DE0000000000", session=_fake_session({"quotes": []})) is None


def test_search_by_id_raises_on_http_error() -> None:
    with pytest.raises(requests.HTTPError):
        search_by_id("US0378331005", session=_fake_session({}, status=500))


def test_symbol_has_data_true_when_chart_returns_bars() -> None:
    payload = {"chart": {"result": [{"timestamp": [1, 2, 3]}]}}
    assert symbol_has_data("AAPL", session=_fake_session(payload)) is True


def test_symbol_has_data_false_when_chart_empty() -> None:
    assert symbol_has_data("NOPE", session=_fake_session({"chart": {"result": []}})) is False
    assert symbol_has_data("NOPE", session=_fake_session({}, status=404)) is False


def test_search_retries_transient_connection_errors() -> None:
    payload = {"quotes": [{"symbol": "AAPL", "quoteType": "EQUITY", "shortname": "Apple Inc."}]}
    calls = {"count": 0}

    class FlakySession:
        def get(self, url: str, **kwargs: object) -> object:
            calls["count"] += 1
            if calls["count"] < 3:
                raise requests.exceptions.ConnectionError("flaky")
            return _fake_session(payload).get(url, **kwargs)

    result = search_by_id("US0378331005", session=FlakySession())
    assert result == {"symbol": "AAPL", "name": "Apple Inc."}
    assert calls["count"] == 3


def test_chart_retries_transient_connection_errors() -> None:
    payload = {"chart": {"result": [{"timestamp": [1, 2, 3]}]}}
    calls = {"count": 0}

    class FlakySession:
        def get(self, url: str, **kwargs: object) -> object:
            calls["count"] += 1
            if calls["count"] < 3:
                raise requests.exceptions.ConnectionError("flaky")
            return _fake_session(payload).get(url, **kwargs)

    assert symbol_has_data("AAPL", session=FlakySession()) is True
    assert calls["count"] == 3
