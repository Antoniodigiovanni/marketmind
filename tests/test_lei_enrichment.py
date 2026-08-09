"""Tests for LEI enrichment of the Eurostoxx universe CSV."""

from __future__ import annotations

from data.universe.lei import enrich_lei, normalize_name


class FakeGleif:
    def __init__(self, results: dict[tuple[str, str], list[object]]) -> None:
        self.results = results
        self.calls = 0

    def search(self, name: str, country: str) -> list[object]:
        self.calls += 1
        return self.results.get((name, country), [])


def _candidate(lei: str, legal_name: str, status: str = "ISSUED") -> object:
    return type(
        "LeiCandidate",
        (),
        {"lei": lei, "legal_name": legal_name, "country": "IT", "status": status},
    )()


def test_normalize_name_strips_legal_forms() -> None:
    assert normalize_name("Banco BPM S.p.A.") == "banco bpm"
    assert normalize_name("HALMA PLC") == "halma"
    assert normalize_name("SOPRA STERIA GROUP") == "sopra steria group"


def test_enrich_writes_lei_column(tmp_path) -> None:
    csv = tmp_path / "eurostoxx600.csv"
    csv.write_text(
        "ticker,name,country,sector,added_date,removed_date,internal_key\n"
        "BAMI.MI,BANCO BPM,IT,,2026-08-04,,737826\n"
        "SOPR.PA,SOPRA STERIA GROUP,FR,,2026-08-04,,FR702F\n"
        "GONE.MI,GONE OLD,IT,,2026-01-01,2026-07-01,999999\n",
        encoding="utf-8",
    )
    fake = FakeGleif(
        {("BANCO BPM", "IT"): [_candidate("815600AB12CD34EF5678", "Banco BPM S.p.A.")]}
    )

    stats = enrich_lei(universe_dir=tmp_path, client=fake, countries=("IT",))

    assert stats == {"matched": 1, "unresolved": 0, "already_have": 0}
    lines = csv.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith(",lei")
    assert "BAMI.MI,BANCO BPM,IT,,2026-08-04,,737826,815600AB12CD34EF5678" in lines
    assert "SOPR.PA,SOPRA STERIA GROUP,FR,,2026-08-04,,FR702F," in lines
    # removed rows keep their historical shape and stay untouched
    assert "GONE.MI,GONE OLD,IT,,2026-01-01,2026-07-01,999999," in lines


def test_enrich_is_idempotent(tmp_path) -> None:
    csv = tmp_path / "eurostoxx600.csv"
    csv.write_text(
        "ticker,name,country,sector,added_date,removed_date,internal_key\n"
        "BAMI.MI,BANCO BPM,IT,,2026-08-04,,737826\n",
        encoding="utf-8",
    )
    fake = FakeGleif(
        {("BANCO BPM", "IT"): [_candidate("815600AB12CD34EF5678", "Banco BPM S.p.A.")]}
    )

    first = enrich_lei(universe_dir=tmp_path, client=fake, countries=("IT",))
    second = enrich_lei(universe_dir=tmp_path, client=fake, countries=("IT",))

    assert first["matched"] == 1
    assert second == {"matched": 0, "unresolved": 0, "already_have": 1}
    assert fake.calls == 1


def test_unresolved_members_stay_null(tmp_path) -> None:
    csv = tmp_path / "eurostoxx600.csv"
    csv.write_text(
        "ticker,name,country,sector,added_date,removed_date,internal_key\n"
        "BAMI.MI,BANCO BPM,IT,,2026-08-04,,737826\n",
        encoding="utf-8",
    )
    fake = FakeGleif({})

    stats = enrich_lei(universe_dir=tmp_path, client=fake, countries=("IT",))

    assert stats == {"matched": 0, "unresolved": 1, "already_have": 0}
    assert "BAMI.MI,BANCO BPM,IT,,2026-08-04,,737826," in csv.read_text(encoding="utf-8")
