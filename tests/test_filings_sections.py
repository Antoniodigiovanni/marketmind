"""Tests for the SEC filing section chunker."""

from __future__ import annotations

from data.filings.parse.sections import split_sections

TWO_SECTIONS = (
    "Item 1A. Risk Factors\nWe face risks.\n\nItem 7. Management's Discussion\nRevenue grew.\n"
)


def test_bounds_chunks_by_item_headers() -> None:
    chunks = split_sections(TWO_SECTIONS)

    assert len(chunks) == 2
    assert chunks[0].startswith("Item 1A")
    assert "We face risks." in chunks[0]
    assert chunks[1].startswith("Item 7")
    assert "Revenue grew." in chunks[1]


def test_header_appears_exactly_once_per_chunk() -> None:
    """A section already opens with its header; prepending it again duplicates it."""
    chunks = split_sections(TWO_SECTIONS)

    assert chunks[0].count("Item 1A. Risk Factors") == 1
    assert chunks[1].count("Item 7. Management's Discussion") == 1


def test_falls_back_to_overlapping_chunks_without_headers() -> None:
    text = "word " * 5000  # no ITEM headers anywhere

    chunks = split_sections(text, max_chunk_chars=1000)

    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert chunks[0] == text[:1000].strip()
    assert chunks[0][-500:].strip() == chunks[1][:500].strip()  # 500-char overlap


def test_oversized_section_is_split_and_continuations_keep_the_header() -> None:
    """Continuation chunks lose the header when cut, so it is restored."""
    body = "risk " * 600  # ~3000 chars, well past the limit below
    text = f"Item 1A. Risk Factors\n{body}\n"

    chunks = split_sections(text, max_chunk_chars=500)

    assert len(chunks) > 1
    assert all(c.startswith("Item 1A. Risk Factors") for c in chunks)
    # The first chunk carries the header inline; it must not be doubled.
    assert chunks[0].count("Item 1A. Risk Factors") == 1


def test_blank_text_yields_no_chunks() -> None:
    assert split_sections("   ") == []


def test_short_text_without_headers_is_a_single_chunk() -> None:
    assert split_sections("A brief filing body.") == ["A brief filing body."]
