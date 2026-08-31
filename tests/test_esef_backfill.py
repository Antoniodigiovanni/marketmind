"""PostgreSQL-only European filing backfill tests."""

from __future__ import annotations

import datetime as dt

from data.db.identifiers import get_or_create_identifier
from data.filings.esef import backfill_esef
from data.sources.xbrlorg import EsefFiling


def _filing(fxo_id: str, lei: str, url: str) -> EsefFiling:
    return EsefFiling(
        fxo_id=fxo_id,
        lei=lei,
        entity_name="Banco BPM S.p.A.",
        country="IT",
        filing_system="ESEF",
        period_end="2025-12-31",
        date_added="2026-04-01",
        package_url=url,
        xhtml_url=None,
        json_url=None,
        sha256=None,
    )


class FakeClient:
    """Minimal European filing index client."""

    def get_filings(self, country: str) -> list[EsefFiling]:
        return [
            _filing("ESEF/IT/1", "815600AB12CD34EF5678", "https://example.test/1.zip"),
            _filing("ESEF/IT/2", "00000000000000000000", "https://example.test/2.zip"),
        ]

    def download(self, url: str) -> bytes:
        return b"PK\x03\x04test"


async def test_esef_backfill_resolves_company_and_is_resumable(pg_conn, tmp_path) -> None:
    """Archive a matched package once and associate its issuer."""
    universe_dir = tmp_path / "universe"
    universe_dir.mkdir()
    (universe_dir / "eurostoxx600.csv").write_text(
        "ticker,name,country,sector,added_date,removed_date,lei\n"
        "BAMI.MI,Banco BPM,IT,Financials,2020-01-01,,815600AB12CD34EF5678\n",
        encoding="utf-8",
    )
    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="BAMI.MI",
        valid_from=dt.date(2020, 1, 1),
        source="test",
        name="Banco BPM",
        country="IT",
        sector="Financials",
    )

    first = await backfill_esef(
        pg_conn, FakeClient(), tmp_path, countries=("IT",), universe_dir=universe_dir
    )
    second = await backfill_esef(
        pg_conn, FakeClient(), tmp_path, countries=("IT",), universe_dir=universe_dir
    )

    assert first == {"fetched": 2, "inserted": 1, "skipped": 0, "ignored": 1, "failed": 0}
    assert second == {
        "fetched": 2,
        "inserted": 0,
        "skipped": 1,
        "ignored": 1,
        "failed": 0,
    }
    row = await (
        await pg_conn.execute("SELECT company_id FROM esef_filings WHERE fxo_id = 'ESEF/IT/1'")
    ).fetchone()
    assert row[0] == company_id
    assert (tmp_path / "filings-eu" / "815600AB12CD34EF5678" / "ESEF_IT_1.zip").exists()
