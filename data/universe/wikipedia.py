"""Wikipedia universe sources — S&P 500 and STOXX Europe 600 constituent lists."""

from __future__ import annotations

import polars as pl
import requests
from bs4 import BeautifulSoup

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
STOXX600_URL = "https://en.wikipedia.org/wiki/STOXX_Europe_600"
# Baseline revision containing the complete constituent table. The live page was
# trimmed by an editor in July 2026 (see revision 1362485298), so daily updates
# fetch this revision until the live table is restored.
STOXX600_BASELINE_OLDID = 1345769562

USER_AGENT = "marketmind/0.1 (universe maintenance; free data only)"


def parse_sp500(html: str) -> pl.DataFrame:
    """Parse the S&P 500 constituent table from Wikipedia HTML.

    Args:
        html: Page HTML.

    Returns:
        DataFrame with columns [ticker, name, sector, sub_industry, country,
        added_date]. country is always 'US'.

    Raises:
        ValueError: If the constituent table cannot be found.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, object]] = []

    for table in soup.find_all("table"):
        header = [
            c.get_text(" ", strip=True) for c in table.find_all("tr")[0].find_all(["th", "td"])
        ]
        if "Symbol" not in header or "Date added" not in header:
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) >= 6 and cells[0]:
                rows.append(
                    {
                        "ticker": cells[0],
                        "name": cells[1],
                        "sector": cells[2],
                        "sub_industry": cells[3],
                        "country": "US",
                        "added_date": cells[5],
                    }
                )
        break

    if not rows:
        raise ValueError("S&P 500 constituent table not found in HTML")
    return pl.DataFrame(rows)


def parse_stoxx600(html: str) -> pl.DataFrame:
    """Parse the STOXX Europe 600 constituent table from Wikipedia HTML.

    Table columns are Ticker, Company, ICB Sector, Country, Headquarters.
    Exact duplicate (ticker, country) rows are dropped, keeping the first.

    Args:
        html: Page HTML.

    Returns:
        DataFrame with columns [ticker, name, sector, country].

    Raises:
        ValueError: If the constituent table cannot be found.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, object]] = []

    for table in soup.find_all("table"):
        header = [
            c.get_text(" ", strip=True) for c in table.find_all("tr")[0].find_all(["th", "td"])
        ]
        if "Ticker" not in header or "Company" not in header or "Country" not in header:
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) >= 4 and cells[0]:
                rows.append(
                    {
                        "ticker": cells[0],
                        "name": cells[1],
                        "sector": cells[2] if len(cells) > 2 else None,
                        "country": cells[3],
                    }
                )
        break

    if not rows:
        raise ValueError("STOXX Europe 600 constituent table not found in HTML")
    df = pl.DataFrame(rows)
    return df.unique(subset=["ticker", "country"], keep="first")


def fetch_sp500(session: requests.Session | None = None) -> pl.DataFrame:
    """Download and parse the current S&P 500 constituent list.

    Args:
        session: Optional requests.Session for connection reuse.

    Returns:
        Parsed DataFrame (see parse_sp500).

    Raises:
        requests.HTTPError: On non-2xx response.
        ValueError: If the table cannot be parsed.
    """
    http = session or requests.Session()
    resp = http.get(SP500_URL, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return parse_sp500(resp.text)


def fetch_stoxx600(
    *,
    oldid: int | None = STOXX600_BASELINE_OLDID,
    session: requests.Session | None = None,
) -> pl.DataFrame:
    """Download and parse the STOXX Europe 600 constituent list.

    Args:
        oldid: Wikipedia revision to fetch. Defaults to the pinned baseline
            revision (the live page was trimmed in July 2026); pass None to
            fetch the live page.
        session: Optional requests.Session for connection reuse.

    Returns:
        Parsed DataFrame (see parse_stoxx600).

    Raises:
        requests.HTTPError: On non-2xx response.
        ValueError: If the table cannot be parsed.
    """
    url = (
        STOXX600_URL
        if oldid is None
        else f"https://en.wikipedia.org/w/index.php?title=STOXX_Europe_600&oldid={oldid}"
    )
    http = session or requests.Session()
    resp = http.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return parse_stoxx600(resp.text)
