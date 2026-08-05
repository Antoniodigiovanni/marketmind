"""Tests for the FRED source client (mocked HTTP, no live calls)."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
import requests

from data.sources import fred


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload: object = None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self) -> object:
        return self._payload


def test_fetch_series_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FRED_API_KEY"):
        fred.fetch_series("DGS10", "2026-08-01", "2026-08-04")


def test_fetch_series_parses_observations(monkeypatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured["params"] = kwargs["params"]
        return FakeResponse(
            {
                "observations": [
                    {"date": "2026-08-03", "value": "4.09"},
                    {"date": "2026-08-04", "value": "."},
                    {"date": "2026-08-01", "value": "4.11"},
                ]
            }
        )

    monkeypatch.setattr(fred.requests, "get", fake_get)

    frame = fred.fetch_series("DGS10", "2026-08-01", "2026-08-04")

    assert captured["url"] == fred.FRED_OBSERVATIONS_URL
    params = captured["params"]
    assert params["series_id"] == "DGS10"
    assert params["observation_start"] == "2026-08-01"
    assert params["observation_end"] == "2026-08-04"
    assert frame.columns == ["date", "value"]
    assert frame["date"].to_list() == [
        dt.date(2026, 8, 1),
        dt.date(2026, 8, 3),
        dt.date(2026, 8, 4),
    ]
    assert frame["value"].to_list() == [4.11, 4.09, None]


def test_fetch_series_empty_observations(monkeypatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    monkeypatch.setattr(
        fred.requests, "get", lambda *args, **kwargs: FakeResponse({"observations": []})
    )

    frame = fred.fetch_series("DGS10", "2026-08-01", "2026-08-04")

    assert frame.is_empty()
    assert frame.schema == {"date": pl.Date, "value": pl.Float64}


def test_fetch_series_raises_on_http_error(monkeypatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    monkeypatch.setattr(
        fred.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=500)
    )

    with pytest.raises(requests.HTTPError):
        fred.fetch_series("DGS10", "2026-08-01", "2026-08-04")
