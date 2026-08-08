"""One-time per-company profile generation (description + categories)."""

from __future__ import annotations

import re
import string
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.llm import parse_json_response
from data.sources.website import crawl_website, fetch_official_website_url
from data.sources.wikipedia import fetch_wikipedia_context

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MAX_CATEGORIES = 12
WIKIPEDIA_CHAR_CAP = 2000

_CATEGORY_SPLIT = re.compile(r"[\n;,]|(?:^|\s)[-*•]\s")

WikipediaFetcher = Callable[[str], dict[str, str | None]]
WebsiteUrlFetcher = Callable[[str], str | None]
Crawler = Callable[[str], list[str]]
LLMCallable = Callable[[list[dict[str, str]]], str]


def _coerce_categories(value: object) -> list[str]:
    """Normalise a model-supplied products/services value into a list of strings.

    The model is asked for a JSON list but intermittently returns a
    comma-separated string, a bulleted block, or a mapping. Rejecting the whole
    profile over the shape of one field loses the description too, so coerce
    instead.

    Args:
        value: Whatever the model put in products_services.

    Returns:
        Up to MAX_CATEGORIES non-empty, stripped category strings.
    """
    if value is None:
        items: list[object] = []
    elif isinstance(value, str):
        items = list(_CATEGORY_SPLIT.split(value))
    elif isinstance(value, dict):
        items = list(value.values())
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]

    cleaned = [str(item).strip().lstrip("-*• ").strip() for item in items if item is not None]
    return [item for item in cleaned if item][:MAX_CATEGORIES]


def generate_profile(
    company_name: str,
    wikipedia_text: str,
    website_text: str,
    llm: LLMCallable,
    *,
    model: str = "deepseek-chat",
) -> dict[str, Any]:
    """Generate a profile from bounded Wikipedia + website text.

    Args:
        company_name: Company display name.
        wikipedia_text: Wikipedia extract (truncated to 2000 chars here).
        website_text: Crawled website text.
        llm: Callable(messages) -> text.
        model: Model id recorded for provenance.

    Returns:
        Dict with description, products_services (≤12 categories), generated_by.

    Raises:
        ValueError: If the response is not a JSON object or has no description.
    """
    template = string.Template((PROMPTS_DIR / "company_profile.md").read_text(encoding="utf-8"))
    prompt = template.substitute(
        company_name=company_name,
        wikipedia_text=(wikipedia_text or "")[:WIKIPEDIA_CHAR_CAP] or "(no content available)",
        website_text=website_text or "(no content available)",
    )
    messages = [
        {"role": "system", "content": "You generate factual company profiles."},
        {"role": "user", "content": prompt},
    ]
    parsed = parse_json_response(llm(messages))
    if not isinstance(parsed, dict):
        raise ValueError("profile response must be a JSON object")
    if "description" not in parsed:
        raise ValueError("profile response must include description")
    categories = _coerce_categories(parsed.get("products_services"))
    return {
        "description": str(parsed["description"]).strip(),
        "products_services": categories,
        "generated_by": model,
    }


def build_company_profile(
    company_name: str,
    *,
    wikipedia_fetcher: WikipediaFetcher | None = None,
    website_url_fetcher: WebsiteUrlFetcher | None = None,
    crawler: Crawler | None = None,
    llm: LLMCallable,
    model: str = "deepseek-chat",
) -> dict[str, Any] | None:
    """Build a profile from Wikipedia, the official site, and the model.

    Graceful degradation: when the crawl fails the profile is built from
    Wikipedia alone; when no input is available at all, returns None.

    Args:
        company_name: Company display name.
        wikipedia_fetcher: Callable(name) -> context dict.
        website_url_fetcher: Callable(wikibase_item) -> official URL or None.
        crawler: Callable(url) -> list of page texts.
        llm: Callable(messages) -> text.
        model: Model id recorded for provenance.

    Returns:
        Profile dict (description, products_services, website_url, sources,
        generated_by) or None when there is no usable input.
    """
    context = (wikipedia_fetcher or fetch_wikipedia_context)(company_name) or {}
    extract = str(context.get("extract") or "")
    wikibase_item = context.get("wikibase_item")
    sources: dict[str, Any] = {"wikipedia": context.get("page_url"), "crawled": []}

    website_url: str | None = None
    website_text = ""
    if wikibase_item:
        website_url = (website_url_fetcher or fetch_official_website_url)(wikibase_item)
    if website_url:
        try:
            pages = (crawler or crawl_website)(website_url)
            website_text = "\n\n".join(pages)
            if pages:
                sources["crawled"] = [website_url]
        except Exception:  # noqa: BLE001 - crawl failure degrades to wiki-only
            website_text = ""

    if not extract and not website_text:
        return None

    generated = generate_profile(company_name, extract, website_text, llm, model=model)
    return {
        **generated,
        "website_url": website_url,
        "sources": sources,
    }
