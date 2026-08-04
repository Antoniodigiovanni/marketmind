"""Tests for the symbol-map resolution job (mocked lookups, in-memory DB)."""

from __future__ import annotations

import polars as pl

import data.universe.resolve as resolve
from data.db.init import init_db


def _write_universe(tmp_path) -> None:
    sp = pl.DataFrame(
        {
            "ticker": ["AAPL", "NOPE", "BRK-B"],
            "name": ["Apple Inc.", "No Such Co", "Berkshire B"],
            "country": ["US", "US", "US"],
            "sector": ["Technology", "X", "Financials"],
            "added_date": ["2026-08-04"] * 3,
            "removed_date": [None] * 3,
            "cusip": ["037833100", None, None],
            "isin": [None, None, "US0846707026"],
        }
    )
    st = pl.DataFrame(
        {
            "ticker": ["ASML.AS"],
            "name": ["ASML Holding"],
            "country": ["NL"],
            "sector": ["Technology"],
            "added_date": ["2026-08-04"],
            "removed_date": [None],
            "internal_key": ["546078"],
            "isin": [None],
            "cusip": [None],
        }
    )
    sp.write_csv(tmp_path / "sp500.csv")
    st.write_csv(tmp_path / "eurostoxx600.csv")


def _fake_lookup(by_id: dict[str, dict[str, str] | None]) -> None:
    def searcher(id_value: str, **kwargs: object) -> dict[str, str] | None:
        return by_id.get(id_value)

    resolve.search_by_id = searcher
    resolve.symbol_has_data = lambda symbol: True  # noqa: E731


def test_resolution_populates_symbol_map(tmp_path) -> None:
    _write_universe(tmp_path)
    _fake_lookup(
        {
            "037833100": {"symbol": "AAPL", "name": "Apple Inc."},
            "US0846707026": {"symbol": "BRK-B", "name": "Berkshire B"},
            "ASML.AS": {"symbol": "ASML.AS", "name": "ASML Holding"},
        }
    )

    conn = init_db(":memory:")
    try:
        stats = resolve.resolve_universe(conn, universe_dir=tmp_path)
        rows = conn.execute("SELECT id_type, id_value, symbol, status FROM symbol_map").fetchall()
        assert len(rows) == 4
        assert stats["unresolved"] == 1  # NOPE has no lookup hit
        assert ("CUSIP", "037833100", "AAPL", "valid") in rows
        assert ("ISIN", "US0846707026", "BRK-B", "valid") in rows
        assert ("TICKER", "ASML.AS", "ASML.AS", "valid") in rows
    finally:
        conn.close()


def test_name_fallback_resolves_when_ticker_search_fails(tmp_path) -> None:
    _write_universe(tmp_path)
    calls: list[str] = []

    def searcher(id_value: str, **kwargs: object) -> dict[str, str] | None:
        calls.append(id_value)
        if id_value == "No Such Co":
            return {"symbol": "REAL", "name": "Real Company"}
        return None

    resolve.search_by_id = searcher
    resolve.symbol_has_data = lambda symbol: True  # noqa: E731

    conn = init_db(":memory:")
    try:
        stats = resolve.resolve_universe(conn, universe_dir=tmp_path)
        assert stats["valid"] == 1  # NOPE resolved via name fallback
        assert calls.count("No Such Co") == 1
        row = conn.execute(
            "SELECT symbol, status FROM symbol_map WHERE id_value = 'NOPE'"
        ).fetchone()
        assert row == ("REAL", "valid")
    finally:
        conn.close()


def test_candidate_variants_resolve_share_class(tmp_path) -> None:
    _write_universe(tmp_path)
    calls: list[str] = []

    def searcher(id_value: str, **kwargs: object) -> dict[str, str] | None:
        calls.append(id_value)
        if id_value == "ERIC-B.ST":
            return {"symbol": "ERIC-B.ST", "name": "Ericsson B"}
        return None

    resolve.search_by_id = searcher
    resolve.symbol_has_data = lambda symbol: True  # noqa: E731

    # A STOXX-style member whose RIC needs share-class hyphenation.
    import polars as pl

    st = pl.DataFrame(
        {
            "ticker": ["ERICb.ST"],
            "name": ["ERICSSON LM B"],
            "country": ["SE"],
            "sector": ["Technology"],
            "added_date": ["2026-08-04"],
            "removed_date": [None],
            "internal_key": ["KEYX"],
            "isin": [None],
            "cusip": [None],
        }
    )
    st.write_csv(tmp_path / "eurostoxx600.csv")
    (tmp_path / "sp500.csv").unlink(missing_ok=True)

    conn = init_db(":memory:")
    try:
        resolve.resolve_universe(conn, universe_dir=tmp_path)
        assert calls[0] == "ERICb.ST"
        assert calls[1] == "ERIC-B.ST"
        row = conn.execute("SELECT symbol, status FROM symbol_map").fetchone()
        assert row == ("ERIC-B.ST", "valid")
    finally:
        conn.close()


def test_chart_failure_marks_review(tmp_path) -> None:
    _write_universe(tmp_path)
    _fake_lookup({"037833100": {"symbol": "AAPL", "name": "Apple Inc."}})

    def checker(symbol: str) -> bool:
        return symbol != "AAPL"

    resolve.symbol_has_data = checker
    conn = init_db(":memory:")
    try:
        stats = resolve.resolve_universe(conn, universe_dir=tmp_path)
        status = conn.execute(
            "SELECT status FROM symbol_map WHERE id_value = '037833100'"
        ).fetchone()[0]
        assert status == "review"
        assert stats["review"] == 1
    finally:
        conn.close()


def test_rerun_updates_existing_rows(tmp_path) -> None:
    _write_universe(tmp_path)
    _fake_lookup({"037833100": {"symbol": "AAPL", "name": "Apple Inc."}})
    conn = init_db(":memory:")
    try:
        resolve.resolve_universe(conn, universe_dir=tmp_path)
        resolve.resolve_universe(conn, universe_dir=tmp_path)
        count = conn.execute("SELECT COUNT(*) FROM symbol_map").fetchone()[0]
        assert count == 4  # upsert, not duplicates
    finally:
        conn.close()
