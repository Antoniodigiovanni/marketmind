"""Tests for Wikidata official-website lookup and bounded website crawling."""

from __future__ import annotations

import json

import requests

from data.sources.website import crawl_website, fetch_official_website_url


class _FakeSession:
    def __init__(self, pages: dict[str, str], robots: str | None = "*\n") -> None:
        self.pages = pages
        self.robots = robots
        self.requested: list[str] = []

    def get(self, url: str, **kwargs) -> requests.Response:
        self.requested.append(url)
        response = requests.Response()
        response.status_code = 200
        params = kwargs.get("params") or {}
        if "wbgetclaims" in url or params.get("action") == "wbgetclaims":
            response._content = json.dumps(
                {
                    "claims": {
                        "P856": [
                            {"mainsnak": {"datavalue": {"value": "https://www.apple.com"}}}
                        ]
                    }
                }
            ).encode("utf-8")
        elif url.endswith("/robots.txt"):
            response._content = (self.robots or "").encode("utf-8")
        elif url in self.pages:
            response._content = self.pages[url].encode("utf-8")
        else:
            response.status_code = 404
        return response


HOMEPAGE = (
    '<html><body><h1>Apple</h1>'
    '<a href="/about">About</a>'
    '<a href="/private/secret">Secret</a>'
    '<a href="https://evil.example.com/">Evil</a>'
    "<p>We design consumer electronics.</p></body></html>"
)
ABOUT = '<html><body><h1>About</h1><a href="/products">Products</a><p>We make phones.</p></body></html>'
PRODUCTS = '<html><body><h1>Products</h1><p>Phones and laptops.</p></body></html>'


def test_fetch_official_website_url_returns_first_claim() -> None:
    session = _FakeSession(pages={})

    url = fetch_official_website_url("Q312", session=session)

    assert url == "https://www.apple.com"


def test_fetch_official_website_url_none_without_claims() -> None:
    session = _FakeSession(pages={})
    session.robots = None

    class _NoClaims:
        def get(self, url: str, **kwargs) -> requests.Response:
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"claims": {}}'
            return response

    assert fetch_official_website_url("Q0", session=_NoClaims()) is None


def test_crawl_website_same_domain_and_robots() -> None:
    session = _FakeSession(
        pages={
            "https://apple.com/": HOMEPAGE,
            "https://apple.com/about": ABOUT,
            "https://apple.com/products": PRODUCTS,
        },
        robots="User-agent: *\nDisallow: /private\n",
    )

    pages = crawl_website("https://apple.com/", session=session)

    joined = " ".join(pages)
    assert "We design consumer electronics." in joined
    assert "We make phones." in joined
    assert "https://evil.example.com/" not in session.requested
    assert "/private" not in joined


def test_crawl_website_honours_page_cap() -> None:
    session = _FakeSession(pages={"https://apple.com/": HOMEPAGE})

    pages = crawl_website("https://apple.com/", session=session, max_pages=1)

    assert len(pages) == 1


def test_crawl_website_honours_char_caps() -> None:
    long_page = "<html><body>" + "<p>word " * 5000 + "</p></body></html>"
    session = _FakeSession(pages={"https://apple.com/": long_page})

    pages = crawl_website(
        "https://apple.com/",
        session=session,
        max_pages=2,
        max_chars_per_page=3000,
        total_char_cap=5000,
    )

    assert len(pages) == 1
    assert len(pages[0]) <= 3000
