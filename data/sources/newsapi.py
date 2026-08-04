"""NewsAPI source — daily batch news fetch per ticker.

Free tier constraints:
- 100 requests/day
- Articles delayed 24 hours
- Developer/non-commercial use only
"""

from __future__ import annotations

import polars as pl


def fetch_articles(tickers: list[str], from_date: str, to_date: str) -> pl.DataFrame:
    """Fetch news articles for a list of tickers from NewsAPI.

    Args:
        tickers: Ticker symbols to search for.
        from_date: Start date string (YYYY-MM-DD). Articles are delayed 24h on free tier.
        to_date: End date string (YYYY-MM-DD).

    Returns:
        DataFrame with columns [url, url_hash, content_hash, source, ticker,
        published_at, title, description, content].

    Raises:
        ValueError: If NEWS_API_KEY env var is not set.
        RuntimeError: If daily request quota is exceeded.
    """
    raise NotImplementedError
