"""Lightweight HTML-to-text extraction for EDGAR documents."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP_TAGS = {"script", "style", "head", "title", "meta", "link"}
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style/head content."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """Convert an HTML document to normalized plain text.

    Args:
        raw: HTML document.

    Returns:
        Text with tags removed, script/style/head content dropped, and
        whitespace collapsed to single lines.
    """
    extractor = _TextExtractor()
    extractor.feed(raw)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(extractor.parts).splitlines()]
    return "\n".join(line for line in lines if line)
