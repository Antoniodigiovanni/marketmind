"""Bounded, traversal-safe reads of scheduled-job log files."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from dashboard.config import MAX_TAIL_BYTES


def _entry(path: Path) -> dict[str, object]:
    """Describe one readable log file."""
    stat = path.stat()
    return {
        "name": path.name,
        "directory": str(path.parent),
        "size_bytes": stat.st_size,
        "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.UTC),
    }


def list_logs(directories: list[Path]) -> tuple[list[dict[str, object]], list[str]]:
    """List the .log files the dashboard may serve, newest first.

    Args:
        directories: Configured log directories, in order.

    Returns:
        A (files, problems) pair. `problems` names directories that are
        missing or unreadable -- the cron log directory is root-owned, so an
        unreadable one is a normal deployment state to report, not an error.
    """
    files: list[dict[str, object]] = []
    problems: list[str] = []
    for directory in directories:
        try:
            entries = sorted(directory.glob("*.log"))
        except (OSError, PermissionError) as exc:
            problems.append(f"{directory}: {exc.strerror or exc}")
            continue
        if not directory.exists():
            problems.append(f"{directory}: not present")
            continue
        for path in entries:
            try:
                files.append(_entry(path))
            except (OSError, PermissionError) as exc:
                problems.append(f"{path}: {exc.strerror or exc}")
    files.sort(key=lambda entry: entry["modified_at"], reverse=True)
    return files, problems


def resolve_log(directories: list[Path], name: str) -> Path | None:
    """Resolve a log file name against the allowed directories.

    Args:
        directories: Configured log directories, in order.
        name: Requested file name.

    Returns:
        The matching path, or None when the name is not an allowed log file.
        Matching is by exact name against a directory listing rather than by
        joining the request to a path, so `..` and absolute names cannot
        escape the configured directories.
    """
    if name != Path(name).name or not name.endswith(".log"):
        return None
    for directory in directories:
        try:
            for path in directory.glob("*.log"):
                if path.name == name and path.is_file():
                    return path
        except (OSError, PermissionError):
            continue
    return None


def tail(path: Path, *, max_bytes: int = MAX_TAIL_BYTES) -> tuple[str, bool]:
    """Read the end of a log file.

    Args:
        path: File to read.
        max_bytes: Maximum number of trailing bytes to return.

    Returns:
        A (text, truncated) pair. Reading only the tail keeps an unrotated
        multi-megabyte log from being loaded into memory to render one page.
    """
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        offset = max(0, size - max_bytes)
        handle.seek(offset)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    if offset:
        text = text.split("\n", 1)[-1]
    return text, bool(offset)
