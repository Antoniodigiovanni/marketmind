"""Tests for the SEC EDGAR client (mocked HTTP, no live calls)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from data.sources.sec import EdgardClient, RateLimiter, Submission

FIXTURES = Path(__file__).parent / "fixtures" / "sec"


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> object:
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload

    @property
    def text(self) -> str:
        return str(self._payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    def __init__(self, routes: dict[str, FakeResponse]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected URL: {url}")
        return self.routes[url]


def test_rate_limiter_wait_does_not_block_first_call(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("data.universe.sources.http.time.sleep", lambda s: sleeps.append(s))
    limiter = RateLimiter(max_per_second=100.0)
    limiter.wait()
    assert sleeps == []


def test_load_company_tickers_pads_cik(monkeypatch) -> None:
    raw = (FIXTURES / "company_tickers.json").read_text(encoding="utf-8")
    session = FakeSession({})
    session.get = lambda url, **kw: FakeResponse(raw)  # type: ignore[method-assign]
    client = EdgardClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    tickers = client.load_company_tickers()

    assert tickers["AAPL"] == {"cik": "0000320193", "name": "Apple Inc."}
    assert tickers["MSFT"]["cik"] == "0000789019"


def test_submissions_parses_recent_filings(monkeypatch) -> None:
    raw = (FIXTURES / "submissions_aapl.json").read_text(encoding="utf-8")
    session = FakeSession({})
    session.get = lambda url, **kw: FakeResponse(raw)  # type: ignore[method-assign]
    client = EdgardClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    subs = client.submissions("0000320193")

    assert len(subs) == 3
    assert subs[0] == Submission(
        accession="000032019326000045",
        form_type="8-K",
        filed_at="2026-08-03",
        period_of_report="2026-08-03",
        primary_document="form8k.htm",
    )
    assert subs[2].form_type == "4"


def test_download_returns_text(monkeypatch) -> None:
    session = FakeSession({})
    session.get = lambda url, **kw: FakeResponse("<html>8-K</html>")  # type: ignore[method-assign]
    client = EdgardClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    text = client.download("0000320193", "000032019326000045", "form8k.htm")

    assert text == "<html>8-K</html>"


def test_get_json_retries_on_429(monkeypatch) -> None:
    responses = [
        FakeResponse("{}", status=429),
        FakeResponse("{}", status=429),
        FakeResponse({"ok": True}),
    ]
    session = FakeSession({})
    session.get = lambda url, **kw: responses.pop(0)  # type: ignore[method-assign]
    client = EdgardClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    assert client.get_json("https://example.com/x") == {"ok": True}
    assert len(responses) == 0


def test_filing_index_returns_file_list(monkeypatch) -> None:
    payload = {
        "directory": {
            "item": [
                {"name": "form13fInfoTable.xml", "type": "xml"},
                {"name": "primary.xml", "type": "xml"},
            ]
        }
    }
    session = FakeSession({})
    session.get = lambda url, **kw: FakeResponse(payload)  # type: ignore[method-assign]
    client = EdgardClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    files = client.filing_index("0000320193", "000000000026000001")

    assert files == [
        {"name": "form13fInfoTable.xml", "type": "xml"},
        {"name": "primary.xml", "type": "xml"},
    ]


def test_submissions_missing_cik_raises(monkeypatch) -> None:
    session = FakeSession({})
    session.get = lambda url, **kw: FakeResponse({}, status=404)  # type: ignore[method-assign]
    client = EdgardClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    with pytest.raises(requests.HTTPError):
        client.submissions("0000000000")


def test_companyfacts_uses_official_xbrl_endpoint(monkeypatch) -> None:
    raw = (FIXTURES / "companyfacts_aapl.json").read_text(encoding="utf-8")
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    session = FakeSession({url: FakeResponse(raw)})
    client = EdgardClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    payload = client.companyfacts("0000320193")

    assert payload["entityName"] == "Apple Inc."
    assert session.calls == [url]


def test_all_submissions_follows_pagination_files(monkeypatch) -> None:
    first = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000000000-26-000001"],
                "filingDate": ["2026-08-01"],
                "form": ["8-K"],
                "primaryDocument": ["a.htm"],
                "reportDate": ["2026-08-01"],
            },
            "files": [{"name": "CIK0000000000-submissions-001.json"}],
        }
    }
    second = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000000000-25-000001"],
                "filingDate": ["2025-08-01"],
                "form": ["10-K"],
                "primaryDocument": ["b.htm"],
                "reportDate": ["2025-06-30"],
            },
            "files": [],
        }
    }
    pages = [first, second]
    session = FakeSession({})
    session.get = lambda url, **kw: FakeResponse(pages.pop(0))  # type: ignore[method-assign]
    client = EdgardClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    subs = client.all_submissions("0000000000")

    assert len(subs) == 2
    assert subs[0].form_type == "8-K"
    assert subs[1].form_type == "10-K"
