"""Market view write, read, and structured-extraction orchestration.

Resolves human-facing ticker strings to company_id before calling the
repository layer, keeps cross-field scope/ticker/theme validation, and owns
the model-facing view-structuring job. All SQL lives in data/db/views.py.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import string
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
from loguru import logger
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.llm import ChatClient, parse_json_response
from data.db import views as views_repo
from data.db.identifiers import resolve_identifier
from data.db.pool import close_pool, open_pool
from data.db.runs import finish_run, start_run

VALID_SCOPES = ("market", "ticker", "theme")
VALID_STANCES = ("bullish", "bearish", "neutral")
VIEW_STRUCTURE_FIELDS = frozenset(
    {
        "stance",
        "thesis",
        "risks",
        "catalysts",
        "confidence",
        "related_tickers",
        "time_horizon_days",
    }
)
MAX_VIEW_CHARS = 50_000
VIEW_STRUCTURE_MAX_ATTEMPTS = 3
VIEW_STRUCTURE_MAX_TOKENS = 800
VIEW_STRUCTURE_SCHEMA_VERSION = 1
VIEW_STRUCTURE_RETRY_WAIT = wait_exponential(multiplier=0.5, max=2)
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
LLMCallable = Callable[[list[dict[str, str]]], str]


async def save_market_view(
    conn: psycopg.AsyncConnection,
    *,
    as_of: str,
    view_text: str,
    model: str,
    data_cutoff: str,
    scope: str = "market",
    ticker: str | None = None,
    theme: str | None = None,
    used_fact_ids: list[str] | None = None,
) -> str:
    """Insert a free-text market view (immutable), resolving ticker -> company_id.

    Args:
        conn: An open connection.
        as_of: Market date (YYYY-MM-DD) the view refers to.
        view_text: Free-text view content, never edited in place.
        model: Model identifier that produced the view.
        data_cutoff: Date (YYYY-MM-DD) of the newest data used to write the view.
        scope: 'market' | 'ticker' | 'theme'.
        ticker: Ticker symbol when scope == 'ticker'; resolved to a
            company_id as of `as_of`.
        theme: Theme label when scope == 'theme'.
        used_fact_ids: company_facts ids that informed this view (provenance).

    Returns:
        New view ID, as a string.

    Raises:
        ValueError: On invalid scope, missing/extra scope argument, bad
            date, or a ticker that does not resolve to a company as of
            `as_of`.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {VALID_SCOPES}, got {scope!r}")
    if scope == "ticker" and not ticker:
        raise ValueError("ticker is required when scope == 'ticker'")
    if scope == "theme" and not theme:
        raise ValueError("theme is required when scope == 'theme'")
    if scope != "ticker" and ticker:
        raise ValueError("ticker is only allowed when scope == 'ticker'")
    if scope != "theme" and theme:
        raise ValueError("theme is only allowed when scope == 'theme'")

    as_of_date = dt.date.fromisoformat(as_of)
    data_cutoff_date = dt.date.fromisoformat(data_cutoff)

    company_id: uuid.UUID | None = None
    if scope == "ticker":
        company_id = await resolve_identifier(
            conn, id_type="ticker", id_value=ticker, as_of=as_of_date
        )
        if company_id is None:
            raise ValueError(f"no company found for ticker {ticker!r} as of {as_of}")

    used_fact_uuids = [uuid.UUID(fid) for fid in used_fact_ids] if used_fact_ids else None

    view_id = await views_repo.save_market_view(
        conn,
        as_of=as_of_date,
        scope=scope,
        company_id=company_id,
        theme=theme,
        view_text=view_text,
        model=model,
        data_cutoff=data_cutoff_date,
        used_fact_ids=used_fact_uuids,
    )
    return str(view_id)


