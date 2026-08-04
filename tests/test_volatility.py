"""Tests for realized volatility computation."""

from __future__ import annotations

import math

import polars as pl
import pytest

from data.processed.volatility import compute_realized_vol


def _price_frame(log_returns: list[float]) -> pl.DataFrame:
    closes = [100.0]
    for log_return in log_returns:
        closes.append(closes[-1] * math.exp(log_return))
    return pl.DataFrame(
        {
            "date": range(len(closes)),
            "ticker": ["TEST"] * len(closes),
            "close": closes,
        }
    )


def test_constant_returns_produce_zero_volatility() -> None:
    out = compute_realized_vol(_price_frame([0.01] * 21))
    last_rv = out.filter(pl.col("rv_20d").is_not_null()).tail(1)["rv_20d"][0]
    assert abs(last_rv) < 1e-9


def test_alternating_returns_produce_known_output() -> None:
    log_returns = [0.01 if i % 2 == 0 else -0.01 for i in range(21)]
    out = compute_realized_vol(_price_frame(log_returns))

    sample_std = math.sqrt(0.002 / 19)
    expected = sample_std * math.sqrt(252) * 100
    last_rv = out.tail(1)["rv_20d"][0]
    assert abs(last_rv - expected) < 1e-9


def test_output_columns_and_order() -> None:
    out = compute_realized_vol(_price_frame([0.01] * 21))
    assert out.columns == ["date", "ticker", "rv_20d", "rv_60d"]
    assert out.sort("date")["date"].to_list() == out["date"].to_list()


def test_missing_columns_raise() -> None:
    bad = pl.DataFrame({"date": [1], "ticker": ["X"]})
    with pytest.raises(ValueError, match="Missing required columns"):
        compute_realized_vol(bad)
