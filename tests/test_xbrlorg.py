"""Tests for the filings.xbrl.org JSON-API client (mocked HTTP)."""

from __future__ import annotations

import requests

from data.sources.xbrlorg import EsefFiling, XbrlorgClient


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> object:
        return self._payload

    @property
    def content(self) -> bytes:
        return b"PK\x03\x04fakezip"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected URL: {url}")
        return FakeResponse(self.routes[url])


def _page(fxo_id: str, lei: str, name: str, country: str, next_url: str | None) -> dict[str, object]:
    return {
        "data": [
            {
                "id": fxo_id,
                "attributes": {
                    "country": country,
                    "period_end": "2020-12-31",
                    "date_added": "2022-01-10",
                    "processed": "2022-01-11",
                    "package_url": f"https://filings.xbrl.org/{fxo_id}/package.zip",
                    "report_url": f"https://filings.xbrl.org/{fxo_id}/report.xhtml",
                    "json_url": f"https://filings.xbrl.org/{fxo_id}/report.json",
                    "sha256": "abc123",
                },
                "relationships": {"entity": {"data": {"id": f"E-{fxo_id}", "type": "entity"}}},
            }
        ],
        "included": [
            {
                "id": f"E-{fxo_id}",
                "type": "entity",
                "attributes": {"identifier": lei, "name": name},
            }
        ],
        "links": {"next": next_url},
    }


def test_get_filings_follows_pagination_and_parses_entity(monkeypatch) -> None:
    page2 = "https://filings.xbrl.org/api/filings?page[number]=2"
    session = FakeSession(
        {
            "https://filings.xbrl.org/api/filings?filter[country]=IT&page[size]=200&include=entity&sort=-processed": _page(
                "ESEF/IT/1", "815600AB12CD34EF5678", "Banco BPM S.p.A.", "IT", page2
            ),
            page2: _page("ESEF/IT/2", "969500Y", "Enel S.p.A.", "IT", None),
        }
    )
    client = XbrlorgClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    filings = client.get_filings("IT")

    assert len(filings) == 2
    assert filings[0] == EsefFiling(
        fxo_id="ESEF/IT/1",
        lei="815600AB12CD34EF5678",
        entity_name="Banco BPM S.p.A.",
        country="IT",
        filing_system="ESEF",
        period_end="2020-12-31",
        date_added="2022-01-10",
        package_url="https://filings.xbrl.org/ESEF/IT/1/package.zip",
        xhtml_url="https://filings.xbrl.org/ESEF/IT/1/report.xhtml",
        json_url="https://filings.xbrl.org/ESEF/IT/1/report.json",
        sha256="abc123",
    )
    assert filings[1].fxo_id == "ESEF/IT/2"
    assert len(session.calls) == 2


def test_get_filings_maps_gb_to_uksef(monkeypatch) -> None:
    session = FakeSession(
        {
            "https://filings.xbrl.org/api/filings?filter[country]=GB&page[size]=200&include=entity&sort=-processed": _page(
                "UKSEF/GB/1", "213800Y", "Halma plc", "GB", None
            )
        }
    )
    client = XbrlorgClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    filings = client.get_filings("GB")

    assert filings[0].filing_system == "UKSEF"
    assert filings[0].entity_name == "Halma plc"


def test_download_returns_bytes(monkeypatch) -> None:
    url = "https://filings.xbrl.org/ESEF/IT/1/package.zip"
    session = FakeSession({url: b"ignored"})
    client = XbrlorgClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)
    assert client.download(url) == b"PK\x03\x04fakezip"
