"""Tests for the deterministic offline embedding and ChromaDB vector store."""

from __future__ import annotations

import math

import numpy as np
import pytest

from agent.embeddings import ChromaVectorStore, HashEmbeddingFunction


def test_hash_embedding_is_deterministic_and_unit_norm() -> None:
    embedder = HashEmbeddingFunction(dim=128)

    first = embedder(["Apple raised guidance"])
    second = embedder(["Apple raised guidance"])

    assert np.array_equal(first, second)
    norm = float(np.linalg.norm(first[0]))
    assert norm == pytest.approx(1.0)


def test_hash_embedding_distinguishes_texts() -> None:
    embedder = HashEmbeddingFunction(dim=128)

    a = embedder(["Apple raised guidance"])
    b = embedder(["CEO resigned yesterday"])

    assert not np.array_equal(a, b)


def test_vector_store_add_and_query_finds_nearest() -> None:
    store = ChromaVectorStore(path=":memory:", collection_name="facts-a")
    store.add(
        ids=["f-1", "f-2", "f-3"],
        documents=[
            "Apple raised Q3 guidance above consensus.",
            "Apple announced a new dividend policy.",
            "TotalEnergies bought a refinery in Africa.",
        ],
        metadatas=[
            {"ticker": "AAPL"},
            {"ticker": "AAPL"},
            {"ticker": "TTE"},
        ],
    )

    hits = store.query(["Apple guidance"], n_results=2, where={"ticker": "AAPL"})

    assert len(hits) == 1
    ids = [hit["id"] for hit in hits[0]]
    assert "f-1" in ids
    assert "f-3" not in ids
    assert all("statement" in hit and "score" in hit for hit in hits[0])


def test_vector_store_query_without_filter_returns_all_scoped() -> None:
    store = ChromaVectorStore(path=":memory:", collection_name="facts-b")
    store.add(
        ids=["f-1", "f-2"],
        documents=["Apple raised guidance.", "SAP cut guidance."],
        metadatas=[{"ticker": "AAPL"}, {"ticker": "SAP"}],
    )

    hits = store.query(["raised guidance"], n_results=5)

    assert {hit["id"] for hit in hits[0]} == {"f-1", "f-2"}
