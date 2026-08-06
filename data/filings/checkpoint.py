"""Checkpoint read/write for resumable backfill jobs."""

from __future__ import annotations

import duckdb


def has_checkpoint(conn: duckdb.DuckDBPyConnection, source: str, target_key: str) -> bool:
    """Return True if a checkpoint exists for (source, target_key)."""
    row = conn.execute(
        "SELECT 1 FROM fetch_checkpoints WHERE source = ? AND target_key = ?",
        [source, target_key],
    ).fetchone()
    return row is not None


def mark_checkpoint(conn: duckdb.DuckDBPyConnection, source: str, target_key: str) -> None:
    """Record that (source, target_key) has been processed."""
    conn.execute(
        """
        INSERT INTO fetch_checkpoints (source, target_key) VALUES (?, ?)
        ON CONFLICT (source, target_key) DO NOTHING
        """,
        [source, target_key],
    )
