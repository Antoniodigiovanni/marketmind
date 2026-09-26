"""Environment configuration for the read-only operations dashboard."""

from __future__ import annotations

import os
from pathlib import Path

from data.credentials import credential

HOST_ENV = "MARKETMIND_DASHBOARD_HOST"
PORT_ENV = "MARKETMIND_DASHBOARD_PORT"
TOKEN_ENV = "MARKETMIND_DASHBOARD_TOKEN"
DSN_ENV = "MARKETMIND_DASHBOARD_DSN"
LOG_DIRS_ENV = "MARKETMIND_DASHBOARD_LOG_DIRS"
UNIVERSE_URL_ENV = "MARKETMIND_CATALOG_PUBLIC_URL"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
MAX_TAIL_BYTES = 256 * 1024


def host() -> str:
    """Return the dashboard bind address, defaulting to loopback only."""
    return os.getenv(HOST_ENV, DEFAULT_HOST).strip() or DEFAULT_HOST


def port() -> int:
    """Return the dashboard listen port."""
    return int(os.getenv(PORT_ENV, str(DEFAULT_PORT)))


def token() -> str:
    """Return the shared secret required on every request.

    Raises:
        RuntimeError: If the token is unset or blank. The dashboard exposes
            full run history and error text, so it never serves unauthenticated.
    """
    value = credential(TOKEN_ENV)
    if not value:
        raise RuntimeError(f"{TOKEN_ENV} must be set to a nonblank value")
    return value


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


def universe_url() -> str:
    """Return the browser-facing link to the universe section."""
    return os.getenv(UNIVERSE_URL_ENV, "http://127.0.0.1:8002/")


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
