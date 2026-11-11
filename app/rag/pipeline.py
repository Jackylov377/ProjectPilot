"""Opt-in retrieval for the agent, with reusable vectors and bounded degradation."""

import asyncio
import hashlib
import json
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

from app.rag.rerank import RerankClient
from app.rag.vector import EmbeddingClient, HybridIndex, VectorIndex, _normalize
from app.redis_support import REDIS_URL, RedisUnavailable, RedisVectorCache

Retriever = Literal["bm25", "vector", "hybrid", "hybrid_rerank"]
RETRIEVERS = ("bm25", "vector", "hybrid", "hybrid_rerank")
MAX_DENSE_CHUNKS = 256
SERVICE_ERRORS = (
    httpx.HTTPError,
    TimeoutError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    OverflowError,
)


def retrieval_secrets():
    return tuple(
        os.getenv(f"PROJECTPILOT_{prefix}API_KEY", "").strip()
        for prefix in ("", "EMBEDDING_", "RERANK_")
    )


def configured_clients(strategy: str, *, allow_remote: bool):
    """Validate explicit configuration without making network requests."""
    if strategy not in RETRIEVERS:
        raise ValueError("不支持的检索方式。")
    if strategy == "bm25":
        return None, None
    if not allow_remote:
        raise ValueError("语义、混合检索和重排仅支持 live 模式；离线模式请使用 BM25。")

    def settings(kind):
        prefix = f"PROJECTPILOT_{kind}_"
        values = [os.getenv(prefix + name, "").strip() for name in ("API_BASE", "API_KEY", "MODEL")]
        if not all(values):
            raise ValueError(f"请配置 {prefix}API_BASE、API_KEY 和 MODEL。")
        try:
            url = urlsplit(values[0])
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{prefix}API_BASE 必须是无凭据、查询参数和片段的 HTTP(S) 地址。")
        return values

    embedding_settings = settings("EMBEDDING")
    try:
        dimensions = int(os.getenv("PROJECTPILOT_EMBEDDING_DIMENSIONS", "256"))
        if not 1 <= dimensions <= 8192:
            raise ValueError()
    except ValueError:
        raise ValueError("PROJECTPILOT_EMBEDDING_DIMENSIONS 必须为 1～8192 的整数。") from None
    embedding = EmbeddingClient(*embedding_settings, dimensions=dimensions, timeout_seconds=8)
    reranker = (
        RerankClient(*settings("RERANK"), timeout_seconds=5)
        if strategy == "hybrid_rerank"
        else None
    )
    return embedding, reranker


