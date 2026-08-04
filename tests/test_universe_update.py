"""Tests for the universe update job."""

from __future__ import annotations

import polars as pl
import pytest

import data.universe.update as upd


def _sp500(tickers: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ticker": tickers,
            "name": [f"Company {t}" for t in tickers],
            "sector": ["Technology"] * len(tickers),
            "country": ["US"] * len(tickers),
            "added_date": ["2026-01-01"] * len(tickers),
        }
    )


def _stoxx(tickers: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ticker": [t for t, _ in tickers],
            "name": [f"Company {t}" for t, _ in tickers],
            "sector": ["Technology"] * len(tickers),
            "country": [c for _, c in tickers],
        }
    )


def test_initial_snapshot_writes_both_universes(tmp_path) -> None:
    summary = upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL", "MSFT"]),
        stoxx600=_stoxx([("ASML", "Netherlands"), ("SAP", "Germany")]),
    )

    assert summary["sp500"]["added"] == ["AAPL", "MSFT"]
    assert summary["eurostoxx600"]["added"] == ["ASML.AS", "SAP.DE"]

    sp500 = pl.read_csv(tmp_path / "sp500.csv")
    stoxx = pl.read_csv(tmp_path / "eurostoxx600.csv")
    assert set(sp500["ticker"].to_list()) == {"AAPL", "MSFT"}
    assert sp500["added_date"].to_list() == ["2026-01-01"] * 2
    assert set(stoxx["ticker"].to_list()) == {"ASML.AS", "SAP.DE"}
    assert (tmp_path / "delisted.csv").exists() is False


def test_sp500_source_added_date_is_preserved(tmp_path) -> None:
    summary = upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=pl.DataFrame(
            {
                "ticker": ["MMM"],
                "name": ["3M"],
                "sector": ["Industrials"],
                "country": ["US"],
                "added_date": ["1957-03-04"],
            }
        ),
        stoxx600=_stoxx([("ASML", "Netherlands")]),
    )
    sp500 = pl.read_csv(tmp_path / "sp500.csv")
    assert sp500["added_date"].to_list() == ["1957-03-04"]
    assert summary["sp500"]["added"] == ["MMM"]


def test_duplicate_yahoo_symbols_are_deduplicated(tmp_path) -> None:
    # SNY and SASY both map to SASY.PA; the CSV must contain it once.
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL"]),
        stoxx600=_stoxx([("SNY", "France"), ("SASY", "France")]),
    )
    stoxx = pl.read_csv(tmp_path / "eurostoxx600.csv")
    assert stoxx["ticker"].to_list() == ["SASY.PA"]


def test_removal_sets_removed_date_and_logs_delisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(upd, "MAX_DROP_FRACTION", 0.6)
    tickers = [f"T{i:02d}" for i in range(10)]
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(tickers),
        stoxx600=_stoxx([("ASML", "Netherlands")]),
    )

    kept = [t for t in tickers if t != "T00"]
    upd.update_universe(
        as_of="2026-08-05",
        universe_dir=tmp_path,
        sp500=_sp500(kept),
        stoxx600=_stoxx([("ASML", "Netherlands")]),
    )

    sp500 = pl.read_csv(tmp_path / "sp500.csv")
    removed = sp500.filter(pl.col("ticker") == "T00")
    assert removed["removed_date"].to_list() == ["2026-08-05"]
    assert sp500.filter(pl.col("ticker") == "T01")["removed_date"].to_list() == [None]

    delisted = pl.read_csv(tmp_path / "delisted.csv")
    assert delisted["ticker"].to_list() == ["T00"]
    assert delisted["delisted_date"].to_list() == ["2026-08-05"]


def test_large_source_drop_raises_guard(tmp_path) -> None:
    tickers = [f"T{i:02d}" for i in range(10)]
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(tickers),
        stoxx600=_stoxx([("ASML", "Netherlands")]),
    )

    with pytest.raises(ValueError, match="refusing removals"):
        upd.update_universe(
            as_of="2026-08-05",
            universe_dir=tmp_path,
            sp500=_sp500(tickers[:9]),
            stoxx600=_stoxx([("ASML", "Netherlands")]),
        )


def test_idempotent_rerun_makes_no_changes(tmp_path) -> None:
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL"]),
        stoxx600=_stoxx([("ASML", "Netherlands")]),
    )
    summary = upd.update_universe(
        as_of="2026-08-05",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL"]),
        stoxx600=_stoxx([("ASML", "Netherlands")]),
    )
    assert summary["sp500"] == {"added": [], "removed": []}
    assert summary["eurostoxx600"] == {"added": [], "removed": []}


def test_stoxx_oldid_is_passed_to_fetch(monkeypatch, tmp_path) -> None:
    calls: dict[str, int] = {}

    def fake_fetch_stoxx600(**kwargs: object) -> pl.DataFrame:
        calls.update(kwargs)  # type: ignore[arg-type]
        return _stoxx([("ASML", "Netherlands")])

    monkeypatch.setattr(upd, "fetch_stoxx600", fake_fetch_stoxx600)
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL"]),
        stoxx_oldid=999,
    )
    assert calls == {"oldid": 999}
