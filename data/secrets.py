"""Read service credentials from an environment value or a mounted file."""

from __future__ import annotations

import os
from pathlib import Path


def credential(name: str) -> str | None:
    """Return a credential, preferring its file-backed form.

    Args:
        name: Environment variable name; ``<name>_FILE`` names a mounted file.

    Returns:
        The nonblank credential, or None when neither form is configured.

    Raises:
        ValueError: If both forms are configured or the file is empty.
    """
    value = os.getenv(name)
    path = os.getenv(f"{name}_FILE")
    if value and path:
        raise ValueError(f"set only one of {name} and {name}_FILE")
    if path:
        result = Path(path).read_text(encoding="utf-8").strip()
        if not result:
            raise ValueError(f"{name}_FILE is empty")
        return result
    return value.strip() if value else None
