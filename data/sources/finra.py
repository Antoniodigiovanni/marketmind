"""FINRA data source — biweekly equity short interest bulk files."""

from __future__ import annotations

import datetime as dt

import polars as pl
import requests

SHORT_INTEREST_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{date}.csv"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125 Safari/537.36"
)

COLUMNS = {
    "symbol": pl.String,
    "symbol_code": pl.String,
    "issue_name": pl.String,
    "market_class": pl.String,
    "current_short": pl.Float64,
    "previous_short": pl.Float64,
    "avg_daily_volume": pl.Float64,
    "days_to_cover": pl.Float64,
    "change_pct": pl.Float64,
    "settlement_date": pl.Date,
}


def _empty_frame() -> pl.DataFrame:
    """Return an empty frame with the canonical short-interest schema."""
    return pl.DataFrame(schema=COLUMNS)


def fetch_short_interest_file(settlement_date: dt.date) -> pl.DataFrame:
    """Fetch the FINRA short interest bulk CSV for one settlement date.

    Args:
        settlement_date: Biweekly settlement date (YYYY-MM-DD).

    Returns:
        DataFrame with the short-interest schema, or an empty frame when the
        file is not yet published (403/404).

    Raises:
        requests.HTTPError: On other non-2xx responses.
    """
    url = SHORT_INTEREST_URL.format(date=settlement_date.strftime("%Y%m%d"))
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    if response.status_code in (403, 404):
        return _empty_frame()
    response.raise_for_status()

    raw = pl.read_csv(
        response.content,
        separator="|",
        schema_overrides={
            "daysToCoverQuantity": pl.String,
            "changePercent": pl.String,
        },
    )
    if raw.is_empty():
        return _empty_frame()

    return (
        raw.rename(
            {
                "symbolCode": "symbol_code",
                "issueName": "issue_name",
                "marketClassCode": "market_class",
                "currentShortPositionQuantity": "current_short",
                "previousShortPositionQuantity": "previous_short",
                "averageDailyVolumeQuantity": "avg_daily_volume",
                "daysToCoverQuantity": "days_to_cover",
                "changePercent": "change_pct",
                "settlementDate": "settlement_date",
            }
        )
        .with_columns(
            pl.col("symbol_code").str.to_uppercase().alias("symbol_code"),
            pl.col("symbol_code")
            .str.to_uppercase()
            .str.replace_all(".", "-", literal=True)
            .alias("symbol"),
            pl.col("settlement_date").str.to_date(),
            pl.col("days_to_cover").cast(pl.Float64, strict=False),
            pl.col("change_pct").cast(pl.Float64, strict=False),
            pl.col("current_short").cast(pl.Float64),
            pl.col("previous_short").cast(pl.Float64),
            pl.col("avg_daily_volume").cast(pl.Float64),
        )
        .select(list(COLUMNS))
    )
