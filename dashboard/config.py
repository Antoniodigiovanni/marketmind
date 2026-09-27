"""Environment configuration for the read-only web app."""

from __future__ import annotations

import os
from pathlib import Path

HOST_ENV = "MARKETMIND_DASHBOARD_HOST"
PORT_ENV = "MARKETMIND_DASHBOARD_PORT"
DSN_ENV = "MARKETMIND_DASHBOARD_DSN"
LOG_DIRS_ENV = "MARKETMIND_DASHBOARD_LOG_DIRS"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
MAX_TAIL_BYTES = 256 * 1024


def host() -> str:
    """Return the dashboard bind address, defaulting to loopback only."""
    return os.getenv(HOST_ENV, DEFAULT_HOST).strip() or DEFAULT_HOST


def port() -> int:
    """Return the dashboard listen port."""
    return int(os.getenv(PORT_ENV, str(DEFAULT_PORT)))


def dsn() -> str:
    """Return the dashboard database DSN, refusing an absent configuration.

    Returns:
        The configured dashboard connection string.

    Raises:
        RuntimeError: If the connection string is unset or blank.
    """
    value = os.getenv(DSN_ENV, "").strip()
    if not value:
        raise RuntimeError(f"{DSN_ENV} must be set to a nonblank value")
    return value


def log_dirs() -> list[Path]:
    """Return the directories whose .log files the dashboard may serve.

    Returns:
        Configured directories in order, colon-separated in the environment.
        Missing or unreadable directories are kept: the reader reports them
        rather than failing at startup, since the cron log directory is
        root-owned and may legitimately be unreadable.
    """
    configured = os.getenv(LOG_DIRS_ENV, "").strip()
    if not configured:
        return []
    return [Path(part) for part in configured.split(":") if part]
