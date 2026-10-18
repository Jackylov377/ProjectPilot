"""Small-corpus dense retrieval and rank fusion; no vector database required."""

import math
from collections import defaultdict
from urllib.parse import urlsplit

import httpx

from app.rag.ingestion import Chunk
from app.rag.retrieval import BM25Index, SearchHit


class EmbeddingClient:
    """OpenAI-compatible embedding batches, using the explicitly configured provider."""

    def __init__(self, api_base: str, api_key: str, model="text-embedding-v4", dimensions=256):
        if not api_base or not api_key or dimensions < 1:
            raise ValueError("Configure the embedding endpoint, API key and positive dimensions")
        self.api_base, self.api_key = api_base.rstrip("/"), api_key
        self.model, self.dimensions = model, dimensions
        self.requests = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        with httpx.Client(
            base_url=self.api_base + "/",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        ) as client:
            for start in range(0, len(texts), 10):
                batch = texts[start : start + 10]
                response = client.post(
                    "embeddings",
                    json={
                        "model": self.model,
                        "input": batch,
                        "dimensions": self.dimensions,
                        "encoding_format": "float",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                ordered = sorted(payload["data"], key=lambda item: item["index"])
                if [item["index"] for item in ordered] != list(range(len(batch))):
                    raise ValueError("Embedding response does not match input order/count")
                if any(len(item["embedding"]) != self.dimensions for item in ordered):
                    raise ValueError("Embedding response has unexpected dimensions")
                vectors.extend(item["embedding"] for item in ordered)
                self.requests.append(
                    {
                        "inputs": len(batch),
                        "usage": payload.get("usage"),
                        "request_id": payload.get("id"),
                    }
                )
        return vectors

    def metadata(self):
        return {
            "destination_hostname": urlsplit(self.api_base).hostname,
            "model": self.model,
            "dimensions": self.dimensions,
            "requests": self.requests,
        }


def _normalize(vector):
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0 or not math.isfinite(norm):
        raise ValueError("Cosine similarity needs finite, nonzero vectors")
    return tuple(value / norm for value in vector)


class VectorIndex:
    """Exact cosine search over a single-workspace snapshot of chunk embeddings."""

    def __init__(self, chunks: list[Chunk], vectors: list[list[float]]):
        if len(chunks) != len(vectors) or len({len(vector) for vector in vectors}) > 1:
            raise ValueError("Provide one equally sized embedding per chunk")
        if len({chunk.workspace_id for chunk in chunks}) > 1:
            raise ValueError("Build a separate index for each workspace")
        self.chunks = tuple(chunks)
        self.vectors = tuple(_normalize(vector) for vector in vectors)

    def search(self, query_vector: list[float], top_k=5) -> list[SearchHit]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        if not self.chunks:
            return []
        if len(query_vector) != len(self.vectors[0]):
            raise ValueError("Query and document embeddings must have equal dimensions")
        query = _normalize(query_vector)
        scored = [
            SearchHit(chunk, sum(a * b for a, b in zip(vector, query)))
            for chunk, vector in zip(self.chunks, self.vectors)
        ]
        return sorted(
            scored,
            key=lambda hit: (
                -hit.score,
                hit.chunk.source,
                hit.chunk.start_offset,
                hit.chunk.chunk_id,
            ),
        )[:top_k]


class HybridIndex:
    """Equal-weight reciprocal rank fusion; cosine/BM25 magnitudes are not mixed."""

    def __init__(self, lexical: BM25Index, dense: VectorIndex, candidate_k=10, rank_constant=60):
        self.lexical, self.dense = lexical, dense
        self.candidate_k, self.rank_constant = candidate_k, rank_constant

    def search(self, query: str, query_vector: list[float], top_k=5) -> list[SearchHit]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        scores, chunks = defaultdict(float), {}
        for hits in (
            self.lexical.search(query, self.candidate_k),
            self.dense.search(query_vector, self.candidate_k),
        ):
            for rank, hit in enumerate(hits, 1):
                key = hit.chunk.chunk_id
                scores[key] += 1 / (self.rank_constant + rank)
                chunks[key] = hit.chunk
        ranked = sorted(
            scores,
            key=lambda key: (-scores[key], chunks[key].source, chunks[key].start_offset, key),
        )
        return [SearchHit(chunks[key], scores[key]) for key in ranked[:top_k]]
