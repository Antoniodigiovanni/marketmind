"""Symbol-map resolution job — resolve universe members to provider symbols."""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import polars as pl

from data.db.init import DEFAULT_DB_PATH, init_db
from data.universe.loader import UNIVERSE_DIR
from data.universe.sources.yahoo import search_by_id, symbol_has_data

UNIVERSE_FILES = ("sp500.csv", "eurostoxx600.csv")


def _members(universe_dir: Path) -> pl.DataFrame:
    """Load active members from both universe CSVs with isin/cusip columns."""
    frames: list[pl.DataFrame] = []
    for filename in UNIVERSE_FILES:
        path = universe_dir / filename
        if not path.exists():
            continue
        frame = pl.read_csv(path, schema_overrides={"isin": pl.String, "cusip": pl.String}).filter(
            pl.col("removed_date").is_null()
        )
        for column in ("isin", "cusip"):
            if column not in frame.columns:
                frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias(column))
        frames.append(frame.with_columns(pl.lit(filename.removesuffix(".csv")).alias("universe")))
    return pl.concat(frames, how="diagonal_relaxed")


def _id_for(row: dict[str, object]) -> tuple[str, str]:
    """Choose the most stable identifier available for a member."""
    if row.get("isin"):
        return ("ISIN", str(row["isin"]))
    if row.get("cusip"):
        return ("CUSIP", str(row["cusip"]))
    return ("TICKER", str(row["ticker"]))


def resolve_universe(
    conn: duckdb.DuckDBPyConnection,
    *,
    universe_dir: Path = UNIVERSE_DIR,
    searcher: object | None = None,
    checker: object | None = None,
) -> dict[str, int]:
    """Resolve every active universe member and upsert the symbol_map table.

    Members are resolved by ISIN, then CUSIP, then the current ticker as
    fallback. Symbols that fail the price-data check are marked 'review';
    members with no lookup result are marked 'unresolved'.

    Args:
        conn: DuckDB connection with the symbol_map table.
        universe_dir: Directory containing the universe CSVs.
        searcher: Callable(id_value) -> {'symbol', 'name'} or None.
        checker: Callable(symbol) -> bool.

    Returns:
        Stats dict: {valid, review, unresolved}.
    """
    search = searcher if searcher is not None else search_by_id
    check = checker if checker is not None else symbol_has_data
    stats = {"valid": 0, "review": 0, "unresolved": 0}

    for row in _members(universe_dir).to_dicts():
        id_type, id_value = _id_for(row)
        found = search(id_value)
        if not found and row.get("name"):
            found = search(str(row["name"]), name_hint=str(row["name"]))
        if not found:
            status = "unresolved"
            symbol = str(row["ticker"])
            name = str(row["name"])
        else:
            symbol = found["symbol"]
            name = found.get("name") or str(row["name"])
            status = "valid" if check(symbol) else "review"

        conn.execute(
            """
            INSERT INTO symbol_map (id_type, id_value, provider, symbol, name, status, last_validated_at)
            VALUES (?, ?, 'yahoo', ?, ?, ?, NOW())
            ON CONFLICT (id_type, id_value, provider) DO UPDATE SET
                symbol = excluded.symbol,
                name = excluded.name,
                status = excluded.status,
                resolved_at = NOW(),
                last_validated_at = NOW()
            """,
            [id_type, id_value, symbol, name, status],
        )
        stats[status] += 1

    return stats


def main() -> None:
    """Run symbol resolution for the active universe."""
    parser = argparse.ArgumentParser(description="Resolve universe members to Yahoo symbols")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="DuckDB path")
    args = parser.parse_args()

    conn = init_db(args.db)
    try:
        stats = resolve_universe(conn)
        print(f"valid={stats['valid']} review={stats['review']} unresolved={stats['unresolved']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
