"""Tests for the macro series registry."""

from __future__ import annotations

from data.macro.series import MACRO_SERIES, SERIES_BY_ID, get_series


def test_registry_ids_are_unique() -> None:
    ids = [series.id for series in MACRO_SERIES]
    assert len(ids) == len(set(ids))


def test_registry_uses_known_providers() -> None:
    assert {series.provider for series in MACRO_SERIES} <= {"fred", "ecb"}


def test_registry_keys_are_non_empty() -> None:
    for series in MACRO_SERIES:
        assert series.key, f"{series.id} has an empty provider key"


def test_series_by_id_resolves_every_series() -> None:
    for series in MACRO_SERIES:
        assert SERIES_BY_ID[series.id] is series


def test_get_series_unknown_returns_none() -> None:
    assert get_series("NOPE") is None


def test_ecb_keys_are_full_sdmx_paths() -> None:
    for series in MACRO_SERIES:
        if series.provider == "ecb":
            assert "/" in series.key
