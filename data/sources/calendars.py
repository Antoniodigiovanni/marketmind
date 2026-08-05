"""Exchange calendar helpers for US/EU trading sessions."""

from __future__ import annotations

import datetime as dt

import exchange_calendars as xcals
import pandas as pd

SUFFIX_TO_EXCHANGE: dict[str, str] = {
    "": "XNYS",
    ".AS": "XAMS",
    ".BR": "XBRU",
    ".CO": "XCSE",
    ".DE": "XETR",
    ".HE": "XHEL",
    ".IR": "XDUB",
    ".L": "XLON",
    ".LS": "XLIS",
    ".MC": "XMAD",
    ".MI": "XMIL",
    ".OL": "XOSL",
    ".PA": "XPAR",
    ".ST": "XSTO",
    ".SW": "XSWX",
    ".VI": "XWBO",
    ".WA": "XWAR",
}


def exchange_for_suffix(suffix: str) -> str:
    """Map a Yahoo exchange suffix (e.g. '.DE') to an exchange-calendars code.

    Args:
        suffix: Yahoo suffix, '' for US listings.

    Returns:
        exchange-calendars exchange code (defaults to XNYS).
    """
    return SUFFIX_TO_EXCHANGE.get(suffix, "XNYS")


def is_trading_day(exchange: str, day: dt.date) -> bool:
    """Return whether a date is a trading day on an exchange.

    Args:
        exchange: exchange-calendars code (e.g. 'XNYS', 'XETR').
        day: Date to check.

    Returns:
        True when the exchange has a session on that day.
    """
    return bool(xcals.get_calendar(exchange).is_session(pd.Timestamp(day)))
