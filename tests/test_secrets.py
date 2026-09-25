"""Credential loading for local development and production mounts."""

from __future__ import annotations

import pytest

from data.credentials import credential


def test_credential_reads_owner_file_without_newline(tmp_path, monkeypatch) -> None:
    path = tmp_path / "token"
    path.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("MARKETMIND_MCP_TOKEN_FILE", str(path))
    monkeypatch.delenv("MARKETMIND_MCP_TOKEN", raising=False)
    assert credential("MARKETMIND_MCP_TOKEN") == "file-secret"


def test_credential_rejects_ambiguous_or_empty_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "token"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("MARKETMIND_MCP_TOKEN_FILE", str(path))
    monkeypatch.setenv("MARKETMIND_MCP_TOKEN", "environment-secret")
    with pytest.raises(ValueError, match="set only one"):
        credential("MARKETMIND_MCP_TOKEN")
    monkeypatch.delenv("MARKETMIND_MCP_TOKEN")
    with pytest.raises(ValueError, match="empty"):
        credential("MARKETMIND_MCP_TOKEN")
