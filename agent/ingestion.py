"""Daily batch ingest pipeline — fetches news, deduplicates, runs agent, updates fact store."""

from __future__ import annotations


def run_daily_ingest(date: str) -> None:
    """Run the full daily ingest pipeline for a given date.

    Pipeline steps:
    1. Fetch articles from NewsAPI for all tickers in universe.
    2. Level-1 dedup: skip articles whose url_hash or content_hash already exist.
    3. Pre-score each article: source_credibility * recency_decay.
    4. For each new article, retrieve current ticker facts from DuckDB and
       semantically similar facts from ChromaDB.
    5. LLM agent decides: REDUNDANT or extracts new facts.
    6. Write new facts to ticker_facts (DuckDB) and embed in ChromaDB.
    7. Log run metadata to agent_runs table.

    Args:
        date: Date string (YYYY-MM-DD) for which to run ingest.

    Raises:
        NotImplementedError: Until pipeline is implemented.
    """
    raise NotImplementedError
