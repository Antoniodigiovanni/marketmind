"""Tests for the keyless Wikipedia company-description fetcher."""

from __future__ import annotations

import json

import requests

from data.sources.wikipedia import fetch_company_description, fetch_wikipedia_context


class _FakeSession:
    def __init__(
        self,
        summary_by_title: dict[str, int],
        search_hits: list[dict],
        *,
        summary_payload: dict | None = None,
    ) -> None:
        self.summary_by_title = summary_by_title
        self.search_hits = search_hits
        self.summary_payload = summary_payload
        self.requested_summaries: list[str] = []
        self.search_called = False

    def get(self, url: str, **kwargs) -> requests.Response:
        if url.startswith("https://en.wikipedia.org/api/rest_v1/page/summary/"):
            title = url.rsplit("/", 1)[1]
            self.requested_summaries.append(title)
            status = self.summary_by_title.get(title, 404)
            payload = {"extract": "Apple Inc. designs consumer electronics."}
            if self.summary_payload:
                payload.update(self.summary_payload)
            return self._response(
                status,
                payload if status == 200 else {},
            )
        self.search_called = True
        return self._response(
            200,
            {"query": {"search": self.search_hits}},
        )

    @staticmethod
    def _response(status_code: int, payload: dict) -> requests.Response:
        response = requests.Response()
        response.status_code = status_code
        response._content = json.dumps(payload).encode("utf-8")
        return response


def test_fetch_company_description_uses_summary_endpoint() -> None:
    session = _FakeSession(summary_by_title={"Apple_Inc.": 200}, search_hits=[])

    description = fetch_company_description("Apple Inc.", session=session)

    assert description == "Apple Inc. designs consumer electronics."
    assert session.requested_summaries == ["Apple_Inc."]
    assert not session.search_called


def test_fetch_company_description_falls_back_to_search() -> None:
    session = _FakeSession(
        summary_by_title={"Charles_Schwab_Corp.": 404, "Charles_Schwab_Corporation": 200},
        search_hits=[{"title": "Charles Schwab Corporation"}],
    )

    description = fetch_company_description("Charles Schwab Corp.", session=session)

    assert description == "Apple Inc. designs consumer electronics."
    assert session.search_called


def test_fetch_company_description_uses_first_comma_part() -> None:
    session = _FakeSession(summary_by_title={"Apple": 200}, search_hits=[])

    fetch_company_description("Apple, Inc.", session=session)

    assert session.requested_summaries == ["Apple"]


def test_fetch_company_description_empty_when_no_hits() -> None:
    session = _FakeSession(summary_by_title={"Nope_Corp": 404}, search_hits=[])

    assert fetch_company_description("Nope Corp", session=session) == ""


def test_fetch_wikipedia_context_returns_extract_item_and_page_url() -> None:
    session = _FakeSession(
        summary_by_title={"Apple_Inc.": 200},
        search_hits=[],
        summary_payload={
            "wikibase_item": "Q312",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Apple_Inc."}},
        },
    )

    context = fetch_wikipedia_context("Apple Inc.", session=session)

    assert context["extract"] == "Apple Inc. designs consumer electronics."
    assert context["wikibase_item"] == "Q312"
    assert context["page_url"] == "https://en.wikipedia.org/wiki/Apple_Inc."


def test_fetch_wikipedia_context_falls_back_to_search() -> None:
    session = _FakeSession(
        summary_by_title={"Charles_Schwab_Corp.": 404, "Charles_Schwab_Corporation": 200},
        search_hits=[{"title": "Charles Schwab Corporation"}],
        summary_payload={"wikibase_item": "Q215546"},
    )

    context = fetch_wikipedia_context("Charles Schwab Corp.", session=session)

    assert context["wikibase_item"] == "Q215546"
    assert context["extract"].startswith("Apple")


def test_fetch_wikipedia_context_empty_when_nothing_found() -> None:
    session = _FakeSession(summary_by_title={"Nope_Corp": 404}, search_hits=[])

    assert fetch_wikipedia_context("Nope Corp", session=session) == {
        "extract": "",
        "wikibase_item": None,
        "page_url": None,
    }
