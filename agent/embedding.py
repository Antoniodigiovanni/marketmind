"""Deterministic, fully offline embedding shared by every fact-writing path.

Not a port of agent/embeddings.py's ChromaVectorStore/HashEmbeddingFunction
-- those are deleted in the ingestion-pipeline cutover (sub-phase B). This
is a plain async function matching data/db/facts.py's and
data/db/aggregate_facts.py's `embed: Callable[[str], Awaitable[list[float]]]`
contract directly, at dim=1536 to match fact_embeddings.embedding's column
type (the original DuckDB+Chroma default was 512).
"""

from __future__ import annotations

import hashlib
import math

DIM = 1536


async def hash_embed(text: str) -> list[float]:
    """Embed text into a deterministic, L2-normalised 1536-dim vector.

    Character n-gram (1/2/3) hashing into fixed buckets -- a bag-of-words
    approximation, not semantic. Swapping in a real embedder later is a
    matter of passing a different callable wherever `embed` is required;
    this function's only job is to be a correct, dependency-free default.

    Args:
        text: The statement or query to embed.

    Returns:
        A 1536-element list of floats with L2 norm 1.0 (or all zeros for
        empty input, which is not L2-normalisable).
    """
    vector = [0.0] * DIM
    for n in (1, 2, 3):
        grams = (
            [text[i : i + n] for i in range(max(len(text) - n + 1, 0))]
            if len(text) >= n
            else [text]
        )
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()[:8]
            bucket = int.from_bytes(digest, "big") % DIM
            vector[bucket] += 1.0

    norm = math.sqrt(sum(v * v for v in vector))
    if norm > 0:
        vector = [v / norm for v in vector]
    return vector
