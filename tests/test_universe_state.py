"""Tests for production universe-state path selection."""

from __future__ import annotations

from pathlib import Path

from data.universe import loader


def test_default_universe_dir_uses_the_package_baseline(monkeypatch) -> None:
    monkeypatch.delenv("MARKETMIND_UNIVERSE_DIR", raising=False)

    assert loader._default_universe_dir() == Path(loader.__file__).parent


def test_default_universe_dir_accepts_a_runtime_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MARKETMIND_UNIVERSE_DIR", str(tmp_path))

    assert loader._default_universe_dir() == tmp_path
