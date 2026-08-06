"""Tests for realized volatility computation."""

from __future__ import annotations

import math

import polars as pl
import pytest

from data.processed.volatility import build_volatility_file, compute_realized_vol


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


def _ticker_frame(name: str, log_returns: list[float]) -> pl.DataFrame:
    closes = [100.0]
    for log_return in log_returns:
        closes.append(closes[-1] * math.exp(log_return))
    return pl.DataFrame(
        {
            "date": range(len(closes)),
            "ticker": [name] * len(closes),
            "close": closes,
        }
    )


def test_two_tickers_have_no_cross_ticker_contamination() -> None:
    flat = _ticker_frame("FLAT", [0.01] * 21)
    swing = _ticker_frame("SWING", [0.01 if i % 2 == 0 else -0.01 for i in range(21)])
    prices = pl.concat([flat, swing])

    out = compute_realized_vol(prices)

    flat_rv = out.filter(pl.col("ticker") == "FLAT")["rv_20d"].drop_nulls().tail(1)[0]
    swing_rv = out.filter(pl.col("ticker") == "SWING")["rv_20d"].drop_nulls().tail(1)[0]
    sample_std = math.sqrt(0.002 / 19)
    assert abs(flat_rv) < 1e-9
    assert abs(swing_rv - sample_std * math.sqrt(252) * 100) < 1e-9


def test_build_volatility_file_writes_known_values(tmp_path) -> None:
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    _ticker_frame("TEST", [0.01] * 21).write_parquet(prices_dir / "TEST.parquet")
    out_path = tmp_path / "processed" / "volatility.parquet"

    vol = build_volatility_file(prices_dir, out_path)

    assert out_path.exists()
    assert vol.columns == ["date", "ticker", "rv_20d", "rv_60d"]
    assert abs(vol["rv_20d"].drop_nulls().tail(1)[0]) < 1e-9


def test_build_volatility_file_empty_dir_writes_empty(tmp_path) -> None:
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    out_path = tmp_path / "processed" / "volatility.parquet"

    vol = build_volatility_file(prices_dir, out_path)

    assert out_path.exists()
    assert vol.is_empty()
    assert vol.schema == {
        "date": pl.Datetime("us", "UTC"),
        "ticker": pl.String,
        "rv_20d": pl.Float64,
        "rv_60d": pl.Float64,
    }


def test_build_volatility_file_rejects_bad_price_columns(tmp_path) -> None:
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    pl.DataFrame({"date": [1]}).write_parquet(prices_dir / "BAD.parquet")
    with pytest.raises(ValueError, match="missing required columns"):
        build_volatility_file(prices_dir, tmp_path / "volatility.parquet")
