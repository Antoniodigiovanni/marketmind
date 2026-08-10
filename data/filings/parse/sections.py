"""Section chunker for SEC filing text (10-K/10-Q/20-F/6-K).

A 10-K runs to hundreds of thousands of characters, far past any usable
extraction context, so filing text has to be split before it can be fed to a
model. Splitting on ITEM headers keeps each chunk semantically whole — risk
factors stay with risk factors — and falls back to fixed overlapping windows
for filings that carry no recognisable headers.
"""

from __future__ import annotations

import re

DEFAULT_MAX_CHUNK = 8000
OVERLAP = 500

_HEADER_RE = re.compile(r"^\s*(?:part\s+[ivx]+\s*)?item\s+(\d+[a-z]?)(?:\.|:|\s|$)", re.IGNORECASE)


def split_sections(text: str, *, max_chunk_chars: int = DEFAULT_MAX_CHUNK) -> list[str]:
    """Split filing text into section chunks bounded by ITEM headers.

    Falls back to fixed-size overlapping chunks when no ITEM headers exist.
    A section longer than the limit is split further, and each continuation
    chunk is prefixed with its section header so it can still be attributed.

    Args:
        text: Plain filing text (see data.filings.parse.html.html_to_text).
        max_chunk_chars: Maximum characters per returned chunk.

    Returns:
        Non-empty text chunks, each trimmed, section-bounded where possible.
    """
    lines = text.splitlines()
    boundaries: list[tuple[int, str]] = [
        (idx, line.strip()) for idx, line in enumerate(lines) if _HEADER_RE.match(line)
    ]
    if not boundaries:
        return _fixed_chunks(text, max_chunk_chars)

    chunks: list[str] = []
    for i, (idx, header) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(lines)
        section = "\n".join(lines[idx:end]).strip()
        if not section:
            continue
        if len(section) <= max_chunk_chars:
            # `section` already opens with the header line — do not re-add it.
            chunks.append(section)
            continue
        parts = _fixed_chunks(section, max_chunk_chars)
        if not parts:
            continue
        chunks.append(parts[0])
        # Continuations lose the header when the section is cut, so restore it.
        chunks.extend(f"{header}\n{part}" for part in parts[1:])
    return chunks


def _fixed_chunks(text: str, max_chunk_chars: int) -> list[str]:
    """Split text into overlapping fixed-size chunks, dropping empties."""
    if len(text) <= max_chunk_chars:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chunk_chars, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return chunks
