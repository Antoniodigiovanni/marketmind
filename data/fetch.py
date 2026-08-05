"""Orchestrates all data sources and writes results to data/raw/."""

from __future__ import annotations

from data.macro.fetch import run_daily_macro_sync


def main() -> None:
    """Download / refresh all data sources into data/raw/.

    Currently runs the macro sync (roadmap 1.3). Price, volatility, and news
    refresh are future slices.
    """
    run_daily_macro_sync()


if __name__ == "__main__":
    main()
