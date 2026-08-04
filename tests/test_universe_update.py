"""Tests for the universe update job (official sources)."""

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
            "weight": [1.0] * len(tickers),
            "currency": ["USD"] * len(tickers),
        }
    )


def _stoxx(rics: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "internal_key": [f"KEY{i}" for i in range(len(rics))],
            "ric": [r for r, _ in rics],
            "name": [f"Company {r}" for r, _ in rics],
            "country": [c for _, c in rics],
            "exchange": ["Euronext Amsterdam"] * len(rics),
            "currency": ["EUR"] * len(rics),
            "rank": list(range(1, len(rics) + 1)),
        }
    )


def test_initial_snapshot_writes_both_universes(tmp_path) -> None:
    summary = upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL", "MSFT"]),
        stoxx600=_stoxx([("ASML.AS", "NL"), ("SAP.DE", "DE")]),
    )

    assert summary["sp500"]["added"] == ["AAPL", "MSFT"]
    assert summary["eurostoxx600"]["added"] == ["ASML.AS", "SAP.DE"]

    sp500 = pl.read_csv(tmp_path / "sp500.csv")
    stoxx = pl.read_csv(tmp_path / "eurostoxx600.csv")
    assert set(sp500["ticker"].to_list()) == {"AAPL", "MSFT"}
    assert sp500["added_date"].to_list() == ["2026-08-04"] * 2
    assert set(stoxx["ticker"].to_list()) == {"ASML.AS", "SAP.DE"}
    assert set(stoxx["internal_key"].to_list()) == {"KEY0", "KEY1"}
    assert (tmp_path / "delisted.csv").exists() is False


def test_swiss_ric_becomes_yahoo_suffix(tmp_path) -> None:
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL"]),
        stoxx600=_stoxx([("NOVN.S", "CH")]),
    )
    stoxx = pl.read_csv(tmp_path / "eurostoxx600.csv")
    assert stoxx["ticker"].to_list() == ["NOVN.SW"]


def test_known_ric_mismatches_are_applied(tmp_path) -> None:
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL"]),
        stoxx600=_stoxx([("MCF.PA", "FR"), ("TTEF.PA", "FR")]),
    )
    stoxx = pl.read_csv(tmp_path / "eurostoxx600.csv")
    assert set(stoxx["ticker"].to_list()) == {"MC.PA", "TTE.PA"}


def test_us_class_shares_normalised_in_csv(tmp_path) -> None:
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["BRKB", "BFB"]),
        stoxx600=_stoxx([("ASML.AS", "NL")]),
    )
    sp500 = pl.read_csv(tmp_path / "sp500.csv")
    assert set(sp500["ticker"].to_list()) == {"BRK-B", "BF-B"}


def test_removal_sets_removed_date_and_logs_delisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(upd, "MAX_DROP_FRACTION", 0.6)
    tickers = [f"T{i:02d}" for i in range(10)]
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(tickers),
        stoxx600=_stoxx([("ASML.AS", "NL")]),
    )

    kept = [t for t in tickers if t != "T00"]
    upd.update_universe(
        as_of="2026-08-05",
        universe_dir=tmp_path,
        sp500=_sp500(kept),
        stoxx600=_stoxx([("ASML.AS", "NL")]),
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
        stoxx600=_stoxx([("ASML.AS", "NL")]),
    )

    with pytest.raises(ValueError, match="refusing removals"):
        upd.update_universe(
            as_of="2026-08-05",
            universe_dir=tmp_path,
            sp500=_sp500(tickers[:9]),
            stoxx600=_stoxx([("ASML.AS", "NL")]),
        )


def test_idempotent_rerun_makes_no_changes(tmp_path) -> None:
    upd.update_universe(
        as_of="2026-08-04",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL"]),
        stoxx600=_stoxx([("ASML.AS", "NL")]),
    )
    summary = upd.update_universe(
        as_of="2026-08-05",
        universe_dir=tmp_path,
        sp500=_sp500(["AAPL"]),
        stoxx600=_stoxx([("ASML.AS", "NL")]),
    )
    assert summary["sp500"] == {"added": [], "removed": []}
    assert summary["eurostoxx600"] == {"added": [], "removed": []}


def test_fetch_is_used_when_frames_not_injected(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def fake_cspx() -> pl.DataFrame:
        calls.append("cspx")
        return _sp500(["AAPL"])

    def fake_stoxx() -> pl.DataFrame:
        calls.append("stoxx")
        return _stoxx([("ASML.AS", "NL")])

    monkeypatch.setattr(upd, "fetch_cspx_holdings", fake_cspx)
    monkeypatch.setattr(upd, "fetch_selection_list", fake_stoxx)
    upd.update_universe(as_of="2026-08-04", universe_dir=tmp_path)
    assert calls == ["cspx", "stoxx"]