async def get_market_views(
    conn: psycopg.AsyncConnection,
    *,
    as_of: str,
    ticker: str | None = None,
    scope: str | None = None,
    schema_version: int | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return market views as of a date (as_of <= :as_of), newest first.

    Args:
        conn: An open connection.
        as_of: Date string (YYYY-MM-DD). Views referring to this date or earlier.
        ticker: Optional ticker filter. Resolved to a company_id as of
            `as_of`; a ticker that does not resolve returns an empty list
            rather than raising — a read is a filter, not an assertion that
            the ticker must exist.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        schema_version: Optional derived-structure version. Defaults to the
            newest version available for each view.
        limit: Maximum number of views to return.

    Returns:
        List of view dicts ordered by as_of descending.

    Raises:
        ValueError: If as_of is not a valid date.
    """
    as_of_date = dt.date.fromisoformat(as_of)

    company_id: uuid.UUID | None = None
    if ticker:
        company_id = await resolve_identifier(
            conn, id_type="ticker", id_value=ticker, as_of=as_of_date
        )
        if company_id is None:
            return []

    return await views_repo.get_market_views(
        conn,
        as_of=as_of_date,
        company_id=company_id,
        scope=scope,
        schema_version=schema_version,
        limit=limit,
    )


def build_view_structuring_messages(view: dict[str, object]) -> list[dict[str, str]]:
    """Render the version-one structuring prompt for a source view.

    Args:
        view: Source row containing view metadata and free text.

    Returns:
        Chat messages with the bounded source view rendered as untrusted data.
    """
    template = string.Template((PROMPTS_DIR / "view_structuring.md").read_text(encoding="utf-8"))
    prompt = template.substitute(
        as_of=view["as_of"],
        scope=view["scope"],
        theme=view.get("theme") or "(none)",
        source_model=view["model"],
        view_text_json=json.dumps(str(view["view_text"])[:MAX_VIEW_CHARS]),
    )
    return [{"role": "user", "content": prompt}]


def parse_view_structure(content: str) -> dict[str, object]:
    """Parse and strictly validate a version-one market-view structure.

    Args:
        content: Raw model response.

    Returns:
        Normalized structure matching the version-one schema exactly.

    Raises:
        ValueError: If the response is not an exact, valid version-one object.
    """
    payload = parse_json_response(content)
    if not isinstance(payload, dict):
        raise ValueError("view structure must be a JSON object")
    fields = frozenset(payload)
    if fields != VIEW_STRUCTURE_FIELDS:
        missing = sorted(VIEW_STRUCTURE_FIELDS - fields)
        extra = sorted(fields - VIEW_STRUCTURE_FIELDS)
        raise ValueError(f"view structure fields mismatch: missing={missing}, extra={extra}")

    stance = payload["stance"]
    if stance not in VALID_STANCES:
        raise ValueError(f"stance must be one of {VALID_STANCES}")
    thesis = payload["thesis"]
    if not isinstance(thesis, str) or not thesis.strip():
        raise ValueError("thesis must be a nonblank string")

    normalized: dict[str, object] = {
        "stance": stance,
        "thesis": thesis.strip(),
    }
    for field in ("risks", "catalysts", "related_tickers"):
        value = payload[field]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"{field} must be a list of nonblank strings")
        normalized[field] = [item.strip() for item in value]

    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be numeric")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("confidence must be between 0 and 1")
    normalized["confidence"] = float(confidence)

    horizon = payload["time_horizon_days"]
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 0 <= horizon <= 365:
        raise ValueError("time_horizon_days must be an integer between 0 and 365")
    normalized["time_horizon_days"] = horizon
    return normalized


def build_view_extractor(llm: LLMCallable) -> views_repo.Extractor:
    """Build an async repository extractor around a synchronous chat callable.

    Args:
        llm: Callable accepting chat messages and returning response text.

    Returns:
        Async extractor suitable for `data.db.views.structure_views`.
    """

    async def extract(view: dict[str, object]) -> dict[str, object]:
        messages = build_view_structuring_messages(view)
        retry = AsyncRetrying(
            retry=retry_if_exception_type(ValueError),
            wait=VIEW_STRUCTURE_RETRY_WAIT,
            stop=stop_after_attempt(VIEW_STRUCTURE_MAX_ATTEMPTS),
            reraise=True,
        )
        async for attempt in retry:
            with attempt:
                response = await asyncio.to_thread(llm, messages)
                return parse_view_structure(response)
        raise RuntimeError("view extraction retry loop ended unexpectedly")  # pragma: no cover

    return extract


async def run_view_structuring(
    conn: psycopg.AsyncConnection,
    date: str,
    *,
    llm: LLMCallable,
    extraction_model: str,
    limit: int = 100,
) -> dict[str, int]:
    """Structure pending market views through a date and audit the run.

    Args:
        conn: Open PostgreSQL connection.
        date: Upper market date bound in YYYY-MM-DD format.
        llm: Synchronous chat-completion callable.
        extraction_model: Model identifier recorded with derived rows.
        limit: Maximum pending views to process.

    Returns:
        Summary containing the number of views structured.

    Raises:
        ValueError: If date, limit, or model output is invalid.
        Exception: After recording any other extraction or database failure.
    """
    as_of = dt.date.fromisoformat(date)
    run_id = await start_run(conn, run_date=as_of, job_type="views")
    summary = {"views_structured": 0}
    try:
        summary["views_structured"] = await views_repo.structure_views(
            conn,
            extractor=build_view_extractor(llm),
            schema_version=VIEW_STRUCTURE_SCHEMA_VERSION,
            extraction_model=extraction_model,
            as_of=as_of,
            limit=limit,
        )
    except Exception as exc:
        await finish_run(
            conn,
            run_id=run_id,
            status="failed",
            counters=summary,
            error=str(exc),
        )
        raise
    await finish_run(conn, run_id=run_id, status="succeeded", counters=summary)
    return summary


async def run_view_structuring_with_pool(date: str, **kwargs: Any) -> dict[str, int]:
    """Open a PostgreSQL pool for one view-structuring run.

    Args:
        date: Upper market date bound in YYYY-MM-DD format.
        **kwargs: Forwarded to `run_view_structuring`.

    Returns:
        Summary from `run_view_structuring`.
    """
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            return await run_view_structuring(conn, date, **kwargs)
    finally:
        await close_pool(pool)


def main() -> None:
    """Run the pending market-view structuring job from the command line."""
    parser = argparse.ArgumentParser(description="Structure pending market views")
    parser.add_argument("date", help="upper market date bound (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    client = ChatClient()
    complete = partial(
        client.complete,
        json_mode=True,
        max_tokens=VIEW_STRUCTURE_MAX_TOKENS,
    )
    summary = asyncio.run(
        run_view_structuring_with_pool(
            args.date,
            llm=complete,
            extraction_model=client.model,
            limit=args.limit,
        )
    )
    logger.info(f"view structuring summary for {args.date}: {summary}")


if __name__ == "__main__":  # pragma: no cover - exercised through main() tests
    main()
