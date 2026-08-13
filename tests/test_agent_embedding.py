"""Tests for the shared deterministic hash embedder."""

from __future__ import annotations

import math

import pytest

from agent.embedding import hash_embed

pytestmark = pytest.mark.asyncio


async def test_hash_embed_returns_1536_dimensions() -> None:
    vector = await hash_embed("Revenue grew 12% YoY")
    assert len(vector) == 1536


async def test_hash_embed_is_deterministic() -> None:
    first = await hash_embed("Revenue grew 12% YoY")
    second = await hash_embed("Revenue grew 12% YoY")
    assert first == second


async def test_hash_embed_differs_for_different_text() -> None:
    a = await hash_embed("Revenue grew 12% YoY")
    b = await hash_embed("New CEO appointed")
    assert a != b


async def test_hash_embed_is_l2_normalised() -> None:
    vector = await hash_embed("Revenue grew 12% YoY")
    norm = math.sqrt(sum(v * v for v in vector))
    assert norm == pytest.approx(1.0, abs=1e-6)
