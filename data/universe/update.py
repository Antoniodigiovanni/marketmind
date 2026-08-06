"""Universe update job — pull official constituent lists, apply append-only changes."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import polars as pl

from data.universe.loader import REQUIRED_COLUMNS, UNIVERSE_DIR
from data.universe.sources.ric import ric_to_yahoo
from data.universe.sources.sp500 import fetch_cspx_holdings, normalise_us_ticker
from data.universe.sources.stoxx import fetch_selection_list

MAX_DROP_FRACTION = 0.05  # refuse removals if a source shrinks more than this


def _current_active(universe_dir: Path, filename: str) -> pl.DataFrame:
    """Read a universe CSV and return rows without a removal date."""
    path = universe_dir / filename
    if not path.exists():
        return pl.DataFrame(schema={c: pl.String for c in REQUIRED_COLUMNS})
    return pl.read_csv(path).filter(pl.col("removed_date").is_null())


def _normalise_sp500(frame: pl.DataFrame) -> pl.DataFrame:
    """Normalise a CSPX holdings frame to the shared universe schema."""
    tickers = [normalise_us_ticker(row["ticker"]) for row in frame.to_dicts()]
    return frame.select(
        pl.col("name"),
        pl.lit("US").alias("country"),
        pl.col("sector"),
    ).with_columns(pl.Series("ticker", tickers))


def _normalise_stoxx(frame: pl.DataFrame) -> pl.DataFrame:
    """Normalise a STOXX selection-list frame to the shared universe schema."""
    yahoo_tickers = [ric_to_yahoo(row["ric"]) for row in frame.to_dicts()]
    return frame.with_columns(pl.Series("ticker", yahoo_tickers)).select(
        pl.col("ticker"),
        pl.col("name"),
        pl.col("country"),
        pl.lit(None, dtype=pl.String).alias("sector"),
        pl.col("internal_key"),
    )


def _apply_universe(
    *,
    filename: str,
    as_of_date: dt.date,
    universe_dir: Path,
    additions: list[dict[str, object]],
    removals: list[dict[str, object]],
) -> None:
    """Merge fetched constituents into a universe CSV, append-only."""
    path = universe_dir / filename
    existing = (
        pl.read_csv(path)
        if path.exists()
        else pl.DataFrame(schema={c: pl.String for c in REQUIRED_COLUMNS})
    )

    if additions:
        base = {
            "ticker": [a["ticker"] for a in additions],
            "name": [a["name"] for a in additions],
            "country": [a["country"] for a in additions],
            "sector": [a["sector"] for a in additions],
            "added_date": [a["added_date"] for a in additions],
            "removed_date": [None] * len(additions),
        }
        if "internal_key" in additions[0]:
            base["internal_key"] = [a.get("internal_key") for a in additions]
        for column in ("isin", "cusip"):
            if column in additions[0]:
                base[column] = [a.get(column) for a in additions]
        new_rows = pl.DataFrame(base)
        existing = pl.concat([existing, new_rows], how="diagonal_relaxed")

    if removals:
        ticker_exprs = [
            (pl.col("ticker") == r["ticker"]) & pl.col("removed_date").is_null() for r in removals
        ]
        removal_filter = ticker_exprs[0]
        for expr in ticker_exprs[1:]:
            removal_filter = removal_filter | expr
        existing = existing.with_columns(
            pl.when(removal_filter)
            .then(pl.lit(as_of_date.isoformat()))
            .otherwise(pl.col("removed_date"))
            .alias("removed_date")
        )

        delisted_path = universe_dir / "delisted.csv"
        delisted_existing = (
            pl.read_csv(delisted_path)
            if delisted_path.exists()
            else pl.DataFrame(
                schema={
                    "ticker": pl.String,
                    "name": pl.String,
                    "country": pl.String,
                    "sector": pl.String,
                    "added_date": pl.String,
                    "removed_date": pl.String,
                    "universe": pl.String,
                    "delisted_date": pl.String,
                    "reason": pl.String,
                    "yahoo_symbol": pl.String,
                }
            )
        )
        delisted_new = []
        for removal in removals:
            rows = existing.filter(pl.col("ticker") == removal["ticker"])
            delisted_new.append(
                {
                    "ticker": removal["ticker"],
                    "name": rows["name"][0] if len(rows) else removal["ticker"],
                    "country": rows["country"][0] if len(rows) and "country" in rows.columns else "",
                    "sector": rows["sector"][0] if len(rows) and "sector" in rows.columns else "",
                    "added_date": rows["added_date"][0]
                    if len(rows) and "added_date" in rows.columns
                    else "",
                    "removed_date": as_of_date.isoformat(),
                    "universe": filename.removesuffix(".csv"),
                    "delisted_date": as_of_date.isoformat(),
                    "reason": f"removed from {filename.removesuffix('.csv')}",
                    "yahoo_symbol": None,
                }
            )
        pl.concat(
            [delisted_existing, pl.DataFrame(delisted_new)], how="vertical_relaxed"
        ).write_csv(delisted_path)

    existing = existing.sort(["ticker"]).unique(subset=["ticker", "added_date"], keep="first")
    existing.write_csv(path)


def update_universe(
    *,
    as_of: str,
    universe_dir: Path = UNIVERSE_DIR,
    sp500: pl.DataFrame | None = None,
    stoxx600: pl.DataFrame | None = None,
) -> dict[str, object]:
    """Apply append-only universe changes from official constituent lists.

    New tickers get added_date = as_of; tickers missing from the fetched list
    get removed_date = as_of and a delisted.csv entry. If a source drops more
    than MAX_DROP_FRACTION of the current members, no removals are applied for
    that universe (source-health guard).

    Args:
        as_of: Date (YYYY-MM-DD) the changes are recorded against.
        universe_dir: Directory containing universe CSVs.
        sp500: S&P 500 constituents [ticker, name, sector, ...] (CSPX holdings
            schema). Fetched from the BlackRock API when None.
        stoxx600: STOXX 600 constituents [internal_key, ric, name, country, ...]
            (selection-list schema). Fetched from stoxx.com when None.

    Returns:
        Summary dict: {sp500: {added: [...], removed: [...]},
        eurostoxx600: {added: [...], removed: [...]}}.

    Raises:
        ValueError: If as_of is invalid or a source drops too many members.
    """
    as_of_date = dt.date.fromisoformat(as_of)

    sp500_frame = sp500 if sp500 is not None else fetch_cspx_holdings()
    stoxx_frame = stoxx600 if stoxx600 is not None else fetch_selection_list()

    sources = {
        "sp500.csv": _normalise_sp500(sp500_frame),
        "eurostoxx600.csv": _normalise_stoxx(stoxx_frame),
    }
    summary: dict[str, object] = {}

    for filename, fetched in sources.items():
        source_name = filename.removesuffix(".csv")
        active = _current_active(universe_dir, filename)
        active_tickers = set(active["ticker"].to_list())
        current_tickers = set(fetched["ticker"].to_list())

        added_tickers = sorted(current_tickers - active_tickers)
        missing_tickers = sorted(active_tickers - current_tickers)

        if active_tickers and len(missing_tickers) / len(active_tickers) > MAX_DROP_FRACTION:
            raise ValueError(
                f"{source_name}: {len(missing_tickers)}/{len(active_tickers)} members missing "
                f"from source (> {MAX_DROP_FRACTION:.0%}) — refusing removals, check source health"
            )

        added_rows = fetched.filter(pl.col("ticker").is_in(added_tickers))
        additions = [
            {
                "ticker": row["ticker"],
                "name": row["name"],
                "country": row["country"],
                "sector": row["sector"],
                "added_date": row.get("added_date") or as_of_date.isoformat(),
                "internal_key": row.get("internal_key"),
                "isin": row.get("isin"),
                "cusip": row.get("cusip"),
            }
            for row in added_rows.to_dicts()
        ]
        removals = [{"ticker": t} for t in missing_tickers]

        _apply_universe(
            filename=filename,
            as_of_date=as_of_date,
            universe_dir=universe_dir,
            additions=additions,
            removals=removals,
        )
        summary[source_name] = {"added": added_tickers, "removed": missing_tickers}

    return summary


def main() -> None:
    """Run the universe update from the command line."""
    parser = argparse.ArgumentParser(description="Update S&P 500 / STOXX 600 universe CSVs")
    parser.add_argument(
        "--as-of", default=dt.date.today().isoformat(), help="YYYY-MM-DD change date"
    )
    args = parser.parse_args()

    summary = update_universe(as_of=args.as_of)
    for source, changes in summary.items():
        print(f"{source}: +{len(changes['added'])} / -{len(changes['removed'])}")


if __name__ == "__main__":
    main()
