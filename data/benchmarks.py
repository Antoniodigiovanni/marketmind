"""Validated registry of free Yahoo benchmark price series."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

BenchmarkKind = Literal["market", "sector"]
BenchmarkRegion = Literal["US", "Europe"]
ALLOWED_KINDS = frozenset({"market", "sector"})
ALLOWED_REGIONS = frozenset({"US", "Europe"})


@dataclass(frozen=True, slots=True)
class Benchmark:
    """Metadata required to fetch and discover one benchmark series."""

    key: str
    symbol: str
    name: str
    kind: BenchmarkKind
    region: BenchmarkRegion
    currency: str
    sector: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly registry metadata."""
        return asdict(self)


BENCHMARKS: tuple[Benchmark, ...] = (
    Benchmark("sp500", "^GSPC", "S&P 500", "market", "US", "USD"),
    Benchmark(
        "stoxx_europe_600",
        "^STOXX",
        "STOXX Europe 600",
        "market",
        "Europe",
        "EUR",
    ),
    Benchmark("sp500_energy", "^SP500-10", "S&P 500 Energy", "sector", "US", "USD", "Energy"),
    Benchmark(
        "sp500_materials", "^SP500-15", "S&P 500 Materials", "sector", "US", "USD", "Materials"
    ),
    Benchmark(
        "sp500_industrials",
        "^SP500-20",
        "S&P 500 Industrials",
        "sector",
        "US",
        "USD",
        "Industrials",
    ),
    Benchmark(
        "sp500_consumer_discretionary",
        "^SP500-25",
        "S&P 500 Consumer Discretionary",
        "sector",
        "US",
        "USD",
        "Consumer Discretionary",
    ),
    Benchmark(
        "sp500_consumer_staples",
        "^SP500-30",
        "S&P 500 Consumer Staples",
        "sector",
        "US",
        "USD",
        "Consumer Staples",
    ),
    Benchmark(
        "sp500_health_care",
        "^SP500-35",
        "S&P 500 Health Care",
        "sector",
        "US",
        "USD",
        "Health Care",
    ),
    Benchmark(
        "sp500_financials",
        "^SP500-40",
        "S&P 500 Financials",
        "sector",
        "US",
        "USD",
        "Financials",
    ),
    Benchmark(
        "sp500_information_technology",
        "^SP500-45",
        "S&P 500 Information Technology",
        "sector",
        "US",
        "USD",
        "Information Technology",
    ),
    Benchmark(
        "sp500_communication_services",
        "^SP500-50",
        "S&P 500 Communication Services",
        "sector",
        "US",
        "USD",
        "Communication Services",
    ),
    Benchmark(
        "sp500_utilities",
        "^SP500-55",
        "S&P 500 Utilities",
        "sector",
        "US",
        "USD",
        "Utilities",
    ),
    Benchmark(
        "sp500_real_estate",
        "^SP500-60",
        "S&P 500 Real Estate",
        "sector",
        "US",
        "USD",
        "Real Estate",
    ),
)


def validate_registry(benchmarks: tuple[Benchmark, ...] = BENCHMARKS) -> None:
    """Validate stable keys, Yahoo symbols, currencies, and sector metadata.

    Args:
        benchmarks: Registry entries to validate.

    Raises:
        ValueError: If an entry is malformed or a key/symbol is duplicated.
    """
    keys = [benchmark.key for benchmark in benchmarks]
    symbols = [benchmark.symbol for benchmark in benchmarks]
    if len(keys) != len(set(keys)):
        raise ValueError("benchmark keys must be unique")
    if len(symbols) != len(set(symbols)):
        raise ValueError("benchmark symbols must be unique")
    for benchmark in benchmarks:
        if not benchmark.key or not benchmark.symbol.startswith("^") or not benchmark.name:
            raise ValueError(f"invalid benchmark identity: {benchmark!r}")
        if benchmark.kind not in ALLOWED_KINDS:
            raise ValueError(f"invalid benchmark kind: {benchmark.kind!r}")
        if benchmark.region not in ALLOWED_REGIONS:
            raise ValueError(f"invalid benchmark region: {benchmark.region!r}")
        if len(benchmark.currency) != 3 or benchmark.currency != benchmark.currency.upper():
            raise ValueError(f"invalid benchmark currency: {benchmark.currency!r}")
        if (benchmark.kind == "sector") != (benchmark.sector is not None):
            raise ValueError(f"sector metadata does not match kind for {benchmark.key}")


def get_benchmarks(*, kind: str | None = None, region: str | None = None) -> tuple[Benchmark, ...]:
    """Return registry entries matching optional exact filters.

    Args:
        kind: Optional ``market`` or ``sector`` filter.
        region: Optional ``US`` or ``Europe`` filter.

    Returns:
        Matching benchmarks in stable registry order.

    Raises:
        ValueError: If a filter is not one of the documented values.
    """
    if kind is not None and kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {sorted(ALLOWED_KINDS)}")
    if region is not None and region not in ALLOWED_REGIONS:
        raise ValueError(f"region must be one of {sorted(ALLOWED_REGIONS)}")
    return tuple(
        benchmark
        for benchmark in BENCHMARKS
        if (kind is None or benchmark.kind == kind)
        and (region is None or benchmark.region == region)
    )


validate_registry()
