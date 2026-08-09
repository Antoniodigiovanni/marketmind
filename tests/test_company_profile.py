"""Tests for generated company profiles (description + categories)."""

from __future__ import annotations

import json

import pytest

from agent.company_profile import (
    MAX_CATEGORIES,
    _coerce_categories,
    build_company_profile,
    generate_profile,
)


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


def test_generate_profile_bounds_wikipedia_input() -> None:
    llm, captured = _fake_llm({"description": "x", "products_services": ["category"]})

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


def _llm_returning(payload: str):
    def _llm(messages: list[dict[str, str]]) -> str:
        return payload

    return _llm


def test_coerce_categories_passes_lists_through() -> None:
    assert _coerce_categories(["Payments", "Banking"]) == ["Payments", "Banking"]


def test_coerce_categories_splits_a_comma_separated_string() -> None:
    assert _coerce_categories("Payments, Banking, Capital Markets") == [
        "Payments",
        "Banking",
        "Capital Markets",
    ]


def test_coerce_categories_splits_a_bulleted_string() -> None:
    assert _coerce_categories("- Payments\n- Banking\n") == ["Payments", "Banking"]


def test_coerce_categories_takes_dict_values() -> None:
    assert _coerce_categories({"a": "Payments", "b": "Banking"}) == ["Payments", "Banking"]


def test_coerce_categories_handles_none_and_scalars() -> None:
    assert _coerce_categories(None) == []
    assert _coerce_categories(42) == ["42"]


def test_coerce_categories_caps_at_max() -> None:
    assert len(_coerce_categories([f"c{i}" for i in range(50)])) == 12


def test_generate_profile_survives_a_string_products_services() -> None:
    payload = json.dumps(
        {"description": "A payments company.", "products_services": "Payments, Banking"}
    )

    profile = generate_profile("FIS", "wiki text", "site text", _llm_returning(payload))

    assert profile["description"] == "A payments company."
    assert profile["products_services"] == ["Payments", "Banking"]


def test_generate_profile_survives_missing_products_services() -> None:
    payload = json.dumps({"description": "A payments company."})

    profile = generate_profile("FIS", "wiki text", "site text", _llm_returning(payload))

    assert profile["products_services"] == []


def test_generate_profile_still_requires_a_description() -> None:
    payload = json.dumps({"products_services": ["Payments"]})

    with pytest.raises(ValueError, match="description"):
        generate_profile("FIS", "wiki text", "site text", _llm_returning(payload))
