"""Tests for the ECB SDW source client (mocked HTTP, no live calls)."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
import requests

from data.sources import ecb

CSV_BODY = (
    "KEY,FREQ,CURRENCY,TIME_PERIOD,OBS_VALUE,TITLE\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,2026-08-03,1.1383,US dollar/Euro\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,2026-08-04,,US dollar/Euro\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,2026-08-01,1.1399,US dollar/Euro\n"
)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, content: bytes = b"", status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def test_fetch_series_parses_csv(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured["params"] = kwargs["params"]
        return FakeResponse(CSV_BODY.encode())

    monkeypatch.setattr(ecb.requests, "get", fake_get)

    frame = ecb.fetch_series("EXR/D.USD.EUR.SP00.A", "2026-08-01", "2026-08-04")

    assert captured["url"] == f"{ecb.ECB_DATA_URL}/EXR/D.USD.EUR.SP00.A"
    params = captured["params"]
    assert params["startPeriod"] == "2026-08-01"
    assert params["endPeriod"] == "2026-08-04"
    assert params["format"] == "csvdata"
    assert frame.columns == ["date", "value"]
    assert frame["date"].to_list() == [
        dt.date(2026, 8, 1),
        dt.date(2026, 8, 3),
        dt.date(2026, 8, 4),
    ]
    assert frame["value"].to_list() == [1.1399, 1.1383, None]


def test_fetch_series_empty_body(monkeypatch) -> None:
    monkeypatch.setattr(ecb.requests, "get", lambda *args, **kwargs: FakeResponse())

    frame = ecb.fetch_series("EST/B.EU000A2X2A25.WT", "2026-08-01", "2026-08-04")

    assert frame.is_empty()
    assert frame.schema == {"date": pl.Date, "value": pl.Float64}


def test_fetch_series_raises_on_http_error(monkeypatch) -> None:
    monkeypatch.setattr(
        ecb.requests, "get", lambda *args, **kwargs: FakeResponse(status_code=404)
    )

    with pytest.raises(requests.HTTPError):
        ecb.fetch_series("NOPE", "2026-08-01", "2026-08-04")
