"""Official STOXX Europe 600 selection list source (keyless CSV download)."""

from __future__ import annotations

import datetime as dt
import html
import io
import json
import re
from urllib.parse import urljoin

import polars as pl
import requests

from data.universe.sources.http import get_with_ca_fallback

STOXX_DATA_URL = "https://www.stoxx.com/selection-lists"
USER_AGENT = "marketmind/0.1 (universe maintenance; free data only)"

REQUIRED_COLUMNS = [
    "Internal_Key",
    "RIC",
    "Instrument_Name",
    "Country",
    "Exchange",
    "Currency",
    "Rank (FINAL)",
]


def parse_selection_list(raw: str) -> pl.DataFrame:
    """Parse the STOXX selection list CSV into the 600 index members.

    The selection list contains the full candidate pool ranked by free-float
    market cap; the STOXX Europe 600 members are exactly the rows with
    Rank (FINAL) <= 600.

    Args:
        raw: CSV text (semicolon-separated) as served by stoxx.com.

    Returns:
        DataFrame with columns [internal_key, ric, name, country, exchange,
        currency, rank], one row per member, sorted by rank.

    Raises:
        ValueError: If required columns are missing or no members are found.
    """
    df = pl.read_csv(io.StringIO(raw), separator=";")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"selection list missing required columns: {missing}")

    members = (
        df.filter(pl.col("Rank (FINAL)").cast(pl.Int64) <= 600)
        .select(
            [
                pl.col("Internal_Key").alias("internal_key"),
                pl.col("RIC").alias("ric"),
                pl.col("Instrument_Name").alias("name"),
                pl.col("Country").alias("country"),
                pl.col("Exchange").alias("exchange"),
                pl.col("Currency").alias("currency"),
                pl.col("Rank (FINAL)").cast(pl.Int64).alias("rank"),
            ]
        )
        .unique(subset=["internal_key"], keep="first")
        .sort("rank")
    )
    if members.is_empty():
        raise ValueError("selection list contains no STOXX Europe 600 members")
    return members


def latest_selection_list_url(session: requests.Session | None = None) -> str:
    """Find the current STOXX selection list CSV URL from the index data page.

    Args:
        session: Optional requests.Session for connection reuse.

    Returns:
        Direct CSV URL, e.g. .../STOXXSelectionList/2026/August/slpublic_sxxp_20260803.csv.

    Raises:
        requests.HTTPError: On non-2xx response.
        ValueError: If the selection list link cannot be found.
    """
    current_month = dt.date.today().replace(day=1)
    previous_month = (current_month - dt.timedelta(days=1)).replace(day=1)
    namespace = "_STOXXNewSelectionlistportlet_WAR_STOXXNewSelectionlistportlet_"
    resp = get_with_ca_fallback(
        STOXX_DATA_URL,
        session=session,
        params={
            "p_p_id": "STOXXNewSelectionlistportlet_WAR_STOXXNewSelectionlistportlet",
            "p_p_lifecycle": "2",
            "p_p_state": "normal",
            "p_p_mode": "view",
            "p_p_cacheability": "cacheLevelPage",
            f"{namespace}cmd": "getStoxxSLData",
            f"{namespace}fromdate": previous_month.strftime("%B %Y"),
            f"{namespace}todate": current_month.strftime("%B %Y"),
            f"{namespace}symbols": "SXXP",
        },
        timeout=40,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()

    payload = resp.json()
    rows = payload.get("data", [])
    if isinstance(rows, str):
        rows = json.loads(rows)
    if not isinstance(rows, list):
        raise ValueError("STOXX selection-list response has no data array")
    row = next(
        (item for item in rows if isinstance(item, dict) and item.get("symbol") == "sxxp"),
        None,
    )
    link = row.get("slpublic", "") if row else ""
    match = re.search(r"href=['\"]([^'\"]+slpublic_sxxp[^'\"]*\.csv)['\"]", link)
    if match is None:
        raise ValueError("STOXX selection list link not found on index data page")
    return urljoin(STOXX_DATA_URL, html.unescape(match.group(1)))


def fetch_selection_list(session: requests.Session | None = None) -> pl.DataFrame:
    """Download and parse the current official STOXX Europe 600 selection list.

    Args:
        session: Optional requests.Session for connection reuse.

    Returns:
        Parsed members DataFrame (see parse_selection_list).
    """
    url = latest_selection_list_url(session)
    resp = get_with_ca_fallback(
        url, session=session, timeout=60, headers={"User-Agent": USER_AGENT}
    )
    resp.raise_for_status()
    return parse_selection_list(resp.text)
