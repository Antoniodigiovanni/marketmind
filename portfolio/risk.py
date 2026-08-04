"""Portfolio risk analytics — volatility, drawdown, VaR, CVaR."""

from __future__ import annotations

import polars as pl


def compute_risk_metrics(returns: pl.DataFrame) -> pl.DataFrame:
    """Compute standard risk metrics for a return series.

    Args:
        returns: DataFrame with [date, return] columns.

    Returns:
        DataFrame with risk metrics as columns.

    Raises:
        NotImplementedError: Until risk module is implemented.
    """
    raise NotImplementedError
