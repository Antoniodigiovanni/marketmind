"""Tests for the ESEF backfill job (mocked client, in-memory DuckDB)."""

from __future__ import annotations

import datetime as dt
import hashlib

from data.db.init import init_db
from data.filings.esef import backfill_esef
from data.sources.xbrlorg import EsefFiling


def _filing(fxo_id: str, lei: str, name: str, country: str, url: str) -> EsefFiling:
    return EsefFiling(
        fxo_id=fxo_id,
        lei=lei,
        entity_name=name,
        country=country,
        filing_system="ESEF",
        period_end="2020-12-31",
        date_added="2022-01-10",
        package_url=url,
        xhtml_url=None,
        json_url=None,
        sha256=None,
    )


class FakeXbrlorg:
    def __init__(
        self, by_country: dict[str, list[EsefFiling]], fail_url: str | None = None
    ) -> None:
        self.by_country = by_country
        self.fail_url = fail_url
        self.downloaded: list[str] = []

    def get_filings(self, country: str) -> list[EsefFiling]:
        return self.by_country.get(country, [])

    def download(self, url: str) -> bytes:
        self.downloaded.append(url)
        if url == self.fail_url:
            raise RuntimeError("boom")
        return b"PK\x03\x04fakezip"


def _universe_csv(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "eurostoxx600.csv").write_text(
        "ticker,name,country,sector,added_date,removed_date,internal_key,lei\n"
        "BAMI.MI,BANCO BPM,IT,,2026-08-04,,737826,815600AB12CD34EF5678\n"
        "SOPR.PA,SOPRA STERIA GROUP,FR,,2026-08-04,,FR702F,969500Y\n",
        encoding="utf-8",
    )


def test_backfill_inserts_matches_and_ignores_others(tmp_path) -> None:
    _universe_csv(tmp_path / "universe")
    conn = init_db(":memory:")
    try:
        client = FakeXbrlorg(
            {
                "IT": [
                    _filing(
                        "ESEF/IT/1",
                        "815600AB12CD34EF5678",
                        "Banco BPM S.p.A.",
                        "IT",
                        "https://x/1.zip",
                    ),
                    _filing("ESEF/IT/2", "OTHERLEI", "Someone Else", "IT", "https://x/2.zip"),
                ],
                "FR": [
                    _filing("ESEF/FR/3", "969500Y", "Sopra Steria Group", "FR", "https://x/3.zip")
                ],
            }
        )

        counts = backfill_esef(
            conn,
            client,
            tmp_path,
            countries=("IT", "FR"),
            universe_dir=tmp_path / "universe",
        )

        assert counts == {
            "fetched": 3,
            "inserted": 2,
            "skipped": 0,
            "ignored": 1,
            "failed": 0,
            "unresolved": 0,
        }
        rows = conn.execute(
            "SELECT fxo_id, lei, ticker, country, filed_at, period_end, text_hash, size_bytes "
            "FROM esef_filings ORDER BY fxo_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][:4] == ("ESEF/FR/3", "969500Y", "SOPR.PA", "FR")
        assert rows[1][:4] == ("ESEF/IT/1", "815600AB12CD34EF5678", "BAMI.MI", "IT")
        assert rows[1][4] == dt.date(2022, 1, 10)
        assert rows[1][5] == dt.date(2020, 12, 31)
        assert rows[1][6] == hashlib.sha256(b"PK\x03\x04fakezip").hexdigest()
        assert (tmp_path / "filings-eu" / "815600AB12CD34EF5678" / "ESEF_IT_1.zip").exists()
    finally:
        conn.close()


def test_backfill_is_resumable(tmp_path) -> None:
    _universe_csv(tmp_path / "universe")
    conn = init_db(":memory:")
    try:
        client = FakeXbrlorg(
            {
                "IT": [
                    _filing(
                        "ESEF/IT/1",
                        "815600AB12CD34EF5678",
                        "Banco BPM S.p.A.",
                        "IT",
                        "https://x/1.zip",
                    )
                ]
            }
        )
        first = backfill_esef(
            conn, client, tmp_path, countries=("IT",), universe_dir=tmp_path / "universe"
        )
        second = backfill_esef(
            conn, client, tmp_path, countries=("IT",), universe_dir=tmp_path / "universe"
        )
        assert first["inserted"] == 1
        assert second == {
            "fetched": 1,
            "inserted": 0,
            "skipped": 1,
            "ignored": 0,
            "failed": 0,
            "unresolved": 0,
        }
    finally:
        conn.close()


def test_backfill_failure_does_not_abort_sweep(tmp_path) -> None:
    _universe_csv(tmp_path / "universe")
    conn = init_db(":memory:")
    try:
        client = FakeXbrlorg(
            {
                "IT": [
                    _filing(
                        "ESEF/IT/1",
                        "815600AB12CD34EF5678",
                        "Banco BPM S.p.A.",
                        "IT",
                        "https://x/1.zip",
                    ),
                    _filing(
                        "ESEF/IT/9",
                        "815600AB12CD34EF5678",
                        "Banco BPM S.p.A.",
                        "IT",
                        "https://x/bad.zip",
                    ),
                ]
            },
            fail_url="https://x/bad.zip",
        )
        counts = backfill_esef(
            conn, client, tmp_path, countries=("IT",), universe_dir=tmp_path / "universe"
        )
        assert counts == {
            "fetched": 2,
            "inserted": 1,
            "skipped": 0,
            "ignored": 0,
            "failed": 1,
            "unresolved": 0,
        }
        assert conn.execute("SELECT COUNT(*) FROM esef_filings").fetchone()[0] == 1
    finally:
        conn.close()
