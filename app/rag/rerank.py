"""Rerank retrieved candidates while preserving their original evidence objects."""

import math
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.rag.retrieval import SearchHit


class RerankClient:
    def __init__(self, api_base: str, api_key: str, model="gte-rerank-v2", timeout_seconds=30):
        if not api_base or not api_key:
            raise ValueError("Configure the rerank endpoint and API key")
        parts = urlsplit(api_base)
        self.url = urlunsplit(
            (parts.scheme, parts.netloc, "/api/v1/services/rerank/text-rerank/text-rerank", "", "")
        )
        self.api_key, self.model = api_key, model
        self.requests = []
        self.attempts = 0
        self.timeout_seconds = timeout_seconds

    def rerank(self, query: str, candidates: list[SearchHit], top_k=3) -> list[SearchHit]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        if not candidates:
            return []
        with httpx.Client(
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {self.api_key}"},
        ) as client:
            self.attempts += 1
            response = client.post(
                self.url,
                json={
                    "model": self.model,
                    "input": {"query": query, "documents": [hit.chunk.text for hit in candidates]},
                    "parameters": {"top_n": min(top_k, len(candidates)), "return_documents": False},
                },
            )
            response.raise_for_status()
            payload = response.json()
        results = payload["output"]["results"]
        indexes = [item["index"] for item in results]
        if (
            len(indexes) != min(top_k, len(candidates))
            or len(set(indexes)) != len(indexes)
            or any(index < 0 or index >= len(candidates) for index in indexes)
            or any(not math.isfinite(item["relevance_score"]) for item in results)
        ):
            raise ValueError("Rerank response does not map to the supplied candidates")
        self.requests.append(
            {
                "query": query,
                "candidate_chunk_ids": [hit.chunk.chunk_id for hit in candidates],
                "results": [
                    {"index": item["index"], "score": item["relevance_score"]} for item in results
                ],
                "usage": payload.get("usage"),
                "request_id": payload.get("request_id"),
            }
        )
        return [
            SearchHit(candidates[item["index"]].chunk, item["relevance_score"]) for item in results
        ]

    def metadata(self):
        return {
            "destination_hostname": urlsplit(self.url).hostname,
            "model": self.model,
            "requests": self.requests,
            "attempts": self.attempts,
        }
