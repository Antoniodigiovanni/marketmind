"""LEI enrichment for the Eurostoxx 600 universe via GLEIF."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import polars as pl
from loguru import logger

from data.sources.gleif import GleifClient, LeiCandidate
from data.universe.loader import UNIVERSE_DIR

LEGAL_FORM_TOKENS = {
    "ab",
    "ag",
    "asa",
    "co",
    "corp",
    "corporation",
    "gmbh",
    "limited",
    "ltd",
    "nv",
    "oyj",
    "plc",
    "sa",
    "sas",
    "se",
    "sl",
    "spa",
    "srl",
}
MATCH_THRESHOLD = 0.6


def normalize_name(name: str) -> str:
    """Normalize a legal name for matching (lowercase, strip legal forms)."""
    compact = name.lower().replace(".", "")
    tokens = re.sub(r"[^a-z0-9\s]", " ", compact).split()
    return " ".join(t for t in tokens if t not in LEGAL_FORM_TOKENS)


def _score(name_a: str, name_b: str) -> float:
    """Return token-Jaccard similarity between two normalized names."""
    a = set(normalize_name(name_a).split())
    b = set(normalize_name(name_b).split())
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return len(a & b) / len(a | b)


def _best_candidate(name: str, candidates: list[LeiCandidate]) -> LeiCandidate | None:
    """Return the best issued candidate above MATCH_THRESHOLD, or None."""
    eligible = [c for c in candidates if c.status == "ISSUED"]
    if not eligible:
        return None
    scored = [(_score(name, c.legal_name), c) for c in eligible]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    return best if best_score >= MATCH_THRESHOLD else None


def enrich_lei(
    *,
    universe_dir: Path = UNIVERSE_DIR,
    client: GleifClient | None = None,
    countries: tuple[str, ...] = ("IT", "FR", "ES"),
) -> dict[str, int]:
    """Resolve LEIs for Eurostoxx 600 members and write them to the universe CSV.

    Args:
        universe_dir: Directory containing eurostoxx600.csv.
        client: GLEIF client (created when None).
        countries: ISO country codes to enrich.

    Returns:
        Stats: {matched, unresolved, already_have}.
    """
    client = client or GleifClient()
    path = universe_dir / "eurostoxx600.csv"
    frame = pl.read_csv(path)
    if "lei" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("lei"))

    stats = {"matched": 0, "unresolved": 0, "already_have": 0}
    rows = frame.to_dicts()
    for row in rows:
        if row.get("country") not in countries or row.get("removed_date") is not None:
            continue
        if row.get("lei"):
            stats["already_have"] += 1
            continue
        name = str(row["name"])
        country = str(row["country"])
        best = _best_candidate(name, client.search(name, country))
        if best is None:
            stats["unresolved"] += 1
            logger.warning(f"lei: no match for {row['ticker']} ({name})")
            continue
        row["lei"] = best.lei
        stats["matched"] += 1

    updated = pl.DataFrame(rows, schema=frame.schema)
    updated = updated.sort("ticker").unique(subset=["ticker", "added_date"], keep="first")
    updated.write_csv(path)
    return stats


def main() -> None:
    """Run LEI enrichment for the Eurostoxx 600 universe."""
    parser = argparse.ArgumentParser(description="Enrich Eurostoxx members with LEIs")
    parser.add_argument(
        "--countries",
        nargs="+",
        default=["IT", "FR", "ES"],
        help="ISO country codes",
    )
    args = parser.parse_args()
    stats = enrich_lei(countries=tuple(args.countries))
    print(stats)


if __name__ == "__main__":
    main()