class RetrievalPipeline:
    def __init__(self, lexical, strategy, trace, *, embedding=None, reranker=None, cache_dir=None):
        self.lexical, self.requested, self.effective = lexical, strategy, strategy
        self.trace, self.embedding, self.reranker = trace, embedding, reranker
        self.dense = None
        self.warnings = []
        self.lock = asyncio.Lock()
        self.cache_dir = None
        self.redis_cache = None
        self.cache_hits = self.cache_misses = 0
        self.redis_cache_hits = 0
        if embedding:
            # Endpoint, model and dimensions are part of the embedding space identity.
            namespace = json.dumps([embedding.api_base, embedding.model, embedding.dimensions])
            self.cache_namespace = hashlib.sha256(namespace.encode()).hexdigest()
            if cache_dir is not None:
                self.cache_dir = Path(cache_dir) / self.cache_namespace
            if url := os.getenv(REDIS_URL):
                self.redis_cache = RedisVectorCache(url)

    @staticmethod
    def _chunk_key(chunk):
        # Chunk identity includes workspace, document content version and source span.
        identity = f"{chunk.workspace_id}:{chunk.chunk_id}"
        return hashlib.sha256(identity.encode()).hexdigest()

    def _cache_path(self, chunk):
        return self.cache_dir / (self._chunk_key(chunk) + ".json")

    def _redis_key(self, chunk):
        return f"projectpilot:embedding:v1:{self.cache_namespace}:{self._chunk_key(chunk)}"

    def _valid_vector(self, vector):
        if not isinstance(vector, list) or len(vector) != self.embedding.dimensions:
            raise ValueError("Unexpected vector dimensions")
        _normalize(vector)
        return vector

    def _read_vector(self, chunk):
        if self.cache_dir is None:
            return None
        try:
            return self._valid_vector(
                json.loads(self._cache_path(chunk).read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError, OverflowError):
            return None

    def _write_vector(self, chunk, vector):
        if self.cache_dir is None:
            return
        temporary = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.cache_dir,
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(vector, handle, allow_nan=False)
            temporary.replace(self._cache_path(chunk))
        except OSError as exc:
            self.trace.emit("knowledge.cache_unavailable", error=type(exc).__name__)
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def _redis_unavailable(self):
        self.redis_cache = None
        self.trace.emit("knowledge.redis_cache_unavailable")

    def _write_shared(self, items):
        if self.redis_cache and items:
            try:
                self.redis_cache.write([(self._redis_key(chunk), vector) for chunk, vector in items])
            except RedisUnavailable:
                self._redis_unavailable()

    def _read_vectors(self, chunks):
        vectors = [None] * len(chunks)
        if self.redis_cache:
            try:
                entries = self.redis_cache.read([self._redis_key(chunk) for chunk in chunks])
            except RedisUnavailable:
                self._redis_unavailable()
            else:
                for i, entry in enumerate(entries):
                    if entry is not None:
                        try:
                            vectors[i] = self._valid_vector(json.loads(entry))
                            self.redis_cache_hits += 1
                        except (ValueError, TypeError, OverflowError):
                            pass
        local_hits = []
        for i, chunk in enumerate(chunks):
            if vectors[i] is None:
                vectors[i] = self._read_vector(chunk)
                if vectors[i] is not None:
                    local_hits.append((chunk, vectors[i]))
        self._write_shared(local_hits)
        return vectors

    def _write_vectors(self, items):
        for chunk, vector in items:
            self._write_vector(chunk, vector)
        self._write_shared(items)

    async def _build_dense(self):
        chunks = list(self.lexical.chunks)
        vectors = await asyncio.to_thread(self._read_vectors, chunks)
        missing = [i for i, vector in enumerate(vectors) if vector is None]
        self.cache_hits += len(vectors) - len(missing)
        self.cache_misses += len(missing)
        for start in range(0, len(missing), 10):
            indexes = missing[start : start + 10]
            batch = await asyncio.to_thread(
                self.embedding.embed, [chunks[i].text for i in indexes]
            )
            for i, vector in zip(indexes, batch, strict=True):
                vectors[i] = self._valid_vector(vector)
            await asyncio.to_thread(self._write_vectors, [(chunks[i], vectors[i]) for i in indexes])
        self.dense = VectorIndex(chunks, vectors)

    def _fallback(self, strategy, reason):
        self.effective = strategy
        message = f"{reason}；本回合已回退到 {strategy} 检索。"
        self.warnings.append(message)
        self.trace.emit(
            "knowledge.retrieval_fallback",
            requested=self.requested,
            effective=strategy,
            reason=reason,
        )

    async def search(self, query, top_k):
        # The upstream loop can issue parallel tools; share one index build per turn.
        async with self.lock:
            started = time.perf_counter()
            hits = await self._search(query, top_k)
            self.trace.emit(
                "knowledge.retrieval_completed",
                requested=self.requested,
                effective=self.effective,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                cache_hits=self.cache_hits,
                cache_misses=self.cache_misses,
                redis_cache_hits=self.redis_cache_hits,
                # These request lists and cache counters are cumulative within the turn.
                embedding=self.embedding.metadata() if self.embedding else None,
                rerank=self.reranker.metadata() if self.reranker else None,
                warnings=self.warnings,
            )
            return hits

    async def _search(self, query, top_k):
        if self.effective == "bm25" or not self.lexical.chunks:
            return self.lexical.search(query, top_k)
        if len(self.lexical.chunks) > MAX_DENSE_CHUNKS:
            self._fallback("bm25", f"当前选中文档超过 {MAX_DENSE_CHUNKS} 个片段的语义检索上限")
            return self.lexical.search(query, top_k)
        try:
            async with asyncio.timeout(15):
                if self.dense is None:
                    await self._build_dense()
                vector = (await asyncio.to_thread(self.embedding.embed, [query]))[0]
                if self.effective == "vector":
                    return self.dense.search(vector, top_k)
                candidates = HybridIndex(self.lexical, self.dense).search(
                    query, vector, 10 if self.effective == "hybrid_rerank" else top_k
                )
        except SERVICE_ERRORS as exc:
            self._fallback("bm25", f"语义检索不可用（{type(exc).__name__}）")
            return self.lexical.search(query, top_k)
        if self.effective == "hybrid_rerank":
            try:
                async with asyncio.timeout(6):
                    return await asyncio.to_thread(self.reranker.rerank, query, candidates, top_k)
            except SERVICE_ERRORS as exc:
                self._fallback("hybrid", f"重排服务不可用（{type(exc).__name__}）")
        return candidates[:top_k]
