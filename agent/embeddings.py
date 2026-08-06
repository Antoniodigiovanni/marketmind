"""Deterministic offline embeddings and a ChromaDB vector store for facts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import chromadb
import numpy as np
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

DEFAULT_DIM = 512
DEFAULT_VECTOR_DIR = Path(__file__).resolve().parent.parent / "data" / "db" / "vectors"


class HashEmbeddingFunction(EmbeddingFunction[Documents]):
    """Chroma-compatible embedding function using hashed character n-grams.

    Deterministic and fully offline (no model downloads), which keeps the
    daily job dependency-free and tests hermetic. Semantic quality is a
    bag-of-words approximation; swap in a learned embedder by passing a
    different embedding function to ChromaVectorStore.
    """

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        """Initialise the embedding dimension."""
        self.dim = dim

    @staticmethod
    def name() -> str:
        """Return the embedder identifier used by Chroma registration."""
        return "hash-ngram"

    def get_config(self) -> dict[str, Any]:
        """Return the serialisable embedder configuration."""
        return {"dim": self.dim}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> HashEmbeddingFunction:
        """Rebuild the embedder from a serialised configuration."""
        return HashEmbeddingFunction(dim=int(config.get("dim", DEFAULT_DIM)))

    def __call__(self, input: Documents) -> Embeddings:
        """Embed a list of texts into L2-normalised fixed-size vectors."""
        vectors: list[list[float]] = []
        for text in input:
            vector = np.zeros(self.dim, dtype=np.float64)
            for n in (1, 2, 3):
                grams = (
                    [text[i : i + n] for i in range(max(len(text) - n + 1, 0))]
                    if len(text) >= n
                    else [text]
                )
                for gram in grams:
                    digest = hashlib.sha256(gram.encode("utf-8")).digest()[:8]
                    bucket = int.from_bytes(digest, "big") % self.dim
                    vector[bucket] += 1.0
            norm = float(np.linalg.norm(vector))
            if norm > 0:
                vector = vector / norm
            vectors.append(vector.tolist())
        return vectors


class ChromaVectorStore:
    """Minimal ChromaDB wrapper for fact embeddings.

    Documents carry metadata so searches can be scoped to a ticker (facts) or
    to a sector/geography key (aggregate facts). Uses an in-memory client when
    path is ":memory:", otherwise a persistent client at the given path.
    """

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        embedding_function: HashEmbeddingFunction | None = None,
        collection_name: str = "facts",
    ) -> None:
        """Initialise the Chroma client and the 'facts' collection."""
        if path is None:
            path = DEFAULT_VECTOR_DIR
        if str(path) == ":memory:":
            self._client: Any = chromadb.EphemeralClient()
        else:
            Path(path).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(path))
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=embedding_function or HashEmbeddingFunction(),
        )

    def add(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, str]] | None = None,
    ) -> None:
        """Upsert documents keyed by ids into the collection."""
        self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def query(
        self,
        query_texts: list[str],
        n_results: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[list[dict[str, object]]]:
        """Return the nearest documents for each query text.

        Args:
            query_texts: Natural-language queries.
            n_results: Number of neighbours to return per query.
            where: Optional Chroma metadata filter (e.g. {"ticker": "AAPL"}).

        Returns:
            One list per query, each item a dict with keys id, statement,
            score, and metadata.
        """
        result = self._collection.query(
            query_texts=query_texts,
            n_results=n_results,
            where=where,
        )
        grouped: list[list[dict[str, object]]] = []
        for ids, documents, distances, metadatas in zip(
            result.get("ids", []),
            result.get("documents", []),
            result.get("distances", []),
            result.get("metadatas", []),
            strict=False,
        ):
            grouped.append(
                [
                    {
                        "id": doc_id,
                        "statement": document,
                        "score": float(distance),
                        "metadata": metadata,
                    }
                    for doc_id, document, distance, metadata in zip(
                        ids, documents, distances, metadatas, strict=False
                    )
                ]
            )
        return grouped
