"""Production runtime compatibility constraints."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_production_uses_the_polars_compatibility_runtime() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert '"polars[rtcompat]>=0.20"' in project
    assert '{ name = "polars", extra = ["rtcompat"] }' in lock
    assert 'name = "polars-runtime-compat"' in lock


def test_daily_fetch_script_does_not_shadow_standard_library_modules() -> None:
    """Load the script exactly as scheduled jobs do in production."""
    result = subprocess.run(
        [sys.executable, "data/fetch.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
