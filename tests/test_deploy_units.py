"""Guards on the systemd unit files — deploy invariants that are easy to regress."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UNIT_DIR = Path(__file__).resolve().parent.parent / "deploy" / "systemd"
INSTALL_TREE = "/srv/marketmind"


def _exec_starts(text: str) -> list[str]:
    """Return the ExecStart command lines of a unit file."""
    return [
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith("ExecStart=") and "=" in line
    ]


@pytest.mark.parametrize("unit", sorted(UNIT_DIR.glob("*.service")), ids=lambda p: p.name)
def test_uv_invocations_never_resync_at_run_time(unit: Path) -> None:
    """`uv run` must pass --frozen --no-sync.

    ProtectSystem=strict leaves .venv outside ReadWritePaths, so any attempt by
    uv to re-lock or re-sync the environment fails the unit with a read-only
    filesystem error.
    """
    commands = _exec_starts(unit.read_text(encoding="utf-8"))
    assert commands, f"{unit.name}: no ExecStart parsed — the guard would be vacuous"
    for command in commands:
        if "uv run" not in command:
            continue
        assert "--frozen" in command, f"{unit.name}: `uv run` without --frozen: {command}"
        assert "--no-sync" in command, f"{unit.name}: `uv run` without --no-sync: {command}"


@pytest.mark.parametrize("unit", sorted(UNIT_DIR.glob("*.service")), ids=lambda p: p.name)
def test_root_units_do_not_execute_from_the_install_tree(unit: Path) -> None:
    """Root-run units must not execute scripts the service user can rewrite.

    The install tree is chowned to the unprivileged `marketmind` user, which
    processes untrusted network input; a root ExecStart pointing into it is a
    privilege-escalation path.
    """
    text = unit.read_text(encoding="utf-8")
    if not re.search(r"^User=root$", text, re.MULTILINE):
        return
    for command in _exec_starts(text):
        assert INSTALL_TREE not in command, (
            f"{unit.name} runs as root but executes from the writable install tree: {command}"
        )
