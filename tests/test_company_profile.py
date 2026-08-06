"""Tests for generated company profiles (description + categories)."""

from __future__ import annotations

import json

import pytest

from agent.company_profile import MAX_CATEGORIES, build_company_profile, generate_profile


def _fake_llm(profile: dict) -> object:
    captured: dict = {}

    def llm(messages: list[dict[str, str]]) -> str:
        captured["user"] = messages[-1]["content"]
        return json.dumps(profile)

    return llm, captured


def test_generate_profile_parses_valid_output() -> None:
    llm, _ = _fake_llm(
        {
            "description": "Apple designs consumer electronics.",
            "products_services": ["smartphones", "laptops", "cloud services"],
        }
    )

    result = generate_profile("Apple Inc.", "wiki text", "site text", llm, model="deepseek-chat")

    assert result["description"] == "Apple designs consumer electronics."
    assert result["products_services"] == ["smartphones", "laptops", "cloud services"]
    assert result["generated_by"] == "deepseek-chat"


def test_generate_profile_caps_categories() -> None:
    llm, _ = _fake_llm(
        {
            "description": "x",
            "products_services": [f"category-{i}" for i in range(20)],
        }
    )

    result = generate_profile("Apple Inc.", "wiki", "site", llm)

    assert len(result["products_services"]) == MAX_CATEGORIES


def test_generate_profile_raises_on_missing_keys() -> None:
    llm, _ = _fake_llm({"description": "only a description"})

    with pytest.raises(ValueError, match="products_services"):
        generate_profile("Apple Inc.", "wiki", "site", llm)


def test_generate_profile_bounds_wikipedia_input() -> None:
    llm, captured = _fake_llm(
        {"description": "x", "products_services": ["category"]}
    )

    generate_profile("Apple Inc.", "w" * 10_000, "site", llm)

    user = captured["user"]
    assert len(user) < 3000  # wikipedia section truncated, prompt stays bounded


def test_build_company_profile_orchestrates_sources() -> None:
    wikipedia = lambda name: {  # noqa: E731
        "extract": "Apple designs electronics.",
        "wikibase_item": "Q312",
        "page_url": "https://en.wikipedia.org/wiki/Apple_Inc.",
    }
    website_url = lambda item: "https://www.apple.com"  # noqa: E731
    crawler = lambda url: ["Apple designs phones and laptops."]  # noqa: E731
    llm, _ = _fake_llm(
        {"description": "Apple designs electronics.", "products_services": ["smartphones"]}
    )

    result = build_company_profile(
        "Apple Inc.",
        wikipedia_fetcher=wikipedia,
        website_url_fetcher=website_url,
        crawler=crawler,
        llm=llm,
    )

    assert result is not None
    assert result["website_url"] == "https://www.apple.com"
    assert result["sources"] == {
        "wikipedia": "https://en.wikipedia.org/wiki/Apple_Inc.",
        "crawled": ["https://www.apple.com"],
    }


def test_build_company_profile_degrades_to_wikipedia_only() -> None:
    wikipedia = lambda name: {  # noqa: E731
        "extract": "Apple designs electronics.",
        "wikibase_item": "Q312",
        "page_url": "https://en.wikipedia.org/wiki/Apple_Inc.",
    }

    def broken_crawler(url: str) -> list[str]:
        raise RuntimeError("site down")

    llm, _ = _fake_llm(
        {"description": "Apple designs electronics.", "products_services": ["smartphones"]}
    )

    result = build_company_profile(
        "Apple Inc.",
        wikipedia_fetcher=wikipedia,
        website_url_fetcher=lambda item: "https://www.apple.com",
        crawler=broken_crawler,
        llm=llm,
    )

    assert result is not None
    assert result["sources"]["crawled"] == []


def test_build_company_profile_none_without_input() -> None:
    wikipedia = lambda name: {"extract": "", "wikibase_item": None, "page_url": None}  # noqa: E731

    result = build_company_profile(
        "Unknown Corp",
        wikipedia_fetcher=wikipedia,
        website_url_fetcher=lambda item: None,
        crawler=lambda url: [],
        llm=lambda messages: "{}",
    )

    assert result is None
