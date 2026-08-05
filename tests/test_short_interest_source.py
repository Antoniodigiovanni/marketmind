"""Tests for the FINRA short interest source (mocked HTTP, no live calls)."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
import requests

from data.sources import finra

CSV_BODY = (
    "accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|"
    "marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity|"
    "stockSplitFlag|averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|"
    "changePercent|changePreviousNumber|settlementDate\n"
    "20260715|A|Agilent Technologies Inc.|A|NYSE|7538437|5331733||2136993|3.53||"
    "41.39|2206704|2026-07-15\n"
    "20260715|BRK.B|Berkshire Hathaway Inc.|B|NYSE|1000|900||500|||-10||2026-07-15\n"
)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, content: bytes = b"", status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def test_fetch_short_interest_file_parses_csv(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        return FakeResponse(CSV_BODY.encode())

    monkeypatch.setattr(finra.requests, "get", fake_get)

    frame = finra.fetch_short_interest_file(dt.date(2026, 7, 15))

    assert captured["url"] == finra.SHORT_INTEREST_URL.format(date="20260715")
    assert "User-Agent" in captured["headers"]
    assert frame.columns == [
        "symbol", "symbol_code", "issue_name", "market_class",
        "current_short", "previous_short", "avg_daily_volume",
        "days_to_cover", "change_pct", "settlement_date",
    ]
    assert frame["symbol"].to_list() == ["A", "BRK-B"]
    assert frame["days_to_cover"].to_list() == [3.53, None]
    assert frame["settlement_date"].to_list() == [dt.date(2026, 7, 15)] * 2
    assert frame["current_short"].to_list() == [7_538_437.0, 1000.0]


def test_fetch_short_interest_file_not_published(monkeypatch) -> None:
    monkeypatch.setattr(
        finra.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(b"", status_code=403),
    )
    frame = finra.fetch_short_interest_file(dt.date(2026, 7, 31))
    assert frame.is_empty()
    assert frame.schema == finra.COLUMNS


def test_fetch_short_interest_file_raises_on_other_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        finra.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(b"", status_code=500),
    )
    with pytest.raises(requests.HTTPError):
        finra.fetch_short_interest_file(dt.date(2026, 7, 15))
