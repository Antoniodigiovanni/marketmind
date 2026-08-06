"""Tests for the GLEIF LEI search client (mocked HTTP, no live calls)."""

from __future__ import annotations

import requests

from data.sources.gleif import GleifClient, LeiCandidate


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.payload: object = {"data": []}

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append((url, kwargs))
        return FakeResponse(self.payload)


def test_search_parses_candidates_and_filters_by_country(monkeypatch) -> None:
    session = FakeSession()
    session.payload = {
        "data": [
            {
                "attributes": {
                    "lei": "815600AB12CD34EF5678",
                    "entity": {
                        "legalName": "Banco BPM S.p.A.",
                        "legalAddress": {"country": "IT"},
                    },
                    "registration": {"status": "ISSUED"},
                }
            }
        ]
    }
    client = GleifClient(session)  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)

    hits = client.search("BANCO BPM", "IT")

    assert hits == [
        LeiCandidate(
            lei="815600AB12CD34EF5678",
            legal_name="Banco BPM S.p.A.",
            country="IT",
            status="ISSUED",
        )
    ]
    url, kwargs = session.requests[0]
    assert "api.gleif.org" in url
    assert kwargs["params"]["filter[fulltext]"] == "BANCO BPM"
    assert kwargs["params"]["filter[entity.legalAddress.country]"] == "IT"


def test_search_returns_empty_list_for_empty_payload(monkeypatch) -> None:
    client = GleifClient(FakeSession())  # type: ignore[arg-type]
    monkeypatch.setattr(client._limiter, "wait", lambda: None)
    assert client.search("NOBODY", "IT") == []
