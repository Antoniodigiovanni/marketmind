"""Derived macro series — credit spreads computed from raw OAS series."""

from __future__ import annotations

import polars as pl

REQUIRED_COLUMNS = ["date", "value"]


def compute_hy_minus_bbb(hy: pl.DataFrame, bbb: pl.DataFrame) -> pl.DataFrame:
    """Compute the high-yield minus BBB OAS credit spread by date.

    Args:
        hy: High-yield OAS frame with columns [date, value].
        bbb: BBB OAS frame with columns [date, value].

    Returns:
        DataFrame with columns [date, hy_oas, bbb_oas, hy_minus_bbb_oas]
        sorted by date, restricted to dates present in both inputs.

    Raises:
        ValueError: If either input has unexpected columns.
    """
    for name, frame in (("hy", hy), ("bbb", bbb)):
        missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise ValueError(f"{name} missing required columns: {missing}")

    joined = hy.rename({"value": "hy_oas"}).join(
        bbb.rename({"value": "bbb_oas"}), on="date", how="inner"
    )
    return joined.with_columns(
        (pl.col("hy_oas") - pl.col("bbb_oas")).alias("hy_minus_bbb_oas")
    ).sort("date")
