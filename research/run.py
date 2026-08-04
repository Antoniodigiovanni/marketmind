"""Entry point: run a backtest by slug. Enforces hypothesis.md exists first."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def run(slug: str) -> None:
    """Run the backtest for the given research slug.

    Enforces that a hypothesis.md file exists in the research/{slug}/ directory
    before executing any backtest code.

    Args:
        slug: Research folder name (e.g. '2024-01-15_momentum').

    Raises:
        FileNotFoundError: If hypothesis.md does not exist for the slug.
        NotImplementedError: Until backtest runner is implemented.
    """
    research_dir = Path(__file__).parent / slug
    hypothesis_path = research_dir / "hypothesis.md"

    if not hypothesis_path.exists():
        raise FileNotFoundError(
            f"hypothesis.md not found at {hypothesis_path}. "
            "Create a hypothesis before running a backtest."
        )

    logger.info(f"Running backtest for slug: {slug}")
    raise NotImplementedError


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: uv run python -m research.run <slug>")
        sys.exit(1)
    run(sys.argv[1])
