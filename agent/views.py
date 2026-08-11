"""Market view write + read — thin orchestration over data/db/views.py.

Resolves human-facing ticker strings to company_id before calling the
repository layer, and keeps the cross-field scope/ticker/theme validation
that lived here in the DuckDB version. All SQL lives in data/db/views.py;
this module contains none. `structure_views` is not wrapped here — callers
use `data.db.views.structure_views` directly, since it takes no ticker and
needs no resolution (see
docs/superpowers/specs/2026-08-11-postgres-views-cutover-design.md).
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg

from data.db import views as views_repo
from data.db.identifiers import resolve_identifier

VALID_SCOPES = ("market", "ticker", "theme")


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
        conn, as_of=as_of_date, company_id=company_id, scope=scope, limit=limit
    )
