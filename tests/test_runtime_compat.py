"""Production runtime compatibility constraints."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_production_uses_the_polars_compatibility_runtime() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert '"polars[rtcompat]>=0.20"' in project
    assert '{ name = "polars", extra = ["rtcompat"] }' in lock
    assert 'name = "polars-runtime-compat"' in lock
