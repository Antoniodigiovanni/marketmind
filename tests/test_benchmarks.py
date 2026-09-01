"""Unit tests for the benchmark registry."""

from __future__ import annotations

import pytest

from data.benchmarks import BENCHMARKS, Benchmark, get_benchmarks, validate_registry


def test_registry_includes_market_anchors_and_complete_sp500_sector_set() -> None:
    symbols = {benchmark.symbol for benchmark in BENCHMARKS}
    sectors = get_benchmarks(kind="sector", region="US")

    assert {"^GSPC", "^STOXX"} <= symbols
    assert len(sectors) == 11
    assert all(benchmark.sector for benchmark in sectors)


def test_registry_filters_are_exact_and_stably_ordered() -> None:
    markets = get_benchmarks(kind="market")
    europe = get_benchmarks(region="Europe")

    assert [benchmark.key for benchmark in markets] == ["sp500", "stoxx_europe_600"]
    assert [benchmark.symbol for benchmark in europe] == ["^STOXX"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"kind": "factor"}, "kind"), ({"region": "Global"}, "region")],
)
def test_registry_rejects_unknown_filters(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        get_benchmarks(**kwargs)


def test_registry_validation_rejects_duplicate_symbols() -> None:
    duplicate = Benchmark("other", "^GSPC", "Other", "market", "US", "USD")
    with pytest.raises(ValueError, match="symbols"):
        validate_registry((BENCHMARKS[0], duplicate))


def test_registry_validation_rejects_sector_without_sector_name() -> None:
    malformed = Benchmark("bad", "^BAD", "Bad", "sector", "US", "USD")
    with pytest.raises(ValueError, match="sector metadata"):
        validate_registry((malformed,))


def test_registry_validation_rejects_unknown_classification() -> None:
    malformed = Benchmark("bad", "^BAD", "Bad", "factor", "Global", "USD")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="kind"):
        validate_registry((malformed,))
