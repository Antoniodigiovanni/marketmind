"""Portfolio optimiser — risk-parity allocation via cvxpy."""

from __future__ import annotations

import polars as pl


def optimise(returns: pl.DataFrame, **kwargs: object) -> pl.DataFrame:
    """Compute risk-parity portfolio weights.

    Defaults: risk-parity, half-Kelly sizing, max 5% single name,
    30% single sector, 20% single country, monthly rebalancing.

    Args:
        returns: DataFrame with [date, ticker, return] columns.
        **kwargs: Override defaults (e.g. max_single_name=0.05).

    Returns:
        DataFrame with [date, ticker, weight] columns.

    Raises:
        NotImplementedError: Until optimiser is implemented.
    """
    raise NotImplementedError
